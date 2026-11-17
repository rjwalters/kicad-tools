# GitHub Workflow and Issue Templates

This directory contains GitHub configuration templates that Loom installs into new workspaces to support the AI-driven development workflow.

## Contents

Loom ships exactly these four files under `.github/` (installed by `scripts/install-loom.sh`'s
defaults walk, which mirrors `defaults/.github/` into the workspace):

- **`CONFIGURATION.md`** — this file
- **`ISSUE_TEMPLATE/task.yml`** — single unified template for all development tasks (Bug Fix,
  Feature, Refactoring, Documentation, Testing, Infrastructure, Research, Improvement); applies
  `loom:triage` and explains that a task is reviewed before work begins
- **`ISSUE_TEMPLATE/config.yml`** — leaves blank issues enabled (GitHub's own default) and adds
  no contact links
- **`labels.yml`** — the authoritative label set for the label-based workflow (see below)

Everything else under a workspace's `.github/` (e.g. `workflows/`) is consumer-owned — Loom
never installs, edits, or removes it. If you see a `.github/` file not in this list, it isn't
from Loom.

Both issue templates are repo-neutral: they name no repository and link nowhere, so they read
correctly in any workspace. To point filers at your own forum or to require the template, see
[Customization](#customization).

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
`scripts/install-loom.sh`'s defaults-directory walk, and are re-synced on a forced `loom update` /
reinstall (they're on the Loom-shipped `.github/` allowlist, so a forced reinstall overwrites
local edits to these four files — customize via `defaults/optional/` or a fork instead).
`CONFIGURATION.md` specifically is also covered by `./.loom/scripts/resync-installed.sh`, so a
fix to this file reaches an existing install without a full forced reinstall.

### Optional: External Issue Labeling Workflow

For repositories that expect external contributors, an optional workflow is available that automatically labels issues from non-collaborators. See `defaults/optional/github-workflows/label-external-issues.yml` in the Loom source repository.

This workflow is not installed by default because it generates "No jobs were run" email notifications from GitHub on every issue event in single-contributor repos.

## Customization

Workspaces can customize non-Loom-shipped `.github/` content freely (it's never touched by
Loom). Customizing one of the four Loom-shipped files above will be clobbered on the next
`loom update` / reinstall — instead add workflows from `defaults/optional/`, or fork.

That includes `ISSUE_TEMPLATE/config.yml`: a `contact_links` entry or a
`blank_issues_enabled: false` added there is overwritten by the next forced reinstall, so
re-apply it afterwards. A separate template file you add yourself (any other name under
`ISSUE_TEMPLATE/`) is yours and is left alone.

## Label-Based Workflow

The issue template integrates with Loom's label-based workflow coordination. `.github/labels.yml`
is the authoritative label set (each label's `Applied by:` field states who sets it) — see that
file rather than a table here, which would drift.

See [WORKFLOWS.md](https://github.com/rjwalters/loom/blob/main/docs/workflows.md) for complete workflow documentation.

## Benefits

1. **Workflow Clarity**: Template explains how issues are used
2. **No Surprises**: Blank issues stay enabled and filers are not sent to another site
3. **AI Integration**: Labels coordinate autonomous agent behavior
4. **Consistent Setup**: Every Loom workspace gets the same configuration
