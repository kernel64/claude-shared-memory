#!/usr/bin/env python3
"""Shared memory for Claude Code: one knowledge base for all projects.

One entry = one Markdown file with a small front matter, stored in SMEM_DIR
(default: ~/.claude-shared-memory). This single file provides:
  smem.py mcp                  MCP server (stdio, JSON-RPC)
  smem.py hook-session-start   SessionStart hook: lists entries for this project
  smem.py hook-prompt          UserPromptSubmit hook: suggests matching entries
  smem.py search <query> | list | info | detect [dir]   small CLI for humans
Standard library only.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path

VERSION = "0.1.0"
TYPES = ("gotcha", "technique", "tool", "optimization", "snippet", "reference")
FIELD_ORDER = ("id", "title", "summary", "type", "tags", "stack", "source", "origin",
               "created", "updated", "verified", "archived", "archive_reason")

# Prompt hook: minimum relevance before an entry is suggested.
PROMPT_MIN_SCORE = 3.5
PROMPT_MAX_ENTRIES = 3
SESSION_MAX_RELEVANT = 25
SESSION_MAX_GENERAL = 10


class ToolError(Exception):
    pass


# --------------------------------------------------------------------------- store

def store_dir() -> Path:
    d = os.environ.get("SMEM_DIR") or str(Path.home() / ".claude-shared-memory")
    p = Path(d).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def today() -> str:
    return _dt.date.today().isoformat()


_FM = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z", re.S)


def _parse_value(v: str):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        return [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if v.startswith('"'):
        try:
            return json.loads(v)
        except ValueError:
            return v.strip('"')
    return v


def parse_entry(text: str):
    m = _FM.match(text)
    if not m:
        return {}, text.strip()
    meta = {}
    for line in m.group(1).splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = _parse_value(v)
    return meta, m.group(2).strip()


def render_entry(meta: dict, body: str) -> str:
    lines = ["---"]
    keys = list(FIELD_ORDER) + [k for k in meta if k not in FIELD_ORDER and not k.startswith("_")]
    for k in keys:
        v = meta.get(k)
        if v is None or v == "" or v == []:
            continue
        if isinstance(v, bool):
            s = "true" if v else "false"
        elif isinstance(v, (list, tuple)):
            s = "[" + ", ".join(str(x) for x in v) + "]"
        elif k in ("title", "summary", "origin", "archive_reason"):
            s = json.dumps(str(v), ensure_ascii=False)
        else:
            s = str(v)
        lines.append(f"{k}: {s}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.strip() + "\n"


def load_entries() -> list[dict]:
    out = []
    for f in sorted(store_dir().glob("*.md")):
        if f.name.lower() in ("readme.md", "index.md"):
            continue
        try:
            meta, body = parse_entry(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        meta["id"] = f.stem  # the file name is the id
        for k in ("tags", "stack"):
            v = meta.get(k)
            meta[k] = [v] if isinstance(v, str) and v else (v if isinstance(v, list) else [])
        meta.setdefault("title", f.stem)
        meta.setdefault("summary", "")
        meta.setdefault("type", "")
        meta["_path"] = f
        meta["_body"] = body
        out.append(meta)
    return out


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _run(args, timeout=10):
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=flags)


def git_commit(paths, message: str) -> None:
    """Best effort: commit the touched files if the store is in a git repo."""
    d = str(store_dir())
    try:
        if _run(["git", "-C", d, "rev-parse", "--is-inside-work-tree"], 5).returncode != 0:
            return
        ps = [str(p) for p in paths]
        _run(["git", "-C", d, "add", "-A", "--", *ps])
        _run(["git", "-C", d, "commit", "-q", "-m", message, "--", *ps])
    except Exception:
        pass


# --------------------------------------------------------------------------- text

STOP = set("""
a an the and or of to in on for with without from by at as is are was were be been it its this that these
those not no yes do does did how what why when where which who can could should would will i you we they he
she my your our me us them there here then than so if but about into over under up out use using used get
le la les un une des du de d l et ou en au aux dans sur pour par avec sans est sont etre ce cet cette ces qui
que quoi comment pourquoi quand il elle ils elles je tu nous vous on ne pas plus mon ma mes ton ta tes son sa
ses notre votre leur leurs se y fait faire faut veux peux peut moi toi lui ok oui non si mais donc car tout
tous toute toutes tres bien aussi encore deja ca cela comme avoir ai as avez ont suis es etes sera
""".split())


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def tokens(text: str) -> list[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")  # camelCase -> camel Case
    raw = re.findall(r"[a-z0-9][a-z0-9+#]*", _fold(text).lower())
    out = []
    prev = ""
    for t in raw:
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        parts = re.findall(r"[a-z]+|[0-9]+", t)
        if len(parts) > 1:  # xbox360 -> xbox360, xbox, 360
            out.extend(p for p in parts if len(p) >= 2 and p not in STOP)
        if prev and prev.isalpha() and t.isdigit():  # "xbox 360" -> also xbox360
            out.append(prev + t)
        if len(t) >= 2 and t not in STOP:
            out.append(t)
        prev = t
    return out


def entry_tokens(e: dict) -> list[str]:
    return (tokens(e.get("title", "")) * 3 + tokens(e.get("summary", "")) * 2
            + tokens(" ".join(e["tags"] + e["stack"])) * 3 + tokens(e.get("_body", "")))


def rank(query: str, entries: list[dict], k1=1.2, b=0.75):
    """BM25. Returns ([(score, hits, entry)], number_of_query_terms)."""
    q = list(dict.fromkeys(tokens(query)))
    if not q or not entries:
        return [], len(q)
    docs = [Counter(entry_tokens(e)) for e in entries]
    lens = [sum(d.values()) for d in docs]
    avg = (sum(lens) / len(lens)) or 1.0
    n = len(docs)
    df = {t: sum(1 for d in docs if t in d) for t in q}
    res = []
    for e, d, length in zip(entries, docs, lens):
        score, hits = 0.0, 0
        for t in q:
            tf = d.get(t, 0)
            if not tf:
                continue
            hits += 1
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / avg))
        if hits:
            res.append((score, hits, e))
    res.sort(key=lambda r: (-r[0], -r[1]))
    return res, len(q)


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", _fold(title).lower()).strip("-")
    return s[:60].rstrip("-") or "entry"


def clean_tags(values) -> list[str]:
    if isinstance(values, str):
        values = re.split(r"[,\s]+", values)
    out = []
    for v in values or []:
        t = re.sub(r"[^a-z0-9.+#-]+", "-", _fold(str(v)).lower()).strip("-.")
        if t and t not in out:
            out.append(t)
    return out


# --------------------------------------------------------------------------- secrets

SECRET_PATTERNS = [
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("AWS access key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    ("API key (sk-...)", r"\bsk-[A-Za-z0-9_-]{20,}"),
    ("Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("JWT", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
    ("credentials in a URL", r"\b[a-z][a-z0-9+.-]*://(?![<{$%])[^\s/:@]+:(?![<{$%*])[^\s/@]+@"),
    ("password or secret value",
     r"(?i)\b(?:password|passwd|pwd|passphrase|mot de passe|secret|api[_ -]?key|access[_ -]?key|"
     r"auth[_ -]?token|token)\b[\"']?\s*[:=]\s*[\"']?(?![<{$\[(*%])[^\s\"',;<>]{4,}"),
]
SENSITIVE_PATTERNS = [
    ("private IP address", r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    ("email address", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
]


def scan(text: str):
    def found(patterns):
        hits = []
        for name, pat in patterns:
            m = re.search(pat, text)
            if m:
                s = m.group(0)
                hits.append(f"{name} ('{s[:6]}...')" if len(s) > 8 else f"{name} ('{s}')")
        return hits
    return found(SECRET_PATTERNS), found(SENSITIVE_PATTERNS)


def _check_text(text: str, allow_sensitive: bool) -> None:
    blocked, sensitive = scan(text)
    if blocked:
        raise ToolError("Refused: the text looks like it contains a secret: " + "; ".join(blocked)
                        + ". Shared memory must never hold credentials. Replace values with placeholders "
                          "such as <PASSWORD> or ${API_KEY} and try again.")
    if sensitive and not allow_sensitive:
        raise ToolError("The text contains machine- or person-specific data: " + "; ".join(sensitive)
                        + ". Shared memory is for reusable knowledge: replace these with placeholders, "
                          "or pass allow_sensitive=true if they really are needed.")


# --------------------------------------------------------------------------- formatting

def fmt_line(e: dict) -> str:
    labels = e["stack"] + [t for t in e["tags"] if t not in e["stack"]]
    s = f"- [{e['id']}] {e['title']}"
    meta = ", ".join(([e["type"]] if e.get("type") else []) + labels[:6])
    if meta:
        s += f" ({meta})"
    if e.get("summary"):
        s += f": {e['summary']}"
    return s


def _filter(entries, stack=None, type_=None, tag=None):
    if stack:
        st = clean_tags([stack])[0] if clean_tags([stack]) else ""
        entries = [e for e in entries if st in e["stack"] or st in e["tags"]]
    if type_:
        entries = [e for e in entries if e.get("type") == type_]
    if tag:
        tg = clean_tags([tag])[0] if clean_tags([tag]) else ""
        entries = [e for e in entries if tg in e["tags"] or tg in e["stack"]]
    return entries


def _by_id(entry_id: str) -> dict:
    for e in load_entries():
        if e["id"] == entry_id:
            return e
    raise ToolError(f"No entry with id '{entry_id}'. Use memory_search or memory_list to find ids.")


# --------------------------------------------------------------------------- tools

def t_search(query: str, stack=None, type=None, limit=8) -> str:
    entries = _filter(load_entries(), stack, type)
    if not entries:
        return "The shared memory has no entry matching these filters."
    ranked, _ = rank(query or "", entries)
    if not ranked:
        return f"No entry matches '{query}' ({len(entries)} entries searched). Try other words or memory_list."
    limit = max(1, min(int(limit or 8), 30))
    lines = [fmt_line(e) for _, _, e in ranked[:limit]]
    return (f"{len(ranked)} matching entries, best first:\n" + "\n".join(lines)
            + "\n\nRead the full entries with memory_get before relying on them.")


def t_get(ids) -> str:
    if isinstance(ids, str):
        ids = [ids]
    by = {e["id"]: e for e in load_entries()}
    parts = []
    for i in ids or []:
        e = by.get(i)
        if not e:
            parts.append(f"## {i}\nNot found.")
        else:
            parts.append(f"## {i}  (file: {e['_path']})\n\n" + e["_path"].read_text(encoding="utf-8"))
    return "\n\n".join(parts) or "No id given."


def _find_duplicates(title: str, summary: str, entries, skip_id=None):
    q = set(tokens(f"{title} {summary}"))
    dups = []
    for e in entries:
        if e["id"] == skip_id:
            continue
        et = set(tokens(f"{e['title']} {e.get('summary', '')}"))
        common = q & et
        if q and len(common) >= 2 and len(common) / len(q) >= 0.6:
            dups.append(e)
    return dups


def t_add(title, summary, body, type, tags=None, stack=None, source=None, origin=None,
          verified=True, force=False, allow_sensitive=False) -> str:
    title, summary, body = (title or "").strip(), " ".join((summary or "").split()), (body or "").strip()
    if not title or not summary or not body:
        raise ToolError("title, summary and body are all required.")
    if type not in TYPES:
        raise ToolError(f"type must be one of: {', '.join(TYPES)}.")
    tags, stack = clean_tags(tags), clean_tags(stack)
    _check_text("\n".join([title, summary, body, " ".join(tags + stack)]), allow_sensitive)
    entries = load_entries()
    if not force:
        dups = _find_duplicates(title, summary, entries)
        if dups:
            return ("Nothing written: similar entries already exist:\n" + "\n".join(fmt_line(e) for e in dups)
                    + "\n\nRead them (memory_get). If one covers the same topic, extend it with memory_update "
                      "(append=...). If the new knowledge is really different, call memory_add again with force=true.")
    existing = {e["id"] for e in entries}
    base = slugify(title)
    new_id, n = base, 2
    while new_id in existing or (store_dir() / f"{new_id}.md").exists():
        new_id, n = f"{base}-{n}", n + 1
    meta = {"id": new_id, "title": title, "summary": summary, "type": type, "tags": tags, "stack": stack,
            "source": (source or "").strip(), "origin": (origin or "").strip(), "created": today(),
            "verified": bool(verified)}
    path = store_dir() / f"{new_id}.md"
    _write_atomic(path, render_entry(meta, body))
    git_commit([path], f"add: {new_id}" + (f" (from {meta['source']})" if meta["source"] else ""))
    return f"Saved [{new_id}] to {path}"


def t_update(id, title=None, summary=None, body=None, append=None, type=None, tags=None, stack=None,
             verified=None, from_project=None, allow_sensitive=False) -> str:
    e = _by_id(id)
    meta, old_body = parse_entry(e["_path"].read_text(encoding="utf-8"))
    new_text = "\n".join(x for x in (title, summary, body, append) if x)
    _check_text(new_text + " " + " ".join(clean_tags(tags) + clean_tags(stack)), allow_sensitive)
    if type is not None and type not in TYPES:
        raise ToolError(f"type must be one of: {', '.join(TYPES)}.")
    if title:
        meta["title"] = title.strip()
    if summary:
        meta["summary"] = " ".join(summary.split())
    if type:
        meta["type"] = type
    if tags is not None:
        meta["tags"] = clean_tags(tags)
    if stack is not None:
        meta["stack"] = clean_tags(stack)
    if verified is not None:
        meta["verified"] = bool(verified)
    new_body = body.strip() if body else old_body
    if append:
        who = f" ({from_project})" if from_project else ""
        new_body = new_body.rstrip() + f"\n\n**Update {today()}{who}:** {append.strip()}"
    meta["id"] = e["id"]
    meta["updated"] = today()
    _write_atomic(e["_path"], render_entry(meta, new_body))
    git_commit([e["_path"]], f"update: {e['id']}" + (f" (from {from_project})" if from_project else ""))
    return f"Updated [{e['id']}]."


def t_delete(id, reason) -> str:
    if not (reason or "").strip():
        raise ToolError("Give a reason (wrong, obsolete, duplicate of X...).")
    e = _by_id(id)
    meta, body = parse_entry(e["_path"].read_text(encoding="utf-8"))
    meta["archived"] = today()
    meta["archive_reason"] = reason.strip()
    arch = store_dir() / "_archive"
    arch.mkdir(exist_ok=True)
    dest = arch / e["_path"].name
    _write_atomic(dest, render_entry(meta, body))
    e["_path"].unlink()
    git_commit([e["_path"], dest], f"archive: {e['id']} ({reason.strip()[:60]})")
    return f"Archived [{e['id']}] to {dest}."


def t_list(stack=None, type=None, tag=None) -> str:
    entries = _filter(load_entries(), stack, type, tag)
    if not entries:
        return "No entry." if not (stack or type or tag) else "No entry matches these filters."
    groups: dict[str, list] = {}
    for e in entries:
        groups.setdefault(", ".join(e["stack"]) or "general", []).append(e)
    out = [f"{len(entries)} entries."]
    for g in sorted(groups):
        out.append(f"\n## {g}")
        out.extend(fmt_line(e) for e in sorted(groups[g], key=lambda x: x["title"].lower()))
    return "\n".join(out)


def t_info() -> str:
    entries = load_entries()
    d = store_dir()
    types = Counter(e.get("type") or "?" for e in entries)
    stacks = Counter(s for e in entries for s in e["stack"])
    archived = len(list((d / "_archive").glob("*.md"))) if (d / "_archive").is_dir() else 0
    try:
        is_git = _run(["git", "-C", str(d), "rev-parse", "--is-inside-work-tree"], 5).returncode == 0
    except Exception:
        is_git = False
    return "\n".join([
        f"Store directory: {d}",
        f"Git versioned: {'yes' if is_git else 'no'}",
        f"Entries: {len(entries)} (archived: {archived})",
        "By type: " + (", ".join(f"{k} {v}" for k, v in types.most_common()) or "-"),
        "Stacks: " + (", ".join(f"{k} {v}" for k, v in stacks.most_common()) or "-"),
        f"Harvest log (origins already reviewed): {d / '.harvest-reviewed.txt'}",
    ])


TOOLS = [
    {
        "name": "memory_search",
        "description": (
            "Search the shared cross-project knowledge base (gotchas, workarounds, tools, techniques, "
            "optimizations learned in the user's other projects). Call it BEFORE investigating a non-trivial "
            "problem from scratch: build/toolchain errors, tool quirks, deployment, performance, platform "
            "limits. Use concrete keywords (tool names, error messages, platform). Returns one line per entry; "
            "read the full entry with memory_get."),
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Keywords, an error message, or a short question."},
            "stack": {"type": "string", "description": "Optional: keep only entries for this stack/tag (e.g. flutter, xbox360)."},
            "type": {"type": "string", "enum": list(TYPES)},
            "limit": {"type": "integer", "description": "Max results (default 8)."}},
            "required": ["query"]},
    },
    {
        "name": "memory_get",
        "description": "Read full shared-memory entries by id (ids come from memory_search, memory_list or the session-start list).",
        "inputSchema": {"type": "object", "properties": {
            "ids": {"type": "array", "items": {"type": "string"}}}, "required": ["ids"]},
    },
    {
        "name": "memory_add",
        "description": (
            "Save a reusable piece of knowledge to the shared cross-project memory. Only for knowledge that would "
            "save time in ANOTHER project: a gotcha and its fix, a workaround, a useful tool and how to run it, a "
            "technique, an optimization with measured gain. Not for project-only decisions or status. Write it "
            "generalized (no 'in this repo'), keep exact error messages, commands and versions (they make it "
            "searchable). Never include secrets, credentials, hostnames or personal data. Body layout: "
            "'## Context' (when it applies, symptoms), '## Solution' (steps/commands), '## Why' (cause, evidence), "
            "optional '## Limits'. Search first: if a similar entry exists, use memory_update instead."),
        "inputSchema": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Short, specific, searchable (e.g. 'XEX over 48 MB fails to launch from Aurora')."},
            "summary": {"type": "string", "description": "One line: the key fact or fix."},
            "body": {"type": "string", "description": "Markdown body (Context / Solution / Why / Limits)."},
            "type": {"type": "string", "enum": list(TYPES)},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Lowercase keywords: tools, libs, error names."},
            "stack": {"type": "array", "items": {"type": "string"},
                      "description": "Platforms/stacks it applies to (e.g. xbox360, flutter, nestjs, esp32, android, windows). Empty = general."},
            "source": {"type": "string", "description": "Project folder name where it was learned."},
            "origin": {"type": "string", "description": "Optional: path of the project memory file it came from (used by harvest)."},
            "verified": {"type": "boolean", "description": "true if tested/measured, false if only believed. Default true."},
            "force": {"type": "boolean", "description": "Write even if similar entries exist."},
            "allow_sensitive": {"type": "boolean", "description": "Allow private IPs/emails when truly needed."}},
            "required": ["title", "summary", "body", "type", "tags"]},
    },
    {
        "name": "memory_update",
        "description": (
            "Update a shared-memory entry: fix it, generalize it, or append a new finding (append= adds a dated "
            "note). Use it when an entry is incomplete, wrong, or confirmed/contradicted in another project."),
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"}, "summary": {"type": "string"},
            "body": {"type": "string", "description": "Replaces the whole body."},
            "append": {"type": "string", "description": "Text added at the end as a dated update."},
            "type": {"type": "string", "enum": list(TYPES)},
            "tags": {"type": "array", "items": {"type": "string"}},
            "stack": {"type": "array", "items": {"type": "string"}},
            "verified": {"type": "boolean"},
            "from_project": {"type": "string", "description": "Project folder name making the update."},
            "allow_sensitive": {"type": "boolean"}},
            "required": ["id"]},
    },
    {
        "name": "memory_delete",
        "description": "Archive a shared-memory entry that is wrong, obsolete or a duplicate (it is moved to _archive, not destroyed). Ask the user first.",
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["id", "reason"]},
    },
    {
        "name": "memory_list",
        "description": "List shared-memory entries (one line each), grouped by stack, optionally filtered.",
        "inputSchema": {"type": "object", "properties": {
            "stack": {"type": "string"}, "type": {"type": "string", "enum": list(TYPES)}, "tag": {"type": "string"}}},
    },
    {
        "name": "memory_info",
        "description": "Shared-memory store location, counts by type and stack, harvest log path.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

HANDLERS = {
    "memory_search": lambda a: t_search(a.get("query", ""), a.get("stack"), a.get("type"), a.get("limit", 8)),
    "memory_get": lambda a: t_get(a.get("ids")),
    "memory_add": lambda a: t_add(**{k: a[k] for k in (
        "title", "summary", "body", "type", "tags", "stack", "source", "origin", "verified", "force",
        "allow_sensitive") if k in a}),
    "memory_update": lambda a: t_update(**{k: a[k] for k in (
        "id", "title", "summary", "body", "append", "type", "tags", "stack", "verified", "from_project",
        "allow_sensitive") if k in a}),
    "memory_delete": lambda a: t_delete(a.get("id", ""), a.get("reason", "")),
    "memory_list": lambda a: t_list(a.get("stack"), a.get("type"), a.get("tag")),
    "memory_info": lambda a: t_info(),
}

INSTRUCTIONS = (
    "Shared memory = the user's knowledge base shared by all their projects. Search it (memory_search) before "
    "investigating a non-trivial problem from scratch. When you solve something reusable elsewhere, offer to "
    "save it (memory_add, or the /shared-memory:learn skill). Generalized wording, no secrets, no project-only facts.")


# --------------------------------------------------------------------------- MCP server

def _handle(msg: dict):
    method, mid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if mid is None:  # notification
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "shared-memory", "version": VERSION},
                  "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool {name}"}}
        try:
            result = {"content": [{"type": "text", "text": fn(args)}], "isError": False}
        except ToolError as ex:
            result = {"content": [{"type": "text", "text": str(ex)}], "isError": True}
        except Exception as ex:  # never crash the server
            result = {"content": [{"type": "text", "text": f"Internal error: {type(ex).__name__}: {ex}"}],
                      "isError": True}
    elif method in ("resources/list", "prompts/list"):
        result = {method.split("/")[0]: []}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def mcp_main() -> None:
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        msgs = msg if isinstance(msg, list) else [msg]
        replies = [r for r in (_handle(m) for m in msgs if isinstance(m, dict)) if r is not None]
        if replies:
            payload = replies if isinstance(msg, list) else replies[0]
            out.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            out.flush()


# --------------------------------------------------------------------------- project detection

MARKERS = {
    "pubspec.yaml": ["flutter", "dart"], "package.json": ["node", "javascript"], "tsconfig.json": ["typescript"],
    "CMakeLists.txt": ["cpp", "cmake"], "platformio.ini": ["platformio", "embedded", "arduino"],
    "Cargo.toml": ["rust"], "go.mod": ["go"], "pyproject.toml": ["python"], "requirements.txt": ["python"],
    "setup.py": ["python"], "composer.json": ["php"], "Gemfile": ["ruby"], "Dockerfile": ["docker"],
    "docker-compose.yml": ["docker"], "docker-compose.yaml": ["docker"], "compose.yaml": ["docker"],
    "build.gradle": ["gradle"], "build.gradle.kts": ["gradle", "kotlin"], "settings.gradle": ["gradle"],
    "settings.gradle.kts": ["gradle", "kotlin"], "monkey.jungle": ["garmin", "monkey-c"], "hugo.toml": ["hugo"],
    "AndroidManifest.xml": ["android"], "artisan": ["laravel", "php"],
}
GLOB_MARKERS = {
    "*.sln": ["cpp", "msvc", "visual-studio"], "*.vcxproj": ["cpp", "msvc", "visual-studio"],
    "*.csproj": ["csharp", "dotnet"], "*.ino": ["arduino", "embedded"], "*.xcodeproj": ["ios", "xcode"],
    "*.xex": ["xbox360"],
}
DEP_TAGS = {
    "react": "react", "next": "nextjs", "vue": "vue", "@nestjs/core": "nestjs", "express": "express",
    "vite": "vite", "electron": "electron", "typescript": "typescript", "prisma": "prisma",
    "@prisma/client": "prisma", "react-native": "react-native", "expo": "expo", "pg": "postgresql",
    "typeorm": "typeorm", "mongoose": "mongodb", "tailwindcss": "tailwind", "puppeteer": "puppeteer",
}
SKIP_DIRS = {"node_modules", "build", "dist", "out", "bin", "obj", "target", "vendor", "__pycache__"}


def _tag_pattern(tag: str):
    runs = re.findall(r"[a-z]+|[0-9]+|[^a-z0-9]", tag)
    parts = [re.escape(r) if not r.isalnum() else r for r in runs if r not in "-_."]
    if not parts:
        return None
    return re.compile(r"(?<![a-z0-9])" + r"[\s_.-]?".join(parts) + r"(?![a-z0-9])")


def _project_memory_dir(cwd: Path) -> Path:
    mangled = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))  # same naming as ~/.claude/projects
    return Path.home() / ".claude" / "projects" / mangled / "memory"


def _read_head(p: Path, n: int) -> str:
    try:
        with open(p, encoding="utf-8", errors="ignore") as f:
            return f.read(n)
    except OSError:
        return ""


def detect_project(cwd: Path, vocabulary) -> list[str]:
    found: list[str] = []

    def add(ts):
        for t in ts:
            if t not in found:
                found.append(t)

    dirs = [cwd]
    try:
        dirs += [d for d in cwd.iterdir() if d.is_dir() and not d.name.startswith(".") and d.name not in SKIP_DIRS][:40]
    except OSError:
        pass
    for d in dirs:
        for name, ts in MARKERS.items():
            if (d / name).exists():
                add(ts)
        for pat, ts in GLOB_MARKERS.items():
            try:
                if next(d.glob(pat), None):
                    add(ts)
            except OSError:
                pass
        pkg = d / "package.json"
        if pkg.exists():
            try:
                data = json.loads(_read_head(pkg, 200_000) or "{}")
                deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                add([v for k, v in DEP_TAGS.items() if k in deps])
            except ValueError:
                pass
        manifest = d / "manifest.json"
        if manifest.exists() and '"manifest_version"' in _read_head(manifest, 20_000):
            add(["chrome-extension"])
    if (cwd / "app" / "src" / "main" / "AndroidManifest.xml").exists() or (cwd / "android").is_dir():
        add(["android"])
    if (cwd / "ios").is_dir():
        add(["ios"])
    if (cwd / ".github" / "workflows").is_dir():
        add(["github-actions"])

    # Stack words mentioned in the project's own docs and memory.
    text = " ".join([
        cwd.name,
        _read_head(cwd / "CLAUDE.md", 30_000), _read_head(cwd / ".claude" / "CLAUDE.md", 30_000),
        _read_head(cwd / "README.md", 8_000), _read_head(_project_memory_dir(cwd) / "MEMORY.md", 20_000),
    ]).lower()
    for tag in vocabulary:
        if tag in found or len(tag) < 2:
            continue
        pat = _tag_pattern(tag)
        if pat and pat.search(text):
            add([tag])
    return found


# --------------------------------------------------------------------------- hooks

def _hook_input() -> dict:
    try:
        return json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except Exception:
        return {}


def _emit(event: str, text: str) -> None:
    payload = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()


def _state_file(session_id: str) -> Path:
    sid = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "") or "nosession"
    return Path(tempfile.gettempdir()) / f"claude-smem-{sid}.json"


def _state_load(session_id: str) -> set:
    try:
        return set(json.loads(_state_file(session_id).read_text(encoding="utf-8")))
    except Exception:
        return set()


def _state_save(session_id: str, ids: set) -> None:
    try:
        _state_file(session_id).write_text(json.dumps(sorted(ids)), encoding="utf-8")
    except OSError:
        pass


def session_context(cwd: Path, entries: list[dict]):
    """Returns (text, listed_ids)."""
    if not entries:
        return ("# Shared memory (cross-project knowledge base)\n"
                "The shared memory is empty for now. When you solve something reusable in other projects, offer "
                "to save it with /shared-memory:learn. To import knowledge from existing project memories, the "
                "user can run /shared-memory:harvest."), set()
    vocab = sorted({s for e in entries for s in e["stack"]})
    detected = detect_project(cwd, vocab)
    det = set(detected)
    scored = []
    for e in entries:
        s = 2 * len(det & set(e["stack"])) + len(det & set(e["tags"]))
        if s and e["stack"]:
            scored.append((s, e.get("updated") or e.get("created") or "", e))
        elif s and not e["stack"]:
            scored.append((s - 0.5, e.get("updated") or e.get("created") or "", e))
    scored.sort(key=lambda x: x[1], reverse=True)  # newest first...
    scored.sort(key=lambda x: -x[0])  # ...within the same relevance (stable sort)
    relevant = [e for _, _, e in scored][:SESSION_MAX_RELEVANT]
    rel_ids = {e["id"] for e in relevant}
    general = sorted((e for e in entries if not e["stack"] and e["id"] not in rel_ids),
                     key=lambda e: e.get("updated") or e.get("created") or "", reverse=True)[:SESSION_MAX_GENERAL]
    listed = rel_ids | {e["id"] for e in general}
    lines = ["# Shared memory (cross-project knowledge base)",
             f"{len(entries)} entries learned across the user's projects. "
             f"Detected for this project: {', '.join(detected) or 'nothing specific'}."]
    if relevant:
        lines.append("\nEntries for this project's stack (titles only: call memory_get with the id before relying on one):")
        lines += [fmt_line(e) for e in relevant]
    if general:
        lines.append("\nGeneral entries:")
        lines += [fmt_line(e) for e in general]
    rest = len(entries) - len(listed)
    if rest > 0:
        lines.append(f"\n({rest} more entries not listed: use memory_search.)")
    lines.append("\nSearch the shared memory before investigating a non-trivial problem from scratch. When you solve "
                 "something reusable in other projects, offer /shared-memory:learn (generalized, no secrets).")
    return "\n".join(lines), listed


def hook_session_start() -> None:
    data = _hook_input()
    cwd = Path(data.get("cwd") or os.getcwd())
    text, listed = session_context(cwd, load_entries())
    _state_save(data.get("session_id", ""), listed)  # fresh list on startup/resume/clear/compact
    _emit("SessionStart", text)


def prompt_suggestions(prompt: str, entries: list[dict], seen: set) -> list[dict]:
    p = (prompt or "").strip()
    if len(p) < 15 or p.startswith("/") or not entries:
        return []
    ranked, nq = rank(p[:4000], entries)
    picked = []
    for score, hits, e in ranked:
        if e["id"] in seen:
            continue
        if hits >= 2 and score >= PROMPT_MIN_SCORE and (hits / max(nq, 1) >= 0.25 or hits >= 3):
            picked.append(e)
        if len(picked) >= PROMPT_MAX_ENTRIES:
            break
    return picked


def hook_prompt() -> None:
    data = _hook_input()
    sid = data.get("session_id", "")
    seen = _state_load(sid)
    picked = prompt_suggestions(data.get("prompt", ""), load_entries(), seen)
    if not picked:
        return
    _state_save(sid, seen | {e["id"] for e in picked})
    _emit("UserPromptSubmit",
          "Shared-memory entries (from the user's other projects) that may be relevant to this request. "
          "Read them with memory_get if useful; ignore them if not:\n" + "\n".join(fmt_line(e) for e in picked))


# --------------------------------------------------------------------------- CLI

def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "mcp"
    if cmd == "mcp":
        mcp_main()
        return 0
    if cmd in ("hook-session-start", "hook-prompt"):
        try:
            hook_session_start() if cmd == "hook-session-start" else hook_prompt()
        except Exception:
            pass  # a hook must never break the session
        return 0
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if cmd == "search":
        print(t_search(" ".join(argv[2:])))
    elif cmd == "list":
        print(t_list())
    elif cmd == "info":
        print(t_info())
    elif cmd == "detect":
        cwd = Path(argv[2]) if len(argv) > 2 else Path.cwd()
        entries = load_entries()
        print("Detected:", detect_project(cwd, sorted({s for e in entries for s in e["stack"]})))
        print()
        print(session_context(cwd, entries)[0])
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
