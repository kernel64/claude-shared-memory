#!/usr/bin/env python3
"""Shared memory for Claude Code: one knowledge base for all projects.

One entry = one Markdown file with a small front matter, stored in SMEM_DIR
(default: ~/.claude-shared-memory). This single file provides:
  smem.py mcp                  MCP server (stdio, JSON-RPC)
  smem.py hook-session-start   SessionStart hook: lists entries for this project
  smem.py hook-prompt          UserPromptSubmit hook: suggests matching entries
  smem.py context [--cwd DIR] [--agent NAME] [--prompt TEXT]   same context for any agent or script
  smem.py search <query> | list | info | validate | maintain | detect [dir]   small CLI for humans
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
import time
import unicodedata
from collections import Counter
from pathlib import Path

VERSION = "0.1.0"
TYPES = ("gotcha", "technique", "tool", "optimization", "snippet", "reference")
FIELD_ORDER = ("id", "title", "summary", "type", "tags", "stack", "source", "origin",
               "created", "updated", "confidence", "status", "superseded_by", "last_verified", "review_after",
               "agent", "session", "verified", "archived", "archive_reason")

# Trust metadata (all optional). v0.1 entries only have `verified`: true reads as "observed"
# (it was the default, so it does not prove a test), false or missing as "assumed".
CONFIDENCES = ("assumed", "observed", "tested", "user-confirmed")
CONFIDENCE_WEIGHT = {"assumed": 0.85, "observed": 1.0, "tested": 1.1, "user-confirmed": 1.15}
STATUSES = ("active", "needs-review", "superseded")
NEEDS_REVIEW_WEIGHT = 0.9
DATE_FIELDS = ("created", "updated", "last_verified", "review_after", "archived")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Prompt hook. Tuned on bench/ (see bench/run_bench.py): an entry is suggested only if
#  - its BM25 score is >= PROMPT_MIN_SCORE and >= PROMPT_REL_SCORE x the best score,
#  - it matches >= 2 query terms, covering >= PROMPT_MIN_COVERAGE of the IDF mass of the
#    query terms the store knows (unknown words are ignored, so long prompts are not penalized).
PROMPT_MIN_SCORE = 3.5
PROMPT_REL_SCORE = 0.7
PROMPT_MIN_COVERAGE = 0.6
PROMPT_MAX_DEFAULT = 1          # SMEM_PROMPT_MAX overrides it
CONTEXT_BUDGET_DEFAULT = 600    # SMEM_CONTEXT_BUDGET overrides it (tokens, estimated as chars / 4)
SUMMARY_MAX_CHARS = 160         # session-start lines only

# Stacks that describe the machine / agent rather than the project. They are detected from
# the system, not from project files. An entry made only of these applies to every project.
ENV_STACKS = {"windows", "linux", "macos", "wsl", "claude-code"}

LOCK_NAME = ".smem.lock"


class ToolError(Exception):
    pass


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "")))
    except ValueError:
        return default


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
    block_key = None  # key whose value is a YAML block list ("tags:" then "  - a" lines)
    for line in m.group(1).splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("- ") and block_key is not None:
            meta[block_key].append(str(_parse_value(s[2:])).strip("\"'"))
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip()
        if v.strip():
            meta[k] = _parse_value(v)
            block_key = None
        else:
            meta[k] = []
            block_key = k
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
        out.append(normalize_entry(meta, body, f))
    return out


def _as_list(v) -> list:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [x.strip() for x in v.split(",") if x.strip()]
    return []


def _as_str(v) -> str:
    return v if isinstance(v, str) else ""


_SOURCE_NAMES: dict[str, str] = {}
_MANGLED = re.compile(r"^[A-Za-z]--")


def normalize_source(name: str) -> str:
    """'E--vs-projects-Xenon64' (Claude Code's project folder naming) -> 'Xenon64'.
    The real path is read from that project's transcripts; otherwise only the drive prefix is dropped."""
    name = name.strip()
    if not _MANGLED.match(name):
        return name
    if name not in _SOURCE_NAMES:
        real = ""
        folder = Path.home() / ".claude" / "projects" / name
        try:
            for transcript in sorted(folder.glob("*.jsonl"))[:3]:
                with open(transcript, encoding="utf-8", errors="ignore") as fh:
                    for _, line in zip(range(30), fh):
                        m = re.search(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"', line)
                        if m:
                            real = json.loads(f'"{m.group(1)}"')
                            break
                if real:
                    break
        except OSError:
            pass
        base = re.split(r"[\\/]", real.rstrip("\\/"))[-1] if real else ""
        _SOURCE_NAMES[name] = base or name[3:]
    return _SOURCE_NAMES[name]


def effective_confidence(meta: dict) -> str:
    c = meta.get("confidence")
    if c in CONFIDENCES:
        return c
    v = meta.get("verified")
    return "observed" if v is True else "assumed"


def effective_status(meta: dict) -> str:
    s = meta.get("status")
    if s not in STATUSES:
        s = "superseded" if _as_str(meta.get("superseded_by")) else "active"
    review = _as_str(meta.get("review_after"))
    if s == "active" and _DATE.match(review) and review < today():
        s = "needs-review"
    return s


def normalize_entry(meta: dict, body: str, path: Path) -> dict:
    """Loaded view of an entry: v0.1 files get defaults here, the file itself is never rewritten."""
    e = dict(meta)
    e["_raw_id"] = meta.get("id")
    e["id"] = path.stem  # the file name is the id
    for k in ("tags", "stack"):
        e[k] = _as_list(meta.get(k))
    for k in ("title", "summary", "type"):
        e[k] = _as_str(meta.get(k))
    e["title"] = e["title"] or path.stem
    e["source"] = [normalize_source(s) for s in _as_list(meta.get("source"))]
    e["confidence"] = effective_confidence(meta)
    e["status"] = effective_status(meta)
    e["superseded_by"] = _as_str(meta.get("superseded_by"))
    e["_path"] = path
    e["_body"] = body
    return e


def is_superseded(e: dict) -> bool:
    return e.get("status") == "superseded"


def trust_weight(e: dict) -> float:
    w = CONFIDENCE_WEIGHT.get(e.get("confidence"), 1.0)
    return w * (NEEDS_REVIEW_WEIGHT if e.get("status") == "needs-review" else 1.0)


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _run(args, timeout=10):
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=flags)


class StoreLock:
    """Cross-process lock (one lock file in the store) around every write and its git commit.

    Several Claude Code sessions run their own MCP server on the same store; without it,
    concurrent writes race on git's index.lock and commits get lost."""

    def __init__(self, timeout: float = 15.0, stale_after: float = 60.0):
        self.path = store_dir() / LOCK_NAME
        self.timeout = timeout
        self.stale_after = stale_after

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()}\n".encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale_after:
                        self.path.unlink()  # left behind by a crashed process
                        continue
                except FileNotFoundError:
                    continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise ToolError("The store is busy (another session is writing to it). Try again in a moment.")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass
        return False


def _is_git_repo(d: str) -> bool:
    try:
        return _run(["git", "-C", d, "rev-parse", "--is-inside-work-tree"], 5).returncode == 0
    except Exception:
        return False  # git not installed


def git_commit(paths, message: str) -> str:
    """Commit the touched files if the store is a git repo. Returns "" or a warning for the user."""
    d = str(store_dir())
    if not _is_git_repo(d):
        return ""
    ps = [str(p) for p in paths]
    error = "unknown error"
    try:
        for attempt in range(4):
            r = _run(["git", "-C", d, "add", "-A", "--", *ps])
            if r.returncode == 0:
                r = _run(["git", "-C", d, "commit", "-q", "-m", message, "--", *ps])
                out = (r.stdout or "") + (r.stderr or "")
                if r.returncode == 0 or "nothing to commit" in out or "nothing added to commit" in out:
                    return ""
            else:
                out = r.stderr or ""
            error = (out.strip().splitlines() or ["unknown error"])[0][:200]
            if "index.lock" not in out:
                break
            time.sleep(0.3 * (attempt + 1))
    except Exception as ex:
        error = f"{type(ex).__name__}: {ex}"
    return (f"\nWarning: the file was saved, but the git commit failed ({error}). "
            "memory_info lists uncommitted files.")


def uncommitted_files() -> list[str] | None:
    """Entry files with changes not committed, or None if the store is not a git repo."""
    d = str(store_dir())
    if not _is_git_repo(d):
        return None
    try:
        r = _run(["git", "-C", d, "status", "--porcelain", "--untracked-files=all"], 10)
    except Exception:
        return None
    files = []
    for line in r.stdout.splitlines():
        name = line[3:].strip().strip('"')
        if name.endswith(".md") or name.startswith(".harvest"):
            files.append(name)
    return files


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


def score_query(query: str, entries: list[dict], k1=1.2, b=0.75) -> list[dict]:
    """BM25, best first. Each result: score, hits (matched query terms), coverage (share of the
    IDF mass of the query terms present in the store that this entry matches), entry."""
    q = list(dict.fromkeys(tokens(query)))
    if not q or not entries:
        return []
    docs = [Counter(entry_tokens(e)) for e in entries]
    lens = [sum(d.values()) for d in docs]
    avg = (sum(lens) / len(lens)) or 1.0
    n = len(docs)
    df = {t: sum(1 for d in docs if t in d) for t in q}
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q}
    known_mass = sum(idf[t] for t in q if df[t]) or 1.0
    res = []
    for e, d, length in zip(entries, docs, lens):
        score, matched = 0.0, []
        for t in q:
            tf = d.get(t, 0)
            if tf:
                matched.append(t)
                score += idf[t] * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / avg))
        if matched:
            res.append({"score": score, "hits": len(matched),
                        "coverage": sum(idf[t] for t in matched) / known_mass, "entry": e})
    res.sort(key=lambda r: (-r["score"], -r["hits"]))
    return res


def rank(query: str, entries: list[dict], k1=1.2, b=0.75):
    """BM25. Returns ([(score, hits, entry)], number_of_query_terms)."""
    nq = len(dict.fromkeys(tokens(query)))
    return [(r["score"], r["hits"], r["entry"]) for r in score_query(query, entries, k1, b)], nq


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

_PLACEHOLDER = re.compile(r"(?i)your|example|placeholder|dummy|xxxx|\*\*\*|redacted|changeme|secret_here|"
                          r"_here$|^<|^\$|^\{|^%|^\[|^\(")


def _looks_like_secret_value(value: str) -> bool:
    """For 'password: X' style matches: ignore placeholders and plain words ('token: required')."""
    v = value.strip().strip("\"'`")
    if not v or _PLACEHOLDER.search(v):
        return False
    if re.fullmatch(r"[A-Za-z_]+", v) and len(v) < 12:
        return False
    return True


# (name, regex, value check on group 1 or None). Matches are reported truncated, never in full.
SECRET_PATTERNS = [
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----|PuTTY-User-Key-File", None),
    ("AWS access key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", None),
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}", None),
    ("GitLab token", r"\bglpat-[A-Za-z0-9_-]{20,}", None),
    ("API key (sk-...)", r"\bsk-[A-Za-z0-9_-]{20,}", None),
    ("Stripe key", r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}|\bwhsec_[A-Za-z0-9]{20,}", None),
    ("npm token", r"\bnpm_[A-Za-z0-9]{36}\b", None),
    ("Hugging Face token", r"\bhf_[A-Za-z0-9]{30,}", None),
    ("Slack token or webhook", r"\bxox[abprs]-[A-Za-z0-9-]{10,}|hooks\.slack\.com/services/T\w+/B\w+/\w+", None),
    ("Discord webhook", r"discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{20,}", None),
    ("Telegram bot token", r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}", None),
    ("SendGrid key", r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}", None),
    ("Google API key or OAuth secret", r"\bAIza[0-9A-Za-z_-]{35}\b|\bGOCSPX-[A-Za-z0-9_-]{20,}", None),
    ("Azure key or SAS signature", r"AccountKey=[A-Za-z0-9+/=]{20,}|[?&]sig=[A-Za-z0-9%+/=]{20,}", None),
    ("JWT", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}", None),
    ("credentials in a URL", r"\b[a-z][a-z0-9+.-]*://(?![<{$%])[^\s/:@]+:(?![<{$%*])[^\s/@]+@", None),
    ("secret in an env/config line",
     r"(?m)^\s*(?:export\s+|set\s+)?[A-Z][A-Z0-9_]*(?:SECRET|PASSWORD|PASSWD|TOKEN|API_KEY|APIKEY|PRIVATE_KEY|"
     r"ACCESS_KEY)[A-Z0-9_]*\s*=\s*[\"']?([^\s\"'#]+)", _looks_like_secret_value),
    ("password or secret value",
     r"(?i)\b(?:password|passwd|pwd|passphrase|mot de passe|secret|api[_ -]?key|access[_ -]?key|"
     r"auth[_ -]?token|token)\b[\"']?\s*[:=]\s*[\"']?([^\s\"',;<>]{4,})", _looks_like_secret_value),
]
SENSITIVE_PATTERNS = [
    ("private IP address", r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b(?!\.\d)", None),
    ("email address", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b", None),
]


def scan(text: str):
    def found(patterns):
        hits = []
        for name, pat, check in patterns:
            for m in re.finditer(pat, text):
                if check and not check(m.group(1)):
                    continue
                s = m.group(0)
                hits.append(f"{name} ('{s[:6]}...')" if len(s) > 8 else f"{name} ('{s}')")
                break
        return hits
    return found(SECRET_PATTERNS), found(SENSITIVE_PATTERNS)


MAX_BODY_CHARS = 4000
MAX_APPEND_CHARS = 2000
_LOG_LINE = re.compile(r"^\s*(?:\[?\d{2,4}[-/:.]\d{2}[-/:.]\d{2}|\d{2}:\d{2}:\d{2}|at \S+\(|File \"|"
                       r"(?:DEBUG|INFO|WARN|WARNING|ERROR|TRACE)\b|#\d+\s+0x[0-9a-f]+)")


def _check_size(text: str, limit: int, what: str) -> None:
    """Shared memory holds findings, not logs or conversation dumps."""
    if len(text) > limit:
        raise ToolError(f"Refused: the {what} is {len(text)} characters (limit {limit}). Keep one finding per entry: "
                        "the context, the fix, the reason, and only the exact error line that makes it searchable.")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 20 and sum(1 for ln in lines if _LOG_LINE.match(ln)) >= 0.4 * len(lines):
        raise ToolError(f"Refused: the {what} looks like a log or stack trace dump. Quote only the key error line "
                        "and describe the fix.")


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

def trust_marker(e: dict) -> str:
    """Short warning shown next to an entry, only when there is something to warn about."""
    if e.get("status") == "superseded":
        return f" [superseded by {e['superseded_by']}]" if e.get("superseded_by") else " [superseded]"
    marks = []
    if e.get("confidence") == "assumed":
        marks.append("unverified")
    if e.get("status") == "needs-review":
        marks.append("needs review")
    return f" [{', '.join(marks)}]" if marks else ""


def fmt_line(e: dict) -> str:
    labels = e["stack"] + [t for t in e["tags"] if t not in e["stack"]]
    s = f"- [{e['id']}]{trust_marker(e)} {e['title']}"
    meta = ", ".join(([e["type"]] if e.get("type") else []) + labels[:6])
    if meta:
        s += f" ({meta})"
    if e.get("source"):
        s += f" [from {', '.join(e['source'][:3])}]"
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

def search_entries(query: str, entries: list[dict], include_superseded=False) -> list[dict]:
    """BM25 results re-weighted by trust (confidence, review status), best first."""
    if not include_superseded:
        entries = [e for e in entries if not is_superseded(e)]
    results = score_query(query or "", entries)
    for r in results:
        r["score"] *= trust_weight(r["entry"])
    results.sort(key=lambda r: (-r["score"], -r["hits"]))
    return results


def t_search(query: str, stack=None, type=None, limit=8, include_superseded=False) -> str:
    entries = _filter(load_entries(), stack, type)
    if not entries:
        return "The shared memory has no entry matching these filters."
    ranked = search_entries(query, entries, bool(include_superseded))
    if not ranked:
        return f"No entry matches '{query}' ({len(entries)} entries searched). Try other words or memory_list."
    limit = max(1, min(int(limit or 8), 30))
    lines = [fmt_line(r["entry"]) for r in ranked[:limit]]
    return (f"{len(ranked)} matching entries, best first:\n" + "\n".join(lines)
            + "\n\nRead the full entries with memory_get before relying on them. [unverified] means nobody "
              "tested it yet: check before relying on it.")


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


CLIENT_NAME = ""  # MCP client (agent) name, from the initialize request


def _default_source() -> str:
    """The project folder the agent runs in (MCP servers are started in the project directory)."""
    cwd = Path.cwd()
    if cwd in (Path.home(), store_dir()) or cwd.parent == cwd:
        return ""
    return cwd.name


def _check_choice(value, choices, name):
    if value is not None and value not in choices:
        raise ToolError(f"{name} must be one of: {', '.join(choices)}.")


def _check_date(value, name):
    if value and not _DATE.match(str(value)):
        raise ToolError(f"{name} must be a date like 2026-12-31.")


def _confidence_from(confidence, verified):
    """Explicit confidence wins; the legacy verified flag maps true -> tested, false -> assumed."""
    if confidence is not None:
        return confidence
    if verified is not None:
        return "tested" if verified else "assumed"
    return None


def t_add(title, summary, body, type, tags=None, stack=None, source=None, origin=None,
          verified=None, confidence=None, review_after=None, session=None,
          force=False, allow_sensitive=False) -> str:
    title, summary, body = (title or "").strip(), " ".join((summary or "").split()), (body or "").strip()
    if not title or not summary or not body:
        raise ToolError("title, summary and body are all required.")
    if type not in TYPES:
        raise ToolError(f"type must be one of: {', '.join(TYPES)}.")
    confidence = _confidence_from(confidence, verified) or "observed"
    _check_choice(confidence, CONFIDENCES, "confidence")
    _check_date(review_after, "review_after")
    tags, stack = clean_tags(tags), clean_tags(stack)
    _check_size(body, MAX_BODY_CHARS, "body")
    _check_text("\n".join([title, summary, body, " ".join(tags + stack)]), allow_sensitive)
    with StoreLock():  # duplicate check, id choice, write and commit must not interleave
        entries = [e for e in load_entries() if not is_superseded(e)]
        if not force:
            dups = _find_duplicates(title, summary, entries)
            if dups:
                return ("Nothing written: similar entries already exist:\n" + "\n".join(fmt_line(e) for e in dups)
                        + "\n\nRead them (memory_get). If one covers the same topic, extend it with memory_update "
                          "(append=...). If the new knowledge is really different, call memory_add again with "
                          "force=true.")
        existing = {e["id"] for e in load_entries()}
        base = slugify(title)
        new_id, n = base, 2
        while new_id in existing or (store_dir() / f"{new_id}.md").exists():
            new_id, n = f"{base}-{n}", n + 1
        src = (source or "").strip() or _default_source()
        meta = {"id": new_id, "title": title, "summary": summary, "type": type, "tags": tags, "stack": stack,
                "source": src, "origin": (origin or "").strip(), "created": today(), "confidence": confidence,
                "review_after": review_after or "", "agent": CLIENT_NAME, "session": (session or "").strip()}
        path = store_dir() / f"{new_id}.md"
        _write_atomic(path, render_entry(meta, body))
        warning = git_commit([path], f"add: {new_id}" + (f" (from {src})" if src else ""))
    return f"Saved [{new_id}] ({confidence}) to {path}" + warning


def t_update(id, title=None, summary=None, body=None, append=None, type=None, tags=None, stack=None,
             verified=None, confidence=None, status=None, superseded_by=None, review_after=None, confirm=False,
             from_project=None, allow_sensitive=False) -> str:
    new_text = "\n".join(x for x in (title, summary, body, append) if x)
    if body:
        _check_size(body, MAX_BODY_CHARS, "body")
    if append:
        _check_size(append, MAX_APPEND_CHARS, "appended text")
    _check_text(new_text + " " + " ".join(clean_tags(tags) + clean_tags(stack)), allow_sensitive)
    if type is not None and type not in TYPES:
        raise ToolError(f"type must be one of: {', '.join(TYPES)}.")
    confidence = _confidence_from(confidence, verified)
    _check_choice(confidence, CONFIDENCES, "confidence")
    _check_choice(status, STATUSES, "status")
    _check_date(review_after, "review_after")
    if superseded_by is not None and status not in (None, "superseded"):
        raise ToolError("superseded_by only goes with status 'superseded'.")
    with StoreLock():  # read-modify-write: another session must not update the same entry in between
        return _update_locked(id, title, summary, body, append, type, tags, stack, confidence, status,
                              superseded_by, review_after, confirm, from_project)


def _update_locked(id, title, summary, body, append, type, tags, stack, confidence, status, superseded_by,
                   review_after, confirm, from_project) -> str:
    e = _by_id(id)
    meta, old_body = parse_entry(e["_path"].read_text(encoding="utf-8"))
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
    if confidence:
        meta["confidence"] = confidence
        meta.pop("verified", None)  # superseded by the explicit confidence
    if superseded_by:
        if superseded_by == e["id"]:
            raise ToolError("An entry cannot supersede itself.")
        if not (store_dir() / f"{superseded_by}.md").exists():
            raise ToolError(f"superseded_by: no entry with id '{superseded_by}'.")
        meta["superseded_by"] = superseded_by
        status = "superseded"
    if status:
        meta["status"] = status
        if status != "superseded":
            meta.pop("superseded_by", None)
    if review_after is not None:
        meta["review_after"] = review_after  # "" clears it
    if confirm:
        meta["last_verified"] = today()
        if meta.get("status") == "needs-review":
            meta["status"] = "active"
        if review_after is None and _as_str(meta.get("review_after")) and meta["review_after"] < today():
            meta["review_after"] = ""  # the review is done
    new_body = body.strip() if body else old_body
    if append:
        who = f" ({from_project})" if from_project else ""
        new_body = new_body.rstrip() + f"\n\n**Update {today()}{who}:** {append.strip()}"
    meta["id"] = e["id"]
    meta["updated"] = today()
    _write_atomic(e["_path"], render_entry(meta, new_body))
    warning = git_commit([e["_path"]], f"update: {e['id']}" + (f" (from {from_project})" if from_project else ""))
    return f"Updated [{e['id']}]." + warning


def t_delete(id, reason) -> str:
    if not (reason or "").strip():
        raise ToolError("Give a reason (wrong, obsolete, duplicate of X...).")
    with StoreLock():
        e = _by_id(id)
        meta, body = parse_entry(e["_path"].read_text(encoding="utf-8"))
        meta["archived"] = today()
        meta["archive_reason"] = reason.strip()
        arch = store_dir() / "_archive"
        arch.mkdir(exist_ok=True)
        dest = arch / e["_path"].name
        _write_atomic(dest, render_entry(meta, body))
        e["_path"].unlink()
        warning = git_commit([e["_path"], dest], f"archive: {e['id']} ({reason.strip()[:60]})")
    return f"Archived [{e['id']}] to {dest}." + warning


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
    pending = uncommitted_files()
    if pending is None:
        git_line = "Git versioned: no"
    elif pending:
        shown = ", ".join(pending[:10]) + (f" (+{len(pending) - 10} more)" if len(pending) > 10 else "")
        git_line = f"Git versioned: yes. Uncommitted files ({len(pending)}): {shown}"
    else:
        git_line = "Git versioned: yes (everything committed)"
    conf = Counter(e["confidence"] for e in entries)
    status = Counter(e["status"] for e in entries)
    return "\n".join([
        f"Store directory: {d}",
        git_line,
        f"Entries: {len(entries)} (archived: {archived})",
        "By type: " + (", ".join(f"{k} {v}" for k, v in types.most_common()) or "-"),
        "By confidence: " + (", ".join(f"{k} {conf[k]}" for k in CONFIDENCES if conf[k]) or "-"),
        "By status: " + (", ".join(f"{k} {status[k]}" for k in STATUSES if status[k]) or "-"),
        "Stacks: " + (", ".join(f"{k} {v}" for k, v in stacks.most_common()) or "-"),
        f"Context budget: {_env_int('SMEM_CONTEXT_BUDGET', CONTEXT_BUDGET_DEFAULT)} tokens at session start, "
        f"{_env_int('SMEM_PROMPT_MAX', PROMPT_MAX_DEFAULT)} suggestion(s) max per prompt",
        f"Harvest log (origins already reviewed): {d / '.harvest-reviewed.txt'}",
    ])


def validate_store() -> tuple[int, list[str], list[str]]:
    """Read-only check of every entry file. Returns (files checked, problems, notices)."""
    d = store_dir()
    files = [f for f in sorted(d.glob("*.md")) if f.name.lower() not in ("readme.md", "index.md")]
    ids = {f.stem for f in files}
    problems, notices = [], []
    for f in files:
        def bad(msg):
            problems.append(f"{f.name}: {msg}")
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as ex:
            bad(f"unreadable ({type(ex).__name__})")
            continue
        if not _FM.match(text):
            bad("no front matter (the file must start with a '---' block)")
            continue
        meta, body = parse_entry(text)
        for k in ("title", "summary", "type"):
            if not _as_str(meta.get(k)).strip():
                bad(f"missing {k}")
        if meta.get("type") and meta.get("type") not in TYPES:
            bad(f"unknown type '{meta.get('type')}' (expected {', '.join(TYPES)})")
        if "confidence" in meta and meta["confidence"] not in CONFIDENCES:
            bad(f"unknown confidence '{meta['confidence']}' (expected {', '.join(CONFIDENCES)})")
        if "status" in meta and meta["status"] not in STATUSES:
            bad(f"unknown status '{meta['status']}' (expected {', '.join(STATUSES)})")
        if "verified" in meta and not isinstance(meta["verified"], bool):
            bad("verified must be true or false")
        for k in DATE_FIELDS:
            v = meta.get(k)
            if v not in (None, "", []) and not _DATE.match(str(v)):
                bad(f"{k} is not a date like 2026-12-31: '{v}'")
        for k in ("tags", "stack"):
            v = meta.get(k)
            if isinstance(v, str) and v.strip():
                bad(f"{k} should be a list like [a, b], got '{v}'")
        sup = _as_str(meta.get("superseded_by"))
        if sup and sup not in ids:
            bad(f"superseded_by points to a missing entry '{sup}'")
        if sup == f.stem:
            bad("superseded_by points to itself")
        raw_id = meta.get("id")
        if raw_id not in (None, "", []) and raw_id != f.stem:
            bad(f"id '{raw_id}' differs from the file name (the file name wins)")
        if not body.strip():
            bad("empty body")
        e = normalize_entry(meta, body, f)
        if e["status"] == "needs-review":
            notices.append(f"{f.name}: needs review (review_after {meta.get('review_after') or '-'})")
    return len(files), problems, notices


def t_validate() -> str:
    n, problems, notices = validate_store()
    out = [f"{n} entries checked: {len(problems)} problem(s)."]
    out += [f"- {p}" for p in problems]
    if notices:
        out.append(f"\n{len(notices)} entr{'y' if len(notices) == 1 else 'ies'} to review:")
        out += [f"- {x}" for x in notices]
    return "\n".join(out)


# --------------------------------------------------------------------------- maintenance (read-only report)

# Cosine similarity of TF-IDF vectors (title, summary and tags weighted as in search). Tuned on bench/.
DUPLICATE_SIMILARITY = 0.5
RELATED_SIMILARITY = 0.25
OLD_ASSUMED_DAYS = 90
LONG_BODY_CHARS = MAX_BODY_CHARS  # older or hand-written entries may exceed the write limit
MAX_PAIRS = 20


def tfidf_vectors(entries: list[dict]) -> list[dict]:
    docs = [Counter(entry_tokens(e)) for e in entries]
    n = len(docs)
    df = Counter(t for d in docs for t in d)
    vecs = []
    for d in docs:
        v = {t: (1 + math.log(c)) * math.log(1 + n / df[t]) for t, c in d.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs.append({t: x / norm for t, x in v.items()})
    return vecs


def cosine(a: dict, b: dict) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(t, 0.0) for t, x in a.items())


def similar_pairs(entries: list[dict]) -> list[tuple[float, dict, dict]]:
    """All pairs above RELATED_SIMILARITY, most similar first. O(n^2): fine for a few thousand entries."""
    vecs = tfidf_vectors(entries)
    pairs = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            s = cosine(vecs[i], vecs[j])
            if s >= RELATED_SIMILARITY:
                pairs.append((s, entries[i], entries[j]))
    pairs.sort(key=lambda p: -p[0])
    return pairs


def _days_since(date: str) -> int | None:
    try:
        return (_dt.date.today() - _dt.date.fromisoformat(date)).days
    except (TypeError, ValueError):
        return None


def maintenance_report() -> dict:
    """Deterministic candidates for the maintain skill. Changes nothing."""
    entries = load_entries()
    active = [e for e in entries if not is_superseded(e)]
    dups, related = [], []
    for s, a, b in similar_pairs(active):
        same_scope = bool(set(a["stack"]) & set(b["stack"])) or (not a["stack"] and not b["stack"])
        if s >= DUPLICATE_SIMILARITY:
            dups.append((s, a, b))
        elif same_scope:
            related.append((s, a, b))
    old_assumed = []
    for e in active:
        age = _days_since(e.get("updated") or e.get("created") or "")
        if e["confidence"] == "assumed" and age is not None and age > OLD_ASSUMED_DAYS:
            old_assumed.append((age, e))
    _, problems, _ = validate_store()
    return {
        "entries": len(entries),
        "superseded": len(entries) - len(active),
        "duplicates": dups,
        "related": related,
        "needs_review": [e for e in active if e["status"] == "needs-review"],
        "old_assumed": sorted(old_assumed, key=lambda x: -x[0]),
        "too_long": sorted(((len(e["_body"]), e) for e in active if len(e["_body"]) > LONG_BODY_CHARS),
                           key=lambda x: -x[0]),
        "problems": problems,
    }


def t_maintain(max_pairs=MAX_PAIRS) -> str:
    r = maintenance_report()
    max_pairs = max(1, min(int(max_pairs or MAX_PAIRS), 100))

    def ref(e):
        return f"[{e['id']}]{trust_marker(e)} {e['title']}"

    def pair_lines(pairs):
        out = []
        for i, (s, a, b) in enumerate(pairs[:max_pairs], 1):
            shared = sorted(set(a["stack"]) & set(b["stack"])) or ["general"]
            out.append(f"{i}. {ref(a)}\n   <-> {ref(b)}\n   similarity {s:.2f}, stack {', '.join(shared)}")
        if len(pairs) > max_pairs:
            out.append(f"... {len(pairs) - max_pairs} more (raise max_pairs)")
        return out

    out = [f"Maintenance report: {r['entries']} entries ({r['superseded']} superseded, ignored). "
           "Read-only: nothing was changed."]
    sections = [
        ("Probable duplicates (same knowledge written twice: merge, or supersede one)", pair_lines(r["duplicates"])),
        ("Related pairs to compare (same topic and stack: check they do not contradict each other)",
         pair_lines(r["related"])),
        ("Needs review (review date passed)", [f"- {ref(e)} (review_after {e.get('review_after')})"
                                               for e in r["needs_review"]]),
        (f"Unverified for more than {OLD_ASSUMED_DAYS} days", [f"- {ref(e)} ({age} days)"
                                                               for age, e in r["old_assumed"]]),
        (f"Too long (over {LONG_BODY_CHARS} characters: logs or conversation dumps?)",
         [f"- {ref(e)} ({n} chars)" for n, e in r["too_long"]]),
        ("Format problems", [f"- {p}" for p in r["problems"]]),
    ]
    found = False
    for title, lines in sections:
        if lines:
            found = True
            out.append(f"\n## {title}")
            out += lines
    if not found:
        out.append("\nNothing to do.")
    else:
        out.append("\nNext: review each case with memory_get, propose actions to the user, apply only what they "
                   "approve (memory_update: append, confirm, confidence, superseded_by). Never delete.")
    return "\n".join(out)


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
            "limit": {"type": "integer", "description": "Max results (default 8)."},
            "include_superseded": {"type": "boolean", "description": "Also return entries replaced by newer ones."}},
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
            "source": {"type": "string", "description": "Project folder name where it was learned (default: the current project folder)."},
            "origin": {"type": "string", "description": "Optional: path of the project memory file it came from (used by harvest)."},
            "confidence": {"type": "string", "enum": list(CONFIDENCES),
                           "description": "Be honest. assumed: believed, not checked. observed (default): seen happen "
                                          "in a session. tested: you reproduced or measured it in this session. "
                                          "user-confirmed: the user explicitly confirmed it."},
            "review_after": {"type": "string", "description": "Optional date (YYYY-MM-DD) after which the entry should be re-checked (temporary workarounds, version-specific facts)."},
            "session": {"type": "string", "description": "Optional session id, for provenance."},
            "verified": {"type": "boolean", "description": "Deprecated, use confidence (true = tested, false = assumed)."},
            "force": {"type": "boolean", "description": "Write even if similar entries exist."},
            "allow_sensitive": {"type": "boolean", "description": "Allow private IPs/emails when truly needed."}},
            "required": ["title", "summary", "body", "type", "tags"]},
    },
    {
        "name": "memory_update",
        "description": (
            "Update a shared-memory entry: fix it, generalize it, append a new finding (append= adds a dated "
            "note), confirm it (confirm=true records today as last_verified), change its confidence, or mark it "
            "superseded by a newer entry (superseded_by=<id>; the old entry is kept but hidden from search). Use it "
            "when an entry is incomplete, wrong, or confirmed/contradicted in another project."),
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"}, "summary": {"type": "string"},
            "body": {"type": "string", "description": "Replaces the whole body."},
            "append": {"type": "string", "description": "Text added at the end as a dated update."},
            "type": {"type": "string", "enum": list(TYPES)},
            "tags": {"type": "array", "items": {"type": "string"}},
            "stack": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "string", "enum": list(CONFIDENCES)},
            "status": {"type": "string", "enum": list(STATUSES)},
            "superseded_by": {"type": "string", "description": "Id of the entry that replaces this one."},
            "review_after": {"type": "string", "description": "YYYY-MM-DD, or empty string to clear it."},
            "confirm": {"type": "boolean", "description": "The entry was checked again and still holds."},
            "verified": {"type": "boolean", "description": "Deprecated, use confidence."},
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
        "description": "Shared-memory store location, counts by type, confidence, status and stack, uncommitted files, harvest log path.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_validate",
        "description": "Check every shared-memory entry file for format problems (missing fields, bad values or dates, broken superseded_by links) and list entries due for review. Read-only.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_maintain",
        "description": (
            "Maintenance report on the shared memory, read-only: probable duplicates, related pairs to compare for "
            "contradictions, entries due for review, old unverified entries, overlong entries, format problems. "
            "Use it for the /shared-memory:maintain workflow; it only lists candidates, you judge them with the user."),
        "inputSchema": {"type": "object", "properties": {
            "max_pairs": {"type": "integer", "description": f"Max pairs listed per section (default {MAX_PAIRS})."}}},
    },
]

HANDLERS = {
    "memory_search": lambda a: t_search(a.get("query", ""), a.get("stack"), a.get("type"), a.get("limit", 8),
                                        a.get("include_superseded", False)),
    "memory_get": lambda a: t_get(a.get("ids")),
    "memory_add": lambda a: t_add(**{k: a[k] for k in (
        "title", "summary", "body", "type", "tags", "stack", "source", "origin", "verified", "confidence",
        "review_after", "session", "force", "allow_sensitive") if k in a}),
    "memory_update": lambda a: t_update(**{k: a[k] for k in (
        "id", "title", "summary", "body", "append", "type", "tags", "stack", "verified", "confidence", "status",
        "superseded_by", "review_after", "confirm", "from_project", "allow_sensitive") if k in a}),
    "memory_delete": lambda a: t_delete(a.get("id", ""), a.get("reason", "")),
    "memory_list": lambda a: t_list(a.get("stack"), a.get("type"), a.get("tag")),
    "memory_info": lambda a: t_info(),
    "memory_validate": lambda a: t_validate(),
    "memory_maintain": lambda a: t_maintain(a.get("max_pairs", MAX_PAIRS)),
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
        global CLIENT_NAME
        client = (params.get("clientInfo") or {}).get("name") or ""
        CLIENT_NAME = re.sub(r"[^a-z0-9._-]+", "-", str(client).lower()).strip("-")[:40]
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

    # Stack words in the project's instructions and memory. Not the README: it often mentions
    # technologies only as examples. Environment stacks come from the system, not from text.
    text = " ".join([
        cwd.name,
        _read_head(cwd / "CLAUDE.md", 30_000), _read_head(cwd / ".claude" / "CLAUDE.md", 30_000),
        _read_head(_project_memory_dir(cwd) / "MEMORY.md", 20_000),
    ]).lower()
    for tag in vocabulary:
        if tag in found or len(tag) < 2 or tag in ENV_STACKS:
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


def environment_stacks(agent: str = "claude-code") -> set[str]:
    """Stacks true for every project on this machine: the OS, and the agent asking for context."""
    env = {clean_tags([agent])[0]} if clean_tags([agent]) else set()
    if sys.platform.startswith(("win", "cygwin", "msys")):
        env.add("windows")
    elif sys.platform == "darwin":
        env.add("macos")
    else:
        env.add("linux")
        try:
            if "microsoft" in os.uname().release.lower():
                env.add("wsl")
        except AttributeError:
            pass
    return env


def relevance(e: dict, project: set, env: set) -> float:
    """Session-start score. 0 = not for this project.
    - entry with project stacks (xbox360, flutter...): needs one of them detected in the project;
    - entry with only environment stacks (windows, claude-code...): needs that environment;
    - entry without stack: general, always a (low priority) candidate."""
    stacks = set(e["stack"])
    tag_bonus = 0.5 * len(set(e["tags"]) & project)
    project_stacks = stacks - ENV_STACKS
    if project_stacks:
        matched = project_stacks & project
        return 3.0 * len(matched) + len(stacks & env) + tag_bonus if matched else 0.0
    if stacks:
        matched = stacks & (env | project)
        return 1.0 * len(matched) + tag_bonus if matched else 0.0
    return 0.5 + tag_bonus


def _short_line(e: dict) -> str:
    summary = e.get("summary") or ""
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[:SUMMARY_MAX_CHARS - 3].rstrip() + "..."
    return f"- [{e['id']}]{trust_marker(e)} {e['title']}" + (f": {summary}" if summary else "")


def _count_by_stack(entries: list[dict]) -> str:
    c = Counter(s for e in entries for s in (e["stack"] or ["general"]))
    return ", ".join(f"{k} {v}" for k, v in c.most_common(6))


def session_context(cwd: Path, entries: list[dict], budget_tokens: int | None = None, agent: str = "claude-code"):
    """Returns (text, listed_ids). The entry list is cut to fit the token budget (chars / 4).
    Agent-neutral: the Claude Code hooks call it, other agents use `smem.py context`."""
    save_hint = ("/shared-memory:learn" if agent == "claude-code"
                 else "the memory_add tool (search first, then update instead of duplicating)")
    if not entries:
        return ("# Shared memory (cross-project knowledge base)\n"
                f"The shared memory is empty for now. When you solve something reusable in other projects, offer "
                f"to save it with {save_hint}."), set()
    if budget_tokens is None:
        budget_tokens = _env_int("SMEM_CONTEXT_BUDGET", CONTEXT_BUDGET_DEFAULT)
    vocab = sorted({s for e in entries for s in e["stack"]})
    detected = detect_project(cwd, vocab)
    env = environment_stacks(agent)
    project = set(detected)
    active = [e for e in entries if not is_superseded(e)]
    scored = [(relevance(e, project, env), e.get("updated") or e.get("created") or "", e) for e in active]
    scored = [x for x in scored if x[0] > 0]
    # Order: relevance, then trust (confidence and review status), then newest. Stable sorts, last key first.
    scored.sort(key=lambda x: x[1], reverse=True)
    scored.sort(key=lambda x: -trust_weight(x[2]))
    scored.sort(key=lambda x: -x[0])
    candidates = [e for _, _, e in scored]

    header = ["# Shared memory (cross-project knowledge base)",
              f"{len(entries)} entries learned across the user's projects. Project stack detected: "
              f"{', '.join(detected) or 'nothing specific'}; environment: {', '.join(sorted(env))}."]
    footer = ["\nSearch the shared memory (memory_search) before investigating a non-trivial problem from scratch. "
              f"When you solve something reusable in other projects, offer to save it with {save_hint} "
              "(generalized, no secrets)."]
    intro = "\nMost relevant entries for this project (titles only: call memory_get with the id before relying on one):"
    others = len(entries) - len(candidates)

    def render(chosen: list[dict]) -> str:
        lines = list(header)
        if chosen:
            lines.append(intro)
            lines += [_short_line(e) for e in chosen]
        rest = candidates[len(chosen):]
        more = []
        if rest:
            more.append(f"{len(rest)} more relevant entries not listed ({_count_by_stack(rest)})")
        if others:
            more.append(f"{others} entries for other stacks")
        if more:
            lines.append("\n(" + "; ".join(more) + ": use memory_search.)")
        return "\n".join(lines + footer)

    limit = budget_tokens * 4
    fixed = len(render([])) + len(intro) + 1
    chosen, used = [], fixed
    for e in candidates:
        cost = len(_short_line(e)) + 1
        if used + cost > limit:
            break
        chosen.append(e)
        used += cost
    text = render(chosen)
    while chosen and len(text) > limit:  # the "more entries" line can change length: trim until it fits
        chosen.pop()
        text = render(chosen)
    return text, {e["id"] for e in chosen}


def hook_session_start() -> None:
    data = _hook_input()
    cwd = Path(data.get("cwd") or os.getcwd())
    text, listed = session_context(cwd, load_entries())
    _state_save(data.get("session_id", ""), listed)  # fresh list on startup/resume/clear/compact
    _emit("SessionStart", text)


def prompt_suggestions(prompt: str, entries: list[dict], seen: set, max_entries: int | None = None) -> list[dict]:
    p = (prompt or "").strip()
    if max_entries is None:
        max_entries = _env_int("SMEM_PROMPT_MAX", PROMPT_MAX_DEFAULT)
    if len(p) < 15 or p.startswith("/") or not entries or max_entries == 0:
        return []
    entries = [e for e in entries if not is_superseded(e)]
    results = [r for r in score_query(p[:4000], entries) if r["entry"]["id"] not in seen]
    if not results or results[0]["score"] < PROMPT_MIN_SCORE:
        return []
    top = results[0]["score"]
    picked = [r["entry"] for r in results
              if r["hits"] >= 2 and r["score"] >= PROMPT_REL_SCORE * top and r["coverage"] >= PROMPT_MIN_COVERAGE]
    return picked[:max_entries]


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
          "Read them with memory_get if useful; ignore them if not:\n" + "\n".join(_short_line(e) for e in picked))


# --------------------------------------------------------------------------- CLI

def cli_context(args: list[str]) -> str:
    """`smem.py context`: the same context the Claude Code hooks inject, for any agent or script.

    smem.py context [--cwd DIR] [--agent NAME] [--budget TOKENS] [--prompt TEXT]
    Without --prompt: the session-start list for the project in DIR (default: current directory).
    With --prompt: the entries the prompt hook would suggest for that text (empty output if none)."""
    import argparse
    ap = argparse.ArgumentParser(prog="smem.py context", description=cli_context.__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cwd", default=os.getcwd(), help="project directory (default: current directory)")
    ap.add_argument("--agent", default="claude-code", help="agent name, also used as an environment stack")
    ap.add_argument("--budget", type=int, default=None, help="token budget (default: SMEM_CONTEXT_BUDGET or 600)")
    ap.add_argument("--prompt", default=None, help="return prompt suggestions for this text instead")
    ap.add_argument("--max", type=int, default=None, help="max prompt suggestions (default: SMEM_PROMPT_MAX or 1)")
    a = ap.parse_args(args)
    entries = load_entries()
    if a.prompt is not None:
        picked = prompt_suggestions(a.prompt, entries, set(), a.max)
        if not picked:
            return ""
        return ("Shared-memory entries (from the user's other projects) that may be relevant to this request:\n"
                + "\n".join(_short_line(e) for e in picked))
    return session_context(Path(a.cwd), entries, a.budget, a.agent)[0]


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
    elif cmd == "validate":
        print(t_validate())
        return 1 if validate_store()[1] else 0
    elif cmd == "maintain":
        print(t_maintain())
    elif cmd == "context":
        print(cli_context(argv[2:]))
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
