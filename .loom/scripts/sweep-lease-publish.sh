#!/usr/bin/env bash
# sweep-lease-publish.sh - Publish a lease record from an IN-SESSION
# `/loom:sweep` run (Issue #6320, Epic #6165).
#
# ## Why this exists
#
# Epic #6165 gives the `loom:building` claim a liveness dimension: a
# `<!-- loom:lease host=<host> sweep=<sweep-id> -->` marker comment whose
# forge-assigned `updated_at` is the freshness signal every host reads
# (`defaults/docs/lease-record.md`). Until this script, exactly ONE dispatch
# path ever wrote that record: `loom-daemon`'s
# `SweepRegistry::write_lease_comment`, called right after its own
# `flip_label_to_building` (#6179).
#
# An in-session `/loom:sweep` (an operator running `/loom:sweep N`, a
# `--no-daemon` run, a GH Actions cron invocation) dispatches its Builder
# through the Task tool one level deep -- there is no `SweepRegistry` entry
# and, before this script, no lease record either. That is a **hole exactly
# the shape of the in-session path**: a live in-session claim was
# indistinguishable from an abandoned one, so any daemon -- local or remote --
# consulting lease records to answer "is anyone still working this?" saw a
# bare `loom:building` label with no lease and reclaimed it. Observed
# consequence (#6320): a fleet-dispatch bot stripped a 9-minute-old in-session
# claim, re-claimed it, and dispatched a second Builder into the SAME
# `.loom/worktrees/issue-<N>` the first was still working in; the second ran
# `git reset --hard HEAD` and discarded the first's uncommitted edits.
#
# This script closes the writer-side hole: the in-session path publishes the
# same signal the daemon path does, in the same format, so both are read
# identically by every existing reader.
#
# ## What reads what this writes
#
#   - `loom-daemon`'s reclamation gate (#6286, Phase 2): a lease fresh within
#     `LOOM_LEASE_TTL_MINUTES` (default 15) REFUSES the reclaim of that
#     claim, whatever the host-scoped evidence concluded. This is the gate
#     that would have prevented #6320's data loss.
#   - `loom-daemon`'s dispatch-time claim-then-verify-order dedup (#6287,
#     Phase 2): a dispatcher that finds a peer's lease comment with an
#     EARLIER forge comment `id` inside its lookback window yields before
#     spawning a builder or touching a worktree. An in-session lease
#     published at pre-flight therefore makes a racing daemon dispatch stand
#     down rather than co-occupy the worktree.
#   - `sweep-lease-fence.sh check` (#6309, Phase 3): the sweep's own
#     pre-push/pre-PR fence.
#   - `sweep-lease-renew.sh` (#6180): keeps the record fresh for the sweep's
#     lifetime. **Pass this script's `--host`/`--sweep-id` through to
#     `renew-once`/`start`** so renewal targets THIS sweep's own lease
#     exactly, rather than "newest wins" -- otherwise a peer's later lease
#     comment would be the one this sweep keeps alive.
#
# ## Publish-at-pre-flight, not publish-at-claim
#
# The daemon writes its lease immediately AFTER flipping the label, because
# the daemon itself performs the flip. In-session, the label flip is done by
# the Builder subagent later, so the orchestrator publishes the lease at
# per-issue pre-flight -- the moment it decides this sweep will work `N`.
# That is deliberate and is the useful semantics: the question a reader asks
# is "is a live worker on this issue right now?", and the pre-flight instant
# is when that becomes true. It also puts the lease comment's `id` EARLIER
# than a racing daemon dispatch's, which is precisely what #6287's ordering
# check needs to make the daemon yield.
#
# ## Idempotency and peer safety
#
#   - If THIS host+sweep-id already has a FRESH (within-TTL) lease comment on
#     the issue, this is a no-op (exit 0) -- a resumed sweep re-running
#     pre-flight never accumulates duplicate lease comments. This is
#     TTL-bounded, not unconditional: once that lease ages past the TTL, the
#     next pre-flight publishes a fresh duplicate rather than treating the
#     stale one as still covering this sweep.
#   - If a DIFFERENT host holds a lease that is still fresh, this script does
#     NOT publish (exit 4). Publishing would supersede a live peer's liveness
#     signal for every freshest-wins reader (`sweep-lease-fence.sh`,
#     `fetch_freshest_lease_updated_at`) and hand this sweep a claim a live
#     worker still holds. The caller should skip the issue.
#   - A stale (past-TTL) lease -- this host's or a peer's -- does not block
#     publication: it is exactly the abandoned-claim case a new lease should
#     supersede.
#
# ## Fail-open, like every other forge probe in this subsystem
#
# A `gh` read failure means "no evidence", never "a peer owns it": the script
# publishes anyway (a duplicate lease comment is harmless -- readers take the
# freshest). A `gh` WRITE failure exits 2 and the caller proceeds without a
# lease, exactly as it did before this script existed. This mirrors
# `write_lease_comment`'s fail-open contract (#6179) -- the `loom:building`
# label remains the authoritative claim; a lease only improves the evidence.
#
# ## Commands
#
#   sweep-lease-publish.sh publish <issue> [--host HOST] [--sweep-id ID]
#                                          [--ttl-minutes N]
#     Publish a lease record for <issue>. --host defaults to the PUBLISHED
#     form of this host's own identity (Issue #6322): the opaque id
#     (`opaque_host_id`, mirroring `sweep_registry::opaque_host_id` byte for
#     byte) of the raw identity `sweep_registry::host_identity()` resolves
#     (`LOOM_HOST_ID` > `$HOSTNAME` > the `hostname` binary > `unknown-host`)
#     -- unless `LOOM_LEASE_PUBLISH_HOSTNAME` opts into raw publishing, in
#     which case the raw identity is used directly. An explicit --host is
#     used verbatim (no transform applied). This is the same identity
#     `sweep-lease-fence.sh check`'s default `--host` resolves, so a publish
#     followed by an unqualified fence check on the same host always agrees
#     on which record is "this sweep's own" (#6333).
#     --sweep-id defaults to `$LOOM_SWEEP_RUN_ID`, else a generated
#     `sweep-insession-<UTC>-<pid>`; callers inside `/loom:sweep` should pass
#     their `$RUN_ID` (`sweep-run-registry.sh new`) so the lease, the run
#     registry, and the checkpoints all key on one identity. --ttl-minutes
#     defaults to `LOOM_LEASE_TTL_MINUTES` or 15 (Phase 2's default).
#
#     Exit codes:
#       0  Published, or a live lease for this host+sweep-id already exists.
#          Proceed.
#       1  Usage error (bad issue number, unknown flag, bad --ttl-minutes).
#       2  The publish `gh` call failed. Proceed WITHOUT a lease (best-effort).
#       4  A DIFFERENT host holds a fresh lease -- nothing published. The
#          caller should skip this issue rather than co-occupy the claim.
#
#     On exit 0 (published or already-held), the resolved identity is printed
#     to stdout as a single line: `<host> <sweep-id>` -- thread it into
#     `sweep-lease-renew.sh start <issue> --host <host> --sweep-id <id>`.
#
# Usage:
#   .loom/scripts/sweep-lease-publish.sh publish 6320
#   .loom/scripts/sweep-lease-publish.sh publish 6320 --sweep-id "$RUN_ID"

set -euo pipefail

LEASE_MARKER_PREFIX="<!-- loom:lease host="
DEFAULT_TTL_MINUTES="${LOOM_LEASE_TTL_MINUTES:-15}"

# --- Opaque host id (Issue #6322, ported from sweep-lease-fence.sh) --------
# `write_lease_comment` (`loom-daemon/src/sweep_registry/guards.rs`) publishes
# an OPAQUE id for `host=`, not the raw hostname `resolve_host` below
# resolves, so a public forge comment never carries a machine name (which
# commonly embeds a person's name). `opaque_host_id` here is a byte-for-byte
# bash port of `sweep_registry::opaque_host_id` — same salt, same "host-" +
# first 8 lowercase hex chars of sha256(salt+host) shape — so this script's
# own default `host=` publication matches every other writer of the same
# lease-record contract.
LEASE_HOST_SALT="loom-lease-host-id-v1:"

usage() {
    awk 'NR < 3 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
    exit 1
}

# --- Repo-relative `gh` targeting (mirrors sweep-lease-fence.sh) -----------
gh_repo_args() {
    if [[ -n "${LOOM_REPO:-}" ]]; then
        printf -- '-R\n%s\n' "$LOOM_REPO"
    fi
}

# --- sha256 hex digest of stdin, tolerating either common tool -------------
sha256_hex_stdin() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 | awk '{print $1}'
    elif command -v sha256sum >/dev/null 2>&1; then
        sha256sum | awk '{print $1}'
    else
        return 1
    fi
}

# opaque_host_id <host> -- prints "host-" + the first 8 hex chars of
# sha256(LEASE_HOST_SALT + host), mirroring `sweep_registry::opaque_host_id`
# exactly. Prints nothing and returns non-zero if neither `shasum` nor
# `sha256sum` is available (see `resolve_published_host`'s fallback for the
# fail-open handling of that case -- an extremely rare environment, not
# expected in practice).
opaque_host_id() {
    local host="$1" hash
    hash="$(printf '%s%s' "$LEASE_HOST_SALT" "$host" | sha256_hex_stdin)" || return 1
    [[ -n "$hash" ]] || return 1
    printf 'host-%s' "${hash:0:8}"
}

# lease_publish_raw_hostname -- true (exit 0) when `LOOM_LEASE_PUBLISH_HOSTNAME`
# opts into publishing the raw hostname, mirroring
# `SweepRegistry::lease_publish_raw_hostname`'s exact truthy-token set and
# env-only precedence (no per-repo config key -- see that function's Rust
# doc comment for why: this script has no access to `loom-daemon`'s own
# config resolution, so env is the only source both sides can agree on).
lease_publish_raw_hostname() {
    case "$(printf '%s' "${LOOM_LEASE_PUBLISH_HOSTNAME:-}" | tr '[:upper:]' '[:lower:]' | xargs)" in
        1 | true | yes | on) return 0 ;;
        *) return 1 ;;
    esac
}

# --- Host identity, mirroring sweep_registry::host_identity()'s precedence -
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
    h="$(hostname 2>/dev/null || true)"
    if [[ -n "$h" ]]; then
        printf '%s' "$h"
        return 0
    fi
    printf 'unknown-host'
}

# resolve_published_host -- the host identity this script publishes and
# compares by default (no explicit --host): the opaque id of resolve_host()'s
# raw value, or the raw value itself when lease_publish_raw_hostname opts in.
# Falls back to the raw value if the opaque transform is unavailable (no
# sha256 tool) -- a degraded-but-non-blocking outcome, matching
# sweep-lease-fence.sh's identical fallback so both sides of the publish/fence
# contract agree even in that rare environment.
resolve_published_host() {
    local raw
    raw="$(resolve_host)"
    if lease_publish_raw_hostname; then
        printf '%s' "$raw"
        return 0
    fi
    opaque_host_id "$raw" || printf '%s' "$raw"
}

# --- ISO-8601 -> epoch (portable across GNU and BSD/macOS date, mirrors
# sweep-lease-fence.sh's `iso_to_epoch`) ------------------------------------
iso_to_epoch() {
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

# --- Parse `host=`/`sweep=` out of a lease marker's literal first line
# (mirrors SweepRegistry::parse_lease_marker_line and
# sweep-lease-fence.sh's copy). Prints "host<TAB>sweep", nothing if malformed.
parse_lease_marker_line() {
    local first_line="$1" rest host sweep_id
    rest="${first_line#"$LEASE_MARKER_PREFIX"}"
    [[ "$rest" == "$first_line" ]] && return 1 # prefix did not match
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

gen_sweep_id() {
    printf 'sweep-insession-%s-%s' "$(date -u +%Y%m%dT%H%M%SZ)" "$$"
}

cmd_publish() {
    local issue="${1:-}"
    shift || true
    [[ "$issue" =~ ^[0-9]+$ ]] || {
        echo "ERROR: publish requires a positive integer issue number (got: '${issue:-}')" >&2
        exit 1
    }

    local host="" sweep_id="" ttl_minutes="$DEFAULT_TTL_MINUTES"
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
            --ttl-minutes)
                ttl_minutes="${2:-}"
                shift 2
                ;;
            *)
                echo "ERROR: publish: unknown flag '$1'" >&2
                exit 1
                ;;
        esac
    done

    # Issue #6322 / #6333: default resolution must match what every other
    # writer of this contract actually PUBLISHES (opaque by default), not the
    # raw hostname -- an explicit --host is a caller-supplied literal and is
    # used verbatim, unmodified by this transform.
    [[ -n "$host" ]] || host="$(resolve_published_host)"
    [[ -n "$sweep_id" ]] || sweep_id="${LOOM_SWEEP_RUN_ID:-$(gen_sweep_id)}"
    if ! [[ "$ttl_minutes" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
        echo "ERROR: publish: --ttl-minutes must be a non-negative number (got: '$ttl_minutes')" >&2
        exit 1
    fi
    # The marker's grammar is flat and space-delimited (`host=<H> sweep=<S>`),
    # so neither field may contain whitespace or the closing `-->`.
    if [[ "$host" == *[[:space:]]* || "$sweep_id" == *[[:space:]]* \
        || "$host" == *"-->"* || "$sweep_id" == *"-->"* ]]; then
        echo "ERROR: publish: --host/--sweep-id must not contain whitespace or '-->' (got host='$host' sweep-id='$sweep_id')" >&2
        exit 1
    fi

    local -a repo_args=()
    while IFS= read -r line; do
        [[ -n "$line" ]] && repo_args+=("$line")
    done < <(gh_repo_args)

    # --- Read existing lease comments (NDJSON; see sweep-lease-fence.sh on
    # why this is deliberately NOT an array literal under `--paginate`) -----
    local comments_ndjson read_ok=1
    if ! comments_ndjson="$(gh api "${repo_args[@]}" "repos/{owner}/{repo}/issues/${issue}/comments" \
        --paginate --jq \
        ".[] | select(.body != null and (.body | startswith(\"${LEASE_MARKER_PREFIX}\"))) | {updated_at: .updated_at, body: .body}" \
        2>&1)"; then
        echo "WARN: could not read existing lease comments for issue #${issue} (${comments_ndjson}) -- publishing anyway (absence of evidence is not evidence of a peer)" >&2
        read_ok=0
        comments_ndjson=""
    fi

    if ((read_ok == 1)) && [[ -n "$(printf '%s' "$comments_ndjson" | tr -d '[:space:]')" ]]; then
        # Scan EVERY fresh lease comment for a foreign host -- not just the
        # single most-recently-updated one. Looking only at the overall
        # freshest comment misses the case where THIS host also has its own
        # more-recently-updated (but still merely fresh) lease from a
        # restarted sweep-id: that comment sorts last, takes the same-host
        # "NOTE" branch, and an older-but-still-fresh PEER lease from a
        # genuinely different host is never inspected -- letting two hosts
        # hold simultaneously-fresh leases on the same issue, exactly the
        # double-claim state this script exists to prevent (#6333).
        local now_epoch ttl_seconds
        now_epoch="${LOOM_LEASE_PUBLISH_NOW:-$(date -u +%s)}"
        ttl_seconds="$(awk -v m="$ttl_minutes" 'BEGIN { printf "%d", m * 60 }')"

        local own_fresh=0 same_host_diff_fresh=0
        local peer_host="" peer_sweep="" peer_updated_at=""
        local comment_line
        while IFS= read -r comment_line; do
            [[ -z "$comment_line" ]] && continue
            local c_updated_at c_body c_first_line c_parsed c_host c_sweep
            c_updated_at="$(jq -r '.updated_at // empty' <<< "$comment_line" 2>/dev/null || true)"
            c_body="$(jq -r '.body // empty' <<< "$comment_line" 2>/dev/null || true)"
            [[ -z "$c_updated_at" || -z "$c_body" ]] && continue
            c_first_line="${c_body%%$'\n'*}"
            c_parsed="$(parse_lease_marker_line "$c_first_line" || true)"
            [[ -z "$c_parsed" ]] && continue
            c_host="${c_parsed%%$'\t'*}"
            c_sweep="${c_parsed#*$'\t'}"

            local c_updated_epoch c_age_seconds c_is_fresh=0
            if c_updated_epoch="$(iso_to_epoch "$c_updated_at")"; then
                c_age_seconds=$((now_epoch - c_updated_epoch))
                ((c_age_seconds < 0)) && c_age_seconds=0
                ((c_age_seconds <= ttl_seconds)) && c_is_fresh=1
            fi
            ((c_is_fresh == 0)) && continue

            if [[ "$c_host" == "$host" && "$c_sweep" == "$sweep_id" ]]; then
                own_fresh=1
            elif [[ "$c_host" != "$host" ]]; then
                # Remember the first foreign fresh lease found -- any single
                # one is sufficient to block publication below.
                if [[ -z "$peer_host" ]]; then
                    peer_host="$c_host"
                    peer_sweep="$c_sweep"
                    peer_updated_at="$c_updated_at"
                fi
            else
                same_host_diff_fresh=1
            fi
        done <<< "$(jq -c '.' <<< "$comments_ndjson" 2>/dev/null || true)"

        # Check for a live peer FIRST, even if this host also has its own
        # fresh record -- superseding a genuine peer's lease is the failure
        # mode this script exists to prevent, so it takes priority over the
        # idempotent-no-op case below.
        if [[ -n "$peer_host" ]]; then
            echo "SKIP: issue #${issue} carries a FRESH lease held by a different host (host=${peer_host} sweep=${peer_sweep}, updated_at=${peer_updated_at}, within the ${ttl_minutes}m TTL). Not publishing -- a live peer worker holds this claim, and superseding its lease would hide it from every freshest-wins reader (#6320). Skip this issue." >&2
            exit 4
        fi
        if ((own_fresh == 1)); then
            echo "OK: issue #${issue} already carries a fresh lease for this sweep (host=${host} sweep=${sweep_id}) -- not publishing a duplicate" >&2
            printf '%s %s\n' "$host" "$sweep_id"
            exit 0
        fi
        if ((same_host_diff_fresh == 1)); then
            # Another local sweep/dispatch is (or just was) working this
            # issue. Publishing our own record is correct -- this sweep is
            # genuinely the one working it now, and the host-scoped readers
            # (`sweep-lease-fence.sh`'s host check) treat both records as
            # this host's either way.
            echo "NOTE: issue #${issue} carries a fresh lease from a different sweep on this same host -- publishing this sweep's own record on top" >&2
        fi
    fi

    # --- Publish ----------------------------------------------------------
    # Body format is the contract in `defaults/docs/lease-record.md`: the
    # marker is the LITERAL first line; everything after it is free-form
    # prose no reader may parse.
    local now_iso lease_body
    now_iso="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    lease_body="$(printf '%s%s sweep=%s -->\n%s' \
        "$LEASE_MARKER_PREFIX" "$host" "$sweep_id" \
        "This issue is being worked by an **in-session** \`/loom:sweep\` run \`${sweep_id}\` on host \`${host}\`, published at ${now_iso}. This comment is a **lease record** (Issue #6320, Epic #6165) in the identical format \`loom-daemon\`'s dispatch-time write uses (#6179) — its liveness signal is this comment's own forge-assigned \`updated_at\`, never the timestamp in this text. It is renewed by \`sweep-lease-renew.sh\` (#6180) for as long as this sweep's own process is alive, and simply ages out afterwards. Readers: the daemon's reclamation gate (#6286) refuses to reclaim this claim while the lease is fresh, dispatch-time ordering (#6287) yields to it, and \`sweep-lease-fence.sh\` (#6309) fences this sweep's own push/PR-open against it. See \`defaults/docs/lease-record.md\`.")"

    # `-F` (NOT `-f`) — only `-F/--field` applies gh's `@-` read-from-stdin
    # magic; `-f/--raw-field` would post the literal two characters `@-`
    # (#6320, the same trap fixed in sweep-lease-renew.sh).
    local post_out
    if ! post_out="$(printf '%s' "$lease_body" \
        | gh api "${repo_args[@]}" --method POST "repos/{owner}/{repo}/issues/${issue}/comments" -F body=@- 2>&1)"; then
        echo "ERROR: failed to publish lease comment on issue #${issue}: ${post_out}" >&2
        echo "Proceeding without a lease is safe but degrades reclaim evidence (best-effort, mirrors #6179's fail-open dispatch write)." >&2
        exit 2
    fi

    local comment_id
    comment_id="$(jq -r '.id // empty' <<< "$post_out" 2>/dev/null || true)"
    echo "OK: published lease record for issue #${issue} (host=${host} sweep=${sweep_id}${comment_id:+ comment_id=${comment_id}}) at ${now_iso}" >&2
    printf '%s %s\n' "$host" "$sweep_id"
}

main() {
    local cmd="${1:-}"
    shift || true
    case "$cmd" in
        publish) cmd_publish "$@" ;;
        -h | --help | "") usage ;;
        *)
            echo "ERROR: unknown command '$cmd'" >&2
            usage
            ;;
    esac
}

main "$@"
