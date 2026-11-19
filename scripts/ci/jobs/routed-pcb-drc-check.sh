#!/usr/bin/env bash
# scripts/ci/jobs/routed-pcb-drc-check.sh -- routed-PCB DRC gate (Issue #2546).
# The single copy of the `routed-pcb-drc-check` job's check command; both
# .github/workflows/ci.yml's `routed-pcb-drc-check` job and
# scripts/ci/local-gate.sh run this file (2am#7561 ruling).
#
# Usage:
#   routed-pcb-drc-check.sh FILE...
#       Check exactly the given committed routed PCBs (the workflow passes the
#       files scripts/ci/select_routed_pcbs.py selected from the PR/main diff;
#       only ORDINARY, non-native-requiring boards take this path -- the
#       board-04 paid-drill dispatch inside check_routed_drc_internal.py and
#       the docker-run native validation are the native path).
#   routed-pcb-drc-check.sh
#       Local mode: select the routed PCBs this branch modified (diff against
#       the merge-base with origin/main, mirroring CI's PR base-sha diff) and
#       check them.
#
# A file fails the gate if its error count exceeds the per-board allowlist
# (.github/routed-drc-tolerance.yml); unlisted files must report 0.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

if [ "$#" -eq 0 ]; then
  # Local mode: diff-driven file selection (no GitHub event context here).
  base="$(git merge-base origin/main HEAD 2>/dev/null || true)"
  if [ -z "$base" ]; then
    base="origin/main"
  fi
  echo "Diffing against base: $base"
  files=()
  while IFS= read -r f; do
    [ -n "$f" ] && files+=("$f")
  done < <(
    git diff --name-only --diff-filter=AM "$base"...HEAD \
      | grep -E '^boards/[^/]+/output/[^/]+_routed\.kicad_pcb$' || true
  )
  if [ ${#files[@]} -eq 0 ]; then
    echo "No routed PCBs modified -- nothing to check (green in CI too)."
    exit 0
  fi
  echo "Modified routed PCBs:"
  printf '  %s\n' "${files[@]}"
  set -- "${files[@]}"
fi

step "Run kct check on each modified routed PCB"
# Pass each file as a separate arg; the helper handles the allowlist
# comparison and emits ::error:: annotations.
uv run python scripts/ci/check_routed_drc_internal.py "$@"
