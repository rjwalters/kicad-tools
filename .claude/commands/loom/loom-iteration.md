# Loom Daemon - Iteration Mode (DEPRECATED)

**This file is deprecated.** The two-tier LLM daemon (parent/iteration) was
removed in the v0.10.0 shepherd/daemon migration (epic #3372).

## Execution

This skill is superseded. Use `/loom:sweep <issue>` for the single-issue
lifecycle, or `mcp__loom__dispatch_sweep` against the Rust `loom-daemon` binary
for multi-account dispatch. See `.loom/docs/daemon-reference.md` for the current
daemon surface.

## Migration

The two-tier LLM architecture (parent/iteration) has been replaced by the Rust
`loom-daemon` binary plus `/loom:sweep`:

- **Old**: `/loom iterate` -> `loom-iteration.md` with full gh commands
- **New**: `/loom:sweep <issue>` (Tier 1) or `mcp__loom__dispatch_sweep` -> Rust `loom-daemon` (Tier 2)

Dispatch, registry tracking, pub/sub eventing, and cancellation now live in the
Rust `loom-daemon` binary's MCP surface (`mcp__loom__dispatch_sweep`,
`mcp__loom__list_sweeps`, `mcp__loom__subscribe_to_events`,
`mcp__loom__cancel_sweep`, …). The historical Python daemon brain
(`loom-tools/src/loom_tools/daemon_v2/`) was deleted in Phase 3 (#3378). See
[`docs/migration/v0.10.0-shepherd-deprecation.md`](https://github.com/rjwalters/loom/blob/main/docs/migration/v0.10.0-shepherd-deprecation.md).
