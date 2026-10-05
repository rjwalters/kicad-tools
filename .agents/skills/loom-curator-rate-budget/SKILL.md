---
name: loom-curator-rate-budget
description: "Why `curator.md`'s per-issue recipes are REST, and what is still unmeasured."
---
<!-- loom-managed-skill -->
<!-- GENERATED FILE — DO NOT EDIT DIRECTLY.
     Produced by `loom-daemon generate-agent-skills` from
     defaults/.claude/commands/loom/curator-rate-budget.md (the same source Claude Code
     reads as /loom:curator-rate-budget via .claude/commands/loom/). This is the
     cross-vendor skill-discovery surface (.agents/skills/<name>/SKILL.md)
     read natively by Codex, Kimi Code, Mistral Vibe, and Grok — see
     runtime-adapters.md §5. To change this file, edit the source above
     and re-run the generator; CI (`loom-daemon generate-agent-skills
     --check`) fails if this file is stale. -->

# Curator: GraphQL/REST Budget (#10039)

Why `curator.md`'s per-issue recipes are REST, and what is still unmeasured.

## Why

`gh issue view/edit/close` are GraphQL (`edit --add-label` also pages the label
list). #10039 reports ~8-10 GraphQL requests per curated issue, and 8 parallel
Curators on one identity draining its pool for the hour (supplied, not
re-measured). The gate takes the min of `rate_limit`'s `core` and `graphql`
resources: one free REST read that still sees GraphQL run out. The ~500 floor
and ~3-Curator cap are policy defaults, not vendor limits; nothing enforces the
cap across repos.

## Static per-pass GraphQL cost (from the recipes, not measured)

Per claimed issue: 0 for the gate; a `loom:blocked` re-check adds
`closedByPullRequestsReferences` and a `gh pr view` per linked PR. Per pass:
the list query. Closes stay on `gh issue close` (~1 mutation each) so
`guards.reversibleGh` still asks; closes are rare, so the cost is negligible.
`gh-cached` uses `gh` (GraphQL) only when `loom-daemon` declines a read; helper
scripts' internal calls are not counted.

## Measurement (close-blocking on #10039, not done)

Same identity, concurrent activity recorded: read `used` from both pools,
curate 15 issues, read again; repeat on a comparable batch with the old
prompt. Report deltas, commands and timestamps; if background activity cannot
be excluded, say so instead of claiming a precise saving.
