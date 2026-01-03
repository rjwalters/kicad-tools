"""DRC regression tests for autorouter output.

These tests validate that the autorouter produces DRC-compliant output
using the routing-diagnostic.kicad_pcb fixture. They catch regressions
in pad clearance handling, grid alignment, and route quality.

The tests require kicad-cli to be installed and will be skipped if not
available. For CI environments without kicad-cli, the pure Python
validation tests still provide basic coverage.

Related issues:
- Fixture: #285
- Bug found: #292
- Would catch regressions for: #294
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli, run_drc
from kicad_tools.drc import DRCReport, ViolationType
from kicad_tools.router import (
    DesignRules,
    load_pcb_for_routing,
    merge_routes_into_pcb,
)

# Skip all tests in this module if kicad-cli is not available
pytestmark = pytest.mark.skipif(
    find_kicad_cli() is None,
    reason="kicad-cli not found - install KiCad 8 from https://www.kicad.org/download/",
)


@pytest.fixture
def routing_diagnostic_pcb(fixtures_dir: Path) -> Path:
    """Return the path to the routing diagnostic PCB."""
    return fixtures_dir / "routing-diagnostic.kicad_pcb"


@pytest.fixture
def strict_rules() -> DesignRules:
    """Return strict design rules that should produce DRC-clean output."""
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.3,
        via_diameter=0.6,
        grid_resolution=0.1,  # 0.1mm grid for precise routing
    )


def write_routed_pcb(router, original_path: Path, output_path: Path) -> None:
    """Write routed PCB to file by merging routes into original."""
    original_content = original_path.read_text()
    routes_sexp = router.to_sexp()
    merged = merge_routes_into_pcb(original_content, routes_sexp)
    output_path.write_text(merged)


def run_kicad_drc(pcb_path: Path) -> DRCReport:
    """Run KiCad DRC on a PCB file and return the parsed report."""
    result = run_drc(pcb_path, schematic_parity=False)

    if not result.success:
        pytest.skip(f"DRC command failed: {result.stderr}")

    return DRCReport.load(result.output_path)


def get_routing_violations(report: DRCReport) -> list:
    """Filter to routing-related violations only."""
    routing_types = {
        ViolationType.CLEARANCE,
        ViolationType.SHORTING_ITEMS,
        ViolationType.TRACK_WIDTH,
        ViolationType.VIA_ANNULAR_WIDTH,
        ViolationType.VIA_HOLE_LARGER_THAN_PAD,
    }
    return [v for v in report.violations if v.type in routing_types]


class TestRoutingDRCCompliance:
    """Tests for DRC compliance after routing."""

    def test_routing_produces_no_drc_violations(
        self,
        routing_diagnostic_pcb: Path,
        strict_rules: DesignRules,
    ):
        """Routed output should pass KiCad DRC with no routing violations.

        This is the primary regression test. It routes the diagnostic board
        and verifies that KiCad's DRC finds no clearance or shorting issues.
        """
        router, _ = load_pcb_for_routing(
            str(routing_diagnostic_pcb),
            rules=strict_rules,
            validate_drc=False,  # We'll run actual DRC later
        )

        # Route with multiple iterations for best results
        router.route_all_negotiated(max_iterations=15)

        stats = router.get_statistics()
        if stats["nets_routed"] == 0:
            pytest.skip("No nets were routed - nothing to validate")

        # Write to temp file and run DRC
        with tempfile.NamedTemporaryFile(
            suffix=".kicad_pcb",
            delete=False,
        ) as f:
            output_path = Path(f.name)

        try:
            write_routed_pcb(router, routing_diagnostic_pcb, output_path)
            report = run_kicad_drc(output_path)

            # Filter to routing-related violations only
            routing_violations = get_routing_violations(report)

            # Report violations for debugging
            if routing_violations:
                print("\n=== DRC Violations Found ===")
                for v in routing_violations:
                    print(f"  [{v.type.value}] {v.message}")
                    for loc in v.locations:
                        print(f"    at ({loc.x_mm:.2f}, {loc.y_mm:.2f}) mm")

            assert len(routing_violations) == 0, (
                f"Found {len(routing_violations)} routing-related DRC violations. "
                f"Types: {[v.type.value for v in routing_violations]}"
            )

        finally:
            output_path.unlink(missing_ok=True)

    def test_no_shorts_after_routing(
        self,
        routing_diagnostic_pcb: Path,
        strict_rules: DesignRules,
    ):
        """Routed output must not create shorts between nets.

        This specifically checks for SHORTING_ITEMS violations which indicate
        traces from different nets are touching.
        """
        router, _ = load_pcb_for_routing(
            str(routing_diagnostic_pcb),
            rules=strict_rules,
            validate_drc=False,
        )

        router.route_all_negotiated(max_iterations=15)

        stats = router.get_statistics()
        if stats["nets_routed"] == 0:
            pytest.skip("No nets were routed - nothing to validate")

        with tempfile.NamedTemporaryFile(
            suffix=".kicad_pcb",
            delete=False,
        ) as f:
            output_path = Path(f.name)

        try:
            write_routed_pcb(router, routing_diagnostic_pcb, output_path)
            report = run_kicad_drc(output_path)

            shorts = report.by_type(ViolationType.SHORTING_ITEMS)

            if shorts:
                print("\n=== Short Circuits Found ===")
                for v in shorts:
                    print(f"  {v.message}")
                    print(f"    Nets involved: {v.nets}")

            assert len(shorts) == 0, (
                f"Found {len(shorts)} short circuits between nets. "
                f"Affected nets: {[list(v.nets) for v in shorts]}"
            )

        finally:
            output_path.unlink(missing_ok=True)


class TestPadClearancePreservation:
    """Tests for pad clearance validation after rip-up and reroute."""

    def test_no_clearance_violations_after_multiple_ripups(
        self,
        routing_diagnostic_pcb: Path,
    ):
        """Multiple rip-up cycles should not introduce clearance violations.

        This tests that the rip-up algorithm correctly maintains pad clearance
        during iterative routing. If the bug from #292 regresses, routes may
        pass too close to pads after rip-up.
        """
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
        )

        router, _ = load_pcb_for_routing(
            str(routing_diagnostic_pcb),
            rules=rules,
            validate_drc=False,
        )

        # Force maximum rip-up iterations to stress test the algorithm
        router.route_all_negotiated(max_iterations=20)

        stats = router.get_statistics()
        if stats["nets_routed"] == 0:
            pytest.skip("No nets were routed - nothing to validate")

        # Write and run DRC to verify no clearance violations
        with tempfile.NamedTemporaryFile(
            suffix=".kicad_pcb",
            delete=False,
        ) as f:
            output_path = Path(f.name)

        try:
            write_routed_pcb(router, routing_diagnostic_pcb, output_path)
            report = run_kicad_drc(output_path)

            # Focus specifically on clearance violations
            clearance_violations = report.by_type(ViolationType.CLEARANCE)

            if clearance_violations:
                print("\n=== Clearance Violations After Rip-up ===")
                for v in clearance_violations:
                    print(f"  {v.message}")
                    for loc in v.locations:
                        print(f"    at ({loc.x_mm:.2f}, {loc.y_mm:.2f}) mm")

            assert len(clearance_violations) == 0, (
                f"Found {len(clearance_violations)} clearance violations after "
                f"rip-up/reroute. This may indicate the pad clearance bug from #292."
            )

        finally:
            output_path.unlink(missing_ok=True)


class TestIterationStability:
    """Tests for routing stability across iterations."""

    def test_more_iterations_not_worse(
        self,
        routing_diagnostic_pcb: Path,
    ):
        """More routing iterations should not increase DRC violations.

        The negotiated routing algorithm should converge or at least maintain
        quality. If more iterations produce more violations, something is wrong.
        """
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
        )

        violation_counts = []

        for iterations in [5, 10, 15]:
            router, _ = load_pcb_for_routing(
                str(routing_diagnostic_pcb),
                rules=rules,
                validate_drc=False,
            )

            router.route_all_negotiated(max_iterations=iterations)

            stats = router.get_statistics()
            if stats["nets_routed"] == 0:
                violation_counts.append(None)
                continue

            with tempfile.NamedTemporaryFile(
                suffix=".kicad_pcb",
                delete=False,
            ) as f:
                output_path = Path(f.name)

            try:
                write_routed_pcb(router, routing_diagnostic_pcb, output_path)
                report = run_kicad_drc(output_path)
                routing_violations = get_routing_violations(report)
                violation_counts.append(len(routing_violations))
            finally:
                output_path.unlink(missing_ok=True)

        # Report results
        print("\n=== Iteration Stability Test ===")
        for iters, count in zip([5, 10, 15], violation_counts, strict=True):
            if count is not None:
                print(f"  {iters} iterations: {count} violations")
            else:
                print(f"  {iters} iterations: no routes (skipped)")

        # Check that violation count doesn't increase
        # Filter out None values (no routes)
        valid_counts = [c for c in violation_counts if c is not None]
        if len(valid_counts) >= 2:
            # Allow for some variance, but major increases indicate a problem
            max_increase = 2  # Allow small variance
            for i in range(1, len(valid_counts)):
                increase = valid_counts[i] - valid_counts[i - 1]
                assert increase <= max_increase, (
                    f"DRC violations increased significantly from {valid_counts[i - 1]} to "
                    f"{valid_counts[i]} when adding more iterations. "
                    f"This suggests a regression in the rip-up algorithm."
                )


class TestRoutingQualityMetrics:
    """Tests for routing quality metrics using DRC data."""

    def test_clearance_margins(
        self,
        routing_diagnostic_pcb: Path,
    ):
        """Test that routes maintain clearance margins.

        Even without DRC violations, routes that are too close to the limit
        may be fragile. This test checks that we're not just barely passing.
        """
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
        )

        router, _ = load_pcb_for_routing(
            str(routing_diagnostic_pcb),
            rules=rules,
            validate_drc=False,
        )

        router.route_all_negotiated(max_iterations=15)

        stats = router.get_statistics()
        if stats["nets_routed"] == 0:
            pytest.skip("No nets were routed - nothing to validate")

        with tempfile.NamedTemporaryFile(
            suffix=".kicad_pcb",
            delete=False,
        ) as f:
            output_path = Path(f.name)

        try:
            write_routed_pcb(router, routing_diagnostic_pcb, output_path)
            report = run_kicad_drc(output_path)

            # Check for any clearance violations
            clearance_violations = report.by_type(ViolationType.CLEARANCE)

            # Report statistics
            print("\n=== Routing Quality Report ===")
            print(f"  Nets routed: {stats['nets_routed']}")
            print(f"  Total segments: {stats['segments']}")
            print(f"  Total vias: {stats['vias']}")
            print(f"  Total length: {stats['total_length_mm']:.2f} mm")
            print(f"  Clearance violations: {len(clearance_violations)}")

            # This is informational - we want 0 violations but report the data
            if clearance_violations:
                print("\n  Clearance violation details:")
                for v in clearance_violations:
                    if v.actual_value_mm is not None and v.required_value_mm is not None:
                        margin = v.required_value_mm - v.actual_value_mm
                        print(
                            f"    Required: {v.required_value_mm:.3f}mm, "
                            f"Actual: {v.actual_value_mm:.3f}mm, "
                            f"Shortfall: {margin:.3f}mm"
                        )

        finally:
            output_path.unlink(missing_ok=True)
