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


class TestNormalizeZonePadConnectionModes:
    """Issue #3727 / #3729: the zone-level pad-connection modes.

    ``normalize_zone_pad_connection`` accepts the explicit KiCad zone-level
    modes (``yes`` / ``thru_hole_only`` / ``no``), each of which adds a leading
    mode token to every copper zone's ``connect_pads`` that lacks one.  The
    *default* mode is now ``selective`` (tested separately in
    :class:`TestSelectivePadConnection`).
    """

    _ZONE_THERMAL = """
    (kicad_pcb
      (version 20240108)
      (generator "test")
      (net 0 "")
      (net 1 "GND")
      (zone
        (net 1)
        (net_name "GND")
        (layer "F.Cu")
        (uuid "z1")
        (hatch edge 0.5)
        (connect_pads (clearance 0.3))
        (min_thickness 0.25)
        (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.4))
        (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))
      )
    )
    """

    def _connect_pads_mode(self, doc):
        zone = doc.find_all("zone")[0]
        connect_pads = zone.find("connect_pads")
        atoms = [c.value for c in connect_pads.children if c.is_atom]
        return atoms[0] if atoms else None

    def test_adds_solid_mode_to_legacy_zone(self):
        from kicad_tools.zones.fill_clearance import normalize_zone_pad_connection

        doc = parse_string(self._ZONE_THERMAL)
        assert self._connect_pads_mode(doc) is None  # thermal-for-all

        changed = normalize_zone_pad_connection(doc, mode="yes")
        assert changed == 1
        assert self._connect_pads_mode(doc) == "yes"
        # Must render as a bare keyword for KiCad, never a quoted string.
        rendered = doc.find_all("zone")[0].to_string()
        assert "(connect_pads yes (clearance" in rendered
        assert '"yes"' not in rendered

    def test_yes_mode_idempotent(self):
        from kicad_tools.zones.fill_clearance import normalize_zone_pad_connection

        doc = parse_string(self._ZONE_THERMAL)
        assert normalize_zone_pad_connection(doc, mode="yes") == 1
        # A second pass finds the mode already present and makes no change.
        assert normalize_zone_pad_connection(doc, mode="yes") == 0
        assert self._connect_pads_mode(doc) == "yes"

    def test_preserves_existing_mode(self):
        from kicad_tools.zones.fill_clearance import normalize_zone_pad_connection

        board = self._ZONE_THERMAL.replace(
            "(connect_pads (clearance 0.3))",
            "(connect_pads thru_hole_only (clearance 0.3))",
        )
        doc = parse_string(board)
        # An explicit upstream mode is never clobbered.
        assert normalize_zone_pad_connection(doc, mode="yes") == 0
        assert self._connect_pads_mode(doc) == "thru_hole_only"

    def test_thru_hole_only_mode(self):
        from kicad_tools.zones.fill_clearance import normalize_zone_pad_connection

        doc = parse_string(self._ZONE_THERMAL)
        assert normalize_zone_pad_connection(doc, mode="thru_hole_only") == 1
        assert self._connect_pads_mode(doc) == "thru_hole_only"

    def test_rejects_unknown_mode(self):
        from kicad_tools.zones.fill_clearance import normalize_zone_pad_connection

        doc = parse_string(self._ZONE_THERMAL)
        with pytest.raises(ValueError, match="Unsupported pad-connection mode"):
            normalize_zone_pad_connection(doc, mode="bogus")

    def test_keepout_zone_untouched(self):
        from kicad_tools.zones.fill_clearance import normalize_zone_pad_connection

        keepout = """
        (kicad_pcb
          (version 20240108)
          (generator "test")
          (net 0 "")
          (zone
            (net 0)
            (net_name "")
            (layers "F.Cu" "B.Cu")
            (uuid "k1")
            (hatch edge 0.5)
            (keepout (tracks not_allowed) (vias not_allowed) (copperpour not_allowed))
            (fill yes)
            (polygon (pts (xy 0 0) (xy 5 0) (xy 5 5) (xy 0 5)))
          )
        )
        """
        doc = parse_string(keepout)
        # Keepout zones have no connect_pads -> nothing to normalize.
        assert normalize_zone_pad_connection(doc, mode="yes") == 0


class TestSelectivePadConnection:
    """Issue #3729: the selective per-pad policy is the default.

    The default mode keeps the zone's thermal-relief default and forces a
    solid ``(zone_connect 2)`` override only on pads too small to host the 2
    thermal spokes KiCad's geometric DRC requires.  Pads that can host 2
    spokes keep their thermal relief (which eases hand-soldering / rework).
    """

    # A GND zone (F.Cu) with three pads of varying size on its net:
    #  - small SMD 0.5x0.5  -> cannot host 2 spokes -> force solid
    #  - large SMD 3.0x3.0  -> hosts 2 spokes        -> keep thermal relief
    #  - THT   1.6x1.6      -> hosts 2 spokes        -> keep thermal relief
    # Plus a foreign-net SMD pad that must never be touched.
    _BOARD = """
    (kicad_pcb
      (version 20240108)
      (generator "test")
      (net 0 "")
      (net 1 "GND")
      (net 2 "SIG")
      (footprint "lib:small" (layer "F.Cu") (at 5 5)
        (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu")
          (net 1 "GND") (uuid "pad-small")))
      (footprint "lib:big" (layer "F.Cu") (at 10 10)
        (pad "1" smd rect (at 0 0) (size 3.0 3.0) (layers "F.Cu")
          (net 1 "GND") (uuid "pad-big")))
      (footprint "lib:tht" (layer "F.Cu") (at 15 15)
        (pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.8) (layers "*.Cu")
          (net 1 "GND") (uuid "pad-tht")))
      (footprint "lib:foreign" (layer "F.Cu") (at 2 2)
        (pad "1" smd rect (at 0 0) (size 0.4 0.4) (layers "F.Cu")
          (net 2 "SIG") (uuid "pad-foreign")))
      (zone
        (net 1)
        (net_name "GND")
        (layer "F.Cu")
        (uuid "z1")
        (hatch edge 0.5)
        (connect_pads (clearance 0.3))
        (min_thickness 0.25)
        (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.4))
        (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))
      )
    )
    """

    def _zone_connect(self, doc, pad_uuid):
        for fp in doc.find_all("footprint"):
            for pad in fp.find_all("pad"):
                u = pad.find("uuid")
                if u is not None and u.get_string(0) == pad_uuid:
                    zc = pad.find("zone_connect")
                    return zc.get_int(0) if zc is not None else None
        raise AssertionError(f"pad {pad_uuid} not found")

    def test_default_mode_is_selective(self):
        from kicad_tools.zones.fill_clearance import normalize_zone_pad_connection

        doc = parse_string(self._BOARD)
        # Default call -> selective: only the small pad is forced solid.
        changed = normalize_zone_pad_connection(doc)
        assert changed == 1
        # The zone keeps thermal relief (no mode token added).
        zone = doc.find_all("zone")[0]
        connect_pads = zone.find("connect_pads")
        assert [c.value for c in connect_pads.children if c.is_atom] == []

    def test_small_pad_forced_solid(self):
        from kicad_tools.zones.fill_clearance import normalize_zone_pad_connection

        doc = parse_string(self._BOARD)
        normalize_zone_pad_connection(doc)
        assert self._zone_connect(doc, "pad-small") == 2

    def test_large_pads_keep_thermal_relief(self):
        from kicad_tools.zones.fill_clearance import normalize_zone_pad_connection

        doc = parse_string(self._BOARD)
        normalize_zone_pad_connection(doc)
        # Large SMD and THT pads can host 2 spokes -> no override (thermal).
        assert self._zone_connect(doc, "pad-big") is None
        assert self._zone_connect(doc, "pad-tht") is None

    def test_foreign_net_pad_never_touched(self):
        from kicad_tools.zones.fill_clearance import normalize_zone_pad_connection

        doc = parse_string(self._BOARD)
        normalize_zone_pad_connection(doc)
        # A pad whose net has no thermal-relief zone is left alone, even if it
        # is tiny.
        assert self._zone_connect(doc, "pad-foreign") is None

    def test_idempotent(self):
        from kicad_tools.zones.fill_clearance import normalize_zone_pad_connection

        doc = parse_string(self._BOARD)
        assert normalize_zone_pad_connection(doc) == 1
        # A second pass finds the small pad already solid -> no change.
        assert normalize_zone_pad_connection(doc) == 0

    def test_preserves_existing_zone_connect(self):
        from kicad_tools.zones.fill_clearance import normalize_zone_pad_connection

        board = self._BOARD.replace(
            '(net 1 "GND") (uuid "pad-small")',
            '(net 1 "GND") (zone_connect 1) (uuid "pad-small")',
        )
        doc = parse_string(board)
        # A deliberate upstream per-pad zone_connect is never clobbered.
        normalize_zone_pad_connection(doc)
        assert self._zone_connect(doc, "pad-small") == 1

    def test_zone_with_explicit_mode_opts_out(self):
        from kicad_tools.zones.fill_clearance import normalize_zone_pad_connection

        board = self._BOARD.replace(
            "(connect_pads (clearance 0.3))",
            "(connect_pads yes (clearance 0.3))",
        )
        doc = parse_string(board)
        # The zone-level mode governs every pad, so no per-pad override added.
        assert normalize_zone_pad_connection(doc) == 0
        assert self._zone_connect(doc, "pad-small") is None


class TestStarvedThermalReportParsing:
    """Issue #3729: read pad/zone UUIDs back from a kicad-cli DRC report."""

    _REPORT = {
        "violations": [
            {
                "type": "starved_thermal",
                "items": [
                    {"description": "Zone [+24V] on F.Cu, priority 5", "uuid": "zone-1"},
                    {"description": "Pad 4 [+3V3] of J3", "uuid": "pad-smd"},
                ],
            },
            {
                "type": "starved_thermal",
                "items": [
                    {"description": "Zone [+24V] on F.Cu", "uuid": "zone-1"},
                    {"description": "PTH pad 2 [+24V] of Q5", "uuid": "pad-tht"},
                ],
            },
            {
                "type": "isolated_copper",
                "items": [
                    {"description": "Zone [+3.3V] on In2.Cu, priority 0", "uuid": "zone-iso"},
                ],
            },
            {
                "type": "clearance",
                "items": [{"description": "Pad 1 of U1", "uuid": "pad-other"}],
            },
        ]
    }

    def test_starved_pad_uuids_match_smd_and_tht(self):
        from kicad_tools.zones.fill_clearance import starved_thermal_pad_uuids

        # Both the SMD ``Pad`` and through-hole ``PTH pad`` forms are caught,
        # the paired zone item is skipped, and unrelated violations ignored.
        assert starved_thermal_pad_uuids(self._REPORT) == {"pad-smd", "pad-tht"}

    def test_isolated_zone_uuids(self):
        from kicad_tools.zones.fill_clearance import isolated_copper_zone_uuids

        assert isolated_copper_zone_uuids(self._REPORT) == {"zone-iso"}

    def test_empty_report(self):
        from kicad_tools.zones.fill_clearance import (
            isolated_copper_zone_uuids,
            starved_thermal_pad_uuids,
        )

        assert starved_thermal_pad_uuids({}) == set()
        assert isolated_copper_zone_uuids({}) == set()


class TestForceSolidByUuid:
    """Issue #3729: force a solid connection on pads named by the DRC report."""

    _BOARD = """
    (kicad_pcb
      (version 20240108)
      (generator "test")
      (net 0 "")
      (net 1 "GND")
      (footprint "lib:a" (layer "F.Cu") (at 5 5)
        (pad "1" smd rect (at 0 0) (size 2.0 2.0) (layers "F.Cu")
          (net 1 "GND") (uuid "pad-a")))
      (footprint "lib:b" (layer "F.Cu") (at 9 5)
        (pad "1" smd rect (at 0 0) (size 2.0 2.0) (layers "F.Cu")
          (net 1 "GND") (uuid "pad-b")))
    )
    """

    def test_forces_named_pads_solid(self):
        from kicad_tools.zones.fill_clearance import force_solid_on_pads_by_uuid

        doc = parse_string(self._BOARD)
        changed = force_solid_on_pads_by_uuid(doc, {"pad-a"})
        assert changed == 1
        for fp in doc.find_all("footprint"):
            for pad in fp.find_all("pad"):
                u = pad.find("uuid").get_string(0)
                zc = pad.find("zone_connect")
                if u == "pad-a":
                    assert zc is not None and zc.get_int(0) == 2
                else:
                    assert zc is None  # pad-b untouched

    def test_empty_uuid_set_is_noop(self):
        from kicad_tools.zones.fill_clearance import force_solid_on_pads_by_uuid

        doc = parse_string(self._BOARD)
        assert force_solid_on_pads_by_uuid(doc, set()) == 0


class TestForceSolidOnIsolatedIslandPads:
    """Issue #3729: resolve isolated-copper slivers to their anchoring pad.

    An ``isolated_copper`` warning names only the zone, so the remediator
    finds the small (sliver) filled_polygons in that zone and forces the
    same-net pad whose copper touches one to a solid connection.  Substantial
    fill lobes and pads not touching a sliver are never touched.
    """

    # A GND/F.Cu zone with two fill polygons: a large main pour and a tiny
    # 0.3x0.3 sliver around a same-net pad whose lone spoke stranded it.
    _BOARD = """
    (kicad_pcb
      (version 20240108)
      (generator "test")
      (net 0 "")
      (net 1 "GND")
      (footprint "lib:sliver" (layer "F.Cu") (at 10 0.5)
        (pad "1" smd rect (at 0 0) (size 0.4 0.4) (layers "F.Cu")
          (net 1 "GND") (uuid "pad-sliver")))
      (footprint "lib:main" (layer "F.Cu") (at 2 2.5)
        (pad "1" smd rect (at 0 0) (size 2.0 2.0) (layers "F.Cu")
          (net 1 "GND") (uuid "pad-main")))
      (zone
        (net 1)
        (net_name "GND")
        (layer "F.Cu")
        (uuid "z-iso")
        (hatch edge 0.5)
        (connect_pads (clearance 0.3))
        (min_thickness 0.25)
        (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.4))
        (polygon (pts (xy 0 0) (xy 20 0) (xy 20 5) (xy 0 5)))
        (filled_polygon (layer "F.Cu")
          (pts (xy 0 0) (xy 5 0) (xy 5 5) (xy 0 5)))
        (filled_polygon (layer "F.Cu")
          (pts (xy 9.8 0.3) (xy 10.2 0.3) (xy 10.2 0.7) (xy 9.8 0.7)))
      )
    )
    """

    def _zc(self, doc, uuid):
        for fp in doc.find_all("footprint"):
            for pad in fp.find_all("pad"):
                u = pad.find("uuid")
                if u is not None and u.get_string(0) == uuid:
                    zc = pad.find("zone_connect")
                    return zc.get_int(0) if zc is not None else None
        raise AssertionError(uuid)

    def test_forces_sliver_anchor_pad_solid(self):
        from kicad_tools.zones.fill_clearance import force_solid_on_isolated_island_pads

        doc = parse_string(self._BOARD)
        changed = force_solid_on_isolated_island_pads(doc, {"z-iso"})
        assert changed == 1
        # The pad whose copper touches the tiny sliver is forced solid...
        assert self._zc(doc, "pad-sliver") == 2
        # ...while the pad in the substantial main pour keeps thermal relief.
        assert self._zc(doc, "pad-main") is None

    def test_noop_for_unlisted_zone(self):
        from kicad_tools.zones.fill_clearance import force_solid_on_isolated_island_pads

        doc = parse_string(self._BOARD)
        # A zone UUID not in the set is ignored entirely.
        assert force_solid_on_isolated_island_pads(doc, {"other-zone"}) == 0
        assert self._zc(doc, "pad-sliver") is None

    def test_empty_set_is_noop(self):
        from kicad_tools.zones.fill_clearance import force_solid_on_isolated_island_pads

        doc = parse_string(self._BOARD)
        assert force_solid_on_isolated_island_pads(doc, set()) == 0
