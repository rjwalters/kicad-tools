#!/usr/bin/env bash
# test-sync-labels-write-failures.sh - sync-labels.sh must not report success
# when label writes fail (issue #11746).
#
# A fleet-wide sync once hit GitHub's secondary rate limit: 615 label writes
# failed and every run still exited 0. Now a write that still fails after the
# rate-limit retry makes the run exit 5 and print "N of M label writes failed";
# a rate limit that clears on retry is absorbed and exits 0.
#
# Black-box: a stub `gh` on PATH, the real script as a subprocess, run from a
# registered checkout of its target (#9548, write-scope-fixture.sh).
#
# Usage:
#   ./defaults/scripts/tests/test-sync-labels-write-failures.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/write-scope-fixture.sh
source "$TEST_DIR/lib/write-scope-fixture.sh"
SLS="$(cd "$TEST_DIR/.." && pwd)/sync-labels.sh"
# shellcheck source=lib/assert.sh
source "$TEST_DIR/lib/assert.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
STUB_DIR="$TMP/bin"
mkdir -p "$STUB_DIR"

# Stub gh. `label create|edit` consult:
#   LOOM_TEST_FAIL_LABEL   label whose writes fail
#   LOOM_TEST_FAIL_MODE    hard (plain error, forever) | ratelimit (secondary
#                          rate limit on the first LOOM_TEST_RL_TIMES tries,
#                          counted in $LOOM_TEST_STATE)
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${LOOM_TEST_GH_LOG:?}"
case "$1" in
  repo)
    if [[ "${2:-}" == "view" && -n "${3:-}" && "$3" != -* ]]; then printf 'ADMIN\n'; exit 0; fi
    echo "stub gh: no remote" >&2; exit 1 ;;
  label)
    case "$2" in
      list) exit 0 ;;
      create|edit)
        if [[ "$3" == "${LOOM_TEST_FAIL_LABEL:-}" ]]; then
          case "${LOOM_TEST_FAIL_MODE:-hard}" in
            hard) echo "HTTP 500: stub server error" >&2; exit 1 ;;
            ratelimit)
              n=$(cat "$LOOM_TEST_STATE" 2>/dev/null || echo 0)
              if [[ "$n" -lt "${LOOM_TEST_RL_TIMES:-1}" ]]; then
                echo $((n + 1)) > "$LOOM_TEST_STATE"
                echo "HTTP 403: You have exceeded a secondary rate limit. Retry-After: 1" >&2
                exit 1
              fi ;;
          esac
        fi
        exit 0 ;;
    esac ;;
  api) exit 0 ;;
esac
echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

FIX="$TMP/ws"
mkdir -p "$FIX/.github"
cat > "$FIX/.github/labels.yml" <<'YML'
# BEGIN LOOM LABELS
- name: loom:issue
  description: "Approved and ready for a Builder"
  color: "3B82F6"
- name: loom:pr
  description: "Approved pull request"
  color: "10B981"
- name: loom:building
  description: "Claimed by a Builder"
  color: "FBBF24"
# END LOOM LABELS
YML
WRITE_SCOPE_INNER_GH="$STUB_DIR/gh" write_scope_register "$FIX" octocat/hello-world

RC=0
OUT=""
run_sls() {
    : > "$TMP/gh.log"
    echo 0 > "$TMP/state"
    OUT="$(
        cd "$FIX" || exit 99
        PATH="$STUB_DIR:$PATH" \
        LOOM_TEST_GH_LOG="$TMP/gh.log" \
        LOOM_TEST_STATE="$TMP/state" \
        LOOM_CONFIG_DEFAULTS_FILE="" \
        LOOM_FORGE_TYPE="" \
        SYNC_LABELS_RETRY_DELAY=0 \
        bash "$SLS" --repo octocat/hello-world "$@" 2>&1
    )"
    RC=$?
}

echo ""
echo "=== all writes succeed: exit 0, unchanged output ==="
LOOM_TEST_FAIL_LABEL="" run_sls
assert_eq "0" "$RC" "clean sync exits 0"
assert_contains "$OUT" "Synced 3 labels" "success line printed"
assert_not_contains "$OUT" "label writes failed" "no failure summary"

echo ""
echo "=== one create fails hard: exit 5 and the count ==="
LOOM_TEST_FAIL_LABEL="loom:pr" LOOM_TEST_FAIL_MODE=hard run_sls
assert_eq "5" "$RC" "a failed create exits 5"
assert_contains "$OUT" "1 of 3 label writes failed" "N of M summary printed"
assert_not_contains "$OUT" "Synced 3 labels" "no success line on failure"
assert_contains "$OUT" "Failed to create label: loom:pr" "per-label warning kept"
assert_contains "$OUT" "Created label: loom:building" "later labels still applied"
assert_eq "1" "$(grep -c '^label create loom:pr ' "$TMP/gh.log")" "hard error is not retried"

echo ""
echo "=== secondary rate limit once, then success: retried, exit 0 ==="
LOOM_TEST_FAIL_LABEL="loom:pr" LOOM_TEST_FAIL_MODE=ratelimit LOOM_TEST_RL_TIMES=1 run_sls
assert_eq "0" "$RC" "rate limit cleared on retry exits 0"
assert_contains "$OUT" "retrying" "retry announced"
assert_contains "$OUT" "Synced 3 labels" "success line printed"
assert_eq "2" "$(grep -c '^label create loom:pr ' "$TMP/gh.log")" "create attempted twice"

echo ""
echo "=== rate limit that never clears: bounded retries, then exit 5 ==="
LOOM_TEST_FAIL_LABEL="loom:pr" LOOM_TEST_FAIL_MODE=ratelimit LOOM_TEST_RL_TIMES=999 run_sls
assert_eq "5" "$RC" "persistent rate limit exits 5"
assert_eq "5" "$(grep -c '^label create loom:pr ' "$TMP/gh.log")" "1 try + 4 retries"
assert_contains "$OUT" "1 of 3 label writes failed" "counted once"

echo ""
echo "=== --dry-run unchanged ==="
LOOM_TEST_FAIL_LABEL="loom:pr" run_sls --dry-run
assert_eq "0" "$RC" "--dry-run exits 0"
assert_not_contains "$OUT" "label writes failed" "no summary on dry run"

echo ""
echo "Tests run: $TESTS_RUN, passed: $TESTS_PASSED, failed: $TESTS_FAILED"
[[ "$TESTS_FAILED" -eq 0 ]]
