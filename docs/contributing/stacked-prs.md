# Stacked PRs: what validation they get

A *stacked* PR is one whose base is a feature branch (`feature/issue-N`), not
`main` (`--depends-on`, `--auto-stack`, `worktree.sh --base`).

## What CI does and does not run

`.github/workflows/ci.yml` triggers `pull_request` only for `branches: [main]`.
`pull_request: branches:` filters on the **base** branch, and `ci.yml` is the
only PR-triggered workflow. So for a stacked PR:

- **No CI runs at all** -- not pending, not failing. `gh pr checks` prints
  "no checks reported".
- **`mergeStateStatus: CLEAN` means "no checks ran", not "checks passed".**
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

## Open decision (maintainer)

Widening the trigger (drop `branches:` or add `feature/**`) would give stacked
PRs real CI, but each one would consume the self-hosted heavy lane, the
fleet's contended resource (2AMLogic/2am#29). Not done here; a job-level `if:`
limiting heavy jobs to `base != main` is the cheaper variant to evaluate.
