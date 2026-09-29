#!/usr/bin/env bash
# lib/worktree-foreign-lease-guard.sh
#
# #5783: refuse to silently hand back a shared, possibly co-occupied
# worktree when an issue carries more than one simultaneously-FRESH sweep
# lease record.
#
# ## Why this exists
#
# Issue #5781 was concurrently claimed by (at least) four independent
# `/loom:sweep` runs on the SAME host within a ~30 minute window. Two of
# them dispatched Builders into the shared `.loom/worktrees/issue-5781`
# worktree at the same time -- `worktree.sh N` always resolves to the same
# path regardless of which sweep asks for it, and its "worktree already
# exists, has uncommitted changes -> preserve" fast path (see the call site
# in worktree.sh) handed the directory back unconditionally, with no check
# for whether a live peer might still be mid-edit in it. One Builder's
# in-progress edit leaked into the other's pushed commit (PR #5782).
#
# `sweep-lease-publish.sh` (#5783 AC1) closes most of this at the SOURCE --
# a sweep that would collide with an already-fresh lease on the same issue
# now fails to publish its own (exit 4) and is expected to skip the issue
# before ever touching a worktree. This file is the worktree-layer backstop
# for the paths that fix does not cover: a lease written by `loom-daemon`'s
# own dispatch-time claim (a Rust binary outside this repo, #6179), a
# `worktree.sh N` invocation that happens outside the `/loom:sweep` lifecycle
# (no Step 1b lease was ever published for this call), or a narrow
# publish/worktree-entry race window.
#
# ## What it checks, and why "2+ live leases" rather than "self vs. foreign"
#
# `worktree.sh` has no reliable way to know its OWN sweep identity --
# `$LOOM_SWEEP_RUN_ID` / `$RUN_ID` are conversation-local shell variables of
# the orchestrating `/loom:sweep` prose session; a Builder subagent's own
# tool-call shells do not inherit them, and a manual/human `worktree.sh N`
# invocation (as this repo's Builder role itself documents) has no sweep
# identity at all. So this guard does not attempt a self/foreign identity
# comparison -- it looks for the one signal that is unambiguous regardless
# of who "self" is: TWO OR MORE DISTINCT (host, sweep) pairs each holding a
# still-fresh, non-yielded lease on the same issue at once. That state is
# never legitimate -- at most one live worker should ever hold a fresh lease
# on one issue -- and it is exactly the signature #5781 exhibited (5 lease
# records, several mutually fresh).
#
# ## Fail-open, like every other forge probe in this subsystem
#
# No `gh`/`jq` on PATH, a `gh api` read failure, or zero/one live lease all
# return 0 ("nothing to warn about, proceed") -- absence of evidence is not
# evidence of a peer (`defaults/docs/lease-record.md`'s reader contract).
# Only an actually-observed 2+-live-lease state returns 1.
#
# ## Commands
#
#   _worktree_foreign_lease_guard <issue>
#     Returns 0 when there is no evidence of more than one simultaneously
#     live lease on <issue> (including every fail-open case above). Returns
#     1, after printing a `print_warning` line per live lease found, when 2
#     or more distinct (host, sweep) pairs hold a fresh lease at once.
#
# Depends on `print_warning` (falls back to a plain stderr echo when not
# already defined, so this file also works sourced standalone / under test).

if ! declare -F print_warning >/dev/null 2>&1; then
    print_warning() { echo "WARNING: $1" >&2; }
fi

_WFLG_LEASE_MARKER_PREFIX="<!-- loom:lease host="
_WFLG_YIELD_MARKER_PREFIX="<!-- loom:lease-yield host="

# --- Repo-relative `gh` targeting (mirrors sweep-lease-publish.sh) ----------
_wflg_gh_repo_args() {
    if [[ -n "${LOOM_REPO:-}" ]]; then
        printf -- '-R\n%s\n' "$LOOM_REPO"
    fi
}

# --- ISO-8601 -> epoch (portable across GNU and BSD/macOS date) ------------
_wflg_iso_to_epoch() {
    local ts="$1" out
    out="$(date -u -d "$ts" +%s 2>/dev/null)" && [[ "$out" =~ ^[0-9]+$ ]] && {
        echo "$out"
        return 0
    }
    out="$(date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$ts" +%s 2>/dev/null)" && [[ "$out" =~ ^[0-9]+$ ]] && {
        echo "$out"
        return 0
    }
    return 1
}

# --- Parse `host=`/`sweep=` out of a lease marker's literal first line -----
_wflg_parse_lease_marker_line() {
    local first_line="$1" rest host sweep_id
    rest="${first_line#"$_WFLG_LEASE_MARKER_PREFIX"}"
    [[ "$rest" == "$first_line" ]] && return 1
    [[ "$rest" == *" -->" ]] || return 1
    rest="${rest% -->}"
    case "$rest" in
        *" sweep="*)
            host="${rest%% sweep=*}"
            sweep_id="${rest#* sweep=}"
            ;;
        *)
            return 1
            ;;
    esac
    [[ -n "$host" && -n "$sweep_id" ]] || return 1
    printf '%s\t%s' "$host" "$sweep_id"
}

# --- Parse `host=`/`sweep=` out of a lease-YIELD marker's literal first
# line: "host=<H> sweep=<S> earliest_host=<EH> earliest_sweep=<ES> -->" ----
_wflg_parse_lease_yield_marker_line() {
    local first_line="$1" rest host sweep_id
    rest="${first_line#"$_WFLG_YIELD_MARKER_PREFIX"}"
    [[ "$rest" == "$first_line" ]] && return 1
    case "$rest" in
        *" sweep="*)
            host="${rest%% sweep=*}"
            sweep_id="${rest#* sweep=}"
            sweep_id="${sweep_id%% earliest_host=*}"
            sweep_id="${sweep_id% }"
            ;;
        *)
            return 1
            ;;
    esac
    [[ -n "$host" && -n "$sweep_id" ]] || return 1
    printf '%s\t%s' "$host" "$sweep_id"
}

# _worktree_foreign_lease_guard <issue> -- see file header.
_worktree_foreign_lease_guard() {
    local issue="$1"
    command -v gh >/dev/null 2>&1 || return 0
    command -v jq >/dev/null 2>&1 || return 0

    local -a repo_args=()
    local r
    while IFS= read -r r; do
        [[ -n "$r" ]] && repo_args+=("$r")
    done < <(_wflg_gh_repo_args)

    # worktree.sh sits on the hot path of EVERY Builder dispatch, so this
    # read must never be allowed to block it for long -- a bounded timeout
    # (default 10s, `LOOM_WORKTREE_LEASE_GUARD_TIMEOUT` to override) keeps a
    # slow/rate-limited `gh api` round trip from stalling worktree creation
    # under exactly the kind of heavy concurrent-sweep host contention this
    # guard exists to help with (#5783). A missing `timeout`/`gtimeout`
    # binary degrades to the old unbounded call, matching
    # check-main-freshness.sh's identical fallback pattern.
    local -a _wflg_timeout_cmd=()
    if command -v timeout > /dev/null 2>&1; then
        _wflg_timeout_cmd=(timeout "${LOOM_WORKTREE_LEASE_GUARD_TIMEOUT:-10}")
    elif command -v gtimeout > /dev/null 2>&1; then
        _wflg_timeout_cmd=(gtimeout "${LOOM_WORKTREE_LEASE_GUARD_TIMEOUT:-10}")
    fi

    local comments_ndjson
    if ! comments_ndjson="$("${_wflg_timeout_cmd[@]+"${_wflg_timeout_cmd[@]}"}" gh api "${repo_args[@]+"${repo_args[@]}"}" \
        "repos/{owner}/{repo}/issues/${issue}/comments" --paginate --jq \
        ".[] | select(.body != null and ((.body | startswith(\"${_WFLG_LEASE_MARKER_PREFIX}\")) or (.body | startswith(\"${_WFLG_YIELD_MARKER_PREFIX}\")))) | {updated_at: .updated_at, body: .body}" \
        2>/dev/null)"; then
        return 0 # fail open: a read failure (including a timeout) is not evidence of a peer
    fi
    [[ -n "$(printf '%s' "$comments_ndjson" | tr -d '[:space:]')" ]] || return 0

    # Yield-exclusion (Issue #5331/#6485): a lease whose own (host, sweep)
    # has a later loom:lease-yield record has already stood down and is
    # never counted as live.
    local yield_ndjson yield_first_lines=""
    yield_ndjson="$(jq -c --arg p "$_WFLG_YIELD_MARKER_PREFIX" 'select(.body != null and (.body | startswith($p)))' <<< "$comments_ndjson" 2>/dev/null || true)"
    if [[ -n "$(printf '%s' "$yield_ndjson" | tr -d '[:space:]')" ]]; then
        yield_first_lines="$(jq -r '.body | split("\n")[0]' <<< "$yield_ndjson" 2>/dev/null || true)"
    fi

    local now_epoch ttl_seconds ttl_minutes="${LOOM_LEASE_TTL_MINUTES:-15}"
    now_epoch="${LOOM_LEASE_PUBLISH_NOW:-$(date -u +%s)}"
    ttl_seconds="$(awk -v m="$ttl_minutes" 'BEGIN { printf "%d", m * 60 }')"

    local -A live_pairs=()
    local comment_line
    while IFS= read -r comment_line; do
        [[ -z "$comment_line" ]] && continue
        local c_updated_at c_body c_first_line c_parsed c_host c_sweep
        c_updated_at="$(jq -r '.updated_at // empty' <<< "$comment_line" 2>/dev/null || true)"
        c_body="$(jq -r '.body // empty' <<< "$comment_line" 2>/dev/null || true)"
        [[ -z "$c_updated_at" || -z "$c_body" ]] && continue
        c_first_line="${c_body%%$'\n'*}"
        c_parsed="$(_wflg_parse_lease_marker_line "$c_first_line" || true)"
        [[ -z "$c_parsed" ]] && continue
        c_host="${c_parsed%%$'\t'*}"
        c_sweep="${c_parsed#*$'\t'}"

        if [[ -n "$yield_first_lines" ]]; then
            local c_yielded=0 y_first_line y_parsed
            while IFS= read -r y_first_line; do
                [[ -z "$y_first_line" ]] && continue
                y_parsed="$(_wflg_parse_lease_yield_marker_line "$y_first_line" || true)"
                [[ -z "$y_parsed" ]] && continue
                if [[ "${y_parsed%%$'\t'*}" == "$c_host" && "${y_parsed#*$'\t'}" == "$c_sweep" ]]; then
                    c_yielded=1
                    break
                fi
            done <<< "$yield_first_lines"
            ((c_yielded == 1)) && continue
        fi

        local c_updated_epoch c_age_seconds
        c_updated_epoch="$(_wflg_iso_to_epoch "$c_updated_at")" || continue
        c_age_seconds=$((now_epoch - c_updated_epoch))
        ((c_age_seconds < 0)) && c_age_seconds=0
        ((c_age_seconds <= ttl_seconds)) || continue

        live_pairs["${c_host}"$'\t'"${c_sweep}"]=1
    done <<< "$(jq -c '.' <<< "$comments_ndjson" 2>/dev/null || true)"

    local live_count=${#live_pairs[@]}
    ((live_count >= 2)) || return 0

    print_warning "Issue #${issue} carries ${live_count} simultaneously FRESH sweep leases -- more than one live worker has claimed this issue at once (#5783)."
    local pair
    for pair in "${!live_pairs[@]}"; do
        print_warning "  live lease: host=${pair%%$'\t'*} sweep=${pair#*$'\t'}"
    done
    return 1
}
