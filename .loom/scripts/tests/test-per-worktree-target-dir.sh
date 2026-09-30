#!/usr/bin/env bash
# test-per-worktree-target-dir.sh — per-worktree CARGO_TARGET_DIR, wired to the
# worktree lifecycle (issue #8458, item 2 of #8453).
#
# Covers the CREATION half added to lib/cargo-target-dir.sh
# (`loom-daemon cargo-target-dir provision`) and the two attribution relaxations it
# licenses on the REMOVAL half, end to end through `worktree.sh`:
#
#   1. Opted in, on a host whose cargo output is redirected to ONE shared root:
#      the worktree gets `<root>/wt/issue-<N>`, a `.loom-cargo-target-dir` marker
#      records it, and `worktree.sh remove` reclaims it. (AC: "a removed worktree
#      leaves no build output behind".)
#   2. Two worktrees get DISTINCT dirs, so neither can execute the other's
#      uplifted `debug/loom-daemon`; removing one leaves the other's intact.
#      (AC: "two worktrees … cannot execute each other's binary".)
#   3. The reclaim still happens when the PRIMARY checkout resolves to the shared
#      root that CONTAINS the per-worktree dir — the containment relaxation. This
#      is the case that vetoed every reclaim before that relaxation existed.
#   4. The reclaim still happens when the remover's own ambient
#      CARGO_TARGET_DIR IS the per-worktree dir (what the spawn path exports) —
#      the gate-2f relaxation.
#   5. REGRESSION (#7239, the data-loss guard): with the feature ENABLED, a
#      machine-global shared root is still refused. Neither relaxation may ever
#      reach a path that lacks the per-worktree shape.
#   6. Default OFF: nothing is provisioned and nothing changes.
#   7. An unredirected host is a no-op: `<worktree>/target` is already
#      per-worktree, so no marker is written and no build cache is relocated
#      (relocating it is #6013/#6014's rebuild storm).
#   8. Idempotence: an already-per-worktree root is not nested a second level,
#      and a second `worktree.sh <N>` reuses the existing marker.
#   9. A corrupt/hostile marker degrades to "no redirect" rather than to a path
#      the reclaim would act on.
#  10. The opt-in resolves from `.loom/config.json`, not only from the env var.
#  11. merge-pr.sh's post-merge cleanup — the SECOND of the three removal paths
#      the issue names — both removes a worktree carrying the new marker (it is
#      filtered from the dirty-worktree guard) and reclaims the dir. Runs the
#      real `_remove_loom_worktree` body extracted from the live source.
#  12. The `loom-daemon cargo-target-dir` query verbs the bash library delegates
#      to — `is-attributable` and `marker` (exit code IS the answer) and
#      `path --issue <N>` (worktree root resolved in Rust, not pre-derived in
#      shell). Their contract is what the two predicates above are now made of.
#
# Harness: the throwaway-repo pattern from test-cargo-target-dir-reclaim.sh —
# bare origin + working repo, worktree.sh and its lib/ copied in, a stub `cargo`
# standing in for `cargo metadata`. Hermetic: no forge, no network, no Rust
# toolchain.
#
# ## Why the shared root here comes from $CARGO_HOME/config.toml
#
# That is the shape #8453 measured: `build.target-dir` in the HOST's
# `~/.cargo/config.toml`, so every checkout on the machine resolves to one
# directory. It is also the shape #7239 deliberately refuses to attribute to any
# single worktree — which is exactly why this feature needs its own marker, and
# why Test 5 re-asserts that the refusal survives.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"
LIB_SH="$SCRIPTS_DIR/lib/cargo-target-dir.sh"

# The CREATION half is `loom-daemon cargo-target-dir` (see lib/cargo-target-dir.sh
# § "Why only the predicates live here"), so `worktree.sh` reaches for a daemon
# binary here. Pin the one built from THIS working tree — otherwise a stale
# machine-level install answers instead and the suite tests the wrong code.
# FATAL rather than SKIP, per the helper: a silently-skipped suite is how the
# provisioning could regress unnoticed.
# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "cargo-target-dir"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }
skip() { echo -e "  ${YELLOW}SKIP${NC}: $1"; }

for required in "$WORKTREE_SH" "$LIB_SH"; do
    if [[ ! -f "$required" ]]; then
        echo -e "${RED}FATAL${NC}: missing $required"
        exit 1
    fi
done

TMP=$(mktemp -d /tmp/loom-per-wt-target.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

# --- A hermetic stand-in for `cargo metadata` -------------------------------
# Implements exactly the slice of the contract the resolver uses: report
# `target_directory` from CARGO_TARGET_DIR, else the nearest `.cargo/config.toml`
# walking up from cwd, else $CARGO_HOME/config.toml, else `<cwd>/target`.
mkdir -p "$TMP/bin" "$TMP/cargo-home"
cat > "$TMP/bin/cargo" <<'CARGO_STUB'
#!/usr/bin/env bash
[[ "${1:-}" == "metadata" ]] || exit 1
root="$PWD"
target="${CARGO_TARGET_DIR:-}"
if [[ -z "$target" ]]; then
    dir="$root"
    while [[ -n "$dir" && "$dir" != "/" ]]; do
        for f in "$dir/.cargo/config.toml" "$dir/.cargo/config"; do
            [[ -f "$f" ]] || continue
            v="$(sed -n 's/^[[:space:]]*target-dir[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$f" | head -1)"
            [[ -n "$v" ]] && { target="$v"; break; }
        done
        [[ -n "$target" ]] && break
        dir="$(dirname "$dir")"
    done
fi
if [[ -z "$target" && -n "${CARGO_HOME:-}" ]]; then
    for f in "$CARGO_HOME/config.toml" "$CARGO_HOME/config"; do
        [[ -f "$f" ]] || continue
        v="$(sed -n 's/^[[:space:]]*target-dir[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$f" | head -1)"
        [[ -n "$v" ]] && { target="$v"; break; }
    done
fi
[[ -n "$target" ]] || target="$root/target"
case "$target" in /*) ;; *) target="$root/$target" ;; esac
printf '{"packages":[],"target_directory":"%s","version":1}\n' "$target"
CARGO_STUB
chmod +x "$TMP/bin/cargo"
export PATH="$TMP/bin:$PATH"
export CARGO_HOME="$TMP/cargo-home"

# A throwaway repo whose PRIMARY checkout is a cargo workspace, so it counts as a
# live referent in the sharing scan — the shape Test 3 needs.
make_repo() {
    local dir="$1" origin="$2"
    git init -q -b main "$origin" --bare
    git init -q -b main "$dir"
    git -C "$dir" config user.email t@t
    git -C "$dir" config user.name t
    printf '[package]\nname = "fixture"\nversion = "0.0.0"\n' > "$dir/Cargo.toml"
    git -C "$dir" add -A >/dev/null 2>&1
    git -C "$dir" commit -q -m init
    git -C "$dir" remote add origin "$origin"
    git -C "$dir" push -q origin main
    mkdir -p "$dir/.loom/scripts/lib"
    cp "$WORKTREE_SH" "$dir/.loom/scripts/worktree.sh"
    cp -R "$SCRIPTS_DIR"/lib/* "$dir/.loom/scripts/lib/" 2>/dev/null || true
    chmod +x "$dir/.loom/scripts/worktree.sh"
}

# Point the whole HOST at one shared target root, the #8453 shape.
share_target_root() {
    local root="$1"
    printf '[build]\ntarget-dir = "%s"\n' "$root" > "$CARGO_HOME/config.toml"
    mkdir -p "$root"
}
unshare_target_root() { rm -f "$CARGO_HOME/config.toml"; }

# Simulate a build: leave a plausible uplifted binary + artifact behind.
seed_build_output() {
    local dir="$1" marker="$2"
    mkdir -p "$dir/debug"
    printf '#!/bin/sh\necho %s\n' "$marker" > "$dir/debug/loom-daemon"
    chmod +x "$dir/debug/loom-daemon"
    head -c 4096 /dev/zero > "$dir/debug/artifact.bin" 2>/dev/null || echo x > "$dir/debug/artifact.bin"
}

wt_create() { ( cd "$1" && ./.loom/scripts/worktree.sh "$2" ) >"$3" 2>&1; }
wt_remove() { ( cd "$1" && ./.loom/scripts/worktree.sh remove "$2" ) >"$3" 2>&1; }

MARKER=".loom-cargo-target-dir"

# ===========================================================================
# Test 1 + 2 + 3: the full lifecycle, two concurrent worktrees, a primary
# checkout that resolves to the containing shared root.
# ===========================================================================
echo "Test 1: opted in + shared root -> per-worktree dir provisioned, marker written"
R1="$TMP/repo1"
SHARED1="$TMP/shared-root-1"
make_repo "$R1" "$TMP/origin1.git"
share_target_root "$SHARED1"
export LOOM_PER_WORKTREE_TARGET_DIR=1

wt_create "$R1" 301 /tmp/pw-c301.$$
wt_create "$R1" 302 /tmp/pw-c302.$$

WT301="$R1/.loom/worktrees/issue-301"
WT302="$R1/.loom/worktrees/issue-302"
EXPECT301="$SHARED1/wt/issue-301"
EXPECT302="$SHARED1/wt/issue-302"

if [[ -f "$WT301/$MARKER" ]]; then
    pass "marker written into the worktree"
else
    fail "no $MARKER in $WT301 (see /tmp/pw-c301.$$)"
fi
if [[ "$(head -n 1 "$WT301/$MARKER" 2>/dev/null)" == "$EXPECT301" ]]; then
    pass "marker names <root>/wt/issue-301"
else
    fail "marker names $(head -n 1 "$WT301/$MARKER" 2>/dev/null), expected $EXPECT301"
fi
if [[ -d "$EXPECT301" ]]; then
    pass "the per-worktree target dir was created"
else
    fail "the per-worktree target dir $EXPECT301 does not exist"
fi
if grep -q "per-worktree cargo target dir" /tmp/pw-c301.$$; then
    pass "worktree.sh reports the provisioning"
else
    fail "worktree.sh did not report the provisioning (see /tmp/pw-c301.$$)"
fi

echo ""
echo "Test 2: two worktrees get DISTINCT target dirs (no uplifted-binary collision)"
if [[ "$EXPECT301" != "$EXPECT302" && "$(head -n 1 "$WT302/$MARKER")" == "$EXPECT302" ]]; then
    pass "issue-301 and issue-302 resolve to different directories"
else
    fail "the two worktrees did not get distinct target dirs"
fi
seed_build_output "$EXPECT301" "binary-301"
seed_build_output "$EXPECT302" "binary-302"
if [[ "$("$EXPECT301/debug/loom-daemon")" == "binary-301" \
    && "$("$EXPECT302/debug/loom-daemon")" == "binary-302" ]]; then
    pass "each worktree's uplifted debug/loom-daemon is its own (no overwrite)"
else
    fail "the uplifted binaries collided"
fi

echo ""
echo "Test 3: removal reclaims the per-worktree dir even though the PRIMARY"
echo "        checkout resolves to the shared root that CONTAINS it"
if [[ "$(cd "$R1" && ./.loom/scripts/worktree.sh remove 301 --force >/tmp/pw-r301.$$ 2>&1; echo $?)" == "0" ]]; then
    if [[ ! -d "$EXPECT301" ]]; then
        pass "issue-301's per-worktree target dir was reclaimed"
    else
        fail "issue-301's target dir survived (see /tmp/pw-r301.$$)"
    fi
    if grep -q "Reclaimed redirected cargo target dir" /tmp/pw-r301.$$; then
        pass "the removal reports the reclaim"
    else
        fail "the removal did not report a reclaim (see /tmp/pw-r301.$$)"
    fi
    if [[ -x "$EXPECT302/debug/loom-daemon" ]]; then
        pass "the still-live sibling's target dir is untouched"
    else
        fail "removing issue-301 destroyed issue-302's build output"
    fi
    if [[ -d "$SHARED1" ]]; then
        pass "the shared root itself is untouched"
    else
        fail "the shared root was deleted — data-loss regression"
    fi
else
    fail "remove 301 exited non-zero (see /tmp/pw-r301.$$)"
fi

# ===========================================================================
# Test 4: gate-2f relaxation — the remover's own ambient CARGO_TARGET_DIR IS
# the per-worktree dir, which is exactly what the spawn path exports.
# ===========================================================================
echo ""
echo "Test 4: reclaimed even when the remover's ambient CARGO_TARGET_DIR is that dir"
if [[ -d "$EXPECT302" ]]; then
    if ( cd "$R1" && CARGO_TARGET_DIR="$EXPECT302" ./.loom/scripts/worktree.sh remove 302 --force ) \
        >/tmp/pw-r302.$$ 2>&1; then
        if [[ ! -d "$EXPECT302" ]]; then
            pass "the per-worktree dir named by the ambient env var was reclaimed"
        else
            fail "it was refused as machine-global (see /tmp/pw-r302.$$)"
        fi
        if [[ -d "$SHARED1" ]]; then
            pass "the shared root is still untouched"
        else
            fail "the shared root was deleted"
        fi
    else
        fail "remove 302 exited non-zero (see /tmp/pw-r302.$$)"
    fi
else
    fail "precondition: issue-302's target dir is missing"
fi

# ===========================================================================
# Test 5: REGRESSION — with the feature ENABLED, a machine-global shared root
# is STILL refused. This is the #7239 data-loss guard; neither relaxation may
# reach a path without the per-worktree shape.
# ===========================================================================
echo ""
echo "Test 5: REGRESSION — a machine-global shared root is still never deleted"
R5="$TMP/repo5"
SHARED5="$TMP/shared-root-5"
make_repo "$R5" "$TMP/origin5.git"
share_target_root "$SHARED5"
seed_build_output "$SHARED5" "shared-cache"
wt_create "$R5" 305 /tmp/pw-c305.$$
WT305="$R5/.loom/worktrees/issue-305"
# Overwrite the marker with the SHARED ROOT: the shape check must reject it, so
# resolution falls back to the pre-#8458 path and gate 2f refuses as before.
printf '%s\n' "$SHARED5" > "$WT305/$MARKER"
if ( cd "$R5" && CARGO_TARGET_DIR="$SHARED5" ./.loom/scripts/worktree.sh remove 305 --force ) \
    >/tmp/pw-r305.$$ 2>&1; then
    if [[ -x "$SHARED5/debug/loom-daemon" ]]; then
        pass "the machine-global shared cache survived"
    else
        fail "DATA LOSS: the shared cache was deleted (see /tmp/pw-r305.$$)"
    fi
    if grep -q "Refusing to reclaim cargo target dir" /tmp/pw-r305.$$; then
        pass "the refusal is reported with its reason"
    else
        fail "no refusal reported (see /tmp/pw-r305.$$)"
    fi
else
    fail "remove 305 exited non-zero (see /tmp/pw-r305.$$)"
fi

# ===========================================================================
# Test 6: default OFF
# ===========================================================================
echo ""
echo "Test 6: default OFF — nothing is provisioned"
R6="$TMP/repo6"
SHARED6="$TMP/shared-root-6"
make_repo "$R6" "$TMP/origin6.git"
share_target_root "$SHARED6"
unset LOOM_PER_WORKTREE_TARGET_DIR
wt_create "$R6" 306 /tmp/pw-c306.$$
WT306="$R6/.loom/worktrees/issue-306"
if [[ ! -e "$WT306/$MARKER" ]]; then
    pass "no marker is written when the feature is off"
else
    fail "a marker was written with the feature off"
fi
if [[ ! -d "$SHARED6/wt" ]]; then
    pass "no per-worktree directory tree was created"
else
    fail "$SHARED6/wt was created with the feature off"
fi
export LOOM_PER_WORKTREE_TARGET_DIR=1

# ===========================================================================
# Test 7: an UNREDIRECTED host is a no-op (the #6013/#6014 lesson: never
# relocate a build cache that is already per-worktree).
# ===========================================================================
echo ""
echo "Test 7: unredirected host -> no-op, <worktree>/target is already per-worktree"
R7="$TMP/repo7"
make_repo "$R7" "$TMP/origin7.git"
unshare_target_root
wt_create "$R7" 307 /tmp/pw-c307.$$
WT307="$R7/.loom/worktrees/issue-307"
if [[ ! -e "$WT307/$MARKER" ]]; then
    pass "no marker on a host with no redirect configured"
else
    fail "a marker was written on an unredirected host: $(cat "$WT307/$MARKER")"
fi
if [[ ! -d "$WT307/target/wt" ]]; then
    pass "the in-worktree target/ was not split a level deeper"
else
    fail "provisioning nested a dir under the in-worktree target/"
fi

# ===========================================================================
# Test 8: idempotence — no nesting, and a re-created worktree reuses the marker.
# ===========================================================================
echo ""
echo "Test 8: idempotence — an already-per-worktree root is not nested"
R8="$TMP/repo8"
SHARED8="$TMP/shared-root-8"
make_repo "$R8" "$TMP/origin8.git"
share_target_root "$SHARED8"
PRESET8="$SHARED8/wt/issue-308"
# Exactly what spawn-claude.sh exports for this sweep, seen by worktree.sh as
# the "root" it resolves.
if ( cd "$R8" && CARGO_TARGET_DIR="$PRESET8" ./.loom/scripts/worktree.sh 308 ) >/tmp/pw-c308.$$ 2>&1; then
    WT308="$R8/.loom/worktrees/issue-308"
    if [[ "$(head -n 1 "$WT308/$MARKER" 2>/dev/null)" == "$PRESET8" ]]; then
        pass "the exported per-worktree value is reused verbatim, not nested"
    else
        fail "marker is $(head -n 1 "$WT308/$MARKER" 2>/dev/null), expected $PRESET8"
    fi
    if [[ ! -d "$PRESET8/wt" ]]; then
        pass "no <root>/wt/issue-308/wt/issue-308 nesting"
    else
        fail "provisioning nested a second level"
    fi
    # Re-invoking worktree.sh must keep the same dir.
    ( cd "$R8" && ./.loom/scripts/worktree.sh 308 ) >/tmp/pw-c308b.$$ 2>&1
    if [[ "$(head -n 1 "$WT308/$MARKER" 2>/dev/null)" == "$PRESET8" ]]; then
        pass "a second worktree.sh invocation reuses the existing marker"
    else
        fail "a re-invocation relocated the target dir"
    fi
else
    fail "worktree.sh 308 exited non-zero (see /tmp/pw-c308.$$)"
fi

# ===========================================================================
# Test 9: a hostile/corrupt marker cannot widen the blast radius.
# ===========================================================================
echo ""
echo "Test 9: a marker that does not carry the per-worktree shape is ignored"
# shellcheck source=../lib/cargo-target-dir.sh
source "$LIB_SH"
WT9="$TMP/repo9-worktree/issue-309"
mkdir -p "$WT9"
printf '[package]\nname = "f"\nversion = "0.0.0"\n' > "$WT9/Cargo.toml"
for hostile in "/" "/tmp" "$HOME" "$TMP/shared-root-1" "/wt/issue-309" "relative/wt/issue-309" ""; do
    printf '%s\n' "$hostile" > "$WT9/$MARKER"
    if loom_read_worktree_target_dir_marker "$WT9" >/dev/null 2>&1; then
        fail "marker value '$hostile' was accepted — must be rejected"
    else
        pass "marker value '${hostile:-<empty>}' rejected"
    fi
done
printf '%s\n' "$TMP/some-root/wt/issue-309" > "$WT9/$MARKER"
if [[ "$(loom_read_worktree_target_dir_marker "$WT9")" == "$TMP/some-root/wt/issue-309" ]]; then
    pass "a well-shaped marker is accepted"
else
    fail "a well-shaped marker was rejected"
fi
# A tree cargo never built in must resolve to its own target/, marker or not —
# the #7239 manifest-first ordering.
rm -f "$WT9/Cargo.toml"
if loom_read_worktree_target_dir_marker "$WT9" >/dev/null 2>&1; then
    fail "a manifest-less tree's marker was honored"
else
    pass "a manifest-less tree's marker is ignored (manifest-first, #7239)"
fi

# ===========================================================================
# Test 10: the config tier (`cargo.perWorktreeTargetDir`), not just the env var.
# ===========================================================================
echo ""
echo "Test 10: the feature can be enabled from .loom/config.json"
if ! command -v jq >/dev/null 2>&1; then
    skip "jq not available — config-tier resolution soft-fails without it"
else
    R10="$TMP/repo10"
    SHARED10="$TMP/shared-root-10"
    make_repo "$R10" "$TMP/origin10.git"
    share_target_root "$SHARED10"
    printf '{"cargo": {"perWorktreeTargetDir": true}}\n' > "$R10/.loom/config.json"
    unset LOOM_PER_WORKTREE_TARGET_DIR
    wt_create "$R10" 310 /tmp/pw-c310.$$
    if [[ "$(head -n 1 "$R10/.loom/worktrees/issue-310/$MARKER" 2>/dev/null)" == "$SHARED10/wt/issue-310" ]]; then
        pass "cargo.perWorktreeTargetDir=true enables provisioning"
    else
        fail "config-tier opt-in did not provision (see /tmp/pw-c310.$$)"
    fi
    export LOOM_PER_WORKTREE_TARGET_DIR=1
fi

# ===========================================================================
# Test 11: the SECOND removal path — merge-pr.sh's post-merge cleanup.
#
# `worktree.sh remove` (tests 3/4) and the daemon's clean/reaper (the Rust twin,
# `cargo_target/per_worktree.rs`) are covered elsewhere; this pins the third one
# the issue names. It runs merge-pr.sh's ACTUAL `_remove_loom_worktree` body,
# extracted from the live source with the no-drift `awk` pattern that
# test-merge-pr-worktree-remove-diagnosis.sh established, so a later edit to
# that function cannot silently stop reclaiming per-worktree dirs.
#
# Two things are asserted together, because the bug here is a PAIR: the marker
# is a NEW untracked file born in every opted-in worktree, so if it were not
# filtered out of the dirty-worktree guard, cleanup would refuse on every single
# post-merge removal — turning the per-worktree dir into the very leak this
# scheme exists to close.
# ===========================================================================
echo ""
echo "Test 11: merge-pr.sh's post-merge cleanup reclaims the per-worktree dir"
MERGE_PR="$SCRIPTS_DIR/merge-pr.sh"
if [[ ! -f "$MERGE_PR" ]]; then
    skip "merge-pr.sh not found next to worktree.sh"
else
    R11="$TMP/repo11"
    SHARED11="$TMP/shared-root-11"
    make_repo "$R11" "$TMP/origin11.git"
    share_target_root "$SHARED11"
    export LOOM_PER_WORKTREE_TARGET_DIR=1
    wt_create "$R11" 311 /tmp/pw-c311.$$
    WT311="$R11/.loom/worktrees/issue-311"
    EXPECT311="$SHARED11/wt/issue-311"
    seed_build_output "$EXPECT311" "binary-311"

    # Run the real function body in a subshell: it needs merge-pr.sh's logging
    # helpers and REPO_ROOT, and `eval`ing it here would leak those into the
    # remaining assertions. Nothing is stubbed on the target-dir path: since
    # #9153 the resolve and the reclaim are both `loom-daemon cargo-target-dir`
    # (the binary $LOOM_DAEMON_BIN pins, above), so what runs here is the
    # production decision rather than a shell stand-in for it.
    mp_out="$(
        set +e
        info() { echo "INFO: $*"; }
        warning() { echo "WARN: $*"; }
        success() { echo "OK: $*"; }
        error() {
            echo "ERROR: $*" >&2
            return 1
        }
        loom_record_worktree_removal() { :; }
        # Consumed by the extracted merge-pr.sh function bodies below.
        # shellcheck disable=SC2034
        REPO_ROOT="$R11"
        extract_fn() {
            awk -v fn="$1" '
                $0 ~ "^"fn"\\(\\) \\{" { grab=1 }
                grab { print }
                grab && /^}/ { exit }
            ' "$MERGE_PR"
        }
        # #8191 slice: the porcelain lookups below shell out through
        # _mp_worktree, so it is extracted with them — without it they die with
        # "_mp_worktree: command not found" under `set -e`.
        eval "$(extract_fn _mp_worktree)"
        # The two helpers the body calls for diagnostics are extracted too, not
        # stubbed: a `command not found` from either would be swallowed by the
        # `|| true` around them and quietly change which branch runs.
        eval "$(extract_fn _primary_worktree_path)"
        eval "$(extract_fn _worktree_branch_for)"
        eval "$(extract_fn _remove_loom_worktree)"
        _remove_loom_worktree "$WT311" 2>&1
    )"

    if [[ ! -d "$WT311" ]]; then
        pass "merge-pr cleanup removed the worktree despite the new marker file"
    else
        fail "cleanup refused — the marker is not filtered from the dirty guard: $mp_out"
    fi
    if [[ ! -d "$EXPECT311" ]]; then
        pass "merge-pr cleanup reclaimed the per-worktree target dir"
    else
        fail "the per-worktree dir survived merge-pr cleanup: $mp_out"
    fi
    if [[ -d "$SHARED11" ]]; then
        pass "merge-pr cleanup left the shared root itself untouched"
    else
        fail "merge-pr cleanup deleted the shared root — data-loss regression"
    fi
    # #9153: the reclaim's `LEVEL<TAB>message` record must be replayed through
    # merge-pr.sh's own `success`, not swallowed or reported as a warning. The
    # `OK:` prefix is this suite's `success` stub, so this pins both halves of
    # the record — the SUCCESS level and the shared message grammar.
    if [[ "$mp_out" == *"OK: Reclaimed redirected cargo target dir"* ]]; then
        pass "merge-pr cleanup replays the reclaim record through success()"
    else
        fail "the reclaim record was not replayed as a success: $mp_out"
    fi
fi

# ---------------------------------------------------------------------------
# Test 12: the daemon query verbs the bash library now delegates to
#
# `lib/cargo-target-dir.sh`'s two predicates are thin wrappers over
# `loom-daemon cargo-target-dir is-attributable|marker`, and `spawn-claude.sh`
# derives its export through `path --issue <N>`. Pin the CLI contract all three
# depend on directly, so a change to the exit codes or the worktree-root
# resolution fails here rather than silently disabling a reclaim.
#
# `resolve` / `reclaim` (#9153) are pinned here too: they are merge-pr.sh's
# post-merge removal pair, and their contract is what makes that call site
# degradable — BOTH must exit 0 for every not-applicable case, and `reclaim`
# must stay silent for the un-redirected layout rather than emitting a record
# the shell would replay as a bogus INFO line.
# ---------------------------------------------------------------------------
echo ""
echo "Test 12: the daemon query verbs (is-attributable / marker / path --issue / resolve / reclaim)"
(
    D12="$TMP/verbs"
    WT12="$D12/.loom/worktrees/issue-312"
    mkdir -p "$WT12"
    printf '[workspace]\n' >"$WT12/Cargo.toml"

    "$LOOM_DAEMON_BIN" cargo-target-dir is-attributable "$WT12" /big/cargo-target/wt/issue-312
) && pass "is-attributable: the provisioned shape exits 0" \
  || fail "is-attributable: the provisioned shape should exit 0"
(
    WT12="$TMP/verbs/.loom/worktrees/issue-312"
    # The shared root, a sibling's dir and a shallow path must all exit 1 — this
    # is the predicate that licenses both attribution relaxations.
    for bad in /big/cargo-target /big/cargo-target/wt/issue-311 /wt/issue-312 relative/wt/issue-312; do
        if "$LOOM_DAEMON_BIN" cargo-target-dir is-attributable "$WT12" "$bad"; then
            echo "  unexpectedly attributable: $bad"
            exit 1
        fi
    done
    # …and a candidate LIST exits 0 when any one member matches (the two-candidate
    # form every bash call site uses).
    "$LOOM_DAEMON_BIN" cargo-target-dir is-attributable "$WT12" \
        /big/cargo-target /big/cargo-target/wt/issue-312
) && pass "is-attributable: non-attributable values exit 1; a matching list exits 0" \
  || fail "is-attributable: rejected a matching candidate list, or accepted a bad value"
(
    WT12="$TMP/verbs/.loom/worktrees/issue-312"
    "$LOOM_DAEMON_BIN" cargo-target-dir marker "$WT12" 2>/dev/null && exit 1
    printf '/big/cargo-target/wt/issue-312\n' >"$WT12/.loom-cargo-target-dir"
    got="$("$LOOM_DAEMON_BIN" cargo-target-dir marker "$WT12")" || exit 1
    [[ "$got" == "/big/cargo-target/wt/issue-312" ]] || exit 1
    # A marker naming the shared root degrades to "no marker" (exit 1), never to
    # a path the reclaim would then act on.
    printf '/big/cargo-target\n' >"$WT12/.loom-cargo-target-dir"
    ! "$LOOM_DAEMON_BIN" cargo-target-dir marker "$WT12" 2>/dev/null
) && pass "marker: absent and corrupt exit 1, a well-shaped value prints and exits 0" \
  || fail "marker: wrong exit code or wrong value"
(
    R12="$TMP/verbs-issue"
    SHARED12="$TMP/verbs-issue-shared"
    mkdir -p "$R12" "$SHARED12"
    printf '[workspace]\n' >"$R12/Cargo.toml"
    # `--issue N` must resolve the worktree root itself. LOOM_WORKTREE_ROOT is
    # the highest-precedence tier, and namespaces by repo basename — so this also
    # pins that the Rust resolver, not a bash `<repo>/.loom/worktrees` guess, is
    # what answers.
    got="$(CARGO_TARGET_DIR="$SHARED12" LOOM_PER_WORKTREE_TARGET_DIR=1 \
        "$LOOM_DAEMON_BIN" cargo-target-dir path --repo-root "$R12" --issue 312)"
    [[ "$got" == "$SHARED12/wt/issue-312" ]] || { echo "  got: $got"; exit 1; }
) && pass "path --issue: derives <shared root>/wt/issue-N with the root resolved in Rust" \
  || fail "path --issue: did not derive the expected directory"
(
    # `resolve` is marker-first, and answers for a worktree that is still on
    # disk — merge-pr.sh's pre-removal call.
    WT12R="$TMP/verbs-resolve/.loom/worktrees/issue-313"
    mkdir -p "$WT12R"
    printf '[workspace]\n' >"$WT12R/Cargo.toml"
    printf '/big/cargo-target/wt/issue-313\n' >"$WT12R/.loom-cargo-target-dir"
    got="$("$LOOM_DAEMON_BIN" cargo-target-dir resolve "$WT12R")" || exit 1
    [[ "$got" == "/big/cargo-target/wt/issue-313" ]] || { echo "  got: $got"; exit 1; }
    # No marker and no redirect ⇒ the in-worktree default, still exit 0. That
    # value is what makes the reclaim below a silent no-op rather than a refusal.
    rm -f "$WT12R/.loom-cargo-target-dir"
    got="$("$LOOM_DAEMON_BIN" cargo-target-dir resolve "$WT12R")" || exit 1
    [[ "$got" == "$WT12R/target" ]] || { echo "  got: $got"; exit 1; }
) && pass "resolve: the marker wins, and an unredirected worktree still exits 0" \
  || fail "resolve: wrong value or non-zero exit"
(
    # `reclaim` must exit 0 and print NOTHING for the in-worktree default (the
    # `Inside` outcome): merge-pr.sh replays any line it gets, so a record here
    # would add spurious output to every un-redirected merge.
    R12C="$TMP/verbs-reclaim"
    WT12C="$R12C/.loom/worktrees/issue-314"
    mkdir -p "$WT12C/target/debug"
    printf '[workspace]\n' >"$R12C/Cargo.toml"
    out="$("$LOOM_DAEMON_BIN" cargo-target-dir reclaim "$WT12C" \
        --resolved "$WT12C/target" --repo-root "$R12C")" || exit 1
    [[ -z "$out" ]] || { echo "  unexpected record: $out"; exit 1; }
    # A path nothing may ever delete comes back as a WARNING record, tab-separated
    # — the shape merge-pr.sh's `IFS=$'\t' read` splits on.
    out="$("$LOOM_DAEMON_BIN" cargo-target-dir reclaim "$WT12C" \
        --resolved / --repo-root "$R12C")" || exit 1
    [[ "$out" == "WARNING"$'\t'* ]] || { echo "  got: $out"; exit 1; }
) && pass "reclaim: silent + exit 0 for the in-worktree default; a refusal is a WARNING record" \
  || fail "reclaim: wrong record shape or non-zero exit"

echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ "$TESTS_FAILED" -eq 0 ]] || exit 1
exit 0
