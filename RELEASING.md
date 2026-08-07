# Releasing kicad-tools

This is the **canonical, PR-based release process** for kicad-tools. Follow it
for every release. It exists to keep releases consistent with `main` branch
protection: every commit reaches `main` through a pull request, including the
version-bump commit.

> **TL;DR** — Reconcile the CHANGELOG (`scripts/changelog_gap_report.py`) →
> branch → bump commit + CHANGELOG → PR → auto-merge → `git fetch` → annotated
> tag on the **merged `main` SHA** → push the tag. The tag is created **only
> after** the bump commit is on `main`. Never push the version-bump commit
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

## The release sequence

Let `X.Y.Z` be the new version.

### (0) Reconcile `CHANGELOG.md` against `git log <last-tag>..main`

**Do this before the bump commit, not during it.** Nothing forces a CHANGELOG
entry at PR time, so `[Unreleased]` drifts silently: between `v0.19.0` and
2026-08-05 it documented 6 of 87 user-visible commits (#4638). Reconstructing
two weeks of history *while* cutting a release is how a permanently wrong
changelog ships — the tag is irrevocable once `publish.yml` uploads to PyPI.

Run the gap report; it exits non-zero and names every undocumented issue:

```bash
uv run python scripts/changelog_gap_report.py            # since the latest v* tag
uv run python scripts/changelog_gap_report.py --since v0.19.0 --json
```

The script walks `git log <tag>..HEAD`, resolves each commit to the **issue**
number it addresses (closing keyword → `feature/issue-<N>` branch name →
`Part of #N`), classifies user-visible vs. internal from the conventional-commit
subject prefix, and prints the user-visible issues that `[Unreleased]` does not
cite. Close every gap it reports — by writing a thematic `[Unreleased]` bullet,
or, for a commit that changes nothing a package consumer observes, by adding an
entry with its rationale to `INTERNAL_ISSUES` / `INTERNAL_COMMITS` in the
script. **Do not** rename `[Unreleased]` to `[X.Y.Z]` yet; that happens in (a).

> Note the trailing `(#NNNN)` in a squash-merge subject is the **PR** number.
> CHANGELOG entries cite **issue** numbers — do not read them off subjects.

A clean run ends with `RESULT: gap set is empty` and exit 0. Only then proceed.

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
# add the X.Y.Z CHANGELOG entry
git add pyproject.toml uv.lock CHANGELOG.md
git commit -m "chore(release): bump version to X.Y.Z"
```

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

This lands the bump commit on `main` through the protected-branch gate. Because
`main` **squash-merges**, the commit that ends up on `main` has a **different
SHA** than the commit on your `release/vX.Y.Z` branch — which is exactly why the
tag must wait until after the merge (see the ordering rule below).

### (d) Fetch, then create an annotated tag on the merged `main` SHA

```bash
git checkout main
git fetch origin
git pull origin main            # main now includes the merged bump commit
git tag -a vX.Y.Z -m "Release X.Y.Z" <merged-main-SHA>
```

Use the SHA of the bump commit **as it landed on `main`** (e.g.
`git rev-parse origin/main`, or the SHA of the squashed commit), not the
pre-merge branch SHA.

### (e) Push the tag — this triggers publish

```bash
git push origin vX.Y.Z
```

Only this step triggers `publish.yml`. It builds the commit the tag points at,
which is now a commit on `main`.

## The hard ordering rule (read this)

**Create the tag ONLY AFTER the bump commit is on `main`. Never tag a
pre-merge PR-branch commit.**

Why this is non-negotiable:

- `publish.yml` triggers `on: push: tags: ["v*"]` and its build job uses
  `actions/checkout@v4` **with no `ref:`** — so it checks out **whatever commit
  the tag points at**.
- `main` **squash-merges** PRs. The squashed commit on `main` has a **different
  SHA** than the commit on your `release/vX.Y.Z` branch.
- If you tag the PR-branch commit *before* the merge, the tag points at an
  orphaned pre-merge commit that **is not on the protected branch**. Pushing
  that tag would publish a commit `main` never saw — defeating the entire
  purpose of the PR gate.

By creating the tag only after `git fetch` brings the merged commit down, the
tag references the commit that is actually on `main`, and `publish.yml` builds
that commit.

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

## Quick checklist

- [ ] `uv run python scripts/changelog_gap_report.py` exits 0 with an empty gap
      set (step (0)) — run this *before* the bump commit.
- [ ] Version bumped in `pyproject.toml`; `uv.lock` regenerated to match.
- [ ] CHANGELOG entry added for `X.Y.Z`.
- [ ] Confirmed `pyproject.toml` is authoritative (ignore the `package.json`
      npm misdetection in `/repo:release` Phase 2).
- [ ] Bump commit on a branch, opened as a PR.
- [ ] PR merged onto `main` via `./.loom/scripts/merge-pr.sh <PR>` (not a direct
      push).
- [ ] `git fetch` / `git pull` so `main` includes the merged bump commit.
- [ ] Annotated tag `vX.Y.Z` created on the **merged `main` SHA**.
- [ ] `git push origin vX.Y.Z` — `publish.yml` builds the tagged commit and
      publishes to PyPI.
