# GitHub Workflow and Issue Templates

This directory contains GitHub configuration templates that Loom installs into new workspaces to support the AI-driven development workflow.

## Contents

### Workflows

**`workflows/label-external-issues.yml`**
- Automatically detects external contributors (non-collaborators)
- Adds `external` label to their issues
- Posts welcome comment explaining the workflow
- Ensures AI agents focus on internal issues only

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

### External Issue Workflow

1. Non-collaborator creates an issue
2. Workflow automatically:
   - Checks if user is a repo collaborator
   - Adds `external` label if not
   - Posts welcome comment explaining the triage process
3. Maintainers review and either:
   - Accept: Remove `external` label, add `loom:triage`
   - Reject: Close with explanation

### Internal Issue Workflow

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

### Required GitHub Label

The workflow requires an `external` label to exist in the repository. Create it with:

```bash
gh label create external --description "External contribution requiring manual triage" --color "6B7280"
```

**Label Protection**: GitHub's default permission model prevents non-collaborators from adding or removing labels. Once the workflow adds the `external` label to an issue, the external contributor cannot remove it. Only users with "Triage" permission or higher (collaborators, maintainers) can manage labels.

## Customization

Workspaces can customize these templates after installation:
- Modify issue template fields
- Adjust workflow triggers or conditions
- Add additional workflows

Changes to workspace `.github/` files don't affect the defaults.

## Label-Based Workflow

The issue template integrates with Loom's label-based workflow coordination:

| Label | Meaning | Who Sets It |
|-------|---------|-------------|
| `external` | External contribution | Workflow (automatic) |
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

1. **Automatic Triage**: External issues clearly marked for manual review
2. **Workflow Clarity**: Template explains how issues are used
3. **Reduced Noise**: Discussions redirected away from issue tracker
4. **AI Integration**: Labels coordinate autonomous agent behavior
5. **Consistent Setup**: Every Loom workspace gets the same workflow
