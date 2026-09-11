# Analog threshold budgets

The explicit analog threshold sidecar evaluates reviewed ideal linear networks.
It does not infer topology or certify a protection circuit. It can expose a
requirement violation under declared assumptions; **vertex sampling cannot prove
that all interior or physically reachable operating points meet a requirement**.

Run a source-bound report with:

```sh
python -m kicad_tools.analysis.analog_threshold board.kicad_sch thresholds.json > threshold-report.json
```

Exit 1 means a sampled model corner violates a requirement. Exit 2 means incomplete
or merely sampled evidence; there is intentionally no release-qualified exit 0.
Every report has `release_eligible: false`. A missing sidecar, unknown bound,
unbound pin, changed MPN/value, stale schematic hash, or unsupported hierarchy
cannot become a qualified pass. Flat schematics are supported in this first
implementation; hierarchy is explicitly incomplete.

The policy object has `schema_version: 1`, `schematic_sha256`, `bindings`, and a
nonempty `networks` array. Bind each used resistor and active device by reference:

```json
{
  "R1": {
    "value": "499k",
    "mpn": "exact-reviewed-MPN",
    "pins": {"1": "BANK_POS", "2": "INPUT_SERIES_1"}
  }
}
```

Actual symbol values, exact `MPN` (or `Mfr_Part_Number`) fields, and the extracted
pin/net connections must match. Modeled resistor ohms must also match the parsed
schematic value. Missing MPNs remain unresolved, including on resistors. Root
schematic and exact sidecar bytes are SHA256-bound in JSON. Any schematic-byte
change invalidates the reviewed bindings; updating the hash is a review action,
not an automatic refresh. The report includes all checked reference/net bindings.

Each network declares `id`, `kind`, `resistors`, `bounds`, `devices`, `conditions`,
`requirements`, optional `assumptions`, and `unmodeled_terms`. Topology/role
assignment is reviewed policy input, **not inferred or independently certified**
from connectivity. `devices` maps `comparator` (and, for the difference-amplifier
model, `amplifier` and `buffer`) to exact schematic references.

Each resistor role is a nonempty series list of objects with `reference`, `ohms`,
`tolerance` (fraction, e.g. 0.001), and `source` provenance. References cannot be
reused in different roles within a model. A series arm's resistance interval is
the exact sum of its independent resistor intervals. No resistor datasheet limit
is invented. Supply and error bounds have `min`, `nominal`, `max`, and `source`.
Missing semiconductor error terms use zero **only to retain a clearly incomplete,
partial illustrative calculation**; the omitted terms are listed in the report.
Missing supply/reference/common-mode ranges prevent calculation.

`conditions.temperature_c` uses the same range/provenance format;
`conditions.linear_operation` records the reviewed operating-region assumption.
These are recorded conditions, not an automatic check of an amplifier's common-
mode or output capability. Error bounds must cover those conditions. List bias-
current, reference/buffer error, loading, semiconductor temperature effects,
nonlinearity, transients and any other unmodeled behavior in `unmodeled_terms`.
Absent temperature or operating-region conditions yield incomplete coverage.

## Difference amplifier and positive-feedback comparator

The `difference_comparator` template uses these resistor roles:

| Role | Meaning |
|---|---|
| `input_positive` | Positive terminal to noninverting amplifier input |
| `input_negative` | Negative terminal to inverting amplifier input |
| `feedback` | Amplifier output to inverting input |
| `reference_arm` | Buffered midpoint to noninverting input |
| `midpoint_top`, `midpoint_bottom` | Supply-to-ground midpoint divider |
| `comparator_top`, `comparator_bottom` | Supply-to-midpoint comparator reference divider |
| `signal`, `hysteresis` | Amplifier signal and comparator output to comparator summing input |

The differential input is `Vpositive - Vnegative`; `common_mode` is
`(Vpositive + Vnegative)/2`. This convention matters when input resistor ratios
are mismatched. Required bounds are `supply`, `common_mode`, `amplifier_offset`,
`buffer_offset`, `comparator_offset`, `output_low`, and `output_high_drop`.
The offset signs are positive additive input-referred errors (buffer offset adds
to midpoint; comparator offset adds to reference). Comparator output high equals
the **same sampled supply** minus `output_high_drop`; low is `output_low` above
ground. This keeps reference, midpoint, and output swing correlated.

For each resistor corner, with P/N the input arms, F the feedback and T the
reference arm, positive gain is `(1 + F/N) * T/(P + T)`, negative gain is `F/N`,
and differential gain is their average. The nominal amplifier baseline includes
the gain mismatch times common mode and the buffered-midpoint contribution.
Comparator trip solves the summing-node KCL with output low for rising and output
high for falling. Hysteresis is computed **at the same corner** as rising minus
falling, rather than subtracting unrelated extrema.

The included [partial softstart model](../../tests/fixtures/analog_threshold/softstart_partial.json)
reproduces the #5064 illustrative fixture. Evaluate an unbound math model using
`evaluate_network(json.load(...))`; its result explicitly says
`source_binding: unbound_math_model` and grants no schematic-backed approval.
Four 499k resistors per arm, ±0.1% resistors, supply 3.234–3.366 V and common mode
−250…250 V give 4,096 reduced-arm vertices:

| Threshold | Nominal | Sampled minimum | Sampled maximum |
|---|---:|---:|---:|
| Rising | 70.24398 V | 67.66329 V | 72.83212 V |
| Falling | 60.33902 V | 57.96032 V | 62.72502 V |

The falling samples violate the declared 60 V minimum. Coverage is still
incomplete because semiconductor limits and reachable corner constraints are
missing. These numbers are not approved design limits or factory guarantees.

## Divider template and requirements

The `divider` template has resistor roles `top` and `bottom`, and bounds
`reference` and `comparator_offset`. Its input trip is
`(reference + comparator_offset) * (1 + top/bottom)`. Rising and falling are equal
and hysteresis is zero. This simple template has no feedback/output-state model.

`requirements` can constrain `rising`, `falling`, and `hysteresis` with `min` /
`max` volts and a reviewed `source`. Reports provide nominal values, sampled
extrema, the corner attaining each extremum, assumptions and omitted terms.
Missing requirements or provenance remain incomplete. Vertices are bounded to
262,144; larger models fail visibly rather than silently truncating the sweep.
The 1e-9 V comparison tolerance is a numerical rounding allowance, not component
or manufacturing tolerance. No geometrical DRC waiver is consulted.
