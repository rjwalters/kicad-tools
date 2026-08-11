# 06 — Differential-Pair DRC Rules

Four `rule_id`s in `kicad_tools.validate.rules.*` validate the routed result.
Run via `kct check --rules=<rule_id>`. The rule_id strings below are the
public CLI surface and match the `ViolationType.from_string` alias table in
`src/kicad_tools/drc/violation.py`.

## `diffpair_clearance_intra`

Source: `DiffPairClearanceIntraRule` in
`src/kicad_tools/validate/rules/diffpair_clearance_intra.py`.
Validates the within-pair gap against
`NetClassRouting.effective_intra_pair_clearance()`. Distinct from generic
`clearance` because the intra-pair gap is allowed to be *tighter* than the
manufacturer's inter-net floor.

```bash
kct check board.kicad_pcb --rules=diffpair_clearance_intra
```

Remediate: raise the routed gap to `effective_intra_pair_clearance()`, OR
lower `intra_pair_clearance` on the net class if the tight gap is
intentional (see guide 02).

## `diffpair_routing_continuity`

Source: `DiffPairRoutingContinuityRule` in
`src/kicad_tools/validate/rules/diffpair_routing_continuity.py`.
Fires when an engaged pair's coupled fraction (share of P's routed length
whose nearest point on N is within the coupling window AND parallel within
±15°) falls below `effective_coupled_continuity_threshold(default=0.7)`.
Topology check, not edge-to-edge spacing.

```bash
kct check board.kicad_pcb --rules=diffpair_routing_continuity
```

Remediate: re-route the pair through `CoupledPathfinder` (set
`coupled_routing=True`), tighten launch geometry, or lower the per-class
threshold for hobby boards (see guide 02).

## `diffpair_length_skew`

Source: `DiffPairLengthSkewRule` in
`src/kicad_tools/validate/rules/diffpair_length_skew.py`.
Fires when an engaged pair's routed length skew (`|L_p - L_n|`) exceeds
`effective_skew_tolerance(default=0.5)`. Total-length parity, not topology.

```bash
kct check board.kicad_pcb --rules=diffpair_length_skew
```

Remediate: insert serpentine on the shorter half (manual today; #2648 will
automate), or relax `skew_tolerance_mm` on the net class (see guide 04).

## `impedance`

Source: `ImpedanceRule` in
`src/kicad_tools/validate/rules/impedance.py`. Validates routed
trace geometry against `target_diff_impedance` or `target_single_impedance`
on the class, allowing `impedance_tolerance_percent` (default 10 %)
deviation.

```bash
kct check board.kicad_pcb --rules=impedance
```

Remediate: re-route with `apply_impedance_driven_sizing` (set
`target_diff_impedance` so the router consumes the stackup), correct the
stackup, or relax `impedance_tolerance_percent` (see guide 03).

## Running all four together

```bash
kct check board.kicad_pcb \
  --rules=diffpair_clearance_intra,diffpair_routing_continuity,diffpair_length_skew,impedance
```

Or use a manufacturer profile — `kct check --mfr jlcpcb` enables all four
plus the manufacturer's inter-net floors.

## ViolationType enumeration

Each `rule_id` maps to a member of the `ViolationType` enum in
`src/kicad_tools/drc/violation.py`. The `_ALIASES` table inside
`ViolationType.from_string` guarantees the rule_id strings round-trip
without falling through the fuzzy fallback to `UNKNOWN`.
