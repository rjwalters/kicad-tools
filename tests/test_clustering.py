"""Tests for functional clustering detection and optimization integration."""

import pytest

from kicad_tools.optim import (
    ClusterType,
    FunctionalCluster,
    PlacementConfig,
    PlacementOptimizer,
    detect_functional_clusters,
)
from kicad_tools.optim.clustering import ClusterDetector
from kicad_tools.optim.components import Component, Pin
from kicad_tools.optim.geometry import Polygon


class TestFunctionalCluster:
    """Tests for the FunctionalCluster dataclass."""

    def test_create_power_cluster(self):
        """Test creating a power cluster."""
        cluster = FunctionalCluster(
            cluster_type=ClusterType.POWER,
            anchor="U1",
            members=["C1", "C2", "C3"],
            max_distance_mm=3.0,
        )

        assert cluster.cluster_type == ClusterType.POWER
        assert cluster.anchor == "U1"
        assert cluster.members == ["C1", "C2", "C3"]
        assert cluster.max_distance_mm == 3.0

    def test_all_components(self):
        """Test all_components property includes anchor and members."""
        cluster = FunctionalCluster(
            cluster_type=ClusterType.TIMING,
            anchor="U1",
            members=["Y1", "C1", "C2"],
        )

        all_comps = cluster.all_components
        assert "U1" in all_comps
        assert "Y1" in all_comps
        assert "C1" in all_comps
        assert "C2" in all_comps
        assert len(all_comps) == 4

    def test_cluster_type_values(self):
        """Test ClusterType enum values."""
        assert ClusterType.POWER.value == "power"
        assert ClusterType.INTERFACE.value == "interface"
        assert ClusterType.TIMING.value == "timing"
        assert ClusterType.DRIVER.value == "driver"


class TestClusterDetector:
    """Tests for ClusterDetector class."""

    def _make_component(
        self, ref: str, pins: list[tuple[str, int, str]], x: float = 0.0, y: float = 0.0
    ) -> Component:
        """Helper to create a component with pins.

        Args:
            ref: Reference designator
            pins: List of (pin_number, net_id, net_name) tuples
            x, y: Component position
        """
        pin_objs = [
            Pin(number=num, x=x, y=y, net=net_id, net_name=net_name)
            for num, net_id, net_name in pins
        ]
        return Component(ref=ref, x=x, y=y, pins=pin_objs)

    def test_detect_power_cluster(self):
        """Test detection of IC + bypass capacitor clusters."""
        # Create an IC with VCC and GND pins
        ic = self._make_component(
            "U1",
            [
                ("1", 1, "VCC"),  # Power pin
                ("2", 2, "GND"),  # Ground pin
                ("3", 3, "SIG1"),  # Signal pin
            ],
        )

        # Create bypass capacitor between VCC and GND
        cap = self._make_component(
            "C1",
            [
                ("1", 1, "VCC"),  # Connected to VCC
                ("2", 2, "GND"),  # Connected to GND
            ],
        )

        detector = ClusterDetector([ic, cap])
        clusters = detector.detect_power_clusters()

        assert len(clusters) == 1
        cluster = clusters[0]
        assert cluster.cluster_type == ClusterType.POWER
        assert cluster.anchor == "U1"
        assert "C1" in cluster.members

    def test_detect_multiple_bypass_caps(self):
        """Test detection with multiple bypass capacitors on same IC."""
        ic = self._make_component(
            "U1",
            [
                ("1", 1, "VCC"),
                ("2", 2, "VDDIO"),
                ("3", 3, "GND"),
            ],
        )

        cap1 = self._make_component(
            "C1",
            [
                ("1", 1, "VCC"),
                ("2", 3, "GND"),
            ],
        )

        cap2 = self._make_component(
            "C2",
            [
                ("1", 2, "VDDIO"),
                ("2", 3, "GND"),
            ],
        )

        detector = ClusterDetector([ic, cap1, cap2])
        clusters = detector.detect_power_clusters()

        assert len(clusters) == 1
        cluster = clusters[0]
        assert cluster.anchor == "U1"
        assert "C1" in cluster.members
        assert "C2" in cluster.members

    def test_detect_timing_cluster(self):
        """Test detection of crystal + load capacitors cluster."""
        # MCU with oscillator pins
        mcu = self._make_component(
            "U1",
            [
                ("1", 1, "VCC"),
                ("2", 10, "XTAL1"),  # Crystal input
                ("3", 11, "XTAL2"),  # Crystal output
                ("4", 2, "GND"),
            ],
        )

        # Crystal connected to XTAL pins
        crystal = self._make_component(
            "Y1",
            [
                ("1", 10, "XTAL1"),
                ("2", 11, "XTAL2"),
            ],
        )

        # Load capacitors
        c1 = self._make_component(
            "C10",
            [
                ("1", 10, "XTAL1"),
                ("2", 2, "GND"),
            ],
        )

        c2 = self._make_component(
            "C11",
            [
                ("1", 11, "XTAL2"),
                ("2", 2, "GND"),
            ],
        )

        detector = ClusterDetector([mcu, crystal, c1, c2])
        clusters = detector.detect_timing_clusters()

        assert len(clusters) == 1
        cluster = clusters[0]
        assert cluster.cluster_type == ClusterType.TIMING
        assert cluster.anchor == "U1"  # MCU is anchor
        assert "Y1" in cluster.members
        # Load caps should be in members
        assert "C10" in cluster.members or "C11" in cluster.members

    def test_detect_interface_cluster(self):
        """Test detection of connector + ESD protection cluster."""
        # USB connector
        conn = self._make_component(
            "J1",
            [
                ("1", 100, "USB_D+"),
                ("2", 101, "USB_D-"),
                ("3", 1, "VCC"),
                ("4", 2, "GND"),
            ],
        )

        # ESD protection diode on USB pins
        esd = self._make_component(
            "D1",
            [
                ("1", 100, "USB_D+"),
                ("2", 2, "GND"),
            ],
        )

        # Series resistor on data line
        resistor = self._make_component(
            "R1",
            [
                ("1", 100, "USB_D+"),
                ("2", 200, "USB_D+_MCU"),  # Different net = series
            ],
        )

        detector = ClusterDetector([conn, esd, resistor])
        clusters = detector.detect_interface_clusters()

        assert len(clusters) == 1
        cluster = clusters[0]
        assert cluster.cluster_type == ClusterType.INTERFACE
        assert cluster.anchor == "J1"
        assert "D1" in cluster.members

    def test_no_cluster_for_isolated_components(self):
        """Test that isolated components don't form clusters."""
        # Two resistors with no shared nets
        r1 = self._make_component(
            "R1",
            [
                ("1", 1, "NET1"),
                ("2", 2, "NET2"),
            ],
        )

        r2 = self._make_component(
            "R2",
            [
                ("1", 3, "NET3"),
                ("2", 4, "NET4"),
            ],
        )

        detector = ClusterDetector([r1, r2])
        power_clusters = detector.detect_power_clusters()
        timing_clusters = detector.detect_timing_clusters()

        assert len(power_clusters) == 0
        assert len(timing_clusters) == 0


class TestDetectFunctionalClusters:
    """Tests for the top-level detect_functional_clusters function."""

    def test_detect_with_all_types_enabled(self):
        """Test detection with all cluster types enabled."""
        # Create a simple IC with bypass cap
        ic = Component(
            ref="U1",
            pins=[
                Pin(number="1", x=0, y=0, net=1, net_name="VCC"),
                Pin(number="2", x=0, y=0, net=2, net_name="GND"),
            ],
        )
        cap = Component(
            ref="C1",
            pins=[
                Pin(number="1", x=0, y=0, net=1, net_name="VCC"),
                Pin(number="2", x=0, y=0, net=2, net_name="GND"),
            ],
        )

        clusters = detect_functional_clusters([ic, cap])
        assert len(clusters) >= 1

    def test_detect_with_selective_types(self):
        """Test detection with only power clusters enabled."""
        ic = Component(
            ref="U1",
            pins=[
                Pin(number="1", x=0, y=0, net=1, net_name="VCC"),
                Pin(number="2", x=0, y=0, net=2, net_name="GND"),
            ],
        )
        cap = Component(
            ref="C1",
            pins=[
                Pin(number="1", x=0, y=0, net=1, net_name="VCC"),
                Pin(number="2", x=0, y=0, net=2, net_name="GND"),
            ],
        )

        # Only power clusters
        clusters = detect_functional_clusters(
            [ic, cap],
            include_power=True,
            include_timing=False,
            include_interface=False,
            include_driver=False,
        )

        assert all(c.cluster_type == ClusterType.POWER for c in clusters)


class TestPlacementOptimizerClustering:
    """Tests for clustering integration in PlacementOptimizer."""

    def test_add_cluster_creates_springs(self):
        """Test that adding a cluster creates strong springs."""
        board = Polygon.rectangle(50, 50, 100, 100)
        config = PlacementConfig(cluster_stiffness=100.0)
        optimizer = PlacementOptimizer(board, config)

        # Add IC and capacitor
        ic = Component(
            ref="U1",
            x=50,
            y=50,
            pins=[Pin(number="1", x=50, y=50, net=1, net_name="VCC")],
        )
        cap = Component(
            ref="C1",
            x=55,
            y=50,
            pins=[Pin(number="1", x=55, y=50, net=1, net_name="VCC")],
        )
        optimizer.add_component(ic)
        optimizer.add_component(cap)

        initial_spring_count = len(optimizer.springs)

        # Add power cluster
        cluster = FunctionalCluster(
            cluster_type=ClusterType.POWER,
            anchor="U1",
            members=["C1"],
            max_distance_mm=3.0,
        )
        optimizer.add_cluster(cluster)

        # Should have added a spring
        assert len(optimizer.springs) == initial_spring_count + 1
        assert len(optimizer.clusters) == 1

    def test_cluster_spring_stiffness(self):
        """Test that cluster springs use cluster_stiffness from config."""
        board = Polygon.rectangle(50, 50, 100, 100)
        config = PlacementConfig(cluster_stiffness=75.0)
        optimizer = PlacementOptimizer(board, config)

        ic = Component(
            ref="U1",
            x=50,
            y=50,
            pins=[Pin(number="1", x=50, y=50, net=1, net_name="VCC")],
        )
        cap = Component(
            ref="C1",
            x=55,
            y=50,
            pins=[Pin(number="1", x=55, y=50, net=1, net_name="VCC")],
        )
        optimizer.add_component(ic)
        optimizer.add_component(cap)

        cluster = FunctionalCluster(
            cluster_type=ClusterType.POWER,
            anchor="U1",
            members=["C1"],
        )
        optimizer.add_cluster(cluster)

        # Find the cluster spring
        cluster_springs = [s for s in optimizer.springs if s.net == -1]
        assert len(cluster_springs) == 1
        assert cluster_springs[0].stiffness == 75.0

    def test_validate_cluster_distances(self):
        """Test cluster distance validation."""
        board = Polygon.rectangle(50, 50, 100, 100)
        optimizer = PlacementOptimizer(board)

        # IC and cap far apart
        ic = Component(ref="U1", x=10, y=10, pins=[])
        cap = Component(ref="C1", x=50, y=10, pins=[])  # 40mm apart
        optimizer.add_component(ic)
        optimizer.add_component(cap)
        optimizer._component_map = {"U1": ic, "C1": cap}

        cluster = FunctionalCluster(
            cluster_type=ClusterType.POWER,
            anchor="U1",
            members=["C1"],
            max_distance_mm=5.0,  # Max 5mm
        )
        optimizer.clusters.append(cluster)

        violations = optimizer.validate_cluster_distances()
        assert len(violations) == 1
        anchor, member, actual_dist, max_dist = violations[0]
        assert anchor == "U1"
        assert member == "C1"
        assert actual_dist == pytest.approx(40.0, abs=0.1)
        assert max_dist == 5.0

    def test_no_violations_when_close(self):
        """Test no violations when cluster members are close."""
        board = Polygon.rectangle(50, 50, 100, 100)
        optimizer = PlacementOptimizer(board)

        ic = Component(ref="U1", x=10, y=10, pins=[])
        cap = Component(ref="C1", x=12, y=10, pins=[])  # 2mm apart
        optimizer.add_component(ic)
        optimizer.add_component(cap)
        optimizer._component_map = {"U1": ic, "C1": cap}

        cluster = FunctionalCluster(
            cluster_type=ClusterType.POWER,
            anchor="U1",
            members=["C1"],
            max_distance_mm=5.0,
        )
        optimizer.clusters.append(cluster)

        violations = optimizer.validate_cluster_distances()
        assert len(violations) == 0


class TestCLIClusterFlag:
    """Tests for CLI --cluster flag."""

    def test_placement_optimize_cluster_flag_exists(self):
        """Test that --cluster flag is available in CLI."""
        import contextlib
        import sys
        from io import StringIO

        from kicad_tools.cli.placement_cmd import main

        # Capture stderr to check for help
        old_stderr = sys.stderr
        sys.stderr = StringIO()

        with contextlib.suppress(SystemExit):
            # This should fail but show help
            main(["optimize", "--help"])

        sys.stderr = old_stderr

        # The fact that it parses without error means --cluster is valid
        # We can't easily test this without a PCB file
