# Judge Reference: Rationale

Background/"why" material for `defaults/.claude/commands/loom/judge-reference.md`,
split out to keep that file to actionable steps only.

## Why Scoped Test Execution Matters

| Metric | Full Suite | Scoped |
|--------|-----------|--------|
| Typical duration | 2-10 minutes | 10-60 seconds |
| Tests executed | All | Only affected |
| Confidence | Maximum | High (with caveats) |
| Use case | Config changes, first run | Focused code changes |

**Key principle**: Scoped execution is an optimization, not a replacement for
CI. The full test suite still runs in CI (step 8 verifies CI status). Scoped
execution gives the Judge faster local feedback during evaluation.
