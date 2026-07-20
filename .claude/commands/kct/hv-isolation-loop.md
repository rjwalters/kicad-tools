---
name: hv-isolation-loop
invocation: /kct:hv-isolation-loop
suggestedModel: sonnet
description: Drive the HV-isolation / creepage design loop on a mains/high-voltage board — voltage-domain capture → per-pair creepage targets → HV plane-voids → HV-aware placement → route/reinforce → creepage + Kelvin + refill-zones gates → EE decision + fab sign-off handoff. Orchestration only; the human EE authors the voltage map and ratifies before fab.
---

# HV-isolation design loop

Drive a non-isolated-mains or high-voltage-bank board **from a raw floorplan to a creepage-closed, sign-off-ready layout**. This skill is the *orchestration layer* over the shipped `kct` HV commands: it sequences voltage-domain capture, per-pair creepage targets, HV plane-void generation, HV-aware placement, routing, and the isolation gates into one coherent runbook, then hands the decision and fab sign-off steps off to the sibling skills.

It **complements, not duplicates**, `/kct:ee-review` (which writes the *decision document*) and `/kct:manufacturing-readiness` / `/kct:tapeout` (which do *fab sign-off*). This skill drives the *layout to closure* and calls those skills at the right steps.

> **The `kct` namespace.** This skill lives in `.claude/commands/kct/` — the **kicad-tools-native**, harness-agnostic agent-tool namespace, invoked as `/kct:hv-isolation-loop`. Keep kicad-tools' own agent tools here, *not* under `.claude/commands/loom/` or `.loom/roles/`: that tree is installed into this repo *from* [rjwalters/loom](https://github.com/rjwalters/loom) and belongs to the loom framework. The `kct` namespace is deliberately harness-agnostic — it hosts kicad-tools-native skills that live alongside installable harness frameworks (loom, [rjwalters/anvil](https://github.com/rjwalters/anvil), or any future orchestrator). It runs from inside a **consumer repo** that depends on kicad-tools as a `uv` dependency (`kct ...` is on `PATH` via the venv); it does not assume the current directory is the kicad-tools repo, and it hardcodes no board path and no fab tier.

> **Scope (advisory / orchestration only).** This skill *sequences commands and states gates*. It does not itself route copper, place parts, or edit the `.kicad_pcb` beyond invoking the documented mutating commands, and it **never self-applies a work label**. The one **human-judgment** input — the voltage-map sidecar (net → volts) — is authored by a qualified EE, and the human EE ratifies the result before fabrication. Derived IEC creepage/clearance values are an **engineering aid, NOT a certification**: the governing standard and a qualified engineer remain authoritative (same disclaimer as `manufacturing-readiness` Gate 4).

## Prerequisite

The native router/DRC backend must be built in the active checkout before the routing/gate steps are meaningful (Epic #4054 convention #1). Run once per checkout / worktree:

```bash
uv run kct build-native --check   # expect: "C++ backend: available"
# if "not installed":
uv run kct build-native
```

`uv sync` does **not** build the native extension. A fresh checkout or git worktree needs this step explicitly.

## Model selection

`suggestedModel: sonnet`. Like `manufacturing-readiness`, this is a deterministic pipeline-orchestration task — author/collect the inputs, run each command in order, read exit codes and reports, and refuse to advance on any failure. It does not require frontier judgment (the frontier-judgment step, the EE decision, is delegated to `/kct:ee-review`, which is opus). Model resolves through the harness's normal precedence chain (explicit dispatch param → harness role config → this doc's frontmatter `suggestedModel` → session default).

## Arguments

**Arguments**: `$ARGUMENTS`

`$ARGUMENTS` is `<board-path> [--voltage-map <file>] [--standard {iec60664|iec62368}] [--pollution-degree {1|2|3}] [--material-group {I|II|IIIa|IIIb}] [--clearance <mm>] [--hv-threshold <V>]`.

| Token | Meaning |
|-------|---------|
| `<board-path>` | **Required.** Path to the `*.kicad_pcb` (or a board directory containing one) to drive through the loop. Everything the skill operates on derives from this token — never a hardcoded board directory. |
| `--voltage-map <file>` | Path to the shared voltage-map sidecar (see "The shared voltage-map sidecar" below). If omitted, Step 1 is where the human EE authors it; the loop cannot classify HV domains without it. |
| `--standard {iec60664\|iec62368}` | IEC insulation standard for the creepage/clearance lookup. Passed to both `creepage --standard` and `optimize-placement --creepage-standard` (default `iec60664`). |
| `--pollution-degree {1\|2\|3}` | IEC pollution degree (1=sealed, 2=typical indoor FR-4, 3=conductive). Default 2. |
| `--material-group {I\|II\|IIIa\|IIIb}` | Insulation material group by CTI. Default IIIa (conservative for common FR-4). |
| `--clearance <mm>` | Void distance for the HV plane-void step (`zones hv-keepout --clearance`, which is **required** by that command). Derive it from the Step-2 creepage/clearance report. |
| `--hv-threshold <V>` | Minimum cross-domain \|ΔV\| that triggers a placement creepage keepout (`optimize-placement --hv-threshold`, default 30.0 V). Lower-ΔV pairs rely on normal DRC clearance so low-voltage nets are not over-segregated. |

## The shared voltage-map sidecar (load-bearing — one file, two consumers)

**A single JSON voltage map `{net_name: volts}` feeds BOTH the HV-aware placement (Step 4) and the creepage gate (Step 6).** Do not maintain two files. Each value is the net's RMS working potential (volts) about a common reference. Conventions (verified against `kct creepage --help` / `kct optimize-placement --help`):

- Reserved key `_edge_voltage` sets the board-edge/earth reference (default 0 V).
- Other `_`-prefixed keys (e.g. `_comment`) are ignored.
- **Unmapped nets default to 0 V.**
- Potentials are worst-case DC-equivalent magnitudes — AC phase is not modelled, so \|ΔV\| is conservative for in-phase nets.

```json
{ "_comment": "softstart rev-C domains",
  "_edge_voltage": 0,
  "/AC_LINE": 150,
  "/AC_NEUTRAL": 0,
  "/V_AC_SENSE_RAW": 150,
  "/SCAP_POS": 90 }
```

**Authoring this sidecar (net → volts) is the one human-EE-judgment step the whole loop keys on.** Classify every net into a domain (mains vs bank vs logic vs analog) and record its worst-case working potential. Sense nets *derived from* HV (e.g. `V_AC_SENSE_RAW` tapped off `AC_LINE`) carry the HV potential until a divider drops them — map them at the HV value.

> The placement command also accepts a manual fallback, `--hv-domains <file>` (`{domain_id: {"refs": [globs], "voltage": v}}`), **mutually exclusive** with `--voltage-map`. Prefer the shared voltage map so placement and the creepage gate agree; reach for `--hv-domains` only when a per-footprint declaration is easier than a per-net one.

## The design loop (verified command sequence)

Run in order. Each mutating step supports `--dry-run` — rehearse before committing copper.

### Step 1 — Author the voltage-map sidecar (human EE judgment)

Produce `<board>/vmap.json` per "The shared voltage-map sidecar" above. This is the EE input the loop keys on; nothing downstream can classify HV domains without it. This step is a human decision, not a `kct` command.

### Step 2 — Read the per-pair creepage/clearance targets (no copper mutated)

Derive the required creepage/clearance **per net pair** from \|V_a − V_b\| before touching copper, so you know the void distance and segregation the later steps must achieve:

```bash
kct creepage <board.kicad_pcb> \
  --voltage-map <board>/vmap.json \
  --standard iec60664 --pollution-degree 2 \
  --format table
```

- With `--voltage-map` + `--standard`, the requirement is derived **per pair** (same-potential nets require ~0; cross-domain pairs use their real \|ΔV\|) instead of one global `--working-voltage`.
- `--format json` gives a machine-readable census if you want to script the void distance for Step 3.
- Read the largest cross-domain requirement — that (plus margin) is the `--clearance` you feed `zones hv-keepout`.

### Step 3 — Void inner planes around HV copper

Cut the plane pours back from HV nets so an inner-layer plane cannot short the isolation barrier:

```bash
kct zones hv-keepout <board.kicad_pcb> \
  --clearance <mm> \
  --net-class-map <board>/net_class_map.json \
  [--plane-layers <csv>] \
  [--dry-run] [--refill]
```

- **`--clearance <mm>` is REQUIRED** — the void distance from HV copper (derive from Step 2).
- HV nets are selected by `--net-class HV` (default) or a `--net-class-map <sidecar>`.
- `--plane-layers <csv>` defaults to all layers carrying a plane pour; `-o/--output` defaults to **overwriting the input**.
- `--dry-run` shows the planned voids without writing; `--refill` runs `kicad-cli pcb drc --refill-zones` after writing so the pours are current.

### Step 4 — HV-aware re-floorplan

Re-place so cross-domain footprints are pushed apart to their required creepage:

```bash
kct optimize-placement <board.kicad_pcb> \
  --voltage-map <board>/vmap.json \
  --creepage-standard iec60664 --pollution-degree 2 \
  --hv-threshold 30
```

- `--voltage-map` and `--hv-domains` are **mutually exclusive**; use the shared voltage map.
- `--hv-threshold VOLTS` (default 30.0) is the minimum cross-domain \|ΔV\| that triggers a creepage keepout — lower-ΔV pairs rely on normal DRC clearance to avoid over-segregating low-voltage nets.
- **Absent a map/domains file the objective is byte-identical to the voltage-blind default** — HV-awareness is strictly opt-in, so passing the map is what activates domain segregation.
- The `--weights` JSON accepts a `creepage` key alongside `overlap/drc/boundary/wirelength/area` if you need to tune the creepage term.

### Step 5 — Route + reinforce

Use the existing 0.18 flow — no new flags here:

```bash
kct route-auto <board.kicad_pcb>       # HV-outer + ampacity considerations apply
kct reinforce <board.kicad_pcb> --all-runs
```

HV nets should route on outer layers (creepage is a surface path) and to their ampacity width. These are standard-flow considerations, referenced here, not new flags introduced by this skill.

### Step 6 — Gates (all must pass)

1. **Creepage gate** — re-run against the *routed* board:
   ```bash
   kct creepage <board.kicad_pcb> \
     --voltage-map <board>/vmap.json \
     --standard iec60664 --pollution-degree 2
   ```
   **Exit codes are the gate:** non-zero (exit 1) iff any pair fails its governing bound. **A distinct exit code (2, `EXIT_HV_UNCLASSIFIED`) fires when NO HV net could be classified on a board that looks like mains** (mains-named copper or a mains-level working voltage was supplied) — the *false-pass safety gate*: it prevents an un-evaluated HV path being mistaken for a clean board. An empty census on a genuinely low-voltage board is a clean **exit 0**. If both `--min` and `--standard` are supplied, the **stricter** of {manual, derived} governs per pair.
2. **Kelvin / current-sense integrity** — for boards with current-sense taps:
   ```bash
   kct analyze current-sense <board.kicad_pcb>
   ```
3. **The mandatory referee cross-gate (non-skippable):**
   ```bash
   kicad-cli pcb drc --refill-zones <board.kicad_pcb>
   ```
   This is a *second, independent* DRC engine and it is **not optional** — `--refill-zones` forces KiCad to refill the pours before checking, catching the stale-zone-fill false passes that `kct check` alone has missed (repo convention, 2026-07-04 live-short defect). Require **0 new errors**. A missing `kicad-cli` is a hard blocker, not a pass.

### Step 7 — Decision + sign-off handoff (do NOT duplicate those skills)

- **EE decision document:** hand the "which parts may move + binding constraints" question to **`/kct:ee-review <issue-or-board>`** (opus). It writes the escalating-ladder decision doc; this loop does not re-implement it.
- **Fab sign-off:** hand off to **`/kct:manufacturing-readiness <board-path>`** (its Gate 4 already runs `kct audit --hv-standard ...`) or **`/kct:tapeout <board-path>`** for the full fab-ready bundle. This loop does not re-implement sign-off.

## Guardrails (must hold — match sibling-skill conventions)

- **Advisory / orchestration only.** The skill sequences commands and states gates; it does not itself route copper, place parts, or edit the `.kicad_pcb` beyond invoking the documented mutating commands, and it **never self-applies a work label**. The human EE authors the voltage map and ratifies before fab.
- **Derived IEC values are an engineering aid, NOT a certification.** The governing standard plus a qualified engineer remain authoritative (same wording as `manufacturing-readiness` Gate 4).
- **The `kicad-cli pcb drc --refill-zones` cross-gate is mandatory and non-skippable** — it catches stale-zone-fill false passes that `kct check` alone misses.
- **One shared voltage-map sidecar** drives *both* placement (Step 4) and the creepage gate (Step 6) — do not maintain two files.
- **No hardcoded fab tier, no invented flags.** Reference only the verified flags above; resolve the fab tier the way `manufacturing-readiness` does (from the board's own recipe/manifest, validated against `kct`'s registry), never a memorized list.

## Relationship to existing skills (not a duplicate)

| Skill | Owns | This loop's relationship |
|-------|------|--------------------------|
| `/kct:ee-review` | The *decision document* (escalating intervention ladder + binding constraints, cited/confidence-graded). | Step 7 hands the EE decision off to it; this loop drives the layout, not the decision. |
| `/kct:manufacturing-readiness` / `/kct:tapeout` | Fab sign-off (their HV Gate 4 wraps `kct audit --hv-standard`). | Step 7 hands final sign-off off to them; this loop does not re-implement sign-off. |

No existing `kct` skill covers the domains → voids → placement → creepage loop; the three siblings above are complementary, not overlapping.

## References

- `kct creepage --help` / `kct zones hv-keepout --help` / `kct optimize-placement --help` — the authoritative flag surfaces this skill cites (verified against `src/kicad_tools/cli/parser.py`).
- **#4371 / #4372 / #4373** — the three capability requests this skill orchestrates (per-net voltage map + pairwise \|ΔV\| creepage; `kct zones hv-keepout`; HV-aware `optimize-placement --voltage-map`), merged via #4383 / #4382 / #4384.
- **#4354** — the creepage false-pass safety gate (`EXIT_HV_UNCLASSIFIED = 2`) referenced in Step 6.
- `/kct:ee-review`, `/kct:manufacturing-readiness`, `/kct:tapeout` — the sibling `kct` skills this loop hands off to (it does not duplicate them).
- The `--refill-zones` cross-gate convention (Epic #4054 convention #2) — established after a 2026-07-04 defect where `kct check` alone missed a live short and read stale zone fills.
