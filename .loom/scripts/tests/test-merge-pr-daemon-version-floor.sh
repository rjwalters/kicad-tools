#!/usr/bin/env bash
# test-merge-pr-daemon-version-floor.sh — merge-pr.sh's refusal on a daemon
# that predates a required subcommand must name the REMEDIATION, not just the
# cause (#8285).
#
# THE INCIDENT. On 2026-09-18 an operator host ran loom-daemon 0.19.161 while
# `main` carried 0.19.170+. `.loom/scripts` is a symlink into
# `defaults/scripts` in the primary checkout, so pulling `main` moved
# merge-pr.sh's daemon floor instantly, and the auto-update loop was deferring
# ~4.5h behind the build-stampede guard (#8252). Every merge on that host
# stopped, and all the refusal said was "A loom-daemon predating #8191 has no
# such subcommand -- update it": no version to roll TO, and no command to roll
# WITH. Same shape fleet-wide, one host at a time, on every future PR that adds
# a daemon dependency.
#
# WHAT IS PINNED HERE. Not the prose — the four facts an operator needs to act:
#   1. the minimum version, read from merge-pr.sh's own `# requires-daemon:`
#      marker rather than hardcoded in the message (one source of truth, which
#      is also the string scripts/check-daemon-subcommand-versions.sh enforces);
#   2. the artifact-first roll command for THIS host
#      (`cli/loom-daemon-update.sh --fetch`);
#   3. the LOOM_DAEMON_BIN pin as the fallback when no artifact carries the
#      floor yet;
#   4. what the resolved binary actually reports, so "is this even my problem?"
#      is answerable without a second command.
#
# Plus, since #8967, a fifth: that the ONE `merge-pr` floor actually covers
# every fail-closed `merge-pr <verb>` the script calls, not just the first one
# that happened to get a marker. See the fail-direction table below.
#
# Strategy mirrors test-merge-pr-partial-increment.sh: extract the functions
# under test (plus the marker comments they read) from merge-pr.sh and source
# them, with a stub `error`. NO real loom-daemon is needed or wanted — the whole
# point is the behaviour against a binary that does NOT have the subcommand, so
# the fixtures are fake binaries.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-daemon-version-floor.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$HELPERS_DIR/../.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"
CHECKER="$REPO_ROOT/scripts/check-daemon-subcommand-versions.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$needle" <<<"$haystack"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    expected substring: '$needle'"
        echo "    in: $haystack"
    fi
}

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    expected: '$expected'"
        echo "    actual:   '$actual'"
    fi
}

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# --- Extract the marker comments + the two functions under test -------------
# The markers MUST travel with the functions: _mp_daemon_roll_hint reads them
# back out of ${BASH_SOURCE[0]}, which here is this extracted file. That is the
# same mechanism that makes the message and the CI gate share one string, so
# extracting them together is testing the real thing, not a copy.
FUNCS_FILE="$WORKDIR/funcs.sh"
awk '
  /^# requires-daemon:/                 { print; next }
  /^_mp_daemon_roll_hint\(\) \{/        { print; next }
  /^_mp_refs\(\) \{/                    { capture = 1 }
  capture                               { print }
  /^\}$/                                { capture = 0 }
' "$MERGE_PR_SRC" >"$FUNCS_FILE"

for _fn in _mp_daemon_roll_hint _mp_refs; do
    if ! grep -q "^${_fn}() {" "$FUNCS_FILE"; then
        echo -e "${RED}FATAL${NC}: could not extract $_fn from $MERGE_PR_SRC" >&2
        exit 2
    fi
done
if ! grep -q '^# requires-daemon: merge-pr-refs >= ' "$FUNCS_FILE"; then
    echo -e "${RED}FATAL${NC}: merge-pr.sh declares no 'requires-daemon: merge-pr-refs' floor" >&2
    exit 2
fi

# `error` in merge-pr.sh prints and EXITS. The stub must exit too, not just
# return: every _mp_refs refusal below is captured in a command substitution
# (its own subshell), so `exit 1` makes the refusal both readable and
# non-zero — while a bare `return 1` would let the function run on past the
# refusal, which is precisely the fail-OPEN behaviour these assertions exist
# to rule out.
# shellcheck disable=SC2317  # invoked indirectly, from the sourced merge-pr.sh functions
error() { printf '%s\n' "$*"; exit 1; }
SCRIPT_DIR_SAVED="$SCRIPT_DIR"
# The hint interpolates $SCRIPT_DIR to name the host-local update script, so
# point it at the real scripts dir the way merge-pr.sh does.
SCRIPT_DIR="$HELPERS_DIR"
# shellcheck disable=SC1090
source "$FUNCS_FILE"
SCRIPT_DIR="$SCRIPT_DIR_SAVED"

# `sed … ;q` rather than `sed … | head -1`: no pipe, so the pipefail +
# early-exit-consumer SIGPIPE class (#7790) cannot apply. Same idiom the hint
# itself uses.
DECLARED_REFS_MIN="$(sed -n '/^# requires-daemon: merge-pr-refs >= /{s/^# requires-daemon: merge-pr-refs >= \([0-9][0-9.]*\).*/\1/p;q;}' "$FUNCS_FILE")"
DECLARED_MERGEPR_MIN="$(sed -n '/^# requires-daemon: merge-pr >= /{s/^# requires-daemon: merge-pr >= \([0-9][0-9.]*\).*/\1/p;q;}' "$FUNCS_FILE")"

# --- Fixture binaries -------------------------------------------------------
# STALE: knows --version, does not know the subcommand (exits 2, exactly as
# clap does for an unrecognized subcommand).
cat >"$WORKDIR/stale-loom-daemon" <<'FAKE'
#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then
  echo "loom-daemon 0.19.161 (commit deadbee, built 2026-09-10T00:00:00Z)"
  exit 0
fi
echo "error: unrecognized subcommand '${1:-}'" >&2
exit 2
FAKE
chmod +x "$WORKDIR/stale-loom-daemon"

echo ""
echo "Testing _mp_daemon_roll_hint (the remediation text)…"

HINT_STALE="$(SCRIPT_DIR="$HELPERS_DIR" _mp_daemon_roll_hint merge-pr-refs "$WORKDIR/stale-loom-daemon")"

assert_contains "$HINT_STALE" "loom-daemon >= $DECLARED_REFS_MIN" \
  "names the MINIMUM version, read from merge-pr.sh's own requires-daemon marker (AC #1)"
assert_contains "$HINT_STALE" "cli/loom-daemon-update.sh --fetch" \
  "names the concrete artifact-first roll command for this host (AC #1)"
assert_contains "$HINT_STALE" "LOOM_DAEMON_BIN" \
  "names the LOOM_DAEMON_BIN pin as the fallback (AC #1)"
assert_contains "$HINT_STALE" "0.19.161" \
  "reports what the RESOLVED binary actually is, so the operator can tell whether it is their problem"
assert_contains "$HINT_STALE" "cargo build --release -p loom-daemon" \
  "names the source-build escape hatch for when no artifact carries the floor yet"
assert_contains "$HINT_STALE" "merge-pr-refs --help" \
  "gives a one-line way to confirm the roll worked before re-running the merge"

HINT_MERGEPR="$(SCRIPT_DIR="$HELPERS_DIR" _mp_daemon_roll_hint merge-pr)"
assert_contains "$HINT_MERGEPR" "loom-daemon >= $DECLARED_MERGEPR_MIN" \
  "the SECOND daemon dependency (merge-pr verdict-contradiction) has its own declared floor"

# A hint for a subcommand with no marker must not invent a version.
HINT_UNDECLARED="$(SCRIPT_DIR="$HELPERS_DIR" _mp_daemon_roll_hint no-such-subcommand)"
assert_contains "$HINT_UNDECLARED" "<undeclared>" \
  "an undeclared subcommand says so rather than quoting a wrong version"

# merge-pr.sh runs under `set -euo pipefail`; this suite does not (it needs to
# survive its own subjects failing). So the hint is re-run here under the
# PRODUCTION shell options for each of the three binary shapes it has to cope
# with — absent, pinned-but-missing, present-but-stale — and required to
# produce complete output, not just to not crash. The hazard is specific and
# quiet: a bare `x="$(cmd)"` assignment adopts cmd's status under `set -e`, so
# a failing read inside the hint would abort it partway and the caller would
# emit a refusal with the remediation truncated off the end — a message that
# still LOOKS right in a log while missing the only part an operator needs.
for _shape in "no-bin:" "missing-bin:$WORKDIR/does-not-exist" "stale-bin:$WORKDIR/stale-loom-daemon"; do
    _label="${_shape%%:*}"
    _arg="${_shape#*:}"
    _out="$(
        set -euo pipefail
        SCRIPT_DIR="$HELPERS_DIR"
        # shellcheck disable=SC1090
        source "$FUNCS_FILE"
        printf '%s' "$(_mp_daemon_roll_hint merge-pr-refs "$_arg")"
    )"; _rc=$?
    assert_eq "0" "$_rc" "hint survives set -euo pipefail ($_label)"
    assert_contains "$_out" "cli/loom-daemon-update.sh --fetch" \
      "hint still produces the remediation under set -euo pipefail ($_label)"
done

echo ""
echo "Testing _mp_refs refusal against a stale daemon…"

OUT="$(LOOM_DAEMON_SELF_BIN="$WORKDIR/stale-loom-daemon" _mp_refs closing-refs 2>&1)"; RC=$?
assert_eq "1" "$RC" "_mp_refs still FAILS CLOSED on a stale daemon (an empty answer is never accepted)"
assert_contains "$OUT" "loom-daemon >= $DECLARED_REFS_MIN" \
  "the refusal names the minimum version (was: only 'predating #8191')"
assert_contains "$OUT" "cli/loom-daemon-update.sh --fetch" \
  "the refusal names the remediation command (was: only 'update it')"
assert_contains "$OUT" "Refusing rather than treating an empty result as 'no references'" \
  "the original fail-closed rationale is preserved, not replaced"

echo ""
echo "Testing _mp_refs refusal when no daemon resolves at all…"

# A PATH with the ordinary tools (the hint shells out to sed/head) but no
# loom-daemon anywhere on it, and a HOME with no ~/.local/bin fallback either.
mkdir -p "$WORKDIR/no-daemon-bin" "$WORKDIR/no-home"
OUT_NONE="$(PATH="$WORKDIR/no-daemon-bin:/usr/bin:/bin" HOME="$WORKDIR/no-home" LOOM_DAEMON_SELF_BIN="" LOOM_DAEMON_BIN="" _mp_refs closing-refs 2>&1)"; RC_NONE=$?
assert_eq "1" "$RC_NONE" "_mp_refs refuses when no loom-daemon can be resolved"
assert_contains "$OUT_NONE" "cli/loom-daemon-update.sh --fetch" \
  "the could-not-resolve refusal also names the remediation"

echo ""
echo "Testing the declared floors are sane…"

REPO_VERSION="$(tr -d '[:space:]' <"$REPO_ROOT/VERSION" 2>/dev/null || echo "")"
for _min in "$DECLARED_REFS_MIN" "$DECLARED_MERGEPR_MIN"; do
    TESTS_RUN=$((TESTS_RUN + 1))
    # `sort -V` over a here-string, then first line by parameter expansion: no
    # pipe, so a pipefail + early-exit-consumer SIGPIPE (#7790) is impossible.
    _sorted="$(sort -V <<<"$_min"$'\n'"$REPO_VERSION")"
    if [[ "$_min" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
       && { [[ -z "$REPO_VERSION" ]] || [[ "${_sorted%%$'\n'*}" == "$_min" ]]; }; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: declared floor '$_min' is semver and <= this repo's VERSION ($REPO_VERSION)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: declared floor '$_min' is not semver, or is above VERSION ($REPO_VERSION)"
    fi
done

echo ""
echo "Testing the merge-pr floor covers every fail-closed sub-subcommand (#8967)…"

# ONE marker covers the whole `merge-pr` family: check-daemon-subcommand-
# versions.sh keys floors on the TOP-LEVEL subcommand, while merge-pr.sh calls
# many different `merge-pr <verb>` sub-subcommands, each added in a different
# release. #8967: the marker sat at 0.19.172 — verdict-contradiction, the
# OLDEST of them — while `loom-pr-guard`, which REFUSES the merge when the
# daemon cannot answer, had needed 0.19.375 since 2026-09-25. A host on
# 0.19.300 therefore PASSED the declared floor and then had every merge
# refused, with _mp_daemon_roll_hint telling the operator to roll to a floor
# the host already satisfied — the exact failure the marker was introduced
# (#8285/#8386) to prevent.
#
# This table is the forcing function the single marker cannot be. Every
# `merge-pr <verb>` INVOCATION in merge-pr.sh must appear here with its fail
# direction:
#
#   closed <version>  the merge is REFUSED when the daemon cannot answer this
#                     verb. <version> is the first VERSION carrying it — the
#                     merge commit's own VERSION plus one, since the post-merge
#                     bump follows the merge (`git show <merge-commit>:VERSION`).
#   open   -          a missing/old verb degrades: it warns, retries, or falls
#                     back to the pre-verb behaviour. Never raises the floor.
#
# A verb missing from this table FAILS this suite, so a new one cannot land
# without being classified; and a `closed` row above the declared marker fails
# the floor assertion below. Together that is the AC: adding a fail-closed
# `merge-pr <x>` call without moving the floor cannot pass CI.
#
# `open` is about the MERGE, not every side effect: dirty-guard refuses the
# post-merge `git worktree remove --force` when it cannot answer (#5031), but
# the merge itself has already happened, so it is `open` here. The rows from
# stacked-children down were added when #8967 was rebased over the #8191
# slices that landed after it branched; each `closed` version is
# `git show <mainline commit that first carried it>:VERSION` + 1:
#   classify-response  97609b86f (#9228, squash)          0.19.455 -> 0.19.456
#   partial-conflict   2ab7630c5 (merge of #9246)          0.19.463 -> 0.19.464
#   checks-failure     397f06feb (merge of #9272)          0.19.464 -> 0.19.465
MERGE_PR_VERB_TABLE="verdict-contradiction closed 0.19.172
stale-checks closed 0.19.221
loom-pr-guard closed 0.19.375
classify-response closed 0.19.456
partial-conflict closed 0.19.464
checks-failure closed 0.19.465
head-sync-retry open -
hold-state open -
redate-checks open -
delete-branch open -
zero-checks-settle open -
check-runs-streak open -
check-runs-rollup open -
stacked-children open -
version-policy open -
partial-reset open -
partial-comment open -
closed-building open -
issue-close-gate open -
dirty-guard open -
worktree-contains open -
worktree-preserve open -
cleanup-paths open -"

# Shared comparison, so the controls below exercise the SAME logic the real
# assertion does rather than a paraphrase of it. `sort -V` over a here-string
# plus parameter expansion for the last line: no pipe, so the pipefail +
# early-exit-consumer SIGPIPE class (#7790) cannot apply, same as above.
_floor_covers() { # <declared> <needed> -> 0 when declared >= needed
    local declared="$1" needed="$2" sorted
    [[ -n "$needed" ]] || return 0
    sorted="$(sort -V <<<"$declared"$'\n'"$needed")"
    [[ "${sorted##*$'\n'}" == "$declared" ]]
}

_verb_row() { # <verb> -> its table row, or empty when unclassified
    awk -v v="$1" '$1 == v { print; exit }' <<<"$MERGE_PR_VERB_TABLE"
}

# Controls for the comparison itself: a gate that silently stops discriminating
# reports OK forever while the next stale floor lands underneath it.
for _ctl in "1:0.19.375:0.19.375" "1:0.19.426:0.19.375" "1:0.19.375:0.19.221" "0:0.19.172:0.19.375" "0:0.19.9:0.19.10"; do
    _want="${_ctl%%:*}"; _rest="${_ctl#*:}"
    _floor_covers "${_rest%%:*}" "${_rest#*:}" && _got=1 || _got=0
    assert_eq "$_want" "$_got" "_floor_covers ${_rest%%:*} vs ${_rest#*:} (control)"
done
assert_eq "" "$(_verb_row no-such-verb)" \
  "an unknown verb looks up to nothing, so the unclassified-verb leg below can fire"

# Only real INVOCATIONS, not the same verb quoted in prose or in a refusal
# message: the shape is `"${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr <verb>`.
CALLED_VERBS="$(grep -o 'loom-daemon}" merge-pr [a-z][a-z0-9-]*' "$MERGE_PR_SRC" | sed 's/.* //' | sort -u)"

# If the invocation idiom is ever refactored, the scan above could come back
# empty and every assertion below would vacuously pass. Pin the three verbs
# that are fail-closed TODAY so that refactor fails loudly instead.
for _known in verdict-contradiction stale-checks loom-pr-guard classify-response partial-conflict checks-failure; do
    assert_contains "$CALLED_VERBS" "$_known" \
      "the invocation scan still finds 'merge-pr $_known' in merge-pr.sh"
done

HIGHEST_CLOSED=""
for _verb in $CALLED_VERBS; do
    _row="$(_verb_row "$_verb")"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ -z "$_row" ]]; then
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: 'merge-pr $_verb' is invoked by merge-pr.sh but is not classified in this suite's fail-direction table"
        echo "    Add a row to MERGE_PR_VERB_TABLE: '$_verb closed <first VERSION carrying it>' or '$_verb open -'."
        echo "    If it is fail-closed, ALSO raise merge-pr.sh's '# requires-daemon: merge-pr >= …' marker to that version."
        continue
    fi
    TESTS_PASSED=$((TESTS_PASSED + 1))
    read -r _ignored _dir _ver <<<"$_row"
    if [[ "$_dir" == "closed" ]]; then
        echo -e "  ${GREEN}PASS${NC}: 'merge-pr $_verb' is classified fail-closed, first released in $_ver"
        # An empty HIGHEST_CLOSED never "covers" a real version, so the first
        # closed verb seeds it without a special case.
        _floor_covers "$HIGHEST_CLOSED" "$_ver" || HIGHEST_CLOSED="$_ver"
    else
        echo -e "  ${GREEN}PASS${NC}: 'merge-pr $_verb' is classified fail-open (degrades; does not raise the floor)"
    fi
done

TESTS_RUN=$((TESTS_RUN + 1))
if [[ -n "$HIGHEST_CLOSED" ]] && _floor_covers "$DECLARED_MERGEPR_MIN" "$HIGHEST_CLOSED"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: declared floor '$DECLARED_MERGEPR_MIN' covers the newest fail-closed verb ($HIGHEST_CLOSED)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: declared 'requires-daemon: merge-pr >= $DECLARED_MERGEPR_MIN' is BELOW the newest fail-closed sub-subcommand's first release (${HIGHEST_CLOSED:-<none found>})"
    echo "    A host between the two passes the declared floor and then has every merge refused,"
    echo "    while _mp_daemon_roll_hint names a version it already satisfies (#8967). Raise the marker."
fi

echo ""
echo "Testing the CI gate agrees merge-pr.sh's dependencies are declared…"

if [[ -x "$CHECKER" || -f "$CHECKER" ]]; then
    : >"$WORKDIR/empty-baseline.txt"
    GATE_OUT="$(bash "$CHECKER" --baseline "$WORKDIR/empty-baseline.txt" "$MERGE_PR_SRC" 2>&1)"; GATE_RC=$?
    assert_eq "0" "$GATE_RC" \
      "check-daemon-subcommand-versions.sh passes merge-pr.sh with an EMPTY baseline (every dependency declared, not grandfathered)"
    [[ "$GATE_RC" -eq 0 ]] || echo "    gate said: $GATE_OUT"
else
    echo "  SKIP: $CHECKER not present (consumer repo)"
fi

echo ""
echo "────────────────────────────────"
if [[ "$TESTS_FAILED" -eq 0 ]]; then
    echo -e "${GREEN}Results: $TESTS_PASSED/$TESTS_RUN passed, 0 failed${NC}"
    exit 0
fi
echo -e "${RED}Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed${NC}"
exit 1
