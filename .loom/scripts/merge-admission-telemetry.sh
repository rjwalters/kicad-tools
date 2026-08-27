#!/usr/bin/env bash
# merge-admission-telemetry.sh - local merge-admission-recheck telemetry
# (issue #6978, follow-up from #6156)
#
# Problem this closes
# --------------------
# `merge-pr.sh`'s `_recheck_mergeable_before_refusal()` (#6104/#6118) decides
# whether a PR whose cached `.mergeable` reads `false` should actually be
# merged (the forge's cache is stale), refused as a confirmed conflict, or
# refused as unresolved/stale. That decision was only ever `echo`ed to
# stdout/stderr via merge-pr.sh's local info/error helpers -- nothing wrote
# it anywhere durable. #6156's efficacy review of that recheck found this was
# the one actionable gap: there is no way to answer, in retrospect, how often
# the forge's cache lied, how often local corroboration confirmed a real
# conflict vs. came back unresolved, or what the backoff actually cost in
# aggregate, across a fleet merging many PRs/day.
#
# This script is a small, DECOUPLED local telemetry surface for exactly that
# one category, mirroring `guide-docs-telemetry.sh` (issue #6136,
# `defaults/docs/observability.md` §5b): `merge-pr.sh` is a bash script
# invoked directly (by Champion's `--auto`, and interactively), never a
# tracked `loom-daemon` `SweepRegistry` sweep, so it has no natural
# attachment point to the Rust daemon's Cloudflare-backed
# `sweep.*`/`tokens.snapshot` pipeline (`.loom/docs/telemetry-schema.md`)
# without a much larger change. Instead this writes a local, append-only
# JSONL log (mirroring the shape -- envelope + record -- of
# .loom/logs/sweep-outcome-telemetry.jsonl and guide-docs-telemetry.jsonl, so
# it reads the same way, but is an entirely separate file) that an operator
# queries directly.
#
# Usage:
#   ./.loom/scripts/merge-admission-telemetry.sh record --pr <number> --action <action> --reason <text> [options]
#   ./.loom/scripts/merge-admission-telemetry.sh report [--since <window>] [--json]
#
# `record` (called from merge-pr.sh's synchronous-merge mergeability gate,
# right after `_recheck_mergeable_before_refusal()` returns its decision, and
# BEFORE the `info`/`error` branch on that decision — `error` exits the
# script, so telemetry must be emitted first or it would never fire on a
# refusal):
#   --pr <number>             Required. The PR number the recheck ran for.
#   --action <action>         Required. One of: merge, refuse-conflict,
#                             refuse-stale (the recheck's three-way decision).
#   --reason <text>           Optional. The free-text reason string the
#                             recheck echoed. Defaults to an empty string when
#                             omitted.
#   --repo <owner/repo>       Optional. Defaults to `gh repo view --json
#                             nameWithOwner --jq .nameWithOwner`.
#   --retries-used <int>      Optional. How many backoff/recheck attempts were
#                             actually consumed before the decision. Omitted
#                             from the record (null) when not provided or not
#                             a non-negative integer.
#   --backoff-delay-sec <int> Optional. The configured per-attempt backoff
#                             delay (LOOM_MERGEABLE_RECHECK_DELAY). Omitted
#                             from the record (null) when not provided or not
#                             a non-negative integer.
#
# `report` (an operator's single place to view merge-admission-recheck
# throughput):
#   --since <window>          Optional. A duration like `7d`, `24h`, `30m`,
#                             `90s`, or a bare integer (seconds). Default: `7d`.
#   --json                    Optional. Emit a machine-readable JSON summary
#                             object instead of the human-readable report.
#
# Log location: $LOOM_MERGE_ADMISSION_TELEMETRY_LOG, default
# <repo-root>/.loom/logs/merge-admission-telemetry.jsonl (gitignored,
# host-local, same directory sweep-outcome-telemetry.jsonl and
# guide-docs-telemetry.jsonl already live in). `record` creates the file
# (and its directory) on first use; `report` treats a missing file as zero
# records rather than an error.
#
# Exit codes: record/report success = 0; usage error = 2; missing/invalid
# --pr, --action, or --reason for `record` = 2.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

print_error() { echo -e "${RED}ERROR: $1${NC}" >&2; }
print_info() { echo -e "${BLUE}ℹ $1${NC}" >&2; }
print_success() { echo -e "${GREEN}✓ $1${NC}" >&2; }

show_help() {
    cat <<'EOF'
Loom Merge Admission Telemetry

Usage:
  ./.loom/scripts/merge-admission-telemetry.sh record --pr <number> --action <merge|refuse-conflict|refuse-stale> --reason <text> [--repo owner/repo] [--retries-used N] [--backoff-delay-sec N]
  ./.loom/scripts/merge-admission-telemetry.sh report [--since 7d] [--json]

See the header comment in this file for the full rationale (issue #6978).
EOF
}

CMD="${1:-}"
if [[ "$CMD" == "--help" ]] || [[ "$CMD" == "-h" ]]; then
    show_help
    exit 0
fi
if [[ "$CMD" != "record" ]] && [[ "$CMD" != "report" ]]; then
    print_error "Unknown or missing command: '${CMD}'"
    show_help >&2
    exit 2
fi
shift || true

# Resolve the log path relative to the canonical git-common-dir, the same way
# guide-docs-telemetry.sh / docs-guide-lock.sh resolve their own paths -- so
# the main workspace and every worktree agree on one file regardless of cwd.
GIT_COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null) || {
    print_error "Not in a git repository"
    exit 1
}
REPO_ROOT=$(cd "$(dirname "$GIT_COMMON_DIR")" && pwd -P)
LOG_FILE="${LOOM_MERGE_ADMISSION_TELEMETRY_LOG:-$REPO_ROOT/.loom/logs/merge-admission-telemetry.jsonl}"

HOST_ID="${LOOM_HOST_ID:-${HOSTNAME:-$(hostname 2>/dev/null || echo unknown-host)}}"

# --- Portable duration-string -> seconds -------------------------------------
# Accepts a bare integer (seconds) or an integer with a single trailing unit
# suffix: s(econds), m(inutes), h(ours), d(ays). No fractional/compound
# durations -- matches guide-docs-telemetry.sh's identical helper.
_duration_to_secs() {
    local raw="$1" num unit
    if [[ "$raw" =~ ^([0-9]+)$ ]]; then
        echo "${BASH_REMATCH[1]}"
        return 0
    fi
    if [[ "$raw" =~ ^([0-9]+)([smhd])$ ]]; then
        num="${BASH_REMATCH[1]}"
        unit="${BASH_REMATCH[2]}"
        case "$unit" in
            s) echo "$num" ;;
            m) echo $((num * 60)) ;;
            h) echo $((num * 3600)) ;;
            d) echo $((num * 86400)) ;;
        esac
        return 0
    fi
    return 1
}

case "$CMD" in
    record)
        PR_NUMBER=""
        REPO=""
        ACTION=""
        REASON=""
        RETRIES_USED=""
        BACKOFF_DELAY_SEC=""

        while [[ $# -gt 0 ]]; do
            case "$1" in
                --pr) PR_NUMBER="${2:-}"; shift 2 ;;
                --repo) REPO="${2:-}"; shift 2 ;;
                --action) ACTION="${2:-}"; shift 2 ;;
                --reason) REASON="${2:-}"; shift 2 ;;
                --retries-used) RETRIES_USED="${2:-}"; shift 2 ;;
                --backoff-delay-sec) BACKOFF_DELAY_SEC="${2:-}"; shift 2 ;;
                *) print_error "record: unknown argument: $1"; exit 2 ;;
            esac
        done

        if [[ -z "$PR_NUMBER" ]] || ! [[ "$PR_NUMBER" =~ ^[0-9]+$ ]]; then
            print_error "record: --pr <number> is required and must be numeric (got: '${PR_NUMBER}')"
            exit 2
        fi

        case "$ACTION" in
            merge|refuse-conflict|refuse-stale) ;;
            *)
                print_error "record: --action must be one of merge|refuse-conflict|refuse-stale (got: '${ACTION}')"
                exit 2
                ;;
        esac

        if [[ -z "$REPO" ]]; then
            REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || echo "")"
        fi

        # retries_used / backoff_delay_sec are only included in the record
        # when they are non-negative integers -- an unset or malformed value
        # is omitted (null in the record), never coerced to 0.
        RETRIES_USED_JSON="null"
        if [[ "$RETRIES_USED" =~ ^[0-9]+$ ]]; then
            RETRIES_USED_JSON="$RETRIES_USED"
        fi
        BACKOFF_DELAY_JSON="null"
        if [[ "$BACKOFF_DELAY_SEC" =~ ^[0-9]+$ ]]; then
            BACKOFF_DELAY_JSON="$BACKOFF_DELAY_SEC"
        fi

        EMITTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        EMITTED_AT_EPOCH="$(date -u +%s)"

        mkdir -p "$(dirname "$LOG_FILE")"

        jq -nc \
            --arg emitted_at "$EMITTED_AT" \
            --argjson emitted_at_epoch "$EMITTED_AT_EPOCH" \
            --arg host_id "$HOST_ID" \
            --arg repo "$REPO" \
            --argjson pr_number "$PR_NUMBER" \
            --arg action "$ACTION" \
            --arg reason "$REASON" \
            --argjson retries_used "$RETRIES_USED_JSON" \
            --argjson backoff_delay_sec "$BACKOFF_DELAY_JSON" \
            '{
                schema_version: 1,
                emitted_at: $emitted_at,
                emitted_at_epoch: $emitted_at_epoch,
                host_id: $host_id,
                record: {
                    kind: "merge.admission_recheck",
                    repo: $repo,
                    pr_number: $pr_number,
                    action: $action,
                    reason: $reason,
                    retries_used: $retries_used,
                    backoff_delay_sec: $backoff_delay_sec
                }
            }' >> "$LOG_FILE"

        print_success "recorded merge-admission-recheck telemetry for PR #${PR_NUMBER} ($ACTION) -> $LOG_FILE"
        exit 0
        ;;
    report)
        SINCE="7d"
        AS_JSON=0

        while [[ $# -gt 0 ]]; do
            case "$1" in
                --since) SINCE="${2:-}"; shift 2 ;;
                --json) AS_JSON=1; shift ;;
                *) print_error "report: unknown argument: $1"; exit 2 ;;
            esac
        done

        WINDOW_SECS="$(_duration_to_secs "$SINCE")" || {
            print_error "report: invalid --since value: '${SINCE}' (expected e.g. 7d, 24h, 30m, 90s, or a bare integer)"
            exit 2
        }

        NOW_EPOCH="$(date -u +%s)"
        CUTOFF_EPOCH=$((NOW_EPOCH - WINDOW_SECS))

        if [[ ! -f "$LOG_FILE" ]]; then
            RECORDS="[]"
        else
            RECORDS="$(jq -n -c --argjson cutoff "$CUTOFF_EPOCH" \
                '[inputs | select(.emitted_at_epoch >= $cutoff)]' \
                "$LOG_FILE" 2>/dev/null || echo "[]")"
        fi

        SUMMARY="$(jq -c \
            --arg since "$SINCE" \
            --argjson window_secs "$WINDOW_SECS" \
            '{
                since: $since,
                window_secs: $window_secs,
                record_count: length,
                merge_count: ([.[] | select(.record.action == "merge")] | length),
                refuse_conflict_count: ([.[] | select(.record.action == "refuse-conflict")] | length),
                refuse_stale_count: ([.[] | select(.record.action == "refuse-stale")] | length),
                records: [.[] | {repo: .record.repo, pr_number: .record.pr_number, action: .record.action, retries_used: .record.retries_used, backoff_delay_sec: .record.backoff_delay_sec, emitted_at: .emitted_at}]
            }' <<<"$RECORDS")"

        if [[ "$AS_JSON" -eq 1 ]]; then
            echo "$SUMMARY"
            exit 0
        fi

        RECORD_COUNT="$(jq -r '.record_count' <<<"$SUMMARY")"
        MERGE_COUNT="$(jq -r '.merge_count' <<<"$SUMMARY")"
        REFUSE_CONFLICT_COUNT="$(jq -r '.refuse_conflict_count' <<<"$SUMMARY")"
        REFUSE_STALE_COUNT="$(jq -r '.refuse_stale_count' <<<"$SUMMARY")"

        echo "Merge-admission-recheck outcomes (last ${SINCE}):"
        if [[ "$RECORD_COUNT" -eq 0 ]]; then
            echo "  No merge-admission-recheck invocations in this window."
            exit 0
        fi
        echo "  Total invocations:     $RECORD_COUNT"
        echo "  merge:                 $MERGE_COUNT"
        echo "  refuse-conflict:       $REFUSE_CONFLICT_COUNT"
        echo "  refuse-stale:          $REFUSE_STALE_COUNT"
        echo ""
        echo "  Record list:"
        jq -r '.records[] | "    #\(.pr_number) (\(.repo // "unknown repo")) at \(.emitted_at) — \(.action)" + (if .retries_used != null then " (retries_used=\(.retries_used))" else "" end)' <<<"$SUMMARY"
        exit 0
        ;;
esac
