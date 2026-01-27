#!/bin/bash
#
# claim.sh - Atomic file-based claiming system for parallel agent orchestration
#
# Uses mkdir for atomic claim creation (succeeds or fails atomically on all platforms).
# Claims are stored in .loom/claims/issue-<N>.lock directories with metadata.
#
# Usage:
#   claim.sh claim <issue-number> [agent-id] [ttl-seconds]
#   claim.sh extend <issue-number> <agent-id> [additional-seconds]
#   claim.sh release <issue-number> [agent-id]
#   claim.sh check <issue-number>
#   claim.sh list
#   claim.sh cleanup
#
# Exit codes:
#   0 - Success
#   1 - Claim already exists (for claim), or general error
#   2 - Invalid arguments
#   3 - Claim not found (for release/check)
#   4 - Agent ID mismatch (for release)
#

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default TTL: 30 minutes
DEFAULT_TTL=1800

# Find the repository root (handles being called from anywhere)
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
CLAIMS_DIR="$REPO_ROOT/.loom/claims"

# Ensure claims directory exists
ensure_claims_dir() {
    mkdir -p "$CLAIMS_DIR"
}

# Get current timestamp
get_timestamp() {
    date -u +"%Y-%m-%dT%H:%M:%SZ"
}

# Get expiration timestamp (current time + TTL seconds)
get_expiration() {
    local ttl="${1:-$DEFAULT_TTL}"
    if [[ "$(uname)" == "Darwin" ]]; then
        date -u -v+"${ttl}S" +"%Y-%m-%dT%H:%M:%SZ"
    else
        date -u -d "+${ttl} seconds" +"%Y-%m-%dT%H:%M:%SZ"
    fi
}

# Check if a claim has expired
is_expired() {
    local expiration="$1"
    local current
    current=$(get_timestamp)

    # Compare ISO timestamps lexicographically (works because ISO format is sortable)
    [[ "$current" > "$expiration" ]]
}

# Generate default agent ID if not provided
get_agent_id() {
    local provided="${1:-}"
    if [[ -n "$provided" ]]; then
        echo "$provided"
    else
        # Use hostname-pid as default agent ID
        echo "$(hostname)-$$"
    fi
}

# Claim an issue
# Usage: claim_issue <issue-number> [agent-id] [ttl-seconds]
claim_issue() {
    local issue_number="$1"
    local agent_id
    local ttl="${3:-$DEFAULT_TTL}"

    if [[ -z "$issue_number" ]]; then
        echo -e "${RED}Error: Issue number required${NC}" >&2
        return 2
    fi

    agent_id=$(get_agent_id "${2:-}")

    ensure_claims_dir

    local claim_dir="$CLAIMS_DIR/issue-${issue_number}.lock"
    local claim_file="$claim_dir/claim.json"

    # Attempt atomic directory creation
    # mkdir will fail if directory already exists (atomic operation)
    if mkdir "$claim_dir" 2>/dev/null; then
        # Successfully created - write metadata
        local timestamp
        local expiration
        timestamp=$(get_timestamp)
        expiration=$(get_expiration "$ttl")

        cat > "$claim_file" << EOF
{
  "issue": $issue_number,
  "agent_id": "$agent_id",
  "claimed_at": "$timestamp",
  "expires_at": "$expiration",
  "ttl_seconds": $ttl
}
EOF
        echo -e "${GREEN}✓ Claimed issue #${issue_number}${NC}"
        echo -e "  Agent: ${agent_id}"
        echo -e "  Expires: ${expiration}"
        return 0
    else
        # Directory already exists - check if claim is expired
        if [[ -f "$claim_file" ]]; then
            local existing_expiration
            existing_expiration=$(grep -o '"expires_at": "[^"]*"' "$claim_file" | cut -d'"' -f4)

            if is_expired "$existing_expiration"; then
                # Expired claim - clean up and retry
                echo -e "${YELLOW}⚠ Found expired claim, cleaning up...${NC}"
                rm -rf "$claim_dir"

                # Retry claim
                claim_issue "$issue_number" "$agent_id" "$ttl"
                return $?
            else
                # Active claim by another agent
                local existing_agent
                existing_agent=$(grep -o '"agent_id": "[^"]*"' "$claim_file" | cut -d'"' -f4)
                echo -e "${RED}✗ Issue #${issue_number} already claimed${NC}" >&2
                echo -e "  By: ${existing_agent}" >&2
                echo -e "  Expires: ${existing_expiration}" >&2
                return 1
            fi
        else
            # Lock dir exists but no claim file - clean up and retry
            echo -e "${YELLOW}⚠ Found incomplete claim, cleaning up...${NC}"
            rm -rf "$claim_dir"
            claim_issue "$issue_number" "$agent_id" "$ttl"
            return $?
        fi
    fi
}

# Extend a claim's TTL
# Usage: extend_claim <issue-number> <agent-id> [additional-seconds]
extend_claim() {
    local issue_number="$1"
    local agent_id="$2"
    local additional_seconds="${3:-$DEFAULT_TTL}"

    if [[ -z "$issue_number" ]]; then
        echo -e "${RED}Error: Issue number required${NC}" >&2
        return 2
    fi

    if [[ -z "$agent_id" ]]; then
        echo -e "${RED}Error: Agent ID required for extend${NC}" >&2
        return 2
    fi

    local claim_dir="$CLAIMS_DIR/issue-${issue_number}.lock"
    local claim_file="$claim_dir/claim.json"

    if [[ ! -d "$claim_dir" ]]; then
        echo -e "${YELLOW}⚠ No claim found for issue #${issue_number}${NC}"
        return 3
    fi

    if [[ ! -f "$claim_file" ]]; then
        echo -e "${YELLOW}⚠ Incomplete claim found for issue #${issue_number}${NC}"
        return 3
    fi

    # Verify agent owns the claim
    local existing_agent
    existing_agent=$(grep -o '"agent_id": "[^"]*"' "$claim_file" | cut -d'"' -f4)

    if [[ "$existing_agent" != "$agent_id" ]]; then
        echo -e "${RED}✗ Cannot extend: claim owned by different agent${NC}" >&2
        echo -e "  Owner: ${existing_agent}" >&2
        echo -e "  Requested by: ${agent_id}" >&2
        return 4
    fi

    # Calculate new expiration from now + additional_seconds
    local new_expiration
    new_expiration=$(get_expiration "$additional_seconds")

    # Read current values
    local issue
    local claimed_at
    local ttl_seconds
    issue=$(grep -o '"issue": [0-9]*' "$claim_file" | cut -d' ' -f2)
    claimed_at=$(grep -o '"claimed_at": "[^"]*"' "$claim_file" | cut -d'"' -f4)
    ttl_seconds=$(grep -o '"ttl_seconds": [0-9]*' "$claim_file" | cut -d' ' -f2)

    # Write updated claim
    cat > "$claim_file" << EOF
{
  "issue": $issue,
  "agent_id": "$agent_id",
  "claimed_at": "$claimed_at",
  "expires_at": "$new_expiration",
  "ttl_seconds": $additional_seconds
}
EOF

    echo -e "${GREEN}✓ Extended claim for issue #${issue_number}${NC}"
    echo -e "  New expiration: ${new_expiration}"
    echo -e "  Extended by: ${additional_seconds} seconds"
    return 0
}

# Release a claim
# Usage: release_claim <issue-number> [agent-id]
release_claim() {
    local issue_number="$1"
    local agent_id="${2:-}"

    if [[ -z "$issue_number" ]]; then
        echo -e "${RED}Error: Issue number required${NC}" >&2
        return 2
    fi

    local claim_dir="$CLAIMS_DIR/issue-${issue_number}.lock"
    local claim_file="$claim_dir/claim.json"

    if [[ ! -d "$claim_dir" ]]; then
        echo -e "${YELLOW}⚠ No claim found for issue #${issue_number}${NC}"
        return 3
    fi

    # If agent_id provided, verify it matches
    if [[ -n "$agent_id" ]] && [[ -f "$claim_file" ]]; then
        local existing_agent
        existing_agent=$(grep -o '"agent_id": "[^"]*"' "$claim_file" | cut -d'"' -f4)

        if [[ "$existing_agent" != "$agent_id" ]]; then
            echo -e "${RED}✗ Cannot release: claim owned by different agent${NC}" >&2
            echo -e "  Owner: ${existing_agent}" >&2
            echo -e "  Requested by: ${agent_id}" >&2
            return 4
        fi
    fi

    # Remove the claim
    rm -rf "$claim_dir"
    echo -e "${GREEN}✓ Released claim for issue #${issue_number}${NC}"
    return 0
}

# Check if an issue is claimed
# Usage: check_claim <issue-number>
check_claim() {
    local issue_number="$1"

    if [[ -z "$issue_number" ]]; then
        echo -e "${RED}Error: Issue number required${NC}" >&2
        return 2
    fi

    local claim_dir="$CLAIMS_DIR/issue-${issue_number}.lock"
    local claim_file="$claim_dir/claim.json"

    if [[ ! -d "$claim_dir" ]]; then
        echo -e "${BLUE}ℹ Issue #${issue_number} is not claimed${NC}"
        return 3
    fi

    if [[ ! -f "$claim_file" ]]; then
        echo -e "${YELLOW}⚠ Incomplete claim found for issue #${issue_number}${NC}"
        return 3
    fi

    # Check expiration
    local expiration
    expiration=$(grep -o '"expires_at": "[^"]*"' "$claim_file" | cut -d'"' -f4)

    if is_expired "$expiration"; then
        echo -e "${YELLOW}⚠ Issue #${issue_number} has an expired claim${NC}"
        cat "$claim_file"
        return 3
    fi

    echo -e "${GREEN}✓ Issue #${issue_number} is claimed${NC}"
    cat "$claim_file"
    return 0
}

# List all active claims
list_claims() {
    ensure_claims_dir

    local count=0
    echo -e "${BLUE}Active claims:${NC}"
    echo ""

    for claim_dir in "$CLAIMS_DIR"/issue-*.lock; do
        if [[ -d "$claim_dir" ]]; then
            local claim_file="$claim_dir/claim.json"
            if [[ -f "$claim_file" ]]; then
                local issue
                local agent
                local expiration
                issue=$(grep -o '"issue": [0-9]*' "$claim_file" | cut -d' ' -f2)
                agent=$(grep -o '"agent_id": "[^"]*"' "$claim_file" | cut -d'"' -f4)
                expiration=$(grep -o '"expires_at": "[^"]*"' "$claim_file" | cut -d'"' -f4)

                if is_expired "$expiration"; then
                    echo -e "  ${YELLOW}Issue #${issue} (EXPIRED)${NC}"
                else
                    echo -e "  ${GREEN}Issue #${issue}${NC} - Agent: ${agent}, Expires: ${expiration}"
                fi
                ((count++))
            fi
        fi
    done

    if [[ $count -eq 0 ]]; then
        echo -e "  ${BLUE}(none)${NC}"
    fi

    echo ""
    echo "Total: $count claim(s)"
}

# Cleanup expired claims
cleanup_claims() {
    ensure_claims_dir

    local cleaned=0
    echo -e "${BLUE}Cleaning up expired claims...${NC}"

    for claim_dir in "$CLAIMS_DIR"/issue-*.lock; do
        if [[ -d "$claim_dir" ]]; then
            local claim_file="$claim_dir/claim.json"
            if [[ -f "$claim_file" ]]; then
                local expiration
                expiration=$(grep -o '"expires_at": "[^"]*"' "$claim_file" | cut -d'"' -f4)

                if is_expired "$expiration"; then
                    local issue
                    issue=$(grep -o '"issue": [0-9]*' "$claim_file" | cut -d' ' -f2)
                    rm -rf "$claim_dir"
                    echo -e "  ${GREEN}✓ Removed expired claim for issue #${issue}${NC}"
                    ((cleaned++))
                fi
            else
                # No claim file - incomplete claim, remove it
                rm -rf "$claim_dir"
                ((cleaned++))
            fi
        fi
    done

    if [[ $cleaned -eq 0 ]]; then
        echo -e "  ${BLUE}No expired claims found${NC}"
    else
        echo -e "\nCleaned up $cleaned expired claim(s)"
    fi
}

# Print usage
usage() {
    cat << EOF
Usage: $(basename "$0") <command> [arguments]

Commands:
  claim <issue-number> [agent-id] [ttl-seconds]
      Atomically claim an issue. Default TTL is 30 minutes (1800 seconds).
      Exits 0 on success, 1 if already claimed.

  extend <issue-number> <agent-id> [additional-seconds]
      Extend an existing claim's TTL. Agent must own the claim.
      Default extension is 30 minutes (1800 seconds) from now.
      Exits 0 on success, 3 if no claim exists, 4 if agent mismatch.

  release <issue-number> [agent-id]
      Release a claim. If agent-id is provided, verifies ownership.
      Exits 0 on success, 3 if no claim exists, 4 if agent mismatch.

  check <issue-number>
      Check if an issue is claimed and print claim metadata.
      Exits 0 if claimed, 3 if not claimed or expired.

  list
      List all active claims.

  cleanup
      Remove all expired claims.

Examples:
  $(basename "$0") claim 123                    # Claim issue with default agent ID
  $(basename "$0") claim 123 builder-1 3600     # Claim for 1 hour
  $(basename "$0") extend 123 builder-1         # Extend by default 30 minutes
  $(basename "$0") extend 123 builder-1 7200    # Extend by 2 hours
  $(basename "$0") release 123 builder-1        # Release with ownership check
  $(basename "$0") check 123                    # Check claim status
  $(basename "$0") list                         # List all claims
  $(basename "$0") cleanup                      # Clean expired claims
EOF
}

# Main entry point
main() {
    local command="${1:-}"

    case "$command" in
        claim)
            claim_issue "${2:-}" "${3:-}" "${4:-}"
            ;;
        extend)
            extend_claim "${2:-}" "${3:-}" "${4:-}"
            ;;
        release)
            release_claim "${2:-}" "${3:-}"
            ;;
        check)
            check_claim "${2:-}"
            ;;
        list)
            list_claims
            ;;
        cleanup)
            cleanup_claims
            ;;
        -h|--help|help)
            usage
            ;;
        "")
            echo -e "${RED}Error: No command specified${NC}" >&2
            echo ""
            usage
            exit 2
            ;;
        *)
            echo -e "${RED}Error: Unknown command '$command'${NC}" >&2
            echo ""
            usage
            exit 2
            ;;
    esac
}

main "$@"
