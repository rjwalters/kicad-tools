#!/bin/bash
#
# validate-roles.sh - Validate Loom role configuration completeness
#
# This script checks that all configured roles have their dependencies
# properly configured, preventing silent failures where work gets stuck.
#
# OPERATOR-MANUAL TOOL (no in-repo caller besides its own test, #5277). The
# canonical, actively-wired validator is the Rust `loom-daemon validate`
# command (`loom-daemon/src/role_validation.rs` /
# `loom-daemon/src/cli/misc_cmds.rs::handle_validate_command`) -- it runs the
# identical role-dependency checks, is invoked proactively at daemon startup
# and documented in `defaults/docs/runtime-adapters.md`. This Bash script is
# kept as a dependency-light fallback: it needs only `jq` + the shared
# `lib/config-resolver.sh`, so it works before `loom-daemon` has ever been
# built (e.g. a fresh source checkout, or a shell-only CI step) or when an
# operator wants a quick check without invoking the daemon binary. Its
# config-tier-resolution behavior is covered by
# `defaults/scripts/tests/test-config-tiers-cli-migration.sh` (Tests 2-4);
# if this script changes, keep that test passing.
#
# Usage:
#   ./validate-roles.sh [OPTIONS]
#
# Options:
#   --json        Output as JSON for programmatic use
#   --strict      Exit with error code if warnings found
#   --quiet       Only output errors/warnings, not success messages
#   --help        Show this help message
#
# Exit codes:
#   0 - Valid (or warnings in non-strict mode)
#   1 - Errors found
#   2 - Warnings found (strict mode only)
#

set -euo pipefail

# Colors for output
RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default options
JSON_OUTPUT=false
STRICT_MODE=false
QUIET_MODE=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        --strict)
            STRICT_MODE=true
            shift
            ;;
        --quiet)
            QUIET_MODE=true
            shift
            ;;
        --help)
            head -35 "$0" | tail -14
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Find workspace root (look for .loom directory)
find_workspace_root() {
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.loom" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    return 1
}

WORKSPACE_ROOT=$(find_workspace_root) || {
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"error": "Not in a Loom workspace (no .loom directory found)"}'
    else
        echo -e "${RED}Error: Not in a Loom workspace (no .loom directory found)${NC}" >&2
    fi
    exit 1
}

# Resolved through the config-resolver tier chain (#4062) rather than a
# hand-rolled single-tier .loom/config.json read -- a workspace may supply
# terminals entirely from .loom-project/project.json or .loom-local/local.json.
# Resolved ONCE and reused below by get_configured_roles (Trap 2 -- never
# re-merge the tier chain per key).
_LOOM_VALIDATE_ROLES_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib/config-resolver.sh
source "$_LOOM_VALIDATE_ROLES_LIB_DIR/lib/config-resolver.sh"

EFFECTIVE_CONFIG=$(loom_resolve_config "$WORKSPACE_ROOT")

# Retargeted hard-fail precondition (#4059/#4062): "no tier supplies a
# non-empty terminals array" replaces "the legacy .loom/config.json file is
# missing" -- mirrors loom-daemon's handle_validate_command / role_validation.rs
# `has_terminals` check so the Bash and Rust validators agree. Both the --json
# and human output branches are preserved, and the message names every tier
# searched instead of a single legacy path.
TERMINAL_COUNT=$(echo "$EFFECTIVE_CONFIG" | jq '(.terminals // []) | length' 2>/dev/null || echo 0)
if [[ "$TERMINAL_COUNT" -eq 0 ]]; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"error": "No Loom config with a non-empty terminals array found in any tier", "searched": ["'"$WORKSPACE_ROOT/$LOOM_CONFIG_LEGACY_REL"'", "'"$WORKSPACE_ROOT/$LOOM_CONFIG_PROJECT_REL"'", "'"$WORKSPACE_ROOT/$LOOM_CONFIG_LOCAL_REL"'"]}'
    else
        echo -e "${RED}Error: No Loom config with a non-empty \`terminals\` array found in any tier.${NC}" >&2
        echo "Searched (lowest to highest precedence):" >&2
        echo "  - $WORKSPACE_ROOT/$LOOM_CONFIG_LEGACY_REL" >&2
        echo "  - $WORKSPACE_ROOT/$LOOM_CONFIG_PROJECT_REL" >&2
        echo "  - $WORKSPACE_ROOT/$LOOM_CONFIG_LOCAL_REL" >&2
    fi
    exit 1
fi

# Extract configured roles from the resolved effective config.
get_configured_roles() {
    echo "$EFFECTIVE_CONFIG" | jq -r '.terminals[]?.roleConfig?.roleFile // empty' 2>/dev/null | \
        sed 's/\.md$//' | \
        sort -u
}

# Define role dependencies
# Format: role:dependency:message
ROLE_DEPENDENCIES=(
    "champion:doctor:Champion can set loom:changes-requested, but Doctor is not configured to handle it"
    "builder:judge:Builder creates PRs with loom:review-requested, but Judge is not configured to review them"
    "judge:doctor:Judge can request changes with loom:changes-requested, but Doctor is not configured to address them"
    "judge:champion:Judge approves PRs with loom:pr, but Champion is not configured to merge them"
    "curator:champion:Curator marks issues loom:curated, but no Champion configured to auto-promote them"
)

# Check if a role is configured
is_role_configured() {
    local role="$1"
    local configured_roles="$2"
    echo "$configured_roles" | grep -q "^${role}$"
}

# Main validation logic
validate_roles() {
    local configured_roles
    configured_roles=$(get_configured_roles)

    # Initialize arrays (need to handle empty case for set -u)
    local -a warnings=()

    # Check each dependency
    for dep_entry in "${ROLE_DEPENDENCIES[@]}"; do
        IFS=':' read -r role dependency message <<< "$dep_entry"

        # If the role is configured, check if its dependency is also configured
        if is_role_configured "$role" "$configured_roles"; then
            if ! is_role_configured "$dependency" "$configured_roles"; then
                warnings+=("$role|$dependency|$message")
            fi
        fi
    done

    # Output results
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        output_json "$configured_roles" "${warnings[@]+"${warnings[@]}"}"
    else
        output_text "$configured_roles" "${warnings[@]+"${warnings[@]}"}"
    fi

    # Determine exit code
    if [[ ${#warnings[@]} -gt 0 && "$STRICT_MODE" == "true" ]]; then
        return 2
    fi
    return 0
}

output_json() {
    local configured_roles="$1"
    shift
    local warnings=("$@")

    # Build JSON array for configured roles
    local roles_json
    roles_json=$(echo "$configured_roles" | jq -R -s 'split("\n") | map(select(length > 0))')

    # Build JSON array for warnings
    local warnings_json="[]"
    if [[ ${#warnings[@]} -gt 0 ]]; then
        warnings_json="["
        local first=true
        for warning in "${warnings[@]}"; do
            IFS='|' read -r role dependency message <<< "$warning"
            if [[ "$first" != "true" ]]; then
                warnings_json+=","
            fi
            warnings_json+="{\"role\":\"$role\",\"missing_dependency\":\"$dependency\",\"message\":\"$message\"}"
            first=false
        done
        warnings_json+="]"
    fi

    # Output complete JSON
    cat <<EOF
{
  "valid": true,
  "configured_roles": $roles_json,
  "warnings": $warnings_json,
  "errors": [],
  "workspace": "$WORKSPACE_ROOT"
}
EOF
}

output_text() {
    local configured_roles="$1"
    shift
    local all_args=("$@")

    # Split warnings and errors (errors would be after a sentinel, but we don't have errors yet)
    local warnings=("${all_args[@]}")

    # Print configured roles
    if [[ "$QUIET_MODE" != "true" ]]; then
        local roles_list
        roles_list=$(echo "$configured_roles" | tr '\n' ', ' | sed 's/,$//')
        echo -e "${GREEN}Configured roles:${NC} $roles_list"
        echo
    fi

    # Print warnings
    if [[ ${#warnings[@]} -gt 0 && "${warnings[0]}" != "" ]]; then
        echo -e "${YELLOW}ROLE CONFIGURATION WARNINGS:${NC}"
        for warning in "${warnings[@]}"; do
            if [[ -n "$warning" ]]; then
                IFS='|' read -r role dependency message <<< "$warning"
                local role_upper
                local dep_upper
                role_upper=$(echo "$role" | tr '[:lower:]' '[:upper:]')
                dep_upper=$(echo "$dependency" | tr '[:lower:]' '[:upper:]')
                echo -e "  ${YELLOW}-${NC} ${BLUE}${role_upper}${NC} -> ${BLUE}${dep_upper}${NC}: $message"
            fi
        done
        echo
        echo "The daemon will continue, but some workflows may get stuck."
        echo "Consider adding the missing roles to .loom/config.json"
        echo

        # Suggest fix
        echo -e "${GREEN}To fix:${NC}"
        echo "  1. Open .loom/config.json"
        echo "  2. Add terminal configurations for missing roles"
        echo "  3. Restart the daemon"
    elif [[ "$QUIET_MODE" != "true" ]]; then
        echo -e "${GREEN}All role dependencies are satisfied.${NC}"
    fi
}

# Run validation
validate_roles
exit_code=$?
exit $exit_code
