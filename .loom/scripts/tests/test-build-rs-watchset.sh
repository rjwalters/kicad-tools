#!/usr/bin/env bash
# test-build-rs-watchset.sh — Regression test for loom-daemon/build.rs's
# `cargo:rerun-if-changed` watch set (Issue #4053).
#
# The baked LOOM_DAEMON_GIT_COMMIT (surfaced in `loom-daemon --version`) must be
# re-derived whenever HEAD moves, or a rebuild silently ships a stale commit and
# any self-update loop that trusts it becomes an infinite retry. The historical
# watch set (`../.git/HEAD` + `../.git/index`) did NOT track HEAD movement:
# `.git/HEAD` is a symbolic ref whose content never changes when the branch
# advances, and the file that actually moves (`.git/refs/heads/<branch>`, or
# `.git/packed-refs`) was not watched. In a linked worktree neither `../.git/*`
# path even resolved.
#
# Build scripts are awkward to unit-test, so this shell test compiles a tiny
# scratch crate whose build.rs is a two-line shim around a VERBATIM COPY of the
# real loom-daemon/build_stamp.rs (the git-stamp / watch-set half of build.rs;
# it uses only std, so it is copy-portable — build.rs itself needs `tar`/`zstd`
# for the install payload, which a hermetic scratch crate cannot fetch) inside a
# scratch git repo, then advances the branch with a REF-ONLY move
# (`git commit-tree` + `git update-ref`, touching neither HEAD nor the index)
# and asserts the embedded commit changed. It exercises:
#   - the main checkout (the decisive ref-only-move regression: old code would
#     NOT re-run build.rs here and the commit would stay stale)
#   - a linked git worktree, where `.git` is a *file* (every Loom Builder's
#     actual compile environment)
#   - a detached HEAD (no symbolic ref — must not error)
#   - a packed-refs repo (loose ref absent — must still bake the right commit)
#
# It is a standalone shell test (NOT run by `cargo test`) because it shells out
# to `cargo build`; it self-skips (exit 0) when `cargo`/`git` are unavailable.
#
# Usage:
#   ./defaults/scripts/tests/test-build-rs-watchset.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# defaults/scripts/tests -> repo root is three levels up.
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
REAL_BUILD_RS="$REPO_ROOT/loom-daemon/build_stamp.rs"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} $msg"
        echo -e "  expected: [$expected]"
        echo -e "  actual:   [$actual]"
    fi
}

assert_ne() {
    local a="$1" b="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$a" != "$b" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} $msg"
        echo -e "  both were: [$a]"
    fi
}

# ---------- preflight / self-skip ----------
if ! command -v cargo >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1; then
    echo -e "${YELLOW}SKIP:${NC} cargo/git not available — build.rs watch-set test skipped."
    exit 0
fi
if [[ ! -f "$REAL_BUILD_RS" ]]; then
    echo -e "${YELLOW}SKIP:${NC} $REAL_BUILD_RS not found — build.rs watch-set test skipped."
    exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Scaffold a tiny standalone crate whose build.rs is the real one, and whose
# main prints the baked commit. `[workspace]` makes it standalone so cargo does
# not try to attach it to any ambient workspace.
scaffold_crate() {
    local dir="$1"
    mkdir -p "$dir/src"
    cat > "$dir/Cargo.toml" <<'EOF'
[package]
name = "wswatch"
version = "0.0.0"
edition = "2021"

[workspace]
EOF
    cat > "$dir/src/main.rs" <<'EOF'
fn main() {
    println!("{}", env!("LOOM_DAEMON_GIT_COMMIT"));
}
EOF
    cp "$REAL_BUILD_RS" "$dir/build_stamp.rs"
    cat > "$dir/build.rs" <<'EOF'
mod build_stamp;

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    build_stamp::emit();
}
EOF
}

# Build the crate at $1 and echo the commit its binary reports.
build_commit() {
    local dir="$1"
    ( cd "$dir" && cargo build --release >/dev/null 2>&1 ) || { echo "BUILD_FAILED"; return 1; }
    "$dir/target/release/wswatch" 2>/dev/null
}

# Advance the branch checked out at $1 with a REF-ONLY move: reuse the current
# tree, create a child commit via commit-tree, and point the branch ref at it
# via update-ref. This touches neither .git/HEAD (symbolic, unchanged) nor the
# index — so only the resolved ref file (or packed-refs) moves, which is exactly
# the movement the old watch set failed to track.
advance_ref_only() {
    local dir="$1"
    local branch tree new_commit
    branch="$(cd "$dir" && git symbolic-ref -q HEAD)" || return 1
    tree="$(cd "$dir" && git rev-parse "HEAD^{tree}")"
    new_commit="$(cd "$dir" && git commit-tree "$tree" -p HEAD -m advance)"
    (cd "$dir" && git update-ref "$branch" "$new_commit")
}

# The scratch crate is placed in a SUBDIRECTORY (`loom-daemon/`) of the git repo,
# exactly mirroring the real layout — this is load-bearing for the test's teeth:
# with the old `../.git/HEAD`+`../.git/index` watch set, `../.git/*` resolves to
# the real gitdir (a subdir crate), the watched files do NOT change on a ref-only
# advance, so the old build.rs would NOT re-run and the baked commit would stay
# stale — failing subtest 1 below. (If the crate were the repo root, `../.git`
# would point outside the repo, not exist, and mask the bug via cargo's
# always-rerun-on-missing-path behavior.)
git_init_commit() {
    local repo="$1"
    ( cd "$repo" && git init -q \
        && git -c user.email=t@t -c user.name=t add -A \
        && git -c user.email=t@t -c user.name=t commit -q -m init )
}

# ============================================================
# 1. Main checkout: a ref-only advance re-bakes the new commit.
#    (Old watch set leaves the commit stale here — the teeth.)
# ============================================================
MAIN="$WORK/main"
MAIN_CRATE="$MAIN/loom-daemon"
scaffold_crate "$MAIN_CRATE"
git_init_commit "$MAIN"
C1="$(build_commit "$MAIN_CRATE")"
HEAD1="$(cd "$MAIN_CRATE" && git rev-parse --short HEAD)"
assert_eq "$HEAD1" "$C1" "main checkout: initial build bakes in current HEAD"

advance_ref_only "$MAIN_CRATE"
C2="$(build_commit "$MAIN_CRATE")"
HEAD2="$(cd "$MAIN_CRATE" && git rev-parse --short HEAD)"
assert_eq "$HEAD2" "$C2" "main checkout: rebuild after ref-only advance bakes in the NEW HEAD"
assert_ne "$C1" "$C2" "main checkout: baked commit actually CHANGED after ref-only advance"

# ============================================================
# 2. Linked worktree (`.git` is a FILE): the same ref-only advance
#    on the worktree's branch re-bakes. This is the Loom Builder's
#    real compile environment and the case the old watch set could
#    not resolve at all.
# ============================================================
WT="$WORK/wt"
WT_CRATE="$WT/loom-daemon"
( cd "$MAIN" && git worktree add -q -b wt-branch "$WT" >/dev/null 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$WT/.git" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} worktree fixture: .git is a file (linked worktree)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} worktree fixture: .git is a file (linked worktree)"
fi
WC1="$(build_commit "$WT_CRATE")"
WHEAD1="$(cd "$WT_CRATE" && git rev-parse --short HEAD)"
assert_eq "$WHEAD1" "$WC1" "linked worktree: initial build bakes in current HEAD"

advance_ref_only "$WT_CRATE"
WC2="$(build_commit "$WT_CRATE")"
WHEAD2="$(cd "$WT_CRATE" && git rev-parse --short HEAD)"
assert_eq "$WHEAD2" "$WC2" "linked worktree: rebuild after ref-only advance bakes in the NEW HEAD"
assert_ne "$WC1" "$WC2" "linked worktree: baked commit actually CHANGED after ref-only advance"

# ============================================================
# 3. Detached HEAD: build.rs must not error and must bake the
#    correct commit (no symbolic ref to resolve).
# ============================================================
DET="$WORK/det"
DET_CRATE="$DET/loom-daemon"
scaffold_crate "$DET_CRATE"
( cd "$DET" && git init -q && git -c user.email=t@t -c user.name=t add -A \
    && git -c user.email=t@t -c user.name=t commit -q -m init \
    && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m second )
DET_SHA="$(cd "$DET_CRATE" && git rev-parse --short HEAD)"
( cd "$DET" && git checkout -q --detach HEAD )
DC="$(build_commit "$DET_CRATE")"
assert_eq "$DET_SHA" "$DC" "detached HEAD: build succeeds and bakes the correct commit"

# ============================================================
# 4. Packed-refs repo (loose ref absent): build.rs must still bake
#    the correct commit — packed-refs is in the watch set.
# ============================================================
PK="$WORK/pk"
PK_CRATE="$PK/loom-daemon"
scaffold_crate "$PK_CRATE"
( cd "$PK" && git init -q && git -c user.email=t@t -c user.name=t add -A \
    && git -c user.email=t@t -c user.name=t commit -q -m init \
    && git pack-refs --all )
TESTS_RUN=$((TESTS_RUN + 1))
if [[ ! -f "$PK/.git/refs/heads/$(cd "$PK" && git symbolic-ref --short HEAD)" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} packed-refs fixture: loose branch ref is absent (packed)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} packed-refs fixture: loose branch ref is absent (packed)"
fi
PC="$(build_commit "$PK_CRATE")"
PHEAD="$(cd "$PK_CRATE" && git rev-parse --short HEAD)"
assert_eq "$PHEAD" "$PC" "packed-refs: build bakes the correct commit with only packed-refs present"

# ---------- summary ----------
echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
