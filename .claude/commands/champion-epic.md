# Champion: Epic Evaluation Context

This file contains epic evaluation instructions for the Champion role. **Read this file when Priority 4 work is found (epic proposals).**

---

## Overview

Evaluate epic proposals (`loom:epic`) and, when approved, create Phase 1 implementation issues. Epics are multi-phase work items that decompose into individual issues with phase dependencies.

---

## Epic Evaluation Criteria

For each epic proposal, evaluate against these **6 criteria**. All must pass for approval:

### 1. Clear Overview
- [ ] Epic has a high-level description of the feature
- [ ] Rationale for epic structure is explained (why not single issues)
- [ ] Scope boundaries are defined

### 2. Well-Defined Phases
- [ ] At least 2 phases with clear boundaries
- [ ] Each phase has a stated goal
- [ ] Phase dependencies are explicit (e.g., "Blocked by: Phase 1")

### 3. Actionable Issues
- [ ] Each issue within phases has enough context to implement
- [ ] Issue descriptions follow the "Brief description" pattern
- [ ] Issues are appropriately sized (not too large or too small)

### 4. Milestone Alignment
- [ ] Epic references current milestone
- [ ] Alignment tier is specified (Tier 1/2/3)
- [ ] Justification explains why this advances project goals

### 5. Success Criteria
- [ ] Measurable outcomes defined for epic completion
- [ ] Criteria are verifiable (not vague)

### 6. Reasonable Scope
- [ ] Total estimated issues is reasonable (typically 4-15)
- [ ] Complexity estimates are provided per phase
- [ ] Epic can be completed in a reasonable timeframe

---

## Epic Approval Workflow

### Step 1: Read the Epic

```bash
gh issue view <number>
```

Read the full epic body, noting phases, issues, and dependencies.

### Step 2: Evaluate Against Criteria

Check each of the 6 criteria above. If ANY criterion fails, skip to Step 4 (rejection).

### Step 3: Approve and Create Phase 1 Issues

If all 6 criteria pass:

1. **Create Phase 1 issues** with `loom:architect` label:

```bash
# For each issue in Phase 1
gh issue create --title "[Epic #<epic>] <Issue Title>" --body "$(cat <<'EOF'
**Epic**: #<epic-number> - <Epic Title>
**Phase**: 1 of N
**Phase Goal**: <phase 1 goal from epic>

## Description

<Issue description from epic, expanded with context>

## Acceptance Criteria

- [ ] <specific criterion>
- [ ] <specific criterion>

## Dependencies

Part of Epic #<epic-number>. This is a Phase 1 issue with no blocking dependencies.

---
*Created by Champion from Epic #<epic-number>*
EOF
)" --label "loom:architect" --label "loom:epic-phase"
```

2. **Update the epic issue** to track phase progress:

```bash
# Add comment tracking Phase 1 creation
gh issue comment <epic-number> --body "**Champion: Epic Approved**

Phase 1 issues created and awaiting individual approval:
- #<issue-1>: <title>
- #<issue-2>: <title>

Epic will progress to Phase 2 when all Phase 1 issues are closed.

---
*Automated by Champion role*"
```

3. **Keep epic open** - it tracks progress across all phases.

### Step 4: Reject (One or More Criteria Fail)

If any criteria fail, leave detailed feedback but keep the `loom:epic` label:

```bash
gh issue comment <number> --body "**Champion Review: Epic Needs Revision**

This epic requires additional work before approval:

- [Criterion that failed]: [Specific reason]
- [Another criterion]: [Specific reason]

**Recommended actions:**
- [Specific suggestion 1]
- [Specific suggestion 2]

Keeping \`loom:epic\` label. The Architect can revise and resubmit.

---
*Automated by Champion role*"
```

---

## Phase Progression

When all issues in a phase are closed, Champion creates the next phase's issues.

### Detecting Phase Completion

```bash
# Check if all Phase N issues for an epic are closed
EPIC_NUMBER=123
PHASE=1

# Get all issues with loom:epic-phase that reference this epic and phase
PHASE_ISSUES=$(gh issue list \
  --label="loom:epic-phase" \
  --state=all \
  --search="Epic: #$EPIC_NUMBER Phase: $PHASE in:body" \
  --json number,state \
  --jq '.')

# Count open vs closed
OPEN_COUNT=$(echo "$PHASE_ISSUES" | jq '[.[] | select(.state == "OPEN")] | length')
CLOSED_COUNT=$(echo "$PHASE_ISSUES" | jq '[.[] | select(.state == "CLOSED")] | length')

if [ "$OPEN_COUNT" -eq 0 ] && [ "$CLOSED_COUNT" -gt 0 ]; then
    echo "Phase $PHASE complete! Creating Phase $((PHASE + 1)) issues..."
fi
```

### Creating Next Phase Issues

When Phase N completes, create Phase N+1 issues following the same pattern as Step 3 above, but with:
- Updated phase number
- Dependencies referencing Phase N completion
- Updated epic comment showing progress

### Epic Completion

When all phases are complete:

```bash
# Close the epic
gh issue close <epic-number> --comment "**Epic Complete**

All phases have been implemented and merged:

**Phase 1**: Complete
- #<issue-1>: <title>
- #<issue-2>: <title>

**Phase 2**: Complete
- #<issue-3>: <title>

**Success Criteria Met**:
- [x] <criterion 1>
- [x] <criterion 2>

Total issues: N
Total PRs merged: N

---
*Automated by Champion role*"
```

---

## Epic Rate Limiting

**Approve at most 1 epic per iteration.**

Epics generate multiple issues, so limit epic approvals to prevent overwhelming the backlog. Phase progression (creating next phase issues) does not count against this limit.

---

## Force Mode Epic Behavior

In force mode, epics are evaluated with relaxed criteria:
- Skip detailed criteria checking
- Auto-approve if epic has at least 2 phases and clear issue list
- Create Phase 1 issues immediately
- Add `[force-mode]` prefix to all comments

```bash
if [ "$FORCE_MODE" = "true" ]; then
    # Minimal epic validation
    BODY=$(gh issue view "$epic" --json body --jq '.body')
    HAS_PHASES=$(echo "$BODY" | grep -c "### Phase")

    if [ "$HAS_PHASES" -ge 2 ]; then
        # Auto-approve and create Phase 1 issues
        create_phase_issues "$epic" 1
        gh issue comment "$epic" --body "**[force-mode] Epic Auto-Approved**

Phase 1 issues created. Epic will progress automatically.

---
*Automated by Champion role (force mode)*"
    fi
fi
```

---

## Return to Main Champion File

After completing epic evaluation work, return to the main champion.md file for completion reporting.
