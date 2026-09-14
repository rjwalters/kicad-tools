#!/usr/bin/env bash
# sweep-lease-renew.sh - Sweep-owned lease renewal loop (Issue #6180, Phase 1
# of Epic #6165: "give the forge claim a liveness dimension").
#
# ## Why this exists
#
# Epic #6165 gives the `loom:building` claim a liveness dimension: a "lease"
# comment on the claimed issue whose forge-assigned `updated_at` is the
# freshness signal a later phase (Phase 2) will read to decide whether a
# claim is still alive. Phase 1 is split into two write-only halves:
#
#   - Issue #6179 (sibling): the daemon writes the lease record ONCE, at the
#     moment `loom:building` is acquired (`SweepRegistry::write_lease_comment`
#     in `loom-daemon/src/sweep_registry/guards.rs`).
#   - Issue #6180 (this script): the SWEEP renews that record for its own
#     full lifetime.
#
# **The sweep renews its own lease — never the daemon.** Role agents run as
# transient scopes parented to `systemd --user` and routinely outlive the
# daemon that spawned them (loom#6129), so supervisor liveness is NOT the
# same thing as work liveness. If the daemon owned renewal, a daemon restart
# would let a live sweep's lease expire, and a peer host could reclaim work
# that was never actually abandoned — reproducing the exact bug this epic
# exists to fix, from a different direction. Renewal must therefore be driven
# by the process actually doing the work: this script is invoked FROM WITHIN
# the sweep's own execution (`defaults/.claude/commands/loom/sweep.md`,
# "Step 1a — daemon self-claim check"), never from `loom-daemon`.
#
# ## Marker format (coordinated with #6179)
#
# At the time this script was written, #6179 had not yet merged. The format
# below is the exact shape documented in #6179's own issue body (and mirrored
# in its in-flight draft of `defaults/docs/lease-record.md`), reproduced here
# so this script has a single, precise, testable contract regardless of merge
# order:
#
#   <!-- loom:lease host=<host> sweep=<sweep-id> -->
#   <free-form prose>
#
# The marker is the comment body's LITERAL FIRST LINE. Everything after it is
# free-form prose that machine readers (this script included) must never
# depend on for anything except locating the comment via
# `startswith("<!-- loom:lease host=")`. **The liveness signal is the
# comment's own forge-assigned `updated_at` timestamp — never a value
# embedded in the marker text.** This script never parses a timestamp out of
# the marker; it exists purely to make `gh` re-touch the comment so the
# forge advances `updated_at` on its own clock.
#
# ## What "renewal" means here
#
# GitHub does not reliably advance a comment's `updated_at` on a byte-for-
# byte-identical PATCH, so a true no-op PATCH would not be a safe renewal
# mechanism. Instead, `renew-once` rewrites a single trailing HTML-comment
# line (its own sub-marker, `<!-- loom:lease-renewed at=... -->`) with a
# fresh timestamp on every call, guaranteeing the body actually changes and
# `updated_at` genuinely advances — while leaving the FIRST-LINE lease marker
# byte-identical, so a `startswith()` reader never sees it move. Like the
# primary marker, `loom:lease-renewed`'s `at=` value is for human debugging
# ONLY; no reader may treat it as authoritative (the forge's own
# `updated_at` always is).
#
# This is an idempotent PATCH of the EXISTING comment — never a new comment.
# One dispatch writes exactly one lease comment (#6179); this script only
# ever edits that same comment, however many times it is called, so no
# duplicate comments accumulate on a long-running sweep.
#
# ## Commands
#
#   sweep-lease-renew.sh start <issue> [--interval SECS] [--watch-pid PID]
#                                       [--host HOST] [--sweep-id ID]
#     Resolve a liveness PID (defaults to the same ancestor-walk
#     `resolve_liveness_pid` uses in sweep-run-registry.sh, i.e. the
#     long-lived `claude -p /loom:sweep ...` orchestrator process, NOT the
#     one-shot Bash-subshell PID of the tool call that invoked `start`), then
#     spawn ONE detached background loop that, every `--interval` seconds
#     (default 300 = 5 minutes; overridable via SWEEP_LEASE_RENEW_INTERVAL_SECS
#     too), best-effort renews the lease for <issue> as long as the watched
#     PID stays alive. Prints the loop's own PID to stdout. The loop is
#     self-terminating in TWO ways:
#       - once the watched PID is no longer alive, the loop exits on its own
#         next wake-up, so the lease record is never explicitly deleted -- it
#         simply ages out, exactly as the epic requires ("positive evidence,
#         not inference from a missing broadcast" applies to the RECLAIM
#         side; the renew side just stops broadcasting);
#       - once a `renew-once` cycle reports the own-yield guard (exit 4 --
#         this dispatcher's own lease target has a matching
#         `loom:lease-yield` record, Issue #6485), the loop stops renewing
#         immediately rather than waiting for the watched PID to die. A
#         stood-down dispatcher must not keep a live-looking lease on work it
#         is not doing.
#     A genuinely FAILING renewal cycle (any `renew-once` exit other than 0,
#     2 "no lease comment", or 4 the own-yield guard -- e.g. a `gh api` 403
#     with an exhausted escalation ladder, Issue #6541) is logged with one
#     line identifying the issue and the exit code, so a failure is visible
#     instead of vanishing for the sweep's entire lease lifetime; the loop
#     itself still does not stop for it (same best-effort contract as before).
#     If --host/--sweep-id are NOT given explicitly, `start` first tries to
#     resolve them itself -- `--sweep-id` from `$LOOM_TERMINAL_ID` (set by
#     `loom-daemon` to `daemon-<sweep-id>` for every child it spawns, Issue
#     #6485) and `--host` via the same opaque-id transform
#     `sweep-lease-fence.sh`'s `resolve_published_host` uses, so a
#     daemon-dispatched sweep's own loop renews ONLY its own lease comment by
#     exact match. This closes a related failure mode observed alongside
#     #6485's own-yield race: without an exact match, `renew-once`'s
#     "newest wins" fallback can PATCH a DIFFERENT dispatcher's more-recently
#     -posted lease comment instead of this sweep's own -- so a live,
#     correctly-working renewal loop silently keeps a PEER's claim looking
#     fresh while its own claim's `updated_at` never advances. When neither
#     can be resolved (manual `/loom:sweep`, GH Actions cron, `--no-daemon`),
#     `start` falls back to the previous "newest wins" behavior unchanged.
#
#   sweep-lease-renew.sh renew-once <issue> [--host HOST] [--sweep-id ID]
#     Perform exactly one renewal cycle synchronously (used internally by
#     `start`'s loop; also directly testable). Locates the newest comment on
#     <issue> whose body starts with the lease marker prefix; if --host AND
#     --sweep-id are BOTH given, requires an exact match on the full marker
#     line (`host=<HOST> sweep=<ID> -->`) instead of "newest wins" — useful
#     when precision matters (e.g. tests, or a future multi-lease scenario).
#     Note (Issue #6322): the daemon publishes an OPAQUE `host=` id by
#     default now, not a raw hostname — an explicit --host here is compared
#     verbatim against the marker, so a caller wanting exact-match precision
#     must pass whatever value was actually published (see
#     `sweep-lease-fence.sh`'s `opaque_host_id`/`resolve_published_host` for
#     the bash-side helper that derives it). `start` now supplies an exact
#     --host/--sweep-id by default whenever it can resolve them (see below),
#     so "newest wins" is only reached as a fallback.
#
#     **Own-yield guard (Issue #6485).** Before PATCHing, the candidate lease
#     comment's OWN `host=`/`sweep=` pair (parsed from its first line, not
#     from the caller's --host/--sweep-id) is checked against every
#     `<!-- loom:lease-yield host=... sweep=... earliest_host=... -->`
#     comment already present in the same fetched batch. If a yield record
#     names the SAME (host, sweep) pair as the candidate lease, that
#     dispatcher has already stood down for this issue (Issue #6287's
#     claim-then-verify-order tie-break) and the candidate is never PATCHed
#     -- renewing it would keep a stood-down claim looking artificially
#     fresh next to the tie-break winner's own (possibly un-renewed) lease,
#     exactly the failure mode reported in #6485. Exits 4 in this case (see
#     below); this guard applies regardless of whether the caller passed
#     --host/--sweep-id or relied on "newest wins".
#     Exits 0 on a successful PATCH, 2 if no matching lease comment exists
#     (nothing to renew — not an error: a manually-launched sweep with no
#     daemon dispatch has no lease at all), 1 on a `gh`/network failure, 4 if
#     the candidate lease's own (host, sweep) has already posted a
#     `loom:lease-yield` record for this issue (own-yield guard above — not
#     an error, a normal stand-down outcome).
#
#   sweep-lease-renew.sh stop <PID>
#     Best-effort kill of a loop PID returned by `start`. NOT required for
#     correctness (the loop already self-terminates once its watched PID
#     dies, OR once its own renewal target's yield record appears -- see
#     `start` below) -- this only speeds up teardown for anyone who wants it.
#
# ## Scope
#
# Only issues that actually have a lease comment can be renewed. This script
# never CREATES a lease comment (that is exclusively #6179's write-on-dispatch
# path) -- `renew-once` finding nothing is a normal, silent no-op for any
# sweep not dispatched by `loom-daemon` (manual `/loom:sweep`, GH Actions
# cron, `--no-daemon`).
#
# Usage:
#   .loom/scripts/sweep-lease-renew.sh start 6180
#   .loom/scripts/sweep-lease-renew.sh renew-once 6180
#   .loom/scripts/sweep-lease-renew.sh stop 12345

set -euo pipefail

LEASE_MARKER_PREFIX="<!-- loom:lease host="
YIELD_MARKER_PREFIX="<!-- loom:lease-yield host="
RENEWED_MARKER_PREFIX="<!-- loom:lease-renewed "
DEFAULT_INTERVAL_SECS="${SWEEP_LEASE_RENEW_INTERVAL_SECS:-300}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"

# forge_gh_perm_safe (Issue #6541): escalation-ladder-aware `gh` wrapper --
# retries a GitHub App-installation permission-scope 403 ("not accessible by
# integration") with a freshly minted installation token, then a personal
# token, before giving up. Both `gh api` call sites in cmd_renew_once() below
# route through it so a mid-lease-lifetime 403 escalates and recovers instead
# of failing closed for the sweep's entire lease lifetime.
# shellcheck source=./lib/forge-helpers.sh
source "$SCRIPT_DIR/lib/forge-helpers.sh"

usage() {
    awk 'NR < 3 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
    exit 1
}

# --- Repo-relative `gh` targeting (#6179's convention: LOOM_REPO override) --
gh_repo_args() {
    if [[ -n "${LOOM_REPO:-}" ]]; then
        printf -- '-R\n%s\n' "$LOOM_REPO"
    fi
}

# --- Opaque host id (Issue #6322, ported verbatim from sweep-lease-fence.sh
# so `start`'s default --host resolution matches whatever the daemon
# actually published for `host=` -- same salt, same "host-" + first 8
# lowercase hex chars of sha256(salt+host) shape) ---------------------------
LEASE_HOST_SALT="loom-lease-host-id-v1:"

sha256_hex_stdin() {
    if command -v shasum > /dev/null 2>&1; then
        shasum -a 256 | awk '{print $1}'
    elif command -v sha256sum > /dev/null 2>&1; then
        sha256sum | awk '{print $1}'
    else
        return 1
    fi
}

opaque_host_id() {
    local host="$1" hash
    hash="$(printf '%s%s' "$LEASE_HOST_SALT" "$host" | sha256_hex_stdin)" || return 1
    [[ -n "$hash" ]] || return 1
    printf 'host-%s' "${hash:0:8}"
}

lease_publish_raw_hostname() {
    case "$(printf '%s' "${LOOM_LEASE_PUBLISH_HOSTNAME:-}" | tr '[:upper:]' '[:lower:]' | xargs)" in
        1 | true | yes | on) return 0 ;;
        *) return 1 ;;
    esac
}

resolve_host() {
    if [[ -n "${LOOM_HOST_ID:-}" ]]; then
        printf '%s' "$LOOM_HOST_ID"
        return 0
    fi
    if [[ -n "${HOSTNAME:-}" ]]; then
        printf '%s' "$HOSTNAME"
        return 0
    fi
    local h
    h="$(hostname 2> /dev/null || true)"
    if [[ -n "$h" ]]; then
        printf '%s' "$h"
        return 0
    fi
    printf 'unknown-host'
}

resolve_published_host() {
    local raw
    raw="$(resolve_host)"
    if lease_publish_raw_hostname; then
        printf '%s' "$raw"
        return 0
    fi
    opaque_host_id "$raw" || printf '%s' "$raw"
}

# --- Marker parsing (mirrors sweep-lease-fence.sh's parse_lease_marker_line)
# Prints "host<TAB>sweep" on success, nothing on a malformed marker. ---------
parse_lease_marker_line() {
    local first_line="$1" rest host sweep_id
    rest="${first_line#"$LEASE_MARKER_PREFIX"}"
    [[ "$rest" == "$first_line" ]] && return 1
    [[ "$rest" == *" -->" ]] || return 1
    rest="${rest% -->}"
    case "$rest" in
        *" sweep="*)
            host="${rest%% sweep=*}"
            sweep_id="${rest#* sweep=}"
            ;;
        *) return 1 ;;
    esac
    [[ -n "$host" && -n "$sweep_id" ]] || return 1
    printf '%s\t%s' "$host" "$sweep_id"
}

# parse_lease_yield_marker_line -- same idea, for the `loom:lease-yield`
# shape: "host=<H> sweep=<S> earliest_host=<EH> earliest_sweep=<ES> -->".
# The earliest_host/earliest_sweep fields identify who WON, not who is
# yielding, so they are parsed off and discarded here.
parse_lease_yield_marker_line() {
    local first_line="$1" rest host sweep_id
    rest="${first_line#"$YIELD_MARKER_PREFIX"}"
    [[ "$rest" == "$first_line" ]] && return 1
    case "$rest" in
        *" sweep="*)
            host="${rest%% sweep=*}"
            sweep_id="${rest#* sweep=}"
            sweep_id="${sweep_id%% earliest_host=*}"
            sweep_id="${sweep_id% }"
            ;;
        *) return 1 ;;
    esac
    [[ -n "$host" && -n "$sweep_id" ]] || return 1
    printf '%s\t%s' "$host" "$sweep_id"
}

# ---------------------------------------------------------------------------
# Liveness (mirrors sweep-run-registry.sh's #4691 "Liveness" section verbatim
# in spirit -- duplicated here, not sourced, because sweep-run-registry.sh
# unconditionally calls `main "$@"` at the bottom of the file with no
# BASH_SOURCE guard, so `source`-ing it would also execute its CLI dispatch).
# ---------------------------------------------------------------------------

is_oneshot_shell() {
    local pid="${1:-}" comm base args a0 a1
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    comm=$(ps -o comm= -p "$pid" 2>/dev/null) || return 1
    [[ -n "$comm" ]] || return 1
    base="${comm##*/}"
    base="${base#-}"
    case "$base" in
        sh | bash | zsh | dash | ksh | ksh93 | mksh) ;;
        *) return 1 ;;
    esac
    args=$(ps -o args= -p "$pid" 2>/dev/null) || return 1
    read -r a0 a1 _ <<< "$args"
    [[ -n "$a0" ]] || return 1
    [[ "${a1:-}" == -*c* ]]
}

resolve_liveness_pid() {
    local pid="${PPID:-$$}" parent depth=0
    while ((depth < 8)); do
        is_oneshot_shell "$pid" || break
        parent=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]')
        if ! [[ "$parent" =~ ^[0-9]+$ ]] || ((parent <= 1)); then
            break
        fi
        pid="$parent"
        depth=$((depth + 1))
    done
    echo "$pid"
}

pid_is_live() {
    local pid="${1:-}" state
    if ! [[ "$pid" =~ ^[0-9]+$ ]] || ((pid <= 0)); then
        return 1
    fi
    kill -0 "$pid" 2>/dev/null && return 0
    state=$(ps -o state= -p "$pid" 2>/dev/null | tr -d '[:space:]')
    [[ -n "$state" ]] || return 1
    [[ "${state:0:1}" == "Z" ]] && return 1
    return 0
}

iso_now() {
    date -u +"%Y-%m-%dT%H:%M:%SZ"
}

# --- renew-once --------------------------------------------------------

cmd_renew_once() {
    local issue="${1:-}"
    shift || true
    [[ "$issue" =~ ^[0-9]+$ ]] || {
        echo "ERROR: renew-once requires a positive integer issue number (got: '${issue:-}')" >&2
        exit 1
    }

    local host="" sweep_id=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --host)
                host="${2:-}"
                shift 2
                ;;
            --sweep-id)
                sweep_id="${2:-}"
                shift 2
                ;;
            *)
                echo "ERROR: renew-once: unknown flag '$1'" >&2
                exit 1
                ;;
        esac
    done

    local -a repo_args=()
    while IFS= read -r line; do
        [[ -n "$line" ]] && repo_args+=("$line")
    done < <(gh_repo_args)

    local exact=""
    if [[ -n "$host" && -n "$sweep_id" ]]; then
        exact="${LEASE_MARKER_PREFIX}${host} sweep=${sweep_id} -->"
    elif [[ -n "$host" || -n "$sweep_id" ]]; then
        echo "ERROR: renew-once: --host and --sweep-id must both be given, or neither" >&2
        exit 1
    fi

    # Routed through forge_gh_perm_safe (Issue #6541) so a GitHub
    # App-installation permission-scope 403 escalates through a fresh
    # installation-token mint, then a personal token, instead of failing this
    # (and every subsequent) renewal cycle silently. stdout/stderr are kept
    # separate here (unlike a plain `2>&1` capture) -- forge_gh_perm_safe
    # writes its own escalation-ladder diagnostics to stderr even on an
    # eventual SUCCESS, and merging those into $comments_json would corrupt
    # the JSON this function is about to parse.
    local comments_json
    if ! comments_json="$(forge_gh_perm_safe api "${repo_args[@]}" "repos/{owner}/{repo}/issues/${issue}/comments" --paginate)"; then
        echo "ERROR: 'gh api .../issues/${issue}/comments --paginate' failed (escalation ladder exhausted)" >&2
        exit 1
    fi

    local candidate_id
    candidate_id="$(jq -r --arg marker "$LEASE_MARKER_PREFIX" --arg exact "$exact" '
        [ .[] | select(.body != null and (.body | startswith($marker)))
              | select($exact == "" or (.body | startswith($exact))) ]
        | sort_by(.id) | reverse | .[0].id // empty
    ' <<< "$comments_json" 2>/dev/null || true)"

    if [[ -z "$candidate_id" ]]; then
        echo "no lease comment found for issue #${issue} (marker=${LEASE_MARKER_PREFIX}...${exact:+, exact=$exact}); nothing to renew (#6180)" >&2
        exit 2
    fi

    local old_body
    old_body="$(jq -r --arg id "$candidate_id" '.[] | select((.id | tostring) == $id) | .body' <<< "$comments_json")"

    # --- Own-yield guard (Issue #6485) -------------------------------------
    # The candidate lease's OWN (host, sweep) -- parsed from its own first
    # line, not from --host/--sweep-id -- may have already stood down via
    # Issue #6287's claim-then-verify-order tie-break (a
    # `<!-- loom:lease-yield host=... sweep=... earliest_host=... -->`
    # comment on this same issue). Renewing a yielded dispatcher's lease
    # keeps it looking artificially fresh next to the tie-break winner's own
    # (possibly un-renewed) lease -- exactly the failure mode reported in
    # #6485. Refuse to PATCH in that case.
    local candidate_first_line candidate_parsed candidate_host candidate_sweep
    candidate_first_line="${old_body%%$'\n'*}"
    candidate_parsed="$(parse_lease_marker_line "$candidate_first_line" || true)"
    if [[ -n "$candidate_parsed" ]]; then
        candidate_host="${candidate_parsed%%$'\t'*}"
        candidate_sweep="${candidate_parsed#*$'\t'}"
        local yield_first_lines yielded="false"
        yield_first_lines="$(jq -r --arg prefix "$YIELD_MARKER_PREFIX" '
            .[] | select(.body != null and (.body | startswith($prefix))) | (.body | split("\n")[0])
        ' <<< "$comments_json" 2> /dev/null || true)"
        while IFS= read -r yield_first_line; do
            [[ -z "$yield_first_line" ]] && continue
            local yparsed
            yparsed="$(parse_lease_yield_marker_line "$yield_first_line" || true)"
            [[ -z "$yparsed" ]] && continue
            if [[ "${yparsed%%$'\t'*}" == "$candidate_host" && "${yparsed#*$'\t'}" == "$candidate_sweep" ]]; then
                yielded="true"
                break
            fi
        done <<< "$yield_first_lines"
        if [[ "$yielded" == "true" ]]; then
            echo "NOT renewing lease comment ${candidate_id} for issue #${issue}: its own owner (host=${candidate_host} sweep=${candidate_sweep}) has already posted a loom:lease-yield standdown record for this issue (#6485 own-yield guard) -- a stood-down dispatcher must not keep a live-looking lease" >&2
            exit 4
        fi
    fi

    local stripped_body now_iso new_body
    stripped_body="$(printf '%s\n' "$old_body" | grep -v "^${RENEWED_MARKER_PREFIX}" || true)"
    now_iso="$(iso_now)"
    new_body="$(printf '%s\n\n%sat=%s by=sweep-lease-renew.sh (#6180) -->\n' \
        "$stripped_body" "$RENEWED_MARKER_PREFIX" "$now_iso")"

    # Routed through forge_gh_perm_safe (Issue #6541), same rationale as the
    # comments-list read above. The renewed body is written to a temp file
    # and referenced via `-F body=@<path>` rather than piped through stdin
    # (`-F body=@-`, the pre-#6541 shape): forge_gh_perm_safe's escalation
    # ladder can re-run this `gh api` call up to three times (ambient, fresh
    # App token, personal token), and a stdin pipe is only readable ONCE --
    # a retry after the first rung's 403 would see empty stdin and PATCH the
    # lease comment's body to empty. A file survives every rung. `-F` (not
    # `-f`) is still required to expand the `@<path>` reference (#6357).
    local patch_body_file
    patch_body_file="$(mktemp)"
    printf '%s' "$new_body" > "$patch_body_file"
    if ! forge_gh_perm_safe api "${repo_args[@]}" --method PATCH "repos/{owner}/{repo}/issues/comments/${candidate_id}" \
        -F "body=@${patch_body_file}" \
        > /dev/null; then
        rm -f "$patch_body_file"
        echo "ERROR: PATCH of lease comment ${candidate_id} on issue #${issue} failed" >&2
        exit 1
    fi
    rm -f "$patch_body_file"

    echo "renewed lease comment ${candidate_id} for issue #${issue} at ${now_iso}" >&2
}

# --- start ---------------------------------------------------------------

cmd_start() {
    local issue="${1:-}"
    shift || true
    [[ "$issue" =~ ^[0-9]+$ ]] || {
        echo "ERROR: start requires a positive integer issue number (got: '${issue:-}')" >&2
        exit 1
    }

    local interval="$DEFAULT_INTERVAL_SECS" watch_pid="" host="" sweep_id=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --interval)
                interval="${2:-}"
                shift 2
                ;;
            --watch-pid)
                watch_pid="${2:-}"
                shift 2
                ;;
            --host)
                host="${2:-}"
                shift 2
                ;;
            --sweep-id)
                sweep_id="${2:-}"
                shift 2
                ;;
            *)
                echo "ERROR: start: unknown flag '$1'" >&2
                exit 1
                ;;
        esac
    done

    if ! [[ "$interval" =~ ^[0-9]+$ ]] || ! ((interval > 0)); then
        echo "ERROR: --interval must be a positive integer (got: '$interval')" >&2
        exit 1
    fi

    if [[ -z "$watch_pid" ]]; then
        watch_pid="$(resolve_liveness_pid)"
    fi
    [[ "$watch_pid" =~ ^[0-9]+$ ]] || {
        echo "ERROR: could not resolve a watch PID" >&2
        exit 1
    }

    # Default to exact-match targeting of THIS sweep's own lease comment
    # (Issue #6485) when the caller did not explicitly pass --host/--sweep-id
    # for either. Without this, `renew-once` falls back to "newest wins",
    # which silently PATCHes a DIFFERENT dispatcher's more-recently-posted
    # lease comment when one exists on the same issue -- the exact mechanism
    # that left a genuine tie-break winner's own lease un-renewed while its
    # loop kept a peer's lease looking fresh instead.
    #   --sweep-id: `loom-daemon` sets $LOOM_TERMINAL_ID to `daemon-<sweep-id>`
    #     for every child it spawns (see dispatch.rs); strip the prefix.
    #   --host: the same opaque-id transform sweep-lease-fence.sh's own
    #     `resolve_published_host` uses, so it matches whatever the dispatch
    #     that wrote the lease actually published.
    # A partial resolution (one but not the other) is discarded rather than
    # passed through -- renew-once requires both --host and --sweep-id
    # together or neither. Explicit --host/--sweep-id flags always win.
    if [[ -z "$host" && -z "$sweep_id" ]]; then
        local auto_sweep_id="" auto_host=""
        if [[ "${LOOM_TERMINAL_ID:-}" == daemon-* ]]; then
            auto_sweep_id="${LOOM_TERMINAL_ID#daemon-}"
        fi
        if [[ -n "$auto_sweep_id" ]]; then
            auto_host="$(resolve_published_host)"
        fi
        if [[ -n "$auto_sweep_id" && -n "$auto_host" ]]; then
            host="$auto_host"
            sweep_id="$auto_sweep_id"
        fi
    fi

    local -a extra_args=()
    [[ -n "$host" ]] && extra_args+=(--host "$host")
    [[ -n "$sweep_id" ]] && extra_args+=(--sweep-id "$sweep_id")

    # Save the ORIGINAL stderr (Issue #6541) on a private fd BEFORE the
    # detach redirect below sends the loop's own stdout/stderr to /dev/null.
    # `exec 9>&2` duplicates whatever fd 2 already resolved to at the moment
    # `start` was invoked (a sweep's own log file, a terminal, or /dev/null
    # if the caller redirected it there itself) onto fd 9. The background
    # subshell inherits that duplicate untouched by its own `2>&1` -- so a
    # renewal failure can still be logged even though the loop's ordinary
    # I/O is unconditionally discarded for detachment. Without this, a
    # gh 403 (or any other failure) vanished with zero trace until a
    # downstream lease-fence check caught it, for the entire lease lifetime.
    exec 9>&2

    # Detached loop: sleeps first (dispatch already wrote a fresh lease right
    # before spawning this sweep, so the first renewal isn't due for a full
    # interval), then renews, then re-checks watch-PID liveness. An ordinary
    # transient failure from a single renew-once call does not kill the loop
    # or the sweep (`|| renew_rc=$?` catches it under `set -e`) -- exactly
    # the same best-effort contract #6179's write-on-dispatch path uses --
    # but (Issue #6541) it now ALSO logs one line to the saved fd 9 so the
    # failure is visible instead of silently discarded. Exit 2 ("no matching
    # lease comment" -- the normal, expected outcome for any sweep with no
    # daemon-written lease at all) and exit 4 (the own-yield guard below) are
    # NOT failures and are not logged as such. The ONE exception to the
    # swallow (Issue #6485): exit 4 from renew-once means this dispatcher's
    # own lease target has itself posted a `loom:lease-yield` standdown
    # record -- the loop stops renewing immediately rather than waiting for
    # the watched PID to die, since renewing further would only keep a
    # stood-down claim looking artificially fresh.
    (
        while pid_is_live "$watch_pid"; do
            sleep "$interval"
            pid_is_live "$watch_pid" || break
            renew_rc=0
            renew_err="$("$SELF" renew-once "$issue" "${extra_args[@]}" 2>&1 > /dev/null)" || renew_rc=$?
            if [[ "$renew_rc" -ne 0 && "$renew_rc" -ne 2 && "$renew_rc" -ne 4 ]]; then
                echo "sweep-lease-renew: renewal cycle for issue #${issue} FAILED (renew-once exit ${renew_rc}): ${renew_err}" >&9
            fi
            if [[ "$renew_rc" -eq 4 ]]; then
                break
            fi
        done
    ) < /dev/null > /dev/null 2>&1 &
    local loop_pid=$!
    exec 9>&-
    disown "$loop_pid" 2> /dev/null || true
    echo "$loop_pid"
}

cmd_stop() {
    local pid="${1:-}"
    [[ "$pid" =~ ^[0-9]+$ ]] || {
        echo "ERROR: stop requires a PID argument" >&2
        exit 1
    }
    kill "$pid" 2> /dev/null || true
}

main() {
    local cmd="${1:-}"
    shift || true
    case "$cmd" in
        start) cmd_start "$@" ;;
        renew-once) cmd_renew_once "$@" ;;
        stop) cmd_stop "$@" ;;
        -h | --help | "") usage ;;
        *)
            echo "ERROR: unknown command '$cmd'" >&2
            usage
            ;;
    esac
}

main "$@"
