"""Regression: mesh routing on a board with no Edge.Cuts (#4268).

``Autorouter._board_bbox`` is only populated from the board outline when the
PCB actually has edge cuts (``router/io.py`` writes it from the extracted
Edge.Cuts bbox).  A board routed under ``--route-engine mesh`` with no edge
cuts therefore leaves ``_board_bbox`` at its ``None`` default.

Before the fix, ``_route_net_mesh`` unpacked ``self._board_bbox`` with no
None guard (``bx0, by0, bx1, by1 = self._board_bbox``), so the None default
crashed with "cannot unpack non-iterable NoneType".  The guard now falls back
to the grid origin/dimensions (mirroring the guarded reader in
``cleanup_artifacts``) so the navmesh still gets a valid outer boundary.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad

# The mesh pathfinder routes through the compiled C++ backend; skip where the
# extension is not built (matches the other tests/router/mesh acceptance runs).
pytest.importorskip("kicad_tools.router.router_cpp")


def _add_pad(router: Autorouter, pad: Pad) -> None:
    key = (pad.ref, pad.pin)
    router.pads[key] = pad
    router.nets.setdefault(pad.net, []).append(key)


def test_route_net_mesh_no_edge_cuts_uses_grid_bbox_fallback() -> None:
    """No Edge.Cuts (``_board_bbox is None``) must route, not crash."""
    router = Autorouter(50, 40, strategy="mesh")
    # Precondition: a bare board (no io.py edge-cuts population) leaves the
    # bbox at its None default -- the exact state that used to crash.
    assert router._board_bbox is None

    _add_pad(
        router,
        Pad(
            x=10, y=10, width=1, height=1, net=1, net_name="N1", layer=Layer.F_CU, ref="R1", pin="1"
        ),
    )
    _add_pad(
        router,
        Pad(
            x=30, y=25, width=1, height=1, net=1, net_name="N1", layer=Layer.F_CU, ref="R2", pin="1"
        ),
    )

    # Must not raise "cannot unpack non-iterable NoneType".
    routes = router._route_net_mesh(1)

    # The pathfinder was built with a grid-derived outer boundary and produced
    # a single-net route for the two same-layer pads.
    assert router._mesh_pathfinder is not None
    assert len(routes) == 1
    assert routes[0].segments
