# claude-shared-memory

**One knowledge base shared by all your Claude Code projects.**

Every new Claude Code project starts from zero: the toolchain gotcha you fixed last month, the deployment trick, the profiler that worked, the platform limit you measured. All of it has to be rediscovered. This plugin keeps that knowledge in one place and brings the relevant part into every session, whatever the project.

- **Recall without asking**: at session start the plugin detects the project's stack and lists the entries that apply. When a request clearly matches an entry, it is suggested.
- **Capture on the spot**: `/shared-memory:learn` saves what was just discovered, rewritten so another project can use it.
- **Bootstrap from what you have**: `/shared-memory:harvest` reviews the per-project memories Claude Code already wrote and imports the reusable ones, with your approval.
- **Trust and upkeep**: each entry says how sure it is (`assumed` to `user-confirmed`); `/shared-memory:maintain` finds duplicates and stale entries and fixes them with your approval, without deleting anything.
- **Plain files**: one Markdown file per entry in a folder you choose, versioned by git. No database, no cloud, no dependency: Python standard library only.

## Philosophy

- **Local-first.** The knowledge stays on your machine. No service, no API key, no telemetry.
- **Markdown is the source of truth.** You can read, edit, grep, diff and version every entry. Nothing is hidden in an index.
- **Deterministic code, judgment by the agent.** The Python code parses, scores and flags. Deciding whether two entries contradict each other, or how to generalize a finding, is done by Claude through skills, with your approval. The code never calls an LLM.
- **Never lose knowledge silently.** Entries are archived or marked superseded, never deleted by maintenance.
- **Small context footprint.** A token budget caps what is injected; the rest is one search away.

## Install

Requirements: Claude Code, Python 3.10+, git (optional, for history).

```
/plugin marketplace add kernel64/claude-shared-memory
/plugin install shared-memory@kernel64
```

Then set where your knowledge lives, in `~/.claude/settings.json`:

```json
{
  "env": {
    "SMEM_DIR": "/path/to/your/knowledge",
    "SMEM_PYTHON": "python"
  }
}
```

| Variable | Default | Meaning |
|---|---|---|
| `SMEM_DIR` | `~/.claude-shared-memory` | The knowledge folder. Run `git init` in it to get one commit per change. |
| `SMEM_PYTHON` | `python3` | Python command for the MCP server. On Windows set it to `python` (the `python3` alias there is usually the Microsoft Store stub). Hooks find Python by themselves. |
| `SMEM_CONTEXT_BUDGET` | `600` | Token budget of the whole session-start text (estimated as characters / 4). The header and footer (~110 tokens) are always included; `0` lists no entry. |
| `SMEM_PROMPT_MAX` | `1` | Maximum entries suggested per prompt (`0` disables prompt suggestions). |

Restart Claude Code. Check with `/mcp`, or ask Claude to call `memory_info`.

### Recommended: tell Claude when to use it

Add this to `~/.claude/CLAUDE.md`:

```markdown
## Shared memory across projects
- Before investigating a non-trivial problem from scratch (build/toolchain error, tool quirk, performance, deployment), run `memory_search` first.
- When you solve something that would save time in another project, offer to save it with `/shared-memory:learn`: generalized wording, no secrets, no project-only details.
- Project-specific facts stay in the project's own memory.
```

## How it works

| Part | What it does |
|---|---|
| `SessionStart` hook | Detects the project stack (`package.json` deps, `pubspec.yaml`, `*.sln`, `platformio.ini`, `Cargo.toml`..., plus stack names found in the project's `CLAUDE.md` and Claude Code memory) and the environment (OS, agent). Injects the most relevant entries (one line each) within the token budget, and summarizes the rest. |
| `UserPromptSubmit` hook | Scores the prompt against the store (BM25) and suggests the best entry only when it clearly matches (see below), never twice in a session. Short prompts and slash commands are skipped. |
| MCP server `memory` | `memory_search`, `memory_get`, `memory_add`, `memory_update`, `memory_delete` (moves to `_archive/`), `memory_list`, `memory_info`, `memory_validate`, `memory_maintain`. |
| Skills | `/shared-memory:learn [what]`, `/shared-memory:recall <topic>`, `/shared-memory:harvest [project]`, `/shared-memory:maintain [stack]`. |

### Scopes: which entries reach a session

There are two kinds of knowledge, kept in two places:

- **Project knowledge** (this repo's decisions, status, paths) stays in Claude Code's own per-project memory. `/shared-memory:harvest` is the way to promote what turns out to be reusable.
- **Shared knowledge** lives in this store. Its `stack` field decides where an entry applies:
  - **Domain stacks** (`xbox360`, `flutter`, `nestjs`...): listed when the project uses that stack.
  - **Environment stacks**: OS (`windows`, `linux`, `macos`, plus `wsl` when a WSL distribution is installed) and agent names (`claude-code`, `codex`, `cursor`, `gemini`, `copilot`, `continue`, `aider`, `opencode`, `windsurf`, `cline`, or the name given to `smem.py context --agent`). They come from the machine and the agent, never from project files. An entry made only of these applies to every project there (`[windows]` on Windows, `[codex]` in Codex sessions); mixed with a domain stack (`[xbox360, claude-code]`) it still needs the domain stack.
  - **No stack**: global knowledge, a low-priority candidate everywhere.

Entries are ordered by relevance (domain matches first, then environment, then global), then by trust, then newest first, and cut to the budget (a line that does not fit is skipped and shorter ones may still be listed).

A prompt gets a suggestion only if the best entry's BM25 score is high enough, it matches at least two query terms, and it covers at least 60% of the weight (IDF) of the prompt words the store knows. Entries about another agent only (`[codex]` in a Claude Code session) are never suggested; OS stacks are not filtered there, since a prompt that clearly matches a `[linux]` entry is relevant on a Windows machine too.

Search works in any language: accents are ignored, words in other scripts (Arabic, Cyrillic...) are kept, and Chinese or Japanese text is indexed by character pairs.

### Entry format

```markdown
---
id: xex-over-48-mb-fails-to-launch-from-aurora
title: "XEX over 48 MB fails to launch from Aurora"
summary: "Keep the XEX image under 0x03000000 bytes; xbdm launch still works."
type: gotcha            # gotcha | technique | tool | optimization | snippet | reference
tags: [xex, aurora]
stack: [xbox360]        # empty = global
source: my-emulator     # project where it was learned (default: the current project folder)
created: 2026-10-20
confidence: tested      # assumed | observed | tested | user-confirmed
# optional: status (active | needs-review | superseded), superseded_by, last_verified, review_after, agent, session
---

## Context
## Solution
## Why
```

You can edit, add or delete these files by hand; the plugin reads the folder on every call. `memory_validate` (or `smem.py validate`) checks them.

The front matter is a YAML subset: one `key: value` per line, inline lists `[a, b]` or block lists (`tags:` then `- a` lines), quoted strings, `true`/`false`, end-of-line `# comments`, and a UTF-8 byte order mark are fine. Multi-line values (`|`, `>`) and nested keys are reported by `memory_validate`, and the tools refuse to rewrite such a file (they would lose data): edit it by hand. A hand-written entry without `confidence` reads as `assumed`; `memory_add` writes `observed` by default. File names `README.md`, `index.md` and names Windows reserves (`con`, `nul`...) are not entries.

### Trust metadata

| Field | Meaning | Effect |
|---|---|---|
| `confidence` | `assumed` (believed, not checked), `observed` (seen happen), `tested` (reproduced or measured), `user-confirmed` | Search score x0.85 / x1 / x1.1 / x1.15; at equal relevance, more trusted entries are injected first. `assumed` entries are shown as `[unverified]`. |
| `review_after` | Date after which the entry should be re-checked | Past that date the entry becomes `needs-review` (shown, slightly down-weighted, never deleted). `memory_update(confirm=true)` records `last_verified` and ends the review. |
| `status: superseded`, `superseded_by` | Replaced by a newer entry | Hidden from search (unless `include_superseded`), from injection and from prompt suggestions. The file is kept. |
| `agent`, `session` | Provenance | `agent` is filled from the MCP client name. |

Supersede links cannot form a loop (the tools refuse it, and `memory_validate` reports hand-made ones). The legacy `verified` flag still works: in a file, `verified: true` reads as `observed` (it was the 0.1 default, so it does not prove a test); as a tool parameter, `verified=true` means `tested`.

### Maintenance

`/shared-memory:maintain` keeps the store clean without losing anything:

1. `memory_maintain` (or `smem.py maintain`) builds a read-only report: probable duplicates, related pairs on the same stack to compare for contradictions, entries past their review date, entries unverified for more than 90 days, bodies over 4,000 characters, format problems.
2. Claude reads the entries involved and judges each case (the code only finds candidates).
3. It proposes numbered actions: merge (update the kept entry, mark the other `superseded_by` it), supersede, confirm, correct.
4. It applies only what you approve. Nothing is deleted; superseded entries stay on disk.

Duplicates are pairs whose TF-IDF cosine similarity is at least 0.5; related pairs start at 0.25. On the benchmark (`bench/dataset.py` `DUPLICATES`), 8 entries rewritten in other words score 0.58 to 0.78 against their originals and are all found, while the closest distinct entries score 0.47 at most (no false alarm).

## Security

`memory_add`, `memory_update` and `memory_delete` check every text they write (title, summary, body, appended text, tags, `source`, `origin`, `session`, `from_project`, archive reason) and refuse:

- **Secrets**: private keys (PEM, OpenSSH, PGP, PuTTY), AWS, GitHub, GitLab, Stripe, npm, Hugging Face, Slack, Discord, Telegram, SendGrid, Google and Azure keys or tokens, `sk-...` API keys, JWTs, credentials or tokens in URLs, `Authorization: Bearer/Basic` values, `--password` options, `mysql -p...`, `sshpass -p`, `.netrc` lines, and `key: value` / `KEY=value` / JSON / YAML pairs whose key names a password, secret, token or key, with a prefix or suffix (`DB_PASSWORD`, `DB_PASSWORD_PROD`, `aws_secret_access_key`, `clientSecret`...). Fine: placeholders (`<PASSWORD>`, `${API_KEY}`, `your-key-here`), code that reads or handles a secret (`os.environ['X']`, `process.env.X`, `Hash::make(...)`, `self.keys[-1]`, type names, identifiers, f-string fields), URLs, paths, numbers, and descriptive words or prose (`password: required`, `password: stored in Bitwarden`, `Second pass: ...`). A refused value is never echoed back. On 900,000 lines of the Python standard library, 68 lines are flagged (380 with 0.2.0).
- **Personal or machine data** unless `allow_sensitive=true`: private IP addresses, email addresses (`git@host:repo` and `icon@2x.png` are not addresses).
- **Oversized or dumped text**: titles over 200 characters, summaries over 500, bodies over 4,000, appended text over 2,000 or making the body exceed 4,000, and long logs or stack traces (20 lines or more, mostly log lines). Titles and other single-line fields are flattened to one line, so they cannot break the front matter or the injected context.

These are pattern checks, not a guarantee: review what goes into the store, especially before sharing it.

**Writes and git.** Every write is serialized by a lock file (each process only removes its own lock) and retried when Windows reports the file in use by a reader. When the store folder is the root of its own git repository, every change is committed; a failed commit is reported in the tool result, and `memory_info` lists uncommitted files. A store placed inside another repository is never committed to it. `memory_delete` never overwrites an earlier archive of the same id.

## Other agents

The core is agent-neutral: the MCP server works with any MCP client, and `smem.py context --cwd DIR --agent NAME` prints the same context the Claude Code hooks inject. See [docs/agents.md](docs/agents.md) for Codex CLI, Cursor, VS Code and Gemini CLI configuration. Only Claude Code is tested end to end; the MCP protocol is also checked with MCP Inspector; the other agents are untested.

## Several machines

The store is a plain folder. Keep it local, or make it a git repo with a private remote and pull/push it yourself.

## CLI

```sh
python plugins/shared-memory/server/smem.py list
python plugins/shared-memory/server/smem.py search "aurora xex"
python plugins/shared-memory/server/smem.py context --cwd /path/to/project   # what a session would receive
python plugins/shared-memory/server/smem.py validate                          # format check (exit 1 on problems)
python plugins/shared-memory/server/smem.py maintain                          # maintenance report
```

## Benchmark

`python bench/run_bench.py` measures search quality, prompt-hook precision, injected tokens, duplicate detection and latency on a synthetic dataset (`bench/dataset.py`: 80 entries over 20 stacks, 59 English and French queries: exact error messages, keywords, paraphrases and unrelated prompts; 8 rewritten duplicates). Results for 0.2.1 (Windows 11, Python 3.12; latency varies with machine load):

| Measure | Result |
|---|---|
| Search: expected entry ranked first / in the top 5 | 93% / 100% (paraphrases: 80% / 100%) |
| Search MRR | 0.97 |
| Prompt hook: suggestions that are relevant | 100% (was 44% in 0.1.0) |
| Prompt hook: prompts with a relevant entry that get it | 86% |
| Prompt hook: unrelated prompts that get a suggestion | 0 of 8 (was 2 of 8) |
| Session-start context | 489 to 600 tokens (budget 600) |
| Prompt suggestion | 82 tokens on average when one is made |
| Maintenance report: rewritten duplicates found / false alarms | 8 of 8 / 0 |
| Search latency, 100 / 1,000 / 5,000 entries | 39 ms / 0.4 s / 2.1 s |
| Prompt hook latency (with Python start), 100 / 1,000 / 5,000 entries | 0.19 s / 0.59 s / 2.3 s |
| Maintenance report time, 100 / 1,000 entries | 0.1 s / 4.1 s |

Limits: the same author wrote the entries and the queries, so they share more vocabulary than real prompts do; treat quality scores as optimistic. Token counts are estimates (characters / 4). Latency stores are generated by cloning the dataset. `--store DIR --queries FILE` runs the quality part on your own store, read-only.

## Development

```sh
python -m unittest discover -s tests -v
python bench/run_bench.py
claude --plugin-dir plugins/shared-memory    # try local changes without reinstalling
```

See [CHANGELOG.md](CHANGELOG.md) for what changed between versions, and [docs/design-learn-reminder.md](docs/design-learn-reminder.md) for a proposed end-of-session reminder.

## License

MIT
