# `kct` skill namespace

This directory holds **kicad-tools-native** agent skills, invoked as `{{skill:<name>}}`.

## Why a separate namespace?

The `loom` skill namespace (and `.loom/roles/`) is installed into this repo
*from* [rjwalters/loom](https://github.com/rjwalters/loom) and belongs to the loom
orchestration framework — kicad-tools should not add its own skills there. The `kct`
namespace is deliberately **harness-agnostic**: it hosts kicad-tools' own agent tools
so they live alongside any installable harness framework (loom,
[rjwalters/anvil](https://github.com/rjwalters/anvil), or a future orchestrator)
without colliding with vendored content.

**Convention:** kicad-tools-native agent tools go under `{{skills-dir}}` and are
invoked as `{{skill:<name>}}`. Do not place them in the `loom` namespace or `.loom/roles/`.

**Harness-neutral text.** Skill bodies are shared by every harness kicad-tools
installs into, so the source never hard-codes one harness's invocation syntax or
install paths: the installer fills those in per harness. See
`docs/agent-surfaces.md` in the kicad-tools repository for the placeholder
vocabulary and what each skill needs from its harness.

## Available skills

| Command | Purpose | Model |
|---------|---------|-------|
| `{{skill:help}} [<command>]` | Explain the installed `{{skill:*}}` skills — what each does, how to invoke it, and the load-bearing conventions — by reading the files actually vendored in this repo. Introspective and strictly read-only; no board argument. | sonnet |
| `{{skill:ee-review}} <issue-or-board>` | Produce an EE decision document (escalating intervention ladder + binding constraints, cited and confidence-graded) for an analog/placement-blocked board. Advisory only — decisions, not copper. | opus |
| `{{skill:manufacturing-readiness}} <board-path>` | Sign off a routed board for fabrication: `kct check` + the mandatory `kicad-cli pcb drc --refill-zones` cross-gate + a `kct export` bundle at the board's fab tier. Refuses sign-off if any gate is skipped. | sonnet |
| `{{skill:hv-isolation-loop}} <board-path>` | Drive the HV-isolation / creepage design loop on a mains/high-voltage board: voltage-domain capture → per-pair creepage targets → HV plane-voids (`zones hv-keepout`) → HV-aware placement (`optimize-placement --voltage-map`) → route/reinforce → creepage + Kelvin + `--refill-zones` gates → EE-review + sign-off handoff. Orchestration only; the human EE authors the voltage map and ratifies before fab. | sonnet |
| `{{skill:board-recipe-scaffold}} <board-path>` | Scaffold a new consumer board recipe (`generate_design.py`) following the artifact-first convention: project → schematic+ERC → PCB → route+pour → check → LVS hard gate → export, with circuit-specific parts left as fill-in points. | sonnet |
| `{{skill:layout-journal}} <board-path>` | Keep a `LAYOUT_NOTES.md` journal for a hand-routing session so the reasoning behind manual copper (decisions, rip-ups, referee results, blockers) survives reboots and hand-offs. | sonnet |
| `{{skill:tapeout}} <board-path> [--assembly\|--pcb-only]` | Produce a complete, fab-ready export bundle — or refuse loudly. Superset of `{{skill:manufacturing-readiness}}`: the three sign-off pre-gates plus a BOM part-number-resolution gate, schematic + assembly-view PDFs, a human README, and a manifest checksumming the entire bundle. "tapeout returned 0" means "upload this directory as-is." | sonnet |
