# Hybrid Figure-of-Merit (FOM)

Issue #3186 introduces a layered Figure-of-Merit (FOM) function for
evaluating PCB placement + routing quality.  This guide explains how the
FOM is structured, how to use it from the CLI and from Python, and how to
extend it.

## Architecture

```
FOM(placement, routing) =
    Π_i  pass(constraint_i)           ← hard gate, ∈ {0, 1}
  × exp(−Σ_j  w_j · soft_term_j)      ← log-linear soft penalty
  × predictor(placement)^β            ← learned residual (issue #3187)
```

* **Hard constraints** are binary: any failure drops the score to 0.
  No partial credit -- this matches the actual ship/no-ship decision.
* **Soft terms** are normalised real-valued penalties (0 = perfect,
  larger = worse).  Their weighted sum goes through an exponential to
  produce a [0, 1] score.
* **Predictor hook** is exposed but unused in issue #3186 (β=0).  Issue
  #3187 fills it in with a learned residual model.

## Hard constraints

Four binary gates run before any soft term is scored:

| Constraint | What it checks |
|---|---|
| `drc_clean` | `kct check --mfr <profile>` reports `errors ≤ tolerance_floor`. Per-board floors live in `.github/routed-drc-tolerance.yml`. |
| `lvs_clean` | No orphan pads (PCB pads with no schematic net). |
| `erc_clean` | `kct erc` reports zero errors. |
| `mfg_tolerance_allowlist` | The allowlist file itself parsed cleanly. |

Each check is **optional**: when the caller doesn't pass the relevant
report, the check is skipped.  This lets the GA inner loop run cheap
DRC-only checks during search and reserve the full ERC+LVS gate for
final candidate evaluation.

## Soft terms

All ten terms are normalised so that **0 = perfect** and **larger =
worse**.  They live in three sister modules under `kicad_tools.optim`:

### Geometry (`fom_geometry.py`)

| Term | Formula | Phase 1 normalisation |
|---|---|---|
| `trace_length_excess` | `Σ_net (actual − RSMT_lower) / RSMT_lower` | Per-net relative; unrouted nets contribute 0. |
| `turning_penalty` | `Σ_seg-pair (θ mod 45°)² / total_length` | deg² per mm. 90° corners score 0; 5° wiggles score 25 per pair. |
| `net_congestion_variance` | `stddev(cell_length) / mean(cell_length)` | Coefficient of variation over a 10×10 grid; unitless. |
| `crossing_count` | Pre-route SMT-projection inter-net crossings | Raw count. |
| `compactness` | `(hull_area − essential_exterior) / pad_count` | mm² per pad. |

### Electrical (`fom_electrical.py`)

| Term | Formula | Phase 1 normalisation |
|---|---|---|
| `weighted_via_count` | `Σ_via cost_class(via)` | Standard=1.0, micro=3.0, blind=5.0, buried=8.0. |
| `match_group_skew` | `Σ_group max(0, skew - tol) / tol` | Tolerance-widths over spec, summed across groups. |
| `diff_pair_clearance_margin` | `Σ_pair max(0, target − actual)` | mm of clearance shortfall, summed across pairs. |
| `decoupling_proximity` | `Σ_IC-power-pin distance_to_nearest_cap` | mm. |

### Thermal (`fom_thermal.py`)

| Term | Formula | Phase 1 normalisation |
|---|---|---|
| `thermal_spread` | `Σ_part power_W × distance_to_pour_mm` | W·mm. Only fires when component-level power metadata is declared. |

## CLI

### `kct optim fom-debug`

Prints the per-term FOM breakdown for an existing PCB:

```bash
kct optim fom-debug board.kicad_pcb
```

With custom weights and verbose feature stats:

```bash
kct optim fom-debug board.kicad_pcb \
  --weights src/kicad_tools/optim/weights/legacy.yaml \
  --verbose
```

JSON output (for piping into analysis scripts):

```bash
kct optim fom-debug board.kicad_pcb --format json | jq '.soft_terms'
```

Sample output:

```
FOM breakdown for board.kicad_pcb
  score:           0.123456
  soft_score:      0.123456
  hard_gate:       PASS
  predictor*beta:  1.0000 ** 0.0

Term                                      Raw     Weight     Weighted
----------------------------------------------------------------------
trace_length_excess                    0.1985      1.000       0.1985
weighted_via_count                     3.0000      1.000       3.0000
turning_penalty                       51.9650      1.000      51.9650
...
```

## Python API

### Basic usage

```python
from kicad_tools.optim.fom import compute_fom, default_weights
from kicad_tools.schema.pcb import PCB

pcb = PCB.load("board.kicad_pcb")
result = compute_fom(pcb)
print(result.summary())
print(f"score = {result.score:.4f}")
print(f"length excess = {result.soft_terms['trace_length_excess']:.4f}")
```

### Loading weights from YAML

```python
from kicad_tools.optim.fom import compute_fom, load_weights_from_yaml

weights = load_weights_from_yaml("custom_weights.yaml")
result = compute_fom(pcb, weights=weights)
```

YAML weights file format:

```yaml
weights:
  trace_length_excess: 2.0  # double-weight wirelength
  weighted_via_count: 0.5    # half-weight vias
  # any term not listed defaults to 1.0
```

### With hard constraints

```python
from kicad_tools.optim.fom import compute_fom
from kicad_tools.drc.report import DRCReport
from kicad_tools.erc.report import ERCReport

drc = DRCReport.load("board.drc.json")
erc = ERCReport.load("board.erc.json")

result = compute_fom(
    pcb,
    drc_report=drc,
    erc_report=erc,
    pcb_path="board.kicad_pcb",
    tolerance_allowlist_path=".github/routed-drc-tolerance.yml",
)
if not result.hard_gate_passed:
    print(f"Failed: {result.hard_failures}")
else:
    print(f"Score: {result.score:.4f}")
```

### Predictor hook (issue #3187)

The predictor parameter and `beta` exponent are reserved for issue #3187
(learned residual).  They are wired through but inert in this issue:

```python
def my_predictor(pcb) -> float:
    """Return a probability in [0, 1]."""
    ...

result = compute_fom(pcb, predictor=my_predictor, beta=1.0)
# score = soft_score * my_predictor(pcb)**beta
```

With `beta=0` (the default), the predictor is ignored.

### Computing individual terms

Each soft term is independently callable:

```python
from kicad_tools.optim.fom_features import extract_features
from kicad_tools.optim.fom_geometry import trace_length_excess, compactness
from kicad_tools.optim.fom_electrical import weighted_via_count

features = extract_features(pcb)
print("length excess:", trace_length_excess(features))
print("compactness:", compactness(features))
print("via cost:    ", weighted_via_count(features))
```

This is useful for plotting any one term against placement variations
during GA tuning.

## Performance

The FOM walks every footprint, pad, segment, and via once via
`extract_features()`.  For a typical board 05 (32 nets, ~250 segments)
the full FOM evaluation runs in roughly 10-50 ms.

If you're computing the FOM repeatedly on the same PCB (e.g. inside a GA
inner loop with predictor variations), cache the
`BoardFeatures` snapshot:

```python
features = extract_features(pcb)
for w in candidate_weights:
    result = compute_fom(pcb, weights=w, features=features)
```

## Extending

To add a new soft term:

1. Implement a function in the appropriate sister module (`fom_geometry.py`,
   `fom_electrical.py`, `fom_thermal.py`) following the contract:
   - Takes `BoardFeatures` (and optionally `PCB`) as input
   - Returns a non-negative float
   - 0 = perfect, larger = worse
   - Has a docstring stating the normalisation
2. Add the term name to `SOFT_TERM_NAMES` in `fom.py`.
3. Add the field to `FOMWeights` (default 1.0).
4. Wire it into `compute_soft_terms()`.
5. Add a unit test in `tests/test_optim_fom_*.py`.

The same applies to hard constraints: extend `HARD_CONSTRAINT_NAMES` and
the body of `check_hard_constraints()`.

## See also

* Issue #3186 -- this FOM implementation
* Issue #3187 -- learned predictor (β > 0)
* Issue #3188 -- weight calibration (Pareto sweep)
* PR #3114 -- the `--use-routing-fitness` baseline this aspires to
  replace
* PR #3145 -- match-group infrastructure reused by the match-group term
