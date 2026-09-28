# Changelog fragments

Every **user-visible** PR adds one small file here instead of editing
`CHANGELOG.md`'s `[Unreleased]` section (issue #5775, epic #5774). A fragment
is a new file, so ~20 merges a day never conflict. At release time
`scripts/changelog_assemble.py` turns the fragments into the
`## [X.Y.Z] - YYYY-MM-DD` section and deletes them.

## File name

```
changelog.d/<issue>.<kind>.md
changelog.d/<issue>.<kind>.<slug>.md   # a second same-kind entry for one issue
```

- `<issue>` is the **issue** number the PR closes (not the PR number).
- `<kind>` is one of:

  | kind          | Release section                      |
  |---------------|--------------------------------------|
  | `upgrade`     | Upgrade notes / behaviour changes    |
  | `fixed`       | Fixed                                |
  | `added`       | Added                                |
  | `changed`     | Changed                              |
  | `performance` | Performance                          |

  Use `upgrade` (in addition to `fixed`/`added`/...) when a user must act on
  the change -- a check that can newly fail, a changed default, a removed flag.
- Several fragments may share an issue (`5775.added.md` + `5775.upgrade.md`).

Any other file name in this directory (except this README) fails CI, so a typo
such as `5775.fix.md` is caught instead of silently dropped.

## Body

The finished bullet, in the existing CHANGELOG voice, citing the issue:

```markdown
- **`kct route` now routes at the clearance the board's project declares**
  (Issue #5645). The router reads the `.kicad_pro` and `.kicad_dru` beside
  the `.kicad_pcb` and treats each declared value as a floor.
```

A body that doesn't start with `- ` is made into one bullet.

## The per-PR check

`.github/workflows/changelog.yml` runs
`scripts/changelog_gap_report.py --pr-event ...` on every PR. It judges the PR
the way its squash commit will look: type from the **PR title**, issue from the
PR body's `Closes #N` (then the `feature/issue-<N>` branch, then `Part of #N`).
A PR passes when any of these hold:

- its title has an internal conventional-commit type (`chore`, `ci`, `docs`,
  `test`, `refactor`, `build`, `style`, `bench`, `config`, `revert`);
- it is a Dependabot PR or a Loom resync/install (internal by construction);
- its issue is listed in `INTERNAL_ISSUES` in `scripts/changelog_gap_report.py`;
- a fragment (or `[Unreleased]`) at the PR head cites its issue;
- it carries the `changelog:skip` label.

Run it locally with
`uv run python scripts/changelog_gap_report.py --pr <N>`.

`changelog:skip` is for PRs that look user-visible but deliberately need no
entry -- e.g. a follow-up fix to a feature that hasn't been released yet and
is already covered by that feature's fragment. If the change is internal by
nature, prefer an internal title type or an `INTERNAL_ISSUES` entry with a
rationale, which stay auditable after the label is gone.
