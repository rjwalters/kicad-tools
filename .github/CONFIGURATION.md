# GitHub Workflow and Issue Templates

This directory contains GitHub configuration templates that Loom installs into new workspaces to support the AI-driven development workflow.

## Contents

Loom ships exactly these four files under `.github/` (installed by `scripts/install-loom.sh`'s
defaults walk, which mirrors `defaults/.github/` into the workspace):

- **`CONFIGURATION.md`** — this file
- **`ISSUE_TEMPLATE/task.yml`** — single unified template for all development tasks (Bug Fix,
  Feature, Refactoring, Documentation, Testing, Infrastructure, Research, Improvement); explains
  that issues control the development process; redirects discussions to GitHub Discussions
- **`ISSUE_TEMPLATE/config.yml`** — disables blank issues (forces template use) and links to
  GitHub Discussions for non-task items
- **`labels.yml`** — the authoritative label set for the label-based workflow (see below)

Everything else under a workspace's `.github/` (e.g. `workflows/`) is consumer-owned — Loom
never installs, edits, or removes it. If you see a `.github/` file not in this list, it isn't
from Loom.

## How It Works

### Issue Workflow

1. Collaborator creates an issue (no auto-labeling)
2. Issue starts with `loom:triage` label (from template)
3. Enters the label-based workflow:
   - Curator enhances → adds `loom:curated`
   - `loom:curated` → `loom:issue` promotion (human, Champion, or the `/loom:sweep`
     orchestrator's approval gate — see `.loom/roles/curator.md` § "Who promotes
     `loom:curated` → `loom:issue`" for the authoritative rule)
   - Builder implements → adds `loom:building`
   - Creates PR → adds `loom:review-requested`
   - Judge approves → adds `loom:pr`
   - Merge completes workflow

## Installation

These files are copied from `defaults/.github/` into `<workspace>/.github/` by
`scripts/install-loom.sh`'s defaults-directory walk, and are re-synced on `loom update` /
reinstall (they're on the Loom-shipped `.github/` allowlist, so a reinstall overwrites local
edits to these four files — customize via `defaults/optional/` or a fork instead).

### Optional: External Issue Labeling Workflow

For repositories that expect external contributors, an optional workflow is available that automatically labels issues from non-collaborators. See `defaults/optional/github-workflows/label-external-issues.yml` in the Loom source repository.

This workflow is not installed by default because it generates "No jobs were run" email notifications from GitHub on every issue event in single-contributor repos.

## Customization

Workspaces can customize non-Loom-shipped `.github/` content freely (it's never touched by
Loom). Customizing one of the four Loom-shipped files above will be clobbered on the next
`loom update` / reinstall — instead add workflows from `defaults/optional/`, or fork.

## Label-Based Workflow

The issue template integrates with Loom's label-based workflow coordination. `.github/labels.yml`
is the authoritative label set (each label's `Applied by:` field states who sets it) — see that
file rather than a table here, which would drift.

See [WORKFLOWS.md](https://github.com/rjwalters/loom/blob/main/docs/workflows.md) for complete workflow documentation.

## Benefits

1. **Workflow Clarity**: Template explains how issues are used
2. **Reduced Noise**: Discussions redirected away from issue tracker
3. **AI Integration**: Labels coordinate autonomous agent behavior
4. **Consistent Setup**: Every Loom workspace gets the same configuration
