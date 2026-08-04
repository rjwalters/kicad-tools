# Champion: Reference Documentation

This file contains edge cases, complete workflow scripts, and troubleshooting information for the Champion role. **Reference this file when handling non-standard situations.**

---

## ⚠️ `--body @path` Does NOT Expand — It Posts the Literal String

If you post a comment via `gh issue comment` / `gh pr comment` / `gh api ...
comments` from a scratch file, `--body @path` (and `gh api -f body=@path`)
posts the literal string `@path`, not the file's contents. **Full pitfall,
incident citation, and fixes**:
[`comment-body-literal-path.md`](comment-body-literal-path.md).

## Edge Cases and Special Scenarios

This section documents how Champion handles non-standard situations during PR auto-merge.

### Edge Case 1: PR with No CI Checks

**Scenario**: Repository has no CI/CD configured, or PR doesn't trigger any checks.

**Handling**:
```bash
# With no checks, `gh pr checks --json bucket,name` prints "no checks reported..."
# to STDERR, exits non-zero, and emits EMPTY stdout. Detect via empty stdout
# (robust) rather than matching error text. CHECKS captured with 2>/dev/null.
# NOTE: `printf '%s\n' "$VAR" | jq`, never `echo "$VAR" | jq` — zsh's `echo`
# builtin reinterprets `\n`/`\t` escapes by default, which corrupts captured
# `gh --json` output (embedded newlines in body/comment text are represented
# as literal `\n` inside the JSON string) before jq ever parses it (#5094).
CHECKS=$(gh pr checks "$PR_NUMBER" --json bucket,name 2>/dev/null)
if [ -z "$CHECKS" ] || [ "$(printf '%s\n' "$CHECKS" | jq 'length')" = "0" ]; then
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
PENDING=$(printf '%s\n' "$CHECKS" | jq -r '.[] | select(.bucket == "pending") | .name')
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
MERGEABLE=$(gh pr view "$PR_NUMBER" --json mergeable --jq '.mergeable')
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

### Edge Case 5b: Doctor-Cycle-Capped PR (`loom:blocked` + `loom:changes-requested`)

**Scenario**: A PR exhausted `sweep.max_doctor_cycles` and `/loom:sweep` parked it with **both** `loom:blocked` and `loom:changes-requested`. Nothing else in the pipeline reconsiders that state — the work-finder skips blocked items and Mode C pre-flight skips blocked PRs — so it is terminal for automation until Champion looks at it (#4574).

**Handling**:
```bash
# Parked set (gh ANDs repeated --label values). Skip any that also carry
# loom:operator-only — already routed to a human.
gh pr list --label "loom:blocked" --label "loom:changes-requested" --state open --limit 500 \
  --json number,title,labels --jq '.[] | "#\(.number) \(.title)"'

# Decide from the FULL history, not the last comment alone.
gh pr view "$PR_NUMBER" --comments
```

**Decision**: **Three-way, on the forward-progress test** — never a default grant.

| Finding in the rejection history | Decision | Action |
|----------------------------------|----------|--------|
| Latest rejection names defects demonstrably **distinct** from the prior one (prior fix landed, new defects only reachable because of it), fixable in one bounded cycle, chain still converging | **Grant one more Doctor cycle** | Comment with `<!-- champion:capped-pr-grant -->` naming both rejections and the distinction, then remove **`loom:blocked` only** (leave `loom:changes-requested` — it is what routes the PR to Doctor) |
| Same defect **re-litigated**, comparison **ambiguous**, only one rejection exists, or the chain is no longer converging | **Keep parked** | Comment the *specific* human judgment needed, guarded by a `<!-- champion:capped-pr-parked:<latest-rejection-comment-id> -->` marker so the 10-minute cron does not re-post per tick; change no labels |
| The **approach** (not the implementation) is the problem — repeated design rejections, superseded premise | **Recommend closing, route to the operator** | Comment with `<!-- champion:capped-pr-close-recommended -->` and add `loom:operator-only` (keeping `loom:blocked`); **do not close the PR** — Champion routes, the human decides |

**Rationale**: This is the *same* forward-progress mechanism as the sweep's in-sweep distinct-defect grace cycle (`sweep.md` → "Doctor-cycle cap"), applied at a different decision point — periodically, post-mortem, with the complete history rather than the dying sweep's local context. There is **no hard grant cap**: repeat grants are allowed as long as each new rejection shows fresh progress, because the anti-thrash guarantee comes from re-applying the test every round, not from a counter. Nor is there a double-grant path: a PR only reaches `loom:blocked` after the sweep-side single-use exception was consumed or was not applicable.

**Entry guards**: the label pair alone is not proof of a cap block. Skip PRs also carrying `loom:operator-only`, keep parked any PR whose history shows no cap block (fewer than two Judge rejections, no `doctor cycle exhausted` line), and never grant over an explicit human hold (`hold until` / `wait until` / `defer` / `not before` / `do not start` phrasing — the sweep's explicit-hold convention).

**Human vs. parked**: `loom:operator-only` means *the approach needs a human ruling and automation should stop touching this PR*; plain `loom:blocked` + a keep-parked comment means *a human should look, but a future rejection could still change the answer* — Champion re-evaluates that PR when a new Judge rejection lands.

**Action** (single authoritative policy — implemented in `champion-pr-merge.md` → "Capped-PR Recovery Pass"): see that section for the exact commands, the full grant/never-grant criteria, and the rationale comment templates.

---

### Edge Case 5c: Unrevised Proposal Re-Entering the Evaluation Queue Every Cycle (#4954)

**Scenario**: A `loom:curated`/`loom:architect`/`loom:hermit`/`loom:auditor` proposal fails promotion criteria and gets a "NEEDS REVISION" comment, but the author never revises it. Every subsequent Champion pass (cron tick, role-runner tick, or a fresh `/loom:sweep` dispatch) re-discovers the same unchanged issue in its Priority 2/3 listing and, without a guard, re-evaluates it from scratch and posts an equivalent rejection comment — observed live as 6 duplicate "NEEDS REVISION" comments over ~6.5 hours on one proposal, with two of them landing 40 seconds apart because nothing claimed the issue mid-evaluation.

**Handling**: `champion-issue-promo.md`'s "Concurrency Guard and Idempotency (`loom:evaluating`)" section (adapted from this file's own Capped-PR Recovery pattern — `PARK_MARKER`/`CLOSE_MARKER` below — and from Judge's `loom:reviewing` claim/stale-check convention):

```bash
# Idempotency: skip without commenting if already evaluated at this revision.
# $BODY_HASH = sha256(title + body), first 16 hex chars — NOT the issue's
# aggregate updatedAt, which Champion's own comment would bump (see #4966).
VERDICT_MARKER="<!-- champion:proposal-verdict:body-$BODY_HASH -->"

# Concurrency: claim before evaluating, staleness-aware (LOOM_STALE_EVALUATING_MINUTES, default 15m).
gh issue edit <number> --add-label "loom:evaluating"
```

**Decision**:

| Finding | Decision | Action |
|---------|----------|--------|
| A prior Champion verdict comment already carries `VERDICT_MARKER` for the issue's **current** title+body hash | **Unrevised since last review — skip** | No comment, no claim, no label change. A genuine title/body edit changes the hash and always produces a fresh marker and a fresh evaluation; comments and label churn do not. |
| Issue already carries `loom:evaluating` and the claim is younger than `LOOM_STALE_EVALUATING_MINUTES` | **Concurrent evaluation in progress** | Skip, do not stomp the claim; continue the batch. |
| Issue already carries `loom:evaluating` and the claim is older than `LOOM_STALE_EVALUATING_MINUTES` | **Stale claim — a prior Champion pass likely died mid-evaluation** | Reclaim (`--add-label "loom:evaluating"` again) then evaluate normally. |
| ≥2 prior "NEEDS REVISION" comments exist and the issue is not already `loom:operator-only` | **N=2 threshold reached** | Escalate instead of posting a third+ near-identical rejection: comment with `<!-- champion:proposal-escalated -->` and add `loom:operator-only` (Champion routes, a human decides — the proposal label stays, nothing is closed). |
| Fewer than 2 prior rejections | **Ordinary reject** | Post the `VERDICT_MARKER`-tagged "NEEDS REVISION" comment as before, release `loom:evaluating`. |

**Rationale**: This is the *same* idempotency-marker + escalation-marker + operator-routing shape as Edge Case 5b's Capped-PR Recovery Pass, applied to the proposal-evaluation side of Champion instead of the PR-merge side — a marker keyed to the thing whose change would invalidate it (here, the proposal's own title+body text; there, the latest Judge rejection comment ID) stops duplicate comments, and a bounded escalation threshold (here, N=2 identical verdicts; there, the Doctor-cycle cap) converts an infinite silent loop into a single human-visible routing decision.

**Anchor discipline (#4966)**: neither half of this mechanism may key off the issue's aggregate `updatedAt` — Champion's own verdict comment bumps it, so a marker stamped with it can never match on the next pass and the skip never fires. Content staleness anchors on the title+body hash; claim staleness anchors on the `loom:evaluating` label's own `labeled` timeline event. Both are invisible to Champion's own comment writes. This is the same rule `judge.md`/`daemon-reference.md` already apply to `loom:reviewing`/`loom:treating` staleness.

---

### Edge Case 6: PR Modifying Only Test Files

**Scenario**: PR changes only test files (e.g., `*.test.ts`, `*.spec.rs`).

**Handling**: No special handling needed - standard safety criteria apply.

**Decision**: **Allow merge if criteria pass** - test-only changes are safe.

**Rationale**: The merge-risk judgment (criterion #2) and CI checks provide sufficient protection — a test-only diff is green on diff composition and revertability by construction, however many lines it is.

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
  STATE=$(gh issue view "$issue" --json state --jq '.state')
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
FAILING=$(printf '%s\n' "$CHECKS" | jq -r '.[] | select(.bucket == "fail" or .bucket == "cancel") | .name')
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

### Edge Case 11: Size and Risk Point in Opposite Directions

**Scenario A — large but low-risk**: An 886-line PR that is ~700 lines of new tests plus one self-contained new module, approved by a Judge whose review names the module's functions and what it verified.

**Scenario B — small but high-risk**: A 40-line PR that changes the ordering guard in `.loom/scripts/merge-pr.sh` (or a `.loom/hooks/guard-*.sh` hook), approved with a one-line "LGTM".

**Handling**: There is no line-count gate. Criterion #2 (Merge-Risk Judgment) scores each PR on diff composition, blast radius, Judge review depth, and revertability.

**Decision**:
- **Scenario A: allow merge** — green on all four axes (test-heavy composition, single-module blast radius, specific review, plain `git revert`).
- **Scenario B: hold for a human** — red on blast radius (merge/guard automation the whole fleet depends on) *and* on review depth (a generic approval), with revertability weak because a bad guard can delete a branch before the revert lands. Comment names that concern, `loom:pr` stays, Champion retries next tick.

**Rationale**: Size was never the risk; it was a proxy that inverted in both directions. The `champion.auto_merge_max_lines` knob that produced this edge case is retired — see the migration note in `champion-pr-merge.md` → "Safety Criteria → 2. Merge-Risk Judgment". `loom:auto-merge-ok` still exists, repurposed: it is an explicit human/Judge override of a Champion merge-risk hold (it does not waive the critical-file check).

---

### Edge Case 11b: Prior Merge-Risk Hold, Later Tick Scores the Same Diff Green

**Scenario**: An earlier Champion tick posted `<!-- champion:merge-risk-hold -->` on a red axis. A later tick re-reads the *same* diff and — axis scoring being a judgment call, not an arithmetic one — scores it green. Nothing external changed: no `loom:auto-merge-ok`, no new push, no new review. (Observed live on PR #4700, 2026-07-31: hold at 04:16Z, merge at 11:21Z, no override label, and no comment of any kind accompanying the merge — the last comment on the PR is still the hold notice. #4742.)

**Handling**: The merge-risk hold is **sticky**. Before scoring the axes, criterion #2 runs a precheck that looks for an existing hold marker and, if found, requires a durable release signal:

```bash
# Plain `gh` — merge-gating, so never "$GH_READ".
PR_JSON=$(gh pr view "$PR_NUMBER" --json comments,commits,labels,headRefOid)
HOLD_BODY=$(jq -r --arg m "<!-- champion:merge-risk-hold -->" \
  '[.comments[] | select(.body | contains($m))] | last | .body // ""' <<<"$PR_JSON")
# Released only by: loom:auto-merge-ok | an explicit operator clearing comment
# posted after the hold (the instruction must OPEN the comment's leading clause
# and not be a question — "do not merge anyway" / "is it ok to merge?" do NOT
# release) | a new head SHA (recorded as `champion:hold-state head=<sha>` in the
# hold comment) | a new Judge review after the hold.
```

**Decision**: **Hold stands** — skip the PR for this pass whatever the axes say this tick. A green re-read of an unchanged diff is not a release signal, and Champion's own comments never count as one.

**On release, one mandatory comment**: when a release signal *does* exist and the PR merges, the pre-merge comment must carry a `<!-- champion:merge-risk-hold-cleared -->` block naming what released the hold (which override was honored, or which axis flipped and why, citing the new commit/review). The `<!-- champion:merge-risk-hold -->` idempotency guard governs **repeat hold notices only** — it must never suppress a hold-to-merge transition, which is a distinct event that always produces exactly one new comment.

**Rationale**: A hold that a later subjective re-read can silently evaporate is not a hold. Because every fleet agent acts under the operator's forge identity, `mergedBy` cannot distinguish a human override from a Champion tick that scored the axes differently — so the comment is the *only* audit trail, and the anti-spam guard that (correctly) suppresses repeat holds was suppressing precisely the comment that would have explained the reversal.

**Action** (single authoritative policy — implemented in `champion-pr-merge.md` → "Safety Criteria → 2. Merge-Risk Judgment → Sticky holds"): see that section for the precheck, the four outcomes, and the reversal-comment template.

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
EXISTING=$(gh issue list --search "Follow-on from PR #$PR_NUMBER" --limit 500)

# Stage 6: Create issue with proper linking
./.loom/scripts/create-issue.sh --title "Follow-on: Work identified in PR #$PR_NUMBER" --label "$LABEL"
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
| Doctor-cycle-capped PR (`loom:blocked` + `loom:changes-requested`) | Three-way on forward progress | Distinct new defects → grant a cycle (remove `loom:blocked` only); same-defect/ambiguous → keep parked with rationale; approach not viable → add `loom:operator-only`, recommend closing (never close it) |
| Unrevised proposal re-entering the queue every cycle | Idempotency marker + N=2 escalation | Unrevised since last review → skip silently (title+body hash marker match, never `updatedAt`); ≥2 prior rejections → escalate to `loom:operator-only` instead of a 3rd+ duplicate comment; `loom:evaluating` claim prevents concurrent double-evaluation |
| Test-only changes | Allow | Standard criteria apply |
| Human holds PR (removes `loom:pr`) | Skip | Not a merge candidate without `loom:pr` |
| Multiple linked issues | Allow | Verify all closed |
| Mixed-state CI | Fail on `fail`/`cancel` | `pending` defers; `skipping` is OK |
| Unknown critical file | Miss | Needs pattern update |
| Large but low-risk PR (e.g. mostly tests) | Allow | Judged on the 4 risk axes, not line count |
| Small but high-blast-radius PR | Hold for human | Comment names the specific concern, keep `loom:pr`, retry next tick |
| Prior merge-risk hold, later tick scores the same diff green | **Hold stands (sticky)** | Skip silently, post nothing (anti-spam guard already covers it). Release only on `loom:auto-merge-ok`, an explicit operator clearing comment after the hold (leading-clause instruction, not a negation or a question), a new head SHA, or a new Judge review |
| Prior merge-risk hold released, PR merges | Allow + **mandatory reversal comment** | Pre-merge comment carries `<!-- champion:merge-risk-hold-cleared -->` naming the override honored or the axis that flipped and why; never suppressed by the hold idempotency guard |
| `loom:auto-merge-ok` present | Allow | Explicit human/Judge override of a merge-risk hold (does not waive critical files); a *previously posted* hold still requires the reversal comment |
| API rate limit | Error | Comment and continue |
| Multiple approvals | Allow | Label is source of truth |
| Follow-on indicators found | Create | If thresholds met |

---

## Complete Auto-Merge Workflow Script

**The auto-merge workflow lives in a single source of truth: [`champion-pr-merge.md`](champion-pr-merge.md).**

This file previously carried a second, full copy of the end-to-end merge script. That duplicate diverged from `champion-pr-merge.md` over time (it lacked Step 5.5 Follow-on Issue Creation and repeated the same bugs — invalid `gh pr checks --json` fields, etc.), forcing every fix to be applied twice. It has been removed to eliminate the drift (issue #3781).

For the authoritative, end-to-end implementation — the Verdict-State Janitor (run before all else, resolves a contradictory `loom:pr` + `loom:changes-requested` state fail-safe, #4570), the 6 safety criteria, the pre-merge comment, the squash merge via `merge-pr.sh`, linked-issue closure verification, dependent-issue unblocking, and Step 5.5 Follow-on Issue Creation — see **`champion-pr-merge.md`**. The edge cases and decision matrix above remain here as the reference for non-standard situations; they describe *behavior*, and defer to `champion-pr-merge.md` for the *script*.

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
- Verify dependency format: "Blocked by #123" or "Depends on #123" — markdown
  emphasis and an optional colon before `#N` are also tolerated, e.g.
  "**Blocked by:** #123 (reason)" or "_Depends on_ #123" (#4508)
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
gh issue list --label "loom:blocked" --state open --limit 500

# Check API rate limit
gh api rate_limit
```
