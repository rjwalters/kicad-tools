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
| `--pr N` | **PR step**: one merge decision on PR `N`, or one triage when it carries no stage label (#12499) |
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
When the step held a PR lease, release it with the same word before that line:
`loom-daemon lease pr release N --role champion --step champion_merge --watch-pid "${LOOM_AGENT_SESSION_PID:-${CLAUDE_PID:-$PPID}}" --outcome <outcome>`
(#12616: the released lease records it, `outcome=none` if you pass none).

**A `--pr` step that does not merge names its gate (#12715).** On `skipped` and
`requeued`, add `reason=<gate>` to **both** the `CHAMPION-STEP` line and the
release (`--reason <gate>`), from this closed list, so the lease comment and
`pass.verdict` say why instead of guessing:

| `reason=` | Stopped at |
|---|---|
| `facts_unread` | "0. PR facts" could not be read (or comments unauthenticated) |
| `janitor_rc3` | Verdict-State Janitor Part 1 (`champion-hold classify`) exit 3: a foreign park or a failed read |
| `verdict_rc11` | Verdict-State Janitor Part 2 (`verdict-staleness-guard.sh`) exit 11: UNVERIFIABLE approval |
| `ci_pending` | CI checks were still running when you read them (say it only if that is what you saw) |
| `mergeable_unknown` | `mergeable` read `UNKNOWN` (GitHub still computing) |
| `pin_missing` | Step 3 printed `CHAMPION-PIN-MISSING` |
| `guard_refused` | A guard hook refused the step's own pin or PR-facts write or removal (#13180) |
| `merge_rc<N>` | `merge-pr.sh` exited `N` (3, 4, 5, 7 or 9) |

Anything else is recorded as `unspecified`; never write `ci_pending` as a
default.

## `--pr N`: one merge decision

1. **Kind probe, eligibility and the pin file.** One plain (uncached) REST
   read (#12242: `pulls/N` 404s for an issue number, so it is the kind probe too):

   ```bash
   PR_NUMBER=N
   PR_JSON=$(gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER") || PR_JSON=""
   NOT_TRIAGE=$(printf '%s\n' loom:pr loom:review-requested loom:changes-requested loom:sequenced; loom-daemon labels list --property hard_exclusion 2>/dev/null || printf 'external\n'; loom-daemon labels list --property hold --with-retired 2>/dev/null || printf 'loom:blocked\nloom:operator\nloom:frozen\n'; loom-daemon labels list --kind claim 2>/dev/null || printf 'loom:reviewing\nloom:treating\nloom:building\n')
   TRIAGE=$(printf '%s' "$PR_JSON" | jq -r --arg skip "$NOT_TRIAGE" '($skip | split("\n")) as $s | select(.state == "open" and (.draft | not) and (([.labels[].name] - $s) == [.labels[].name])) | "yes"')
   echo "TRIAGE=${TRIAGE:-no}"
   UNTRUSTED_FORK=$(printf '%s' "$PR_JSON" | jq -r '(.author_association // "NONE") as $a | select((.head.repo.full_name // "") != .base.repo.full_name and ((["OWNER","MEMBER","COLLABORATOR"] | index($a)) == null)) | "yes"')
   echo "UNTRUSTED_FORK=${UNTRUSTED_FORK:-no}"
   EVALUATED_SHA=$(printf '%s' "$PR_JSON" | jq -r 'select(.state == "open" and any(.labels[]; .name == "loom:pr") and (any(.labels[]; .name | startswith("loom:operator")) | not)) | .head.sha // empty')
   CHAMPION_KEY=$(git rev-parse --path-format=absolute --git-common-dir | sha256sum | cut -c1-12)
   mkdir -p -m 700 /tmp/loom-champion "/tmp/loom-champion/$CHAMPION_KEY"
   rm -f "/tmp/loom-champion/$CHAMPION_KEY/pin-pr-N"
   [ -n "$EVALUATED_SHA" ] && printf '%s %s\n' "$EVALUATED_SHA" "$PPID" > "/tmp/loom-champion/$CHAMPION_KEY/pin-pr-N"
   echo "EVALUATED_SHA=${EVALUATED_SHA:-none}"
   ```

   Write `N` in the two pin-file paths as the PR number's digits (not `$PR_NUMBER`):
   the worktree-write guard admits only that exact spelling and fails closed on a
   variable path (#13180). A step whose own pin or facts write a guard refused is
   released with `reason=guard_refused`.

   `TRIAGE=yes` (open, not a draft, no stage label, no hold, no claim):
   run **"`--pr N` on an unlabelled PR: triage"** below instead of the rest
   of this section. Otherwise
   `none` (not a PR, not open, no `loom:pr`, or `loom:operator`,
   #11861): outcome `not-eligible`, no writes. `loom:pr` + `loom:blocked`
   (Champion's own hold) is eligible. (A Doctor-capped PR is the non-item pass's
   Priority 5, not a merge.)
2. **Pin the head you evaluate.** `EVALUATED_SHA` is the head every criterion
   below is judged against. **Shell variables do not survive between tool
   calls**: in Step 3 of `champion-pr-merge.md`, the block's
   first line must be `EXPECTED_HEAD=<that SHA, written out literally>` (its
   merge-queue `--approved-sha` and `merge-pr.sh --expected-head`). The pin
   file is the fail-closed backstop: while under 60 minutes old, or while the
   agent process it records still runs (#11902, up to 24h), a Step 3 run without
   that `EXPECTED_HEAD` prints `CHAMPION-PIN-MISSING` and merges nothing.
   **Last action, whatever the outcome:**
   `rm -f "/tmp/loom-champion/$CHAMPION_KEY/pin-pr-N"` (`CHAMPION_KEY` as in step 1)
   (and the PR facts file, `/tmp/loom-champion/$CHAMPION_KEY/facts-pr-N.json`, beside it)
3. **Run `champion-pr-merge.md` for this one PR, as one batch row.** The
   Verdict-State Janitor (Parts 1 and 2), "0. PR facts" (one read for every
   criterion, #12242), all 6 Safety Criteria with the merge-risk
   axes and sticky holds, the critical-file hold and, when a hold binds, the
   Held-PR Health Pass for this PR only. Then Steps 2, 3, 4, 5 and 5.5
   (pre-merge comment, merge, issue close, dependents, follow-on issues). A
   single-PR invocation **exits where the batch would `continue`**.
4. **Outcome:**

| Outcome | When |
|---|---|
| `merged` | Step 3 printed `CHAMPION-MERGE-OUTCOME pr=N rc=0` |
| `held` | Champion's hold (`loom:blocked`) binds (Health Pass ran), or Janitor Part 1 / Step 3 exit 10 (`CHAMPION-HOLD-SKIP`) skipped a park |
| `rejected` | A criterion failed and the PR Rejection Workflow ran (Janitor, stale route, CI) |
| `skipped` | A stop **before Step 2** (facts unread, Janitor exit 3 / 11, `mergeable` UNKNOWN): nothing evaluated far enough to merge, no writes. Carries `reason=` |
| `requeued` | A Step 3 exit 3, 4, 5, 7, 9 (`CHAMPION-PIN-MISSING`): not merged, still approved, no "Merge Failed". Carries `reason=` (`merge_rc<N>`, `pin_missing`, or `ci_pending` when CI was running) |
| `head-moved` | `merge-pr.sh` exit **8**: the head is no longer `EVALUATED_SHA`. Not a failure: post no "Merge Failed", leave the labels; the next step evaluates the new head |
| `failed` | Exit 1 (Error Handling's "Merge Failed" comment), or no `CHAMPION-MERGE-OUTCOME` line (its "No exit code at all") |
| `not-eligible` | Step 1 |

**Never re-read the head and retry the merge** inside the step: the retry is
the next step, after a fresh evaluation.

## `--pr N` on an unlabelled PR: triage

The step engine sends Champion an open, non-draft PR with no stage label and
no hold (#12499): hand-opened, or a role stopped before labelling it. Look at
it once and make **exactly one label transition**, so the PR flows through the
normal state machine and never comes back here.

**Outside contributions first.** `UNTRUSTED_FORK=yes` (step 1: the head is a
fork, or a deleted one, **and** `author_association` is not `OWNER`, `MEMBER`
or `COLLABORATOR`, `comment-trust.md`) is **always `triage-operator`, never
`triaged`**: a maintainer decides whether an outside contribution enters the
automated review-to-merge path. No label marks it (`external` is applied by
humans), and nothing after triage checks the author, so this row is the guard.

Otherwise, read the PR's body and
branch, the issue it closes (`Closes #N`, `feature/issue-N`) and the other
open PRs on that issue. Do not review the code: that is Judge's.

| Outcome | When | Write |
|---|---|---|
| `triaged` | **The normal case**: none of the rows below applies | `gh pr edit N --add-label loom:review-requested` |
| `triage-blocked` | A **duplicate** of another open PR on the same issue, **superseded** (its change is already on the default branch, or a newer PR replaces it), or it **closes an issue that is already closed** | **Duplicate / closes an already-closed issue**: a comment naming the reason and the PR or issue it points at, then `loom-daemon park-record apply --pr N --by champion --reason "<duplicate of #M or closes #K, already closed>"` (it adds `loom:blocked`; a reason-only park, so the PR is not released back here when #M closes). **Superseded, confirmed**: the superseder is a PR that `gh pr view M --json state,mergedAt` shows **merged** into the default branch, or a commit that `git merge-base --is-ancestor <sha> origin/<default>` shows is on it; read it live, then comment `Superseded by #M` (or `by <sha>`), then `gh pr close N`. **Superseded, not confirmed** (superseder still open, unreadable, or only "looks similar"): park it with `--reason "superseded (unconfirmed): <what you could not verify>"` |
| `triage-operator` | `UNTRUSTED_FORK=yes`, whatever else applies; or it **needs a human decision** that an agent may not make ("Routing to a human" in `label-state-machine.md`), e.g. it carries a change no issue asks for and only the operator can rule on its scope | A ruling: `loom-daemon operator-decision apply N --input d.json` (2-4 ranked options; for an outside contribution, admit it to review or close it), which adds `loom:operator` itself. Only a human-hands step: a comment opening `Operator reason: <class> — <one line>`, then `gh pr edit N --add-label loom:operator` |
| `not-eligible` | Re-read the labels just before the write: a stage label, hold or claim appeared (a peer moved it) | No writes |

When in doubt between `triaged` and the others, choose `triaged`: Judge
reviews it, and Judge can still reject it. The one exception is
`UNTRUSTED_FORK=yes`, which is never `triaged`. Close a PR only when its
supersession is confirmed (the `triage-blocked` row); never push to it or
add `loom:pr`.

## `--issue N`: one promotion verdict

1. **Kind probe and eligibility:** `gh issue view N --json url,state,body,labels`. A
   `/pull/` URL, a failed read, or a closed issue: outcome `not-eligible`. It
   must carry a proposal label (`loom:curated`, `loom:architect`, `loom:hermit`
   or `loom:auditor`) and neither `loom:issue` nor `loom:building`; otherwise
   `not-eligible`; so is `loom:operator`. `loom:blocked` (#11860, #12078): the
   work finder draws a curated, dependency-parked issue every time it comes up,
   without checking the blockers first, so this check is the only one. Re-read
   them now: when every blocker `loom-daemon park-record parse` reads from the
   body is a closed issue or a merged PR, remove `loom:blocked` and go on.
   Otherwise leave the issue exactly as it is (no comment, no label change,
   no park-record edit) and end with outcome `blocked`; the daemon spaces the
   next draw.
2. **Run `champion-issue-promo.md`'s per-issue order for this issue only**
   ("Issue Promotion Batch Processing" → "Per-issue order in the loop"): the
   Dependency-Defer Fast Path, the Idempotency check, the Claim with `loom:evaluating`
   (staleness-aware: Pass 0b for this issue), Steps 1-4, and the verdict-time
   recheck.
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

The role runner launches every interval and idle-edge Champion run this way
(#11909). Item work (curated issues, `loom:pr` PRs) arrives as `--issue` /
`--pr` steps; this pass must not act on it.

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
claims, evaluates or writes a verdict on a curated proposal (epics are
Priority 4's), and it writes nothing on a `loom:pr` PR.

Outcome: `done`, or `idle` when every queue was empty. The completion report's
`Merge-risk holds:` line is still mandatory.
