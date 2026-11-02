# claude-shared-memory

**One knowledge base shared by all your Claude Code projects.**

Every new Claude Code project starts from zero: the toolchain gotcha you fixed last month, the deployment trick, the profiler that worked, the platform limit you measured — all of it has to be rediscovered. This plugin keeps that knowledge in one place and brings the relevant part into every session, whatever the project.

- **Recall without asking**: at session start the plugin detects the project's stack and lists the entries that apply. When a request strongly matches an entry, it is suggested.
- **Capture on the spot**: `/shared-memory:learn` saves what was just discovered, rewritten so another project can use it.
- **Bootstrap from what you have**: `/shared-memory:harvest` reviews the per-project memories Claude Code already wrote and imports the reusable ones, with your approval.
- **Plain files**: one Markdown file per entry in a folder you choose, versioned by git. No database, no cloud, no dependency: Python standard library only.
- **Safe by default**: secrets are refused, private IPs and emails need explicit opt-in, duplicates are detected.

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

- `SMEM_DIR`: the knowledge folder (default `~/.claude-shared-memory`). Run `git init` in it to get one commit per change.
- `SMEM_PYTHON`: the Python command for the MCP server (default `python3`). On Windows, set it to `python` (the `python3` alias there is usually the Microsoft Store stub). Hooks find Python by themselves.

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
| `SessionStart` hook | Detects the stack (`package.json` deps, `pubspec.yaml`, `*.sln`, `platformio.ini`, `Cargo.toml`..., plus stack names found in the project's `CLAUDE.md`, `README.md` and Claude Code memory) and injects a short list of matching entries (titles only, ~1 line each). |
| `UserPromptSubmit` hook | Scores the prompt against the store (BM25) and suggests at most 3 strong matches, never twice in a session. Short prompts and slash commands are skipped. |
| MCP server `memory` | `memory_search`, `memory_get`, `memory_add`, `memory_update`, `memory_delete` (moves to `_archive/`), `memory_list`, `memory_info`. |
| Skills | `/shared-memory:learn [what]`, `/shared-memory:recall <topic>`, `/shared-memory:harvest [project]`. |

Always-on context cost is small (~340 tokens for the skill descriptions, plus the session-start list).

### Entry format

```markdown
---
id: xex-over-48-mb-fails-to-launch-from-aurora
title: "XEX over 48 MB fails to launch from Aurora"
summary: "Keep the XEX image under 0x03000000 bytes; xbdm launch still works."
type: gotcha            # gotcha | technique | tool | optimization | snippet | reference
tags: [xex, aurora]
stack: [xbox360]        # empty = general, listed in every project
source: my-emulator     # project where it was learned
created: 2026-10-20
verified: true
---

## Context
## Solution
## Why
```

You can edit, add or delete these files by hand; the plugin reads the folder on every call.

### Safety checks

`memory_add` and `memory_update` refuse private keys, cloud/GitHub/Slack tokens, JWTs, credentials in URLs and `password: value`-style lines. Placeholders such as `<PASSWORD>` or `${API_KEY}` are fine. Private IP addresses and email addresses are refused unless `allow_sensitive=true`. These are pattern checks, not a guarantee: review what goes into the store.

### Several machines

The store is a plain folder. Keep it local, or make it a git repo with a private remote and pull/push it yourself.

## CLI

```sh
python plugins/shared-memory/server/smem.py list
python plugins/shared-memory/server/smem.py search "aurora xex"
python plugins/shared-memory/server/smem.py detect /path/to/project   # what the SessionStart hook sees
```

## Development

```sh
python -m unittest discover -s tests -v
claude --plugin-dir plugins/shared-memory    # try local changes without reinstalling
```

## License

MIT
