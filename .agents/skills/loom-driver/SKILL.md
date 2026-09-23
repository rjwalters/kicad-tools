---
name: loom-driver
description: "Standard shell environment with no specialized role"
---
<!-- loom-managed-skill -->
<!-- GENERATED FILE — DO NOT EDIT DIRECTLY.
     Produced by `loom-daemon generate-agent-skills` from
     defaults/.claude/commands/loom/driver.md (the same source Claude Code
     reads as /loom:driver via .claude/commands/loom/). This is the
     cross-vendor skill-discovery surface (.agents/skills/<name>/SKILL.md)
     read natively by Codex, Kimi Code, Mistral Vibe, and Grok — see
     runtime-adapters.md §5. To change this file, edit the source above
     and re-run the generator; CI (`loom-daemon generate-agent-skills
     --check`) fails if this file is stale. -->

# Default Shell

You are working in a standard shell environment in this repository.

This is a plain terminal without any specialized role. You can use this for general shell commands, exploration, and ad-hoc tasks.

## Terminal Probe Protocol

When you receive a probe command, respond with: `AGENT:Driver:<brief-task>` — e.g. `AGENT:Driver:idle-awaiting-work`.

**The full probe protocol** (format, per-role examples, task-description conventions, and rationale) **lives in [`probe-protocol.md`](probe-protocol.md).**
