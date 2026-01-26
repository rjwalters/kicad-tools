# Champion: PR Auto-Merge Context

This file contains PR auto-merge instructions for the Champion role. **Read this file when Priority 1 work is found (PRs with loom:pr label).**

---

## Overview

Auto-merge Judge-approved PRs that are safe, routine, and low-risk.

The Champion acts as the final step in the PR pipeline, merging PRs that have passed Judge review and meet all safety criteria.

---

## Safety Criteria

For each `loom:pr` PR, verify ALL 7 safety criteria. If ANY criterion fails, do NOT merge.

### 1. Label Check
- [ ] PR has `loom:pr` label (Judge approval)
- [ ] PR does NOT have `loom:manual-merge` label (human override)

**Verification command**:
```bash
# Get all labels for the PR
LABELS=$(gh pr view <number> --json labels --jq '.labels[].name' | tr '\n' ' ')

# Check for loom:pr label
if ! echo "$LABELS" | grep -q "loom:pr"; then
  echo "FAIL: Missing loom:pr label"
  exit 1
fi

# Check for manual-merge override
if echo "$LABELS" | grep -q "loom:manual-merge"; then
  echo "SKIP: Has loom:manual-merge label (human override)"
  exit 1
fi

echo "PASS: Label check"
```

**Rationale**: Only merge PRs explicitly approved by Judge, respect human override

### 2. Size Check
- [ ] Total lines changed <= 200 (additions + deletions)
- [ ] **Force mode**: Size limit is waived

**Verification command**:
```bash
# Get additions and deletions
PR_DATA=$(gh pr view <number> --json additions,deletions --jq '{additions, deletions, total: (.additions + .deletions)}')
ADDITIONS=$(echo "$PR_DATA" | jq -r '.additions')
DELETIONS=$(echo "$PR_DATA" | jq -r '.deletions')
TOTAL=$((ADDITIONS + DELETIONS))

# Check force mode
FORCE_MODE=$(cat .loom/daemon-state.json 2>/dev/null | jq -r '.force_mode // false')

if [ "$FORCE_MODE" = "true" ]; then
  echo "PASS: Size check waived in force mode ($TOTAL lines)"
else
  # Check size limit (normal mode)
  if [ "$TOTAL" -gt 200 ]; then
    echo "FAIL: Too large ($TOTAL lines, limit is 200)"
    exit 1
  fi
  echo "PASS: Size check ($TOTAL lines)"
fi
```

**Rationale**: Small PRs are easier to revert if problems arise. In force mode, trust Judge review for larger changes.

### 3. Critical File Exclusion Check
- [ ] No changes to critical configuration or infrastructure files
- [ ] **Force mode**: Critical file check is waived (trust Judge review)

**Critical file patterns** (do NOT auto-merge if PR modifies any of these - normal mode only):
- `src-tauri/tauri.conf.json` - app configuration
- `Cargo.toml` - root dependency changes
- `loom-daemon/Cargo.toml` - daemon dependency changes
- `src-tauri/Cargo.toml` - tauri dependency changes
- `package.json` - npm dependency changes
- `pnpm-lock.yaml` - lock file changes
- `.github/workflows/*` - CI/CD pipeline changes
- `*.sql` - database schema changes
- `*migration*` - database migration files

**Verification command**:
```bash
# Check force mode first
FORCE_MODE=$(cat .loom/daemon-state.json 2>/dev/null | jq -r '.force_mode // false')

if [ "$FORCE_MODE" = "true" ]; then
  echo "PASS: Critical file check waived in force mode"
else
  # Get all changed files (normal mode)
  FILES=$(gh pr view <number> --json files --jq -r '.files[].path')

  # Define critical patterns (extend as needed)
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

  # Check each file against patterns
  for file in $FILES; do
    for pattern in "${CRITICAL_PATTERNS[@]}"; do
      if [[ "$file" == *"$pattern"* ]]; then
        echo "FAIL: Critical file modified: $file"
        exit 1
      fi
    done
  done

  echo "PASS: No critical files modified"
fi
```

**Rationale**: Changes to these files require careful human review due to high impact. In force mode, trust Judge review for critical file changes.

### 4. Merge Conflict Check
- [ ] PR is mergeable (no conflicts with base branch)

**Verification command**:
```bash
# Check merge status
MERGEABLE=$(gh pr view <number> --json mergeable --jq -r '.mergeable')

# Verify mergeable state
if [ "$MERGEABLE" != "MERGEABLE" ]; then
  echo "FAIL: Not mergeable (state: $MERGEABLE)"
  exit 1
fi

echo "PASS: No merge conflicts"
```

**Expected states**:
- `MERGEABLE` - Safe to merge (PASS)
- `CONFLICTING` - Has merge conflicts (FAIL)
- `UNKNOWN` - GitHub still calculating, try again later (FAIL)

**Rationale**: Conflicting PRs require human resolution before merging

### 5. Recency Check
- [ ] PR updated within last 24 hours (normal mode)
- [ ] **Force mode**: Extended to 72 hours

**Verification command**:
```bash
# Get PR last update time
UPDATED_AT=$(gh pr view <number> --json updatedAt --jq -r '.updatedAt')

# Convert to Unix timestamp
UPDATED_TS=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$UPDATED_AT" +%s 2>/dev/null || \
             date -d "$UPDATED_AT" +%s 2>/dev/null)

# Get current time
NOW_TS=$(date +%s)

# Calculate hours since update
HOURS_AGO=$(( (NOW_TS - UPDATED_TS) / 3600 ))

# Check force mode for extended window
FORCE_MODE=$(cat .loom/daemon-state.json 2>/dev/null | jq -r '.force_mode // false')
if [ "$FORCE_MODE" = "true" ]; then
  RECENCY_LIMIT=72
else
  RECENCY_LIMIT=24
fi

# Check if within recency limit
if [ "$HOURS_AGO" -gt "$RECENCY_LIMIT" ]; then
  echo "FAIL: Stale PR (updated $HOURS_AGO hours ago, limit is ${RECENCY_LIMIT}h)"
  exit 1
fi

echo "PASS: Recently updated ($HOURS_AGO hours ago)"
```

**Rationale**: Ensures PR reflects recent state of main branch and hasn't gone stale. In force mode, allows older PRs to merge since aggressive development may queue up PRs faster than they can be merged.

### 6. CI Status Check
- [ ] If CI checks exist, all checks must be passing
- [ ] If no CI checks exist, this criterion passes automatically

**Verification command**:
```bash
# Get all CI checks
CHECKS=$(gh pr checks <number> --json name,conclusion,status 2>&1)

# Handle case where no checks exist
if echo "$CHECKS" | grep -q "no checks reported"; then
  echo "PASS: No CI checks required"
  exit 0
fi

# Parse checks
FAILING_CHECKS=$(echo "$CHECKS" | jq -r '.[] | select(.conclusion != "SUCCESS" and .conclusion != null) | .name')
PENDING_CHECKS=$(echo "$CHECKS" | jq -r '.[] | select(.status == "IN_PROGRESS" or .status == "QUEUED") | .name')

# Check for failing checks
if [ -n "$FAILING_CHECKS" ]; then
  echo "FAIL: CI checks failing:"
  echo "$FAILING_CHECKS"
  exit 1
fi

# Check for pending checks
if [ -n "$PENDING_CHECKS" ]; then
  echo "SKIP: CI checks still running:"
  echo "$PENDING_CHECKS"
  exit 1
fi

echo "PASS: All CI checks passing"
```

**Edge cases handled**:
- **No CI checks**: Passes (allows merge)
- **Pending checks**: Skips (waits for completion)
- **Failed checks**: Fails (blocks merge)
- **Mixed state**: Fails if any check is not SUCCESS

**Rationale**: Only merge when all automated checks pass or no checks are configured

### 7. Human Override Check
- [ ] PR does NOT have `loom:manual-merge` label

**Verification command**:
```bash
# This check is already covered in criterion #1 (Label Check)
# Included here for completeness - see Label Check for implementation

# Quick standalone check if needed:
if gh pr view <number> --json labels --jq -e '.labels[] | select(.name == "loom:manual-merge")' > /dev/null 2>&1; then
  echo "SKIP: Has loom:manual-merge label (human override)"
  exit 1
fi

echo "PASS: No manual-merge override"
```

**Rationale**: Allows humans to prevent auto-merge by adding this label.

---

## Auto-Merge Workflow

### Step 1: Verify Safety Criteria

For each candidate PR, check ALL 7 criteria in order. If any criterion fails, skip to rejection workflow.

### Step 2: Add Pre-Merge Comment

Before merging, add a comment documenting why the PR is safe to auto-merge.

```bash
PR_NUMBER=$1

# Gather verification data
PR_DATA=$(gh pr view "$PR_NUMBER" --json additions,deletions,updatedAt)
ADDITIONS=$(echo "$PR_DATA" | jq -r '.additions')
DELETIONS=$(echo "$PR_DATA" | jq -r '.deletions')
TOTAL_LINES=$((ADDITIONS + DELETIONS))

UPDATED_AT=$(echo "$PR_DATA" | jq -r '.updatedAt')
UPDATED_TS=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$UPDATED_AT" +%s 2>/dev/null || \
             date -d "$UPDATED_AT" +%s 2>/dev/null)
NOW_TS=$(date +%s)
HOURS_AGO=$(( (NOW_TS - UPDATED_TS) / 3600 ))

# Check CI status
CHECKS=$(gh pr checks "$PR_NUMBER" --json name,conclusion,status 2>&1)
if echo "$CHECKS" | grep -q "no checks reported"; then
  CI_STATUS="No CI checks required"
else
  CI_STATUS="All CI checks passing"
fi

# Generate comment with actual data
gh pr comment "$PR_NUMBER" --body "$(cat <<EOF
**Champion Auto-Merge**

This PR meets all safety criteria for automatic merging:

- Judge approved (\`loom:pr\` label)
- Small change ($TOTAL_LINES lines: +$ADDITIONS/-$DELETIONS)
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
```

### Step 3: Merge the PR

Execute the squash merge with comprehensive error handling.

```bash
PR_NUMBER=$1

echo "Attempting to merge PR #$PR_NUMBER..."

# Execute merge with output capture
MERGE_OUTPUT=$(gh pr merge "$PR_NUMBER" --squash --auto --delete-branch 2>&1)
MERGE_EXIT_CODE=$?

# IMPORTANT: Worktree Checkout Error Handling
# When running from a worktree, `gh pr merge` may succeed on GitHub but fail
# locally with: "fatal: 'main' is already used by worktree at '/path/to/repo'"
# Always verify PR state via GitHub API rather than relying on exit code.

PR_STATE=$(gh pr view "$PR_NUMBER" --json state --jq '.state')

if [ "$PR_STATE" = "MERGED" ]; then
  # Merge succeeded - any error was just the local checkout failure (expected in worktrees)
  if [ $MERGE_EXIT_CODE -ne 0 ]; then
    if echo "$MERGE_OUTPUT" | grep -q "already used by worktree"; then
      echo "Successfully merged PR #$PR_NUMBER (local checkout skipped - worktree conflict is expected)"
    else
      echo "Successfully merged PR #$PR_NUMBER (non-fatal local error ignored)"
    fi
  else
    echo "Successfully merged PR #$PR_NUMBER"
  fi
else
  # Merge actually failed on GitHub - this is a real error
  echo "Merge failed for PR #$PR_NUMBER"
  echo "Error: $MERGE_OUTPUT"
  # Post failure comment (see Error Handling section)
fi
```

**Merge strategy**:
- **`--squash`**: Combines all commits into single commit (clean history)
- **`--auto`**: Enables GitHub's auto-merge if branch protection requires wait
- **`--delete-branch`**: Automatically removes feature branch after merge

### Step 4: Verify Issue Auto-Close

After successful merge, verify that linked issues were automatically closed by GitHub.

```bash
PR_NUMBER=$1

# Extract linked issues from PR body
PR_BODY=$(gh pr view "$PR_NUMBER" --json body --jq -r '.body')
LINKED_ISSUES=$(echo "$PR_BODY" | grep -Eo "(Closes|Fixes|Resolves) #[0-9]+" | grep -Eo "[0-9]+" | sort -u)

if [ -z "$LINKED_ISSUES" ]; then
  echo "No linked issues found in PR body"
  exit 0
fi

# Check each linked issue
for issue in $LINKED_ISSUES; do
  ISSUE_STATE=$(gh issue view "$issue" --json state --jq -r '.state' 2>&1)

  if [ "$ISSUE_STATE" = "CLOSED" ]; then
    echo "Issue #$issue is closed (auto-closed by PR merge)"
  else
    echo "Issue #$issue is still $ISSUE_STATE - closing manually..."
    gh issue close "$issue" --comment "Closed by PR #$PR_NUMBER which was auto-merged by Champion."
  fi
done
```

### Step 5: Unblock Dependent Issues

After verifying issue closure, check for blocked issues that can now be unblocked.

```bash
PR_NUMBER=$1
CLOSED_ISSUE=$2

echo "Checking for issues blocked by #$CLOSED_ISSUE..."

# Find issues with loom:blocked that reference the closed issue
BLOCKED_ISSUES=$(gh issue list --label "loom:blocked" --state open --json number,body \
  --jq ".[] | select(.body | test(\"(Blocked by|Depends on|Requires) #$CLOSED_ISSUE\"; \"i\")) | .number")

if [ -z "$BLOCKED_ISSUES" ]; then
  echo "No issues found blocked by #$CLOSED_ISSUE"
  exit 0
fi

for blocked in $BLOCKED_ISSUES; do
  echo "Checking if #$blocked can be unblocked..."

  # Get the issue body to check ALL dependencies
  BLOCKED_BODY=$(gh issue view "$blocked" --json body --jq -r '.body')

  # Extract all referenced dependencies
  ALL_DEPS=$(echo "$BLOCKED_BODY" | grep -Eo "(Blocked by|Depends on|Requires) #[0-9]+" | grep -Eo "[0-9]+" | sort -u)

  # Check if ALL dependencies are now closed
  ALL_RESOLVED=true
  for dep in $ALL_DEPS; do
    DEP_STATE=$(gh issue view "$dep" --json state --jq -r '.state' 2>/dev/null)
    if [ "$DEP_STATE" != "CLOSED" ]; then
      echo "  Still blocked: dependency #$dep is still open"
      ALL_RESOLVED=false
      break
    fi
  done

  if [ "$ALL_RESOLVED" = true ]; then
    echo "  All dependencies resolved - unblocking #$blocked"
    gh issue edit "$blocked" --remove-label "loom:blocked" --add-label "loom:issue"
    gh issue comment "$blocked" --body "**Unblocked** by merge of PR #$PR_NUMBER (resolved #$CLOSED_ISSUE)

All dependencies are now resolved. This issue is ready for implementation.

---
*Automated by Champion role*"
  fi
done
```

---

## PR Rejection Workflow

If ANY safety criterion fails, do NOT merge. Instead, add a comment explaining why:

```bash
gh pr comment <number> --body "**Champion: Cannot Auto-Merge**

This PR cannot be automatically merged due to the following:

- <CRITERION_NAME>: <SPECIFIC_REASON>

**Next steps:**
- <SPECIFIC_ACTION_1>
- <SPECIFIC_ACTION_2>

Keeping \`loom:pr\` label. A human will need to manually merge this PR or address the blocking criteria.

---
*Automated by Champion role*"
```

**Do NOT remove the `loom:pr` label** - let the human decide whether to merge or close.

---

## PR Auto-Merge Rate Limiting

**Merge at most 3 PRs per iteration.**

If more than 3 PRs qualify for auto-merge, select the 3 oldest (by creation date) and defer others to next iteration. This prevents overwhelming the main branch with simultaneous merges.

---

## Error Handling

If `gh pr merge` fails for any reason:

1. **Capture error message**
2. **Add comment to PR** with error details
3. **Do NOT remove `loom:pr` label**
4. **Report error in completion summary**
5. **Continue to next PR** (don't abort entire iteration)

Example error comment:

```bash
gh pr comment <number> --body "**Champion: Merge Failed**

Attempted to auto-merge this PR but encountered an error:

\`\`\`
<ERROR_MESSAGE>
\`\`\`

This PR met all safety criteria but the merge operation failed. A human will need to investigate and merge manually.

---
*Automated by Champion role*"
```

---

## Force Mode PR Merging

**In force mode, Champion relaxes PR auto-merge criteria** for aggressive autonomous development:

| Criterion | Normal Mode | Force Mode |
|-----------|-------------|------------|
| Size limit | <= 200 lines | **No limit** (trust Judge review) |
| Critical files | Block `Cargo.toml`, `package.json`, etc. | **Allow all** (trust Judge review) |
| Recency | Updated within 24h | Updated within **72h** |
| CI status | All checks must pass | All checks must pass (unchanged) |
| Merge conflicts | Block if conflicting | Block if conflicting (unchanged) |
| Manual override | Respect `loom:manual-merge` | Respect `loom:manual-merge` (unchanged) |

**Rationale**: In force mode, the Judge has already reviewed the PR. Champion's role is to merge quickly, not to second-guess the review. Essential safety checks (CI, conflicts, manual override) remain.

**Force mode PR merge comment**:
```bash
gh pr comment "$PR_NUMBER" --body "$(cat <<EOF
**[force-mode] Champion Auto-Merge**

This PR has been auto-merged in force mode. Relaxed criteria:
- Size limit: waived (was $TOTAL_LINES lines)
- Critical files: waived
- Trust: Judge review + passing CI

**Merged via squash.** If this was merged in error:
\`git revert <commit-sha>\`

---
*Automated by Champion role (force mode)*
EOF
)"
```

---

## Return to Main Champion File

After completing PR merge work, return to the main champion.md file for completion reporting.
