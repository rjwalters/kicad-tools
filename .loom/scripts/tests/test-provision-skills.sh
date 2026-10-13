#!/usr/bin/env bash
# test-provision-skills.sh — tests for user-scope Loom skills + agents
# provisioning (Epic #3835 Phase 4, #4261).
#
# Covers scripts/install/provision-skills.sh:
#   - fresh provision creates the whole-dir commands link + all agent links,
#     all pointing THROUGH the machine checkout (#4261 design decision 3)
#   - the verifiable-globals contract (PROVISIONED_SKILLS_LINK /
#     PROVISIONED_AGENT_LINKS, mirroring provision-dispatcher.sh #4053)
#   - idempotent re-provision
#   - a pre-existing REAL file/dir at a destination is left untouched + warned
#     (never clobber an operator's hand-tuned agent or a real commands dir)
#   - a stale symlink (into an old checkout) is repointed
#   - a dangling loom-* agent link (renamed/removed role) is pruned
#   - soft failure (missing source) returns 1 without aborting the caller
#   - deprovision removes links iff they point into the checkout, never real files
#
# Sandboxed $HOME per case via mktemp -d, matching test-loom-dispatcher.sh.
#
# Source-tree-only by design (#6194/#6241): scripts/install/provision-skills.sh
# lives at the repo root, not under defaults/, so it is never shipped into an
# installed consumer repo. This suite SKIPs (exit 0) rather than errors when
# run outside Loom's own checkout.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# defaults/scripts/tests -> defaults/scripts/tests/../../.. -> repo root
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

PROVISION_LIB="$REPO_ROOT/scripts/install/provision-skills.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$2', got '$1')"; fi
}
assert_contains() {
    if [[ "$1" == *"$2"* ]]; then pass "$3"; else fail "$3 (missing substring: '$2' in: $1)"; fi
}

if [[ ! -f "$PROVISION_LIB" ]]; then
    echo "SKIP: source-tree-only test, $PROVISION_LIB not found (not shipped into an installed repo)" >&2
    exit 0
fi

# shellcheck source=/dev/null
source "$PROVISION_LIB"

# Build a fake machine-level checkout carrying the shipped skill + agent trees.
# Echoes the checkout path. Optionally wrap it in a symlink (as the real
# install does: ~/.local/share/loom -> source checkout) via make_linked_checkout.
make_real_checkout() {
    local c; c="$(mktemp -d)"
    mkdir -p "$c/defaults/.claude/commands/loom" "$c/defaults/.claude/agents"
    # A handful of representative skill files + a cross-referenced include.
    for s in builder judge sweep probe-protocol; do
        echo "# $s skill" > "$c/defaults/.claude/commands/loom/$s.md"
    done
    echo "# star skill" > "$c/defaults/.claude/commands/loom/star.md"
    for a in builder judge doctor curator champion; do
        echo "# loom-$a agent" > "$c/defaults/.claude/agents/loom-$a.md"
    done
    printf '%s\n' "$c"
}

# ── Test 1: fresh provision creates all links, pointing through the checkout ──
echo "Test 1: fresh provision links the commands dir + every agent (AC1/AC2)"
CHK=$(make_real_checkout)
HOME_T=$(mktemp -d)
# Run in the CURRENT shell (not $(...) which would subshell the globals away),
# capturing output via a temp file — matches how test-loom-dispatcher.sh asserts
# on provisioning globals after a direct call.
OUT_FILE=$(mktemp)
set +e
provision_loom_skills "$CHK" "$HOME_T/.claude" >"$OUT_FILE" 2>&1
rc=$?
set -e 2>/dev/null || true
out=$(cat "$OUT_FILE")
assert_eq "$rc" "0" "fresh provision returns 0"

DEST_CMD="$HOME_T/.claude/commands/loom"
if [[ -L "$DEST_CMD" ]]; then
    tgt="$(readlink "$DEST_CMD")"
    assert_eq "$tgt" "$CHK/defaults/.claude/commands/loom" "commands is a whole-dir symlink through the checkout"
else
    fail "commands destination was not created as a symlink"
fi
# The link resolves — a skill file is readable through it.
[[ -f "$DEST_CMD/builder.md" ]] && pass "a skill file resolves through the commands link" || fail "skill file does not resolve through the commands link"

agent_ok=true
for a in builder judge doctor curator champion; do
    d="$HOME_T/.claude/agents/loom-$a.md"
    [[ -L "$d" && "$(readlink "$d")" == "$CHK/defaults/.claude/agents/loom-$a.md" ]] || agent_ok=false
done
$agent_ok && pass "every loom-* agent is a per-file symlink through the checkout" || fail "not all agent links point through the checkout"

# ── Test 2: verifiable-globals contract (#4053) ──────────────────────────────
echo "Test 2: provisioning exposes verifiable globals (#4053)"
assert_eq "$PROVISIONED_SKILLS_LINK" "$DEST_CMD" "PROVISIONED_SKILLS_LINK points at the commands link"
agent_count=$(printf '%s\n' "$PROVISIONED_AGENT_LINKS" | grep -c 'loom-' || true)
assert_eq "$agent_count" "5" "PROVISIONED_AGENT_LINKS enumerates all 5 agent links"

# ── Test 3: idempotent re-provision ──────────────────────────────────────────
echo "Test 3: re-provision is idempotent (second run returns 0, no changes)"
before=$(cd "$HOME_T/.claude" && find . | sort)
set +e
provision_loom_skills "$CHK" "$HOME_T/.claude" >/dev/null 2>&1
rc=$?
set -e 2>/dev/null || true
after=$(cd "$HOME_T/.claude" && find . | sort)
assert_eq "$rc" "0" "second provision returns 0"
assert_eq "$after" "$before" "tree is byte-identical after a second provision"

# ── Test 4: pre-existing REAL file/dir is left untouched + warned ────────────
echo "Test 4: a real destination file/dir is preserved, never clobbered (design decision 4)"
CHK2=$(make_real_checkout)
HOME2=$(mktemp -d)
mkdir -p "$HOME2/.claude/commands/loom" "$HOME2/.claude/agents"
echo "OPERATOR REAL COMMANDS" > "$HOME2/.claude/commands/loom/mine.md"
echo "OPERATOR REAL AGENT" > "$HOME2/.claude/agents/loom-judge.md"
set +e
out=$(provision_loom_skills "$CHK2" "$HOME2/.claude" 2>&1)
set -e 2>/dev/null || true
# Real commands dir preserved (still a dir with the operator's file, not a link).
if [[ -L "$HOME2/.claude/commands/loom" ]]; then
    fail "clobbered the operator's real commands directory with a symlink"
else
    assert_eq "$(cat "$HOME2/.claude/commands/loom/mine.md")" "OPERATOR REAL COMMANDS" "operator's real commands dir is preserved"
fi
assert_contains "$out" "leaving as-is" "provisioning warns it left the real commands dir alone"
# Real operator agent preserved (real file, not a link).
[[ -L "$HOME2/.claude/agents/loom-judge.md" ]] && fail "clobbered operator's real loom-judge.md" \
    || assert_eq "$(cat "$HOME2/.claude/agents/loom-judge.md")" "OPERATOR REAL AGENT" "operator's real loom-judge.md is preserved"
# Other agents still linked despite the one preserved real file.
[[ -L "$HOME2/.claude/agents/loom-builder.md" ]] && pass "sibling agents still linked when one is a real file" || fail "sibling agents were not linked"

# ── Test 5: a stale symlink into an old checkout is repointed ─────────────────
echo "Test 5: a symlink into an OLD checkout is repointed to the current one (design decision 4)"
OLD=$(make_real_checkout)
NEW=$(make_real_checkout)
HOME3=$(mktemp -d)
mkdir -p "$HOME3/.claude/commands" "$HOME3/.claude/agents"
ln -s "$OLD/defaults/.claude/commands/loom" "$HOME3/.claude/commands/loom"
ln -s "$OLD/defaults/.claude/agents/loom-builder.md" "$HOME3/.claude/agents/loom-builder.md"
set +e
provision_loom_skills "$NEW" "$HOME3/.claude" >/dev/null 2>&1
set -e 2>/dev/null || true
assert_eq "$(readlink "$HOME3/.claude/commands/loom")" "$NEW/defaults/.claude/commands/loom" "stale commands link repointed to the new checkout"
assert_eq "$(readlink "$HOME3/.claude/agents/loom-builder.md")" "$NEW/defaults/.claude/agents/loom-builder.md" "stale agent link repointed to the new checkout"

# ── Test 6: a dangling loom-* agent link is pruned ───────────────────────────
echo "Test 6: a dangling loom-* agent link (removed role) is pruned (design decision 5)"
CHK6=$(make_real_checkout)
HOME6=$(mktemp -d)
mkdir -p "$HOME6/.claude/agents"
# Simulate a role that used to exist in the checkout but was removed upstream:
# the link points into the checkout's agents tree but the target is gone.
ln -s "$CHK6/defaults/.claude/agents/loom-removed.md" "$HOME6/.claude/agents/loom-removed.md"
# A dangling link that points OUTSIDE the checkout must NOT be pruned.
ln -s "/nonexistent/operator/place/loom-mine.md" "$HOME6/.claude/agents/loom-mine.md"
set +e
provision_loom_skills "$CHK6" "$HOME6/.claude" >/dev/null 2>&1
set -e 2>/dev/null || true
[[ -e "$HOME6/.claude/agents/loom-removed.md" || -L "$HOME6/.claude/agents/loom-removed.md" ]] \
    && fail "dangling in-checkout agent link was not pruned" \
    || pass "dangling in-checkout agent link pruned"
[[ -L "$HOME6/.claude/agents/loom-mine.md" ]] \
    && pass "a dangling link OUTSIDE the checkout is left alone (not Loom's to prune)" \
    || fail "wrongly pruned an operator link pointing outside the checkout"

# ── Test 7: soft failure (missing source) returns 1, does not abort ──────────
echo "Test 7: a missing skills source returns 1 (soft failure) without aborting the caller"
HOME7=$(mktemp -d)
OUT7=$(mktemp)
set +e
provision_loom_skills "/nonexistent/checkout/xyz" "$HOME7/.claude" >"$OUT7" 2>&1
rc=$?
set -e 2>/dev/null || true
out=$(cat "$OUT7")
assert_eq "$rc" "1" "missing source returns 1"
assert_contains "$out" "skills source not found" "explains the missing source"
assert_eq "$PROVISIONED_SKILLS_LINK" "$HOME7/.claude/commands/loom" "globals still published on the soft-fail path"

# Prove non-abort: a caller with 'set -e' semantics can note the failure and go on.
caller_reached_end=false
run_caller() {
    provision_loom_skills "/nonexistent/checkout/xyz" "$HOME7/.claude" >/dev/null 2>&1 || true
    caller_reached_end=true
}
run_caller
$caller_reached_end && pass "caller continues past a soft failure" || fail "soft failure aborted the caller"

# ── Test 8: deprovision removes checkout links but never real files ──────────
echo "Test 8: deprovision removes links into the checkout, preserves real operator files (design decision 9)"
CHK8=$(make_real_checkout)
HOME8=$(mktemp -d)
provision_loom_skills "$CHK8" "$HOME8/.claude" >/dev/null 2>&1
# Add a real operator agent alongside the links.
echo "OPERATOR" > "$HOME8/.claude/agents/loom-mine.md"  # not from the checkout
set +e
deprovision_loom_skills "$CHK8" "$HOME8/.claude" >/dev/null 2>&1
set -e 2>/dev/null || true
[[ -e "$HOME8/.claude/commands/loom" || -L "$HOME8/.claude/commands/loom" ]] \
    && fail "deprovision did not remove the commands link" \
    || pass "deprovision removed the commands link"
[[ -L "$HOME8/.claude/agents/loom-builder.md" ]] \
    && fail "deprovision did not remove an agent link" \
    || pass "deprovision removed the agent links"
assert_eq "$(cat "$HOME8/.claude/agents/loom-mine.md" 2>/dev/null)" "OPERATOR" "deprovision preserved the operator's real agent file"

# ── Test 9: deprovision leaves a REAL commands dir alone ─────────────────────
echo "Test 9: deprovision never removes a real (non-link) destination"
CHK9=$(make_real_checkout)
HOME9=$(mktemp -d)
mkdir -p "$HOME9/.claude/commands/loom"
echo "REAL" > "$HOME9/.claude/commands/loom/keep.md"
set +e
deprovision_loom_skills "$CHK9" "$HOME9/.claude" >/dev/null 2>&1
set -e 2>/dev/null || true
assert_eq "$(cat "$HOME9/.claude/commands/loom/keep.md" 2>/dev/null)" "REAL" "deprovision preserved a real commands directory"

# ── Test 10: /star alias (#10154) ────────────────────────────────────────────
echo "Test 10: ~/.claude/commands/star.md aliases loom/star.md (#10154)"
CHK10=$(make_real_checkout)
HOME10=$(mktemp -d)
STAR_SRC="$CHK10/defaults/.claude/commands/loom/star.md"
STAR_DEST="$HOME10/.claude/commands/star.md"
provision_loom_skills "$CHK10" "$HOME10/.claude" >/dev/null 2>&1
assert_eq "$(readlink "$STAR_DEST")" "$STAR_SRC" "create: star.md symlinks to the checkout's loom/star.md"
assert_eq "$(cat "$STAR_DEST")" "# star skill" "create: /star resolves to /loom:star content"
echo "# star v2" > "$STAR_SRC"
assert_eq "$(cat "$STAR_DEST")" "# star v2" "single edit of loom/star.md changes /star"
before=$(cd "$HOME10/.claude" && find . | sort)
provision_loom_skills "$CHK10" "$HOME10/.claude" >/dev/null 2>&1
rc=$?
assert_eq "$rc" "0" "idempotent: re-provision returns 0"
assert_eq "$(cd "$HOME10/.claude" && find . | sort)" "$before" "idempotent: tree unchanged"

echo "Test 11: a real star.md is preserved with a warning; stale link repointed (#10154)"
HOME11=$(mktemp -d)
mkdir -p "$HOME11/.claude/commands"
echo "OPERATOR STAR" > "$HOME11/.claude/commands/star.md"
out=$(provision_loom_skills "$CHK10" "$HOME11/.claude" 2>&1)
[[ -L "$HOME11/.claude/commands/star.md" ]] && fail "clobbered operator's real star.md" \
    || assert_eq "$(cat "$HOME11/.claude/commands/star.md")" "OPERATOR STAR" "real star.md preserved"
assert_contains "$out" "star.md already exists" "real star.md produces a warning"
HOME12=$(mktemp -d)
mkdir -p "$HOME12/.claude/commands"
OLD12=$(make_real_checkout)
ln -s "$OLD12/defaults/.claude/commands/loom/star.md" "$HOME12/.claude/commands/star.md"
provision_loom_skills "$CHK10" "$HOME12/.claude" >/dev/null 2>&1
assert_eq "$(readlink "$HOME12/.claude/commands/star.md")" "$STAR_SRC" "stale star.md link repointed"

echo "Test 12: deprovision removes star.md only when it points into the checkout (#10154)"
deprovision_loom_skills "$CHK10" "$HOME10/.claude" >/dev/null 2>&1
[[ -e "$STAR_DEST" || -L "$STAR_DEST" ]] && fail "deprovision left the star alias" || pass "deprovision removed the star alias"
deprovision_loom_skills "$CHK10" "$HOME11/.claude" >/dev/null 2>&1
assert_eq "$(cat "$HOME11/.claude/commands/star.md" 2>/dev/null)" "OPERATOR STAR" "deprovision preserved a real star.md"
HOME13=$(mktemp -d)
mkdir -p "$HOME13/.claude/commands"
ln -s "/elsewhere/star.md" "$HOME13/.claude/commands/star.md"
deprovision_loom_skills "$CHK10" "$HOME13/.claude" >/dev/null 2>&1
assert_eq "$(readlink "$HOME13/.claude/commands/star.md")" "/elsewhere/star.md" "deprovision preserved a foreign star.md symlink"

echo "Test 13: resync-installed.sh (re)provisions user-scope links best-effort, never fatal (#10238)"
set +e
RESYNC="$REPO_ROOT/defaults/scripts/resync-installed.sh"
make_resync_repo() {
    local r; r="$(mktemp -d)"
    mkdir -p "$r/scripts/install" "$r/defaults/hooks" "$r/.loom/hooks" \
             "$r/defaults/.claude/commands/loom" "$r/defaults/.claude/agents"
    cp "$REPO_ROOT/scripts/loom" "$r/scripts/loom"
    cp "$REPO_ROOT/scripts/install/provision-dispatcher.sh" "$REPO_ROOT/scripts/install/provision-skills.sh" "$r/scripts/install/"
    echo "# star" > "$r/defaults/.claude/commands/loom/star.md"
    echo "# loom-builder agent" > "$r/defaults/.claude/agents/loom-builder.md"
    echo A > "$r/defaults/hooks/guard.sh"; echo OLD > "$r/.loom/hooks/guard.sh"
    git -C "$r" init -q
    printf '%s\n' "$r"
}
if [[ -f "$RESYNC" ]]; then
    RR=$(make_resync_repo); HR=$(mktemp -d)
    (cd "$RR" && HOME="$HR" bash "$RESYNC" >/dev/null 2>&1); rc=$?
    assert_eq "$rc" "0" "resync exits 0 with provisioning enabled"
    assert_eq "$(cat "$RR/.loom/hooks/guard.sh")" "A" "resync still syncs the repo surfaces"
    [[ -L "$HR/.claude/commands/loom" && -L "$HR/.claude/commands/star.md" && -L "$HR/.claude/agents/loom-builder.md" ]] \
        && pass "resync created the commands, /star and agent links" || fail "resync did not create the user-scope links"
    # dry-run provisions nothing
    RR2=$(make_resync_repo); HR2=$(mktemp -d)
    (cd "$RR2" && HOME="$HR2" bash "$RESYNC" --dry-run >/dev/null 2>&1)
    [[ -e "$HR2/.claude" ]] && fail "--dry-run provisioned user-scope links" || pass "--dry-run provisions nothing"
    # a provisioning failure (broken lib) does not change the exit status
    RR3=$(make_resync_repo); HR3=$(mktemp -d)
    echo 'provision_loom_skills() { return 1; }; provision_loom_dispatcher() { exit 9; }' > "$RR3/scripts/install/provision-skills.sh"
    (cd "$RR3" && HOME="$HR3" bash "$RESYNC" >/dev/null 2>&1); rc=$?
    assert_eq "$rc" "0" "provisioning failure does not change resync's exit status"
else
    echo "  SKIP: resync-installed.sh not found"
fi

echo ""
echo "======================================"
echo "test-provision-skills.sh: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
echo "======================================"
[[ "$TESTS_FAILED" -eq 0 ]]
