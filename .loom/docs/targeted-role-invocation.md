# Targeted role invocation (`/loom:<role> <N>`, Issue #11863)

A **targeted** invocation hands a role one item number instead of letting it
discover its own work: `/loom:judge <PR>`, `/loom:curator <issue>`,
`/loom:builder <issue>`, `/loom:doctor <PR>`. The daemon already invokes
Doctor (#10632) and Judge (#10630) this way through the per-repository lanes
in `role_runner/lanes.rs`. Step dispatch (one role session moves one item one
stage) will invoke every item role this way. That makes the targeted path a
daemon contract, not just an operator convenience, and this doc is the
contract:

- the **gates** each role runs, in order, before its first write;
- the **outcome classes** a step ends in, and how the daemon tells them apart.

The role prompts carry the gates inline (they must run before anything else
loads). This doc carries the reasoning, the outcome vocabulary, and the audit.

## Gates, in order, before the first write

| Role | 1. Pool gate | 2. Kind probe | 3. State and skip labels | 4. Claim + lease | Stale-claim check |
|---|---|---|---|---|---|
| Curator | `gh api rate_limit`, min(core, graphql) under ~500 or unreadable → refuse `pool_gate` | one live REST read of `issues/<N>`: `pull_request != null` → refuse `target_is_pr` | same read: `closed`, `loom:frozen`, any `loom:operator*` label, a hard-exclusion label → refuse | `loom:curating`; **no lease yet** (see "Gaps") | `claim-staleness.sh check --label loom:curating` |
| Judge | none (the role runner's pool gate runs before spawn) | one live REST read of `issues/<N>`: `pull_request == null` → refuse `target_is_issue` | same read: `closed` (also merged), `loom:frozen`, any `loom:operator*` label → refuse; verdict label without `loom:review-requested` → `noop already_judged`; no stage label (`loom:review-requested`, `loom:reviewing`, a verdict label; a priority label alone does not count) → Fallback Queue rules (review only, never claim) | `lease pr claim --role judge`, then `loom:reviewing` | `claim-staleness.sh check --label loom:reviewing` |
| Builder | none (as Judge) | `forge check-claim <N>` → `TARGET_IS_PR` | same probe → `CLOSED`, `FROZEN`, `OPERATOR`, `OPEN_PR`, `MERGED_PR`, `TRIAGE`; a hard-exclusion label → refuse | `loom:issue` → `loom:building`; `worktree.sh` runs `lease ensure` | `check-claim`'s `BUILDING` / `LEASE_ALREADY_HELD` / `BRANCH_EXISTS`, unless the invoker holds the claim |

Why each gate sits where it does:

- **Pool gate first** (Curator, #10039). A claim taken when the forge budget
  is nearly spent becomes a claim the session cannot finish, and it then sits
  until the stale-claim check reclaims it 30 minutes later. Judge and Builder
  rely on the role runner's pre-spawn credential gate instead; their claim
  paths spend mostly REST.
- **Kind before claim** (#9929). Issues and pull requests share one number
  space, and `gh issue edit`, the labels API and `gh issue view` all accept
  the other kind silently. An issue role that labels a PR (or a PR role that
  leases an issue) starts a lifecycle that can never close.
- **One read, three answers.** `GET /repos/{owner}/{repo}/issues/<N>` returns
  `pull_request` (kind), `state` and `labels` together. It is REST (the core
  pool, not GraphQL) and live, never cached, because it gates a claim.
  Builder gets the same three answers from `forge check-claim`, whose label
  leg requests `labels,state,url` (the `CLOSED` token was added here).
- **Skip labels refuse without a write.** `loom:frozen` means only a human
  removes it, so a targeted role must not comment on it, relabel it or claim
  it. The same holds for a hard-exclusion label (`hard-exclusion-labels.sh`).
- **A dispatcher-held claim is the invoker's, not a peer's.** A daemon sweep
  flips `loom:issue → loom:building` and publishes a lease before it spawns
  the session, and says so through `LOOM_SWEEP_CLAIM_OWNED=<N>`. An in-session
  `/loom:sweep` publishes the lease at pre-flight. A Builder launched by either
  sees its invoker's claim as `BUILDING` / `LEASE_ALREADY_HELD` /
  `BRANCH_EXISTS` and must continue, not refuse. The signal is mechanical on
  every launch path:
  - daemon sweep (Task subagent or `worker run`): the daemon exports
    `LOOM_SWEEP_CLAIM_OWNED=<N>` and every child inherits it;
  - attended sweep, `worker run --role builder --issue <N>`: the worker is
    launched with `LOOM_SWEEP_CLAIM_OWNED=<N>` when none is inherited
    (`worker_spawn/claim_owned.rs`). Its other readers agree for a
    sweep-owned Builder: `build-gate.sh` arms the dispatch backoff for `<N>`,
    `cpu-budget.sh` excludes the sweep's own count, and the containment
    label names `<N>`;
  - attended sweep, Task subagent: the Task tool cannot set a per-subagent
    environment (#3719), so `sweep-wave-lifecycle.md` step 4 requires the
    brief to say, verbatim, `This /loom:sweep pre-flighted #N and holds its
    claim and lease.`

  The never-overridable tokens (`TARGET_IS_PR`, `CLOSED`, `FROZEN`,
  `OPERATOR`, `OPEN_PR`, `MERGED_PR`, `TRIAGE`) refuse whoever claimed. A step engine that pre-claims a Builder
  step must export the same `LOOM_SWEEP_CLAIM_OWNED=<N>`, or not pre-claim.
- **The step engine pre-takes only the lease for every other role (#11906).**
  Judge, Doctor, Curator and Champion read a fresh claim label as another
  agent's, so for their steps the daemon takes the role-keyed lease alone
  (`lease pr claim --role <role> --step <step>`, under the step's sweep id)
  and the prompt applies its own label. The session's `lease pr claim`
  (no `--step`) resolves the same sweep id (`LOOM_TERMINAL_ID`), and a lease
  is owned by role, host and sweep (the step is metadata), so it re-attaches
  to that lease (exit 0) rather than standing down. A
  lease held by `$LOOM_SWEEP_ID` is the session's own: each prompt says so
  beside its claim, the same override `LEASE_ALREADY_HELD` has for Builder.
- **An operator's explicit word overrides a claim, never a category.** An
  attended `/loom:builder <N>` told to resume or force a claim (the
  operator's own crashed run, say) passes `BUILDING` / `LEASE_ALREADY_HELD` /
  `BRANCH_EXISTS`, exactly as `check-claim --force-claim` does. It never
  passes the seven never-overridable tokens.

### The `loom:operator` skip (#11861)

`loom:operator` is a no-op for every role. Each targeted path refuses it (by
prefix, so the retired `loom:operator-*` names too) in
the same place as `loom:frozen`, writing nothing:

- Curator and Judge: a refusal condition on the one REST read (gate 3);
- Builder: the `forge check-claim` token `OPERATOR`, beside `FROZEN` and as
  never-overridable, so every `check-claim` caller (`worktree.sh` included)
  gets it;
- Doctor: its PR Fix Mode, beside its `loom:frozen` skip.

## Outcome classes

Every targeted step ends in exactly one class. The class decides what the
step-outcome ledger does next, so the four must be told apart:

| Class | Meaning | Ledger effect (#11900) |
|---|---|---|
| `verdict` | The role reached a decision that moved the item: Judge approved or requested changes, Builder opened a PR or parked the issue, Curator curated, parked or closed it | none; the next step comes from the labels |
| `noop` | The role took (or evaluated) the item and found nothing to do | a cooldown per `(item, step)`, of the issue-keyed no-op cooldown's default length (1 h); `ci_pending` does not count toward the repeat cap |
| `refused` | The item was not this step's to act on: wrong kind, wrong state, a skip label, a fresh peer claim, or the pool gate | a short cooldown for `pool_gate` and `claimed`; `claim_lost` redraws (counted). A state refusal re-classifies the item. A second identical state refusal, or a second `claim_lost`, in a row on the same `(item, step)` arms the refusal cooldown, doubling to 24 h. No refusal counts toward the repeat cap |
| `failed` | The step did not complete for a reason unrelated to the item's state: a crash, a probe that could not answer, a claim left held | fast, Builder step: the fast-fail ladder; fast, any other step (Curator, Champion promotion, a PR step): the ledger's own doubling window, never the issue quarantine, whose release restores `loom:issue`; slow: counted toward the repeat cap only |

### How the daemon tells them apart

**The forge post-state is authoritative.** A session's own words never
upgrade a step: a `verdict` claim with no matching label is `failed`, the
same rule `sandbox_noop` and `toolless_launch` apply to an exit-0 run that
did nothing. The table below is what a classifier reads after the session
exits (pre-state = the state the step was drawn on):

| Role | `verdict` when | `noop` when | `refused` when | `failed` when |
|---|---|---|---|---|
| Judge | the PR gained `loom:pr` or `loom:changes-requested` with a verdict-SHA marker at the current head; or, for a fallback PR, a `loom:fallback-evaluated` marker at the head | no verdict and no claim left behind, with a `noop` outcome line | nothing changed and the pre-state fails gate 2 or 3, or a stand-down marker was posted | `loom:reviewing` or a live Judge PR lease is still held after exit; anything else |
| Builder | an open PR linked to `<N>` with `loom:review-requested` was created during the step; or the issue was parked (`loom:blocked` / `loom:operator`) or closed | `.loom/worktrees/issue-<N>/.no-changes-needed` exists (the sweep's own signal, #6670) | nothing changed and `forge check-claim` would refuse the pre-state | `loom:building` is still held with no PR and no marker |
| Curator | the issue gained `loom:curated`, was parked or was closed, and `loom:curating` is gone | `loom:curating` is gone and nothing else changed, with a `noop` outcome line | nothing changed and the pre-state fails gates 1 to 3, or a stand-down marker was posted | `loom:curating` is still on the issue after exit |

**The outcome line separates what the forge cannot.** `refused` and `failed`
both leave the forge untouched, and a `pool_gate` refusal is invisible in the
item's own state. So the session ends with one line:

```
LOOM_STEP_OUTCOME role=<judge|curator|builder> target=<N> class=<verdict|noop|refused|failed> reason=<token>
```

It is advisory: a classifier uses it only to choose between classes the
post-state already allows, never to claim a `verdict` the forge does not show.
A missing line on an unchanged item is `failed` with reason `unclassified`.

### Reason tokens

| Class | Token | Roles | Source |
|---|---|---|---|
| `verdict` | `approved`, `changes_requested`, `fallback_reviewed` | Judge | `post-verdict.sh`; the fallback marker |
| `verdict` | `pr_opened`, `parked`, `closed` | Builder | `create-pr.sh`; `park-record apply`; "Issues Are Suggestions" |
| `verdict` | `curated`, `parked`, `closed` | Curator | Decision Tree; Routing to a human; Admission keep-or-close |
| `noop` | `already_judged`, `ci_pending`, `fallback_skip` | Judge | gate 2; Primary Queue step 2b; `judge-fallback-guard.sh` exit 10/11/12 |
| `verdict` | `fixed` | Doctor | a pushed fix; the PR returns to review |
| `noop` | `no_fix_needed` | Doctor | the PR needed no change |
| `noop` | `no_changes_needed` | Builder | the `.no-changes-needed` marker |
| `noop` | `nothing_to_add` | Curator | the issue already meets the Quality Checklist and was curated before |
| `refused` | `target_is_pr`, `target_is_issue` | issue roles, PR roles | gate 2 |
| `refused` | `closed`, `frozen`, `hard_excluded`, `open_pr`, `merged_pr`, `triage`, `branch_exists` | as gate 3 applies | gate 3; `check-claim` tokens |
| `refused` | `claimed`, `claim_lost`, `head_moved` | all | a fresh peer claim or lease; Judge's verdict-time CAS recheck |
| `refused` | `pool_gate` | Curator | gate 1 |
| `failed` | `probe_failed`, `crashed`, `unclassified` | all | a gate read that could not answer (`check-claim` exit 5, a failed REST read); everything else |

A Builder `noop` needs one more thing the step engine must do itself: the
sweep releases `loom:building` back to `loom:issue` and records the no-op
(`record-noop-release.sh`) when it finds the marker. A Builder step run
without a sweep around it gets neither unless the engine does both.

**Who posts the reason comment (#13054).** For a Builder step the daemon does,
not the Builder prompt (a cut-short Builder never reaches the prompt): when
the step engine records a Builder step as `noop` or `failed`,
`SweepRegistry::note_builder_outcome` posts one comment tagged
`<!-- loom:builder-step-outcome (#13054) sweep=<id> -->` with the outcome's
reason token and, for a `noop`, the first 200 characters of
`.no-changes-needed` when the worktree still has it. It is keyed by the step's
sweep id, so one step never comments twice. The `/loom:sweep` orchestrator
posts its own comment on its path (`sweep-wave-lifecycle.md`); the two paths
do not overlap, because a sweep-run Builder is not a recorded step.

## Audit (2026-10-14, at `6b6f2074f`)

| Role | Gate | Before this change | Now |
|---|---|---|---|
| Curator | pool gate before claim | present (Argument Handling step 1) | unchanged |
| Curator | kind probe | present, via `gh issue view --json url` (GraphQL) | one live REST read that also answers state and labels |
| Curator | closed / frozen / hard-excluded | checked only in "Finding Work", which the targeted path skips | refused on the same read |
| Curator | claim + lease | `loom:curating` only; no lease exists | unchanged (see "Gaps") |
| Curator | stale claim | `claim-staleness.sh` | unchanged |
| Judge | kind probe | **absent**: an issue number reached `lease pr claim` and `gh pr edit` | refused on one REST read |
| Judge | closed / frozen | `loom:frozen` checked only in discovery | refused on the same read |
| Judge | stage | none: a targeted Judge claimed and labelled an unlabelled PR, which the Fallback Queue forbids; the lanes assign fallback rows too | fallback PRs follow the Fallback Queue rules; a judged PR is a no-op |
| Judge | claim + lease, stale claim | `lease pr claim` + `loom:reviewing`, stale check | unchanged |
| Builder | kind, frozen, open PR, peer claim | **absent**: the targeted path claimed straight away, skipping the guard the "Finding Work" path runs | `forge check-claim` first, with the invoker's own claim allowed |
| Builder | closed | absent everywhere, `check-claim` included | `check-claim` token `CLOSED`, never overridable |
| Builder | claim + lease | label claim; `worktree.sh` leases | unchanged |
| all three | outcome reporting | exit status only; the role runner sees success or failure | this doc's classes, forge post-state rules and outcome line |

## Gaps left to other work

- **The Curator prompt does not take its lease yet.** #11898 added the
  Curator lease: `loom-daemon lease pr claim <N> --role curator` (marker
  `<!-- loom:pr-lease role=curator host=… sweep=… step=curator -->` when a step
  claims it). It is role-keyed, like the Judge and Doctor leases, rather than
  an issue-path `loom:lease`, because every issue-path reader assumes one
  claim per number: a live Curator lease would read as a peer to the Builder
  that follows it. The step engine's spawn (#11906) takes it for a daemon
  Curator step (`LOOM_SWEEP_CLAIM_OWNED` is a Builder step's alone). An attended or role-runner
  `/loom:curator <N>` still claims by label, with `claim-staleness.sh` and
  `stale_after_minutes` (`loom:curating`, 30) as the liveness, until its
  prompt is taught the lease.
  A dead daemon step does not strip such a label-only claim (#11969): its
  release reads a claim label applied more than 60 s after the step started,
  and outside the step's own lease, as another claimant's and writes nothing.
  The same holds for an attended Doctor, which also claims by label alone.
- **The classifier.** The step-outcome ledger (#11900,
  `loom-daemon/src/step_dispatch/outcome_ledger.rs`) parses
  `LOOM_STEP_OUTCOME` and `CHAMPION-STEP`, applies the post-state rule
  (`classify`) and maps each class to its effect. The step spawn (#11906)
  reads a step's last `LOOM_STEP_OUTCOME` line from its log into its end
  record and role-tick capture. The work finder's step tick computes each
  role's post-state from the claim release's own read, before its restore
  write (`step_dispatch/step_end.rs`, #11909).
- **Doctor** was out of this audit's scope (it was already targeted). Its PR
  Fix Mode has no kind probe: it should take gate 2 of the Judge row.
- **The Judge lane assigner** hands fallback rows to `/loom:judge <PR>`. The
  prompt now handles them review-only, so this is safe, but each such run
  spends a session on a comment.

## Verification

Statically: each role's targeted section was read against the four gates
above, and `defaults/scripts/tests/test-targeted-role-invocation.sh` pins the
gates and their order in all three prompts and their generated skills.
`cargo test -p loom-daemon forge_check_claim` covers the `CLOSED` token.
Against the live forge, read-only: `forge check-claim` on a closed issue and
on a PR number, and the REST probe on an issue, an open PR and a merged PR.
A full targeted step per role on a scratch repository was **not** run
in-session: it needs a daemon-spawned agent with forge write access to a test
repository.
