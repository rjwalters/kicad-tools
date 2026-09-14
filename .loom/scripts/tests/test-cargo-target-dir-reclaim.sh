#!/usr/bin/env bash
# test-cargo-target-dir-reclaim.sh — `worktree.sh remove` reclaims a REDIRECTED
# cargo target directory (issue #7239).
#
# Covers the removal-time half of lib/cargo-target-dir.sh:
#   1. A per-worktree target dir redirected OUTSIDE the worktree is reclaimed
#      when its worktree is removed.
#   2. A target dir another LIVE worktree also resolves to is left untouched
#      (the host-optimize single-shared-target-dir convention) — and IS
#      reclaimed once that last referencing worktree is gone too.
#   3. `--dry-run` lists the reclaimable dir with its size and deletes nothing.
#   4. The default in-worktree `target/` is never reported as a reclaim (it
#      disappears with the worktree for free).
#   5. A dir a live process is sitting in is left untouched.
#   6. Paths the pass must never delete (the repo itself, the primary
#      checkout's own `target/`) are refused.
#   7. Resolution parity with the standalone scripts/cargo-target-dir.sh, so
#      the library twin can never drift from the script it mirrors.
#   9. An ambient CARGO_TARGET_DIR is never treated as this worktree's own
#      directory — the data-loss regression this suite exists to pin.
#
# Follows the throwaway-repo harness pattern in test-worktree-remove.sh: a bare
# origin remote + a working repo, with worktree.sh + its lib/ helpers copied
# into a temp tree, then the script driven directly. Hermetic: no forge, no
# network, no Rust toolchain (a stub `cargo metadata`, below, stands in for the
# real one).
#
# ## Why the redirects here come from .cargo/config.toml, not CARGO_TARGET_DIR
#
# CARGO_TARGET_DIR is read from the REMOVER'S OWN environment: it is machine-
# or session-global and resolves identically for every path on the host, so it
# can never be evidence that a directory belongs to one worktree. Driving these
# fixtures with it would have been testing the one shape the library must
# refuse (and did, before this suite was rewritten alongside that fix — Test 9
# is the regression guard). Every redirect below therefore uses the mechanism
# the feature actually reclaims: `build.target-dir` in a per-worktree
# `.cargo/config.toml`, the host-optimize convention issue #7239 describes.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"

WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"
LIB_SH="$SCRIPTS_DIR/lib/cargo-target-dir.sh"
STANDALONE_SH="$REPO_ROOT/scripts/cargo-target-dir.sh"

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

# --- Throwaway repo setup ---------------------------------------------------
TMP=$(mktemp -d /tmp/loom-target-reclaim.XXXXXX)
HOLDER_PID=""
cleanup() {
    [[ -n "$HOLDER_PID" ]] && kill "$HOLDER_PID" 2>/dev/null
    rm -rf "$TMP"
}
trap 'cleanup' EXIT

git init -q -b main "$TMP/origin.git" --bare
git init -q -b main "$TMP/repo"
cd "$TMP/repo" || exit 1
git config user.email t@t
git config user.name t
git commit --allow-empty -q -m init
git remote add origin "$TMP/origin.git"
git push -q origin main

mkdir -p .loom/scripts/lib
cp "$WORKTREE_SH" .loom/scripts/worktree.sh
if [[ -d "$SCRIPTS_DIR/lib" ]]; then
    cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
fi
chmod +x .loom/scripts/worktree.sh

REPO="$TMP/repo"

# --- A hermetic stand-in for `cargo metadata` -------------------------------
# The resolver reads a `build.target-dir` redirect by asking cargo, so a
# config.toml-driven fixture needs *some* cargo on PATH. Depending on a real
# Rust toolchain would make this suite non-hermetic (it runs in the shell-only
# CI job) and slow, so stand in a cargo that implements exactly the slice of
# the contract the resolver uses: report `target_directory` from the nearest
# `.cargo/config.toml` on the lookup path (env first, as real cargo does),
# else `<cwd>/target`.
mkdir -p "$TMP/bin" "$TMP/cargo-home"
#
# Two behaviors of real cargo matter to the gates below and are reproduced
# faithfully: `$CARGO_HOME/config.toml` is the last stop on the lookup path
# (Test 10's shapes D and E hinge on it), and `cargo metadata` can simply FAIL
# — a mid-edit Cargo.toml, a conflicted merge — which a `.cargo-metadata-broken`
# marker in the workspace simulates hermetically (Test 10b/10c).
cat > "$TMP/bin/cargo" <<'CARGO_STUB'
#!/usr/bin/env bash
[[ "${1:-}" == "metadata" ]] || exit 1
root="$PWD"
[[ -f "$root/.cargo-metadata-broken" ]] && exit 101
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
# An empty CARGO_HOME so a real one on the host (which may itself set
# target-dir) cannot perturb the redirect pre-check.
export CARGO_HOME="$TMP/cargo-home"

make_worktree() {
    local n="$1"
    ( cd "$REPO" && ./.loom/scripts/worktree.sh "$n" ) >/dev/null 2>&1
}

# A target dir with something in it, so `du` reports a non-zero size.
make_target_dir() {
    local dir="$1"
    mkdir -p "$dir/debug"
    head -c 4096 /dev/zero > "$dir/debug/artifact.bin" 2>/dev/null || echo "artifact" > "$dir/debug/artifact.bin"
}

# Point a worktree's build output at <target_dir> exactly the way the
# host-optimize convention does: a per-worktree `.cargo/config.toml` carrying
# `build.target-dir`, next to the manifest that proves this tree really does
# build with cargo. Committed, so the fixture itself does not trip the
# removal path's uncommitted-changes guard.
redirect_worktree() {
    local wt="$1" ext="$2"
    mkdir -p "$wt/.cargo"
    printf '[package]\nname = "fixture"\nversion = "0.0.0"\n' > "$wt/Cargo.toml"
    printf '[build]\ntarget-dir = "%s"\n' "$ext" > "$wt/.cargo/config.toml"
    git -C "$wt" add -A >/dev/null 2>&1
    git -C "$wt" commit -q -m "fixture: redirect the cargo target dir" >/dev/null 2>&1
}

# A worktree that builds with cargo but configures no redirect of its own.
manifest_only_worktree() {
    local wt="$1"
    printf '[package]\nname = "fixture"\nversion = "0.0.0"\n' > "$wt/Cargo.toml"
    git -C "$wt" add -A >/dev/null 2>&1
    git -C "$wt" commit -q -m "fixture: a cargo workspace" >/dev/null 2>&1
}

# A throwaway repo with worktree.sh + its lib/ installed, wired to its own bare
# origin. Several cases below need a repo of a specific shape (a manifest-less
# root, a Rust root, no other live cargo worktree) that reusing $REPO would
# mask.
make_throwaway_repo() {
    local dir="$1" origin="$2"
    git init -q -b main "$origin" --bare
    git init -q -b main "$dir"
    git -C "$dir" config user.email t@t
    git -C "$dir" config user.name t
    git -C "$dir" commit --allow-empty -q -m init
    git -C "$dir" remote add origin "$origin"
    git -C "$dir" push -q origin main
    mkdir -p "$dir/.loom/scripts/lib"
    cp "$WORKTREE_SH" "$dir/.loom/scripts/worktree.sh"
    cp -R "$SCRIPTS_DIR"/lib/* "$dir/.loom/scripts/lib/" 2>/dev/null || true
    chmod +x "$dir/.loom/scripts/worktree.sh"
}

# --- Test 1: a redirected, exclusive target dir is reclaimed -----------------
echo "Test 1: removing a worktree reclaims its redirected (external) cargo target dir"
EXT1="$TMP/ext-target-201"
make_target_dir "$EXT1"
make_worktree 201
redirect_worktree "$REPO/.loom/worktrees/issue-201" "$EXT1"
if [[ -d "$REPO/.loom/worktrees/issue-201" ]]; then
    pass "precondition: worktree issue-201 created"
else
    fail "precondition: worktree issue-201 was not created"
fi
if ( cd "$REPO" && ./.loom/scripts/worktree.sh remove 201 ) >/tmp/tr-out1.$$ 2>&1; then
    if [[ ! -d "$EXT1" ]]; then
        pass "external target dir reclaimed with the worktree"
    else
        fail "external target dir survived the removal (see /tmp/tr-out1.$$)"
    fi
    if grep -q "Reclaimed redirected cargo target dir" /tmp/tr-out1.$$; then
        pass "removal reports what it reclaimed"
    else
        fail "removal did not report the reclaim"
    fi
else
    fail "remove exited non-zero (see /tmp/tr-out1.$$)"
fi

# --- Test 2: a target dir shared with another live worktree is untouched -----
echo ""
echo "Test 2: a target dir another LIVE worktree resolves to is never deleted"
SHARED="$TMP/ext-target-shared"
make_target_dir "$SHARED"
make_worktree 202
make_worktree 203
# Both worktrees redirect into ONE dir — the exact host-optimize shape. The
# harness repo's primary checkout has no Cargo.toml, so it is (correctly) not
# counted as a referent; issue-203 is the one live worktree that shares this
# target dir with the one being removed.
redirect_worktree "$REPO/.loom/worktrees/issue-202" "$SHARED"
redirect_worktree "$REPO/.loom/worktrees/issue-203" "$SHARED"
if ( cd "$REPO" && ./.loom/scripts/worktree.sh remove 202 ) >/tmp/tr-out2.$$ 2>&1; then
    if [[ -d "$SHARED" && -f "$SHARED/debug/artifact.bin" ]]; then
        pass "shared target dir (and its contents) survived removal of one sharer"
    else
        fail "shared target dir was deleted out from under a live worktree (see /tmp/tr-out2.$$)"
    fi
    if grep -q "still used by" /tmp/tr-out2.$$; then
        pass "removal explains that the target dir is still in use"
    else
        fail "removal did not explain why the target dir was kept"
    fi
else
    fail "remove 202 exited non-zero (see /tmp/tr-out2.$$)"
fi

echo ""
echo "Test 2b: the same dir IS reclaimed once the last referencing worktree goes"
if ( cd "$REPO" && ./.loom/scripts/worktree.sh remove 203 --force ) >/tmp/tr-out2b.$$ 2>&1; then
    if [[ ! -d "$SHARED" ]]; then
        pass "target dir reclaimed after the last sharer was removed"
    else
        fail "target dir survived after every referencing worktree was removed (see /tmp/tr-out2b.$$)"
    fi
else
    fail "remove 203 --force exited non-zero (see /tmp/tr-out2b.$$)"
fi

# --- Test 3: --dry-run reports with a size and deletes nothing ---------------
echo ""
echo "Test 3: --dry-run lists the reclaimable target dir with its size, deletes nothing"
EXT3="$TMP/ext-target-204"
make_target_dir "$EXT3"
make_worktree 204
redirect_worktree "$REPO/.loom/worktrees/issue-204" "$EXT3"
if ( cd "$REPO" && ./.loom/scripts/worktree.sh remove 204 --dry-run ) >/tmp/tr-out3.$$ 2>&1; then
    if [[ -d "$EXT3" ]]; then
        pass "--dry-run left the target dir on disk"
    else
        fail "--dry-run DELETED the target dir"
    fi
    if [[ -d "$REPO/.loom/worktrees/issue-204" ]]; then
        pass "--dry-run left the worktree on disk"
    else
        fail "--dry-run removed the worktree"
    fi
    if grep -q "Would reclaim redirected cargo target dir: $EXT3" /tmp/tr-out3.$$; then
        pass "--dry-run names the reclaimable dir"
    else
        fail "--dry-run did not name the reclaimable dir (see /tmp/tr-out3.$$)"
    fi
    if grep -Eq "Would reclaim redirected cargo target dir: .*\([0-9.]+[BKMGT]?\)" /tmp/tr-out3.$$; then
        pass "--dry-run reports a size for the reclaimable dir"
    else
        fail "--dry-run did not report a size (see /tmp/tr-out3.$$)"
    fi
else
    fail "remove --dry-run exited non-zero (see /tmp/tr-out3.$$)"
fi
# --json must still be a single parseable document, now carrying the plan.
if ( cd "$REPO" && ./.loom/scripts/worktree.sh remove 204 --dry-run --json ) >/tmp/tr-out3b.$$ 2>/dev/null; then
    if [[ "$(grep -c . /tmp/tr-out3b.$$)" == "1" ]] && grep -q '"dryRun": true' /tmp/tr-out3b.$$ && \
       grep -q "\"targetDirStatus\": \"would-reclaim\"" /tmp/tr-out3b.$$; then
        pass "--dry-run --json emits one document reporting the target-dir plan"
    else
        fail "--dry-run --json output unexpected (see /tmp/tr-out3b.$$)"
    fi
else
    fail "remove --dry-run --json exited non-zero (see /tmp/tr-out3b.$$)"
fi

# --- Test 4: the default in-worktree target/ is not a "reclaim" --------------
echo ""
echo "Test 4: an un-redirected in-worktree target/ is not reported as a reclaim"
make_worktree 205
make_target_dir "$REPO/.loom/worktrees/issue-205/target"
if ( cd "$REPO" && env -u CARGO_TARGET_DIR ./.loom/scripts/worktree.sh remove 205 --force ) >/tmp/tr-out4.$$ 2>&1; then
    if ! grep -q "redirected cargo target dir" /tmp/tr-out4.$$; then
        pass "no redirected-target-dir reporting for the default layout"
    else
        fail "reported a redirect for a plain in-worktree target/ (see /tmp/tr-out4.$$)"
    fi
    if [[ ! -d "$REPO/.loom/worktrees/issue-205" ]]; then
        pass "worktree (and its in-tree target/) removed as before"
    else
        fail "worktree was not removed"
    fi
else
    fail "remove 205 exited non-zero (see /tmp/tr-out4.$$)"
fi

# --- Test 5: a dir a live process is using is left alone --------------------
echo ""
echo "Test 5: a target dir with a live process inside it is never deleted"
EXT5="$TMP/ext-target-206"
make_target_dir "$EXT5"
make_worktree 206
redirect_worktree "$REPO/.loom/worktrees/issue-206" "$EXT5"
( cd "$EXT5" && exec sleep 120 ) &
HOLDER_PID=$!
sleep 0.3
if ( cd "$REPO" && ./.loom/scripts/worktree.sh remove 206 ) >/tmp/tr-out5.$$ 2>&1; then
    if [[ -d "$EXT5" ]]; then
        pass "target dir backing a live process survived"
    else
        fail "target dir was deleted while a process was using it (see /tmp/tr-out5.$$)"
    fi
    if grep -q "still using it" /tmp/tr-out5.$$; then
        pass "removal names the live-process hold as the reason"
    else
        # /proc and lsof are both fail-open by design; on a host where neither
        # can answer this gate cannot fire, so report rather than hard-fail.
        if [[ -d /proc ]] || command -v lsof >/dev/null 2>&1; then
            fail "no live-process explanation emitted (see /tmp/tr-out5.$$)"
        else
            skip "neither /proc nor lsof available — process gate cannot be exercised"
        fi
    fi
else
    fail "remove 206 exited non-zero (see /tmp/tr-out5.$$)"
fi
kill "$HOLDER_PID" 2>/dev/null
HOLDER_PID=""

# --- Test 6: never-delete paths are refused ---------------------------------
echo ""
echo "Test 6: the primary checkout's own target/ is refused, not reclaimed"
make_target_dir "$REPO/target"
make_worktree 207
redirect_worktree "$REPO/.loom/worktrees/issue-207" "$REPO/target"
if ( cd "$REPO" && ./.loom/scripts/worktree.sh remove 207 ) >/tmp/tr-out6.$$ 2>&1; then
    if [[ -d "$REPO/target" ]]; then
        pass "the primary checkout's own target/ was not deleted"
    else
        fail "the primary checkout's own target/ was deleted (see /tmp/tr-out6.$$)"
    fi
    if grep -q "Refusing to reclaim" /tmp/tr-out6.$$; then
        pass "refusal is explained"
    else
        fail "no refusal explanation emitted (see /tmp/tr-out6.$$)"
    fi
else
    fail "remove 207 exited non-zero (see /tmp/tr-out6.$$)"
fi

echo ""
echo "Test 6b: a target dir containing the repository itself is refused"
make_worktree 208
redirect_worktree "$REPO/.loom/worktrees/issue-208" "$TMP"
if ( cd "$REPO" && ./.loom/scripts/worktree.sh remove 208 ) >/tmp/tr-out6b.$$ 2>&1; then
    if [[ -d "$REPO" ]]; then
        pass "an ancestor of the repository was not deleted"
    else
        fail "CATASTROPHIC: the repository's parent was deleted"
    fi
    if grep -q "Refusing to reclaim" /tmp/tr-out6b.$$; then
        pass "ancestor refusal is explained"
    else
        fail "no refusal explanation for the ancestor case (see /tmp/tr-out6b.$$)"
    fi
else
    fail "remove 208 exited non-zero (see /tmp/tr-out6b.$$)"
fi

# --- Test 6c: an unanswerable sharing question fails CLOSED ------------------
echo ""
echo "Test 6c: a failed 'git worktree list' refuses instead of assuming 'unshared'"
# shellcheck source=../lib/cargo-target-dir.sh
source "$LIB_SH"
EXT6C="$TMP/ext-target-209"
make_target_dir "$EXT6C"
# A directory that is not a git repository at all, so `git worktree list`
# cannot answer. An empty list and a failed list look identical on stdout —
# only the exit status distinguishes them, and mistaking the second for the
# first would delete a dir a sibling may still be building into.
NOT_A_REPO="$TMP/not-a-repo"
mkdir -p "$NOT_A_REPO"
rec6c="$(loom_reclaim_worktree_target_dir "$NOT_A_REPO" "$TMP/phantom-worktree" "$EXT6C" false)"
if [[ -d "$EXT6C" ]]; then
    pass "target dir survived an unanswerable sharing check"
else
    fail "target dir was deleted despite an unanswerable sharing check"
fi
if [[ "$rec6c" == refused* && "$rec6c" == *"enumerate"* ]]; then
    pass "the refusal names the failed worktree enumeration"
else
    fail "expected a 'refused ... enumerate' record, got: $rec6c"
fi

# --- Test 7: resolver parity with scripts/cargo-target-dir.sh ---------------
echo ""
echo "Test 7: the library resolver agrees with scripts/cargo-target-dir.sh"
if [[ ! -x "$STANDALONE_SH" ]]; then
    skip "scripts/cargo-target-dir.sh not present (consumer-repo checkout)"
else
    # shellcheck source=../lib/cargo-target-dir.sh
    source "$LIB_SH"
    PARITY_ROOT="$TMP/parity-root"
    mkdir -p "$PARITY_ROOT"
    parity_case() {
        local label="$1" env_value="$2"
        local lib_says script_says
        if [[ -n "$env_value" ]]; then
            lib_says="$(CARGO_TARGET_DIR="$env_value" loom_resolve_cargo_target_dir "$PARITY_ROOT")"
            script_says="$(CARGO_TARGET_DIR="$env_value" "$STANDALONE_SH" "$PARITY_ROOT" 2>/dev/null)"
        else
            lib_says="$(env -u CARGO_TARGET_DIR bash -c "source '$LIB_SH'; loom_resolve_cargo_target_dir '$PARITY_ROOT'")"
            script_says="$(env -u CARGO_TARGET_DIR "$STANDALONE_SH" "$PARITY_ROOT" 2>/dev/null)"
        fi
        if [[ "$lib_says" == "$script_says" && -n "$lib_says" ]]; then
            pass "parity ($label): both resolve to '$lib_says'"
        else
            fail "parity ($label): lib says '$lib_says', script says '$script_says'"
        fi
    }
    parity_case "absolute CARGO_TARGET_DIR" "$TMP/abs-target"
    parity_case "relative CARGO_TARGET_DIR" "rel-target"
    # No manifest under PARITY_ROOT, so both fall through to `<root>/target`
    # (cargo metadata fails there) without a network call.
    parity_case "no redirect configured" ""
fi

# --- Test 8: merge-pr.sh's post-merge cleanup reclaims too -------------------
# Post-merge cleanup is the removal path most worktrees actually take, so the
# leak lives here more than anywhere else. Drives the REAL `_remove_loom_worktree`
# body extracted from the live source (the no-drift pattern from
# test-merge-pr-dirty-worktree-guard.sh), not a reimplementation.
echo ""
echo "Test 8: merge-pr.sh's post-merge worktree cleanup reclaims a redirected target dir"
MERGE_PR="$SCRIPTS_DIR/merge-pr.sh"
if [[ ! -f "$MERGE_PR" ]]; then
    skip "merge-pr.sh not found"
else
    extract_fn() {
        local name="$1" file="$2"
        awk -v fn="$name" '
          $0 ~ "^"fn"\\(\\) \\{" { grab=1 }
          grab { print }
          grab && /^}/ { exit }
        ' "$file"
    }
    info()    { echo "INFO: $*"; }
    warning() { echo "WARN: $*"; }
    success() { echo "OK: $*"; }
    error()   { echo "ERROR: $*" >&2; return 1; }
    loom_record_worktree_removal() { :; }

    # shellcheck source=../lib/cargo-target-dir.sh
    source "$LIB_SH"
    eval "$(extract_fn _primary_worktree_path "$MERGE_PR")"
    eval "$(extract_fn _worktree_branch_for "$MERGE_PR")"
    eval "$(extract_fn _worktree_branch_fully_captured "$MERGE_PR")"
    eval "$(extract_fn _maybe_delete_local_branch "$MERGE_PR")"
    eval "$(extract_fn _mp_report_target_dir_reclaim "$MERGE_PR")"
    eval "$(extract_fn _remove_loom_worktree "$MERGE_PR")"

    MP_REPO="$TMP/mp-repo"
    git init -q -b main "$MP_REPO"
    git -C "$MP_REPO" config user.email t@t
    git -C "$MP_REPO" config user.name t
    echo hello > "$MP_REPO/README.md"
    git -C "$MP_REPO" add -A
    git -C "$MP_REPO" commit -q -m init
    REPO_ROOT="$MP_REPO"

    MP_WT="$TMP/mp-wt-301"
    git -C "$MP_REPO" worktree add -q -b feature/issue-301 "$MP_WT" >/dev/null 2>&1
    touch "$MP_WT/.loom-managed"
    MP_EXT="$TMP/ext-target-301"
    make_target_dir "$MP_EXT"
    redirect_worktree "$MP_WT" "$MP_EXT"

    mp_out="$(_remove_loom_worktree "$MP_WT" 2>&1)"
    if [[ ! -d "$MP_WT" ]]; then
        pass "merge-pr cleanup removed the worktree"
    else
        fail "merge-pr cleanup did not remove the worktree: $mp_out"
    fi
    if [[ ! -d "$MP_EXT" ]]; then
        pass "merge-pr cleanup reclaimed the redirected target dir"
    else
        fail "merge-pr cleanup left the redirected target dir behind: $mp_out"
    fi
    if [[ "$mp_out" == *"Reclaimed redirected cargo target dir"* ]]; then
        pass "merge-pr cleanup reports the reclaim"
    else
        fail "merge-pr cleanup did not report the reclaim: $mp_out"
    fi

    echo ""
    echo "Test 8b: merge-pr.sh's cleanup never deletes a target dir a live worktree shares"
    MP_SHARED="$TMP/ext-target-shared-302"
    make_target_dir "$MP_SHARED"
    MP_WT2="$TMP/mp-wt-302"
    MP_WT3="$TMP/mp-wt-303"
    git -C "$MP_REPO" worktree add -q -b feature/issue-302 "$MP_WT2" >/dev/null 2>&1
    git -C "$MP_REPO" worktree add -q -b feature/issue-303 "$MP_WT3" >/dev/null 2>&1
    touch "$MP_WT2/.loom-managed" "$MP_WT3/.loom-managed"
    redirect_worktree "$MP_WT2" "$MP_SHARED"
    redirect_worktree "$MP_WT3" "$MP_SHARED"
    mp_out2="$(_remove_loom_worktree "$MP_WT2" 2>&1)"
    if [[ -f "$MP_SHARED/debug/artifact.bin" ]]; then
        pass "shared target dir survived merge-pr cleanup of one sharer"
    else
        fail "merge-pr cleanup deleted a shared target dir: $mp_out2"
    fi
    if [[ "$mp_out2" == *"still used by"* ]]; then
        pass "merge-pr cleanup explains why it kept the dir"
    else
        fail "merge-pr cleanup gave no explanation: $mp_out2"
    fi
fi

# --- Test 9: an ambient CARGO_TARGET_DIR is never this worktree's own dir ----
# The data-loss regression. CARGO_TARGET_DIR is read from the REMOVER'S OWN
# environment — a single shared build cache for the whole machine is a common
# dev-host setting — so its mere presence can never establish that a directory
# belongs to the one worktree being removed. Both halves are pinned here,
# because closing only the first one still deletes:
#
#   9a. A worktree with NO manifest (every non-Rust consumer repo) must not
#       resolve to the ambient path at all: the manifest pre-check runs BEFORE
#       the env short-circuit, so it resolves to its own <worktree>/target.
#   9b. A worktree WITH a manifest, inside a repo whose root and siblings have
#       none, passes that pre-check and does resolve to the ambient path — and
#       the sharing scan skips every manifest-less tree, so nothing looks
#       shared. The reclaim step itself must refuse.
echo ""
echo "Test 9: an ambient CARGO_TARGET_DIR is never treated as this worktree's own dir"
AMBIENT="$TMP/machine-global-cargo-cache"
make_target_dir "$AMBIENT"
echo "another project's build cache" > "$AMBIENT/PRECIOUS.txt"

# A DEDICATED throwaway repo. Reusing $REPO would mask the defect: a
# manifest-bearing worktree left alive by an earlier case (issue-204, kept by
# the --dry-run test) resolves to the ambient dir too, so the sharing gate
# would report "shared" and the dangerous branch would never be reached. This
# repo has exactly the shape the defect needs — a manifest-less root and no
# other cargo worktree.
REPO9="$TMP/repo9"
git init -q -b main "$TMP/origin9.git" --bare
git init -q -b main "$REPO9"
git -C "$REPO9" config user.email t@t
git -C "$REPO9" config user.name t
git -C "$REPO9" commit --allow-empty -q -m init
git -C "$REPO9" remote add origin "$TMP/origin9.git"
git -C "$REPO9" push -q origin main
mkdir -p "$REPO9/.loom/scripts/lib"
cp "$WORKTREE_SH" "$REPO9/.loom/scripts/worktree.sh"
cp -R "$SCRIPTS_DIR"/lib/* "$REPO9/.loom/scripts/lib/" 2>/dev/null || true
chmod +x "$REPO9/.loom/scripts/worktree.sh"

( cd "$REPO9" && ./.loom/scripts/worktree.sh 209 ) >/dev/null 2>&1
if ( cd "$REPO9" && CARGO_TARGET_DIR="$AMBIENT" ./.loom/scripts/worktree.sh remove 209 --force ) >/tmp/tr-out9a.$$ 2>&1; then
    if [[ -f "$AMBIENT/PRECIOUS.txt" ]]; then
        pass "9a: manifest-less worktree + ambient CARGO_TARGET_DIR left the shared cache intact"
    else
        fail "9a: the machine-global cargo cache was DELETED (see /tmp/tr-out9a.$$)"
    fi
    if ! grep -q "Reclaimed redirected cargo target dir" /tmp/tr-out9a.$$; then
        pass "9a: nothing was reported as reclaimed"
    else
        fail "9a: reported a reclaim of the ambient dir (see /tmp/tr-out9a.$$)"
    fi
else
    fail "9a: remove 209 exited non-zero (see /tmp/tr-out9a.$$)"
fi

( cd "$REPO9" && ./.loom/scripts/worktree.sh 210 ) >/dev/null 2>&1
manifest_only_worktree "$REPO9/.loom/worktrees/issue-210"
if ( cd "$REPO9" && CARGO_TARGET_DIR="$AMBIENT" ./.loom/scripts/worktree.sh remove 210 --force ) >/tmp/tr-out9b.$$ 2>&1; then
    if [[ -f "$AMBIENT/PRECIOUS.txt" ]]; then
        pass "9b: manifest-bearing worktree in a manifest-less repo left the shared cache intact"
    else
        fail "9b: the machine-global cargo cache was DELETED (see /tmp/tr-out9b.$$)"
    fi
    if grep -q "machine-global" /tmp/tr-out9b.$$; then
        pass "9b: the refusal names the ambient CARGO_TARGET_DIR as the reason"
    else
        fail "9b: no ambient-env refusal explanation emitted (see /tmp/tr-out9b.$$)"
    fi
else
    fail "9b: remove 210 exited non-zero (see /tmp/tr-out9b.$$)"
fi

# 9c: the gate is scoped to the ambient path ITSELF, not to "an ambient
# CARGO_TARGET_DIR exists". A dir that is genuinely this worktree's own is
# still reclaimed while some unrelated env value points elsewhere — otherwise
# the feature would silently stop working on every host that exports one.
# shellcheck source=../lib/cargo-target-dir.sh
source "$LIB_SH"
EXT9C="$TMP/ext-target-211"
make_target_dir "$EXT9C"
rec9c="$(CARGO_TARGET_DIR="$AMBIENT" loom_reclaim_worktree_target_dir \
    "$REPO" "$REPO/.loom/worktrees/issue-211" "$EXT9C" false)"
if [[ "$rec9c" == reclaimed* && ! -d "$EXT9C" ]]; then
    pass "9c: a dir unrelated to the ambient env value is still reclaimed"
else
    fail "9c: expected a 'reclaimed' record, got: $rec9c"
fi
if [[ -f "$AMBIENT/PRECIOUS.txt" ]]; then
    pass "9c: the unrelated ambient cache is untouched"
else
    fail "9c: the ambient cache was deleted"
fi

# --- Test 10: the other two machine-global attribution sources ---------------
# Same defect class as Test 9, two more members of it. A directory may only be
# deleted on evidence that it belongs to THE ONE worktree being removed, and
# neither of these is such evidence:
#
#   D. `build.target-dir` in $CARGO_HOME/config.toml (or any ancestor
#      .cargo/config.toml above the worktree). Exactly as machine-global as
#      CARGO_TARGET_DIR — every worktree on the host resolves to the same dir.
#   E. A DEGRADED resolution of some other live worktree. When `cargo metadata`
#      fails for a sibling, the resolver falls back to `<sibling>/target`, so a
#      sibling that really does build into the dir we are about to delete stops
#      counting as a sharer. "Builds elsewhere" and "could not find out" must
#      not look alike — the same rule already applied to `git worktree list`.
echo ""
echo "Test 10: a machine-global config redirect is not this worktree's own dir"
# Each sub-case gets its own shared cache and its own $CARGO_HOME, so that a
# case which deletes (as every one of them does against the pre-fix library)
# cannot mask the next one by leaving nothing left to delete.
machine_global_cargo_home() {
    local home="$1" shared="$2"
    mkdir -p "$home"
    make_target_dir "$shared"
    echo "another project's build cache" > "$shared/PRECIOUS.txt"
    printf '[build]\ntarget-dir = "%s"\n' "$shared" > "$home/config.toml"
}

# 10a (shape D): manifest-bearing worktree, manifest-less repo root, redirect
# declared ONLY in $CARGO_HOME/config.toml.
REPO10="$TMP/repo10"
make_throwaway_repo "$REPO10" "$TMP/origin10.git"
CH10A="$TMP/cargo-home-10a"
SHARED10A="$TMP/machine-shared-cache-a"
machine_global_cargo_home "$CH10A" "$SHARED10A"
( cd "$REPO10" && ./.loom/scripts/worktree.sh 401 ) >/dev/null 2>&1
manifest_only_worktree "$REPO10/.loom/worktrees/issue-401"
if ( cd "$REPO10" && CARGO_HOME="$CH10A" ./.loom/scripts/worktree.sh remove 401 --force ) >/tmp/tr-out10a.$$ 2>&1; then
    if [[ -f "$SHARED10A/PRECIOUS.txt" ]]; then
        pass "10a: a \$CARGO_HOME target-dir redirect left the machine-global cache intact"
    else
        fail "10a: the machine-global cargo cache was DELETED (see /tmp/tr-out10a.$$)"
    fi
    if ! grep -q "Reclaimed redirected cargo target dir" /tmp/tr-out10a.$$; then
        pass "10a: nothing was reported as reclaimed"
    else
        fail "10a: reported a reclaim of the machine-global dir (see /tmp/tr-out10a.$$)"
    fi
else
    fail "10a: remove 401 exited non-zero (see /tmp/tr-out10a.$$)"
fi

# 10b (shape E, as reported): an ordinary Rust repo — root manifest and all —
# on a host whose $CARGO_HOME redirects every build into one shared dir. Safety
# used to rest entirely on the sharing scan noticing that the PRIMARY CHECKOUT
# resolves there too; break that one `cargo metadata` call and the scan reports
# "exclusive". The worktree must not resolve to the machine-global dir at all.
echo ""
echo "Test 10b: a primary checkout whose 'cargo metadata' fails does not license a delete"
REPO10B="$TMP/repo10b"
make_throwaway_repo "$REPO10B" "$TMP/origin10b.git"
CH10B="$TMP/cargo-home-10b"
SHARED10B="$TMP/machine-shared-cache-b"
machine_global_cargo_home "$CH10B" "$SHARED10B"
printf '[package]\nname = "root"\nversion = "0.0.0"\n' > "$REPO10B/Cargo.toml"
git -C "$REPO10B" add -A >/dev/null 2>&1
git -C "$REPO10B" commit -q -m "a rust repo" >/dev/null 2>&1
# Pushed, so the new worktree branches off a main that carries the manifest —
# otherwise the worktree would be manifest-less and the case would not be the
# ordinary-Rust-repo shape it is meant to reproduce.
git -C "$REPO10B" push -q origin main
( cd "$REPO10B" && ./.loom/scripts/worktree.sh 402 ) >/dev/null 2>&1
# ONLY the primary checkout's manifest is unreadable to cargo — mid-edit, a
# conflicted merge, any transient error. Left uncommitted and created after the
# worktree so the worktree itself still resolves normally; the whole point of
# the shape is that a healthy worktree resolves to the shared dir while the one
# tree that would have marked it "shared" cannot answer.
touch "$REPO10B/.cargo-metadata-broken"
if ( cd "$REPO10B" && CARGO_HOME="$CH10B" ./.loom/scripts/worktree.sh remove 402 --force ) >/tmp/tr-out10b.$$ 2>&1; then
    if [[ -f "$SHARED10B/PRECIOUS.txt" ]]; then
        pass "10b: the machine-global cache survived a degraded primary-checkout resolution"
    else
        fail "10b: the machine-global cargo cache was DELETED (see /tmp/tr-out10b.$$)"
    fi
    if ! grep -q "Reclaimed redirected cargo target dir" /tmp/tr-out10b.$$; then
        pass "10b: nothing was reported as reclaimed"
    else
        fail "10b: reported a reclaim of the machine-global dir (see /tmp/tr-out10b.$$)"
    fi
else
    fail "10b: remove 402 exited non-zero (see /tmp/tr-out10b.$$)"
fi

# 10c (shape E, in the shape this feature actually reclaims): both worktrees
# carry their OWN .cargo/config.toml pointing at one shared dir — Test 2's
# fixture — but the surviving sibling's `cargo metadata` fails. Restricting the
# redirect pre-check to in-worktree configs does NOT cover this: the sharing
# scan itself has to fail closed on an answer it could not get.
echo ""
echo "Test 10c: a sibling whose 'cargo metadata' fails is not assumed to build elsewhere"
REPO10C="$TMP/repo10c"
make_throwaway_repo "$REPO10C" "$TMP/origin10c.git"
SHARED10C="$TMP/shared-cache-10c"
make_target_dir "$SHARED10C"
echo "the sibling is still building into this" > "$SHARED10C/PRECIOUS.txt"
( cd "$REPO10C" && ./.loom/scripts/worktree.sh 403 ) >/dev/null 2>&1
( cd "$REPO10C" && ./.loom/scripts/worktree.sh 404 ) >/dev/null 2>&1
redirect_worktree "$REPO10C/.loom/worktrees/issue-403" "$SHARED10C"
redirect_worktree "$REPO10C/.loom/worktrees/issue-404" "$SHARED10C"
touch "$REPO10C/.loom/worktrees/issue-404/.cargo-metadata-broken"
if ( cd "$REPO10C" && ./.loom/scripts/worktree.sh remove 403 --force ) >/tmp/tr-out10c.$$ 2>&1; then
    if [[ -f "$SHARED10C/PRECIOUS.txt" ]]; then
        pass "10c: the shared dir survived — the sibling's answer was unavailable, not 'elsewhere'"
    else
        fail "10c: deleted a dir a live sibling builds into (see /tmp/tr-out10c.$$)"
    fi
    if grep -q "cargo metadata failed" /tmp/tr-out10c.$$; then
        pass "10c: the refusal names the unresolvable sibling"
    else
        fail "10c: no degraded-sibling refusal explanation emitted (see /tmp/tr-out10c.$$)"
    fi
else
    fail "10c: remove 403 exited non-zero (see /tmp/tr-out10c.$$)"
fi

# 10d: the gate-2f backstop. A worktree-local config is normally per-worktree
# attribution — but not when it names the very directory a machine-global
# config names. Verified through the library directly, since the point is the
# reclaim-time decision.
echo ""
echo "Test 10d: a worktree-local redirect that duplicates a machine-global one is refused"
CH10D="$TMP/cargo-home-10d"
SHARED10D="$TMP/machine-shared-cache-d"
machine_global_cargo_home "$CH10D" "$SHARED10D"
rec10d="$(CARGO_HOME="$CH10D" env -u CARGO_TARGET_DIR bash -c \
    "source '$LIB_SH'; loom_reclaim_worktree_target_dir '$REPO10' '$REPO10/.loom/worktrees/issue-405' '$SHARED10D' false")"
if [[ -f "$SHARED10D/PRECIOUS.txt" ]]; then
    pass "10d: the machine-global cache was not deleted"
else
    fail "10d: the machine-global cache was deleted"
fi
if [[ "$rec10d" == refused* && "$rec10d" == *"machine-global"* ]]; then
    pass "10d: the refusal names the machine-global config redirect"
else
    fail "10d: expected a 'refused ... machine-global' record, got: $rec10d"
fi

# 10e: and the backstop stays scoped — a genuinely per-worktree dir is still
# reclaimed while an unrelated machine-global redirect exists on the host.
EXT10E="$TMP/ext-target-406"
make_target_dir "$EXT10E"
rec10e="$(CARGO_HOME="$CH10D" env -u CARGO_TARGET_DIR bash -c \
    "source '$LIB_SH'; loom_reclaim_worktree_target_dir '$REPO10' '$REPO10/.loom/worktrees/issue-406' '$EXT10E' false")"
if [[ "$rec10e" == reclaimed* && ! -d "$EXT10E" ]]; then
    pass "10e: a dir unrelated to the machine-global value is still reclaimed"
else
    fail "10e: expected a 'reclaimed' record, got: $rec10e"
fi

# --- Summary ----------------------------------------------------------------
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
