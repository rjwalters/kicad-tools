# Sweep — PR-set wave lifecycle (Mode C only)

> **Reference file for [`sweep.md`](sweep.md)**, the `/loom:sweep` dispatcher.
>
> **Load when:** **Mode C only** (`--prs`, or a PR-side NL trigger). Mutually exclusive with `sweep-wave-lifecycle.md` — never load both for one run.
>
> **Flat, one level deep.** `sweep.md` names every file a given run needs up
> front; nothing here requires opening a *third* file to follow its own
> procedure. Cross-references worded "above"/"below" that do not resolve inside
> this file point at a sibling `sweep-*.md` section — see the reference-file map
> in [`sweep.md`](sweep.md). The body below is **verbatim** from the pre-split
> `sweep.md` (#7726): no rule, warning, edge case, table, or code block was
> reworded, softened, reordered, or dropped.

## Contents

- [PR-set Wave Lifecycle (Mode C only)](#pr-set-wave-lifecycle-mode-c-only)
  - [C0. Per-PR pre-flight (before any role dispatch)](#c0-per-pr-pre-flight-before-any-role-dispatch)
  - [C1. Per-PR routing by current label](#c1-per-pr-routing-by-current-label)
  - [C2. Merge (per PR)](#c2-merge-per-pr)
  - [C3. Wave settled → advance to next PR](#c3-wave-settled--advance-to-next-pr)
  - [Mode C summary output](#mode-c-summary-output)

---

## PR-set Wave Lifecycle (Mode C only)

If Mode C was selected, the wave lifecycle is the **back half** of the issue-side lifecycle: **no Curator, no Approval gate, no Builder**. Each PR is routed by its current label to Judge, Doctor→Judge, or Merge directly.

> **Stage skip is explicit and load-bearing for Mode C.** The issue-side "MANDATORY: do not skip any stage" rule applies to the **issue** lifecycle. For an existing open PR, the Curator and Builder stages already ran (the PR exists, so the issue was implemented). Re-running them would be incorrect and wasteful. Mode C's wave lifecycle is the symmetric counterpart that handles the post-Builder phases without touching the front half.

For each PR `P` in the candidate list, processed sequentially one PR per wave (size-1 waves):

### C0. Per-PR pre-flight (before any role dispatch)

```bash
# Plain `gh` — NOT "$GH_READ". This read routes the PR to Judge / Doctor /
# Merge, so it must observe a concurrent Judge's or Champion's just-written
# label. See "Cached forge reads (`gh-cached`)" for the uncached carve-outs.
gh pr view P --json number,state,labels,closingIssuesReferences \
  --jq '{number, state, labels: [.labels[].name], closes: [.closingIssuesReferences[].number]}'
```

Apply the following skip rules (each "skip" logs the reason; the PR does NOT contribute to any further phase; advance to the next PR):

| Condition | Action | Reason |
|-----------|--------|--------|
| `state != OPEN` (MERGED or CLOSED) | skip | PR is not open; nothing to do |
| Has `loom:blocked` | skip | Operator-flagged; do not act |
| Has none of `{loom:review-requested, loom:changes-requested, loom:pr}` | skip | No actionable label — Mode C only handles these three states |
| Has two or more of `{loom:review-requested, loom:changes-requested, loom:pr}` simultaneously | skip | Conflicting state; human-attention case |
| Has `loom:operator-only` **and** `loom:operator-mechanical`, and this worker holds **every** capability the PR body's `<!-- loom:capability=<name> -->` marker(s) declare | **do not skip** | Capability-aware mechanical lane (#6893). Route through the normal label branch (C1a/C1b/C1c) but in **propose mode**: the phase produces the exact commands / a PR description for an operator to approve, never a live credentialed action. See "Capability-aware `loom:operator-mechanical` lane" |
| Has `loom:operator-only` **and** `loom:operator-mechanical`, but a declared capability is **not** held | skip (comment first) | Post the capability request naming what is missing (lane section below), then log `PR #P: skip — mechanical, missing capability <name>` |
| Has `loom:operator-only` (any other case) | skip | Operator-only PR; do not act. Unchanged — the two rows above are the only carve-out, and they fail closed |
| Has `loom:needs-capability` | skip | Blocked on a missing tool/agent capability, not operator-by-right (#5817); do not act. Explicitly **not** part of the capability lane |

**`loom:reviewing`/`loom:treating` are claim *overlays*, not one of the three state labels the "two or more" conflict-skip row above counts (Issue #6167).** A PR carrying `loom:review-requested` **and** `loom:reviewing` together (a Judge has claimed it and is mid-review — or died mid-review) still has exactly **one** of `{loom:review-requested, loom:changes-requested, loom:pr}`, so it does not hit the conflict-skip row and routes normally to **C1a**. A *stale* `loom:reviewing` next to an actionable state label is therefore **recoverable, not a human-attention case**:

- judge.md's own "Stale `loom:reviewing` Claim Check" (Step 2, before claiming in C1a) reclaims it inline the moment a Judge is actually dispatched for that PR.
- The sweep-start orphan-recovery pass (`recover-orphaned-shepherds.sh --recover` under the `all` sentinel — see "Build-everything sentinel" below) now also reclaims stale `loom:reviewing`/`loom:treating` claims proactively, across the whole PR set, before any PR-specific Judge/Doctor is even dispatched — closing the gap where a dead Judge's claim on a PR nobody happens to re-visit could otherwise sit unrecovered indefinitely (observed on kicad-tools #4791/#4792, ~36h stale). Doctor's `loom:treating` claim label is the identical overlay for `loom:changes-requested`/C1b and is handled the same way.

Determine the **closing issue number** (used for checkpoint scope below) from `closingIssuesReferences`. This is the GitHub-native `Closes/Fixes/Resolves #N` parser (matches the convention used by the issue-side pre-flight via `closedByPullRequestsReferences`). Record up to one closing issue number per PR:

- **0 closing issues** → no checkpoint scope for this PR. Log a warning at PR start (`PR #P lacks a Closes #N reference; skipping per-issue checkpoint for this PR`) and proceed without checkpointing. Mid-phase resume after a kill will not be available for this PR — Judge / Doctor / Merge will simply re-run from scratch on the next sweep, which is acceptable since the operations are idempotent at the GitHub-state level (Judge re-runs if `loom:review-requested` is still set; Merge re-runs only if the PR is still open and labeled `loom:pr`).
- **1 closing issue** → use that issue number `N` as the checkpoint key. The existing `./.loom/scripts/sweep-checkpoint.sh` is keyed by issue number (#3373) and is reused as-is. **Read the existing checkpoint** before dispatching Judge:
  ```bash
  CHECKPOINT_PHASE=$(./.loom/scripts/sweep-checkpoint.sh phase N)
  ```
  If `CHECKPOINT_PHASE == "merge-done"`, the closing issue was already merged in a previous sweep — skip this PR with `already merged (per checkpoint)` and delete the stale checkpoint.
- **2 or more closing issues** → log all closing issue numbers and skip checkpointing (multi-closing PRs are uncommon; a follow-up issue can add a multi-key checkpoint variant if needed). Proceed with Judge/Doctor/Merge as normal.

### C1. Per-PR routing by current label

Apply exactly one of the three branches below, based on the PR's current label:

#### C1a. `loom:review-requested` → Judge phase only

- Load and follow the instructions in `.claude/commands/loom/judge.md` for this PR.
- Dispatch `loom-judge` as a **single subagent Task** from this orchestrator session. Do **NOT** invoke `/loom:sweep` or `/loom:judge` slash-commands as subagents — see "CRITICAL: One level deep" in the Execution Model.
- If a previous Judge attempt for this PR died mid-flight without a fresh checkpoint (rate limit, crash), re-verify forge state and complete only the missing steps before re-dispatching — see "Mid-phase-death recovery" in the Wave Lifecycle (the rule is phase-generic; Mode C inherits it, same as the Doctor-cycle cap).
- Expected exit states:
  - **Approve** → PR labeled `loom:pr` by Judge. If a closing-issue checkpoint is in scope, write `judge-done`:
    ```bash
    # Append --model <resolved> when you passed a model param to the judge subagent (#3482).
    ./.loom/scripts/sweep-checkpoint.sh write N judge-done --task-id "$RUN_ID" --pr-number P
    ```
    Continue to **C2 (Merge)** for this PR.
  - **Request changes** → PR labeled `loom:changes-requested` by Judge. If a closing-issue checkpoint is in scope, write `judge-rejected` **before** entering C1b, so an interrupted sweep resumes at Doctor rather than repeating this completed Judge pass:
    ```bash
    ./.loom/scripts/sweep-checkpoint.sh write N judge-rejected --task-id "$RUN_ID" --pr-number P
    ```
    Continue to **C1b (Doctor → Judge)** for this PR (inline Doctor → Judge cycle(s), up to `sweep.max_doctor_cycles`, matching the issue-side cap).

#### C1b. `loom:changes-requested` → inline Doctor → Judge (up to `sweep.max_doctor_cycles` cycles)

If the PR entered the wave already labeled `loom:changes-requested` (e.g., from a previous Judge run), or just transitioned there from C1a, run inline Doctor → Judge cycles for this PR — **up to `sweep.max_doctor_cycles`** (default 1; see "Doctor-cycle cap" in the Execution Model):

- Load and follow the instructions in `.claude/commands/loom/doctor.md` for this PR.
- Dispatch `loom-doctor` as a **single subagent Task** from this orchestrator session. Do **NOT** invoke `/loom:sweep` or `/loom:doctor` slash-commands as subagents — see "CRITICAL: One level deep".
- If a previous Doctor attempt for this PR died mid-flight without a fresh `doctor-done` checkpoint (rate limit, crash), re-verify forge state (pushed commit? already re-labeled `loom:review-requested`?) and complete only the missing steps rather than duplicating the pushed fix — see "Mid-phase-death recovery" in the Wave Lifecycle (inherited here, same as the Doctor-cycle cap).
- **Model escalation (#3481)**: Mode C inherits the issue-side rule unchanged — this Doctor is dispatched because of a `loom:changes-requested` rejection, so resolve its model per "Model escalation on Judge rejection" in the Execution Model: pass `ladder[1]` from `sweep.escalation` (default ladder: `opus`, resolved through `resolve-model.sh` to `claude-opus-5` — #3982) via the Task tool's `model` parameter, **unless** a tier-1/tier-2 pin applies (pins win) or escalation is disabled (`[]`/`false`). The pinned ID degrades to its alias on this Task-tool dispatch — run it through `resolve-model.sh --task-alias` (see "Pinned-ID degradation on Task-tool dispatch", #4282).
- Doctor addresses the judge feedback, commits the fixes, pushes, and re-labels the PR `loom:review-requested`.
- If a closing-issue checkpoint is in scope, write `doctor-done` (with the attempt counter and the model the Doctor actually ran on — escalated or pinned, #3482) **before** the follow-up Judge:
  ```bash
  # <attempt> is the cycle index + 1: 2 for the first Doctor cycle, 3 for the second, etc.
  ./.loom/scripts/sweep-checkpoint.sh write N doctor-done --task-id "$RUN_ID" --pr-number P --attempt <attempt> --model <doctor-model>
  ```
- Re-dispatch `loom-judge` for the PR (now `loom:review-requested` again).
- Expected exit states:
  - **Approve** → PR labeled `loom:pr`. Write `judge-done` checkpoint (if in scope), continue to **C2 (Merge)**.
  - **Request changes again, cap not yet reached** (`sweep.max_doctor_cycles > 1`) → if a closing-issue checkpoint is in scope, write `judge-rejected` (with `--attempt` matching the value the **next** `doctor-done` write will use) before running the next Doctor → Judge cycle for this PR (incrementing `--attempt`), up to the configured cap:
    ```bash
    ./.loom/scripts/sweep-checkpoint.sh write N judge-rejected --task-id "$RUN_ID" --pr-number P --attempt <next-attempt>
    ```
  - **Request changes again, cap reached** → PR labeled `loom:changes-requested`. **Do NOT run another Doctor** — mark this PR as blocked (log `PR #P blocked: doctor cycle exhausted after <k> Doctor→Judge round(s); human attention required`), advance to the next PR in the candidate list. Do NOT block the rest of the candidate list on it. **Do NOT write a `judge-rejected` checkpoint for this terminal rejection** — leave the last checkpoint (`doctor-done`) as-is for the stale-checkpoint cleanup path. **Distinct-defect exception (default cap only):** when `max_doctor_cycles` is at its default of 1 and this second rejection is a demonstrably distinct defect from the first, you MAY grant exactly one additional bounded cycle (single-use per PR, log `PR #P: granted one extra Doctor cycle — second rejection is a distinct defect (<short reason>)`) — see "Doctor-cycle cap". If granted, this is a "cap not yet reached" case per the bullet above — write `judge-rejected` with the matching `--attempt` before the grace cycle. Same-defect / ambiguous still blocks (no grace, no `judge-rejected` write).

This configurable cap matches the issue-side Wave Lifecycle §6 — Mode C inherits the same rule (and the same default-cap distinct-defect exception) for the same reason (bounds worst-case latency, prevents Judge/Doctor disagreement loops).

#### C1c. `loom:pr` → Merge phase only

If the PR entered the wave already labeled `loom:pr`, skip Judge and Doctor entirely — the PR has already been judged. Continue directly to **C2 (Merge)**, subject to the two gates below.

**First, check for an operator hold (#6398).** `loom:operator` (`.loom/docs/label-state-machine.md`) means "the engine will not work this item further; a human is the only transition out" — most commonly Champion's merge-risk hold (`champion:merge-risk-hold`), posted alongside the label. The `labels` array C0 already fetched for this PR carries this — no extra call needed. If it includes `loom:operator`, **do not continue to Merge**: log `PR #P: skip — held by loom:operator (human required)` and advance to the next PR in the candidate list, leaving `loom:pr` and `loom:operator` untouched (the hold is re-evaluable, per the label-state-machine doc, so the next sweep re-checks it — but the engine itself never overrides it). This check runs regardless of the verdict-staleness outcome below; `verdict-staleness-guard.sh` clears a *stale-SHA* verdict, not an operator hold (by design — the guard explicitly does not clear `loom:operator`, `loom:operator-only`, or `loom:blocked`), so it does not substitute for this check.

**Second, confirm the approval still describes THIS tree (#5686).** "Already judged" is a claim about a specific head SHA, and a `loom:pr` label survives a rebase or force-push that replaced every commit it was rendered against. Mode C is the one merge path that does not run `champion-pr-merge.md`'s Verdict-State Janitor, so run the same gate here before skipping review:

```bash
./.loom/scripts/verdict-staleness-guard.sh P --clear
VERDICT_RC=$?
```

| Exit | Meaning | Action |
|------|---------|--------|
| `0` (FRESH) / `11` (UNVERIFIABLE, no marker — pre-#5686 verdict, fails safe) | The approval stands | Continue to **C2 (Merge)** as today. |
| `12` (STALE) | The approval covers a tree that is gone. The guard has already cleared `loom:pr`, re-queued the PR as `loom:review-requested`, and commented naming both SHAs. | **Do not merge.** Log `PR #P: stale approval cleared (head moved) — routing to Judge`, then process this PR through **C1a** (`loom:review-requested` → Judge) on this same pass. |
| `10` / anything else | No verdict label, or a `gh`/environment error | **Do not merge.** Log and skip this PR; the next sweep re-evaluates it. |

### C2. Merge (per PR)

Use the dedicated merge script (CLAUDE.md "Merging PRs" mandate — never `gh pr merge`):

```bash
./.loom/scripts/merge-pr.sh P --auto
```

The script merges via the forge API and cleans up the worktree. `--auto` enables GitHub's server-side auto-merge queue (queues the merge until required checks pass); on PRs that are already in `CLEAN` state, the script transparently falls back to an immediate merge — see #3371. **On a repo with GitHub auto-merge disabled** (`allow_auto_merge:false`), `merge-pr.sh` now detects the setting up front and degrades `--auto` gracefully to wait-for-checks-then-merge (immediate if already CLEAN) instead of failing (#3820) — so you can pass `--auto` uniformly regardless of the repo's auto-merge setting; no per-repo branching is needed here. On a repo that additionally has **no GitHub Actions workflows configured** (so the Checks API itself 404s for every SHA), the degraded wait path now distinguishes that persistent-404 shape from a transient fetch blip and proceeds straight to the synchronous merge instead of polling to `LOOM_AUTO_MERGE_TIMEOUT` (#6389) — the "pass `--auto` uniformly" guidance above still holds without qualification.

**On successful merge** (script returns 0):
- If a closing-issue checkpoint is in scope, delete it:
  ```bash
  ./.loom/scripts/sweep-checkpoint.sh delete N
  ```
- Advance to the next PR in the candidate list.

**On merge failure** (script returns non-zero):
- Classify `<reason>` through "Forge write failure diagnosis (#6425)" above **before** writing the log line — do not assert a permission/credential diagnosis without running `forge_write_permission_confirmed` and getting positive evidence. Log `PR #P merge failed: <reason>` using that section's vocabulary (`forge-transient: …`, `permission fault not confirmed — will retry`, or the confirmed-and-cited form).
- Do **NOT** delete the checkpoint — leave it at `judge-done` (or earlier) so the next sweep retries.
- Advance to the next PR in the candidate list (do not block the rest of the list).

### C3. Wave settled → advance to next PR

Mode C waves are size-1, so "wave settled" is synonymous with "this PR reached a terminal state (merged, blocked, or skipped)". Advance to the next PR in the candidate list and repeat from C0. Do not parallelize PRs (sequential per-PR processing is load-bearing — see "CRITICAL: One level deep" in the Execution Model).

### Mode C summary output

When the entire PR list has been processed, print a per-PR summary:

```
/loom:sweep --prs complete. Processed M PR(s):

  PR #200  → merged                                                                  [judged, merged]
  PR #201  → blocked (judge requested changes after doctor cycle exhausted)          [judged, doctor, judged]
  PR #202  → merged  (was already loom:pr; no judge or doctor)                       [merge-only]
  PR #205  → merged  (rate-limited (resumed: doctor TOKEN_EXHAUSTED mid-phase — fix already pushed, re-labeled + re-judged))  [judged, doctor, judged, merged]
  PR #206  → rate-limited (unresumable: judge TOKEN_EXPIRED mid-phase, human attention required)  [judged]
  PR #203  → skipped (no actionable label)                                           [pre-flight skip]
  PR #204  → skipped (PR already merged)                                             [pre-flight skip]

Total: 3 merged, 1 blocked, 2 skipped, 1 rate-limited (unresumable).
```

`rate-limited (...)` here carries the same meaning as in the issue-set Summary Output (see "`rate-limited` vs `blocked`" there): the reason reuses `TOKEN_EXPIRED` / `TOKEN_EXHAUSTED` / `MODEL_CREDITS_EXHAUSTED` from `.loom/scripts/lib/classify-error.sh`, a `resumed:` or `downgraded:` outcome already succeeded (via mid-phase-death recovery and the credit-exhaustion model fallback respectively), and only an `unresumable:` outcome needs a human — distinct from `blocked (...)`, which means the work itself failed. Mode C inherits the credit-exhaustion fallback unchanged: a Judge or Doctor killed by `MODEL_CREDITS_EXHAUSTED` at C1a/C1b is re-dispatched one rung down for the same PR, same attempt, without consuming a Doctor cycle. Mode C likewise inherits the spend-limit fallback unchanged: a Judge or Doctor killed by the `"spend limit"` signature at C1a/C1b is re-dispatched for the same PR, same attempt, with the `model` param omitted first (see "Spend-limit fallback" above), without consuming a Doctor cycle.

