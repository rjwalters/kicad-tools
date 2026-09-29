# Releasing kicad-tools

This is the **canonical, PR-based release process** for kicad-tools. It exists
to keep releases consistent with `main` branch protection: every commit reaches
`main` through a pull request, including the version-bump commit.

**The default path is automated** (epic #5774): a daily workflow opens and
merges the release PR, a push-to-`main` workflow tags the release PR's
tested head (now on `main`'s history), the
existing `publish.yml` publishes the tag to PyPI, and a follow-up workflow
creates the GitHub Release and verifies PyPI. See
[Automated release (default)](#automated-release-default). The
[manual release](#manual-release-fallback) steps remain the fallback when the
automation is off, paused or broken, and they follow the same rules.

> **TL;DR** — Automated: set `AUTO_RELEASE=on` and watch the "Automated
> release status" issue. Manual: reconcile the CHANGELOG
> (`scripts/changelog_gap_report.py`) → branch → bump commit + CHANGELOG → PR →
> merge → `git fetch` → annotated tag on the **release PR's head as merged
> into `main`** (the merge commit's second parent) → push the tag. The tag is
> created **only after** the merge, so it points at the release PR's tested
> head, which is on `main`'s history. Never push the version-bump commit
> directly to `main`.

## Why PR-based (not a direct push)

`main` is a protected branch: normal changes must land via a pull request.
The release version-bump commit is not special — it goes through the same gate.

This is already the established convention:

| Release | Bump commit | Path taken |
|---------|-------------|------------|
| 0.17.0  | `653ad9c0` (#4313) | PR — correct |
| 0.18.0  | `5076003b` (#4345) | PR — correct |
| 0.19.0  | `70322f59` (no PR)  | direct push that **bypassed branch protection** — the regression this document prevents |

When 0.19.0 was cut, the final `git push origin main --follow-tags` printed:

> remote: - Changes must be made through a pull request.

…yet the ref updated anyway (`95307b52..70322f59`) because the actor has admin
bypass. The release published successfully, but it skipped the gate every other
change to `main` must pass. This document makes the PR-based path the single
uniform rule.

## Version source of truth

- **`pyproject.toml`** `version = "…"` is the authoritative version, with
  **`uv.lock`** regenerated to match (`uv lock` after the bump).
- **Detection gotcha:** a vestigial `package.json` still sits at the repo root.
  When you run `/repo:release`, its Phase 2 detection will *provisionally*
  detect `npm` because of that file. **This is wrong for this repo.** During
  the Phase 2 "Cross-source reconciliation" step, confirm `pyproject.toml`
  (+ `uv.lock`) is authoritative. **Do NOT run `npm version`** — it would bump
  the wrong file and desync the real version.

## Automated release (default)

Three workflows chain together. Each has its logic in a script with unit tests,
and each obeys the same kill switch.

| Stage | Workflow | Script (tests) |
|-------|----------|----------------|
| 1. Release PR: plan, gate, open, merge | `release-pr.yml` (daily 14:00 UTC) | `scripts/release_plan.py` (`tests/test_release_plan.py`) |
| 2. Tag the release PR head (merge's 2nd parent, on `main`) | `release-tag.yml` (every push to `main`) | `scripts/release_tag.py tag` (`tests/test_release_tag.py`) |
| 3. Publish to PyPI | `publish.yml` (on the `v*` tag, **unchanged**) | — |
| 4. GitHub Release + PyPI check | `release-finalize.yml` (when publish completes) | `scripts/release_tag.py finalize` |

**Kill switch:** the repo variable `AUTO_RELEASE` is `on`, `off` or `dry-run`;
**unset means `dry-run`**. `off` stops every stage, including tagging.
`dry-run` makes each stage log what it would do (the release PR it would open,
the tag it would push, the Release it would create) and write nothing.

### Stage 1: the daily release PR (`release-pr.yml`, #5776)

Manual steps (0)–(c) are automated by `.github/workflows/release-pr.yml`.
The manual path still works and takes precedence: while a hand-opened
`release/v*` PR is open, the automation pauses.

- **When:** daily at 14:00 UTC, and on `workflow_dispatch` (inputs `level` =
  `auto|patch|minor|major` and `dry_run`). A run exits quietly when no PR has
  merged since the latest `v*` tag. Direct pushes such as Loom resyncs don't
  count. It also exits quietly when nothing user-visible is waiting (no
  `changelog.d/` fragment and an empty `[Unreleased]`), or when `pyproject.toml`
  is already ahead of the latest tag, meaning a merged release is waiting for
  its tag.
- **Kill switch:** the repo variable `AUTO_RELEASE` is `on`, `off` or
  `dry-run`; **unset means `dry-run`**. `dry-run` prints the plan, the gate
  result and the assembled section to the job summary, and opens or comments
  nothing.
- **Gate:** checked on the exact `origin/main` SHA.
  - The newest `CI` push run on that SHA has every release-topic job green or
    skipped: Lint & Format, Type Check, Test, C++ Build Check, kicad-cli
    Round-trip Smoke, Routed PCB DRC Check, Diff-Pair, Match-Group, Board 00
    E2E. Skipped always counts as not red, because Match-Group is skipped
    while `BOARD_07_CI_ENABLED` is unset. If the run is still in progress,
    the workflow waits up to 90 min.
  - `changelog_gap_report.py` reports no gaps.
  - `uv lock --check` passes.

  If the gate is red, no release PR is opened, and in `on` mode the reasons
  are commented on the **"Automated release status"** issue. The workflow
  finds that issue by title or creates it; pin it by hand once.
- **Level:**
  - patch by default;
  - minor if there's any `added` or `upgrade` fragment, or any `feat` commit
    (including a `feat:` PR title in a merge commit);
  - a breaking change (`type!:` or `BREAKING CHANGE`) is minor below 1.0 and
    major from 1.0;
  - the dispatch `level` input overrides the rule.
- **Release PR:** branch `release/vX.Y.Z`, titled `chore(release): vX.Y.Z`.
  - Contents:
    - the assembled CHANGELOG section, with the fragments deleted;
    - `pyproject.toml` bumped;
    - **only** the kicad-tools `version` line of `uv.lock` changed, because a
      full `uv lock` rewrites hundreds of marker lines across uv versions;
      `uv lock --check` is re-run on the result;
    - a WORK_LOG entry.
  - At most one is open at a time. The same version is force-pushed and
    edited in place. A new version supersedes the old PR, which is closed and
    its branch deleted.
- **Merge:** the workflow waits up to 150 min for the release PR's own checks.
  It merges through the API with the head SHA pinned once every
  `pull_request` workflow run on that SHA is green or skipped. If the checks
  are red or time out, the PR stays open and the tracking issue gets a
  comment. The merge commit is titled `chore(release): vX.Y.Z (#N)`, which
  is what stage 2 looks for.
- **Identity:** the loom-fleet-dispatch GitHub App. Its token comes from the
  secrets `LOOM_FLEET_DISPATCH_APP_ID` and
  `LOOM_FLEET_DISPATCH_APP_PRIVATE_KEY`, through
  `actions/create-github-app-token`. It pushes the branch and opens, merges
  and comments, which is why `ci.yml`'s `pull_request` trigger fires. A
  `GITHUB_TOKEN` push or PR would not trigger it, and this repo doesn't let
  `GITHUB_TOKEN` open PRs anyway. The App has no `actions` read, so CI status
  is read with the workflow's `GITHUB_TOKEN` (`actions: read`).

Preview locally with no network writes:

```bash
uv run python scripts/release_plan.py plan               # tag, merges, level, section
uv run python scripts/release_plan.py run --mode dry-run --wait-ci 0
```

### Stage 2: tag the release PR's head (`release-tag.yml`, #5777)

On every push to `main`, `release_tag.py tag` decides whether the pushed SHA
is a release merge. Two commit subjects count:

- `chore(release): vX.Y.Z (#N)`: an automated release PR merged by stage 1;
- `Merge pull request #N from <owner>/release/vX.Y.Z`: a hand-opened release
  PR merged by `merge-pr.sh` or the GitHub button (the manual path below).

**The tag points at the release PR's tested head, which is on `main`'s
history** — the merge commit's **second parent**, not the merge commit. That
head is exactly what the release gate and the PR's own CI tested. PRs that
merged to `main` while the release PR was waiting are in the merge commit's
first parent, not in the tag: they ship in the next release instead of
untested in this one.

The subject alone never tags. The pushed SHA must be a two-parent merge
commit: a single-parent commit with a release subject (a squash or rebase
merge) fails the run, because there is no PR head on `main`'s history to tag;
tag it by hand with the `sha` input below. `pyproject.toml`'s
`[project].version` at the second parent must equal the subject's version and
differ from the version at the first parent, and both the merge commit and its
second parent must be reachable from `origin/main`
(`git merge-base --is-ancestor`). If `vX.Y.Z` already exists on `origin`, the
run exits quietly, so a rerun cannot double-tag and a human who tagged first
is left alone. Every other push, which is almost all of them, exits quietly.

In `on` mode it pushes an **annotated** tag on the second parent. The
push authenticates as the loom-fleet-dispatch App, which matters: a tag pushed
with `GITHUB_TOKEN` would not trigger `publish.yml`, while an App-token push
triggers it exactly like a human `git push origin vX.Y.Z`. If a merge was
missed (for example `AUTO_RELEASE` was `dry-run` at the time), dispatch
`release-tag.yml` with its `sha` input set to the release PR's head
(`git rev-parse <merge-sha>^2`), or tag by hand as in manual step (d). A given
`sha` is tagged **exactly** (`release_tag.py tag --exact`), after the same
checks: its `pyproject.toml` version is ahead of the latest `v*` tag reachable
from it, it is on `origin/main`, and the tag does not exist yet. Left empty,
the input re-runs the detection on the `main` tip.

Once the tag exists, stage 1's "pyproject is ahead of the latest tag" guard
clears and daily planning resumes. Stage 1 reads "the latest tag" as the
highest `vX.Y.Z` tag reachable from `main` (not `git describe`'s nearest tag,
which can prefer an older first-parent tag over one reached through a second
parent), and it does not count the release merge itself as a PR to release.

### Stage 3: publish (`publish.yml`, unchanged)

The tag triggers `publish.yml` as before (see
[How the tag drives publish](#how-the-tag-drives-publish)). The file is not
modified by the automation, so PyPI trusted publishing, which names this
workflow file, needs no settings change. `uv publish --check-url` skips files
PyPI already has, so re-running a partly failed publish run is safe.

### Stage 4: GitHub Release and PyPI check (`release-finalize.yml`, #5777)

When `Publish to PyPI` completes for a `v*` tag, `release_tag.py finalize`:

- reports on the **"Automated release status"** issue and fails if the
  publish run did not succeed;
- otherwise creates the GitHub Release `vX.Y.Z`, using the `## [X.Y.Z]`
  section of `CHANGELOG.md` at the tag as its notes, unless a Release for the
  tag already exists;
- polls `https://pypi.org/pypi/kicad-tools/X.Y.Z/json` (10 checks, 30 s
  apart) until both a `bdist_wheel` and an `sdist` are listed. If either is
  missing, it reports on the tracking issue and fails.

This stage also runs for tags pushed by hand, so a manual release gets its
GitHub Release too, but only when `AUTO_RELEASE=on` (under `dry-run`, the
default when the variable is unset, finalize only logs what it would do).

Preview locally:

```bash
uv run python scripts/release_tag.py tag --sha origin/main --mode dry-run
uv run python scripts/release_tag.py tag --exact --sha <sha> --mode dry-run
uv run python scripts/release_tag.py notes X.Y.Z         # the Release notes
```

**Recovery:** if any stage fails, the tracking issue says which. Re-run the
failed workflow run: each stage is idempotent (existing tag, files already on
PyPI and an existing Release are all skipped). If the automation itself is
broken, set `AUTO_RELEASE=off` and finish the release with the manual steps
below from wherever it stopped.

## Manual release (fallback)

Use this path when `AUTO_RELEASE` is `off` or `dry-run`, when the automation is
broken, or for an out-of-band release. It follows the same rules as the
automated path. With `AUTO_RELEASE=on`, `release-tag.yml` tags a hand-merged
release PR itself; check `git ls-remote --tags origin vX.Y.Z` before doing
step (d).

Let `X.Y.Z` be the new version.

### (0) Reconcile `CHANGELOG.md` against `git log <last-tag>..main`

**Do this before the bump commit, not during it.** Changelog entries are now
written **at PR time** as `changelog.d/<issue>.<kind>.md` fragments (issue
#5775; format in `changelog.d/README.md`), and the `Changelog Fragment Check`
workflow fails a user-visible PR that has none. Before fragments, nothing
forced an entry and `[Unreleased]` drifted silently — between `v0.19.0` and
2026-08-05 it documented 6 of 87 user-visible commits (#4638), and 20
user-visible issues were missing when v0.22.0 was cut (#5772). This step is now
the residual safety net: it catches what slipped past the PR check (a
`changelog:skip` that shouldn't have been, a direct push, legacy history).

Run the gap report; it exits non-zero and names every undocumented issue:

```bash
uv run python scripts/changelog_gap_report.py            # since the latest v* tag
uv run python scripts/changelog_gap_report.py --since v0.19.0 --json
```

The script walks `git log <tag>..HEAD`, resolves each commit to the **issue**
number it addresses (closing keyword → `feature/issue-<N>` branch name →
`Part of #N`), classifies user-visible vs. internal from the conventional-commit
subject prefix, and prints the user-visible issues that neither a
`changelog.d/` fragment nor `[Unreleased]` cites. Close every gap it reports —
by adding a fragment (preferred), or, for a commit that changes nothing a
package consumer observes, by adding an entry with its rationale to
`INTERNAL_ISSUES` / `INTERNAL_COMMITS` in the script.

> Note the trailing `(#NNNN)` in a squash-merge subject is the **PR** number.
> CHANGELOG entries cite **issue** numbers — do not read them off subjects.

A clean run ends with `RESULT: gap set is empty` and exit 0. Only then proceed.

The fragments become the release section in step (a), via
`scripts/changelog_assemble.py` (preview first with `--dry-run`).

### (a) Create a release branch with the bump commit

```bash
git checkout main
git pull origin main
git checkout -b release/vX.Y.Z
```

Bump the version in `pyproject.toml`, regenerate the lockfile, and add a
`CHANGELOG` entry for `X.Y.Z`:

```bash
# edit pyproject.toml: version = "X.Y.Z"
uv lock            # regenerate uv.lock to match
# assemble changelog.d/ fragments + any [Unreleased] bullets into
# "## [X.Y.Z] - <today>" (sections: Summary, Upgrade notes, Fixed, Added,
# Changed, Performance), add the footer link, and delete the fragments:
uv run python scripts/changelog_assemble.py --version X.Y.Z --dry-run   # preview
uv run python scripts/changelog_assemble.py --version X.Y.Z [--summary-file summary.md]
git add pyproject.toml uv.lock CHANGELOG.md changelog.d/
git commit -m "chore(release): bump version to X.Y.Z"
```

Before pushing the branch, verify the lockfile actually matches the bumped
version — this is the step the v0.20.0 release skipped (#4698: `uv.lock`
stayed at 0.19.0, and every subsequent `uv` invocation dirtied every
checkout until #4702):

```bash
uv lock --check    # must exit 0 — errors if uv.lock is stale vs pyproject.toml
git diff --stat HEAD -- uv.lock   # expect no output (already committed above)
```

CI will **not** catch a stale lockfile: every CI job installs with
`uv sync --frozen`, which trusts the lockfile without checking it against
`pyproject.toml`. This local check is the only gate.

### (b) Open a pull request

```bash
git push -u origin release/vX.Y.Z
gh pr create --title "chore(release): bump version to X.Y.Z" --body "Release X.Y.Z"
```

### (c) Auto-merge the PR onto `main`

Once CI is green, merge the bump PR using the repo merge helper — an API merge
that does **not** require a local checkout — rather than pushing the bump commit
to `main` directly:

```bash
./.loom/scripts/merge-pr.sh <PR-NUMBER>
```

This lands the bump commit on `main` through the protected-branch gate. The
commit that ends up on `main` is a **new** commit — today a two-parent merge
commit (`Merge pull request #N from rjwalters/release/vX.Y.Z`, e.g. `9e8bc11b`
for v0.22.0), and a squash commit would be new too — so it has a **different
SHA** than the commit on your `release/vX.Y.Z` branch. With a merge commit,
the release branch's head becomes that merge's **second parent**, and so part
of `main`'s history. The tag must still wait until after the merge (see the
ordering rule below).

### (d) Fetch, then create an annotated tag on the release PR's head

```bash
git checkout main
git fetch origin
git pull origin main            # main now includes the release merge
git merge-base --is-ancestor <merge-SHA>^2 origin/main && \
  git tag -a vX.Y.Z -m "Release X.Y.Z" <merge-SHA>^2
```

Tag the release PR's head **as merged into `main`**: the merge commit's second
parent, which is exactly what the PR's CI tested. It is only on `main`'s
history once the merge has happened, so check with
`git merge-base --is-ancestor` first. For a squash merge there is no second
parent; tag the squashed commit on `main` instead.

### (e) Push the tag — this triggers publish

```bash
git push origin vX.Y.Z
```

Only this step triggers `publish.yml`. It builds the commit the tag points at,
which is now a commit on `main`'s history.

## The hard ordering rule (read this)

**Create the tag ONLY AFTER the release PR has merged into `main`, and only on
a commit reachable from `main`. Never tag a PR-branch commit before its merge.**

Why this is non-negotiable:

- `publish.yml` triggers `on: push: tags: ["v*"]` and its build job uses
  `actions/checkout@v4` **with no `ref:`** — so it checks out **whatever commit
  the tag points at**.
- Merging creates a **new** commit on `main` (a merge commit today; a squash
  commit would be new too), with a **different SHA** than the commit on your
  `release/vX.Y.Z` branch.
- If you tag the PR-branch commit *before* the merge, the tag points at an
  orphaned pre-merge commit that **is not on the protected branch**. Pushing
  that tag would publish a commit `main` never saw — defeating the entire
  purpose of the PR gate.

By creating the tag only after `git fetch` brings the merge down, the tag
references a commit that is actually on `main`'s history (the release PR's
tested head, reached through the merge's second parent), and `publish.yml`
builds that commit.

`release-tag.yml` follows the same rule by construction: it runs on the push
to `main` and tags the pushed merge's second parent only after checking with
`git merge-base --is-ancestor` that it is reachable from `origin/main`.

## How the tag drives publish

`.github/workflows/publish.yml`:

```yaml
on:
  push:
    tags:
      - "v*"
```

The `build` job checks out with `actions/checkout@v4` (no `ref:`), so it builds
the commit the tag references, runs `uv build`, and the `publish` job runs
`uv publish` to PyPI (trusted publishing via the `pypi` environment). This is
the mechanism that makes the tag — and therefore the tag's ordering relative to
the merge — load-bearing.

## Actions outage fallback (local CI-equivalent gate)

When GitHub Actions is down or badly degraded (as during the ~6-hour
2026-08-06 outage that wedged 11 runs mid-release), the merge/release gate can
be substituted with the checked-in local gate — **with explicit operator
sign-off for each gate substitution**. This formalizes the ad-hoc procedure
that kept v0.20.0 on schedule and caught release blocker #4667 before CI ever
could.

The playbook that worked in the 2026-08-06 outage, now scripted:

1. **Run the local gate against a clean integration worktree** (not a dirty
   working copy — the script warns if the tree is dirty):

   ```bash
   scripts/ci/local-gate.sh --release
   ```

   `--release` runs the cheap CI gates (ruff format + check, baseline-gated
   mypy, the full non-slow pytest suite with the C++ backend built,
   cpp-build-check, kicad-cli round-trip smoke, routed-PCB DRC check) plus the
   two release extras used in the outage: the board-03 routing baseline
   (`tests/router/test_board03_routing_baseline.py`) and
   `scripts/changelog_gap_report.py`. Use `--full` to add the long board
   end-to-end jobs (multiple hours), `--list` to see the job manifest, or name
   individual jobs. The manifest is drift-guarded against
   `.github/workflows/ci.yml` by `tests/test_local_gate_manifest.py`.

2. **Operator sign-off**: merging or tagging on the strength of a local gate
   run (instead of green CI) requires explicit human operator approval,
   recorded on the PR/release thread. The local gate is a backstop, not a
   replacement — advisory jobs and kicad-cli-dependent steps have
   documented parity caveats (see the script header).

3. **Reconcile once Actions recovers**: trigger one CI run on the `main` tip
   (an empty commit or re-run works) and confirm it is green. Any divergence
   between that run and the local gate result is a bug in the gate script —
   file it.

**Ephemeral-runner verdict** (recorded per issue #4671): a standing
self-hosted runner pool was evaluated during the outage and **rejected** — on
a public repository it is a fork-PR code-execution liability, and it still
depends on GitHub's Actions control plane, which was itself degraded during
the incident (so it would not have helped). An *ephemeral* on-demand cloud
runner (restricted runner group, torn down after use) remains a possible
follow-up if outages recur, but the cheaper, safer backstop is the local gate
above; no runner infrastructure is maintained for this repo.

## Quick checklist (manual release)

- [ ] `uv run python scripts/changelog_gap_report.py` exits 0 with an empty gap
      set (step (0)) — run this *before* the bump commit.
- [ ] Version bumped in `pyproject.toml`; `uv lock` run and the regenerated
      `uv.lock` committed **in the same bump commit**.
- [ ] `uv lock --check` exits 0 on the release branch before the PR is opened
      (CI's `uv sync --frozen` will NOT catch a stale lockfile — #4698).
- [ ] CHANGELOG entry for `X.Y.Z` assembled with
      `scripts/changelog_assemble.py --version X.Y.Z` (fragments consumed).
- [ ] Confirmed `pyproject.toml` is authoritative (ignore the `package.json`
      npm misdetection in `/repo:release` Phase 2).
- [ ] Bump commit on a branch, opened as a PR.
- [ ] PR merged onto `main` via `./.loom/scripts/merge-pr.sh <PR>` (not a direct
      push).
- [ ] `git fetch` / `git pull` so `main` includes the merged bump commit.
- [ ] Annotated tag `vX.Y.Z` created on the **release PR's head as merged into `main`** (the merge's second parent).
- [ ] `git push origin vX.Y.Z` — `publish.yml` builds the tagged commit and
      publishes to PyPI.
