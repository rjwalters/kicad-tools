# CI Integration: Skipping CI for Loom Install PRs

When Loom is installed into a target repository, the installer creates a
pull request that adds the `.loom/`, `.claude/`, `.github/labels.yml`,
`.codex/`, and `CLAUDE.md` files. These changes do not touch the target
project's source — they're purely orchestration scaffolding. Yet, by default,
the target's CI workflows will run in full on the install PR, which can be
slow and expensive on projects with heavy test suites.

This document explains the three opt-in mechanisms Loom provides so target
repositories can skip CI on install PRs without globally disabling CI.

## The default markers (always on)

Every install PR created by `scripts/install-loom.sh` carries **three
passive markers**. They are passive in the sense that they do not include
`[skip ci]` or `[ci skip]` directives by themselves — they only signal that
the PR is a Loom install PR, leaving it up to the target's CI configuration
to decide whether to act on the signal.

| Marker | Location | Example |
|--------|----------|---------|
| **Title prefix** | PR title | `chore(loom): Install Loom 0.8.0` |
| **Body marker line** | PR body, separator-isolated | `loom-install: true` |
| **Commit trailer** | Latest commit message | `Skip-CI-Hint: docs-only` |

The body also includes a `docs-only: true` line on the next line as a second
machine-detectable signal.

These markers are designed to be detected by:

- **GitHub Actions** workflows using `paths-ignore` (most common; see below).
- **Conventional-commit-aware CI** that filters on the `chore` or `chore(loom)`
  type prefix.
- **CI scripts that read the PR body via API** and grep for the marker line.
- **Commit-trailer scanners** that read `Skip-CI-Hint` and route the PR
  to a fast-path job set.

## Recommended: GitHub Actions `paths-ignore`

The simplest way to skip CI on install PRs is to add a `paths-ignore`
block to the target's GitHub Actions workflows. This skips workflow runs
when **every** changed file matches one of the listed patterns, which is
exactly the situation for a Loom install PR.

```yaml
# .github/workflows/ci.yml
on:
  pull_request:
    paths-ignore:
      - '.loom/**'
      - '.claude/**'
      - '.codex/**'
      - 'CLAUDE.md'
      - '.github/labels.yml'
```

After merging this change into the target repo, future Loom install/update
PRs will not trigger the workflow.

### Caveats

- `paths-ignore` is **per workflow** — repeat the block in every workflow
  you want to skip.
- If a workflow's `pull_request` block omits `paths-ignore`, the workflow
  will still run. (There is no global default in GitHub Actions.)
- `paths-ignore` only takes effect when *all* changed files match one of
  the listed patterns. An install PR that also modifies (e.g.) the target
  repo's `Cargo.toml` would still run the workflow — by design.
- **Required-checks rulesets** that block merges until a workflow run
  reports success will leave install PRs un-mergeable when `paths-ignore`
  causes the workflow to be skipped (because no status is reported). For
  these repos, see "Opt-in `--skip-target-ci`" below, or configure the
  ruleset to treat skipped workflows as passing.

## Body / title / trailer detection

For repos that cannot use `paths-ignore` (or want finer-grained control),
the markers can be read explicitly from a workflow:

```yaml
# .github/workflows/ci.yml
on:
  pull_request:

jobs:
  check-install-pr:
    runs-on: ubuntu-latest
    outputs:
      is_install: ${{ steps.detect.outputs.is_install }}
    steps:
      - id: detect
        run: |
          if [[ "${{ github.event.pull_request.title }}" == chore\(loom\):* ]]; then
            echo "is_install=true" >> "$GITHUB_OUTPUT"
          elif echo "${{ github.event.pull_request.body }}" | grep -q '^loom-install: true$'; then
            echo "is_install=true" >> "$GITHUB_OUTPUT"
          else
            echo "is_install=false" >> "$GITHUB_OUTPUT"
          fi

  expensive-tests:
    needs: check-install-pr
    if: needs.check-install-pr.outputs.is_install != 'true'
    runs-on: ubuntu-latest
    steps:
      - run: # ... your slow tests ...
```

The `Skip-CI-Hint: docs-only` commit trailer can be inspected similarly
via `git log -1 --format=%B`.

## Opt-in `--skip-target-ci` flag

When the target repo's CI cannot be configured (e.g. third-party
required-checks beyond the installer's control), `scripts/install-loom.sh`
supports a `--skip-target-ci` flag that adds `[skip ci]` to the PR title
and commit subject:

```bash
./scripts/install-loom.sh --skip-target-ci /path/to/target-repo
```

`[skip ci]` is GitHub's universal native CI-skip directive: any GitHub
Actions workflow triggered by `push` or `pull_request` will not run on a
commit whose subject line contains `[skip ci]`, `[ci skip]`, `[no ci]`,
`[skip actions]`, or `[actions skip]`.

### When to use `--skip-target-ci`

- Target repo has expensive CI you don't need to run for install PRs.
- You have confirmed install PRs do not touch CI-relevant paths.
- The target's required-checks rulesets **do not** depend on CI
  completing (e.g., no required status checks).

### When NOT to use `--skip-target-ci`

- The target has required status checks that gate merges. `[skip ci]`
  will leave the PR un-mergeable because the required check will never
  report a status.
- Renovate/Dependabot or similar bots depend on the CI completing.
- You want defense-in-depth: the markers still allow opt-in detection,
  but you don't want to forcibly suppress all CI.

The flag is **off by default** for exactly these reasons.

## Composability with `PR_TITLE` / `PR_BODY` / `COMMIT_MSG` overrides

`scripts/install/create-pr.sh` supports three environment-variable overrides
for custom install workflows:

- `PR_TITLE` — fully overrides the default title (no marker injection).
- `PR_BODY` — fully overrides the default body (no marker injection).
- `COMMIT_MSG` — fully overrides the default commit message (no marker
  injection).

When any of these are set, the corresponding marker is **not** injected,
because the caller has explicitly opted out of the default behavior. If you
want to keep the markers in a custom workflow, include them yourself in
your custom strings.

## Reference

| File | Purpose |
|------|---------|
| `scripts/install/create-pr.sh` | Generates the install PR with markers |
| `scripts/install-loom.sh` | Top-level installer; parses `--skip-target-ci` |
| `defaults/docs/ci-integration.md` | This document (shipped with installs) |

For questions or improvements, file an issue at
<https://github.com/rjwalters/loom/issues>.
