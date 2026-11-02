# claude-shared-memory: project guide

A Claude Code plugin that gives every project one shared, local knowledge base (Markdown files), with an MCP server, two hooks and four skills. User-facing documentation is in `README.md`; this file is for working on the code.

## Layout

| Path | Role |
|---|---|
| `plugins/shared-memory/server/smem.py` | The whole engine, one file, Python standard library only: store, parsing, search, safety checks, maintenance, MCP server, hooks, CLI. |
| `plugins/shared-memory/server/hook.sh` | Finds a Python >= 3.8 (`$SMEM_PYTHON`, `python3`, `python`, `py`) and runs `smem.py hook-<name>`; always exits 0. |
| `plugins/shared-memory/hooks/hooks.json` | `SessionStart` (startup, resume, clear, compact) and `UserPromptSubmit` hooks. |
| `plugins/shared-memory/.mcp.json` | MCP server `memory`: `${SMEM_PYTHON:-python3} smem.py mcp`. |
| `plugins/shared-memory/skills/*/SKILL.md` | `learn`, `recall`, `harvest`, `maintain`: instructions only, no code. `harvest` and `maintain` are user-invoked only. |
| `plugins/shared-memory/.claude-plugin/plugin.json` | Plugin manifest; its `version` drives `claude plugin update`. |
| `.claude-plugin/marketplace.json` | Marketplace `kernel64` publishing the plugin. |
| `tests/` | `unittest` suites: `test_smem.py` (features), `test_audit.py` (regressions from the 0.2.0 audit, one test or more per finding), `test_bench.py` (dataset integrity, quick benchmark). CI runs them on Linux, macOS, Windows with Python 3.10 and 3.13. |
| `bench/` | Synthetic dataset (`dataset.py`) and benchmark (`run_bench.py`). |
| `docs/` | `agents.md` (other MCP clients), `design-learn-reminder.md` (proposal, not built). |

## smem.py sections (in file order)

1. Constants: types, field order, trust levels and weights, write limits, reserved ids, prompt-hook thresholds, budget defaults, environment stacks (`OS_STACKS`, `AGENT_STACKS`).
2. Store: `store_dir()` (`SMEM_DIR`), front-matter parser (YAML subset; unsupported syntax goes to `meta["_unsupported"]` and such files are never rewritten) and renderer (free-text fields always JSON-quoted), `load_entries()` → `normalize_entry()` (defaults for v0.1 files, effective `confidence` and `status`, one-line titles, string dates, `source` names), `_write_atomic()` (retries on Windows `PermissionError`, never leaves `.tmp-*` files), `StoreLock` (owner token), `git_state()` / `git_commit()` (only when the store is the root of its own repo; retries; returns a warning string), `uncommitted_files()`.
3. Text: `tokens()` (accents removed, other scripts kept, CJK bigrams, camelCase and letter/digit splits, light stemming; ASCII behaviour is pinned by a test), `score_query()` (BM25 + IDF coverage), `rank()`.
4. Safety: `SECRET_PATTERNS` / `SENSITIVE_PATTERNS` (compiled, bounded, with value checks: placeholders, code references, descriptive words), `scan()` (never echoes a match), `_check_text()`, `_check_size()` (length and log-dump limits), `_check_field()` (single-line fields, length caps).
5. Tools: `t_search` (trust-weighted via `search_entries`), `t_get`, `t_add`, `t_update`, `t_delete` (archive), `t_list`, `t_info`, `validate_store`/`t_validate`, `maintenance_report`/`t_maintain`; `TOOLS` schemas and `HANDLERS`.
6. MCP server: newline-delimited JSON-RPC over stdio; `_handle()` never raises (malformed requests get JSON-RPC errors); `HANDLERS` coerce argument types (`_bool`, `_int`, `_text`); `initialize` records the client name as `CLIENT_NAME` (stored as `agent`).
7. Project detection: file markers (root and first-level subfolders), `package.json` dependencies, stack names in the project's `CLAUDE.md` and Claude Code memory (not the README, never environment names); `environment_stacks(agent)` from the OS (plus `wsl` when available) and the agent. `detect_project()` never raises.
8. Hooks: `session_context()` (relevance, trust, date; token budget, long lines skipped), `prompt_suggestions()` (filters entries about other agents only); per-session state file in the temp folder so an entry is suggested once (no state without a session id).
9. CLI: `context`, `search`, `list`, `info`, `validate`, `maintain`, `detect`.

## Design rules

- **Standard library only** in `smem.py`. An optional dependency must stay optional, installed explicitly, and never download a model by itself.
- **Markdown files are the source of truth.** Any cache must be derived and rebuildable.
- **Backward compatible.** Old entries load unchanged (defaults are computed at load time, files are rewritten only by an explicit update). Never rename the `memory_*` tools or their existing parameters; add new ones.
- **Never delete knowledge silently.** `memory_delete` archives to `_archive/`; maintenance supersedes instead of deleting.
- **Deterministic code, judgment in skills.** Python parses, scores and flags; deciding contradictions, merges and wording is the agent's job through the skills, with the user's approval. No LLM call from Python.
- **No speculative features.** Thresholds and defaults come from measurements (`bench/`); when you change scoring, rerun the benchmark and update the numbers in `README.md`.
- **Public and generic.** No personal data, local paths, real project names or real knowledge entries in code, tests, fixtures, docs or commit messages. The benchmark dataset is synthetic.
- **Hooks never break a session**: they swallow errors and exit 0. The MCP server never exits on bad input.
- **Every text written is scanned and size-limited**, including metadata fields (`source`, `origin`, `session`, `from_project`, archive reason).
- **Fix with a regression test.** Each bug fix adds a test in `tests/test_audit.py` (or the relevant suite) that fails before the fix.

## Commands

```sh
python -m unittest discover -s tests -v       # full suite (~40 s)
python bench/run_bench.py --quick             # quality + latency at 100 entries; full run without --quick
claude plugin validate .                      # marketplace manifest
claude plugin validate plugins/shared-memory  # plugin manifest
claude --plugin-dir plugins/shared-memory     # try local changes in a session
python plugins/shared-memory/server/smem.py context --cwd <project>   # what a session would receive
```

Real-session check without touching a real store: run `claude -p --settings <file>` from a temporary project folder, with a settings file whose `env.SMEM_DIR` points to a temporary store.

## Testing conventions

- Tests always use a temporary `SMEM_DIR` (set before loading the module, restored after). Never point a test at a real store.
- Load `smem.py` with `importlib` (see `load_module()` in `tests/test_smem.py`); MCP tests spawn `smem.py mcp` and talk JSON-RPC.
- On Windows, `print()` writes CRLF: normalize line endings when comparing CLI output.
- Store tests create a git repo in the temporary store with a local `user.email` so commits work on CI.

## Release process

1. Update `CHANGELOG.md`, bump `version` in `plugin.json` and `VERSION` in `smem.py` (same value). `claude plugin update` only picks up a new version, not a new commit.
2. Run the tests and the full benchmark; update the README benchmark table if numbers changed.
3. Commit, push, wait for CI, then tag `vX.Y.Z` and create the GitHub release from the CHANGELOG section (no binaries).

## Windows notes

- Edit files with the editor tools rather than shell heredocs or `sed`: backslashes get mangled through the shell.
- The `python3` command on Windows is often the Microsoft Store stub; `hook.sh` skips it, the MCP server uses `SMEM_PYTHON`.
