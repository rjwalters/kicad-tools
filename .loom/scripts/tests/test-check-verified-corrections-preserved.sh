#!/usr/bin/env bash
# test-check-verified-corrections-preserved.sh - Unit tests for
# check-verified-corrections-preserved.sh, the diff-before-rewrite guard for
# the Curator's append-only "## Verified corrections" convention (#4135).
#
# This is the "scenario" acceptance criterion #4135 AC4 asks for: it exercises
# the incident that motivated the issue (#4042 -- a second Curator pass
# rewrote a body and silently dropped three live-verified findings) as a
# concrete, checkable regression test, plus the surrounding edge cases
# (append-only success, whole-section removal, no section to begin with, a
# dated counter-finding that supersedes without deleting).
#
# Usage:
#   ./.loom/scripts/tests/test-check-verified-corrections-preserved.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
CVCP="$SCRIPTS_DIR/check-verified-corrections-preserved.sh"

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

# assert_contains/assert_not_contains use a pure-bash substring match (no
# forked printf|grep pipeline) so a transient fork/exec failure under
# run-ci-suites.sh's parallel suite pool can never masquerade as a genuine
# content mismatch (#7819, #7874).
assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" == *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

if [[ ! -x "$CVCP" ]]; then
    echo -e "${RED}FAIL${NC}: check-verified-corrections-preserved.sh missing or not executable at $CVCP"
    exit 1
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

run_cvcp() {
    OUT="$("$CVCP" "$@" 2>&1)"
    RC=$?
}

echo "=== check-verified-corrections-preserved.sh ==="
echo ""

# --- Scenario 1: the #4042 incident, reconstructed ------------------------
# Pass 1 records three live-verified findings. Pass 2 rewrites the body and
# (as actually happened) drops all three -- this must fail with all three
# named, not just report "something changed".
cat > "$WORK_DIR/4042-pass1.md" <<'EOF'
## Problem

`loom-daemon-update.sh` cannot manage a launchd-installed daemon.

## Verified corrections

- **2026-07-XX, verified via `launchctl print`**: `KeepAlive = false` in the
  live plist, contradicting the assumption that the daemon self-restarts.

- **2026-07-XX, verified via `--print-plist`**: no `LOOM_DAEMON_SUPERVISOR`
  variable is present in the rendered plist.

- **2026-07-XX, verified via `--print-plist` diff**: six autonomy variables
  (`LOOM_WORK_FINDER`, `LOOM_MAIN_HEALTH_GATE`, and four others) are present
  in the live plist but the updater never reads or re-renders them.

## Acceptance Criteria

- [ ] Updater preserves the six autonomy vars across an update
EOF

cat > "$WORK_DIR/4042-pass2-bad.md" <<'EOF'
## Problem

`loom-daemon-update.sh` cannot manage a launchd-installed daemon.

The updater needs no flag replay and no bootout/bootstrap; plist parsing
should not be reimplemented.

## Acceptance Criteria

- [ ] Confirm the updater's current approach is sufficient
EOF

run_cvcp "$WORK_DIR/4042-pass1.md" "$WORK_DIR/4042-pass2-bad.md"
assert_eq "1" "$RC" "(1a) Reconstructed #4042 pass-2 rewrite (whole section dropped) -> exit 1"
assert_contains "$OUT" "has none at all" "(1a) Failure names the section as entirely absent, not just changed"

# --- Scenario 2: a compliant re-curation pass appends, drops nothing ------
cat > "$WORK_DIR/4042-pass2-good.md" <<'EOF'
## Problem

`loom-daemon-update.sh` cannot manage a launchd-installed daemon.

## Verified corrections

- **2026-07-XX, verified via `launchctl print`**: `KeepAlive = false` in the
  live plist, contradicting the assumption that the daemon self-restarts.

- **2026-07-XX, verified via `--print-plist`**: no `LOOM_DAEMON_SUPERVISOR`
  variable is present in the rendered plist.

- **2026-07-XX, verified via `--print-plist` diff**: six autonomy variables
  (`LOOM_WORK_FINDER`, `LOOM_MAIN_HEALTH_GATE`, and four others) are present
  in the live plist but the updater never reads or re-renders them.

- **2026-08-01, re-verified after #4090 merged**: the updater now re-renders
  the plist from the live host state instead of the invoking shell's
  environment, closing the six-var gap identified above.

## Acceptance Criteria

- [ ] Updater preserves the six autonomy vars across an update (closed by #4090)
EOF

run_cvcp "$WORK_DIR/4042-pass1.md" "$WORK_DIR/4042-pass2-good.md"
assert_eq "0" "$RC" "(2a) Compliant re-curation pass (all 3 preserved + 1 appended) -> exit 0"
assert_contains "$OUT" "OK" "(2a) Success message reported"

# --- Scenario 3: dated counter-finding supersedes without deleting --------
# A later pass disagrees with one entry. It must ADD a dated counter-finding,
# not edit/replace the original -- the original stays verbatim.
cat > "$WORK_DIR/counter-finding.md" <<'EOF'
## Problem

Something is broken.

## Verified corrections

- **2026-07-27, verified against `origin/main` @ `a1b2c3d`**: the retry
  budget is hardcoded to 3.

- **2026-08-02, supersedes the 2026-07-27 entry above (re-verified after
  #4200 merged)**: the retry budget is now a configurable
  `RETRY_BUDGET` env var, default 3. The 07-27 finding was correct for the
  code at the time.

## Acceptance Criteria

- [ ] N/A
EOF

cat > "$WORK_DIR/counter-finding-old.md" <<'EOF'
## Problem

Something is broken.

## Verified corrections

- **2026-07-27, verified against `origin/main` @ `a1b2c3d`**: the retry
  budget is hardcoded to 3.

## Acceptance Criteria

- [ ] N/A
EOF

run_cvcp "$WORK_DIR/counter-finding-old.md" "$WORK_DIR/counter-finding.md"
assert_eq "0" "$RC" "(3a) Dated counter-finding appended alongside the original -> exit 0 (original preserved)"

# --- Scenario 4: no '## Verified corrections' section in the old body -----
# The common case -- most issues never accumulate this section. Must be a
# silent no-op, not a failure.
cat > "$WORK_DIR/no-section-old.md" <<'EOF'
## Problem

Nothing verified yet, just a first pass.

## Acceptance Criteria

- [ ] TBD
EOF

cat > "$WORK_DIR/no-section-new.md" <<'EOF'
## Problem

Nothing verified yet, just a first pass -- refreshed wording.

## Acceptance Criteria

- [ ] TBD, still
EOF

run_cvcp "$WORK_DIR/no-section-old.md" "$WORK_DIR/no-section-new.md"
assert_eq "0" "$RC" "(4a) Old body has no Verified corrections section -> exit 0 (no-op)"
assert_contains "$OUT" "nothing to preserve" "(4a) No-op reported explicitly"

# --- Scenario 5: heading is case-insensitive -------------------------------
cat > "$WORK_DIR/case-old.md" <<'EOF'
## Verified Corrections

- **2026-07-27**: a finding.
EOF

cat > "$WORK_DIR/case-new-good.md" <<'EOF'
## verified corrections

- **2026-07-27**: a finding.

- **2026-08-01**: an appended finding.
EOF

run_cvcp "$WORK_DIR/case-old.md" "$WORK_DIR/case-new-good.md"
assert_eq "0" "$RC" "(5a) Heading match is case-insensitive across old/new -> exit 0"

# --- Scenario 6: usage errors -----------------------------------------------
run_cvcp "$WORK_DIR/case-old.md"
assert_eq "2" "$RC" "(6a) Missing second argument -> exit 2 (usage error)"

run_cvcp "$WORK_DIR/does-not-exist.md" "$WORK_DIR/case-new-good.md"
assert_eq "2" "$RC" "(6b) Nonexistent old-body file -> exit 2 (usage error)"

# --- Scenario 7: large section, byte-identical OLD/NEW -> exit 0 (#6768) --
# Regression test for the SIGPIPE/pipefail false-positive: under
# `set -euo pipefail`, a `grep -qFx | tr ...` pipeline (the pre-fix
# implementation) can report a false FAIL once the "## Verified corrections"
# section grows large enough, because `grep -q` exits as soon as it finds its
# match and SIGPIPEs the upstream `tr`, which then makes the *pipeline's*
# exit status nonzero even though the match was found. A large, byte-identical
# section is the minimal repro shape for that.
gen_large_section() {
    local n="$1"
    echo "## Verified corrections"
    echo ""
    local i
    for ((i = 1; i <= n; i++)); do
        echo "- **2026-08-1${i}, verified via check #${i}**: finding number ${i} confirmed against the live host, paragraph padding text to make this entry realistically sized rather than a one-liner."
        echo ""
    done
}

gen_large_section 300 > "$WORK_DIR/large-section.md"

run_cvcp "$WORK_DIR/large-section.md" "$WORK_DIR/large-section.md"
assert_eq "0" "$RC" "(7a) Large (300-paragraph) section, byte-identical OLD/NEW -> exit 0 (no false-positive FAIL)"
assert_contains "$OUT" "OK" "(7a) Success message reported for the large identical section"

# --- Scenario 8: early-paragraph match in a large section (#6768) ---------
# The specific case that triggers the SIGPIPE/pipefail bug: the very first
# (or second) paragraph in the OLD section's search loop matches a paragraph
# that also appears early in the NEW paragraph stream, so `grep -q` returns
# almost immediately -- maximizing the chance the upstream `tr` is still
# writing when it gets SIGPIPE'd. A size-only test that never checks an early
# match specifically would not catch a regression back to the buggy pipe.
{
    echo "## Verified corrections"
    echo ""
    echo "- **2026-08-01, verified via check #1**: the very first finding, which must be located correctly even though hundreds of other paragraphs follow it in the stream."
    echo ""
    gen_large_section 300 | tail -n +3
} > "$WORK_DIR/early-match-old.md"

cp "$WORK_DIR/early-match-old.md" "$WORK_DIR/early-match-new.md"

run_cvcp "$WORK_DIR/early-match-old.md" "$WORK_DIR/early-match-new.md"
assert_eq "0" "$RC" "(8a) Early-paragraph (1st entry) match found correctly in a large section -> exit 0"
assert_contains "$OUT" "OK" "(8a) Success message reported for the early-match case"

# --- Scenario 9: genuine missing paragraph in a large section still FAILs -
# The fix must not loosen detection -- a real gap in a large section must
# still be reported, named, and exit 1.
gen_large_section 300 > "$WORK_DIR/large-section-missing-old.md"
gen_large_section 300 | grep -v "finding number 2 confirmed" > "$WORK_DIR/large-section-missing-new.md"

run_cvcp "$WORK_DIR/large-section-missing-old.md" "$WORK_DIR/large-section-missing-new.md"
assert_eq "1" "$RC" "(9a) Genuine missing paragraph in a large section -> exit 1 (still correctly detected)"
assert_contains "$OUT" "finding number 2 confirmed" "(9a) Failure names the specific missing paragraph"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
