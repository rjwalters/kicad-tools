# Simple LED (Hello World)

The smallest board in the repo: a 5 V power header, a current-limiting resistor,
and an LED. Three components, three nets. It exists to exercise the *entire*
kicad-tools pipeline — project → schematic → ERC → PCB → zones → route → DRC →
LVS → manufacturing bundle — at the smallest scale where every stage still has
something to do, and its end-to-end result is pinned in CI.

## Quick Start

```bash
# One-command build (recommended)
kct build boards/00-simple-led

# Or run specific steps
kct build boards/00-simple-led --step schematic
kct build boards/00-simple-led --step pcb
kct build boards/00-simple-led --step route
kct build boards/00-simple-led --step verify

# Preview what would happen, touching nothing
kct build boards/00-simple-led --dry-run
```

`kct build` discovers `generate_design.py` generically (see
`src/kicad_tools/cli/build_cmd.py`), so no board-specific registration is
needed. The default manufacturer profile is JLCPCB.

Verify the committed artifacts without rebuilding anything:

```bash
kct check boards/00-simple-led/output/simple_led_routed.kicad_pcb
# Overall:   PASSED   (DRC + ERC + LVS + Manifest)

kct net-status boards/00-simple-led/output/simple_led_routed.kicad_pcb
# Summary: 3 nets total — Complete: 3 (100% connected)
```

## Circuit Overview

```
    +5V ──── J1.1 ────┬──────────── VCC   (pour)
                      │
                     [R1]
                     330R
                      │
                      ├──────────── LED_ANODE
                      │
                     (D1)  LED, Vf ≈ 2 V
                      │
    GND ──── J1.2 ────┴──────────── GND   (pour)
```

Three nets: **`VCC`**, **`LED_ANODE`**, **`GND`**. `VCC` and `GND` are pour
nets (copper zones); `LED_ANODE` is the single trace-routed signal net — which
is exactly why this board is the cheapest possible end-to-end pipeline test.

### Resistor sizing

```
R = (V_in − V_f) / I = (5 V − 2 V) / 10 mA = 300 Ω  →  330 Ω (nearest E24)
```

330 Ω delivers roughly **9 mA**, which is safe and clearly visible. The
rationale is recorded in `generate_design.py`'s module docstring and under
`decisions:` in `project.kct`.

## Components

| Reference | Description | Value | Footprint | Part | LCSC |
|-----------|-------------|-------|-----------|------|------|
| J1 | Power input header (VCC, GND) | — | `PinHeader_1x02_P2.54mm_Vertical` | B-2100S02P-A110 | C49257 |
| R1 | Current-limiting resistor | 330 Ω | `R_0805_2012Metric` | 0805W8F3300T5E | C17630 |
| D1 | Indicator LED | Vf ≈ 2 V | `LED_D5.0mm` | C503B-RCN-CW0Z0AA1 | C84256 |

The 5 mm through-hole LED plus the 0805 SMD resistor make this a deliberately
mixed THT/SMD board — see the `decisions:` block in `project.kct`.

## Design Specifications

| Parameter | Value |
|-----------|-------|
| Input Voltage | 5 V |
| LED Forward Voltage | ~2 V |
| Target LED Current | 10 mA (≈9 mA actual with 330 Ω) |
| Board Size | 25 mm × 20 mm |
| Layers | 2 |
| Nets | 3 (`VCC`, `LED_ANODE`, `GND`) |
| Target Fab | JLCPCB |

## Files

| File | Description |
|------|-------------|
| `generate_design.py` | The recipe: builds project, schematic, PCB, routes, runs ERC/DRC/LVS, exports the manufacturing bundle. Accepts an optional output directory as `argv[1]`. |
| `project.kct` | Project specification — intent, requirements, component list, BOM/LCSC mapping, and the `decisions:` log. |
| `output/simple_led.kicad_sch` | Generated schematic |
| `output/simple_led.kicad_pcb` | Generated unrouted PCB (+ `.kicad_pro`, `.kicad_prl`, `.kicad_pcb.kct.json` sidecar) |
| `output/simple_led_routed.kicad_pcb` | Routed PCB (+ `.kicad_pro`, `.kicad_prl`, `.kicad_dru` design rules) |
| `output/lvs.json` | Copper + label LVS report; `clean: true` on the committed board |
| `output/drc_report.json` | DRC snapshot from the run that produced these artifacts |
| `output/erc_report.json` | `kicad-cli sch erc` JSON report |
| `output/manufacturing/` | Full fab bundle — see below |

The `output/manufacturing/` bundle contains `gerbers/gerbers.zip`,
`bom_jlcpcb.csv`, `cpl_jlcpcb.csv`, `kicad_project.zip`, `manifest.json`,
`README.txt`, `report.md`, `report.pdf`, and `images/` (front/back/copper/
assembly renders plus per-layer PNGs and the schematic render).

`output/board.json` is **not** committed — it is the public data contract
emitted on demand by `kct board-metrics`, which CI runs against a temporary
regeneration (below).

## Regenerating: always target a scratch directory

```bash
uv run python boards/00-simple-led/generate_design.py /tmp/board00
```

**Do not regenerate into the committed `output/` tree.** `generate_design.py`
falls back to `<board_dir>/output` when no `argv[1]` is given, which rewrites
tracked artifacts; `tests/conftest.py` additionally fails the test suite if
tracked files under `boards/**/output/` are modified during a test run. Passing
an explicit scratch directory is exactly what CI does, and it keeps a
verification run out of your diff.

If you *intend* to refresh the committed artifacts, do it as a deliberate,
reviewed change — not as a side effect of checking that the recipe still works.

## CI gate: `Board 00 End-to-End`

The `Board 00 End-to-End` job in `.github/workflows/ci.yml` runs on pushes to
`main` and on pull requests targeting `main`, inside a `kicad/kicad:10.0`
container. It is the contract this board must keep:

1. `uv sync --frozen --extra dev --python 3.12`, then `uv run kct build-native`
   — `uv sync` does not build the C++ router extension, and without it routing
   falls back to slow pure Python.
2. Regenerate into a clean temp directory:
   `uv run python boards/00-simple-led/generate_design.py /tmp/board00-ci`.
   The committed `output/` tree is never touched.
3. `uv run kct check /tmp/board00-ci/simple_led_routed.kicad_pcb` must print
   `Overall: PASSED` — a grep on `^Overall:[[:space:]]+PASSED` gates the build,
   so DRC **and** ERC **and** LVS **and** Manifest must all pass. No
   `--allow-incomplete`.
4. Mirror the regen into a `<board_dir>/output/` shape and emit `board.json`
   with `uv run kct board-metrics /tmp/board00-staging/00-simple-led`.
5. `scripts/ci/check_net_status.py` asserts plane-net completion — the raw
   `total_unconnected_pads` count, so a floating `VCC`/`GND` pad cannot hide
   behind the advisory power/plane classification (#4531).
6. `scripts/ci/check_board_00_e2e.py` asserts the artifact contract: every
   expected artifact present (schematic, PCB, routed PCB, `lvs.json`,
   `manufacturing/manifest.json`), `lvs.json` `clean: true`, and `board.json`
   reporting `status: ok`, `drc_violations: 0`, `lvs_clean: true`.

Note that the gate re-derives DRC from scratch rather than trusting the
committed `drc_report.json`; the recipe's own fast-fail gates (partial-route
detection, `write_lvs_report(require_clean=True)`) run inside step 2. Whether
this job is a *required* status check on `main` is a separate repo-admin
setting — it landed advisory, per the note in the job's own comment block.

## Why This Board?

It is the minimum viable proof that the toolchain works end to end. One signal
net means routing can never be the interesting failure — so when this job goes
red, the break is in the pipeline (schematic emission, zone creation, LVS,
export, manifest freshness), not in the router. That makes it the fastest
possible canary and the right place to start reading if you are new to the
repo.

## Related

- [`../README.md`](../README.md) — the board index and status table
- [01-voltage-divider](../01-voltage-divider/) — next step up: 4 components, a divider, and the same pipeline
- [02-charlieplex-led](../02-charlieplex-led/) — where routing starts to be the hard part
