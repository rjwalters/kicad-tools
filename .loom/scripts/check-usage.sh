#!/bin/bash
# check-usage.sh - Query claude-monitor database for session usage
#
# Usage:
#   ./.loom/scripts/check-usage.sh           # Returns JSON with usage data
#   ./.loom/scripts/check-usage.sh --status  # Human-readable status
#
# Exit codes:
#   0 - Data returned successfully
#   1 - Database not found or query failed
#
# This script queries the claude-monitor SQLite database to get current
# session usage. Used by the Loom daemon to detect approaching rate limits.
#
# Requires: claude-monitor browser extension
# See: https://github.com/rjwalters/claude-monitor

set -e

DB_PATH="$HOME/.claude-monitor/usage.db"

# Check if database exists
if [ ! -f "$DB_PATH" ]; then
    if [ "$1" = "--status" ]; then
        echo "NO_DATABASE: claude-monitor not installed"
        echo ""
        echo "For multi-day autonomous operation, install claude-monitor:"
        echo "  https://github.com/rjwalters/claude-monitor"
    else
        echo '{"error": "NO_DATABASE", "message": "claude-monitor not installed"}'
    fi
    exit 1
fi

# Check if sqlite3 is available
if ! command -v sqlite3 &> /dev/null; then
    if [ "$1" = "--status" ]; then
        echo "ERROR: sqlite3 not found"
    else
        echo '{"error": "NO_SQLITE3", "message": "sqlite3 command not found"}'
    fi
    exit 1
fi

if [ "$1" = "--status" ]; then
    # Human-readable format
    result=$(sqlite3 "$DB_PATH" "
        SELECT
            session_percent,
            session_reset,
            weekly_all_percent,
            weekly_reset,
            datetime(timestamp, 'localtime') as local_time
        FROM usage_history
        WHERE is_synthetic = 0
        ORDER BY timestamp DESC
        LIMIT 1
    " -separator '|' 2>/dev/null)

    if [ -z "$result" ]; then
        echo "NO_DATA: No usage data in database"
        echo "Make sure claude.ai/settings/usage is open in your browser"
        exit 1
    fi

    IFS='|' read -r session_pct session_reset weekly_pct weekly_reset timestamp <<< "$result"

    echo "Claude Usage Status (as of $timestamp)"
    echo "========================================"
    echo ""
    echo "Session:     ${session_pct}% used"
    echo "  Resets:    $session_reset"
    echo ""
    echo "Weekly:      ${weekly_pct}% used"
    echo "  Resets:    $weekly_reset"
    echo ""

    # Provide recommendation
    if [ "${session_pct%.*}" -ge 97 ]; then
        echo "⚠️  RECOMMENDATION: Pause operations until session resets"
    elif [ "${session_pct%.*}" -ge 80 ]; then
        echo "⚠️  WARNING: Approaching session limit"
    else
        echo "✓ Session usage is healthy"
    fi
else
    # JSON format for programmatic use
    sqlite3 "$DB_PATH" "
        SELECT json_object(
            'session_percent', session_percent,
            'session_reset', session_reset,
            'weekly_all_percent', weekly_all_percent,
            'weekly_reset', weekly_reset,
            'timestamp', timestamp,
            'data_age_seconds', CAST((julianday('now') - julianday(timestamp)) * 86400 AS INTEGER)
        )
        FROM usage_history
        WHERE is_synthetic = 0
        ORDER BY timestamp DESC
        LIMIT 1
    " 2>/dev/null || {
        echo '{"error": "QUERY_FAILED", "message": "Failed to query database"}'
        exit 1
    }
fi
