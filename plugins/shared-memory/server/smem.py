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

VERSION = "0.2.1"
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

# Write limits (entries hold findings, not logs or conversation dumps).
MAX_TITLE_CHARS = 200
MAX_SUMMARY_CHARS = 500
MAX_FIELD_CHARS = 200           # source, origin, session, from_project, archive reason
MAX_BODY_CHARS = 4000
MAX_APPEND_CHARS = 2000

# File names that are not entries: the store README/index, and names Windows reserves.
RESERVED_IDS = {"readme", "index", "con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} \
    | {f"lpt{i}" for i in range(1, 10)}
ID_RE = re.compile(r"[\w][\w-]*")


def _is_date(value) -> bool:
    """A real calendar date written YYYY-MM-DD (2026-13-45 and trailing newlines are rejected)."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        _dt.date.fromisoformat(value)
        return True
    except ValueError:
        return False

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

# Stacks that describe the machine or the agent rather than the project. They come from the system
# and from the agent asking for context, never from project files. An entry made only of these
# applies to every project where they hold (e.g. [windows] on Windows, [codex] in Codex sessions).
OS_STACKS = {"windows", "linux", "macos", "wsl"}
AGENT_STACKS = {"claude-code", "codex", "cursor", "gemini", "copilot", "continue", "aider", "opencode",
                "windsurf", "cline"}
ENV_STACKS = OS_STACKS | AGENT_STACKS

LOCK_NAME = ".smem.lock"
LOCK_TIMEOUT = 30.0
LOCK_STALE_AFTER = 60.0


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


# The closing '---' must start a line (an empty front matter "---\n---" is valid).
_FM = re.compile(r"\A---[ \t]*\r?\n(?:(.*?)\r?\n)?---[ \t]*(?:\r?\n|\Z)(.*)\Z", re.S)
_BLOCK_SCALAR = re.compile(r"^[|>][+-]?\d*$")


def _strip_comment(v: str) -> str:
    """End-of-line comment: ' # ...' (a space before and after '#'). Kept: 'c#', '[c#, f#]', 'my app #2'
    (unquoted values written by 0.2.0 must load unchanged)."""
    return re.sub(r"\s+#(?:\s.*)?$", "", v)


def _parse_value(v: str):
    v = v.strip()
    if v.startswith('"'):
        try:
            value, _ = json.JSONDecoder().raw_decode(v)  # ignores what follows, e.g. a comment
            return value if isinstance(value, str) else str(value)
        except ValueError:
            return _strip_comment(v).strip('"')
    if v.startswith("'"):
        whole = re.fullmatch(r"'((?:[^']|'')*)'", _strip_comment(v).strip())
        if whole:  # only a whole single-quoted scalar: "'foo' and 'bar'" stays as written
            return whole.group(1).replace("''", "'")
    v = _strip_comment(v).strip()
    if v.startswith("[") and v.endswith("]"):
        items = [_parse_value(x) for x in v[1:-1].split(",") if x.strip()]
        return [str(x).lower() if isinstance(x, bool) else str(x) for x in items]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    return v


def _complete_json_string(v: str) -> bool:
    try:
        json.JSONDecoder().raw_decode(v)
        return True
    except ValueError:
        return False


def parse_entry(text: str):
    """Front matter subset: 'key: value', inline lists [a, b], block lists ('key:' then '- a' lines),
    quoted strings, booleans, end-of-line comments. Other YAML (multi-line '|'/'>' values, nested keys)
    is recorded in meta['_unsupported'] so that such a file is reported and never rewritten."""
    text = text.lstrip("﻿")
    m = _FM.match(text)
    if not m:
        return {}, text.strip()
    meta, unsupported = {}, []
    block_key = None  # key whose value is a YAML block list
    for line in (m.group(1) or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("- ") or s == "-":
            if block_key is not None:
                item = _parse_value(s[2:]) if len(s) > 2 else ""
                if item != "":
                    meta[block_key].append(item if isinstance(item, str) else str(item).lower())
            else:
                unsupported.append(f"list item outside a list: '{s[:40]}'")
            continue
        if line[:1] in (" ", "\t"):
            unsupported.append(f"indented line (nested key or multi-line value): '{s[:40]}'")
            continue
        if ":" not in line:
            unsupported.append(f"line without 'key: value': '{s[:40]}'")
            continue
        k, v = line.split(":", 1)
        k = k.strip()
        v = v.strip()
        if v and not v.startswith("#"):
            if _BLOCK_SCALAR.match(_strip_comment(v)):
                unsupported.append(f"multi-line value for '{k}'")
            elif v[0] in "{&*!":
                unsupported.append(f"YAML mapping, anchor, alias or tag in '{k}'")
            elif v[0] == '"' and not _complete_json_string(v):
                unsupported.append(f"quoted value for '{k}' not closed on its line")
            elif v[0] == "'" and not re.match(r"'(?:[^']|'')*'", v):
                unsupported.append(f"quoted value for '{k}' not closed on its line")
            meta[k] = _parse_value(v)
            block_key = None
        else:
            meta[k] = []
            block_key = k
    if unsupported:
        meta["_unsupported"] = unsupported
    return meta, m.group(2).strip()


# Free-text fields are always written as JSON strings: quotes, colons, '#' or newlines cannot
# break the front matter or inject other keys.
_QUOTED_FIELDS = ("title", "summary", "source", "origin", "session", "archive_reason")


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
            s = "[" + ", ".join(_one_line(str(x)).replace(",", " ") for x in v) + "]"
        else:
            s = str(v)
            # Quote free text, and anything that would not read back as the same string ('true', '# x'...).
            if k in _QUOTED_FIELDS or not re.fullmatch(r"[\w.+@/ -]*", s) or _parse_value(s) != s:
                s = json.dumps(s, ensure_ascii=False)
        lines.append(f"{k}: {s}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.strip() + "\n"


def _one_line(s: str) -> str:
    return " ".join(str(s).split())


def read_entry_file(path: Path) -> str:
    """Entry text; tolerates a UTF-8 byte order mark (files saved by Windows Notepad)."""
    return path.read_text(encoding="utf-8-sig")


def is_entry_file(path: Path) -> bool:
    return path.suffix == ".md" and path.stem.lower() not in RESERVED_IDS and not path.name.startswith(".")


def load_entries() -> list[dict]:
    out = []
    for f in sorted(store_dir().glob("*.md")):
        if not is_entry_file(f):
            continue
        try:  # Windows: a file being replaced by a writer is briefly unreadable (PermissionError)
            meta, body = parse_entry(_retry_io(lambda: read_entry_file(f), attempts=6))
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
_MANGLED = re.compile(r"^(?:[A-Za-z]--|-)")  # Windows 'C--work-my-app', Linux/macOS '-home-user-my-app'


def _unmangle_fallback(name: str) -> str:
    if name[:1] != "-":
        return name[3:]  # drop the drive prefix
    home = re.match(r"^-(?:home|Users)-[^-]+-(.+)$", name)
    return home.group(1) if home else name.lstrip("-")


def normalize_source(name: str) -> str:
    """'C--work-my-app' or '-home-me-my-app' (Claude Code's project folder naming) -> 'my-app'.
    The real path is read from that project's transcripts; otherwise a best-effort guess is used."""
    name = name.strip()
    if not _MANGLED.match(name) or len(name) < 3:
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
        _SOURCE_NAMES[name] = base or _unmangle_fallback(name)
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
    if s == "active" and _is_date(review) and review < today():
        s = "needs-review"
    return s


def normalize_entry(meta: dict, body: str, path: Path) -> dict:
    """Loaded view of an entry: v0.1 files get defaults here, the file itself is never rewritten."""
    e = dict(meta)
    e["_raw_id"] = meta.get("id")
    e["id"] = path.stem  # the file name is the id
    for k in ("tags", "stack"):
        e[k] = [x.lower() for x in _as_list(meta.get(k))]
    for k in ("title", "summary", "type"):
        e[k] = _one_line(_as_str(meta.get(k)))  # a newline must never reach the injected context
    e["title"] = e["title"] or path.stem
    for k in DATE_FIELDS:  # hand-edited files may hold lists or booleans here: keep sorting safe
        if k in meta:
            e[k] = _as_str(meta.get(k)).strip()
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


def _retry_io(action, attempts: int = 25):
    """Windows refuses to replace or delete a file that another process has open (a hook or another
    session reading the store): PermissionError. Retry for up to ~3 s before giving up."""
    for i in range(attempts):
        try:
            return action()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(min(0.02 * (i + 1), 0.25))


def _write_atomic(path: Path, data) -> None:
    """Write text (UTF-8, LF) or bytes (kept exactly) to a temporary file, then move it into place."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
    try:
        if isinstance(data, bytes):
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        else:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(data)
        _retry_io(lambda: os.replace(tmp, path))
    except PermissionError:
        raise ToolError(f"Could not write {path.name}: the file stays in use by another program. Try again.")
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _unlink(path: Path) -> None:
    try:
        _retry_io(path.unlink)
    except PermissionError:
        raise ToolError(f"Could not remove {path.name}: the file stays in use by another program. Try again.")


def _run(args, timeout=10):
    """Run git: English messages (parsed below), UTF-8 output, never reads our stdin (the MCP channel)."""
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    env = dict(os.environ, LC_ALL="C", LANGUAGE="C", GIT_TERMINAL_PROMPT="0")
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=timeout, creationflags=flags, env=env)


class StoreLock:
    """Cross-process lock (one lock file in the store) around every write and its git commit.

    Several sessions run their own MCP server on the same store; without it, concurrent writes race
    on git's index.lock and commits get lost. The lock file holds a unique token: a process only
    removes a lock it owns, or a stale one whose token did not change since it was judged stale."""

    def __init__(self, timeout: float = LOCK_TIMEOUT, stale_after: float = LOCK_STALE_AFTER):
        self.path = store_dir() / LOCK_NAME
        self.timeout = timeout
        self.stale_after = stale_after
        self.token = f"{os.getpid()}-{os.urandom(8).hex()}"

    def _read(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return ""

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(self.token)
                return self
            except FileExistsError:
                pass
            except PermissionError:  # Windows: the lock file is being deleted right now
                time.sleep(0.02)
                continue
            try:
                stale = time.time() - self.path.stat().st_mtime > self.stale_after
            except FileNotFoundError:
                continue
            except OSError:
                stale = False
            if stale:
                seen = self._read()
                if seen is None:
                    continue
                time.sleep(0.05)
                if self._read() == seen:  # nobody took it over meanwhile: left by a crashed process
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
            if time.monotonic() > deadline:
                raise ToolError("The store is busy (another session is writing to it). Try again in a moment.")
            time.sleep(0.05)

    def __exit__(self, *exc):
        if self._read() == self.token:
            try:
                _retry_io(self.path.unlink)
            except OSError:
                pass
        return False


def git_state(d: str) -> str:
    """'own' (the store is the root of a git repo), 'nested' (inside another repo: never commit there),
    or 'none' (no repo, or git not installed)."""
    try:
        r = _run(["git", "-C", d, "rev-parse", "--show-toplevel"], 5)
    except Exception:
        return "none"
    if r.returncode != 0 or not r.stdout.strip():
        return "none"
    top = Path(r.stdout.strip())
    try:
        same = os.path.normcase(str(top.resolve())) == os.path.normcase(str(Path(d).resolve()))
    except OSError:
        same = False
    return "own" if same else "nested"


def _is_git_repo(d: str) -> bool:
    return git_state(d) == "own"


def git_commit(paths, message: str) -> str:
    """Commit the touched files if the store is the root of a git repo. Returns "" or a warning."""
    d = str(store_dir())
    if not _is_git_repo(d):
        return ""
    ps = []
    for p in paths:  # a removed file is only staged if git tracked it (else 'pathspec did not match')
        p = Path(p)
        try:  # paths relative to the store: absolute ones break on subst/mapped drives ('outside repository')
            rel = os.path.relpath(str(p), d)
        except ValueError:
            rel = str(p)
        if p.exists():
            ps.append(rel)
        else:
            try:
                if _run(["git", "-C", d, "ls-files", "--", rel], 5).stdout.strip():
                    ps.append(rel)
            except Exception:
                pass
    if not ps:
        return ""
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
        r = _run(["git", "-C", d, "-c", "core.quotepath=false", "status", "--porcelain", "-z",
                  "--untracked-files=all", "--", "."], 10)
    except Exception:
        return None
    files, items = [], r.stdout.split("\0")
    i = 0
    while i < len(items):
        item = items[i]
        i += 1
        if len(item) < 4:
            continue
        status, name = item[:2], item[3:]
        if status[0] in "RC":  # rename/copy: the original path follows as its own field
            i += 1
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


STOP |= set("""
في من على الى عن مع هذا هذه ذلك التي الذي الذين او ان لا ما لم لن هل كيف لماذا متى اين كل بعض قد كان
""".split())

# Latin letters that do not decompose into ASCII + accent.
_TRANSLIT = str.maketrans({"\u00df": "ss", "\u00e6": "ae", "\u00c6": "AE", "\u0153": "oe", "\u0152": "OE", "\u00f8": "o", "\u00d8": "O",
                           "\u0111": "d", "\u0110": "D", "\u0142": "l", "\u0141": "L", "\u00fe": "th", "\u00de": "TH", "\u0131": "i"})
# Arabic: unify letter variants so spelling differences still match (hamza forms of alef, alef maqsura).
_AR_NORM = str.maketrans({"\u0623": "\u0627", "\u0625": "\u0627", "\u0622": "\u0627", "\u0671": "\u0627", "\u0649": "\u064a", "\u0640": None})
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _fold(s: str) -> str:
    """Accents removed, other scripts kept: 'Été' -> 'Ete', Arabic and CJK unchanged (but normalized)."""
    if s.isascii():  # fast path: most text
        return s
    return _MARKS.sub("", unicodedata.normalize("NFKD", s.translate(_TRANSLIT))).translate(_AR_NORM)


# Combining marks removed after NFKD: Latin/Greek/Cyrillic accents, Hebrew points, Arabic harakat.
_MARKS = re.compile("[\u0300-\u036f\u0483-\u0489\u0591-\u05c7\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed"
                    "\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]")


_CAMEL = re.compile(r"([a-z])([A-Z])")
_ASCII_WORD = re.compile(r"[a-z0-9][a-z0-9+#]*")
_ANY_WORD = re.compile(r"[a-z0-9][a-z0-9+#]*|[^\W\d_a-z]+")
_PARTS = re.compile(r"[a-z]+|[0-9]+")


def tokens(text: str) -> list[str]:
    text = _CAMEL.sub(r"\1 \2", text or "")  # camelCase -> camel Case
    folded = _fold(text).lower()
    ascii_only = folded.isascii()  # English, and French once accents are removed: the 0.2.0 fast path
    out = []
    prev = ""
    for t in (_ASCII_WORD if ascii_only else _ANY_WORD).findall(folded):
        if not ascii_only and not t.isascii():  # another script: words as they are; CJK -> character bigrams
            if _CJK.search(t):
                chars = [c for c in t if _CJK.match(c)]
                out.extend(chars if len(chars) == 1 else (a + b for a, b in zip(chars, chars[1:])))
            elif len(t) >= 2 and t not in STOP:
                out.append(t)
            prev = ""
            continue
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        parts = _PARTS.findall(t)
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
    """File name / id from a title: ASCII words as before, letters of other scripts kept."""
    s = re.sub(r"[\W_]+", "-", _fold(title).lower()).strip("-")
    s = s[:60].rstrip("-") or "entry"
    return f"{s}-entry" if s in RESERVED_IDS else s


def clean_tags(values) -> list[str]:
    if isinstance(values, str):
        values = re.split(r"[,\s]+", values)
    elif not isinstance(values, (list, tuple)):
        values = [] if values is None else [values]
    out = []
    for v in values:
        t = re.sub(r"[^\w.+#-]+|_+", "-", _fold(str(v)).lower()).strip("-.")
        if t and t not in out:
            out.append(t)
    return out


# --------------------------------------------------------------------------- secrets

# Words that, as a whole value or as a part of it ('your-api-key-here'), mark a placeholder.
_PLACEHOLDER_PARTS = {"your", "my", "example", "examples", "placeholder", "dummy", "changeme", "redacted", "here",
                      "xxx", "xxxx", "xxxxx", "sample", "fake", "test", "secret", "password", "token", "value",
                      "foo", "bar", "replace", "insert", "todo", "tbd", "none", "null"}
# Plain words that describe a value instead of being one ('password: required', 'pwd: hashed').
_DESCRIPTIVE = {"required", "optional", "hidden", "masked", "hashed", "encrypted", "prompted", "empty", "blank",
                "unset", "none", "null", "true", "false", "string", "text", "default", "same", "stdin", "env",
                "environment", "variable", "value", "secret", "password", "token", "auto", "generated", "random",
                "admin", "root", "guest", "user", "changeme", "example", "omitted", "redacted", "removed"}
# References to where a secret is read from, not the secret itself.
_CODE_REF = re.compile(r"(?i)^(?:os\.(?:environ|getenv)|process\.env|getenv|environ|env\b|env\(|\$|%|\{\{|<|\[|\(|"
                       r"config\(|settings\.|secrets\.|vault\(|keyring\.|import\b|require\()")


def _is_placeholder(v: str) -> bool:
    low = v.lower()
    if _CODE_REF.match(v) or re.fullmatch(r"[*x.•-]{3,}|\.\.\.+", low):
        return True
    parts = [p for p in re.split(r"[-_.\s/]+", low) if p]
    return bool(parts) and any(p in _PLACEHOLDER_PARTS for p in parts) and not re.search(r"\d", low)


def _secret_value(v: str | None, strict: bool) -> bool:
    """Does the value after 'password:' / 'token=' look like a real secret?
    strict (password-like keys): any non-descriptive value of 6+ chars counts.
    loose (token/key/secret keys): plain words, identifiers and paths do not count."""
    if v is None:
        return False
    v = v.strip().strip("\"'`")
    if len(v) < 4 or _is_placeholder(v) or v.lower() in _DESCRIPTIVE:
        return False
    if v.startswith(("/", "~/", "./", "../")) or re.match(r"^[A-Za-z]:[\\/]", v):
        return False  # a path
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", v) or re.fullmatch(r"\d+", v):
        return False  # a URL (credentials in URLs have their own pattern) or a number (port, timeout)
    if re.fullmatch(r"[a-z]+(?:-[a-z0-9]+)+", v):
        return False  # a kebab-case name (k8s secret name, scheme like bcrypt-hashed)
    if re.search(r"[()\[]|::|->|\$\{|\$\(|^\$", v) or re.fullmatch(r"[A-Za-z_]+(?:\.[A-Za-z_]+)+", v):
        return False  # code: a call, an index, Hash::make, $var, ${VAR}, response.data.token (ya29.x has digits)
    if v.startswith("{") or re.fullmatch(r"[a-z_]+(?:\+[a-z_]+)+", v):
        return False  # a template/f-string field ('{password!r}') or an expression ('pwd+salt')
    if re.fullmatch(r"[a-z]+", v) or (not strict and re.fullmatch(r"[a-z]+(?: [a-z]+)+", v)):
        return False  # a lowercase word ('credentials'), or lowercase prose after a token/secret key
    if re.fullmatch(r"[A-Za-z_]+", v) and ("_" in v or re.search(r"[a-z][A-Z]", v)):
        return False  # an identifier without digits: snake_case, camelCase, PascalCase type names
    if re.fullmatch(r"[a-z_]+(?:\|[\w:,]+)+", v):
        return False  # a validation rule list ('required|min:8')
    if strict:
        return len(v) >= 6
    if re.fullmatch(r"[A-Za-z_]+", v) and len(v) < 12:
        return False  # a plain word ('token: required')
    return True


def _value(m) -> str | None:
    return next((g for g in m.groups() if g is not None), None)


def _followed_by_words(m) -> bool:
    """'password: stored in Bitwarden': a bare word followed by more words is prose, not a value."""
    v = _value(m) or ""
    return bool(re.fullmatch(r"[A-Za-z]+", v)) and bool(re.match(r"[ \t]+[A-Za-z]", m.string[m.end():m.end() + 3]))


def _strict(m):
    return not _followed_by_words(m) and _secret_value(_value(m), True)


def _loose(m):
    return not _followed_by_words(m) and _secret_value(_value(m), False)


def _option(m):
    """'--password X': a plain English word after the option is prose ('--password option')."""
    v = (_value(m) or "").strip("\"'`")
    return not re.fullmatch(r"[A-Za-z]+", v) and _secret_value(v, True)


def _not_aws_example(m):
    return not m.group(0).endswith("EXAMPLE")


_EMAIL_NOT_ADDRESS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp", "js", "ts", "css", "json"}


def _real_email(m):
    after = m.string[m.end():m.end() + 1]
    tld = m.group(0).rsplit(".", 1)[-1].lower()
    return after != ":" and tld not in _EMAIL_NOT_ADDRESS  # 'git@github.com:user/repo', 'icon@2x.png'


_QUOTED_OR_BARE = r"""(?:"([^"\n]{4,200})"|'([^'\n]{4,200})'|`([^`\n]{4,200})`|([^\s"'`,;<>)\]}]{4,200}))"""
_PASSWORD_KEYS = r"(?:password|passwd|pwd|passphrase|mot[ _-]?de[ _-]?passe|(?<=[_-])pass)"  # 'pass' only as DB_PASS
_TOKEN_KEYS = (r"(?:secret|client[ _-]?secret|api[ _-]?key|apikey|api[ _-]?token|access[ _-]?key|secret[ _-]?key|"
               r"private[ _-]?key|access[ _-]?token|refresh[ _-]?token|auth[ _-]?token|bearer[ _-]?token|token|"
               r"_auth(?:token)?)")
# Before the key: an optional name joined by '_' or '-' (DB_PASSWORD, aws_secret_access_key), never by a space
# ('Second pass:' is prose). After it: up to 3 suffix parts (DB_PASSWORD_PROD, GITHUB_TOKEN_RW).
_KEY_PREFIX = r"(?<![A-Za-z0-9_-])(?:[A-Za-z0-9]{1,30}[_-]){0,3}"
_KEY_SUFFIX = r"(?:[_-][A-Za-z0-9]{1,20}){0,3}(?![A-Za-z0-9])"

# (name, regex, check(match) -> bool or None). Reported matches are always truncated.
SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY[A-Z ]{0,10}-----|PuTTY-User-Key-File"), None),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), _not_aws_example),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}"), None),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), None),
    ("API key (sk-...)", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), None),
    ("Stripe key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}|\bwhsec_[A-Za-z0-9]{20,}"), None),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), None),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}"), None),
    ("Slack token or webhook", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}|hooks\.slack\.com/services/T\w+/B\w+/\w+"),
     None),
    ("Discord webhook", re.compile(r"discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{20,}"), None),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}"), None),
    ("SendGrid key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"), None),
    ("Google API key or OAuth secret", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b|\bGOCSPX-[A-Za-z0-9_-]{20,}"), None),
    ("Azure key or SAS signature", re.compile(r"AccountKey=[A-Za-z0-9+/=]{20,}|[?&]sig=[A-Za-z0-9%+/=]{20,}"), None),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"), None),
    ("credentials in a URL",
     re.compile(r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{0,20}://(?![<{$%])[^\s/:@]{0,100}:(?![<{$%*])[^\s/@]{1,200}@"),
     None),
    ("token in a URL", re.compile(r"(?i)[?&](?:access_token|token|api_key|apikey|key|secret|password)=([^&\s#]{8,})"),
     _loose),
    ("authorization header",
     re.compile(r"(?i)\b(?:authorization\s*[:=]\s*[\"']?(?:bearer|basic|token)|bearer)\s+([A-Za-z0-9._~+/=-]{16,})"),
     _loose),
    ("password option", re.compile(r"(?i)(?<![\w-])--?(?:password|passwd|pass)(?:=|\s+)" + _QUOTED_OR_BARE), _option),
    ("command-line password",
     re.compile(r"(?i)\b(?:mysql(?:dump|admin)?\b[^\n]{0,200}?\s-p|sshpass\s+-p\s*)" + _QUOTED_OR_BARE), _strict),
    (".netrc password",
     re.compile(r"(?im)^\s*(?:machine\s+\S+\s+(?:login\s+\S+\s+)?|login\s+\S+\s+)password\s+(\S{4,200})"), _strict),
    ("password value", re.compile(r"(?i)" + _KEY_PREFIX + _PASSWORD_KEYS + _KEY_SUFFIX + r"[\"']?\s*(?:[:=]|=>)\s*"
                                  + _QUOTED_OR_BARE), _strict),
    ("password in markup", re.compile(r"(?i)<(?:password|secret|token|apikey|api_key)>\s*([^<\s]{4,200})\s*</"),
     _strict),
    ("secret or token value", re.compile(r"(?i)" + _KEY_PREFIX + _TOKEN_KEYS + _KEY_SUFFIX + r"[\"']?\s*(?:[:=]|=>)\s*"
                                         + _QUOTED_OR_BARE), _loose),
]
SENSITIVE_PATTERNS = [
    ("private IP address",
     re.compile(r"(?<![\d.])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?![\d.]*\d)"), None),
    ("email address", re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}"
                                 r"(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}\b"), _real_email),
]


def _redact(s: str) -> str:
    """Enough to locate the match, never enough to reveal it."""
    return s[:min(6, max(1, len(s) // 3))] + "..."


def scan(text: str):
    def found(patterns):
        hits = []
        for name, pat, check in patterns:
            for m in pat.finditer(text):
                if check and not check(m):
                    continue
                hits.append(f"{name} ('{_redact(m.group(0))}')")
                break
        return hits
    return found(SECRET_PATTERNS), found(SENSITIVE_PATTERNS)


_LOG_LINE = re.compile(r"^\s*(?:\[?\d{2,4}[-/:.]\d{2}[-/:.]\d{2}|\d{2}:\d{2}:\d{2}|at \S+\(|File \"|"
                       r"(?:DEBUG|INFO|WARN|WARNING|ERROR|TRACE)\b|#\d+\s+0x[0-9a-f]+)")


def _check_size(text: str, limit: int, what: str) -> None:
    """Shared memory holds findings, not logs or conversation dumps."""
    if len(text) > limit:
        raise ToolError(f"Refused: the {what} is {len(text)} characters (limit {limit}). Keep one finding per entry: "
                        "the context, the fix, the reason, and only the exact error line that makes it searchable.")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 20 and sum(1 for ln in lines if _LOG_LINE.match(ln)) >= 0.4 * len(lines):
        raise ToolError(f"Refused: the {what} looks like a log or stack trace dump (20+ lines, mostly log lines). "
                        "Quote only the key error line and describe the fix.")


def _raw_words(values) -> str:
    """Tags or stacks exactly as given, one per line, for the secret scan."""
    if values is None:
        return ""
    if isinstance(values, (list, tuple)):
        return "\n".join(str(v) for v in values)
    return str(values)


def _check_field(value, limit: int, what: str) -> str:
    """Single-line text field: whitespace collapsed (no newline can reach the front matter), length capped."""
    v = _one_line(value) if value is not None else ""
    if len(v) > limit:
        raise ToolError(f"Refused: the {what} is {len(v)} characters (limit {limit}). Keep it short; details go "
                        "in the body.")
    return v


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
    limit = max(1, min(_int(limit, 8) or 8, 30))
    lines = [fmt_line(r["entry"]) for r in ranked[:limit]]
    return (f"{len(ranked)} matching entries, best first:\n" + "\n".join(lines)
            + "\n\nRead the full entries with memory_get before relying on them. [unverified] means nobody "
              "tested it yet: check before relying on it.")


def t_get(ids) -> str:
    if isinstance(ids, (str, int, float)):
        ids = [ids]
    elif not isinstance(ids, (list, tuple)):
        ids = []
    by = {e["id"]: e for e in load_entries()}
    parts = []
    for i in ids[:50]:
        i = str(i)
        e = by.get(i)
        if not e:
            parts.append(f"## {i}\nNot found.")
        else:
            parts.append(f"## {i}  (file: {e['_path']})\n\n" + read_entry_file(e["_path"]))
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
    if value and not _is_date(str(value)):
        raise ToolError(f"{name} must be a real date written YYYY-MM-DD, like 2026-12-31.")


def _confidence_from(confidence, verified):
    """Explicit confidence wins; the legacy verified flag maps true -> tested, false -> assumed."""
    if confidence is not None:
        return confidence
    if verified is not None:
        return "tested" if verified else "assumed"
    return None


def _refuse_unsupported(meta: dict, path: Path) -> None:
    if meta.get("_unsupported"):
        raise ToolError(f"{path.name} uses front-matter syntax this tool does not rewrite safely "
                        f"({'; '.join(meta['_unsupported'][:3])}). Edit the file by hand (one 'key: value' per line), "
                        "then retry.")


def _supersede_cycle(start: str, target: str, entries: list[dict]) -> bool:
    """Would 'target superseded_by start' close a loop? Follows start's superseded_by chain."""
    links = {e["id"]: e.get("superseded_by") or "" for e in entries}
    seen, cur = set(), start
    while cur and cur not in seen:
        if cur == target:
            return True
        seen.add(cur)
        cur = links.get(cur, "")
    return False


def t_add(title, summary, body, type, tags=None, stack=None, source=None, origin=None,
          verified=None, confidence=None, review_after=None, session=None,
          force=False, allow_sensitive=False) -> str:
    title = _check_field(title, MAX_TITLE_CHARS, "title")
    summary = _check_field(summary, MAX_SUMMARY_CHARS, "summary")
    body = (body or "").strip()
    if not title or not summary or not body:
        raise ToolError("title, summary and body are all required.")
    if type not in TYPES:
        raise ToolError(f"type must be one of: {', '.join(TYPES)}.")
    confidence = _confidence_from(confidence, verified) or "observed"
    _check_choice(confidence, CONFIDENCES, "confidence")
    _check_date(review_after, "review_after")
    tags_in, stack_in = tags, stack
    tags, stack = clean_tags(tags), clean_tags(stack)
    source = _check_field(source, MAX_FIELD_CHARS, "source")
    origin = _check_field(origin, 500, "origin")
    session = _check_field(session, MAX_FIELD_CHARS, "session")
    _check_size(body, MAX_BODY_CHARS, "body")
    _check_text("\n".join([title, summary, body, _raw_words(tags_in), _raw_words(stack_in), source, origin, session]),
                allow_sensitive)  # tags are scanned as given: normalizing them first would hide secrets
    with StoreLock():  # duplicate check, id choice, write and commit must not interleave
        all_entries = load_entries()
        if not force:
            dups = _find_duplicates(title, summary, [e for e in all_entries if not is_superseded(e)])
            if dups:
                return ("Nothing written: similar entries already exist:\n" + "\n".join(fmt_line(e) for e in dups)
                        + "\n\nRead them (memory_get). If one covers the same topic, extend it with memory_update "
                          "(append=...). If the new knowledge is really different, call memory_add again with "
                          "force=true.")
        existing = {e["id"].lower() for e in all_entries}
        base = slugify(title)
        new_id, n = base, 2
        while new_id.lower() in existing or (store_dir() / f"{new_id}.md").exists():
            new_id, n = f"{base}-{n}", n + 1
        src = source or _default_source()
        meta = {"id": new_id, "title": title, "summary": summary, "type": type, "tags": tags, "stack": stack,
                "source": src, "origin": origin, "created": today(), "confidence": confidence,
                "review_after": review_after or "", "agent": CLIENT_NAME, "session": session}
        path = store_dir() / f"{new_id}.md"
        _write_atomic(path, render_entry(meta, body))
        warning = git_commit([path], f"add: {new_id}" + (f" (from {src})" if src else ""))
    return f"Saved [{new_id}] ({confidence}) to {path}" + warning


def t_update(id, title=None, summary=None, body=None, append=None, type=None, tags=None, stack=None,
             verified=None, confidence=None, status=None, superseded_by=None, review_after=None, confirm=False,
             from_project=None, allow_sensitive=False) -> str:
    if title is not None:
        title = _check_field(title, MAX_TITLE_CHARS, "title")
        if not title:
            raise ToolError("title cannot be empty.")
    if summary is not None:
        summary = _check_field(summary, MAX_SUMMARY_CHARS, "summary")
        if not summary:
            raise ToolError("summary cannot be empty.")
    if body is not None:
        body = body.strip()
        if not body:
            raise ToolError("body cannot be empty (omit it to keep the current body).")
        _check_size(body, MAX_BODY_CHARS, "body")
    if append:
        append = append.strip()
        _check_size(append, MAX_APPEND_CHARS, "appended text")
    from_project = _check_field(from_project, MAX_FIELD_CHARS, "from_project")
    if superseded_by is not None:
        superseded_by = _one_line(superseded_by)
    new_text = "\n".join(x for x in (title, summary, body, append, from_project) if x)
    _check_text(new_text + "\n" + _raw_words(tags) + "\n" + _raw_words(stack), allow_sensitive)
    if type is not None and type not in TYPES:
        raise ToolError(f"type must be one of: {', '.join(TYPES)}.")
    confidence = _confidence_from(confidence, verified)
    _check_choice(confidence, CONFIDENCES, "confidence")
    _check_choice(status, STATUSES, "status")
    _check_date(review_after, "review_after")
    if superseded_by and status not in (None, "superseded"):
        raise ToolError("superseded_by only goes with status 'superseded'.")
    with StoreLock():  # read-modify-write: another session must not update the same entry in between
        return _update_locked(id, title, summary, body, append, type, tags, stack, confidence, status,
                              superseded_by, review_after, confirm, from_project)


def _update_locked(id, title, summary, body, append, type, tags, stack, confidence, status, superseded_by,
                   review_after, confirm, from_project) -> str:
    entries = load_entries()
    e = next((x for x in entries if x["id"] == id), None)
    if e is None:
        raise ToolError(f"No entry with id '{id}'. Use memory_search or memory_list to find ids.")
    meta, old_body = parse_entry(read_entry_file(e["_path"]))
    _refuse_unsupported(meta, e["_path"])
    if title:
        meta["title"] = title
    if summary:
        meta["summary"] = summary
    if type:
        meta["type"] = type
    if tags is not None:
        meta["tags"] = clean_tags(tags)
    if stack is not None:
        meta["stack"] = clean_tags(stack)
    if confidence:
        meta["confidence"] = confidence
        meta.pop("verified", None)  # replaced by the explicit confidence
    if superseded_by:
        ids = {x["id"] for x in entries}
        if superseded_by == e["id"]:
            raise ToolError("An entry cannot supersede itself.")
        if not ID_RE.fullmatch(superseded_by) or superseded_by not in ids:
            raise ToolError(f"superseded_by: no entry with id '{superseded_by}'.")
        if _supersede_cycle(superseded_by, e["id"], entries):
            raise ToolError(f"superseded_by: '{superseded_by}' is itself (directly or through others) superseded by "
                            f"'{e['id']}'; that would hide both. Mark the other direction, or reactivate one first.")
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
        if review_after is None and _is_date(_as_str(meta.get("review_after"))) and meta["review_after"] < today():
            meta["review_after"] = ""  # the review is done
    new_body = body if body else old_body
    if append:
        who = f" ({from_project})" if from_project else ""
        new_body = new_body.rstrip() + f"\n\n**Update {today()}{who}:** {append}"
        if len(new_body) > MAX_BODY_CHARS:
            raise ToolError(f"Refused: with this addition the body would be {len(new_body)} characters (limit "
                            f"{MAX_BODY_CHARS}). Consolidate it: pass a rewritten, shorter body= instead of append=.")
    meta["id"] = e["id"]
    meta["updated"] = today()
    _write_atomic(e["_path"], render_entry(meta, new_body))
    warning = git_commit([e["_path"]], f"update: {e['id']}" + (f" (from {from_project})" if from_project else ""))
    return f"Updated [{e['id']}]." + warning


def _free_archive_path(arch: Path, name: str) -> Path:
    """Never overwrite an earlier archive of the same id."""
    dest, stem, n = arch / name, Path(name).stem, 2
    while dest.exists():
        dest, n = arch / f"{stem}-{n}.md", n + 1
    return dest


def t_delete(id, reason) -> str:
    reason = _check_field(reason, MAX_FIELD_CHARS, "reason")
    if not reason:
        raise ToolError("Give a reason (wrong, obsolete, duplicate of X...).")
    _check_text(reason, False)
    with StoreLock():
        entries = load_entries()
        e = next((x for x in entries if x["id"] == id), None)
        if e is None:
            raise ToolError(f"No entry with id '{id}'. Use memory_search or memory_list to find ids.")
        text = read_entry_file(e["_path"])
        meta, body = parse_entry(text)
        arch = store_dir() / "_archive"
        arch.mkdir(exist_ok=True)
        dest = _free_archive_path(arch, e["_path"].name)
        if meta.get("_unsupported"):  # keep the file byte for byte rather than rewrite it lossily
            _write_atomic(dest, e["_path"].read_bytes())
        else:
            meta["archived"] = today()
            meta["archive_reason"] = reason
            _write_atomic(dest, render_entry(meta, body))
        _unlink(e["_path"])
        warning = git_commit([e["_path"], dest], f"archive: {e['id']} ({reason[:60]})")
    dependents = [x["id"] for x in entries if x.get("superseded_by") == e["id"]]
    note = ""
    if dependents:
        note = (f"\nNote: {', '.join(dependents[:10])} pointed to it with superseded_by and stay hidden. If they "
                "should come back, call memory_update(id, status='active') on them.")
    return f"Archived [{e['id']}] to {dest}." + warning + note


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
        git_line = ("Git versioned: no (the store is inside another git repository; auto-commit only works when "
                    "the store folder is the root of its own repository)" if git_state(str(d)) == "nested"
                    else "Git versioned: no (run 'git init' in the store folder to keep a history)")
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
    files = [f for f in sorted(d.glob("*.md")) if is_entry_file(f)]
    ids = {f.stem for f in files}
    problems, notices, links = [], [], {}
    for f in files:
        def bad(msg):
            problems.append(f"{f.name}: {msg}")
        try:
            text = read_entry_file(f)
        except (OSError, UnicodeDecodeError) as ex:
            bad(f"unreadable ({type(ex).__name__})")
            continue
        if not _FM.match(text):
            bad("no front matter (the file must start with a '---' block)")
            continue
        meta, body = parse_entry(text)
        for reason in meta.get("_unsupported", []):
            bad(f"unsupported front matter, the file will not be rewritten by the tools: {reason}")
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
            if v not in (None, "", []) and not _is_date(v if isinstance(v, str) else repr(v)):
                bad(f"{k} is not a real date like 2026-12-31: '{v}'")
        for k in ("tags", "stack"):
            v = meta.get(k)
            if isinstance(v, str) and v.strip():
                bad(f"{k} should be a list like [a, b], got '{v}'")
        sup = _as_str(meta.get("superseded_by"))
        if sup and sup not in ids:
            bad(f"superseded_by points to a missing entry '{sup}'")
        if sup == f.stem:
            bad("superseded_by points to itself")
        elif sup:
            links[f.stem] = sup
        raw_id = meta.get("id")
        if raw_id not in (None, "", []) and raw_id != f.stem:
            bad(f"id '{raw_id}' differs from the file name (the file name wins)")
        if not body.strip():
            bad("empty body")
        e = normalize_entry(meta, body, f)
        if e["status"] == "needs-review":
            notices.append(f"{f.name}: needs review (review_after {meta.get('review_after') or '-'})")
    reported = set()
    for start in sorted(links):  # superseded_by loops hide every entry in them
        path, cur = [], start
        while cur in links and cur not in path:
            path.append(cur)
            cur = links[cur]
        if cur in path:
            loop = path[path.index(cur):]
            key = frozenset(loop)
            if key not in reported:
                reported.add(key)
                problems.append(f"superseded_by loop hides all of: {' -> '.join(loop + [cur])}")
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
    max_pairs = max(1, min(_int(max_pairs, MAX_PAIRS) or MAX_PAIRS, 100))

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
            "required": ["title", "summary", "body", "type"]},
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

def _bool(v, default=False) -> bool:
    """JSON booleans, and the strings/numbers some clients send instead ('false' must stay false)."""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y", "on")
    return default


def _opt_bool(v):
    return None if v is None else _bool(v)


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return default


def _text(v):
    """Optional text argument: None stays None, anything else becomes a string."""
    return None if v is None else (v if isinstance(v, str) else str(v))


def _h_add(a):
    return t_add(_text(a.get("title")), _text(a.get("summary")), _text(a.get("body")), _text(a.get("type")),
                 tags=a.get("tags"), stack=a.get("stack"), source=_text(a.get("source")),
                 origin=_text(a.get("origin")), verified=_opt_bool(a.get("verified")),
                 confidence=_text(a.get("confidence")), review_after=_text(a.get("review_after")),
                 session=_text(a.get("session")), force=_bool(a.get("force")),
                 allow_sensitive=_bool(a.get("allow_sensitive")))


def _h_update(a):
    if not isinstance(a.get("id"), str) or not a["id"]:
        raise ToolError("id is required (a string, from memory_search or memory_list).")
    return t_update(a["id"], title=_text(a.get("title")), summary=_text(a.get("summary")),
                    body=_text(a.get("body")), append=_text(a.get("append")), type=_text(a.get("type")),
                    tags=a.get("tags"), stack=a.get("stack"), verified=_opt_bool(a.get("verified")),
                    confidence=_text(a.get("confidence")), status=_text(a.get("status")),
                    superseded_by=_text(a.get("superseded_by")), review_after=_text(a.get("review_after")),
                    confirm=_bool(a.get("confirm")), from_project=_text(a.get("from_project")),
                    allow_sensitive=_bool(a.get("allow_sensitive")))


HANDLERS = {
    "memory_search": lambda a: t_search(_text(a.get("query")) or "", _text(a.get("stack")), _text(a.get("type")),
                                        _int(a.get("limit"), 8), _bool(a.get("include_superseded"))),
    "memory_get": lambda a: t_get(a.get("ids")),
    "memory_add": _h_add,
    "memory_update": _h_update,
    "memory_delete": lambda a: t_delete(_text(a.get("id")) or "", _text(a.get("reason"))),
    "memory_list": lambda a: t_list(_text(a.get("stack")), _text(a.get("type")), _text(a.get("tag"))),
    "memory_info": lambda a: t_info(),
    "memory_validate": lambda a: t_validate(),
    "memory_maintain": lambda a: t_maintain(_int(a.get("max_pairs"), MAX_PAIRS)),
}

INSTRUCTIONS = (
    "Shared memory = the user's knowledge base shared by all their projects. Search it (memory_search) before "
    "investigating a non-trivial problem from scratch. When you solve something reusable elsewhere, offer to "
    "save it (memory_add, or the /shared-memory:learn skill). Generalized wording, no secrets, no project-only facts.")


# --------------------------------------------------------------------------- MCP server

def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _handle(msg):
    """One JSON-RPC message -> reply dict, or None for notifications and client responses.
    Never raises: a malformed request gets a JSON-RPC error and the server keeps running."""
    if not isinstance(msg, dict):
        return _error(None, -32600, "Invalid Request: expected a JSON object")
    if "method" not in msg:
        return None  # a response to a server request (we send none) or junk: nothing to answer
    method, mid = msg.get("method"), msg.get("id")
    if "id" not in msg:  # notification
        return None
    valid_id = mid is None or isinstance(mid, str) or (isinstance(mid, (int, float)) and not isinstance(mid, bool)
                                                       and math.isfinite(mid))
    if not isinstance(method, str) or not valid_id:
        return _error(mid if valid_id else None, -32600, "Invalid Request")
    params = msg.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error(mid, -32602, "Invalid params: expected an object")
    try:
        if method == "initialize":
            global CLIENT_NAME
            client = params.get("clientInfo")
            client = client.get("name") if isinstance(client, dict) else ""
            CLIENT_NAME = re.sub(r"[^a-z0-9._-]+", "-", str(client or "").lower()).strip("-")[:40]
            version = params.get("protocolVersion")
            result = {"protocolVersion": version if isinstance(version, str) and version else "2025-06-18",
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "shared-memory", "version": VERSION},
                      "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments")
            if args is None:
                args = {}
            if not isinstance(name, str) or name not in HANDLERS:
                return _error(mid, -32602, f"Unknown tool {name!r}")
            if not isinstance(args, dict):
                return _error(mid, -32602, "Invalid params: 'arguments' must be an object")
            try:
                result = {"content": [{"type": "text", "text": HANDLERS[name](args)}], "isError": False}
            except ToolError as ex:
                result = {"content": [{"type": "text", "text": str(ex)}], "isError": True}
            except Exception as ex:  # a tool bug must not take the server down
                result = {"content": [{"type": "text", "text": f"Internal error: {type(ex).__name__}: {ex}"}],
                          "isError": True}
        elif method in ("resources/list", "prompts/list"):
            result = {method.split("/")[0]: []}
        else:
            return _error(mid, -32601, f"Method not found: {method}")
    except Exception as ex:
        return _error(mid, -32603, f"Internal error: {type(ex).__name__}")
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def mcp_main() -> None:
    out = sys.stdout.buffer

    def send(payload):
        out.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        out.flush()

    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send(_error(None, -32700, "Parse error"))
            continue
        if isinstance(msg, list):
            if not msg:
                send(_error(None, -32600, "Invalid Request: empty batch"))
                continue
            replies = [r for r in (_handle(m) for m in msg) if r is not None]
            if replies:
                send(replies)
        else:
            reply = _handle(msg)
            if reply is not None:
                send(reply)


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
    """Domain stacks of the project in cwd. Never raises: a broken project file only loses its signal."""
    try:
        return _detect_project(Path(cwd), vocabulary)
    except Exception:
        return []


def _detect_project(cwd: Path, vocabulary) -> list[str]:
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
            except ValueError:
                data = None
            deps = {}
            if isinstance(data, dict):
                for key in ("dependencies", "devDependencies"):
                    if isinstance(data.get(key), dict):
                        deps.update(data[key])
            add([v for k, v in DEP_TAGS.items() if k in deps])
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
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _emit(event: str, text: str) -> None:
    payload = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()


def _state_file(session_id) -> Path | None:
    """Per-session list of entries already shown. No usable session id: no state (sessions must not share one)."""
    sid = re.sub(r"[^A-Za-z0-9_-]", "", session_id if isinstance(session_id, str) else "")[:100]
    return Path(tempfile.gettempdir()) / f"claude-smem-{sid}.json" if sid else None


def _state_load(session_id) -> set:
    f = _state_file(session_id)
    try:
        return set(json.loads(f.read_text(encoding="utf-8"))) if f else set()
    except Exception:
        return set()


def _state_save(session_id, ids: set) -> None:
    f = _state_file(session_id)
    if f:
        try:
            f.write_text(json.dumps(sorted(ids)), encoding="utf-8")
        except OSError:
            pass


def _wsl_distro_installed() -> bool:
    """Windows: at least one WSL distribution registered (wsl.exe alone ships with every Windows 10/11)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Lxss") as k:
            return winreg.QueryInfoKey(k)[0] > 0
    except Exception:
        return False


def environment_stacks(agent: str = "claude-code") -> set[str]:
    """Stacks true for every project on this machine: the OS (plus WSL when available), and the agent."""
    env = {clean_tags([agent])[0]} if clean_tags([agent]) else set()
    if sys.platform.startswith(("win", "cygwin", "msys")):
        env.add("windows")
        if _wsl_distro_installed():
            env.add("wsl")
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


def env_names(env: set) -> set:
    """Stacks treated as environment: known OS and agent names, plus the current agent's name."""
    return ENV_STACKS | env


def relevance(e: dict, project: set, env: set) -> float:
    """Session-start score. 0 = not for this project.
    - entry with project stacks (xbox360, flutter...): needs one of them detected in the project;
    - entry with only environment stacks (windows, claude-code, codex...): needs that environment;
    - entry without stack: general, always a (low priority) candidate."""
    stacks = set(e["stack"])
    tag_bonus = 0.5 * len(set(e["tags"]) & project)
    project_stacks = stacks - env_names(env)
    if project_stacks:
        matched = project_stacks & project
        return 3.0 * len(matched) + len(stacks & env) + tag_bonus if matched else 0.0
    if stacks:
        matched = stacks & (env | project)
        return 1.0 * len(matched) + tag_bonus if matched else 0.0
    return 0.5 + tag_bonus


TITLE_LINE_MAX_CHARS = 140  # session-start and prompt lines


def _cut(s: str, n: int) -> str:
    s = _one_line(s)
    return s if len(s) <= n else s[:n - 3].rstrip() + "..."


def _short_line(e: dict) -> str:
    summary = _cut(e.get("summary") or "", SUMMARY_MAX_CHARS)
    return f"- [{e['id']}]{trust_marker(e)} {_cut(e['title'], TITLE_LINE_MAX_CHARS)}" + (f": {summary}" if summary else "")


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
    env = environment_stacks(agent)
    vocab = sorted({s for e in entries for s in e["stack"]} - env_names(env))  # env stacks never come from text
    detected = detect_project(cwd, vocab)
    project = set(detected)
    active = [e for e in entries if not is_superseded(e)]
    scored = [(relevance(e, project, env), str(e.get("updated") or e.get("created") or ""), e) for e in active]
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
    others = len(active) - len(candidates)  # superseded entries are not counted anywhere

    def render(chosen: list[dict]) -> str:
        lines = list(header)
        if chosen:
            lines.append(intro)
            lines += [_short_line(e) for e in chosen]
        chosen_ids = {e["id"] for e in chosen}
        rest = [e for e in candidates if e["id"] not in chosen_ids]
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
    for e in candidates:  # best first; a line that does not fit is skipped, shorter ones may still fit
        cost = len(_short_line(e)) + 1
        if used + cost > limit:
            continue
        chosen.append(e)
        used += cost
    text = render(chosen)
    while chosen and len(text) > limit:  # the "more entries" line can change length: trim until it fits
        chosen.pop()
        text = render(chosen)
    return text, {e["id"] for e in chosen}


def hook_session_start() -> None:
    data = _hook_input()
    cwd = Path(data["cwd"]) if isinstance(data.get("cwd"), str) and data["cwd"] else Path.cwd()
    text, listed = session_context(cwd, load_entries())
    _state_save(data.get("session_id", ""), listed)  # fresh list on startup/resume/clear/compact
    _emit("SessionStart", text)


def agent_compatible(e: dict, agent: str) -> bool:
    """For prompt suggestions: an entry about agents only ([codex]) is for that agent only. OS stacks are not
    filtered here: a prompt that clearly matches a [linux] entry is relevant on a Windows machine too
    (deploying to Linux servers), unlike the session-start list, which only shows the current OS."""
    stacks = set(e["stack"])
    agents = (AGENT_STACKS | {agent}) & stacks
    return not agents or agent in agents or bool(stacks - AGENT_STACKS - {agent})


def prompt_suggestions(prompt: str, entries: list[dict], seen: set, max_entries: int | None = None,
                       agent: str = "claude-code") -> list[dict]:
    p = prompt.strip() if isinstance(prompt, str) else ""
    if max_entries is None:
        max_entries = _env_int("SMEM_PROMPT_MAX", PROMPT_MAX_DEFAULT)
    max_entries = max(0, max_entries)
    if len(p) < 15 or p.startswith("/") or not entries or max_entries == 0:
        return []
    agent = clean_tags([agent])[0] if clean_tags([agent]) else ""
    entries = [e for e in entries if not is_superseded(e) and agent_compatible(e, agent)]
    results = [r for r in score_query(p[:4000], entries) if r["entry"]["id"] not in seen]
    if not results or results[0]["score"] < PROMPT_MIN_SCORE:
        return []
    top = results[0]["score"]
    picked = [r["entry"] for r in results
              if r["hits"] >= 2 and r["score"] >= PROMPT_REL_SCORE * top and r["coverage"] >= PROMPT_MIN_COVERAGE]
    return picked[:max_entries]


def prompt_context(picked: list[dict]) -> str:
    return ("Shared-memory entries (from the user's other projects) that may be relevant to this request. "
            "Read them with memory_get if useful; ignore them if not:\n" + "\n".join(_short_line(e) for e in picked))


def hook_prompt() -> None:
    data = _hook_input()
    if not isinstance(data, dict):
        return
    sid = data.get("session_id") if isinstance(data.get("session_id"), str) else ""
    seen = _state_load(sid)
    picked = prompt_suggestions(data.get("prompt", ""), load_entries(), seen)
    if not picked:
        return
    _state_save(sid, seen | {e["id"] for e in picked})
    _emit("UserPromptSubmit", prompt_context(picked))


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
    if a.prompt is not None:  # same text as the prompt hook (without its once-per-session memory)
        picked = prompt_suggestions(a.prompt, entries, set(), a.max, a.agent)
        return prompt_context(picked) if picked else ""
    budget = None if a.budget is None else max(0, a.budget)
    return session_context(Path(a.cwd), entries, budget, a.agent)[0]


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "mcp"
    if cmd == "mcp":
        mcp_main()
        return 0
    if cmd.startswith("hook-"):
        try:
            if cmd == "hook-session-start":
                hook_session_start()
            elif cmd == "hook-prompt":
                hook_prompt()
        except Exception:
            pass  # a hook must never break the session (unknown hook names do nothing)
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
