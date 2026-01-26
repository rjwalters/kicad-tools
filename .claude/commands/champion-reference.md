# Champion: Reference Documentation

This file contains edge cases, complete workflow scripts, and troubleshooting information for the Champion role. **Reference this file when handling non-standard situations.**

---

## Edge Cases and Special Scenarios

This section documents how Champion handles non-standard situations during PR auto-merge.

### Edge Case 1: PR with No CI Checks

**Scenario**: Repository has no CI/CD configured, or PR doesn't trigger any checks.

**Handling**:
```bash
# gh pr checks returns "no checks reported"
if echo "$CHECKS" | grep -q "no checks reported"; then
  echo "PASS: No CI checks required"
  # Continue to merge
fi
```

**Decision**: **Allow merge** - absence of CI is not a blocker.

**Rationale**: Many repositories don't use CI, or use branch protection without status checks.

---

### Edge Case 2: PR with Pending CI Checks

**Scenario**: CI checks are queued or in progress when Champion evaluates the PR.

**Handling**:
```bash
# Check for pending/running checks
PENDING=$(echo "$CHECKS" | jq -r '.[] | select(.status == "IN_PROGRESS" or .status == "QUEUED") | .name')
if [ -n "$PENDING" ]; then
  echo "SKIP: CI checks still running - will retry next iteration"
  # Skip this PR, try again later
fi
```

**Decision**: **Skip and defer** - do not merge, check again in next iteration.

**Rationale**: Wait for CI to complete to ensure quality. Champion will naturally retry on next cycle (10 minutes).

---

### Edge Case 3: Force-Push After Judge Approval

**Scenario**: Builder force-pushes new commits after Judge added `loom:pr` label.

**Handling**:
- **Recency check** catches this (PR updated recently)
- **CI check** re-runs after force push
- **Judge approval remains valid** if PR still has `loom:pr` label

**Decision**: **Allow merge if all criteria pass** - recency and CI checks provide sufficient safety.

**Recommended improvement**: Judge should remove `loom:pr` on force-push (not Champion's responsibility).

---

### Edge Case 4: Merge Conflicts Develop After Approval

**Scenario**: PR was mergeable when Judge approved, but another PR merged first causing conflicts.

**Handling**:
```bash
MERGEABLE=$(gh pr view "$PR_NUMBER" --json mergeable --jq -r '.mergeable')
if [ "$MERGEABLE" != "MERGEABLE" ]; then
  echo "FAIL: Merge conflicts detected"
  # Add comment explaining conflict
  gh pr comment "$PR_NUMBER" --body "Cannot auto-merge: merge conflicts with base branch"
fi
```

**Decision**: **Skip and comment** - do not merge, notify via comment.

**Rationale**: Conflicts require human/Builder resolution. Champion should not attempt to resolve conflicts.

**Next steps**: Builder or Doctor should resolve conflicts and re-request Judge review.

---

### Edge Case 5: Stale PR (Updated > 24 Hours Ago)

**Scenario**: PR has `loom:pr` label but hasn't been updated in over 24 hours.

**Handling**:
```bash
HOURS_AGO=$(( (NOW_TS - UPDATED_TS) / 3600 ))
if [ "$HOURS_AGO" -gt 24 ]; then
  echo "FAIL: Stale PR (updated $HOURS_AGO hours ago)"
  # Skip merge, add comment
fi
```

**Decision**: **Skip and comment** - do not merge stale PRs.

**Rationale**: Main branch may have evolved significantly. Stale PRs should be rebased or re-reviewed.

**Recommended action**: Remove `loom:pr` label on stale PRs, request rebase from Builder.

---

### Edge Case 6: PR Modifying Only Test Files

**Scenario**: PR changes only test files (e.g., `*.test.ts`, `*.spec.rs`).

**Handling**: No special handling needed - standard safety criteria apply.

**Decision**: **Allow merge if criteria pass** - test-only changes are safe.

**Rationale**: Size limit (200 lines) and CI checks provide sufficient protection.

---

### Edge Case 7: PR with `loom:manual-merge` Added Mid-Evaluation

**Scenario**: Human adds `loom:manual-merge` label while Champion is evaluating the PR.

**Handling**: Label check (#1) runs first, catches override immediately.

**Decision**: **Skip immediately** - respect human override.

**Rationale**: Champion re-fetches labels at start of each evaluation, race condition window is minimal.

---

### Edge Case 8: PR Linked to Multiple Issues

**Scenario**: PR body contains "Closes #123, Closes #456, Fixes #789".

**Handling**:
```bash
# Extract all linked issues
LINKED_ISSUES=$(gh pr view "$PR_NUMBER" --json body --jq '.body' | grep -Eo "(Closes|Fixes|Resolves) #[0-9]+" | grep -Eo "[0-9]+")

# Verify each issue closed after merge
for issue in $LINKED_ISSUES; do
  STATE=$(gh issue view "$issue" --json state --jq -r '.state')
  if [ "$STATE" != "CLOSED" ]; then
    echo "Warning: Issue #$issue not auto-closed, closing manually"
    gh issue close "$issue" --comment "Closed by PR #$PR_NUMBER (auto-merged by Champion)"
  fi
done
```

**Decision**: **Allow merge, verify all linked issues** - standard practice.

**Rationale**: GitHub auto-closes multiple issues, but verify and manually close if needed.

---

### Edge Case 9: PR with Mixed-State CI Checks

**Scenario**: Some checks pass, some pending, some skipped.

**Handling**:
```bash
# Any non-SUCCESS conclusion fails the check
FAILING=$(echo "$CHECKS" | jq -r '.[] | select(.conclusion != "SUCCESS" and .conclusion != null) | .name')
if [ -n "$FAILING" ]; then
  echo "FAIL: Some checks did not pass"
fi
```

**Decision**: **Fail if any check is not SUCCESS** - conservative approach.

**Rationale**: "Skipped" or "Neutral" conclusions indicate incomplete validation.

---

### Edge Case 10: Critical File Pattern Extensions

**Scenario**: Repository adds new critical files not in pattern list (e.g., `auth.config.ts`).

**Handling**: Champion uses hardcoded patterns - will **not** catch new critical files.

**Decision**: **Requires pattern update** - human must extend `CRITICAL_PATTERNS` array.

**Maintenance**: Review and update critical file patterns periodically as codebase evolves.

**Recommended**: Add repository-specific `.loom/champion-critical-files.txt` for custom patterns (future enhancement).

---

### Edge Case 11: PR Size Exactly at Limit (200 Lines)

**Scenario**: PR has exactly 200 lines changed (e.g., 100 additions + 100 deletions).

**Handling**:
```bash
if [ "$TOTAL" -gt 200 ]; then  # Strictly greater than
  echo "FAIL: Too large"
fi
```

**Decision**: **Allow merge** - limit is inclusive (<= 200 allowed).

**Rationale**: 200-line PRs are still considered "small" for auto-merge purposes.

---

### Edge Case 12: GitHub API Rate Limiting

**Scenario**: Champion makes too many API calls and hits rate limit.

**Handling**: `gh` commands will fail with rate limit error.

**Current behavior**: Error handling workflow catches this, adds comment to PR, continues.

**Recommendation**: Add exponential backoff or skip iteration if rate-limited (future enhancement).

---

### Edge Case 13: PR Approved by Multiple Judges

**Scenario**: Multiple agents or humans add comments/approvals to the same PR.

**Handling**: No special handling - `loom:pr` label is single source of truth.

**Decision**: **Allow merge** - redundant approvals are harmless.

**Rationale**: Label-based coordination prevents duplicate merges.

---

## Summary: Edge Case Decision Matrix

| Edge Case | Decision | Action |
|-----------|----------|--------|
| No CI checks | Allow | Continue to merge |
| Pending CI checks | Skip | Defer to next iteration |
| Force-push after approval | Allow | If criteria still pass |
| Merge conflicts | Fail | Comment and skip |
| Stale PR (>24h) | Fail | Comment and skip |
| Test-only changes | Allow | Standard criteria apply |
| Manual-merge override | Skip | Respect human decision |
| Multiple linked issues | Allow | Verify all closed |
| Mixed-state CI | Fail | Require all SUCCESS |
| Unknown critical file | Miss | Needs pattern update |
| Exactly 200 lines | Allow | Limit is inclusive |
| API rate limit | Error | Comment and continue |
| Multiple approvals | Allow | Label is source of truth |

---

## Complete Auto-Merge Workflow Script

This section provides the full end-to-end implementation integrating all steps.

```bash
#!/bin/bash
# Complete Champion PR auto-merge workflow
# Usage: champion_automerge <pr-number>

PR_NUMBER=$1

if [ -z "$PR_NUMBER" ]; then
  echo "Usage: $0 <pr-number>"
  exit 1
fi

echo "========================================="
echo "Champion Auto-Merge Workflow: PR #$PR_NUMBER"
echo "========================================="
echo ""

# ============================================
# STEP 1: Verify Safety Criteria
# ============================================

echo "STEP 1/5: Verifying safety criteria..."
echo ""

# Criterion 1: Label Check
LABELS=$(gh pr view "$PR_NUMBER" --json labels --jq '.labels[].name' | tr '\n' ' ')
if ! echo "$LABELS" | grep -q "loom:pr"; then
  echo "FAIL: Missing loom:pr label"
  exit 1
fi
if echo "$LABELS" | grep -q "loom:manual-merge"; then
  echo "SKIP: Has loom:manual-merge label (human override)"
  exit 1
fi
echo "PASS: Label check"

# Criterion 2: Size Check
FORCE_MODE=$(cat .loom/daemon-state.json 2>/dev/null | jq -r '.force_mode // false')
PR_DATA=$(gh pr view "$PR_NUMBER" --json additions,deletions)
ADDITIONS=$(echo "$PR_DATA" | jq -r '.additions')
DELETIONS=$(echo "$PR_DATA" | jq -r '.deletions')
TOTAL=$((ADDITIONS + DELETIONS))
if [ "$FORCE_MODE" != "true" ] && [ "$TOTAL" -gt 200 ]; then
  echo "FAIL: Too large ($TOTAL lines, limit is 200)"
  exit 1
fi
echo "PASS: Size check ($TOTAL lines)"

# Criterion 3: Critical File Exclusion
if [ "$FORCE_MODE" != "true" ]; then
  FILES=$(gh pr view "$PR_NUMBER" --json files --jq -r '.files[].path')
  CRITICAL_PATTERNS=(
    "src-tauri/tauri.conf.json"
    "Cargo.toml"
    "loom-daemon/Cargo.toml"
    "src-tauri/Cargo.toml"
    "package.json"
    "pnpm-lock.yaml"
    ".github/workflows/"
    ".sql"
    "migration"
  )
  for file in $FILES; do
    for pattern in "${CRITICAL_PATTERNS[@]}"; do
      if [[ "$file" == *"$pattern"* ]]; then
        echo "FAIL: Critical file modified: $file"
        exit 1
      fi
    done
  done
fi
echo "PASS: No critical files modified"

# Criterion 4: Merge Conflict Check
MERGEABLE=$(gh pr view "$PR_NUMBER" --json mergeable --jq -r '.mergeable')
if [ "$MERGEABLE" != "MERGEABLE" ]; then
  echo "FAIL: Not mergeable (state: $MERGEABLE)"
  exit 1
fi
echo "PASS: No merge conflicts"

# Criterion 5: Recency Check
UPDATED_AT=$(gh pr view "$PR_NUMBER" --json updatedAt --jq -r '.updatedAt')
UPDATED_TS=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$UPDATED_AT" +%s 2>/dev/null || \
             date -d "$UPDATED_AT" +%s 2>/dev/null)
NOW_TS=$(date +%s)
HOURS_AGO=$(( (NOW_TS - UPDATED_TS) / 3600 ))
if [ "$FORCE_MODE" = "true" ]; then
  RECENCY_LIMIT=72
else
  RECENCY_LIMIT=24
fi
if [ "$HOURS_AGO" -gt "$RECENCY_LIMIT" ]; then
  echo "FAIL: Stale PR (updated $HOURS_AGO hours ago)"
  exit 1
fi
echo "PASS: Recently updated ($HOURS_AGO hours ago)"

# Criterion 6: CI Status Check
CHECKS=$(gh pr checks "$PR_NUMBER" --json name,conclusion,status 2>&1)
if ! echo "$CHECKS" | grep -q "no checks reported"; then
  FAILING=$(echo "$CHECKS" | jq -r '.[] | select(.conclusion != "SUCCESS" and .conclusion != null) | .name')
  PENDING=$(echo "$CHECKS" | jq -r '.[] | select(.status == "IN_PROGRESS" or .status == "QUEUED") | .name')
  if [ -n "$FAILING" ]; then
    echo "FAIL: CI checks failing:"
    echo "$FAILING"
    exit 1
  fi
  if [ -n "$PENDING" ]; then
    echo "SKIP: CI checks still running:"
    echo "$PENDING"
    exit 1
  fi
fi
echo "PASS: All CI checks passing"

echo ""
echo "All safety criteria passed"
echo ""

# ============================================
# STEP 2: Post Pre-Merge Comment
# ============================================

echo "STEP 2/5: Posting pre-merge comment..."
echo ""

# Determine CI status text
if echo "$CHECKS" | grep -q "no checks reported"; then
  CI_STATUS="No CI checks required"
else
  CI_STATUS="All CI checks passing"
fi

# Post comment
gh pr comment "$PR_NUMBER" --body "$(cat <<EOF
**Champion Auto-Merge**

This PR meets all safety criteria for automatic merging:

- Judge approved (\`loom:pr\` label)
- Small change ($TOTAL lines: +$ADDITIONS/-$DELETIONS)
- No critical files modified
- No merge conflicts
- Updated recently ($HOURS_AGO hours ago)
- $CI_STATUS
- No manual-merge override

**Proceeding with squash merge...** If this was merged in error, you can revert with:
\`git revert <commit-sha>\`

---
*Automated by Champion role*
EOF
)"

echo "Posted pre-merge comment"
echo ""

# ============================================
# STEP 3: Execute Merge
# ============================================

echo "STEP 3/5: Executing squash merge..."
echo ""

MERGE_OUTPUT=$(gh pr merge "$PR_NUMBER" --squash --auto --delete-branch 2>&1)
MERGE_EXIT_CODE=$?

# Verify actual merge state via GitHub API
PR_STATE=$(gh pr view "$PR_NUMBER" --json state --jq '.state')

if [ "$PR_STATE" = "MERGED" ]; then
  echo "Successfully merged PR #$PR_NUMBER"
  echo ""
else
  echo "Merge failed!"
  echo "Error output: $MERGE_OUTPUT"
  exit 1
fi

# ============================================
# STEP 4: Verify Issue Closure
# ============================================

echo "STEP 4/5: Verifying linked issue closure..."
echo ""

PR_BODY=$(gh pr view "$PR_NUMBER" --json body --jq -r '.body')
LINKED_ISSUES=$(echo "$PR_BODY" | grep -Eo "(Closes|Fixes|Resolves) #[0-9]+" | grep -Eo "[0-9]+" | sort -u)

if [ -z "$LINKED_ISSUES" ]; then
  echo "No linked issues found - skipping closure verification"
else
  echo "Found linked issues: $LINKED_ISSUES"
  for issue in $LINKED_ISSUES; do
    echo "Checking issue #$issue..."
    ISSUE_STATE=$(gh issue view "$issue" --json state --jq -r '.state' 2>&1)
    if [ "$ISSUE_STATE" = "CLOSED" ]; then
      echo "Issue #$issue is closed"
    else
      echo "Issue #$issue still open - closing manually..."
      gh issue close "$issue" --comment "Closed by PR #$PR_NUMBER (auto-merged by Champion)"
      echo "Manually closed issue #$issue"
    fi
  done
fi

echo ""

# ============================================
# STEP 5: Unblock Dependent Issues
# ============================================

echo "STEP 5/5: Checking for dependent issues to unblock..."
echo ""

for closed_issue in $LINKED_ISSUES; do
  echo "Checking for issues blocked by #$closed_issue..."
  BLOCKED_ISSUES=$(gh issue list --label "loom:blocked" --state open --json number,body --jq ".[] | select(.body | test(\"(Blocked by|Depends on|Requires) #$closed_issue\"; \"i\")) | .number")

  if [ -z "$BLOCKED_ISSUES" ]; then
    echo "  No issues found blocked by #$closed_issue"
    continue
  fi

  for blocked in $BLOCKED_ISSUES; do
    echo "  Checking if #$blocked can be unblocked..."
    BLOCKED_BODY=$(gh issue view "$blocked" --json body --jq -r '.body')
    ALL_DEPS=$(echo "$BLOCKED_BODY" | grep -Eo "(Blocked by|Depends on|Requires) #[0-9]+" | grep -Eo "[0-9]+" | sort -u)

    ALL_RESOLVED=true
    for dep in $ALL_DEPS; do
      DEP_STATE=$(gh issue view "$dep" --json state --jq -r '.state' 2>/dev/null)
      if [ "$DEP_STATE" != "CLOSED" ]; then
        echo "    Still blocked: dependency #$dep is still open"
        ALL_RESOLVED=false
        break
      fi
    done

    if [ "$ALL_RESOLVED" = true ]; then
      echo "    All dependencies resolved - unblocking #$blocked"
      gh issue edit "$blocked" --remove-label "loom:blocked" --add-label "loom:issue"
      gh issue comment "$blocked" --body "**Unblocked** by merge of PR #$PR_NUMBER (resolved #$closed_issue)

All dependencies are now resolved. This issue is ready for implementation.

---
*Automated by Champion role*"
      echo "    Unblocked issue #$blocked"
    fi
  done
done

echo ""
echo "========================================="
echo "Champion auto-merge complete!"
echo "========================================="
echo ""
echo "Summary:"
echo "- PR #$PR_NUMBER: Merged successfully"
echo "- Lines changed: $TOTAL (+$ADDITIONS/-$DELETIONS)"
echo "- Linked issues: ${LINKED_ISSUES:-none}"
echo ""
exit 0
```

**Usage**:

```bash
# Auto-merge a single PR
./champion_automerge.sh 123

# Use in Champion iteration loop
for pr in $(gh pr list --label="loom:pr" --json number --jq '.[].number' | head -3); do
  ./champion_automerge.sh "$pr" || echo "Failed to merge PR #$pr, continuing..."
done
```

---

## Troubleshooting

### Common Issues

**PR not merging despite passing all checks**
- Check if branch protection requires additional approvals
- Verify GitHub API rate limits haven't been hit
- Check for webhook delays in GitHub's processing

**Issue not auto-closing after merge**
- Verify PR body uses correct format: "Closes #123" (not "closes issue #123")
- Check if issue is in the same repository
- Manual close may be needed for cross-repo references

**Blocked issues not unblocking**
- Verify dependency format: "Blocked by #123" or "Depends on #123"
- Check if all dependencies are truly closed
- Manual unblock may be needed for complex dependency patterns

**Worktree checkout errors**
- These are expected when running from a worktree
- Champion verifies merge via API, not exit code
- No action needed - merge still succeeds

### Debugging Commands

```bash
# Check PR merge status
gh pr view <number> --json state,mergeable,statusCheckRollup

# View linked issues
gh pr view <number> --json body --jq '.body' | grep -Eo "(Closes|Fixes|Resolves) #[0-9]+"

# Check daemon state
cat .loom/daemon-state.json | jq '.force_mode'

# List blocked issues
gh issue list --label "loom:blocked" --state open

# Check API rate limit
gh api rate_limit
```
