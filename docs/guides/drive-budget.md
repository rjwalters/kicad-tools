# Weak-source and gate-drive budget review

Run a declared budget without modifying design files:

```sh
uv run python -m kicad_tools.analysis.drive_budget drive-budget.json > drive-report.json
```

The report distinguishes nominal shortfalls from shortfalls proved by supplied
upper/lower bounds. Exit 1 means a guaranteed optimistic voltage ceiling is below
the target; exit 2 means incomplete evidence, including a nominal shortfall.
**This screen never returns a qualification pass.** An upper ceiling above the
target does not establish available current at the loaded operating point,
current gain, collector headroom, switching performance or hardware readiness.

`DriveBudget.model_json_schema()` in `kicad_tools.analysis.drive_budget` provides
the full schema. Every electrical quantity carries `value`, `basis`, originating
`part` and `source`. Basis is explicitly `nominal`, `upper_bound` or `lower_bound`.
Do not relabel typical values or a minimum short-circuit current as an upper
bound. Values must be finite, nonnegative and no greater than 1e18; nonzero
quantities must be at least 1e-18. Resistance and effective capacitance are positive.

## Inputs and outputs

The schema requires `schema_version: 1`, a named `state`, `topology`,
`target_drive_v`, `duration_s`, explicit `assumptions` and `unmodeled_effects`.
Electrical quantities (each in the four-field form above) are:

| Field | Meaning |
|---|---|
| `source_current_cap_a` | Declared optimistic source-current ceiling, not an assumption that short-circuit current is available at nonzero voltage |
| `source_voltage_cap_v` | Declared optimistic source-voltage ceiling |
| `bias_resistance_ohm` | Source/base-node resistance to its floating reference |
| `follower_drop_v` | Nonnegative base-to-emitter drop for the declared follower |
| `bleeder_resistance_ohm` | Static gate/source bleeder load |
| `reservoir_initial_v` | Initial reservoir voltage |
| `reservoir_effective_f` | Effective capacitance under the stated voltage/temperature conditions |
| `gate_charge_c` | Total charge drawn at the edge, including all simultaneously driven gates |
| `additional_static_a` | Additional constant load during the state |

The implemented source screen is an **emitter follower**. It reports an optimistic
base ceiling `min(I_cap R_bias, V_cap)` and subtracts the declared nonnegative
drop. Collector reservoir voltage cannot increase this ceiling. Base loading is
omitted deliberately, so the estimate is optimistic. An unsupported topology is
explicitly incomplete; no voltage-gain topology is silently mapped to a follower.

For a guaranteed shortfall, only directionally valid inputs participate: an
upper source-current bound times an upper bias-resistance bound, or an upper
source-voltage bound. A lower bound on follower drop can tighten that ceiling;
otherwise zero drop is used optimistically. A source-current minimum alone
cannot establish a maximum output voltage. Full source I/V curves and nonlinear
loaded operating-point solutions remain explicit simulation/review work; this
first screen consumes declared I/V **ceilings**, not inferred curve interpolation.

Outputs also include required bias current at the target, nominal source-current
headroom, static bleeder current, gate charge voltage step, and charge demand
versus reservoir charge available above the target. Every originating part and
source is retained in the report, with the complete input sidecar's SHA256.

## Reservoir models

Two ideal alternatives are reported separately; neither proves the actual
collector/gate circuit follows that load law:

1. Instant gate-charge loss `Q/C`, then an RC bleeder in parallel with a declared
   constant additional current. The residual voltage is clamped at zero.
2. Instant gate-charge loss, then constant current equal to target voltage divided
   by bleeder resistance plus additional static load.

A regulated emitter output and a freely decaying RC node have different load
laws. The report does not quietly choose one as verified topology. User-supplied
bound labels remain visible, but these reservoir calculations are labeled nominal
model estimates, not guaranteed switching behavior. Base current, minimum gain,
collector headroom, leakage, dynamic gate charge, MLCC bias loss and transient
coupling need evidence or remain listed as unmodeled. No post-edge load disappears
just because the gate has finished charging.

## Reproduction values

[Vishay VOM1271 revision 1.9](https://www.vishay.com/docs/83469/vom1271.pdf)
identifies 15 µA short-circuit current and 8.4 V open-circuit voltage as typical
at 10 mA LED current. Its 6 µA minimum short-circuit current is not a guaranteed
upper bound. With an optimistic 15 µA available and a 100k bias resistor, the base
ceiling is only 1.5 V before transistor loading. These inputs must remain nominal;
the report flags a nominal shortfall against a 12 V target, not a guaranteed
manufacturer limit.

A 10k bleeder at 12 V draws 1.2 mA. A 10 µF reservoir with that ideal RC load loses
about 0.923 V over 8 ms, even with zero gate charge and zero additional current.
The constant 1.2 mA alternative loses 0.96 V. Additional charge and current worsen
both budgets.

Optional `crossing` inputs are `rms_voltage_v`, `frequency_hz`, `threshold_v`.
For a sinusoid, the full interval around **one** zero crossing below the absolute
threshold is `asin(threshold / (sqrt(2) Vrms)) / (pi frequency)`. At 120 Vrms,
60 Hz and 10 V it is about 0.313 ms, with two such intervals per cycle. At or
above peak voltage, the entire cycle is within the threshold and the report
returns one full-period interval instead of double-counting it.
