#!/usr/bin/env bash
# Session reflection - daemon shutdown stage for self-improvement
# Usage: ./scripts/session-reflection.sh [options]
#
# Analyzes the daemon session and optionally creates upstream issues for improvements.
# Called during graceful daemon shutdown.

set -euo pipefail

# ANSI color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

error() { echo -e "${RED}Error: $*${NC}" >&2; exit 1; }
info() { echo -e "${BLUE}$*${NC}"; }
success() { echo -e "${GREEN}$*${NC}"; }
warning() { echo -e "${YELLOW}$*${NC}"; }
header() { echo -e "${CYAN}${BOLD}$*${NC}"; }
dim() { echo -e "\033[2m$*${NC}"; }

# Detect repository root
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || error "Not in a git repository"

# Paths
DAEMON_STATE="$REPO_ROOT/.loom/daemon-state.json"
CONFIG_FILE="$REPO_ROOT/.loom/config.json"

# Default configuration
DEFAULT_ENABLED=true
DEFAULT_AUTO_CREATE=false
DEFAULT_MIN_DURATION=300  # 5 minutes
DEFAULT_UPSTREAM_REPO="rjwalters/loom"

# Parse configuration
get_config() {
  local key="$1"
  local default="$2"

  if [[ -f "$CONFIG_FILE" ]]; then
    local value
    value=$(jq -r --arg k "$key" --arg d "$default" '.reflection[$k] // $d' "$CONFIG_FILE" 2>/dev/null || echo "$default")
    if [[ "$value" == "null" || -z "$value" ]]; then
      echo "$default"
    else
      echo "$value"
    fi
  else
    echo "$default"
  fi
}

# Load configuration
REFLECTION_ENABLED=$(get_config "enabled" "$DEFAULT_ENABLED")
AUTO_CREATE=$(get_config "auto_create_issues" "$DEFAULT_AUTO_CREATE")
MIN_DURATION=$(get_config "min_session_duration" "$DEFAULT_MIN_DURATION")
UPSTREAM_REPO=$(get_config "upstream_repo" "$DEFAULT_UPSTREAM_REPO")

# Check for help
for arg in "$@"; do
  if [[ "$arg" == "--help" || "$arg" == "-h" ]]; then
    cat <<EOF
Session reflection - daemon shutdown stage for self-improvement

Usage: ./scripts/session-reflection.sh [options]

Options:
  --dry-run         Preview without creating issues
  --auto            Auto-create issues without prompting (requires config)
  --skip            Skip reflection entirely
  --json            Output analysis as JSON (no issue creation)
  -h, --help        Show this help message

Configuration (.loom/config.json):
  {
    "reflection": {
      "enabled": true,              # Enable reflection stage
      "auto_create_issues": false,  # Create issues without prompting
      "min_session_duration": 300,  # Skip for sessions < 5 min
      "upstream_repo": "rjwalters/loom"
    }
  }

Categories of issues created:
  - bug: Unexpected errors or workarounds needed
  - enhancement: Missing features, improvement opportunities
  - documentation: Gaps or confusion in documentation

Privacy:
  - Does NOT include repository-specific code or file paths
  - Only includes Loom framework observations and metrics
  - All issues tagged with [Session Reflection] for transparency
EOF
    exit 0
  fi
done

# Parse arguments
DRY_RUN=false
AUTO_MODE=false
SKIP=false
JSON_OUTPUT=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --auto)
      AUTO_MODE=true
      shift
      ;;
    --skip)
      SKIP=true
      shift
      ;;
    --json)
      JSON_OUTPUT=true
      shift
      ;;
    *)
      error "Unknown option: $1\nUse --help for usage information"
      ;;
  esac
done

# Check if reflection is enabled
if [[ "$REFLECTION_ENABLED" != "true" ]]; then
  info "Session reflection disabled (config: reflection.enabled = false)"
  exit 0
fi

if [[ "$SKIP" == true ]]; then
  info "Session reflection skipped (--skip flag)"
  exit 0
fi

# Check for daemon state
if [[ ! -f "$DAEMON_STATE" ]]; then
  info "No daemon state found, skipping reflection"
  exit 0
fi

# Calculate session duration
calculate_duration() {
  local started_at shutdown_at
  started_at=$(jq -r '.started_at // empty' "$DAEMON_STATE")
  shutdown_at=$(jq -r '.shutdown_at // empty' "$DAEMON_STATE")

  if [[ -z "$started_at" ]]; then
    echo "0"
    return
  fi

  # Use shutdown_at if session has ended, otherwise use current time
  local end_time
  if [[ -n "$shutdown_at" ]]; then
    end_time="$shutdown_at"
  else
    end_time=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  fi

  local start_epoch
  local end_epoch

  # macOS and Linux compatible date parsing
  if date --version >/dev/null 2>&1; then
    # GNU date (Linux)
    start_epoch=$(date -d "$started_at" +%s 2>/dev/null || echo "0")
    end_epoch=$(date -d "$end_time" +%s 2>/dev/null || echo "0")
  else
    # BSD date (macOS)
    start_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$started_at" +%s 2>/dev/null || echo "0")
    end_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$end_time" +%s 2>/dev/null || echo "0")
  fi

  echo $((end_epoch - start_epoch))
}

duration=$(calculate_duration)

# Check minimum duration
if [[ "$duration" -lt "$MIN_DURATION" ]]; then
  info "Session too short ($duration seconds < $MIN_DURATION seconds), skipping reflection"
  exit 0
fi

# Format duration for display
format_duration() {
  local seconds=$1
  local hours=$((seconds / 3600))
  local minutes=$(((seconds % 3600) / 60))
  local secs=$((seconds % 60))

  if [[ $hours -gt 0 ]]; then
    echo "${hours}h ${minutes}m"
  elif [[ $minutes -gt 0 ]]; then
    echo "${minutes}m ${secs}s"
  else
    echo "${secs}s"
  fi
}

# Extract session metrics
extract_metrics() {
  local state="$1"

  jq -c '{
    iteration: (.iteration // 0),
    completed_issues: ((.completed_issues // []) | length),
    total_prs_merged: (.total_prs_merged // 0),
    warnings: ((.warnings // []) | length),
    stuck_detections: ((.stuck_detection.total_detections // 0)),
    force_mode: (.force_mode // false),
    session_limit_pauses: ((.session_limit_awareness.total_pauses // 0))
  }' "$state"
}

# Extract warnings (filtering sensitive info)
extract_warnings() {
  local state="$1"

  # Extract warnings but remove any file paths or sensitive context
  jq '[(.warnings // [])[] | {
    type: (.type // "unknown"),
    severity: (.severity // "unknown"),
    message: ((.message // "") | gsub("/[^\\s]+"; "[path]")),
    time: (.time // "unknown")
  }] | if length > 10 then .[-10:] else . end' "$state"
}

# Extract stuck detection patterns
extract_stuck_patterns() {
  local state="$1"

  jq '{
    config: (.stuck_detection.config // {}),
    recent_detections: ([
      ((.stuck_detection // {}).recent_detections // [])[] | {
        severity: (.severity // "unknown"),
        indicators: (.indicators // []),
        intervention: (.intervention // "none")
      }
    ] | if length > 5 then .[-5:] else . end),
    total: ((.stuck_detection // {}).total_detections // 0)
  }' "$state"
}

# Analyze session and identify improvements
analyze_session() {
  local state="$1"
  local metrics
  local warnings
  local stuck

  metrics=$(extract_metrics "$state")
  warnings=$(extract_warnings "$state")
  stuck=$(extract_stuck_patterns "$state")

  # Build analysis output
  jq -n --argjson metrics "$metrics" \
        --argjson warnings "$warnings" \
        --argjson stuck "$stuck" \
        --arg duration "$(format_duration "$duration")" \
        --arg duration_seconds "$duration" '{
    session: {
      duration: $duration,
      duration_seconds: ($duration_seconds | tonumber),
      iterations: $metrics.iteration,
      completed_issues: $metrics.completed_issues,
      prs_merged: $metrics.total_prs_merged,
      force_mode: $metrics.force_mode
    },
    health: {
      warnings_count: $metrics.warnings,
      stuck_count: $metrics.stuck_detections,
      rate_limit_pauses: $metrics.session_limit_pauses
    },
    warnings: $warnings,
    stuck_detection: $stuck,
    improvement_signals: []
  }' | add_improvement_signals
}

# Add improvement signals based on patterns
add_improvement_signals() {
  local analysis
  analysis=$(cat)

  local signals='[]'

  # Check for stuck detection issues
  local stuck_count
  stuck_count=$(echo "$analysis" | jq '.health.stuck_count')
  if [[ "$stuck_count" -gt 0 ]]; then
    signals=$(echo "$signals" | jq --arg count "$stuck_count" '. += [{
      category: "enhancement",
      title: "Agents getting stuck frequently",
      signal: "stuck_detection",
      severity: "medium",
      context: "Stuck agents detected \($count) times during session"
    }]')
  fi

  # Check for repeated warning patterns
  local warning_types
  warning_types=$(echo "$analysis" | jq '[.warnings[].type] | group_by(.) | map({type: .[0], count: length}) | sort_by(-.count) | .[0:3]')

  for pattern in $(echo "$warning_types" | jq -c '.[] | select(.count >= 3)'); do
    local type count
    type=$(echo "$pattern" | jq -r '.type')
    count=$(echo "$pattern" | jq -r '.count')

    signals=$(echo "$signals" | jq --arg type "$type" --arg count "$count" '. += [{
      category: "bug",
      title: "Repeated warning: \($type)",
      signal: "warning_pattern",
      severity: "medium",
      context: "Warning type \($type) occurred \($count) times"
    }]')
  done

  # Check for rate limit issues
  local rate_pauses
  rate_pauses=$(echo "$analysis" | jq '.health.rate_limit_pauses')
  if [[ "$rate_pauses" -gt 0 ]]; then
    signals=$(echo "$signals" | jq --arg count "$rate_pauses" '. += [{
      category: "enhancement",
      title: "Rate limiting affecting operation",
      signal: "rate_limit",
      severity: "low",
      context: "Session paused \($count) times due to rate limits"
    }]')
  fi

  # Check for low completion rate
  local iterations completed
  iterations=$(echo "$analysis" | jq '.session.iterations')
  completed=$(echo "$analysis" | jq '.session.completed_issues')

  if [[ "$iterations" -gt 10 && "$completed" -eq 0 ]]; then
    signals=$(echo "$signals" | jq --arg iters "$iterations" '. += [{
      category: "bug",
      title: "No issues completed despite many iterations",
      signal: "low_velocity",
      severity: "high",
      context: "0 issues completed after \($iters) iterations"
    }]')
  fi

  echo "$analysis" | jq --argjson signals "$signals" '.improvement_signals = $signals'
}

# Generate issue proposals from analysis
generate_proposals() {
  local analysis="$1"

  echo "$analysis" | jq '[.improvement_signals[] | {
    title: "[Session Reflection] \(.title)",
    category: .category,
    severity: .severity,
    body: "## Observation\n\n\(.context)\n\n## Session Context\n\n- Signal type: `\(.signal)`\n- Severity: \(.severity)\n\n## Suggested Investigation\n\nThis was automatically identified during a Loom daemon session reflection.\n\n---\n*Auto-generated by Loom session reflection*"
  }]'
}

# Display proposals for user review
display_proposals() {
  local proposals="$1"
  local count
  count=$(echo "$proposals" | jq 'length')

  if [[ "$count" -eq 0 ]]; then
    info "No improvement opportunities identified this session."
    return 1
  fi

  echo ""
  header "═══════════════════════════════════════════════════════════════════"
  header "  SESSION REFLECTION"
  header "═══════════════════════════════════════════════════════════════════"
  echo ""

  info "Reviewing session for improvement opportunities..."
  echo ""

  echo -e "Found ${BOLD}$count${NC} potential upstream issue(s):"
  echo ""

  local idx=1
  while IFS= read -r proposal; do
    local title category severity
    title=$(echo "$proposal" | jq -r '.title')
    category=$(echo "$proposal" | jq -r '.category')
    severity=$(echo "$proposal" | jq -r '.severity')

    echo -e "  ${BOLD}$idx.${NC} [$category] $title"
    echo -e "     Severity: $severity"
    echo ""

    ((idx++))
  done < <(echo "$proposals" | jq -c '.[]')

  return 0
}

# Create issues on GitHub
create_issues() {
  local proposals="$1"
  local created=0

  while IFS= read -r proposal; do
    local title body category
    title=$(echo "$proposal" | jq -r '.title')
    body=$(echo "$proposal" | jq -r '.body')
    category=$(echo "$proposal" | jq -r '.category')

    if [[ "$DRY_RUN" == true ]]; then
      info "[DRY-RUN] Would create: $title"
    else
      local issue_url
      issue_url=$(gh issue create \
        --repo "$UPSTREAM_REPO" \
        --title "$title" \
        --label "$category" \
        --body "$body" 2>/dev/null || echo "")

      if [[ -n "$issue_url" ]]; then
        success "Created: $issue_url"
        ((created++))
      else
        warning "Failed to create issue: $title"
      fi
    fi
  done < <(echo "$proposals" | jq -c '.[]')

  echo "$created"
}

# Interactive consent flow
prompt_consent() {
  local proposals="$1"
  local count
  count=$(echo "$proposals" | jq 'length')

  echo "Options:"
  echo "  [1] Create all issues"
  echo "  [2] Review individually"
  echo "  [3] Skip reflection"
  echo ""

  read -r -p "Choice (1-3): " choice

  case "$choice" in
    1)
      create_issues "$proposals"
      ;;
    2)
      # Individual review
      local selected='[]'
      local idx=1

      while IFS= read -r proposal; do
        local title
        title=$(echo "$proposal" | jq -r '.title')

        read -r -p "Create '$title'? (y/N): " response
        if [[ "$response" =~ ^[Yy]$ ]]; then
          selected=$(echo "$selected" | jq --argjson p "$proposal" '. += [$p]')
        fi

        ((idx++))
      done < <(echo "$proposals" | jq -c '.[]')

      local selected_count
      selected_count=$(echo "$selected" | jq 'length')

      if [[ "$selected_count" -gt 0 ]]; then
        create_issues "$selected"
      else
        info "No issues selected"
      fi
      ;;
    3)
      info "Reflection skipped"
      ;;
    *)
      warning "Invalid choice, skipping reflection"
      ;;
  esac
}

# Main execution
main() {
  # Run analysis
  local analysis
  analysis=$(analyze_session "$DAEMON_STATE")

  # JSON output mode
  if [[ "$JSON_OUTPUT" == true ]]; then
    echo "$analysis"
    exit 0
  fi

  # Generate proposals
  local proposals
  proposals=$(generate_proposals "$analysis")

  # Display session summary
  local session_info
  session_info=$(echo "$analysis" | jq -r '"Session: \(.session.duration) | Iterations: \(.session.iterations) | Completed: \(.session.completed_issues) | PRs: \(.session.prs_merged)"')

  dim "$session_info"

  # Display proposals
  if ! display_proposals "$proposals"; then
    exit 0
  fi

  # Handle issue creation
  if [[ "$DRY_RUN" == true ]]; then
    info "[DRY-RUN] Would create the following issues:"
    create_issues "$proposals"
  elif [[ "$AUTO_MODE" == true || "$AUTO_CREATE" == "true" ]]; then
    info "Auto-create mode enabled"
    create_issues "$proposals"
  else
    # Interactive consent
    prompt_consent "$proposals"
  fi

  echo ""
  success "Session reflection complete"
}

main
