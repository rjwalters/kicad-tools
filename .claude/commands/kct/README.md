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
| `/kct:help [<command>]` | Explain the installed `/kct:*` skills — what each does, how to invoke it, and the load-bearing conventions — by reading the files actually vendored in this repo. Introspective and strictly read-only; no board argument. | sonnet |
| `/kct:ee-review <issue-or-board>` | Produce an EE decision document (escalating intervention ladder + binding constraints, cited and confidence-graded) for an analog/placement-blocked board. Advisory only — decisions, not copper. | opus |
| `/kct:manufacturing-readiness <board-path>` | Sign off a routed board for fabrication: `kct check` + the mandatory `kicad-cli pcb drc --refill-zones` cross-gate + a `kct export` bundle at the board's fab tier. Refuses sign-off if any gate is skipped. | sonnet |
| `/kct:board-recipe-scaffold <board-path>` | Scaffold a new consumer board recipe (`generate_design.py`) following the artifact-first convention: project → schematic+ERC → PCB → route+pour → check → LVS hard gate → export, with circuit-specific parts left as fill-in points. | sonnet |
| `/kct:layout-journal <board-path>` | Keep a `LAYOUT_NOTES.md` journal for a hand-routing session so the reasoning behind manual copper (decisions, rip-ups, referee results, blockers) survives reboots and hand-offs. | sonnet |
| `/kct:tapeout <board-path> [--assembly\|--pcb-only]` | Produce a complete, fab-ready export bundle — or refuse loudly. Superset of `/kct:manufacturing-readiness`: the three sign-off pre-gates plus a BOM part-number-resolution gate, schematic + assembly-view PDFs, a human README, and a manifest checksumming the entire bundle. "tapeout returned 0" means "upload this directory as-is." | sonnet |
