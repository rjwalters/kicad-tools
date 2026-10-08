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

### Kelvin Current-Sense Branches

For grid routing, a net with at least three terminals is recognized as Kelvin
when its name matches a current-sense convention (for example `ISENSE_A`,
`SHUNT`, or `KELVIN`) and a resistor pad can be identified as the shunt tap.
With multiple resistor candidates, the router chooses the candidate with the
smallest total Manhattan distance to the other terminals. Check this choice
against the circuit's intended shunt before relying on automatic recognition.

Current-sense polarity suffixes such as `ISENSE_A+` and `VSNS_N` do not by
themselves select a 100-ohm differential impedance target. Their existing
trace width and clearance remain in force. Explicit impedance specifications
and net-class targets still apply.

Recognized nets use separate branches from the shunt pad. Previously routed
branches and other terminals become temporary physical obstacles during each
search. This applies to Python and C++ grid routing, including negotiated
routing, and avoids joining nearby pins of the same IC before routing the star.
An existing shunt escape does not move the tap to its outer endpoint.

This standalone example constructs an adversarial four-terminal net: the force
terminal lies directly between the shunt and a sense terminal.

```python
from kicad_tools.router.core import Autorouter

router = Autorouter(22, 22, force_python=True, physics_enabled=False)
for ref, x, y in [("R1", 4, 10), ("Q1", 12, 10), ("U1", 16, 10), ("U2", 16, 12)]:
    router.add_component(
        ref,
        [
            {
                "number": "1",
                "x": x,
                "y": y,
                "width": 0.8,
                "height": 0.8,
                "net": 1,
                "net_name": "ISENSE_TEST",
            }
        ],
    )
branches = router.route_net(1)
assert len(branches) == 3
```

The physical-isolation implementation described here covers the grid engine;
mesh and lattice routing have separate implementations. It does not repair
pre-existing copper that already joins sense and force branches away from the
shunt. Small shunt pads or dense obstacles may still prevent completion. Check
the emitted copper and native saved/refilled connectivity: an electrical
same-net connectivity result alone does not prove the intended Kelvin tap.
Dense-board completion remains tracked in [issue #5398](https://github.com/rjwalters/kicad-tools/issues/5398).

### Custom Design Rules

```python
from kicad_tools.router import DesignRules

rules = DesignRules(
    trace_width=0.2,  # mm
    clearance=0.15,  # mm
    via_drill=0.3,  # mm
    via_diameter=0.6,  # mm
    grid_resolution=0.25,  # Routing grid
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
    trace_clearance=0.15,  # Default clearance for most components
    component_clearances={
        "U1": 0.1,  # Tighter clearance for fine-pitch IC
        "U2": 0.08,  # Even tighter for QFN
    },
)
```

#### Automatic Fine-Pitch Detection

Instead of manually specifying each component, enable automatic fine-pitch
clearance based on pin pitch:

```python
rules = DesignRules(
    trace_clearance=0.15,  # Default
    fine_pitch_clearance=0.1,  # For fine-pitch components
    fine_pitch_threshold=0.8,  # Components with pitch < 0.8mm use fine_pitch_clearance
)
```

The router automatically detects component pin pitch and applies the appropriate
clearance. This is useful for boards with many fine-pitch ICs.

#### Combining Both Approaches

Explicit `component_clearances` take precedence over automatic detection:

```python
rules = DesignRules(
    trace_clearance=0.15,
    fine_pitch_clearance=0.1,  # Auto-apply to pitch < 0.8mm
    fine_pitch_threshold=0.8,
    component_clearances={
        "U3": 0.05,  # Override: U3 needs extra-tight clearance
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
--max-layers {2,4,...,32}  Maximum layer count for auto-escalation (default: 6;
                      above 6 the last rung is the board's own detected stack)
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
[`router/rules.py`](../../src/kicad_tools/router/rules.py) (already has
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

## Swap Groups and Recorded Deltas (Netlist Degrees of Freedom)

Epic #5511. A **swap group** declares that a subset of a bundle's nets may
have their pad bindings on one facing component re-assigned among
themselves — the exact opposite default of a match group (which says
"these nets must arrive length-matched", not "these nets' pins may move").
This is for the case a match group alone cannot fix: a facing bundle (e.g.
a DDR data byte between two parts) measured **reversed**, where re-ordering
pins on the secondary component removes facing-row crossings that no amount
of length tuning touches.

### Declaring a swap group

Swap-group membership is **declared only** — never inferred from net names,
pin functions, or footprints:

```python
NetClassRouting(
    name="DDR_DATA_BYTE_0",
    length_critical=True,
    length_match_group="DDR_DATA_BYTE_0",  # these nets must length-match...
    swap_group="DDR_BYTE0",  # ...and these nets' PINS may swap
)
```

`length_match_group` and `swap_group` are separate, narrower channels: a
match group may include nets whose pin binding stays fixed (e.g. a DQS
strobe), so a net can belong to the match group without opting into
swapping. At least two nets must share the same `swap_group` value for a
proposal to exist (`MIN_SWAP_GROUP_NETS` in
[`router/swap_groups.py`](../../src/kicad_tools/router/swap_groups.py)); a
net with no `swap_group` key is fixed by omission, full stop.

### How the proposal is computed

When the stuck-net classifier (`kct net-status --why`, or the placement-delta
feedback loop below) measures a declared swap group's bundle as
`ORIENT_REVERSED`, `swap_groups.propose_swap_assignment` computes a
**sort-and-pair, crossing-minimising** pad re-binding: each row is sorted by
its own projection along the facing edge and re-bound rank-for-rank, reusing
the same crossing counter as the length-match detector
(`compute_facing_row_inversions`). The result is pure data — a
`net_rebinding` map plus `crossings_before` / `crossings_after` counts — and
mutates nothing on its own. An already co-oriented (planar) subset degrades
to the identity permutation, which is the proposer's own no-op guard.

This surfaces as a `kind="reorder_pins"` `PlacementDelta`
(`router/placement_delta.py`) carrying a `pad_map` (`{pad_number:
new_net_name}`) and the crossing counts, alongside whatever the classifier's
top-ranked fix (`mirror`, `translate`, ...) was for the same diagnosis — see
`deltas_from_result`.

### Netlist write-back (the applicator)

`kct route --placement-delta-feedback` runs the classifier-driven feedback
loop after the initial routing pass. **Default: auto (Issue #5890)** — with
neither `--placement-delta-feedback` nor `--no-placement-delta-feedback`
given, the loop runs only when the report-only routing plan (Epic #5510)
says the board is infeasible and nets remain unrouted; pass
`--no-placement-delta-feedback` to opt out, or `--placement-delta-feedback`
to force it. See [`docs/reference/cli.md`](../reference/cli.md)'s
"Placement-delta feedback" section for the full flag reference.

When a `reorder_pins` delta carrying a `pad_map` is kept (strict routed-net
improvement, no clearance / pairwise-creepage / keepout regression), the
pads are re-bound on **both** the PCB and the router's own pad list — never
a net rename, so `diffpair_partner`, `length_match_reference` and every
other net-name-keyed field stay valid. Every decision — `applied`,
`proposed` (considered but not kept) and `reverted` (kept briefly, then
undone) — is written to `<output>_placement_delta.json`
(`write_placement_delta_json`), so a swap is never a silent artifact edit:
it is always visible in that sidecar, whether or not it was kept.

### Replaying a committed delta as a reviewable recipe input

`<output>_placement_delta.json` is a **run artifact**, not a recipe input —
regenerating the board from source does not reproduce it. To make an
applied delta a **committed, diffable recipe input** instead of a side
artifact, promote the deltas you want to keep from that artifact's
`proposed` (or `applied`) list into a small, hand-reviewed JSON file
committed next to the recipe, and have the recipe replay it explicitly:

```python
from kicad_tools.router.placement_delta import (
    format_pad_map_report,
    load_placement_deltas,
    pad_map_overrides,
)

deltas = load_placement_deltas("regression-fixture/placement_delta.json")
pad_overrides = pad_map_overrides(deltas)  # {target_key: {pad: net_name}}
report = format_pad_map_report(deltas)  # human-readable change report
```

- `load_placement_deltas` reads the `applied` section by default (never
  `proposed` — resurrecting a refused candidate must be an explicit choice,
  passed via `section="proposed"`), and raises loudly on a path that is not
  a delta artifact at all rather than silently routing as though nothing
  were declared.
- `pad_map_overrides` merges every delta's `pad_map` into one
  `{target: {pad: net}}` table (raising `ValueError` on a genuine
  contradiction — two deltas binding the same pad to different nets) that a
  recipe's schematic/PCB generators can thread through net assignment.
  Deltas with no `pad_map` (every geometry-only kind, and a rationale-only
  `reorder_pins` with no declared swap group) contribute nothing — a delta
  artifact without a `pad_map` must not move a single pin.
- `format_pad_map_report` turns the same deltas into an auditable report
  (target, source action, `crossings_before` -> `crossings_after`, and every
  `pad -> net` binding) a recipe can print and write alongside its other
  generated output.

**Promotion into the committed file is the human review step** — a recipe
must never replay `proposed` entries automatically. Committing the small
JSON file (instead of hand-editing generated schematic/PCB files) is what
keeps the change a reviewable `git diff` of recipe *input*, not a diff of
generated bytes nobody can audit pin-by-pin.

**Reference implementation: board 07.**
[`boards/07-matchgroup-test/generate_design.py`](../../boards/07-matchgroup-test/generate_design.py)
wires this with an explicit, opt-in `--placement-delta PATH` flag (never
auto-discovered) that replays the committed, reviewed
[`regression-fixture/placement_delta.json`](../../boards/07-matchgroup-test/regression-fixture/placement_delta.json)
into both the generated schematic and PCB before routing, so the swap
survives a full regeneration and the two views still agree pad-for-pad:

```sh
uv run python boards/07-matchgroup-test/generate_design.py /tmp/board07-swap \
  --placement-delta boards/07-matchgroup-test/regression-fixture/placement_delta.json
```

With the flag omitted, the recipe's output is unchanged — committing the
delta file does not alter the board's default shipped artifacts; replaying
it is a deliberate, separately-reviewed choice (promoting a swapped board to
the *default* shipped fixture is its own decision, re-measured on its own
merits). See `regression-fixture/README.md`'s "Netlist write-back from a
committed delta" section for the full worked trace, including why `DM0` /
`DQS_P` / `DQS_N` (declared match-group members, not declared swappable)
stay fixed while `DQ0`-`DQ7` re-bind.

Not every recipe has a swap group to carry: a board whose nets are placed
via reviewed, fingerprinted reference copper rather than `NetClassRouting`
declarations and the router's own classifier (board 05's `design.py`, see
[`redesign/routing.py`](../../boards/05-bldc-motor-controller/redesign/routing.py))
never runs the classifier loop above at all, so there is no delta to
promote — the mechanism applies to any recipe built on `NetClassRouting`
+ `kct route`, not to a hand-placed, hash-pinned board by construction.

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

## Pad-Access Invariant

*Epic #5508 Phase 2, issue #5891. On by default for the grid engine.*

A pad's **access set** is everything still legal as a *first move* out of it:
the exit stubs on its own layer, plus the via sites reachable from them. A pad
whose access set is empty cannot be routed, whatever the search does afterwards
— and the usual way a reachable pad gets there is that copper committed for
*other* nets encloses it before its own net's turn. PathFinder negotiation
cannot repair that: the enclosing copper is legal and unshared, so it never
appears in `find_overused_cells` and rip-up has nothing to target.

The invariant makes that a **hard commit-time rule**: a negotiated candidate
route is refused when committing it would reduce another unrouted pad's access
set to empty. A refusal is handled exactly like a failed search, so the
existing targeted rip-up retries the connection — usually on a path that does
not close anybody in.

```bash
# On by default -- nothing to pass.
kct route board.kicad_pcb -o routed.kicad_pcb

# Opt out (byte-identical to pre-#5891 behaviour).
kct route board.kicad_pcb -o routed.kicad_pcb --no-pad-access-invariant
kct route-auto board.kicad_pcb --no-pad-access-invariant
```

Each refusal is retained as a witness, so a run can say *which* pad it
protected from *which* net:

```python
router.route_all_negotiated(max_iterations=5)

for veto in router.pad_access_vetoes:
    print(veto.one_line())
    # COMP refused at initial[0]: committing it would strand U3.1 (ISENSE_A+)
    # -- last access was 1 stub(s) / 1 via site(s), closed by COMP

gate = router.pad_access_invariant  # None when never consulted
print(gate.summary_line())
```

**Scope.** The rule is consulted from the one commit path that turns a
negotiated A* result into new grid copper, which covers the initial pass, the
grace pass, every rip-up iteration, the relief probe and the region-parallel
path. Three things are deliberately outside it: the escape pre-phase (its stubs
are the baseline access is measured *against*), rip-up **re-land** paths
(re-marking a victim restores copper that was already committed, and refusing
it would strand the net the rescue is for), and the lattice / mesh strategies
(Phase 4). It is bounded by an evaluation budget and a protected-pad cap, both
of which fail *open* and are reported through
`PadAccessInvariant.truncated` rather than silently narrowing the rule.

**Cost.** The rule runs before every guarded commit, so it reports what it
spends: `PadAccessInvariant.seconds` is the wall-clock time the gate itself
consumed, alongside the candidates it was asked about, the terminals each
prefilter stage kept, and the access evaluations it paid for.

```python
print(router.pad_access_invariant.to_dict())
# {'enabled': True, 'protected_pads': 44, 'checks': 124, 'coarse_hits': 512,
#  'fine_hits': 503, 'evaluations': 220, 'seconds': 1.037, ...}
```

Those are board 06's real numbers: **1.0 s of an ~870 s route**, for 44
protected terminals. Three things keep it there — a two-stage bbox prefilter
(whole-route envelope, then the candidate's per-primitive boxes), an
early-exit "has this terminal *any* way out?" test instead of enumerating the
whole access set, and a cached "before" verdict that only the copper landing
inside a terminal's own box invalidates.

The offline counterpart — *which commit stranded a pad, after the fact* — is the
[access witness](../diagnostics/access-witness.md), built from the
[commit journal](../reference/commit-journal.md).

### Access-loss rip-up targeting

*Epic #5508 Phase 3a, issue #5913. On by default, same switch as the invariant.*

The rule above refuses a commit it is *asked* about. It does not cover every
way a pad gets sealed in: it fails open past its evaluation budget, it does not
protect a pad whose net already landed copper, and it judges one candidate at a
time — so a seal built out of several individually admissible commits still
happens.

When that leaves a net the loop cannot route, the same witness becomes a
**rip-up target list**. Phase 1a is asked for the failed net's *own* terminals;
the committed nets its `closing_copper` names are added to the existing
`targeted_ripup(blocking_nets=…)` set, beside the Bresenham direct-line scan
and the same-tier destination siblings. Nothing else about the rip-up changes —
`ripup_history` / `max_ripups_per_net` still bound it, and a reroute that does
not converge still rolls the whole transaction back verbatim.

Only copper the negotiated loop owns is a target (`net_routes`), so escape
stubs, `--preserve-existing` copper and coupled diff-pair bodies are never
named; and only route copper is, so a foreign pad, a fill, a keepout, a hard
reservation, the board edge and a Kelvin sibling's isolated branch are reported
as *held* rather than ripped:

```python
for witness in router.access_loss_witnesses:
    print(witness.one_line())
    # ISENSE_A+ sealed in at iteration[1]: U3.1 has no legal exit,
    # closed by COMP, FOREIGN -- ripping COMP

targeter = router.access_loss_targeter  # None when never consulted
print(targeter.summary_line())
```

---

## Routing Quality

### Optimization

```python
# After initial routing, optimize
router.optimize_traces(
    straighten=True,  # Remove unnecessary bends
    minimize_length=True,  # Shorten traces
    minimize_vias=True,  # Reduce via count
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

### Diagnosing clearance / under-clearance bugs

When a route appears to violate clearance and you need to know which A*
neighbor-expansion gate allowed (or unexpectedly rejected) a move, set the
``KICAD_ROUTER_TRACE_ASTAR`` environment variable. Both the C++ backend
(``router/cpp/src/pathfinder.cpp``) and the Python fallback
(``router/pathfinder.py``) honor this flag and write one ``[A*]`` /
``[A*/one-shot]`` / ``[A*/py]`` line per neighbor decision to ``stderr``.

```bash
# C++ backend
KICAD_ROUTER_TRACE_ASTAR=1 uv run pytest \
    tests/test_cpp_clearance_enforcement.py::TestGap1UnblockedCellClearance \
    -v --no-cov 2> astar.log

# Each line is structured:
#   [A*] cur=(x,y,Layer) nbr=(x,y,Layer) DECISION reason=... [extra fields]
# DECISION is ACCEPT or REJECT.  reasons include: oob,
# diagonal_corner_blocked, foreign_net_blocked,
# trace_clearance_envelope_overlap, trace_blocked(no_net_blocked_cell),
# zone_blocked.

# Filter to decisions for a specific suspect cell:
grep "nbr=(8,8,L0)" astar.log
```

The output is voluminous (thousands of lines for even small fixtures) so the
flag is **off by default** and the per-iteration overhead is a single
predicted branch when unset. Use it for ad-hoc debugging only.

Added by Issue #3135 to make future Gap-1-style under-clearance
investigations restart-proof: prior debugging of the same fixture had to
re-derive the gate-by-gate decision flow from source each time.

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
    trace_clearance=0.127,  # JLCPCB 5mil
    grid_resolution=0.0635,  # Half of clearance (default)
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

### Region routing: trunk-first, then tile-refine

On a large board a single *safe fine grid* over the whole area can blow the
memory budget. The grid cell count is roughly:

```
cells ≈ (width / grid) × (height / grid)
```

For a 160×100mm board at a 0.03175mm (1.25 mil) fine grid that is
`(160/0.03175) × (100/0.03175) ≈ 5040 × 3150 ≈ 15.9M cells` — far above the
`--max-cells` default of 500,000 (see [#4249](https://github.com/rjwalters/kicad-tools/issues/4249)).
`--grid auto` reacts by selecting a coarse, less-safe grid, and forcing the
fine grid with an explicit `--grid` would need a 16M-cell budget and the
memory to match.

The fix is to route the board in **tiles** with `--region X1,Y1,X2,Y2`, which
confines all new routing to an axis-aligned box (board-relative mm) and treats
everything outside as a fixed obstacle. A single 32×20mm tile at the same
0.03175mm grid is `(32/0.03175) × (20/0.03175) ≈ 1008 × 630 ≈ 635k cells` —
affordable with a modest `--max-cells` bump, where the whole board is not.

#### The boundary-stub problem

`--region` deliberately **refuses** any net that has a pad *outside* the tile
unless that net already has a *same-net boundary stub* — a piece of its own
copper crossing into the tile that the in-region router can reconnect to. A
cross-region net with no such stub fails with (from
`src/kicad_tools/cli/route_cmd.py`):

```
Error: --region cannot route the following net(s) because they have pad(s)
outside the region with no same-net boundary stub to reconnect to: +3.3V,
/+12V, /AC_NEUTRAL, /I_SENSE_OUT, ... (+N more)
```

(Boundary-stub detection is the pure detector in
`src/kicad_tools/router/stub_terminals.py`.) This is correct behavior, not a
bug: a tile pass has no visibility outside its box, so a net with no in-tile
copper simply cannot be reconnected there.

#### The recipe

Avoid the refusal *by construction*: give every cross-region net a boundary
stub **before** you tile, by routing the long "trunk" nets (power rails, long
buses — the `+3.3V / +12V / AC_NEUTRAL / I_SENSE_OUT` class above) across the
whole board **first**, at a cheap coarse grid. Their copper then crosses every
future tile boundary, so each tile pass finds a same-net stub to reconnect to.

Worked example — a softstart-shape **160×100mm** board (motivating case from
[#4242](https://github.com/rjwalters/kicad-tools/issues/4242)), tiled into a
5×5 grid of 32×20mm boxes:

```bash
# 1. TRUNK PASS — route the whole board at a cheap coarse grid so the long
#    cross-region nets lay down copper that crosses every tile boundary.
#    160x100 @ 0.2mm ≈ 800 x 500 ≈ 400k cells — under the 500k default.
#    (Use --skip-nets to defer purely-local nets to the tile passes, or
#    hand-place the trunk traces if you prefer manual control.)
kct route board.kicad_pcb --grid 0.2 -o trunk.kicad_pcb

# 2. TILE-REFINE PASSES — refine each 32x20mm tile at the fine grid, chaining
#    the output so every pass preserves prior copper (--region implies
#    --preserve-existing). Cross-region nets now have a boundary stub from the
#    trunk pass, so they reconnect instead of being refused. Each tile is
#    ~635k cells, so raise --max-cells above the 500k default.
kct route trunk.kicad_pcb --region 0,0,32,20    --grid 0.03175 --max-cells 700000 -o t00.kicad_pcb
kct route t00.kicad_pcb   --region 32,0,64,20   --grid 0.03175 --max-cells 700000 -o t01.kicad_pcb
kct route t01.kicad_pcb   --region 64,0,96,20   --grid 0.03175 --max-cells 700000 -o t02.kicad_pcb
# ... continue across the row (x = 96..128, 128..160), then the next row
#     (y = 20..40, 40..60, 60..80, 80..100), chaining -o each time.
```

The full box list is the 5×5 lattice with column edges at
`x ∈ {0, 32, 64, 96, 128, 160}` and row edges at
`y ∈ {0, 20, 40, 60, 80, 100}`. Because the trunk pass already crossed every
one of those cut lines, no tile hits the boundary-stub refusal.

If a tile *does* report the refusal, it means a straddling net had no trunk
copper crossing that boundary — extend the trunk pass (or hand-route that net)
so it does, then re-run the tile.

> **Coming soon:** `route --auto-partition` — a planned follow-up that tiles
> the board, plants the cross-region boundary stubs, and routes the tiles in
> sequence automatically, making this recipe a single command.

### `--preserve-existing` leaves an already-connected net alone

A bare `--preserve-existing` pass routes **only the nets that still have open
connections**. A net whose pads are already all on one piece of copper — what
`kct check` reports as connected, counting traces, vias and same-net filled
zones — is held out of the route set entirely: its copper stays a hard
clearance obstacle for everything else and is re-emitted unchanged, rather than
being replaced by a fresh route
([#5788](https://github.com/rjwalters/kicad-tools/issues/5788)). Before that
fix a complete net was re-routed and its copper dropped, which cost board 06 all
64 of its pre-routed, coupled LVDS segments. The pass prints the nets it left
untouched.

Selecting nets explicitly overrides the exclusion, because there the re-route
*is* the request: `--nets NET[,NET...]`, `--region X1,Y1,X2,Y2` and `--complete`
each choose their own set and route it even when those nets are already
connected. Reach for `--nets` when you deliberately want a completed net
rebuilt (say, to re-apply a wider `--net-class-map` trace width to it).

### Preserved copper keeps its `--net-class-map` clearance (lattice engine)

Any multi-step composition (`--region`, `--nets`, or an explicit
`--preserve-existing`) routes one group of nets per step and treats the copper
from earlier steps as a fixed obstacle. On the **lattice** engine
(`--engine lattice`) that preserved copper is spaced at the
`--net-class-map` clearance **of the preserved net**, not merely at the
board-global DRU clearance
([#4597](https://github.com/rjwalters/kicad-tools/issues/4597)). The required
edge-to-edge gap between new copper and preserved copper is

```
max(new_net_class_clearance, preserved_net_class_clearance)
```

floored at `DesignRules.trace_clearance` — the same rule that applies to two
nets routed in a single pass, so a step boundary no longer changes the answer.
This is what makes the HV-outer recipe (route the mains group first, then the
low-voltage group with a map that puts 2.0–3.2 mm on the HV nets) actually
enforce those isolation gaps; before this the cross-step pairs collapsed to
the DRU floor (~0.2 mm) and passed DRC silently.

Practical consequence: **carry the HV entries in every step's map**, not just
the step that routes them. A preserved net with no entry in the current step's
map resolves to the global clearance exactly as before.

This works through **both** ways of supplying the map — the `net_class_map` in
a design/config script and the `--net-class-map <sidecar.json>` CLI flag.
Sidecar keys naming a preserved net used to be dropped on every filtered pass
(`--nets` / `--skip-nets` / `--region` / `--complete`), because the loader
rewrites a skipped net's pads to net 0 and the key was resolved only against
the nets being routed. The symptom was a
`Net-class map: merged 0/N sidecar entries` line and DRU-floor spacing on
preserved copper; that is fixed
([#4622](https://github.com/rjwalters/kicad-tools/issues/4622)) — the merge
line now reports the true count, and the run prints the usual
`WARNING: net-class-map: …` only for keys that match no net at all.

Two details worth knowing when writing the sidecar:

- **A preserved net must already carry copper on the input board.** The
  preserved-name domain comes from the segments and vias actually present in
  the file being routed. A net that has pads but no copper yet is not
  "preserved" — there is no earlier-step geometry to space against — so an
  entry naming it matches nothing and is reported as unmatched. Route it in an
  earlier step (or write the entry in the step that routes it) rather than
  expecting the current step to hold a gap around nothing.
- **Bare keys resolve against the nets being routed first.** Keys are matched
  on the sheet-local suffix after the last `/`, so a bare `LINE` key with a
  routable `/HV/LINE` and a preserved `/LV/LINE` on the board resolves to the
  **routable** net; the preserved net is left alone. Write the fully-qualified
  name when you mean the preserved one. A key ambiguous *within* one of the two
  groups is still applied to neither and warns, as before.

Not covered — document, do not assume:

- **`--engine grid`** blocks preserved routes at
  `segment_width / 2 + DesignRules.trace_clearance` for all routes, preserved
  or fresh; per-net class clearance is not applied to preserved copper there.
- **`--engine mesh`** inflates its preserved-copper obstacles at the global
  `trace_width + trace_clearance` and its committed model is net-agnostic, so
  it cannot express a per-net clearance at all.
- **Class-pair (pairwise) clearance** — the voltage-matrix mechanism is a
  separate feature and is not applied to preserved copper by this rule.
- **Via-to-via**: a *new* via applies its own class clearance to preserved
  copper traces, and honors a preserved via's class clearance, but a new via
  does not yet apply *its own* class clearance against other vias.

### Keepout rule areas (lattice engine)

The **lattice** engine (`--route-engine lattice`) honors KiCad **keepout rule
areas** — `(zone … (keepout …))` — at search time
([#4605](https://github.com/rjwalters/kicad-tools/issues/4605)):

- `(tracks not_allowed)` keeps every routed segment's **copper edge** (not
  just the centerline) out of the area, on the area's declared layers;
- `(vias not_allowed)` rejects any through-via whose barrel would enter the
  area on any of its layers;
- `(copperpour not_allowed)` is the zone filler's business and never
  constrains routing — the pour voids `kct zones hv-keepout` emits route
  identically with or without this feature;
- `pads` / `footprints` flags are ignored by the router (placement's remit).

Rule areas apply to **all nets by default** (standard KiCad semantics). To
scope an area per net class — the spatial HV-segregation primitive: disjoint
bank corridors are just complementary keepouts — add a `spatial_keepouts`
block to the `--net-class-map` sidecar, keyed by the rule area's **zone name**
(the `(name "…")` node, editable in the KiCad zone properties dialog):

```json
{
  "/HV_A": { "name": "HV_A" },
  "/HV_B": { "name": "HV_B" },
  "spatial_keepouts": {
    "hv-a-corridor-guard": { "only_classes": ["HV_A"] },
    "hv-b-corridor-guard": { "only_classes": ["HV_B"] },
    "antenna-zone":        { "except_classes": ["RF"] }
  }
}
```

Per entry, exactly one of:

- `only_classes` — the area constrains **only** nets in the listed classes
  (every other net routes through freely). "Class `HV_A` stays inside region
  A" is expressed as an area covering the **complement** of region A with
  `only_classes: ["HV_A"]`.
- `except_classes` — the area constrains every net **except** the listed
  classes (e.g. an exclusion zone that the zone's own bank may still enter).

A rule area with no `spatial_keepouts` entry (or no zone name) applies to all
nets. Naming a class that exists nowhere in the effective net-class map is a
hard error (exit 1) — a typo would otherwise silently widen or narrow a
spatial safety constraint. A net rendered unroutable by a keepout reports a
keepout-attributed decline (`pad-escape-*-keepout`,
`no-path-keepout-constrained`) rather than a generic failure.

**Engine support**: lattice only. `--route-engine grid` / `mesh` print a
one-line warning when the board declares track/via-blocking rule areas and
will route through them (KiCad's own DRC still flags the violations
downstream).

---

## See Also

- [Region routing: trunk-first, then tile-refine](#region-routing-trunk-first-then-tile-refine) — large-board tiling with `--region`
- [Swap Groups and Recorded Deltas](#swap-groups-and-recorded-deltas-netlist-degrees-of-freedom) — declaring a swap group, and replaying a committed delta as a recipe input
- [Placement Optimization Guide](placement-optimization.md)
- [DRC & Validation Guide](drc-and-validation.md)
- [Example: Autorouter](https://github.com/rjwalters/kicad-tools/tree/main/examples/04-autorouter)

### Project rules when changing the output name

When post-route DRC writes a renamed PCB, it carries the source board's
`.kicad_pro` and `.kicad_dru` into the destination before merging manufacturer
floors. The source files remain unchanged. Authored project settings, netclass
assignments, custom DRU text, and the `KCT_PRESERVE_BOARD_RULES` text variable
follow the output. By default, stricter authored minima remain active while
manufacturer limits can tighten weaker minima (see below).

#### `KCT_PRESERVE_BOARD_RULES`: how manufacturer floors meet authored minima

Every DRC-constraint export (`kct route` post-route DRC, `kct check
--emit-dru` / `--emit-drc-constraints`, `kct mfr apply-rules`, the
manufacturing export) merges the manufacturer profile into the board's
`.kicad_pro` and `.kicad_dru`. The project text variable
`KCT_PRESERVE_BOARD_RULES` selects how (Issues #5023, #6191):

| Value | Minima (`design_settings.rules`, `defaults`, `Default` netclass, DRU floors) | Severities | `Reviewed clearance - <class>` DRU rules |
|-------|------|------|------|
| unset / empty (default) | `max(authored, profile)` | overwritten by kct | only for classes above the clearance floor |
| `1` / `true` / `yes` / `on` | `max(authored, profile)` | authored kept | one per netclass |
| `0` / `false` / `no` / `off` | profile overwrites | overwritten by kct | none |

A stricter authored value always wins unless you opt out, and the profile
still raises any authored value below the fab floor. The DRU scalar floors
(track width, clearance, via drill and diameter, annular ring, copper to
edge) follow the same rule, so native zone fill and `kicad-cli` DRC use the
stricter authored clearance as well.

In the default mode only, a `Default` netclass whose `(clearance,
track_width, via_diameter, via_drill)` exactly matches a known template is
treated as unauthored and relaxed to the profile, as before #6191. The
templates are kct's own (`(0.15, 0.25, 0.6, 0.3)`), kct's pre-#5654 template
`(0.2, 0.25, 0.6, 0.3)`, KiCad 5/6 stock `(0.2, 0.25, 0.8, 0.4)` and KiCad 7+
stock `(0.2, 0.2, 0.6, 0.3)`. Change any one of those four values to make the
netclass count as authored, or set `KCT_PRESERVE_BOARD_RULES=1`. Other values
are treated as the default and logged as a warning.

Values kct itself wrote on an earlier pass count as authored too, so a script
that deliberately applies a *looser* reviewed process on top of them passes
`preserve_board_rules="0"` to `write_drc_constraints` (board 04's paid
0.15 mm drilling option does). That overrides the text variable for one call
without writing it into the project.

An existing destination sidecar must match either the corresponding authored
source or the result of applying the current manufacturer floors to it. JSON
project formatting does not matter; custom DRU text must match exactly.
Conflicting destination sidecars cause a visible error and exit code 1, even
with `--quiet` or `--auto-fix`. Neither project nor DRU is replaced on a conflict.
Choose a fresh output name or reconcile the authored files explicitly before
retrying. Identical repeated exports are accepted. If a source sidecar is
absent, an existing destination sidecar retains the usual preserve-and-merge
behavior. This propagation belongs to post-route DRC; `--skip-drc` does not
perform it.

### Verifying saved route artifacts

Routing writes `<output-stem>.route.json` after the final board save and
postprocessing. The versioned receipt records the route exit code and SHA-256
hashes and sizes of the saved PCB and its effective sibling `.kicad_pro` and
`.kicad_dru`. Missing optional sidecars are recorded explicitly. Authored source
sidecars are retained for renamed outputs, including completion no-op and
`--skip-drc` paths that do not generate new factory rules.
Conflicting existing destination sidecars that were not propagated from this
invocation's source cause a nonzero exit and no receipt; their bytes are retained.

The receipt also accompanies useful partial outputs. A stale output, staging
copy, or fatal constraint-propagation failure does not receive a current receipt.
Keep the receipt and its named files together when moving them. Verify them with:

```python
from kicad_tools.cli.route_receipt import verify_route_receipt

problems = verify_route_receipt("output_routed.route.json")
if problems:
    raise ValueError("; ".join(problems))
```

An empty problem list confirms matching artifact bytes and optional-file absence.
It does not certify electrical connectivity, native DRC, or factory DFM, and does
not authenticate who created the receipt. Later board or rule edits invalidate
the binding. Manufacturing packages retain their separate archive manifest.
