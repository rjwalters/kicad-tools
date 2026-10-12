"""Access-witness replay survives a released routing grid (issue #6346).

``kct route`` calls ``RoutingGrid.release_arrays()`` (via
``_release_routing_engine_state``) *before* it writes the access-witness
sidecars, so the replay runs against zero-size ``(0, 0, 0)`` occupancy planes.
The descriptive raster-labelling helper ``pad_access._marking_at`` used to
index those planes directly and raise ``IndexError``, which dropped the
``witness`` block from every partial-exit sidecar.

Markings only LABEL closing copper -- they never decide legality -- so after a
release the replay must return the same verdicts and attribution, with the
markings simply absent.
"""

from __future__ import annotations

import dataclasses

import pytest

from kicad_tools.router.access_witness import AccessWitness, replay
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.pad_access import _marking_at
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.timeout(180)


def test_marking_at_on_released_grid_returns_empty() -> None:
    rules = DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.2)
    grid = RoutingGrid(5, 5, rules)
    gx, gy = grid.world_to_grid(2.5, 2.5)
    grid._pad_blocked[0, gy, gx] = True
    grid._is_obstacle[0, gy, gx] = True
    grid._usage_count[0, gy, gx] = 1
    live = _marking_at(grid, 2.5, 2.5, 0)
    assert {"pad_blocked", "is_obstacle", "usage_shared"} <= live

    grid.release_arrays()
    assert _marking_at(grid, 2.5, 2.5, 0) == frozenset()
    # Idempotent, and out-of-range points stay label-free rather than raising.
    grid.release_arrays()
    assert _marking_at(grid, 4.9, 4.9, grid.num_layers - 1) == frozenset()


def _without_markings(witness: AccessWitness) -> dict:
    return {pad.pad_key: dataclasses.replace(pad, closing_markings=()) for pad in witness.pads}


def test_replay_after_release_matches_live_replay() -> None:
    # A FRESH router, not the module-cached ``_routed_kelvin()`` from the 5508
    # suite: releasing the grid is destructive and must not leak into other
    # tests that read the cached run.
    from tests.router.test_access_witness_replay_5508 import U3, _route_once

    router = _route_once((3.0, 7.0))
    journal = router.commit_journal

    live = replay(journal, router, pad_keys=[U3])
    live_u3 = live.for_pad(*U3)
    assert live_u3 is not None
    assert live_u3.closing_markings, "live grid must still label closing copper"
    assert live_u3.first_closed_at is not None

    router.grid.release_arrays()
    released = replay(journal, router, pad_keys=[U3])  # used to raise IndexError

    released_u3 = released.for_pad(*U3)
    assert released_u3 is not None
    assert released_u3.closing_markings == ()
    for field in (
        "final_access",
        "access_at_escape_end",
        "first_closed_at",
        "first_closed_index",
        "first_closed_kind",
        "closing_nets",
        "closing_refs",
        "closing_copper_class",
        "reopened",
    ):
        assert getattr(released_u3, field) == getattr(live_u3, field), field
    assert _without_markings(released) == _without_markings(live)
