# Changelog

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
