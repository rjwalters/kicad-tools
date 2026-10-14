#!/usr/bin/env bash
# test-ci-suite-manifest-installed-layout.sh - the ci-wired/ci-excluded
# partition must classify correctly in an INSTALLED copy, not just in the
# Loom source tree.
#
# Both manifests, check-ci-suite-manifest.sh and every suite ship into an
# installing repo as .loom/scripts/tests/. Two things used to break there,
# which pushed installing repos into pinning both manifests locally (and so
# losing every later upstream reclassification):
#
#   1. check-ci-suite-manifest.sh failed on every `/`-qualified entry
#      (tests/hooks/..., scripts/test-*.sh, ...) -- source-tree-only suites
#      that are never installed -- so the shipped checker could not confirm
#      the partition at all.
#   2. test-create-pr-version-check.sh hard-aborted ("scripts/version.sh not
#      found") because its T5-T7 copy Loom's own root scripts/version.sh,
#      which is not shipped. Wired in ci-wired.txt, it therefore failed in
#      every installing repo that runs the wired suites.
#
# Strategy: copy the shipped scripts tree into a scratch `.loom/scripts/` of
# a fresh git repo (the installed layout) and run the subjects from there.
#
# Usage:
#   ./.loom/scripts/tests/test-ci-suite-manifest-installed-layout.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_SRC="$(cd "$SCRIPT_DIR/.." && pwd)"

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

check() { # <expected-rc> <actual-rc> <msg>
  TESTS_RUN=$((TESTS_RUN + 1))
  if [[ "$1" == "$2" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo "  PASS: $3"
  else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo "  FAIL: $3 (expected rc=$1, got rc=$2)"
  fi
}

contains() { # <haystack> <needle> <msg>
  TESTS_RUN=$((TESTS_RUN + 1))
  if [[ "$1" == *"$2"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo "  PASS: $3"
  else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo "  FAIL: $3 (missing '$2')"
    echo "    output: $1"
  fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
INSTALL="$WORK/consumer"
mkdir -p "$INSTALL/.loom"
cp -R "$SCRIPTS_SRC" "$INSTALL/.loom/scripts"
(cd "$INSTALL" && git init -q)
CHECK="$INSTALL/.loom/scripts/tests/check-ci-suite-manifest.sh"

echo "check-ci-suite-manifest.sh in the installed layout"

# T1: verbatim shipped tree -> partition holds, qualified entries skipped.
rc=0; out="$(bash "$CHECK" 2>&1)" || rc=$?
check 0 "$rc" "verbatim installed copy passes the partition check"
contains "$out" "installed layout" "reports that path-qualified entries were skipped"

# T2: the installing repo's OWN suites beside the install (top-level
# scripts/test-*.sh, tests/hooks/) are not Loom's to classify.
mkdir -p "$INSTALL/scripts" "$INSTALL/tests/hooks"
printf '#!/bin/sh\n' > "$INSTALL/scripts/test-consumer-owned.sh"
printf '#!/bin/sh\n' > "$INSTALL/tests/hooks/test-consumer-hook.sh"
rc=0; out="$(bash "$CHECK" 2>&1)" || rc=$?
check 0 "$rc" "installing repo's own external-dir suites are not flagged"

# T3: the forcing function still works for this directory: an unlisted suite
# dropped into .loom/scripts/tests/ fails.
printf '#!/bin/sh\n' > "$INSTALL/.loom/scripts/tests/test-unlisted-local.sh"
rc=0; out="$(bash "$CHECK" 2>&1)" || rc=$?
check 1 "$rc" "an unlisted suite in .loom/scripts/tests/ still fails"
contains "$out" "test-unlisted-local.sh" "names the unlisted suite"
rm -f "$INSTALL/.loom/scripts/tests/test-unlisted-local.sh"

# T4: a bare (unqualified) entry that is missing is still an error -- only
# path-qualified entries are exempted in the installed layout.
victim="$INSTALL/.loom/scripts/tests/test-create-pr-version-check.sh"
mv "$victim" "$victim.bak"
rc=0; out="$(bash "$CHECK" 2>&1)" || rc=$?
check 1 "$rc" "a missing bare-named entry is still reported"
contains "$out" "nonexistent suite: test-create-pr-version-check.sh" "names the missing bare entry"
mv "$victim.bak" "$victim"

echo "test-create-pr-version-check.sh in the installed layout"

# T5: wired upstream, so it must pass (not abort) where Loom's root
# scripts/version.sh is absent...
rc=0; out="$(bash "$victim" 2>&1)" || rc=$?
check 0 "$rc" "runs green with no scripts/version.sh at the repo root"
contains "$out" "SKIP: T5-T7" "skips only the source-tree-only cases"

# T6: ...and must not pick up an installing repo's unrelated
# scripts/version.sh as if it were Loom's.
printf '#!/bin/sh\necho unrelated; exit 1\n' > "$INSTALL/scripts/version.sh"
chmod +x "$INSTALL/scripts/version.sh"
rc=0; out="$(bash "$victim" 2>&1)" || rc=$?
check 0 "$rc" "ignores an installing repo's own scripts/version.sh"
contains "$out" "SKIP: T5-T7" "still skips T5-T7 beside a foreign version.sh"

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]]
