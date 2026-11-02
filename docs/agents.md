# Using the shared memory from other agents

The core is agent-neutral: one Python file (`plugins/shared-memory/server/smem.py`, standard library only) that speaks MCP over stdio and has a CLI. Claude Code gets the full integration (plugin, hooks, skills). Other agents can use the same store through MCP and the `context` command.

## Support status

| Agent | MCP tools | Session context | Status |
|---|---|---|---|
| Claude Code | plugin | `SessionStart` and `UserPromptSubmit` hooks | Tested (CI on Linux, macOS and Windows, plus real sessions) |
| Any MCP client | stdio server | `smem.py context` | Protocol tested with [MCP Inspector](https://github.com/modelcontextprotocol/inspector) 0.17.2 in CLI mode: tools/list returns the 9 tools, tools/call works |
| Codex CLI, Cursor, VS Code (Copilot agent mode), Gemini CLI | configuration below | instruction file below | **Untested.** The snippets follow each tool's documented MCP format; check your version's docs if a field differs |

## 1. Get the server

```sh
git clone https://github.com/kernel64/claude-shared-memory
# server: claude-shared-memory/plugins/shared-memory/server/smem.py
```

Requirements: Python 3.10+. Point every agent at the same store with `SMEM_DIR`, so they share one knowledge base.

## 2. Register the MCP server

The command is always `python <path>/smem.py mcp` (or `python3` on macOS/Linux) with the `SMEM_DIR` environment variable.

**Codex CLI** (`~/.codex/config.toml`):

```toml
[mcp_servers.shared-memory]
command = "python3"
args = ["/path/to/claude-shared-memory/plugins/shared-memory/server/smem.py", "mcp"]
env = { SMEM_DIR = "/path/to/your/knowledge" }
```

**Cursor** (`~/.cursor/mcp.json`) and **Gemini CLI** (`~/.gemini/settings.json`):

```json
{
  "mcpServers": {
    "shared-memory": {
      "command": "python3",
      "args": ["/path/to/claude-shared-memory/plugins/shared-memory/server/smem.py", "mcp"],
      "env": { "SMEM_DIR": "/path/to/your/knowledge" }
    }
  }
}
```

**VS Code** (`.vscode/mcp.json` in a workspace, or the user-level MCP configuration):

```json
{
  "servers": {
    "shared-memory": {
      "type": "stdio",
      "command": "python3",
      "args": ["/path/to/claude-shared-memory/plugins/shared-memory/server/smem.py", "mcp"],
      "env": { "SMEM_DIR": "/path/to/your/knowledge" }
    }
  }
}
```

The server records the MCP client name in the `agent` field of new entries, and uses the agent's working directory as the default `source` project.

## 3. Give the agent the session context

Agents without hooks do not get the relevant entries automatically. Two options:

- **Ask for it in the agent's instruction file** (`AGENTS.md`, `.cursor/rules`, `GEMINI.md`...):

  ```markdown
  ## Shared memory
  At the start of a task, run `python3 /path/to/smem.py context --cwd . --agent <your-agent-name>` and read the output:
  it lists knowledge learned in the user's other projects that applies here.
  Before investigating a non-trivial problem, call the `memory_search` MCP tool.
  When you solve something reusable in other projects, offer to save it with `memory_add` (generalized, no secrets).
  ```

- **Script it**: `smem.py context --cwd DIR --agent NAME` prints the same text the Claude Code `SessionStart` hook injects. `smem.py context --prompt "..."` prints what the prompt hook would suggest (empty when nothing matches).

`--agent` also acts as an environment stack: entries with `stack: [codex]` reach Codex sessions only, like `claude-code` entries reach Claude Code sessions.

## Not available outside Claude Code

The `learn`, `recall`, `harvest` and `maintain` skills are Claude Code skills. Other agents can follow the same steps by hand with the MCP tools: the skill files in `plugins/shared-memory/skills/*/SKILL.md` are plain instructions you can copy into the agent's rules.
