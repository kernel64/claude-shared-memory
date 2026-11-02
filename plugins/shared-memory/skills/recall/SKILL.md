---
name: recall
description: Look up the shared cross-project memory for a topic (what was already learned in the user's other projects) and apply it. Use when the user says "recall", "on a déjà vu ça ?", "regarde dans la mémoire partagée", or before researching a problem that may have been solved in another project.
argument-hint: "<topic, error message or question>"
---

# Recall from shared memory

Topic: $ARGUMENTS (if empty, use the problem currently being discussed).

1. Call `memory_search` with concrete keywords (tool names, platform, exact error text). If nothing comes back, retry once with synonyms or broader words, and try `memory_list` with the project's stack.
2. `memory_get` the promising entries (at most 5).
3. Answer: what the memory says, which project it came from, its confidence (`[unverified]` entries were never checked: say so), and how it applies here. Be clear when an entry only partly fits.
4. If you then use it: confirmed in this project → `memory_update(confirm=true, from_project=<current folder>)` (and `confidence="tested"` if you tested it); incomplete → `append`; wrong → tell the user and propose a corrected entry that supersedes it.
5. If nothing relevant exists, say so in one line and continue normally. Once solved, offer `/shared-memory:learn`.
