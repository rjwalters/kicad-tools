# 06 — Differential-Pair DRC Rules

Five `rule_id`s in `kicad_tools.validate.rules.*` validate the routed result
(`kct check --only <rule_id>`). They match the `ViolationType.from_string`
alias table in `src/kicad_tools/drc/violation.py`.

## `diffpair_clearance_intra`

Source: `DiffPairClearanceIntraRule` in
`src/kicad_tools/validate/rules/diffpair_clearance_intra.py`.
Validates the within-pair gap against
`NetClassRouting.effective_intra_pair_clearance()`. Distinct from generic
`clearance` because the intra-pair gap is allowed to be *tighter* than the
manufacturer's inter-net floor.

```bash
kct check board.kicad_pcb --only diffpair_clearance_intra
```

Remediate: raise the routed gap to `effective_intra_pair_clearance()`, or
lower `intra_pair_clearance` if the tight gap is intentional (guide 02).

## `diffpair_routing_continuity` (info only)

Source: `DiffPairRoutingContinuityRule` in
`src/kicad_tools/validate/rules/diffpair_routing_continuity.py`.
Measures an engaged pair's coupled fraction (share of P's routed length
whose nearest point on N is within the coupling window AND parallel within
±15°). **Info severity only since #6443**: it never fails a board, and KiCad
has no native check for it. What a fab or protocol limits is the uncoupled
length (next rule). `coupled_continuity_threshold` is **deprecated** (it only
moves the figure the info line compares with) and emits a `DeprecationWarning`.

## `diffpair_uncoupled_length`

Source: `DiffPairUncoupledLengthRule` in
`src/kicad_tools/validate/rules/diffpair_uncoupled_length.py`.
Holds the larger of the two nets' uncoupled lengths to the pair's limit
(`max_uncoupled_total_mm`, else the protocol's per-terminal allowance times the
components on the net), each terminal's uncoupled copper outside the
courtyards to `max_uncoupled_per_terminal_mm` (kct-only), each net's via count
to `max_vias_per_net`, and warns on unequal P and N via counts. Details: [guide 08](08-uncoupled-length.md).

## `diffpair_length_skew`

Source: `DiffPairLengthSkewRule` in
`src/kicad_tools/validate/rules/diffpair_length_skew.py`.
Fires when an engaged pair's routed length skew (`|L_p - L_n|`) exceeds
its limit. Total-length parity, not topology.

The limit resolves as `skew_tolerance_mm` on the class, then the declared
`pair_protocol`'s value in the protocol defaults table, then the generic
0.5 mm (see [guide 07](07-protocol-defaults.md)). The finding names the source
in brackets. It is an **error** when the limit is cited or explicit, and a
**warning** when the limit is an `uncited` table value.

```bash
kct check board.kicad_pcb --only diffpair_length_skew
```

Remediate: insert serpentine on the shorter half (manual today; #2648 will
automate), declare the pair's real `pair_protocol`, or set
`skew_tolerance_mm` on the net class (see guide 04).

## `impedance`

Source: `ImpedanceRule` in
`src/kicad_tools/validate/rules/impedance.py`. Validates routed
trace geometry against `target_diff_impedance` or `target_single_impedance`
on the class, allowing `impedance_tolerance_percent` (default 10 %)
deviation.

```bash
kct check board.kicad_pcb --only impedance
```

Remediate: re-route with `apply_impedance_driven_sizing` (set
`target_diff_impedance` so the router consumes the stackup), correct the
stackup, or relax `impedance_tolerance_percent` (see guide 03).

## Running all five together

`--only diffpair_clearance_intra,diffpair_routing_continuity,diffpair_length_skew,diffpair_uncoupled_length,impedance`,
or `kct check --mfr jlcpcb`, which enables all five plus the manufacturer's
inter-net floors.

## Native pair rules

`kct route` writes native `diff_pair_gap`, `diff_pair_uncoupled`, `skew` and
`via_count` rules per coupled pair into one managed `.kicad_dru` block, at the
limits `kct check` resolves (guide 02 for the gap, guide 08 for the rest).
Pair violations block `manufacturable` only when the limit is cited or
explicit; the generic 0.5 mm skew default stays an error.

## ViolationType enumeration

Each `rule_id` maps to a member of the `ViolationType` enum in
`src/kicad_tools/drc/violation.py`. The `_ALIASES` table inside
`ViolationType.from_string` guarantees the rule_id strings round-trip
without falling through the fuzzy fallback to `UNKNOWN`.
