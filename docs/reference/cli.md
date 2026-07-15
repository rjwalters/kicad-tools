# CLI Reference

Complete reference for the `kct` (kicad-tools) command-line interface.

---

## Global Options

```bash
kct [--help] [--version] <command> [options]
```

| Option | Description |
|--------|-------------|
| `--help`, `-h` | Show help message |
| `--version`, `-V` | Show version number |

---

## Commands Overview

| Category | Command | Description |
|----------|---------|-------------|
| **Analysis** | `symbols` | List and query schematic symbols |
| | `nets` | Trace and analyze nets |
| | `sch` | Schematic analysis tools |
| | `pcb` | PCB query tools |
| **Validation** | `erc` | Parse ERC reports |
| | `drc` | Parse DRC reports with manufacturer rules |
| | `check` | Pure Python DRC (no kicad-cli) |
| | `validate` | Schematic-to-PCB sync validation |
| **Manufacturing** | `bom` | Generate bill of materials |
| | `mfr` | Manufacturer tools and rules |
| | `fleet status` | Fleet-wide routing + manufacturing readiness survey |
| | `stitch` | Add via stitching to power planes |
| | `build` | One-shot pipeline (schematic → PCB → manufacturing) |
| **Libraries** | `lib` | Symbol library tools |
| | `footprint` | Footprint generation tools |
| | `parts` | LCSC parts lookup and search |
| | `datasheet` | Datasheet search and PDF parsing |
| **PCB Operations** | `route` | Autoroute a PCB |
| | `zones` | Add copper pour zones |
| | `placement` | Detect and fix placement conflicts |
| | `optimize-placement` | CMA-ES placement optimizer (anchor-aware) |
| | `optimize-traces` | Optimize PCB traces |
| **AI Integration** | `reason` | LLM-driven PCB layout reasoning |
| | `mcp` | MCP server for AI agent integration |
| **Analysis (v0.7)** | `analyze congestion` | Routing congestion hotspots |
| | `analyze trace-lengths` | Timing-critical trace analysis |
| | `analyze thermal` | Thermal hotspot detection |
| | `analyze signal-integrity` | Crosstalk and impedance analysis |
| | `erc explain` | ERC root cause analysis |
| | `constraints check` | Constraint conflict detection |
| | `net-status` | Net connectivity validation |
| **Cost (v0.7)** | `estimate cost` | Manufacturing cost estimation |
| | `parts availability` | LCSC stock checking |
| | `suggest alternatives` | Alternative part suggestions |
| **Utilities** | `config` | View/manage configuration |
| | `interactive` | Launch interactive REPL mode |

---

## Analysis Commands

### `symbols`

List and query schematic symbols.

```bash
kct symbols <schematic> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json,csv}` | Output format (default: table) |
| `--filter PATTERN` | Filter by reference pattern (e.g., "R*") |
| `--lib LIB_ID` | Filter by library ID |
| `--verbose`, `-v` | Show additional details |

**Examples:**
```bash
kct symbols project.kicad_sch
kct symbols project.kicad_sch --format json
kct symbols project.kicad_sch --filter "C*"  # All capacitors
```

---

### `nets`

Trace and analyze nets in a schematic.

```bash
kct nets <schematic> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json}` | Output format (default: table) |
| `--net NAME` | Show only specified net |
| `--stats` | Show net statistics |

**Examples:**
```bash
kct nets project.kicad_sch
kct nets project.kicad_sch --net VCC
kct nets project.kicad_sch --stats --format json
```

---

### `sch`

Schematic analysis subcommands.

```bash
kct sch <subcommand> <schematic> [options]
```

| Subcommand | Description |
|------------|-------------|
| `summary` | Show schematic summary |
| `symbols` | List symbols with details |
| `labels` | List all labels |
| `wires` | List wire segments |
| `hierarchy` | Show sheet hierarchy |
| `validate` | Validate schematic structure |
| `pin-positions` | Get symbol pin positions |
| `check-connections` | Check for unconnected pins |
| `find-unconnected` | Find unconnected nets |

**Examples:**
```bash
kct sch summary project.kicad_sch
kct sch hierarchy project.kicad_sch --format json
kct sch find-unconnected project.kicad_sch
```

---

### `pcb`

PCB query subcommands.

```bash
kct pcb <subcommand> <pcb_file> [options]
```

| Subcommand | Description |
|------------|-------------|
| `query` | Query footprints and tracks |
| `modify` | Modify PCB elements |

**Examples:**
```bash
kct pcb query board.kicad_pcb --footprints
kct pcb query board.kicad_pcb --tracks --net GND
```

---

## Validation Commands

### `erc`

Parse and analyze ERC (Electrical Rule Check) reports.

```bash
kct erc <report> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json}` | Output format (default: table) |
| `--errors-only` | Show only errors, not warnings |
| `--type TYPE` | Filter by error type |
| `--sheet SHEET` | Filter by sheet name |

**Examples:**
```bash
kct erc project.erc
kct erc project.erc --errors-only --format json
kct erc project.erc --type "unconnected"
```

---

### `drc`

Parse DRC reports with optional manufacturer rule comparison.

```bash
kct drc <report> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json}` | Output format (default: table) |
| `--errors-only` | Show only errors |
| `--type TYPE` | Filter by violation type |
| `--net NET` | Filter by net name |
| `--mfr {jlcpcb,oshpark,pcbway,seeed}` | Apply manufacturer rules |
| `--layers N` | Number of layers (default: 2) |
| `--compare` | Compare manufacturer rules |

**Examples:**
```bash
kct drc board.drc
kct drc board.kicad_pcb --mfr jlcpcb
kct drc --compare  # Show all manufacturer rules
```

---

### `check`

Pure Python DRC (no kicad-cli required).

```bash
kct check <pcb_file> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json}` | Output format |
| `--mfr {jlcpcb,oshpark,pcbway}` | Manufacturer rules |
| `--rules RULES` | Custom rules file |
| `--refill-zones` | Refill zone fills in-place via `kicad-cli pcb drc --refill-zones --save-board` before checking (mutates the board file; kills stale-fill clearance false positives, #4113) |

**Examples:**
```bash
kct check board.kicad_pcb
kct check board.kicad_pcb --mfr jlcpcb --format json
```

---

### `validate`

Validate schematic-to-PCB synchronization.

```bash
kct validate --sync <schematic> <pcb>
```

**Examples:**
```bash
kct validate --sync project.kicad_sch board.kicad_pcb
```

---

## Manufacturing Commands

### `bom`

Generate bill of materials.

```bash
kct bom <schematic> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json,csv}` | Output format (default: table) |
| `--group` | Group identical components |
| `--exclude PATTERN` | Exclude by reference (can repeat) |
| `--include-dnp` | Include DNP (Do Not Place) parts |
| `--sort {reference,value,footprint}` | Sort order |
| `--output`, `-o` | Output file |

**Examples:**
```bash
kct bom project.kicad_sch
kct bom project.kicad_sch --format csv --group -o bom.csv
kct bom project.kicad_sch --exclude "TP*" --exclude "H*"
```

---

### `mfr`

Manufacturer tools and design rules.

```bash
kct mfr <subcommand> [options]
```

| Subcommand | Description |
|------------|-------------|
| `rules` | Show manufacturer design rules |
| `compare` | Compare multiple manufacturers |
| `dru` | Generate KiCad DRU file |

**Examples:**
```bash
kct mfr rules jlcpcb
kct mfr compare jlcpcb oshpark
kct mfr dru jlcpcb -o jlcpcb.dru
```

---

### `fleet status`

Survey routing and manufacturing readiness across every board in the repo.
Implemented in [`src/kicad_tools/cli/fleet_cmd.py`](../../src/kicad_tools/cli/fleet_cmd.py).

```text
usage: kicad-tools fleet status [-h] [--boards-dir FLEET_BOARDS_DIR]
                                [--format {table,json}] [--ship-only]
                                [--include-stale] [--pattern FLEET_PATTERN]
```

| Option | Description |
|--------|-------------|
| `--boards-dir DIR` | Root containing per-board subdirs (default: `boards`) |
| `--format {table,json}` | Output format (default: `table`) |
| `--ship-only` | Show only ship-ready boards in table output |
| `--include-stale` | (Reserved) treat stale artifacts as not shippable |
| `--pattern GLOB` | Glob to identify the routed PCB inside `output/` (default: `*_routed.kicad_pcb`) |

Each board is scored on net completion, DRC status, and presence of the
required manufacturing artifacts (gerbers, BOM, CPL). A board is "ship-ready"
when every gate is green; otherwise the row lists the first blocker. The
table view summarises one board per line; `--format json` emits the full
per-board breakdown for downstream tooling.

**Examples:**
```bash
# Quick "what's shippable?" overview across the fleet
kct fleet status

# CI-friendly machine output
kct fleet status --format json > fleet.json

# Restrict to a custom layout (boards live under hardware/v2/...)
kct fleet status --boards-dir hardware/v2 --pattern '*-final.kicad_pcb'

# Only show what is ready to ship
kct fleet status --ship-only
```

See also: [Manufacturing Export → ship-ready check](../guides/manufacturing-export.md#are-we-ship-ready-kct-fleet-status).

---

### `stitch`

Add via stitching to power-plane nets. Implemented in
[`src/kicad_tools/cli/stitch_cmd.py`](../../src/kicad_tools/cli/stitch_cmd.py).

```bash
kct stitch <pcb_file> [options]
```

| Option | Description |
|--------|-------------|
| `--net NET`, `-n` | Net to stitch (repeatable). Default: auto-detect power-plane nets from zones |
| `--via-size MM` | Via pad diameter in mm (default: 0.45) |
| `--drill MM` | Via drill in mm (default: 0.2) |
| `--clearance MM` | Minimum clearance from existing copper (default: 0.2) |
| `--offset MM` | Max distance from pad center for via placement (default: 0.5) |
| `--target-layer LAYER`, `-t` | Target plane layer (e.g. `In1.Cu`). Default: auto |
| `--trace-width MM` | Width of pad-to-via trace segments (default: 0.2) |
| `--escape-distance MM` | Max escape trace length for dense IC pads (default: 3.0) |
| `--blanket`, `-b` | Place vias on a grid across zone polygons |
| `--spacing MM` | Grid spacing for blanket stitching (default: 3.0) |
| `--mfr NAME`, `--manufacturer NAME` | Manufacturer profile (e.g. `jlcpcb`, `jlcpcb-tier1`) — overrides `--via-size`/`--drill` |
| `--copper OZ` | Outer copper weight in oz (default: 1.0); selects the correct row from the manufacturer YAML |
| `--dry-run`, `-d` | Show changes without applying |
| `-o`, `--output PATH` | Output file (default: modify in place) |
| `--drc` | Run DRC after stitching (fills zones via `kicad-cli`) |

**Manufacturer-driven via dimensions.** When `--mfr` is set the stitch via
size and drill are resolved from the manufacturer YAML using the board's
actual copper layer count, **overriding** `--via-size` and `--drill`. This
keeps stitching dimensions consistent with the rules the router and DRC are
already enforcing. Use `--copper` to pick the right stackup row (1.0 oz vs.
2.0 oz).

**Examples:**
```bash
# Auto-detect power nets and stitch with default 0.45/0.2 vias
kct stitch board.kicad_pcb

# Use the JLCPCB tier-1 profile (smaller vias, fine-pitch friendly)
kct stitch board.kicad_pcb --mfr jlcpcb-tier1 --copper 1.0

# Blanket-stitch a specific net on a 3mm grid
kct stitch board.kicad_pcb --net GND --blanket --spacing 3.0
```

---

### `build`

One-shot pipeline that runs schematic generation, sync, PCB layout, routing,
stitching, manufacturing artefacts and verification in order. Implemented in
[`src/kicad_tools/cli/build_cmd.py`](../../src/kicad_tools/cli/build_cmd.py).

```bash
kct build [SPEC] [--step STEP] [options]
```

`SPEC` is a positional argument (`.kct` file or project directory). Defaults
to the current working directory if omitted.

| Step (`--step`) | Purpose |
|-----------------|---------|
| `schematic` | Generate `.kicad_sch` from the design spec |
| `erc` | Run ERC against the schematic |
| `pcb` | Generate a fresh `.kicad_pcb` skeleton |
| `sync` | Sync footprints/nets from schematic into the PCB |
| `outline` / `placement` / `zones` / `silkscreen` | Layout-stage passes |
| `route` | Autoroute |
| `stitch` | Add power-plane via stitching |
| `preflight-routing` | **Routing-completeness gate** before manufacturing |
| `verify` | Final connectivity + DRC validation |
| `export` | Emit gerbers / BOM / CPL |
| `all` (default) | Run the whole sequence |

The `preflight-routing` step runs between `stitch` and `verify` in the
default `all` sequence. It re-uses `kct net-status` semantics in-process and
**blocks the build** if any nets are incomplete. Override with
`--allow-incomplete` (advertised; CI-greppable) or the global `--force`. See
[Routing Completeness Preflight](../guides/manufacturing-export.md#routing-completeness-preflight)
for the full semantics.

The outer `kct build` parser is kept in lockstep with the authoritative
inner parser at
[`src/kicad_tools/cli/build_cmd.py`](../../src/kicad_tools/cli/build_cmd.py)
by [`tests/test_build_parser_parity.py`](../../tests/test_build_parser_parity.py),
which fails if a new flag or `--step` choice is added to the inner parser
without also being surfaced on the outer CLI.

**Examples:**
```bash
# Full pipeline driven by the project spec. `SPEC` is positional.
kct build boards/05-bldc-motor-controller/project.kct

# Skip the routing-completeness gate (WIP escape hatch).
kct build boards/05-bldc-motor-controller/project.kct --allow-incomplete

# Run a single stage.
kct build boards/05-bldc-motor-controller/project.kct \
    --step preflight-routing
```

---

## Library Commands

### `lib`

Symbol library tools.

```bash
kct lib <subcommand> [options]
```

| Subcommand | Description |
|------------|-------------|
| `list` | List symbols in a library |
| `info` | Show symbol details |
| `create` | Create a new symbol |

**Examples:**
```bash
kct lib list Device.kicad_sym
kct lib info Device.kicad_sym R
```

---

### `footprint`

Footprint generation tools.

```bash
kct footprint <subcommand> [options]
```

| Subcommand | Description |
|------------|-------------|
| `generate` | Generate parametric footprints |
| `validate` | Validate footprint pad spacing |
| `fix` | Fix footprint issues |

```bash
kct footprint generate <type> [options]
```

| Type | Description |
|------|-------------|
| `soic` | SOIC packages |
| `qfp` | QFP packages |
| `qfn` | QFN packages |
| `dfn` | DFN packages |
| `bga` | BGA packages |
| `chip` | Chip resistors/capacitors |
| `sot` | SOT packages |
| `dip` | DIP packages |

**Examples:**
```bash
kct footprint generate soic --pins 8 --pitch 1.27
kct footprint generate qfn --pins 24 --size 4x4
kct footprint validate MyLib.kicad_mod
```

---

### `parts`

LCSC parts database lookup.

```bash
kct parts <subcommand> [options]
```

| Subcommand | Description |
|------------|-------------|
| `search` | Search for parts |
| `lookup` | Get part details by LCSC number |
| `check` | Check part availability |
| `sync-catalog` | Download the jlcparts dataset (~620 MB) into the cache dir for fully-offline lookups (#4117) |

**Examples:**
```bash
kct parts search "STM32F103"
kct parts lookup C8734
kct parts check C8734 --quantity 100
```

---

### `datasheet`

Datasheet tools.

```bash
kct datasheet <subcommand> [options]
```

| Subcommand | Description |
|------------|-------------|
| `search` | Search for datasheets |
| `download` | Download a datasheet |
| `parse` | Extract pin info from PDF |

**Examples:**
```bash
kct datasheet search "ATmega328P"
kct datasheet download "ATmega328P" -o atmega328p.pdf
kct datasheet parse atmega328p.pdf --pins
```

---

## PCB Operation Commands

### `route`

Autoroute a PCB. See [Routing Guide](../guides/routing.md) for strategy
choices and worked examples. Implemented in
[`src/kicad_tools/cli/route_cmd.py`](../../src/kicad_tools/cli/route_cmd.py).

```bash
kct route <pcb_file> [options]
```

Common flags (the full surface lives in `kct route --help`):

| Option | Description |
|--------|-------------|
| `-o`, `--output PATH` | Output file (default: `<input>_routed.kicad_pcb`) |
| `--strategy {basic,negotiated,monte-carlo,evolutionary}` | Routing strategy (default: `negotiated`) |
| `--trace-width MM` / `--clearance MM` | Trace + clearance overrides |
| `--via-diameter MM` / `--via-drill MM` | Via geometry |
| `--manufacturer NAME` (`--mfr`) | Manufacturer profile for DRC and adaptive rules |
| `--layers {auto,2,4,4-sig,4-all,6}` | Layer stack configuration (default: `auto`) |
| `--min-completion FLOAT` | Minimum completion ratio for success (default: 0.95) |
| `--timeout SEC` / `--per-net-timeout SEC` | Global / per-net wall-clock caps |
| `--seed N` | Seed Python `random` for reproducible routing (#2589) |
| `--auto-fix` / `--auto-fix-passes N` | Run `kct fix-drc` after routing on DRC failure |
| `--skip-drc` | Skip post-route DRC validation |

#### Feasibility / coupling flags (v0.15.0, all default off)

| Option | Description |
|--------|-------------|
| `--monotone-certificate-order` | Certify bundle escape feasibility (monotone-routability) and route bundle nets in the derived constructive order (#4089/#4103) |
| `--cross-package-pair-corridor` | Reserve a soft corridor between the two legs of a cross-package differential pair at escape time (#4090) |
| `--slack-corridor-widening` | Widen pair-continuation corridors by an estimated skew budget so length matching has room by construction (#4092) |

#### Strategy escalation flags

| Option | Description |
|--------|-------------|
| `--auto-layers` / `--no-auto-layers` | Escalate layer count on routing failure. **Default: enabled.** Tries 2 → 4 → 6 until success or `--max-layers` is reached. Pass `--no-auto-layers` to opt out. |
| `--max-layers {2,4,6}` | Upper bound for `--auto-layers` (default: 6) |
| `--auto-mfr-tier` | Escalate to a tighter manufacturer tier when geometry blocks routing (e.g. `jlcpcb` → `jlcpcb-tier1` to gain via-in-pad). Default: disabled. |
| `--mfr-tier-ladder LIST` | Explicit comma-separated tier ladder, e.g. `'jlcpcb,jlcpcb-tier1'`. Overrides the default ladder registered for `--mfr`. |
| `--adaptive-rules` | Progressively relax trace width / clearance until routing succeeds or manufacturer limits are reached. |
| `--min-trace MM` / `--min-clearance-floor MM` | Floors for `--adaptive-rules` |

See [Routing Guide → Strategy Escalation](../guides/routing.md#strategy-escalation).

#### Long-running routes

| Option | Description |
|--------|-------------|
| `--checkpoint-interval SEC` | Interval between best-so-far checkpoint writes to `--output`. Default: 30. Pass `0` to disable. |
| `--export-failed-nets PATH` | Write failed-net names (one per line) for follow-up. |
| `--strict` | Exit non-zero if the written PCB has any disconnected net. |

`--output` writes are atomic; combined with `--checkpoint-interval` this means
a long route can be safely interrupted (SIGINT) and the partial result on
disk remains valid for inspection or resume. See
[Routing Guide → Long-Running Routes](../guides/routing.md#long-running-routes-checkpointing).

#### Auto-fix and exit codes

`--auto-fix` runs the DRC repair pass after routing. If repair cannot be
applied cleanly the partial output is **rolled back** so the file on disk
matches what the router actually produced (issue #2852, #2853, #2861). The
rollback path is wired through all four routing code paths
(`Autorouter`, `RoutingOrchestrator`, MCP, reasoning agent) and surfaces as
exit code 3. See the [Exit Codes](#exit-codes) table.

**Examples:**
```bash
# Default escalation: 2L → 4L → 6L, atomic checkpoint every 30s
kct route board.kicad_pcb -o routed.kicad_pcb

# Stay at 2L (e.g. for cost) but allow manufacturer-tier escalation for fine-pitch QFP
kct route board.kicad_pcb --no-auto-layers --auto-mfr-tier --mfr jlcpcb

# Explicit ladder; long timeout; auto-fix DRC; reproducible
kct route board.kicad_pcb \
  --auto-mfr-tier --mfr-tier-ladder 'jlcpcb,jlcpcb-tier1' \
  --timeout 1500 --auto-fix --seed 42 -o routed.kicad_pcb

# CI-friendly checkpointing every 10s
kct route board.kicad_pcb --checkpoint-interval 10 -o routed.kicad_pcb
```

---

### `zones`

Add copper pour zones.

```bash
kct zones <subcommand> <pcb_file> [options]
```

| Subcommand | Description |
|------------|-------------|
| `add` | Add a copper zone |
| `list` | List existing zones |
| `batch` | Add multiple zones from a `NET:LAYER,...` spec |
| `fill` | Fill all zones in a PCB (via `kicad-cli`) |

**Output-path behavior:** for the mutating subcommands (`add`, `batch`,
`fill`), `-o`/`--output` is optional and **defaults to overwriting the input
in place** — consistent with `optimize-placement`. Because each in-place `add`
reads its own prior output, chaining `zones add` calls against one file
accumulates zones instead of silently discarding earlier ones. Pass an
explicit `-o <path>` to write a side file and leave the input untouched.

| Option | Description |
|--------|-------------|
| `-o`, `--output PATH` | Output PCB (default: overwrite input) |

**Examples:**
```bash
kct zones add board.kicad_pcb --net GND --layer B.Cu     # modifies board.kicad_pcb in place
kct zones add board.kicad_pcb --net +3.3V --layer In2.Cu # accumulates onto the GND zone
kct zones batch board.kicad_pcb --power-nets GND:B.Cu,+3.3V:F.Cu
kct zones fill board.kicad_pcb                           # fills all zones in place
kct zones add board.kicad_pcb --net GND --layer B.Cu -o with_zones.kicad_pcb  # side file
kct zones list board.kicad_pcb
```

---

### `placement`

Detect and fix placement conflicts.

```bash
kct placement <subcommand> <pcb_file> [options]
```

| Subcommand | Description |
|------------|-------------|
| `check` | Check for placement conflicts |
| `optimize` | Optimize component placement |
| `suggestions` | Get placement suggestions |

**Examples:**
```bash
kct placement check board.kicad_pcb
kct placement optimize board.kicad_pcb -o optimized.kicad_pcb
kct placement suggestions board.kicad_pcb --format json
```

---

### `optimize-placement`

CMA-ES placement optimizer. Distinct from `kct placement optimize` (physics
/ evolutionary): this command runs a CMA-ES loop with explicit anchor and
feasibility semantics. Implemented in
[`src/kicad_tools/cli/optimize_placement_cmd.py`](../../src/kicad_tools/cli/optimize_placement_cmd.py).

```bash
kct optimize-placement <pcb_file> [options]
```

| Option | Description |
|--------|-------------|
| `--strategy {cmaes}` | Optimization strategy (default: `cmaes`) |
| `--max-iterations N` | Maximum optimizer iterations (default: 1000) |
| `-o`, `--output PATH` | Output PCB (default: overwrite input) |
| `--seed {force-directed,random}` | Seed placement method |
| `--weights JSON` | Custom cost weights: `overlap`, `drc`, `boundary`, `wirelength`, `area` |
| `--dry-run` | Evaluate current placement without optimizing |
| `--progress N` | Print score every N iterations (0 disables) |
| `--checkpoint DIR` | Directory for checkpoint save/resume |
| `--no-slide-off` | Disable slide-off overlap pre-processing on the seed |
| `--anchor-weight FLOAT` | Per-net HPWL multiplier boost for nets that touch `(locked)` footprints. Scales each qualifying net's HPWL by `1 + anchor_weight * (anchored_pins / total_pins)`. **Default 0.0**. |
| `--time-budget SEC` | Wall-clock budget (bounds the feasibility-gated convergence loop) |
| `--allow-infeasible` | Exit 0 even when overlap/DRC/boundary violations remain |
| `-v` / `-q` | Verbose / quiet |

**Anchor-weight discrepancy to know about.** The `--help` text recommends
`2.0 .. 5.0` as a starting range. The validated recipe in
[Placement Optimization → Anchoring Perimeter Footprints](../guides/placement-optimization.md#anchoring-perimeter-footprints)
uses `--anchor-weight 1.0` — the help-text range is the conservative knob
designers would reach for; `1.0` is the value that actually lifted board-05
BLDC from 40% → 60% routing completion in practice. Treat help text as the
ceiling and the guide as the proven floor.

**Feasibility gate.** By default the optimizer exits **1** with
`FATAL: optimizer exited with infeasible placement (...)` on stderr if the
final placement still has overlap/DRC/boundary violations (issue #2821). Use
`--allow-infeasible` to override (recommended only when the next step is a
router that can absorb residual boundary violations). Use `--time-budget` to
bound the "keep going past plateau while infeasible" loop.

**Examples:**
```bash
# Anchored optimization — perimeter parts already marked locked=true
kct optimize-placement board.kicad_pcb \
  --anchor-weight 1.0 --max-iterations 400 --time-budget 120 \
  --allow-infeasible -o optim.kicad_pcb

# Evaluate the seed placement without moving anything
kct optimize-placement board.kicad_pcb --dry-run -v

# Resume from a checkpoint dir
kct optimize-placement board.kicad_pcb --checkpoint .optim_state/
```

See [Placement Optimization Guide](../guides/placement-optimization.md).

---

### `optimize-traces`

Optimize PCB traces.

```bash
kct optimize-traces <pcb_file> [options]
```

| Option | Description |
|--------|-------------|
| `--output`, `-o` | Output file |
| `--length-match` | Enable length matching |

---

## AI Integration Commands

### `reason`

LLM-driven PCB layout reasoning.

```bash
kct reason <pcb_file> [options]
```

| Option | Description |
|--------|-------------|
| `--interactive` | Interactive mode |
| `--export-state` | Export state for external LLM |
| `--auto-route` | Auto-route priority nets |
| `--output`, `-o` | Output file |

**Examples:**
```bash
kct reason board.kicad_pcb --interactive
kct reason board.kicad_pcb --export-state > state.json
kct reason board.kicad_pcb --auto-route -o routed.kicad_pcb
```

---

### `mcp`

MCP server for AI agent integration.

```bash
kct mcp serve [options]
```

| Option | Description |
|--------|-------------|
| `--http` | Use HTTP transport instead of stdio |
| `--port PORT` | HTTP port (default: 8080) |

**Examples:**
```bash
# Start stdio server (for Claude Desktop)
kct mcp serve

# Start HTTP server
kct mcp serve --http --port 8080
```

See [MCP Documentation](../mcp/README.md) for configuration details.

---

## Analysis Commands (v0.7)

### `analyze congestion`

Detect routing congestion hotspots.

```bash
kct analyze congestion <pcb_file> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json}` | Output format |
| `--threshold FLOAT` | Congestion threshold (0-1) |

---

### `analyze trace-lengths`

Analyze timing-critical trace lengths.

```bash
kct analyze trace-lengths <pcb_file> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json}` | Output format |
| `--net NET` | Analyze specific net |
| `--diff-pairs` | Show differential pair skew |

---

### `analyze thermal`

Detect thermal hotspots and heat sources.

```bash
kct analyze thermal <pcb_file> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json}` | Output format |

---

### `analyze signal-integrity`

Analyze crosstalk risk and impedance discontinuities.

```bash
kct analyze signal-integrity <pcb_file> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json}` | Output format |

---

### `erc explain`

ERC root cause analysis with fix suggestions.

```bash
kct erc explain <schematic> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json}` | Output format |

---

### `constraints check`

Detect constraint conflicts (keepout/grouping/region).

```bash
kct constraints check <pcb_file> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json}` | Output format |

---

### `net-status`

Validate net connectivity (unrouted, islands, isolated pads). The
`preflight-routing` step of `kct build` and the readiness check inside
`kct fleet status` both delegate to this analyzer in-process.

```bash
kct net-status <pcb_file> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json}` | Output format |
| `--net NET` | Check a specific net |
| `--incomplete` | Show only incomplete / unrouted nets |
| `--by-class` | Group output by net class |
| `-v`, `--verbose` | Per-segment / per-pad detail |

Exit code semantics are reused by the `preflight-routing` step in
`kct build` and by `kct fleet status` to decide "ship-ready vs. not".

**Examples:**
```bash
kct net-status board.kicad_pcb --incomplete --format json
kct net-status board.kicad_pcb --by-class
```

---

## Cost Commands (v0.7)

### `estimate cost`

Estimate manufacturing cost.

```bash
kct estimate cost <pcb_file> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json}` | Output format |
| `--mfr {jlcpcb,pcbway,oshpark,seeed}` | Manufacturer |
| `--quantity N` | Board quantity |

---

### `parts availability`

Check LCSC stock availability.

```bash
kct parts availability <schematic> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json}` | Output format |
| `--quantity N` | Required quantity |

---

### `suggest alternatives`

Suggest alternative parts for unavailable components.

```bash
kct suggest alternatives <schematic> [options]
```

| Option | Description |
|--------|-------------|
| `--format {table,json}` | Output format |

---

## Utility Commands

### `config`

View and manage configuration.

```bash
kct config [options]
```

| Option | Description |
|--------|-------------|
| `--show` | Show current configuration |
| `--set KEY VALUE` | Set a configuration value |
| `--reset` | Reset to defaults |

---

### `interactive`

Launch interactive REPL mode.

```bash
kct interactive [pcb_file]
```

Provides an interactive shell for exploring and modifying PCB files.

---

## Output Formats

Most commands support multiple output formats:

| Format | Description |
|--------|-------------|
| `table` | Human-readable table (default) |
| `json` | Machine-readable JSON |
| `csv` | Comma-separated values |

Use `--format` to specify:
```bash
kct symbols project.kicad_sch --format json
```

---

## Exit Codes

### Default (most commands)

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error (invalid input, file not found, etc.) |
| 2 | Invalid arguments |
| 130 | Interrupted (Ctrl+C) |

### `kct route` ladder

The router exposes a finer-grained ladder so CI can tell partial routing
apart from clean routing with DRC issues. Source of truth: the epilog at
[`src/kicad_tools/cli/route_cmd.py:4051-4059`](../../src/kicad_tools/cli/route_cmd.py).

| Code | Meaning |
|------|---------|
| 0 | All nets routed (or meets `--min-completion`), DRC clean |
| 1 | Fatal failure — no nets routed |
| 2 | Partial routing — below `--min-completion` threshold |
| 3 | Routing meets threshold **but** DRC violations remain — **also** returned when `--auto-fix` rollback fires (issue #2852). Both meanings share this code by design (see `route_cmd.py:2576-2580`). |
| 4 | Partial routing **and** segment-segment clearance violations |
| 5 | Interrupted by SIGINT with partial results saved (file on disk is valid) |

### `kct optimize-placement`

| Code | Meaning |
|------|---------|
| 0 | Optimizer converged to a feasible placement (or `--allow-infeasible` was set) |
| 1 | Final placement infeasible — overlap / DRC / boundary violations remain (issue #2821). Override with `--allow-infeasible`. |
| 2 | Invalid arguments |
| 130 | Interrupted (Ctrl+C) |

### `kct fleet status`

Source of truth: module docstring at
[`src/kicad_tools/cli/fleet_cmd.py:17-21`](../../src/kicad_tools/cli/fleet_cmd.py).

| Code | Meaning |
|------|---------|
| 0 | All surveyed boards are ship-ready |
| 1 | Argparse / IO error |
| 2 | One or more boards are not ship-ready (also returned when no boards are found, since "no ship-ready boards" is treated as not-ship-ready). Matches `kct net-status` semantics. |

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `KICAD_TOOLS_CONFIG` | Config file path |
| `KICAD_CLI_PATH` | Path to kicad-cli binary |
| `LCSC_API_KEY` | LCSC API key for parts lookup |
| `JLCPCB_ACCESS_KEY` / `JLCPCB_SECRET_KEY` | BYO-key official JLCPCB open-platform API — when both are set, becomes the preferred parts-lookup tier (#4119); see README "Using your own JLCPCB API key" |
| `JLCPCB_APP_ID` | Optional app id for the official JLCPCB API |

## Export / BOM Notes

`kct export` enriches the BOM with LCSC part numbers by default (`--auto-lcsc`,
on by default for JLCPCB exports). If the `parts` extra is missing, the export
**fails loudly** with an install hint instead of silently shipping an empty
LCSC column (#4116); pass `--no-auto-lcsc` to export without enrichment.
`kct mfr apply-rules` writes a sibling `.kicad_pro` (rules + Default netclass)
so `kicad-cli pcb drc` uses the applied constraints instead of factory
defaults (#4109).
