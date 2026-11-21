"""The --drc-aware referee sees mask_to_copper regressions (issue #9748).

``optimize-traces --drc-aware --mfr jlcpcb`` used to re-angle mask-clean pad
escapes into ``mask_to_copper`` errors while its summary reported ``DRC
errors: 0 -> 0 (no regressions)``: the in-process referee merged no mask
check at all, and the pre-#9748 pad-escape guard missed the off-axis entry
and the clearance-band pass-through.

Covered here:

* :class:`MaskExposureScreen` -- the referee's conservative count over the
  pure mask snapshot, mirroring the native error classes (an off-axis
  own-pad entry; a same-net track inside the mask-clearance band of another
  pad's opening) and excusing clean perpendicular and 45-degree entries.
* The referee's ``mask_to_copper`` category and the per-net rollback gate
  (growth fires even at a flat total, like a pad-clearance swap).
* The ``DRC errors:`` summary line, which must never claim bare ``no
  regressions`` while the mask rule went unevaluated.

The guard-side cases (on-axis entry not re-angled; band keep-out) live in
``tests/test_optimize_pad_entry_jog.py`` (TestMaskEscapeGuard9748).  The
native ``kct check --mfr`` comparison on the 40-module recipe board needs a
qualified KiCad runtime and is tracked as the issue's manual test plan.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.router.optimizer.config import OptimizationConfig, OptimizationStats
from kicad_tools.router.optimizer.mask_screen import (
    MaskExposureScreen,
    mask_exposure_screen_for_manufacturer,
)
from kicad_tools.router.optimizer.pcb import (
    _pad_violations_grew,
    _run_drc_error_count_by_category,
    optimize_pcb,
    parse_segments,
)

JLCPCB_CLEARANCE = 0.09  # the cited interim JLCDFM policy floor (epic #8838)


def _board(*segments: str, chamfered: bool = False) -> str:
    """A 2-pad board the pure mask snapshot can model completely.

    Pad A1 at (100, 50): a flat 1.2 x 0.6 SMD rect (half extents 0.6/0.3).
    Pad B2 at (110, 50): a 1.2 mm through-hole circle on both sides.  Board
    mask expansion is 0, so each opening is the pad copper itself grown by
    nothing, and every margin-0 escape geometry below is exact.
    """
    chamfer = "\t\t\t(chamfer top_left)" if chamfered else ""
    return (
        "(kicad_pcb\n"
        "\t(version 20240108)\n"
        '\t(generator "test")\n'
        '\t(generator_version "8.0")\n'
        "\t(general (thickness 1.6) (legacy_teardrops no))\n"
        '\t(paper "A4")\n'
        "\t(layers\n"
        '\t\t(0 "F.Cu" signal)\n'
        '\t\t(31 "B.Cu" signal)\n'
        '\t\t(44 "Edge.Cuts" user)\n'
        "\t)\n"
        "\t(setup (pad_to_mask_clearance 0))\n"
        '\t(net 0 "")\n'
        '\t(net 1 "N1")\n'
        '\t(net 2 "N2")\n'
        '\t(footprint "FLAT" (layer "F.Cu") (at 100 50)\n'
        '\t\t(uuid "fp-a")\n'
        '\t\t(pad "1" smd rect (at 0 0) (size 1.2 0.6) (layers "F.Cu" "F.Paste" "F.Mask")'
        f"{chamfer}\n"
        '\t\t\t(uuid "pad-a1") (net 1 "N1"))\n'
        "\t)\n"
        '\t(footprint "TH" (layer "F.Cu") (at 110 50)\n'
        '\t\t(uuid "fp-b")\n'
        '\t\t(pad "2" thru_hole circle (at 0 0) (size 1.2 1.2) (drill 0.6)\n'
        '\t\t\t(layers "*.Cu" "*.Mask") (uuid "pad-b2") (net 1 "N1"))\n'
        "\t)\n" + "".join(f"\t{seg}\n" for seg in segments) + ")\n"
    )


def _seg(sx: float, sy: float, ex: float, ey: float, uuid: str = "s1") -> str:
    return (
        f"(segment (start {sx:.4f} {sy:.4f}) (end {ex:.4f} {ey:.4f}) "
        f'(width 0.25) (layer "F.Cu") (net 1) (uuid "{uuid}"))'
    )


# Geometries against pad A1's opening (x in [99.4, 100.6], y in [49.7, 50.3]):
# perpendicular on-axis entry (clean), 45-degree entry (the allowance
# boundary, clean) and a ~31-degree re-angle through the pad centre (the
# issue's own-pad-entry leak: ends in the copper, crosses the opening edge
# far too shallowly).
ON_AXIS = _seg(97.0, 50.0, 100.2, 50.0)
DIAG_45 = _seg(98.0, 52.3, 100.05, 50.25)
OFF_AXIS = _seg(99.1, 49.46, 100.3, 50.18)  # slope 0.6 through the centre
# A same-net pass-through 0.05 mm from the through-hole pad's opening
# (circle r=0.6 at (110, 50)): the capsule's near edge sits at y = 50.65.
BAND_PASS_THROUGH = _seg(105.0, 50.775, 115.0, 50.775)
FAR_PASS_THROUGH = _seg(105.0, 51.2, 115.0, 51.2)


def _count(board: str, tmp_path: Path, name: str) -> int | None:
    from kicad_tools.schema.pcb import PCB

    path = tmp_path / f"{name}.kicad_pcb"
    path.write_text(board)
    return MaskExposureScreen(JLCPCB_CLEARANCE).count(path, PCB.load(str(path)))


class TestMaskExposureScreen:
    def test_clean_perpendicular_entry_is_not_counted(self, tmp_path: Path):
        assert _count(_board(ON_AXIS), tmp_path, "clean") == 0

    def test_clean_45_degree_entry_is_not_counted(self, tmp_path: Path):
        assert _count(_board(DIAG_45), tmp_path, "diag") == 0

    def test_off_axis_own_pad_entry_is_counted(self, tmp_path: Path):
        """The re-angled last leg: ends in the pad copper and runs past the
        centre, but its ~31-degree crossing leaves beside-pad copper beyond
        the one-clean-entry allowance (``same-net own-pad entry``)."""
        assert _count(_board(OFF_AXIS), tmp_path, "off") == 1

    def test_same_net_pass_through_in_clearance_band_is_counted(self, tmp_path: Path):
        """The disjoint class: 0.05 mm from the opening, inside the 0.09 mm
        policy clearance band, terminating nowhere near the pad."""
        assert _count(_board(BAND_PASS_THROUGH), tmp_path, "band") >= 1

    def test_far_pass_through_is_not_counted(self, tmp_path: Path):
        assert _count(_board(FAR_PASS_THROUGH), tmp_path, "far") == 0

    def test_incomplete_snapshot_is_not_evaluated_and_latches(self, tmp_path: Path):
        """Unsupported pad geometry (a chamfer) must yield no count at all,
        and the latch keeps later boards unevaluated too, so a run can never
        mix screened and unscreened totals."""
        from kicad_tools.schema.pcb import PCB

        path = tmp_path / "chamfer.kicad_pcb"
        path.write_text(_board(ON_AXIS, chamfered=True))
        pcb = PCB.load(str(path))
        screen = MaskExposureScreen(JLCPCB_CLEARANCE)
        assert screen.count(path, pcb) is None
        assert screen.status == "not_evaluated"
        clean = tmp_path / "clean.kicad_pcb"
        clean.write_text(_board(ON_AXIS))
        assert screen.count(clean, PCB.load(str(clean))) is None
        assert screen.status == "not_evaluated"

    def test_screen_policy_matches_kct_check_default(self):
        """The screen reuses the exact policy ``kct check --mfr`` engages, so
        the referee's floor and the reported check cannot drift."""
        screen = mask_exposure_screen_for_manufacturer("JLCPCB-Color")
        assert screen is not None and screen.clearance_mm == JLCPCB_CLEARANCE
        assert mask_exposure_screen_for_manufacturer("other-fab") is None
        assert mask_exposure_screen_for_manufacturer(None) is None

    def test_screen_constants_match_native_mask_rules(self):
        """The band/excess math must track the native measurement rules."""
        from kicad_tools.router.optimizer import mask_screen
        from kicad_tools.validate import mask_copper

        assert mask_screen._ENTRY_CROSSING_FACTOR == mask_copper._ENTRY_CROSSING_FACTOR
        assert mask_screen._AREA_EPSILON_MM2 == mask_copper._AREA_EPSILON_MM2


class TestRefereeCategory:
    def _empty_checker(self, monkeypatch):
        """Neutralise the DRC checks so counts isolate the mask screen."""
        import kicad_tools.validate as validate

        class _Res:
            errors: list = []

            def merge(self, other):
                return None

        class _Checker:
            def __init__(self, *a, **k):
                pass

            def check_clearances(self):
                return _Res()

            def check_dimensions(self):
                return _Res()

            def check_segment_zone_clearances(self):
                return _Res()

            def check_via_zone_clearances(self):
                return _Res()

        monkeypatch.setattr(validate, "DRCChecker", _Checker)

    def test_referee_counts_mask_to_copper(self, tmp_path: Path, monkeypatch):
        self._empty_checker(monkeypatch)
        clean = _run_drc_error_count_by_category(
            _board(ON_AXIS),
            manufacturer="jlcpcb",
            layers=2,
            copper_oz=1.0,
            mask_screen=mask_exposure_screen_for_manufacturer("jlcpcb"),
        )
        assert clean.get("mask_to_copper", 0) == 0
        dirty = _run_drc_error_count_by_category(
            _board(BAND_PASS_THROUGH),
            manufacturer="jlcpcb",
            layers=2,
            copper_oz=1.0,
            mask_screen=mask_exposure_screen_for_manufacturer("jlcpcb"),
        )
        assert dirty["mask_to_copper"] >= 1
        assert dirty["__total__"] >= 1

    def test_referee_without_screen_keeps_pre_9748_counts(self, tmp_path: Path, monkeypatch):
        self._empty_checker(monkeypatch)
        counts = _run_drc_error_count_by_category(
            _board(BAND_PASS_THROUGH),
            manufacturer="jlcpcb",
            layers=2,
            copper_oz=1.0,
        )
        assert "mask_to_copper" not in counts
        assert counts["__total__"] == 0

    def test_mask_to_copper_growth_triggers_rollback(self, tmp_path: Path, monkeypatch):
        """AC: a ``mask_to_copper`` growth fires the per-net rollback even
        at a flat total, exactly like a pad-clearance category swap."""
        self._empty_checker(monkeypatch)
        src = tmp_path / "in.kicad_pcb"
        out = tmp_path / "out.kicad_pcb"
        far_n2 = _seg(105.0, 51.2, 115.0, 51.2, uuid="s2").replace("(net 1)", "(net 2)")
        src.write_text(_board(ON_AXIS, far_n2))

        # Move net 2's track from far away into pad B2's clearance band.
        def hug_the_opening(segs):
            moved = []
            for s in segs:
                if s.net_name == "N2":
                    moved.append(type(s)(**{**s.__dict__, "y1": 50.775, "y2": 50.775}))
                else:
                    moved.append(s)
            return moved

        cfg = OptimizationConfig(drc_aware=True, drc_manufacturer="jlcpcb")
        stats = optimize_pcb(str(src), str(out), hug_the_opening, cfg)
        assert stats.drc_mask_screen == "screened"
        assert stats.nets_rolled_back == 1
        assert stats.drc_errors_after == stats.drc_errors_before == 0
        rows = {row.net: row for row in stats.per_net}
        assert rows["N2"].new_violations == {"mask_to_copper": 1}
        final = parse_segments(out.read_text())
        assert final["N2"][0].y1 == 51.2  # restored

    def test_growth_detected_at_flat_total(self):
        before = {"__total__": 5, "clearance_segment_segment": 5}
        after = {"__total__": 5, "clearance_segment_segment": 4, "mask_to_copper": 1}
        assert _pad_violations_grew(before, after)

    def test_clean_optimize_reports_screened_and_no_rollback(self, tmp_path: Path, monkeypatch):
        self._empty_checker(monkeypatch)
        src = tmp_path / "in.kicad_pcb"
        out = tmp_path / "out.kicad_pcb"
        src.write_text(_board(ON_AXIS, FAR_PASS_THROUGH))
        cfg = OptimizationConfig(drc_aware=True, drc_manufacturer="jlcpcb")
        stats = optimize_pcb(str(src), str(out), lambda segs: segs, cfg)
        assert stats.drc_mask_screen == "screened"
        assert stats.nets_rolled_back == 0

    def test_unsupported_geometry_reports_not_evaluated(self, tmp_path: Path, monkeypatch):
        self._empty_checker(monkeypatch)
        src = tmp_path / "in.kicad_pcb"
        out = tmp_path / "out.kicad_pcb"
        src.write_text(_board(ON_AXIS, chamfered=True))
        cfg = OptimizationConfig(drc_aware=True, drc_manufacturer="jlcpcb")
        stats = optimize_pcb(str(src), str(out), lambda segs: segs, cfg)
        assert stats.drc_mask_screen == "not_evaluated"


class TestSummaryLine:
    """The ``DRC errors:`` suffix (Issue #9748: never a bare ``no
    regressions`` while the mask rule went unevaluated)."""

    def _stats(self, screen: str) -> OptimizationStats:
        stats = OptimizationStats(drc_errors_before=3, drc_errors_after=3)
        stats.drc_mask_screen = screen
        return stats

    def test_off_keeps_bare_no_regressions(self):
        from kicad_tools.cli.optimize_cmd import _drc_summary_suffix

        assert _drc_summary_suffix(self._stats("off"), None) == "(no regressions)"

    def test_not_evaluated_says_so(self):
        from kicad_tools.cli.optimize_cmd import _drc_summary_suffix

        suffix = _drc_summary_suffix(self._stats("not_evaluated"), None)
        assert suffix == "(no regressions; mask_to_copper not evaluated)"

    def test_screened_mentions_the_screen(self):
        from kicad_tools.cli.optimize_cmd import _drc_summary_suffix

        suffix = _drc_summary_suffix(self._stats("screened"), None)
        assert suffix == "(no regressions; incl. mask_to_copper screen)"

    def test_not_evaluated_with_rollbacks(self):
        from kicad_tools.cli.optimize_cmd import _drc_summary_suffix

        stats = self._stats("not_evaluated")
        stats.nets_rolled_back = 2
        suffix = _drc_summary_suffix(stats, None)
        assert "2 nets rolled back" in suffix
        assert "mask_to_copper not evaluated" in suffix
