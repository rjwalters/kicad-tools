#!/bin/bash

# check-duplicate.sh - Check for potential duplicate issues before creating new ones
#
# This script searches existing open issues for potential duplicates based on
# keyword matching and similarity heuristics. Used by Architect, Hermit, and
# Auditor roles before creating new issues.
#
# With --include-merged-prs, also checks recently merged PRs and recently
# closed issues to catch near-duplicate issues that arrive right after their
# counterpart's PR merges.
#
# Usage:
#   check-duplicate.sh "Issue title" ["Issue body"]
#   check-duplicate.sh --title "Issue title" [--body "Issue body"]
#   check-duplicate.sh --include-merged-prs --title "Issue title"
#   check-duplicate.sh -- "--title-like text that starts with a dash" ["Body"]
#   check-duplicate.sh --help
#
# A title (or body) that itself begins with "-"/"--" (common in CLI-tool
# repos, where a bug report's title quotes the offending flag) is NOT safe to
# pass positionally without a guard: bash's ordinary `while ... case "$1" in`
# argument loop cannot distinguish "the caller meant this as an option" from
# "the caller meant this as literal text that happens to start with a dash"
# just by looking at the token. Use the standard `--` end-of-options
# separator (#5898): every argument after a bare `--` is treated as
# positional text, verbatim, no matter what it starts with.
#
# Exit codes:
#   0 - No duplicates found, safe to create issue
#   1 - Potential duplicates found (listed to stdout)
#   2 - Error (invalid arguments, gh command failed, etc.)
#
# A confirmed duplicate always wins over a partial failure (#4526): if ANY
# search pool (open issues / merged PRs / closed issues) finds a match, the
# exit code is 1 even when another pool could not be searched at all. Exit 2
# means "no answer at all", never "we found something but also had trouble".
#
# Output format (when duplicates found):
#   DUPLICATE_FOUND
#   #<number>: <title> (similarity: <percent>%)
#   PR #<number>: <title> (similarity: <percent>%)
#   ...
#
# Near-match band (#8289, re-landed as a daemon port by #8360), OPT-IN via
# --warn-threshold N and OPEN ISSUES ONLY. Candidates scoring in
# [N, --threshold) are reported as CONTEXT, never as a verdict: they do not
# set the exit code, are not counted toward NON_DISCRIMINATIVE, and appear
# under their own marker so no caller can mistake them for a DUPLICATE_FOUND
# row:
#   NEAR_DUPLICATE (context only, not a duplicate verdict)
#   NEAR #<number>: <title> (similarity: <percent>%)
# Why the band exists: below --threshold the scorer used to say NOTHING AT
# ALL, so create-issue.sh's backstop was a cliff -- a hard, unexplained
# refusal at threshold and total silence one point below it (#8289). The
# band turns that cliff into a gradient without moving the block line.
#
# Title corroboration at low block scores (#8591). The shipped defaults:
#
#   --threshold          18   block line (the #4409 calibration, unmoved)
#   --title-threshold    18   the title-only Jaccard a low block must reach
#   --corroborate-below  25   ceiling of the region where that is required
#
# Only --threshold is a flag of THIS script; the other two are constants of
# `loom-daemon duplicate-scan` (flags of that subcommand, defined once in
# loom-daemon/src/cli/duplicate_scan.rs) and are not re-exposed here.
#
# An OPEN-issues candidate whose full-text score lands in [18, 25) blocks
# only if its TITLE-only Jaccard also reaches 18%. Two long issues in the
# same subsystem share enough jargon to clear 18% on full text alone: #8561
# (a Kimi CLI harness adapter) was refused as a duplicate of #8505 (an
# OpenCode metered-runtime budget bug) at exactly that floor on 2026-09-21,
# on one shared title keyword ("runtime"). Titles are short and specific, so
# they are the cheap second opinion: the confirmed pair #3550/#3551 scores
# 19% on bodies but 26% on titles and still blocks; #8561/#8505 scores 3% and
# no longer does. This is a calibration, not a disable -- and an
# uncorroborated candidate is DEMOTED to a NEAR row (annotated with the title
# overlap that fell short), never silently dropped. At/above 25% the body
# overlap stands on its own. Pass --corroborate-below 0 for the old behaviour.
#
# Since #8360 the similarity scan itself (keyword extraction, true-Jaccard
# scoring, threshold banding, degenerate detection) is
# `loom-daemon duplicate-scan` (loom-daemon/src/cli/duplicate_scan.rs), per
# .loom/docs/shell-language-policy.md: this file is a `contract`-category
# entry whose allowlist line says new logic goes behind it into loom-daemon.
# What stays here is the forge fetch (with its rate-limit REST fallback,
# #4526) and the three-pool output aggregation. Without a loom-daemon binary
# the scan cannot run: each pool reports "incomplete" and the script exits 2
# (create-issue.sh fails open on that, as it always has on a broken check).
#
# Degraded-coverage marker lines (#4526), appended after the match list when
# GraphQL rate-limiting forced a degraded search. Both are informational: they
# are not matches, and the --json parser reports them as `search_incomplete`
# rather than as entries in `matches`.
#   SEARCH_INCOMPLETE: <pools> -- that pool could not be searched at all
#                                 (GraphQL rate-limited AND REST fallback
#                                 failed), so the match list is partial
#   REST_FALLBACK: <pools>     -- that pool was answered by the REST fallback,
#                                 whose similarity ranking basis differs
#
# Degenerate-result self-detection (#4409): if more than half of the
# candidates scanned in a given search (open issues / merged PRs / closed
# issues) score at/above --threshold, that search's similarity scores aren't
# discriminating anything for this query. Its block prints NON_DISCRIMINATIVE
# instead of DUPLICATE_FOUND (still exit code 1, so existing
# `if ! check-duplicate.sh ...` callers still fall back to manual review --
# they just should NOT treat the (absent) match list as real duplicates).

set -euo pipefail

# Minimum number of scanned candidates before degenerate-result
# self-detection (#4409) kicks in now lives in the daemon port
# (loom-daemon/src/cli/duplicate_scan.rs, MIN_SCANNED_FOR_DEGENERATE), like
# the rest of the scan since #8360.

# Forge-agnostic issue/PR operations via the native `loom-daemon forge`
# subcommand (port of the retired `loom-forge`). On GitHub it is a byte-identical
# passthrough to `gh`; on Gitea it declines (exit 3) and the caller degrades to
# the `gh` fallback. Validate loom-daemon actually works (not just on PATH), then
# keep the `gh` fallback so a workspace with no loom-daemon still functions.
if command -v loom-daemon &>/dev/null && loom-daemon --version &>/dev/null; then
    FORGE="loom-daemon forge"
else
    if command -v loom-daemon &>/dev/null; then
        echo "WARNING: loom-daemon is on PATH but non-functional, falling back to gh" >&2
    fi
    FORGE="gh"
fi

# The similarity scan is `loom-daemon duplicate-scan` (#8360). The pool
# functions below exec it through lib/script-helper.sh's standard resolution
# ($LOOM_DAEMON_SELF_BIN first, then the locate-daemon-bin chain), so a test
# harness pins the binary with the same seam every ported stub uses.
# LOOM_SCRIPT_HELPER_MISSING_RC is load-bearing: with no binary the helper
# must fail as "could not run" (2) — for the open-issues pool, its default
# exit 1 would read as a duplicate VERDICT, the worst possible lie a
# duplicate check can tell.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=lib/script-helper.sh
source "$SCRIPT_DIR/lib/script-helper.sh"
export LOOM_SCRIPT_HELPER_MISSING_RC=2

# Colors for output (only when stderr is a terminal)
if [[ -t 2 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    NC=''
fi

print_error() {
    echo -e "${RED}ERROR: $1${NC}" >&2
}

print_warning() {
    echo -e "${YELLOW}WARNING: $1${NC}" >&2
}

print_success() {
    echo -e "${GREEN}$1${NC}" >&2
}

print_help() {
    cat << 'EOF'
check-duplicate.sh - Check for potential duplicate issues

USAGE:
    check-duplicate.sh "Issue title" ["Issue body"]
    check-duplicate.sh --title "Issue title" [--body "Issue body"]
    check-duplicate.sh --threshold 50 --title "Title"
    check-duplicate.sh --include-merged-prs --title "Title"
    check-duplicate.sh --issue 42 --title "Title"
    check-duplicate.sh -- "--strategy basic is ignored" ["Body"]

OPTIONS:
    --                      End of options. Every argument after a bare "--"
                            is treated as positional text (title, then body)
                            verbatim, even if it starts with "-" or "--".
                            Required when a title/body begins with a dash
                            and is passed positionally rather than via
                            --title/--body (#5898).
    --title TEXT            The title of the issue to check
    --body TEXT             The body/description of the issue (optional)
    --threshold NUM         Similarity threshold percentage, on a true Jaccard
                             scale -- matches / |union| (default: 18). 60%+
                             Jaccard is rarely reachable on richly-worded
                             bodies; 18 is calibrated against real historical
                             pairs from this repo (#4409): a confirmed
                             duplicate (#3550/#3551) scored 19%, while
                             unrelated richly-worded issues scored 4-13%.
                             This is a noisy signal on long, jargon-heavy
                             bodies (a second real duplicate pair scored only
                             13%, indistinguishable from unrelated) -- treat
                             a miss as inconclusive, not proof of no overlap.
                             Which is also why a match between this line and
                             25% needs >= 18% TITLE overlap to block (#8591);
                             an uncorroborated one is demoted to a NEAR row.
    --warn-threshold NUM    Also report OPEN issues scoring in the
                             [NUM, --threshold) band as NEAR_DUPLICATE
                             context rows (#8289). Off by default, so every
                             existing caller's output and exit code are
                             unchanged. Near matches NEVER affect the exit
                             code and never count toward NON_DISCRIMINATIVE.
                             Calibration, from the same #4409 data as
                             --threshold: 13 is both the TOP of the observed
                             unrelated-issue range (4-13%) and the score of a
                             second CONFIRMED duplicate pair -- i.e. [13, 18)
                             is the band where real duplicates and unrelated
                             issues are known to be indistinguishable, which
                             is why it warns rather than blocks. Ignored when
                             0 or >= --threshold.
    --include-merged-prs    Also check recently merged PRs and closed issues
    --issue N               Also probe for OPEN issues/PRs that cross-reference
                             issue N (GitHub timeline API). Curator use --
                             surfaces "related open work" distinct from
                             duplicates: open items that may already argue for
                             a different spec for N. GitHub-only; a non-GitHub
                             forge or API failure skips the probe with a
                             warning rather than failing the whole check.
    --json                  Output results as JSON
    --help                  Show this help message

EXAMPLES:
    # Check if an issue about button styling might be a duplicate
    check-duplicate.sh "Fix button styling in dark mode"

    # Check with body content for better matching
    check-duplicate.sh --title "Fix crash on startup" --body "App crashes when..."

    # Use custom threshold (lower = more matches)
    check-duplicate.sh --threshold 40 "Refactor authentication module"

    # Also check recently merged PRs and closed issues
    check-duplicate.sh --include-merged-prs "Refactor authentication module"

    # Also surface open work that cross-references issue #42
    check-duplicate.sh --issue 42 --title "Refactor authentication module"

    # A title that itself starts with a dash (e.g. quoting a CLI flag) --
    # "--" marks the end of options so it is not mistaken for one (#5898)
    check-duplicate.sh -- "--strategy basic is ignored"

EXIT CODES:
    0  No duplicates (and, with --issue, no related open work) found
    1  Potential duplicates (or related open work) found (listed to stdout)
    2  Error (invalid arguments, gh command failed, etc.)

INTEGRATION:
    Use in Architect/Hermit/Auditor roles before gh issue create:

    if ./.loom/scripts/check-duplicate.sh "My issue title"; then
        ./.loom/scripts/create-issue.sh --title "My issue title" ...
    else
        echo "Potential duplicate detected, skipping creation"
    fi

    Use with --include-merged-prs in Curator/Guide roles:

    if ! ./.loom/scripts/check-duplicate.sh --include-merged-prs "$TITLE" "$BODY"; then
        echo "Potential overlap with merged PR or closed issue"
    fi

    Use with --issue N in the Curator role, before enriching issue N, to
    surface open cross-referencing work that may already argue for a
    different spec (issue #4162):

    if ! ./.loom/scripts/check-duplicate.sh --include-merged-prs --issue "$N" "$TITLE" "$BODY"; then
        echo "Read the DUPLICATE_FOUND / RELATED_OPEN_WORK output before curating"
    fi
EOF
}

# Keyword extraction, Jaccard similarity, threshold banding and the
# degenerate-result detector now live in `loom-daemon duplicate-scan`
# (loom-daemon/src/cli/duplicate_scan.rs, #8360). They were shell functions
# here from #4409 until that port; the port is behaviour-identical (the
# black-box suite's hand-checkable percentages run against the daemon now),
# and this file keeps the forge fetch + aggregation because that is I/O with
# its own degradation contract (#4526), not decision logic.

# Detect a GitHub rate-limit rejection in captured `gh` output (stdout+stderr
# merged via `2>&1`). GraphQL and REST draw on independent quotas (confirmed
# live during the #4526 incident: `gh issue/pr list --json ...` failed with a
# rate-limit error while `gh api repos/OWNER/REPO/...` succeeded), so this
# class of error -- and only this class -- justifies retrying the same query
# via REST instead of giving up.
#
# The signature table below mirrors loom-daemon/src/rate_limit_breaker.rs's
# RATE_LIMIT_SIGNATURES, which is this repo's tested ground truth for what
# `gh` actually prints. Two phrasings matter and they are NOT substrings of
# each other:
#
#   GraphQL (`gh issue list` / `gh pr list`, i.e. every call in this script):
#     "GraphQL: API rate limit already exceeded for user ID ..."
#   REST (`gh api ...`, i.e. the fallbacks below):
#     "HTTP 403: API rate limit exceeded for ..."
#
# The word "already" breaks the contiguous "API rate limit exceeded" match, so
# a single-pattern check misses the GraphQL form -- which is precisely the
# form #4526 is about. Matching is case-insensitive, as in the Rust original.
is_rate_limit_error() {
    local text
    text=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
    case "$text" in
        *"api rate limit exceeded"*) return 0 ;;
        *"api rate limit already exceeded"*) return 0 ;;
        *"secondary rate limit"*) return 0 ;;
        *"abuse detection mechanism"*) return 0 ;;
        *"was submitted too quickly"*) return 0 ;;
    esac
    return 1
}

# Resolve "owner/repo" from the local git remote, for the small number of
# call sites (search_cross_references() below) that need the literal string
# rather than just an API endpoint to hit. Deliberately NOT `gh repo view`
# (#4659): that call is itself GraphQL-backed, so under GraphQL exhaustion it
# fails before any REST fallback is even attempted -- the exact bug this
# helper exists to avoid reintroducing. `git remote get-url origin` is a pure
# local read (no network round-trip at all), so it survives total GraphQL AND
# REST outages alike.
#
# The three REST-fallback call sites in search_similar_issues() /
# search_merged_prs() / search_closed_issues() do NOT call this helper --
# they hit `gh api "repos/{owner}/{repo}/..."` directly, letting `gh` resolve
# the `{owner}/{repo}` placeholder locally from the same git remote with zero
# API calls of its own (#4659).
get_repo_nwo() {
    local url
    if ! url=$(git remote get-url origin 2>&1); then
        echo "$url" >&2
        return 1
    fi
    # Strip a trailing ".git" and/or "/", then take the final two "/"- or
    # ":"-delimited path segments -- "owner/repo" -- which works for both the
    # SSH ("git@host:owner/repo.git") and HTTPS ("https://host/owner/repo")
    # remote URL forms.
    url="${url%.git}"
    url="${url%/}"
    local nwo
    nwo=$(printf '%s' "$url" | grep -oE '[^/:]+/[^/]+$') || true
    if [[ -z "$nwo" ]]; then
        echo "could not parse owner/repo from remote URL: $url" >&2
        return 1
    fi
    echo "$nwo"
}

# Search for similar issues. The fetch (with its #4526 REST fallback) stays
# here; the scan is `loom-daemon duplicate-scan` (#8360), exec'd through
# script-helper's standard resolution so the subcommand's stdout/exit code
# reach this function's caller unchanged.
search_similar_issues() {
    local title="$1"
    local body="${2:-}"
    local threshold="${3:-18}"
    local self_issue="${4:-}"
    # Empty (or 0, or >= threshold) disables the near-match band (#8289).
    local warn_threshold="${5:-}"

    # Search open issues. On a GraphQL rate-limit failure, retry via REST
    # (an independent quota, #4526) before giving up.
    local issues
    local rest_fallback=false
    if ! issues=$($FORGE issue list --state=open --limit=50 --json number,title,body 2>&1); then
        if is_rate_limit_error "$issues"; then
            # `gh api` resolves the "{owner}/{repo}" placeholder locally from
            # the git remote -- no GraphQL (or any) API call, unlike the
            # get_repo_nwo()-via-`gh repo view` round-trip this used to make
            # (#4659), which itself failed under GraphQL exhaustion before
            # REST was ever attempted.
            local rest_issues
            if rest_issues=$(gh api "repos/{owner}/{repo}/issues?state=open&per_page=50" 2>&1); then
                # REST's /issues endpoint also returns PRs; exclude them.
                issues=$(echo "$rest_issues" | jq -c '[.[] | select(.pull_request == null)]')
                rest_fallback=true
            else
                print_error "GraphQL rate-limited fetching open issues, and REST fallback also failed: ${rest_issues}"
                return 2
            fi
        else
            print_error "Failed to fetch issues: $issues"
            return 2
        fi
    fi

    local scan_args=(--pool open-issues --title "$title" --body "$body" --threshold "$threshold")
    [[ -n "$self_issue" ]] && scan_args+=(--self-issue "$self_issue")
    if [[ -n "$warn_threshold" ]] && (( warn_threshold > 0 )) && (( warn_threshold < threshold )); then
        scan_args+=(--warn-threshold "$warn_threshold")
    fi
    # --near-file is passed UNCONDITIONALLY, not only with --warn-threshold
    # (#8591): the scan also uses that channel to report a candidate DEMOTED
    # out of the block band for want of title corroboration. Gating the file
    # on the opt-in warn band would make those demotions vanish instead of
    # merely stop blocking -- strictly worse than the false positive.
    [[ -n "${NEAR_MATCH_FILE:-}" ]] && scan_args+=(--near-file "${NEAR_MATCH_FILE}")
    if $rest_fallback; then
        scan_args+=(--rest-fallback)
    fi
    loom_exec_script_helper duplicate-scan "${scan_args[@]}" <<< "$issues"
}

# Search for similar recently merged PRs. Fetch here, scan in the daemon
# requires-daemon: duplicate-scan >= 0.19.224  #8412 — the scan port; without it the helper exits 2 (pool incomplete) and create-issue fails open
# (#8360) -- same split as search_similar_issues().
search_merged_prs() {
    local title="$1"
    local body="${2:-}"
    local threshold="${3:-18}"
    local self_issue="${4:-}"

    # Search recently merged PRs. On a GraphQL rate-limit failure, retry via
    # REST (#4526). REST has no `state=merged` filter, so fetch closed PRs
    # and filter to actually-merged ones locally.
    local prs
    local rest_fallback=false
    if ! prs=$($FORGE pr list --state=merged --limit=20 --json number,title,body 2>&1); then
        if is_rate_limit_error "$prs"; then
            # See the matching comment in search_similar_issues(): `gh api`
            # resolves "{owner}/{repo}" locally, no GraphQL call (#4659).
            local rest_prs
            if rest_prs=$(gh api "repos/{owner}/{repo}/pulls?state=closed&per_page=20" 2>&1); then
                prs=$(echo "$rest_prs" | jq -c '[.[] | select(.merged_at != null)]')
                rest_fallback=true
            else
                print_error "GraphQL rate-limited fetching merged PRs, and REST fallback also failed: ${rest_prs}"
                return 2
            fi
        else
            print_warning "Failed to fetch merged PRs: $prs"
            return 0
        fi
    fi

    local scan_args=(--pool merged-prs --title "$title" --body "$body" --threshold "$threshold")
    [[ -n "$self_issue" ]] && scan_args+=(--self-issue "$self_issue")
    if $rest_fallback; then
        scan_args+=(--rest-fallback)
    fi
    loom_exec_script_helper duplicate-scan "${scan_args[@]}" <<< "$prs"
}

# Search for similar recently closed issues. Fetch here, scan in the daemon
# (#8360) -- same split as search_similar_issues().
search_closed_issues() {
    local title="$1"
    local body="${2:-}"
    local threshold="${3:-18}"
    local self_issue="${4:-}"

    # Search recently closed issues. On a GraphQL rate-limit failure, retry
    # via REST (#4526).
    local issues
    local rest_fallback=false
    if ! issues=$($FORGE issue list --state=closed --limit=20 --json number,title,body 2>&1); then
        if is_rate_limit_error "$issues"; then
            # See the matching comment in search_similar_issues(): `gh api`
            # resolves "{owner}/{repo}" locally, no GraphQL call (#4659).
            local rest_issues
            if rest_issues=$(gh api "repos/{owner}/{repo}/issues?state=closed&per_page=20" 2>&1); then
                # REST's /issues endpoint also returns PRs; exclude them.
                issues=$(echo "$rest_issues" | jq -c '[.[] | select(.pull_request == null)]')
                rest_fallback=true
            else
                print_error "GraphQL rate-limited fetching closed issues, and REST fallback also failed: ${rest_issues}"
                return 2
            fi
        else
            print_warning "Failed to fetch closed issues: $issues"
            return 0
        fi
    fi

    local scan_args=(--pool closed-issues --title "$title" --body "$body" --threshold "$threshold")
    [[ -n "$self_issue" ]] && scan_args+=(--self-issue "$self_issue")
    if $rest_fallback; then
        scan_args+=(--rest-fallback)
    fi
    loom_exec_script_helper duplicate-scan "${scan_args[@]}" <<< "$issues"
}

# Probe for OPEN issues/PRs whose bodies or comments cross-reference the given
# issue number (issue #4162). This answers a different question than
# duplicate detection above: not "is this the same issue" but "is there open
# work that already argues for a different/changed spec for this issue" --
# e.g. an open issue that critiques/rewrites #N's acceptance criteria via a
# cross-reference in its body. Uses GitHub's timeline API (the same
# `cross-referenced` event already used for PR detection in
# .claude/commands/loom/sweep.md's existing-PR probe), which surfaces every
# `#N` mention regardless of similarity -- no local text/keyword matching.
#
# GitHub-specific. Gracefully degrades (stderr warning, empty result, does
# NOT fail the whole duplicate check) when `gh` is missing, the repo can't be
# resolved (e.g. a Gitea remote `gh` doesn't recognize), or the API call
# fails -- same pattern as search_merged_prs() above.
#
# Outputs a JSON array of {number, title, is_pr} for OPEN, same-repo,
# non-self cross-references, deduped by number. Emits "[]" on any failure.
search_cross_references() {
    local issue_num="$1"

    if ! command -v gh &>/dev/null; then
        print_warning "gh CLI not found; skipping cross-reference probe for #${issue_num}"
        echo "[]"
        return 0
    fi

    # Resolved from the local git remote (#4659), not `gh repo view` -- the
    # literal "owner/repo" string is only needed below to filter the timeline
    # to same-repo cross-references; it costs no API call either way.
    local repo_nwo
    if ! repo_nwo=$(get_repo_nwo 2>&1); then
        print_warning "Failed to resolve repository for cross-reference probe (non-GitHub forge, or no git remote?): $repo_nwo"
        echo "[]"
        return 0
    fi

    local timeline
    if ! timeline=$(gh api "repos/${repo_nwo}/issues/${issue_num}/timeline" --paginate 2>&1); then
        print_warning "Failed to fetch timeline for #${issue_num}: $timeline"
        echo "[]"
        return 0
    fi

    echo "$timeline" | jq -c --arg repo "$repo_nwo" --argjson self "$issue_num" '
        [.[] | select(.event == "cross-referenced"
                       and .source.issue != null
                       and (.source.issue.repository.full_name // "") == $repo
                       and .source.issue.number != $self
                       and .source.issue.state == "open")
         | {number: .source.issue.number,
            title: .source.issue.title,
            is_pr: (.source.issue.pull_request != null)}]
        | unique_by(.number)
    ' 2>/dev/null || echo "[]"
}

# Format a RELATED_OPEN_WORK JSON array (from search_cross_references) into
# the human-readable lines used by the text-output mode.
format_related_open_work() {
    local issue_num="$1"
    local json_array="$2"

    echo "$json_array" | jq -r --arg n "$issue_num" '
        .[] | (if .is_pr then "PR #" else "#" end) + (.number | tostring) + ": " + .title +
              (if .is_pr then " (open PR, cross-references #" else " (open issue, cross-references #" end) + $n + ")"
    '
}

# Main function
main() {
    local title=""
    local body=""
    local threshold=18
    local warn_threshold=""
    local json_output=false
    local include_merged_prs=false
    local issue=""

    # Parse arguments. `end_of_options` tracks whether a bare "--" has been
    # seen: once set, EVERY remaining argument is consumed as positional text
    # (title, then body), verbatim, regardless of a leading "-"/"--" (#5898).
    # Without this, a title like "--strategy basic is ignored" passed
    # positionally falls into the `-*)` unknown-option branch below and is
    # rejected as a flag instead of being read as the title.
    local end_of_options=false
    while [[ $# -gt 0 ]]; do
        if ! $end_of_options; then
            case "$1" in
                --)
                    end_of_options=true
                    shift
                    continue
                    ;;
                --help|-h)
                    print_help
                    exit 0
                    ;;
                --title)
                    shift
                    title="$1"
                    ;;
                --body)
                    shift
                    body="$1"
                    ;;
                --threshold)
                    shift
                    threshold="$1"
                    ;;
                --warn-threshold)
                    shift
                    warn_threshold="$1"
                    ;;
                --include-merged-prs)
                    include_merged_prs=true
                    ;;
                --issue)
                    shift
                    issue="$1"
                    ;;
                --json)
                    json_output=true
                    ;;
                -*)
                    print_error "Unknown option: $1"
                    print_help >&2
                    exit 2
                    ;;
                *)
                    # Positional arguments: first is title, second is body
                    if [[ -z "$title" ]]; then
                        title="$1"
                    elif [[ -z "$body" ]]; then
                        body="$1"
                    else
                        print_error "Too many arguments"
                        print_help >&2
                        exit 2
                    fi
                    ;;
            esac
        else
            # Past "--": always positional, never re-parsed as an option --
            # this is what lets a leading-dash title/body round-trip.
            if [[ -z "$title" ]]; then
                title="$1"
            elif [[ -z "$body" ]]; then
                body="$1"
            else
                print_error "Too many arguments"
                print_help >&2
                exit 2
            fi
        fi
        shift
    done

    # Validate required arguments
    if [[ -z "$title" ]]; then
        print_error "Issue title is required"
        print_help >&2
        exit 2
    fi

    # Validate threshold is a number
    if ! [[ "$threshold" =~ ^[0-9]+$ ]]; then
        print_error "Threshold must be a number"
        exit 2
    fi

    # Validate --warn-threshold, when given (#8289). A value at/above
    # --threshold cannot describe a band BELOW it, so it disables the band
    # rather than silently reinterpreting the caller's intent.
    if [[ -n "$warn_threshold" ]]; then
        if ! [[ "$warn_threshold" =~ ^[0-9]+$ ]]; then
            print_error "Warn threshold must be a number"
            exit 2
        fi
        if (( warn_threshold >= threshold )); then
            print_warning "--warn-threshold ${warn_threshold} is not below --threshold ${threshold}; near-match band disabled"
            warn_threshold=""
        fi
    fi

    # Validate --issue is a number, when given
    if [[ -n "$issue" ]] && ! [[ "$issue" =~ ^[0-9]+$ ]]; then
        print_error "--issue must be a number"
        exit 2
    fi

    # Check for forge CLI. $FORGE may be two words ("loom-daemon forge"), so
    # probe only the binary name (its first word).
    if ! command -v "${FORGE%% *}" &> /dev/null; then
        print_error "$FORGE CLI not found. Please install loom-daemon or GitHub CLI."
        exit 2
    fi

    # Check forge authentication
    if ! $FORGE auth status &> /dev/null; then
        print_error "Not authenticated with forge. Run 'gh auth login' (GitHub) or set GITEA_TOKEN (Gitea)."
        exit 2
    fi

    # Verdict tracking (#4526). "A duplicate was found" and "a search pool
    # could not be checked at all" are INDEPENDENT facts, and each of the
    # three pools (open issues / merged PRs / closed issues) can report them
    # separately -- partial rate-limiting across pools is the exact scenario
    # this fallback exists for. Keeping the two facts in separate variables
    # (instead of overwriting one exit code as each pool reports in) is what
    # stops a total failure in one pool from discarding a confirmed match
    # found by another: `exit_code` is derived from both, once, below.
    #
    #   duplicate_found  -> any pool produced a match/NON_DISCRIMINATIVE line
    #   incomplete_pools -> human-readable list of pools where GraphQL was
    #                       rate-limited AND the REST fallback also failed
    #   fallback_pools   -> pools that were answered by the REST fallback
    #
    # Comma-joined strings rather than arrays: this script runs under
    # `set -u` on bash 3.2 (macOS system bash), where empty-array expansion
    # is a portability landmine.
    local duplicate_found=false
    local incomplete_pools=""
    local fallback_pools=""
    local header_labeled_rest=false

    # Near-match side channel (#8289). Allocated on EVERY invocation since
    # #8591: the band is still opt-in, but a title-corroboration demotion
    # rides the same channel and is not. Inherited by the
    # command-substitution subshell below without export (same process tree);
    # the daemon writes it only when there is at least one row to report, so
    # -s is the "there is context to show" signal.
    NEAR_MATCH_FILE=$(mktemp) || NEAR_MATCH_FILE=""
    # shellcheck disable=SC2064 # expand NEAR_MATCH_FILE now, not at exit
    [[ -n "$NEAR_MATCH_FILE" ]] && trap 'rm -f "'"$NEAR_MATCH_FILE"'"' EXIT

    # Search for similar issues
    local result
    local exit_code=0
    result=$(search_similar_issues "$title" "$body" "$threshold" "$issue" "$warn_threshold") || exit_code=$?
    if [[ $exit_code -eq 1 ]]; then
        duplicate_found=true
    elif [[ $exit_code -eq 2 ]]; then
        incomplete_pools="open issues"
    fi

    # If --include-merged-prs, also search merged PRs and closed issues
    local merged_result=""
    local closed_result=""
    if $include_merged_prs; then
        local merged_exit_code=0
        local closed_exit_code=0
        merged_result=$(search_merged_prs "$title" "$body" "$threshold" "$issue") || merged_exit_code=$?
        closed_result=$(search_closed_issues "$title" "$body" "$threshold" "$issue") || closed_exit_code=$?

        # #4526: a GraphQL rate-limit that ALSO fails via REST means this
        # pool genuinely could not be checked. Record it; the final verdict
        # below turns that into exit 2 ("could not run at all") rather than
        # silently reporting "no duplicates" the way a plain `return 0` used
        # to -- but only when no other pool found a real duplicate.
        if [[ $merged_exit_code -eq 2 ]]; then
            incomplete_pools="${incomplete_pools:+${incomplete_pools}, }merged PRs"
        fi
        if [[ $closed_exit_code -eq 2 ]]; then
            incomplete_pools="${incomplete_pools:+${incomplete_pools}, }closed issues"
        fi

        # Strip the leading REST-fallback sentinel line (if present) from
        # each result before it's displayed or aggregated, remembering
        # whether it fired so the umbrella DUPLICATE_FOUND header below can
        # be labeled correctly (#4526).
        local merged_rest_fallback=false
        local closed_rest_fallback=false
        if [[ "$merged_result" == "RATE_LIMIT_FALLBACK"$'\n'* ]]; then
            merged_rest_fallback=true
            merged_result="${merged_result#RATE_LIMIT_FALLBACK$'\n'}"
        fi
        if [[ "$closed_result" == "RATE_LIMIT_FALLBACK"$'\n'* ]]; then
            closed_rest_fallback=true
            closed_result="${closed_result#RATE_LIMIT_FALLBACK$'\n'}"
        fi
        if $merged_rest_fallback; then
            fallback_pools="${fallback_pools:+${fallback_pools}, }merged PRs"
        fi
        if $closed_rest_fallback; then
            fallback_pools="${fallback_pools:+${fallback_pools}, }closed issues"
        fi

        # If we found matches in merged PRs or closed issues, flag as duplicate
        if [[ -n "$merged_result" || -n "$closed_result" ]]; then
            if ! $duplicate_found; then
                # The open-issues search produced no match of its own (either
                # it found nothing, or it could not run at all -- in both
                # cases $result is empty here, so overwriting it is safe), but
                # merged/closed matches exist. Only synthesize a
                # "DUPLICATE_FOUND" umbrella header when at least one of
                # merged/closed is a real match list -- a degenerate (#4409)
                # result already self-announces with its own
                # "NON_DISCRIMINATIVE (...)" line, and prefixing THAT with
                # "DUPLICATE_FOUND" would be misleading. Check each side
                # independently (not "both must be non-degenerate") so a real
                # match on one side still gets its header even when the other
                # side is degenerate.
                if [[ ( -n "$merged_result" && "$merged_result" != NON_DISCRIMINATIVE* ) || \
                      ( -n "$closed_result" && "$closed_result" != NON_DISCRIMINATIVE* ) ]]; then
                    if [[ -n "$fallback_pools" ]]; then
                        result="DUPLICATE_FOUND (REST fallback -- similarity ranking basis differs from GraphQL)"$'\n'
                        header_labeled_rest=true
                    else
                        result="DUPLICATE_FOUND"$'\n'
                    fi
                fi
                duplicate_found=true
            fi
            # Re-establish the line separator before appending another pool's
            # list. `result=$(search_similar_issues ...)` came back through a
            # command substitution, which STRIPS the trailing newline, so
            # appending directly splices the last open-issue line and the
            # first merged-PR line into one line -- which the --json parser
            # then reads as a single bogus match (the second entry's number
            # lost, its title swallowed into the first entry's title).
            if [[ -n "$result" && "$result" != *$'\n' ]]; then
                result+=$'\n'
            fi
            if [[ -n "$merged_result" ]]; then
                result+="$merged_result"$'\n'
            fi
            if [[ -n "$closed_result" ]]; then
                result+="$closed_result"$'\n'
            fi
        fi
    fi

    # Degraded-coverage notes (#4526). Appended as their own marker lines so a
    # Curator reading text output learns WHY a match list may be partial or
    # differently ranked, and so --json can surface it as a field. Both are
    # skipped by the --json line parser below.
    #
    # SEARCH_INCOMPLETE only appears alongside a real match: with no match the
    # verdict is exit 2 and the per-pool stderr errors already say what broke.
    if [[ -n "$result" && "$result" != *$'\n' ]] && \
       { [[ -n "$incomplete_pools" ]] || [[ -n "$fallback_pools" ]]; } && $duplicate_found; then
        result+=$'\n'
    fi
    if [[ -n "$incomplete_pools" ]] && $duplicate_found; then
        result+="SEARCH_INCOMPLETE: ${incomplete_pools} -- GraphQL rate-limited and REST fallback also failed; matches below are partial"$'\n'
    fi
    # REST_FALLBACK covers the interleaving the umbrella header above cannot:
    # the open-issues search already matched via ordinary GraphQL (so its own
    # plain "DUPLICATE_FOUND" header stands) while a LATER pool fell back to
    # REST. Without this, that REST-sourced subset would be reported with no
    # indication its similarity ranking basis differs.
    if [[ -n "$fallback_pools" ]] && ! $header_labeled_rest && $duplicate_found; then
        result+="REST_FALLBACK: ${fallback_pools} -- answered via REST; similarity ranking basis differs from GraphQL"$'\n'
    fi

    # Final verdict (#4526): a confirmed duplicate ALWAYS outranks incomplete
    # coverage. Reporting exit 2 when some other pool did find a match would
    # discard the actionable answer -- and in --json mode the exit-2 branch
    # below emits a bare {"error": ...} with no `matches` array at all, so the
    # match would vanish entirely rather than merely be mis-coded.
    if $duplicate_found; then
        exit_code=1
    elif [[ -n "$incomplete_pools" ]]; then
        exit_code=2
    else
        exit_code=0
    fi

    # Cross-reference probe (--issue N only, issue #4162). Distinct from
    # duplicate detection above: surfaces OPEN issues/PRs that already
    # cross-reference N, as "related open work" the Curator must read. Skip
    # entirely when the base similarity check already hard-failed (exit 2) --
    # no point probing on top of an already-broken forge call.
    local related_json="[]"
    local related_count=0
    if [[ -n "$issue" && $exit_code -ne 2 ]]; then
        related_json=$(search_cross_references "$issue")
        related_count=$(echo "$related_json" | jq 'length' 2>/dev/null || echo 0)
        if [[ "$related_count" -gt 0 ]]; then
            exit_code=1
        fi
    fi

    # Near-match band (#8289): context only. Read here so both output modes
    # below can surface it regardless of the verdict -- including exit 0,
    # which is the whole point (a sub-threshold match used to be invisible).
    # Text rows and the --json field are rendered from the same JSON array
    # the daemon wrote, so the two modes cannot drift apart.
    local near_output=""
    local near_json="[]"
    if [[ -n "$NEAR_MATCH_FILE" && -s "$NEAR_MATCH_FILE" ]]; then
        near_json=$(jq -c 'map(. + {type: "near_match"})' "$NEAR_MATCH_FILE" 2>/dev/null || echo '[]')
        # The header no longer quotes the band arithmetic: since #8591 a row
        # here can ALSO be a candidate demoted out of the block band for want
        # of title corroboration, whose score is at/above --threshold. Each
        # row carries its own score, and a demoted one names the title
        # overlap that fell short, so the numbers are still all present.
        # The bar a demoted row failed is a daemon-side constant, so it is NOT
        # restated here -- one definition, in duplicate_scan.rs, not two that
        # can drift.
        near_output=$(jq -r \
            '"NEAR_DUPLICATE (context only, not a duplicate verdict)", (.[] | "NEAR #\(.number): \(.title) (similarity: \(.similarity)%" + (if .title_similarity then ", title overlap only \(.title_similarity)% -- not corroborated, so not a block" else "" end) + ")")' \
            "$NEAR_MATCH_FILE" 2>/dev/null || true)
    fi

    if $json_output; then
        if [[ $exit_code -eq 2 ]]; then
            echo '{"error": "Failed to check duplicates"}'
        else
            # Degenerate-result flag (#4409): true when any search's
            # candidate pool self-detected as non-discriminative (>50% of
            # scanned candidates over threshold). Surfaced separately from
            # `matches` so callers can distinguish a real duplicate list from
            # noise instead of silently misreading one as the other.
            local degenerate=false
            if echo "$result" | grep -q '^NON_DISCRIMINATIVE'; then
                degenerate=true
            fi

            # Degraded-coverage flag (#4526): true when at least one search
            # pool could not be checked at all (GraphQL rate-limited and its
            # REST fallback also failed) while another pool still produced
            # matches. Surfaced separately from `matches` so a caller can tell
            # "no other duplicates exist" from "we could not look everywhere".
            local search_incomplete=false
            if [[ -n "$incomplete_pools" ]]; then
                search_incomplete=true
            fi

            # Parse duplicates into JSON (unconditional -- harmless no-op on
            # an empty $result, e.g. exit_code 0 or "related work only")
            local matches="[]"
            while IFS= read -r line; do
                # Prefix match, not exact-equals: the REST-fallback path
                # (#4526) emits "DUPLICATE_FOUND (REST fallback -- ...)".
                [[ "$line" == DUPLICATE_FOUND* ]] && continue
                [[ "$line" == NON_DISCRIMINATIVE* ]] && continue
                # Degraded-coverage marker lines (#4526) -- reported via the
                # `search_incomplete` field, never as a match.
                [[ "$line" == SEARCH_INCOMPLETE:* ]] && continue
                [[ "$line" == REST_FALLBACK:* ]] && continue
                [[ -z "$line" ]] && continue

                # Parse "#123: Title (similarity: 75%)" or "PR #123: Title (similarity: 75%)"
                # or "Closed #123: Title (similarity: 75%)"
                local num title_part sim match_type
                if [[ "$line" == PR\ * ]]; then
                    match_type="pr"
                    num=$(echo "$line" | sed -n 's/^PR #\([0-9]*\):.*/\1/p')
                    title_part=$(echo "$line" | sed -n 's/^PR #[0-9]*: \(.*\) (similarity:.*/\1/p')
                elif [[ "$line" == Closed\ * ]]; then
                    match_type="closed_issue"
                    num=$(echo "$line" | sed -n 's/^Closed #\([0-9]*\):.*/\1/p')
                    title_part=$(echo "$line" | sed -n 's/^Closed #[0-9]*: \(.*\) (similarity:.*/\1/p')
                else
                    match_type="issue"
                    num=$(echo "$line" | sed -n 's/^#\([0-9]*\):.*/\1/p')
                    title_part=$(echo "$line" | sed -n 's/^#[0-9]*: \(.*\) (similarity:.*/\1/p')
                fi
                sim=$(echo "$line" | sed -n 's/.*(similarity: \([0-9]*\)%).*/\1/p')

                if [[ -n "$num" ]]; then
                    matches=$(echo "$matches" | jq --arg n "$num" --arg t "$title_part" --arg s "$sim" --arg type "$match_type" \
                        '. + [{"number": ($n | tonumber), "title": $t, "similarity": ($s | tonumber), "type": $type}]')
                fi
            done <<< "$result"

            if [[ "$related_count" -gt 0 ]]; then
                local cross_matches
                cross_matches=$(echo "$related_json" | jq '[.[] | {number, title, similarity: 0, type: "cross_reference"}]')
                matches=$(echo "$matches" | jq --argjson extra "$cross_matches" '. + $extra')
            fi

            # Near matches ride their own field (#8289), NEVER folded into
            # `matches` and never reflected in `duplicate_found` -- those two
            # stay driven by --threshold alone, so a caller that ignores this
            # field behaves exactly as it did before the band existed.
            if [[ "$matches" == "[]" ]]; then
                echo "{\"duplicate_found\": false, \"degenerate\": $degenerate, \"search_incomplete\": $search_incomplete, \"matches\": [], \"near_matches\": $near_json}"
            else
                echo "{\"duplicate_found\": true, \"degenerate\": $degenerate, \"search_incomplete\": $search_incomplete, \"matches\": $matches, \"near_matches\": $near_json}"
            fi
        fi
    else
        if [[ $exit_code -eq 0 ]]; then
            # Near matches print even here -- exit 0 with a silent
            # sub-threshold match is the gap #8289 exists to close.
            [[ -n "$near_output" ]] && echo "$near_output"
            print_success "No duplicates found"
        else
            echo "$result"
            if [[ "$related_count" -gt 0 ]]; then
                echo "RELATED_OPEN_WORK"
                format_related_open_work "$issue" "$related_json"
            fi
            [[ -n "$near_output" ]] && echo "$near_output"
        fi
    fi

    exit $exit_code
}

main "$@"
