# `kct` slash-command namespace

This directory holds **kicad-tools-native** agent skills, invoked as `/kct:<name>`.

## Why a separate namespace?

The `.claude/commands/loom/` tree (and `.loom/roles/`) is installed into this repo
*from* [rjwalters/loom](https://github.com/rjwalters/loom) and belongs to the loom
orchestration framework — kicad-tools should not add its own skills there. The `kct`
namespace is deliberately **harness-agnostic**: it hosts kicad-tools' own agent tools
so they live alongside any installable harness framework (loom,
[rjwalters/anvil](https://github.com/rjwalters/anvil), or a future orchestrator)
without colliding with vendored content.

**Convention:** kicad-tools-native agent tools go under `.claude/commands/kct/` and are
invoked as `/kct:<name>`. Do not place them under `.claude/commands/loom/` or `.loom/roles/`.

## Available skills

| Command | Purpose | Model |
|---------|---------|-------|
| `/kct:ee-review <issue-or-board>` | Produce an EE decision document (escalating intervention ladder + binding constraints, cited and confidence-graded) for an analog/placement-blocked board. Advisory only — decisions, not copper. | opus |
