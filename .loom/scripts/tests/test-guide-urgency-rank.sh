#!/usr/bin/env bash
# test-guide-urgency-rank.sh - Regression test for issue #8769 (recurring #8649)
#
# guide.md's urgency_rank() rank-2 branch ("the delivery pipeline itself is
# down") used to match a BARE `outage` substring in the title. `outage` is a
# plausible component NAME, not just a live-incident claim: #8649 ("watchdog:
# peer-coord/outage sentinel writes silently swallow I/O errors, causing
# duplicate escalation issues under disk pressure", tier:maintenance) is a
# routine reliability bug, but the bare-substring regex promoted it to
# loom:urgent on one fleet-host tick (2026-09-23 ~09:13 UTC). Two later triage
# sessions then reached two DIFFERENT judgment calls on the identical
# false-positive input — exactly the divergence-between-hosts failure mode
# the deterministic ladder exists to prevent.
#
# The fix requires adjacent pipeline/CI/build context for `outage`
# (`pipeline outage` / `CI outage` / `build outage`); `stalled`/`halted`/
# `wedged` already carry their `pipeline` prefix and never match alone.
#
# This suite extracts the real urgency_rank() function from BOTH shipped
# copies — defaults/.claude/commands/loom/guide.md (what .loom/roles/guide.md
# and defaults/roles/guide.md symlink to) and the byte-identical
# defaults/.agents/skills/loom-guide/SKILL.md agent-skill duplicate — and
# EXECUTES it against a hermetic gh stub, so the regex is exercised as
# shipped, not re-derived here. It also asserts the two copies stay
# byte-identical (the two surfaces — slash-command prompt vs agent skill —
# must not diverge).
#
# Hermetic: no forge/network calls. A stub `gh` on PATH answers exactly the
# two reads urgency_rank() makes (title, labels) from the fixture table.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"

# guide.md is shipped (installed at .claude/commands/loom/guide.md), so
# resolve it the way each layout actually lays it out: the installed path
# first (consumer repos, and Loom's own dogfooded checkout), falling back
# to the defaults/ source-tree path (a bare source checkout with no
# .claude/commands/loom/ copy yet). See issue #6194 / #6241 — same
# resolution test-urgent-flip-guard.sh uses.
if [[ -f "$REPO_ROOT/.claude/commands/loom/guide.md" ]]; then
    GUIDE_MD="$REPO_ROOT/.claude/commands/loom/guide.md"
else
    GUIDE_MD="$REPO_ROOT/defaults/.claude/commands/loom/guide.md"
fi
# The agent-skill duplicate only ships under defaults/ (there is no
# .agents/ install mirror to prefer).
SKILL_MD="$REPO_ROOT/defaults/.agents/skills/loom-guide/SKILL.md"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

summarize_and_exit() {
    echo ""
    echo "================================"
    echo "Tests run:    $TESTS_RUN"
    echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
    if [[ $TESTS_FAILED -gt 0 ]]; then
        echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"
        exit 1
    fi
    echo "All tests passed"
    exit 0
}

# --- Extract the shipped urgency_rank() (top-level function: from its
# `urgency_rank() {` line to the first column-0 `}` after it). ---------------
extract_urgency_rank() {
    awk '/^urgency_rank\(\) \{/{f=1} f{print} f && /^\}/{exit}' "$1"
}

# --- Hermetic gh stub: answers the two reads urgency_rank() makes. ----------
# Fixture table (issue number -> title / comma-joined labels):
#   101  the exact #8649 title, tier:maintenance      -> must rank 5 (not 2)
#   102  "outage detector" component name, untiered  -> must rank 5 (not 2)
#   103  "outage watcher" component, tier:goal-advancing -> must rank 3 (not 2)
#   104  "main is red: ..."                           -> must rank 2
#   105  "CI is red after flake storm"                -> must rank 2
#   106  "pipeline is stalled ..."                    -> must rank 2
#   107  "pipeline stalled ..." (no "is")             -> must rank 2
#   108  "broken main: revert needed"                 -> must rank 2
#   109  "pipeline outage ..."                        -> must rank 2
#   110  "CI outage: ..."                             -> must rank 2
#   111  "build outage ..."                           -> must rank 2
#   112  "pipeline is halted at gate"                 -> must rank 2
#   113  "pipeline is wedged after merge"             -> must rank 2
#   114  "Main is red ..." (case-insensitivity)       -> must rank 2
#   115  "security: credential leak ..."              -> must rank 1
STUB_BIN="$(mktemp -d)"
# shellcheck disable=SC2317  # invoked indirectly, via the EXIT trap below
cleanup() { rm -rf "$STUB_BIN"; }
trap cleanup EXIT

cat > "$STUB_BIN/gh" <<'STUB'
#!/usr/bin/env bash
num=""
for a in "$@"; do
    case "$a" in ''|*[!0-9]*) ;; *) num="$a"; break ;; esac
done
case "$*" in
    *"--json title"*)
        case "$num" in
            101) echo "watchdog: peer-coord/outage sentinel writes silently swallow I/O errors, causing duplicate escalation issues under disk pressure" ;;
            102) echo "outage detector flaps between fleet hosts" ;;
            103) echo "outage watcher reports stale sentinels" ;;
            104) echo "main is red: commit 4f2a1b9 fails all builds" ;;
            105) echo "CI is red after flake storm" ;;
            106) echo "pipeline is stalled behind stuck runner" ;;
            107) echo "pipeline stalled: queued jobs not starting" ;;
            108) echo "broken main: revert needed" ;;
            109) echo "pipeline outage takes down deploys" ;;
            110) echo "CI outage: actions API returning 5xx" ;;
            111) echo "build outage on fleet host alpha" ;;
            112) echo "pipeline is halted at gate" ;;
            113) echo "pipeline is wedged after merge" ;;
            114) echo "Main is red: nightly job failing" ;;
            115) echo "security: credential leak in telemetry exporter" ;;
        esac
        ;;
    *"--json labels"*)
        case "$num" in
            101) echo "tier:maintenance" ;;
            103) echo "tier:goal-advancing" ;;
            115) echo "tier:maintenance" ;;
        esac
        ;;
esac
exit 0
STUB
chmod +x "$STUB_BIN/gh"
PATH="$STUB_BIN:$PATH"
export PATH

if [[ ! -f "$GUIDE_MD" ]]; then
    echo "guide.md not found at $GUIDE_MD" >&2
    exit 1
fi
if [[ ! -f "$SKILL_MD" ]]; then
    echo "SKILL.md not found at $SKILL_MD" >&2
    exit 1
fi

URGENCY_GUIDE="$(extract_urgency_rank "$GUIDE_MD")"
URGENCY_SKILL="$(extract_urgency_rank "$SKILL_MD")"

if [[ -z "$URGENCY_GUIDE" ]]; then
    echo "could not extract urgency_rank() from $GUIDE_MD" >&2
    exit 1
fi
if [[ -z "$URGENCY_SKILL" ]]; then
    echo "could not extract urgency_rank() from $SKILL_MD" >&2
    exit 1
fi

# The two shipped copies must stay byte-identical, or the slash-command
# surface and the agent-skill surface diverge (#8769 curator finding).
if [[ "$URGENCY_GUIDE" == "$URGENCY_SKILL" ]]; then
    pass "SKILL.md urgency_rank() is byte-identical to guide.md's"
else
    fail "SKILL.md urgency_rank() diverged from guide.md (the two surfaces must change in lockstep)"
fi

# Tripwire: the pre-#8769 bare-`outage` pattern must not come back.
# (here-string, not `printf | grep -q` — that pipeline is the pipefail
#  early-exit SIGPIPE class check-pipefail-early-exit.sh ratchets against)
if grep -Fq "pipeline (is )?(stalled|halted|wedged)|outage" <<<"$URGENCY_GUIDE"; then
    fail "rank-2 regex still contains the bare |outage alternative (reverted fix?)"
else
    pass "rank-2 regex no longer carries the bare |outage alternative"
fi

# Load the real function and exercise it through the stub.
# shellcheck disable=SC1090
eval "$URGENCY_GUIDE"

assert_rank() {
    local num="$1" expected="$2" msg="$3" got
    got="$(urgency_rank "$num")"
    if [[ "$got" == "$expected" ]]; then
        pass "$msg"
    else
        fail "$msg (urgency_rank $num = '$got', expected '$expected')"
    fi
}

echo "rank-2 false positives (component-name collocations, #8649/#8769):"
assert_rank 101 5 "#8649 exact title ranks tier-derived 5, not 2"
assert_rank 102 5 "'outage detector' component name ranks 5, not 2"
assert_rank 103 3 "'outage watcher' component + tier:goal-advancing ranks 3, not 2"

echo "genuine rank-2 titles still rank 2 (no false negatives):"
assert_rank 104 2 "'main is red' still ranks 2"
assert_rank 105 2 "'CI is red' still ranks 2"
assert_rank 106 2 "'pipeline is stalled' still ranks 2"
assert_rank 107 2 "'pipeline stalled' (no 'is') still ranks 2"
assert_rank 108 2 "'broken main' still ranks 2"
assert_rank 112 2 "'pipeline is halted' still ranks 2"
assert_rank 113 2 "'pipeline is wedged' still ranks 2"
assert_rank 114 2 "'Main is red' still ranks 2 (case-insensitive)"

echo "outage WITH adjacent pipeline/CI/build context ranks 2:"
assert_rank 109 2 "'pipeline outage' ranks 2"
assert_rank 110 2 "'CI outage' ranks 2"
assert_rank 111 2 "'build outage' ranks 2"

echo "rank-1 branch unaffected:"
assert_rank 115 1 "'security: credential leak' still ranks 1"

summarize_and_exit
