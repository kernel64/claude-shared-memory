#!/usr/bin/env bash
# Runs a smem.py hook with the first Python >= 3.8 found ($SMEM_PYTHON, python3, python).
# A hook must never break the session: always exit 0.
dir="$(cd "$(dirname "$0")" && pwd)"
for p in "${SMEM_PYTHON:-}" python3 python py; do
  [ -n "$p" ] || continue
  if "$p" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1; then
    exec "$p" "$dir/smem.py" "hook-$1"
  fi
done
exit 0
