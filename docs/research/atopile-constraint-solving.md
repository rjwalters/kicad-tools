# Atopile Constraint Solving & Parametric Part Selection Research

## Overview

This document captures research findings from analyzing atopile's constraint solving system for parametric component selection (Issue #303). The atopile project uses a sophisticated symbolic constraint solver combined with an LCSC/EasyEDA-backed part picker to automatically select components that satisfy design constraints.

## Source Code Locations

- **Solver Core**: `vendor/atopile/src/faebryk/core/solver/`
- **Part Picker**: `vendor/atopile/src/faebryk/libs/picker/`
- **Build Pipeline**: `vendor/atopile/src/atopile/build_steps.py`
- **Parameter System**: `vendor/atopile/src/faebryk/core/parameter.py`
- **Interval Sets**: `vendor/atopile/src/faebryk/libs/sets/quantity_sets.py`

## Architecture Overview

### Constraint Solver (`DefaultSolver`)

The solver is a symbolic constraint solving system that operates iteratively:

```
┌─────────────────────────────────────────────────────────────────┐
│                     DefaultSolver                               │
├─────────────────────────────────────────────────────────────────┤
│  Pre-processing Phase:                                          │
│    - convert_to_canonical_literals                              │
│    - convert_to_canonical_operations                            │
│    - constrain_within_domain                                    │
│    - alias_predicates_to_true                                   │
├─────────────────────────────────────────────────────────────────┤
│  Iterative Phase (runs until no changes):                       │
│    - Structural algorithms (contradiction detection, aliasing)  │
│    - Expression grouping (associative, reflexive, idempotent)   │
│    - Pure literal folding                                       │
│    - Expression-wise folding                                    │
│    - Subset merging and transitivity                            │
│    - Upper estimation                                           │
└─────────────────────────────────────────────────────────────────┘
```

Key characteristics:
- **Graph-based**: Parameters and expressions form a graph structure
- **Iterative refinement**: Algorithms run until fixpoint (no more changes)
- **Timeout protection**: `MAX_ITERATIONS_HEURISTIC` prevents infinite loops
- **Contradiction detection**: Raises `Contradiction` for unsatisfiable constraints

### Part Picker Flow

```
┌────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│ Design Module  │ ──> │ _prepare_query() │ ──> │ atopile backend │
│ (constraints)  │     │ (serialize sets) │     │ (LCSC API)      │
└────────────────┘     └──────────────────┘     └────────┬────────┘
                                                         │
                                                         v
┌────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│ attach() with  │ <── │ Check compatible │ <── │ Component       │
│ footprint/sym  │     │ params via solver│     │ candidates      │
└────────────────┘     └──────────────────┘     └─────────────────┘
```

## Key Questions Answered

### Q1: How does the solver handle over-constrained or under-constrained systems?

**Over-constrained systems:**
```python
# From solver.py - NotDeducibleException and Contradiction
class NotDeducibleException(Exception):
    """Raised when predicate cannot be proven true or false"""

# From utils.py - Contradiction types
class Contradiction(Exception): ...
class ContradictionByLiteral(Contradiction): ...
```

The solver detects contradictions through:
1. `structural.check_literal_contradiction` - Direct literal conflicts
2. `structural.empty_set` - Empty intersection of constraints
3. Failed predicate fulfillment in `try_fulfill()`

When a contradiction is detected, the picker raises `PickError` and aborts.

**Under-constrained systems:**
- Parameters remain as unbounded intervals (`Quantity_Interval_Disjoint.unbounded()`)
- The picker queries with domain defaults
- First compatible part is selected (sorted by basic/preferred status, then price)

### Q2: What's the algorithm for part selection from LCSC?

The algorithm in `picker_lib.py`:

```python
def pick_topologically(tree, solver, progress):
    # 1. Pick explicit parts (specified by LCSC ID or MPN)
    _pick_explicit_modules(explicit_modules)

    # 2. For parametric picking
    while tree:
        # 2a. Get candidates matching constraints
        candidates = get_candidates(tree, solver)

        # 2b. Find independent module groups
        groups = find_independent_groups(candidates.keys(), solver)

        # 2c. Pick module with least candidates first (most constrained)
        picked = [
            (min(group, key=lambda m: len(candidates[m])), candidates[m][0])
            for group in groups
        ]

        # 2d. Attach selected parts
        for m, part in picked:
            attach_single_no_check(m, part, solver)
```

Query construction (`_prepare_query`):
1. Extract module parameters (resistance, power, voltage, etc.)
2. Get known superset from solver for each parameter
3. Serialize as `P_Set` intervals
4. Query backend API with serialized constraints

### Q3: How are tolerances propagated through calculations?

Tolerances use **interval arithmetic** via `Quantity_Interval_Disjoint`:

```python
# Creating intervals with tolerances
Quantity_Interval.from_center(center=10.0, abs_tol=0.5)  # 9.5 to 10.5
Quantity_Interval.from_center_rel(center=10.0, rel_tol=0.05)  # 10 +/- 5%

# Operations preserve intervals
interval_a + interval_b  # op_add_intervals (adds ranges)
interval_a * interval_b  # op_mul_intervals (multiplies ranges)
interval_a / interval_b  # op_div_intervals (divides with proper handling)
```

Example from `ResistorVoltageDivider`:
```python
# Equations create constraint relationships
r_bottom.alias_is(v_out * r_top / (v_in - v_out))
v_out.alias_is(v_in * ratio)

# Solver propagates intervals through these relationships
```

Key features:
- Unit-aware operations (prevents ohms + volts)
- Disjoint intervals for division by zero handling
- `is_superset_of()` checks for constraint satisfaction

### Q4: Can we adopt similar constraint-based component selection?

**Yes**, with these key components needed:

1. **Parameter System**
   - Declare parameters with units and domains
   - Support constraint operations (`alias_is`, `constrain_le`, `constrain_subset`)

2. **Interval Sets**
   - Represent tolerances as intervals
   - Support arithmetic operations
   - Serialize for API queries

3. **Solver Integration**
   - Simplify constraint graphs
   - Detect contradictions
   - Deduce parameter bounds

4. **Part Database Integration**
   - Query with parameter intervals
   - Match returned part specs against constraints
   - Attach selected parts with metadata

## Potential Improvements for kicad-tools

### Minimal Implementation

```python
# Simplified constraint-aware component block
class ResistorBlock:
    def __init__(self, sch, ref,
                 resistance: Interval,  # e.g., Interval(9.5e3, 10.5e3)
                 power: Interval = Interval(0, 0.125)):
        # Store constraints
        self.constraints = {
            'resistance': resistance,
            'max_power': power,
        }

    def pick_part(self, db: PartDatabase) -> LCSCPart:
        """Query LCSC for compatible parts"""
        candidates = db.query_resistors(
            resistance=self.constraints['resistance'],
            power=self.constraints['max_power'],
        )
        return candidates[0]  # Return best match
```

### Full Implementation Path

1. **Phase 1: Interval Types**
   - Port `Quantity_Interval_Disjoint` or create simpler version
   - Add unit-aware interval arithmetic

2. **Phase 2: Parameter Constraints**
   - Add `constrain()` method to block parameters
   - Support equations between parameters

3. **Phase 3: LCSC Integration**
   - Query atopile backend API (or implement direct LCSC access)
   - Filter results by constraints

4. **Phase 4: Footprint/Symbol Integration**
   - Use EasyEDA API for KiCad library generation
   - Auto-assign footprints based on package selection

## Example Use Case (from Issue)

```python
# Current kicad-tools approach
ldo = LDOBlock(sch, ref="U1", value="AMS1117-3.3",
               input_cap="10uF", output_caps=["10uF", "100nF"])

# Proposed constraint-based approach
ldo = LDOBlock(sch, ref="U1",
               input_voltage=Interval.from_center_rel(5.0, 0.10),  # 5V +/- 10%
               output_voltage=Interval.from_center_rel(3.3, 0.05), # 3.3V +/- 5%
               output_current=0.5)  # Auto-select LDO and caps

# System would:
# 1. Query LCSC for LDOs matching Vin, Vout, Iout specs
# 2. Select appropriate input/output capacitors based on LDO requirements
# 3. Generate complete circuit with picked parts
```

## Key Files Reference

| File | Purpose |
|------|---------|
| `defaultsolver.py` | Main solver implementation with algorithm pipeline |
| `picker.py` | Part picking orchestration (`pick_part_recursively`) |
| `picker_lib.py` | LCSC API integration and candidate matching |
| `lcsc.py` | EasyEDA/LCSC part attachment (footprint, symbol, 3D) |
| `quantity_sets.py` | Interval arithmetic with units |
| `parameter.py` | Parameter and expression graph nodes |
| `models.py` | API data models for part queries |

## Conclusion

Atopile's constraint solving system is a powerful, production-ready implementation that combines:
- Symbolic constraint solving for complex equation systems
- Interval arithmetic for tolerance propagation
- Cloud-based part database integration (LCSC/EasyEDA)
- Automatic footprint and symbol generation

For kicad-tools, a phased adoption could start with simple interval-based part selection and grow toward full equation-based constraint solving.
