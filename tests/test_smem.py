"""End-to-end tests for smem.py: MCP protocol, tools, safety checks, hooks, project detection.

Run: python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

SMEM = Path(__file__).resolve().parents[1] / "plugins" / "shared-memory" / "server" / "smem.py"

XEX = dict(title="XEX over 48 MB fails to launch from Aurora",
           summary="Keep the XEX image under 0x03000000 bytes or Aurora shows a game error; xbdm launch still works.",
           body="## Context\nAurora launch fails.\n## Solution\nShrink the image.\n## Why\nLoader limit.",
           type="gotcha", tags=["xex", "aurora"], stack=["xbox360"], source="emu")
NGINX = dict(title="Nginx reverse proxy breaks Next.js basePath under /admin",
             summary="Do not strip the /admin prefix in proxy_pass when Next.js has basePath=/admin.",
             body="## Context\n404 on assets.\n## Solution\nproxy_pass without trailing slash.\n## Why\nbasePath expects the prefix.",
             type="gotcha", tags=["nginx", "basepath"], stack=["nginx", "nextjs"], source="web")
WORKTREE = dict(title="Use git worktrees for parallel agents",
                summary="Give each parallel subagent its own git worktree and disjoint files.",
                body="## Context\nParallel agents.\n## Solution\nOne worktree each.\n## Why\nNo conflicts.",
                type="technique", tags=["git", "subagents"], stack=[])


class SmemTest(unittest.TestCase):
    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="smem-store-")
        self.env = dict(os.environ, SMEM_DIR=self.store)
        for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", self.store, *args], check=True)
        self.proc = subprocess.Popen([sys.executable, str(SMEM), "mcp"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, env=self.env)
        self.n = 0
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "shared-memory")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tearDown(self):
        self.proc.stdin.close()
        self.proc.stdout.close()
        self.proc.wait(timeout=10)
        shutil.rmtree(self.store, ignore_errors=True)

    # helpers
    def send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def rpc(self, method, params=None):
        self.n += 1
        self.send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        return json.loads(self.proc.stdout.readline())

    def tool(self, name, **args):
        res = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        return res["isError"], res["content"][0]["text"]

    def ok(self, name, **args):
        err, text = self.tool(name, **args)
        self.assertFalse(err, text)
        return text

    def hook(self, cmd, payload):
        r = subprocess.run([sys.executable, str(SMEM), cmd], input=json.dumps(payload).encode(),
                           capture_output=True, env=self.env, timeout=30)
        self.assertEqual(r.returncode, 0)
        out = r.stdout.decode("utf-8")
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""

    def seed(self):
        for e in (XEX, NGINX, WORKTREE):
            self.assertIn("Saved", self.ok("memory_add", **e))

    # tests
    def test_tools_list(self):
        names = {t["name"] for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertEqual(names, {"memory_search", "memory_get", "memory_add", "memory_update", "memory_delete",
                                 "memory_list", "memory_info", "memory_validate"})

    def test_agent_and_confidence_recorded(self):
        # setUp's initialize sent clientInfo name "test"
        self.ok("memory_add", **dict(XEX, verified=True))
        text = (Path(self.store) / "xex-over-48-mb-fails-to-launch-from-aurora.md").read_text(encoding="utf-8")
        self.assertIn("agent: test", text)
        self.assertIn("confidence: tested", text)  # legacy verified=true maps to tested
        self.assertNotIn("verified:", text)
        self.assertIn("0 problem(s)", self.ok("memory_validate"))

    def test_add_search_get_update_delete(self):
        self.seed()
        self.assertIn("xex-over-48-mb", self.ok("memory_search", query="Xbox 360 aurora launch"))
        self.assertIn("nginx-reverse-proxy", self.ok("memory_search", query="le proxy nginx casse le basePath"))
        self.assertIn("Updated", self.ok("memory_update", id="use-git-worktrees-for-parallel-agents",
                                         append="Confirmed again.", from_project="p2"))
        self.assertIn("Confirmed again.", self.ok("memory_get", ids=["use-git-worktrees-for-parallel-agents"]))
        self.assertIn("Archived", self.ok("memory_delete", id="use-git-worktrees-for-parallel-agents", reason="test"))
        self.assertIn("2 entries", self.ok("memory_list"))
        self.assertTrue((Path(self.store) / "_archive" / "use-git-worktrees-for-parallel-agents.md").exists())
        log = subprocess.run(["git", "-C", self.store, "log", "--oneline"], capture_output=True, text=True).stdout
        self.assertEqual(len(log.strip().splitlines()), 5)

    def test_duplicate_detection(self):
        self.seed()
        text = self.ok("memory_add", title="XEX bigger than 48 MB does not launch from Aurora",
                       summary="XEX image limit 48 MB on Aurora", body="x", type="gotcha", tags=["xex"])
        self.assertIn("Nothing written", text)
        self.assertIn("Saved", self.ok("memory_add", title="XEX bigger than 48 MB does not launch from Aurora",
                                       summary="XEX image limit 48 MB on Aurora", body="x", type="gotcha",
                                       tags=["xex"], force=True))

    def test_secrets_are_refused(self):
        for body in ("password: Hunter2xyz", "postgres://admin:s3cr3t@db:5432/x",
                     "ghp_" + "a" * 36, "-----BEGIN RSA PRIVATE KEY-----"):
            err, text = self.tool("memory_add", title="t " + body[:5], summary="s", body=body, type="tool", tags=["x"])
            self.assertTrue(err, body)
            self.assertIn("secret", text)

    def test_placeholders_are_accepted(self):
        self.ok("memory_add", title="Postgres connection string format", summary="Use DATABASE_URL",
                body="DATABASE_URL=postgres://<USER>:<PASSWORD>@host/db and password: <PASSWORD>",
                type="snippet", tags=["postgres"])

    def test_sensitive_needs_opt_in(self):
        err, _ = self.tool("memory_add", title="Console FTP", summary="s", body="ftp 192.168.1.20", type="tool",
                           tags=["ftp"])
        self.assertTrue(err)
        self.ok("memory_add", title="Console FTP", summary="s", body="ftp 192.168.1.20", type="tool", tags=["ftp"],
                allow_sensitive=True)

    def test_session_start_detects_stack(self):
        self.seed()
        proj = Path(tempfile.mkdtemp(prefix="smem-proj-"))
        try:
            (proj / "web").mkdir()
            (proj / "web" / "package.json").write_text(json.dumps({"dependencies": {"next": "14"}}))
            (proj / "CLAUDE.md").write_text("Deployed behind nginx.")
            ctx = self.hook("hook-session-start", {"cwd": str(proj), "session_id": uuid.uuid4().hex})
            self.assertIn("nginx-reverse-proxy", ctx)
            self.assertNotIn("xex-over-48-mb", ctx.split("General entries")[0])
            self.assertIn("use-git-worktrees", ctx)  # general entries are always listed
        finally:
            shutil.rmtree(proj, ignore_errors=True)

    def test_prompt_hook(self):
        self.seed()
        sid = uuid.uuid4().hex
        prompt = "mon nginx renvoie 404 sur les assets next.js sous /admin basePath"
        self.assertIn("nginx-reverse-proxy", self.hook("hook-prompt", {"prompt": prompt, "session_id": sid}))
        self.assertEqual(self.hook("hook-prompt", {"prompt": prompt, "session_id": sid}), "")  # once per session
        self.assertEqual(self.hook("hook-prompt", {"prompt": "ok go", "session_id": sid}), "")
        self.assertEqual(self.hook("hook-prompt", {"prompt": "add a share button to the profile screen",
                                                   "session_id": sid}), "")

    def test_hook_survives_bad_input(self):
        r = subprocess.run([sys.executable, str(SMEM), "hook-prompt"], input=b"not json", capture_output=True,
                           env=self.env, timeout=30)
        self.assertEqual(r.returncode, 0)


def load_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("smem_mod", SMEM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def entry(i, stack, title=None):
    return dict(title=title or f"Synthetic entry number {i} about widget {i}",
                summary=f"Summary {i} for frobnicator {i}.",
                body=f"## Context\nCase {i}.\n## Solution\nDo {i}.\n## Why\nBecause {i}.",
                type="gotcha", tags=[f"tag{i}"], stack=stack)


class StoreWritesTest(unittest.TestCase):
    """Locking, commits and failure reporting (in-process, temporary store)."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="smem-writes-")
        for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", self.store, *args], check=True)
        self.old_env = os.environ.get("SMEM_DIR")
        os.environ["SMEM_DIR"] = self.store
        self.m = load_module()

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop("SMEM_DIR", None)
        else:
            os.environ["SMEM_DIR"] = self.old_env
        shutil.rmtree(self.store, ignore_errors=True)

    def git(self, *args):
        return subprocess.run(["git", "-C", self.store, *args], capture_output=True, text=True).stdout

    def test_parallel_writers_all_commit(self):
        code = ("import importlib.util, sys; spec = importlib.util.spec_from_file_location('s', sys.argv[1]); "
                "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); i = int(sys.argv[2]); "
                "print(m.t_add(title=f'Parallel entry {i} about gizmo{i}', summary=f'Summary {i} zork{i}.', "
                "body='## Context\\nx\\n## Solution\\ny', type='tool', tags=[f't{i}']))")
        env = dict(os.environ, SMEM_DIR=self.store)
        procs = [subprocess.Popen([sys.executable, "-c", code, str(SMEM), str(i)], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env) for i in range(6)]
        outs = [p.communicate(timeout=120) for p in procs]
        for out, err in outs:
            self.assertIn(b"Saved", out, err)
            self.assertNotIn(b"Warning", out)
        self.assertEqual(len(self.git("log", "--oneline").splitlines()), 6)
        self.assertEqual(self.git("status", "--porcelain").strip(), "")
        self.assertFalse((Path(self.store) / ".smem.lock").exists())

    def test_commit_failure_is_reported(self):
        hook = Path(self.store) / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho refused by test hook >&2\nexit 1\n", encoding="utf-8")
        os.chmod(hook, 0o755)
        text = self.m.t_add(**entry(1, ["xbox360"]))
        self.assertIn("Saved", text)
        self.assertIn("git commit failed", text)
        self.assertIn("Uncommitted files (1)", self.m.t_info())

    def test_stale_lock_is_taken_over(self):
        lock = Path(self.store) / ".smem.lock"
        lock.write_text("12345\n")
        old = time.time() - 600
        os.utime(lock, (old, old))
        self.assertIn("Saved", self.m.t_add(**entry(2, [])))
        self.assertFalse(lock.exists())

    def test_busy_lock_times_out(self):
        (Path(self.store) / ".smem.lock").write_text("12345\n")
        with self.assertRaises(self.m.ToolError):
            with self.m.StoreLock(timeout=0.3):
                pass


class InjectionTest(unittest.TestCase):
    """Project detection, environment stacks, session-start budget, prompt-hook limits."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="smem-inject-")
        self.proj = Path(tempfile.mkdtemp(prefix="smem-proj-"))
        self.saved = {k: os.environ.get(k) for k in ("SMEM_DIR", "SMEM_CONTEXT_BUDGET", "SMEM_PROMPT_MAX")}
        os.environ["SMEM_DIR"] = self.store
        os.environ.pop("SMEM_CONTEXT_BUDGET", None)
        os.environ.pop("SMEM_PROMPT_MAX", None)
        self.m = load_module()
        self.os_stack = sorted(self.m.environment_stacks() - {"claude-code"})[0]

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.store, ignore_errors=True)
        shutil.rmtree(self.proj, ignore_errors=True)

    def add(self, i, stack, **kw):
        self.m.t_add(**entry(i, stack, **kw), force=True)

    def test_readme_mentions_do_not_select_a_stack(self):
        self.add(1, ["xbox360"])
        (self.proj / "README.md").write_text("Example: stack: [xbox360]\n", encoding="utf-8")
        text, listed = self.m.session_context(self.proj, self.m.load_entries())
        self.assertEqual(listed, set())
        (self.proj / "CLAUDE.md").write_text("Xbox 360 homebrew.\n", encoding="utf-8")
        text, listed = self.m.session_context(self.proj, self.m.load_entries())
        self.assertEqual(len(listed), 1)

    def test_environment_stacks(self):
        self.add(1, [self.os_stack])               # OS-only entry: every project on this OS
        self.add(2, ["claude-code"])               # agent-only entry: every project
        self.add(3, ["xbox360", "claude-code"])    # needs an xbox360 project
        self.add(4, [])                            # general
        ids = {e["id"]: e for e in self.m.load_entries()}
        _, listed = self.m.session_context(self.proj, list(ids.values()))
        titles = {ids[i]["title"].split(" about")[0] for i in listed}
        self.assertEqual(titles, {"Synthetic entry number 1", "Synthetic entry number 2", "Synthetic entry number 4"})

    def test_budget_limits_injected_text(self):
        (self.proj / "CLAUDE.md").write_text("Xbox 360 homebrew.\n", encoding="utf-8")
        for i in range(40):
            self.add(i, ["xbox360"])
        entries = self.m.load_entries()
        text, listed = self.m.session_context(self.proj, entries, budget_tokens=300)
        self.assertLessEqual(len(text), 300 * 4)
        self.assertTrue(0 < len(listed) < 40)
        self.assertIn(f"{40 - len(listed)} more relevant entries", text)
        os.environ["SMEM_CONTEXT_BUDGET"] = "0"
        text, listed = self.m.session_context(self.proj, entries)
        self.assertEqual(listed, set())
        self.assertIn("40 more relevant entries", text)

    def test_prompt_max(self):
        for i in range(3):
            self.add(i, ["docker"], title=f"Docker compose healthcheck readiness variant {i}")
        for i in range(10, 25):  # unrelated entries, so that docker words are rare (as in a real store)
            self.add(i, ["flutter"])
        entries = self.m.load_entries()
        prompt = "docker compose healthcheck readiness problem again"
        self.assertEqual(len(self.m.prompt_suggestions(prompt, entries, set())), 1)
        self.assertEqual(len(self.m.prompt_suggestions(prompt, entries, set(), max_entries=3)), 3)
        os.environ["SMEM_PROMPT_MAX"] = "0"
        self.assertEqual(self.m.prompt_suggestions(prompt, entries, set()), [])


V01_ENTRY = """---
id: old-entry
title: "Legacy gizmo frobnication fails on startup"
summary: "Restart the gizmo daemon before frobnicating."
type: gotcha
tags: [gizmo, daemon]
stack: [xbox360]
source: X--legacy-projects-Gizmo, OtherProject
created: 2026-01-01
verified: true
---

## Context
Old style entry.
"""


class TrustTest(unittest.TestCase):
    """Phase 3: confidence, status, review dates, supersede, validation, v0.1 compatibility."""

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="smem-trust-")
        self.saved = os.environ.get("SMEM_DIR")
        os.environ["SMEM_DIR"] = self.store
        self.m = load_module()

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("SMEM_DIR", None)
        else:
            os.environ["SMEM_DIR"] = self.saved
        shutil.rmtree(self.store, ignore_errors=True)

    def write(self, name, text):
        p = Path(self.store) / name
        p.write_text(text, encoding="utf-8")
        return p

    def get(self, entry_id):
        return next(e for e in self.m.load_entries() if e["id"] == entry_id)

    def test_v01_entry_reads_with_defaults_and_is_not_rewritten(self):
        p = self.write("old-entry.md", V01_ENTRY)
        before = p.read_bytes()
        e = self.get("old-entry")
        self.assertEqual(e["confidence"], "observed")  # verified: true was the v0.1 default
        self.assertEqual(e["status"], "active")
        self.assertEqual(e["source"], ["legacy-projects-Gizmo", "OtherProject"])  # unknown project: drive dropped
        self.m.t_search("gizmo daemon")
        self.m.session_context(Path(self.store), self.m.load_entries())
        self.assertEqual(p.read_bytes(), before)
        self.write("no-flag.md", V01_ENTRY.replace("verified: true\n", ""))
        self.assertEqual(self.get("no-flag")["confidence"], "assumed")
        self.write("unverified.md", V01_ENTRY.replace("verified: true", "verified: false"))
        self.assertEqual(self.get("unverified")["confidence"], "assumed")

    def test_yaml_block_lists(self):
        self.write("block.md", V01_ENTRY.replace("tags: [gizmo, daemon]", "tags:\n  - gizmo\n  - \"daemon\""))
        self.assertEqual(self.get("block")["tags"], ["gizmo", "daemon"])

    def test_confidence_orders_search(self):
        for conf, word in (("assumed", "alpha"), ("user-confirmed", "beta")):
            self.m.t_add(title=f"Widget frobnicator crash fix {word}", summary="Widget frobnicator crash on boot.",
                         body="## Context\nx", type="gotcha", tags=["widget"], confidence=conf, force=True)
        out = self.m.t_search("widget frobnicator crash")
        first = out.splitlines()[1]
        self.assertIn("beta", first)
        self.assertIn("[unverified]", out.splitlines()[2])

    def test_superseded_entries_are_hidden_but_kept(self):
        self.m.t_add(title="Old way to frobnicate widgets", summary="Use the old frobnicator flag.",
                     body="## Context\nx", type="technique", tags=["widget"], stack=["xbox360"])
        self.m.t_add(title="New way to frobnicate widgets", summary="Use the new frobnicator API.",
                     body="## Context\ny", type="technique", tags=["widget"], stack=["xbox360"], force=True)
        old, new = "old-way-to-frobnicate-widgets", "new-way-to-frobnicate-widgets"
        with self.assertRaises(self.m.ToolError):
            self.m.t_update(old, superseded_by="does-not-exist")
        with self.assertRaises(self.m.ToolError):
            self.m.t_update(old, superseded_by=old)
        self.m.t_update(old, superseded_by=new)
        self.assertTrue((Path(self.store) / f"{old}.md").exists())
        self.assertNotIn(old, self.m.t_search("frobnicate widgets"))
        self.assertIn(f"[superseded by {new}]", self.m.t_search("frobnicate widgets", include_superseded=True))
        proj = Path(tempfile.mkdtemp(prefix="smem-proj-"))
        try:
            (proj / "CLAUDE.md").write_text("Xbox 360 project\n", encoding="utf-8")
            _, listed = self.m.session_context(proj, self.m.load_entries())
        finally:
            shutil.rmtree(proj, ignore_errors=True)
        self.assertEqual(listed, {new})
        picked = self.m.prompt_suggestions("how do I frobnicate widgets, old way or new way?",
                                           self.m.load_entries(), set(), max_entries=5)
        self.assertNotIn(old, [e["id"] for e in picked])
        self.assertIn("0 problem(s)", self.m.t_validate())

    def test_review_after_and_confirm(self):
        self.m.t_add(title="Temporary workaround for gizmo 1.2", summary="Pin gizmo to 1.2 until the fix ships.",
                     body="## Context\nx", type="gotcha", tags=["gizmo"], review_after="2000-01-01")
        eid = "temporary-workaround-for-gizmo-1-2"
        self.assertEqual(self.get(eid)["status"], "needs-review")
        self.assertIn("[needs review]", self.m.t_search("gizmo workaround"))
        self.assertIn("1 entry to review", self.m.t_validate())
        self.m.t_update(eid, confirm=True)
        e = self.get(eid)
        self.assertEqual(e["status"], "active")
        self.assertEqual(e.get("last_verified"), self.m.today())
        self.assertFalse(e.get("review_after"))

    def test_bad_values_are_refused(self):
        with self.assertRaises(self.m.ToolError):
            self.m.t_add(title="t", summary="s", body="b", type="gotcha", tags=[], confidence="sure")
        with self.assertRaises(self.m.ToolError):
            self.m.t_add(title="t", summary="s", body="b", type="gotcha", tags=[], review_after="next week")

    def test_default_source_is_the_project_folder(self):
        proj = Path(tempfile.mkdtemp(prefix="proj-")) / "my-app"
        proj.mkdir()
        old_cwd = os.getcwd()
        try:
            os.chdir(proj)
            self.m.t_add(title="Gizmo needs a restart", summary="Restart gizmo.", body="b", type="gotcha", tags=[])
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(proj.parent, ignore_errors=True)
        self.assertEqual(self.get("gizmo-needs-a-restart")["source"], ["my-app"])

    def test_validate_reports_problems(self):
        self.write("ok.md", V01_ENTRY.replace("id: old-entry", "id: ok"))
        self.write("no-front-matter.md", "just text\n")
        self.write("bad-values.md", V01_ENTRY.replace("type: gotcha", "type: trick")
                   .replace("created: 2026-01-01", "created: yesterday\nconfidence: sure\nstatus: done"))
        self.write("bad-links.md", V01_ENTRY.replace("summary: \"Restart the gizmo daemon before frobnicating.\"\n", "")
                   .replace("stack: [xbox360]", "stack: xbox360, emulator\nsuperseded_by: missing-entry"))
        out = self.m.t_validate()
        for expected in ("no-front-matter.md: no front matter", "unknown type 'trick'", "unknown confidence 'sure'",
                         "unknown status 'done'", "created is not a date", "bad-links.md: missing summary",
                         "stack should be a list", "superseded_by points to a missing entry",
                         "id 'old-entry' differs from the file name"):
            self.assertIn(expected, out)
        self.assertNotIn("ok.md:", out)


if __name__ == "__main__":
    unittest.main()
