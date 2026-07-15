# Release topic: kicad-tools specifics

This note records the kicad-tools-specific release facts (CI gate jobs,
version-bearing files, the PyPI publish trigger) and is injected as
additional context by the methodology-injection hook whenever you work on
a release. Release management uses the general-purpose **`/repo:release`**
command (from [rjwalters/repo](https://github.com/rjwalters/repo));
Loom's own `/loom:release` was retired in Loom 0.10.7. `/repo:release`
discovers the version-bearing files itself (it honors `scripts/version.sh`
first, then `pyproject.toml`) — the sections below are the authoritative
repo-specific values it should confirm against, not a fork of the command.

## CI green-gate jobs (Phase 1)

The release-readiness gate for this repo is a green run on `main` of the
following CI jobs, as defined by the `name:` fields in
`.github/workflows/ci.yml`:

- **Lint & Format**
- **Type Check**
- **Test**
- **C++ Build Check**
- **kicad-cli Round-trip Smoke**
- **Routed PCB DRC Check**
- **Diff-Pair Routing Regression**
- **Match-Group Routing Regression**
- **Board 00 End-to-End**

Do not cut a release while any of these are red on `main`. The skill's
generic CI detection will find these workflows automatically; this list
is the authoritative set the operator should confirm green.

## Version-bearing files (Phase 5)

This repo does **not** ship `scripts/version.sh` on `main`, so the skill
falls through its tool-detection chain. The canonical version manifest is:

- `pyproject.toml` — `[project]` `version = "X.Y.Z"` (line ~7). This is
  the single source of truth consumed by hatchling and PyPI.

Human-facing "current release" references that should be updated as
ordinary edits when cutting a release (not version-bearing for tooling,
but kept in sync for humans):

- `CHANGELOG.md` — add the new `## [X.Y.Z] - <date>` section plus the
  matching `[X.Y.Z]: .../releases/tag/vX.Y.Z` footer link.
- `WORK_LOG.md` — append a dated release line (chronological log,
  append-only).
- `WORK_PLAN.md` — update the "current release" highlight row.

> Note: `tests/test_board_metrics.py` contains a string `"kicad-tools
> 0.13.0"` inside a **sample report fixture** — it is test input data, not
> a package-version assertion. Do NOT bump it during a release.

## PyPI publish trigger (Phase 6) — TAGGING IS THE PUBLISH TRIGGER

There is **no** `release.yml` keyed on GitHub Release creation in this
repo. Distribution is driven entirely by `.github/workflows/publish.yml`,
which runs on:

```yaml
on:
  push:
    tags:
      - "v*"
```

That workflow runs `uv build` then `uv publish` via PyPI **trusted
publishing** (`id-token: write`, `environment: pypi`). The implication
for the operator:

- **Pushing the `v*` tag is what publishes to PyPI.** There is no
  separate "create a GitHub Release to publish" step required.
- A GitHub Release is still **recommended** for human-readable notes
  (use the `[X.Y.Z]` CHANGELOG entry as the body), but it does **not**
  drive the build — the tag push already did.

At extension point `pre-push`: confirm the operator intends to push tag
`vX.Y.Z`, since that single action triggers `publish.yml` and the
irreversible PyPI publish. At extension point `post-push`: poll the
`Publish to PyPI` workflow run to completion and verify the package is
live on PyPI before considering the release done.
