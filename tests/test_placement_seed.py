"""Tests for the initial placement heuristic (seed) module.

Covers both force-directed placement and random placement with overlap
resolution, verifying the acceptance criteria from issue #1207:
- Force-directed produces zero-overlap results on simple boards
- Components sharing many nets are placed closer together
- Random placement with overlap resolution produces zero-overlap results
- Both methods respect board boundary constraints
- Force-directed seed scores better than random seed on HPWL metric
- Completes in <1s for 20-component boards
"""

from __future__ import annotations

import math
import time

import numpy as np

from kicad_tools.placement.cost import (
    BoardOutline,
    ComponentPlacement,
    Net,
    compute_overlap,
    compute_wirelength,
)
from kicad_tools.placement.seed import (
    _build_net_adjacency,
    force_directed_placement,
    random_placement,
)
from kicad_tools.placement.vector import (
    FIELDS_PER_COMPONENT,
    ComponentDef,
    PlacementVector,
    decode,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_board(width: float = 100.0, height: float = 100.0) -> BoardOutline:
    """Create a board outline centred at the origin."""
    return BoardOutline(
        min_x=-width / 2,
        min_y=-height / 2,
        max_x=width / 2,
        max_y=height / 2,
    )


def _make_components(n: int, size: float = 2.0) -> list[ComponentDef]:
    """Create *n* identical square components."""
    return [ComponentDef(reference=f"U{i + 1}", width=size, height=size) for i in range(n)]


def _vector_to_component_placements(
    vec: PlacementVector, components: list[ComponentDef]
) -> list[ComponentPlacement]:
    """Convert PlacementVector into ComponentPlacement list for cost functions."""
    placements = decode(vec, components)
    return [
        ComponentPlacement(reference=p.reference, x=p.x, y=p.y, rotation=p.rotation)
        for p in placements
    ]


def _footprint_sizes(
    components: list[ComponentDef],
) -> dict[str, tuple[float, float]]:
    """Build footprint size map from component defs."""
    return {c.reference: (c.width, c.height) for c in components}


def _check_no_overlap(
    vec: PlacementVector,
    components: list[ComponentDef],
) -> float:
    """Return total overlap area for the given placement."""
    cp = _vector_to_component_placements(vec, components)
    sizes = _footprint_sizes(components)
    return compute_overlap(cp, sizes)


def _check_within_bounds(
    vec: PlacementVector,
    components: list[ComponentDef],
    board: BoardOutline,
) -> bool:
    """Check that all components are within the board outline."""
    placements = decode(vec, components)
    for i, p in enumerate(placements):
        hw = components[i].width / 2.0
        hh = components[i].height / 2.0
        if p.x - hw < board.min_x - 1e-9:
            return False
        if p.x + hw > board.max_x + 1e-9:
            return False
        if p.y - hh < board.min_y - 1e-9:
            return False
        if p.y + hh > board.max_y + 1e-9:
            return False
    return True


# ---------------------------------------------------------------------------
# Tests: _build_net_adjacency
# ---------------------------------------------------------------------------


class TestBuildNetAdjacency:
    def test_empty_nets(self) -> None:
        components = _make_components(3)
        adj = _build_net_adjacency(components, [])
        assert adj.shape == (3, 3)
        assert np.all(adj == 0)

    def test_single_net(self) -> None:
        components = _make_components(3)
        nets = [Net(name="VCC", pins=[("U1", "1"), ("U3", "1")])]
        adj = _build_net_adjacency(components, nets)
        assert adj[0, 2] == 1.0
        assert adj[2, 0] == 1.0
        assert adj[0, 1] == 0.0

    def test_multiple_shared_nets(self) -> None:
        components = _make_components(3)
        nets = [
            Net(name="N1", pins=[("U1", "1"), ("U2", "1")]),
            Net(name="N2", pins=[("U1", "2"), ("U2", "2")]),
            Net(name="N3", pins=[("U1", "3"), ("U2", "3")]),
        ]
        adj = _build_net_adjacency(components, nets)
        # U1-U2 share 3 nets
        assert adj[0, 1] == 3.0
        assert adj[1, 0] == 3.0
        # U3 is disconnected
        assert adj[0, 2] == 0.0
        assert adj[2, 1] == 0.0

    def test_unknown_ref_ignored(self) -> None:
        components = _make_components(2)
        nets = [Net(name="N1", pins=[("U1", "1"), ("UNKNOWN", "1")])]
        adj = _build_net_adjacency(components, nets)
        # UNKNOWN is not in components, so U1 has no in-component connection
        assert adj[0, 1] == 0.0


# ---------------------------------------------------------------------------
# Tests: force_directed_placement
# ---------------------------------------------------------------------------


class TestForceDirectedPlacement:
    def test_empty_components(self) -> None:
        board = _make_board()
        result = force_directed_placement([], [], board)
        assert isinstance(result, PlacementVector)
        assert len(result.data) == 0

    def test_single_component(self) -> None:
        board = _make_board()
        components = _make_components(1)
        result = force_directed_placement(components, [], board)
        assert result.num_components == 1
        # Should be within bounds
        assert _check_within_bounds(result, components, board)

    def test_two_components_no_nets_repel(self) -> None:
        """Two disconnected components should be pushed apart by repulsion."""
        board = _make_board(50, 50)
        components = _make_components(2, size=2.0)
        result = force_directed_placement(components, [], board)
        placements = decode(result, components)
        dist = math.sqrt(
            (placements[0].x - placements[1].x) ** 2 + (placements[0].y - placements[1].y) ** 2
        )
        # They should be separated (not on top of each other)
        assert dist > 1.0

    def test_connected_components_closer_than_disconnected(self) -> None:
        """Components sharing nets should end up closer than disconnected ones."""
        board = _make_board(100, 100)
        # U1, U2, U3: U1 and U2 share 5 nets, U3 is disconnected
        components = _make_components(3, size=2.0)
        nets = [Net(name=f"N{i}", pins=[("U1", str(i)), ("U2", str(i))]) for i in range(5)]
        result = force_directed_placement(components, nets, board)
        placements = decode(result, components)

        dist_12 = math.sqrt(
            (placements[0].x - placements[1].x) ** 2 + (placements[0].y - placements[1].y) ** 2
        )
        dist_13 = math.sqrt(
            (placements[0].x - placements[2].x) ** 2 + (placements[0].y - placements[2].y) ** 2
        )
        # Connected pair (U1-U2) should be closer than disconnected pair (U1-U3)
        assert dist_12 < dist_13

    def test_zero_overlap_simple_board(self) -> None:
        """Force-directed placement should produce zero overlap on a simple board."""
        board = _make_board(100, 100)
        components = _make_components(5, size=3.0)
        nets = [
            Net(name="N1", pins=[("U1", "1"), ("U2", "1")]),
            Net(name="N2", pins=[("U2", "1"), ("U3", "1")]),
        ]
        result = force_directed_placement(components, nets, board)
        overlap = _check_no_overlap(result, components)
        assert overlap == 0.0, f"Expected zero overlap, got {overlap}"

    def test_boundary_constraints(self) -> None:
        """All components should stay within board bounds."""
        board = _make_board(40, 40)
        components = _make_components(8, size=3.0)
        nets = [
            Net(name="N1", pins=[("U1", "1"), ("U5", "1")]),
            Net(name="N2", pins=[("U3", "1"), ("U7", "1")]),
        ]
        result = force_directed_placement(components, nets, board)
        assert _check_within_bounds(result, components, board)

    def test_rotation_and_side_defaults(self) -> None:
        """Seed placement should use rotation=0, side=0 for all components."""
        board = _make_board()
        components = _make_components(3)
        result = force_directed_placement(components, [], board)
        for i in range(3):
            vals = result.component_slice(i)
            assert vals[2] == 0.0, "rotation should be 0"
            assert vals[3] == 0.0, "side should be 0 (front)"

    def test_performance_20_components(self) -> None:
        """Force-directed placement should complete in <1s for 20 components."""
        board = _make_board(200, 200)
        components = _make_components(20, size=5.0)
        # Create a moderate connectivity (each component connected to 2 neighbours)
        nets = []
        for i in range(19):
            nets.append(
                Net(
                    name=f"N{i}",
                    pins=[
                        (f"U{i + 1}", "1"),
                        (f"U{i + 2}", "1"),
                    ],
                )
            )
        start = time.monotonic()
        result = force_directed_placement(components, nets, board)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"Took {elapsed:.2f}s, expected <1s"
        assert result.num_components == 20

    def test_many_shared_nets_cluster(self) -> None:
        """Components sharing many nets should cluster more tightly."""
        board = _make_board(200, 200)
        # Group A: U1, U2, U3 share 10 nets each pair
        # Group B: U4, U5, U6 share 1 net each pair
        components = _make_components(6, size=3.0)

        nets_a = [
            Net(
                name=f"GA{i}",
                pins=[("U1", str(i)), ("U2", str(i)), ("U3", str(i))],
            )
            for i in range(10)
        ]
        nets_b = [
            Net(name="GB1", pins=[("U4", "1"), ("U5", "1"), ("U6", "1")]),
        ]
        result = force_directed_placement(components, nets_a + nets_b, board)
        placements = decode(result, components)

        # Compute average pairwise distance within group A
        group_a_dists = []
        for i in range(3):
            for j in range(i + 1, 3):
                d = math.sqrt(
                    (placements[i].x - placements[j].x) ** 2
                    + (placements[i].y - placements[j].y) ** 2
                )
                group_a_dists.append(d)

        # Group B average distance
        group_b_dists = []
        for i in range(3, 6):
            for j in range(i + 1, 6):
                d = math.sqrt(
                    (placements[i].x - placements[j].x) ** 2
                    + (placements[i].y - placements[j].y) ** 2
                )
                group_b_dists.append(d)

        avg_a = sum(group_a_dists) / len(group_a_dists)
        avg_b = sum(group_b_dists) / len(group_b_dists)

        # Group A (10 shared nets) should be tighter than Group B (1 shared net)
        assert avg_a < avg_b, f"Group A avg dist ({avg_a:.2f}) should be < Group B ({avg_b:.2f})"


# ---------------------------------------------------------------------------
# Tests: random_placement
# ---------------------------------------------------------------------------


class TestRandomPlacement:
    def test_empty_components(self) -> None:
        board = _make_board()
        result = random_placement([], board)
        assert isinstance(result, PlacementVector)
        assert len(result.data) == 0

    def test_single_component(self) -> None:
        board = _make_board()
        components = _make_components(1)
        result = random_placement(components, board, seed=42)
        assert result.num_components == 1
        assert _check_within_bounds(result, components, board)

    def test_zero_overlap_after_resolution(self) -> None:
        """Random placement with overlap resolution should produce zero overlap."""
        board = _make_board(60, 60)
        components = _make_components(10, size=3.0)
        result = random_placement(components, board, seed=99)
        overlap = _check_no_overlap(result, components)
        assert overlap == 0.0, f"Expected zero overlap, got {overlap}"

    def test_boundary_constraints(self) -> None:
        """All components should be within board bounds after placement."""
        board = _make_board(50, 50)
        components = _make_components(8, size=4.0)
        result = random_placement(components, board, seed=7)
        assert _check_within_bounds(result, components, board)

    def test_deterministic_with_seed(self) -> None:
        """Same seed should produce same result."""
        board = _make_board()
        components = _make_components(5)
        r1 = random_placement(components, board, seed=123)
        r2 = random_placement(components, board, seed=123)
        assert np.array_equal(r1.data, r2.data)

    def test_different_seeds_different_results(self) -> None:
        """Different seeds should produce different results."""
        board = _make_board()
        components = _make_components(5)
        r1 = random_placement(components, board, seed=1)
        r2 = random_placement(components, board, seed=2)
        assert not np.array_equal(r1.data, r2.data)

    def test_rotation_and_side_defaults(self) -> None:
        """Random placement should use rotation=0, side=0."""
        board = _make_board()
        components = _make_components(3)
        result = random_placement(components, board, seed=42)
        for i in range(3):
            vals = result.component_slice(i)
            assert vals[2] == 0.0
            assert vals[3] == 0.0

    def test_component_larger_than_board(self) -> None:
        """Component wider than board should be centred."""
        board = _make_board(5, 5)  # tiny board
        components = [ComponentDef(reference="U1", width=10.0, height=10.0)]
        result = random_placement(components, board, seed=42)
        vals = result.component_slice(0)
        # Should be at board centre
        assert abs(vals[0] - 0.0) < 1e-9
        assert abs(vals[1] - 0.0) < 1e-9


# ---------------------------------------------------------------------------
# Tests: force-directed beats random on HPWL
# ---------------------------------------------------------------------------


class TestForceDirectedBeatsRandom:
    def test_hpwl_improvement(self) -> None:
        """Force-directed seed should score better (lower) on HPWL than random."""
        board = _make_board(100, 100)
        components = _make_components(10, size=3.0)

        # Create rich connectivity
        nets = [
            Net(name="N1", pins=[("U1", "1"), ("U2", "1"), ("U3", "1")]),
            Net(name="N2", pins=[("U2", "1"), ("U4", "1")]),
            Net(name="N3", pins=[("U3", "1"), ("U5", "1"), ("U6", "1")]),
            Net(name="N4", pins=[("U5", "1"), ("U7", "1"), ("U8", "1")]),
            Net(name="N5", pins=[("U7", "1"), ("U9", "1")]),
            Net(name="N6", pins=[("U8", "1"), ("U10", "1")]),
            Net(name="N7", pins=[("U1", "2"), ("U10", "2")]),
        ]

        fd_vec = force_directed_placement(components, nets, board)
        fd_placements = _vector_to_component_placements(fd_vec, components)
        fd_hpwl = compute_wirelength(fd_placements, nets)

        # Average HPWL over several random seeds
        random_hpwls = []
        for seed in range(10):
            rand_vec = random_placement(components, board, seed=seed)
            rand_placements = _vector_to_component_placements(rand_vec, components)
            random_hpwls.append(compute_wirelength(rand_placements, nets))

        avg_random_hpwl = sum(random_hpwls) / len(random_hpwls)

        assert fd_hpwl < avg_random_hpwl, (
            f"Force-directed HPWL ({fd_hpwl:.2f}) should be less than "
            f"average random HPWL ({avg_random_hpwl:.2f})"
        )


# ---------------------------------------------------------------------------
# Tests: Vector structure
# ---------------------------------------------------------------------------


class TestVectorStructure:
    def test_vector_length(self) -> None:
        """Output vector should have length 4*N."""
        board = _make_board()
        components = _make_components(7)
        result = force_directed_placement(components, [], board)
        assert len(result.data) == 7 * FIELDS_PER_COMPONENT

    def test_decode_roundtrip(self) -> None:
        """Should be decodable back to PlacedComponent list."""
        board = _make_board()
        components = _make_components(4)
        nets = [Net(name="N1", pins=[("U1", "1"), ("U3", "1")])]
        result = force_directed_placement(components, nets, board)
        placed = decode(result, components)
        assert len(placed) == 4
        for p in placed:
            assert p.rotation == 0.0
            assert p.side == 0
