#!/usr/bin/env bash
# test-create-issue-curated.sh — create-issue.sh must not mint loom:curated
# without a points marker (#11787). The refusal fires before any forge call.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CREATE="$SCRIPT_DIR/../create-issue.sh"
fail=0
for body in "no marker here" 'inline `<!-- loom:points=3 -->` in prose'; do
  rc=0
  err="$("$CREATE" --title t --body "$body" --label loom:curated 2>&1)" || rc=$?
  if [[ "$rc" -eq 2 && "$err" == *"refusing --label loom:curated"* ]]; then
    echo "PASS: refused ($body)"
  else
    echo "FAIL: expected exit 2 refusal for ($body), got rc=$rc: $err"; fail=1
  fi
done
exit "$fail"
