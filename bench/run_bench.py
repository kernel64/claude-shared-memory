#!/usr/bin/env python3
"""Retrieval benchmark for smem.py (search quality, prompt-hook precision, latency, injected tokens).

  python bench/run_bench.py                 full run on the synthetic dataset (sizes 100, 1000, 5000)
  python bench/run_bench.py --quick         quality + size 100 only
  python bench/run_bench.py --json out.json also write the raw numbers
  python bench/run_bench.py --store DIR --queries FILE
        quality only, on an existing store, read-only. FILE is JSON:
        [{"query": "...", "expected": ["id", ...]}, ...]  (expected [] = nothing relevant)

Token counts are estimates: characters / 4.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
SMEM_PATH = HERE.parent / "plugins" / "shared-memory" / "server" / "smem.py"
sys.path.insert(0, str(HERE))
import dataset  # noqa: E402

KS = (1, 3, 5)
LATENCY_QUERIES = ["docker compose postgres not ready", "esp32 brownout wifi", "le jeu plante au lancement",
                   "nginx websocket upgrade headers", "rust slow linker"]


def load_smem():
    spec = importlib.util.spec_from_file_location("smem_bench", SMEM_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tokens_estimate(text: str) -> int:
    return len(text) // 4


def entry_body(e: dict) -> str:
    return f"## Context\n{e['context']}\n\n## Solution\n{e['solution']}\n\n## Why\n{e['why']}"


def write_store(m, directory: Path, entries: list[dict], total: int | None = None, seed: int = 1) -> None:
    """Write the dataset as Markdown entries. With total > len(entries), add noisy clones (latency only)."""
    rnd = random.Random(seed)
    vocab = sorted({w for e in entries for w in m.tokens(entry_body(e))})
    total = total or len(entries)
    for i in range(total):
        e = entries[i % len(entries)]
        clone = i // len(entries)
        eid = e["id"] if clone == 0 else f"{e['id']}-c{clone}"
        body = entry_body(e)
        title = e["title"]
        if clone:
            title += f" (variant {clone})"
            body += "\n\n" + " ".join(rnd.choice(vocab) for _ in range(30))
        meta = {"id": eid, "title": title, "summary": e["summary"], "type": e["type"], "tags": e["tags"],
                "stack": e["stack"], "source": "bench", "created": "2026-01-01", "verified": True}
        (directory / f"{eid}.md").write_text(m.render_entry(meta, body), encoding="utf-8")


# ----------------------------------------------------------------------------- quality

def quality(m, entries: list[dict], queries: list[dict]) -> dict:
    rows = []
    for q in queries:
        if hasattr(m, "search_entries"):  # same ranking as memory_search (trust-weighted, v0.2+)
            ids = [r["entry"]["id"] for r in m.search_entries(q["query"], entries)]
        else:
            ids = [e["id"] for _, _, e in m.rank(q["query"], entries)[0]]
        exp = set(q["expected"])
        suggested = [e["id"] for e in m.prompt_suggestions(q["query"], entries, set())]
        row = {"query": q["query"], "lang": q.get("lang", "?"), "kind": q.get("kind", "?"),
               "expected": sorted(exp), "top3": ids[:3], "suggested": suggested}
        if exp:
            first = next((i + 1 for i, x in enumerate(ids) if x in exp), None)
            row["rr"] = 1.0 / first if first else 0.0
            row["first_rank"] = first
            for k in KS:
                row[f"recall@{k}"] = len(exp & set(ids[:k])) / len(exp)
            row["hook_hit"] = bool(exp & set(suggested))
            row["hook_relevant"] = len(exp & set(suggested))
        row["hook_count"] = len(suggested)
        rows.append(row)
    return {"rows": rows, "summary": summarize(rows)}


def summarize(rows: list[dict]) -> dict:
    def agg(subset):
        rel = [r for r in subset if r["expected"]]
        unrel = [r for r in subset if not r["expected"]]
        out = {"queries": len(subset)}
        if rel:
            for k in KS:
                out[f"recall@{k}"] = round(statistics.mean(r[f"recall@{k}"] for r in rel), 3)
            out["mrr"] = round(statistics.mean(r["rr"] for r in rel), 3)
            out["hook_hit_rate"] = round(sum(r["hook_hit"] for r in rel) / len(rel), 3)
        sugg = sum(r["hook_count"] for r in subset)
        relevant = sum(r.get("hook_relevant", 0) for r in subset)
        out["hook_suggestions"] = sugg
        out["hook_precision"] = round(relevant / sugg, 3) if sugg else None
        if unrel:
            out["unrelated_with_suggestion"] = f"{sum(1 for r in unrel if r['hook_count'])}/{len(unrel)}"
        return out

    groups = {"all": rows}
    for key in ("kind", "lang"):
        for v in sorted({r[key] for r in rows}):
            groups[f"{key}={v}"] = [r for r in rows if r[key] == v]
    return {g: agg(rs) for g, rs in groups.items()}


# ----------------------------------------------------------------------------- hooks, tokens, latency

def run_hook(cmd: str, payload: dict, store: Path) -> tuple[float, str]:
    env = dict(os.environ, SMEM_DIR=str(store))
    t = time.perf_counter()
    r = subprocess.run([sys.executable, str(SMEM_PATH), cmd], input=json.dumps(payload).encode(),
                       capture_output=True, env=env, timeout=120)
    ms = (time.perf_counter() - t) * 1000
    out = r.stdout.decode("utf-8")
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""
    return ms, text


def make_projects(root: Path) -> dict[str, Path]:
    paths = {}
    for name, files in dataset.PROJECTS.items():
        p = root / name
        for rel, content in files.items():
            f = p / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding="utf-8")
        paths[name] = p
    return paths


def injected_tokens(store: Path, projects: dict[str, Path], queries: list[dict]) -> dict:
    session = {}
    for name, p in projects.items():
        _, text = run_hook("hook-session-start", {"cwd": str(p), "session_id": uuid.uuid4().hex}, store)
        lines = [ln for ln in text.splitlines() if ln.startswith("- [")]
        session[name] = {"entries": len(lines), "tokens_est": tokens_estimate(text)}
    per_prompt = []
    for q in queries:
        _, text = run_hook("hook-prompt", {"prompt": q["query"], "session_id": uuid.uuid4().hex}, store)
        per_prompt.append(tokens_estimate(text))
    nonzero = [t for t in per_prompt if t]
    return {"session_start": session,
            "prompt_hook": {"prompts": len(per_prompt), "prompts_with_injection": len(nonzero),
                            "mean_tokens_est_when_injected": round(statistics.mean(nonzero)) if nonzero else 0,
                            "max_tokens_est": max(per_prompt) if per_prompt else 0}}


def latency(m, sizes, entries: list[dict], projects: dict[str, Path], runs: int = 3) -> dict:
    out = {}
    for n in sizes:
        d = Path(tempfile.mkdtemp(prefix=f"smem-bench-{n}-"))
        try:
            write_store(m, d, entries, total=n)
            os.environ["SMEM_DIR"] = str(d)
            loads, ranks = [], []
            for q in LATENCY_QUERIES:
                t0 = time.perf_counter()
                es = m.load_entries()
                t1 = time.perf_counter()
                m.rank(q, es)
                t2 = time.perf_counter()
                loads.append((t1 - t0) * 1000)
                ranks.append((t2 - t1) * 1000)
            prompt_ms = [run_hook("hook-prompt", {"prompt": LATENCY_QUERIES[i % len(LATENCY_QUERIES)],
                                                  "session_id": uuid.uuid4().hex}, d)[0] for i in range(runs)]
            start_ms = [run_hook("hook-session-start", {"cwd": str(projects["xbox-homebrew"]),
                                                        "session_id": uuid.uuid4().hex}, d)[0] for _ in range(runs)]
            out[n] = {"load_ms": round(statistics.median(loads)), "rank_ms": round(statistics.median(ranks)),
                      "search_total_ms": round(statistics.median(a + b for a, b in zip(loads, ranks))),
                      "hook_prompt_ms": round(statistics.median(prompt_ms)),
                      "hook_session_start_ms": round(statistics.median(start_ms))}
        finally:
            shutil.rmtree(d, ignore_errors=True)
    return out


def duplicates(m) -> dict:
    """Duplicate detection of the maintenance report: dataset entries plus DUPLICATES (rewritten copies)."""
    d = Path(tempfile.mkdtemp(prefix="smem-bench-dups-"))
    try:
        write_store(m, d, dataset.ENTRIES + dataset.DUPLICATES)
        os.environ["SMEM_DIR"] = str(d)
        es = m.load_entries()
        vecs = dict(zip([e["id"] for e in es], m.tfidf_vectors(es)))
        pairs = {frozenset(p) for p in dataset.DUPLICATE_OF.items()}
        true_scores = sorted(m.cosine(vecs[a], vecs[b]) for a, b in dataset.DUPLICATE_OF.items())
        ids = sorted(vecs)
        distinct_max = max(m.cosine(vecs[a], vecs[b]) for i, a in enumerate(ids) for b in ids[i + 1:]
                           if frozenset((a, b)) not in pairs)
        found = {frozenset((a["id"], b["id"])) for _, a, b in m.maintenance_report()["duplicates"]}
        return {"pairs": len(pairs), "found": len(found & pairs), "false_alarms": len(found - pairs),
                "true_min": round(true_scores[0], 2), "true_max": round(true_scores[-1], 2),
                "distinct_max": round(distinct_max, 2), "threshold": m.DUPLICATE_SIMILARITY}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def maintenance_latency(m, sizes) -> dict:
    out = {}
    for n in sizes:
        d = Path(tempfile.mkdtemp(prefix=f"smem-bench-maint-{n}-"))
        try:
            write_store(m, d, dataset.ENTRIES, total=n)
            os.environ["SMEM_DIR"] = str(d)
            t = time.perf_counter()
            m.maintenance_report()
            out[n] = round(time.perf_counter() - t, 2)
        finally:
            shutil.rmtree(d, ignore_errors=True)
    return out


# ----------------------------------------------------------------------------- report

def print_report(res: dict) -> None:
    print(f"# smem retrieval benchmark ({res['dataset']})\n")
    print("## Search quality (memory_search ranking) and prompt hook\n")
    cols = ["queries", "recall@1", "recall@3", "recall@5", "mrr", "hook_hit_rate", "hook_suggestions",
            "hook_precision", "unrelated_with_suggestion"]
    print("| group | " + " | ".join(cols) + " |")
    print("|---" * (len(cols) + 1) + "|")
    for g, s in res["quality"]["summary"].items():
        print(f"| {g} | " + " | ".join("-" if s.get(c) is None else str(s.get(c)) for c in cols) + " |")
    misses = [r for r in res["quality"]["rows"] if r["expected"] and r["rr"] < 1]
    if misses:
        print("\nQueries whose expected entry is not ranked first:")
        for r in misses:
            print(f"- [{r['kind']}/{r['lang']}] rank {r['first_rank'] or 'not found'}: {r['query'][:80]}"
                  f"  (expected {', '.join(r['expected'])}; top {r['top3'][:1]})")
    fps = [r for r in res["quality"]["rows"] if r["hook_count"] and not (set(r["expected"]) & set(r["suggested"]))]
    if fps:
        print("\nPrompt-hook suggestions with no relevant entry:")
        for r in fps:
            print(f"- [{r['kind']}/{r['lang']}] {r['query'][:70]} -> {', '.join(r['suggested'])}")
    if "tokens" in res:
        print("\n## Injected context (tokens are estimates: chars/4)\n")
        print("| synthetic project | entries listed | tokens (est.) |\n|---|---|---|")
        for name, v in res["tokens"]["session_start"].items():
            print(f"| {name} | {v['entries']} | {v['tokens_est']} |")
        p = res["tokens"]["prompt_hook"]
        print(f"\nPrompt hook: injected on {p['prompts_with_injection']}/{p['prompts']} prompts, "
              f"mean {p['mean_tokens_est_when_injected']} tokens when injected, max {p['max_tokens_est']}.")
    if "latency" in res:
        print("\n## Latency (median ms; hooks include Python process start; stores are noisy clones)\n")
        print("| entries | load | rank | search total | prompt hook | session-start hook |\n|---|---|---|---|---|---|")
        for n, v in res["latency"].items():
            print(f"| {n} | {v['load_ms']} | {v['rank_ms']} | {v['search_total_ms']} | {v['hook_prompt_ms']} | "
                  f"{v['hook_session_start_ms']} |")
    if "duplicates" in res:
        d = res["duplicates"]
        print(f"\n## Maintenance report\n\nDuplicates: {d['found']}/{d['pairs']} rewritten copies found, "
              f"{d['false_alarms']} false alarms (threshold {d['threshold']}). Similarity of the copies: "
              f"{d['true_min']} to {d['true_max']}; closest distinct pair: {d['distinct_max']}.")
        print("Report time: " + ", ".join(f"{n} entries {s} s" for n, s in res["maintenance_latency"].items()))
    print(f"\nPython {res['python']}, {res['platform']}, smem {res['smem_version']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="quality + latency at 100 entries only")
    ap.add_argument("--sizes", default="100,1000,5000", help="store sizes for latency")
    ap.add_argument("--json", help="write raw results to this file")
    ap.add_argument("--store", help="existing store to evaluate (read-only, quality only)")
    ap.add_argument("--queries", help="JSON query file for --store")
    args = ap.parse_args(argv)
    m = load_smem()
    res = {"python": sys.version.split()[0], "platform": sys.platform, "smem_version": m.VERSION}

    if args.store:
        if not args.queries:
            ap.error("--store needs --queries")
        os.environ["SMEM_DIR"] = args.store
        queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))
        for q in queries:
            q.setdefault("lang", "?")
            q.setdefault("kind", "custom")
        res["dataset"] = f"store {args.store}, {len(queries)} queries"
        res["quality"] = quality(m, m.load_entries(), queries)
    else:
        work = Path(tempfile.mkdtemp(prefix="smem-bench-"))
        try:
            store = work / "store"
            store.mkdir()
            write_store(m, store, dataset.ENTRIES)
            os.environ["SMEM_DIR"] = str(store)
            res["dataset"] = f"synthetic, {len(dataset.ENTRIES)} entries, {len(dataset.QUERIES)} queries"
            res["quality"] = quality(m, m.load_entries(), dataset.QUERIES)
            projects = make_projects(work / "projects")
            res["tokens"] = injected_tokens(store, projects, dataset.QUERIES)
            sizes = [100] if args.quick else [int(s) for s in args.sizes.split(",") if s]
            res["latency"] = latency(m, sizes, dataset.ENTRIES, projects)
            res["duplicates"] = duplicates(m)
            res["maintenance_latency"] = maintenance_latency(m, [100] if args.quick else [100, 1000])
        finally:
            shutil.rmtree(work, ignore_errors=True)

    print_report(res)
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
