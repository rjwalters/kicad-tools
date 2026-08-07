"""Tests for the dangling-copper DRC rule (Issue #4680).

``kicad-cli pcb drc`` reports ``track_dangling`` (50 findings on the
reporting board) and ``via_dangling`` (19) but ``kct check`` had no
detection for either class -- the same gap shape as #4612's
``silk_overlap``.  ``DanglingCopperRule`` closes the gap natively.

Scenario coverage (per the #4680 acceptance criteria):

* dangling stub (free track end) -> one ``track_dangling`` warning
  per track, at the first dangling endpoint;
* negative cases: stub terminated by a pad, a via, another track's
  endpoint or body (T-junction), a same-net pour;
* dangling via (0-layer and 1-layer bond) -> ``via_dangling``;
* the classic false positive: a via bonded ONLY by pour copper on two
  layers must NOT be flagged;
* foreign-net pour does NOT terminate a track end (that is a short,
  owned by the clearance rules);
* everything is ``severity="warning"`` (KiCad default severity parity)
  and classified advisory-quality for reporting but NOT gating-advisory.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kicad_tools.validate import DRCChecker
from kicad_tools.validate.rules.dangling_copper import DanglingCopperRule

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
    net_name: str = "SIG"


@dataclass
class _FakeVia:
    position: tuple[float, float]
    size: float = 0.6
    drill: float = 0.3
    layers: tuple[str, ...] = ("F.Cu", "B.Cu")
    net_number: int = 1
    net_name: str = "SIG"


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
    net_name: str = "SIG"
    layer: str = "F.Cu"
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


_NETS = {1: _FakeNet(1, "SIG"), 2: _FakeNet(2, "+3V3")}
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
    rule = DanglingCopperRule()
    return rule.check(pcb, design_rules=None)


def _by_rule(results, rule_id):
    return [v for v in results.violations if v.rule_id == rule_id]


# A 10x10 mm square fill at (100,100)..(110,110)
_SQUARE_FILL = [(100.0, 100.0), (110.0, 100.0), (110.0, 110.0), (100.0, 110.0)]


# ---------------------------------------------------------------------------
# track_dangling: positive cases
# ---------------------------------------------------------------------------


class TestTrackDangling:
    def test_isolated_segment_flagged_once(self):
        """A fully floating track emits ONE violation (kicad-cli parity:
        one finding per track, not per endpoint)."""
        seg = _FakeSegment(start=(10.0, 10.0), end=(14.0, 10.0))
        results = _run(segments=[seg])

        flagged = _by_rule(results, "track_dangling")
        assert len(flagged) == 1
        v = flagged[0]
        assert v.severity == "warning"
        assert v.location == (10.0, 10.0)
        assert "both ends dangling" in v.message
        assert "SIG" in v.message
        assert v.layer == "F.Cu"
        assert v.actual_value is not None and abs(v.actual_value - 4.0) < 1e-9

    def test_stub_off_pad_flagged_at_free_end(self):
        """Track leaving a pad but ending nowhere -> flagged at the free end."""
        fp = _FakeFootprint(pads=[_FakePad(position=(10.0, 10.0))])
        seg = _FakeSegment(start=(10.0, 10.0), end=(14.0, 10.0))
        results = _run(segments=[seg], footprints=[fp])

        flagged = _by_rule(results, "track_dangling")
        assert len(flagged) == 1
        assert flagged[0].location == (14.0, 10.0)
        assert "both ends" not in flagged[0].message

    def test_endpoint_in_foreign_fill_still_dangling(self):
        """A foreign-net pour does NOT terminate a track end."""
        zone = _FakeZone(net_number=2, net_name="+3V3", filled_polygons=[_SQUARE_FILL])
        fp = _FakeFootprint(pads=[_FakePad(position=(90.0, 105.0))])
        seg = _FakeSegment(start=(90.0, 105.0), end=(105.0, 105.0))
        results = _run(segments=[seg], footprints=[fp], zones=[zone])

        assert len(_by_rule(results, "track_dangling")) == 1

    def test_zero_length_isolated_segment_flagged_once(self):
        seg = _FakeSegment(start=(10.0, 10.0), end=(10.0, 10.0))
        results = _run(segments=[seg])
        assert len(_by_rule(results, "track_dangling")) == 1


# ---------------------------------------------------------------------------
# track_dangling: negative (terminated) cases
# ---------------------------------------------------------------------------


class TestTrackTerminated:
    def test_pad_to_pad_track_clean(self):
        fp = _FakeFootprint(
            pads=[_FakePad(position=(10.0, 10.0)), _FakePad(position=(14.0, 10.0), number="2")]
        )
        seg = _FakeSegment(start=(10.0, 10.0), end=(14.0, 10.0))
        results = _run(segments=[seg], footprints=[fp])
        assert _by_rule(results, "track_dangling") == []

    def test_chained_segments_terminated_at_shared_endpoint(self):
        """Endpoint-to-endpoint chaining terminates both inner ends."""
        fp = _FakeFootprint(
            pads=[_FakePad(position=(10.0, 10.0)), _FakePad(position=(18.0, 10.0), number="2")]
        )
        seg_a = _FakeSegment(start=(10.0, 10.0), end=(14.0, 10.0))
        seg_b = _FakeSegment(start=(14.0, 10.0), end=(18.0, 10.0))
        results = _run(segments=[seg_a, seg_b], footprints=[fp])
        assert _by_rule(results, "track_dangling") == []

    def test_t_junction_terminated_on_segment_body(self):
        """An endpoint landing on another track's BODY (not endpoint) is
        terminated -- KiCad's HitTest is copper containment."""
        fp = _FakeFootprint(
            pads=[
                _FakePad(position=(10.0, 10.0)),
                _FakePad(position=(18.0, 10.0), number="2"),
                _FakePad(position=(14.0, 14.0), number="3"),
            ]
        )
        bar = _FakeSegment(start=(10.0, 10.0), end=(18.0, 10.0))
        stem = _FakeSegment(start=(14.0, 10.0), end=(14.0, 14.0))
        results = _run(segments=[bar, stem], footprints=[fp])
        assert _by_rule(results, "track_dangling") == []

    def test_endpoint_terminated_by_via(self):
        """A via barrel terminates a track end; the via itself is bonded
        on only one layer, so the finding shifts to via_dangling."""
        fp = _FakeFootprint(pads=[_FakePad(position=(10.0, 10.0))])
        seg = _FakeSegment(start=(10.0, 10.0), end=(14.0, 10.0))
        via = _FakeVia(position=(14.0, 10.0))
        results = _run(segments=[seg], vias=[via], footprints=[fp])
        assert _by_rule(results, "track_dangling") == []
        assert len(_by_rule(results, "via_dangling")) == 1

    def test_endpoint_terminated_by_same_net_fill(self):
        zone = _FakeZone(net_number=1, net_name="SIG", filled_polygons=[_SQUARE_FILL])
        fp = _FakeFootprint(pads=[_FakePad(position=(90.0, 105.0))])
        seg = _FakeSegment(start=(90.0, 105.0), end=(105.0, 105.0))
        results = _run(segments=[seg], footprints=[fp], zones=[zone])
        assert _by_rule(results, "track_dangling") == []

    def test_different_layer_copper_does_not_terminate(self):
        """Copper on ANOTHER layer never terminates an endpoint."""
        fp = _FakeFootprint(
            pads=[_FakePad(position=(10.0, 10.0)), _FakePad(position=(14.0, 10.0), number="2")]
        )
        seg = _FakeSegment(start=(10.0, 10.0), end=(14.0, 10.0), layer="B.Cu")
        results = _run(segments=[seg], footprints=[fp])
        # The F.Cu pads do not touch B.Cu copper -> both ends dangle.
        assert len(_by_rule(results, "track_dangling")) == 1


# ---------------------------------------------------------------------------
# via_dangling
# ---------------------------------------------------------------------------


class TestViaDangling:
    def test_via_touching_nothing_flagged(self):
        via = _FakeVia(position=(50.0, 50.0))
        results = _run(vias=[via])

        flagged = _by_rule(results, "via_dangling")
        assert len(flagged) == 1
        v = flagged[0]
        assert v.severity == "warning"
        assert v.location == (50.0, 50.0)
        assert v.actual_value == 0.0
        assert v.required_value == 2.0
        assert "F.Cu - B.Cu" in v.message

    def test_via_bonded_on_one_layer_flagged(self):
        fp = _FakeFootprint(pads=[_FakePad(position=(10.0, 10.0))])
        seg = _FakeSegment(start=(10.0, 10.0), end=(14.0, 10.0))
        via = _FakeVia(position=(14.0, 10.0))
        results = _run(segments=[seg], vias=[via], footprints=[fp])

        flagged = _by_rule(results, "via_dangling")
        assert len(flagged) == 1
        assert flagged[0].actual_value == 1.0

    def test_via_bonded_on_two_layers_by_tracks_clean(self):
        pads = [_FakePad(position=(10.0, 10.0)), _FakePad(position=(18.0, 10.0), layers=("B.Cu",))]
        fp = _FakeFootprint(pads=pads)
        top = _FakeSegment(start=(10.0, 10.0), end=(14.0, 10.0), layer="F.Cu")
        bottom = _FakeSegment(start=(14.0, 10.0), end=(18.0, 10.0), layer="B.Cu")
        via = _FakeVia(position=(14.0, 10.0))
        results = _run(segments=[top, bottom], vias=[via], footprints=[fp])
        assert _by_rule(results, "via_dangling") == []

    def test_via_bonded_by_pour_on_two_layers_clean(self):
        """The classic false positive: a stitching via bonded ONLY by
        same-net pour copper on both layers must NOT be flagged."""
        zones = [
            _FakeZone(net_number=1, net_name="SIG", layer="F.Cu", filled_polygons=[_SQUARE_FILL]),
            _FakeZone(net_number=1, net_name="SIG", layer="B.Cu", filled_polygons=[_SQUARE_FILL]),
        ]
        via = _FakeVia(position=(105.0, 105.0))
        results = _run(vias=[via], zones=zones)
        assert _by_rule(results, "via_dangling") == []

    def test_via_in_foreign_pour_still_dangling(self):
        """Foreign-net pour copper does not bond a via."""
        zones = [
            _FakeZone(net_number=2, net_name="+3V3", layer="F.Cu", filled_polygons=[_SQUARE_FILL]),
            _FakeZone(net_number=2, net_name="+3V3", layer="B.Cu", filled_polygons=[_SQUARE_FILL]),
        ]
        via = _FakeVia(position=(105.0, 105.0))
        results = _run(vias=[via], zones=zones)
        assert len(_by_rule(results, "via_dangling")) == 1

    def test_via_through_hole_pad_bonds_all_layers(self):
        """A ``*.Cu`` through-hole pad at the via location bonds every
        copper layer -> not dangling."""
        fp = _FakeFootprint(
            pads=[_FakePad(position=(14.0, 10.0), layers=("*.Cu", "*.Mask"), drill=0.8)]
        )
        via = _FakeVia(position=(14.0, 10.0))
        results = _run(vias=[via], footprints=[fp])
        assert _by_rule(results, "via_dangling") == []

    def test_blind_via_counts_only_spanned_layers(self):
        """A blind F.Cu-In1.Cu via with track copper on F.Cu and B.Cu is
        bonded on ONE spanned layer only -> dangling."""
        layers = [
            _FakeLayer("F.Cu"),
            _FakeLayer("In1.Cu"),
            _FakeLayer("In2.Cu"),
            _FakeLayer("B.Cu"),
        ]
        fp = _FakeFootprint(
            pads=[
                _FakePad(position=(10.0, 10.0)),
                _FakePad(position=(18.0, 10.0), number="2", layers=("B.Cu",)),
            ]
        )
        top = _FakeSegment(start=(10.0, 10.0), end=(14.0, 10.0), layer="F.Cu")
        bottom = _FakeSegment(start=(14.0, 10.0), end=(18.0, 10.0), layer="B.Cu")
        via = _FakeVia(position=(14.0, 10.0), layers=("F.Cu", "In1.Cu"))
        results = _run(segments=[top, bottom], vias=[via], footprints=[fp], layers=layers)

        flagged = _by_rule(results, "via_dangling")
        assert len(flagged) == 1
        assert flagged[0].actual_value == 1.0

    def test_zero_size_via_skipped(self):
        via = _FakeVia(position=(50.0, 50.0), size=0.0)
        results = _run(vias=[via])
        assert _by_rule(results, "via_dangling") == []


# ---------------------------------------------------------------------------
# Severity / taxonomy / registration
# ---------------------------------------------------------------------------


class TestSeverityAndTaxonomy:
    def test_all_findings_are_warnings(self):
        seg = _FakeSegment(start=(10.0, 10.0), end=(14.0, 10.0))
        via = _FakeVia(position=(50.0, 50.0))
        results = _run(segments=[seg], vias=[via])
        assert results.violations
        assert all(v.severity == "warning" for v in results.violations)
        assert results.error_count == 0

    def test_reporting_category_is_advisory(self):
        assert DRCChecker.category_for_rule("track_dangling") == DRCChecker.CATEGORY_ADVISORY
        assert DRCChecker.category_for_rule("via_dangling") == DRCChecker.CATEGORY_ADVISORY

    def test_not_gating_advisory(self):
        """Gating set membership is unchanged (warnings never block, so
        no ADVISORY_RULE_IDS entry is needed -- documented in #4680)."""
        assert DRCChecker.is_advisory_rule("track_dangling") is False
        assert DRCChecker.is_advisory_rule("via_dangling") is False

    def test_registered_in_check_all(self):
        assert "check_dangling_copper" in DRCChecker.CHECK_ALL_METHODS

    def test_rules_checked_bookkeeping(self):
        results = _run()
        assert results.rules_checked_by_rule.get("track_dangling") == 1
        assert results.rules_checked_by_rule.get("via_dangling") == 1


# ---------------------------------------------------------------------------
# End-to-end on a real parsed board (checker path + track-arc candidates)
# ---------------------------------------------------------------------------

_ARC_BOARD = """(kicad_pcb (version 20240108) (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "SIG")
  (net 2 "STUB")
  (gr_line (start 0 0) (end 30 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 30 0) (end 30 30) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 30 30) (end 0 30) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 30) (end 0 0) (layer "Edge.Cuts") (width 0.1))
  (segment (start 5 5) (end 10 5) (width 0.2) (layer "F.Cu") (net 1))
  (arc (start 10 5) (mid 12.928932 5.857864) (end 15 8) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 15 8) (end 15 12) (width 0.2) (layer "F.Cu") (net 1))
  (via (at 5 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (segment (start 5 5) (end 5 12) (width 0.2) (layer "B.Cu") (net 1))
  (via (at 15 12) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (segment (start 15 12) (end 5 12) (width 0.2) (layer "B.Cu") (net 1))
  (segment (start 20 20) (end 25 20) (width 0.2) (layer "F.Cu") (net 2))
  (via (at 28 28) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2))
)
"""


class TestEndToEndParsedBoard:
    def test_arc_terminates_track_ends_and_stubs_flagged(self, tmp_path):
        """On a parsed board: a segment-arc-segment chain closed through
        two vias is clean (the arc terminates both adjacent segment
        ends); a floating stub and a lone via are flagged."""
        from kicad_tools.schema.pcb import PCB

        board = tmp_path / "arc_board.kicad_pcb"
        board.write_text(_ARC_BOARD)
        pcb = PCB.load(str(board))

        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_dangling_copper()

        track = _by_rule(results, "track_dangling")
        via = _by_rule(results, "via_dangling")

        # Exactly one dangling track: the net-2 stub (both ends free).
        assert len(track) == 1
        assert "STUB" in track[0].message
        # Exactly one dangling via: the lone net-2 via at (28, 28).
        assert len(via) == 1
        assert via[0].location == (28.0, 28.0)
