#!/usr/bin/env bash
# test-check-ci-status.sh - Unit tests for check-ci-status.sh's workflow-run
# fold-in (issue #5495).
#
# Bug: analyze_status() only ever iterated over check-runs the Checks API
# already knows about. A workflow_run that is still `queued` (no jobs
# dispatched yet, so zero check-runs exist for it) was completely invisible
# to the pending count. If a handful of OTHER, faster/independent workflows
# for the same commit had already completed successfully,
# `success > 0 && pending == 0` was satisfied and the script reported
# overall "success" even though the primary CI workflow hadn't run a single
# job -- exactly the incident reproduced on commit
# 96c2b8246403c9c91d37c2c7d6eebf7558f790f4 in the issue (4 unrelated
# already-completed checks satisfied success>0/pending==0 while the "CI"
# workflow_run sat `pending` with zero jobs for 3+ minutes).
#
# check-ci-status.sh is a full CLI script (not sourced functions), so this
# is a black-box test: stub `gh` on PATH (mirrors test-check-duplicate.sh's
# convention) and invoke the real script as a subprocess, asserting on
# stdout/exit code. `LOOM_FORGE_TYPE=github` short-circuits forge_detect()
# before it ever touches the config-resolver tier chain or `git remote`, so
# no `git` stub is needed; every test passes an explicit `--commit`, so
# `git rev-parse HEAD` is never invoked either.
#
# Usage:
#   ./.loom/scripts/tests/test-check-ci-status.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
CCS="$SCRIPTS_DIR/check-ci-status.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if printf '%s' "$haystack" | grep -qF -- "$needle"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

if [[ ! -x "$CCS" ]]; then
    echo -e "${RED}FATAL${NC}: $CCS not found or not executable" >&2
    exit 2
fi

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

# --- Stub gh on PATH ---
#   gh repo view --json nameWithOwner --jq ...  -> "owner/repo"
#   gh api repos/{nwo}/commits/{sha}/check-runs ...        -> cat $STUB_DIR/check-runs.json
#                                                              (or {"total_count":0,"check_runs":[]};
#                                                              fails if $STUB_DIR/check-runs-fail exists)
#   gh api repos/{nwo}/commits/{sha}/status ...            -> cat $STUB_DIR/commit-status.json
#                                                              (or {"state":"unknown","statuses":[]};
#                                                              fails if $STUB_DIR/commit-status-fail exists)
#   gh api repos/{nwo}/actions/runs?head_sha=...&per_page=... -> cat $STUB_DIR/workflow-runs.json
#                                                              (or {"workflow_runs":[]};
#                                                              fails if $STUB_DIR/workflow-runs-fail exists)
#
# The stub deliberately outputs already-`--jq`-shaped JSON (matching the
# shape forge-helpers.sh's own --jq filters produce) rather than applying the
# real gh --jq filter itself -- the same convention test-check-duplicate.sh
# uses for its `gh api` stub.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"

case "$1" in
  repo)
    if [[ "$2" == "view" ]]; then
      echo "owner/repo"
      exit 0
    fi
    echo "stub gh: unhandled repo args: $*" >&2
    exit 3
    ;;
  api)
    path="$2"
    if [[ "$path" == *"/actions/runs?"* ]]; then
      if [[ -f "$STUB_DIR_FROM_ENV/workflow-runs-fail" ]]; then
        echo "stub gh: workflow runs api call failed" >&2
        exit 1
      fi
      canned="$STUB_DIR_FROM_ENV/workflow-runs.json"
      if [[ -f "$canned" ]]; then cat "$canned"; else echo '{"workflow_runs": []}'; fi
      exit 0
    elif [[ "$path" == *"/check-runs" ]]; then
      if [[ -f "$STUB_DIR_FROM_ENV/check-runs-fail" ]]; then
        echo "stub gh: check-runs api call failed" >&2
        exit 1
      fi
      canned="$STUB_DIR_FROM_ENV/check-runs.json"
      if [[ -f "$canned" ]]; then cat "$canned"; else echo '{"total_count": 0, "check_runs": []}'; fi
      exit 0
    elif [[ "$path" == *"/status" ]]; then
      if [[ -f "$STUB_DIR_FROM_ENV/commit-status-fail" ]]; then
        echo "stub gh: commit status api call failed" >&2
        exit 1
      fi
      canned="$STUB_DIR_FROM_ENV/commit-status.json"
      if [[ -f "$canned" ]]; then cat "$canned"; else echo '{"state": "unknown", "statuses": []}'; fi
      exit 0
    fi
    echo "stub gh: unhandled api args: $*" >&2
    exit 3
    ;;
  *)
    echo "stub gh: unhandled args: $*" >&2
    exit 3
    ;;
esac
STUB
chmod +x "$STUB_DIR/gh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"
export LOOM_FORGE_TYPE="github"

reset_state() {
    rm -f "$STUB_DIR"/check-runs.json "$STUB_DIR"/commit-status.json "$STUB_DIR"/workflow-runs.json
    rm -f "$STUB_DIR"/check-runs-fail "$STUB_DIR"/commit-status-fail "$STUB_DIR"/workflow-runs-fail
}

run_ccs() {
    # Runs check-ci-status.sh, capturing stdout to $OUT, exit to $RC.
    OUT="$("$CCS" "$@" 2>"$STUB_DIR/stderr.log")"
    RC=$?
}

SHA="96c2b8246403c9c91d37c2c7d6eebf7558f790f4"

# --- Fixture used by every scenario below: four unrelated, fast checks that
# have already completed successfully (CodeQL, Cargo Audit, Shellcheck, loc)
# -- exactly the four named in the issue's reproduction. ---
write_four_fast_checks() {
    cat > "$STUB_DIR/check-runs.json" <<EOF
{
  "total_count": 4,
  "check_runs": [
    {"name": "CodeQL Analysis", "status": "completed", "conclusion": "success", "html_url": "https://example.invalid/1"},
    {"name": "Cargo Audit", "status": "completed", "conclusion": "success", "html_url": "https://example.invalid/2"},
    {"name": "Shellcheck", "status": "completed", "conclusion": "success", "html_url": "https://example.invalid/3"},
    {"name": "loc", "status": "completed", "conclusion": "success", "html_url": "https://example.invalid/4"}
  ]
}
EOF
}

echo "Testing check-ci-status.sh workflow-run pending fold-in (#5495)..."

# (a) THE bug: 4 fast checks completed, but the primary "CI" workflow_run is
# still `queued` with zero dispatched jobs (so it has no check-runs entry at
# all). Must NOT report "success".
reset_state
write_four_fast_checks
cat > "$STUB_DIR/commit-status.json" <<'EOF'
{"state": "pending", "statuses": []}
EOF
cat > "$STUB_DIR/workflow-runs.json" <<'EOF'
{"workflow_runs": [{"name": "CI", "status": "queued", "conclusion": null}]}
EOF
run_ccs --commit "$SHA" --quiet
assert_eq "pending" "$OUT" "(a) Queued primary workflow run with 4 unrelated completed checks -> quiet output 'pending', not 'success'"
assert_eq "2" "$RC" "(a) Exit code 2 (pending)"

# (a2) Same fixture, but the primary run is `in_progress` instead of
# `queued` -- both non-completed states must be folded into pending.
reset_state
write_four_fast_checks
cat > "$STUB_DIR/workflow-runs.json" <<'EOF'
{"workflow_runs": [{"name": "CI", "status": "in_progress", "conclusion": null}]}
EOF
run_ccs --commit "$SHA" --quiet
assert_eq "pending" "$OUT" "(a2) in_progress primary workflow run -> quiet output 'pending'"

# (a3) --json output surfaces the workflow_runs array and the corrected
# pending count so callers other than --quiet can see the same signal.
reset_state
write_four_fast_checks
cat > "$STUB_DIR/workflow-runs.json" <<'EOF'
{"workflow_runs": [{"name": "CI", "status": "queued", "conclusion": null}]}
EOF
run_ccs --commit "$SHA" --json
status_field="$(echo "$OUT" | jq -r '.status')"
assert_eq "pending" "$status_field" "(a3) --json .status is 'pending'"
pending_count="$(echo "$OUT" | jq -r '.counts.pending')"
assert_eq "1" "$pending_count" "(a3) --json .counts.pending reflects the queued workflow run"
wf_status="$(echo "$OUT" | jq -r '.workflow_runs[0].status')"
assert_eq "queued" "$wf_status" "(a3) --json .workflow_runs[0].status is 'queued'"

# (b) Regression guard: no false negative introduced. Same 4 fast checks,
# but the workflow-run list is empty (nothing queued/in_progress for this
# commit) -> still reports "success", exactly like before this fix.
reset_state
write_four_fast_checks
cat > "$STUB_DIR/workflow-runs.json" <<'EOF'
{"workflow_runs": []}
EOF
run_ccs --commit "$SHA" --quiet
assert_eq "success" "$OUT" "(b) No pending workflow runs -> quiet output still 'success'"
assert_eq "0" "$RC" "(b) Exit code 0 (success)"

# (c) A `completed` workflow run must NOT be double-counted into pending --
# its job-level detail already arrived via check_runs.
reset_state
write_four_fast_checks
cat > "$STUB_DIR/workflow-runs.json" <<'EOF'
{"workflow_runs": [{"name": "CI", "status": "completed", "conclusion": "success"}]}
EOF
run_ccs --commit "$SHA" --quiet
assert_eq "success" "$OUT" "(c) completed workflow run -> quiet output 'success' (not double-counted)"

# (d) Graceful degradation: the workflow-runs API call itself fails (e.g.
# Actions scope/permissions issue). This must NOT break the script -- it
# should fall back to check-run-only behavior, exactly like the pre-fix
# script (fail-open: a failure here can only ever miss a true pending, never
# fabricate one).
reset_state
write_four_fast_checks
: > "$STUB_DIR/workflow-runs-fail"
run_ccs --commit "$SHA" --quiet
assert_eq "success" "$OUT" "(d) Workflow-runs API failure -> degrades gracefully, quiet output still 'success'"
assert_eq "0" "$RC" "(d) Exit code 0 (success) despite workflow-runs API failure"

# (e) The most literal form of the bug: check-runs are COMPLETELY empty
# (nothing has reported anything yet at all), but the workflow-run API
# already shows the primary run queued. Must report "pending", not
# "unknown" -- this is the "3+ minutes of pending with zero jobs" state from
# the issue's reproduction, before any of the fast checks even started.
reset_state
cat > "$STUB_DIR/check-runs.json" <<'EOF'
{"total_count": 0, "check_runs": []}
EOF
cat > "$STUB_DIR/commit-status.json" <<'EOF'
{"state": "pending", "statuses": []}
EOF
cat > "$STUB_DIR/workflow-runs.json" <<'EOF'
{"workflow_runs": [{"name": "CI", "status": "queued", "conclusion": null}]}
EOF
run_ccs --commit "$SHA" --quiet
assert_eq "pending" "$OUT" "(e) Zero check-runs anywhere, one queued workflow run -> quiet output 'pending'"
assert_eq "2" "$RC" "(e) Exit code 2 (pending)"

# (f) Human-readable output lists the queued workflow run distinctly under
# "Pending checks" (annotated as a workflow run, since it has no check-run
# name/status pairing of its own).
reset_state
write_four_fast_checks
cat > "$STUB_DIR/workflow-runs.json" <<'EOF'
{"workflow_runs": [{"name": "CI", "status": "queued", "conclusion": null}]}
EOF
run_ccs --commit "$SHA"
assert_contains "$OUT" "PENDING" "(f) Human-readable output shows overall PENDING"
assert_contains "$OUT" "CI: queued (workflow run)" "(f) Human-readable pending list includes the queued workflow run"

echo ""
echo "=== Test Summary ==="
echo "Total:  $TESTS_RUN"
echo -e "Passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
else
    echo -e "Failed: $TESTS_FAILED"
    exit 0
fi
