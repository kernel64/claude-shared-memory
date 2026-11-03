#!/usr/bin/env bash
# Runs a smem.py hook (session-start, prompt or confirm) with the first Python >= 3.8 found:
# $SMEM_PYTHON, python3, python, py (the Windows Store 'python3' stub fails the check and is skipped).
# A hook must never break the session: always exit 0, print nothing on unknown names.
case "$1" in
  session-start|prompt|confirm) ;;
  *) exit 0 ;;
esac
dir="$(cd "$(dirname "$0")" && pwd)"
for p in "${SMEM_PYTHON:-}" python3 python py; do
  [ -n "$p" ] || continue
  if "$p" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1; then
    "$p" "$dir/smem.py" "hook-$1"
    exit 0
  fi
done
if [ "$1" = "confirm" ]; then  # no Python: still ask before a shared-memory write (fail closed)
  printf '%s' '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": "Shared memory: confirm this write (visible from all your projects)."}}'
fi
exit 0
