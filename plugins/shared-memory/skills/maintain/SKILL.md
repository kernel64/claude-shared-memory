---
name: maintain
description: Review the shared cross-project memory for duplicates, contradictions, entries due for review and old unverified entries, and fix them with the user's approval (merge, supersede, confirm, correct). Nothing is ever deleted. Use when the user says "maintain", "fais le ménage dans la mémoire partagée", "vérifie les doublons", "nettoie la mémoire".
argument-hint: "[stack to focus on (optional)]"
disable-model-invocation: true
---

# Maintain the shared memory

Focus: $ARGUMENTS (empty = whole store).

## 1. Report
Call `memory_maintain` (read-only). If it says "Nothing to do", tell the user in one line and stop. If a focus was given, keep only the cases touching that stack.

## 2. Judge each case
Read the entries involved with `memory_get` (batch the ids). For each case decide:
- **Probable duplicate**: same knowledge twice? Then *merge*: keep the most complete or most trusted entry, move what is missing from the other into it (`memory_update` with `append` or a rewritten `body`), then mark the other `superseded_by` the kept one. If they actually say different things, it is not a duplicate: say why and leave both.
- **Related pair**: do they contradict each other (different fix for the same symptom, different numbers, "always" vs "never")? If yes, decide which one holds from their evidence, confidence, dates and sources; if you cannot tell, say so and suggest how to check (a test, the docs). If they are complementary, nothing to do.
- **Needs review**: is it still true? If you can check it from this machine or from evidence in the entry, propose `confirm`; otherwise ask the user.
- **Old unverified**: propose a way to check it, or leave it marked unverified. Never upgrade confidence without evidence.
- **Too long**: propose a shorter body keeping the commands, error messages and numbers.
- **Format problem**: propose the exact fix.

## 3. Propose
Show the user a numbered list in French, one line per action, for example:
`3. Fusionner [b] dans [a] (même solution, [a] est tested) puis marquer [b] remplacée par [a]`
Group by type, at most ~15 actions at a time. Ask which to apply ("tout", "1-4", "tout sauf 3").

## 4. Apply only what was approved
- Merge: `memory_update(id=<kept>, append=... or body=..., from_project=<current folder>)`, then `memory_update(id=<other>, superseded_by=<kept>)`.
- Supersede: `memory_update(id=<old>, superseded_by=<new>)`.
- Confirm: `memory_update(id, confirm=true)` (add `confidence="tested"` only if you just tested it).
- Correct: `memory_update` with the corrected fields.
- **Never** call `memory_delete` unless the user explicitly asks to remove an entry; superseded entries stay on disk.

## 5. Report
Run `memory_maintain` again and give a two-line summary: what was applied, what is left and why.
