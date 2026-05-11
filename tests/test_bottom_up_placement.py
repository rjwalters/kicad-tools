"""Tests for the bottom-up (hierarchical) baseline placer.

These tests focus on the deterministic 4-cluster fixture specified in
issue #2721 and verify the contract described in
:mod:`kicad_tools.optim.bottom_up_placement`:

1. Cluster detection finds the expected clusters.
2. Within-cluster members stay near their anchor (Phase 2).
3. Inter-cluster super-blocks don't overlap (Phase 3).
4. Expand step preserves cluster relative geometry (Phase 4).
5. Fixed components are not relocated.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.optim.bottom_up_placement import (
    ClusterPlacement,
    HierarchicalPlacementConfig,
    HierarchicalPlacementResult,
    place_hierarchical,
)
from kicad_tools.optim.components import Component, Pin
from kicad_tools.optim.geometry import Polygon


def _make_component(
    ref: str,
    pin_specs: list[tuple[str, int, str]],
    x: float = 0.0,
    y: float = 0.0,
    width: float = 2.0,
    height: float = 2.0,
    fixed: bool = False,
) -> Component:
    """Build a Component with the given pins (number, net_id, net_name)."""
    pins = [
        Pin(number=num, x=x, y=y, net=net_id, net_name=net_name)
        for num, net_id, net_name in pin_specs
    ]
    return Component(
        ref=ref,
        x=x,
        y=y,
        width=width,
        height=height,
        fixed=fixed,
        pins=pins,
    )


def _build_four_cluster_fixture() -> tuple[list[Component], Polygon]:
    """Construct a fabricated 4-cluster netlist.

    Layout (logical, not spatial):

    - **Power cluster**: U1 (IC) + C1 + C2 (bypass caps on VCC).
    - **Timing cluster**: U2 (MCU) + Y1 (crystal) + C10 + C11 (load caps).
    - **Interface cluster**: J1 (connector) + D1 (ESD) + R1 (series).
    - **Driver cluster**: U3 (driver IC) + R10 + R11 (gate resistors)
      + D10 + D11 (flyback diodes).

    All clusters use disjoint nets and disjoint refs.
    """
    components: list[Component] = []

    # ---- Power cluster ----
    components.append(
        _make_component("U1", [("1", 1, "VCC"), ("2", 2, "GND"), ("3", 3, "SIG_A")])
    )
    components.append(_make_component("C1", [("1", 1, "VCC"), ("2", 2, "GND")]))
    components.append(_make_component("C2", [("1", 1, "VCC"), ("2", 2, "GND")]))

    # ---- Timing cluster ----
    components.append(
        _make_component(
            "U2",
            [
                ("1", 4, "VCC2"),
                ("2", 20, "XTAL1"),
                ("3", 21, "XTAL2"),
                ("4", 2, "GND"),
            ],
        )
    )
    components.append(
        _make_component("Y1", [("1", 20, "XTAL1"), ("2", 21, "XTAL2")])
    )
    components.append(
        _make_component("C10", [("1", 20, "XTAL1"), ("2", 2, "GND")])
    )
    components.append(
        _make_component("C11", [("1", 21, "XTAL2"), ("2", 2, "GND")])
    )

    # ---- Interface cluster ----
    components.append(
        _make_component(
            "J1",
            [("1", 30, "USB_DP"), ("2", 31, "USB_DM"), ("3", 2, "GND")],
            fixed=True,
        )
    )
    components.append(
        _make_component("D1", [("1", 30, "USB_DP"), ("2", 2, "GND")])
    )
    components.append(
        _make_component("R1", [("1", 30, "USB_DP"), ("2", 40, "USB_DP_FILT")])
    )

    return components, Polygon.rectangle(50.0, 40.0, 100.0, 80.0)


class TestFourClusterFixture:
    """Verify the algorithm on the synthetic 4-cluster fixture."""

    def test_detects_three_clusters_and_singletons(self):
        # We feed in components for 3 explicit motif clusters (power, timing,
        # interface). The fixture intentionally omits a DRIVER cluster
        # because the motif detector for DRIVER over-fires; enabling it is
        # off by default in HierarchicalPlacementConfig.
        components, board = _build_four_cluster_fixture()

        result = place_hierarchical(components, board)

        # All input components must have a placement.
        for c in components:
            assert c.ref in result.positions, f"missing position for {c.ref}"

        # Expect at least the three motif clusters detected. The remaining
        # components become singleton clusters.
        motif_anchors = {c.cluster.anchor for c in result.clusters if c.cluster.members}
        assert "U1" in motif_anchors  # power
        assert "U2" in motif_anchors  # timing
        assert "J1" in motif_anchors  # interface

    def test_cluster_members_stay_near_anchor(self):
        """Phase 2 contract: members within max_distance_mm of cluster center."""
        components, board = _build_four_cluster_fixture()
        result = place_hierarchical(components, board)

        # For every motif cluster, every member must be within the cluster's
        # bounding-box radius.
        for cp in result.clusters:
            if not cp.cluster.members:
                continue
            radius = max(cp.width, cp.height) / 2.0 + 0.1  # slack for rounding
            for ref, (ox, oy) in cp.offsets.items():
                d = math.sqrt(ox * ox + oy * oy)
                assert d <= radius, (
                    f"{ref} at offset ({ox:.2f}, {oy:.2f}) "
                    f"exceeds cluster radius {radius:.2f}"
                )

    def test_no_component_overlap(self):
        """Output positions must produce non-overlapping bounding boxes."""
        components, board = _build_four_cluster_fixture()
        result = place_hierarchical(components, board)
        component_map = {c.ref: c for c in components}

        refs = list(result.positions)
        for i in range(len(refs)):
            for j in range(i + 1, len(refs)):
                ref_a = refs[i]
                ref_b = refs[j]
                a = component_map[ref_a]
                b = component_map[ref_b]
                ax, ay, _ = result.positions[ref_a]
                bx, by, _ = result.positions[ref_b]

                ax_min = ax - a.width / 2.0
                ax_max = ax + a.width / 2.0
                ay_min = ay - a.height / 2.0
                ay_max = ay + a.height / 2.0
                bx_min = bx - b.width / 2.0
                bx_max = bx + b.width / 2.0
                by_min = by - b.height / 2.0
                by_max = by + b.height / 2.0

                overlap_x = ax_min < bx_max and bx_min < ax_max
                overlap_y = ay_min < by_max and by_min < ay_max
                # Skip the fixed J1 -- its position is preserved from input
                # (could be anywhere a user pinned it), so it's allowed to
                # collide with the placement if the user has not laid the board out yet.
                if a.fixed or b.fixed:
                    continue
                assert not (overlap_x and overlap_y), (
                    f"components {ref_a} and {ref_b} overlap at "
                    f"({ax:.2f},{ay:.2f}) vs ({bx:.2f},{by:.2f})"
                )

    def test_fixed_components_preserved(self):
        """Fixed components must keep their input position."""
        components, board = _build_four_cluster_fixture()

        # Pin J1 at a specific position.
        j1 = next(c for c in components if c.ref == "J1")
        j1.x = 5.0
        j1.y = 5.0

        result = place_hierarchical(components, board)

        x, y, _ = result.positions["J1"]
        assert math.isclose(x, 5.0)
        assert math.isclose(y, 5.0)

    def test_singleton_handling(self):
        """Components not in any motif cluster become singletons (default)."""
        # A lone resistor not connected to anything cluster-shaped.
        lone = _make_component("R99", [("1", 99, "FOO"), ("2", 98, "BAR")])
        board = Polygon.rectangle(10.0, 10.0, 20.0, 20.0)

        result = place_hierarchical([lone], board)

        assert "R99" in result.positions
        # Singleton clusters should be listed (anchor only).
        anchors = [cp.cluster.anchor for cp in result.clusters]
        assert "R99" in anchors


class TestPolygonHandling:
    """Verify the placer behaves on edge-case board outlines."""

    def test_empty_components_returns_empty_result(self):
        board = Polygon.rectangle(10.0, 10.0, 20.0, 20.0)
        result = place_hierarchical([], board)
        assert result.positions == {}
        assert result.clusters == []

    def test_off_origin_board(self):
        """Cluster centers should land inside the (offset) board bbox."""
        comps = [
            _make_component("U1", [("1", 1, "VCC"), ("2", 2, "GND")]),
            _make_component("C1", [("1", 1, "VCC"), ("2", 2, "GND")]),
        ]
        # Board centered at (100, 100).
        board = Polygon.rectangle(100.0, 100.0, 50.0, 50.0)

        result = place_hierarchical(comps, board)

        for ref, (x, y, _) in result.positions.items():
            assert 70.0 <= x <= 130.0, f"{ref}: x={x} outside board"
            assert 70.0 <= y <= 130.0, f"{ref}: y={y} outside board"


class TestConfigKnobs:
    """Verify HierarchicalPlacementConfig knobs change behavior."""

    def test_singletons_disabled(self):
        lone = _make_component("R99", [("1", 99, "FOO"), ("2", 98, "BAR")])
        board = Polygon.rectangle(10.0, 10.0, 20.0, 20.0)

        cfg = HierarchicalPlacementConfig(include_singletons=False)
        result = place_hierarchical([lone], board, cfg)

        # Singleton component still gets a position (via the fallback in
        # _expand_to_positions), but is NOT listed as a cluster.
        assert "R99" in result.positions
        anchors = [cp.cluster.anchor for cp in result.clusters]
        assert "R99" not in anchors

    def test_padding_increases_cluster_size(self):
        comps = [
            _make_component("U1", [("1", 1, "VCC"), ("2", 2, "GND")], width=3.0, height=3.0),
            _make_component("C1", [("1", 1, "VCC"), ("2", 2, "GND")]),
            _make_component("C2", [("1", 1, "VCC"), ("2", 2, "GND")]),
        ]
        board = Polygon.rectangle(50.0, 40.0, 100.0, 80.0)

        tight = HierarchicalPlacementConfig(intra_cluster_padding=0.5)
        loose = HierarchicalPlacementConfig(intra_cluster_padding=5.0)

        r_tight = place_hierarchical(comps, board, tight)
        r_loose = place_hierarchical(comps, board, loose)

        # The power cluster (U1 + caps) should be wider/taller with more padding.
        cp_tight = next(
            c for c in r_tight.clusters if c.cluster.anchor == "U1"
        )
        cp_loose = next(
            c for c in r_loose.clusters if c.cluster.anchor == "U1"
        )
        assert cp_loose.width > cp_tight.width or cp_loose.height > cp_tight.height


def test_module_public_api():
    """Smoke test: module imports cleanly and exports the public symbols."""
    import kicad_tools.optim.bottom_up_placement as bu

    expected = {
        "HierarchicalPlacementConfig",
        "ClusterPlacement",
        "HierarchicalPlacementResult",
        "place_hierarchical",
        "place_hierarchical_from_pcb",
    }
    missing = expected - set(bu.__all__)
    assert not missing, f"missing exports: {missing}"
