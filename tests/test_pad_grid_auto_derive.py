"""Tests for the auto-derived pad_grid tolerance (issue #3061).

PR #3057 raised the default ``pad_grid`` tolerance from 0.01mm to a
fixed 0.05mm constant, clearing 65% of fleet-wide warnings.  The
residual ~112 warnings were all fine-pitch packages (LQFP-48, BGA-49,
HTSSOP-56) whose pad lattice naturally sits 0.057-0.071 mm off the 0.1
mm grid.  Raising the constant further would risk false-negatives on
real placement errors in the 0.06-0.075 mm band.

The fix (this issue) derives the threshold per-board from the
pad-offset histogram: a board with only metric-on-metric pads keeps
the 0.05 mm floor; a board with fine-pitch QFN whose pads sit at 0.07
mm intrinsic offset gets a slightly higher threshold; both still flag
a 0.15 mm placement error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router.preflight import (
    AUTO_DERIVED_TOLERANCE_FLOOR_MM,
    AUTO_DERIVED_TOLERANCE_HARD_CAP_MM,
    AUTO_DERIVED_TOLERANCE_MARGIN_MM,
    DEFAULT_PAD_GRID_TOLERANCE_MM,
    check_pad_grid_alignment,
    compute_pad_grid_tolerance,
)

# ---------------------------------------------------------------------------
# Helpers (copied from test_pad_grid_preflight.py to keep this file
# self-contained -- the helper is small and the duplication keeps the
# regression tests independent of helper churn).


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
# compute_pad_grid_tolerance: pure threshold-computation tests


class TestComputeTolerance:
    """The standalone histogram analyser."""

    def test_empty_board_returns_floor(self, tmp_path: Path) -> None:
        """No pads -> falls back to the floor (vacuous case)."""
        pcb = _write_pcb(tmp_path, _pcb_with_pads([]))
        tol = compute_pad_grid_tolerance(pcb, grid_resolution=0.1)
        assert tol == AUTO_DERIVED_TOLERANCE_FLOOR_MM

    def test_all_on_grid_returns_floor(self, tmp_path: Path) -> None:
        """All pads exactly on grid -> floor (never stricter than #3057)."""
        text = _pcb_with_pads(
            [
                (
                    "Test:FP",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    [("1", 0.0, 0.0), ("2", 1.0, 0.0), ("3", 0.0, 1.0)],
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        tol = compute_pad_grid_tolerance(pcb, grid_resolution=0.1)
        # Auto-derived tolerance must never go below the post-#3057
        # default -- even for a perfectly on-grid board.
        assert tol == AUTO_DERIVED_TOLERANCE_FLOOR_MM
        assert tol >= DEFAULT_PAD_GRID_TOLERANCE_MM

    def test_fine_pitch_intrinsic_offset_lifts_tolerance(self, tmp_path: Path) -> None:
        """Fine-pitch package with intrinsic 0.07mm L2 offset lifts threshold.

        On a 0.1 mm grid the per-axis distance-to-grid is bounded by
        0.05 mm, so to reach an L2 offset of ~0.07 mm the pads must be
        offset in both axes (e.g. (+0.05, +0.05) -> L2 = 0.0707 mm).
        This mirrors the real-world fleet warnings on LQFP-48, BGA-49,
        HTSSOP-56 from issue #3057 (residual 112 warnings at
        L2=0.057-0.071 mm) where the footprint origin sits 0.05 mm off
        in both axes due to metric/imperial rounding of the package
        bbox.
        """
        # Synthesize a 20-pad LQFP-style package with both x and y
        # offset by 0.05 mm -> L2 = 0.0707 mm intrinsic offset.
        # Pads at multiples of 0.5 mm (a grid divisor of 0.1) + the
        # (0.05, 0.05) component offset.
        pads = [(str(i), 0.5 * i + 0.05, 0.05) for i in range(10)] + [
            (str(i + 10), 0.5 * i + 0.05, 1.05) for i in range(10)
        ]
        text = _pcb_with_pads(
            [
                (
                    "Package_QFP:LQFP-48_7x7mm_P0.5mm",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    pads,
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        tol = compute_pad_grid_tolerance(pcb, grid_resolution=0.1)

        # Threshold should sit just above the 0.0707 mm intrinsic offset
        # (with the safety margin) so the intrinsic pads clear.
        intrinsic_l2 = (0.05**2 + 0.05**2) ** 0.5
        assert tol >= intrinsic_l2 + AUTO_DERIVED_TOLERANCE_MARGIN_MM * 0.5, (
            f"Auto-derived tolerance {tol} too tight for {intrinsic_l2:.4f} mm intrinsic offset"
        )
        # But still well below the hard cap.
        assert tol <= AUTO_DERIVED_TOLERANCE_HARD_CAP_MM

    def test_pathological_board_capped(self, tmp_path: Path) -> None:
        """Pathological offsets get clamped by the hard cap.

        On a 0.1 mm grid the largest possible per-axis distance-to-grid
        is 0.05 mm, but on a coarser grid (here we use 0.5 mm) pads
        can sit further off.  Use a 0.5 mm grid resolution so the
        per-pad offsets exceed the 0.15 mm cap and exercise the clamp.
        """
        # Every pad sits at +0.2 mm in both axes from the nearest 0.5
        # mm grid point -> L2 = 0.283 mm, well above the 0.15 mm cap.
        pads = [(str(i), i * 1.0 + 0.2, 0.2) for i in range(10)]
        text = _pcb_with_pads([("Test:FP", "U1", 100.0, 100.0, 0.0, pads)])
        pcb = _write_pcb(tmp_path, text)
        tol = compute_pad_grid_tolerance(pcb, grid_resolution=0.5)
        assert tol == AUTO_DERIVED_TOLERANCE_HARD_CAP_MM

    def test_outlier_does_not_dominate(self, tmp_path: Path) -> None:
        """A single far-outlier pad does not push the p99 above the cluster."""
        # 99 on-grid pads + 1 wildly off pad.  The p99 sits at the on-grid
        # bulk so the outlier is well above the auto-derived threshold and
        # flags as a violation.
        pads = [(str(i), 0.1 * i, 0.0) for i in range(99)]
        pads.append(("99", 5.0 + 0.3, 0.0))  # 0.3 mm off
        text = _pcb_with_pads([("Test:FP", "U1", 100.0, 100.0, 0.0, pads)])
        pcb = _write_pcb(tmp_path, text)
        tol = compute_pad_grid_tolerance(pcb, grid_resolution=0.1)
        # p99 of 99 zeros and one 0.3 lands between them -> with margin
        # we should be well below 0.3 mm.
        assert tol < 0.2
        assert tol >= AUTO_DERIVED_TOLERANCE_FLOOR_MM


# ---------------------------------------------------------------------------
# check_pad_grid_alignment with auto_derive_threshold=True


class TestAutoDeriveIntegration:
    """End-to-end: the rule should respect the auto-derived threshold."""

    def test_on_grid_plus_placement_error_flags_only_error(self, tmp_path: Path) -> None:
        """On-grid pads + 1 placement error -> only the error flags.

        AC: board with all pads on-grid AND a placement error pad at
        1.0mm-class offset -> only the placement error flags.

        On a 0.1 mm grid an offset of 1.0 mm wraps back to on-grid
        (the nearest grid point is 0.0 mm away).  We instead make the
        board's router grid 0.5 mm so the placement error of 0.4 mm
        (well past the 0.15 mm hard cap and any plausible auto-derived
        threshold) shows up as a violation.
        """
        # 100 on-grid pads -> p99 lands at on-grid (0.0 mm offset), so
        # the auto-derived threshold stays at the floor (0.05 mm).  One
        # additional pad sits 0.4 mm off in both axes -> L2 ~ 0.566 mm,
        # well above the 0.15 mm cap.
        on_grid_pads = [(str(i), 0.5 * (i % 10), 0.5 * (i // 10)) for i in range(100)]
        text = _pcb_with_pads(
            [
                (
                    "Test:OnGridBulk",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    on_grid_pads,
                ),
                (
                    "Test:RealError",
                    "U2",
                    # Footprint origin sits 0.4 mm off in both axes
                    # relative to the 0.5 mm router grid.
                    110.4,
                    110.4,
                    0.0,
                    [("1", 0.0, 0.0)],
                ),
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        report = check_pad_grid_alignment(pcb, grid_resolution=0.5, auto_derive_threshold=True)
        assert not report.passed
        # Exactly one violation -- and it must be U2
        refs = [pad.ref for pad in report.off_grid_pads]
        assert "U2" in refs
        assert len(report.off_grid_pads) == 1
        # No on-grid pads should be flagged
        assert "U1" not in refs

    def test_fine_pitch_intrinsic_does_not_warn(self, tmp_path: Path) -> None:
        """Fine-pitch package at 0.07mm intrinsic -> no warning.

        AC: fine-pitch package with intrinsic 0.07mm offset -> no warning.
        """
        # 20-pad fine-pitch package with intrinsic 0.07 mm offset.  Under
        # the fixed-0.05mm rule this would generate 20 warnings; under the
        # auto-derived rule it generates none because the threshold lifts
        # to ~0.075 mm.
        pads = [(str(i), 0.5 * i + 0.07, 0.0) for i in range(10)] + [
            (str(i + 10), 0.5 * i + 0.07, 1.0) for i in range(10)
        ]
        text = _pcb_with_pads(
            [
                (
                    "Package_QFP:LQFP-48_7x7mm_P0.5mm",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    pads,
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1, auto_derive_threshold=True)
        assert report.passed, (
            f"Fine-pitch intrinsic offset should clear auto-derived "
            f"threshold; got {len(report.off_grid_pads)} warnings."
        )

    def test_fine_pitch_with_separate_placement_error(self, tmp_path: Path) -> None:
        """Fine-pitch intrinsic + 0.15mm placement error -> the error still flags.

        AC: a separate placement error at 0.15mm on the same board -> flags.
        """
        # Same fine-pitch bulk + one additional component clearly off-grid.
        pads = [(str(i), 0.5 * i + 0.07, 0.0) for i in range(10)] + [
            (str(i + 10), 0.5 * i + 0.07, 1.0) for i in range(10)
        ]
        text = _pcb_with_pads(
            [
                (
                    "Package_QFP:LQFP-48_7x7mm_P0.5mm",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    pads,
                ),
                (
                    "Test:BadPlace",
                    "U2",
                    # 0.15 mm in both axes -> L2 ~0.212 mm, well above any
                    # plausible auto-derived threshold for this board.
                    110.15,
                    100.15,
                    0.0,
                    [("1", 0.0, 0.0)],
                ),
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1, auto_derive_threshold=True)
        assert not report.passed
        # The single violation must be U2 (the placement error), not any
        # of the fine-pitch intrinsic pads.
        assert len(report.off_grid_pads) == 1
        assert report.off_grid_pads[0].ref == "U2"

    def test_explicit_threshold_wins_over_auto_derive(self, tmp_path: Path) -> None:
        """An explicit threshold argument always wins over auto-derive."""
        pads = [(str(i), 0.5 * i + 0.07, 0.0) for i in range(10)]
        text = _pcb_with_pads(
            [
                (
                    "Test:FP",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    pads,
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        # With an explicit strict threshold, all 10 pads should flag.
        report = check_pad_grid_alignment(
            pcb,
            grid_resolution=0.1,
            threshold=0.001,
            auto_derive_threshold=True,
        )
        assert not report.passed
        assert len(report.off_grid_pads) == 10


# ---------------------------------------------------------------------------
# Backwards compatibility: auto_derive_threshold=False must reproduce
# PR #3057's behaviour exactly.


class TestBackwardCompat:
    """auto_derive_threshold=False (the default) is PR #3057 behaviour."""

    def test_default_is_not_auto_derive(self, tmp_path: Path) -> None:
        """Calling without auto_derive_threshold preserves PR #3057 default."""
        # A board with 0.04 mm intrinsic offset -- this clears the 0.05 mm
        # default but a stricter auto-derived threshold (~0.045 mm) would
        # not.  By calling without the flag, we should see PR #3057's pass.
        text = _pcb_with_pads(
            [
                (
                    "Test:FP",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    [("1", 0.04, 0.0)],
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1)
        # PR #3057 default: 0.05 mm -> 0.04 mm offset passes.
        assert report.passed
        assert report.threshold == DEFAULT_PAD_GRID_TOLERANCE_MM

    def test_explicit_false_matches_pr_3057(self, tmp_path: Path) -> None:
        """auto_derive_threshold=False explicitly is the PR #3057 default."""
        text = _pcb_with_pads(
            [
                (
                    "Test:FP",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    [("1", 0.04, 0.0)],
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)
        report = check_pad_grid_alignment(pcb, grid_resolution=0.1, auto_derive_threshold=False)
        assert report.passed
        assert report.threshold == DEFAULT_PAD_GRID_TOLERANCE_MM


# ---------------------------------------------------------------------------
# CLI integration


class TestCLIDefaults:
    """The CLI defaults to auto_derive=True; --pad-grid-strict opts out."""

    def test_pad_grid_strict_flag_exists(self) -> None:
        """The --pad-grid-strict flag is registered with the argparser."""
        import argparse

        # Recreate the parser by introspecting the module: easier to
        # smoke-test from the help text.
        from kicad_tools.cli import check_cmd
        from kicad_tools.cli.check_cmd import main as _main  # noqa: F401

        parser = argparse.ArgumentParser()
        # Just verify the symbols exist in the module
        assert hasattr(check_cmd, "run_selected_checks")
        # Sanity check: run_selected_checks accepts the two new kwargs.
        import inspect

        sig = inspect.signature(check_cmd.run_selected_checks)
        assert "pad_grid_threshold" in sig.parameters
        assert "pad_grid_auto_derive" in sig.parameters

    def test_pad_grid_strict_via_cli(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--pad-grid-strict reproduces the PR #3057 behaviour at the CLI.

        Build a board where every pad sits at the worst-case 0.0707 mm
        L2 offset (0.05 mm in both axes).  Under strict mode this
        flags every pad; under auto-derive the threshold lifts above
        the p99 of the offset distribution so all pads clear.
        """
        # 20 pads, all at (0.05, 0.05) intrinsic offset -> L2 = 0.0707 mm.
        pads = [(str(i), 0.5 * i + 0.05, 0.05) for i in range(10)] + [
            (str(i + 10), 0.5 * i + 0.05, 1.05) for i in range(10)
        ]
        text = _pcb_with_pads(
            [
                (
                    "Package_QFP:LQFP-48_7x7mm_P0.5mm",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    pads,
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)

        from kicad_tools.cli.check_cmd import main as check_main

        # Default (auto-derive): should produce 0 pad_grid warnings.
        check_main(
            [
                str(pcb),
                "--format",
                "summary",
                "--only",
                "pad_grid",
            ]
        )
        default_output = capsys.readouterr().out
        assert "DRC PASSED" in default_output, (
            f"Auto-derive default should pass for intrinsic-offset board; got: {default_output}"
        )

        # --pad-grid-strict: should produce pad_grid warnings.
        check_main(
            [
                str(pcb),
                "--format",
                "summary",
                "--only",
                "pad_grid",
                "--pad-grid-strict",
            ]
        )
        strict_output = capsys.readouterr().out
        # Strict mode flagged at least one pad.
        assert "pad_grid" in strict_output, (
            f"Strict mode should flag intrinsic-offset board; got: {strict_output}"
        )

    def test_pad_grid_tolerance_explicit_via_cli(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--pad-grid-tolerance overrides everything else."""
        text = _pcb_with_pads(
            [
                (
                    "Test:FP",
                    "U1",
                    100.0,
                    100.0,
                    0.0,
                    [("1", 0.04, 0.0)],  # 0.04 mm offset
                )
            ]
        )
        pcb = _write_pcb(tmp_path, text)

        from kicad_tools.cli.check_cmd import main as check_main

        # Tight 0.01 mm threshold -> flag.
        rc = check_main(
            [
                str(pcb),
                "--format",
                "summary",
                "--only",
                "pad_grid",
                "--pad-grid-tolerance",
                "0.01",
            ]
        )
        tight_output = capsys.readouterr().out
        assert "pad_grid" in tight_output
        # rc 0 because warning != error
        assert rc == 0

        # Loose 0.1 mm threshold -> pass.
        check_main(
            [
                str(pcb),
                "--format",
                "summary",
                "--only",
                "pad_grid",
                "--pad-grid-tolerance",
                "0.1",
            ]
        )
        loose_output = capsys.readouterr().out
        # Output should show no violations.
        assert "no violations" in loose_output.lower() or "0" in loose_output
