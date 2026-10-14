# Champion: Targeted and Non-Item Modes (#11859)

**Load when:** `$ARGUMENTS` carries `--pr N`, `--issue N` or `--non-item`
(`champion.md` → "Argument Handling"). With no arguments Champion runs the batch
pass in `champion.md`, unchanged; nothing in this file applies to it.

The step dispatcher hands Champion **one item per step**: one merge decision on
one PR, or one promotion verdict on one issue. The interval run keeps the work
that has no item to draw (`--non-item`). The criteria, holds, markers and
scripts are the batch pass's own; this file only says which of them a mode
runs, on what, and how the step ends.

## Parsing

| Arguments | Mode |
|---|---|
| `--pr N` | **PR merge step**: one merge decision on PR `N` |
| `--issue N` | **Issue promotion step**: one promotion verdict on issue `N` |
| `--non-item` | **Non-item pass**: every queue except curated issues and `loom:pr` PRs |
| anything else, or two of the above | Refuse: print `Error: /loom:champion takes --pr N, --issue N or --non-item` and stop. Touch nothing |

`N` must be a positive integer. A targeted step **skips "Finding Work"** and the
batch-wide passes (Pass 0b, 0c, the Held-PR Census and digest): those belong
to `--non-item` and the batch pass. `loom:frozen` on the item: report it and
stop, with no comment and no label change.

**Every mode ends with one line, last:**
`CHAMPION-STEP mode=<pr|issue|non-item> item=<N|-> outcome=<outcome>`. The
outcome vocabulary for each mode is the table in its section. Never invent one.

## `--pr N`: one merge decision

1. **Kind probe, eligibility and the pin file.** One plain (uncached) read:

   ```bash
   PR_NUMBER=N
   PR_JSON=$(gh pr view "$PR_NUMBER" --json url,state,headRefOid,labels) || PR_JSON=""
   EVALUATED_SHA=$(printf '%s' "$PR_JSON" | jq -r 'select((.url | test("/pull/")) and .state == "OPEN" and any(.labels[]; .name == "loom:pr") and (any(.labels[]; .name | startswith("loom:operator")) | not)) | .headRefOid // empty')
   PIN_FILE="$(git rev-parse --git-common-dir)/loom-champion-pin-pr-$PR_NUMBER"; rm -f "$PIN_FILE"
   [ -n "$EVALUATED_SHA" ] && printf '%s\n' "$EVALUATED_SHA" > "$PIN_FILE"
   echo "EVALUATED_SHA=${EVALUATED_SHA:-none}"
   ```

   `none` (not a `/pull/` URL, not `OPEN`, no `loom:pr`, or `loom:operator`,
   #11861): outcome `not-eligible`, no writes. `loom:pr` + `loom:blocked`
   (Champion's own hold) is eligible. (A Doctor-capped PR is the non-item pass's
   Priority 5, not a merge.)
2. **Pin the head you evaluate.** The printed `EVALUATED_SHA` is the head every
   criterion below is judged against. **Shell variables do not survive between
   tool calls**, so when you run Step 3 of `champion-pr-merge.md`, its block's
   first line must be `EXPECTED_HEAD=<that SHA, written out literally>`. Step 3
   then uses it as the merge-queue `--approved-sha` and passes
   `merge-pr.sh --expected-head`. The pin file from step 1 is the fail-closed
   backstop: while it is under 60 minutes old, a Step 3 run whose
   `EXPECTED_HEAD` is missing or different prints `CHAMPION-PIN-MISSING` and
   merges nothing. **Last action, whatever the outcome:**
   `rm -f "$(git rev-parse --git-common-dir)/loom-champion-pin-pr-N"`
3. **Run `champion-pr-merge.md` for this one PR, as one batch row.** The
   Verdict-State Janitor (Parts 1 and 2), all 6 Safety Criteria with the merge-risk
   axes and sticky holds, the critical-file hold and, when a hold binds, the
   Held-PR Health Pass for this PR only. Then Steps 2, 3, 4, 5 and 5.5
   (pre-merge comment, merge, issue close, dependents, follow-on issues). A
   single-PR invocation **exits where the batch would `continue`**.
4. **Outcome:**

| Outcome | When |
|---|---|
| `merged` | Step 3 printed `CHAMPION-MERGE-OUTCOME pr=N rc=0` |
| `held` | Champion's hold (`loom:blocked`) binds (Health Pass ran), or Janitor Part 1 skipped another's park |
| `rejected` | A criterion failed and the PR Rejection Workflow ran (Janitor, stale route, CI) |
| `requeued` | Exit 3, 4, 5, 7, 9 (`CHAMPION-PIN-MISSING`): not merged, still approved, no "Merge Failed" |
| `head-moved` | `merge-pr.sh` exit **8**: the head is no longer `EVALUATED_SHA`. Not a failure: post no "Merge Failed", leave the labels; the next step evaluates the new head |
| `failed` | Exit 1 (Error Handling's "Merge Failed" comment), or no `CHAMPION-MERGE-OUTCOME` line (its "No exit code at all") |
| `not-eligible` | Step 1 |

**Never re-read the head and retry the merge** inside the step: the retry is
the next step, after a fresh evaluation.

## `--issue N`: one promotion verdict

1. **Kind probe and eligibility:** `gh issue view N --json url,state,body,labels`. A
   `/pull/` URL, a failed read, or a closed issue: outcome `not-eligible`. It
   must carry a proposal label (`loom:curated`, `loom:architect`, `loom:hermit`
   or `loom:auditor`) and neither `loom:issue` nor `loom:building`; otherwise
   `not-eligible`; so is `loom:operator`. `loom:blocked` (#11860): when every
   blocker `loom-daemon park-record parse` reads from the body is a closed issue
   or a merged PR, remove `loom:blocked` and go on; else outcome `blocked`.
2. **Run `champion-issue-promo.md`'s per-issue order for this issue only**
   ("Issue Promotion Batch Processing" → "Per-issue order in the loop"): the
   Dependency-Defer Fast Path, the Idempotency check, the Claim with `loom:evaluating`
   (staleness-aware: Pass 0b for this issue), Steps 1-4, and the verdict-time
   recheck. The per-pass level cap and the promotion slice do not apply.
3. **Exactly one outcome**, from the verdict set below.

### Promotion verdict set

**This table is the only list of `--issue` outcomes.** An evaluation is
**terminal** (#11860): no `deferred`, no `skipped`.

| Outcome | Written by | End state |
|---|---|---|
| `promoted` | Step 3 | `loom:issue` added, claim released |
| `revise` | Step 4 | NEEDS REVISION + revise marker; `loom:curated` and the claim removed, `loom:triage` added |
| `blocked` | Dependency-Defer Fast Path, Step 4's `DEFER` or dependency-only failure; step 1 | `loom:blocked` + park record |
| `operator` | Step 4's ranked decision, a dependency cycle | `loom:operator` |
| `closed` | Step 4's premise-false close gate | issue closed |
| `not-eligible` | step 1, a fresh concurrent claim, the verdict-time recheck abort | no writes |

## `--non-item`: the interval run without item queues

The role runner launches this when the root sets
`autonomous.roleRunner.championMode: "non-item"` (daemon-reference.md). Item
work (curated issues, `loom:pr` PRs) arrives as `--issue` / `--pr` steps
instead; this pass must not act on it.

| Runs | Source |
|---|---|
| Pass 0b, Pass 0c (batch-wide repairs) | `champion-issue-promo.md` |
| Priority 3: architect, hermit and auditor proposals | `champion.md`, then `champion-issue-promo.md` |
| Priority 4: epic proposals | `champion-epic.md` |
| Priority 5: Doctor-cycle-capped PRs (Capped-PR Recovery Pass) | `champion-pr-merge.md` |
| Rollout Check Pass | `.loom/docs/rollout-check.md` |
| Held-PR Census and Per-PR Digest (report and the digest issue) | `champion-pr-merge.md` |

**Skipped:** Priority 1 (`pr-queue --role champion`, every merge, the Held-PR
Health Pass and the Verdict-State Janitor) and Priority 2 (the curated query).
Follow-on issues come from a merge's Step 5.5, so they arrive with `--pr`
steps; this pass has no follow-on queue of its own.

**The only writes this pass makes to a `loom:curated` issue** are Pass 0b /
0c's reconciliations of a verdict or claim already decided (clear a stale
claim, land an approved promotion). It never
claims, evaluates or writes a verdict on one, and it writes nothing on a
`loom:pr` PR (the census only reads them).

Outcome: `done`, or `idle` when every queue was empty. The completion report's
`Merge-risk holds:` line is still mandatory.
