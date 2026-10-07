#!/usr/bin/env bash
# test-check-labels-drift.sh — regression suite for check-labels-drift.sh's
# registry branch (#10013, PR #10053).
#
# check-labels-drift.sh has two halves: a byte `diff` of the two labels.yml
# copies (#3896, covered by scripts/test-installer.sh), and — since #10013 — a
# `loom-daemon labels check` comparison of both copies against
# defaults/labels.json. The second half was dead code in CI when first landed:
# it only ran when `loom-daemon` was on PATH, and the installer-tests job never
# put its downloaded build there. Nothing failed, because the byte diff alone
# still reported OK.
#
# This suite pins the one case only the registry half can catch: both
# labels.yml copies BYTE-IDENTICAL to each other but BOTH diverging from
# defaults/labels.json (a label hand-added to both copies, skipping the
# registry). If the registry branch stops executing, case (b) goes green-OK and
# this suite fails.
#
# Covers:
#   a. The real (in-sync) tree passes, and the registry check actually ran.
#   b. Fixture with identical copies that both carry an extra label absent from
#      defaults/labels.json -> non-zero exit naming defaults/labels.json.
#   c. The same fixture restored to the real copies -> passes.
#
# Needs a built loom-daemon with the `labels` subcommand; it calls
# loom_test_require_daemon_bin, so it FAILS, never SKIPs, without one.
#
# Usage:
#   ./.loom/scripts/tests/test-check-labels-drift.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO="$(cd "$SCRIPTS_DIR/../.." && pwd)"
DRIFT="$SCRIPTS_DIR/check-labels-drift.sh"

# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "labels"

TESTS_RUN=0
TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo "  PASS: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo "  FAIL: $1"; }

FIX="$(mktemp -d)"
trap 'rm -rf "$FIX"' EXIT

echo "Test (a): the real in-sync tree passes and runs the registry check"
OUT="$(bash "$DRIFT" "$REPO" 2>&1)" && RC=0 || RC=$?
if [[ "$RC" -eq 0 ]]; then
  pass "real tree exits 0"
else
  fail "real tree exited $RC: $OUT"
fi
if grep -q "registry check via" <<<"$OUT"; then
  pass "registry branch executed on the real tree"
else
  fail "registry branch did not execute (output: $OUT)"
fi

echo "Test (b): identical copies that both diverge from defaults/labels.json fail"
mkdir -p "$FIX/.github" "$FIX/defaults/.github"
cp "$REPO/defaults/labels.json" "$FIX/defaults/labels.json"
# Insert a label inside the Loom-managed block, just above the END marker.
awk '/^# END LOOM LABELS/ {
       print "- name: \"loom:drift-fixture\""
       print "  description: \"Hand-added to labels.yml only (fixture).\""
       print "  color: \"000000\""
       print ""
     }
     { print }' "$REPO/.github/labels.yml" > "$FIX/.github/labels.yml"
cp "$FIX/.github/labels.yml" "$FIX/defaults/.github/labels.yml"

if cmp -s "$FIX/.github/labels.yml" "$FIX/defaults/.github/labels.yml" \
  && ! cmp -s "$FIX/.github/labels.yml" "$REPO/.github/labels.yml"; then
  pass "fixture copies are byte-identical and differ from the real tree"
else
  fail "fixture setup is wrong (copies must match each other, not the real tree)"
fi

OUT="$(bash "$DRIFT" "$FIX" 2>&1)" && RC=0 || RC=$?
if [[ "$RC" -ne 0 ]] && grep -q "differs from defaults/labels.json" <<<"$OUT"; then
  pass "registry drift fails (exit $RC)"
else
  fail "registry drift not caught (rc=$RC, output: $OUT)"
fi

echo "Test (c): the same fixture with the real copies passes"
cp "$REPO/.github/labels.yml" "$FIX/.github/labels.yml"
cp "$REPO/defaults/.github/labels.yml" "$FIX/defaults/.github/labels.yml"
OUT="$(bash "$DRIFT" "$FIX" 2>&1)" && RC=0 || RC=$?
if [[ "$RC" -eq 0 ]]; then
  pass "in-sync fixture exits 0"
else
  fail "in-sync fixture exited $RC: $OUT"
fi

echo ""
echo "Results: $((TESTS_RUN - TESTS_FAILED))/$TESTS_RUN passed"
[[ "$TESTS_FAILED" -eq 0 ]]
