#!/bin/bash

# validate-phase.sh - Validate shepherd phase contracts and attempt recovery
#
# Usage:
#   validate-phase.sh <phase> <issue-number> [options]
#
# Phases:
#   curator     Check for loom:curated label
#   builder     Check for PR with loom:review-requested
#   judge       Check for loom:pr or loom:changes-requested on PR
#   doctor      Check for loom:review-requested on PR
#
# Options:
#   --worktree <path>   Worktree path (required for builder recovery)
#   --pr <number>       PR number (required for judge/doctor)
#   --task-id <id>      Shepherd task ID for milestone reporting
#   --json              Output results as JSON
#
# Exit codes:
#   0 - Contract satisfied (initially or after recovery)
#   1 - Contract failed, recovery failed or not possible
#   2 - Invalid arguments
#
# Part of the Loom orchestration system for shepherd phase contract validation.

set -euo pipefail

# Colors for output (disabled if stdout is not a terminal)
# BLUE unused but kept for consistency with other scripts and future use
# shellcheck disable=SC2034
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    NC=''
fi

# Find the repository root (works from any subdirectory including worktrees)
find_repo_root() {
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.git" ]] || [[ -f "$dir/.git" ]]; then
            if [[ -f "$dir/.git" ]]; then
                local gitdir
                gitdir=$(cat "$dir/.git" | sed 's/^gitdir: //')
                if [[ "$gitdir" == /* ]]; then
                    dirname "$(dirname "$(dirname "$gitdir")")"
                else
                    dirname "$(dirname "$(dirname "$dir/$gitdir")")"
                fi
            else
                echo "$dir"
            fi
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    echo "$PWD"
}

REPO_ROOT="$(find_repo_root)"

# Parse arguments
PHASE=""
ISSUE=""
WORKTREE=""
PR_NUMBER=""
TASK_ID=""
JSON_OUTPUT=false

if [[ $# -lt 2 ]]; then
    echo -e "${RED}Error: Missing required arguments${NC}"
    echo "Usage: validate-phase.sh <phase> <issue-number> [--worktree <path>] [--pr <number>] [--task-id <id>] [--json]"
    exit 2
fi

PHASE="$1"
ISSUE="$2"
shift 2

while [[ $# -gt 0 ]]; do
    case "$1" in
        --worktree)
            WORKTREE="$2"
            shift 2
            ;;
        --pr)
            PR_NUMBER="$2"
            shift 2
            ;;
        --task-id)
            TASK_ID="$2"
            shift 2
            ;;
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        *)
            echo -e "${RED}Error: Unknown argument: $1${NC}"
            exit 2
            ;;
    esac
done

# Validate phase name
case "$PHASE" in
    curator|builder|judge|doctor) ;;
    *)
        echo -e "${RED}Error: Invalid phase '$PHASE'. Must be one of: curator, builder, judge, doctor${NC}"
        exit 2
        ;;
esac

# Validate issue number is numeric
if ! [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
    echo -e "${RED}Error: Issue number must be numeric, got '$ISSUE'${NC}"
    exit 2
fi

# Helper: report milestone if task-id provided
report_milestone() {
    local event="$1"
    shift
    if [[ -n "$TASK_ID" ]] && [[ -x "$REPO_ROOT/.loom/scripts/report-milestone.sh" ]]; then
        "$REPO_ROOT/.loom/scripts/report-milestone.sh" "$event" --task-id "$TASK_ID" "$@" 2>/dev/null || true
    fi
}

# Helper: output result
output_result() {
    local status="$1"  # satisfied, recovered, failed
    local message="$2"
    local recovery_action="${3:-none}"

    if $JSON_OUTPUT; then
        cat <<EOF
{"phase":"$PHASE","issue":$ISSUE,"status":"$status","message":"$message","recovery_action":"$recovery_action"}
EOF
    else
        case "$status" in
            satisfied)
                echo -e "${GREEN}✓ $PHASE phase contract satisfied: $message${NC}"
                ;;
            recovered)
                echo -e "${YELLOW}⟳ $PHASE phase recovered: $message${NC}"
                ;;
            failed)
                echo -e "${RED}✗ $PHASE phase contract failed: $message${NC}"
                ;;
        esac
    fi
}

# Helper: mark issue as blocked
# Uses atomic transition: loom:building -> loom:blocked (mutually exclusive states)
mark_blocked() {
    local reason="$1"
    local diagnostics="${2:-}"
    # Atomic transition to prevent state machine violation
    gh issue edit "$ISSUE" --remove-label "loom:building" --add-label "loom:blocked" 2>/dev/null || true
    local comment_body="**Phase contract failed**: \`$PHASE\` phase did not produce expected outcome. $reason"
    if [[ -n "$diagnostics" ]]; then
        comment_body="$comment_body

$diagnostics"
    fi
    gh issue comment "$ISSUE" --body "$comment_body" 2>/dev/null || true
}

# Helper: gather builder diagnostic information for debugging
# Returns diagnostic text that can be included in the blocked comment
gather_builder_diagnostics() {
    local diagnostics=""
    local worktree_path="$WORKTREE"
    local issue_number="$ISSUE"

    diagnostics+="<details>
<summary>Diagnostic Information</summary>

"

    # 1. Worktree state
    if [[ -d "$worktree_path" ]]; then
        local branch_name
        branch_name=$(git -C "$worktree_path" rev-parse --abbrev-ref HEAD 2>/dev/null) || branch_name="unknown"
        diagnostics+="**Worktree**: \`$worktree_path\` exists
**Branch**: \`$branch_name\`
"

        # Check commits on branch vs main
        local main_branch
        main_branch=$(git -C "$worktree_path" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@') || main_branch="main"
        local commits_ahead
        commits_ahead=$(git -C "$worktree_path" rev-list --count "origin/${main_branch}..HEAD" 2>/dev/null) || commits_ahead="?"
        local commits_behind
        commits_behind=$(git -C "$worktree_path" rev-list --count "HEAD..origin/${main_branch}" 2>/dev/null) || commits_behind="?"
        diagnostics+="**Commits ahead of $main_branch**: $commits_ahead
**Commits behind $main_branch**: $commits_behind
"

        # Check if branch was ever pushed
        if git -C "$worktree_path" rev-parse --abbrev-ref '@{upstream}' &>/dev/null; then
            diagnostics+="**Remote tracking**: configured
"
        else
            diagnostics+="**Remote tracking**: not configured (branch never pushed)
"
        fi
    else
        diagnostics+="**Worktree**: \`$worktree_path\` does not exist
"
    fi

    # 2. Check for tmux session log (builder may have left output)
    local session_name="loom-builder-issue-${issue_number}"
    local log_patterns=(
        "/tmp/loom-${session_name}.out"
        "$REPO_ROOT/.loom/logs/${session_name}.log"
    )
    local found_log=""
    for log_path in "${log_patterns[@]}"; do
        if [[ -f "$log_path" ]]; then
            found_log="$log_path"
            break
        fi
    done

    if [[ -n "$found_log" ]]; then
        local log_tail
        log_tail=$(tail -20 "$found_log" 2>/dev/null | head -15) || log_tail=""
        if [[ -n "$log_tail" ]]; then
            diagnostics+="
**Last 15 lines from session log** (\`$found_log\`):
\`\`\`
$log_tail
\`\`\`
"
        fi
    fi

    # 3. Check issue state for clues
    local issue_labels
    issue_labels=$(gh issue view "$issue_number" --json labels --jq '.labels[].name' 2>/dev/null | tr '\n' ', ') || issue_labels=""
    if [[ -n "$issue_labels" ]]; then
        diagnostics+="
**Current issue labels**: $issue_labels
"
    fi

    # 4. Check for uncommitted changes on main (common workflow violation)
    local main_changes=""
    main_changes=$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null) || true
    if [[ -n "$main_changes" ]]; then
        diagnostics+="
**⚠️ WARNING: Uncommitted changes detected on main branch**:
\`\`\`
$(echo "$main_changes" | head -10)
\`\`\`
This suggests the builder may have worked directly on main instead of in a worktree.
This is a workflow violation - builders MUST work in worktrees.
"
    fi

    # 5. Potential causes based on state
    diagnostics+="
**Possible causes**:
"
    if [[ ! -d "$worktree_path" ]]; then
        diagnostics+="- Worktree was never created (agent may have failed early)
- Worktree creation script failed
- **Agent worked on main instead of worktree** (check for uncommitted changes on main)
"
    elif [[ "${commits_ahead:-0}" == "0" ]]; then
        diagnostics+="- Builder exited without making any commits
- Builder may have determined issue was invalid or already resolved
- Builder may have encountered an error during implementation
- Builder may have timed out before completing work
- **Agent may have worked on main instead of worktree** (check for uncommitted changes on main)
"
    fi

    diagnostics+="
**Recovery suggestions**:
1. Check the issue description for clarity - is it actionable?
2. Review any curator comments for implementation guidance
3. If the issue is valid, remove \`loom:blocked\` and add \`loom:issue\` to retry
4. Consider adding more detail to the issue if it was unclear

</details>"

    echo "$diagnostics"
}

# ─── Phase contract validators ───────────────────────────────────────────────

validate_curator() {
    local labels
    labels=$(gh issue view "$ISSUE" --json labels --jq '.labels[].name' 2>/dev/null) || {
        output_result "failed" "Could not fetch issue labels"
        return 1
    }

    if echo "$labels" | grep -q "loom:curated"; then
        output_result "satisfied" "Issue has loom:curated label"
        return 0
    fi

    # Recovery: apply loom:curated label (curator may have enhanced but not labeled)
    echo -e "${YELLOW}Attempting recovery: applying loom:curated label${NC}"
    if gh issue edit "$ISSUE" --add-label "loom:curated" 2>/dev/null; then
        report_milestone "heartbeat" --action "recovery: applied loom:curated label"
        output_result "recovered" "Applied loom:curated label" "apply_label"
        return 0
    fi

    output_result "failed" "Could not apply loom:curated label"
    return 1
}

validate_builder() {
    # Pre-check: Detect if builder worked on main instead of worktree (workflow violation)
    # This catches the common failure mode where agents forget to create/use worktrees
    if [[ -n "$WORKTREE" ]] && [[ ! -d "$WORKTREE" ]]; then
        local main_changes
        main_changes=$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null) || true
        if [[ -n "$main_changes" ]]; then
            echo -e "${RED}⚠️ WORKFLOW VIOLATION DETECTED${NC}"
            echo -e "${RED}Builder appears to have worked on main instead of in a worktree.${NC}"
            echo -e "${YELLOW}Uncommitted changes on main:${NC}"
            echo "$main_changes" | head -10
            echo ""
            echo -e "${YELLOW}Expected worktree path: $WORKTREE${NC}"
            echo ""
            # Don't fail immediately - continue with normal validation which will gather diagnostics
        fi
    fi

    # First: Check if issue is already closed (builder may have resolved without PR)
    # This is valid for verification tasks, documentation issues, research tasks, etc.
    local issue_state
    issue_state=$(gh issue view "$ISSUE" --json state --jq '.state' 2>/dev/null) || true
    if [[ "$issue_state" == "CLOSED" ]]; then
        output_result "satisfied" "Issue #$ISSUE is closed (resolved without PR)"
        return 0
    fi

    # Check if a PR already exists for this issue
    # Strategy: Try multiple search methods to find the PR
    # Method 1 (branch-based) is deterministic and preferred over search API (has indexing lag)
    local pr=""
    local pr_found_by=""

    # Method 1: Branch-based lookup (deterministic, no indexing lag)
    # Branch name follows convention from worktree.sh: feature/issue-<number>
    pr=$(gh pr list --head "feature/issue-${ISSUE}" --state open --json number --jq '.[0].number' 2>/dev/null) || true
    if [[ -n "$pr" && "$pr" != "null" ]]; then
        pr_found_by="branch_name"
    fi

    # Method 2: Search by "Closes #N" in PR body (fallback if branch name differs)
    if [[ -z "$pr" || "$pr" == "null" ]]; then
        pr=$(gh pr list --search "Closes #${ISSUE}" --state open --json number --jq '.[0].number' 2>/dev/null) || true
        if [[ -n "$pr" && "$pr" != "null" ]]; then
            pr_found_by="closes_keyword"
        fi
    fi

    # Method 3: Search by "Fixes #N" in PR body
    if [[ -z "$pr" || "$pr" == "null" ]]; then
        pr=$(gh pr list --search "Fixes #${ISSUE}" --state open --json number --jq '.[0].number' 2>/dev/null) || true
        if [[ -n "$pr" && "$pr" != "null" ]]; then
            pr_found_by="fixes_keyword"
        fi
    fi

    # Method 4: Search by "Resolves #N" in PR body
    if [[ -z "$pr" || "$pr" == "null" ]]; then
        pr=$(gh pr list --search "Resolves #${ISSUE}" --state open --json number --jq '.[0].number' 2>/dev/null) || true
        if [[ -n "$pr" && "$pr" != "null" ]]; then
            pr_found_by="resolves_keyword"
        fi
    fi

    if [[ -n "$pr" && "$pr" != "null" ]]; then
        # PR found - check if it has proper issue reference (needed for auto-close)
        if [[ "$pr_found_by" == "branch_name" ]]; then
            # PR found by branch name - check if it needs "Closes #N" added
            local pr_body
            pr_body=$(gh pr view "$pr" --json body --jq '.body' 2>/dev/null) || pr_body=""

            if ! echo "$pr_body" | grep -qE "(Closes|Fixes|Resolves)[[:space:]]+#${ISSUE}"; then
                echo -e "${YELLOW}PR #$pr found but missing issue reference - adding 'Closes #${ISSUE}'${NC}"
                # Append "Closes #N" to existing body
                local new_body
                if [[ -z "$pr_body" || "$pr_body" == "null" ]]; then
                    new_body="Closes #${ISSUE}"
                else
                    new_body="${pr_body}

Closes #${ISSUE}"
                fi
                if gh pr edit "$pr" --body "$new_body" 2>/dev/null; then
                    report_milestone "heartbeat" --action "recovery: added 'Closes #${ISSUE}' to PR #$pr body"
                    echo -e "${GREEN}Added 'Closes #${ISSUE}' to PR #$pr body${NC}"
                else
                    echo -e "${YELLOW}Warning: Could not add issue reference to PR body${NC}"
                fi
            fi
        fi

        # Check for loom:review-requested label
        local pr_labels
        pr_labels=$(gh pr view "$pr" --json labels --jq '.labels[].name' 2>/dev/null) || true
        if echo "$pr_labels" | grep -q "loom:review-requested"; then
            output_result "satisfied" "PR #$pr exists with loom:review-requested"
            return 0
        fi
        # PR exists but missing label - add it
        echo -e "${YELLOW}Attempting recovery: adding loom:review-requested to PR #$pr${NC}"
        if gh pr edit "$pr" --add-label "loom:review-requested" 2>/dev/null; then
            report_milestone "heartbeat" --action "recovery: added loom:review-requested to PR #$pr"
            output_result "recovered" "Added loom:review-requested to existing PR #$pr" "add_label"
            return 0
        fi
    fi

    # No PR found (searched by branch name and Closes/Fixes/Resolves keywords)
    # Attempt recovery from worktree
    if [[ -z "$WORKTREE" ]]; then
        output_result "failed" "No PR found (searched by branch 'feature/issue-${ISSUE}' and keywords) and no worktree path provided"
        mark_blocked "Builder did not create a PR. Searched for: branch 'feature/issue-${ISSUE}' and 'Closes/Fixes/Resolves #${ISSUE}' in PR body. No worktree available for recovery."
        return 1
    fi

    if [[ ! -d "$WORKTREE" ]]; then
        output_result "failed" "Worktree path does not exist: $WORKTREE"
        local diagnostics
        diagnostics=$(gather_builder_diagnostics)
        mark_blocked "Builder did not create a PR and worktree path does not exist." "$diagnostics"
        return 1
    fi

    # Check for uncommitted changes in worktree
    local status_output
    status_output=$(git -C "$WORKTREE" status --porcelain 2>/dev/null) || {
        output_result "failed" "Could not check worktree status"
        mark_blocked "Builder did not create a PR and worktree is not a valid git directory."
        return 1
    }

    if [[ -z "$status_output" ]]; then
        # Check if there are unpushed commits
        local unpushed
        unpushed=$(git -C "$WORKTREE" log --oneline '@{upstream}..HEAD' 2>/dev/null) || unpushed=""
        if [[ -z "$unpushed" ]]; then
            # Worktree exists but builder made no commits and has no changes
            # Clean up the stale worktree so the next retry starts fresh
            local stale_branch
            stale_branch=$(git -C "$WORKTREE" rev-parse --abbrev-ref HEAD 2>/dev/null) || stale_branch=""
            echo -e "${YELLOW}Cleaning up stale worktree (no commits, no changes): $WORKTREE${NC}"
            if git worktree remove "$WORKTREE" --force 2>/dev/null; then
                echo -e "${GREEN}✓ Removed stale worktree: $WORKTREE${NC}"
                # Delete the empty branch
                if [[ -n "$stale_branch" && "$stale_branch" != "main" ]]; then
                    if git -C "$REPO_ROOT" branch -d "$stale_branch" 2>/dev/null; then
                        echo -e "${GREEN}✓ Removed empty branch: $stale_branch${NC}"
                    else
                        echo -e "${YELLOW}Could not delete branch $stale_branch (may have upstream references)${NC}"
                    fi
                fi
            else
                echo -e "${YELLOW}Could not remove stale worktree (may need manual cleanup)${NC}"
            fi
            output_result "failed" "No PR found and no changes in worktree to recover. Stale worktree cleaned up for retry."
            local diagnostics
            diagnostics=$(gather_builder_diagnostics)
            mark_blocked "Builder did not create a PR. Worktree had no uncommitted or unpushed changes. Stale worktree has been cleaned up so the next attempt starts fresh." "$diagnostics"
            return 1
        fi
    fi

    # Guard: check if changes are substantive (not just marker files)
    # When agents are rate-limited, the only file in the worktree may be .loom-in-use
    # or other non-code artifacts. Creating a PR from these wastes review cycles.
    if [[ -n "$status_output" ]]; then
        local substantive_changes
        substantive_changes=$(echo "$status_output" | grep -v '\.loom-in-use$' | grep -v '\.loom/' || true)
        if [[ -z "$substantive_changes" ]]; then
            # Only marker files found - clean up the stale worktree
            local stale_branch
            stale_branch=$(git -C "$WORKTREE" rev-parse --abbrev-ref HEAD 2>/dev/null) || stale_branch=""
            echo -e "${YELLOW}Cleaning up stale worktree (only marker files, no substantive changes): $WORKTREE${NC}"
            if git worktree remove "$WORKTREE" --force 2>/dev/null; then
                echo -e "${GREEN}✓ Removed stale worktree: $WORKTREE${NC}"
                if [[ -n "$stale_branch" && "$stale_branch" != "main" ]]; then
                    if git -C "$REPO_ROOT" branch -d "$stale_branch" 2>/dev/null; then
                        echo -e "${GREEN}✓ Removed empty branch: $stale_branch${NC}"
                    fi
                fi
            else
                echo -e "${YELLOW}Could not remove stale worktree (may need manual cleanup)${NC}"
            fi
            output_result "failed" "No substantive changes to recover (only marker files found). Stale worktree cleaned up for retry."
            local diagnostics
            diagnostics=$(gather_builder_diagnostics)
            mark_blocked "Builder did not produce substantive changes. Only marker/infrastructure files were found in the worktree. Stale worktree has been cleaned up so the next attempt starts fresh." "$diagnostics"
            return 1
        fi
    fi

    echo -e "${YELLOW}Attempting recovery: committing and pushing worktree changes${NC}"
    report_milestone "heartbeat" --action "recovery: attempting builder worktree recovery"

    # Commit uncommitted changes if any
    if [[ -n "$status_output" ]]; then
        git -C "$WORKTREE" add -A 2>/dev/null || {
            output_result "failed" "Could not stage changes"
            mark_blocked "Recovery failed: could not stage worktree changes."
            return 1
        }
        git -C "$WORKTREE" commit -m "Auto-commit: builder did not complete workflow for #${ISSUE}" 2>/dev/null || {
            output_result "failed" "Could not commit changes"
            mark_blocked "Recovery failed: could not commit worktree changes."
            return 1
        }
    fi

    # Push if not pushed
    local branch
    branch=$(git -C "$WORKTREE" rev-parse --abbrev-ref HEAD 2>/dev/null) || {
        output_result "failed" "Could not determine branch name"
        mark_blocked "Recovery failed: could not determine worktree branch."
        return 1
    }

    # Check if upstream exists
    if ! git -C "$WORKTREE" rev-parse --abbrev-ref '@{upstream}' &>/dev/null; then
        git -C "$WORKTREE" push -u origin "$branch" 2>/dev/null || {
            output_result "failed" "Could not push branch"
            mark_blocked "Recovery failed: could not push worktree branch."
            return 1
        }
    else
        git -C "$WORKTREE" push 2>/dev/null || {
            output_result "failed" "Could not push changes"
            mark_blocked "Recovery failed: could not push worktree changes."
            return 1
        }
    fi

    # Create PR
    local new_pr
    new_pr=$(gh pr create \
        --head "$branch" \
        --label "loom:review-requested" \
        --title "Issue #${ISSUE}: Auto-recovered PR" \
        --body "$(cat <<EOF
Closes #${ISSUE}

_PR created by shepherd recovery after builder failed to complete workflow._
EOF
)" 2>/dev/null) || {
        output_result "failed" "Could not create PR"
        mark_blocked "Recovery failed: could not create PR from worktree."
        return 1
    }

    report_milestone "heartbeat" --action "recovery: created PR from worktree"
    output_result "recovered" "Created PR from worktree changes: $new_pr" "create_pr"
    return 0
}

validate_judge() {
    if [[ -z "$PR_NUMBER" ]]; then
        output_result "failed" "PR number required for judge phase validation"
        return 1
    fi

    local labels
    labels=$(gh pr view "$PR_NUMBER" --json labels --jq '.labels[].name' 2>/dev/null) || {
        output_result "failed" "Could not fetch PR labels"
        return 1
    }

    if echo "$labels" | grep -q "loom:pr"; then
        output_result "satisfied" "PR #$PR_NUMBER approved (loom:pr)"
        return 0
    fi

    if echo "$labels" | grep -q "loom:changes-requested"; then
        output_result "satisfied" "PR #$PR_NUMBER has changes requested (loom:changes-requested)"
        return 0
    fi

    # No recovery possible for judge - it must make a decision
    output_result "failed" "Judge did not produce loom:pr or loom:changes-requested on PR #$PR_NUMBER"
    mark_blocked "Judge phase did not produce a review decision on PR #$PR_NUMBER."
    return 1
}

validate_doctor() {
    if [[ -z "$PR_NUMBER" ]]; then
        output_result "failed" "PR number required for doctor phase validation"
        return 1
    fi

    local labels
    labels=$(gh pr view "$PR_NUMBER" --json labels --jq '.labels[].name' 2>/dev/null) || {
        output_result "failed" "Could not fetch PR labels"
        return 1
    }

    if echo "$labels" | grep -q "loom:review-requested"; then
        output_result "satisfied" "PR #$PR_NUMBER has loom:review-requested"
        return 0
    fi

    # No recovery possible for doctor
    output_result "failed" "Doctor did not re-request review on PR #$PR_NUMBER"
    mark_blocked "Doctor phase did not apply loom:review-requested to PR #$PR_NUMBER."
    return 1
}

# ─── Main ─────────────────────────────────────────────────────────────────────

case "$PHASE" in
    curator)  validate_curator  ;;
    builder)  validate_builder  ;;
    judge)    validate_judge    ;;
    doctor)   validate_doctor   ;;
esac
