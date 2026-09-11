# Resistor operating budgets

Evaluate a user-reviewed JSON sidecar with:

```sh
uv run python -m kicad_tools.analysis.resistor_rating resistor-budget.json > resistor-report.json
```

Exit codes: 0 means all supplied envelopes pass, 1 means an envelope is
exceeded, and 2 means evidence is incomplete or input is invalid. This command
never changes a schematic or manufacturing readiness. A pass covers only the
selected operating states and supplied ratings; it does not establish heatsink
performance or physical qualification.

Use `Budget.model_json_schema()` from `kicad_tools.analysis.resistor_rating`
to obtain the complete input schema. Unknown fields, nonfinite numbers,
invalid curves, duplicate references, and invalid sample periods are rejected.
All field names include their units; resistance tolerance is a fraction.
Nonzero numeric inputs must have magnitude between `1e-20` and `1e20`
(inclusive); zero is allowed where the field permits it. Values outside this
documented arithmetic range are rejected as incomplete instead of risking
underflow/overflow false passes. This includes curve coordinates and fractions.

## Minimal continuous-load sidecar

The following values are **synthetic examples**, not qualified ratings for
any manufacturer part. Replace every hash with the SHA256 of the exact reviewed
file. `MPN` must match the corresponding schematic symbol property exactly.
All evidence paths resolve relative to the sidecar and must stay inside its
directory (including after symlink resolution).

```json
{
  "schema_version": 1,
  "schematic": {"path": "board.kicad_sch", "sha256": "<64 lowercase hex digits>"},
  "operating_policy": {"path": "operating-policy.md", "sha256": "<64 lowercase hex digits>"},
  "active_states": ["balance"],
  "resistors": [{
    "reference": "R1",
    "mpn": "replace-with-reviewed-MPN",
    "resistance_ohm": 10,
    "tolerance_fraction": 0.01,
    "ratings": [{
      "basis": "ambient",
      "power_w": 2,
      "requires_mounting": false,
      "derating": [[0, 1], [70, 1], [150, 0]],
      "provenance": {
        "source": "Reviewed datasheet revision, page and condition",
        "reviewed_by": "Reviewer name",
        "reviewed_on": "2026-09-10"
      }
    }],
    "states": {
      "balance": {
        "kind": "continuous",
        "samples": [{"duration_s": 1, "voltage_v": 3}],
        "temperature_basis": "ambient",
        "temperature_c": 25,
        "temperature_evidence": {"path": "conditions.md", "sha256": "<64 lowercase hex digits>"},
        "assumptions": ["Constant applied voltage; switch drop ignored"]
      }
    }
  }],
  "simultaneous_groups": {"balance-bank": ["R1"]}
}
```

At 3 V, a 10 ohm ±1% resistor is evaluated at 9.9 ohms: about 0.90909 W.
Ten explicitly listed simultaneous channels produce about 9.0909 W aggregate.
The aggregate is heat generation, not a temperature prediction; a sum of peak
powers is an upper bound assuming coincident peaks.

Only names in `active_states` are evaluated. Historical scenarios may remain
in `states` without becoming current requirements. Missing selected states
produce incomplete rows. The report includes the sidecar hash (binding all
MPNs, ratings, assumptions and selected states), schematic/policy hashes, and
per-reference/per-state results. Changing any evidence requires deliberate
review and hash replacement; hashes are never updated automatically.

## Rating conditions

Supply the rating basis as `ambient`, `terminal`, or `flange`. The temperature
basis must match; an ambient temperature cannot qualify a flange rating.
Derating curves use increasing temperatures and nonincreasing fractions of the
nameplate power, with piecewise-linear interpolation and no extrapolation.
Choose only ratings applicable to the intended operating condition. If multiple
ratings are supplied, **all** are checked independently; any incomplete or
failed row prevents an overall pass. To assess alternative ambient versus
terminal installations, use separate budgets with their own evidence.

A terminal/flange rating, or any rating with `requires_mounting: true`, also
requires `mounting_evidence` and `interface_evidence` in the operating state.
Both use the same path/hash form as temperature evidence. A load below a
100 W nameplate remains incomplete without these reviewed installation and
temperature records. File presence/hash verifies identity, not the scientific
adequacy of the review; the reviewer must establish actual mounting and thermal
conditions. No datasheet is scraped and no package-size estimate is promoted
into a qualified rating.

## Waveforms and pulse envelopes

Each sample is a **piecewise-constant interval**, not a point to interpolate.
Supply exactly one of `voltage_v`, `current_a`, or nonnegative `power_w` per
interval, plus positive `duration_s`. Voltage drive uses minimum tolerance
resistance; current drive uses maximum resistance. Direct dissipation requires
`reviewed_power_includes_tolerance: true`, otherwise its row is incomplete.

Use `kind: periodic` with `period_s` at least the total interval duration. Any
remaining time is explicitly zero power. Use `single_pulse` without a period
for one event. `continuous` accepts exactly one constant interval. Samples for
one pulse envelope describe **one conservative pulse window**: interspersed
zero-power intervals do not reset its duration. More detailed multi-pulse or
frequency-dependent thermal models are outside this report.

The report calculates peak and cycle-average power, mathematical RMS of power,
window energy, window duration, declared duty, and repetition frequency.
`rms_power_w` is sqrt(mean(P²)); it is **not** an average heating-power substitute.
For a single pulse, the reported average is over that pulse window. Continuous
window energy depends on the declared observation duration.

Every non-continuous state requires an applicable reviewed `pulse_limits`
envelope on the resistor, with:

- `basis`, `temperature_max_c`, and `provenance`;
- `duration_max_s`, `peak_max_w`, `energy_max_j`, and `repetition_max_hz`.

All supplied envelopes matching the rating basis are constraints, not alternative
operating options. They must be reviewed conservative bounds for the sampled
waveform shape and installation. Temperature applicability must be established;
missing limits are incomplete. An average-safe waveform can still fail peak,
energy, duration, or repetition limits. This first version does not interpolate
manufacturer pulse curves or infer their shape/applicability.
