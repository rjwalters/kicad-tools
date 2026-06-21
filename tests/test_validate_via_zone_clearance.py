"""Tests for the via/pad-vs-foreign-zone-fill DRC rule (Issue #3556).

The sibling of ``SegmentZoneClearanceRule`` (Issue #3527).  Before
``ViaZoneClearanceRule`` no rule in ``kct check`` compared a via barrel
or pad to another net's committed ``filled_polygon`` copper, so a via
dropped sub-clearance to (or through) a stale foreign pour shipped
uncaught -- the exact failure class that motivated Issue #3556 (a
surgical trace move on board 06 left copper 0.0347 mm from an
un-refilled GND pour; KiCad DRC flagged it, every project gate stayed
green).

Mirrors the scenario coverage of
``tests/test_validate_segment_zone_clearance.py``:

1. Synthetic short (via center inside a foreign fill)
2. Clearance graze (positive gap below the manufacturer minimum)
3. Same-net pass-through (legal -- a via stitching its own pour)
4. Knocked-out fill (legal -- the fill has a corridor with adequate gap)

plus all-layer barrel coverage, pad coverage, and net-0 skips.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock

from kicad_tools.validate.rules.clearance import ViaZoneClearanceRule

# ---------------------------------------------------------------------------
# Minimal stubs mirroring the fields the rule reads
# ---------------------------------------------------------------------------


@dataclass
class _FakeNet:
    number: int
    name: str


@dataclass
class _FakeVia:
    position: tuple[float, float]
    size: float = 0.6
    layers: tuple[str, ...] = ("F.Cu", "B.Cu")
    net_number: int = 1
    net_name: str = "SIG"
    uuid: str = "deadbeef-0000"


@dataclass
class _FakePad:
    position: tuple[float, float]
    size: tuple[float, float] = (1.0, 1.0)
    layers: tuple[str, ...] = ("F.Cu",)
    net_number: int = 1
    net_name: str = "SIG"
    number: str = "1"
    rotation: float = 0.0
    shape: str = "rect"
    roundrect_rratio: float = 0.25


@dataclass
class _FakeFootprint:
    pads: list[_FakePad] = field(default_factory=list)
    position: tuple[float, float] = (0.0, 0.0)
    rotation: float = 0.0
    reference: str = "U1"


@dataclass
class _FakeZone:
    net_number: int = 2
    net_name: str = "+3V3"
    layer: str = "F.Cu"
    filled_polygons: list[list[tuple[float, float]]] = field(default_factory=list)
    filled_polygon_layers: list[str] = field(default_factory=list)

    def filled_polygon_layer(self, index: int) -> str:
        if index < len(self.filled_polygon_layers) and self.filled_polygon_layers[index]:
            return self.filled_polygon_layers[index]
        return self.layer


def _make_pcb(zones, nets, vias=None, footprints=None):
    pcb = MagicMock()
    pcb.zones = zones
    pcb.nets = {n.number: n for n in nets}
    pcb.vias = vias or []
    pcb.footprints = footprints or []
    return pcb


def _make_design_rules(min_clearance: float = 0.127):
    rules = MagicMock()
    rules.min_clearance_mm = min_clearance
    return rules


# A 10x10 mm square fill on net 2 (+3V3) at (100,100)..(110,110)
_SQUARE_FILL = [(100.0, 100.0), (110.0, 100.0), (110.0, 110.0), (100.0, 110.0)]

_NETS = [_FakeNet(1, "SIG"), _FakeNet(2, "+3V3")]


def _run(zones, vias=None, footprints=None, min_clearance=0.127):
    pcb = _make_pcb(zones, _NETS, vias=vias, footprints=footprints)
    rule = ViaZoneClearanceRule()
    return rule.check(pcb, _make_design_rules(min_clearance))


# ---------------------------------------------------------------------------
# Scenario 1: hard short -- via center inside a foreign fill
# ---------------------------------------------------------------------------


class TestViaShortDetection:
    def test_via_inside_foreign_fill_is_short(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        via = _FakeVia(position=(105.0, 105.0))
        results = _run([zone], vias=[via])

        shorts = [v for v in results.violations if v.rule_id == "clearance_via_zone"]
        assert len(shorts) == 1
        v = shorts[0]
        assert v.severity == "error"
        assert "Short" in v.message
        assert v.actual_value is not None and v.actual_value < 0
        assert v.nets == ("SIG", "+3V3")
        assert v.layer == "F.Cu"

    def test_via_overlapping_fill_edge_is_short(self):
        """Via center outside the fill but the barrel disc overlaps the edge."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        # Center 0.2mm left of the fill edge; radius 0.3mm -> 0.1mm overlap.
        via = _FakeVia(position=(99.8, 105.0), size=0.6)
        results = _run([zone], vias=[via])

        assert len(results.violations) == 1
        v = results.violations[0]
        assert "Short" in v.message
        assert v.actual_value < 0


# ---------------------------------------------------------------------------
# Scenario 2: clearance graze (gap > 0 but < minimum)
# ---------------------------------------------------------------------------


class TestViaClearanceGraze:
    def test_sub_minimum_gap_is_flagged(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        # Center 0.35mm left of the edge; radius 0.3mm -> copper gap 0.05mm.
        via = _FakeVia(position=(99.65, 105.0), size=0.6)
        results = _run([zone], vias=[via])

        assert len(results.violations) == 1
        v = results.violations[0]
        assert v.severity == "error"
        assert "Short" not in v.message
        assert abs(v.actual_value - 0.05) < 1e-6
        assert v.required_value == 0.127

    def test_adequate_gap_passes(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        # Center 0.6mm left of the edge; radius 0.3mm -> copper gap 0.3mm.
        via = _FakeVia(position=(99.4, 105.0), size=0.6)
        results = _run([zone], vias=[via])
        assert results.violations == []

    def test_motivating_0p0347_gap_is_flagged(self):
        """Issue #3556's reproduction: a 0.0347mm gap to a foreign pour."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        # radius 0.3mm; place center so the copper edge sits 0.0347mm from
        # the fill edge at x=100.0 -> center_x = 100 - 0.3 - 0.0347.
        via = _FakeVia(position=(100.0 - 0.3 - 0.0347, 105.0), size=0.6)
        results = _run([zone], vias=[via])
        assert len(results.violations) == 1
        v = results.violations[0]
        assert "Short" not in v.message
        assert abs(v.actual_value - 0.0347) < 1e-3


# ---------------------------------------------------------------------------
# Scenario 3: same-net / net-0 pass-through is legal
# ---------------------------------------------------------------------------


class TestViaSameNet:
    def test_via_in_own_pour_passes(self):
        zone = _FakeZone(net_number=1, net_name="SIG", filled_polygons=[_SQUARE_FILL])
        via = _FakeVia(position=(105.0, 105.0))
        results = _run([zone], vias=[via])
        assert results.violations == []

    def test_net0_via_skipped(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        via = _FakeVia(position=(105.0, 105.0), net_number=0)
        results = _run([zone], vias=[via])
        assert results.violations == []

    def test_name_only_zone_net_resolved(self):
        """KiCad 9 name-only zone net resolves through the net table."""
        zone = _FakeZone(net_number=0, net_name="SIG", filled_polygons=[_SQUARE_FILL])
        via = _FakeVia(position=(105.0, 105.0))
        # Same net after resolution -> legal.
        results = _run([zone], vias=[via])
        assert results.violations == []


# ---------------------------------------------------------------------------
# Scenario 4: knocked-out fill (corridor with adequate gap) is legal
# ---------------------------------------------------------------------------


class TestViaKnockedOutFill:
    def test_fill_with_clearance_hole_around_via_passes(self):
        """A properly refilled zone leaves a knockout around the via.

        The fill is the 10x10 square with an octagonal-ish knockout
        around a via at (105,105): the knockout walls sit 0.5mm from the
        via center (radius 0.3mm -> 0.2mm gap >= 0.127mm).
        """
        knockout_fill = [
            (100.0, 100.0),
            (110.0, 100.0),
            (110.0, 110.0),
            (100.0, 110.0),
            (100.0, 100.0),
            # inner clearance hole (reverse-wound square 104.5..105.5)
            (104.5, 104.5),
            (104.5, 105.5),
            (105.5, 105.5),
            (105.5, 104.5),
            (104.5, 104.5),
        ]
        zone = _FakeZone(filled_polygons=[knockout_fill])
        via = _FakeVia(position=(105.0, 105.0), size=0.6)
        results = _run([zone], vias=[via])
        assert results.violations == []


# ---------------------------------------------------------------------------
# All-layer barrel coverage (Issue #3487 parity)
# ---------------------------------------------------------------------------


class TestViaAllLayer:
    def test_through_via_checked_against_inner_layer_fill(self):
        """A through-via barrel collides with an In1.Cu fill it spans."""
        zone = _FakeZone(
            net_number=2,
            net_name="+3V3",
            layer="In1.Cu",
            filled_polygons=[_SQUARE_FILL],
            filled_polygon_layers=["In1.Cu"],
        )
        # Through-via F.Cu<->B.Cu spans In1.Cu; barrel overlaps the inner fill.
        via = _FakeVia(position=(105.0, 105.0), layers=("F.Cu", "B.Cu"))
        results = _run([zone], vias=[via])
        assert len(results.violations) == 1
        assert results.violations[0].layer == "In1.Cu"
        assert "Short" in results.violations[0].message

    def test_blind_via_not_checked_against_unspanned_layer(self):
        """A blind F.Cu<->In1.Cu via does not reach a B.Cu fill."""
        zone = _FakeZone(
            net_number=2,
            net_name="+3V3",
            layer="B.Cu",
            filled_polygons=[_SQUARE_FILL],
            filled_polygon_layers=["B.Cu"],
        )
        via = _FakeVia(position=(105.0, 105.0), layers=("F.Cu", "In1.Cu"))
        results = _run([zone], vias=[via])
        assert results.violations == []


# ---------------------------------------------------------------------------
# Pad coverage
# ---------------------------------------------------------------------------


class TestPadZone:
    def test_pad_overlapping_foreign_fill_is_short(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        pad = _FakePad(position=(105.0, 105.0), size=(1.0, 1.0))
        fp = _FakeFootprint(pads=[pad], reference="U2")
        results = _run([zone], footprints=[fp])

        shorts = [v for v in results.violations if v.rule_id == "clearance_pad_zone"]
        assert len(shorts) == 1
        v = shorts[0]
        assert "Short" in v.message
        assert v.items[0] == "U2-1"
        assert v.nets == ("SIG", "+3V3")

    def test_pad_sub_minimum_gap_flagged(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        # 1mm-wide pad centered at x so its right edge sits 0.05mm from the
        # fill edge at x=100: center_x = 100 - 0.5 - 0.05.
        pad = _FakePad(position=(100.0 - 0.5 - 0.05, 105.0), size=(1.0, 1.0))
        fp = _FakeFootprint(pads=[pad])
        results = _run([zone], footprints=[fp])
        assert len(results.violations) == 1
        v = results.violations[0]
        assert "Short" not in v.message
        assert abs(v.actual_value - 0.05) < 1e-6

    def test_pad_adequate_gap_passes(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        pad = _FakePad(position=(100.0 - 0.5 - 0.3, 105.0), size=(1.0, 1.0))
        fp = _FakeFootprint(pads=[pad])
        results = _run([zone], footprints=[fp])
        assert results.violations == []

    def test_through_hole_pad_wildcard_layer_checked(self):
        """A ``*.Cu`` pad is copper on every layer, incl. an inner fill."""
        zone = _FakeZone(
            net_number=2,
            net_name="+3V3",
            layer="In1.Cu",
            filled_polygons=[_SQUARE_FILL],
            filled_polygon_layers=["In1.Cu"],
        )
        pad = _FakePad(position=(105.0, 105.0), size=(1.0, 1.0), layers=("*.Cu",))
        fp = _FakeFootprint(pads=[pad])
        results = _run([zone], footprints=[fp])
        assert len(results.violations) == 1
        assert results.violations[0].layer == "In1.Cu"

    def test_net0_pad_skipped(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        pad = _FakePad(position=(105.0, 105.0), net_number=0)
        fp = _FakeFootprint(pads=[pad])
        results = _run([zone], footprints=[fp])
        assert results.violations == []


# ---------------------------------------------------------------------------
# Rounded-pad geometry regression (Issue #3826)
#
# The fill occupies x,y in [100,110].  Its bottom-left corner is at
# (100, 100).  We place a 1x1 mm pad in the third quadrant relative to
# that corner so ONLY the pad's top-right corner is near the fill.  For a
# roundrect/oval pad the rounded corner clears the fill, while the
# square AABB used by the old code pokes into it -> phantom short.
# ---------------------------------------------------------------------------


class TestRoundedPadCornerNoPhantomShort:
    # min_clearance well below the AABB poke-in so we isolate the
    # short/overlap behavior, not a clearance graze.
    _MIN_CLEARANCE = 0.001

    def _pad_corner_overlap_setup(self, shape, rratio=0.25):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        # AABB corner of the pad pokes ~0.06mm diagonally into the fill
        # corner: place pad center so its top-right AABB corner is at
        # (100.04, 100.04) -- inside the fill -- but the rounded corner
        # (radius 0.25mm) is pulled back inboard and clears (100,100).
        cx = 100.04 - 0.5
        cy = 100.04 - 0.5
        pad = _FakePad(position=(cx, cy), size=(1.0, 1.0), shape=shape, roundrect_rratio=rratio)
        fp = _FakeFootprint(pads=[pad], reference="U3")
        return zone, fp

    def test_rect_pad_aabb_corner_overlaps_is_short(self):
        # Control: a plain rect pad's sharp corner genuinely overlaps the
        # fill corner, so a violation MUST be reported.
        zone, fp = self._pad_corner_overlap_setup("rect")
        results = _run([zone], footprints=[fp], min_clearance=self._MIN_CLEARANCE)
        shorts = [v for v in results.violations if v.rule_id == "clearance_pad_zone"]
        assert len(shorts) == 1, "rect pad with overlapping corner must short"

    def test_roundrect_pad_corner_clears_no_phantom_short(self):
        # The bug: the roundrect's rounded corner clears the fill, so the
        # NEW true-geometry code must report NO short (old AABB code did).
        zone, fp = self._pad_corner_overlap_setup("roundrect")
        results = _run([zone], footprints=[fp], min_clearance=self._MIN_CLEARANCE)
        shorts = [v for v in results.violations if v.rule_id == "clearance_pad_zone"]
        assert shorts == [], "roundrect rounded corner must not phantom-short"

    def test_oval_pad_corner_clears_no_phantom_short(self):
        zone, fp = self._pad_corner_overlap_setup("oval")
        results = _run([zone], footprints=[fp], min_clearance=self._MIN_CLEARANCE)
        shorts = [v for v in results.violations if v.rule_id == "clearance_pad_zone"]
        assert shorts == [], "oval (circular) corner must not phantom-short"

    def test_roundrect_genuine_body_overlap_still_shorts(self):
        # Guard: a roundrect pad whose BODY (not just the phantom corner)
        # overlaps the fill must still be flagged.
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        pad = _FakePad(position=(100.4, 105.0), size=(1.0, 1.0), shape="roundrect")
        fp = _FakeFootprint(pads=[pad], reference="U4")
        results = _run([zone], footprints=[fp], min_clearance=self._MIN_CLEARANCE)
        shorts = [v for v in results.violations if v.rule_id == "clearance_pad_zone"]
        assert len(shorts) == 1, "deep roundrect body overlap must still short"


# ---------------------------------------------------------------------------
# Layer discipline
# ---------------------------------------------------------------------------


class TestViaLayers:
    def test_fill_on_unspanned_layer_ignored_for_pad(self):
        zone = _FakeZone(
            filled_polygons=[_SQUARE_FILL],
            filled_polygon_layers=["B.Cu"],
        )
        pad = _FakePad(position=(105.0, 105.0), layers=("F.Cu",))
        fp = _FakeFootprint(pads=[pad])
        results = _run([zone], footprints=[fp])
        assert results.violations == []
