---
name: "optimize-ci"
description: "Audit GitHub Actions for wasted CI minutes — change-relevance path filtering (required-check safe), cache keys, superseded runs; ranked by measured savings, report-only unless --apply"
domain: repo
type: command
user-invocable: true
---

# /repo:optimize-ci — Stop CI Doing Work That Cannot Matter

Find the three ways CI routinely wastes runner minutes, prove how much each one
costs from real run history, and propose the fix:

1. **No change-relevance filtering** — full suites on documentation-only PRs,
   heavy jobs on changes that cannot affect them. And the opposite hazard: an
   existing `paths:` filter that omits a real input (a lockfile, the workflow
   file itself, a local action), which lets a breaking change merge green.
2. **Weak or missing caching** — no toolchain cache at all, or a cache keyed so
   it never hits (`github.sha` in the key) or never invalidates (no lockfile
   hash in the key).
3. **Superseded runs** — no `concurrency:` / `cancel-in-progress`, so every push
   to a PR leaves the previous run burning minutes; or duplicate `push` +
   `pull_request` triggers running the same commit twice.

Modeled on [[deps]]: a report-first audit of repo configuration that proposes
changes and gates writes. The default mode and `--check` never write. `--apply`
writes proposed workflow edits on a branch and opens a PR, under the same
authorization convention as [[deps]] and [[release]]: apply existing
authorization when it covers the concrete edits, otherwise show the diff and
confirm first. Never push to the default branch.

The deterministic half — workflow parsing, trigger/paths/cache/concurrency
extraction, required-check lookup, run-timing aggregation — is
`scripts/repo/repo-optimize-ci.py` in the source checkout, installed to
`.claude/skills/repo/scripts/repo-optimize-ci.py`. Every GitHub call it makes is
a GET. This file is the judgment half: verifying what the helper inferred,
choosing the right fix shape, and writing it.

## Usage

```
/repo:optimize-ci                 # Report findings for this repo, ranked by estimated savings
/repo:optimize-ci --check         # Report only — never writes (same as the default)
/repo:optimize-ci --apply         # Write proposed workflow edits on a branch and open a PR — confirm first
/repo:optimize-ci --all-repos [--owner OWNER]   # Read-only org survey, repos ranked by wasted CI minutes
```

`--all-repos` surveys many repos instead of the invoking one and is **always**
report-only — see "Fan-out survey" below.

## Prerequisites

GitHub Actions only (v1). Confirm before doing anything else, and if either
check fails say so and stop — GitLab CI, Gitea Actions, and CircleCI are out of
scope:

```bash
git config --get remote.origin.url    # → OWNER/REPO; must be a GitHub host
ls .github/workflows/*.y*ml           # at least one workflow file
gh auth status
```

`python3` (3.9+) is required for the helper; it needs nothing outside the
standard library.

## 1. Gather the evidence

```bash
python3 .claude/skills/repo/scripts/repo-optimize-ci.py report --json
```

`report` reads the local checkout's workflows, then (read-only):

- **Required status checks** for the default branch — the union of classic
  branch protection and every ruleset that applies to the branch
  (`repos/O/R/rules/branches/<default>`, which includes org-level rulesets).
  Classic checks come from `repos/O/R/branches/<default>`, whose `protected`
  flag and `protection.required_status_checks` are visible with read access.
  The dedicated `.../protection/required_status_checks` endpoint is only a
  fallback: it answers **404 to every non-admin token even when checks are
  required**, so its 404 means "none" only when the branch reports
  `protected: false`; on a protected (or unreadable) branch it is `unknown`.
  With no readable source, `requiredChecks.state` is `unknown` — never report an
  unreadable source as "no required checks". When one source returned checks
  but another was unreadable, `requiredChecks.partial` is `true`: any workflow
  matching none of the known checks is still treated as possibly required
  (job-level filter only), and an existing workflow-level PR `paths:` filter
  gets an `unverified-required-check-paths` warning to verify by hand.
- **Run history** over the last 30 days (`--days`, `--runs` to widen): runner
  minutes per workflow and per job (sampled from the jobs API), which PR runs
  changed only documentation files (from each PR's file list), which PR runs
  kept running after a newer push to the same branch, and which `push` runs
  duplicated a `pull_request` run of the same commit. If Actions history is
  unreadable or empty, `history.state` is `not measured` and every finding
  still appears, just unranked by minutes — this never fails the command.

Human-readable output (drop `--json`) is the report skeleton in step 4. Use
`scan --root . [--required CONTEXT]... [--required-unknown]` for a fully
offline run.

## 2. Verify what the helper inferred — before recommending anything

The helper's heuristics are deliberately conservative, but they are heuristics.
Check each finding against the repo before it goes in the report:

- **Documentation-only filtering.** The helper's doc-only set is `docs/**`,
  `README*`, `CHANGELOG*`, `CONTRIBUTING*`, `LICENSE*`, issue/PR templates — not
  `**/*.md`, because in a prompt or skill repo markdown *is* the product. Before
  proposing any `paths-ignore:`, grep the jobs' scripts and test suites for the
  candidate files: a README-layout test, a doc linter, a link checker, or a
  generated-docs check means that file is an input and must not be ignored.
  Drop the finding (and say why) if the evidence line shows ~0 docs-only runs —
  a filter that never fires is complexity with no payoff.
- **Inferred inputs** (for under-filtering). The helper counts the workflow file
  itself, root-level manifests/lockfiles for each toolchain it detects in the
  steps (node, python, rust, go, docker), and any `./` local action or reusable
  workflow the jobs call. It does not see monorepo sub-package lockfiles or
  shared directories like `lib/` that the build reads implicitly — read the
  job's commands and add those yourself.
- **Required-check matching.** A job's check context is its `name:` (or job id),
  with ` (matrix values)` appended for matrix legs and `caller / callee` for
  reusable workflows. Confirm the helper's match by eye when names contain
  `${{ }}` expressions.

## 3. Choose the fix shape

### Path filtering — the required-check rule is absolute

A workflow-level `on.pull_request.paths` / `paths-ignore` filter skips the
**whole workflow**, and a skipped workflow reports **no status at all**. If any
of its jobs is a required status check, every PR that touches none of those
paths is blocked forever waiting for a status that will never arrive.

So: **never recommend workflow-level `paths:` on a workflow that contains a
required check — or when required checks could not be (fully) read.** Recommend the
job-level pattern instead:

```yaml
jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      code: ${{ steps.filter.outputs.code }}
    steps:
      - uses: actions/checkout@v4
      - uses: dorny/paths-filter@v3
        id: filter
        with:
          filters: |
            code:
              - 'src/**'
              - 'package.json'
              - 'package-lock.json'
              - '.github/workflows/ci.yml'
  test:
    needs: changes
    if: needs.changes.outputs.code == 'true'
    # ...unchanged...
  ci-gate:                       # ← make THIS the required check
    if: always()
    needs: [changes, test]
    runs-on: ubuntu-latest
    steps:
      - run: |
          [[ "${{ contains(needs.*.result, 'failure') || contains(needs.*.result, 'cancelled') }}" == "false" ]]
```

A skipped *job* reports success, so the gate passes on docs-only PRs and fails
on a real failure. When migrating, the required-check list must switch from
the old job names to the gate in the same change window — say so in the PR.

A workflow with **no** required check may take a plain workflow-level
`paths-ignore:` instead; that is simpler and is the right recommendation there.
This repo's own `.github/workflows/ci.yml` (the comment above its `test` job,
issue #73) is prior art for choosing visible-but-not-required checks — note the
tradeoff when proposing to make a gate required.

For **under-filtered** workflows, the fix is always additive: put the missing
inputs into `paths:` (or drop them from `paths-ignore:`).

### Caching — per toolchain

| Toolchain | Preferred fix |
|---|---|
| node (npm/pnpm/yarn) | `actions/setup-node` with `cache: npm\|pnpm\|yarn` (keys on the lockfile) |
| python (pip/poetry/pipenv) | `actions/setup-python` with `cache:` + `cache-dependency-path:` the lockfile; or `astral-sh/setup-uv` (cache on by default — don't disable it) |
| rust | `Swatinem/rust-cache` after the toolchain step (keys on `Cargo.lock` + toolchain) |
| go | `actions/setup-go` default cache (keys on `go.sum`) — remove `cache: false` |
| docker | `docker/build-push-action` with `cache-from: type=gha` / `cache-to: type=gha,mode=max` (or a registry cache) |

For a hand-rolled `actions/cache`: key = `${{ runner.os }}-<tool>-${{ hashFiles('<lockfile>') }}`,
plus `restore-keys: ${{ runner.os }}-<tool>-`, plus `${{ matrix.* }}` in the
key for matrix jobs. The helper reports cache findings as `not measured`
(hit/miss rates need job logs); if the job-minutes context makes one worth
quantifying, fetch a recent job log with `gh run view --log --job <id>` and
count `Cache restored` / `Cache not found` lines.

### Wasted runs

Recommend exactly this, and nothing that cancels default-branch runs:

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```

An existing unconditional `cancel-in-progress: true` on a workflow that also
runs on default-branch pushes is itself a finding: a burst of merges cancels
intermediate default-branch runs, so some merged commits never get a completed
run. For duplicate `push` + `pull_request` triggers, restrict `on.push.branches`
to the default branch.

## 4. Report

Findings ranked by estimated minutes saved over the sampled window, critical
required-check hazards always first:

```
CI OPTIMIZATION REPORT — OWNER/REPO (3 workflows, default branch main)
======================================================================
Required checks: `ci-gate`, `lint`
Run history: 100 completed runs since 2026-08-30; PR file lists for 25/25 PRs

 1. [critical] ci.yml — required-check-workflow-paths (not measured)
    workflow-level `pull_request` path filter on a workflow whose job(s) `lint` are REQUIRED ...
    fix: Remove the workflow-level `paths:`... job-level filter + aggregator gate
 2. [medium] e2e.yml — unfiltered-pr-workflow (~412 min saved)
    evidence: 14/40 classified PR runs touched only documentation files
 3. [medium] e2e.yml — missing-concurrency (~96 min saved)
    evidence: 9 PR run(s) kept running after a newer push to the same branch
 4. [high] build.yml / test — cache-key-never-hits (not measured)
Verified and dropped: ci.yml unfiltered-pr-workflow — tests/readme.sh reads README.md
```

Say which findings you verified and dropped in step 2, and why, so the report
is not mistaken for the helper's raw output. Under `--check` (and by default)
stop here; name `/repo:optimize-ci --apply` as the next step if anything is
worth fixing.

## 5. `--apply` — write the edits on a branch and open a PR (confirm first)

1. Pick the findings to fix (default: every verified finding with a concrete
   YAML fix). Make one branch off the up-to-date default branch
   (`ci/optimize-<short-topic>`), never the default branch itself. In a
   Loom-managed repo, use a worktree rather than switching the main checkout.
2. Edit the workflow files — minimal, surgical YAML edits that preserve
   comments and ordering. Show the concrete diff and confirm before
   committing, unless existing authorization already covers these edits.
3. Re-run `repo-optimize-ci.py scan --root .` on the branch (with the same
   `--required` contexts) and confirm the fixed findings are gone and nothing
   new appeared.
4. Commit, push the branch, and open a PR whose body lists each finding, its
   evidence line, and — for any path-filter change — the exact required-check
   migration the operator must perform (old check names out, `ci-gate` in).
   Never merge it yourself; CI on the PR is itself the first test of the new
   filters.

Never change branch protection or rulesets from this command. Switching the
required check to a gate job is an operator action, named in the PR body.

## Fan-out survey (`--all-repos`)

A **read-only** survey answering "where is CI wasting the most minutes across
the org?" It never writes, edits, or opens a PR in any repo it enumerates —
`--all-repos` is report-only regardless of `--check`. To act on a row, run
`/repo:optimize-ci --apply` in that repo.

Enumerate exactly as [[deps]]' fan-out survey does — explicit `--owner`, else
the origin's owner; `--paginate`; org endpoint first, user endpoint on 404;
archived repos excluded and counted in the header:

```bash
gh api orgs/OWNER/repos --paginate --jq '.[] | select(.archived == false) | .full_name'
gh api users/OWNER/repos --paginate --jq '.[] | select(.archived == false) | .full_name'
```

Then, per repo, read its workflows via the contents API — no clone:

```bash
python3 .claude/skills/repo/scripts/repo-optimize-ci.py report --repo OWNER/NAME --remote --json
```

A repo with no workflows reports `workflowsFound: 0` (a `no CI` row, not an
error). A helper exit that is non-zero or a listed repo whose contents cannot
be read is a **could not check** row naming the failure — never a silently
dropped row or a guessed state.

```
CI OPTIMIZATION SURVEY — OWNER (14 repos, 1 archived excluded)
===============================================================
| Repo          | Est. wasted min (30d) | Critical | Top finding                          |
|---------------|-----------------------|----------|--------------------------------------|
| OWNER/alpha   | ~512                  | 0        | e2e.yml unfiltered-pr-workflow       |
| OWNER/beta    | ~140                  | 1        | ci.yml required-check-workflow-paths |
| OWNER/gamma   | not measured          | 0        | build.yml cache-key-never-hits       |
| OWNER/delta   | —                     | —        | no CI                                |
| OWNER/epsilon | —                     | —        | could not check (403 reading contents) |
```

Rank by the sum of `estMinutesSaved`; repos with any `critical` finding are
flagged regardless of minutes, because a blocked-forever PR costs more than any
runner time.

## Safety Rules

1. **Never recommend workflow-level `paths:` / `paths-ignore:` on a workflow
   containing a required status check, or when required checks are unknown.**
   Job-level filter + always-running gate, every time.
2. **Never recommend cancelling default-branch runs.** Cancellation is always
   `${{ github.event_name == 'pull_request' }}`.
3. **Never narrow a filter past a real input.** Lockfiles, manifests, the
   workflow file, local actions/reusable workflows, and shared directories the
   job reads are inputs.
4. **Default and `--check` never write**; `--all-repos` never writes anywhere.
5. **`--apply` writes only on a branch, only after the diff is shown and
   authorized**, and opens a PR — never pushes to or merges into the default
   branch, and never edits branch protection or rulesets.
6. **Quantify, don't opine.** A finding's rank comes from measured minutes; when
   history is unavailable, say `not measured` rather than inventing a number.
