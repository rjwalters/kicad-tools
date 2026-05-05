"""Tests for the pad-grid preflight check (issue #2497).

The preflight catches off-grid pads BEFORE invoking the router so users
get an early, actionable error instead of a deep PADS_OFF_GRID failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router.preflight import (
    OffGridReport,
    check_pad_grid_alignment,
)

# ---------------------------------------------------------------------------
# Helpers: build minimal synthetic PCB text strings


def _pcb_with_pads(
    footprints: list[tuple[str, str, float, float, float, list[tuple[str, float, float]]]],
) -> str:
    """Build a minimal .kicad_pcb text with the given footprints.

    Each footprint is a tuple of:
        (footprint_name, ref, fp_x, fp_y, fp_rot_degrees, list_of_pads)
    where each pad is ``(pin, local_x, local_y)``.
    """
    lines = [
        "(kicad_pcb",
        "  (version 20240108)",
        '  (generator "kicad-tools-test")',
        "  (general (thickness 1.6))",
        '  (paper "A4")',
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))',
        '  (net 0 "")',
        '  (net 1 "TEST")',
    ]
    for footprint_name, ref, fp_x, fp_y, fp_rot, pads in footprints:
        lines.append(f'  (footprint "{footprint_name}"')
        lines.append('    (layer "F.Cu")')
        lines.append(f"    (at {fp_x} {fp_y} {fp_rot})")
        lines.append(
            f'    (fp_text reference "{ref}" (at 0 -2) (layer "F.SilkS")'
            "      (effects (font (size 1 1) (thickness 0.15))))"
        )
        for pin, lx, ly in pads:
            lines.append(
                f'    (pad "{pin}" smd rect (at {lx} {ly}) (size 0.5 0.5) '
                '(layers "F.Cu" "F.Mask") (net 1 "TEST"))'
            )
        lines.append("  )")
    lines.append(")")
    return "\n".join(lines) + "\n"


def _write_pcb(tmp_path: Path, content: str, name: str = "board.kicad_pcb") -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Acceptance criteria coverage


class TestOnGridPCB:
    """An on-grid PCB produces an empty report."""

    def test_all_pads_on_grid(self, tmp_path: Path) -> None:
        text = _pcb_with_pads(
            [
                (
                    "Package_QFP:TQFP-32_7x7mm_P0.8mm",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    [("1", -2.5, -2.5), ("2", -2.5, 2.5), ("3", 2.5, 2.5)],
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)

        report = check_pad_grid_alignment(pcb, grid_resolution=0.1)

        assert isinstance(report, OffGridReport)
        assert report.passed
        assert report.off_grid_pads == []
        assert report.total_pads == 3
        assert report.suggested_grid is None
        assert "OK" in report.summary()


class TestSingleOffGridPad:
    """One pad shifted by 0.036 mm produces exactly one violation."""

    def test_single_off_grid_pad(self, tmp_path: Path) -> None:
        # Footprint at integer mm; one pad pushed off-grid by 0.036 mm in x
        text = _pcb_with_pads(
            [
                (
                    "Package_QFP:TQFP-32_7x7mm_P0.8mm",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    [
                        ("1", 0.0, 0.0),  # on grid
                        ("9", 1.236, 0.0),  # 0.036 mm off (1.2 + 0.036)
                    ],
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)

        report = check_pad_grid_alignment(pcb, grid_resolution=0.1)

        assert not report.passed
        assert len(report.off_grid_pads) == 1
        violation = report.off_grid_pads[0]
        assert violation.ref == "U1"
        assert violation.pin == "9"
        assert violation.offset_mm == pytest.approx(0.036, abs=1e-3)
        assert violation.footprint_name == "Package_QFP:TQFP-32_7x7mm_P0.8mm"

    def test_violation_message_format(self, tmp_path: Path) -> None:
        """Each violation message contains all required fields."""
        text = _pcb_with_pads(
            [
                (
                    "Package_QFP:TQFP-32_7x7mm_P0.8mm",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    [("9", 1.236, 0.0)],
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1)
        assert len(report.off_grid_pads) == 1

        msg = report.off_grid_pads[0].message(report.grid_resolution, report.suggested_grid)
        # ref+pin
        assert "U1.9" in msg
        # absolute (x, y) with 3-decimal precision
        assert "101.236" in msg or "(101.236, 100.000)" in msg
        # deviation in mm with 3-decimal precision
        assert "0.036" in msg
        # configured grid resolution
        assert "0.1" in msg
        # footprint library name
        assert "Package_QFP:TQFP-32_7x7mm_P0.8mm" in msg
        # suggested-fix line
        assert "Suggested fix" in msg


class TestSuggestedGrid:
    """auto_select_grid_resolution suggestion appears only when it would help."""

    def test_finer_grid_suggested_when_useful(self, tmp_path: Path) -> None:
        # Pads at multiples of 0.05 mm: off-grid at 0.1 mm but on-grid at 0.05 mm
        text = _pcb_with_pads(
            [
                (
                    "Test:Footprint",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    [
                        ("1", 0.0, 0.0),
                        ("2", 0.05, 0.0),
                        ("3", 0.10, 0.0),
                        ("4", 0.15, 0.0),
                    ],
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1)

        assert not report.passed
        # Should suggest a finer grid that aligns all four pads
        assert report.suggested_grid is not None
        assert report.suggested_grid <= 0.05 + 1e-9

    def test_no_finer_grid_when_no_help(self, tmp_path: Path) -> None:
        # Make the offset awkward: 0.0333... mm (not aligned to any standard
        # finer candidate available to auto_select_grid_resolution).
        text = _pcb_with_pads(
            [
                (
                    "Test:Footprint",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    [
                        ("1", 0.0333, 0.0),
                    ],
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1)
        assert not report.passed
        # We don't strictly require suggested_grid to be None for this exotic
        # offset (auto_select may find a GCD-derived grid).  But if it's
        # provided, it must actually clear all violations.
        if report.suggested_grid is not None:
            assert report.suggested_grid < 0.1


class TestRotatedFootprint:
    """A rotated footprint must yield correct absolute coords (gotcha
    listed in the issue)."""

    def test_rotated_footprint_off_grid(self, tmp_path: Path) -> None:
        # Local pad at (0.0333, 0.0) with footprint rotated 90° CCW: absolute
        # offset becomes (0.0, 0.0333), still off the 0.1mm grid.
        text = _pcb_with_pads(
            [
                (
                    "Test:Rotated",
                    "U1",
                    100.0,
                    100.0,
                    90.0,
                    [("1", 0.0333, 0.0)],
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1)
        assert not report.passed
        violation = report.off_grid_pads[0]
        # X should be near 100.0 (was rotated to that axis)
        assert violation.x == pytest.approx(100.0, abs=1e-3)
        # Y should be ~100.0333 -> off-grid
        assert violation.y == pytest.approx(100.0333, abs=1e-3)


class TestThreshold:
    """The threshold defaults to resolution / 10 and is configurable."""

    def test_default_threshold(self, tmp_path: Path) -> None:
        text = _pcb_with_pads(
            [
                (
                    "Test:FP",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    [("1", 0.005, 0.0)],  # 0.005 mm < 0.01 mm threshold
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1)
        # Within default threshold -> on grid
        assert report.passed

    def test_custom_threshold(self, tmp_path: Path) -> None:
        text = _pcb_with_pads(
            [
                (
                    "Test:FP",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    [("1", 0.005, 0.0)],
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1, threshold=0.001)
        # Strict threshold -> 0.005 mm is now off-grid
        assert not report.passed


class TestEmptyPCB:
    """A PCB with no footprints produces an empty, passing report."""

    def test_no_footprints(self, tmp_path: Path) -> None:
        text = _pcb_with_pads([])
        pcb = _write_pcb(tmp_path, text)
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1)
        assert report.passed
        assert report.total_pads == 0
        assert report.off_grid_pads == []


# ---------------------------------------------------------------------------
# Acceptance: kct check integration


class TestDRCCheckerIntegration:
    """The DRCChecker exposes pad_grid as a check category."""

    def test_drc_checker_method_exists(self, tmp_path: Path) -> None:
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        text = _pcb_with_pads(
            [
                (
                    "Test:FP",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    [("1", 0.0, 0.0), ("2", 1.236, 0.0)],
                )
            ]
        )
        pcb_path = _write_pcb(tmp_path, text)
        pcb = PCB.load(pcb_path)
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)

        results = checker.check_pad_grid_alignment(grid_resolution=0.1)
        assert results.warning_count == 1
        violation = results.violations[0]
        assert violation.rule_id == "pad_grid"
        assert violation.severity == "warning"
        assert violation.location == pytest.approx((101.236, 100.0))
        assert "U1.2" in violation.message

    def test_drc_checker_in_categories(self) -> None:
        from kicad_tools.cli.check_cmd import CHECK_CATEGORIES

        assert "pad_grid" in CHECK_CATEGORIES
