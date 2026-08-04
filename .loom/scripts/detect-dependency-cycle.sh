#!/usr/bin/env bash
# detect-dependency-cycle.sh - Bounded, cross-repo dependency-cycle detection
# for Champion's blocking evaluation (issue #5213).
#
# THE FAILURE MODE THIS EXISTS FOR
#
# Champion's unblock evaluation (champion-pr-merge.md "Step 5: Unblock
# Dependent Issues") and its promotion feasibility check are both SINGLE-HOP
# and SAME-REPO: they read a blocked issue's `Blocked by`/`Depends on`/
# `Requires` references and ask "is #N closed yet?". That question has no
# answer when the dependency graph contains a cycle -- A waits on B, B waits
# on A -- so every pass re-derives "still blocked", forever, and nothing in
# either issue's text makes the cycle visible. A real fleet deadlock of
# exactly this shape (an epic in one repo blocking a dependent issue in
# another, whose own remaining phase depended on that dependent's output) was
# found only by an operator manually walking 15 child issues.
#
# This script walks the declared dependency edges from one issue, BOUNDED in
# depth / fetched nodes / examined edges, following `owner/repo#N` and issue-URL
# references ACROSS repos, and reports a cycle if the walk ever re-enters a node
# already on the current path. With --report it additionally surfaces the cycle
# on the issue (one idempotent comment naming every node in the cycle) and routes
# the issue to `loom:operator-only`, because breaking a cycle is a human decision
# (which declared edge is wrong, or which side ships a partial first) that no
# automated pass is entitled to make.
#
# Usage:
#   detect-dependency-cycle.sh --issue <N> [--repo <owner/repo>] [options]
#
# Examples:
#   detect-dependency-cycle.sh --issue 56
#   detect-dependency-cycle.sh --issue 391 --repo 2AMLogic/klayout-tools
#   detect-dependency-cycle.sh --issue 56 --report          # comment + operator-only
#
# Options:
#   --issue <N>        Issue number to start the walk from (required).
#   --repo <nwo>       owner/repo of --issue (default: the current repo's origin).
#   --max-depth <D>    Max hops from the root = longest detectable cycle
#                       (default: 4, $LOOM_DEPCYCLE_MAX_DEPTH).
#   --max-nodes <M>    Max DISTINCT issues fetched from the forge (default: 25,
#                       $LOOM_DEPCYCLE_MAX_NODES).
#   --max-steps <S>    Max dependency edges examined (default: 500,
#                       $LOOM_DEPCYCLE_MAX_STEPS).
#   --report           On a cycle, post an idempotent comment on --issue naming
#                       every node in the cycle and add `loom:operator-only`.
#                       Without this flag the script is strictly read-only.
#   --no-cache         Read via plain `gh` instead of the `gh-cached` wrapper.
#   --help,-h          Show this help.
#
# Output (stdout, marker lines -- parseable by the Champion prose):
#   Cycle found:
#     CYCLE_DETECTED
#     CYCLE_PATH: owner/a#1 -> owner/b#2 -> owner/a#1
#     CYCLE_NODES: owner/a#1 owner/b#2
#     CYCLE_FINGERPRINT: <16 hex>
#   No cycle:
#     NO_CYCLE
#     SCANNED: <distinct issues fetched>
#   Partial-coverage markers (either case; informational, never a match):
#     SEARCH_TRUNCATED: depth|nodes|steps ...   -- a bound stopped the walk, so
#                                                  "NO_CYCLE" means "no cycle
#                                                  within the bounds", not "none"
#     UNREADABLE: owner/repo#N ...              -- not visible to this token
#   --report only:
#     REPORTED: <issue>            -- comment posted + loom:operator-only added
#     ALREADY_REPORTED: <issue>    -- this exact cycle was already surfaced
#
# Exit codes (mirrors check-duplicate.sh's convention):
#   0 - No cycle found within the bounds
#   1 - Dependency cycle detected
#   2 - Error (bad arguments, root issue unreadable, missing jq)
#
# BOUNDED COST. Every knob above is a hard stop, and the walk is additionally
# memoized: each distinct issue is fetched at most once per invocation, and a
# subtree that completed without hitting the depth bound is never re-walked.
# Reads default to the `gh-cached` wrapper (30s TTL), so a Champion pass that
# checks several blocked issues in a row shares one set of responses. Callers
# should still invoke this only when the cheap single-hop check has already
# concluded "still blocked" -- that is the trigger condition, not every pass.
#
# FALSE-POSITIVE POSTURE. The edge parser deliberately reuses the repo's existing
# two-stage dependency-phrase parse (#4508) rather than inventing a stricter
# second one, and that parse is line-scoped: every reference on a line that
# declares a dependency phrase is treated as an edge, so "Blocked by #2. See also
# #99." yields both. That is over-inclusive by design -- under-parsing here would
# miss a real deadlock, while over-parsing at worst routes one issue to a human
# with the full path spelled out. A false positive is bounded and reversible: one
# comment, one label, no state machine advanced, `loom:blocked` left untouched.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- output helpers ----
if [[ -t 2 ]]; then
    RED='\033[0;31m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
else
    RED=''; YELLOW=''; BLUE=''; NC=''
fi
err()  { echo -e "${RED}ERROR: $1${NC}" >&2; }
warn() { echo -e "${YELLOW}WARNING: $1${NC}" >&2; }
info() { echo -e "${BLUE}i $1${NC}" >&2; }

show_help() {
    sed -n '2,72p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---- defaults ----
ISSUE=""
REPO_NWO=""
MAX_DEPTH="${LOOM_DEPCYCLE_MAX_DEPTH:-4}"
MAX_NODES="${LOOM_DEPCYCLE_MAX_NODES:-25}"
MAX_STEPS="${LOOM_DEPCYCLE_MAX_STEPS:-500}"
DO_REPORT=0
NO_CACHE=0

# ---- dependency-reference parser (reused vocabulary, extracted by tests) ----
#
# REUSES the exact dependency-phrase vocabulary established in guide.md's
# `parse_dependencies` and shared by champion-pr-merge.md's unblock loop
# (`(Blocked by|Depends on|Requires)[*_:[:space:]]*#N`, tolerant of markdown
# emphasis/colon between the phrase and the ref, #4508). It does NOT introduce
# a second divergent parser; it only widens the REFERENCE half so a cross-repo
# edge is followable:
#
#   #391                                          -> same repo as the citing issue
#   2AMLogic/klayout-tools#391                    -> explicit owner/repo
#   https://github.com/2AMLogic/marketing/issues/56 -> issue URL
#
# Two-stage, exactly like every other reuser: stage 1 selects lines declaring a
# dependency phrase, stage 2 extracts EVERY reference on those lines (not just
# the first), so comma-separated lists are captured in full. Emits normalized
# `owner/repo#N` nodes, deduplicated.
parse_dependency_refs() {
    local body="$1" default_repo="$2"
    printf '%s\n' "$body" \
        | grep -E '(Blocked by|Depends on|Requires)[*_:[:space:]]*(([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)?#[0-9]+|https?://[^[:space:]),]+/issues/[0-9]+)' \
        | grep -oE '([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)?#[0-9]+|https?://[^[:space:]),]+/issues/[0-9]+' \
        | while IFS= read -r ref; do
            case "$ref" in
                http*)
                    # https://<host>/<owner>/<repo>/issues/<N>
                    local num="${ref##*/}"
                    local rest="${ref%/issues/*}"   # https://<host>/<owner>/<repo>
                    rest="${rest#*://}"             # <host>/<owner>/<repo>
                    rest="${rest#*/}"               # <owner>/<repo>
                    [[ "$rest" == */* ]] && printf '%s#%s\n' "$rest" "$num"
                    ;;
                '#'*)
                    printf '%s#%s\n' "$default_repo" "${ref#\#}"
                    ;;
                *)
                    printf '%s\n' "$ref"
                    ;;
            esac
        done \
        | sort -u
}

# ---- small set helpers (space-separated strings; no bash-4 assoc arrays) ----
# True (0) if $1 appears as a whole word in the space-separated list $2.
_in_set() {
    local needle="$1" haystack=" $2 "
    [[ "$haystack" == *" $needle "* ]]
}

# Portable sha256 (sha256sum on Linux, shasum on macOS) - same fallback shape
# the rest of the repo's scripts and the Champion prose use.
_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum
    elif command -v shasum >/dev/null 2>&1; then shasum -a 256
    else cksum; fi
}

# Filesystem-safe cache key for an `owner/repo#N` node.
_node_key() {
    printf '%s' "$1" | tr '/#' '__'
}

# ---- forge reads (memoized, bounded) ----
FETCH_COUNT=0
UNREADABLE_NODES=""
TRUNCATED=""
# Monotonic tally of every event that left part of the graph unexplored (a bound
# stopped the walk, or a node was unreadable). The memoization guard in _walk
# snapshots it around a subtree to decide whether that subtree was complete.
INCOMPLETE_COUNT=0

_note_truncated() {
    _in_set "$1" "$TRUNCATED" || TRUNCATED="$TRUNCATED $1"
    INCOMPLETE_COUNT=$((INCOMPLETE_COUNT + 1))
}

# Populate $CACHE_DIR/<key> with "STATE\n<body>" for a node. Returns 0 when the
# node is readable (or already cached), 1 when it is not (unreadable, or the
# fetch budget is spent). Each distinct node costs at most one forge read.
_fetch_node() {
    local node="$1"
    local key f
    key="$(_node_key "$node")"
    f="$CACHE_DIR/$key"
    [[ -f "$f" ]] && return 0
    [[ -f "$f.miss" ]] && return 1

    if [[ "$FETCH_COUNT" -ge "$MAX_NODES" ]]; then
        _note_truncated "nodes"
        return 1
    fi
    FETCH_COUNT=$((FETCH_COUNT + 1))

    local repo="${node%#*}" num="${node##*#}" json
    json="$("$GH_READ" issue view "$num" --repo "$repo" --json state,body 2>/dev/null)" || json=""
    if [[ -z "$json" ]]; then
        : > "$f.miss"
        _in_set "$node" "$UNREADABLE_NODES" || UNREADABLE_NODES="$UNREADABLE_NODES $node"
        INCOMPLETE_COUNT=$((INCOMPLETE_COUNT + 1))
        return 1
    fi
    # `printf '%s\n' "$VAR" | jq`, never `echo "$VAR" | jq` (#5094): zsh's echo
    # reinterprets backslash escapes and corrupts captured `gh --json` payloads.
    {
        printf '%s\n' "$json" | jq -r '.state // "UNKNOWN"'
        printf '%s\n' "$json" | jq -r '.body // ""'
    } > "$f"
    return 0
}

_node_state() { head -n 1 "$CACHE_DIR/$(_node_key "$1")" 2>/dev/null; }
_node_body()  { tail -n +2 "$CACHE_DIR/$(_node_key "$1")" 2>/dev/null; }

# ---- the walk ----
EXPLORED=""       # nodes whose subtree completed without hitting the depth bound
STEP_COUNT=0
CYCLE_NODES_PATH=""   # space-separated cycle, first node repeated at the end

# Given the node already on the path and the current path, emit the cycle
# segment: from the first occurrence of $1 through the end of the path, with
# $1 repeated to close the loop.
_cycle_segment() {
    local ref="$1" path="$2" out="" started=0 n
    for n in $path; do
        [[ "$started" -eq 0 && "$n" == "$ref" ]] && started=1
        [[ "$started" -eq 1 ]] && out="$out $n"
    done
    printf '%s %s\n' "${out# }" "$ref"
}

# Depth-first walk from $1 (already fetched). $3 is the space-separated path of
# nodes from the root through $1 inclusive.
# Returns 1 as soon as a cycle is found (setting CYCLE_NODES_PATH), else 0.
_walk() {
    local node="$1" depth="$2" path="$3"

    local body ref
    body="$(_node_body "$node")"

    for ref in $(parse_dependency_refs "$body" "${node%#*}"); do
        if [[ "$STEP_COUNT" -ge "$MAX_STEPS" ]]; then
            _note_truncated "steps"
            return 0
        fi
        STEP_COUNT=$((STEP_COUNT + 1))

        # A body naming its own number is never a dependency on itself.
        [[ "$ref" == "$node" ]] && continue

        # Re-entering a node already on this path IS the cycle.
        if _in_set "$ref" "$path"; then
            CYCLE_NODES_PATH="$(_cycle_segment "$ref" "$path")"
            return 1
        fi

        # Already fully explored under an unbounded subtree - nothing new below.
        _in_set "$ref" "$EXPLORED" && continue

        # The depth bound is enforced HERE, before the fetch, not on entry to the
        # recursive call: a node we will never expand has a body we will never
        # parse, so fetching it would spend a forge read for nothing -- and the
        # frontier is the widest layer of the walk, so that waste is the single
        # largest avoidable cost. Detection power is unchanged: the back-edge
        # test above has already run against this ref, so a cycle that closes
        # exactly at the bound is still found. MAX_DEPTH is therefore the length
        # of the longest cycle this walk can detect.
        if [[ "$((depth + 1))" -ge "$MAX_DEPTH" ]]; then
            _note_truncated "depth"
            continue
        fi

        _fetch_node "$ref" || continue

        # A CLOSED dependency is a resolved edge, not a live blocker: it cannot
        # participate in a deadlock, and following it would report historical
        # cycles that no longer hold anyone up.
        [[ "$(_node_state "$ref")" == "OPEN" ]] || continue

        local incomplete_before="$INCOMPLETE_COUNT"

        if ! _walk "$ref" "$((depth + 1))" "$path $ref"; then
            return 1
        fi

        # Memoize only when this subtree was explored COMPLETELY. A subtree cut
        # short by the depth bound (or by an unreadable/unfetchable node) may
        # still hold a cycle reachable from a shallower path, so recording it as
        # explored there would under-report.
        if [[ "$INCOMPLETE_COUNT" -eq "$incomplete_before" ]]; then
            _in_set "$ref" "$EXPLORED" || EXPLORED="$EXPLORED $ref"
        fi
    done
    return 0
}

# ---- reporting (--report only) ----
# Comment once per distinct cycle (fingerprinted by the cycle's node SET, so the
# same cycle discovered from either side collapses to one marker) and route the
# issue to loom:operator-only. Mirrors champion-issue-promo.md's marker-and-skip
# idempotency: an unchanged cycle is never re-commented, a genuinely different
# cycle always is.
_report_cycle() {
    local node="$1" pretty="$2" nodes="$3" fingerprint="$4"
    local repo="${node%#*}" num="${node##*#}"
    local marker="<!-- champion:dep-cycle:$fingerprint -->"

    if gh issue view "$num" --repo "$repo" --json comments \
        --jq '.comments[].body' 2>/dev/null | grep -qF "$marker"; then
        echo "ALREADY_REPORTED: $node"
        return 0
    fi

    local body
    body="$(cat <<EOF
**Dependency cycle detected** - this issue cannot be unblocked by waiting.

The declared \`Blocked by\` / \`Depends on\` / \`Requires\` references form a closed loop:

\`\`\`
$pretty
\`\`\`

Every issue on that path is OPEN, and each one's progress is declared to depend on
the next, so the loop can never resolve itself: each side re-derives "still blocked"
on every pass, indefinitely. This is the deadlock shape that previously had to be
found by hand.

Breaking a cycle is a human decision - which declared dependency edge is wrong, or
which side ships a partial increment first - so Champion is routing this to
\`loom:operator-only\` instead of re-deriving the same conclusion forever.

**Cycle members**: $nodes

---
*Automated by Champion role (detect-dependency-cycle.sh)*
$marker
EOF
)"

    if ! gh issue comment "$num" --repo "$repo" --body "$body" >/dev/null 2>&1; then
        warn "could not comment on $node"
        return 1
    fi
    if ! gh issue edit "$num" --repo "$repo" --add-label "loom:operator-only" >/dev/null 2>&1; then
        warn "could not add loom:operator-only to $node"
    fi
    echo "REPORTED: $node"
    return 0
}

# ---- repo resolution ----
_resolve_repo() {
    local url
    url="$(git remote get-url origin 2>/dev/null || true)"
    if [[ -n "$url" ]]; then
        url="${url%.git}"
        url="$(printf '%s' "$url" | sed -E 's#^.*[:/]([^/]+/[^/]+)$#\1#')"
        if [[ "$url" == */* ]]; then
            printf '%s\n' "$url"
            return 0
        fi
    fi
    gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null
}

# ---- main ----
main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --issue)     ISSUE="${2:-}"; shift 2 ;;
            --repo)      REPO_NWO="${2:-}"; shift 2 ;;
            --max-depth) MAX_DEPTH="${2:-}"; shift 2 ;;
            --max-nodes) MAX_NODES="${2:-}"; shift 2 ;;
            --max-steps) MAX_STEPS="${2:-}"; shift 2 ;;
            --report)    DO_REPORT=1; shift ;;
            --no-cache)  NO_CACHE=1; shift ;;
            --help|-h)   show_help; exit 0 ;;
            *)           err "Unexpected argument: $1"; show_help >&2; exit 2 ;;
        esac
    done

    if [[ -z "$ISSUE" ]]; then
        err "Usage: detect-dependency-cycle.sh --issue <N> [--repo <owner/repo>]"
        exit 2
    fi
    if ! [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
        err "--issue must be a number (got: $ISSUE)"
        exit 2
    fi
    if ! command -v jq >/dev/null 2>&1; then
        err "jq is required"
        exit 2
    fi

    # Reads go through the gh-cached wrapper when it is available and working -
    # this is backlog analysis, not merge-gate arbitration, and a shared 30s TTL
    # keeps a multi-issue Champion pass to one set of responses.
    GH_READ="gh"
    if [[ "$NO_CACHE" -eq 0 ]]; then
        local ghc="$SCRIPT_DIR/gh-cached"
        if [[ -x "$ghc" ]] && "$ghc" --version >/dev/null 2>&1; then GH_READ="$ghc"; fi
    fi

    if [[ -z "$REPO_NWO" ]]; then
        REPO_NWO="$(_resolve_repo)"
    fi
    if [[ -z "$REPO_NWO" ]]; then
        err "could not determine the repo - pass --repo <owner/repo>"
        exit 2
    fi

    CACHE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/loom-depcycle.XXXXXX")" || {
        err "could not create a scratch directory"
        exit 2
    }
    trap 'rm -rf "$CACHE_DIR"' EXIT

    local root="$REPO_NWO#$ISSUE"
    if ! _fetch_node "$root"; then
        err "could not read $root"
        exit 2
    fi

    local rc=0
    _walk "$root" 0 "$root" || rc=1

    if [[ "$rc" -eq 1 ]]; then
        local pretty nodes fingerprint
        pretty="$(printf '%s' "$CYCLE_NODES_PATH" | sed 's/ / -> /g')"
        # Fingerprint the cycle's node SET (sorted, deduplicated) so the same
        # cycle collapses to one identity no matter which member it was found
        # from - and so a genuine change in membership yields a new identity.
        # shellcheck disable=SC2086  # intentional word splitting: space-separated node list
        nodes="$(printf '%s\n' $CYCLE_NODES_PATH | sort -u | tr '\n' ' ')"
        nodes="${nodes% }"
        fingerprint="$(printf '%s' "$nodes" | _sha256 | awk '{print substr($1, 1, 16)}')"

        echo "CYCLE_DETECTED"
        echo "CYCLE_PATH: $pretty"
        echo "CYCLE_NODES: $nodes"
        echo "CYCLE_FINGERPRINT: $fingerprint"
        [[ -n "$UNREADABLE_NODES" ]] && echo "UNREADABLE:$UNREADABLE_NODES"

        if [[ "$DO_REPORT" -eq 1 ]]; then
            _report_cycle "$root" "$pretty" "$nodes" "$fingerprint"
        fi
        exit 1
    fi

    echo "NO_CYCLE"
    echo "SCANNED: $FETCH_COUNT"
    [[ -n "$TRUNCATED" ]] && echo "SEARCH_TRUNCATED:$TRUNCATED"
    [[ -n "$UNREADABLE_NODES" ]] && echo "UNREADABLE:$UNREADABLE_NODES"
    exit 0
}

# Only run main when executed directly (not when sourced by tests).
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
