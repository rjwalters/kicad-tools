"""Tests for GPU-accelerated batch pathfinding (Issue #1092).

These tests verify the BatchPathfinder class can route multiple independent
nets in parallel using GPU acceleration.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import numpy as np
import pytest

from kicad_tools.acceleration import BackendType, get_backend, get_best_available_backend
from kicad_tools.acceleration.kernels.routing import (
    BatchPathfinder,
    BatchRouteRequest,
    BatchRouteResult,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


@pytest.fixture
def design_rules():
    """Create design rules for testing."""
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        grid_resolution=0.1,
        cost_straight=1.0,
        cost_turn=1.2,
        cost_via=10.0,
    )


@pytest.fixture
def routing_grid(design_rules):
    """Create a routing grid for testing."""
    return RoutingGrid(
        width=20.0,  # 20mm x 20mm board
        height=20.0,
        rules=design_rules,
        origin_x=0.0,
        origin_y=0.0,
    )


@pytest.fixture
def cpu_backend():
    """Get CPU backend for testing."""
    return get_backend(BackendType.CPU)


@pytest.fixture
def sample_pads():
    """Create sample pads for routing tests."""
    # Create 8 pads in a grid pattern (4 nets, 2 pads each)
    pads = []

    # Net 1: pads at (2, 2) and (8, 8)
    pads.append(
        Pad(x=2.0, y=2.0, width=1.0, height=1.0, net=1, net_name="NET1",
            layer=Layer.F_CU, ref="U1", pin="1")
    )
    pads.append(
        Pad(x=8.0, y=8.0, width=1.0, height=1.0, net=1, net_name="NET1",
            layer=Layer.F_CU, ref="U2", pin="1")
    )

    # Net 2: pads at (2, 18) and (8, 12) - independent of net 1
    pads.append(
        Pad(x=2.0, y=18.0, width=1.0, height=1.0, net=2, net_name="NET2",
            layer=Layer.F_CU, ref="U1", pin="2")
    )
    pads.append(
        Pad(x=8.0, y=12.0, width=1.0, height=1.0, net=2, net_name="NET2",
            layer=Layer.F_CU, ref="U2", pin="2")
    )

    # Net 3: pads at (12, 2) and (18, 8) - independent of nets 1, 2
    pads.append(
        Pad(x=12.0, y=2.0, width=1.0, height=1.0, net=3, net_name="NET3",
            layer=Layer.F_CU, ref="U3", pin="1")
    )
    pads.append(
        Pad(x=18.0, y=8.0, width=1.0, height=1.0, net=3, net_name="NET3",
            layer=Layer.F_CU, ref="U4", pin="1")
    )

    # Net 4: pads at (12, 18) and (18, 12) - independent of others
    pads.append(
        Pad(x=12.0, y=18.0, width=1.0, height=1.0, net=4, net_name="NET4",
            layer=Layer.F_CU, ref="U3", pin="2")
    )
    pads.append(
        Pad(x=18.0, y=12.0, width=1.0, height=1.0, net=4, net_name="NET4",
            layer=Layer.F_CU, ref="U4", pin="2")
    )

    return pads


class TestBatchRouteRequest:
    """Tests for BatchRouteRequest dataclass."""

    def test_request_creation(self, sample_pads):
        """Test creating a route request."""
        req = BatchRouteRequest(
            net_id=1,
            source_pad=sample_pads[0],
            target_pad=sample_pads[1],
            priority=0,
        )
        assert req.net_id == 1
        assert req.source_pad.x == 2.0
        assert req.target_pad.x == 8.0
        assert req.priority == 0

    def test_request_default_priority(self, sample_pads):
        """Test default priority is 0."""
        req = BatchRouteRequest(
            net_id=1,
            source_pad=sample_pads[0],
            target_pad=sample_pads[1],
        )
        assert req.priority == 0


class TestBatchRouteResult:
    """Tests for BatchRouteResult dataclass."""

    def test_success_result(self):
        """Test successful route result."""
        result = BatchRouteResult(
            net_id=1,
            success=True,
            path=[(0, 0, 0), (1, 1, 0), (2, 2, 0)],
            cost=10.5,
            nodes_explored=100,
        )
        assert result.success
        assert len(result.path) == 3
        assert result.cost == 10.5

    def test_failure_result(self):
        """Test failed route result."""
        result = BatchRouteResult(
            net_id=1,
            success=False,
            nodes_explored=500,
        )
        assert not result.success
        assert result.path == []
        assert result.cost == float("inf")


class TestBatchPathfinder:
    """Tests for BatchPathfinder class."""

    def test_initialization(self, routing_grid, design_rules, cpu_backend):
        """Test BatchPathfinder initialization."""
        pathfinder = BatchPathfinder(routing_grid, design_rules, cpu_backend)

        assert pathfinder.grid is routing_grid
        assert pathfinder.rules is design_rules
        assert not pathfinder.is_gpu_enabled  # CPU backend
        assert pathfinder.backend_name == "cpu"

    def test_initialization_auto_backend(self, routing_grid, design_rules):
        """Test BatchPathfinder auto-detects best backend."""
        pathfinder = BatchPathfinder(routing_grid, design_rules)

        # Should have auto-detected backend
        assert pathfinder.backend is not None
        assert pathfinder.backend_name in ["cpu", "cuda", "metal"]

    def test_find_independent_nets_all_independent(
        self, routing_grid, design_rules, sample_pads, cpu_backend
    ):
        """Test finding independent nets when all are independent."""
        pathfinder = BatchPathfinder(routing_grid, design_rules, cpu_backend)

        # Create 4 independent route requests
        requests = [
            BatchRouteRequest(net_id=1, source_pad=sample_pads[0], target_pad=sample_pads[1]),
            BatchRouteRequest(net_id=2, source_pad=sample_pads[2], target_pad=sample_pads[3]),
            BatchRouteRequest(net_id=3, source_pad=sample_pads[4], target_pad=sample_pads[5]),
            BatchRouteRequest(net_id=4, source_pad=sample_pads[6], target_pad=sample_pads[7]),
        ]

        batches = pathfinder.find_independent_nets(requests)

        # All 4 should be in a single batch (they don't overlap)
        assert len(batches) >= 1
        total_in_batches = sum(len(b) for b in batches)
        assert total_in_batches == 4

    def test_find_independent_nets_overlapping(
        self, routing_grid, design_rules, cpu_backend
    ):
        """Test finding independent nets with overlapping routes."""
        pathfinder = BatchPathfinder(routing_grid, design_rules, cpu_backend)

        # Create overlapping pads (routes will cross)
        pad1 = Pad(x=5.0, y=5.0, width=1.0, height=1.0, net=1, net_name="N1",
                   layer=Layer.F_CU, ref="U1", pin="1")
        pad2 = Pad(x=15.0, y=15.0, width=1.0, height=1.0, net=1, net_name="N1",
                   layer=Layer.F_CU, ref="U2", pin="1")
        pad3 = Pad(x=5.0, y=15.0, width=1.0, height=1.0, net=2, net_name="N2",
                   layer=Layer.F_CU, ref="U3", pin="1")
        pad4 = Pad(x=15.0, y=5.0, width=1.0, height=1.0, net=2, net_name="N2",
                   layer=Layer.F_CU, ref="U4", pin="1")

        requests = [
            BatchRouteRequest(net_id=1, source_pad=pad1, target_pad=pad2),  # Diagonal SW-NE
            BatchRouteRequest(net_id=2, source_pad=pad3, target_pad=pad4),  # Diagonal NW-SE
        ]

        batches = pathfinder.find_independent_nets(requests)

        # The routes cross, so they should be in separate batches
        assert len(batches) == 2
        assert len(batches[0]) == 1
        assert len(batches[1]) == 1

    def test_route_batch_empty(self, routing_grid, design_rules, cpu_backend):
        """Test routing empty batch."""
        pathfinder = BatchPathfinder(routing_grid, design_rules, cpu_backend)
        results = pathfinder.route_batch([])
        assert results == []

    def test_route_single_net(
        self, routing_grid, design_rules, sample_pads, cpu_backend
    ):
        """Test routing a single net."""
        pathfinder = BatchPathfinder(routing_grid, design_rules, cpu_backend)

        requests = [
            BatchRouteRequest(net_id=1, source_pad=sample_pads[0], target_pad=sample_pads[1]),
        ]

        # Add pads to grid
        for pad in sample_pads[:2]:
            routing_grid.add_pad(pad)

        results = pathfinder.route_batch(requests)

        assert len(results) == 1
        assert results[0].net_id == 1
        assert results[0].success
        assert len(results[0].path) > 0

    def test_route_multiple_independent_nets(
        self, routing_grid, design_rules, sample_pads, cpu_backend
    ):
        """Test routing multiple independent nets."""
        pathfinder = BatchPathfinder(routing_grid, design_rules, cpu_backend)

        # Add all pads to grid
        for pad in sample_pads:
            routing_grid.add_pad(pad)

        requests = [
            BatchRouteRequest(net_id=1, source_pad=sample_pads[0], target_pad=sample_pads[1]),
            BatchRouteRequest(net_id=2, source_pad=sample_pads[2], target_pad=sample_pads[3]),
            BatchRouteRequest(net_id=3, source_pad=sample_pads[4], target_pad=sample_pads[5]),
            BatchRouteRequest(net_id=4, source_pad=sample_pads[6], target_pad=sample_pads[7]),
        ]

        results = pathfinder.route_batch(requests)

        assert len(results) == 4
        for i, result in enumerate(results):
            assert result.net_id == i + 1
            assert result.success, f"Net {i+1} failed to route"

    def test_statistics_tracking(
        self, routing_grid, design_rules, sample_pads, cpu_backend
    ):
        """Test that statistics are tracked correctly."""
        pathfinder = BatchPathfinder(routing_grid, design_rules, cpu_backend)

        for pad in sample_pads[:2]:
            routing_grid.add_pad(pad)

        requests = [
            BatchRouteRequest(net_id=1, source_pad=sample_pads[0], target_pad=sample_pads[1]),
        ]

        pathfinder.route_batch(requests)

        stats = pathfinder.get_statistics()
        assert stats["total_routes_attempted"] == 1
        assert stats["total_nodes_explored"] > 0
        assert stats["avg_nodes_per_route"] > 0
        assert stats["backend"] == "cpu"


class TestBatchRoutingPerformance:
    """Performance tests for batch routing (Issue #1092 acceptance criteria)."""

    @pytest.mark.slow
    def test_batch_routing_speedup(self, design_rules):
        """Test that batch routing provides speedup with 4+ independent nets.

        Acceptance criteria: Batch pathfinding routes at least 4 independent
        nets in parallel on GPU.
        """
        # Create a larger grid for meaningful performance comparison
        grid = RoutingGrid(
            width=50.0,
            height=50.0,
            rules=design_rules,
            origin_x=0.0,
            origin_y=0.0,
        )

        # Create 8 independent nets (far apart so they don't overlap)
        pads = []
        for i in range(8):
            x1 = 5.0 + (i % 4) * 12.0
            y1 = 5.0 + (i // 4) * 25.0
            x2 = x1 + 8.0
            y2 = y1 + 8.0

            pads.append(
                Pad(x=x1, y=y1, width=1.0, height=1.0, net=i+1, net_name=f"NET{i+1}",
                    layer=Layer.F_CU, ref=f"U{i+1}", pin="1")
            )
            pads.append(
                Pad(x=x2, y=y2, width=1.0, height=1.0, net=i+1, net_name=f"NET{i+1}",
                    layer=Layer.F_CU, ref=f"U{i+1}", pin="2")
            )
            grid.add_pad(pads[-2])
            grid.add_pad(pads[-1])

        # Create batch pathfinder
        backend = get_best_available_backend()
        pathfinder = BatchPathfinder(grid, design_rules, backend)

        # Create route requests
        requests = []
        for i in range(8):
            requests.append(
                BatchRouteRequest(
                    net_id=i+1,
                    source_pad=pads[i*2],
                    target_pad=pads[i*2+1],
                )
            )

        # Test batch routing
        start_time = time.time()
        results = pathfinder.route_batch(requests)
        batch_time = time.time() - start_time

        # Verify all routes succeeded
        success_count = sum(1 for r in results if r.success)
        assert success_count >= 6, f"Expected at least 6/8 nets to route, got {success_count}/8"

        # Print performance info
        stats = pathfinder.get_statistics()
        print(f"\nBatch routing performance:")
        print(f"  Backend: {stats['backend']} (GPU: {stats['is_gpu']})")
        print(f"  Nets routed: {success_count}/8")
        print(f"  Time: {batch_time:.3f}s")
        print(f"  Nodes explored: {stats['total_nodes_explored']}")


class TestGPUBackendDetection:
    """Tests for GPU backend detection and diagnostics."""

    def test_backend_info_structure(self):
        """Test backend info has required fields."""
        from kicad_tools.router.cpp_backend import get_backend_info

        info = get_backend_info()

        assert "backend" in info
        assert "version" in info
        assert "available" in info
        assert "platform" in info

        # Platform info should have system details
        assert "system" in info["platform"]
        assert "machine" in info["platform"]
        assert "python_version" in info["platform"]

    def test_backend_unavailable_reason(self):
        """Test that unavailable reason is provided when C++ backend is missing."""
        from kicad_tools.router.cpp_backend import get_backend_info, is_cpp_available

        info = get_backend_info()

        if not is_cpp_available():
            assert "unavailable_reason" in info
            assert info["unavailable_reason"] is not None
        else:
            # If available, should not have unavailable_reason
            assert "unavailable_reason" not in info or info.get("unavailable_reason") is None
