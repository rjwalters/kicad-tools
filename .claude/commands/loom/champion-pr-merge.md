# Champion: PR Auto-Merge Context

This file contains PR auto-merge instructions for the Champion role. **Read this file when Priority 1 work is found (PRs with loom:pr label).**

---

## Overview

Auto-merge Judge-approved PRs that are safe, routine, and low-risk.

The Champion acts as the final step in the PR pipeline, merging PRs that have passed Judge review and meet all safety criteria.

---

## Safety Criteria

For each `loom:pr` PR, verify ALL 6 safety criteria. If ANY criterion fails, do NOT merge.

### 1. Label Check
- [ ] PR has `loom:pr` label (Judge approval)

**Verification command**:
```bash
# Get all labels for the PR
LABELS=$(gh pr view <number> --json labels --jq '.labels[].name' | tr '\n' ' ')

# Check for loom:pr label
if ! echo "$LABELS" | grep -q "loom:pr"; then
  echo "FAIL: Missing loom:pr label"
  exit 1
fi

echo "PASS: Label check"
```

**Rationale**: Only merge PRs explicitly approved by Judge. A human holds a PR by removing its `loom:pr` label (or adding `loom:changes-requested`), which fails this check.

### 2. Size Check
- [ ] Total lines changed <= configured limit (additions + deletions)
- [ ] **Default limit**: 200 lines (configurable via `.loom/config.json` `champion.auto_merge_max_lines`)
- [ ] **`loom:auto-merge-ok` label**: Size limit is waived (applied by Judge or human to signal large PR is safe)

**Verification command**:
```bash
# Get additions and deletions
PR_DATA=$(gh pr view <number> --json additions,deletions --jq '{additions, deletions, total: (.additions + .deletions)}')
ADDITIONS=$(echo "$PR_DATA" | jq -r '.additions')
DELETIONS=$(echo "$PR_DATA" | jq -r '.deletions')
TOTAL=$((ADDITIONS + DELETIONS))

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
```

**Rationale**: Small PRs are easier to revert if problems arise. The size limit is configurable via `.loom/config.json` to allow teams to tune the risk/autonomy tradeoff. The `loom:auto-merge-ok` label provides a per-PR escape hatch for large but safe PRs.

### 3. Critical File Exclusion Check
- [ ] No changes to critical configuration or infrastructure files

**Critical file patterns** (do NOT auto-merge if PR modifies any of these):
- `Cargo.toml` - root dependency changes
- `loom-daemon/Cargo.toml` - daemon dependency changes
- `loom-api/Cargo.toml` - api dependency changes
- `package.json` - npm dependency changes
- `.github/workflows/*` - CI/CD pipeline changes
- `*.sql` - database schema changes
- `*migration*` - database migration files

**Verification command**:
```bash
# Get all changed files
FILES=$(gh pr view <number> --json files --jq -r '.files[].path')

# Define critical patterns (extend as needed)
CRITICAL_PATTERNS=(
  "Cargo.toml"
  "loom-daemon/Cargo.toml"
  "loom-api/Cargo.toml"
  "package.json"
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
```

**Rationale**: Changes to these files require careful human review due to high impact.

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
- [ ] PR updated within last 24 hours

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

RECENCY_LIMIT=24

# Check if within recency limit
if [ "$HOURS_AGO" -gt "$RECENCY_LIMIT" ]; then
  echo "FAIL: Stale PR (updated $HOURS_AGO hours ago, limit is ${RECENCY_LIMIT}h)"
  exit 1
fi

echo "PASS: Recently updated ($HOURS_AGO hours ago)"
```

**Rationale**: Ensures PR reflects recent state of main branch and hasn't gone stale.

**On failure**: a stale PR is handled by the dedicated stale-PR policy (see "PR Rejection Workflow → Stale PR"), not the transient-failure path — it is commented once (idempotently) and routed out of the queue via `loom:pr` → `loom:changes-requested` so it reaches Doctor rather than being re-commented every cron tick.

### 6. CI Status Check
- [ ] If CI checks exist, all checks must be passing
- [ ] If no CI checks exist, this criterion passes automatically

**Verification command**:
```bash
# Get all CI checks. `gh pr checks --json` exposes `bucket` (the rolled-up
# pass/fail/pending/skipping/cancel state) and `name` — there is NO `conclusion`
# or `status` field (those were invalid and made this gate silently vacuous).
# Capture stdout ONLY: when a PR has no checks, gh prints "no checks reported..."
# to STDERR and exits non-zero with EMPTY stdout, so an empty result is the
# robust no-checks signal (do not grep error text).
CHECKS=$(gh pr checks <number> --json bucket,name 2>/dev/null)

# Handle case where no checks exist (empty stdout, or an empty JSON array)
if [ -z "$CHECKS" ] || [ "$(echo "$CHECKS" | jq 'length')" = "0" ]; then
  echo "PASS: No CI checks required"
  exit 0
fi

# Parse checks by bucket. Buckets: pass, fail, pending, skipping, cancel.
# `fail`/`cancel` block the merge; `pending` defers; `pass`/`skipping` are OK.
FAILING_CHECKS=$(echo "$CHECKS" | jq -r '.[] | select(.bucket == "fail" or .bucket == "cancel") | .name')
PENDING_CHECKS=$(echo "$CHECKS" | jq -r '.[] | select(.bucket == "pending") | .name')

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
- **No CI checks**: Passes (allows merge) — detected via empty stdout, not error text
- **Pending checks**: Skips (waits for completion) — `bucket == "pending"`
- **Failed checks**: Fails (blocks merge) — `bucket == "fail"` or `"cancel"`
- **Skipped checks**: Passes — `bucket == "skipping"` is not a failure

**Rationale**: Only merge when all automated checks pass or no checks are configured

---

## Auto-Merge Workflow

### Step 1: Verify Safety Criteria

For each candidate PR, check ALL 6 criteria in order. If any criterion fails, skip to rejection workflow.

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

# Check CI status (empty stdout = no checks; see criterion #6 above)
CHECKS=$(gh pr checks "$PR_NUMBER" --json bucket,name 2>/dev/null)
if [ -z "$CHECKS" ] || [ "$(echo "$CHECKS" | jq 'length')" = "0" ]; then
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

# Ensure we're on main so .loom/scripts exists (issue #2289)
# merge-pr.sh may not exist on PR branches checked out via gh pr checkout
git checkout main 2>/dev/null || true

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

# Extract linked issues using GitHub's own parser (closingIssuesReferences).
# This is the authoritative set of issues GitHub will auto-close on merge.
# It correctly ignores `Updates #N`, `See #N`, code-fenced text, and substring
# traps like `Discloses #N`. The previous regex-based approach silently
# misclassified `Updates #N` as a closing reference — see issue #3267.
source "$(git rev-parse --show-toplevel)/.loom/scripts/lib/forge-helpers.sh"
forge_detect
LINKED_ISSUES=$(forge_pr_close_targets "$PR_NUMBER")

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
    # POSIX awk: 2-arg match() sets RSTART/RLENGTH (the gawk-only 3-arg
    # match($0, re, arr) form errors on BSD awk / macOS). Capture the "+<n>"
    # token, then strip the leading "+" with substr().
    if (match($0, /\+[0-9]+/)) {
      line_num = substr($0, RSTART + 1, RLENGTH - 1)
    }
    in_hunk = 1
  }
  in_hunk && /^\+[^+]/ {
    # Added line (not the +++ header)
    # POSIX-portable word boundary: BSD awk (macOS) does NOT support the gawk-only
    # \b escape, so `/\b(TODO...):/` silently matches nothing there. Anchor on
    # start-of-string-or-non-word-char instead so this fires on BSD awk too.
    if ($0 ~ /(^|[^A-Za-z0-9_])(TODO|FIXME|HACK|XXX|FUTURE):/) {
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

# Follow-on issues go to the Champion evaluation queue.
ISSUE_LABEL="loom:curated"

# Create the issue.
# NOTE: `gh issue create` does NOT support --json/--jq (only `gh issue view`
# and `gh issue list` do). On success it prints the new issue's URL to stdout
# (e.g. https://github.com/<owner>/<repo>/issues/<N>); parse the trailing
# number from that URL.
ISSUE_TITLE="Follow-on: Work identified in PR #$PR_NUMBER"
NEW_ISSUE_URL=$(gh issue create \
  --title "$ISSUE_TITLE" \
  --body "$ISSUE_BODY" \
  --label "$ISSUE_LABEL")
NEW_ISSUE=$(echo "$NEW_ISSUE_URL" | grep -oE '[0-9]+$')

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

**Follow-on Issue Labeling**: Follow-on issues are created with `loom:curated` (goes to the Champion evaluation queue).

---

## PR Rejection Workflow

If ANY safety criterion fails, do NOT merge. How the failure is handled depends on whether it is **transient** (clears on its own or on the next push — pending CI, conflicts being resolved, `UNKNOWN` mergeability) or **terminal** (the PR has gone stale and cannot clear without a rebase).

### Transient failures — keep `loom:pr`, retry next tick

Add a comment explaining why, and **keep the `loom:pr` label** so the PR is re-evaluated on the next Champion tick once the blocking condition clears:

```bash
gh pr comment <number> --body "**Champion: Cannot Auto-Merge**

This PR cannot be automatically merged due to the following:

- <CRITERION_NAME>: <SPECIFIC_REASON>

**Next steps:**
- <SPECIFIC_ACTION_1>
- <SPECIFIC_ACTION_2>

Keeping \`loom:pr\` label. Champion will retry on the next tick once the blocking condition clears.

---
*Automated by Champion role*"
```

**Do NOT remove the `loom:pr` label for transient failures** — the next tick retries automatically.

### Stale PR (recency check failed) — comment once, route to Doctor

A stale PR (>24h) will never clear on its own, and under the 10-minute cron a bare "keep the label + comment" loop would re-comment on the same PR **every tick forever**. Instead, **comment once (idempotently)** and **swap `loom:pr` → `loom:changes-requested`** so the PR leaves the auto-merge queue and is picked up by Doctor for a rebase/refresh. This is the single, authoritative stale-PR policy — `champion-reference.md` Edge Case 5 defers to it.

```bash
PR_NUMBER=<number>
STALE_MARKER="<!-- champion:stale-pr-notice -->"

# Idempotency guard: only comment + relabel once. If a prior tick already
# posted the stale notice, do nothing (prevents per-tick comment spam).
if gh pr view "$PR_NUMBER" --json comments --jq '.comments[].body' | grep -qF "$STALE_MARKER"; then
  echo "Stale-PR notice already posted for #$PR_NUMBER — skipping"
else
  gh pr comment "$PR_NUMBER" --body "$STALE_MARKER
**Champion: PR Is Stale**

This PR has not been updated within the recency window (24h), so it has been routed out of the auto-merge queue for a rebase/refresh.

**Next steps:**
- Rebase onto the latest \`main\` and resolve any drift
- Re-request Judge review to return it to the auto-merge queue

---
*Automated by Champion role*"
  # Route to Doctor: leave the auto-merge queue.
  gh pr edit "$PR_NUMBER" --remove-label "loom:pr" --add-label "loom:changes-requested"
  echo "Routed stale PR #$PR_NUMBER to Doctor (loom:pr → loom:changes-requested)"
fi
```

---

## PR Auto-Merge Batch Processing

**Process all qualifying PRs in one iteration — drain the full queue.**

Evaluate and merge qualifying PRs sequentially (oldest first) until the queue is empty. Sequential processing is safe and prevents the bottleneck that occurs when PRs accumulate while the champion waits for the next interval.

If an individual merge fails, continue to the next PR rather than aborting the entire iteration.

---

## Error Handling

If the merge fails for any reason:

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

## Return to Main Champion File

After completing PR merge work, return to the main champion.md file for completion reporting.
