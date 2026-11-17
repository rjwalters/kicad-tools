"""Portal-capacity model unit tests for mesh-router P2 (#4269).

Validates the ``NavMesh`` capacity/occupancy/congestion primitives against a
hand-computed corridor: capacity is the integer edge-length quotient
``floor(edge_len / channel)`` (P0.5 measured 2/12/64 lanes across a real
corridor), occupancy is a per-portal counter, and the congestion penalty is a
PathFinder present+history term that only bites above the threshold.
"""

from __future__ import annotations

import math

from kicad_tools.router.mesh.navmesh import NavMesh


def _unit_square_mesh(channel: float) -> tuple[NavMesh, tuple[int, int]]:
    """Two triangles sharing the diagonal of a 10 x 10 mm square.

    Vertices: 0=(0,0) 1=(10,0) 2=(10,10) 3=(0,10).  Triangles (0,1,2) and
    (0,2,3) share edge (0,2) -- the diagonal, length 10*sqrt(2) mm.
    """
    verts = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    tris = [(0, 1, 2), (0, 2, 3)]
    nm = NavMesh(verts, tris, channel=channel)
    return nm, (0, 2)  # the shared diagonal portal key (sorted)


def test_capacity_is_floor_edge_len_over_channel() -> None:
    # Diagonal length = 10*sqrt(2) ~= 14.142 mm.  channel = 0.454 mm (the P0.5
    # trace 0.2 + 2*clearance 0.127).  Hand-computed lanes = floor(14.142/0.454)
    # = floor(31.15) = 31.
    channel = 0.2 + 2 * 0.127
    nm, portal = _unit_square_mesh(channel)
    assert math.isclose(nm.edge_length(portal), 10.0 * math.sqrt(2.0), rel_tol=1e-12)
    assert nm.capacity(portal) == int(10.0 * math.sqrt(2.0) / channel)
    assert nm.capacity(portal) == 31


def test_capacity_matches_p05_measured_lanes() -> None:
    # P0.5 spike measured 2 / 12 / 64 lanes at channel 0.454 mm for portals of
    # length ~0.96 / 5.54 / 29.2 mm.  Reproduce each quotient exactly.
    channel = 0.2 + 2 * 0.127
    for edge_len, expected in ((0.96, 2), (5.54, 12), (29.2, 64)):
        verts = [(0.0, 0.0), (edge_len, 0.0), (0.0, 1.0)]
        # portal = the (0,1) edge of length ``edge_len``.
        nm = NavMesh(verts, [(0, 1, 2)], channel=channel)
        assert nm.capacity((0, 1)) == expected


def test_zero_channel_disables_capacity_model() -> None:
    nm, portal = _unit_square_mesh(channel=0.0)
    assert nm.capacity(portal) >= 1_000_000  # effectively unbounded


def test_occupancy_counter_commit_release_reset() -> None:
    nm, portal = _unit_square_mesh(channel=0.454)
    assert nm.occupancy(portal) == 0
    nm.commit_portal(portal)
    nm.commit_portal(portal)
    assert nm.occupancy(portal) == 2
    assert portal in nm.occupied_portals()
    nm.release_portal(portal)
    assert nm.occupancy(portal) == 1
    nm.reset_occupancy()
    assert nm.occupancy(portal) == 0
    assert nm.occupied_portals() == []


def test_congestion_penalty_is_zero_below_threshold() -> None:
    # Capacity 31, threshold 0.3: an empty portal has density 1/31 ~= 0.032,
    # well under threshold -> zero present penalty and zero history.
    nm, portal = _unit_square_mesh(channel=0.454)
    penalty = nm.portal_penalty(
        portal, present_cost_factor=1.0, cost_congestion=2.0, congestion_threshold=0.3
    )
    assert penalty == 0.0


def test_congestion_penalty_grows_when_over_threshold() -> None:
    # Force density over threshold by loading occupancy near capacity.
    nm, portal = _unit_square_mesh(channel=0.454)
    cap = nm.capacity(portal)  # 31
    for _ in range(cap):  # occupancy == capacity -> density = (cap+1)/cap > 1
        nm.commit_portal(portal)
    penalty = nm.portal_penalty(
        portal, present_cost_factor=1.0, cost_congestion=2.0, congestion_threshold=0.3
    )
    density = (cap + 1) / cap
    assert penalty > 0.0
    assert math.isclose(penalty, 2.0 * (density - 0.3), rel_tol=1e-9)


def test_history_accumulates_and_is_added_unscaled() -> None:
    nm, portal = _unit_square_mesh(channel=0.454)
    nm.add_history(portal, 5.0)
    nm.add_history(portal, 3.0)
    # Empty portal below threshold -> present term 0, so penalty == history.
    penalty = nm.portal_penalty(
        portal, present_cost_factor=0.0, cost_congestion=2.0, congestion_threshold=0.3
    )
    assert penalty == 8.0
    nm.reset_occupancy()
    # reset_occupancy must NOT clear history (PathFinder persistence).
    assert nm.history(portal) == 8.0


def test_astar_default_is_byte_identical_to_p1_cost() -> None:
    # With present_cost_factor 0 and no history, the negotiated astar reduces to
    # the P1 portal-midpoint distance: same corridor, unchanged.
    verts = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    tris = [(0, 1, 2), (0, 2, 3)]
    nm_plain = NavMesh(verts, tris)
    nm_cap = NavMesh(verts, tris, channel=0.454)
    start, goal = (1.0, 1.0), (9.0, 9.0)
    assert nm_plain.astar(start, goal) == nm_cap.astar(start, goal)
