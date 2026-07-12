---
name: board-recipe-scaffold
invocation: /kct:board-recipe-scaffold
suggestedModel: sonnet
description: Scaffold a new consumer board recipe (generate_design.py) following the artifact-first convention — a project → schematic+ERC → PCB → route+pour → check → LVS-hard-gate → export pipeline with circuit-specific parts left as fill-in points.
---

# Board recipe scaffold

Scaffold a new `generate_design.py`-style board recipe for a consumer PCB, following the **artifact-first convention**: the committed board artifact is shipping truth, and regeneration may diverge by design. This skill emits the proven sequential pipeline (steps 1-9) as a template with the circuit-specific parts left as clearly marked fill-in points — it does **not** reproduce any specific board's circuit.

> **The `kct` namespace.** This skill lives in `.claude/commands/kct/` — the kicad-tools-native, harness-agnostic agent-tool namespace, invoked as `/kct:board-recipe-scaffold`. It runs from inside a **consumer repo** that depends on kicad-tools as a `uv` dependency. It hardcodes no board path and assumes nothing about the current directory being the kicad-tools repo.

> **Artifact-first (why this shape).** The recipe's job is to *generate* a board artifact; once committed, that artifact is the shipping truth. A later `python generate_design.py` run may diverge from the committed artifact (placement heuristics, router seed) — that is intentional, not a bug. The committed `*.kicad_pcb` and its `manufacturing/` bundle are what ships; the recipe documents *how* it was produced. This is Epic #4054 convention #3 expressed as code structure.

## Prerequisite

The native router/DRC backend must be built in the active checkout before the routing step (step 5) will run at full speed (Epic #4054 convention #1):

```bash
uv run kct build-native --check   # expect: "C++ backend: available"
uv run kct build-native           # if "not installed" (uv sync does NOT build it)
```

## Model selection

`suggestedModel: sonnet`. Emitting a well-understood pipeline template with marked fill-in points is a structured code-generation task, not a frontier-judgment one. Model resolves through the harness's normal precedence chain.

## Arguments

**Arguments**: `$ARGUMENTS`

`$ARGUMENTS` is `<board-path> [--name <slug>] [--mfr <tier>]`.

| Token | Meaning |
|-------|---------|
| `<board-path>` | **Required.** The directory for the new board recipe (e.g. the path where `generate_design.py` and its `output/` will live). The user supplies it; the scaffold writes into it. Never a hardcoded board directory. |
| `--name <slug>` | Project slug used for the `.kicad_pro` / output filenames. Optional; default derived from `<board-path>`'s basename. |
| `--mfr <tier>` | Fab tier for the export step. Optional; if omitted, leave a marked fill-in point and resolve it the same way `/kct:manufacturing-readiness` does (read the recipe/manifest or ask the user; validate against `kicad_tools.manufacturers.get_manufacturer_ids()` / `kct export --help`). **Do not hardcode a tier list.** |

## The recipe skeleton (steps 1-9)

Emit a `<board-path>/generate_design.py` with these nine steps as functions, and a `main()` that runs them in order, prints a summary, and gates the exit code. Everything in **‹angle brackets›** is a circuit-specific fill-in point the author must complete — do not invent a specific circuit.

1. **`create_project(output_dir, name)`** — minimal `.kicad_pro` via `kicad_tools.core.project_file.create_minimal_project`.

2. **`create_‹circuit›_schematic(output_dir)`** — build the schematic with `kicad_tools.schematic.models.schematic.Schematic`, add the ‹components, nets, wiring›, then `sch.validate()` (ERC-adjacent structural checks), then `sch.write(...)`. Return the `.kicad_sch` path.

3. **`run_erc(sch_path)`** — `kicad_tools.cli.runner.run_erc` + `kicad_tools.erc.ERCReport`. **Treat "kicad-cli not found" as skip-not-fail** (dev-machine tolerant) but **hard-fail on any ERC error violation**.

4. **`create_‹circuit›_pcb(output_dir)`** — build the `.kicad_pcb` with an explicit `NETS` dict and footprints placed at **0° / 45° / 90° / 135° only**. Non-multiple-of-45° rotations hit a footprint rotation-transform ambiguity (see issue #3737 for *why* — cited as rationale, not a required read). Return the unrouted PCB path.

5. **`route_pcb(input_path, output_path)`** — `kicad_tools.router.DesignRules` + `load_pcb_for_routing` + `router.route_all()` + `TraceOptimizer`; then pour: `kicad_tools.router.auto_pour.auto_pour_if_missing()` + `kicad_tools.cli.route_cmd._fill_zones_after_route()` for the power/ground pour nets. Return success (partial routing is tolerated — see `main()`).

6. **`run_drc(pcb_path)`** — subprocess the CLI: `kct check <pcb> --allow-incomplete`. The `--allow-incomplete` opt-in matters: it runs **before** the manufacturing bundle exists, so the Manifest sub-check would otherwise report NOT RUN and fail the whole gate. Hard-fail on real DRC errors.

7. **`run_lvs(sch_path, routed_pcb_path, output_dir)`** — `kicad_tools.lvs.write_lvs_report(..., require_clean=True, run_copper=True, run_label=True)`. **This is a HARD GATE, not optional.** LVS is what catches schematic/PCB divergence (a real polarity flip, issue #3747, slipped through everything else and was caught here). A dirty LVS raises; the recipe must exit non-zero rather than ship a diverging board.

8. **`export_manufacturing_bundle(routed_path, output_dir)`** — subprocess `kct export <routed.kicad_pcb> --output <output_dir>/manufacturing --mfr ‹tier› --skip-preflight`; then verify `manifest.json` was written. Resolve ‹tier› per the `--mfr` argument rules above (never hardcode a tier list). Run this **unconditionally** so the manifest mtime stays newer than the routed PCB even when routing is incomplete.

9. **`main()`** — run steps 1-8 in order, print a summary table (ERC / Routing / DRC / LVS / MFG), and **exit 0 only if ERC + DRC + LVS all pass**. **Routing partial-completion is tolerated and tracked separately** — surface this as a documented, explicit option in the summary (e.g. print `Routing: PARTIAL`), not a silent swallow. Wrap the body in try/except that prints the traceback and returns 1.

## The gating contract (make it explicit in `main()`)

```
return 0 if erc_success and drc_success and lvs_success else 1
```

- ERC, DRC, LVS are **hard gates** — any failure ⇒ non-zero exit.
- Routing may be **PARTIAL** and still exit 0, but the summary must say so plainly (documented option, not hidden).
- LVS specifically is never downgraded to advisory — it is the divergence catcher.

## What to leave as fill-in points (do NOT invent a circuit)

The scaffold ships the pipeline shape and the gating contract. The author fills in, per their actual board:

- schematic topology (components, values, net names, wiring) — step 2;
- footprint selection + placement coordinates (multiples of 45° only) — step 4;
- the `NETS` dict and pour-net names (power/ground) — steps 4-5;
- the fab `‹tier›` — step 8, resolved via the registry, never a hardcoded list.

Mark each of these `# TODO(author): ...` in the emitted template so they cannot be mistaken for done.

## What this skill does NOT do

- It does not choose or invent a specific circuit — only the pipeline scaffold + gating contract.
- It does not depend on any CI workflow, GitHub Actions context, or repo-internal `scripts/ci/*` gate. The recipe it emits runs as a plain `python generate_design.py <output_dir>` inside any consumer repo.
- It does not hardcode fab tiers; the tier set is owned by `kicad_tools.manufacturers`.

## References

- The nine-step skeleton is the validated artifact-first pipeline shape used across kicad-tools' own board fleet (validated end-to-end on the repo's own smallest fixture recipe). Issue #3737 (rotation-transform ambiguity → 45°-multiple placements) and #3747 (LVS caught a polarity divergence) are the *why* behind two of the constraints.
- `/kct:manufacturing-readiness` — the sibling sign-off skill for the committed artifact (adds the `kicad-cli pcb drc --refill-zones` cross-gate on top of `kct check` + `kct export`).
