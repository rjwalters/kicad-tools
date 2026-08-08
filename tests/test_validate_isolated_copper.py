"""Tests for the isolated-copper DRC rule (Issue #4680, second slice).

``kicad-cli pcb drc`` reports ``isolated_copper`` (199 findings on the
reporting board -- orphaned zone-fill islands on ground/supply planes)
but ``kct check`` had no detection for the class.  ``IsolatedCopperRule``
closes the gap natively on the committed ``filled_polygon`` copper.

Since #4729 the predicate is KiCad's own: an island is isolated iff its
transitive same-net copper cluster contains **no pad**.  Tracks, arcs and
via barrels are *conductors* that extend the cluster, not terminals that
satisfy it.

Scenario coverage (per the #4680 / #4729 acceptance criteria):

* orphan island (no copper of its net touches it) -> one
  ``isolated_copper`` warning per island, kicad-cli-parity message
  (``Zone [NET] on LAYER, priority N``);
* negative cases: island whose cluster reaches a pad -- directly, over a
  multi-hop track chain, or across a via to another layer;
* positive cases: island held only by pad-less copper (a lone track, a
  lone via, a track chain ending nowhere) -> flagged, matching KiCad;
* foreign-net copper never conducts (that is a clearance problem, not a
  connection);
* island transitivity: an island touching a pad-bearing island is
  connected; two touching orphans are both flagged;
* the degrade-silently guard: zones with NO committed fill polygons
  produce no findings (never a false "0 isolated" clean bill);
* layer resolution honors per-polygon ``filled_polygon_layers`` --
  copper on another layer only conducts through a via;
* everything is ``severity="warning"`` and classified advisory-quality
  for reporting but NOT gating-advisory;
* end-to-end kicad-cli parity on ``tests/fixtures/drc/orphan_island*``
  (``TestPadInClusterSemantics``): 2 / 1 / 1 findings, matching
  kicad-cli 10.0.5 on the committed fills (no ``--refill-zones``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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

    def test_foreign_net_pad_does_not_connect(self):
        """A foreign-net pad on the island is a clearance problem, not a
        connection."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        fp = _FakeFootprint(pads=[_FakePad(position=(105.0, 105.0), net_number=2, net_name="+3V3")])
        results = _run(footprints=[fp], zones=[zone])
        assert len(_flagged(results)) == 1

    def test_foreign_net_track_does_not_conduct(self):
        """A foreign-net track crossing the island never carries the
        island's cluster to that net's pads."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        seg = _FakeSegment(start=(95.0, 105.0), end=(115.0, 105.0), net_number=2, net_name="+3V3")
        fp = _FakeFootprint(pads=[_FakePad(position=(115.0, 105.0), net_number=2, net_name="+3V3")])
        results = _run(segments=[seg], footprints=[fp], zones=[zone])
        assert len(_flagged(results)) == 1

    def test_pad_on_other_layer_does_not_connect(self):
        """A same-net pad on ANOTHER layer needs a via to conduct."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL], layer="B.Cu")
        fp = _FakeFootprint(pads=[_FakePad(position=(105.0, 105.0), layers=("F.Cu",))])
        results = _run(footprints=[fp], zones=[zone])

        flagged = _flagged(results)
        assert len(flagged) == 1
        assert flagged[0].layer == "B.Cu"

    def test_per_polygon_layer_resolution(self):
        """A multi-layer zone resolves each island's layer from
        ``filled_polygon_layers``: the In1.Cu island is orphaned even
        though the F.Cu island of the same zone reaches a pad."""
        layers = [_FakeLayer("F.Cu"), _FakeLayer("In1.Cu"), _FakeLayer("B.Cu")]
        zone = _FakeZone(
            filled_polygons=[_SQUARE_FILL, _SQUARE_FILL],
            filled_polygon_layers=["F.Cu", "In1.Cu"],
        )
        fp = _FakeFootprint(pads=[_FakePad(position=(105.0, 105.0), layers=("F.Cu",))])
        results = _run(footprints=[fp], zones=[zone], layers=layers)

        flagged = _flagged(results)
        assert len(flagged) == 1
        assert flagged[0].layer == "In1.Cu"
        assert "on In1.Cu" in flagged[0].message

    def test_priority_reported_in_message(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL], priority=2)
        results = _run(zones=[zone])
        assert "priority 2" in _flagged(results)[0].message


# ---------------------------------------------------------------------------
# isolated_copper: negative (pad reached) cases
# ---------------------------------------------------------------------------


class TestIslandReachesPad:
    def test_pad_directly_on_island(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        fp = _FakeFootprint(pads=[_FakePad(position=(105.0, 105.0))])
        results = _run(footprints=[fp], zones=[zone])
        assert _flagged(results) == []

    def test_pad_reached_through_one_track(self):
        """A track from the island to an off-island pad connects it."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        seg = _FakeSegment(start=(95.0, 105.0), end=(105.0, 105.0))
        fp = _FakeFootprint(pads=[_FakePad(position=(95.0, 105.0))])
        results = _run(segments=[seg], footprints=[fp], zones=[zone])
        assert _flagged(results) == []

    def test_pad_reached_through_multi_hop_track_chain(self):
        """Transitive reach: island -- track -- track -- pad.

        One-hop pad touch is not sufficient for KiCad parity; the
        segment-to-segment edge is load-bearing (#4729).
        """
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        seg_a = _FakeSegment(start=(95.0, 105.0), end=(105.0, 105.0))
        seg_b = _FakeSegment(start=(85.0, 105.0), end=(95.0, 105.0))
        fp = _FakeFootprint(pads=[_FakePad(position=(85.0, 105.0))])
        results = _run(segments=[seg_a, seg_b], footprints=[fp], zones=[zone])
        assert _flagged(results) == []

    def test_pad_reached_across_a_via_on_another_layer(self):
        """Cross-layer bridge: F.Cu island -- via -- B.Cu pad.

        The via is indexed once per spanned layer under a shared key;
        unioning those instances is what carries the cluster across.
        """
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL], layer="F.Cu")
        via = _FakeVia(position=(105.0, 105.0))
        fp = _FakeFootprint(pads=[_FakePad(position=(105.0, 105.0), layers=("B.Cu",))])
        results = _run(vias=[via], footprints=[fp], zones=[zone])
        assert _flagged(results) == []

    def test_through_hole_pad_on_inner_layer(self):
        """A ``*.Cu`` through-hole pad connects an inner-layer island."""
        layers = [_FakeLayer("F.Cu"), _FakeLayer("In1.Cu"), _FakeLayer("B.Cu")]
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL], filled_polygon_layers=["In1.Cu"])
        fp = _FakeFootprint(
            pads=[_FakePad(position=(105.0, 105.0), layers=("*.Cu", "*.Mask"), drill=0.8)]
        )
        results = _run(footprints=[fp], zones=[zone], layers=layers)
        assert _flagged(results) == []

    def test_edge_touching_pad_counts(self):
        """A pad exactly at the island edge (distance 0) connects."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        # Pad square centered so its right edge lands on the x=100 fill edge
        fp = _FakeFootprint(pads=[_FakePad(position=(99.5, 105.0))])
        results = _run(footprints=[fp], zones=[zone])
        assert _flagged(results) == []


# ---------------------------------------------------------------------------
# isolated_copper: pad-less clusters ARE isolated (KiCad parity, #4729)
# ---------------------------------------------------------------------------


class TestPadlessClusterIsIsolated:
    def test_island_held_only_by_padless_track_flagged(self):
        """A same-net stub with no pad anywhere in its cluster does NOT
        connect the island -- kicad-cli reports ``isolated_copper``."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        seg = _FakeSegment(start=(95.0, 105.0), end=(105.0, 105.0))
        results = _run(segments=[seg], zones=[zone])
        assert len(_flagged(results)) == 1

    def test_island_held_only_by_padless_via_flagged(self):
        """Same with a via as the sole (pad-less) contact."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        via = _FakeVia(position=(105.0, 105.0))
        results = _run(vias=[via], zones=[zone])
        assert len(_flagged(results)) == 1

    def test_padless_track_chain_flagged(self):
        """A multi-hop chain that never reaches a pad is still isolated."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        seg_a = _FakeSegment(start=(95.0, 105.0), end=(105.0, 105.0))
        seg_b = _FakeSegment(start=(85.0, 105.0), end=(95.0, 105.0))
        results = _run(segments=[seg_a, seg_b], zones=[zone])
        assert len(_flagged(results)) == 1

    def test_padless_cross_layer_cluster_flagged(self):
        """A via bridging two pad-less layers does not connect either."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL], layer="F.Cu")
        via = _FakeVia(position=(105.0, 105.0))
        seg = _FakeSegment(start=(105.0, 105.0), end=(115.0, 105.0), layer="B.Cu")
        results = _run(segments=[seg], vias=[via], zones=[zone])
        assert len(_flagged(results)) == 1


# ---------------------------------------------------------------------------
# Same-layer same-net island transitivity
# ---------------------------------------------------------------------------


class TestIslandTransitivity:
    def test_island_touching_pad_bearing_island_is_connected(self):
        """Island B touches island A which carries a pad (same net, same
        layer) -> both connected (KiCad clusters fills geometrically)."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL, _TOUCHING_FILL])
        fp = _FakeFootprint(pads=[_FakePad(position=(105.0, 105.0))])
        results = _run(footprints=[fp], zones=[zone])
        assert _flagged(results) == []

    def test_transitivity_across_zones(self):
        """The touching islands may belong to DIFFERENT same-net zones."""
        zone_a = _FakeZone(filled_polygons=[_SQUARE_FILL])
        zone_b = _FakeZone(filled_polygons=[_TOUCHING_FILL])
        fp = _FakeFootprint(pads=[_FakePad(position=(105.0, 105.0))])
        results = _run(footprints=[fp], zones=[zone_a, zone_b])
        assert _flagged(results) == []

    def test_two_touching_orphans_both_flagged(self):
        """A cluster with NO pad is isolated -- every member island is
        reported (one finding per island, kicad-cli parity)."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL, _TOUCHING_FILL])
        results = _run(zones=[zone])
        assert len(_flagged(results)) == 2

    def test_foreign_net_island_does_not_bridge(self):
        """A touching island of a DIFFERENT net never conducts the pad
        across."""
        gnd_zone = _FakeZone(filled_polygons=[_TOUCHING_FILL])
        foreign = _FakeZone(net_number=2, net_name="+3V3", filled_polygons=[_SQUARE_FILL])
        # The pad sits on the foreign island only
        fp = _FakeFootprint(pads=[_FakePad(position=(105.0, 105.0), net_number=2, net_name="+3V3")])
        results = _run(footprints=[fp], zones=[gnd_zone, foreign])

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
        fp = _FakeFootprint(pads=[_FakePad(position=(105.0, 105.0))])
        results = _run(footprints=[fp], zones=[zone])
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
    def test_both_padless_islands_flagged(self, tmp_path):
        """On a parsed board: BOTH islands of the zone are isolated --
        the geometric orphan, and the island carrying only a pad-less
        GND track + via.  kicad-cli 10.0.5 reports 2 here as well.
        """
        from kicad_tools.schema.pcb import PCB

        board = tmp_path / "filled_board.kicad_pcb"
        board.write_text(_FILLED_BOARD)
        pcb = PCB.load(str(board))

        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_isolated_copper()

        flagged = _flagged(results)
        assert len(flagged) == 2
        assert all("Zone [GND] on F.Cu" in v.message for v in flagged)
        # One finding lands in each island
        located = sorted(v.location for v in flagged)
        assert 3.0 <= located[0][0] <= 12.0 and 3.0 <= located[0][1] <= 12.0
        assert 25.0 <= located[1][0] <= 35.0 and 25.0 <= located[1][1] <= 35.0


# ---------------------------------------------------------------------------
# KiCad pad-in-cluster semantics: kicad-cli parity on the disk fixtures
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures" / "drc"


def _fixture_finding_count(name: str) -> int:
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(_FIXTURES / name))
    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
    return len(_flagged(checker.check_isolated_copper()))


class TestPadInClusterSemantics:
    """kicad-cli parity for KiCad's actual predicate (#4729).

    KiCad flags an island whose copper connectivity cluster contains no
    **pad**, reached transitively through tracks, vias and touching
    fills.  Before #4729 this rule flagged only islands touched by no
    same-net copper at all, a strict subset -- so a pad-less track or
    via cleared kct's test but not KiCad's.

    Counts below were measured on kicad-cli 10.0.5 against the committed
    fills (**no** ``--refill-zones``), on the very fixtures loaded here:

    ==========================================  =========  ===
    Fixture                                     kicad-cli  kct
    ==========================================  =========  ===
    ``orphan_island.kicad_pcb``                 2          2
    ``orphan_island_pad.kicad_pcb``             1          1
    ``orphan_island_transitive.kicad_pcb``      1          1
    ==========================================  =========  ===
    """

    def test_padless_cluster_reports_both_islands(self):
        """No pad anywhere: island A (track + via) and island B (a
        geometric orphan) are both isolated."""
        assert _fixture_finding_count("orphan_island.kicad_pcb") == 2

    def test_pad_directly_on_island_clears_it(self):
        """A GND pad on island A leaves only the orphan island B."""
        assert _fixture_finding_count("orphan_island_pad.kicad_pcb") == 1

    def test_pad_reached_through_track_chain_clears_island(self):
        """A GND pad off-island, reached through a two-segment chain,
        clears island A -- transitive reach, not one-hop touch."""
        assert _fixture_finding_count("orphan_island_transitive.kicad_pcb") == 1

    def test_inline_fixture_matches_disk_fixture(self, tmp_path):
        """``_FILLED_BOARD`` and ``orphan_island.kicad_pcb`` are the same
        board -- guard against the two copies drifting apart."""
        assert (_FIXTURES / "orphan_island.kicad_pcb").read_text() == _FILLED_BOARD

    def test_dangling_copper_semantics_unchanged(self, tmp_path):
        """#4680 slice 1 is untouched: the pad-less stub still terminates
        on the same-net committed fill, so ``track_dangling`` stays
        silent (only ``isolated_copper`` changed)."""
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(_FIXTURES / "orphan_island.kicad_pcb"))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        dangling = checker.check_dangling_copper()
        assert [v for v in dangling.violations if v.rule_id == "track_dangling"] == []
