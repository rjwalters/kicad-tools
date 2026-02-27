"""Tests for netlist graph analysis and placement priors.

Covers the acceptance criteria from issue #1211:
- Affinity graph correctly computed from netlist
- Clusters match intuitive component groupings (e.g., USB components cluster)
- Power domain detection identifies distinct power rails
- Proximity prior produces placements where high-affinity components are nearby
- Prior-seeded optimisation converges faster than random-seeded (proxy: lower HPWL)
"""

from __future__ import annotations

import math
import time

import numpy as np

from kicad_tools.placement.cost import (
    BoardOutline,
    ComponentPlacement,
    Net,
    compute_wirelength,
)
from kicad_tools.placement.priors import (
    build_affinity_graph,
    detect_power_domains,
    detect_signal_flow,
    find_clusters,
    power_domain_clustering,
    prior_mean_position,
    schematic_proximity_prior,
)
from kicad_tools.placement.seed import random_placement
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


def _make_components(n: int, size: float = 2.0, prefix: str = "U") -> list[ComponentDef]:
    """Create *n* identical square components."""
    return [ComponentDef(reference=f"{prefix}{i + 1}", width=size, height=size) for i in range(n)]


def _distance(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Euclidean distance between two (x, y) points."""
    return math.sqrt((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2)


def _placement_positions(
    vec: PlacementVector, components: list[ComponentDef]
) -> dict[str, tuple[float, float]]:
    """Extract (x, y) positions from a PlacementVector by reference."""
    placed = decode(vec, components)
    return {p.reference: (p.x, p.y) for p in placed}


# ---------------------------------------------------------------------------
# Tests: AffinityGraph construction
# ---------------------------------------------------------------------------


class TestBuildAffinityGraph:
    def test_empty_inputs(self) -> None:
        graph = build_affinity_graph([], [])
        assert graph.num_components == 0
        assert graph.weights.shape == (0, 0)

    def test_no_nets(self) -> None:
        components = _make_components(3)
        graph = build_affinity_graph(components, [])
        assert graph.num_components == 3
        assert np.all(graph.weights == 0)

    def test_single_net_two_components(self) -> None:
        components = _make_components(3)
        nets = [Net(name="SIG1", pins=[("U1", "1"), ("U3", "2")])]
        graph = build_affinity_graph(components, nets)

        assert graph.weight("U1", "U3") == 1.0
        assert graph.weight("U3", "U1") == 1.0  # symmetric
        assert graph.weight("U1", "U2") == 0.0  # not connected
        assert graph.weight("U2", "U3") == 0.0

    def test_multiple_shared_nets(self) -> None:
        components = _make_components(2)
        nets = [
            Net(name="N1", pins=[("U1", "1"), ("U2", "1")]),
            Net(name="N2", pins=[("U1", "2"), ("U2", "2")]),
            Net(name="N3", pins=[("U1", "3"), ("U2", "3")]),
        ]
        graph = build_affinity_graph(components, nets)
        assert graph.weight("U1", "U2") == 3.0

    def test_multi_pin_net(self) -> None:
        """A net with 3 pins creates edges between all 3 pairs."""
        components = _make_components(3)
        nets = [Net(name="BUS", pins=[("U1", "1"), ("U2", "1"), ("U3", "1")])]
        graph = build_affinity_graph(components, nets)
        assert graph.weight("U1", "U2") == 1.0
        assert graph.weight("U1", "U3") == 1.0
        assert graph.weight("U2", "U3") == 1.0

    def test_unknown_ref_ignored(self) -> None:
        components = _make_components(2)
        nets = [Net(name="N1", pins=[("U1", "1"), ("UNKNOWN", "1")])]
        graph = build_affinity_graph(components, nets)
        assert graph.weight("U1", "U2") == 0.0

    def test_exclude_power_nets(self) -> None:
        components = _make_components(2)
        nets = [
            Net(name="VCC", pins=[("U1", "1"), ("U2", "1")]),
            Net(name="GND", pins=[("U1", "2"), ("U2", "2")]),
            Net(name="SIG", pins=[("U1", "3"), ("U2", "3")]),
        ]
        graph_all = build_affinity_graph(components, nets, exclude_power_nets=False)
        graph_sig = build_affinity_graph(components, nets, exclude_power_nets=True)

        assert graph_all.weight("U1", "U2") == 3.0
        assert graph_sig.weight("U1", "U2") == 1.0  # only SIG

    def test_references_tuple(self) -> None:
        components = _make_components(3)
        graph = build_affinity_graph(components, [])
        assert graph.references == ("U1", "U2", "U3")

    def test_weight_nonexistent_ref(self) -> None:
        """Querying weight for unknown refs returns 0."""
        components = _make_components(2)
        graph = build_affinity_graph(components, [])
        assert graph.weight("U1", "NONEXISTENT") == 0.0
        assert graph.weight("NONEXISTENT", "U1") == 0.0


# ---------------------------------------------------------------------------
# Tests: Cluster detection
# ---------------------------------------------------------------------------


class TestFindClusters:
    def test_empty_graph(self) -> None:
        graph = build_affinity_graph([], [])
        clusters = find_clusters(graph)
        assert clusters == []

    def test_all_isolated(self) -> None:
        """Components with no connections form singleton clusters."""
        components = _make_components(3)
        graph = build_affinity_graph(components, [])
        clusters = find_clusters(graph)
        assert len(clusters) == 3
        for c in clusters:
            assert len(c.references) == 1

    def test_two_clusters(self) -> None:
        """Two groups connected internally but not to each other."""
        components = _make_components(4)
        nets = [
            Net(name="N1", pins=[("U1", "1"), ("U2", "1")]),  # group 1
            Net(name="N2", pins=[("U3", "1"), ("U4", "1")]),  # group 2
        ]
        graph = build_affinity_graph(components, nets)
        clusters = find_clusters(graph)
        assert len(clusters) == 2

        # Verify groups
        refs_by_cluster = [set(c.references) for c in clusters]
        assert {"U1", "U2"} in refs_by_cluster
        assert {"U3", "U4"} in refs_by_cluster

    def test_usb_components_cluster(self) -> None:
        """USB components connected by shared nets should cluster together."""
        usb_comps = [
            ComponentDef(reference="U1"),  # USB controller
            ComponentDef(reference="R1"),  # USB D+ pullup
            ComponentDef(reference="R2"),  # USB D- pullup
            ComponentDef(reference="J1"),  # USB connector
        ]
        other_comps = [
            ComponentDef(reference="U2"),  # MCU
            ComponentDef(reference="C1"),  # Decoupling cap
        ]
        all_comps = usb_comps + other_comps

        nets = [
            Net(name="USB_DP", pins=[("U1", "1"), ("R1", "1"), ("J1", "1")]),
            Net(name="USB_DM", pins=[("U1", "2"), ("R2", "1"), ("J1", "2")]),
            Net(name="USB_VBUS", pins=[("U1", "3"), ("J1", "3")]),
            # MCU connected only to U2-C1
            Net(name="MCU_VCC", pins=[("U2", "1"), ("C1", "1")]),
        ]
        graph = build_affinity_graph(all_comps, nets)
        clusters = find_clusters(graph)

        # USB group should contain U1, R1, R2, J1
        usb_refs = {"U1", "R1", "R2", "J1"}
        found_usb = False
        for c in clusters:
            if usb_refs.issubset(set(c.references)):
                found_usb = True
                break
        assert found_usb, (
            f"USB components should cluster together, got {[c.references for c in clusters]}"
        )

    def test_min_affinity_threshold(self) -> None:
        """Higher min_affinity splits weakly-connected components."""
        components = _make_components(3)
        nets = [
            # U1-U2 share 5 nets (strong connection)
            *[Net(name=f"STRONG{i}", pins=[("U1", str(i)), ("U2", str(i))]) for i in range(5)],
            # U2-U3 share 1 net (weak connection)
            Net(name="WEAK", pins=[("U2", "w"), ("U3", "w")]),
        ]
        graph = build_affinity_graph(components, nets)

        # Low threshold: all in one cluster
        clusters_low = find_clusters(graph, min_affinity=1.0)
        assert len(clusters_low) == 1

        # High threshold: U3 splits off
        clusters_high = find_clusters(graph, min_affinity=3.0)
        assert len(clusters_high) == 2

    def test_cluster_names(self) -> None:
        components = _make_components(3)
        graph = build_affinity_graph(components, [])
        clusters = find_clusters(graph)
        names = [c.name for c in clusters]
        assert "cluster-0" in names
        assert "cluster-1" in names
        assert "cluster-2" in names


# ---------------------------------------------------------------------------
# Tests: Power domain detection
# ---------------------------------------------------------------------------


class TestDetectPowerDomains:
    def test_no_power_nets(self) -> None:
        components = _make_components(3)
        nets = [Net(name="SIG1", pins=[("U1", "1"), ("U2", "1")])]
        domains = detect_power_domains(components, nets)
        assert domains == []

    def test_single_power_domain(self) -> None:
        components = _make_components(3)
        nets = [
            Net(name="VCC", pins=[("U1", "1"), ("U2", "1"), ("U3", "1")]),
        ]
        domains = detect_power_domains(components, nets)
        assert len(domains) == 1
        assert domains[0].name == "VCC"
        assert set(domains[0].references) == {"U1", "U2", "U3"}

    def test_multiple_power_domains(self) -> None:
        """3.3V and 5V domains should be identified separately."""
        components = _make_components(4)
        nets = [
            Net(name="+3V3", pins=[("U1", "1"), ("U2", "1")]),
            Net(name="+5V", pins=[("U3", "1"), ("U4", "1")]),
            Net(name="GND", pins=[("U1", "2"), ("U2", "2"), ("U3", "2"), ("U4", "2")]),
        ]
        domains = detect_power_domains(components, nets)
        assert len(domains) == 3  # +3V3, +5V, GND

        domain_names = {d.name for d in domains}
        assert "+3V3" in domain_names
        assert "+5V" in domain_names
        assert "GND" in domain_names

    def test_ground_variants(self) -> None:
        """Various ground net names should be detected."""
        components = _make_components(2)
        for name in ["GND", "AGND", "DGND", "PGND", "VSS"]:
            nets = [Net(name=name, pins=[("U1", "1"), ("U2", "1")])]
            domains = detect_power_domains(components, nets)
            assert len(domains) == 1, f"Expected 1 domain for {name}, got {len(domains)}"
            assert domains[0].name == name

    def test_power_variants(self) -> None:
        """Various power net names should be detected."""
        components = _make_components(2)
        for name in ["VCC", "VDD", "+3V3", "+5V", "AVCC"]:
            nets = [Net(name=name, pins=[("U1", "1"), ("U2", "1")])]
            domains = detect_power_domains(components, nets)
            assert len(domains) == 1, f"Expected 1 domain for {name}, got {len(domains)}"

    def test_single_component_net_excluded(self) -> None:
        """Power nets with only one component are not domains."""
        components = _make_components(2)
        nets = [Net(name="VCC", pins=[("U1", "1")])]  # only U1
        domains = detect_power_domains(components, nets)
        assert domains == []

    def test_power_domain_clustering_alias(self) -> None:
        """power_domain_clustering should return same result as detect_power_domains."""
        components = _make_components(3)
        nets = [Net(name="VCC", pins=[("U1", "1"), ("U2", "1"), ("U3", "1")])]
        d1 = detect_power_domains(components, nets)
        d2 = power_domain_clustering(components, nets)
        assert len(d1) == len(d2)
        assert d1[0].name == d2[0].name
        assert d1[0].references == d2[0].references


# ---------------------------------------------------------------------------
# Tests: Signal flow detection
# ---------------------------------------------------------------------------


class TestDetectSignalFlow:
    def test_no_connectors(self) -> None:
        """Without connectors, returns original order."""
        components = _make_components(3)
        nets = [Net(name="N1", pins=[("U1", "1"), ("U2", "1")])]
        result = detect_signal_flow(components, nets)
        assert result.ordering == ("U1", "U2", "U3")
        assert result.sources == ()
        assert result.sinks == ()

    def test_single_connector(self) -> None:
        comps = [
            ComponentDef(reference="J1"),  # connector
            ComponentDef(reference="U1"),
        ]
        nets = [Net(name="SIG", pins=[("J1", "1"), ("U1", "1")])]
        result = detect_signal_flow(comps, nets)
        assert "J1" in result.sources
        assert "J1" in result.ordering
        assert "U1" in result.ordering

    def test_input_to_output_flow(self) -> None:
        """Signal flows from input connector through ICs to output connector."""
        comps = [
            ComponentDef(reference="J1"),  # input connector
            ComponentDef(reference="U1"),  # processor stage 1
            ComponentDef(reference="U2"),  # processor stage 2
            ComponentDef(reference="J2"),  # output connector
        ]
        nets = [
            Net(name="IN", pins=[("J1", "1"), ("U1", "1")]),
            Net(name="MID", pins=[("U1", "2"), ("U2", "1")]),
            Net(name="OUT", pins=[("U2", "2"), ("J2", "1")]),
        ]
        result = detect_signal_flow(comps, nets)
        ordering = list(result.ordering)

        # J1 should come before J2 in the ordering
        assert ordering.index("J1") < ordering.index("J2")
        # U1 should come before U2
        assert ordering.index("U1") < ordering.index("U2")

    def test_ordering_includes_all_components(self) -> None:
        comps = [
            ComponentDef(reference="J1"),
            ComponentDef(reference="U1"),
            ComponentDef(reference="U2"),  # disconnected
        ]
        nets = [Net(name="SIG", pins=[("J1", "1"), ("U1", "1")])]
        result = detect_signal_flow(comps, nets)
        assert set(result.ordering) == {"J1", "U1", "U2"}

    def test_power_nets_excluded_from_flow(self) -> None:
        """Power/ground nets should not affect signal flow ordering."""
        comps = [
            ComponentDef(reference="J1"),
            ComponentDef(reference="U1"),
        ]
        nets = [
            Net(name="VCC", pins=[("J1", "1"), ("U1", "1")]),  # power, not signal
        ]
        result = detect_signal_flow(comps, nets)
        # With only power nets, J1 and U1 have no signal edges
        # J1 is source, but U1 is unreachable via signal edges
        assert len(result.ordering) == 2


# ---------------------------------------------------------------------------
# Tests: Schematic proximity prior
# ---------------------------------------------------------------------------


class TestSchematicProximityPrior:
    def test_empty_components(self) -> None:
        board = _make_board()
        result = schematic_proximity_prior([], [], board)
        assert isinstance(result, PlacementVector)
        assert len(result.data) == 0

    def test_single_component(self) -> None:
        board = _make_board()
        components = _make_components(1)
        result = schematic_proximity_prior(components, [], board)
        assert result.num_components == 1

    def test_vector_length(self) -> None:
        board = _make_board()
        components = _make_components(5)
        nets = [Net(name="N1", pins=[("U1", "1"), ("U2", "1")])]
        result = schematic_proximity_prior(components, nets, board)
        assert len(result.data) == 5 * FIELDS_PER_COMPONENT

    def test_rotation_and_side_defaults(self) -> None:
        """Prior placement should use rotation=0, side=0."""
        board = _make_board()
        components = _make_components(3)
        result = schematic_proximity_prior(components, [], board)
        for i in range(3):
            vals = result.component_slice(i)
            assert vals[2] == 0.0, "rotation should be 0"
            assert vals[3] == 0.0, "side should be 0 (front)"

    def test_connected_components_closer(self) -> None:
        """High-affinity components should be placed closer together."""
        board = _make_board(200, 200)
        components = _make_components(4, size=2.0)
        # U1 and U2 share 5 nets, U3 and U4 are disconnected
        nets = [Net(name=f"N{i}", pins=[("U1", str(i)), ("U2", str(i))]) for i in range(5)]
        result = schematic_proximity_prior(components, nets, board)
        pos = _placement_positions(result, components)

        dist_12 = _distance(pos["U1"], pos["U2"])
        dist_13 = _distance(pos["U1"], pos["U3"])
        dist_14 = _distance(pos["U1"], pos["U4"])

        # Connected pair should be closer than disconnected pairs
        assert dist_12 < dist_13, (
            f"U1-U2 ({dist_12:.2f}) should be closer than U1-U3 ({dist_13:.2f})"
        )
        assert dist_12 < dist_14, (
            f"U1-U2 ({dist_12:.2f}) should be closer than U1-U4 ({dist_14:.2f})"
        )

    def test_within_board_bounds(self) -> None:
        """All components should be within board bounds."""
        board = _make_board(50, 50)
        components = _make_components(8, size=3.0)
        nets = [
            Net(name="N1", pins=[("U1", "1"), ("U5", "1")]),
            Net(name="N2", pins=[("U3", "1"), ("U7", "1")]),
        ]
        result = schematic_proximity_prior(components, nets, board)
        placed = decode(result, components)
        for i, p in enumerate(placed):
            hw = components[i].width / 2.0
            hh = components[i].height / 2.0
            assert p.x - hw >= board.min_x - 1e-9, f"{p.reference} left edge out of bounds"
            assert p.x + hw <= board.max_x + 1e-9, f"{p.reference} right edge out of bounds"
            assert p.y - hh >= board.min_y - 1e-9, f"{p.reference} top edge out of bounds"
            assert p.y + hh <= board.max_y + 1e-9, f"{p.reference} bottom edge out of bounds"

    def test_stronger_affinity_tighter_placement(self) -> None:
        """Strongly connected components cluster more tightly than weakly connected.

        Setup: U1-U2-U3 are strongly connected (5 shared nets each pair).
        U4-U5 are weakly connected (1 shared net) and also weakly connected
        to the strong group via U3-U4 (1 shared net).  The strong group
        internal distances should be smaller than the U4-U5 distance.
        """
        board = _make_board(200, 200)
        components = _make_components(5, size=2.0)

        nets = [
            # Strong group: U1-U2-U3 share 5 nets per pair
            *[
                Net(name=f"S{i}", pins=[("U1", str(i)), ("U2", str(i)), ("U3", str(i))])
                for i in range(5)
            ],
            # Weak bridge: U3-U4 share 1 net
            Net(name="BRIDGE", pins=[("U3", "b"), ("U4", "b")]),
            # Weak pair: U4-U5 share 1 net
            Net(name="WEAK", pins=[("U4", "w"), ("U5", "w")]),
        ]
        result = schematic_proximity_prior(components, nets, board)
        pos = _placement_positions(result, components)

        # Average distance within strong group (U1, U2, U3)
        dists_strong = []
        for r1, r2 in [("U1", "U2"), ("U1", "U3"), ("U2", "U3")]:
            dists_strong.append(_distance(pos[r1], pos[r2]))
        avg_strong = sum(dists_strong) / len(dists_strong)

        # Distance between weakly-connected pair (U4-U5)
        dist_weak = _distance(pos["U4"], pos["U5"])

        # Strong group should be at least as tight as the weak pair
        # (both may converge very close to zero, so we also accept equal)
        assert avg_strong <= dist_weak + 1e-6, (
            f"Strong group avg dist ({avg_strong:.4f}) should be <= "
            f"weak pair dist ({dist_weak:.4f})"
        )

    def test_prior_beats_random_on_hpwl(self) -> None:
        """Proximity prior should produce lower HPWL than random placement."""
        board = _make_board(100, 100)
        components = _make_components(10, size=3.0)
        nets = [
            Net(name="N1", pins=[("U1", "1"), ("U2", "1"), ("U3", "1")]),
            Net(name="N2", pins=[("U2", "1"), ("U4", "1")]),
            Net(name="N3", pins=[("U3", "1"), ("U5", "1"), ("U6", "1")]),
            Net(name="N4", pins=[("U5", "1"), ("U7", "1"), ("U8", "1")]),
            Net(name="N5", pins=[("U7", "1"), ("U9", "1")]),
            Net(name="N6", pins=[("U8", "1"), ("U10", "1")]),
            Net(name="N7", pins=[("U1", "2"), ("U10", "2")]),
        ]

        prior_vec = schematic_proximity_prior(components, nets, board)
        prior_placed = decode(prior_vec, components)
        prior_placements = [
            ComponentPlacement(reference=p.reference, x=p.x, y=p.y) for p in prior_placed
        ]
        prior_hpwl = compute_wirelength(prior_placements, nets)

        # Average random HPWL over several seeds
        random_hpwls = []
        for seed in range(10):
            rand_vec = random_placement(components, board, seed=seed)
            rand_placed = decode(rand_vec, components)
            rand_placements = [
                ComponentPlacement(reference=p.reference, x=p.x, y=p.y) for p in rand_placed
            ]
            random_hpwls.append(compute_wirelength(rand_placements, nets))

        avg_random = sum(random_hpwls) / len(random_hpwls)
        assert prior_hpwl < avg_random, (
            f"Prior HPWL ({prior_hpwl:.2f}) should be < average random ({avg_random:.2f})"
        )

    def test_performance(self) -> None:
        """Proximity prior should complete in <1s for 20 components."""
        board = _make_board(200, 200)
        components = _make_components(20, size=5.0)
        nets = [Net(name=f"N{i}", pins=[(f"U{i + 1}", "1"), (f"U{i + 2}", "1")]) for i in range(19)]
        start = time.monotonic()
        result = schematic_proximity_prior(components, nets, board)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"Took {elapsed:.2f}s, expected <1s"
        assert result.num_components == 20


# ---------------------------------------------------------------------------
# Tests: Prior mean position (GP prior)
# ---------------------------------------------------------------------------


class TestPriorMeanPosition:
    def test_isolated_component(self) -> None:
        """Component with no neighbours returns its current position."""
        components = _make_components(3)
        graph = build_affinity_graph(components, [])
        positions = np.array([[10.0, 20.0], [30.0, 40.0], [50.0, 60.0]])

        x, y = prior_mean_position(0, positions, graph)
        assert x == 10.0
        assert y == 20.0

    def test_single_neighbour(self) -> None:
        """With one neighbour, prior mean is that neighbour's position."""
        components = _make_components(2)
        nets = [Net(name="N1", pins=[("U1", "1"), ("U2", "1")])]
        graph = build_affinity_graph(components, nets)
        positions = np.array([[0.0, 0.0], [10.0, 10.0]])

        x, y = prior_mean_position(0, positions, graph)
        assert abs(x - 10.0) < 1e-9
        assert abs(y - 10.0) < 1e-9

    def test_weighted_centroid(self) -> None:
        """Prior mean is weighted centroid of neighbours."""
        components = _make_components(3)
        nets = [
            # U1-U2: 3 shared nets
            *[Net(name=f"A{i}", pins=[("U1", str(i)), ("U2", str(i))]) for i in range(3)],
            # U1-U3: 1 shared net
            Net(name="B1", pins=[("U1", "b"), ("U3", "b")]),
        ]
        graph = build_affinity_graph(components, nets)
        positions = np.array([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0]])

        # For U1: mean = (3*10 + 1*0) / 4, (3*0 + 1*10) / 4 = (7.5, 2.5)
        x, y = prior_mean_position(0, positions, graph)
        assert abs(x - 7.5) < 1e-9
        assert abs(y - 2.5) < 1e-9

    def test_symmetric_neighbours(self) -> None:
        """Two equally-weighted neighbours yield midpoint."""
        components = _make_components(3)
        nets = [
            Net(name="N1", pins=[("U1", "1"), ("U2", "1")]),
            Net(name="N2", pins=[("U1", "2"), ("U3", "2")]),
        ]
        graph = build_affinity_graph(components, nets)
        positions = np.array([[0.0, 0.0], [10.0, 0.0], [-10.0, 0.0]])

        x, y = prior_mean_position(0, positions, graph)
        assert abs(x - 0.0) < 1e-9  # midpoint of 10 and -10
        assert abs(y - 0.0) < 1e-9


# ---------------------------------------------------------------------------
# Tests: Integration -- end-to-end prior pipeline
# ---------------------------------------------------------------------------


class TestPriorPipeline:
    def test_full_pipeline_usb_board(self) -> None:
        """End-to-end: USB-like board with connectors, IC, passives."""
        board = _make_board(80, 80)
        components = [
            ComponentDef(reference="J1", width=8, height=5),  # USB connector
            ComponentDef(reference="U1", width=4, height=4),  # USB controller
            ComponentDef(reference="R1", width=1, height=0.5),  # pullup
            ComponentDef(reference="R2", width=1, height=0.5),  # pullup
            ComponentDef(reference="C1", width=1, height=0.5),  # decoupling
            ComponentDef(reference="U2", width=5, height=5),  # MCU
            ComponentDef(reference="C2", width=1, height=0.5),  # MCU decoupling
            ComponentDef(reference="J2", width=6, height=4),  # output connector
        ]
        nets = [
            Net(name="USB_DP", pins=[("J1", "D+"), ("R1", "1"), ("U1", "DP")]),
            Net(name="USB_DM", pins=[("J1", "D-"), ("R2", "1"), ("U1", "DM")]),
            Net(name="USB_VBUS", pins=[("J1", "VBUS"), ("C1", "1"), ("U1", "VIN")]),
            Net(name="SPI_CLK", pins=[("U1", "SCK"), ("U2", "SCK")]),
            Net(name="SPI_MOSI", pins=[("U1", "MOSI"), ("U2", "MOSI")]),
            Net(name="SPI_MISO", pins=[("U1", "MISO"), ("U2", "MISO")]),
            Net(name="MCU_OUT", pins=[("U2", "OUT"), ("J2", "1")]),
            Net(name="VCC", pins=[("U1", "VCC"), ("U2", "VCC"), ("C1", "1"), ("C2", "1")]),
            Net(
                name="GND",
                pins=[
                    ("J1", "GND"),
                    ("U1", "GND"),
                    ("U2", "GND"),
                    ("C1", "2"),
                    ("C2", "2"),
                    ("J2", "GND"),
                ],
            ),
        ]

        # 1. Build affinity graph
        graph = build_affinity_graph(components, nets)
        assert graph.num_components == 8
        # J1 and U1 share multiple nets
        assert graph.weight("J1", "U1") >= 2.0

        # 2. Find clusters (excluding power nets for signal clustering)
        sig_graph = build_affinity_graph(components, nets, exclude_power_nets=True)
        clusters = find_clusters(sig_graph)
        assert len(clusters) >= 1  # at least one connected cluster

        # 3. Detect power domains
        domains = detect_power_domains(components, nets)
        domain_names = {d.name for d in domains}
        assert "VCC" in domain_names
        assert "GND" in domain_names

        # 4. Signal flow
        flow = detect_signal_flow(components, nets)
        assert len(flow.ordering) == 8
        assert len(flow.sources) >= 1

        # 5. Proximity prior
        prior_vec = schematic_proximity_prior(components, nets, board)
        assert prior_vec.num_components == 8
        pos = _placement_positions(prior_vec, components)

        # USB components (J1, U1, R1, R2) should be relatively close
        usb_positions = [pos["J1"], pos["U1"], pos["R1"], pos["R2"]]
        usb_dists = []
        for a in range(len(usb_positions)):
            for b in range(a + 1, len(usb_positions)):
                usb_dists.append(_distance(usb_positions[a], usb_positions[b]))
        avg_usb_dist = sum(usb_dists) / len(usb_dists)

        # Compare with average distance between USB group and MCU output
        cross_dists = [_distance(pos["J1"], pos["J2"]), _distance(pos["U1"], pos["J2"])]
        avg_cross_dist = sum(cross_dists) / len(cross_dists)

        # USB internal distances should be smaller than cross-group
        assert avg_usb_dist < avg_cross_dist, (
            f"USB avg dist ({avg_usb_dist:.2f}) should be < cross dist ({avg_cross_dist:.2f})"
        )
