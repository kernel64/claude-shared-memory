# Design note: reminding the user to save what a session learned

Status: proposal, not implemented.

## Problem

Knowledge only reaches the shared memory when someone runs `/shared-memory:learn` or Claude offers it. Both are easy to forget at the end of a session that solved a hard problem.

## Constraints

- No spam: most sessions learn nothing reusable. A reminder on every session would be ignored, then disabled.
- No extra model turns by default: forcing Claude to continue costs tokens and interrupts the user.
- Deterministic and cheap: hooks run as plain Python, with no LLM call (project rule).

## Options

| Option | How | Verdict |
|---|---|---|
| A. Instruction only | `CLAUDE.md` asks Claude to offer `/shared-memory:learn` after solving something reusable. | Already in place. Free, but depends on the model remembering at the right moment. |
| B. `Stop` hook that blocks | On a qualifying turn, return `decision: block` with a reason, so Claude continues and offers to save. | Works, but adds a model turn and can feel intrusive. Only acceptable if very selective. |
| C. `Stop` hook with a user-facing message | On a qualifying turn, return a `systemMessage` shown to the user only ("this session fixed X: `/shared-memory:learn`?"). The model is not involved. | Recommended: no token cost, the user decides. |
| D. `SessionEnd` hook | Same as C at session end. | The user has already left: the message is lost. |

## Proposed design (option C)

A `Stop` hook reads the transcript (`transcript_path` in the hook input) and shows a reminder **at most once per session** when all of these are true:

1. **A problem was solved**: a failing signal followed later by a passing one. Failing: a tool result with a non-zero exit code or an error/traceback pattern. Passing: the same command, or a test or build command, succeeding afterwards.
2. **It took effort**: at least N failed attempts (start with 3) or at least M minutes between the first failure and the success.
3. **Nothing was saved yet**: no `memory_add` or `memory_update` call in the session.
4. **Not a trivial session**: at least K user prompts (start with 3).

Message example: `Shared memory: this session fixed a build error after 4 attempts. /shared-memory:learn to keep it for other projects?`

## Open questions, to measure before building

- Precision of the "problem solved" signal on real transcripts (`~/.claude/projects/*/*.jsonl`, read locally). Target: under 1 reminder in 5 sessions, and most reminders judged useful by the user.
- Transcript format stability: the JSONL layout is internal to Claude Code and may change. The hook must fail silently.
- Cost on long transcripts: read only the tail, with a size cap.

## Next step

Build a read-only script that applies the four rules to the user's existing transcripts and reports which sessions would have triggered. The user reviews that list, then the thresholds are set, and only then is the hook written.
