# Releasing kicad-tools

This is the **canonical, PR-based release process** for kicad-tools. Follow it
for every release. It exists to keep releases consistent with `main` branch
protection: every commit reaches `main` through a pull request, including the
version-bump commit.

> **TL;DR** — Branch → bump commit + CHANGELOG → PR → auto-merge → `git fetch`
> → annotated tag on the **merged `main` SHA** → push the tag. The tag is
> created **only after** the bump commit is on `main`. Never push the
> version-bump commit directly to `main`.

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

## Quick checklist

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
