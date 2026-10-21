"""The grid's authored-copper index is built once per copper state (Issue #7716).

On a board whose authored minima are all item-level (no per-net floors), the
cache key used ``id(<fresh {}>)`` and so missed on every call: the Python
fallback A* rebuilt the whole index per expansion step (~0.1 s each on board 07).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kicad_tools.router.authored_clearance import grid_authored_index
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad


def _item_floor_grid(floors):
    pad = Pad(
        x=1.0,
        y=1.0,
        width=0.5,
        height=0.5,
        net=1,
        net_name="A",
        ref="U1",
        pin="1",
        layer=Layer.F_CU,
        authored_clearance=0.3,
    )
    return SimpleNamespace(
        rules=SimpleNamespace(net_clearance_floors=floors),
        _pads=[pad],
        routes=[],
        _authored_item_floors=True,
    )


def _second_lookup_is_cached(grid) -> bool:
    first = grid_authored_index(grid)
    assert first is not None
    # A freed ``{}`` can be re-allocated at the same address, which would hide
    # an ``id()``-keyed miss; keep live allocations around the second lookup.
    ballast = [{} for _ in range(64)]
    try:
        return grid_authored_index(grid) is first
    finally:
        del ballast


@pytest.mark.parametrize("floors", [{}, None])
def test_empty_floors_do_not_rebuild_index_per_call(floors):
    assert _second_lookup_is_cached(_item_floor_grid(floors))
