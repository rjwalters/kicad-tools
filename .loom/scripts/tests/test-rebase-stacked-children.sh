#!/usr/bin/env bash
# test-rebase-stacked-children.sh - Unit tests for rebase-stacked-children.sh
# (#3747, stacked-PR v2 item 3: rebase-on-parent-amend).
#
# When a stacked PARENT branch (feature/issue-<N>) is amended/pushed while still
# open under review, any CHILD PR that branched off its pre-amend tip is stale.
# rebase-stacked-children.sh discovers open CHILD PRs based on the parent branch
# via a LIVE forge query (`gh pr list --base <parent>`, never the daemon
# registry) and, per child:
#   - Up to date (parent tip is already an ancestor): no-op, skip.
#   - Stale + safe   (child issue NOT loom:building): rebase onto the parent's
#     current tip + push --force-with-lease. NO PR base retarget.
#   - Stale + unsafe (child issue still loom:building): skip the rebase and post
#     a deferred-rebase comment on the child PR instead.
# It is a no-op for non-feature/issue-N parent branches and non-GitHub forges,
# and --dry-run reports the per-child outcome without any git/gh mutation.
#
# Strategy (mirrors test-merge-pr-auto-reconcile.sh): the functions under test
# (_rebase_stacked_children, _process_one_stacked_child, run) depend only on
# globals (FORGE_TYPE, REPO_NWO, DRY_RUN, RSC_FAILURE) plus the `gh` and `git`
# CLIs. We extract the function definitions from rebase-stacked-children.sh and
# source them, stub `gh` + `git` on PATH to serve canned data and record mutating
# calls, then assert on the recorded calls. Extracting from source (rather than
# replicating) keeps the test in lockstep with the script.
#
# Usage:
#   ./.loom/scripts/tests/test-rebase-stacked-children.sh

# SC2034: several globals (FORGE_TYPE, REPO_NWO, DRY_RUN, RSC_FAILURE) are read
# only by the functions extracted+sourced from rebase-stacked-children.sh, which
# the linter cannot see — every such assignment looks "unused" to it.
# shellcheck disable=SC2034

set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$TEST_DIR/.." && pwd)"
RSC_SRC="$HELPERS_DIR/rebase-stacked-children.sh"
# The extracted function span (see the awk capture below) starts at `run() {`,
# AFTER the real script's own `SCRIPT_DIR="$(cd "$(dirname ...)" && pwd)"`
# assignment -- so the extracted _process_one_stacked_child's reference to
# $SCRIPT_DIR (the #7168 version-check-gate.sh call site) needs it set here
# too. HELPERS_DIR already resolves to the identical directory the real
# script's own SCRIPT_DIR would.
SCRIPT_DIR="$HELPERS_DIR"

# Colors (YELLOW/BLUE are referenced by the extracted run()/info shims under set -u).
# shellcheck source=lib/assert.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"
YELLOW='\033[1;33m'
BLUE='\033[0;34m'


assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if ! [[ "$haystack" == *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

# --- Minimal logging shims the extracted functions call ---
err()  { echo "ERR: $*" >&2; }
ok()   { echo "OK: $*"; }
info() { echo "INFO: $*"; }
warn() { echo "WARN: $*" >&2; }

# --- Extract the functions under test from rebase-stacked-children.sh ---
# From `run() {` up to (not including) the `# ---- main ----` sentinel. This span
# holds run(), _process_one_stacked_child(), and _rebase_stacked_children().
FUNCS_FILE="$(mktemp)"
STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$FUNCS_FILE" "$STUB_DIR" 2>/dev/null || true' EXIT
awk '
  /^run\(\) \{/       { capture=1 }
  /^# ---- main ----/ { capture=0 }
  capture { print }
' "$RSC_SRC" > "$FUNCS_FILE"

if ! grep -q '_rebase_stacked_children()' "$FUNCS_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _rebase_stacked_children from $RSC_SRC" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# push_landed_despite_rejection() is normally sourced by rebase-stacked-children.sh
# itself from lib/push-lease-verify.sh (outside the extracted run()..main span
# above) — source the real library directly so the extracted functions can call it.
# shellcheck disable=SC1091
source "$HELPERS_DIR/lib/push-lease-verify.sh"

# Same deal for check_branch_name (#9106): _process_one_stacked_child validates
# each child's forge-supplied headRefName before the fetch, and the script's own
# `source lib/default-branch.sh` sits above the extracted span. The REAL library
# is sourced here — a stub would let the guard rot untested in the one suite
# that drives this function.
# shellcheck disable=SC1091
source "$HELPERS_DIR/lib/default-branch.sh"

# --- Stub gh on PATH ---
#   gh api repos/OWNER/REPO/issues/N   -> cat $STUB_DIR/issue-N.json (or {})
#   gh pr list --base B ...            -> cat $STUB_DIR/prlist-<sanitized B>.json (or [])
#   gh pr comment N ...                -> record to $STUB_DIR/gh-calls.log
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
LOG="$STUB_DIR_FROM_ENV/gh-calls.log"

if [[ "$1" == "api" ]]; then
  path="${!#}"
  num="${path##*/}"
  canned="$STUB_DIR_FROM_ENV/issue-$num.json"
  if [[ -f "$canned" ]]; then cat "$canned"; else echo '{}'; fi
  exit 0
fi

if [[ "$1" == "pr" && "$2" == "list" ]]; then
  base=""
  shift 2
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--base" ]]; then base="$2"; shift 2; continue; fi
    shift
  done
  safe="${base//\//_}"
  canned="$STUB_DIR_FROM_ENV/prlist-$safe.json"
  if [[ -f "$canned" ]]; then cat "$canned"; else echo '[]'; fi
  exit 0
fi

if [[ "$1" == "pr" && "$2" == "comment" ]]; then
  echo "$*" >> "$LOG"
  exit 0
fi

echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

# --- Stub git on PATH ---
#   git fetch ...                          -> record + exit 0
#   git merge-base --is-ancestor A <child> -> exit code from $STUB_DIR/ancestor-<safe child>
#                                             (file present -> its value; absent -> 1 = stale)
#   git rebase --abort                     -> record + exit 0
#   git rebase A B                         -> record + exit $LOOM_TEST_REBASE_EXIT (default 0)
#   git push ...                           -> record + exit $LOOM_TEST_PUSH_EXIT (default 0)
#   git rev-parse <ref>                    -> $STUB_DIR/rev-parse-sha (default "localsha123")
#   git ls-remote <remote> refs/heads/<b>  -> "<sha>\trefs/heads/<b>" from
#                                             $STUB_DIR/ls-remote-sha (absent -> nothing, i.e.
#                                             the "ref not found / could not verify" case)
#   (anything else)                        -> record + exit 0
cat > "$STUB_DIR/git" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub git: LOOM_TEST_STUB_DIR not set}"
LOG="$STUB_DIR_FROM_ENV/git-calls.log"
echo "git $*" >> "$LOG"
echo "git $*" >> "$STUB_DIR_FROM_ENV/all-calls.log"
case "$1" in
  fetch) exit 0 ;;
  merge-base)
    # merge-base --is-ancestor origin/<parent> origin/<child>; last arg = child ref.
    child="${!#}"
    safe="${child//\//_}"
    f="$STUB_DIR_FROM_ENV/ancestor-$safe"
    if [[ -f "$f" ]]; then exit "$(cat "$f")"; else exit 1; fi
    ;;
  rebase)
    if [[ "$2" == "--abort" ]]; then exit 0; fi
    exit "${LOOM_TEST_REBASE_EXIT:-0}"
    ;;
  push) exit "${LOOM_TEST_PUSH_EXIT:-0}" ;;
  rev-parse)
    f="$STUB_DIR_FROM_ENV/rev-parse-sha"
    if [[ -f "$f" ]]; then cat "$f"; else echo "localsha123"; fi
    exit 0
    ;;
  ls-remote)
    branch="${!#}"
    f="$STUB_DIR_FROM_ENV/ls-remote-sha"
    if [[ -f "$f" ]]; then printf '%s\t%s\n' "$(cat "$f")" "$branch"; fi
    exit 0
    ;;
  *) exit 0 ;;
esac
STUB
chmod +x "$STUB_DIR/git"

# --- Stub loom-daemon on PATH (#9487, #9282) ---
# Subcommands rebase-stacked-children.sh calls:
#   loom-daemon push-lease pin-flag --remote R --branch B --local-ref L
#   loom-daemon forge rebase-guard ... -> exit $LOOM_TEST_GUARD_EXIT
#     (default 1 = verified safe; 0 = blocked, with a token; else = no answer).
# git is a stub here, so the child's "remote head carries a hand-resolved
# merge" is the verb's ANSWER, not a git shape: the shapes themselves are
# covered against real git in loom-daemon/src/rebase_guard/tests.rs.
# Fixtures in $LOOM_TEST_STUB_DIR, keyed on the sanitized branch name:
#   pin-rc-<safe>   present -> refuse with that exit code (nothing on stdout).
#                              3 = origin unreadable, 4 = origin's head is not
#                              incorporated here (cli/push_lease.rs's codes).
#   pin-oid-<safe>  present -> pin to that oid instead of the default.
# Stubbing the subcommand (rather than building the real binary) keeps this
# suite hermetic — the real implementation's equivalence is covered by its own
# Rust unit tests and by test-reconcile-stack.sh, which runs the real binary.
cat > "$STUB_DIR/loom-daemon" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub loom-daemon: LOOM_TEST_STUB_DIR not set}"
echo "loom-daemon $*" >> "$STUB_DIR_FROM_ENV/daemon-calls.log"
echo "loom-daemon $*" >> "$STUB_DIR_FROM_ENV/all-calls.log"
if [[ "$1" == "forge" && "$2" == "rebase-guard" ]]; then
  rc="${LOOM_TEST_GUARD_EXIT:-1}"
  if [[ "$rc" == "0" ]]; then
    echo "RESOLUTION_DISCARDED 0123abc"
    echo "BLOCKER: rebase-guard (stub)" >&2
  fi
  exit "$rc"
fi
if [[ "$1" == "push-lease" && "$2" == "pin-flag" ]]; then
  branch=""
  shift 2
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--branch" ]]; then branch="$2"; shift 2; continue; fi
    shift
  done
  safe="${branch//\//_}"
  rc_file="$STUB_DIR_FROM_ENV/pin-rc-$safe"
  if [[ -f "$rc_file" ]]; then
    rc="$(cat "$rc_file")"
    if [[ "$rc" == "4" ]]; then
      echo "push-lease: origin/$branch has not incorporated into this checkout (#9487)" >&2
    else
      echo "push-lease: could not read origin's live head for '$branch' (#9487)" >&2
    fi
    exit "$rc"
  fi
  oid_file="$STUB_DIR_FROM_ENV/pin-oid-$safe"
  oid="sha-origin-head"
  [[ -f "$oid_file" ]] && oid="$(cat "$oid_file")"
  printf -- '--force-with-lease=%s:%s\n' "$branch" "$oid"
  exit 0
fi
echo "stub loom-daemon: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/loom-daemon"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

# --- Shared globals the functions read (see the file-level SC2034 disable). ---
REPO_NWO="owner/repo"
FORGE_TYPE="github"
DRY_RUN=false
RSC_FAILURE=0
# The real script resolves this above the extracted run()..main span (#9487).
DAEMON_BIN="$STUB_DIR/loom-daemon"
# Also resolved above the span (#10771). Pinned to `rebase` for the rebase
# cases: anything else, unset included, is merge and rewrites no child.
BRANCH_UPDATE_STRATEGY=rebase

# Canned issue fixtures (child issue label state).
cat > "$STUB_DIR/issue-201.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:issue"}]}
EOF
cat > "$STUB_DIR/issue-202.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"}]}
EOF

# Fixture writers.
write_prlist()   { printf '%s\n' "$2" > "$STUB_DIR/prlist-${1//\//_}.json"; }
clear_prlist()   { rm -f "$STUB_DIR/prlist-${1//\//_}.json"; }
mark_uptodate()  { echo 0 > "$STUB_DIR/ancestor-origin_${1//\//_}"; }  # parent tip IS ancestor
clear_uptodate() { rm -f "$STUB_DIR/ancestor-origin_${1//\//_}"; }     # default: stale (exit 1)
set_rev_parse_sha()  { printf '%s' "$1" > "$STUB_DIR/rev-parse-sha"; }
clear_rev_parse_sha() { rm -f "$STUB_DIR/rev-parse-sha"; }
set_ls_remote_sha()   { printf '%s' "$1" > "$STUB_DIR/ls-remote-sha"; }
clear_ls_remote_sha() { rm -f "$STUB_DIR/ls-remote-sha"; }
# #9487: `loom-daemon push-lease pin-flag` builds the pinned lease argument.
# refuse_pin <branch> [rc]  -> the subcommand refuses (3 unreadable, 4 not
#                              incorporated); the script must SKIP the child
#                              rather than fall back to the bare lease.
refuse_pin() { echo "${2:-3}" > "$STUB_DIR/pin-rc-${1//\//_}"; }
allow_pin()  { rm -f "$STUB_DIR/pin-rc-${1//\//_}"; }

# The live-remote head the stub pins to unless a scenario overrides it.
DEFAULT_ORIGIN_HEAD="sha-origin-head"

reset_state() {
    : > "$STUB_DIR/gh-calls.log"
    : > "$STUB_DIR/git-calls.log"
    : > "$STUB_DIR/daemon-calls.log"
    : > "$STUB_DIR/all-calls.log"
    DRY_RUN=false
    RSC_FAILURE=0
    unset LOOM_TEST_REBASE_EXIT
    unset LOOM_TEST_GUARD_EXIT
    unset LOOM_TEST_PUSH_EXIT
    clear_rev_parse_sha
    set_ls_remote_sha "$DEFAULT_ORIGIN_HEAD"
    # #9487 default: the pinned lease builds successfully for every child.
    allow_pin "feature/issue-201"
    allow_pin "feature/issue-202"
}
read_gh()  { cat "$STUB_DIR/gh-calls.log" 2>/dev/null || true; }
read_git() { cat "$STUB_DIR/git-calls.log" 2>/dev/null || true; }
read_daemon() { cat "$STUB_DIR/daemon-calls.log" 2>/dev/null || true; }
# Every stubbed git AND loom-daemon call, in call order — the ordering log the
# #9487 "pin before the rebase" assertion reads.
read_all() { cat "$STUB_DIR/all-calls.log" 2>/dev/null || true; }

echo "Testing _rebase_stacked_children behavior..."

# (a) No open children -> no-op (no rebase, no comment).
reset_state
clear_prlist "feature/issue-100"     # stub returns [] with no fixture
_rebase_stacked_children "feature/issue-100"
assert_not_contains "$(read_git)" "rebase" "(a) No open children -> no rebase attempted"
assert_eq "" "$(read_gh)" "(a) No open children -> no comment posted"

# (b) One child already up to date (parent tip is an ancestor) -> no-op.
reset_state
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
mark_uptodate "feature/issue-201"
_rebase_stacked_children "feature/issue-100"
assert_not_contains "$(read_git)" "rebase" "(b) Up-to-date child -> no rebase attempted"
assert_eq "" "$(read_gh)" "(b) Up-to-date child -> no comment posted"
clear_uptodate "feature/issue-201"

# (c) One stale, safe child (issue 201 not loom:building) -> rebase + push,
#     no PR base retarget (no `gh pr edit`), no deferred comment.
reset_state
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_uptodate "feature/issue-201"   # stale
_rebase_stacked_children "feature/issue-100"
assert_contains "$(read_git)" "git rebase -- origin/feature/issue-100 feature/issue-201" \
  "(c) Safe stale child -> rebased onto origin/feature/issue-100"
assert_contains "$(read_git)" \
  "git push --force-with-lease=feature/issue-201:$DEFAULT_ORIGIN_HEAD origin feature/issue-201" \
  "(c) Safe stale child -> pushed with the lease PINNED to origin's live head (#9487)"
assert_not_contains "$(read_git)" "git push --force-with-lease origin" \
  "(c) Safe stale child -> never the bare --force-with-lease (#9487)"
assert_not_contains "$(read_gh)" "pr edit" "(c) Safe stale child -> PR base NOT retargeted"
assert_eq "" "$(read_gh)" "(c) Safe stale child -> no deferred comment"
assert_eq "0" "$RSC_FAILURE" "(c) Safe stale child -> RSC_FAILURE stays 0"
assert_contains "$(read_daemon)" \
  "loom-daemon forge rebase-guard --base origin/feature/issue-100 --old origin/feature/issue-201 --new feature/issue-201" \
  "(c) Safe stale child -> rebase-guard asked about the head the push replaces (#9282)"

# #9487 ordering: the pin must be built BEFORE the rebase rewrites the branch.
# A pin taken after (or just before the push) would read whatever a sibling had
# already published and launder the clobber as "fresh".
ALL_LOG_C="$(read_all)"
# Pure-bash scan for the two call positions: no `grep | head` pipeline, which
# under `set -o pipefail` can report SIGPIPE from the producer (#7060 class).
PIN_LINE=""
REBASE_LINE=""
LOG_LINE_NO=0
while IFS= read -r log_line; do
    LOG_LINE_NO=$((LOG_LINE_NO + 1))
    if [[ -z "$PIN_LINE" && "$log_line" == *"push-lease pin-flag"*"--branch feature/issue-201"* ]]; then
        PIN_LINE="$LOG_LINE_NO"
    fi
    if [[ -z "$REBASE_LINE" && "$log_line" == *"git rebase -- origin/feature/issue-100"* ]]; then
        REBASE_LINE="$LOG_LINE_NO"
    fi
done <<< "$ALL_LOG_C"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -n "$PIN_LINE" && -n "$REBASE_LINE" && "$PIN_LINE" -lt "$REBASE_LINE" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: (c) the lease pin is built BEFORE the rebase (#9487)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: (c) the lease pin is built BEFORE the rebase (#9487)"
    echo "    push-lease at line '$PIN_LINE', rebase at line '$REBASE_LINE' in:"
    printf '%s\n' "$ALL_LOG_C"
fi
assert_contains "$ALL_LOG_C" \
  "loom-daemon push-lease pin-flag --remote origin --branch feature/issue-201 --local-ref refs/heads/feature/issue-201" \
  "(c) the pin is built by the loom-daemon subcommand, with the local-ref ancestry check wired (#9487)"

# (c-merge) #10771: same stale, safe child, but this repo's PR branches are
#     not rebased (loom-daemon branch-update strategy = merge). The child is
#     reported and left alone: no pin, no rebase, no push, no comment, and not a
#     failure — rewriting it would delete other agents' merge commits.
for CM_MODE in merge unset; do
reset_state
if [[ "$CM_MODE" == unset ]]; then unset BRANCH_UPDATE_STRATEGY; else BRANCH_UPDATE_STRATEGY=merge; fi
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_uptodate "feature/issue-201"   # stale
OUT_CM_FILE="$(mktemp)"
_rebase_stacked_children "feature/issue-100" >"$OUT_CM_FILE" 2>&1
OUT_CM="$(cat "$OUT_CM_FILE")"
rm -f "$OUT_CM_FILE"
assert_not_contains "$(read_git)" "rebase" "(c-merge/$CM_MODE) merge-only repo -> no rebase attempted (#10771)"
assert_not_contains "$(read_git)" "push" "(c-merge/$CM_MODE) merge-only repo -> nothing pushed, let alone forced (#10771)"
assert_not_contains "$(read_all)" "push-lease pin-flag" "(c-merge/$CM_MODE) merge-only repo -> no lease is even built"
assert_eq "" "$(read_gh)" "(c-merge/$CM_MODE) merge-only repo -> no comment posted"
assert_eq "0" "$RSC_FAILURE" "(c-merge/$CM_MODE) merge-only repo -> a policy skip is not a failure"
assert_contains "$OUT_CM" "git merge origin/feature/issue-100" "(c-merge/$CM_MODE) the skip names the merge-based remedy"
done
BRANCH_UPDATE_STRATEGY=rebase

# (c2) Same as (c), but version-check-gate.sh (real script, #7168) reports a
#      mismatch via LOOM_VERSION_CHECK_SCRIPT -> rebase runs, but the push is
#      NEVER attempted and RSC_FAILURE=2 (mirrors the rebase-conflict path).
reset_state
cat > "$STUB_DIR/version-mismatch.sh" <<'STUB'
#!/usr/bin/env bash
echo "MISMATCH  .loom/install-metadata.json: 0.18.130 (expected 0.18.131)"
exit 1
STUB
chmod +x "$STUB_DIR/version-mismatch.sh"
export LOOM_VERSION_CHECK_SCRIPT="$STUB_DIR/version-mismatch.sh"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_uptodate "feature/issue-201"   # stale
OUT_C2_FILE="$(mktemp)"
_rebase_stacked_children "feature/issue-100" >"$OUT_C2_FILE" 2>&1
OUT_C2="$(cat "$OUT_C2_FILE")"
rm -f "$OUT_C2_FILE"
assert_contains "$(read_git)" "git rebase -- origin/feature/issue-100 feature/issue-201" \
  "(c2) Version mismatch after rebase -> rebase still ran"
assert_not_contains "$(read_git)" "git push --force-with-lease" \
  "(c2) Version mismatch after rebase -> push NEVER attempted"
assert_eq "2" "$RSC_FAILURE" "(c2) Version mismatch after rebase -> RSC_FAILURE=2"
assert_contains "$OUT_C2" "MISMATCH" "(c2) Underlying MISMATCH line is surfaced"
assert_contains "$OUT_C2" "out of sync" "(c2) Failure is reported against the child branch"
unset LOOM_VERSION_CHECK_SCRIPT

# (c3) --dry-run with a stale safe child AND a mismatching version-check-gate
#      stub -> the gate is skipped entirely (no rebase actually ran, so there
#      is nothing real to check yet) — dry-run behavior is unaffected by #7168.
reset_state
DRY_RUN=true
export LOOM_VERSION_CHECK_SCRIPT="$STUB_DIR/version-mismatch.sh"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_uptodate "feature/issue-201"   # stale
_rebase_stacked_children "feature/issue-100"
assert_eq "0" "$RSC_FAILURE" "(c3) Dry-run -> version-check-gate skipped, RSC_FAILURE stays 0"
unset LOOM_VERSION_CHECK_SCRIPT
assert_not_contains "$(read_daemon)" "rebase-guard" "(c3) Dry-run -> rebase-guard not asked either (#9282)"
DRY_RUN=false

# (c4) #9282: the child's remote head carries a hand-resolved merge the rebase
#      flattened -> rebase-guard answers BLOCKED (rc 0): the rebase ran, the
#      push is NEVER attempted, and this child fails with RSC_FAILURE=2.
reset_state
export LOOM_TEST_GUARD_EXIT=0
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_uptodate "feature/issue-201"   # stale
OUT_C4_FILE="$(mktemp)"
_rebase_stacked_children "feature/issue-100" >"$OUT_C4_FILE" 2>&1
OUT_C4="$(cat "$OUT_C4_FILE")"
rm -f "$OUT_C4_FILE"
assert_contains "$(read_git)" "git rebase -- origin/feature/issue-100 feature/issue-201" \
  "(c4) Discarded resolution -> rebase still ran"
assert_not_contains "$(read_git)" "git push" \
  "(c4) Discarded resolution -> push NEVER attempted"
assert_eq "2" "$RSC_FAILURE" "(c4) Discarded resolution -> RSC_FAILURE=2"
assert_contains "$OUT_C4" "RESOLUTION_DISCARDED" "(c4) The verb's finding is surfaced"
assert_contains "$OUT_C4" "Not pushing 'feature/issue-201'" \
  "(c4) Failure is reported against the child branch"

# (c5) #9282: rebase-guard gives NO answer (a loom-daemon predating the verb
#      exits 2) -> a loud warning, and the push still happens: a script newer
#      than the installed binary must not strand every stacked rebase.
reset_state
export LOOM_TEST_GUARD_EXIT=2
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_uptodate "feature/issue-201"   # stale
OUT_C5_FILE="$(mktemp)"
_rebase_stacked_children "feature/issue-100" >"$OUT_C5_FILE" 2>&1
OUT_C5="$(cat "$OUT_C5_FILE")"
rm -f "$OUT_C5_FILE"
assert_contains "$OUT_C5" "rebase-guard gave no answer (rc=2) for 'feature/issue-201'" \
  "(c5) Unanswered guard -> warned, naming the exit code and the child"
assert_contains "$OUT_C5" "UNCHECKED" "(c5) Unanswered guard -> the warning says the push is unchecked"
assert_contains "$(read_git)" "git push --force-with-lease" \
  "(c5) Unanswered guard -> push still attempted (non-blocking)"
assert_eq "0" "$RSC_FAILURE" "(c5) Unanswered guard -> RSC_FAILURE stays 0"
unset LOOM_TEST_GUARD_EXIT

# (d) One stale, unsafe child (issue 202 loom:building) -> deferred comment, no rebase.
reset_state
write_prlist "feature/issue-100" '[{"number":502,"headRefName":"feature/issue-202"}]'
clear_uptodate "feature/issue-202"   # stale
_rebase_stacked_children "feature/issue-100"
assert_not_contains "$(read_git)" "rebase" "(d) Unsafe stale child -> rebase NOT attempted"
assert_contains "$(read_gh)" "pr comment 502 --repo owner/repo" \
  "(d) Unsafe stale child -> deferred-rebase comment posted on PR #502"

# (e) Non-feature/issue-N parent branch -> script skips entirely (no discovery).
reset_state
write_prlist "release-1" '[{"number":503,"headRefName":"feature/issue-201"}]'
_rebase_stacked_children "release-1"
assert_not_contains "$(read_git)" "rebase" "(e) Non-feature/issue-N parent -> no rebase"
assert_eq "" "$(read_gh)" "(e) Non-feature/issue-N parent -> no comment, no discovery"

# (e2) FORGE_TYPE != github -> no-op (GitHub-only).
reset_state
FORGE_TYPE="gitea"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
_rebase_stacked_children "feature/issue-100"
assert_not_contains "$(read_git)" "rebase" "(e2) FORGE_TYPE=gitea -> no rebase (GitHub-only)"
assert_eq "" "$(read_gh)" "(e2) FORGE_TYPE=gitea -> no comment"
FORGE_TYPE="github"

# (f) --dry-run with a stale safe child -> reports would-be rebase without executing.
reset_state
DRY_RUN=true
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_uptodate "feature/issue-201"   # stale
_rebase_stacked_children "feature/issue-100"
assert_not_contains "$(read_git)" "git rebase -- origin/feature/issue-100 feature/issue-201" \
  "(f) Dry-run stale safe child -> rebase NOT executed"
assert_not_contains "$(read_git)" "git push --force-with-lease" \
  "(f) Dry-run stale safe child -> push NOT executed"
assert_eq "" "$(read_gh)" "(f) Dry-run stale safe child -> no comment posted"
DRY_RUN=false

# (g bonus) Rebase conflict on the safe path -> RSC_FAILURE=2, abort recorded,
#           run continues (does not tear down the process).
reset_state
export LOOM_TEST_REBASE_EXIT=1
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_uptodate "feature/issue-201"   # stale
_rebase_stacked_children "feature/issue-100"
assert_eq "2" "$RSC_FAILURE" "(g) Rebase conflict -> RSC_FAILURE=2 (exit 2 at end)"
assert_contains "$(read_git)" "git rebase --abort" \
  "(g) Rebase conflict -> conflicted rebase aborted so remaining children process"
unset LOOM_TEST_REBASE_EXIT

# (h) push --force-with-lease reports a rejection, but the live remote ref
#     (git ls-remote) already matches the pushed local sha -> treated as a
#     landed push despite the reported rejection (#6695), logged with the
#     greppable PUSH-LEASE-RACE-DETECTED marker, RSC_FAILURE stays 0.
reset_state
export LOOM_TEST_PUSH_EXIT=1
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_uptodate "feature/issue-201"   # stale
set_rev_parse_sha "sha-landed"
set_ls_remote_sha "sha-landed"
OUT_H_FILE="$(mktemp)"
_rebase_stacked_children "feature/issue-100" >"$OUT_H_FILE" 2>&1
OUT_H="$(cat "$OUT_H_FILE")"
rm -f "$OUT_H_FILE"
assert_eq "0" "$RSC_FAILURE" \
  "(h) Push reports rejection but ref landed -> RSC_FAILURE stays 0"
assert_contains "$OUT_H" "PUSH-LEASE-RACE-DETECTED" \
  "(h) Landed-despite-rejection push logs the greppable PUSH-LEASE-RACE-DETECTED marker"
assert_contains "$OUT_H" "Rebased child PR #501" \
  "(h) Processing continues past the false rejection (still reports success)"
unset LOOM_TEST_PUSH_EXIT

# (i) push --force-with-lease reports a rejection AND the live remote ref does
#     NOT match the pushed local sha -> a genuine rejection, reported as an
#     ordinary failure (never mislabeled as the race condition).
reset_state
export LOOM_TEST_PUSH_EXIT=1
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_uptodate "feature/issue-201"   # stale
set_rev_parse_sha "sha-local"
set_ls_remote_sha "sha-someone-else-pushed"
OUT_I_FILE="$(mktemp)"
_rebase_stacked_children "feature/issue-100" >"$OUT_I_FILE" 2>&1
OUT_I="$(cat "$OUT_I_FILE")"
rm -f "$OUT_I_FILE"
assert_eq "2" "$RSC_FAILURE" "(i) Genuine rejection -> RSC_FAILURE=2"
assert_contains "$OUT_I" "force-with-lease push rejected for 'feature/issue-201'" \
  "(i) Genuine rejection is reported as an ordinary failure"
assert_not_contains "$OUT_I" "PUSH-LEASE-RACE-DETECTED" \
  "(i) A real rejection is never mislabeled as the race condition"
unset LOOM_TEST_PUSH_EXIT

# (j) #9487: origin's live head cannot be read -> the child is SKIPPED rather
#     than pushed with an unpinned (bare) lease. Nothing is rebased or pushed.
reset_state
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_uptodate "feature/issue-201"   # stale
refuse_pin "feature/issue-201" 3     # loom-daemon: origin could not be queried
OUT_J_FILE="$(mktemp)"
_rebase_stacked_children "feature/issue-100" >"$OUT_J_FILE" 2>&1
OUT_J="$(cat "$OUT_J_FILE")"
rm -f "$OUT_J_FILE"
assert_not_contains "$(read_git)" "git rebase -- origin/feature/issue-100" \
  "(j) No readable live head -> rebase NOT attempted (#9487)"
assert_not_contains "$(read_git)" "git push" \
  "(j) No readable live head -> push NOT attempted with an unpinned lease (#9487)"
assert_eq "2" "$RSC_FAILURE" "(j) No readable live head -> RSC_FAILURE=2"
assert_contains "$OUT_J" "#9487" "(j) Refusal cites the pinned-lease requirement"
assert_contains "$OUT_J" "push-lease pin-flag exit 3" \
  "(j) Refusal names the subcommand and its exit code"

# (k) #9487: origin's head is NOT an ancestor of this clone's local child
#     branch -> someone published commits this clone never incorporated. The
#     pin would be accurate and the push would still delete them, so refuse.
reset_state
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_uptodate "feature/issue-201"   # stale
refuse_pin "feature/issue-201" 4     # loom-daemon: origin's head is not incorporated here
OUT_K_FILE="$(mktemp)"
_rebase_stacked_children "feature/issue-100" >"$OUT_K_FILE" 2>&1
OUT_K="$(cat "$OUT_K_FILE")"
rm -f "$OUT_K_FILE"
assert_not_contains "$(read_git)" "git rebase -- origin/feature/issue-100" \
  "(k) Unincorporated origin head -> rebase NOT attempted (#9487)"
assert_not_contains "$(read_git)" "git push" \
  "(k) Unincorporated origin head -> push NOT attempted (#9487)"
assert_eq "2" "$RSC_FAILURE" "(k) Unincorporated origin head -> RSC_FAILURE=2"
assert_contains "$OUT_K" "has not incorporated" \
  "(k) Refusal names the unincorporated remote head"
allow_pin "feature/issue-201"

# --- Source-contains guards (fail if a refactor drops the key behavior) ---
echo ""
echo "Testing rebase-stacked-children.sh source guards..."
src="$(cat "$RSC_SRC")"
assert_contains "$src" 'gh pr list --repo "$REPO_NWO" --base "$parent_branch" --state open' \
  "script discovers children via a live forge query, not the daemon registry"
assert_contains "$src" 'git merge-base --is-ancestor "origin/$parent_branch" "origin/$child_branch"' \
  "script determines staleness via git merge-base --is-ancestor"
assert_contains "$src" "grep -qx 'loom:building'" \
  "script gates safe/unsafe on the child issue's loom:building label"
# `--` before the two ref operands (#9106): without it, a forge-supplied
# headRefName beginning with `-` is parsed by `git rebase` as a switch.
assert_contains "$src" 'run git rebase -- "origin/$parent_branch" "$child_branch"' \
  "safe path rebases the child onto the parent tip, with the -- separator"
assert_contains "$src" 'run git push "$child_lease_arg" origin "$child_branch"' \
  "safe path publishes with the PINNED --force-with-lease argument (never bare --force, never the bare lease — #9487)"
assert_not_contains "$src" 'git push --force-with-lease;' \
  "script never issues a BARE --force-with-lease (#9487)"
assert_contains "$src" '"$DAEMON_BIN" push-lease pin-flag --remote origin --branch "$child_branch" --local-ref "refs/heads/$child_branch"' \
  "the pinned lease argument comes from loom-daemon push-lease pin-flag, not from shell logic (#9487)"
assert_contains "$src" 'loom_daemon_version_preflight push-lease "$DAEMON_BIN"' \
  "a loom-daemon too old for push-lease is refused with the floor, never degraded to a bare lease (#9487)"
assert_not_contains "$src" "gh pr edit" \
  "script never retargets the child PR base (stays stacked on the parent)"
assert_contains "$src" "push_landed_despite_rejection" \
  "script verifies the actual remote ref state after a rejected --force-with-lease push (#6695)"
assert_contains "$src" "PUSH-LEASE-RACE-DETECTED" \
  "script logs a greppable marker when a reported rejection is actually landed"
assert_contains "$src" 'source "$SCRIPT_DIR/lib/push-lease-verify.sh"' \
  "script sources the shared push-lease-verify helper"
assert_contains "$src" 'BRANCH_UPDATE_STRATEGY="$("$DAEMON_BIN" branch-update strategy 2>/dev/null)" || BRANCH_UPDATE_STRATEGY=merge' \
  "script asks loom-daemon branch-update strategy, failing toward merge (#10771)"
assert_contains "$src" 'if [[ "${BRANCH_UPDATE_STRATEGY:-}" != "rebase" ]]; then' \
  "any answer but an explicit rebase is merge (#10771)"
assert_contains "$src" '"$SCRIPT_DIR/version-check-gate.sh"' \
  "safe path runs the shared version-check-gate.sh after rebase, before push (#7168)"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
