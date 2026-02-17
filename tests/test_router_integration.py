"""Real-world board routing integration tests.

This module validates the autorouter's effectiveness on actual board layouts
from the boards/ directory. These tests measure practical performance metrics
and catch regressions in routing quality.

Test boards (in order of complexity):
1. voltage-divider: 4 components, 3 nets (2-layer, simplest)
2. charlieplex-led: 14 components, 8 nets (2-layer, dense topology)
3. usb-joystick: ~20 components, 13 nets (2-layer, mixed signals)

Success criteria:
- >= 80% net completion rate
- Zero net-to-net shorts
- Valid KiCad output (file loads without errors)
- Routing completes within reasonable time (<5 minutes per board)

These tests use pytest.mark.integration for separate execution from unit tests
since they may be slower and depend on complete board fixtures.
"""

from pathlib import Path
import time
import json

import pytest

from kicad_tools.router import load_pcb_for_routing, DesignRules
from kicad_tools.router.io import merge_routes_into_pcb, validate_routes


@pytest.fixture
def boards_dir() -> Path:
    """Return the path to the boards directory."""
    repo_root = Path(__file__).parent.parent
    return repo_root / "boards"


@pytest.fixture
def voltage_divider_pcb(boards_dir: Path) -> Path:
    """Return path to the simplest test board (4 components, 3 nets)."""
    return boards_dir / "01-voltage-divider" / "output" / "voltage_divider.kicad_pcb"


@pytest.fixture
def charlieplex_pcb(boards_dir: Path) -> Path:
    """Return path to dense topology test board (14 components, 8 nets)."""
    return boards_dir / "02-charlieplex-led" / "output" / "charlieplex_3x3.kicad_pcb"


@pytest.fixture
def usb_joystick_pcb(boards_dir: Path) -> Path:
    """Return path to complex test board (~20 components, 13 nets)."""
    return boards_dir / "03-usb-joystick" / "output" / "usb_joystick.kicad_pcb"


@pytest.mark.integration
class TestRealBoardRouting:
    """End-to-end routing tests on real board layouts."""

    def test_voltage_divider_high_completion(self, voltage_divider_pcb: Path):
        """Simplest board should achieve >= 80% routing completion."""
        assert voltage_divider_pcb.exists(), f"Board fixture not found: {voltage_divider_pcb}"

        # Load board
        router, net_map = load_pcb_for_routing(str(voltage_divider_pcb))
        assert router is not None
        assert len(net_map) >= 3, "Expected at least 3 nets"

        # Count signal nets (exclude net 0 which is unconnected)
        total_nets = len([n for n in router.nets if n > 0])
        assert total_nets > 0, "No signal nets to route"

        # Route all nets
        start_time = time.time()
        router.route_all()
        routing_time = time.time() - start_time

        # Get statistics
        stats = router.get_statistics()
        routed_nets = stats["nets_routed"]
        completion_rate = routed_nets / total_nets if total_nets > 0 else 0

        # Assertions
        assert completion_rate >= 0.80, (
            f"Completion rate {completion_rate:.1%} below 80% threshold "
            f"({routed_nets}/{total_nets} nets routed)"
        )
        assert routing_time < 300, f"Routing took {routing_time:.1f}s (>5 min)"

        # Log metrics for benchmarking
        print(f"\n=== voltage-divider routing metrics ===")
        print(f"Nets: {routed_nets}/{total_nets} ({completion_rate:.1%})")
        print(f"Time: {routing_time:.2f}s")
        print(f"Segments: {stats.get('segments', 0)}")
        print(f"Vias: {stats.get('vias', 0)}")
        if hasattr(router, 'routing_failures'):
            print(f"Failures: {len(router.routing_failures)}")

    def test_charlieplex_high_completion(self, charlieplex_pcb: Path):
        """Dense topology board should achieve >= 80% routing completion."""
        assert charlieplex_pcb.exists(), f"Board fixture not found: {charlieplex_pcb}"

        # Load board
        router, net_map = load_pcb_for_routing(str(charlieplex_pcb))
        assert router is not None
        assert len(net_map) >= 8, "Expected at least 8 nets"

        # Count signal nets (exclude net 0)
        total_nets = len([n for n in router.nets if n > 0])
        assert total_nets > 0, "No signal nets to route"

        # Route all nets
        start_time = time.time()
        router.route_all()
        routing_time = time.time() - start_time

        # Get statistics
        stats = router.get_statistics()
        routed_nets = stats["nets_routed"]
        completion_rate = routed_nets / total_nets if total_nets > 0 else 0

        # Assertions
        assert completion_rate >= 0.80, (
            f"Completion rate {completion_rate:.1%} below 80% threshold "
            f"({routed_nets}/{total_nets} nets routed)"
        )
        assert routing_time < 300, f"Routing took {routing_time:.1f}s (>5 min)"

        # Log metrics
        print(f"\n=== charlieplex routing metrics ===")
        print(f"Nets: {routed_nets}/{total_nets} ({completion_rate:.1%})")
        print(f"Time: {routing_time:.2f}s")
        print(f"Segments: {stats.get('segments', 0)}")
        print(f"Vias: {stats.get('vias', 0)}")
        if hasattr(router, 'routing_failures'):
            print(f"Failures: {len(router.routing_failures)}")

    def test_usb_joystick_valid_output(self, usb_joystick_pcb: Path):
        """Complex board should produce valid KiCad output."""
        assert usb_joystick_pcb.exists(), f"Board fixture not found: {usb_joystick_pcb}"

        # Load board
        with open(usb_joystick_pcb, "r") as f:
            original_pcb_text = f.read()

        router, net_map = load_pcb_for_routing(str(usb_joystick_pcb))
        assert router is not None

        # Count signal nets
        total_nets = len([n for n in router.nets if n > 0])

        # Route
        router.route_all()

        # Merge routes into PCB (validates output structure)
        try:
            merged_pcb = merge_routes_into_pcb(original_pcb_text, router.to_sexp())
            assert merged_pcb is not None
            assert len(merged_pcb) > len(original_pcb_text), "Output should contain routes"

            # Verify it's valid s-expression format
            assert merged_pcb.startswith("(kicad_pcb"), "Output should be valid KiCad PCB"
            assert merged_pcb.count("(") == merged_pcb.count(")"), "S-expression balanced"

        except Exception as e:
            pytest.fail(f"Failed to merge routes into PCB: {e}")

        # Get statistics
        stats = router.get_statistics()
        routed_nets = stats["nets_routed"]
        completion_rate = routed_nets / total_nets if total_nets > 0 else 0

        print(f"\n=== usb-joystick routing metrics ===")
        print(f"Nets: {routed_nets}/{total_nets} ({completion_rate:.1%})")
        print(f"Output size: {len(merged_pcb)} bytes")
        print(f"Segments: {stats.get('segments', 0)}")
        print(f"Vias: {stats.get('vias', 0)}")
        if hasattr(router, 'routing_failures'):
            print(f"Failures: {len(router.routing_failures)}")

    def test_no_shorts_in_output(self, voltage_divider_pcb: Path):
        """Routed output must have zero net-to-net shorts."""
        # Load and route board
        router, net_map = load_pcb_for_routing(str(voltage_divider_pcb))

        router.route_all()

        # Check for shorts using validate_routes
        violations = validate_routes(router)

        # Filter for shorts (clearance violations between different nets)
        shorts = [v for v in violations if v.get("type") == "short"]

        assert len(shorts) == 0, (
            f"Found {len(shorts)} net-to-net shorts:\n"
            + "\n".join(f"  - {s}" for s in shorts[:5])
        )

        print(f"\n=== Short detection ===")
        print(f"Shorts: {len(shorts)}")
        print(f"Other violations: {len(violations) - len(shorts)}")

    def test_routing_time_budget(self, voltage_divider_pcb: Path):
        """Board routing should complete within 5 minutes."""
        router, net_map = load_pcb_for_routing(str(voltage_divider_pcb))

        total_nets = len([n for n in router.nets if n > 0])

        start_time = time.time()
        router.route_all()
        routing_time = time.time() - start_time

        assert routing_time < 300, (
            f"Routing took {routing_time:.1f}s, exceeding 5-minute budget"
        )

        stats = router.get_statistics()

        print(f"\n=== Performance ===")
        print(f"Routing time: {routing_time:.2f}s")
        print(f"Nets routed: {stats['nets_routed']}/{total_nets}")


@pytest.mark.integration
class TestRoutingBenchmark:
    """Performance tracking and benchmarking tests."""

    def test_benchmark_all_boards(
        self,
        voltage_divider_pcb: Path,
        charlieplex_pcb: Path,
        usb_joystick_pcb: Path,
    ):
        """Record routing metrics for all test boards for trend analysis."""
        boards = [
            ("voltage-divider", voltage_divider_pcb),
            ("charlieplex-led", charlieplex_pcb),
            ("usb-joystick", usb_joystick_pcb),
        ]

        results = []

        for board_name, board_path in boards:
            if not board_path.exists():
                print(f"Skipping {board_name}: board file not found")
                continue

            # Load and route
            router, net_map = load_pcb_for_routing(str(board_path))

            nets_to_route = [
                (net_name, pads)
                for net_name, pads in net_map.items()
                if net_name not in {"GND", "VCC", "+3V3", "+5V", "+12V"}
            ]

            total_nets = len(nets_to_route)

            start_time = time.time()
            router.route_all(nets_to_route)
            routing_time = time.time() - start_time

            routed_nets = len([r for r in router.routes if r])
            completion_rate = routed_nets / total_nets if total_nets > 0 else 0

            # Collect statistics
            stats = router.get_statistics() if hasattr(router, "get_statistics") else {}

            result = {
                "board": board_name,
                "total_nets": total_nets,
                "routed_nets": routed_nets,
                "completion_rate": completion_rate,
                "routing_time": routing_time,
                "failures": len(router.routing_failures),
                "segments": stats.get("segments", 0),
                "vias": stats.get("vias", 0),
            }

            results.append(result)

            print(f"\n=== {board_name} ===")
            print(f"Completion: {routed_nets}/{total_nets} ({completion_rate:.1%})")
            print(f"Time: {routing_time:.2f}s")
            print(f"Failures: {len(router.routing_failures)}")
            print(f"Segments: {stats.get('segments', 0)}")
            print(f"Vias: {stats.get('vias', 0)}")

        # Write benchmark results to file for tracking
        benchmark_file = Path(__file__).parent / "benchmark_results.json"
        with open(benchmark_file, "w") as f:
            json.dump(results, f, indent=2)

        print(f"\nBenchmark results written to: {benchmark_file}")

        # Verify at least one board was tested
        assert len(results) > 0, "No boards were successfully tested"
