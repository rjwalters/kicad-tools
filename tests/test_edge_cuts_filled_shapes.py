"""Issue #9690: filled Edge.Cuts shapes are cut-outs, not just strokes.

KiCad (pinned image kicad-cli 10.0.6, black-box probed -- see the parity
sibling ``test_edge_cuts_filled_native_parity.py``) treats a *filled*
Edge.Cuts shape as a filled cut-out region: copper anywhere inside it is a
``copper_edge_clearance`` violation with actual 0.0 mm.  The probed fill
resolution for board-level shapes:

==========================  ============  ===========
encoding                    circle / rect  poly
==========================  ============  ===========
``(fill yes)`` / ``(fill solid)``  filled        filled
``(fill no)`` / ``(fill none)``    stroke-only   stroke-only
no fill token, stroke width > 0    stroke-only   stroke-only
no fill token, stroke width == 0   filled        stroke-only
==========================  ============  ===========

The "width == 0 means filled" default is KiCad's legacy convention for
circle/rect; ``gr_poly`` never defaults to filled.  A shape that resolves to
stroke-only keeps exactly the outline reading it had before this issue (rect
strokes; circle/poly contribute no stroke segments on main).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.schema.pcb import PCB, BoardGraphic
from kicad_tools.validate.rules.edge import EdgeClearanceRule
from kicad_tools.validate.violations import DRCResults

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

BOARD_TMPL = """(kicad_pcb (version 20241229) (generator pcbnew)
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (38 "B.Mask" user) (39 "F.Mask" user))
  (net 0 "")
  (net 1 "N1")
  (setup (pad_to_mask_clearance 0))
  (gr_rect (start {ox} {oy}) (end {ex} {ey}) (stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts"))
  {shape}
  (footprint "U1" (layer "F.Cu") (at {fpx} {fpy})
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu" "F.Mask")))
  {copper}
)"""

#: A bare r=20 mm circle cut-out centred on (60, 50) in sheet coordinates.
BARE_CIRCLE = '(gr_circle (center 60 50) (end 80 50) (layer "Edge.Cuts"))'
#: The same circle with an explicit unfilled token: stroke-only.
FILLNO_CIRCLE = (
    "(gr_circle (center 60 50) (end 80 50) "
    '(stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts"))'
)
#: An explicitly filled polygon cut-out covering 40..80 x 30..70.
FILLED_POLY = (
    "(gr_poly (pts (xy 40 30) (xy 80 30) (xy 80 70) (xy 40 70)) "
    '(stroke (width 0.05) (type solid)) (fill yes) (layer "Edge.Cuts"))'
)
FILLNO_POLY = (
    "(gr_poly (pts (xy 40 30) (xy 80 30) (xy 80 70) (xy 40 70)) "
    '(stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts"))'
)
#: A bare rect cut-out covering 40..80 x 30..70 (no stroke/fill token).
BARE_RECT = '(gr_rect (start 40 30) (end 80 70) (layer "Edge.Cuts"))'
FILLNO_RECT = (
    "(gr_rect (start 40 30) (end 80 70) "
    '(stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts"))'
)

DEEP_INTERIOR_TRACK = '(segment (start 60 50) (end 62 50) (width 0.25) (layer "F.Cu") (net 1))'
FAR_EXTERIOR_TRACK = '(segment (start 20 10) (end 22 10) (width 0.25) (layer "F.Cu") (net 1))'
#: Both endpoints outside the r=20 circle at (60, 50), midpoint inside.
STRADDLING_TRACK = '(segment (start 35 50) (end 78 50) (width 0.25) (layer "F.Cu") (net 1))'
INTERIOR_VIA = '(via (at 61 51) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))'
EXTERIOR_VIA = '(via (at 21 11) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))'
#: A zone whose fill polygon has exactly one vertex inside the circle cut-out.
INTERIOR_ZONE = (
    '(zone (net 0) (net_name "") (layer "F.Cu") (polygon '
    "(pts (xy 61 51) (xy 90 10) (xy 95 10) (xy 95 15))))"
)
EXTERIOR_ZONE = (
    '(zone (net 0) (net_name "") (layer "F.Cu") (polygon '
    "(pts (xy 10 5) (xy 13 5) (xy 13 8) (xy 10 8))))"
)


def _write_board(
    tmp_path: Path, shape: str, copper: str = "", *, origin=(0, 0), fp=(60, 50)
) -> PCB:
    ox, oy = origin
    ex, ey = ox + 100, oy + 100
    path = tmp_path / f"board_{abs(hash((shape, copper, origin, fp)))}.kicad_pcb"
    path.write_text(
        BOARD_TMPL.format(
            ox=ox, oy=oy, ex=ex, ey=ey, shape=shape, copper=copper, fpx=fp[0], fpy=fp[1]
        )
    )
    return PCB.load(path)


def _design_rules(copper_edge: float = 0.3, hole_edge: float = 0.5) -> SimpleNamespace:
    return SimpleNamespace(
        min_copper_to_edge_mm=copper_edge,
        min_hole_to_edge_mm=hole_edge,
    )


def _run_rule(pcb: PCB) -> DRCResults:
    results = EdgeClearanceRule().check(pcb, _design_rules())
    return results


def _rule_ids(results: DRCResults) -> list[str]:
    return [v.rule_id for v in results.errors]


# ---------------------------------------------------------------------------
# Fill resolution: BoardGraphic.is_filled_cutout
# ---------------------------------------------------------------------------


def _graphic(
    graphic_type: str,
    *,
    fill: str = "",
    stroke_width: float = 0.0,
    legacy_width: float | None = None,
) -> BoardGraphic:
    return BoardGraphic(
        graphic_type=graphic_type,
        layer="Edge.Cuts",
        stroke_width=stroke_width,
        fill=fill,
        legacy_width=legacy_width,
    )


class TestFillResolution:
    """The probed KiCad fill matrix, as ``is_filled_cutout`` resolutions."""

    @pytest.mark.parametrize("graphic_type", ["circle", "rect"])
    @pytest.mark.parametrize("token", ["yes", "solid"])
    def test_fill_token_yes_is_filled(self, graphic_type, token):
        assert _graphic(graphic_type, fill=token, stroke_width=0.05).is_filled_cutout

    @pytest.mark.parametrize("graphic_type", ["circle", "rect", "poly"])
    @pytest.mark.parametrize("token", ["no", "none"])
    def test_fill_token_no_is_stroke_only(self, graphic_type, token):
        assert not _graphic(graphic_type, fill=token, stroke_width=0.05).is_filled_cutout

    @pytest.mark.parametrize("graphic_type", ["circle", "rect"])
    def test_missing_token_with_zero_width_is_filled(self, graphic_type):
        assert _graphic(graphic_type, stroke_width=0.0).is_filled_cutout

    @pytest.mark.parametrize("graphic_type", ["circle", "rect"])
    def test_missing_token_with_positive_stroke_is_stroke_only(self, graphic_type):
        assert not _graphic(graphic_type, stroke_width=0.05).is_filled_cutout

    @pytest.mark.parametrize("graphic_type", ["circle", "rect"])
    def test_missing_token_with_positive_legacy_width_is_stroke_only(self, graphic_type):
        assert not _graphic(graphic_type, legacy_width=0.05).is_filled_cutout

    @pytest.mark.parametrize("graphic_type", ["circle", "rect"])
    def test_missing_token_with_zero_legacy_width_is_filled(self, graphic_type):
        assert _graphic(graphic_type, legacy_width=0.0).is_filled_cutout

    @pytest.mark.parametrize("stroke_width", [0.0, 0.05])
    def test_poly_never_defaults_to_filled(self, stroke_width):
        assert not _graphic("poly", stroke_width=stroke_width).is_filled_cutout
        assert not _graphic("poly", legacy_width=0.0).is_filled_cutout

    def test_fill_token_wins_over_width(self):
        # (fill yes) with a positive stroke width is still filled; the
        # legacy (width 0.05) (fill yes) probe agrees.
        assert _graphic("circle", fill="yes", stroke_width=0.05).is_filled_cutout
        assert not _graphic("circle", fill="no", stroke_width=0.0).is_filled_cutout


class TestLegacyWidthParsing:
    """A pre-KiCad-6 top-level ``(width N)`` feeds the width-0 default."""

    def test_legacy_width_parsed_when_no_stroke_node(self, tmp_path):
        pcb = _write_board(
            tmp_path,
            '(gr_circle (center 60 50) (end 80 50) (width 0.05) (layer "Edge.Cuts"))',
        )
        circle = next(
            g for g in pcb.graphics if g.layer == "Edge.Cuts" and g.graphic_type == "circle"
        )
        assert circle.legacy_width == pytest.approx(0.05)
        assert not circle.is_filled_cutout

    def test_legacy_width_zero_is_filled_cutout(self, tmp_path):
        pcb = _write_board(
            tmp_path,
            '(gr_circle (center 60 50) (end 80 50) (width 0) (layer "Edge.Cuts"))',
        )
        circle = next(
            g for g in pcb.graphics if g.layer == "Edge.Cuts" and g.graphic_type == "circle"
        )
        assert circle.legacy_width == pytest.approx(0.0)
        assert circle.is_filled_cutout

    def test_stroke_node_suppresses_legacy_width(self, tmp_path):
        pcb = _write_board(
            tmp_path,
            "(gr_circle (center 60 50) (end 80 50) (width 0.05) "
            '(stroke (width 0) (type solid)) (layer "Edge.Cuts"))',
        )
        circle = next(
            g for g in pcb.graphics if g.layer == "Edge.Cuts" and g.graphic_type == "circle"
        )
        assert circle.stroke_width == pytest.approx(0.0)
        assert circle.is_filled_cutout


# ---------------------------------------------------------------------------
# Region extraction: PCB.get_board_outline_filled_regions
# ---------------------------------------------------------------------------


class TestFilledRegions:
    def test_bare_circle_yields_circle_region(self, tmp_path):
        pcb = _write_board(tmp_path, BARE_CIRCLE)
        regions = pcb.get_board_outline_filled_regions()
        assert len(regions) == 1
        assert regions[0].shape_type == "circle"
        assert regions[0].center == pytest.approx((60.0, 50.0))
        assert regions[0].radius == pytest.approx(20.0)

    def test_fillno_circle_yields_no_region(self, tmp_path):
        assert _write_board(tmp_path, FILLNO_CIRCLE).get_board_outline_filled_regions() == []

    def test_bare_rect_yields_ring_region(self, tmp_path):
        regions = _write_board(tmp_path, BARE_RECT).get_board_outline_filled_regions()
        assert len(regions) == 1
        assert regions[0].shape_type == "rect"
        assert len(regions[0].ring) == 4

    def test_fillno_rect_yields_no_region(self, tmp_path):
        assert _write_board(tmp_path, FILLNO_RECT).get_board_outline_filled_regions() == []

    def test_filled_poly_yields_ring_region(self, tmp_path):
        regions = _write_board(tmp_path, FILLED_POLY).get_board_outline_filled_regions()
        assert len(regions) == 1
        assert regions[0].shape_type == "poly"
        assert len(regions[0].ring) == 4
        for got, want in zip(
            regions[0].ring,
            [(40.0, 30.0), (80.0, 30.0), (80.0, 70.0), (40.0, 70.0)],
            strict=False,
        ):
            assert got == pytest.approx(want)

    def test_bare_poly_yields_no_region(self, tmp_path):
        # The probed KiCad default: a gr_poly with no fill token is not filled.
        pcb = _write_board(
            tmp_path,
            '(gr_poly (pts (xy 40 30) (xy 80 30) (xy 80 70) (xy 40 70)) (layer "Edge.Cuts"))',
        )
        assert pcb.get_board_outline_filled_regions() == []

    def test_regions_are_board_relative(self, tmp_path):
        # Outline at (10, 20): board origin shifts every region coordinate.
        pcb = _write_board(tmp_path, BARE_CIRCLE, origin=(10, 20), copper="", fp=(60, 50))
        assert pcb.board_origin == pytest.approx((10.0, 20.0))
        regions = pcb.get_board_outline_filled_regions()
        assert regions[0].center == pytest.approx((50.0, 30.0))

    def test_zero_radius_circle_is_skipped(self, tmp_path):
        pcb = _write_board(tmp_path, '(gr_circle (center 60 50) (end 60 50) (layer "Edge.Cuts"))')
        assert pcb.get_board_outline_filled_regions() == []


# ---------------------------------------------------------------------------
# EdgeClearanceRule: copper inside a filled region is gap 0
# ---------------------------------------------------------------------------


class TestEdgeClearanceFilledCutouts:
    def test_deep_interior_track_flagged_with_zero_gap(self, tmp_path):
        pcb = _write_board(tmp_path, BARE_CIRCLE, DEEP_INTERIOR_TRACK + "\n  " + FAR_EXTERIOR_TRACK)
        results = _run_rule(pcb)
        traces = [v for v in results.errors if v.rule_id == "edge_clearance_trace"]
        assert len(traces) == 1
        assert traces[0].actual_value == pytest.approx(0.0)
        # The finding points at the interior track, not the far-exterior one.
        assert traces[0].location[0] == pytest.approx(60.0)

    def test_fillno_circle_interior_track_stays_clean(self, tmp_path):
        pcb = _write_board(
            tmp_path, FILLNO_CIRCLE, DEEP_INTERIOR_TRACK + "\n  " + FAR_EXTERIOR_TRACK
        )
        results = _run_rule(pcb)
        assert _rule_ids(results) == []

    def test_board_with_only_a_bare_circle_still_checks(self, tmp_path):
        # No outline *segments* at all -- the early "no outline" return must
        # not swallow the filled-region check (a lone circle is the issue's
        # own reproducer).
        path = tmp_path / "only_circle.kicad_pcb"
        path.write_text(
            BOARD_TMPL.format(
                ox=0,
                oy=0,
                ex=100,
                ey=100,
                shape="",
                copper=DEEP_INTERIOR_TRACK,
                fpx=60,
                fpy=50,
            ).replace(
                "(gr_rect (start 0 0) (end 100 100) "
                '(stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts"))',
                BARE_CIRCLE,
            )
        )
        pcb = PCB.load(path)
        assert pcb.get_board_outline_segments() == []
        results = _run_rule(pcb)
        assert "edge_clearance_trace" in _rule_ids(results)

    def test_straddling_track_caught_by_midpoint(self, tmp_path):
        # Both endpoints outside the circle; only the midpoint is inside.
        pcb = _write_board(tmp_path, BARE_CIRCLE, STRADDLING_TRACK + "\n  " + FAR_EXTERIOR_TRACK)
        results = _run_rule(pcb)
        traces = [v for v in results.errors if v.rule_id == "edge_clearance_trace"]
        assert len(traces) == 1
        assert traces[0].actual_value == pytest.approx(0.0)

    def test_interior_via_flagged(self, tmp_path):
        pcb = _write_board(tmp_path, BARE_CIRCLE, INTERIOR_VIA + "\n  " + EXTERIOR_VIA)
        results = _run_rule(pcb)
        vias = [v for v in results.errors if v.rule_id == "edge_clearance_via"]
        assert len(vias) == 1
        assert vias[0].actual_value == pytest.approx(0.0)
        assert vias[0].location == pytest.approx((61.0, 51.0))

    def test_pad_inside_cutout_flagged(self, tmp_path):
        # The single U1 pad sits at the circle centre (footprint at 60, 50).
        pcb = _write_board(tmp_path, BARE_CIRCLE)
        results = _run_rule(pcb)
        pads = [v for v in results.errors if v.rule_id.startswith("edge_clearance_pad")]
        assert len(pads) == 1
        assert pads[0].actual_value == pytest.approx(0.0)

    def test_zone_vertex_inside_cutout_flagged(self, tmp_path):
        pcb = _write_board(tmp_path, BARE_CIRCLE, INTERIOR_ZONE + "\n  " + EXTERIOR_ZONE)
        results = _run_rule(pcb)
        zones = [v for v in results.errors if v.rule_id == "edge_clearance_zone"]
        assert len(zones) == 1
        assert zones[0].actual_value == pytest.approx(0.0)

    def test_filled_poly_interior_track_flagged(self, tmp_path):
        pcb = _write_board(tmp_path, FILLED_POLY, DEEP_INTERIOR_TRACK + "\n  " + FAR_EXTERIOR_TRACK)
        results = _run_rule(pcb)
        assert "edge_clearance_trace" in _rule_ids(results)

    def test_fillno_poly_interior_track_stays_clean(self, tmp_path):
        pcb = _write_board(tmp_path, FILLNO_POLY, DEEP_INTERIOR_TRACK + "\n  " + FAR_EXTERIOR_TRACK)
        results = _run_rule(pcb)
        assert _rule_ids(results) == []

    def test_bare_rect_interior_pad_flagged(self, tmp_path):
        # U1 at (60, 50) is inside the bare 40..80 x 30..70 rect cut-out.
        pcb = _write_board(tmp_path, BARE_RECT)
        results = _run_rule(pcb)
        pads = [v for v in results.errors if v.rule_id.startswith("edge_clearance_pad")]
        assert len(pads) == 1
        assert pads[0].actual_value == pytest.approx(0.0)

    def test_keepout_zone_inside_cutout_not_flagged(self, tmp_path):
        keepout = INTERIOR_ZONE.replace(
            '(zone (net 0) (net_name "") (layer "F.Cu")',
            '(zone (net 0) (net_name "") (layer "F.Cu") (hatch edge 0.5) '
            "(keepout (tracks not_allowed) (vias not_allowed))",
            1,
        )
        pcb = _write_board(tmp_path, BARE_CIRCLE, keepout)
        results = _run_rule(pcb)
        assert "edge_clearance_zone" not in _rule_ids(results)


# ---------------------------------------------------------------------------
# Writer audit pin: the repo's gr_rect builder keeps an explicit (fill no)
# ---------------------------------------------------------------------------


class TestWriterAudit:
    def test_gr_rect_builder_writes_explicit_fill_no(self):
        from kicad_tools.sexp.builders import gr_rect_node

        node = gr_rect_node(0.0, 0.0, 10.0, 10.0, "Edge.Cuts")
        fill = node.find("fill")
        assert fill is not None
        assert fill.get_string(0) == "none"
