# Changelog

## 0.2.1

Fixes from an independent audit of 0.2.0 (code review, docs-versus-code check and adversarial testing), then an independent re-check of the fixes against 0.2.0 for regressions. Each fix has a regression test in `tests/test_audit.py`. No migration: existing entries load unchanged (values that 0.2.0 wrote unquoted, such as `source: my app #2`, included), ASCII tokenization is identical (checked on 345,000 lines), search scores and benchmark results are identical, and tokenizing is not slower.

### Fixed: knowledge could be lost or hidden
- Windows: a write could fail with `PermissionError` while another session read the same file, leaving `.tmp-*.part` files. Writes and deletes are now retried and temporary files always cleaned up (41 failures in 300 concurrent writes before, 0 after).
- Archiving an id that was archived before overwrote the earlier archive: archives now get a free name (`id-2.md`).
- Titles such as "Index", "README" or "CON" produced invisible entries: these ids get an `-entry` suffix.
- A newline in `source` or `session` could inject front-matter keys (an entry could mark itself superseded), and a multi-line title could inject fake lines in the session context. Free-text fields are now single-line and always quoted.
- Supersede loops (A by B, B by A) hid every entry involved: they are refused, `superseded_by` must be an existing entry id, and `memory_validate` reports hand-made loops. Deleting an entry that others point to now says which entries stay hidden.
- Multi-line YAML values (`|`, `>`) and nested keys were silently flattened on the next update: such files are reported and never rewritten (archived byte for byte).

### Fixed: secret scanner
- Missed: `DB_PASSWORD=...` in prose, code, lists, YAML or JSON; lowercase, camelCase and suffixed keys (`db_password`, `aws_secret_access_key`, `clientSecret`, `refresh_token`, `DB_PASSWORD_PROD`, `GITHUB_TOKEN_RW`); `Authorization: Bearer/Basic`; `--password`, `mysql -p...`, `sshpass -p`, `.netrc`, `<password>` markup; tokens in URLs (including `redis://:password@host`) and `.npmrc`; PGP private keys; quoted passwords with spaces; values containing "example".
- Not scanned before: `source`, `origin`, `session`, `from_project`, the archive reason, and tags as given (normalizing them first hid secrets).
- False positives removed: code that reads or handles secrets (`os.environ['X']`, `process.env.X`, `Hash::make(...)`, indexing, type annotations, identifiers, f-string fields), `git@host:repo`, `icon@2x.png`, URLs, numbers, descriptive values and prose (`password: bcrypt-hashed`, `pwd: /path`, `secret: my-k8s-secret-name`, `password: stored in Bitwarden`, `Second pass: resolve symbols`), AWS's documented example key. On the Python standard library (900,000 lines): 68 lines flagged instead of 380.
- A refused value is never echoed back, even when short. Patterns are bounded (no slow regex on long input).
- Title (200), summary (500) and metadata (200) lengths are capped; appends cannot grow a body past 4,000 characters.

### Fixed: robustness
- One entry with a non-text date (`updated: [2026-01-01]`) or a `package.json` with `"dependencies": null` emptied the whole session context.
- Files saved with a UTF-8 byte order mark (Windows Notepad) were not recognized; end-of-line `# comments`, `'single quoted'` values and an empty front matter were misread. The README entry example now works as written. Values that would not read back identically (`true` as an id, `# x`) are written quoted; unclosed quoted values, flow mappings and anchors are reported and never rewritten.
- A reader (hook) could skip an entry while a writer replaced it on Windows: reads are retried. Git receives paths relative to the store (absolute ones failed on `subst` drives).
- A malformed MCP request (non-object `params`, non-string tool name, non-object `arguments`) crashed the server. It now answers with JSON-RPC errors, including parse errors (-32700), empty batches (-32600) and `id: null`.
- String booleans (`"false"`) were read as true; impossible dates (`2026-13-45`) were accepted.
- A store inside another git repository committed into that repository: auto-commit now requires the store to be the root of its own repository. Deleting a never-committed entry no longer reports a false git error. Git output is parsed in English with UTF-8 file names.
- The lock file holds an owner token: a process never removes a lock taken over by another one.
- Sessions without an id shared one "already suggested" list.
- `hook.sh` and `smem.py hook-<unknown>` printed help text into the session; they now stay silent.

### Fixed: behaviour that did not match the docs
- `--agent` as an environment stack: `[codex]` entries now reach Codex sessions only, and a project mentioning Codex no longer pulls them into Claude Code sessions. Prompt suggestions skip entries about other agents.
- Search ignored every non-Latin word: Arabic, Cyrillic and other scripts are now indexed, Chinese and Japanese by character pairs. ASCII tokenization is unchanged.
- Session budget: one long line no longer hides the shorter entries after it; superseded entries are no longer counted as "entries for other stacks"; long titles are cut in the list.
- Claude Code project folder names from Linux and macOS (`-home-user-my-app`) are now shown as project names.
- WSL is an environment on Windows when a WSL distribution is installed (`wsl.exe` alone ships with every Windows).
- The benchmark now reproduces the duplicate-detection and maintenance-time figures quoted in the README (`bench/dataset.py` `DUPLICATES`).

## 0.2.0

Upgrading from 0.1.0 needs no migration: existing entries are read as they are and only rewritten when updated.

### Added
- **Trust metadata**: optional `confidence` (`assumed`, `observed`, `tested`, `user-confirmed`), `status`, `superseded_by`, `last_verified`, `review_after`, `agent`, `session`. Search is weighted by trust; superseded entries are hidden from search, injection and prompt suggestions.
- `memory_update`: `confidence`, `status`, `superseded_by`, `review_after`, `confirm`.
- `memory_validate` tool and `smem.py validate`: format check of every entry.
- `memory_maintain` tool, `smem.py maintain` and the `/shared-memory:maintain` skill: read-only report of duplicates, related pairs, entries due for review, old unverified and overlong entries; fixes applied only with the user's approval, nothing deleted.
- `smem.py context`: the session-start (or prompt) context for any agent or script; `--agent` is also an environment stack. `docs/agents.md` for other MCP clients.
- `SMEM_CONTEXT_BUDGET` and `SMEM_PROMPT_MAX` settings.
- Benchmark (`bench/`): synthetic dataset, search quality, prompt-hook precision, latency, injected tokens.
- Secret scanner: GitLab, GitHub fine-grained, Stripe, npm, Hugging Face, Slack/Discord webhooks, Telegram, SendGrid, Google OAuth, Azure, PuTTY keys, secrets in env/config lines.

### Changed
- **Session start**: the list is cut to a token budget (600 by default) and the rest summarized; ordered by relevance, trust, then date. A real 25-entry store went from about 2,300 to 550 tokens per session.
- **Prompt hook**: one suggestion by default, only when the entry covers most of the prompt's known terms. Benchmark precision 44% to 100%; unrelated prompts no longer trigger.
- **Stack detection**: README mentions no longer select a stack; OS and agent stacks come from the system.
- `memory_add` writes `confidence` instead of `verified`, records the MCP client as `agent`, and defaults `source` to the project folder. Legacy `verified` is still accepted.
- `source` values written as Claude Code project folder names are shown as project names.
- Front matter accepts YAML block lists.

### Fixed
- Concurrent writes from several sessions could lose git commits silently: writes are serialized by a lock file, commits retried, and failures reported in the tool result; `memory_info` lists uncommitted files.
- Placeholders and plain words (`token: required`) no longer trigger the secret scanner.
- Overlong bodies, log dumps and stack traces are refused.

## 0.1.0

First release: Markdown store, MCP server (`memory_search`, `memory_get`, `memory_add`, `memory_update`, `memory_delete`, `memory_list`, `memory_info`), `SessionStart` and `UserPromptSubmit` hooks, `learn`, `recall` and `harvest` skills.
