# Champion: Reference Documentation

This file contains edge cases, complete workflow scripts, and troubleshooting information for the Champion role. **Reference this file when handling non-standard situations.**

---

## Edge Cases and Special Scenarios

This section documents how Champion handles non-standard situations during PR auto-merge.

### Edge Case 1: PR with No CI Checks

**Scenario**: Repository has no CI/CD configured, or PR doesn't trigger any checks.

**Handling**:
```bash
# With no checks, `gh pr checks --json bucket,name` prints "no checks reported..."
# to STDERR, exits non-zero, and emits EMPTY stdout. Detect via empty stdout
# (robust) rather than matching error text. CHECKS captured with 2>/dev/null.
CHECKS=$(gh pr checks "$PR_NUMBER" --json bucket,name 2>/dev/null)
if [ -z "$CHECKS" ] || [ "$(echo "$CHECKS" | jq 'length')" = "0" ]; then
  echo "PASS: No CI checks required"
  # Continue to merge
fi
```

**Decision**: **Allow merge** - absence of CI is not a blocker.

**Rationale**: Many repositories don't use CI, or use rulesets without status checks.

---

### Edge Case 2: PR with Pending CI Checks

**Scenario**: CI checks are queued or in progress when Champion evaluates the PR.

**Handling**:
```bash
# Check for pending/running checks (bucket == "pending")
PENDING=$(echo "$CHECKS" | jq -r '.[] | select(.bucket == "pending") | .name')
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

**Decision**: **Comment once, then route out of the queue** - do not merge stale PRs, and do not re-comment every cron tick.

**Rationale**: Main branch may have evolved significantly. Stale PRs should be rebased or re-reviewed.

**Action** (single authoritative policy — implemented in `champion-pr-merge.md` → "PR Rejection Workflow → Stale PR"): post the stale notice **once**, guarded by an idempotency marker (`<!-- champion:stale-pr-notice -->`) so the 10-minute cron does not spam the PR, and **swap `loom:pr` → `loom:changes-requested`** to route the PR to Doctor for a rebase/refresh. This removes `loom:pr` (unlike the transient-failure path, which keeps it), because a stale PR cannot clear itself and must leave the auto-merge queue. See `champion-pr-merge.md` for the exact commands.

---

### Edge Case 6: PR Modifying Only Test Files

**Scenario**: PR changes only test files (e.g., `*.test.ts`, `*.spec.rs`).

**Handling**: No special handling needed - standard safety criteria apply.

**Decision**: **Allow merge if criteria pass** - test-only changes are safe.

**Rationale**: Size limit (configurable, default 200 lines) and CI checks provide sufficient protection.

---

### Edge Case 7: PR with `loom:pr` Removed Mid-Evaluation

**Scenario**: Human removes the `loom:pr` label (or adds `loom:changes-requested`) to hold a PR while Champion is evaluating it.

**Handling**: Label check (#1) runs first, catches the missing `loom:pr` immediately.

**Decision**: **Skip immediately** - a PR without `loom:pr` is not a merge candidate.

**Rationale**: Champion re-fetches labels at start of each evaluation, so the human hold takes effect on the next evaluation; the race-condition window is minimal.

---

### Edge Case 8: PR Linked to Multiple Issues

**Scenario**: PR body contains "Closes #123, Closes #456, Fixes #789".

**Handling**:
```bash
# Extract all linked issues using GitHub's own parser (closingIssuesReferences).
# Note: `Updates #N` is intentionally excluded — it does not close the issue
# (see issue #3267). The forge_pr_close_targets helper handles this correctly.
source "$(git rev-parse --show-toplevel)/.loom/scripts/lib/forge-helpers.sh"
forge_detect
LINKED_ISSUES=$(forge_pr_close_targets "$PR_NUMBER")

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

**Rationale**: GitHub auto-closes multiple issues, but verify and manually close if needed. The helper uses GitHub's `closingIssuesReferences` so `Updates #N` (and similar non-closing references) are correctly excluded.

---

### Edge Case 9: PR with Mixed-State CI Checks

**Scenario**: Some checks pass, some pending, some skipped.

**Handling**:
```bash
# A "fail" or "cancel" bucket blocks the merge; "pending" defers; "pass" and
# "skipping" are acceptable. (gh buckets: pass, fail, pending, skipping, cancel.)
FAILING=$(echo "$CHECKS" | jq -r '.[] | select(.bucket == "fail" or .bucket == "cancel") | .name')
if [ -n "$FAILING" ]; then
  echo "FAIL: Some checks did not pass"
fi
```

**Decision**: **Fail on any `fail`/`cancel` bucket; defer on `pending`** - conservative but not falsely blocking.

**Rationale**: A `skipping` bucket (a conditionally-skipped job) is not a failure and does not block auto-merge; only `fail`/`cancel` block and `pending` defers.

---

### Edge Case 10: Critical File Pattern Extensions

**Scenario**: Repository adds new critical files not in pattern list (e.g., `auth.config.ts`).

**Handling**: Champion uses hardcoded patterns - will **not** catch new critical files.

**Decision**: **Requires pattern update** - human must extend `CRITICAL_PATTERNS` array.

**Maintenance**: Review and update critical file patterns periodically as codebase evolves.

**Recommended**: Add repository-specific `.loom/champion-critical-files.txt` for custom patterns (future enhancement).

---

### Edge Case 11: PR Size Exactly at Limit

**Scenario**: PR has exactly the configured limit of lines changed (e.g., if limit is 200: 100 additions + 100 deletions).

**Handling**:
```bash
SIZE_LIMIT=$(jq -r '.champion.auto_merge_max_lines // 200' .loom/config.json 2>/dev/null || echo 200)
if [ "$TOTAL" -gt "$SIZE_LIMIT" ]; then  # Strictly greater than
  echo "FAIL: Too large"
fi
```

**Decision**: **Allow merge** - limit is inclusive (<= configured limit allowed).

**Rationale**: PRs exactly at the limit are still considered acceptable for auto-merge purposes. The limit is configurable via `champion.auto_merge_max_lines` in `.loom/config.json` (default: 200). PRs can also bypass the size limit entirely with the `loom:auto-merge-ok` label.

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

### Edge Case 14: Follow-on Issue Creation

**Scenario**: Merged PR contains TODOs, FIXMEs, deferred scope sections, or review comments suggesting future work.

**Handling**:
```bash
# After merge, scan for follow-on indicators
# Stage 1: Extract TODO/FIXME from diff with file:line attribution
TODOS=$(gh pr diff "$PR_NUMBER" | awk '...')  # See champion-pr-merge.md

# Stage 2: Parse PR body for follow-on sections
FOLLOWON=$(echo "$PR_BODY" | sed -n '/^## Follow-on/,/^## /p')

# Stage 3: Parse review comments for deferred suggestions
NOTES=$(gh api repos/.../pulls/$PR_NUMBER/comments --jq '...')

# Stage 4: Apply threshold logic
# - 1+ critical (FIXME/HACK/XXX) -> always create
# - Explicit follow-on section -> always create
# - 3+ TODOs -> create consolidated
# - Otherwise -> skip (too noisy)

# Stage 5: Duplicate detection
EXISTING=$(gh issue list --search "Follow-on from PR #$PR_NUMBER")

# Stage 6: Create issue with proper linking
gh issue create --title "Follow-on: Work identified in PR #$PR_NUMBER" --label "$LABEL"
```

**Decision**: **Create follow-on issue if thresholds met** - captures future work.

**Rationale**: Prevents valuable context about follow-on work from being lost when PRs merge. TODOs in code, deferred scope items, and review suggestions become trackable issues.

**Threshold Logic**:

| Indicator | Threshold | Action |
|-----------|-----------|--------|
| Critical patterns (FIXME, HACK, XXX) | 1+ | Always create |
| Explicit follow-on section | Any | Always create |
| Standard TODOs | 3+ | Create consolidated |
| Below threshold | < 3 TODOs, no sections | Skip |

**Follow-on Issue Labeling**: Follow-on issues are created with the `loom:curated` label (goes to Champion evaluation).

**Edge Cases Within Follow-on**:

1. **PR with no original issue**: Use PR title instead of issue title for context
2. **TODO without colon**: Pattern requires `TODO:` not just `TODO` to avoid false positives
3. **Multi-line TODOs**: Only first line captured, truncated at 200 chars
4. **Duplicate follow-on issue exists**: Search before creation, skip if found

---

## Summary: Edge Case Decision Matrix

| Edge Case | Decision | Action |
|-----------|----------|--------|
| No CI checks | Allow | Continue to merge |
| Pending CI checks | Skip | Defer to next iteration |
| Force-push after approval | Allow | If criteria still pass |
| Merge conflicts | Fail | Comment and skip |
| Stale PR (>24h) | Route to Doctor | Comment once (idempotent marker), swap `loom:pr` → `loom:changes-requested` |
| Test-only changes | Allow | Standard criteria apply |
| Human holds PR (removes `loom:pr`) | Skip | Not a merge candidate without `loom:pr` |
| Multiple linked issues | Allow | Verify all closed |
| Mixed-state CI | Fail on `fail`/`cancel` | `pending` defers; `skipping` is OK |
| Unknown critical file | Miss | Needs pattern update |
| Exactly at size limit | Allow | Limit is inclusive |
| API rate limit | Error | Comment and continue |
| Multiple approvals | Allow | Label is source of truth |
| Follow-on indicators found | Create | If thresholds met |

---

## Complete Auto-Merge Workflow Script

**The auto-merge workflow lives in a single source of truth: [`champion-pr-merge.md`](champion-pr-merge.md).**

This file previously carried a second, full copy of the end-to-end merge script. That duplicate diverged from `champion-pr-merge.md` over time (it lacked Step 5.5 Follow-on Issue Creation and repeated the same bugs — invalid `gh pr checks --json` fields, etc.), forcing every fix to be applied twice. It has been removed to eliminate the drift (issue #3781).

For the authoritative, end-to-end implementation — the 6 safety criteria, the pre-merge comment, the squash merge via `merge-pr.sh`, linked-issue closure verification, dependent-issue unblocking, and Step 5.5 Follow-on Issue Creation — see **`champion-pr-merge.md`**. The edge cases and decision matrix above remain here as the reference for non-standard situations; they describe *behavior*, and defer to `champion-pr-merge.md` for the *script*.

---

## Troubleshooting

### Common Issues

**PR not merging despite passing all checks**
- Check if rulesets require additional approvals
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

# View linked issues (uses GitHub's authoritative parser; `Updates #N` is excluded)
gh pr view <number> --json closingIssuesReferences --jq '.closingIssuesReferences[].number'

# List blocked issues
gh issue list --label "loom:blocked" --state open

# Check API rate limit
gh api rate_limit
```
