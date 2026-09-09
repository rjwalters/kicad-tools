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

**#6211: empty stdout alone is NOT proof "no checks exist".** An earlier
version of this doc detected "no checks" via empty stdout from `gh pr checks
--json` alone (with stderr discarded via `2>/dev/null` and the exit code
never inspected). That is unsafe: `gh pr checks` can ALSO return empty stdout
during a transient forge failure (e.g. the intermittent TLS handshake error
#6169 hit, observed ~1 call in 3 on one host) — a state indistinguishable
from genuine "no checks configured" once stderr is thrown away. Since this
read gates Champion's auto-merge, that ambiguity is a real (if rare)
false-positive path: a PR with actual pending/unrun CI could get merged.

The fix distinguishes the two cases instead of collapsing them: the genuine
no-checks case has a documented, stable signature — EMPTY stdout, NONZERO
exit, and stderr containing "no checks reported" — and only that combination
is trusted. Any other empty read is ambiguous and gets one retry before
failing closed (never silently defaulting to "no checks"). Critically, the
genuine no-checks case is resolved on the very first read (its stderr always
matches immediately), so a repo that legitimately has zero CI checks
configured (e.g. a quickstart repo) is never made to wait — only an
ambiguous read pays the one retry.

```bash
# See champion-pr-merge.md criterion #6 ("CI Status Check") for the full
# read_ci_checks() definition and rationale (#6211). Summary of its contract:
#   NO_CHECKS="true"    -> confirmed genuine no-checks signature; PASS, merge.
#   NO_CHECKS="false"   -> real checks exist; $CHECKS holds them, evaluate buckets.
#   NO_CHECKS="unknown" -> still ambiguous after one retry; SKIP, do not merge.
read_ci_checks "$PR_NUMBER"
if [ "$NO_CHECKS" = "true" ]; then
  echo "PASS: No CI checks required"
  # Continue to merge
fi
```

**Decision**: **Allow merge** only once the genuine no-checks signature is
confirmed — absence of CI is not a blocker, but an *ambiguous* empty read is
not the same thing as absence of CI and must not be treated as one.

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

**Fixed by #5686** — this used to be a real gap: recency and CI re-running are
necessary but not sufficient, because neither one re-checks whether the
*approval itself* still describes the current tree. `loom:pr` is a verdict
about a specific head SHA, not about the PR as an object.

**Handling** (current): Judge stamps every verdict comment with
`<!-- loom:verdict-sha sha=<head> verdict=approved|changes-requested -->`
(`judge.md` → "Verdict SHA Marker"). `champion-pr-merge.md`'s Verdict-State
Janitor Part 2 runs `./.loom/scripts/verdict-staleness-guard.sh <PR> --clear`
on every `loom:pr` candidate **before** the 6 safety criteria:
- If the marker's SHA still matches the current head (`FRESH`, exit `0`) or no
  marker exists at all — the verdict predates this convention, or the Judge
  dropped the marker (`UNVERIFIABLE`, exit `11`, fails safe) → proceed to the
  safety criteria as before. Since #6319 an unmarked verdict is *anchored* to
  the then-current head by Judge's sweep (`--anchor`) and by the daemon's
  periodic pass, so exit `11` should be rare and short-lived rather than a
  permanent resting state.
- If the head has moved since the verdict was rendered (`STALE`, exit `12`)
  → the guard has already cleared `loom:pr` and re-queued the PR as
  `loom:review-requested` with an auditable old→new-SHA comment. **Do not
  merge**; a fresh Judge pass evaluates the tree that is actually here now.

`loom-daemon`'s `claim_reconciliation::reconcile_pr_verdicts` runs the same
decision as an always-on periodic backstop, independent of any Judge/Champion
pass happening to look at the PR (see `daemon-reference.md` → "Stale-verdict
reconciliation").

**Decision**: **Allow merge only if the approval is still fresh** (or
unverifiable, pre-migration) — a stale approval is never merge-eligible,
regardless of recency/CI.

**This was the "recommended improvement" in a prior revision of this
document** ("Judge should remove `loom:pr` on force-push, not Champion's
responsibility") — it now ships as the Verdict SHA Marker convention plus the
Verdict-State Janitor Part 2 gate.

---

### Edge Case 3b: Merged PR Whose Linked Issue Names an Out-of-Band Verification (#6883)

**Scenario**: a PR merges green and GitHub's `Closes #N` linkage auto-closes the
issue — but that issue's own acceptance criteria include a step CI structurally
cannot perform (a live external source, a real scheduled run, an observation over
time). A green suite is not evidence for such a criterion, yet the close reads as
if it were. In the incident behind #6883 the fix was wrong, its regression test
fabricated the source payload the fix assumed, and the bug survived the close
undetected until a human asked why the item still had not appeared.

**Handling**: `champion-pr-merge.md` → Step 4 → "Out-of-Band Acceptance-Criteria
Gate" runs `./.loom/scripts/classify-ac-verification.sh --issue <N> --pr <PR>
--head-sha <head>` for every linked issue before confirming or forcing its close:

- `0` CLEAR (AC list found, all items CI-checkable) / `10` NO-AC (no
  acceptance-criteria checklist at all) / `11` SATISFIED → close exactly as
  before. **`10` is the common case and must remain a byte-for-byte no-op.**
- `12` UNVERIFIED (out-of-band criterion, no marker) / `13` STALE-MARKER (marker
  names another tree) / `1` ERROR (fails closed) → **hold**: reopen the issue if
  GitHub already closed it, post a comment quoting the unmet criterion verbatim,
  and apply `loom:operator`. The merge itself is never reversed.

**The evidence marker** — the sibling of Edge Case 3's `loom:verdict-sha`:

```
<!-- loom:ac-verified sha=<head> -->
```

`verdict-sha` answers *"which tree does this verdict describe"*;
`ac-verified` answers *"was the out-of-band step actually performed, and against
which tree"*. Posted by whoever performed the step (Builder, issue author,
operator, or a Judge who witnessed a recorded live run — `judge.md` → "Live
Verification and the Circular-Fixture Smell"), on the issue, the PR body, or a PR
comment; Champion searches all three. It satisfies the gate only when its SHA is
the merged `headRefOid` (abbreviation-tolerant), so evidence about a superseded
tree holds exactly like no evidence at all.

**Decision**: **Merge normally, but do not let the issue close on unverified
out-of-band criteria.** The asymmetry is deliberate — a false hold is an open
issue with a comment that a human or a one-line marker clears in seconds; a false
close is a silent, permanent wrong answer.

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

**Action** (single authoritative policy — implemented in `champion-pr-merge.md` → "PR Rejection Workflow → Stale PR"): post the stale notice **once per episode**, guarded by an idempotency marker keyed on `$LAST_ACTIVITY` — the same "most recent commit or non-Champion comment" timestamp the recency check above just computed (`<!-- champion:stale-pr-notice:$LAST_ACTIVITY -->`, mirroring the reject/park markers' own per-episode keying, #6860) — so the 10-minute cron does not spam the PR within one still-stale episode, while a PR that cycles back to `loom:pr` with a new commit or a human/Judge comment and then goes stale *again* gets a fresh notice instead of being silently suppressed forever by a marker from a past episode. **Swap `loom:pr` → `loom:changes-requested`** to route the PR to Doctor for a rebase/refresh. This removes `loom:pr` (unlike the transient-failure path, which keeps it), because a stale PR cannot clear itself and must leave the auto-merge queue. See `champion-pr-merge.md` for the exact commands.

**A merge-risk hold does not exempt a PR from this (#6720), UNLESS the hold is
the PR's only blocker (#6852).** The route fires from a held state too — it is
the only automated path from `loom:pr` to Doctor, and gating it behind the hold
left 20 of 21 held PRs conflicting with Doctor's queue empty. On the held
**hold-plus-feedback** variant (a failing required check, unrelated to the
hold) the `champion:merge-risk-hold` marker is **preserved** and
`loom:operator` is **kept** (the human merge decision is still outstanding);
on the unheld variant `loom:operator` is cleared as before (#5802). On the held
**hold-only** variant (no failing check, nothing else red) the PR is left in
place instead — routing it would not resolve anything the hold isn't already
the resolution for, and `main` moving again would just repeat the cycle (the
"rebase treadmill"). See `champion-pr-merge.md` → "Held-PR Health Pass".

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
| A prior comment carries a `<!-- champion:dep-defer:<fp> -->` marker and re-running `classify-dependency-block.sh --check-defer` returns the **same** fingerprint | **Dependency-Defer Fast Path hard-stop — run this check FIRST, before everything else in this table** | No comment, no claim, no label change, and none of the other rows below are consulted this pass. Only a changed fingerprint or `--check-defer` returning `REEVALUATE` (every recorded blocker has since closed) falls through to the rest of this table. This closes a gap where a pass reaching this table by any path other than the Idempotency-check → escalation call chain (e.g. a body-hash mismatch, or a pass reasoning about the situation ad hoc) could bypass the defer gate below and post a duplicate comment even on an unchanged blocker fingerprint. |
| A prior Champion verdict comment already carries `VERDICT_MARKER` for the issue's **current** title+body hash | **Unrevised since last review — skip** | No comment, no claim, no label change. A genuine title/body edit changes the hash and always produces a fresh marker and a fresh evaluation; comments and label churn do not. |
| Issue already carries `loom:evaluating` and the claim is younger than `LOOM_STALE_EVALUATING_MINUTES` | **Concurrent evaluation in progress** | Skip, do not stomp the claim; continue the batch. |
| Issue already carries `loom:evaluating` and the claim is older than `LOOM_STALE_EVALUATING_MINUTES` | **Stale claim — a prior Champion pass likely died mid-evaluation** | Reclaim (`--add-label "loom:evaluating"` again) then evaluate normally. |
| ≥2 prior "NEEDS REVISION" comments exist, the issue is not already `loom:operator-only`, and the recurring findings are **not** dependency-only | **N=2 threshold reached** | Escalate instead of posting a third+ near-identical rejection: comment with `<!-- champion:proposal-escalated -->` and add `loom:operator-only` (Champion routes, a human decides — the proposal label stays, nothing is closed). |
| ≥2 prior rejections but **every** recurring finding names a dependency *and* cites an open, non-cycle issue/PR | **Timing finding, not a merits finding (#5664)** | **Defer — do not escalate.** `classify-dependency-block.sh --check-defer` returns `DEFER`; no label, no new comment, only a `<!-- champion:dep-defer:<fingerprint> -->` marker PATCHed onto the existing verdict comment. The condition ends when the blocker closes, an event a later pass detects for free. |
| Same, but every recorded blocker has since **closed** | **Stale verdict (#5664)** | `--check-defer` returns `REEVALUATE`; re-run the 8 criteria instead of escalating on a finding that no longer holds. |
| Already `loom:operator-only`, escalation was Champion's own and dependency-only, every recorded blocker now closed | **Self-healing un-escalation (#5664)** | `classify-dependency-block.sh --check-unescalate --apply` removes `loom:operator-only` (and its `loom:operator-blocked` sub-kind label, #5671, if present — never required, since a pre-#5679 escalation carries no sub-label at all) and posts one `<!-- champion:proposal-unescalated:<fingerprint> -->` comment; the proposal rejoins normal evaluation in the same pass. A merits escalation, a `<!-- champion:dep-cycle:` escalation, or a human-applied label is never un-escalated. |
| Blocker is still **open**, but the issue declares a `## Startable Subset` — a part of its work stated to be independent of that blocker | **Sub-issue granularity carve-out ("recurred after closure", #5664)** | Criterion 2 does not fail on this dependency; promote (Step 3) scoped to the declared subset, directing the Builder to `Part of #<issue>` and leaving the remainder for a later pass. Already-parked case: `--check-unescalate` also un-escalates here (`SUBSET_CARVEOUT: yes`) even though the blocker is still open — distinct from the row above, which requires the blocker to have *closed*. |
| Fewer than 2 prior rejections | **Ordinary reject** | Post the `VERDICT_MARKER`-tagged "NEEDS REVISION" comment as before, release `loom:evaluating`. |

**Rationale**: This is the *same* idempotency-marker + escalation-marker + operator-routing shape as Edge Case 5b's Capped-PR Recovery Pass, applied to the proposal-evaluation side of Champion instead of the PR-merge side — a marker keyed to the thing whose change would invalidate it (here, the proposal's own title+body text; there, the latest Judge rejection comment ID) stops duplicate comments, and a bounded escalation threshold (here, N=2 identical verdicts; there, the Doctor-cycle cap) converts an infinite silent loop into a single human-visible routing decision.

**Anchor discipline (#4966)**: neither half of this mechanism may key off the issue's aggregate `updatedAt` — Champion's own verdict comment bumps it, so a marker stamped with it can never match on the next pass and the skip never fires. Content staleness anchors on the title+body hash; claim staleness anchors on the `loom:evaluating` label's own `labeled` timeline event. Both are invisible to Champion's own comment writes. This is the same rule `judge.md`/`daemon-reference.md` already apply to `loom:reviewing`/`loom:treating` staleness.

---

### Edge Case 5d: Capacity-Deferral Comments Re-Posted Every Pass for an Unchanged Backlog (#6729)

**Scenario**: A proposal passes all 8 promotion criteria, but `champion-issue-promo.md`'s "Rate Limiting by Tier" caps the promotion (Tier 3: only 1 per iteration and only if fewer than 5 Tier 3 issues are already in the backlog; Tier 2: up to 2 per iteration). This is a **capacity deferral, not a rejection** — no revision is needed, so Edge Case 5c's `VERDICT_MARKER` is never written for it (capacity deferral is explicitly Step 3, not Step 4). Without a guard, every subsequent Champion pass re-derives the same "criteria pass, tier cap still blocks it" conclusion and posts an equivalent "Tier N backlog cap reached — deferring promotion" comment again — observed live as 10 near-identical comments on #6628 over ~22 hours, and 2-3 more on each of #6647/#6649, all citing the SAME five occupant `tier:maintenance` issues.

**Handling**: `champion-issue-promo.md`'s "Step 3c: Capacity Deferral" mirrors Edge Case 5c's own mechanism, keyed to a **different** anchor because a capacity deferral has no verdict comment to PATCH a marker onto: the **tier and occupant set** that explains the deferral (`classify-capacity-defer.sh`, sourceable pure helpers `normalize_occupants` / `capacity_fingerprint`).

```bash
# FINGERPRINT is a hash of (tier, sorted-unique occupant set) — NOT of the
# issue's own title+body (that hash belongs to Edge Case 5c's unrevised-
# proposal check and answers a different question: "did the PROPOSAL change").
CAP_RC=0
./.loom/scripts/classify-capacity-defer.sh --issue "$ISSUE_NUMBER" \
  --tier "tier:maintenance" --occupants "$OCCUPANTS" --apply || CAP_RC=$?
```

**Decision**:

| `CAP_RC` | Finding | Action |
|---|---|---|
| `1` | `POST_COMMENT` (first deferral on this issue, or the occupant set changed since the last one) | Post the deferral comment, carrying `<!-- champion:capacity-defer:<tier>:<fingerprint> -->` + a seeded `<!-- champion:capacity-defer-seen:<fingerprint>:1 -->` counter |
| `0` | `SKIP_COMMENT` (same tier, same occupant set as the last capacity-deferral comment on this issue) | Post nothing. `--apply` already bumped the existing comment's seen-counter in place |
| `2` | Error (unreadable issue, bad arguments) | Fail toward the pre-#6729 behaviour — post the deferral comment as before |

**Rationale**: Same *shape* of fix as Edge Case 5c/#4954 (a fingerprint anchored to the thing that actually changes suppresses a duplicate-comment loop) but a **different** anchor and **no escalation counterpart** — a capacity deferral is never a merits problem, so unlike the N=2 unrevised-proposal escalation there is nothing here for a human to decide, and the condition ends the instant the backlog composition changes, which the very next pass's fingerprint comparison detects for free without any bounded-streak mechanism. Bounding a state that already self-resolves and never needs a human would only reintroduce the noise this edge case exists to close.

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

### Edge Case 10b: Critical-File Match Is a Durable Hold, Not a Retry (#6879)

**Scenario**: A PR touches a file matching `CRITICAL_PATTERNS` (e.g. a new `.github/workflows/*.yml` job), with no version-only carve-out applicable. Before #6879, this FAIL routed through the same generic "Transient failures — keep `loom:pr`, retry next tick" template as `label-check`/`size-check`/`ci-status`, even though nothing about a diff's critical-file-ness ever clears without a human decision or a force-push that narrows the diff — one fleet PR was re-evaluated and re-rejected ~59 times over 36 hours this way, at 200-300s per tick.

**Handling**: Criterion #3 now mirrors criterion #2's merge-risk-hold pattern (see Edge Case 11b below): on FAIL, comment once behind a `<!-- champion:critical-file-hold -->` idempotency marker and add `loom:operator` (#5502), instead of the shared transient-failure template. Unlike criterion #2, this hold needs no sticky-hold precheck machinery — the pattern match is deterministic, not a judgment call, so there is nothing for a later re-read of the *same* diff to score differently. Release fires the moment a later push narrows the diff so it no longer matches any critical-file pattern: `loom:operator` is removed and a one-time `<!-- champion:critical-file-hold-cleared -->` notice is posted. `loom:auto-merge-ok` does **not** release this hold — it is scoped to criterion #2 only (see Edge Case 11's rationale).

**Decision**: **Durable hold, not a retry** — `loom:operator` joins the PR, and the Held-PR Census (`loom:operator`-keyed) counts it alongside merge-risk holds for free. The exact commands live in `champion-pr-merge.md` → "Safety Criteria → 3. Critical File Exclusion Check → Durable hold on FAIL".

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

### Edge Case 11c: Standing Operator Authorization for a Merge-Risk Class (#6850)

**Scenario**: A repo's operator has repeatedly reviewed the same *class* of PR
— e.g. any PR that only touches `.loom/hooks/guard-*.sh` /
`defaults/hooks/guard-*.sh` — and made the same merge-risk call every time
given Judge approval and green CI. Without a standing-authorization
mechanism, every future PR in that class still needs its own individual
merge-risk judgment (and, if scored red even once, its own individual human
merge to clear the hold) even though the operator has effectively already
decided the policy question for the whole class.

**Handling**: An operator opts a repo in via an optional
`.loom/config.json` → `champion.standingAuthorizations` block (absent by
default — no config, no behavior change):

```json
{
  "champion": {
    "standingAuthorizations": [
      {
        "id": "guard-hooks",
        "description": "Guard hook script changes, given Judge approval and green CI",
        "filePatterns": [".loom/hooks/guard-*.sh", "defaults/hooks/guard-*.sh"],
        "conditions": ["judgeApproval", "greenCi"]
      }
    ]
  }
}
```

Criterion #2 consults this config **after** the sticky-hold precheck and the
docs-only fast path (#6134), and **before** the four-axis table: if the PR's
full changed-file list is an exact-glob-subset match against one class's
`filePatterns` **and** every condition in that class's `conditions` array
is mechanically re-verified true (not merely assumed from an earlier
criterion), criterion #2 passes without scoring the four axes — the same
shape as the docs-only fast path, generalized to an operator-declared,
per-repo class instead of a hardcoded, always-safe one.

**Precedence and interactions** (all preserved exactly):

- **Sticky-hold precheck runs first, always.** A standing authorization
  never releases or bypasses an existing hold on a specific PR — only the
  precheck's own release signals do (`champion-pr-merge.md` → "Sticky
  holds").
- **`loom:auto-merge-ok` is untouched and orthogonal.** That label overrides
  a hold already written on *one* PR; a standing authorization instead
  prevents a hold from being written for a whole *class* of PR in the first
  place. Neither reads the other, and this mechanism does not change
  `loom:auto-merge-ok`'s semantics or its critical-file caveat.
- **Fail-safe on malformed config.** A class entry missing `id`,
  `filePatterns`, or `conditions`, or naming a condition outside the closed
  vocabulary (today: `judgeApproval`, `greenCi`), is skipped — that entry
  never authorizes anything, and a malformed entry never disables sibling
  entries in the array. No config, or an empty array, is a full no-op.
- **Criteria #1/#3/#4/#5/#6 still run independently.** A standing
  authorization only ever waives criterion #2's four-axis judgment for a
  matching PR — nothing else. A PR that matches a class but fails, say, the
  critical-file check (#3) or CI (#6) still does not merge.

**Decision**:
- Full changed-file list matches an authorized class's patterns and every
  stated condition holds -> criterion #2 **PASS** without judging the axes;
  name the class `id` in the Step 2 rationale.
- Any file outside every authorized class's patterns, or any stated
  condition does not hold, or the config entry is malformed/unrecognized ->
  fall through to the normal four-axis judgment, unchanged.

**Rationale**: This is additive to, not a replacement for, the existing
conservative bias — "unsure on any axis -> HOLD" still applies to any PR
that does not cleanly match an authorized class. It gives an operator a way
to retire a whole class of recurring, already-decided merge-risk judgments
without weakening review for anything outside that class, addressing the
monotonically-growing held-PR backlog documented in #6848.

**Action** (single authoritative policy — implemented in
`champion-pr-merge.md` → "Safety Criteria → 2. Merge-Risk Judgment →
Standing operator authorization (#6850)"): see that section for the config
schema, the matcher, and the condition vocabulary.

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
| Proposal already recorded as dependency-deferred, evaluated again by a pass that did not walk the Idempotency-check → escalation call chain | Dependency-Defer Fast Path (run FIRST, before the Idempotency check or anything else) | Recorded `<!-- champion:dep-defer:<fp> -->` fingerprint still matches `classify-dependency-block.sh --check-defer`'s current answer → hard-stop, no comment/claim/label change, continue the batch loop; fingerprint changed or `--check-defer` returns `REEVALUATE` → fall through to the Idempotency check and the rest of the Promotion Workflow unchanged |
| Unrevised proposal re-entering the queue every cycle | Idempotency marker + N=2 escalation | Unrevised since last review → skip silently (title+body hash marker match, never `updatedAt`); ≥2 prior rejections → escalate to `loom:operator-only` instead of a 3rd+ duplicate comment; `loom:evaluating` claim prevents concurrent double-evaluation |
| Proposal whose only recurring finding is an open, non-cycle dependency | Dependency-timing gate (`classify-dependency-block.sh --check-defer`) | **Defer, never escalate** — the condition self-clears when the blocker closes; blocker already closed → re-evaluate rather than escalate on a stale finding (#5664) |
| Proposal stuck at `loom:operator-only` from a dependency-only escalation whose blocker has since closed | Pass 0 self-healing re-scan (`--check-unescalate --apply`) | Remove `loom:operator-only`, post one fingerprinted un-escalation comment, rejoin evaluation the same pass; merits/cycle/human-applied escalations are never touched (#5664) |
| Proposal (parked or not) whose blocker is still open but that declares a `## Startable Subset` covering only part of its scope | Sub-issue granularity carve-out ("recurred after closure", #5664) | Criterion 2's dependency finding does not fail the whole issue; promote scoped to the declared subset (`Part of #<issue>`), or un-escalate an already-parked one via `--check-unescalate` (`SUBSET_CARVEOUT: yes`) even while the blocker stays open — `detect-startable-subset.sh` |
| Proposal passes all 8 criteria but the tier's rate limit blocks promotion this pass | Capacity-deferral idempotency (`classify-capacity-defer.sh`, #6729) | **Post the deferral comment only on first deferral or a changed occupant set** — same tier + same occupant set as the last capacity-deferral comment → skip silently, bump the existing comment's seen-counter in place; no escalation counterpart, the condition self-resolves the moment the composition changes |
| Test-only changes | Allow | Standard criteria apply |
| Human holds PR (removes `loom:pr`) | Skip | Not a merge candidate without `loom:pr` |
| Multiple linked issues | Allow | Verify all closed (each is gated independently by the out-of-band AC gate below) |
| Linked issue's AC names a live-source / real-run / over-time step, with no `loom:ac-verified` marker at the merged head | **Merge, but hold the issue's close** (#6883) | `classify-ac-verification.sh` exit `12`/`13`/`1` → reopen if GitHub auto-closed, comment quoting the unmet criterion verbatim (idempotent `<!-- champion:ac-hold pr=<N> sha=<head> -->`), add `loom:operator`. Exits `0`/`10`/`11` close exactly as before — a body with no AC checklist is a byte-for-byte no-op |
| Mixed-state CI | Fail on `fail`/`cancel` | `pending` defers; `skipping` is OK |
| Unknown critical file | Miss | Needs pattern update |
| Critical file matched (no version-only carve-out) | **Durable hold, not a retry** (#6879) | Comment once behind `<!-- champion:critical-file-hold -->`, add `loom:operator`, keep `loom:pr`; releases (removes `loom:operator`, posts `<!-- champion:critical-file-hold-cleared -->`) only when a later push no longer matches any critical-file pattern — `loom:auto-merge-ok` does not release it |
| Large but low-risk PR (e.g. mostly tests) | Allow | Judged on the 4 risk axes, not line count |
| Small but high-blast-radius PR | Hold for human | Comment names the specific concern, keep `loom:pr`, retry next tick |
| Prior merge-risk hold, later tick scores the same diff green | **Hold stands (sticky)** | Skip silently, post nothing (anti-spam guard already covers it). Release only on `loom:auto-merge-ok`, an explicit operator clearing comment after the hold (leading-clause instruction, not a negation or a question), a new head SHA, or a new Judge review |
| Prior merge-risk hold released, PR merges | Allow + **mandatory reversal comment** | Pre-merge comment carries `<!-- champion:merge-risk-hold-cleared -->` naming the override honored or the axis that flipped and why; never suppressed by the hold idempotency guard |
| `loom:auto-merge-ok` present | Allow | Explicit human/Judge override of a merge-risk hold (does not waive critical files); a *previously posted* hold still requires the reversal comment |
| PR matches an operator-declared standing authorization class (`champion.standingAuthorizations`, #6850) and every stated condition holds | Allow | Criterion #2 passes without the four-axis judgment; runs after the sticky-hold precheck and docs-only fast path, orthogonal to `loom:auto-merge-ok`; no config present = unchanged behavior |
| API rate limit | Error | Comment and continue |
| Multiple approvals | Allow | Label is source of truth |
| Follow-on indicators found | Create | If thresholds met |

---

## Complete Auto-Merge Workflow Script

**The auto-merge workflow lives in a single source of truth: [`champion-pr-merge.md`](champion-pr-merge.md).**

This file previously carried a second, full copy of the end-to-end merge script. That duplicate diverged from `champion-pr-merge.md` over time (it lacked Step 5.5 Follow-on Issue Creation and repeated the same bugs — invalid `gh pr checks --json` fields, etc.), forcing every fix to be applied twice. It has been removed to eliminate the drift (issue #3781).

For the authoritative, end-to-end implementation — the Verdict-State Janitor (run before all else, resolves a contradictory `loom:pr` + `loom:changes-requested`/`loom:review-requested` state fail-safe, #4570/#7018), the 6 safety criteria, the pre-merge comment, the squash merge via `merge-pr.sh`, linked-issue closure verification, dependent-issue unblocking, and Step 5.5 Follow-on Issue Creation — see **`champion-pr-merge.md`**. The edge cases and decision matrix above remain here as the reference for non-standard situations; they describe *behavior*, and defer to `champion-pr-merge.md` for the *script*.

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
