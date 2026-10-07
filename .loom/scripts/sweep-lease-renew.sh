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
# by the process actually doing the work.
#
# ## Who may invoke `start` (amended by loom#7672)
#
# The rule above constrains **which process the loop watches**, not which
# process typed `start`. `loom-daemon` MAY invoke `start` — and does, once,
# in `SweepRegistry::finish_issue_dispatch`, immediately after spawning a
# `--claim-owned` sweep child, passing `--watch-pid <that child's pid>`
# (plus the `--host`/`--sweep-id` pair it published in the lease comment).
# That is a one-shot invocation, not ownership: `cmd_start` forks the loop,
# `disown`s it, and returns, so the daemon holds no handle and runs no tick;
# the loop's lifetime is pinned to the SWEEP's pid and a daemon restart
# leaves it running untouched. The hand-off exists because the previous
# arrangement — prose in sweep.md Step 1a asking the spawned LLM session to
# run `start` itself — failed exactly once in production and cost ~2.5h of
# fleet-wide claim/yield thrash plus a near-miss double-claim on a shared
# worktree (the downstream incident cited in loom#7672).
#
# What `loom-daemon` still must NEVER do is own ONGOING renewal — no
# tick-loop `renew-once`, no "re-arm every live sweep's renewal on startup".
# That, and only that, is what loom#6129 forbids.
#
# In-session callers (operator `/loom:sweep`, `--no-daemon`, GH Actions cron)
# have no dispatch code to do this for them and still invoke `start`
# themselves from `defaults/.claude/commands/loom/sweep.md` — Step 1b
# unconditionally, and Step 1a as a fallback when the dispatch's
# `LOOM_SWEEP_LEASE_RENEW_DISPATCHED` capability marker is absent (a
# pre-#7672 daemon binary, which rolls on a different cadence than the
# installed prompt).
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
#                                       [--watch-ident TOKEN] [--max-age SECS]
#                                       [--host HOST] [--sweep-id ID]
#     Resolve a liveness PID (defaults to the same ancestor-walk
#     `resolve_liveness_pid` uses in sweep-run-registry.sh, i.e. the
#     long-lived `claude -p /loom:sweep ...` orchestrator process, NOT the
#     one-shot Bash-subshell PID of the tool call that invoked `start`), then
#     spawn ONE detached background loop that, every `--interval` seconds
#     (default 300 = 5 minutes; overridable via SWEEP_LEASE_RENEW_INTERVAL_SECS
#     too), best-effort renews the lease for <issue> as long as that PID is
#     still running THE SAME PROCESS. Prints the loop's own PID to stdout.
#     The loop is self-terminating in FOUR ways:
#       - once the watched PID is no longer alive, the loop exits on its own
#         next wake-up, so the lease record is never explicitly deleted -- it
#         simply ages out, exactly as the epic requires ("positive evidence,
#         not inference from a missing broadcast" applies to the RECLAIM
#         side; the renew side just stops broadcasting);
#       - once the watched PID's START-TIME IDENTITY stops matching the one
#         recorded at `start` time (Issue #7825), even though the PID NUMBER
#         is still live. A bare PID is not a durable handle: the kernel
#         recycles PID numbers, and a PID-only test flips permanently back to
#         "alive" the moment an unrelated process inherits the number. That is
#         what produced six orphan renewal loops (oldest 18 days) on one
#         worker, each keeping a long-dead sweep's claim looking fresh so no
#         peer host would ever reclaim it. `--watch-ident` pins the token
#         explicitly (a caller that already captured it at spawn time can
#         close the microsecond probe race); otherwise `start` probes it
#         itself;
#       - once the loop has been running longer than the ABSOLUTE age cap
#         (Issue #7825), regardless of what the watch test reports:
#         `--max-age SECS` / SWEEP_LEASE_RENEW_MAX_AGE_SECS, default 86400
#         (24 h), `0` = unbounded. This is the backstop that makes the whole
#         failure class bounded even if the liveness test is defeated again by
#         something nobody has anticipated;
#       - once a `renew-once` cycle reports the own-yield guard (exit 4 --
#         this dispatcher's own lease target has a matching
#         `loom:lease-yield` record, Issue #6485), the loop stops renewing
#         immediately rather than waiting for the watched PID to die. A
#         stood-down dispatcher must not keep a live-looking lease on work it
#         is not doing.
#     Nothing outside this script reaps the loop -- no daemon tick, no
#     reaper pass, no worktree-scoped orphan hunt can even see it (#7825 §3).
#     Self-termination is the ONLY mechanism, which is why it now has four
#     independent legs instead of one.
#     A genuinely FAILING renewal cycle (any `renew-once` exit other than 0,
#     2 "no lease comment", or 4 the own-yield guard -- e.g. a `gh api` 403
#     with an exhausted escalation ladder, Issue #6541) is logged with one
#     line identifying the issue and the exit code, so a failure is visible
#     instead of vanishing for the sweep's entire lease lifetime; the loop
#     itself still does not stop for it (same best-effort contract as before).
#     The loop remembers its lease comment between cycles (Issue #10021): only
#     the first cycle -- or one following a deleted comment -- lists every
#     comment on the issue; see `renew-once --cached-lease` below.
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
#     `start` REFUSES (exit 1, no loop forked) a `--host`/`--sweep-id` pair
#     that cannot possibly match its own lease comment: either value
#     containing whitespace, or exactly one of the two given non-empty
#     (Issue #7876). Passing neither remains legal. That shape is what a
#     caller produces when it word-splits `sweep-lease-publish.sh publish`'s
#     "<host> <sweep-id>" line under zsh, which does not split unquoted
#     parameters -- and a loop that can never renew is worse than no loop,
#     because the caller believes renewal is running while the lease ages
#     out. Split that line with `read -r LEASE_HOST LEASE_SWEEP
#     <<<"$LEASE_IDENT"`, which behaves identically in bash and zsh.
#
#   sweep-lease-renew.sh renew-once <issue> [--host HOST] [--sweep-id ID]
#                                            [--cached-lease ID@SINCE]
#     Perform exactly one renewal cycle synchronously (used internally by
#     `start`'s loop; also directly testable). With --cached-lease (Issue
#     #10021; `start`'s loop passes the value the previous successful cycle
#     reported as `lease-cache=<id>@<since>` on stderr), the lookup is ONE
#     non-paginated page of the comments updated since SINCE -- never a
#     `--paginate` listing -- and the cached comment is renewed with a single
#     PATCH. It falls back to the full paginated lookup below (re-exec without
#     the cache) only when that comment is missing from the window (deleted,
#     or no longer matching), the window is a full page, or the PATCH 404s.
#     Without --cached-lease: locates the newest comment on
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
#     comment already present in the same fetched batch (with --cached-lease,
#     the since-SINCE window, which holds every yield record posted
#     after the lease). If a yield record
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
#   sweep-lease-renew.sh release <issue> [--host HOST] [--sweep-id ID]
#     End the renewer owning this (repo, host, sweep, issue) key now (#10229);
#     idempotent, and a peer's key is never touched. `start` is single-owner
#     per that key, and each cycle also reads the issue's state and stops for
#     good once it is closed (an unreadable state skips that cycle's PATCH).
#     All three live in `loom-daemon lease renewer` (claim / check / release);
#     records sit under ~/.loom/lease-renew ($LOOM_LEASE_RENEW_STATE_DIR).
#     Budget: 3 requests per cycle, 36/h per held lease at 300 s, spent on the
#     host's GitHub App installation when one is configured (`lease_gh`), so
#     the personal login pays nothing in steady state. A loop with no lease
#     to renew stops after two consecutive misses; the window read slides with
#     the lease's own updated_at. See defaults/docs/lease-record.md.
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

# requires-daemon: lease optional   `lease renewer` (#10229): any exit but 3 (stop) / 4 (skip) from a binary predating it renews exactly as before.

set -euo pipefail

LEASE_MARKER_PREFIX="<!-- loom:lease host="
YIELD_MARKER_PREFIX="<!-- loom:lease-yield host="
RENEWED_MARKER_PREFIX="<!-- loom:lease-renewed "
DEFAULT_INTERVAL_SECS="${SWEEP_LEASE_RENEW_INTERVAL_SECS:-300}"

# Absolute lifetime cap for one `start` loop (Issue #7825). The loop exits
# unconditionally once it has been running this long, no matter what the
# watch-PID probe says. This is the LAST line of defense, deliberately
# independent of the identity check above it: even if the liveness test is
# defeated again by some mechanism nobody has thought of yet, the failure is
# bounded at a day instead of the 18 days observed in #7825. A sweep that
# legitimately outruns the cap loses lease renewal, not its work -- the claim
# simply becomes reclaimable, which is the correct outcome for a sweep nobody
# can distinguish from a dead one. `0` disables the cap entirely.
DEFAULT_MAX_AGE_SECS="${SWEEP_LEASE_RENEW_MAX_AGE_SECS:-86400}"

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

# lease_gh <read|write> <gh args...> (#10229): run one forge call on this host's
# GitHub App installation -- `loom-daemon forge token` picks a reader App for the
# GETs and the writer App for the PATCH -- so renewal spends an App bucket, not
# the personal login an interactive session inherits. forge_gh_perm_safe's
# ladder still runs under the App token. With no App configured, or when the App
# attempt fails for any reason, the call re-runs exactly as before on the
# caller's own credential, tagged `lease-credential=ambient-fallback` on stderr.
#
# Attribution is per ATTEMPT, not per call: the ladder's personal rungs
# (LOOM_PERSONAL_GH_TOKEN, then the ambient personal login) can recover an App
# 403 inside the App attempt, so each `gh` exec goes through LEASE_GH_ATTEMPT,
# which classifies the credential it is actually about to run on and exports
# LOOM_LEASE_CREDENTIAL (app / ambient) for gh-shim telemetry accordingly. A
# ladder that escalated prints one `lease-credential-attempt:` line per attempt,
# and an App call recovered on a personal rung is tagged
# `lease-credential=ambient-recovered` -- both on stderr, even on success.
#
# One ladder attempt: attempt 1 is the call's base credential (LOOM_LEASE_BASE:
# app / ambient); a later one is the fresh installation-token mint (app), or a
# personal rung -- GH_TOKEN equal to LOOM_PERSONAL_GH_TOKEN, or unset (the
# ambient personal login) -- which is attributed ambient.
# shellcheck disable=SC2016 # expanded by the child bash, per attempt
LEASE_GH_ATTEMPT='n=$(($(wc -l < "$LOOM_LEASE_ATTEMPTS") + 1)) c=app-fresh-mint a=ambient
if ((n == 1)); then c="$LOOM_LEASE_BASE"
elif [[ -z "${GH_TOKEN:-}" ]]; then c=personal-ambient
elif [[ "$GH_TOKEN" == "${LOOM_PERSONAL_GH_TOKEN:-}" ]]; then c=personal-token; fi
[[ "$c" != app* ]] || a=app
echo "attempt=$n credential=$c attribution=$a" >> "$LOOM_LEASE_ATTEMPTS"
LOOM_LEASE_CREDENTIAL=$a exec gh "$@"'

# _lease_gh_ladder <app|ambient> <attempts-file> <gh args...>: forge_gh_perm_safe's
# ladder (forge_cmd_perm_safe) with every attempt classified by LEASE_GH_ATTEMPT.
_lease_gh_ladder() {
    LOOM_LEASE_BASE="$1" LOOM_LEASE_ATTEMPTS="$2" LOOM_PERSONAL_GH_TOKEN="${LOOM_PERSONAL_GH_TOKEN:-}" \
        forge_cmd_perm_safe bash -c "$LEASE_GH_ATTEMPT" lease-gh "${@:3}"
}

# _lease_gh_attempts <attempts-file>: one stderr line per attempt, only when the
# ladder escalated (steady state is a single, silent attempt).
_lease_gh_attempts() {
    (($(wc -l < "$1") < 2)) || sed 's/^/lease-credential-attempt: /' "$1" >&2
}

# requires-daemon: forge optional   Without `forge token` (absent or older binary), or with no App configured, every call runs on the caller's own credential, exactly as before #10229.
lease_gh() {
    local access="$1" tok="" out="" rc=0 diag attempts last; shift
    tok="$("${LOOM_DAEMON_BIN:-loom-daemon}" forge token --repo "${LOOM_REPO:-$(_forge_nwo_from_remote || true)}" --access "$access" 2> /dev/null | jq -r 'select(.status == "ok") | .token // empty' 2> /dev/null)" || tok=""
    attempts="$(mktemp)" diag="$(mktemp)"
    if [[ -n "$tok" ]] && out="$(GH_TOKEN="$tok" _lease_gh_ladder app "$attempts" "$@" 2> "$diag")"; then
        # Success: replay the ladder's diagnostics verbatim (none in steady state).
        _lease_gh_attempts "$attempts"
        cat "$diag" >&2
        last="$(sed -n '$s/.*credential=//p' "$attempts")"
        [[ "$last" != *attribution=ambient ]] || echo "lease-credential=ambient-recovered: the ${access} call was recovered on the personal credential (${last%% *}) after the App credential's permission 403 (#10229)" >&2
        rm -f "$diag" "$attempts"
        [[ -z "$out" ]] || printf '%s\n' "$out"
        return 0
    elif [[ -n "$tok" ]]; then
        # Failure: replay only the ladder's own escalation lines. The App attempt's
        # raw gh error is summarised by the tag below; replaying it would let an
        # App-only `HTTP 404` (App not installed) read as the PATCH's own 404.
        _lease_gh_attempts "$attempts"
        grep '^forge: ' "$diag" >&2 || true
        : > "$attempts"
        echo "lease-credential=ambient-fallback: the ${access} call failed on the App credential (#10229)" >&2
    fi
    rm -f "$diag"
    _lease_gh_ladder ambient "$attempts" "$@" || rc=$?
    _lease_gh_attempts "$attempts"
    rm -f "$attempts"
    return "$rc"
}

usage() {
    awk 'NR < 3 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
    exit 1
}

# --- Repo-relative `gh` targeting (#6179's convention: LOOM_REPO override) --
# `gh api` has no -R flag (#9552), so the repo goes in the endpoint path.
gh_repo_path() {
    local placeholder='{owner}/{repo}'
    printf '%s' "${LOOM_REPO:-$placeholder}"
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
    local h="${LOOM_HOST_ID:-${HOSTNAME:-}}"
    [[ -n "$h" ]] || h="$(hostname 2> /dev/null || true)"
    printf '%s' "${h:-unknown-host}"
}

resolve_published_host() {
    local raw
    raw="$(resolve_host)"
    ! lease_publish_raw_hostname || { printf '%s' "$raw"; return 0; }
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
# Liveness (#4691, hardened by #7825)
# ---------------------------------------------------------------------------
#
# Duplicated from sweep-run-registry.sh rather than sourced from it, because
# that script unconditionally calls `main "$@"` at the bottom of the file with
# no BASH_SOURCE guard, so `source`-ing it would also execute its CLI dispatch.
# The duplication is machine-checked -- see the block header below.
# ---------------------------------------------------------------------------

# --- BEGIN shared pid-liveness block (#4691, #7825) ------------------------
#
# This block is DUPLICATED VERBATIM in `defaults/scripts/sweep-lease-renew.sh`
# and `defaults/scripts/sweep-run-registry.sh` and MUST stay byte-identical in
# both: `defaults/scripts/tests/test-sweep-lease-renew.sh` case (q) diffs the
# two copies and fails the suite on any drift. It is deliberately NOT extracted
# into `defaults/scripts/lib/`: ADR-0018 / `scripts/shell-allowlist.txt` admits
# no category for a NEW shared shell library (`contract` is BASELINE-ONLY, and
# a library is not bootstrap/hook-entry/vendored/stub/test), so a new lib file
# would fail `scripts/check-shell-allowlist.sh`. The machine-checked
# byte-identity is the substitute for `source`. Edit one copy, then run
# `diff` — or just let case (q) tell you.
#
# Is `$1` a one-shot `<shell> -c …` wrapper process?
#
# An agent harness (Claude Code's Bash tool, and any `bash -c`/`zsh -c` wrapper)
# spawns a FRESH shell per tool call and reaps it the moment that call returns.
# Such a process is never a valid liveness handle for a sweep that spans hundreds
# of tool calls. An INTERACTIVE or login shell (no `-c`) is long-lived and IS a
# valid handle, so the `-c` flag — not merely "is a shell" — is the discriminator.
is_oneshot_shell() {
    local pid="${1:-}" comm base args a0 a1
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    comm=$(ps -o comm= -p "$pid" 2>/dev/null) || return 1
    [[ -n "$comm" ]] || return 1
    base="${comm##*/}"
    base="${base#-}" # a login shell reports as "-zsh"
    case "$base" in
        sh | bash | zsh | dash | ksh | ksh93 | mksh) ;;
        *) return 1 ;;
    esac
    args=$(ps -o args= -p "$pid" 2>/dev/null) || return 1
    # argv[1] carries the flags; `-c`, `-lc`, `-ec` … all mean "run this string".
    read -r a0 a1 _ <<< "$args"
    [[ -n "$a0" ]] || return 1
    [[ "${a1:-}" == -*c* ]]
}

# Is `$1` a session/service supervisor that must NEVER serve as a liveness
# handle for WORK (#7825, defect (c))?
#
# Before #7825, `resolve_liveness_pid` walked UP the process tree and returned
# whatever it landed on with no validation whatsoever. When a sweep's own
# intermediate ancestors have already been reaped, that walk lands on the
# session supervisor — `systemd --user`, `launchd`, a `tmux` server, `sshd`,
# `init` — every one of which outlives the machine's entire work queue. A
# renewal loop pinned to one of those is immortal BY CONSTRUCTION: it keeps a
# long-dead sweep's `loom:building` lease looking fresh forever, so no peer
# host will ever reclaim the claim. Six such loops (oldest 18 days, mostly
# with no worktree left behind them) were found on one worker in #7825.
#
# This is a DENYLIST, not an allowlist, on purpose. An allowlist of "real"
# sweep processes would have to enumerate every runtime adapter, wrapper, and
# future harness (`claude`, `codex`, `spawn-claude.sh`, `claude-wrapper.sh`,
# `node`, …) and would silently start refusing legitimate handles the moment
# one was missed. The denylist only has to name the handful of processes known
# to outlive ALL work; anything unrecognised keeps the pre-#7825 behavior.
is_supervisor_process() {
    local pid="${1:-}" comm base
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    if ((pid <= 1)); then
        return 0 # pid 1 is init/launchd by definition
    fi
    comm=$(ps -o comm= -p "$pid" 2>/dev/null) || return 1
    [[ -n "$comm" ]] || return 1
    base="${comm##*/}"
    base="${base#-}"
    case "$base" in
        init | systemd | launchd | upstart) return 0 ;;
        tmux*) return 0 ;; # `ps -o comm=` reports the server as `tmux: server`
        screen | sshd | login | getty) return 0 ;;
        supervisord | s6-svscan | runsvdir | runsv | tini | docker-init | dumb-init) return 0 ;;
        cron | crond | atd) return 0 ;;
        *) return 1 ;;
    esac
}

# Resolve the PID to record as this run's liveness handle: walk up from $PPID
# past every one-shot shell wrapper to the first ancestor that outlives a single
# tool call (in practice the `claude -p /loom:sweep …` orchestrator). Falls back
# to $PPID whenever `ps` is unavailable or the walk cannot proceed, which is
# exactly the pre-#4691 behavior — never worse.
#
# #7825 (c): the walk now refuses to ascend INTO a supervisor, and refuses to
# RETURN one even if $PPID already is one (a unit-file or cron invocation). When
# no valid work-liveness handle exists, the honest answer is this script's OWN
# short-lived PID: a caller that watches it stops within one tick and lets the
# lease age out, which is the correct outcome. Silently substituting a process
# that will still be running next month is not.
resolve_liveness_pid() {
    local pid="${PPID:-$$}" parent depth=0
    while ((depth < 8)); do
        is_oneshot_shell "$pid" || break
        parent=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]')
        # Stop at an unreadable parent, or at pid 1 (init is not a sweep owner).
        if ! [[ "$parent" =~ ^[0-9]+$ ]] || ((parent <= 1)); then
            break
        fi
        if is_supervisor_process "$parent"; then
            break
        fi
        pid="$parent"
        depth=$((depth + 1))
    done
    if is_supervisor_process "$pid"; then
        echo "$$"
        return 0
    fi
    echo "$pid"
}

# Start-time identity token for `$1` — the second half of a durable process
# handle (#7825, defect (a)). Prints nothing and exits non-zero when it cannot
# be determined.
#
# A bare PID is NOT a durable handle on a process: the kernel recycles PID
# numbers, and on a busy worker polled over a multi-day horizon wraparound is a
# certainty, not an edge case. Once an unrelated process inherits the watched
# PID number, a PID-only liveness test flips back to "alive" PERMANENTLY — the
# mechanism behind #7825's 18-day-old orphan renewal loops. Pairing the PID
# with the process's START TIME makes the handle identifying: the pair
# (pid, starttime) is not reused within any horizon that matters here.
#
# Linux: `/proc/<pid>/stat` field 22 (`starttime`, in clock ticks since boot)
# is monotonic, cheap, and world-readable. Field 2 (`comm`) can itself contain
# BOTH spaces and parentheses, so everything through the LAST ") " is stripped
# first; in the remainder, field 20 is the overall field 22.
# Elsewhere (macOS and any host without /proc): `ps -o lstart=`, whose
# one-second granularity combined with the PID space is still overwhelmingly
# identifying.
pid_start_identity() {
    local pid="${1:-}" stat_line rest ident
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    if [[ -r "/proc/$pid/stat" ]]; then
        stat_line=$(cat "/proc/$pid/stat" 2>/dev/null) || return 1
        [[ -n "$stat_line" ]] || return 1
        rest="${stat_line##*') '}"
        ident=$(printf '%s\n' "$rest" | awk '{print $20}')
        [[ -n "$ident" ]] || return 1
        printf 'starttime:%s' "$ident"
        return 0
    fi
    ident=$(ps -o lstart= -p "$pid" 2>/dev/null | tr -s '[:space:]' ' ')
    ident="${ident# }"
    ident="${ident% }"
    [[ -n "$ident" ]] || return 1
    printf 'lstart:%s' "$ident"
}

# Is `$1` a live process, biased to fail SAFE (#4691) — and, when a start-time
# identity token is supplied as `$2`, is it still the SAME process (#7825)?
#
# POSIX `kill(2)` has two distinct failure modes and a bare `kill -0` conflates
# them:
#   ESRCH — no such process        → genuinely dead, safe to prune.
#   EPERM — the process EXISTS but this caller may not signal it (different UID,
#           sandbox, namespace)    → NOT dead; pruning it destroys live state.
# `ps -p` answers "does this PID exist?" without needing signal permission, so it
# separates the two without parsing locale-dependent errno strings.
#
# #7825 (b): `ps` is consulted FIRST rather than behind a `kill -0` fast path.
# A zombie (state `Z`) has exited and is only awaiting reaping, so it counts as
# dead — but `kill -0` SUCCEEDS for a zombie, so under the pre-#7825 ordering
# the fast path returned "alive" and the `Z` branch was UNREACHABLE. The
# documented intent and the code now agree. `kill -0` survives only as the
# fallback for a host with no usable `ps`, never as the whole decision.
#
# #7825 (a): `$2`, when non-empty, is a token previously obtained from
# `pid_start_identity` for this same PID. It is re-probed on every call; a
# mismatch — or an identity that can no longer be read at all — reports DEAD
# even though the PID number is live, which is precisely the PID-reuse case.
# Callers that pass no token keep the pre-#7825 contract unchanged.
pid_is_live() {
    local pid="${1:-}" want_ident="${2:-}" state have_ident
    if ! [[ "$pid" =~ ^[0-9]+$ ]] || ((pid <= 0)); then
        return 1
    fi
    state=$(ps -o state= -p "$pid" 2>/dev/null | tr -d '[:space:]')
    if [[ -n "$state" ]]; then
        if [[ "${state:0:1}" == "Z" ]]; then
            return 1 # exited, awaiting reaping — dead for every purpose here
        fi
        # Any other state: the process exists. EPERM and friends land here too,
        # which is the #4691 fail-safe.
    elif ! kill -0 "$pid" 2>/dev/null; then
        return 1 # ESRCH, or no usable `ps` and no signal permission: dead.
    fi
    if [[ -n "$want_ident" ]]; then
        have_ident="$(pid_start_identity "$pid" 2>/dev/null || true)"
        [[ -n "$have_ident" ]] || return 1
        [[ "$have_ident" == "$want_ident" ]] || return 1
    fi
    return 0
}
# --- END shared pid-liveness block (#4691, #7825) --------------------------

iso_now() {
    date -u +"%Y-%m-%dT%H:%M:%SZ"
}

# Has a loop started at epoch `$1` outlived the cap `$2`? (Issue #7825.)
#
# A cap of 0 (or a non-numeric/unreadable value) means "unbounded" and always
# answers no -- the cap can only ever SHORTEN a loop's life, never extend it,
# so every failure mode of this helper is a no-op rather than a premature
# stop. Not part of the shared pid-liveness block: sweep-run-registry.sh runs
# no loop and has nothing to cap.
max_age_exceeded() {
    local started="${1:-}" cap="${2:-0}" now
    [[ "$started" =~ ^[0-9]+$ ]] || return 1
    [[ "$cap" =~ ^[0-9]+$ ]] || return 1
    ((cap > 0)) || return 1
    now="$(date -u +%s 2>/dev/null || true)"
    [[ "$now" =~ ^[0-9]+$ ]] || return 1
    ((now - started >= cap))
}

# --- renew-once --------------------------------------------------------

cmd_renew_once() {
    local issue="${1:-}"
    shift || true
    [[ "$issue" =~ ^[0-9]+$ ]] || {
        echo "ERROR: renew-once requires a positive integer issue number (got: '${issue:-}')" >&2
        exit 1
    }

    local host="" sweep_id="" cached=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --host) host="${2:-}"; shift 2 ;;
            --sweep-id) sweep_id="${2:-}"; shift 2 ;;
            --cached-lease) cached="${2:-}"; shift 2 ;;
            *) echo "ERROR: renew-once: unknown flag '$1'" >&2; exit 1 ;;
        esac
    done

    local repo_path
    repo_path="$(gh_repo_path)"

    local exact=""
    if [[ -n "$host" && -n "$sweep_id" ]]; then
        exact="${LEASE_MARKER_PREFIX}${host} sweep=${sweep_id} -->"
    elif [[ -n "$host" || -n "$sweep_id" ]]; then
        echo "ERROR: renew-once: --host and --sweep-id must both be given, or neither" >&2
        exit 1
    fi
    # --cached-lease ID@SINCE (Issue #10021; SINCE is a cursor, #10229): the loop's remembered lease
    # comment. Validated strictly because SINCE is spliced into a URL.
    if [[ -n "$cached" && ! "$cached" =~ ^[0-9]+@[0-9TZ:-]+$ ]]; then
        echo "ERROR: renew-once: --cached-lease must be <comment-id>@<since> (got: '$cached')" >&2
        exit 1
    fi
    # gh-call attribution (Issue #10139): the detached renewer is reparented
    # to init/launchd, so a gh shim walking its parent chain never reaches the
    # sweep; these two variables are the only way its forge calls can be
    # attributed. Every forge call -- including the detached `start` loop's,
    # which all run through this subcommand with the resolved --sweep-id --
    # happens after this point. Telemetry only: lease targeting still uses the
    # local host/sweep pair. An empty sweep id keeps any inherited
    # LOOM_SWEEP_ID (never invent or erase one).
    [[ -z "$sweep_id" ]] || export LOOM_SWEEP_ID="$sweep_id"
    export LOOM_ROLE="sweep-lease-renew"
    # The cache-miss path: the same invocation minus the cache, i.e. today's
    # full paginated lookup. `exec` (not a call) so it can never recurse twice.
    # Each re-list exports its reason as LOOM_LEASE_FALLBACK_REASON (#10229,
    # gh-shim telemetry: full-window / missing-comment / patch-404); an uncached
    # call without one is an initial lookup.
    local -a relist=("$SELF" renew-once "$issue" ${host:+--host "$host" --sweep-id "$sweep_id"})

    # Routed through forge_gh_perm_safe (Issue #6541) so a GitHub
    # App-installation permission-scope 403 escalates through a fresh
    # installation-token mint, then a personal token, instead of failing this
    # (and every subsequent) renewal cycle silently. stdout/stderr are kept
    # separate here (unlike a plain `2>&1` capture) -- forge_gh_perm_safe
    # writes its own escalation-ladder diagnostics to stderr even on an
    # eventual SUCCESS, and merging those into $comments_json would corrupt
    # the JSON this function is about to parse.
    #
    # Plain `gh` via forge_gh_perm_safe, never gh-cached (docs/gh-cached.md
    # policy, #9953): lease comments drive own-yield/fence decisions (CAS-style
    # claim), so a 30s-stale read could renew a lease that was just yielded.
    # Steady state (Issue #10021): with --cached-lease, ONE non-paginated page
    # of the comments updated since the cursor (below) replaces the
    # `--paginate` listing of the whole issue. That window always contains the
    # lease comment itself (its updated_at only moves forward) and every
    # `loom:lease-yield` record posted after it, so the own-yield guard below
    # sees exactly what it would in the full listing. A full page (100) means
    # the window may be truncated, so it is treated as a cache miss.
    #
    # Sliding window (#10229): the cached cursor is the lease comment's
    # updated_at AS LISTED by the previous cycle -- i.e. that cycle's view of
    # the PATCH before it -- not its created_at. Every comment created after
    # the previous listing has a later updated_at, so the own-yield guard still
    # sees every new yield record, while the window holds about two intervals of
    # activity instead of everything since the claim, and no longer fills a
    # page (the `full-window` fallback) on a long-lived, busy issue.
    local comments_json endpoint="repos/${repo_path}/issues/${issue}/comments" paginate="--paginate"
    [[ -z "$cached" ]] || { endpoint+="?since=${cached#*@}&per_page=100"; paginate=""; }
    if ! comments_json="$(lease_gh read api "$endpoint" ${paginate:+"$paginate"})"; then
        echo "ERROR: 'gh api .../issues/${issue}/comments${paginate:+ $paginate}' failed (escalation ladder exhausted)" >&2
        exit 1
    fi
    if [[ -n "$cached" && "$(jq 'if type == "array" then length else 100 end' <<< "$comments_json" 2> /dev/null || echo 100)" -ge 100 ]]; then
        echo "cached lease comment window for issue #${issue} is a full page; re-listing all comments (#10021) lease-fallback=full-window" >&2
        LOOM_LEASE_FALLBACK_REASON=full-window exec "${relist[@]}"
    fi
    # #9548: renew only a TRUSTED author's lease, and honour only a trusted
    # yield record; an outsider's copy of either is prose.
    # requires-daemon: forge optional   Without the `trusted-comments` verb the listing cannot be authenticated: exit 1, the same transient failure as an unreadable listing (nothing is patched).
    comments_json="$("${LOOM_DAEMON_BIN:-loom-daemon}" forge trusted-comments <<< "$comments_json" 2> /dev/null)" \
        || { echo "ERROR: lease comments on issue #${issue} could not be authenticated (loom-daemon forge trusted-comments unavailable)" >&2; exit 1; }

    # Prints "<id>@<updated_at as listed>" (the next window's cursor, #10229;
    # created_at for a shape without updated_at). A cached id must still pass
    # every filter (trusted, marker, exact match) -- one that stopped matching
    # is stale.
    local candidate candidate_id
    candidate="$(jq -r --arg marker "$LEASE_MARKER_PREFIX" --arg exact "$exact" --arg cid "${cached%%@*}" '
        [ .[] | select(.body != null and (.body | startswith($marker)))
              | select($exact == "" or (.body | startswith($exact)))
              | select($cid == "" or (.id | tostring) == $cid) ]
        | sort_by(.id) | reverse | .[0] // empty | "\(.id)@\(.updated_at // .created_at)"
    ' <<< "$comments_json" 2>/dev/null || true)"
    candidate_id="${candidate%%@*}"

    if [[ -z "$candidate_id" && -n "$cached" ]]; then
        echo "cached lease comment ${cached%%@*} is gone from issue #${issue} (deleted or no longer matching); re-listing all comments (#10021) lease-fallback=missing-comment" >&2
        LOOM_LEASE_FALLBACK_REASON=missing-comment exec "${relist[@]}"
    elif [[ -z "$candidate_id" ]]; then
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
    # #9548: PATCH only a repo this installation manages and can write,
    # named explicitly rather than through the `{owner}/{repo}` placeholder.
    #
    # stderr is captured (and always replayed) so a 404 -- the comment was
    # deleted between the read and the write -- is recognisable: on the
    # cached path it re-lists instead of failing (Issue #10021).
    local patch_body_file write_repo patch_err patch_rc=0
    write_repo="$(loom_write_repo "${LOOM_REPO:-}")" || { echo "ERROR: not renewing lease comment ${candidate_id} on issue #${issue}: loom-daemon forge may-write refused the repo (#9548)" >&2; exit 1; }
    patch_body_file="$(mktemp)"
    printf '%s' "$new_body" > "$patch_body_file"
    patch_err="$(lease_gh write api --method PATCH "repos/${write_repo}/issues/comments/${candidate_id}" \
        -F "body=@${patch_body_file}" 2>&1 > /dev/null)" || patch_rc=$?
    rm -f "$patch_body_file"
    [[ -z "$patch_err" ]] || printf '%s\n' "$patch_err" >&2
    if ((patch_rc != 0)) && [[ -n "$cached" && "$patch_err" == *"HTTP 404"* ]]; then
        echo "cached lease comment ${candidate_id} on issue #${issue} returned 404; re-listing all comments (#10021) lease-fallback=patch-404" >&2
        LOOM_LEASE_FALLBACK_REASON=patch-404 exec "${relist[@]}"
    elif ((patch_rc != 0)); then
        echo "ERROR: PATCH of lease comment ${candidate_id} on issue #${issue} failed" >&2
        exit 1
    fi

    # `lease-cache=<id>@<since>` is the token `start`'s loop parses to
    # pass --cached-lease on its next cycle (Issue #10021).
    echo "renewed lease comment ${candidate_id} for issue #${issue} at ${now_iso} lease-cache=${candidate}" >&2
}

# --- start ---------------------------------------------------------------

cmd_start() {
    # Issue #10203: re-enter once through the daemon, which marks every fd the
    # caller leaked (above 2) close-on-exec before exec'ing us again. Skipped
    # when the binary predates `sanitize-exec` (--check) so `start` stays fail-open.
    [[ -n "${LOOM_RENEW_FDS_CLEAN:-}" ]] || ! "${LOOM_DAEMON_BIN:-loom-daemon}" lease renewer sanitize-exec --check > /dev/null 2>&1 || LOOM_RENEW_FDS_CLEAN=1 exec "${LOOM_DAEMON_BIN:-loom-daemon}" lease renewer sanitize-exec -- "$SELF" start "$@"
    local issue="${1:-}"
    shift || true
    [[ "$issue" =~ ^[0-9]+$ ]] || {
        echo "ERROR: start requires a positive integer issue number (got: '${issue:-}')" >&2
        exit 1
    }

    local interval="$DEFAULT_INTERVAL_SECS" watch_pid="" host="" sweep_id=""
    local watch_ident="" max_age="$DEFAULT_MAX_AGE_SECS"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --interval) interval="${2:-}"; shift 2 ;;
            --watch-pid) watch_pid="${2:-}"; shift 2 ;;
            --watch-ident) watch_ident="${2:-}"; shift 2 ;;
            --max-age) max_age="${2:-}"; shift 2 ;;
            --host) host="${2:-}"; shift 2 ;;
            --sweep-id) sweep_id="${2:-}"; shift 2 ;;
            *) echo "ERROR: start: unknown flag '$1'" >&2; exit 1 ;;
        esac
    done

    if ! [[ "$interval" =~ ^[0-9]+$ ]] || ! ((interval > 0)); then
        echo "ERROR: --interval must be a positive integer (got: '$interval')" >&2
        exit 1
    fi

    # Refuse a mis-split identity pair instead of forking a doomed loop
    # (Issue #7876). `sweep-lease-publish.sh publish` prints ONE line,
    # "<host> <sweep-id>"; a caller that splits it with bash's unquoted
    # word-splitting gets the WHOLE line as --host and an empty --sweep-id
    # under zsh (SH_WORD_SPLIT is off there), which is exactly what the
    # orchestrator's login shell did. That combination forks a loop whose
    # exact-match targeting (#6485) can never match its own lease comment, so
    # the lease silently ages out while the operator believes renewal is
    # running -- strictly worse than no loop at all. Fail loudly at `start`.
    local ident_hint
    ident_hint="use 'read -r LEASE_HOST LEASE_SWEEP <<<\"\$LEASE_IDENT\"' (bash AND zsh) to split sweep-lease-publish.sh's \"<host> <sweep-id>\" output, never 'set -- \$LEASE_IDENT'"
    if [[ "$host" =~ [[:space:]] ]]; then
        echo "ERROR: start: --host must not contain whitespace (got: '$host') -- $ident_hint" >&2
        exit 1
    fi
    if [[ "$sweep_id" =~ [[:space:]] ]]; then
        echo "ERROR: start: --sweep-id must not contain whitespace (got: '$sweep_id') -- $ident_hint" >&2
        exit 1
    fi
    # Exact-match targeting needs BOTH fields; `renew-once` rejects a partial
    # pair, so a `start` that forwards one alone produces a loop that fails
    # every cycle. Passing NEITHER stays legal (the documented "newest wins"
    # fallback plus the auto-resolution below).
    if [[ -n "$host" && -z "$sweep_id" ]]; then
        echo "ERROR: start: --host '$host' given without a non-empty --sweep-id -- $ident_hint" >&2
        exit 1
    fi
    if [[ -n "$sweep_id" && -z "$host" ]]; then
        echo "ERROR: start: --sweep-id '$sweep_id' given without a non-empty --host -- $ident_hint" >&2
        exit 1
    fi
    # 0 = unbounded; anything else must be a non-negative integer. A bogus
    # value is a hard error rather than a silent fall-back to "unbounded",
    # because "unbounded" is the exact state #7825 exists to make impossible.
    if ! [[ "$max_age" =~ ^[0-9]+$ ]]; then
        echo "ERROR: max age must be a non-negative integer (0 = unbounded); got: '$max_age' (--max-age / SWEEP_LEASE_RENEW_MAX_AGE_SECS)" >&2
        exit 1
    fi

    [[ -n "$watch_pid" ]] || watch_pid="$(resolve_liveness_pid)"
    [[ "$watch_pid" =~ ^[0-9]+$ ]] || {
        echo "ERROR: could not resolve a watch PID" >&2
        exit 1
    }

    # Pin the watched PID's start-time identity NOW (Issue #7825, defect (a)),
    # unless the caller supplied one explicitly. From here on the loop's
    # liveness test is the PAIR (pid, identity), so the kernel recycling this
    # PID number onto an unrelated process reads as DEAD instead of flipping
    # the loop back to "alive" forever. An unresolvable identity (no /proc, no
    # usable `ps`, or the watched process died in the microseconds between the
    # caller capturing its PID and this probe) leaves the token empty and the
    # loop falls back to the pre-#7825 PID-only test -- still bounded by the
    # absolute age cap below, which is why that cap is not optional.
    [[ -n "$watch_ident" ]] || watch_ident="$(pid_start_identity "$watch_pid" 2>/dev/null || true)"

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
    if [[ -z "$host" && -z "$sweep_id" && "${LOOM_TERMINAL_ID:-}" == daemon-?* ]]; then
        local auto_host
        auto_host="$(resolve_published_host)"
        [[ -z "$auto_host" ]] || { host="$auto_host"; sweep_id="${LOOM_TERMINAL_ID#daemon-}"; }
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
    #
    # Issue #7825 adds two more exits, both unconditional:
    #   - the watch test is now the PAIR (pid, start-time identity), so a
    #     RECYCLED pid number reads as dead instead of resurrecting the loop;
    #   - `max_age_exceeded` stops the loop after $max_age seconds no matter
    #     what the watch test says. It is checked on BOTH sides of the sleep so
    #     a loop whose watch target is immortal still exits within one interval
    #     of the cap rather than at the next liveness transition (which, for an
    #     immortal target, never comes).
    #
    # Issue #10021: the loop REMEMBERS its lease comment. A successful cycle
    # reports `lease-cache=<id>@<since>` on stderr; the next cycle passes
    # it back as --cached-lease, so steady state is one non-paginated read plus
    # one PATCH instead of a `--paginate` listing of every comment. renew-once
    # itself falls back to the full listing on a miss or a PATCH 404. A failed
    # cycle keeps the cache; exit 2 (no lease) clears it.
    local loop_started_at lease_cache_re='lease-cache=([0-9]+@[0-9TZ:-]+)'
    loop_started_at="$(date -u +%s)"
    #
    # Issue #10203: the loop (and its `sleep` children) must hold no fd it
    # inherited from the caller except its own log (fd 9), so the closing
    # redirect below also closes 3-8. An inherited fd 3 -- worktree.sh's saved
    # stdout, i.e. a `worktree.sh N | tail` pipe -- otherwise kept that pipe
    # open for the loop's whole 4h lifetime. `cmd_start` re-enters itself
    # through `loom-daemon lease renewer sanitize-exec` so fds 10+ are
    # closed too; `loom-daemon lease ensure` does the same marking before `start`.
    # Issue #10229: one renewer per (repo, host, sweep, issue), and a cycle
    # that ends the loop once the issue is closed, even while the watched
    # interactive parent lives on. The decisions live in `loom-daemon lease
    # renewer`; the state read stays here, on lease_gh's credentials. Its stderr
    # (credential fallback/recovery tags, ladder lines, errors) goes to fd 9 like
    # the other two calls' diagnostics, never /dev/null.
    # check: 3 = stop for good (closed / released / superseded), 4 = state
    # unverified, skip this PATCH; anything else (incl. an older binary) renews.
    local -a owner_args=("$issue" --host "$host" --sweep-id "$sweep_id" --token "$$.${RANDOM}.${loop_started_at}")
    #
    # A cycle whose authenticated listing finds no lease comment (renew-once
    # exit 2) re-lists with --paginate next time, because there is nothing to
    # cache. Before #10229 such a loop paid a full listing every interval for
    # its whole life -- the recurring `--paginate` share in the gh-shim data. The
    # lease is published before `start` on every path, so two consecutive
    # misses (one interval apart, any success in between resets the count) mean
    # there is nothing this loop can ever renew: it stops.
    local cap_msg="sweep-lease-renew: renewal loop for issue #${issue} exiting: reached the ${max_age}s absolute lifetime cap (SWEEP_LEASE_RENEW_MAX_AGE_SECS / --max-age, #7825). The lease now ages out and the claim becomes reclaimable; set the cap to 0 to disable it."
    (
        cached_lease="" misses=0
        while pid_is_live "$watch_pid" "$watch_ident"; do
            ! max_age_exceeded "$loop_started_at" "$max_age" || { echo "$cap_msg" >&9; break; }
            sleep "$interval"
            pid_is_live "$watch_pid" "$watch_ident" || break
            ! max_age_exceeded "$loop_started_at" "$max_age" || { echo "$cap_msg" >&9; break; }
            gate_rc=0
            issue_state="$(export LOOM_ROLE=sweep-lease-renew; [[ -z "$sweep_id" ]] || export LOOM_SWEEP_ID="$sweep_id"; lease_gh read api "repos/$(gh_repo_path)/issues/${issue}" --jq .state 2>&9)" || issue_state=""
            "${LOOM_DAEMON_BIN:-loom-daemon}" lease renewer check "${owner_args[@]}" --issue-state "$issue_state" 2>&9 || gate_rc=$?
            ((gate_rc != 3)) || break
            ((gate_rc != 4)) || continue
            renew_rc=0
            # The `${arr[@]+...}` guard below is mandatory -- NOT an
            # unguarded expansion (Issue #8333, same defect class as #8281):
            # under bash 3.2 (stock macOS /bin/bash) + `set -u`, expanding an
            # EMPTY array dies with "extra_args[@]: unbound variable". The
            # array is empty on every legal invocation that passed neither
            # --host nor --sweep-id AND could not auto-resolve them above
            # (any $LOOM_TERMINAL_ID without a `daemon-` prefix), so on those
            # hosts each cycle's command substitution died and the lease was
            # never actually renewed -- silently, since `|| renew_rc=$?`
            # catches it and only a generic FAILED line reached fd 9.
            renew_err="$("$SELF" renew-once "$issue" "${extra_args[@]+"${extra_args[@]}"}" ${cached_lease:+--cached-lease "$cached_lease"} 2>&1 > /dev/null)" || renew_rc=$?
            if [[ "$renew_rc" -eq 0 || "$renew_rc" -eq 2 ]]; then
                cached_lease=""
                if [[ "$renew_rc" -eq 0 && "$renew_err" =~ $lease_cache_re ]]; then cached_lease="${BASH_REMATCH[1]}"; fi
            elif [[ "$renew_rc" -ne 4 ]]; then
                echo "sweep-lease-renew: renewal cycle for issue #${issue} FAILED (renew-once exit ${renew_rc}): ${renew_err}" >&9
            fi
            [[ "$renew_rc" -ne 4 ]] || break
            case "$renew_rc" in 0) misses=0 ;; 2) misses=$((misses + 1)) ;; esac
            ((misses < 2)) || { echo "sweep-lease-renew: renewal loop for issue #${issue} exiting: no lease comment to renew on two consecutive cycles (#10229)" >&9; break; }
        done
    ) < /dev/null > /dev/null 2>&1 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- &
    local loop_pid=$! owner_pid=""
    # A live renewer already owns this key: drop the loop just forked (still in
    # its first sleep, no forge call made) and report the owner's pid instead.
    owner_pid="$("${LOOM_DAEMON_BIN:-loom-daemon}" lease renewer claim "${owner_args[@]}" --pid "$loop_pid" 2>&9)" || owner_pid=""
    [[ ! "$owner_pid" =~ ^[0-9]+$ || "$owner_pid" == "$loop_pid" ]] || { kill "$loop_pid" 2> /dev/null || true; loop_pid="$owner_pid"; }
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
        release) exec "${LOOM_DAEMON_BIN:-loom-daemon}" lease renewer release "$@" ;;
        -h | --help | "") usage ;;
        *)
            echo "ERROR: unknown command '$cmd'" >&2
            usage
            ;;
    esac
}

main "$@"
