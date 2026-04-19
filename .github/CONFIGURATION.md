# GitHub Workflow and Issue Templates

This directory contains GitHub configuration templates that Loom installs into new workspaces to support the AI-driven development workflow.

## Contents

### Issue Templates

**`ISSUE_TEMPLATE/task.yml`**
- Single unified template for all development tasks
- Supports: Bug Fix, Feature, Refactoring, Documentation, Testing, Infrastructure, Research, Improvement
- Clearly explains that issues control the development process
- Redirects discussions to GitHub Discussions

**`ISSUE_TEMPLATE/config.yml`**
- Disables blank issues (forces template use)
- Links to GitHub Discussions for non-task items

## How It Works

### Issue Workflow

1. Collaborator creates an issue (no auto-labeling)
2. Issue starts with `loom:triage` label (from template)
3. Enters the label-based workflow:
   - Curator enhances → adds `loom:curated` (human approves → `loom:issue`)
   - Builder implements → adds `loom:building`
   - Creates PR → adds `loom:review-requested`
   - Judge approves → adds `loom:pr`
   - Merge completes workflow

## Installation

### Automatic Template Installation

These templates are automatically copied to `<workspace>/.github/` during:
- Initial workspace setup
- Factory reset
- New project creation

### Optional: External Issue Labeling Workflow

For repositories that expect external contributors, an optional workflow is available that automatically labels issues from non-collaborators. See `defaults/optional/github-workflows/label-external-issues.yml` in the Loom source repository.

This workflow is not installed by default because it generates "No jobs were run" email notifications from GitHub on every issue event in single-contributor repos.

## Customization

Workspaces can customize these templates after installation:
- Modify issue template fields
- Add additional workflows from `defaults/optional/`

Changes to workspace `.github/` files don't affect the defaults.

## Label-Based Workflow

The issue template integrates with Loom's label-based workflow coordination:

| Label | Meaning | Who Sets It |
|-------|---------|-------------|
| `loom:triage` | New issue awaiting Curator enhancement | Issue template (automatic) |
| `loom:curated` | Enhanced by Curator, awaiting human approval | Curator agent |
| `loom:issue` | Approved for work, ready for Builder | Human (from curated) |
| `loom:building` | Builder is implementing | Builder agent |
| `loom:curating` | Curator is enhancing | Curator agent |
| `loom:treating` | Doctor is fixing bug/PR feedback | Doctor agent |
| `loom:review-requested` | PR needs review | Builder agent |
| `loom:reviewing` | Judge is actively reviewing | Judge agent |
| `loom:pr` | Approved for merge | Judge agent |

See [WORKFLOWS.md](../../WORKFLOWS.md) for complete workflow documentation.

## Benefits

1. **Workflow Clarity**: Template explains how issues are used
2. **Reduced Noise**: Discussions redirected away from issue tracker
3. **AI Integration**: Labels coordinate autonomous agent behavior
4. **Consistent Setup**: Every Loom workspace gets the same configuration
