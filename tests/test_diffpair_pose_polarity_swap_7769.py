"""A polarity-swapped pair reports a real pose-decline reason (Issue #7769).

The centreline pose trunk has no crossover, so ``route_differential_pair``
never offers a swapped pair to it.  The ``[coupled-select]`` line used to show
the opaque ``pose=declined:n/a`` for that case.
"""

from __future__ import annotations

from kicad_tools.router import diffpair_routing
from kicad_tools.router.diffpair_routing import _POSE_DECLINE_POLARITY_SWAP


def _select_line(pose_decline):
    cls = next(
        c
        for c in vars(diffpair_routing).values()
        if isinstance(c, type) and hasattr(c, "_coupled_select_line")
    )
    return cls._coupled_select_line(None, {}, {}, None, None, pose_decline, 0.0)


def test_swapped_pair_decline_reason_is_not_na():
    line = _select_line(_POSE_DECLINE_POLARITY_SWAP)
    assert "n/a" not in line
    assert "polarity-swap" in line


def test_route_gate_assigns_the_swap_reason():
    import inspect

    src = inspect.getsource(diffpair_routing)
    assert "pose_decline = _POSE_DECLINE_POLARITY_SWAP" in src
