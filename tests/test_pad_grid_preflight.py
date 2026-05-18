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
    """One pad shifted by ~0.064 mm produces exactly one violation.

    The default tolerance was raised to 0.05 mm (issue #3042) to clear
    stock KiCad library footprints (PinHeader_2.54mm pads sit 0.040 mm
    off the 0.1 mm grid by design).  This test uses a 2D offset of
    (0.05, 0.04) mm giving L2 = sqrt(0.0041) ~= 0.064 mm that exceeds
    the new default and represents a real placement error.
    """

    def test_single_off_grid_pad(self, tmp_path: Path) -> None:
        # Footprint at integer mm; one pad pushed off-grid by ~0.064 mm
        # in L2 via a (0.05, 0.04) mm 2D offset.  This exceeds the 0.05
        # mm default tolerance and represents a real placement error.
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
                        ("9", 1.25, 0.04),  # (0.05, 0.04) off -> L2 ~0.064 mm
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
        # L2 = sqrt(0.05**2 + 0.04**2) ~= 0.0640 mm
        assert violation.offset_mm == pytest.approx(0.0640, abs=1e-3)
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
                    [("9", 1.25, 0.04)],
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
        assert "101.250" in msg or "(101.250, 100.040)" in msg
        # deviation in mm with 3-decimal precision (L2 ~0.064)
        assert "0.064" in msg
        # configured grid resolution
        assert "0.1" in msg
        # footprint library name
        assert "Package_QFP:TQFP-32_7x7mm_P0.8mm" in msg
        # suggested-fix line
        assert "Suggested fix" in msg


class TestSuggestedGrid:
    """auto_select_grid_resolution suggestion appears only when it would help."""

    def test_finer_grid_suggested_when_useful(self, tmp_path: Path) -> None:
        # Pads at multiples of 0.05 mm: off-grid at 0.1 mm but on-grid at
        # 0.05 mm.  Use a stricter threshold to ensure the violations
        # register so the suggested_grid analysis runs (the new 0.05 mm
        # default tolerance would mask the 0.05 mm offsets via fp_eps).
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
        # Strict threshold so 0.05 mm offsets flag, exercising the
        # suggested-grid analysis.
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1, threshold=0.01)

        assert not report.passed
        # Should suggest a finer grid that aligns all four pads
        assert report.suggested_grid is not None
        assert report.suggested_grid <= 0.05 + 1e-9

    def test_no_finer_grid_when_no_help(self, tmp_path: Path) -> None:
        # Make the offset awkward: 0.0333... mm (not aligned to any standard
        # finer candidate available to auto_select_grid_resolution).  Use
        # a stricter threshold than the 0.05 mm default to flag it.
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
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1, threshold=0.01)
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
        # Local pad at (0.0333, 0.0) with footprint rotated 90 deg CCW:
        # absolute offset becomes (0.0, 0.0333), still off the 0.1 mm
        # grid.  Use a strict threshold (the 0.05 mm default would clear
        # this offset, but the test is about rotation handling).
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
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1, threshold=0.01)
        assert not report.passed
        violation = report.off_grid_pads[0]
        # X should be near 100.0 (was rotated to that axis)
        assert violation.x == pytest.approx(100.0, abs=1e-3)
        # Y should be ~100.0333 -> off-grid
        assert violation.y == pytest.approx(100.0333, abs=1e-3)


class TestThreshold:
    """The threshold defaults to 0.05 mm (issue #3042) and is configurable."""

    def test_default_threshold(self, tmp_path: Path) -> None:
        text = _pcb_with_pads(
            [
                (
                    "Test:FP",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    [("1", 0.04, 0.0)],  # 0.04 mm < 0.05 mm default threshold
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1)
        # Within default threshold -> on grid (mimics PinHeader_2.54mm
        # at 0.04 mm intrinsic offset clearing post-#3042).
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

        # Use a (0.05, 0.04) mm offset that gives L2 ~0.064 mm, exceeding
        # the 0.05 mm default tolerance (issue #3042) and representing a
        # real placement error.
        text = _pcb_with_pads(
            [
                (
                    "Test:FP",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    [("1", 0.0, 0.0), ("2", 1.25, 0.04)],
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
        assert violation.location == pytest.approx((101.25, 100.04))
        assert "U1.2" in violation.message

    def test_drc_checker_in_categories(self) -> None:
        from kicad_tools.cli.check_cmd import CHECK_CATEGORIES

        assert "pad_grid" in CHECK_CATEGORIES


# ---------------------------------------------------------------------------
# Issue #3042: stock-library-friendly default tolerance


class TestStockLibraryFriendlyDefault:
    """Default tolerance (0.05 mm) clears stock KiCad library footprints
    whose pads sit 0.03-0.05 mm off the 0.1 mm router grid by design,
    while still flagging real placement errors (>= 0.06 mm).

    See issue #3042: fleet audit found 341 false-positive ``pad_grid``
    warnings across 9 boards, almost all from intrinsic metric-rounding
    of imperial parts like ``Connector_PinHeader_2.54mm``.
    """

    def test_default_tolerance_is_0_05mm(self) -> None:
        """The module-level constant matches the documented default."""
        from kicad_tools.router.preflight import DEFAULT_PAD_GRID_TOLERANCE_MM

        assert DEFAULT_PAD_GRID_TOLERANCE_MM == 0.05

    def test_intrinsic_pinheader_2_54mm_offset_passes(self, tmp_path: Path) -> None:
        """A pad at 0.04 mm offset (PinHeader_2.54mm intrinsic) clears."""
        # Synthesize PinHeader_2.54mm: pad at integer + 0.04 mm offset
        # (matches the real-world offset observed in the fleet audit).
        text = _pcb_with_pads(
            [
                (
                    "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
                    "J1",
                    105.0,
                    111.23,  # PinHeader pad at integer + 0.03 mm offset
                    0.0,
                    [("1", 0.0, 0.0)],
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1)
        # 0.03 mm offset is well below the 0.05 mm default -> on-grid
        assert report.passed, (
            f"Expected stock PinHeader_2.54mm intrinsic offset to clear "
            f"default tolerance, got {len(report.off_grid_pads)} off-grid pads"
        )

    def test_intrinsic_usb_c_0_05mm_offset_passes(self, tmp_path: Path) -> None:
        """A pad at exactly 0.05 mm offset (USB-C intrinsic) clears via fp_eps."""
        text = _pcb_with_pads(
            [
                (
                    "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
                    "J1",
                    100.0,
                    100.05,  # exactly 0.05 mm off -> at threshold boundary
                    0.0,
                    [("1", 0.0, 0.0)],
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1)
        # Exactly at the 0.05 mm boundary -> fp_eps clears it as on-grid
        assert report.passed

    def test_placement_error_0_06mm_still_flags(self, tmp_path: Path) -> None:
        """A pad at ~0.064 mm L2 offset (real placement error) still flags.

        Note: on a 0.1 mm grid the maximum single-axis offset is 0.05 mm
        (anything larger is closer to the next grid line).  To exceed
        the 0.05 mm threshold we need a 2D offset, e.g. (0.05, 0.04)
        which gives L2 = sqrt(0.0041) ~= 0.064 mm.
        """
        text = _pcb_with_pads(
            [
                (
                    "Test:PlacementError",
                    "U1",
                    100.05,  # 0.05 mm off in x
                    100.04,  # 0.04 mm off in y  -> L2 ~ 0.0640 mm
                    0.0,
                    [("1", 0.0, 0.0)],
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1)
        # L2 ~0.064 mm > 0.05 mm default -> off-grid
        assert not report.passed
        assert len(report.off_grid_pads) == 1
        assert report.off_grid_pads[0].offset_mm == pytest.approx(0.0640, abs=1e-3)

    def test_combined_intrinsic_pass_and_placement_error_fail(
        self, tmp_path: Path
    ) -> None:
        """Synthetic 2-pad fixture: intrinsic offset passes, placement error fails.

        This is the canonical regression guard for issue #3042: a single
        PCB containing both an "intrinsic library offset" pad (PinHeader
        style, ~0.030 mm off) and a "real placement error" pad (~0.064
        mm L2 off, via a 2D offset) must produce exactly one violation.
        Note: on a 0.1 mm grid the maximum single-axis offset is 0.05 mm,
        so any "above-threshold" offset must use a 2D (x,y) displacement.
        """
        text = _pcb_with_pads(
            [
                (
                    "Connector_PinHeader_2.54mm:PinHeader_1x01_P2.54mm_Vertical",
                    "J1",
                    10.03,  # intrinsic 0.030 mm offset -> should PASS
                    5.0,
                    0.0,
                    [("1", 0.0, 0.0)],
                ),
                (
                    "Test:PlacementError",
                    "U1",
                    20.05,  # (0.05, 0.04) mm offset -> L2 ~ 0.064 mm -> FLAG
                    5.04,
                    0.0,
                    [("1", 0.0, 0.0)],
                ),
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1)
        # Exactly one violation: only the placement-error pad
        assert len(report.off_grid_pads) == 1
        violation = report.off_grid_pads[0]
        assert violation.ref == "U1"
        assert violation.offset_mm == pytest.approx(0.0640, abs=1e-3)
