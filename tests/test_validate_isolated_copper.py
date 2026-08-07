"""Tests for the isolated-copper DRC rule (Issue #4680, second slice).

``kicad-cli pcb drc`` reports ``isolated_copper`` (199 findings on the
reporting board -- orphaned zone-fill islands on ground/supply planes)
but ``kct check`` had no detection for the class.  ``IsolatedCopperRule``
closes the gap natively on the committed ``filled_polygon`` copper.

Scenario coverage (per the #4680 acceptance criteria):

* orphan island (no copper of its net touches it) -> one
  ``isolated_copper`` warning per island, kicad-cli-parity message
  (``Zone [NET] on LAYER, priority N``);
* negative cases: island anchored by a same-net pad, via, track
  segment;
* foreign-net copper does NOT anchor an island (that is a clearance
  problem, not a connection);
* same-layer same-net island transitivity: an island touching an
  anchored island is connected; two touching orphans are both flagged;
* the degrade-silently guard: zones with NO committed fill polygons
  produce no findings (never a false "0 isolated" clean bill);
* layer resolution honors per-polygon ``filled_polygon_layers`` --
  copper on another layer never anchors;
* everything is ``severity="warning"`` and classified advisory-quality
  for reporting but NOT gating-advisory;
* the **known divergence** from KiCad's pad-in-cluster semantics is
  pinned (``TestKnownDivergencePadCluster``) so the current, narrower
  predicate cannot change silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kicad_tools.validate import DRCChecker
from kicad_tools.validate.rules.zone_fill import IsolatedCopperRule

# ---------------------------------------------------------------------------
# Minimal stubs mirroring the fields the rule reads
# ---------------------------------------------------------------------------


@dataclass
class _FakeNet:
    number: int
    name: str


@dataclass
class _FakeLayer:
    name: str
    type: str = "signal"


@dataclass
class _FakeSegment:
    start: tuple[float, float]
    end: tuple[float, float]
    width: float = 0.2
    layer: str = "F.Cu"
    net_number: int = 1
    net_name: str = "GND"


@dataclass
class _FakeVia:
    position: tuple[float, float]
    size: float = 0.6
    drill: float = 0.3
    layers: tuple[str, ...] = ("F.Cu", "B.Cu")
    net_number: int = 1
    net_name: str = "GND"


@dataclass
class _FakePad:
    position: tuple[float, float]
    size: tuple[float, float] = (1.0, 1.0)
    layers: tuple[str, ...] = ("F.Cu",)
    net_number: int = 1
    net_name: str = "GND"
    number: str = "1"
    rotation: float = 0.0
    shape: str = "rect"
    roundrect_rratio: float = 0.25
    drill: float = 0.0


@dataclass
class _FakeFootprint:
    pads: list[_FakePad] = field(default_factory=list)
    position: tuple[float, float] = (0.0, 0.0)
    rotation: float = 0.0
    reference: str = "U1"


@dataclass
class _FakeZone:
    net_number: int = 1
    net_name: str = "GND"
    layer: str = "F.Cu"
    priority: int = 0
    filled_polygons: list[list[tuple[float, float]]] = field(default_factory=list)
    filled_polygon_layers: list[str] = field(default_factory=list)

    def filled_polygon_layer(self, index: int) -> str:
        if index < len(self.filled_polygon_layers) and self.filled_polygon_layers[index]:
            return self.filled_polygon_layers[index]
        return self.layer


@dataclass
class _FakePCB:
    segments: list[_FakeSegment] = field(default_factory=list)
    vias: list[_FakeVia] = field(default_factory=list)
    footprints: list[_FakeFootprint] = field(default_factory=list)
    zones: list[_FakeZone] = field(default_factory=list)
    nets: dict[int, _FakeNet] = field(default_factory=dict)
    copper_layers: list[_FakeLayer] = field(default_factory=list)
    board_origin: tuple[float, float] = (0.0, 0.0)


_NETS = {1: _FakeNet(1, "GND"), 2: _FakeNet(2, "+3V3")}
_TWO_LAYERS = [_FakeLayer("F.Cu"), _FakeLayer("B.Cu")]


def _run(segments=None, vias=None, footprints=None, zones=None, layers=None):
    pcb = _FakePCB(
        segments=segments or [],
        vias=vias or [],
        footprints=footprints or [],
        zones=zones or [],
        nets=dict(_NETS),
        copper_layers=layers if layers is not None else list(_TWO_LAYERS),
    )
    rule = IsolatedCopperRule()
    return rule.check(pcb, design_rules=None)


def _flagged(results):
    return [v for v in results.violations if v.rule_id == "isolated_copper"]


# A 10x10 mm square fill at (100,100)..(110,110)
_SQUARE_FILL = [(100.0, 100.0), (110.0, 100.0), (110.0, 110.0), (100.0, 110.0)]
# A second square fill sharing the x=110 edge with _SQUARE_FILL
_TOUCHING_FILL = [(110.0, 100.0), (120.0, 100.0), (120.0, 110.0), (110.0, 110.0)]
# A far-away square fill (no contact with anything above)
_FAR_FILL = [(50.0, 50.0), (55.0, 50.0), (55.0, 55.0), (50.0, 55.0)]


# ---------------------------------------------------------------------------
# isolated_copper: positive cases
# ---------------------------------------------------------------------------


class TestIsolatedCopper:
    def test_orphan_island_flagged(self):
        """A fill island touched by no copper of its net -> one warning
        with the kicad-cli-parity message shape."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        results = _run(zones=[zone])

        flagged = _flagged(results)
        assert len(flagged) == 1
        v = flagged[0]
        assert v.severity == "warning"
        assert "Zone [GND] on F.Cu, priority 0" in v.message
        assert v.layer == "F.Cu"
        # actual_value carries the island area (100 mm2 square)
        assert v.actual_value is not None and abs(v.actual_value - 100.0) < 1e-6
        # Location is inside the island
        x, y = v.location
        assert 100.0 <= x <= 110.0 and 100.0 <= y <= 110.0

    def test_one_violation_per_island(self):
        """Two orphan islands of one zone -> two findings (kicad-cli
        reports per island, not per zone)."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL, _FAR_FILL])
        results = _run(zones=[zone])
        assert len(_flagged(results)) == 2

    def test_foreign_net_track_does_not_anchor(self):
        """A foreign-net track crossing the island is a clearance
        problem, not a connection."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        seg = _FakeSegment(start=(95.0, 105.0), end=(115.0, 105.0), net_number=2, net_name="+3V3")
        results = _run(segments=[seg], zones=[zone])
        assert len(_flagged(results)) == 1

    def test_copper_on_other_layer_does_not_anchor(self):
        """Same-net copper on ANOTHER layer never anchors an island."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL], layer="B.Cu")
        seg = _FakeSegment(start=(95.0, 105.0), end=(115.0, 105.0), layer="F.Cu")
        results = _run(segments=[seg], zones=[zone])

        flagged = _flagged(results)
        assert len(flagged) == 1
        assert flagged[0].layer == "B.Cu"

    def test_per_polygon_layer_resolution(self):
        """A multi-layer zone resolves each island's layer from
        ``filled_polygon_layers``: the In1.Cu island is orphaned even
        though the F.Cu island of the same zone is anchored."""
        layers = [_FakeLayer("F.Cu"), _FakeLayer("In1.Cu"), _FakeLayer("B.Cu")]
        zone = _FakeZone(
            filled_polygons=[_SQUARE_FILL, _SQUARE_FILL],
            filled_polygon_layers=["F.Cu", "In1.Cu"],
        )
        seg = _FakeSegment(start=(95.0, 105.0), end=(105.0, 105.0), layer="F.Cu")
        results = _run(segments=[seg], zones=[zone], layers=layers)

        flagged = _flagged(results)
        assert len(flagged) == 1
        assert flagged[0].layer == "In1.Cu"
        assert "on In1.Cu" in flagged[0].message

    def test_priority_reported_in_message(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL], priority=2)
        results = _run(zones=[zone])
        assert "priority 2" in _flagged(results)[0].message


# ---------------------------------------------------------------------------
# isolated_copper: negative (anchored) cases
# ---------------------------------------------------------------------------


class TestIslandAnchored:
    def test_anchored_by_same_net_segment(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        seg = _FakeSegment(start=(95.0, 105.0), end=(105.0, 105.0))
        results = _run(segments=[seg], zones=[zone])
        assert _flagged(results) == []

    def test_anchored_by_same_net_via(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        via = _FakeVia(position=(105.0, 105.0))
        results = _run(vias=[via], zones=[zone])
        assert _flagged(results) == []

    def test_anchored_by_same_net_pad(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        fp = _FakeFootprint(pads=[_FakePad(position=(105.0, 105.0))])
        results = _run(footprints=[fp], zones=[zone])
        assert _flagged(results) == []

    def test_anchored_by_through_hole_pad_on_inner_layer(self):
        """A ``*.Cu`` through-hole pad anchors an inner-layer island."""
        layers = [_FakeLayer("F.Cu"), _FakeLayer("In1.Cu"), _FakeLayer("B.Cu")]
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL], filled_polygon_layers=["In1.Cu"])
        fp = _FakeFootprint(
            pads=[_FakePad(position=(105.0, 105.0), layers=("*.Cu", "*.Mask"), drill=0.8)]
        )
        results = _run(footprints=[fp], zones=[zone], layers=layers)
        assert _flagged(results) == []

    def test_edge_touching_anchor_counts(self):
        """An anchor exactly at the island edge (distance 0) anchors."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        # Track ending exactly on the x=100 fill edge
        seg = _FakeSegment(start=(95.0, 105.0), end=(100.0, 105.0))
        results = _run(segments=[seg], zones=[zone])
        assert _flagged(results) == []


# ---------------------------------------------------------------------------
# Same-layer same-net island transitivity
# ---------------------------------------------------------------------------


class TestIslandTransitivity:
    def test_island_touching_anchored_island_is_connected(self):
        """Island B touches anchored island A (same net, same layer) ->
        both connected (KiCad clusters fills geometrically)."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL, _TOUCHING_FILL])
        seg = _FakeSegment(start=(95.0, 105.0), end=(105.0, 105.0))
        results = _run(segments=[seg], zones=[zone])
        assert _flagged(results) == []

    def test_transitivity_across_zones(self):
        """The touching islands may belong to DIFFERENT same-net zones."""
        zone_a = _FakeZone(filled_polygons=[_SQUARE_FILL])
        zone_b = _FakeZone(filled_polygons=[_TOUCHING_FILL])
        seg = _FakeSegment(start=(95.0, 105.0), end=(105.0, 105.0))
        results = _run(segments=[seg], zones=[zone_a, zone_b])
        assert _flagged(results) == []

    def test_two_touching_orphans_both_flagged(self):
        """A cluster with NO anchor is isolated -- every member island
        is reported (one finding per island, kicad-cli parity)."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL, _TOUCHING_FILL])
        results = _run(zones=[zone])
        assert len(_flagged(results)) == 2

    def test_foreign_net_island_does_not_bridge(self):
        """A touching island of a DIFFERENT net never conducts the
        anchor across."""
        gnd_zone = _FakeZone(filled_polygons=[_TOUCHING_FILL])
        foreign = _FakeZone(net_number=2, net_name="+3V3", filled_polygons=[_SQUARE_FILL])
        # Anchor touches only the foreign island
        seg = _FakeSegment(start=(95.0, 105.0), end=(105.0, 105.0), net_number=2, net_name="+3V3")
        results = _run(segments=[seg], zones=[gnd_zone, foreign])

        flagged = _flagged(results)
        assert len(flagged) == 1
        assert "GND" in flagged[0].message


# ---------------------------------------------------------------------------
# Degrade-silently guard + subject filtering
# ---------------------------------------------------------------------------


class TestDegradeSilently:
    def test_unfilled_zone_reports_nothing(self):
        """No committed fill polygons -> no findings, never a false
        clean bill (ZoneFillRule owns the unfilled-zone warning)."""
        zone = _FakeZone(filled_polygons=[])
        results = _run(zones=[zone])
        assert results.violations == []

    def test_no_zones_reports_nothing(self):
        results = _run(segments=[_FakeSegment(start=(0.0, 0.0), end=(5.0, 0.0))])
        assert _flagged(results) == []

    def test_no_net_zone_skipped(self):
        """A zone with no resolvable net has no connectivity contract
        (``zone_no_net`` owns that finding)."""
        zone = _FakeZone(net_number=0, net_name="", filled_polygons=[_SQUARE_FILL])
        results = _run(zones=[zone])
        assert _flagged(results) == []

    def test_degenerate_polygon_skipped(self):
        zone = _FakeZone(filled_polygons=[[(0.0, 0.0), (1.0, 1.0)]])
        results = _run(zones=[zone])
        assert _flagged(results) == []

    def test_name_only_net_resolved(self):
        """KiCad-10 name-only zone net dialect resolves by name."""
        zone = _FakeZone(net_number=0, net_name="GND", filled_polygons=[_SQUARE_FILL])
        seg = _FakeSegment(start=(95.0, 105.0), end=(105.0, 105.0))
        results = _run(segments=[seg], zones=[zone])
        assert _flagged(results) == []


# ---------------------------------------------------------------------------
# Severity / taxonomy / registration
# ---------------------------------------------------------------------------


class TestSeverityAndTaxonomy:
    def test_all_findings_are_warnings(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL, _FAR_FILL])
        results = _run(zones=[zone])
        assert results.violations
        assert all(v.severity == "warning" for v in results.violations)
        assert results.error_count == 0

    def test_reporting_category_is_advisory(self):
        assert DRCChecker.category_for_rule("isolated_copper") == DRCChecker.CATEGORY_ADVISORY

    def test_not_gating_advisory(self):
        """Gating set membership is unchanged (warnings never block, so
        no ADVISORY_RULE_IDS entry is needed -- documented in #4680)."""
        assert DRCChecker.is_advisory_rule("isolated_copper") is False

    def test_registered_in_check_all(self):
        assert "check_isolated_copper" in DRCChecker.CHECK_ALL_METHODS

    def test_rules_checked_bookkeeping(self):
        results = _run()
        assert results.rules_checked_by_rule.get("isolated_copper") == 1


# ---------------------------------------------------------------------------
# End-to-end on a real parsed board (checker path)
# ---------------------------------------------------------------------------

_FILLED_BOARD = """(kicad_pcb (version 20240108) (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (gr_line (start 0 0) (end 40 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 40 0) (end 40 40) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 40 40) (end 0 40) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 40) (end 0 0) (layer "Edge.Cuts") (width 0.1))
  (segment (start 5 5) (end 10 8) (width 0.3) (layer "F.Cu") (net 1))
  (via (at 5 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (zone (net 1) (net_name "GND") (layer "F.Cu") (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.2) (filled_areas_thickness no)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 2 2) (xy 38 2) (xy 38 38) (xy 2 38)))
    (filled_polygon (layer "F.Cu")
      (pts (xy 3 3) (xy 12 3) (xy 12 12) (xy 3 12)))
    (filled_polygon (layer "F.Cu")
      (pts (xy 25 25) (xy 35 25) (xy 35 35) (xy 25 35)))
  )
)
"""


class TestEndToEndParsedBoard:
    def test_orphan_island_flagged_connected_island_clean(self, tmp_path):
        """On a parsed board: the island containing the GND track/via is
        clean; the far corner island of the same zone is isolated.

        NOTE: kicad-cli 10.0.5 reports **2** isolated_copper on this
        board (the track+via island has no pad in its cluster) -- the
        documented pad-cluster divergence, pinned by
        ``TestKnownDivergencePadCluster`` below.
        """
        from kicad_tools.schema.pcb import PCB

        board = tmp_path / "filled_board.kicad_pcb"
        board.write_text(_FILLED_BOARD)
        pcb = PCB.load(str(board))

        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_isolated_copper()

        flagged = _flagged(results)
        assert len(flagged) == 1
        assert "Zone [GND] on F.Cu" in flagged[0].message
        # The orphan is the (25,25)..(35,35) island
        x, y = flagged[0].location
        assert 25.0 <= x <= 35.0 and 25.0 <= y <= 35.0


# ---------------------------------------------------------------------------
# Known divergence: KiCad's pad-in-cluster semantics (pinned, NOT implemented)
# ---------------------------------------------------------------------------


class TestKnownDivergencePadCluster:
    """Pin the documented divergence from KiCad's actual predicate.

    KiCad flags an island whose copper connectivity cluster contains no
    **pad** (reached transitively through tracks/vias/touching fills).
    This rule flags an island whose cluster is touched by no same-net
    copper *at all*.  A pad-less track or via therefore clears kct's test
    but not KiCad's, so kct's findings are a strict **subset** of
    kicad-cli's: never a false positive, but pad-less clusters are
    under-reported.

    Measured on KiCad 10.0.5 against the committed fills (no
    ``--refill-zones``); full evidence table in the *Known divergences*
    section of :mod:`kicad_tools.validate.rules.zone_fill`.  These tests
    assert the CURRENT behavior deliberately -- implementing full
    pad-cluster semantics (tracked as #4729) should update them as a
    visible decision, not silently flip a passing suite.
    """

    def test_island_held_only_by_padless_track_not_flagged(self):
        """A same-net stub with no pad anywhere in its cluster anchors
        the island here; kicad-cli still reports ``isolated_copper``."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        seg = _FakeSegment(start=(95.0, 105.0), end=(105.0, 105.0))
        results = _run(segments=[seg], zones=[zone])
        assert _flagged(results) == []

    def test_island_held_only_by_padless_via_not_flagged(self):
        """Same divergence with a via as the sole (pad-less) anchor."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        via = _FakeVia(position=(105.0, 105.0))
        results = _run(vias=[via], zones=[zone])
        assert _flagged(results) == []

    def test_padless_cluster_end_to_end(self, tmp_path):
        """Judge fixture for #4728: island A carries a same-net track
        AND via but no pad.

        kicad-cli 10.0.5 (no refill) reports **2** ``isolated_copper``
        on this board -- both islands, because neither cluster reaches a
        pad.  kct reports **1** (the geometric orphan only).
        """
        from kicad_tools.schema.pcb import PCB

        board = tmp_path / "padless_cluster.kicad_pcb"
        board.write_text(_FILLED_BOARD)
        pcb = PCB.load(str(board))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)

        # kct: 1 (kicad-cli: 2) -- the divergence, in numbers.
        assert len(_flagged(checker.check_isolated_copper())) == 1

        # Compounding blind spot with the #4680 first slice: the
        # pad-less stub terminates on the same-net committed fill, so
        # ``track_dangling`` does not see it either.  (Its via barrel
        # bonds only F.Cu, so ``via_dangling`` still fires -- this
        # fixture is not entirely silent, but the isolated island that
        # kicad-cli names is invisible to both kct rules.)
        dangling = checker.check_dangling_copper()
        assert [v for v in dangling.violations if v.rule_id == "track_dangling"] == []
