"""End-to-end tests for smem.py: MCP protocol, tools, safety checks, hooks, project detection.

Run: python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
                                 "memory_list", "memory_info"})

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


if __name__ == "__main__":
    unittest.main()
