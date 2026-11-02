"""Checks on the synthetic benchmark dataset, plus a quick run of the benchmark script."""
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))
import dataset  # noqa: E402
import run_bench  # noqa: E402

spec = importlib.util.spec_from_file_location("smem_t", ROOT / "plugins" / "shared-memory" / "server" / "smem.py")
smem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smem)


class DatasetTest(unittest.TestCase):
    def test_sizes(self):
        self.assertGreaterEqual(len(dataset.ENTRIES), 80)
        self.assertGreaterEqual(len(dataset.QUERIES), 50)
        kinds = {q["kind"] for q in dataset.QUERIES}
        self.assertTrue({"error", "keyword", "paraphrase", "unrelated"} <= kinds)
        self.assertEqual({q["lang"] for q in dataset.QUERIES}, {"en", "fr"})

    def test_entries_valid(self):
        ids = [e["id"] for e in dataset.ENTRIES]
        self.assertEqual(len(ids), len(set(ids)), "duplicate ids")
        for e in dataset.ENTRIES:
            self.assertRegex(e["id"], r"^[a-z0-9-]+$")
            self.assertIn(e["type"], smem.TYPES, e["id"])
            for field in ("title", "summary", "context", "solution", "why"):
                self.assertTrue(e[field].strip(), f"{e['id']}: empty {field}")

    def test_expected_ids_exist(self):
        ids = {e["id"] for e in dataset.ENTRIES}
        for q in dataset.QUERIES:
            for x in q["expected"]:
                self.assertIn(x, ids, q["query"])
            self.assertEqual(not q["expected"], q["kind"] == "unrelated", q["query"])

    def test_entries_pass_the_safety_scan(self):
        for e in dataset.ENTRIES:
            text = "\n".join([e["title"], e["summary"], run_bench.entry_body(e)])
            blocked, sensitive = smem.scan(text)
            self.assertEqual((blocked, sensitive), ([], []), e["id"])


class BenchSmokeTest(unittest.TestCase):
    def test_quick_run(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "res.json"
            self.assertEqual(run_bench.main(["--quick", "--json", str(out)]), 0)
            res = json.loads(out.read_text(encoding="utf-8"))
        s = res["quality"]["summary"]["all"]
        self.assertEqual(s["queries"], len(dataset.QUERIES))
        for key in ("recall@1", "recall@5", "mrr", "hook_hit_rate"):
            self.assertTrue(0 <= s[key] <= 1, key)
        self.assertIn("100", {str(k) for k in res["latency"]})
        self.assertEqual(set(res["tokens"]["session_start"]), set(dataset.PROJECTS))


if __name__ == "__main__":
    unittest.main()
