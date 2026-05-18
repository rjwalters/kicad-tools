# Routing Guide

This guide covers using kicad-tools autorouter for PCB trace routing.

---

## Overview

kicad-tools includes an A* pathfinding-based autorouter that supports:
- Single-layer and multi-layer routing
- Differential pair routing
- Length matching
- Zone (copper pour) awareness
- Obstacle avoidance

---

## CLI Usage

### Basic Routing

```bash
# Route all nets
kct route board.kicad_pcb -o routed.kicad_pcb

# Route specific net
kct route board.kicad_pcb --net CLK -o routed.kicad_pcb

# With custom trace width
kct route board.kicad_pcb --width 0.25 -o routed.kicad_pcb
```

### Design Rules

```bash
# Apply manufacturer rules
kct route board.kicad_pcb --mfr jlcpcb

# Custom clearance
kct route board.kicad_pcb --clearance 0.15 --via-size 0.6
```

---

## Python API

### Basic Routing

```python
from kicad_tools.router import Autorouter, DesignRules

# Load PCB
router = Autorouter.from_pcb("board.kicad_pcb")

# Route all nets
result = router.route_all()

print(f"Routed: {result.routed_nets}/{result.total_nets}")
print(f"Vias used: {result.via_count}")

# Save result
router.save("routed.kicad_pcb")
```

### Custom Design Rules

```python
from kicad_tools.router import DesignRules

rules = DesignRules(
    trace_width=0.2,        # mm
    clearance=0.15,         # mm
    via_drill=0.3,          # mm
    via_diameter=0.6,       # mm
    grid_resolution=0.25,   # Routing grid
)

router = Autorouter.from_pcb("board.kicad_pcb", rules=rules)
result = router.route_all()
```

### Per-Component Clearance

Fine-pitch ICs (like TSSOP, QFP with 0.5mm pitch or finer) often require tighter
clearances than the rest of the board. Use per-component clearance to handle this:

```python
from kicad_tools.router import DesignRules

rules = DesignRules(
    trace_clearance=0.15,              # Default clearance for most components
    component_clearances={
        "U1": 0.1,                      # Tighter clearance for fine-pitch IC
        "U2": 0.08,                     # Even tighter for QFN
    },
)
```

#### Automatic Fine-Pitch Detection

Instead of manually specifying each component, enable automatic fine-pitch
clearance based on pin pitch:

```python
rules = DesignRules(
    trace_clearance=0.15,              # Default
    fine_pitch_clearance=0.1,          # For fine-pitch components
    fine_pitch_threshold=0.8,          # Components with pitch < 0.8mm use fine_pitch_clearance
)
```

The router automatically detects component pin pitch and applies the appropriate
clearance. This is useful for boards with many fine-pitch ICs.

#### Combining Both Approaches

Explicit `component_clearances` take precedence over automatic detection:

```python
rules = DesignRules(
    trace_clearance=0.15,
    fine_pitch_clearance=0.1,          # Auto-apply to pitch < 0.8mm
    fine_pitch_threshold=0.8,
    component_clearances={
        "U3": 0.05,                     # Override: U3 needs extra-tight clearance
    },
)

# U1 (pitch 0.65mm): uses fine_pitch_clearance (0.1mm)
# U2 (pitch 2.54mm): uses trace_clearance (0.15mm)
# U3: uses explicit override (0.05mm)
```

#### Fine-Pitch Warnings

The CLI automatically warns about fine-pitch components that may cause routing
issues:

```bash
kct route board.kicad_pcb
# Warning: Fine-pitch components detected:
#   U1 (TSSOP-24): 0.65mm pitch - may need reduced clearance
#   U4 (QFP-64): 0.5mm pitch - routing may be challenging
```

### Layer Configuration

```python
# 2-layer board
router.set_layers(["F.Cu", "B.Cu"])

# 4-layer with inner layers
router.set_layers(["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])

# Prefer top layer for signals
router.set_layer_preference("F.Cu", weight=2.0)
```

---

## Routing Strategies

### Net Priority

Route critical nets first:

```python
# Set priorities (higher = first)
router.set_priority("CLK", 100)
router.set_priority("DATA*", 50)  # Wildcards supported
router.set_priority("GND", 10)

# Route in priority order
result = router.route_all()
```

### Net Classes

Apply different rules to net classes:

```python
# Power nets: wider traces
router.set_net_class_rules("Power", trace_width=0.5)

# High-speed: tighter clearance
router.set_net_class_rules("HighSpeed", clearance=0.2)
```

---

## Strategy Escalation

When a route hits a wall, the autorouter can climb two orthogonal ladders
before giving up: **layer count** and **manufacturer tier**. Both are off-by-
default in the legacy Python `Router` API but on by default (layer-only) in
`kct route`. Documented exit codes live in
[CLI Reference → Exit Codes](../reference/cli.md#kct-route-ladder).

### Layer escalation: `--auto-layers`

Defaults: **enabled**. Tries 2 → 4 → 6 layers until routing succeeds or
`--max-layers` is reached.

```text
--auto-layers, --no-auto-layers
                      Automatically escalate layer count on routing failure
                      (default: enabled). Tries 2 -> 4 -> 6 layers until
                      routing succeeds or --max-layers is reached. Use --no-
                      auto-layers to disable and route at a fixed layer
                      count.
--max-layers {2,4,6}  Maximum layer count for auto-escalation (default: 6)
```

Because this is the default, the opt-**out** is `--no-auto-layers`. Pin a
fixed layer count when you want cost certainty (a 2-layer board must stay
2-layer) or when comparing baselines across runs.

```bash
# Default: escalate as needed up to 6 layers
kct route board.kicad_pcb -o routed.kicad_pcb

# Cost-locked: stay at 2 layers, accept partial routing
kct route board.kicad_pcb --no-auto-layers --layers 2 -o routed.kicad_pcb
```

### Manufacturer-tier escalation: `--auto-mfr-tier`

Defaults: **disabled**. When set, the router jumps to a tighter tier of the
current manufacturer profile when geometric infeasibility (typically QFP/QFN
fine-pitch escape) blocks routing.

```text
--auto-mfr-tier       Automatically escalate to a tighter manufacturer tier
                      when geometric infeasibility blocks routing on the
                      current tier (default: disabled). E.g. jlcpcb ->
                      jlcpcb-tier1 to gain via-in-pad for fine-pitch QFP
                      escape.
--mfr-tier-ladder MFR_TIER_LADDER
                      Explicit comma-separated manufacturer tier ladder for
                      --auto-mfr-tier (e.g. 'jlcpcb,jlcpcb-tier1').
                      Overrides the default ladder registered for the
                      current --mfr.
```

The default ladder for each manufacturer is registered with the rules
package; `--mfr-tier-ladder` lets you pin a specific climb (useful in CI to
keep cost differences predictable).

### Combining both ladders

The two flags compose. A typical "make this board route at any cost" recipe:

```bash
kct route board.kicad_pcb \
  --mfr jlcpcb \
  --auto-layers --max-layers 4 \
  --auto-mfr-tier --mfr-tier-ladder 'jlcpcb,jlcpcb-tier1' \
  --timeout 1500 -o routed.kicad_pcb
```

The router will first try the cheaper tier at 2 layers, escalate to 4
layers, and only as a last resort climb to `jlcpcb-tier1` for via-in-pad. If
you also pass `--adaptive-rules`, trace width / clearance are relaxed within
the chosen tier's floor before either ladder advances.

---

## Differential Pairs

Diff-pair routing is configured per net class on
[`NetClassRouting`](../../src/kicad_tools/router/rules.py), not via imperative
`Router.method(...)` calls. See the dedicated guides under
[`docs/guides/diff-pairs/`](diff-pairs/README.md):

- [Declaring a pair](diff-pairs/01-declaring-pairs.md) (`diffpair_partner`, suffix inference, single-ended refusal)
- [Clearance](diff-pairs/02-clearance-and-classes.md) (`intra_pair_clearance`, `coupled_routing`)
- [Impedance](diff-pairs/03-impedance-and-sizing.md) (`target_diff_impedance`, `kct impedance` CLI)
- [Length matching](diff-pairs/04-length-matching.md) (`skew_tolerance_mm`, `Autorouter.update_diffpair_skew`)
- [Protocol recipes](diff-pairs/05-protocol-recipes.md) (USB 2.0 / USB 3.0 / PCIe / MIPI)
- [DRC rules](diff-pairs/06-drc-rules.md) (`diffpair_clearance_intra`, `_routing_continuity`, `_length_skew`, `impedance`)

The canonical pre-configured class is `NET_CLASS_HIGH_SPEED` in
[`router/rules.py:675`](../../src/kicad_tools/router/rules.py) (already has
`coupled_routing=True`).

---

## Match Groups (Parallel Buses)

Length-matching a parallel-bus **group** (DDR data byte, MIPI lane group,
HDMI TMDS, address bus) is configured per net class on `NetClassRouting`
— same pattern as diff pairs, but for N>=3 nets. See the dedicated
guides under [`docs/guides/match-groups/`](match-groups/README.md):

- [Declaring a group](match-groups/01-declaring-groups.md) (`length_match_group`, suffix detection, legacy API)
- [Reference selection](match-groups/02-reference-selection.md) (longest / explicit / `clock`)
- [Groups whose members are diff pairs](match-groups/03-group-of-pairs.md) (MIPI / HDMI)
- [Cascade safety](match-groups/04-cascade-safety.md) (when the tuner gives up)
- [Protocol recipes](match-groups/05-protocol-recipes.md) (DDR / MIPI / HDMI / address bus)
- [DRC rule](match-groups/06-drc-rule.md) (`match_group_length_skew`)
- [CLI + sidecar](match-groups/07-cli-and-sidecar.md) (`--length-match-groups`, `--net-class-map`)

Engage from the CLI with `kct route --length-match-diffpairs --length-match-groups`.

---

## Zone Awareness

Handle copper pour zones:

```python
# Respect existing zones
router.set_zone_mode("avoid")  # Route around zones

# Or route through (zones will pour around traces)
router.set_zone_mode("through")

# Thermal relief for pads in zones
router.enable_thermal_relief(spoke_width=0.3, gap=0.3)
```

---

## Via Management

```python
# Limit via count
router.set_max_vias_per_net(4)

# Via-in-pad (for BGA)
router.enable_via_in_pad(components=["U1"])

# Blind/buried vias (4+ layers)
router.enable_blind_vias(top_layer="F.Cu", bottom_layer="In1.Cu")
```

---

## Routing Quality

### Optimization

```python
# After initial routing, optimize
router.optimize_traces(
    straighten=True,        # Remove unnecessary bends
    minimize_length=True,   # Shorten traces
    minimize_vias=True,     # Reduce via count
)
```

### Length Reports

```python
# Get trace lengths
lengths = router.get_trace_lengths()

for net, length in lengths.items():
    print(f"{net}: {length:.2f}mm")

# Check for length violations
violations = router.check_length_constraints()
```

---

## Incremental Routing

Route nets one at a time:

```python
# Route one net
result = router.route_net("CLK")

if result.success:
    print(f"Routed CLK: {result.length:.2f}mm, {result.vias} vias")
else:
    print(f"Failed: {result.failure_reason}")

# Undo if needed
router.unroute_net("CLK")
```

---

## LLM-Driven Routing

Use the reasoning module for AI-assisted routing:

```python
from kicad_tools import PCBReasoningAgent

agent = PCBReasoningAgent.from_pcb("board.kicad_pcb")

while not agent.is_complete():
    # Get state for LLM
    prompt = agent.get_prompt()

    # LLM decides what to route next
    command = your_llm(prompt)  # e.g., "ROUTE CLK"

    # Execute
    result, diagnosis = agent.execute(command)

agent.save("routed.kicad_pcb")
```

See [LLM Routing Example](https://github.com/rjwalters/kicad-tools/tree/main/examples/llm-routing).

---

## Determinism and Reproducibility

By default the python router backend uses Python's global `random` module without seeding,
so two `kct route` invocations on the same input can produce different byte output and,
on stuck boards, different DRC error counts run-to-run (issue #2589).

For reproducible routing (CI baselines, regression debugging, board regeneration),
pass `--seed N`:

```bash
# Two runs with --seed 42 produce byte-identical output
# (modulo per-element UUIDs which are intentionally random)
kct route board.kicad_pcb --backend python --seed 42 -o run1.kicad_pcb
kct route board.kicad_pcb --backend python --seed 42 -o run2.kicad_pcb
```

What `--seed` covers:

- `random.shuffle` in `_escape_shuffle_order`, `_escape_random_subset`, and `_escape_full_reorder`
  (the negotiated router's escape strategies that fire under congestion).
- `random.shuffle` in the MST fine-grid trial loop (`router/core.py`).

What `--seed` does *not* cover:

- Per-element UUID generation in the output PCB file -- these stay random by design.
- Wall-clock-based escape budgets driven by `--timeout`: on a heavily loaded machine, fewer
  routing iterations may complete before timeout, producing a different (still deterministic
  within a budget) intermediate result. For fully reproducible CI runs, combine `--seed` with
  a generous `--timeout`.
- The C++ backend (`--backend cpp`): already deterministic for a given input grid.

---

## Troubleshooting

### Unroutable Nets

```python
result = router.route_all()

for net in result.failed_nets:
    print(f"Failed: {net}")
    diagnosis = router.diagnose_failure(net)
    print(f"  Reason: {diagnosis.reason}")
    print(f"  Suggestion: {diagnosis.suggestion}")
```

### Common Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| "No path found" | Blocked by components | Improve placement |
| "Clearance violation" | Too tight | Increase clearance or use smaller traces |
| "Via limit exceeded" | Complex routing | Allow more vias or add layers |
| "Length mismatch" | Serpentine needed | Enable serpentine routing |

### Reading the exit code

`kct route` returns a structured exit code (see
[CLI Reference → Exit Codes](../reference/cli.md#kct-route-ladder) for the full
ladder). The two non-obvious cases:

- **Exit 3** means "routing met `--min-completion` but DRC violations remain"
  **or** "auto-fix tried to clean DRC and rolled back" (issue #2852). When
  you see exit 3, re-run with `--auto-fix --auto-fix-passes 5` or inspect the
  DRC report — the partial result on disk is still routable input.
- **Exit 5** is a graceful SIGINT — the file on disk is the most recent
  checkpoint and is safe to feed back into `kct route` or KiCad.

---

## Performance

### Native C++ Backend (Build First!)

The router has a C++ A* implementation that delivers a **10-100x speedup**
over pure Python for the inner pathfinding loop. **It is not built by
`uv sync`** — you must explicitly run `kct build-native` once after every
fresh checkout or new git worktree.

```bash
# Check whether the C++ extension is already built
kct build-native --check
# "C++ backend: available (version 1.0.0)"  <-- good
# "C++ backend: not installed"              <-- run kct build-native

# Build (takes ~30s; one-time cost per worktree)
kct build-native
```

`kct route` automatically uses the C++ backend when it's present and falls
back to pure-Python silently when it isn't. The routing log prints the
active backend:

```text
Backend:    cpp v1.0.0 (native, 10-100x faster)   <-- C++ active
Backend:    python (fallback)                     <-- C++ missing/broken
```

**If `kct route` appears stuck or per-net log lines show tens of seconds**,
verify the backend before tuning anything else — a missing C++ extension is
the single most common cause of router slowness on developer workstations
and CI runners.

```bash
# Force backend selection (default = auto)
kct route board.kicad_pcb --backend cpp     # require C++ (error if missing)
kct route board.kicad_pcb --backend python  # force pure-Python
```

#### Fresh worktree gotcha

When using git worktrees (`.loom/worktrees/issue-N/`), each worktree has
its own `.venv/` and its own `src/kicad_tools/router/router_cpp.*.so`.
Building in the main checkout does **not** propagate to worktrees. After
`cd` into a new worktree, always run `uv run kct build-native` once
before benchmarking routing performance.

---

### Long-Running Routes (Checkpointing)

Multi-layer boards with hundreds of nets can run for minutes. `kct route`
writes the current best-so-far to `--output` on a timer so that a SIGINT
(Ctrl+C) or a wall-clock `--timeout` leaves a valid PCB on disk you can
inspect, route again, or hand to KiCad.

```text
--checkpoint-interval CHECKPOINT_INTERVAL
                      Interval in seconds between best-so-far checkpoint
                      writes to --output. Default: 30. Use 0 to disable.
```

Key behaviours:

- Writes are **atomic** (write-then-rename), so a crash mid-checkpoint never
  corrupts the file.
- On SIGINT the router exits **5** with the most recent checkpoint already on
  disk; the partial result is valid input for another `kct route` pass.
- `--checkpoint-interval 0` disables checkpointing (slightly faster on small
  boards where the cost of serialising the PCB every 30 s dominates).

Pair with `--seed` for reproducible long routes and with
`--export-failed-nets path.txt` to capture the unrouted-nets list at every
checkpoint for post-hoc analysis.

```bash
# 25-minute route with a checkpoint every 10s and a failed-net log
kct route board.kicad_pcb \
  --timeout 1500 --checkpoint-interval 10 \
  --export-failed-nets failed.txt \
  -o routed.kicad_pcb
```

---

### Grid Resolution Strategies

kicad-tools supports multiple grid strategies to balance routing accuracy against performance:

#### Standard Grid (Default)

Uses `grid_resolution` from design rules. Fine grids ensure accuracy but scale as O(1/resolution²).

```python
rules = DesignRules(
    trace_clearance=0.127,    # JLCPCB 5mil
    grid_resolution=0.0635,   # Half of clearance (default)
)
```

#### Expanded Obstacle Mode

Pre-expands all obstacles by the full clearance, allowing a coarser grid. Achieves ~4x speedup for tight-clearance designs.

```python
from kicad_tools.router import RoutingGrid, DesignRules

# Create grid with expanded obstacles
grid = RoutingGrid.create_expanded(
    width=65.0,
    height=56.0,
    rules=rules,
)

# Uses trace_width as resolution instead of clearance/2
print(f"Cells: {grid.cols * grid.rows}")  # ~75% fewer cells
```

#### Adaptive Grid

Automatically calculates optimal resolution based on board size and target cell count.

```python
# Target 500K cells regardless of board size
grid = RoutingGrid.create_adaptive(
    width=100.0,
    height=80.0,
    rules=rules,
    target_cells=500000,
)
```

#### Sparse Routing (Clearance Contours)

For maximum performance with tight clearances, use the sparse router which generates waypoints only where needed:

```python
from kicad_tools.router import SparseRouter, Pad

router = SparseRouter(
    width=40.0,
    height=30.0,
    rules=rules,
    num_layers=2,
)

# Add pads
for pad in pads:
    router.add_pad(pad)

# Build visibility graph
router.build_graph()

# Route
route = router.route(start_pad, end_pad)
```

Performance comparison for 65x56mm board with JLCPCB clearances:

| Mode | Grid Points | Routing Time |
|------|-------------|--------------|
| Standard (0.0635mm) | ~900,000 | ~120s |
| Expanded (0.127mm) | ~225,000 | ~30s |
| Sparse (contours) | ~10,000 | <10s |

### Large Board Tips

```python
# Use progress callback
from kicad_tools import create_print_callback

result = router.route_all(progress=create_print_callback())

# Route in parallel (experimental)
router.set_parallel(threads=4)
```

---

## See Also

- [Placement Optimization Guide](placement-optimization.md)
- [DRC & Validation Guide](drc-and-validation.md)
- [Example: Autorouter](https://github.com/rjwalters/kicad-tools/tree/main/examples/04-autorouter)
