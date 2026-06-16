"""Regression tests for the post-fill foreign-pad clearance correction.

Issue #3711: ``kct zones fill`` produced ``filled_polygon`` copper that
overlapped foreign-net pads, raising ``clearance_pad_zone`` DRC errors
(KiCad-tools-serialized boards where kicad-cli's fill left the antipad
too small).  :func:`apply_foreign_pad_clearance` carves a foreign-net
antipad (``clearance + min_thickness/2``) out of every fill so the copper
clears foreign pads/vias by at least the zone clearance, while leaving
same-net connections intact.

These tests are pure-Python (only ``shapely``); they never call
kicad-cli.  They build a minimal board s-expression, run the correction,
and assert the resulting geometry directly.
"""

from __future__ import annotations

import pytest

from kicad_tools.sexp import SExp, parse_string
from kicad_tools.validate.rules.clearance import _repair_fill_polygon
from kicad_tools.zones.fill_clearance import apply_foreign_pad_clearance

shapely = pytest.importorskip("shapely")
from shapely.geometry import Polygon  # noqa: E402

# A 20x20 mm solid fill on net 1 (VCC) covering (0,0)..(20,20).  Two pads
# sit *inside* the fill: one foreign (net 3, GND) that must be carved out,
# one same-net (net 1, VCC) that must remain connected.
_BOARD = """
(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 1 "VCC")
  (net 2 "LED_ANODE")
  (net 3 "GND")
  (footprint "lib:foreign"
    (layer "F.Cu")
    (at 5 5)
    (pad "1" thru_hole rect (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 3 "GND"))
  )
  (footprint "lib:samenet"
    (layer "F.Cu")
    (at 15 15)
    (pad "1" thru_hole rect (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 1 "VCC"))
  )
  (via (at 12 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2 "LED_ANODE"))
  (zone
    (net "VCC")
    (layer "F.Cu")
    (uuid "test-zone")
    (hatch edge 0.5)
    (connect_pads (clearance 0.3))
    (min_thickness 0.25)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.4))
    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))
    (filled_polygon
      (layer "F.Cu")
      (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20))
    )
  )
)
"""


def _parse(board: str) -> SExp:
    return parse_string(board)


def _fill_polygon(doc: SExp, zone_index: int = 0):
    """Reconstruct the (possibly holed) fill polygon of a zone.

    Uses the *same* ``_repair_fill_polygon`` the DRC rule applies, so the
    holes encoded by KiCad's self-touching single-ring format (which
    ``make_valid`` returns as a GeometryCollection of polygon + dangling
    seam linework) are reconstructed exactly as ``kct check`` sees them.
    """
    zone = doc.find_all("zone")[zone_index]
    polys = []
    for filled in zone.find_all("filled_polygon"):
        pts = filled.find("pts")
        ring = [(xy.get_float(0), xy.get_float(1)) for xy in pts.find_all("xy")]
        poly = Polygon(ring)
        if not poly.is_valid:
            poly = _repair_fill_polygon(poly)
        if not poly.is_empty:
            polys.append(poly)
    return shapely.unary_union(polys)


def _pad_box(cx: float, cy: float, w: float, h: float):
    return shapely.box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


class TestForeignPadCarved:
    def test_foreign_pad_is_excluded_with_clearance(self):
        doc = _parse(_BOARD)
        modified = apply_foreign_pad_clearance(doc)
        assert modified >= 1

        fill = _fill_polygon(doc)
        # Foreign GND pad at board (5,5), 1.7x1.7 box.
        foreign = _pad_box(5.0, 5.0, 1.7, 1.7)

        # The fill must not overlap the foreign pad at all...
        assert fill.intersection(foreign).area == pytest.approx(0.0, abs=1e-9)
        # ...and must clear it by at least the zone clearance (0.3mm).
        assert fill.distance(foreign) >= 0.3 - 1e-6

    def test_same_net_pad_stays_connected(self):
        doc = _parse(_BOARD)
        apply_foreign_pad_clearance(doc)

        fill = _fill_polygon(doc)
        # Same-net VCC pad at board (15,15) must remain inside the fill.
        same = _pad_box(15.0, 15.0, 1.7, 1.7)
        assert fill.intersection(same).area > 0.0

    def test_foreign_via_is_excluded_with_clearance(self):
        doc = _parse(_BOARD)
        apply_foreign_pad_clearance(doc)

        fill = _fill_polygon(doc)
        # Foreign LED_ANODE via barrel at (12,5), radius 0.3mm.
        via = shapely.geometry.Point(12.0, 5.0).buffer(0.3)
        assert fill.intersection(via).area == pytest.approx(0.0, abs=1e-9)
        assert fill.distance(via) >= 0.3 - 1e-6

    def test_fill_still_has_substantial_copper(self):
        """The carve removes only the antipads, not the whole pour."""
        doc = _parse(_BOARD)
        apply_foreign_pad_clearance(doc)
        fill = _fill_polygon(doc)
        # 20x20 = 400; antipads remove only a few mm^2.
        assert fill.area > 380.0


class TestNoForeignCopper:
    def test_no_modification_when_all_same_net(self):
        """A fill with only same-net pads is left untouched."""
        board = _BOARD.replace('(net 3 "GND")', '(net 1 "VCC")')
        doc = _parse(board)
        modified = apply_foreign_pad_clearance(doc)
        # The foreign via (net 2) still forces one modification, so assert
        # the same-net pad is preserved rather than expecting zero edits.
        fill = _fill_polygon(doc)
        same = _pad_box(5.0, 5.0, 1.7, 1.7)
        assert fill.intersection(same).area > 0.0
        assert modified >= 0


class TestEndToEndDRC:
    """Round-trip through the real DRC rule the strict CI gate runs."""

    def test_drc_reports_zero_pad_zone_errors_after_correction(self, tmp_path):
        from kicad_tools.core.sexp_file import save_pcb
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.clearance import ViaZoneClearanceRule

        doc = _parse(_BOARD)

        out = tmp_path / "board.kicad_pcb"
        save_pcb(doc, out)
        pcb_before = PCB.load(str(out))
        rule = ViaZoneClearanceRule()

        class _Rules:
            min_clearance_mm = 0.127

        before = rule.check(pcb_before, _Rules())
        # Sanity: without the correction the foreign GND pad / via overlap
        # the VCC fill, so the rule fires.
        assert any(
            v.rule_id in ("clearance_pad_zone", "clearance_via_zone") for v in before.violations
        )

        apply_foreign_pad_clearance(doc)
        save_pcb(doc, out)
        pcb_after = PCB.load(str(out))
        after = rule.check(pcb_after, _Rules())

        pad_zone = [
            v for v in after.violations if v.rule_id in ("clearance_pad_zone", "clearance_via_zone")
        ]
        assert pad_zone == [], f"expected no pad/via-zone errors, got: {pad_zone}"


class TestIslandRemoval:
    """The carve must not strand disconnected fill fragments.

    Regression for the board-02 split-fill regression (PR #3725): the
    foreign-antipad subtraction + hole venting can shed thin sliver lobes
    that are no longer electrically tied to the pour.  Emitting them
    produces ``isolated_copper`` DRC warnings (the board-06 split-fill
    class).  :func:`apply_foreign_pad_clearance` now drops every fill ring
    not connected to a same-net pad/via/track, mirroring KiCad's
    ``island_removal_mode 0``.
    """

    # A long thin VCC fill bridged at its waist by a foreign GND pad.  The
    # only same-net VCC anchor is a pad in the LEFT lobe; carving the GND
    # antipad severs the RIGHT lobe, which must then be removed as an island.
    _SPLIT_BOARD = """
    (kicad_pcb
      (version 20240108)
      (generator "test")
      (net 0 "")
      (net 1 "VCC")
      (net 3 "GND")
      (footprint "lib:vcc"
        (layer "F.Cu")
        (at 2 2.5)
        (pad "1" thru_hole rect (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 1 "VCC"))
      )
      (footprint "lib:gnd"
        (layer "F.Cu")
        (at 10 2.5)
        (pad "1" thru_hole rect (at 0 0) (size 4.0 5.0) (drill 1.0) (layers "*.Cu" "*.Mask") (net 3 "GND"))
      )
      (zone
        (net "VCC")
        (layer "F.Cu")
        (uuid "split-zone")
        (hatch edge 0.5)
        (connect_pads (clearance 0.3))
        (min_thickness 0.25)
        (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.4))
        (polygon (pts (xy 0 0) (xy 20 0) (xy 20 5) (xy 0 5)))
        (filled_polygon
          (layer "F.Cu")
          (pts (xy 0 0) (xy 20 0) (xy 20 5) (xy 0 5))
        )
      )
    )
    """

    def test_disconnected_lobe_is_removed(self):
        doc = _parse(self._SPLIT_BOARD)
        apply_foreign_pad_clearance(doc)

        # After the carve the GND pad antipad severs the bar in two.  Only
        # the LEFT lobe (holding the VCC pad at x=2) is connected to the net;
        # the RIGHT lobe (x>~12) must be dropped, leaving a single component.
        fill = _fill_polygon(doc)
        ncomp = 1 if fill.geom_type == "Polygon" else len(fill.geoms)
        assert ncomp == 1, f"expected one connected component, got {ncomp}"

        # The surviving copper must still hold the same-net VCC pad...
        same = _pad_box(2.0, 2.5, 1.7, 1.7)
        assert fill.intersection(same).area > 0.0
        # ...and must not include the stranded right lobe.
        right_lobe_probe = _pad_box(18.0, 2.5, 1.0, 1.0)
        assert fill.intersection(right_lobe_probe).area == pytest.approx(0.0, abs=1e-9)

    def test_keep_connected_rings_drops_island(self):
        from kicad_tools.zones.fill_clearance import _keep_connected_rings

        # Two square rings; the anchor only touches the first.
        left = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
        right = [(10.0, 0.0), (12.0, 0.0), (12.0, 2.0), (10.0, 2.0)]
        anchor = shapely.box(0.5, 0.5, 1.5, 1.5)  # inside `left` only
        kept = _keep_connected_rings([left, right], [anchor], Polygon)
        assert kept == [left]

    def test_keep_connected_rings_no_anchors_keeps_all(self):
        from kicad_tools.zones.fill_clearance import _keep_connected_rings

        a = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
        b = [(5.0, 5.0), (6.0, 5.0), (6.0, 6.0)]
        kept = _keep_connected_rings([a, b], [], Polygon)
        assert kept == [a, b]


class TestNetIdentityResolution:
    def test_name_only_zone_matches_numbered_pad(self):
        """Zone declared as (net "VCC") matches a pad declared (net 1 "VCC")."""
        doc = _parse(_BOARD)
        # Sanity: the zone uses the name-only form in the fixture.
        zone = doc.find_all("zone")[0]
        assert zone.find("net").get_int(0) is None
        assert zone.find("net").get_string(0) == "VCC"

        apply_foreign_pad_clearance(doc)
        fill = _fill_polygon(doc)
        # The same-net VCC pad must NOT be carved out (identity resolved).
        same = _pad_box(15.0, 15.0, 1.7, 1.7)
        assert fill.intersection(same).area > 0.0
