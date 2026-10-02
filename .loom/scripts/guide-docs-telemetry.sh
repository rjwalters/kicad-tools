#!/usr/bin/env bash
# guide-docs-telemetry.sh - local doc-maintenance throughput telemetry (issue #6136)
#
# Problem this closes
# --------------------
# `loom-ui:docs/token-analytics.md` documents the fleet's per-repo token
# attribution model: usage is joined against `sweep.*` telemetry, which only
# Builder sweeps emit. Support-role crons (Judge, Champion, Curator, and by
# the same logic Guide) never emit `sweep.*` records, so all of their token
# spend -- including Guide's Document Maintenance phase (WORK_LOG.md /
# WORK_PLAN.md / README.md PRs) -- falls into an undifferentiated
# "unattributed" bucket that is reported but never broken down further. An
# operator has no way to tell "how much of the fleet's pool went to doc
# maintenance" without manually correlating `docs/guide-update-*` PR history.
#
# This script is a small, DECOUPLED local telemetry surface for exactly that
# one category. It does NOT plug into the Rust `loom-daemon` observability
# pipeline (loom-daemon/src/observability/, the Cloudflare-backed
# `sweep.*`/`tokens.snapshot` schema in .loom/docs/telemetry-schema.md) --
# Guide runs as a role PROMPT (defaults/.claude/commands/loom/guide.md)
# executed as a sequence of Bash tool calls, not as a tracked `SweepRegistry`
# sweep, so it has no natural attachment point to that pipeline without a much
# larger daemon change. Instead this writes a local, append-only JSONL log
# (mirroring the shape -- envelope + record -- of
# .loom/logs/sweep-outcome-telemetry.jsonl, so it reads the same way, but is
# an entirely separate file) that an operator queries directly. Extending
# this into the full daemon-integrated pipeline (real per-account token
# attribution, a dashboard panel) is a natural follow-up, not required by
# this issue's acceptance criteria (visibility without behavior change).
#
# Usage:
#   ./.loom/scripts/guide-docs-telemetry.sh record --pr <number> [options]
#   ./.loom/scripts/guide-docs-telemetry.sh report [--since <window>] [--json]
#
# `record` (called from guide.md's create_docs_pr(), Step 5, right before the
# lock is released):
#   --pr <number>            Required. The doc-maintenance PR number just opened.
#   --repo <owner/repo>      Optional. Defaults to `gh repo view --json
#                             nameWithOwner --jq .nameWithOwner`.
#   --duration-sec <int>     Optional. Elapsed seconds the Document Maintenance
#                             phase held the docs-guide lock (see
#                             docs-guide-lock.sh's `age` command) -- the
#                             agent/token-spend proxy. Omitted from the record
#                             when not provided or not a non-negative integer.
#   --files <csv>            Optional. Comma-separated list of files changed.
#                             Defaults to "WORK_LOG.md,WORK_PLAN.md,README.md".
#
# `report` (an operator's single place to view doc-maintenance throughput):
#   --since <window>         Optional. A duration like `7d`, `24h`, `30m`, `90s`,
#                             or a bare integer (seconds). Default: `7d`.
#   --json                   Optional. Emit a machine-readable JSON summary
#                             object instead of the human-readable report.
#
# Log location: $LOOM_GUIDE_DOCS_TELEMETRY_LOG, default
# <repo-root>/.loom/logs/guide-docs-telemetry.jsonl (gitignored, host-local,
# same directory sweep-outcome-telemetry.jsonl already lives in). `record`
# creates the file (and its directory) on first use; `report` treats a
# missing file as zero records rather than an error.
#
# Exit codes: record/report success = 0; usage error = 2; missing --pr for
# `record` = 2.

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
Loom Guide Docs Telemetry

Usage:
  ./.loom/scripts/guide-docs-telemetry.sh record --pr <number> [--repo owner/repo] [--duration-sec N] [--files a.md,b.md]
  ./.loom/scripts/guide-docs-telemetry.sh report [--since 7d] [--json]

See the header comment in this file for the full rationale (issue #6136).
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
# docs-guide-lock.sh resolves its lock dir -- so the main workspace and every
# worktree agree on one file regardless of cwd.
GIT_COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null) || {
    print_error "Not in a git repository"
    exit 1
}
REPO_ROOT=$(cd "$(dirname "$GIT_COMMON_DIR")" && pwd -P)
LOG_FILE="${LOOM_GUIDE_DOCS_TELEMETRY_LOG:-$REPO_ROOT/.loom/logs/guide-docs-telemetry.jsonl}"

HOST_ID="${LOOM_HOST_ID:-${HOSTNAME:-$(hostname 2>/dev/null || echo unknown-host)}}"

# --- Portable duration-string -> seconds -------------------------------------
# Accepts a bare integer (seconds) or an integer with a single trailing unit
# suffix: s(econds), m(inutes), h(ours), d(ays). No fractional/compound
# durations (e.g. "1h30m") -- deliberately simple, matches the other window
# flags in this repo (e.g. archive-logs.sh's RETENTION_DAYS).
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
        DURATION_SEC=""
        FILES="WORK_LOG.md,WORK_PLAN.md,README.md"

        while [[ $# -gt 0 ]]; do
            case "$1" in
                --pr) PR_NUMBER="${2:-}"; shift 2 ;;
                --repo) REPO="${2:-}"; shift 2 ;;
                --duration-sec) DURATION_SEC="${2:-}"; shift 2 ;;
                --files) FILES="${2:-}"; shift 2 ;;
                *) print_error "record: unknown argument: $1"; exit 2 ;;
            esac
        done

        if [[ -z "$PR_NUMBER" ]] || ! [[ "$PR_NUMBER" =~ ^[0-9]+$ ]]; then
            print_error "record: --pr <number> is required and must be numeric (got: '${PR_NUMBER}')"
            exit 2
        fi

        if [[ -z "$REPO" ]]; then
            REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || echo "")"
        fi

        # duration_sec is only included in the record when it is a
        # non-negative integer -- an unset or malformed value is omitted
        # (null in the record), never coerced to 0 (0 would falsely claim an
        # instant phase).
        DURATION_JSON="null"
        if [[ "$DURATION_SEC" =~ ^[0-9]+$ ]]; then
            DURATION_JSON="$DURATION_SEC"
        fi

        EMITTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        EMITTED_AT_EPOCH="$(date -u +%s)"

        # Build the files_changed JSON array from the comma-separated list.
        FILES_JSON="[]"
        if [[ -n "$FILES" ]]; then
            IFS=',' read -ra _DOCS_FILES_ARR <<< "$FILES"
            FILES_JSON="$(printf '%s\n' "${_DOCS_FILES_ARR[@]}" | jq -R . | jq -s -c .)"
        fi

        mkdir -p "$(dirname "$LOG_FILE")"

        jq -nc \
            --arg emitted_at "$EMITTED_AT" \
            --argjson emitted_at_epoch "$EMITTED_AT_EPOCH" \
            --arg host_id "$HOST_ID" \
            --arg repo "$REPO" \
            --argjson pr_number "$PR_NUMBER" \
            --argjson duration_sec "$DURATION_JSON" \
            --argjson files_changed "$FILES_JSON" \
            '{
                schema_version: 1,
                emitted_at: $emitted_at,
                emitted_at_epoch: $emitted_at_epoch,
                host_id: $host_id,
                record: {
                    kind: "guide.docs_maintenance",
                    repo: $repo,
                    pr_number: $pr_number,
                    duration_sec: $duration_sec,
                    files_changed: $files_changed
                }
            }' >> "$LOG_FILE"

        print_success "recorded doc-maintenance telemetry for PR #${PR_NUMBER} -> $LOG_FILE"
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
                pr_count: length,
                total_duration_sec: ([.[] | .record.duration_sec | select(. != null)] | add // 0),
                duration_known_count: ([.[] | .record.duration_sec | select(. != null)] | length),
                prs: [.[] | {repo: .record.repo, pr_number: .record.pr_number, duration_sec: .record.duration_sec, emitted_at: .emitted_at}]
            }' <<<"$RECORDS")"

        if [[ "$AS_JSON" -eq 1 ]]; then
            echo "$SUMMARY"
            exit 0
        fi

        PR_COUNT="$(jq -r '.pr_count' <<<"$SUMMARY")"
        TOTAL_DURATION="$(jq -r '.total_duration_sec' <<<"$SUMMARY")"
        DURATION_KNOWN="$(jq -r '.duration_known_count' <<<"$SUMMARY")"

        echo "Guide doc-maintenance throughput (last ${SINCE}):"
        if [[ "$PR_COUNT" -eq 0 ]]; then
            echo "  No doc-maintenance PRs in this window."
            exit 0
        fi
        echo "  PRs opened:            $PR_COUNT"
        if [[ "$DURATION_KNOWN" -gt 0 ]]; then
            AVG_DURATION=$((TOTAL_DURATION / DURATION_KNOWN))
            echo "  Total phase time:      ${TOTAL_DURATION}s (proxy for agent/token spend, ${DURATION_KNOWN}/${PR_COUNT} PRs report a duration)"
            echo "  Average phase time:    ${AVG_DURATION}s"
        else
            echo "  Total phase time:      unknown (no PR in this window recorded a duration)"
        fi
        echo ""
        echo "  PR list:"
        jq -r '.prs[] | "    #\(.pr_number) (\(.repo // "unknown repo")) at \(.emitted_at)" + (if .duration_sec != null then " — \(.duration_sec)s" else "" end)' <<<"$SUMMARY"
        exit 0
        ;;
esac
