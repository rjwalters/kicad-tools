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
circle/rect; ``gr_poly`` never defaults to filled.  Independently of fill,
every closed shape's boundary is board edge: ``get_board_outline_segments``
emits a circle's tessellated ring and a poly's closed vertex chain alongside
a rect's edges (#9690 review), so copper straddling or just outside a
cut-out boundary is measured exactly as kicad-cli measures it.
"""

from __future__ import annotations

import math
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
        # A lone circle is the issue's own reproducer: its tessellated ring
        # is now the outline stroke geometry, and the filled-region check
        # must not be swallowed by any "no outline" early return.
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
        segments = pcb.get_board_outline_segments()
        # A certified ring: every segment endpoint sits on the r=20 circle.
        # The lone circle is also the whole outline, so the board origin is
        # its bbox corner and the ring is emitted board-relative.
        assert segments
        ox, oy = pcb.board_origin
        cx, cy = 60.0 - ox, 50.0 - oy
        for (x1, y1), (x2, y2) in segments:
            for x, y in ((x1, y1), (x2, y2)):
                assert math.hypot(x - cx, y - cy) == pytest.approx(20.0, abs=1e-3)
        results = _run_rule(pcb)
        assert "edge_clearance_trace" in _rule_ids(results)

    def test_straddling_track_caught_by_midpoint(self, tmp_path):
        # Both endpoints outside the circle; only the midpoint is inside.
        pcb = _write_board(tmp_path, BARE_CIRCLE, STRADDLING_TRACK + "\n  " + FAR_EXTERIOR_TRACK)
        results = _run_rule(pcb)
        traces = [v for v in results.errors if v.rule_id == "edge_clearance_trace"]
        assert len(traces) == 1
        assert traces[0].actual_value == pytest.approx(0.0)

    def test_near_outside_track_flagged_by_circle_boundary(self, tmp_path):
        # Copper 0.125 mm outside the circle's top point: every sample point
        # is outside the region, so only the boundary stroke can flag it
        # (#9690 review probe -- kicad-cli flags it).
        near = '(segment (start 59 29.75) (end 61 29.75) (width 0.25) (layer "F.Cu") (net 1))'
        pcb = _write_board(tmp_path, BARE_CIRCLE, near + "\n  " + FAR_EXTERIOR_TRACK)
        results = _run_rule(pcb)
        traces = [v for v in results.errors if v.rule_id == "edge_clearance_trace"]
        assert len(traces) == 1
        assert traces[0].actual_value == pytest.approx(0.125, abs=1e-3)

    def test_crossing_track_with_outside_samples_flagged(self, tmp_path):
        # Crosses the circle with start, end and midpoint all outside it
        # (end is 0.52 mm outside the ring): the stroke gap reports the
        # copper's penetration past the boundary (kernel signed gap, <= 0),
        # which the interior test alone would have missed (#9690 review
        # probe).
        cross = '(segment (start 5 35) (end 74 35) (width 0.25) (layer "F.Cu") (net 1))'
        pcb = _write_board(tmp_path, BARE_CIRCLE, cross + "\n  " + FAR_EXTERIOR_TRACK)
        results = _run_rule(pcb)
        traces = [v for v in results.errors if v.rule_id == "edge_clearance_trace"]
        assert len(traces) == 1
        assert traces[0].actual_value <= 0.0

    def test_fillno_circle_boundary_tracks_flagged(self, tmp_path):
        # The boundary of a stroke-only circle is board edge too: both
        # review probes stay flagged without any interior semantics
        # (kicad-cli parity for ``(fill no)``; most of #9695).
        near = '(segment (start 59 29.75) (end 61 29.75) (width 0.25) (layer "F.Cu") (net 1))'
        cross = '(segment (start 5 35) (end 74 35) (width 0.25) (layer "F.Cu") (net 1))'
        pcb = _write_board(
            tmp_path, FILLNO_CIRCLE, near + "\n  " + cross + "\n  " + FAR_EXTERIOR_TRACK
        )
        results = _run_rule(pcb)
        traces = [v for v in results.errors if v.rule_id == "edge_clearance_trace"]
        assert len(traces) == 2

    def test_fillno_poly_near_outside_track_flagged(self, tmp_path):
        near = '(segment (start 59 29.75) (end 61 29.75) (width 0.25) (layer "F.Cu") (net 1))'
        pcb = _write_board(tmp_path, FILLNO_POLY, near + "\n  " + FAR_EXTERIOR_TRACK)
        results = _run_rule(pcb)
        traces = [v for v in results.errors if v.rule_id == "edge_clearance_trace"]
        assert len(traces) == 1
        assert traces[0].actual_value == pytest.approx(0.125, abs=1e-3)

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
# Footprint-local filled cut-outs (Issue #9692)
# ---------------------------------------------------------------------------

#: Footprint template carrying one closed Edge.Cuts shape (``{fp_shape}``)
#: placed at ``(at {fpx} {fpy} {fpr})`` -- the fp-local half of the cut-out
#: feature: KiCad resolves a filled ``fp_circle``/``fp_poly``/``fp_rect``
#: exactly like its board-level siblings, but the geometry lives in the
#: footprint's own (rotated, translated) frame.
FP_BOARD_TMPL = """(kicad_pcb (version 20241229) (generator pcbnew)
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (38 "B.Mask" user) (39 "F.Mask" user))
  (net 0 "")
  (net 1 "N1")
  (setup (pad_to_mask_clearance 0))
  (gr_rect (start {ox} {oy}) (end {ex} {ey}) (stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts"))
  (footprint "CUT" (layer "{fplayer}") (at {fpx} {fpy} {fpr})
    {fp_shape}
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu" "F.Mask")))
  {copper}
)"""

FP_BARE_CIRCLE = '(fp_circle (center 0 0) (end 20 0) (layer "Edge.Cuts"))'
FP_FILLNO_CIRCLE = (
    "(fp_circle (center 0 0) (end 20 0) "
    '(stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts"))'
)
#: No fill token but a positive stroke width: KiCad's legacy convention
#: keeps it stroke-only (circle/rect fill only on a zero stroke width).
FP_STROKE_CIRCLE = (
    '(fp_circle (center 0 0) (end 20 0) (stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))'
)
#: Pre-KiCad-6 legacy ``(width N)`` token instead of a stroke node.
FP_LEGACY_WIDTH_CIRCLE = '(fp_circle (center 0 0) (end 20 0) (width 0.05) (layer "Edge.Cuts"))'
FP_LEGACY_ZERO_CIRCLE = '(fp_circle (center 0 0) (end 20 0) (width 0) (layer "Edge.Cuts"))'
#: A filled fp_poly in a footprint rotated 90 deg at (60, 50): local
#: (x, y) -> (60 + y, 50 - x), so the +-20 square lands on 40..80 x 30..70.
FP_FILLED_POLY = (
    "(fp_poly (pts (xy -20 -20) (xy 20 -20) (xy 20 20) (xy -20 20)) "
    '(stroke (width 0.05) (type solid)) (fill yes) (layer "Edge.Cuts"))'
)
FP_BARE_POLY = '(fp_poly (pts (xy -20 -20) (xy 20 -20) (xy 20 20) (xy -20 20)) (layer "Edge.Cuts"))'
#: A bare fp_rect (no stroke/fill token: the legacy filled default) in a
#: footprint rotated 90 deg at (60, 50): local (-20, -15)-(20, 15) maps to
#: the board-frame ring (45, 70), (45, 30), (75, 30), (75, 70).
FP_BARE_RECT = '(fp_rect (start -20 -15) (end 20 15) (layer "Edge.Cuts"))'
FP_FILLNO_RECT = (
    "(fp_rect (start -20 -15) (end 20 15) "
    '(stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts"))'
)


def _write_fp_board(
    tmp_path: Path,
    fp_shape: str,
    copper: str = "",
    *,
    origin: tuple[float, float] = (0, 0),
    fp: tuple[float, float] = (60, 50),
    rot: float = 0.0,
    fp_layer: str = "F.Cu",
) -> PCB:
    ox, oy = origin
    path = (
        tmp_path / f"fpboard_{abs(hash((fp_shape, copper, origin, fp, rot, fp_layer)))}.kicad_pcb"
    )
    path.write_text(
        FP_BOARD_TMPL.format(
            ox=ox,
            oy=oy,
            ex=ox + 100,
            ey=oy + 100,
            fp_shape=fp_shape,
            fpx=fp[0],
            fpy=fp[1],
            fpr=rot,
            fplayer=fp_layer,
            copper=copper,
        )
    )
    return PCB.load(path)


class TestFootprintFilledRegions:
    """``get_board_outline_filled_regions`` picks up fp-local filled shapes."""

    def test_bare_fp_circle_yields_circle_region(self, tmp_path):
        pcb = _write_fp_board(tmp_path, FP_BARE_CIRCLE)
        regions = pcb.get_board_outline_filled_regions()
        assert len(regions) == 1
        assert regions[0].shape_type == "circle"
        assert regions[0].center == pytest.approx((60.0, 50.0))
        assert regions[0].radius == pytest.approx(20.0)

    def test_fillno_fp_circle_yields_no_region(self, tmp_path):
        assert _write_fp_board(tmp_path, FP_FILLNO_CIRCLE).get_board_outline_filled_regions() == []

    def test_stroked_fp_circle_without_token_yields_no_region(self, tmp_path):
        assert _write_fp_board(tmp_path, FP_STROKE_CIRCLE).get_board_outline_filled_regions() == []

    def test_legacy_width_fp_circle_yields_no_region(self, tmp_path):
        assert (
            _write_fp_board(tmp_path, FP_LEGACY_WIDTH_CIRCLE).get_board_outline_filled_regions()
            == []
        )

    def test_legacy_zero_width_fp_circle_yields_region(self, tmp_path):
        regions = _write_fp_board(
            tmp_path, FP_LEGACY_ZERO_CIRCLE
        ).get_board_outline_filled_regions()
        assert len(regions) == 1
        assert regions[0].shape_type == "circle"

    def test_filled_fp_poly_rotated_yields_ring_region(self, tmp_path):
        pcb = _write_fp_board(tmp_path, FP_FILLED_POLY, rot=90.0)
        regions = pcb.get_board_outline_filled_regions()
        assert len(regions) == 1
        assert regions[0].shape_type == "poly"
        for got, want in zip(
            regions[0].ring,
            [(40.0, 70.0), (40.0, 30.0), (80.0, 30.0), (80.0, 70.0)],
            strict=True,
        ):
            assert got == pytest.approx(want)

    def test_bare_fp_poly_yields_no_region(self, tmp_path):
        # A poly never defaults to filled, footprint-local or not.
        assert (
            _write_fp_board(tmp_path, FP_BARE_POLY, rot=90.0).get_board_outline_filled_regions()
            == []
        )

    def test_bare_fp_rect_rotated_yields_ring_region(self, tmp_path):
        pcb = _write_fp_board(tmp_path, FP_BARE_RECT, rot=90.0)
        regions = pcb.get_board_outline_filled_regions()
        assert len(regions) == 1
        assert regions[0].shape_type == "rect"
        for got, want in zip(
            regions[0].ring,
            [(45.0, 70.0), (45.0, 30.0), (75.0, 30.0), (75.0, 70.0)],
            strict=True,
        ):
            assert got == pytest.approx(want)

    def test_fillno_fp_rect_yields_no_region(self, tmp_path):
        assert (
            _write_fp_board(tmp_path, FP_FILLNO_RECT, rot=90.0).get_board_outline_filled_regions()
            == []
        )

    def test_rotated_offset_fp_circle_region(self, tmp_path):
        # Footprint at (60, 50) rotated 30 deg with a local (5, 0) circle
        # centre: board centre (60 + 5 cos 30, 50 - 5 sin 30), radius 15
        # (rotation-invariant; the local end (20, 0) is on the ring).
        shape = '(fp_circle (center 5 0) (end 20 0) (layer "Edge.Cuts"))'
        pcb = _write_fp_board(tmp_path, shape, rot=30.0)
        regions = pcb.get_board_outline_filled_regions()
        assert len(regions) == 1
        assert regions[0].center == pytest.approx(
            (60.0 + 5.0 * math.cos(math.radians(30.0)), 50.0 - 5.0 * math.sin(math.radians(30.0)))
        )
        assert regions[0].radius == pytest.approx(15.0)

    def test_back_side_fp_circle_not_mirrored(self, tmp_path):
        # A back-side footprint's local geometry is already mirrored in the
        # file (Issue #8581): reading applies translation + rotation only,
        # so local (5, 3) lands at (65, 53) with no extra y mirror.
        shape = '(fp_circle (center 5 3) (end 20 3) (layer "Edge.Cuts"))'
        pcb = _write_fp_board(tmp_path, shape, fp_layer="B.Cu")
        regions = pcb.get_board_outline_filled_regions()
        assert len(regions) == 1
        assert regions[0].center == pytest.approx((65.0, 53.0))
        assert regions[0].radius == pytest.approx(15.0)

    def test_fp_regions_are_board_relative(self, tmp_path):
        pcb = _write_fp_board(tmp_path, FP_BARE_CIRCLE, origin=(10, 20))
        assert pcb.board_origin == pytest.approx((10.0, 20.0))
        regions = pcb.get_board_outline_filled_regions()
        assert regions[0].center == pytest.approx((50.0, 30.0))

    def test_board_and_footprint_regions_coexist(self, tmp_path):
        # A board-level cut-out plus an fp-local one: two distinct regions,
        # nothing double-counted.
        path = tmp_path / "both.kicad_pcb"
        path.write_text(
            FP_BOARD_TMPL.format(
                ox=0,
                oy=0,
                ex=100,
                ey=100,
                fp_shape=FP_BARE_CIRCLE,
                fpx=60,
                fpy=50,
                fpr=0.0,
                fplayer="F.Cu",
                copper=BARE_CIRCLE,
            )
        )
        pcb = PCB.load(path)
        regions = pcb.get_board_outline_filled_regions()
        assert len(regions) == 2
        assert all(r.shape_type == "circle" for r in regions)


class TestEdgeClearanceFootprintCutouts:
    """``EdgeClearanceRule`` reports gap 0 inside an fp-local filled shape."""

    def test_deep_interior_track_inside_fp_circle_flagged(self, tmp_path):
        pcb = _write_fp_board(
            tmp_path, FP_BARE_CIRCLE, DEEP_INTERIOR_TRACK + "\n  " + FAR_EXTERIOR_TRACK
        )
        results = _run_rule(pcb)
        traces = [v for v in results.errors if v.rule_id == "edge_clearance_trace"]
        assert len(traces) == 1
        assert traces[0].actual_value == pytest.approx(0.0)
        assert traces[0].location[0] == pytest.approx(60.0)

    def test_interior_track_inside_fillno_fp_circle_stays_clean(self, tmp_path):
        pcb = _write_fp_board(
            tmp_path, FP_FILLNO_CIRCLE, DEEP_INTERIOR_TRACK + "\n  " + FAR_EXTERIOR_TRACK
        )
        results = _run_rule(pcb)
        assert _rule_ids(results) == []

    def test_pad_inside_fp_cutout_flagged(self, tmp_path):
        # The CUT footprint's own pad sits at (60, 50), the fp circle centre.
        pcb = _write_fp_board(tmp_path, FP_BARE_CIRCLE)
        results = _run_rule(pcb)
        pads = [v for v in results.errors if v.rule_id.startswith("edge_clearance_pad")]
        assert len(pads) == 1
        assert pads[0].actual_value == pytest.approx(0.0)

    def test_interior_track_inside_rotated_fp_poly_flagged(self, tmp_path):
        pcb = _write_fp_board(
            tmp_path, FP_FILLED_POLY, DEEP_INTERIOR_TRACK + "\n  " + FAR_EXTERIOR_TRACK, rot=90.0
        )
        results = _run_rule(pcb)
        traces = [v for v in results.errors if v.rule_id == "edge_clearance_trace"]
        assert len(traces) == 1
        assert traces[0].actual_value == pytest.approx(0.0)

    def test_interior_track_inside_rotated_fp_rect_flagged(self, tmp_path):
        pcb = _write_fp_board(
            tmp_path, FP_BARE_RECT, DEEP_INTERIOR_TRACK + "\n  " + FAR_EXTERIOR_TRACK, rot=90.0
        )
        results = _run_rule(pcb)
        traces = [v for v in results.errors if v.rule_id == "edge_clearance_trace"]
        assert len(traces) == 1
        assert traces[0].actual_value == pytest.approx(0.0)

    def test_interior_via_inside_fp_cutout_flagged(self, tmp_path):
        pcb = _write_fp_board(tmp_path, FP_BARE_CIRCLE, INTERIOR_VIA + "\n  " + EXTERIOR_VIA)
        results = _run_rule(pcb)
        vias = [v for v in results.errors if v.rule_id == "edge_clearance_via"]
        assert len(vias) == 1
        assert vias[0].actual_value == pytest.approx(0.0)


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


# ---------------------------------------------------------------------------
# Footprint closed-shape reader: arcs and rounded corners (review of #9721)
# ---------------------------------------------------------------------------


def _fp_shapes(shape: str):
    from kicad_tools.core.board_outline import footprint_outline_closed_shapes
    from kicad_tools.sexp import parse_sexp

    root = parse_sexp('(kicad_pcb (footprint "F" (layer "F.Cu") (at 0 0) ' + shape + "))")
    return list(footprint_outline_closed_shapes(root))


def test_fp_poly_arc_is_preserved_in_ring():
    (shape,) = _fp_shapes(
        "(fp_poly (pts (xy -20 -15) (xy 20 -15) (xy 20 15) "
        "(arc (start 15 15) (mid 0 30) (end -15 15)) (xy -20 15)) "
        '(fill yes) (layer "Edge.Cuts"))'
    )
    assert max(y for _x, y in shape.ring) == pytest.approx(30.0, abs=0.01)
    assert len(shape.ring) > 5


def test_fp_rect_radius_rounds_corners():
    (shape,) = _fp_shapes(
        '(fp_rect (start -20 -20) (end 20 20) (radius 12) (fill yes) (layer "Edge.Cuts"))'
    )
    assert (-20.0, -20.0) not in shape.ring
    assert min(math.hypot(x + 20, y + 20) for x, y in shape.ring) > 3.0
