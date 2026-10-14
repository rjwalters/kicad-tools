#!/usr/bin/env bash
# test-create-issue-refuse-labels.sh — create-issue.sh refuses to open an
# issue as `loom:issue` or `loom:curated` (#11820).
#
# Operator ruling (2026-10-13): issues enter at
# `loom:triage`. One opened as `loom:issue`/`loom:curated` skips intake, the
# Curator and approval. The daemon's intake reconcile demotes such issues
# after the fact; this script refuses them up front.
#
# Cases:
#   1. `--label loom:issue`, `--label loom:curated`,
#      `--label "loom:triage,loom:issue"` and `-l loom:curated` each exit
#      non-zero, name `loom:triage` on stderr, and make no forge call.
#   2. `--label loom:triage` and `--label loom:architect` are not refused:
#      the run gets past label parsing (stopped by the missing --title, so the
#      suite never reaches the forge either way).
#
# Hermetic: create-issue.sh + lib/ are copied into a throwaway dir and a stub
# `gh` / `loom-daemon` on PATH records any call.

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}
fail() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/scripts" "$WORK/bin"
cp "$SCRIPTS_DIR/create-issue.sh" "$WORK/scripts/"
cp -R "$SCRIPTS_DIR/lib" "$WORK/scripts/lib"
CALLS="$WORK/forge-calls.log"
for tool in gh loom-daemon tea; do
    cat > "$WORK/bin/$tool" << STUB
#!/usr/bin/env bash
echo "$tool \$*" >> "$CALLS"
exit 1
STUB
    chmod +x "$WORK/bin/$tool"
done

run() {
    : > "$CALLS"
    OUT="$(cd "$WORK" && PATH="$WORK/bin:$PATH" bash "$WORK/scripts/create-issue.sh" "$@" 2>&1)"
    RC=$?
}

echo "create-issue.sh refuses loom:issue / loom:curated (#11820)"

refused() {
    local desc="$1"
    shift
    run --title "t" --body "b" "$@"
    if [[ "$RC" -ne 0 ]]; then pass "$desc: exits non-zero"; else fail "$desc: exited 0"; fi
    if [[ "$OUT" == *"refusing"* && "$OUT" == *"loom:triage"* ]]; then
        pass "$desc: names loom:triage"
    else
        fail "$desc: message missing (got: $OUT)"
    fi
    if [[ ! -s "$CALLS" ]]; then pass "$desc: no forge call"; else fail "$desc: forge called: $(cat "$CALLS")"; fi
}

refused "--label loom:issue" --label loom:issue
refused "--label loom:curated" --label loom:curated
refused "--label loom:triage,loom:issue" --label "loom:triage,loom:issue"
refused "-l loom:curated" -l loom:curated
refused "--label 'bug, loom:issue'" --label "bug, loom:issue"

allowed() {
    local desc="$1"
    shift
    run "$@"
    if [[ "$OUT" == *"--title is required"* && "$OUT" != *"refusing"* ]]; then
        pass "$desc: not refused"
    else
        fail "$desc: refused or wrong stop (rc=$RC, got: $OUT)"
    fi
    if [[ ! -s "$CALLS" ]]; then pass "$desc: no forge call"; else fail "$desc: forge called"; fi
}

allowed "--label loom:triage" --label loom:triage
allowed "--label loom:architect" --label loom:architect
allowed "--label bug,enhancement" --label "bug,enhancement"

echo
if [[ "$TESTS_FAILED" -eq 0 ]]; then
    echo -e "${GREEN}All $TESTS_RUN checks passed${NC}"
    exit 0
fi
echo -e "${RED}$TESTS_FAILED of $TESTS_RUN checks failed${NC}"
exit 1
