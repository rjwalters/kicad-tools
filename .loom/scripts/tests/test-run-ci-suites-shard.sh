#!/usr/bin/env bash
# test-run-ci-suites-shard.sh — LOOM_CI_SHARD=k/N in run-ci-suites.sh (#9065).
#
# CI splits the wired suites across runners with LOOM_CI_SHARD. The split is
# only safe if it is COMPLETE (every suite runs in exactly one shard), if no
# bad value can quietly run nothing and exit 0 (ci-principles.md rule 6), and
# if the variable does not leak into suites the runner launches — its own
# self-tests re-invoke this runner on fixture manifests, and an inherited
# shard dropped fixture suites they expected (the first CI run of #9074).
#
# Part 1 drives the real manifest through --plan, which executes nothing.
# Part 2 runs a throwaway fixture tree, so no real suite is ever executed.
#
# Usage:
#   ./defaults/scripts/tests/test-run-ci-suites-shard.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
RUNNER="$SCRIPT_DIR/run-ci-suites.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} $1"
}
fail() {
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} $1"
    [[ -n "${2:-}" ]] && echo "$2" | sed 's/^/    /'
}
check() {
    local rc="$1" msg="$2" detail="${3:-}"
    if [[ "$rc" -eq 0 ]]; then pass "$msg"; else fail "$msg" "$detail"; fi
}

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# ==============================================================
# Part 1 — planning against the real manifest.
# ==============================================================

# The suites a plan names, RUN or SKIP alike: sharding decides membership,
# the live-daemon guard decides whether a member runs, and the two must not
# be confused here. `none` pins the guard's candidates so the host is moot.
planned() { # [shard]
    LOOM_CI_DAEMON_PIDFILE_CANDIDATES=none LOOM_CI_SHARD="${1:-}" \
        bash "$RUNNER" --plan 2>/dev/null | awk '$1 == "RUN" || $1 == "SKIP" { print $2 }' | sort
}

planned > "$WORKDIR/all"
all_count=$(wc -l < "$WORKDIR/all" | tr -d ' ')
check "$([[ "$all_count" -gt 10 ]] && echo 0 || echo 1)" \
    "the unsharded plan names the whole manifest ($all_count suites)"

for n in 2 3; do
    : > "$WORKDIR/union"
    for k in $(seq 1 "$n"); do
        planned "$k/$n" > "$WORKDIR/shard-$k"
        cat "$WORKDIR/shard-$k" >> "$WORKDIR/union"
    done
    sort "$WORKDIR/union" -o "$WORKDIR/union"
    check "$(diff -q "$WORKDIR/all" <(sort -u "$WORKDIR/union") >/dev/null && echo 0 || echo 1)" \
        "N=$n: the shards together name every suite"
    dups=$(uniq -d "$WORKDIR/union" | wc -l | tr -d ' ')
    check "$([[ "$dups" -eq 0 ]] && echo 0 || echo 1)" \
        "N=$n: no suite is in two shards ($dups duplicated)"
done

# Every malformed or empty-selecting value must fail, never plan nothing.
for bad in "0/2" "3/2" "08/10" "1/0" "abc" "1/2/3" " 1/2" "999/999"; do
    LOOM_CI_DAEMON_PIDFILE_CANDIDATES=none LOOM_CI_SHARD="$bad" \
        bash "$RUNNER" --plan >/dev/null 2>&1
    rc=$?
    check "$([[ "$rc" -eq 2 ]] && echo 0 || echo 1)" \
        "LOOM_CI_SHARD='$bad' is refused with exit 2 (got $rc)"
done

# ==============================================================
# Part 2 — execution, against an isolated fixture repo.
# ==============================================================

FIXTURE="$WORKDIR/fixture"
FIX_TESTS="$FIXTURE/defaults/scripts/tests"
FIX_LIB="$FIXTURE/defaults/scripts/lib"
mkdir -p "$FIX_TESTS" "$FIX_LIB" "$FIX_TESTS/lib"
cp "$RUNNER" "$SCRIPT_DIR/check-ci-suite-manifest.sh" "$FIX_TESTS/"
cp "$REPO_ROOT/defaults/scripts/lib/live-daemon-guard.sh" \
   "$REPO_ROOT/defaults/scripts/lib/cpu-budget.sh" "$FIX_LIB/"
cp "$SCRIPT_DIR/lib/live-state-sandbox.sh" "$FIX_TESTS/lib/"
# The failure-excerpt helper the runner sources, so a failing fixture suite
# would still print its excerpt.
cp "$REPO_ROOT/defaults/scripts/lib/ci-suite-excerpt.sh" "$FIX_LIB/"

# Each fixture suite records that it ran and what LOOM_CI_SHARD it saw.
SEEN="$WORKDIR/seen"
mkdir -p "$SEEN"
export SEEN
FIXTURES=(test-fixture-shard-a.sh test-fixture-shard-b.sh test-fixture-shard-c.sh)
for s in "${FIXTURES[@]}"; do
    cat > "$FIX_TESTS/$s" <<EOF
#!/usr/bin/env bash
# fixture suite (test-run-ci-suites-shard.sh)
printf '%s\n' "\${LOOM_CI_SHARD-<unset>}" > "\$SEEN/$s"
exit 0
EOF
    chmod +x "$FIX_TESTS/$s"
done
printf '%s\n' "${FIXTURES[@]}" > "$FIX_TESTS/ci-wired.txt"
: > "$FIX_TESTS/ci-excluded.txt"

RUN_OUT="$( cd "$FIXTURE" && \
    LOOM_CI_DAEMON_PIDFILE_CANDIDATES=none \
    LOOM_CI_SHARD=1/2 \
    LOOM_CI_PARALLELISM=2 \
    bash "$FIX_TESTS/run-ci-suites.sh" 2>&1 )"
run_rc=$?
check "$run_rc" "fixture shard 1/2 exits 0" "$RUN_OUT"

# Manifest indices 0 and 2 belong to shard 1/2; index 1 does not.
check "$([[ -f "$SEEN/test-fixture-shard-a.sh" && -f "$SEEN/test-fixture-shard-c.sh" ]] && echo 0 || echo 1)" \
    "shard 1/2 ran manifest entries 0 and 2" "$RUN_OUT"
check "$([[ ! -f "$SEEN/test-fixture-shard-b.sh" ]] && echo 0 || echo 1)" \
    "shard 1/2 did not run manifest entry 1"

leaked=""
for s in test-fixture-shard-a.sh test-fixture-shard-c.sh; do
    [[ -f "$SEEN/$s" ]] || continue
    v="$(cat "$SEEN/$s")"
    [[ "$v" == "<unset>" ]] || leaked+="$s saw '$v' "
done
check "$([[ -z "$leaked" ]] && echo 0 || echo 1)" \
    "LOOM_CI_SHARD does not leak into the suites it launches" "$leaked"

echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
