#!/usr/bin/env bash
# validate-daemon-state.sh - Validate daemon-state.json structure and task IDs
#
# This script validates the daemon state file to catch corruption and fabricated
# task IDs before they cause cascading failures in the orchestration system.
#
# Usage:
#   ./validate-daemon-state.sh                 # Validate (dry run)
#   ./validate-daemon-state.sh --fix           # Auto-fix common issues
#   ./validate-daemon-state.sh --json          # Output JSON for programmatic use
#   ./validate-daemon-state.sh <path>          # Validate specific file
#
# Exit codes:
#   0 - Valid (or fixed successfully with --fix)
#   1 - Invalid (with error details)
#   2 - File not found or unreadable
#
# Validates:
#   - JSON syntax is valid
#   - Task IDs match ^[a-f0-9]{7}$ (real Task tool IDs)
#   - Required fields present (started_at, running, iteration)
#   - Shepherd status values are valid (working, idle, errored, paused)
#   - Support role status values are valid (running, idle)
#   - Timestamps are valid ISO 8601 format
#
# Fixes (with --fix):
#   - Resets shepherds with invalid task IDs to idle
#   - Resets support roles with invalid task IDs to idle
#   - Removes orphaned output_file references

set -euo pipefail

# ANSI color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Valid shepherd status values
VALID_SHEPHERD_STATUS="working|idle|errored|paused"

# Valid support role status values
VALID_SUPPORT_ROLE_STATUS="running|idle"

# Task ID pattern (7-char lowercase hex, e.g., 'a7dc1e0')
TASK_ID_PATTERN='^[a-f0-9]{7}$'

# Find the repository root
find_repo_root() {
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.git" ]] || [[ -f "$dir/.git" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    echo "Error: Not in a git repository" >&2
    return 1
}

REPO_ROOT=$(find_repo_root)
DEFAULT_STATE_FILE="$REPO_ROOT/.loom/daemon-state.json"

# Parse arguments
FIX_MODE=false
JSON_OUTPUT=false
DRY_RUN=false
STATE_FILE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --fix)
            FIX_MODE=true
            shift
            ;;
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help|-h)
            cat <<EOF
Usage: $0 [OPTIONS] [STATE_FILE]

Validate daemon-state.json structure and task IDs.

Arguments:
  STATE_FILE    Path to state file (default: .loom/daemon-state.json)

Options:
  --fix         Auto-fix common issues (resets invalid entries to idle)
  --json        Output JSON for programmatic use
  --dry-run     Show what would be fixed without making changes
  --help        Show this help message

Exit codes:
  0 - Valid (or fixed successfully with --fix)
  1 - Invalid (with error details)
  2 - File not found or unreadable

Validations performed:
  - JSON syntax
  - Task IDs match ^[a-f0-9]{7}$ (real Task tool IDs)
  - Required fields: started_at, running, iteration
  - Shepherd status: working, idle, errored, paused
  - Support role status: running, idle
  - ISO 8601 timestamp format

Examples:
  $0                           # Validate default state file
  $0 --fix                     # Fix and write back
  $0 --fix --dry-run           # Preview fixes without writing
  $0 --json                    # Machine-readable output
  $0 /path/to/state.json       # Validate specific file
EOF
            exit 0
            ;;
        -*)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
        *)
            STATE_FILE="$1"
            shift
            ;;
    esac
done

# Use default if not specified
STATE_FILE="${STATE_FILE:-$DEFAULT_STATE_FILE}"

# Logging helpers
log_info() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        echo -e "${BLUE}[INFO]${NC} $*" >&2
    fi
}

log_success() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        echo -e "${GREEN}[OK]${NC} $*" >&2
    fi
}

log_warn() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        echo -e "${YELLOW}[WARN]${NC} $*" >&2
    fi
}

log_error() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        echo -e "${RED}[ERROR]${NC} $*" >&2
    fi
}

# Check if file exists
if [[ ! -f "$STATE_FILE" ]]; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"valid": false, "error": "file_not_found", "file": "'"$STATE_FILE"'"}'
    else
        log_error "State file not found: $STATE_FILE"
    fi
    exit 2
fi

# Check if file is readable
if [[ ! -r "$STATE_FILE" ]]; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"valid": false, "error": "file_not_readable", "file": "'"$STATE_FILE"'"}'
    else
        log_error "State file not readable: $STATE_FILE"
    fi
    exit 2
fi

# Initialize error tracking
declare -a ERRORS=()
declare -a WARNINGS=()
declare -a FIXES=()

# Validate JSON syntax
log_info "Validating JSON syntax..."
if ! jq empty "$STATE_FILE" 2>/dev/null; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"valid": false, "error": "invalid_json", "file": "'"$STATE_FILE"'"}'
    else
        log_error "Invalid JSON in $STATE_FILE"
    fi
    exit 1
fi

# Load state
STATE=$(cat "$STATE_FILE")

# Validate required top-level fields
log_info "Validating required fields..."
REQUIRED_FIELDS=("started_at" "running" "iteration")
for field in "${REQUIRED_FIELDS[@]}"; do
    if [[ $(echo "$STATE" | jq "has(\"$field\")") != "true" ]]; then
        ERRORS+=("missing_field:$field")
    fi
done

# Validate shepherds section
log_info "Validating shepherds..."
SHEPHERDS=$(echo "$STATE" | jq -r '.shepherds // {} | to_entries[]' 2>/dev/null || echo "")

if [[ -n "$SHEPHERDS" ]]; then
    while IFS= read -r shepherd_entry; do
        shepherd_id=$(echo "$shepherd_entry" | jq -r '.key')
        shepherd_data=$(echo "$shepherd_entry" | jq -r '.value')

        # Validate status
        status=$(echo "$shepherd_data" | jq -r '.status // "unknown"')
        if [[ ! "$status" =~ ^($VALID_SHEPHERD_STATUS)$ ]]; then
            ERRORS+=("invalid_shepherd_status:$shepherd_id:$status")
        fi

        # Validate task_id format if present
        task_id=$(echo "$shepherd_data" | jq -r '.task_id // empty')
        if [[ -n "$task_id" ]]; then
            if [[ ! "$task_id" =~ $TASK_ID_PATTERN ]]; then
                ERRORS+=("invalid_task_id:$shepherd_id:$task_id")
                if [[ "$FIX_MODE" == "true" ]]; then
                    FIXES+=("reset_shepherd:$shepherd_id")
                fi
            fi
        fi

        # Warn if working but no task_id (unless tmux mode)
        execution_mode=$(echo "$shepherd_data" | jq -r '.execution_mode // "direct"')
        if [[ "$status" == "working" && -z "$task_id" && "$execution_mode" == "direct" ]]; then
            WARNINGS+=("working_without_task_id:$shepherd_id")
        fi

    done < <(echo "$STATE" | jq -c '.shepherds // {} | to_entries[]' 2>/dev/null || true)
fi

# Validate support_roles section
log_info "Validating support roles..."
SUPPORT_ROLES=$(echo "$STATE" | jq -r '.support_roles // {} | to_entries[]' 2>/dev/null || echo "")

if [[ -n "$SUPPORT_ROLES" ]]; then
    while IFS= read -r role_entry; do
        role_name=$(echo "$role_entry" | jq -r '.key')
        role_data=$(echo "$role_entry" | jq -r '.value')

        # Validate status
        status=$(echo "$role_data" | jq -r '.status // "unknown"')
        if [[ ! "$status" =~ ^($VALID_SUPPORT_ROLE_STATUS)$ ]]; then
            ERRORS+=("invalid_support_role_status:$role_name:$status")
        fi

        # Validate task_id format if present and status is running
        task_id=$(echo "$role_data" | jq -r '.task_id // empty')
        if [[ -n "$task_id" ]]; then
            if [[ ! "$task_id" =~ $TASK_ID_PATTERN ]]; then
                ERRORS+=("invalid_task_id:$role_name:$task_id")
                if [[ "$FIX_MODE" == "true" ]]; then
                    FIXES+=("reset_support_role:$role_name")
                fi
            fi
        fi

    done < <(echo "$STATE" | jq -c '.support_roles // {} | to_entries[]' 2>/dev/null || true)
fi

# Validate timestamp formats
log_info "Validating timestamps..."
TIMESTAMP_FIELDS=("started_at" "last_poll" "last_architect_trigger" "last_hermit_trigger")
for field in "${TIMESTAMP_FIELDS[@]}"; do
    value=$(echo "$STATE" | jq -r ".$field // empty")
    if [[ -n "$value" ]]; then
        # Basic ISO 8601 check (YYYY-MM-DDTHH:MM:SSZ)
        if [[ ! "$value" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z?$ ]]; then
            WARNINGS+=("invalid_timestamp_format:$field:$value")
        fi
    fi
done

# Apply fixes if requested
if [[ "$FIX_MODE" == "true" && ${#FIXES[@]} -gt 0 ]]; then
    log_info "Applying fixes..."
    FIXED_STATE="$STATE"

    for fix in "${FIXES[@]}"; do
        fix_type=$(echo "$fix" | cut -d: -f1)
        fix_target=$(echo "$fix" | cut -d: -f2)

        case "$fix_type" in
            reset_shepherd)
                log_info "  Resetting shepherd $fix_target to idle..."
                FIXED_STATE=$(echo "$FIXED_STATE" | jq ".shepherds[\"$fix_target\"] = {
                    \"status\": \"idle\",
                    \"issue\": null,
                    \"task_id\": null,
                    \"output_file\": null,
                    \"idle_since\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",
                    \"idle_reason\": \"invalid_task_id_reset\"
                }")
                ;;
            reset_support_role)
                log_info "  Resetting support role $fix_target to idle..."
                FIXED_STATE=$(echo "$FIXED_STATE" | jq ".support_roles[\"$fix_target\"] = {
                    \"status\": \"idle\",
                    \"task_id\": null,
                    \"output_file\": null,
                    \"last_completed\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"
                }")
                ;;
        esac
    done

    # Write fixed state
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "Dry run - would write fixed state to $STATE_FILE"
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            echo "Fixed state would be:"
            echo "$FIXED_STATE" | jq .
        fi
    else
        echo "$FIXED_STATE" | jq . > "${STATE_FILE}.tmp"
        mv "${STATE_FILE}.tmp" "$STATE_FILE"
        log_success "Fixed state written to $STATE_FILE"
    fi
fi

# Generate output
if [[ "$JSON_OUTPUT" == "true" ]]; then
    # Build JSON output (handle empty arrays safely)
    if [[ ${#ERRORS[@]} -gt 0 ]]; then
        errors_json=$(printf '%s\n' "${ERRORS[@]}" | jq -R . | jq -s .)
    else
        errors_json="[]"
    fi
    if [[ ${#WARNINGS[@]} -gt 0 ]]; then
        warnings_json=$(printf '%s\n' "${WARNINGS[@]}" | jq -R . | jq -s .)
    else
        warnings_json="[]"
    fi
    if [[ ${#FIXES[@]} -gt 0 ]]; then
        fixes_json=$(printf '%s\n' "${FIXES[@]}" | jq -R . | jq -s .)
    else
        fixes_json="[]"
    fi

    valid="true"
    if [[ ${#ERRORS[@]} -gt 0 ]]; then
        valid="false"
    fi

    jq -n \
        --argjson valid "$valid" \
        --argjson errors "$errors_json" \
        --argjson warnings "$warnings_json" \
        --argjson fixes "$fixes_json" \
        --arg file "$STATE_FILE" \
        --argjson fix_mode "$FIX_MODE" \
        --argjson dry_run "$DRY_RUN" \
        '{
            valid: $valid,
            file: $file,
            errors: $errors,
            warnings: $warnings,
            fixes_applied: (if $fix_mode and ($dry_run | not) then $fixes else [] end),
            fixes_available: (if $fix_mode | not then $fixes else [] end),
            error_count: ($errors | length),
            warning_count: ($warnings | length)
        }'
else
    # Human-readable output
    if [[ ${#ERRORS[@]} -eq 0 ]]; then
        log_success "State file is valid: $STATE_FILE"
    else
        log_error "State file has ${#ERRORS[@]} error(s):"
        for error in "${ERRORS[@]}"; do
            echo "  - $error"
        done
    fi

    if [[ ${#WARNINGS[@]} -gt 0 ]]; then
        log_warn "Warnings (${#WARNINGS[@]}):"
        for warning in "${WARNINGS[@]}"; do
            echo "  - $warning"
        done
    fi

    if [[ ${#FIXES[@]} -gt 0 ]]; then
        if [[ "$FIX_MODE" == "true" ]]; then
            if [[ "$DRY_RUN" == "true" ]]; then
                log_info "Would apply ${#FIXES[@]} fix(es) (dry run)"
            else
                log_success "Applied ${#FIXES[@]} fix(es)"
            fi
        else
            log_info "Available fixes (run with --fix): ${#FIXES[@]}"
            for fix in "${FIXES[@]}"; do
                echo "  - $fix"
            done
        fi
    fi
fi

# Exit with appropriate code
if [[ ${#ERRORS[@]} -gt 0 && "$FIX_MODE" != "true" ]]; then
    exit 1
fi

exit 0
