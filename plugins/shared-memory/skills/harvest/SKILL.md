---
name: harvest
description: Import reusable knowledge from the per-project Claude Code memories (~/.claude/projects/*/memory) into the shared cross-project memory, with the user's approval for each entry. Use when the user says "harvest", "importe mes mémoires", "récupère les connaissances de mes projets".
argument-hint: "[project folder filter, e.g. my-app (optional)]"
disable-model-invocation: true
---

# Harvest project memories into shared memory

Filter: $ARGUMENTS (empty = all projects).

## 1. Collect
- Call `memory_info` to get the store path and the harvest log path (`.harvest-reviewed.txt`, one origin path per line: files already reviewed, approved or not). Read the log if it exists.
- List `~/.claude/projects/*/memory/*.md` (skip `MEMORY.md` and files already in the log). Apply the filter on the project folder name.
- Read the files. If there are more than ~40, split the work by project across a few subagents and have each one return candidates in the format of step 2 (they must not write anything).

## 2. Classify each memory
- **Shareable**: a gotcha, workaround, tool usage, technique, optimization or platform limit that would help another project. Keep it.
- **Preference**: how the user wants to work that applies everywhere. Collect it for `~/.claude/CLAUDE.md` (do not put it in shared memory).
- **Project-only**: decisions, status, plans, repo-specific rules. Skip.
- Anything containing credentials, passwords, tokens, personal data: never copy the sensitive part. If the useful knowledge can be kept without it, keep only that.
- Merge candidates that say the same thing in several projects into one entry (note all sources). Run `memory_search` to drop or merge with what the store already has.

## 3. Ask the user
Show a numbered list in French, grouped by stack, one line each: `N. [type] title (source project): summary`. Then the preference candidates separately. Ask which to keep (e.g. "tout", "tout sauf 3 et 7", "1-5"). Go in batches of about 20.

## 4. Write
- For each approved candidate: rewrite it following the `/shared-memory:learn` rules (generalized title, one-line summary, English body with Context / Solution / Why, stack, tags) and call `memory_add` with `source` = project folder and `origin` = the memory file path.
- For approved preferences: propose the exact lines to add to `~/.claude/CLAUDE.md` and add them once confirmed.
- Append every reviewed origin path (approved or rejected) to the harvest log so it is not proposed again.
- Do not edit or delete the project memories themselves.

## 5. Report
One short summary: how many added, merged, skipped, and the preferences added to CLAUDE.md.
