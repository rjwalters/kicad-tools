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
- [ ] Total lines changed <= configured limit (additions + deletions)
- [ ] **Default limit**: 200 lines (configurable via `.loom/config.json` `champion.auto_merge_max_lines`)
- [ ] **`loom:auto-merge-ok` label**: Size limit is waived (applied by Judge or human to signal large PR is safe)
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
  # Check for loom:auto-merge-ok label override
  HAS_AUTO_MERGE_OK=$(gh pr view <number> --json labels --jq '[.labels[].name] | any(. == "loom:auto-merge-ok")')

  if [ "$HAS_AUTO_MERGE_OK" = "true" ]; then
    echo "PASS: Size check waived by loom:auto-merge-ok label ($TOTAL lines)"
  else
    # Read configurable size limit from .loom/config.json (default: 200)
    SIZE_LIMIT=$(jq -r '.champion.auto_merge_max_lines // 200' .loom/config.json 2>/dev/null || echo 200)

    if [ "$TOTAL" -gt "$SIZE_LIMIT" ]; then
      echo "FAIL: Too large ($TOTAL lines, limit is $SIZE_LIMIT)"
      exit 1
    fi
    echo "PASS: Size check ($TOTAL lines, limit is $SIZE_LIMIT)"
  fi
fi
```

**Rationale**: Small PRs are easier to revert if problems arise. The size limit is configurable via `.loom/config.json` to allow teams to tune the risk/autonomy tradeoff. The `loom:auto-merge-ok` label provides a per-PR escape hatch for large but safe PRs. In force mode, trust Judge review for all changes.

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
- Size check passed ($TOTAL_LINES lines: +$ADDITIONS/-$DELETIONS)
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

# Use merge-pr.sh for worktree-safe merge via GitHub API
# --auto enables auto-merge if ruleset requires wait
./.loom/scripts/merge-pr.sh "$PR_NUMBER" --auto || {
  echo "Merge failed for PR #$PR_NUMBER"
  # Post failure comment (see Error Handling section)
}
```

**Merge strategy**:
- Uses `merge-pr.sh` which merges via GitHub API (worktree-safe)
- **Squash merge**: Combines all commits into single commit (clean history)
- **`--auto`**: Enables GitHub's auto-merge if ruleset requires wait
- Branch deleted automatically after merge

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

### Step 5.5: Create Follow-on Issues

After unblocking dependent issues, scan the merged PR for follow-on work indicators and create consolidated issues.

```bash
PR_NUMBER=$1
ORIGINAL_ISSUE=$2  # The issue this PR closed (may be empty)

echo "Scanning PR #$PR_NUMBER for follow-on work indicators..."

# Check force mode for label selection
FORCE_MODE=$(cat .loom/daemon-state.json 2>/dev/null | jq -r '.force_mode // false')

# ============================================
# Stage 1: Extract TODO/FIXME from Diff
# ============================================

# Get PR diff and extract added lines with TODO patterns
# Parse unified diff to get file:line attribution
TODOS_RAW=$(gh pr diff "$PR_NUMBER" 2>/dev/null | awk '
  /^diff --git/ {
    # Extract filename from diff header
    split($0, a, " b/")
    current_file = a[2]
  }
  /^@@/ {
    # Parse hunk header for line number: @@ -old,count +new,count @@
    match($0, /\+([0-9]+)/, arr)
    line_num = arr[1]
    in_hunk = 1
  }
  in_hunk && /^\+[^+]/ {
    # Added line (not the +++ header)
    if (/\b(TODO|FIXME|HACK|XXX|FUTURE):/) {
      # Extract the comment text after the pattern
      line = $0
      sub(/^\+/, "", line)
      gsub(/^[ \t]*/, "", line)
      # Truncate to 200 chars
      if (length(line) > 200) line = substr(line, 1, 197) "..."
      print current_file ":" line_num ":" line
    }
    line_num++
  }
  in_hunk && !/^[+ -@]/ { in_hunk = 0 }
' | head -20)

# Categorize TODOs by severity
CRITICAL_TODOS=""
STANDARD_TODOS=""
CRITICAL_COUNT=0
STANDARD_COUNT=0

while IFS= read -r todo_line; do
  [ -z "$todo_line" ] && continue
  if echo "$todo_line" | grep -qE '\b(FIXME|HACK|XXX):'; then
    CRITICAL_TODOS="${CRITICAL_TODOS}${todo_line}"$'\n'
    CRITICAL_COUNT=$((CRITICAL_COUNT + 1))
  else
    STANDARD_TODOS="${STANDARD_TODOS}${todo_line}"$'\n'
    STANDARD_COUNT=$((STANDARD_COUNT + 1))
  fi
done <<< "$TODOS_RAW"

TOTAL_TODOS=$((CRITICAL_COUNT + STANDARD_COUNT))
echo "Found $TOTAL_TODOS TODOs ($CRITICAL_COUNT critical, $STANDARD_COUNT standard)"

# ============================================
# Stage 2: Parse PR Body Sections
# ============================================

PR_BODY=$(gh pr view "$PR_NUMBER" --json body --jq -r '.body // ""')

# Extract follow-on sections (case-insensitive matching)
FOLLOWON_SECTION=""
for section_name in "Follow-on Work" "Follow-on" "Out of Scope" "Future Work" "Deferred" "Phase 2" "Phase II"; do
  # Match section header and capture content until next ## or end
  extracted=$(echo "$PR_BODY" | sed -n "/^## *${section_name}/I,/^## /p" | sed '1d;$d' | head -20)
  if [ -n "$extracted" ]; then
    FOLLOWON_SECTION="${FOLLOWON_SECTION}### ${section_name}"$'\n'"${extracted}"$'\n\n'
  fi
done

HAS_FOLLOWON_SECTION=false
[ -n "$FOLLOWON_SECTION" ] && HAS_FOLLOWON_SECTION=true
echo "Has explicit follow-on section: $HAS_FOLLOWON_SECTION"

# ============================================
# Stage 3: Parse Review Comments
# ============================================

# Get review comments containing deferred work indicators
REVIEW_NOTES=$(gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER/comments" --jq '
  .[] |
  select(.body | test("not blocking|consider for future|technical debt|would be nice|future enhancement|could be improved"; "i")) |
  "- \(.body | split("\n")[0] | .[0:200])"
' 2>/dev/null | head -10)

HAS_REVIEW_NOTES=false
[ -n "$REVIEW_NOTES" ] && HAS_REVIEW_NOTES=true
echo "Has deferred review notes: $HAS_REVIEW_NOTES"

# ============================================
# Stage 4: Apply Threshold Logic
# ============================================

SHOULD_CREATE_ISSUE=false

# Always create if:
# - 1+ critical patterns (FIXME, HACK, XXX)
# - Explicit follow-on section in PR
# - 3+ TODOs total

if [ "$CRITICAL_COUNT" -gt 0 ]; then
  SHOULD_CREATE_ISSUE=true
  echo "Creating issue: found $CRITICAL_COUNT critical TODOs"
elif [ "$HAS_FOLLOWON_SECTION" = true ]; then
  SHOULD_CREATE_ISSUE=true
  echo "Creating issue: found explicit follow-on section"
elif [ "$TOTAL_TODOS" -ge 3 ]; then
  SHOULD_CREATE_ISSUE=true
  echo "Creating issue: found $TOTAL_TODOS TODOs (>= 3 threshold)"
fi

if [ "$SHOULD_CREATE_ISSUE" = false ]; then
  echo "No follow-on issue needed (below threshold)"
  exit 0
fi

# ============================================
# Stage 5: Duplicate Detection
# ============================================

# Search for existing follow-on issues from this PR
EXISTING_ISSUE=$(gh issue list --state open --search "Follow-on from PR #$PR_NUMBER" --json number --jq '.[0].number // empty')

if [ -n "$EXISTING_ISSUE" ]; then
  echo "Follow-on issue already exists: #$EXISTING_ISSUE - skipping creation"
  exit 0
fi

# ============================================
# Stage 6: Create Follow-on Issue
# ============================================

# Get original issue title if available
if [ -n "$ORIGINAL_ISSUE" ]; then
  ORIGINAL_TITLE=$(gh issue view "$ORIGINAL_ISSUE" --json title --jq -r '.title' 2>/dev/null || echo "")
  PARENT_REF="Follow-on from PR #$PR_NUMBER which closed #$ORIGINAL_ISSUE"
  CONTEXT_LINE="**$ORIGINAL_TITLE** was implemented in PR #$PR_NUMBER."
else
  PR_TITLE=$(gh pr view "$PR_NUMBER" --json title --jq -r '.title')
  PARENT_REF="Follow-on from PR #$PR_NUMBER"
  CONTEXT_LINE="**$PR_TITLE** was merged in PR #$PR_NUMBER."
fi

# Build issue body
ISSUE_BODY="## Parent PR

$PARENT_REF

## Context

$CONTEXT_LINE During implementation/review, the following follow-on work was identified:

"

# Add Code TODOs section if present
if [ -n "$TODOS_RAW" ]; then
  ISSUE_BODY="${ISSUE_BODY}## Code TODOs

"
  # Format each TODO as a checkbox item
  while IFS= read -r todo_line; do
    [ -z "$todo_line" ] && continue
    file_line=$(echo "$todo_line" | cut -d: -f1-2)
    comment=$(echo "$todo_line" | cut -d: -f3-)
    ISSUE_BODY="${ISSUE_BODY}- [ ] \`$file_line\` - $comment
"
  done <<< "$TODOS_RAW"
  ISSUE_BODY="${ISSUE_BODY}
"
fi

# Add Follow-on sections if present
if [ -n "$FOLLOWON_SECTION" ]; then
  ISSUE_BODY="${ISSUE_BODY}## Deferred Scope

$FOLLOWON_SECTION"
fi

# Add Review Notes if present
if [ -n "$REVIEW_NOTES" ]; then
  ISSUE_BODY="${ISSUE_BODY}## Review Notes

$REVIEW_NOTES

"
fi

# Add acceptance criteria
ISSUE_BODY="${ISSUE_BODY}## Acceptance Criteria

- [ ] All identified TODOs addressed or converted to separate issues
- [ ] Deferred scope items implemented or explicitly deferred again
- [ ] Review suggestions addressed

---
*Auto-generated by Champion from PR #$PR_NUMBER*"

# Select label based on force mode
if [ "$FORCE_MODE" = "true" ]; then
  ISSUE_LABEL="loom:issue"
  FORCE_MARKER="[force-mode] "
else
  ISSUE_LABEL="loom:curated"
  FORCE_MARKER=""
fi

# Create the issue
ISSUE_TITLE="${FORCE_MARKER}Follow-on: Work identified in PR #$PR_NUMBER"
NEW_ISSUE=$(gh issue create \
  --title "$ISSUE_TITLE" \
  --body "$ISSUE_BODY" \
  --label "$ISSUE_LABEL" \
  --json number --jq '.number')

if [ -n "$NEW_ISSUE" ]; then
  echo "Created follow-on issue #$NEW_ISSUE with label $ISSUE_LABEL"

  # Add comment to original PR linking to the follow-on issue
  gh pr comment "$PR_NUMBER" --body "**Champion: Follow-on Issue Created**

Identified follow-on work during merge:
- **TODOs**: $TOTAL_TODOS ($CRITICAL_COUNT critical)
- **Deferred sections**: $HAS_FOLLOWON_SECTION
- **Review notes**: $HAS_REVIEW_NOTES

Created issue #$NEW_ISSUE to track this work.

---
*Automated by Champion role*"
else
  echo "Failed to create follow-on issue"
fi
```

**Threshold Logic Summary**:

| Indicator | Threshold | Action |
|-----------|-----------|--------|
| Critical patterns (FIXME, HACK, XXX) | 1+ | Always create |
| Explicit follow-on section | Any | Always create |
| Standard TODOs | 3+ | Create consolidated |
| TODOs with review notes | < 3 TODOs, has notes | Skip (too noisy) |
| Minimal indicators | < 3 TODOs, no sections | Skip |

**Force Mode Behavior**:
- Normal mode: Create with `loom:curated` (goes to Champion evaluation queue)
- Force mode: Create with `loom:issue` (goes directly to Builder queue)

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
| Size limit | <= configured limit (default 200, see `champion.auto_merge_max_lines` in `.loom/config.json`; waived by `loom:auto-merge-ok` label) | **No limit** (trust Judge review) |
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
