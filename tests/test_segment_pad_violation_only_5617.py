"""``segment_pad_clearance(violation_only=True)`` keeps the exact verdict (Issue #5617).

The pure-Python A* own-pad step gate reads only ``violation_loc``.  In that
mode pads whose bounding circle is provably ``required_clearance`` away from
the segment are skipped before the exact rect/disc distance (the pad loop was
~11 s of pad_segment_edge_clearance plus ~12 s of ``_own_pad_step_clear`` in
the Board 06 re-route).  Pin that the violation location is identical to the
full scan over a randomised corpus covering rotations, circles, through-hole
pads, per-component overrides, fine pitch and same-component carve-outs.
"""

from __future__ import annotations

import random

import pytest

from kicad_tools.router import DesignRules, RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad


def _grid(seed: int, strict: bool) -> tuple[RoutingGrid, dict[str, float]]:
    rng = random.Random(seed)
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        grid_resolution=0.1,
        component_clearances={"U2": 0.08},
        fine_pitch_clearance=0.1,
        strict_pad_clearance=strict,
    )
    grid = RoutingGrid(width=30.0, height=30.0, rules=rules, layer_stack=LayerStack.two_layer())
    pitches = {"U1": 0.5, "U2": 0.4, "J1": 1.27, "R1": 2.0}
    for i in range(120):
        ref = rng.choice(list(pitches))
        grid.add_pad(
            Pad(
                x=rng.uniform(2, 28),
                y=rng.uniform(2, 28),
                width=rng.choice([0.3, 0.6, 1.2, 1.8]),
                height=rng.choice([0.3, 0.6, 1.2]),
                net=rng.choice([0, 1, 2, 3, 4]),
                net_name=rng.choice(["", "SIG", "GND", "+3.3V", "NET"]),
                layer=rng.choice([Layer.F_CU, Layer.B_CU]),
                through_hole=rng.random() < 0.2,
                drill=0.3,
                ref=ref,
                pin=str(i),
                rotation=rng.choice([0.0, 0.0, 90.0, 45.0, 17.5, 270.0]),
                shape=rng.choice(["rect", "rect", "circle"]),
            )
        )
    return grid, pitches


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_violation_only_matches_full_scan(seed: int, strict: bool) -> None:
    grid, pitches = _grid(seed, strict)
    rng = random.Random(seed * 100)
    violations = 0
    for _ in range(1500):
        x1, y1 = rng.uniform(1, 29), rng.uniform(1, 29)
        reach = rng.choice([0.1, 0.3, 1.0, 3.0])
        x2, y2 = x1 + rng.uniform(-reach, reach), y1 + rng.uniform(-reach, reach)
        kwargs: dict = {
            "layer_idx": rng.choice([0, 1]),
            "half_width": rng.choice([0.075, 0.1, 0.25]),
            "exclude_net": rng.choice([0, 1, 2]),
            "min_clearance": 0.15,
            "component_pitches": pitches,
            "exclude_refs": rng.choice([None, {"U1"}, {"U1", "U2", "J1"}]),
        }
        _full_min, full = grid.segment_pad_clearance(x1, y1, x2, y2, **kwargs)
        _fast_min, fast = grid.segment_pad_clearance(x1, y1, x2, y2, violation_only=True, **kwargs)
        assert fast == full
        violations += full is not None
    # Both outcomes must be exercised or the equivalence is vacuous.
    assert 0 < violations < 1500
