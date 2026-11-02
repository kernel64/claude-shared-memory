---
name: learn
description: Save a discovery from the current session into the shared cross-project memory (gotcha, workaround, tool, technique, optimization) so other projects can reuse it. Use when the user says "learn", "retiens ça pour les autres projets", "sauvegarde dans la mémoire partagée", or right after solving a non-trivial problem whose solution is reusable elsewhere.
argument-hint: "[what to remember (optional: otherwise taken from the conversation)]"
---

# Save to shared memory

The user wants to keep something learned here so their other projects benefit. What to save: $ARGUMENTS (if empty, take the most recent reusable finding from the conversation; if several, list them and save each one separately).

## 1. Gate: is it reusable elsewhere?
Ask yourself: "Would this save time in a *different* project?"
- Yes: a gotcha + its fix, a workaround, a tool and how to run it, a measured optimization, a platform limit, a reliable technique, a good default config.
- No: this repo's decisions, its status, its file paths, its business rules. Those belong in the project's own memory. Say so in one line and stop.
- A user preference about how to work (language, commit style...) belongs in `~/.claude/CLAUDE.md`: propose that edit instead.

## 2. Write it for a stranger project
- **Title**: specific and searchable ("Nginx strips basePath when proxying Next.js /admin", not "proxy issue").
- **Summary**: one line with the key fact or fix.
- **Body**, in English, with these sections:
  - `## Context`: when it applies, symptoms, the exact error message.
  - `## Solution`: concrete steps, commands, config, versions.
  - `## Why`: root cause or evidence (measurement, doc link).
  - `## Limits` (optional): where it does not apply, what was not tested.
- Generalize: replace project names, hosts, IPs, users, emails and paths specific to one repo with placeholders (`<APP_DIR>`, `<CONSOLE_IP>`). Tool install paths on this machine are fine (they help other projects).
- **Never** include passwords, tokens, keys, connection strings. The tool refuses them anyway.
- `type`: gotcha | technique | tool | optimization | snippet | reference.
- `stack`: the platforms it applies to (e.g. `xbox360`, `flutter`, `nestjs`, `esp32`, `android`, `windows`). Empty only if truly general.
- `tags`: lowercase tool/lib/error keywords.
- `source`: the current project folder name. `verified`: false if it was not actually tested.

## 3. Avoid duplicates
Run `memory_search` with the key words first. If an entry already covers it, `memory_get` it and `memory_update` it (`append` for a new finding, `from_project` = current folder) instead of adding.

## 4. Save and report
Call `memory_add`. If it answers "similar entries already exist", read them and decide: update one, or `force=true` if really different. Then tell the user in one line what was saved (id + title).
