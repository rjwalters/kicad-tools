# Merge-Queue CI Qualification

**Issue #10257** (Phase C of #9978). With GitHub's merge queue, Loom would
enqueue a Judge-approved PR rather than re-dating and merging it directly. That
only protects `main` if every suite Loom relies on validates the **combined
merge-group tree**, meaning the base plus every PR queued ahead of this one. A
workflow can list `merge_group` as a trigger and still skip the suites that
matter on it. This document covers the read-only tooling that proves a
repository's workflows are qualified, the prerequisites for enabling queue mode,
and how to roll back.

**Status:** this is preparation only. Nothing in Loom enqueues yet: Phases A
and B, #10255 and #10256, provide the queue controls and the Champion handoff.
`rjwalters/loom` is user-owned, and GitHub documents merge queues for
organization-owned repositories, so this repository is **not eligible** for the
live pilot. Its `ci.yml` carries a `merge_group` trigger anyway. The trigger is
dormant here because GitHub only emits the event on a queue-enabled branch, and
keeping it in place keeps the workflow qualified and audited.

## `loom-daemon merge-group-ci`

Both verbs are read-only. Neither verb enqueues, edits a ruleset, or changes
branch protection, and neither writes to any repository.

| Verb | Exit 0 | Exit 1 | Exit 2 |
|---|---|---|---|
| `audit` | every relied-on suite and required context covers `merge_group` | a finding | could not run |
| `eligibility` | the repository may pilot queue mode | a named prerequisite failed | could not run |

```bash
loom-daemon merge-group-ci audit                     # this checkout; required = .loom/config.json
loom-daemon merge-group-ci audit --required "Gate" --workflow ci.yml --json
loom-daemon merge-group-ci eligibility --repo acme/widgets --root ../widgets
loom-daemon merge-group-ci eligibility --facts facts.json   # offline
```

### What `audit` checks

The audit relies on a workflow when it hosts a required context or runs on
every pull request, meaning a `pull_request` trigger with no `paths:` filter. A
path-filtered PR workflow is listed as advisory: a skipped required check never
reports, so such a workflow cannot be a required gate. The audit evaluates each
job and step of a relied-on workflow under synthetic `pull_request`, `push` and
`merge_group` contexts with a small GitHub-expression evaluator. A value the
evaluator cannot know statically, such as `matrix`, `steps`, or a `needs`
output of a job that actually ran, counts as **unknown**, and an unknown
counts as uncovered.

| Code | Meaning |
|---|---|
| `MISSING_MERGE_GROUP_TRIGGER` | relied-on workflow has no `merge_group` trigger, or its `types:` excludes `checks_requested` |
| `PR_ONLY_CONDITION` | a job's `if:` is false on `merge_group` (`github.event_name == 'pull_request'`, …) |
| `SKIPPED_DEPENDENCY` | a job `needs:` a job skipped on `merge_group` and has no status function (`!cancelled() && …`) |
| `PATH_FILTER_SKIP` | a job/step runs on `merge_group` only if a path-filter job's output says so |
| `UNDETERMINED_CONDITION` | the condition cannot be decided on `merge_group` |
| `PR_ONLY_STEP` | a step of a covered job runs on PRs but is skipped on `merge_group` |
| `NON_MERGE_GROUP_CHECKOUT` | `actions/checkout` `ref:` resolves to something other than the merge-group commit |
| `CANCELLING_CONCURRENCY` | `cancel-in-progress` is (or may be) true on `merge_group` |
| `SHARED_CONCURRENCY_GROUP` | the group is not unique to the merge-group commit, so a pending run can be superseded |
| `MISSING_REQUIRED_SUITE` | a required context matches no job name (matrix names are expanded) |
| `REQUIRED_SUITE_UNCOVERED` | a required context's job is not covered on `merge_group` |
| `UNPARSEABLE_WORKFLOW` | a workflow could not be read, so it cannot count as covered |

The output records each workflow's and job's `permissions:` for review, but
permissions do not produce findings.

A job that exists only to trim PR runs, such as `ci.yml`'s `changes`
path-filter job, declares itself with a marker comment inside the job block:

```yaml
  changes:
    # merge-group-audit: pr-only -- path groups exist only to trim PR runs
```

The marker takes the job out of the relied-on set. The audit never reports a
marked job as covered, and it still audits a marked job that hosts a required
context.

### What `eligibility` checks

`eligibility` gathers the repository's facts with `GET` reads only, through the
counted gh facade, or loads them from `--facts`. It reads `repos/{nwo}` for the
owner type and permissions, and `repos/{nwo}/rules/branches/{branch}` for the
effective active rules. It then runs `audit` against the branch's **actual**
required checks. Every prerequisite fails closed: a fact the credential cannot
see becomes a named `*_UNKNOWN` failure, never a pass.

| Prerequisite | Failure codes |
|---|---|
| Organization-owned repository | `OWNER_NOT_ORGANIZATION`, `OWNER_TYPE_UNKNOWN` |
| Credential can push (needed to enqueue) | `INSUFFICIENT_PERMISSION`, `PERMISSION_UNKNOWN` |
| Active `merge_queue` rule on the branch | `NO_MERGE_QUEUE_RULE`, `MERGE_QUEUE_UNKNOWN` |
| Required status checks exist | `NO_REQUIRED_CHECKS` |
| Each required check covers `merge_group` | `MISSING_REQUIRED_SUITE`, `REQUIRED_SUITE_UNCOVERED` |
| No other audit finding | `WORKFLOW_NOT_QUALIFIED` |

A GitHub App installation token (`ghs_…`, the fleet's usual credential) gets
an all-`false` `permissions` object, even on a repository it can read. The
check reads that as `PERMISSION_UNKNOWN`, not as a denial. To get a real
answer, run `eligibility` with a credential whose permissions are visible, or
record `can_push` in a `--facts` file.

The `--facts` file has the shape
`{"repository","owner_type","branch","can_push","rules":[{"type","ruleset_id","parameters"}]}`.
A missing or `null` field means "unknown".

## How `ci.yml` is qualified

- `on.merge_group: types: [checks_requested]` is added. The `push` and
  `pull_request` triggers are unchanged.
- Every path-filtered job's condition admits `merge_group` unconditionally:
  `github.event_name == 'push' || github.event_name == 'merge_group' ||
  needs.changes.outputs.<group> == 'true'`. A merge group therefore runs the
  full suite. `changes` stays PR-only and carries the marker.
- The image jobs (`worker-base-image` and the three image smokes) are the one
  exception on `push` (#10825, [ci-principles](ci-principles.md) rule 3).
  Their condition puts `github.event_name == 'merge_group' ||` first, so a
  merge group still runs them unconditionally, while a push runs them only
  when the push-only `changes-push` job finds an image input or fails.
  `changes-push` never runs on a PR or a merge group, so the audit does not
  count it as a relied-on suite.
- The concurrency group for a merge-group run is keyed on
  `merge_group.head_sha`, so it can never share a group with a PR run, with
  `main`, or with another merge group. `cancel-in-progress` stays true only
  for `pull_request`, so a started merge-group run is never cancelled
  ([ci-principles](ci-principles.md) rule 2).
- The PR-only steps that guard the merged tree also run on `merge_group`, over
  `merge_group.base_sha..head_sha`. These are the version-bearing-file check
  in `Structural Checks` and the secret scan in `Daemon Checks`.
- No checkout sets `ref:`, so every checkout lands on `github.sha`, which is
  the merge-group head.
- Regression tests in `loom-daemon/src/merge_group_ci/tests.rs` audit this
  repository's own workflows against `.loom/config.json`'s required checks.
  They also assert that every job still runs on `push` and may still run on a
  PR. The exceptions are named explicitly: `changes-push` (push-only), and the
  four image jobs, which must not be `Skipped` on push and must `Run` on
  `merge_group`.

## Enablement prerequisites (all required, in order)

1. Phases A and B (#10255, #10256) have landed: the queue controls are present
   and the Champion handoff preserves the safety invariant.
2. `loom-daemon merge-group-ci eligibility --repo <org>/<repo>` exits 0 for the
   pilot repository. That requires an organization owner, push permission, an
   active `merge_queue` rule, required checks, and a clean audit.
3. **Operator authorization.** Adding a `merge_queue` rule, changing required
   checks, or transferring a repository are protected settings changes. No
   agent makes them, and nothing in this tooling attempts them.
4. Record the direct-mode baseline before the switch. The baseline covers
   approval-to-merge median and p90, re-date pushes, head-induced Judge
   re-reviews, CI runs and runner-minutes per merged PR, and red-main
   incidents.

## Rollback to direct mode

Direct mode is always available. To roll back:

1. Switch the repository's merge mode back to direct with the Phase A setting.
   The Champion then returns to `merge-pr.sh`.
2. Have the operator disable or remove the `merge_queue` rule.
3. Leave the `merge_group` trigger in place. It is inert without a queue and
   keeps the workflow audited.

## #9990 tier split (fast PR / full integration): evaluation

#9990 proposed a fast PR tier (`pull_request`) and a full integration tier
(`merge_group`). The data below is from this repository's `CI` workflow, read
on 2026-10-04 with the Actions API. It covers the last 200 completed runs per
event and the job lists of the last 30 successful runs per event.

| Event | Wall time p50 / p90 (success) | Jobs per run (median) | Jobs skipped (median) | Runner-minutes per run (median / p90) |
|---|---|---|---|---|
| `pull_request` | 7.3 / 11.4 min | 26 | 4 | 70.0 / 73.2 |
| `push` (main) | 8.3 / 13.9 min | 26 | 1 (`changes`) | 85.9 / 89.1 |

The findings:

- **The PR tier is already the "fast" tier.** Path filtering skips a median of
  4 of 26 jobs and saves about 16 runner-minutes (about 18%) per PR run. The
  wall-time difference against a full run is about 1 minute at p50. Trimming
  PR suites further would buy little time and would delay failure discovery
  from the PR to the queue, where a failure ejects the entry and costs a queue
  cycle. **No PR suite is removed.** Every job that runs on a PR today still
  runs there.
- **The integration tier is the full suite.** `merge_group` runs exactly what
  `push` runs, so the combined tree gets the same coverage `main` does.
- **Queue limits do not combine builds.** Each queue entry gets its own
  `merge_group` run on its own temporary branch. Queue settings such as
  `max_entries_to_build` and grouping bound concurrency and what merges
  together. They do not reduce Loom's per-PR run count, and this design does
  not assume they do.
- **Expected cost shape.** Per merged PR, queue mode adds one full
  `merge_group` run of about 86 runner-minutes and removes the re-date reruns
  that direct mode pays whenever `main` moves under an approved PR. Whether
  that nets out positive is the pilot's question. GitHub fast-forwards `main`
  to the merge-group head, so the `push` run on `main` re-tests an identical
  SHA. Removing that `push` run would be a separate, evidence-gated change and
  is **not** made here: `main` verification stays (rule 2).
- **Interaction to watch in the pilot:** `version-bump-on-merge.yml` pushes
  directly to `main`, at most once per `RELEASE_MIN_INTERVAL` (24 h by
  default, #11174; see [`release-cadence.md`](release-cadence.md)). In a
  queue-enabled repository, each such push moves the base under every queued
  entry. The pilot repository must
  either route that bump through the queue or measure the rebuild churn it
  causes.

## Still open: the live pilot (out of band)

Merging this tooling does not complete #10257. The remaining acceptance
criteria need an eligible organization-owned pilot repository and explicit
operator authorization. Evidence for each item is recorded with Loom's
`loom:ac-verified sha=<head>` convention.

- [ ] Select the pilot repository and record its queue, ruleset and check
  prerequisites, using `eligibility --json` as the record.
- [ ] Live: individually green but mutually incompatible PRs fail
  combined-tree validation and never reach `main`.
- [ ] Live: queued approval revocation, a head change, and a hold each confirm
  the Phase B safety invariant, recorded with timestamps and SHAs.
- [ ] One pilot week compared against the direct-mode baseline. Report the
  metrics listed under the prerequisites above, plus removals by reason and
  the study's limitations.

## Related

- [ci-principles](ci-principles.md): the started-run and check-evidence rules
  merge-group runs inherit (rules 2 and 6).
- [hyperparameters](hyperparameters.md): the pointer to these prerequisites
  and the rollback steps.
- #9978 (parent), #10255 (Phase A), #10256 (Phase B), #9990 (the original
  proposal).
