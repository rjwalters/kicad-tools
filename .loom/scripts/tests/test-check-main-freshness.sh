#!/usr/bin/env bash
# test-check-main-freshness.sh - Smoke tests for check-main-freshness.sh (#3770)
#
# Unlike test-check-host-sleep.sh (which probes the live host), this harness
# constructs throwaway local git repos with a synthetic `origin` remote so it can
# deterministically exercise the load-bearing cases:
#   (a) up-to-date  -> exit 0, no stderr warning
#   (b) behind      -> exit 0, prints the "behind" warning to stderr
#   (c) fetch fails -> exit 0 (never blocks) even when origin is unreachable
#   (d) ahead       -> exit 0, prints the "ahead" warning to stderr (#5182)
#   (e) diverged    -> exit 0, prints BOTH warnings (#5182)
# Plus the flag/contract checks mirrored from test-check-host-sleep.sh:
#   - always exits 0
#   - --quiet suppresses the stdout one-liner
#   - --help prints usage
#   - unknown args don't break it
#
# Usage:
#   ./.loom/scripts/tests/test-check-main-freshness.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/check-main-freshness.sh"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

# Scratch area for fixtures — cleaned on exit.
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-freshness.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

# git needs an identity in a clean CI environment.
export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"
# Force the default-branch helper to a known value so detection is deterministic
# regardless of the host's git init.defaultBranch config.
export LOOM_DEFAULT_BRANCH="main"

# --- fixture builder ---------------------------------------------------------
# Creates:
#   $WORKDIR/origin.git  — a bare "remote"
#   $WORKDIR/clone       — a working clone with local `main` tracking origin/main
# The clone's `main` starts at parity with origin/main. Callers then advance
# origin and/or rewind the clone to create the "behind" case.
make_fixture() {
    local origin="$WORKDIR/origin.git"
    local clone="$WORKDIR/clone"
    rm -rf "$origin" "$clone"

    git init --quiet --bare "$origin"
    # Point the bare repo's HEAD at main so `git clone` doesn't warn about a
    # nonexistent default ref (bare init defaults HEAD to refs/heads/master).
    git -C "$origin" symbolic-ref HEAD refs/heads/main >/dev/null 2>&1 || true

    # Seed the remote via a throwaway seed clone.
    local seed="$WORKDIR/seed"
    rm -rf "$seed"
    git init --quiet "$seed"
    git -C "$seed" checkout -q -b main
    echo "v1" > "$seed/file.txt"
    git -C "$seed" add file.txt
    git -C "$seed" commit -q -m "c1"
    git -C "$seed" remote add origin "$origin"
    git -C "$seed" push -q origin main

    # The clone under test.
    git clone -q "$origin" "$clone"
    git -C "$clone" checkout -q main
    # Populate refs/remotes/origin/HEAD so loom_default_branch resolves offline
    # too (belt-and-suspenders; LOOM_DEFAULT_BRANCH already forces it).
    git -C "$clone" remote set-head origin main >/dev/null 2>&1 || true
}

# Advance origin/main by one commit (simulating another PR merging mid-sweep).
advance_origin() {
    local seed="$WORKDIR/seed"
    echo "v2-$RANDOM" >> "$seed/file.txt"
    git -C "$seed" add file.txt
    git -C "$seed" commit -q -m "c2"
    git -C "$seed" push -q origin main
}

# Advance the clone's local main by one commit WITHOUT pushing (simulating the
# #5182 incident: unpushed local work that worktree.sh's BASE_REF=origin/<branch>
# would never see).
advance_clone() {
    local clone="$WORKDIR/clone"
    echo "local-$RANDOM" >> "$clone/file.txt"
    git -C "$clone" add file.txt
    git -C "$clone" commit -q -m "local unpushed commit"
}

# -------- Test 1: script exists and is executable --------
echo "Test 1: script exists and is executable"
if [[ -x "$SCRIPT" ]]; then
    pass "check-main-freshness.sh is executable"
else
    fail "check-main-freshness.sh is missing or not executable: $SCRIPT"
    echo "FAILED: $TESTS_FAILED/$TESTS_RUN"
    exit 1
fi

# -------- Test 2: up-to-date -> exit 0, no stderr warning --------
echo "Test 2: up-to-date case exits 0 with no warning"
make_fixture
stderr_out="$(cd "$WORKDIR/clone" && "$SCRIPT" 2>&1 >/dev/null)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "up-to-date exit code is 0"
else
    fail "up-to-date expected exit 0, got $rc"
fi
if ! printf '%s' "$stderr_out" | grep -qi "behind"; then
    pass "up-to-date prints no 'behind' warning"
else
    fail "up-to-date unexpectedly warned: $stderr_out"
fi
stdout_out="$(cd "$WORKDIR/clone" && "$SCRIPT" 2>/dev/null)"
if printf '%s' "$stdout_out" | grep -qi "up to date"; then
    pass "up-to-date prints an up-to-date one-liner"
else
    fail "up-to-date missing one-liner. Got: $stdout_out"
fi

# -------- Test 3: behind -> exit 0 and warns --------
echo "Test 3: behind case exits 0 and prints the warning"
make_fixture
advance_origin   # origin/main now ahead; clone hasn't fetched yet
stderr_out="$(cd "$WORKDIR/clone" && "$SCRIPT" 2>&1 >/dev/null)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "behind exit code is 0"
else
    fail "behind expected exit 0, got $rc"
fi
if printf '%s' "$stderr_out" | grep -qi "behind"; then
    pass "behind prints the 'behind' warning to stderr"
else
    fail "behind did not warn. Got: $stderr_out"
fi
if printf '%s' "$stderr_out" | grep -q "3770"; then
    pass "behind warning references issue #3770"
else
    fail "behind warning missing #3770 reference"
fi
if printf '%s' "$stderr_out" | grep -q -- "--ff-only"; then
    pass "behind warning suggests git merge --ff-only remediation"
else
    fail "behind warning missing --ff-only remediation"
fi

# -------- Test 4: ahead -> exit 0 and warns (#5182) --------
echo "Test 4: ahead case exits 0 and prints the warning"
make_fixture
advance_clone   # local main now has an unpushed commit; origin/main untouched
stderr_out="$(cd "$WORKDIR/clone" && "$SCRIPT" 2>&1 >/dev/null)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "ahead exit code is 0"
else
    fail "ahead expected exit 0, got $rc"
fi
if printf '%s' "$stderr_out" | grep -qi "ahead"; then
    pass "ahead prints the 'ahead' warning to stderr"
else
    fail "ahead did not warn. Got: $stderr_out"
fi
if printf '%s' "$stderr_out" | grep -q "5182"; then
    pass "ahead warning references issue #5182"
else
    fail "ahead warning missing #5182 reference"
fi
if printf '%s' "$stderr_out" | grep -q "BASE_REF"; then
    pass "ahead warning names worktree.sh's BASE_REF consequence"
else
    fail "ahead warning missing BASE_REF consequence explanation"
fi
if printf '%s' "$stderr_out" | grep -q -- "git push origin"; then
    pass "ahead warning suggests git push origin remediation"
else
    fail "ahead warning missing git push origin remediation"
fi
if ! printf '%s' "$stderr_out" | grep -qi "behind"; then
    pass "ahead-only case does not also print a 'behind' warning"
else
    fail "ahead-only case unexpectedly warned about being behind: $stderr_out"
fi
stdout_out="$(cd "$WORKDIR/clone" && "$SCRIPT" 2>/dev/null)"
if printf '%s' "$stdout_out" | grep -qi "ahead"; then
    pass "ahead prints an ahead one-liner to stdout"
else
    fail "ahead missing stdout one-liner. Got: $stdout_out"
fi

# -------- Test 5: diverged (both ahead and behind) -> exit 0, warns both (#5182) --------
echo "Test 5: diverged case exits 0 and prints both warnings"
make_fixture
advance_origin   # origin/main gets a commit the clone doesn't have
advance_clone    # clone's local main gets a commit origin doesn't have
stderr_out="$(cd "$WORKDIR/clone" && "$SCRIPT" 2>&1 >/dev/null)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "diverged exit code is 0"
else
    fail "diverged expected exit 0, got $rc"
fi
if printf '%s' "$stderr_out" | grep -qi "behind"; then
    pass "diverged prints the 'behind' warning to stderr"
else
    fail "diverged did not warn about behind. Got: $stderr_out"
fi
if printf '%s' "$stderr_out" | grep -qi "ahead"; then
    pass "diverged prints the 'ahead' warning to stderr"
else
    fail "diverged did not warn about ahead. Got: $stderr_out"
fi
stdout_out="$(cd "$WORKDIR/clone" && "$SCRIPT" 2>/dev/null)"
if printf '%s' "$stdout_out" | grep -qi "diverged"; then
    pass "diverged prints a diverged one-liner to stdout"
else
    fail "diverged missing stdout one-liner. Got: $stdout_out"
fi

# -------- Test 6: fetch failure -> still exit 0 (never blocks) --------
echo "Test 6: fetch failure still exits 0 (never blocks)"
make_fixture
advance_origin
# Point origin at an unreachable path so `git fetch` fails; the local
# refs/remotes/origin/main from clone time is still present (stale) as fallback.
git -C "$WORKDIR/clone" remote set-url origin "/nonexistent/path/repo.git"
# Run it in the clone dir and capture exit.
rc=0
( cd "$WORKDIR/clone" && "$SCRIPT" >/dev/null 2>&1 ) || rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "fetch-failure exit code is 0"
else
    fail "fetch-failure expected exit 0, got $rc"
fi

# -------- Test 7: --quiet suppresses stdout, still exits 0 --------
echo "Test 7: --quiet suppresses stdout"
make_fixture
stdout_quiet="$(cd "$WORKDIR/clone" && "$SCRIPT" --quiet 2>/dev/null)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "--quiet exit code is 0"
else
    fail "--quiet exit expected 0, got $rc"
fi
if [[ -z "$stdout_quiet" ]]; then
    pass "--quiet produces no stdout"
else
    fail "--quiet produced stdout: $stdout_quiet"
fi

# -------- Test 8: --help prints usage and exits 0 --------
echo "Test 8: --help prints usage and exits 0"
help_out="$("$SCRIPT" --help 2>&1 || true)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "--help exit code is 0"
else
    fail "--help exit expected 0, got $rc"
fi
if printf '%s' "$help_out" | grep -qi "Usage"; then
    pass "--help mentions Usage"
else
    fail "--help did not mention Usage. Got: $help_out"
fi

# -------- Test 9: unknown args do not break it --------
echo "Test 9: unknown args do not break the script"
make_fixture
rc=0
( cd "$WORKDIR/clone" && "$SCRIPT" --some-nonsense-flag --another 99 >/dev/null 2>&1 ) || rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "unknown args tolerated; exit 0"
else
    fail "unknown args caused non-zero exit ($rc)"
fi

# -------- Test 10: outside a git repo -> exit 0, skip gracefully --------
echo "Test 10: outside a git repo exits 0"
non_git="$WORKDIR/not-a-repo"
mkdir -p "$non_git"
rc=0
( cd "$non_git" && "$SCRIPT" >/dev/null 2>&1 ) || rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "non-git dir exit code is 0"
else
    fail "non-git dir expected exit 0, got $rc"
fi

# -------- Test 11: up-to-date but installed surfaces drifted from local
# defaults/ -> exit 0, WARNS (#5874) --------
# This is the exact blind spot #5874 reports: local main already has a merged
# defaults/ change (so N == 0, A == 0 -- ordinary "behind/ahead" comparison
# sees nothing), but the installed .claude/commands/loom/ copy was never
# resynced from local defaults/.claude/commands/loom/. Version-based currency
# checks report "current" here; this script must not.
echo "Test 11: up-to-date but installed surfaces drifted from local defaults/ warns (#5874)"
make_fixture
clone="$WORKDIR/clone"
mkdir -p "$clone/defaults/.claude/commands/loom" "$clone/.claude/commands/loom"
echo "new prompt text" > "$clone/defaults/.claude/commands/loom/builder.md"
echo "old prompt text" > "$clone/.claude/commands/loom/builder.md"
git -C "$clone" add -A
git -C "$clone" commit -q -m "merge a defaults/ role-prompt change (installed copy not resynced)"
# Also advance origin to the same commit so N == 0 && A == 0 (truly up to date).
git -C "$clone" push -q origin main
stderr_out="$(cd "$clone" && "$SCRIPT" 2>&1 >/dev/null)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "drifted-but-up-to-date exit code is 0"
else
    fail "drifted-but-up-to-date expected exit 0, got $rc"
fi
if printf '%s' "$stderr_out" | grep -q "5874"; then
    pass "drifted-but-up-to-date warning references issue #5874"
else
    fail "drifted-but-up-to-date warning missing #5874 reference. Got: $stderr_out"
fi
if printf '%s' "$stderr_out" | grep -q "installed roles/builder.md differs from defaults/roles/builder.md"; then
    pass "drifted-but-up-to-date warning names the differing role-prompt file"
else
    fail "drifted-but-up-to-date warning missing the differing file. Got: $stderr_out"
fi
if printf '%s' "$stderr_out" | grep -q -- "resync-installed.sh"; then
    pass "drifted-but-up-to-date warning suggests resync-installed.sh remediation"
else
    fail "drifted-but-up-to-date warning missing resync-installed.sh remediation"
fi
stdout_out="$(cd "$clone" && "$SCRIPT" 2>/dev/null)"
if printf '%s' "$stdout_out" | grep -qi "installed surfaces differ"; then
    pass "drifted-but-up-to-date prints a distinct one-liner to stdout"
else
    fail "drifted-but-up-to-date missing stdout one-liner. Got: $stdout_out"
fi

# -------- Test 12: truly up-to-date with no defaults/ or installed dirs still
# gives the plain up-to-date result (no regression from Test 11's setup) --------
echo "Test 12: plain up-to-date fixture (no defaults/ dirs) still gives a clean result"
make_fixture
stderr_out="$(cd "$WORKDIR/clone" && "$SCRIPT" 2>&1 >/dev/null)"
if [[ -z "$stderr_out" ]]; then
    pass "plain up-to-date fixture prints no stderr warning"
else
    fail "plain up-to-date fixture unexpectedly warned: $stderr_out"
fi

# -------- Test 13: behind, with no resync source tree available -> the
# "run resync" remediation ALSO warns about the missing precondition (#6202) --------
# This is the exact scenario reported in #6202: a plain consumer clone that
# never ran install.sh has no defaults/hooks|scripts, no .loom/loom-source-path,
# and no install-metadata.json "loom_source" — so following the standard
# remediation ("run resync-installed.sh") fails on first use. The behind-branch
# warning must now say so up front.
echo "Test 13: behind + no resync source tree warns about the missing precondition (#6202)"
make_fixture
advance_origin
clone="$WORKDIR/clone"
rm -rf "$clone/defaults" "$clone/.loom"   # ensure no source tree is resolvable
stderr_out="$(cd "$clone" && "$SCRIPT" 2>&1 >/dev/null)"
if printf '%s' "$stderr_out" | grep -q "6202"; then
    pass "behind-with-no-source-tree warning references issue #6202"
else
    fail "behind-with-no-source-tree warning missing #6202 reference. Got: $stderr_out"
fi
if printf '%s' "$stderr_out" | grep -q "loom-source-path"; then
    pass "behind-with-no-source-tree warning names the loom-source-path sidecar fix"
else
    fail "behind-with-no-source-tree warning missing the loom-source-path fix. Got: $stderr_out"
fi

# -------- Test 14: behind, but a resync source tree IS resolvable via
# .loom/loom-source-path -> no missing-precondition note (#6202) --------
echo "Test 14: behind + resolvable .loom/loom-source-path prints no missing-precondition note (#6202)"
make_fixture
advance_origin
clone="$WORKDIR/clone"
rm -rf "$clone/defaults"
src_clone="$WORKDIR/loom-source"
mkdir -p "$src_clone/defaults/scripts" "$clone/.loom"
printf '%s' "$src_clone" > "$clone/.loom/loom-source-path"
stderr_out="$(cd "$clone" && "$SCRIPT" 2>&1 >/dev/null)"
if printf '%s' "$stderr_out" | grep -qi "behind"; then
    pass "behind-with-resolvable-source still prints the behind warning"
else
    fail "behind-with-resolvable-source lost the behind warning. Got: $stderr_out"
fi
if ! printf '%s' "$stderr_out" | grep -q "6202"; then
    pass "behind-with-resolvable-source prints no missing-precondition note"
else
    fail "behind-with-resolvable-source unexpectedly warned about a missing precondition. Got: $stderr_out"
fi

# -------- Test 15: behind, .loom/loom-source-path points at a NONEXISTENT
# directory -> treated as unresolvable, same as no sidecar at all (#6780) ----
echo "Test 15: behind + .loom/loom-source-path pointing at a nonexistent directory still warns about the missing precondition (#6780)"
make_fixture
advance_origin
clone="$WORKDIR/clone"
rm -rf "$clone/defaults"
mkdir -p "$clone/.loom"
printf '%s/does-not-exist\n' "$WORKDIR" > "$clone/.loom/loom-source-path"
stderr_out="$(cd "$clone" && "$SCRIPT" 2>&1 >/dev/null)"
if printf '%s' "$stderr_out" | grep -q "6202"; then
    pass "(#6780) nonexistent loom-source-path target still warns about the missing precondition"
else
    fail "(#6780) nonexistent loom-source-path target lost the missing-precondition warning. Got: $stderr_out"
fi

# -------- Test 16: behind, .loom/loom-source-path points at a directory that
# EXISTS (even with a `defaults/` subdir) but is not a real Loom checkout --
# e.g. a scratch clone whose contents were emptied without removing the
# directory itself. This is the precise #6780 gap: a bare `-d "$src/defaults"`
# check treats this as resolvable, which would let resync-installed.sh
# silently report a false "already in sync" against an empty source tree.
# The precondition check must require the SAME populated defaults/hooks or
# defaults/scripts resolve_defaults() itself requires. --------
echo "Test 16: behind + .loom/loom-source-path pointing at a stale/empty source tree still warns about the missing precondition (#6780)"
make_fixture
advance_origin
clone="$WORKDIR/clone"
rm -rf "$clone/defaults"
stale_src="$WORKDIR/stale-loom-source"
rm -rf "$stale_src"
mkdir -p "$stale_src/defaults" "$clone/.loom"   # defaults/ exists, but empty
printf '%s\n' "$stale_src" > "$clone/.loom/loom-source-path"
stderr_out="$(cd "$clone" && "$SCRIPT" 2>&1 >/dev/null)"
if printf '%s' "$stderr_out" | grep -q "6202"; then
    pass "(#6780) stale/empty source tree still warns about the missing precondition"
else
    fail "(#6780) stale/empty source tree lost the missing-precondition warning. Got: $stderr_out"
fi

# -------- Summary --------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}OK${NC}: all tests passed"
exit 0
