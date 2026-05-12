# 02 — Reference Selection

`length_match_reference` decides **which trace's length the rest of the
group must match**. Field declared at `src/kicad_tools/router/rules.py:632`;
accessor `effective_length_match_reference()` at `rules.py:737`.

Reference board: [`boards/07-matchgroup-test`](../../../boards/07-matchgroup-test/)
exercises all three policies.

## Three policies

### 1. `None` — longest-in-group (default)

The longest routed net becomes the reference; every shorter member is
serpentined up to its length. Matches the legacy `tune_match_group`
semantics at `router/optimizer/serpentine.py:438`. Use this when no
member of the group has special timing-budget constraints.

```python
from kicad_tools.router.rules import NetClassRouting

ddr_dq = NetClassRouting(
    name="DDR_DQ",
    length_match_group="DDR_DATA_BYTE_0",
    length_match_reference=None,           # explicit, but the default
    length_match_tolerance_mm=0.1,
)
```

### 2. Explicit net name — "pace-car"

Name a single net. Every other member of the group must match **that
net's** length, even when it isn't the longest. Use this when one
member is hard to perturb — e.g. a DDR strobe whose timing budget
can't absorb the cumulative skew of inserted bulges, or a clock that
must not gain phase delay.

```python
ddr_dq_pace_car = NetClassRouting(
    name="DDR_DQ",
    length_match_group="DDR_DATA_BYTE_0",
    length_match_reference="DQS_P",        # pace-car: DQS_P holds, DQ meanders
    length_match_tolerance_mm=0.1,
)
```

The tuner refuses to perturb the named reference; if a shorter member
cannot reach the pace-car's length within the cascade budget it returns
`reason="exceeded_max_inserts"` and the violation is reported by the
`match_group_length_skew` DRC rule (guide 06).

### 3. `"clock"` sentinel — protocol-aware (forward-compat)

Reserved value for MIPI/HDMI clock-relative matching. Phase 1A accepts
the sentinel; Phase 2/3 resolves it to the lane group's clock pair.
Set this when a future protocol-aware resolver should pick the
reference automatically (e.g. MIPI CSI clock = `CSI_CLK_P/N`).

```python
mipi_csi_data = NetClassRouting(
    name="MIPI_CSI_DATA",
    length_match_group="MIPI_CSI",
    length_match_reference="clock",        # resolver picks the clock pair
    length_match_tolerance_mm=0.05,
)
```

## Precedence

Detection at `match_group_detection.py:418-432` records the policy
verbatim on `MatchGroup.length_match_reference`. `MatchGroupTracker.get_reference_length`
consumes it: explicit name first (must exist in the group's measured
lengths), `None` falls back to the longest, `"clock"` is a no-op in
Phase 1A and the longest wins.

## See also

- [01-declaring-groups.md](01-declaring-groups.md) — declare the group.
- [04-cascade-safety.md](04-cascade-safety.md) — what happens when the
  pace-car policy refuses to be reached.
