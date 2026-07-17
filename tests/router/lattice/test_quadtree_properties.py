"""Property tests for the octilinear lattice substrate (issue #4278).

These run BEFORE any routing test (the issue's mandated order): the
<=1-level balance invariant and octilinearity-across-T-junctions are
load-bearing for everything downstream -- a bug here emits off-angle or
overlapping copper, so the properties are pinned first.
"""

from __future__ import annotations

import math

from kicad_tools.router.lattice.geometry import dist, segs_intersect
from kicad_tools.router.lattice.obstacles import LatticeObstacleModel
from kicad_tools.router.lattice.quadtree import OctilinearLattice, RefineRegion
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.quantize import is_45_aligned

_SIDES = ("E", "W", "N", "S")


def _lattice_with_t_junctions() -> OctilinearLattice:
    """A lattice whose refine regions force multi-level T-junction boundaries."""
    bbox = (0.0, 0.0, 25.6, 19.2)
    regions = [
        RefineRegion((4.0, 4.0, 7.0, 7.0), 0.4),  # 3 levels below coarse
        RefineRegion((15.0, 9.0, 18.0, 12.0), 0.8),  # 2 levels
        RefineRegion((20.0, 2.0, 22.0, 4.0), 1.6),  # 1 level
    ]
    return OctilinearLattice(bbox, regions, coarse=3.2)


# ---------------------------------------------------------------------------
# Property 1: every lattice edge is octilinear, INCLUDING across refinement
# boundaries / T-junctions.
# ---------------------------------------------------------------------------


def test_every_edge_is_octilinear_including_t_junctions() -> None:
    lattice = _lattice_with_t_junctions()
    assert len(lattice.leaves) > 60, "refinement did not happen"
    # Sanity: refinement produced more than one level (T-junctions exist).
    levels = {leaf[0] for leaf in lattice.leaves}
    assert len(levels) >= 3

    assert lattice.edges, "empty lattice"
    for k1, k2 in lattice.edges:
        a = lattice.node_point(k1)
        b = lattice.node_point(k2)
        dx, dy = b[0] - a[0], b[1] - a[1]
        assert is_45_aligned(dx, dy), f"off-angle edge {a} -> {b}"


def test_edges_are_axis_or_exact_diagonal_in_key_space() -> None:
    """Integer-key check (no float tolerance): |dx| == |dy| or axis-aligned."""
    lattice = _lattice_with_t_junctions()
    for k1, k2 in lattice.edges:
        dx = abs(k2[0] - k1[0])
        dy = abs(k2[1] - k1[1])
        assert dx == 0 or dy == 0 or dx == dy, f"non-octilinear key delta {k1} -> {k2}"


# ---------------------------------------------------------------------------
# Property 2: balanced refinement -- adjacent leaves differ by <= 1 level.
# ---------------------------------------------------------------------------


def test_balance_invariant_adjacent_leaves_differ_at_most_one_level() -> None:
    lattice = _lattice_with_t_junctions()
    for level, i, j in lattice.leaves:
        for direction in _SIDES:
            neighbor_level = lattice.neighbor_max_level(level, i, j, direction)
            assert neighbor_level <= level + 1, (
                f"balance violated: leaf ({level},{i},{j}) has {direction} "
                f"neighbor at level {neighbor_level}"
            )


# ---------------------------------------------------------------------------
# Property 3: planarity -- edges meet only at shared nodes.
# ---------------------------------------------------------------------------


def test_edges_intersect_only_at_shared_nodes() -> None:
    # Small lattice (one refined pocket) so the O(E^2) sweep stays cheap.
    bbox = (0.0, 0.0, 9.6, 9.6)
    lattice = OctilinearLattice(bbox, [RefineRegion((3.0, 3.0, 5.0, 5.0), 0.8)], coarse=3.2)
    edges = [(lattice.node_point(k1), lattice.node_point(k2), k1, k2) for k1, k2 in lattice.edges]
    assert 50 < len(edges) < 2500
    for x in range(len(edges)):
        a, b, ka, kb = edges[x]
        for y in range(x + 1, len(edges)):
            c, d, kc, kd = edges[y]
            if {ka, kb} & {kc, kd}:
                continue  # shared node: touching there is legal
            assert not segs_intersect(a, b, c, d), f"edges cross: {a}-{b} x {c}-{d}"
            # Collinear-overlap guard: no interior point of one edge may lie
            # on the other (proper crossings are caught above).
            for p in (c, d):
                assert not _interior_point_on_segment(a, b, p), (
                    f"edge endpoint {p} lies inside edge {a}-{b}"
                )
            for p in (a, b):
                assert not _interior_point_on_segment(c, d, p), (
                    f"edge endpoint {p} lies inside edge {c}-{d}"
                )


def _interior_point_on_segment(a, b, p) -> bool:
    if dist(a, p) < 1e-9 or dist(b, p) < 1e-9:
        return False
    return abs(dist(a, p) + dist(p, b) - dist(a, b)) < 1e-9


# ---------------------------------------------------------------------------
# Property 4: node coordinates are exact multiples of the lattice unit.
# ---------------------------------------------------------------------------


def test_node_coordinates_are_quantized_to_unit() -> None:
    lattice = _lattice_with_t_junctions()
    for key, point in lattice.nodes.items():
        expected = lattice.node_point(key)
        assert point == expected
        rx = (point[0] - lattice.origin[0]) / lattice.unit
        ry = (point[1] - lattice.origin[1]) / lattice.unit
        assert abs(rx - round(rx)) < 1e-9
        assert abs(ry - round(ry)) < 1e-9


# ---------------------------------------------------------------------------
# Property 5: per-layer masking.
# ---------------------------------------------------------------------------


def _pad(x: float, y: float, net: int, *, layer: Layer = Layer.F_CU, through: bool = False) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=1.0,
        height=1.0,
        net=net,
        net_name=f"N{net}",
        layer=layer,
        ref=f"P{net}",
        pin="1",
        through_hole=through,
        drill=0.3 if through else 0.0,
    )


def _masked_model(
    pads: list[Pad], num_layers: int = 2
) -> tuple[OctilinearLattice, LatticeObstacleModel]:
    bbox = (0.0, 0.0, 12.8, 12.8)
    regions = [RefineRegion((p.x - 1.3, p.y - 1.3, p.x + 1.3, p.y + 1.3), 0.4) for p in pads]
    lattice = OctilinearLattice(bbox, regions, coarse=3.2)
    stack = LayerStack.two_layer() if num_layers == 2 else LayerStack.four_layer_all_signal()

    def indices(pad: Pad) -> tuple[int, ...]:
        if pad.through_hole:
            return tuple(range(num_layers))
        return (stack.layer_enum_to_index(pad.layer),)

    model = LatticeObstacleModel(
        lattice, pads, [indices(p) for p in pads], num_layers, agent_radius=0.3
    )
    return lattice, model


def test_smd_pad_masks_only_its_own_layer() -> None:
    pad = _pad(6.4, 6.4, net=7, layer=Layer.F_CU)
    lattice, model = _masked_model([pad])
    inside = [
        key
        for key, point in lattice.nodes.items()
        if abs(point[0] - pad.x) <= 0.5 + 0.3 - 1e-9 and abs(point[1] - pad.y) <= 0.5 + 0.3 - 1e-9
    ]
    assert inside, "no lattice node landed inside the pad keep-out"
    for key in inside:
        # Blocked for another net on the pad's layer (F.Cu = index 0)...
        assert model.node_blocked(key, 0, net=99)
        # ... present (unmasked) on the other layer ...
        assert not model.node_blocked(key, 1, net=99)
        # ... and never an obstacle to its OWN net.
        assert not model.node_blocked(key, 0, net=7)


def test_through_hole_pad_masks_every_layer() -> None:
    pad = _pad(6.4, 6.4, net=7, through=True)
    lattice, model = _masked_model([pad], num_layers=4)
    inside = [
        key
        for key, point in lattice.nodes.items()
        if abs(point[0] - pad.x) <= 0.5 + 0.3 - 1e-9 and abs(point[1] - pad.y) <= 0.5 + 0.3 - 1e-9
    ]
    assert inside
    for key in inside:
        for layer in range(4):
            assert model.node_blocked(key, layer, net=99)


def test_edge_crossing_pad_keepout_is_masked_per_layer() -> None:
    pad = _pad(6.4, 6.4, net=7, layer=Layer.B_CU)
    lattice, model = _masked_model([pad])
    crossing = [
        edge for edge in lattice.edges if edge in model.edge_pads[1] or edge in model.edge_pads[0]
    ]
    assert crossing, "no lattice edge crosses the pad keep-out"
    b_idx = LayerStack.two_layer().layer_enum_to_index(Layer.B_CU)
    assert b_idx == 1
    blocked_on_b = [e for e in crossing if model.edge_blocked(e, 1, net=99)]
    assert blocked_on_b
    for edge in blocked_on_b:
        assert not model.edge_blocked(edge, 0, net=99), "SMD B.Cu pad leaked onto F.Cu"


# ---------------------------------------------------------------------------
# Property 6: refinement actually responds to RefineRegion density requests.
# ---------------------------------------------------------------------------


def test_refine_regions_densify_locally_not_globally() -> None:
    bbox = (0.0, 0.0, 25.6, 25.6)
    plain = OctilinearLattice(bbox, [], coarse=3.2)
    refined = OctilinearLattice(bbox, [RefineRegion((10.0, 10.0, 12.0, 12.0), 0.4)], coarse=3.2)
    assert len(refined.nodes) > len(plain.nodes)
    # Far-away coarse cells are untouched: level-0 leaves survive far from
    # the refine region.
    coarse_leaves = [leaf for leaf in refined.leaves if leaf[0] == 0]
    assert coarse_leaves, "refinement leaked into open space"
    # Memory estimate scales with layers.
    n2, m2, by2 = refined.memory_estimate(2)
    n4, m4, by4 = refined.memory_estimate(4)
    assert (n2, m2) == (n4, m4)
    assert math.isclose(by4, 2 * by2)
