# Stacked PRs: what validation they get

A *stacked* PR is one whose base is a feature branch (`feature/issue-N`), not
`main` (`--depends-on`, `--auto-stack`, `worktree.sh --base`).

## What CI does and does not run

`pull_request: branches:` filters on the **base** branch. The PR-triggered
workflows are:

| Workflow | `pull_request` filter | Runs on a stacked PR? |
| --- | --- | --- |
| `ci.yml` (`CI`) | `branches: [main]` | **No** |
| `changelog.yml` (`Changelog`) | `branches: [main]` | **No** |
| `ecosystem-drift.yml` (`Ecosystem Drift`) | `paths:` only (no `branches:`) | Only if it touches `src/kicad_tools/ecosystem/**`, `scripts/ecosystem_refresh.py`, `scripts/ecosystem_render.py` or the workflow itself |

(`release-pr.yml` has no `pull_request` trigger; it opens PRs into `main`.)
So for a stacked PR:

- **None of `ci.yml`'s gates run** -- not pending, not failing. Usually
  `gh pr checks` prints "no checks reported"; if the PR touches the ecosystem
  paths above, it shows only the `Ecosystem Drift` check, which says nothing
  about tests, lint or the board gates.
- **`mergeStateStatus: CLEAN` means "the `ci.yml` gates never ran", not
  "checks passed".**
- The containerized board end-to-end, DRC/LVS and native gates first run on
  the **parent's** PR (or on `main`) after the child merges into the parent --
  i.e. after review.

Stacked PRs are therefore validated by (1) the author's local verification,
which must be written into the PR body (commands and results), and (2) the
parent PR's CI once the child has merged into it.

## Merge gate

`.loom/scripts/merge-pr.sh` is Loom-managed (overwritten on resync), so the
absent-vs-passing check lives in a repo-owned script. Run it before merging any
stacked PR:

```bash
uv run python scripts/ci/check_pr_checks_present.py <pr>
```

Exit 0 = checks passing; 2 = failing/pending; 3 = **no checks ran**.
`--allow-absent` turns exit 3 into a warning for an operator who has recorded
local verification.

Because a path-filtered workflow can make the rollup non-empty, "any check
passed" is not enough. When the base is not `main`, the script also requires
at least one check whose `workflowName` is `CI` (`--required-workflow`,
default `CI`). A rollup with only other checks (e.g. `Ecosystem Drift`)
counts as **absent** and exits 3. With base `main` there is no such
requirement, since `ci.yml` always triggers there. Caveat: this checks that
*some* `CI` job reported, not that every `ci.yml` job did. Per-job coverage is
still enforced by branch protection on `main`, not by this script.

## Open decision (maintainer)

Widening the trigger (drop `branches:` or add `feature/**`) would give stacked
PRs real CI, but each one would consume the self-hosted heavy lane, the
fleet's contended resource (2AMLogic/2am#29). Not done here. A cheaper variant
to evaluate is to widen the trigger but give the heavy jobs a job-level
`if: github.base_ref == 'main'`. Stacked PRs would then run only the light jobs
(e.g. `lint`, `typecheck`, `test`) and skip the heavy lane.
