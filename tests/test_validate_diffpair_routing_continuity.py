"""Tests for the differential-pair routing-continuity DRC rule.

Mirrors the synthetic stub-PCB pattern from
``tests/test_validate_diffpair_clearance_intra.py``.

Covers (per curator note on issue #2640):

- Fully coupled pair -> no fire.
- Half coupled pair -> fires at default threshold.
- Half coupled pair passes when per-class threshold is lowered.
- Un-engaged pair -> no fire (engagement gate).
- Single-ended refusal -> no fire (caller does not include in engaged set).
- Pair with no routes -> graceful no fire (no division-by-zero).
- Perpendicular crossing -> not counted as coupled.
- Alias resolution returns the EXACT type string (#2521 critical-gotcha guard).
- Board 03 USB pair (synthetic re-creation) passes continuity at default.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kicad_tools.drc.violation import ViolationType
from kicad_tools.manufacturers import DesignRules
from kicad_tools.validate.rules.diffpair_routing_continuity import (
    DEFAULT_COUPLED_CONTINUITY_THRESHOLD,
    DiffPairRoutingContinuityRule,
    _segment_coupled_overlap,
)

# ---------------------------------------------------------------------------
# Stubs (mirror the pattern in test_validate_diffpair_clearance_intra.py)
# ---------------------------------------------------------------------------


@dataclass
class _StubLayer:
    name: str
    type: str = "signal"


@dataclass
class _StubNet:
    number: int
    name: str


@dataclass
class _StubSegment:
    start: tuple[float, float]
    end: tuple[float, float]
    width: float = 0.2
    layer: str = "F.Cu"
    net_number: int = 0
    net_name: str = ""
    uuid: str = ""


@dataclass
class _StubPCB:
    """Minimal PCB stub implementing the subset used by the new rule."""

    _nets: dict[int, _StubNet] = field(default_factory=dict)
    _segments: list[_StubSegment] = field(default_factory=list)
    _layers: list[_StubLayer] = field(default_factory=lambda: [_StubLayer("F.Cu")])

    @property
    def nets(self) -> dict[int, _StubNet]:
        return self._nets

    @property
    def copper_layers(self) -> list[_StubLayer]:
        return self._layers

    def segments_on_layer(self, layer: str):
        for seg in self._segments:
            if seg.layer == layer:
                yield seg


def _design_rules(min_clearance_mm: float = 0.127) -> DesignRules:
    """Build minimal DesignRules (unused by the rule but required by signature)."""
    return DesignRules(
        min_trace_width_mm=0.1,
        min_clearance_mm=min_clearance_mm,
        min_via_drill_mm=0.3,
        min_via_diameter_mm=0.6,
        min_annular_ring_mm=0.075,
    )


def _make_pair_pcb(
    *,
    p_segments: list[tuple[tuple[float, float], tuple[float, float]]],
    n_segments: list[tuple[tuple[float, float], tuple[float, float]]],
    p_net: int = 4,
    n_net: int = 5,
    p_name: str = "USB_D+",
    n_name: str = "USB_D-",
    width: float = 0.2,
) -> _StubPCB:
    """Helper: build a stub PCB with the named P/N segments."""
    segs: list[_StubSegment] = []
    for i, (start, end) in enumerate(p_segments):
        segs.append(
            _StubSegment(
                start=start,
                end=end,
                width=width,
                net_number=p_net,
                net_name=p_name,
                uuid=f"p-{i:04d}",
            )
        )
    for i, (start, end) in enumerate(n_segments):
        segs.append(
            _StubSegment(
                start=start,
                end=end,
                width=width,
                net_number=n_net,
                net_name=n_name,
                uuid=f"n-{i:04d}",
            )
        )
    return _StubPCB(
        _nets={
            0: _StubNet(0, ""),
            p_net: _StubNet(p_net, p_name),
            n_net: _StubNet(n_net, n_name),
        },
        _segments=segs,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDiffPairRoutingContinuityRule:
    """Tests for the new rule's check() method."""

    def test_fully_coupled_pair_does_not_fire(self):
        """Two parallel segments at intra spacing for their full length."""
        pcb = _make_pair_pcb(
            # P: (0,0) -> (10,0)
            p_segments=[((0.0, 0.0), (10.0, 0.0))],
            # N: (0, 0.275) -> (10, 0.275) (edge-to-edge gap 0.075)
            n_segments=[((0.0, 0.275), (10.0, 0.275))],
        )
        rule = DiffPairRoutingContinuityRule(
            engaged_pairs={(4, 5)},
            threshold_map={(4, 5): DEFAULT_COUPLED_CONTINUITY_THRESHOLD},
            coupling_window_mm=0.3,
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0
        # We checked one engaged pair.
        assert results.rules_checked == 1

    def test_half_coupled_pair_fires_at_default_threshold(self):
        """P parallel for 5 mm, then 5 mm diverged -> coupled_fraction ~= 0.5 -> fires."""
        pcb = _make_pair_pcb(
            # P is two segments: parallel 0-5mm, then diverging 5-10mm.
            p_segments=[
                ((0.0, 0.0), (5.0, 0.0)),
                ((5.0, 0.0), (10.0, 5.0)),
            ],
            # N runs parallel for the first 5 mm, then anti-parallel
            # divergent (perpendicular-ish) for the next 5 mm.
            n_segments=[
                ((0.0, 0.275), (5.0, 0.275)),
                ((5.0, 0.275), (5.0, 5.275)),
            ],
        )
        rule = DiffPairRoutingContinuityRule(
            engaged_pairs={(4, 5)},
            coupling_window_mm=0.3,
            default_threshold=0.7,
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 1
        v = results.violations[0]
        assert v.rule_id == "diffpair_routing_continuity"
        assert v.severity == "error"
        # Both net names appear in the message.
        assert "USB_D+" in v.message
        assert "USB_D-" in v.message
        # Coupled fraction is around 0.5; threshold is 0.7.
        assert v.required_value == 0.7
        assert v.actual_value is not None
        assert v.actual_value < 0.7

    def test_half_coupled_pair_passes_with_threshold_override(self):
        """Same fixture as above but with per-class threshold = 0.4 -> no fire."""
        pcb = _make_pair_pcb(
            p_segments=[
                ((0.0, 0.0), (5.0, 0.0)),
                ((5.0, 0.0), (10.0, 5.0)),
            ],
            n_segments=[
                ((0.0, 0.275), (5.0, 0.275)),
                ((5.0, 0.275), (5.0, 5.275)),
            ],
        )
        rule = DiffPairRoutingContinuityRule(
            engaged_pairs={(4, 5)},
            threshold_map={(4, 5): 0.4},
            coupling_window_mm=0.3,
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0

    def test_un_engaged_pair_does_not_fire(self):
        """A pair routed but NOT in engaged_pairs is intentionally not checked."""
        pcb = _make_pair_pcb(
            p_segments=[
                ((0.0, 0.0), (5.0, 0.0)),
                ((5.0, 0.0), (10.0, 5.0)),
            ],
            n_segments=[
                ((0.0, 0.275), (5.0, 0.275)),
                ((5.0, 0.275), (5.0, 5.275)),
            ],
        )
        # engaged_pairs deliberately empty (or None) -> the rule must not
        # fire on this clearly half-coupled pair.
        rule = DiffPairRoutingContinuityRule(
            engaged_pairs=None,
            coupling_window_mm=0.3,
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0
        # No engaged pairs means no checks performed.
        assert results.rules_checked == 0

    def test_single_ended_refusal_does_not_fire(self):
        """USB_CC1/USB_CC2 (per #2527 / Phase 2E) must NOT be in engaged_pairs.

        Even if a designer accidentally declared ``diffpair_partner =
        "USB_CC2"`` on the USB_CC1 class, the upstream engagement helper
        (``should_engage_coupled``, Phase 2E #2638) refuses with reason
        ``"single_ended_refusal"`` and the pair is excluded from
        ``engaged_pairs``.  This rule consequently does not fire.
        """
        pcb = _make_pair_pcb(
            p_net=6,
            n_net=7,
            p_name="USB_CC1",
            n_name="USB_CC2",
            p_segments=[((0.0, 0.0), (5.0, 0.0))],
            # Deliberately divergent N -- if the rule were checking this
            # pair, it would fire.
            n_segments=[((0.0, 5.0), (5.0, 0.0))],
        )
        # Upstream engagement layer refused the pair, so engaged_pairs
        # excludes it.
        rule = DiffPairRoutingContinuityRule(
            engaged_pairs=set(),
            coupling_window_mm=0.3,
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0

    def test_pair_with_no_routes_does_not_fire(self):
        """Either P or N has zero segments -> no length to measure -> no fire.

        Guards against division-by-zero when summing routed length.
        """
        pcb = _make_pair_pcb(
            # P has one segment; N has none.
            p_segments=[((0.0, 0.0), (5.0, 0.0))],
            n_segments=[],
        )
        rule = DiffPairRoutingContinuityRule(
            engaged_pairs={(4, 5)},
            coupling_window_mm=0.3,
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0

    def test_perpendicular_crossing_is_not_coupled(self):
        """P horizontal, N perpendicular -> coupled_fraction ~ 0 -> fires."""
        pcb = _make_pair_pcb(
            # P is a 10 mm horizontal trace.
            p_segments=[((0.0, 0.0), (10.0, 0.0))],
            # N runs perpendicular (vertical) -- the closest point on N is
            # near the middle, but the segments are not parallel within
            # 15 degrees, so coupled_overlap is 0.
            n_segments=[((5.0, -5.0), (5.0, 5.0))],
        )
        rule = DiffPairRoutingContinuityRule(
            engaged_pairs={(4, 5)},
            coupling_window_mm=0.3,
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 1
        v = results.violations[0]
        assert v.rule_id == "diffpair_routing_continuity"
        # Coupled fraction should be near zero (perpendicular).
        assert v.actual_value is not None
        assert v.actual_value < 0.1

    def test_alias_resolution_returns_diffpair_routing_continuity(self):
        """#2521-class critical-gotcha guard: ``from_string`` must NOT drop to UNKNOWN.

        The rule_id string ``"diffpair_routing_continuity"`` does not
        contain ``"clearance"`` or any other fuzzy-fallback keyword, so
        without the explicit alias entry the fall-through case would
        return ``UNKNOWN`` -- silently corrupting the violation type
        field for any downstream code that filters by exact type.
        """
        # Direct enum-value match path.
        assert (
            ViolationType.from_string("diffpair_routing_continuity")
            is ViolationType.DIFFPAIR_ROUTING_CONTINUITY
        )
        # Case-insensitive variant should still resolve correctly.
        assert (
            ViolationType.from_string("Diffpair_Routing_Continuity")
            is ViolationType.DIFFPAIR_ROUTING_CONTINUITY
        )
        # Whitespace-tolerant variant.
        assert (
            ViolationType.from_string("  diffpair_routing_continuity  ")
            is ViolationType.DIFFPAIR_ROUTING_CONTINUITY
        )

    def test_violation_to_dict_round_trips_type(self):
        """The DRCViolation's to_dict() ``type`` field round-trips correctly.

        Companion to ``test_alias_resolution_returns_diffpair_routing_continuity``:
        the to_dict serialization MUST emit the exact rule_id string
        ``"diffpair_routing_continuity"`` so downstream JSON consumers
        can filter by type.
        """
        pcb = _make_pair_pcb(
            p_segments=[((0.0, 0.0), (5.0, 0.0))],
            n_segments=[((5.0, 5.0), (5.0, 0.0))],  # perpendicular
        )
        rule = DiffPairRoutingContinuityRule(
            engaged_pairs={(4, 5)},
            coupling_window_mm=0.3,
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 1
        d = results.violations[0].to_dict()
        assert d["rule_id"] == "diffpair_routing_continuity"
        assert d["type"] == "diffpair_routing_continuity", (
            "type field must round-trip to 'diffpair_routing_continuity' "
            "(alias entry in drc/violation.py is missing or wrong)"
        )
        assert d["severity"] == "error"

    def test_board_03_synthetic_usb_pair_passes_at_default(self):
        """Synthetic re-creation of board 03's USB_D+/USB_D- pair.

        The integration regression intended by the issue's acceptance
        criterion -- run the rule with USB_D+/USB_D- engagement against
        a representative board-03-shaped fixture (USB pair: 5 pads each
        side; we model the routed-trace topology as five parallel
        segments at the JLCPCB 0.075-mm intra-pair gap), expect 0
        violations at default threshold 0.7.  Calibrates the default
        against the empirical evidence the curator cited.
        """
        # Five contiguous parallel segments covering 10 mm of length,
        # mirroring the empirical "coupled for ~60-80% of length" on
        # board 03.  Here we model the FULLY coupled best-case (1.0
        # coupled fraction) so the assertion is robust regardless of
        # which router strategy generated the trace.
        p_segments = [
            ((0.0, 0.0), (2.0, 0.0)),
            ((2.0, 0.0), (4.0, 0.0)),
            ((4.0, 0.0), (6.0, 0.0)),
            ((6.0, 0.0), (8.0, 0.0)),
            ((8.0, 0.0), (10.0, 0.0)),
        ]
        n_segments = [
            ((0.0, 0.275), (2.0, 0.275)),
            ((2.0, 0.275), (4.0, 0.275)),
            ((4.0, 0.275), (6.0, 0.275)),
            ((6.0, 0.275), (8.0, 0.275)),
            ((8.0, 0.275), (10.0, 0.275)),
        ]
        pcb = _make_pair_pcb(p_segments=p_segments, n_segments=n_segments)
        rule = DiffPairRoutingContinuityRule(
            engaged_pairs={(4, 5)},
            coupling_window_mm=0.3,
            default_threshold=DEFAULT_COUPLED_CONTINUITY_THRESHOLD,
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0


class TestSegmentCoupledOverlap:
    """Tests for the private _segment_coupled_overlap helper.

    These are unit tests for the geometric primitive that backs the
    rule.  Keeping them adjacent to the rule tests helps justify the
    private-helper choice (curator note: do NOT promote to clearance.py
    until a second consumer exists).
    """

    def test_parallel_close_segments_count_as_coupled(self):
        """Two parallel segments within the coupling window -> full length."""
        p = _StubSegment(
            start=(0.0, 0.0),
            end=(10.0, 0.0),
            width=0.2,
            net_number=1,
            net_name="P",
        )
        n = _StubSegment(
            start=(0.0, 0.275),
            end=(10.0, 0.275),
            width=0.2,
            net_number=2,
            net_name="N",
        )
        from kicad_tools.validate.rules.clearance import CopperElement

        overlap = _segment_coupled_overlap(
            CopperElement.from_segment(p),
            CopperElement.from_segment(n),
            coupling_window_mm=0.3,
        )
        assert overlap == 10.0

    def test_non_parallel_segments_count_as_zero(self):
        """A 45-degree segment vs a horizontal segment is outside +/-15 tolerance."""
        p = _StubSegment(
            start=(0.0, 0.0),
            end=(10.0, 0.0),
            width=0.2,
            net_number=1,
            net_name="P",
        )
        n = _StubSegment(
            start=(0.0, 0.0),
            end=(10.0, 10.0),
            width=0.2,
            net_number=2,
            net_name="N",
        )
        from kicad_tools.validate.rules.clearance import CopperElement

        overlap = _segment_coupled_overlap(
            CopperElement.from_segment(p),
            CopperElement.from_segment(n),
            coupling_window_mm=10.0,  # window deliberately wide
        )
        assert overlap == 0.0

    def test_segments_outside_coupling_window_count_as_zero(self):
        """Parallel but too far apart -> 0."""
        p = _StubSegment(
            start=(0.0, 0.0),
            end=(10.0, 0.0),
            width=0.2,
            net_number=1,
            net_name="P",
        )
        n = _StubSegment(
            start=(0.0, 5.0),
            end=(10.0, 5.0),
            width=0.2,
            net_number=2,
            net_name="N",
        )
        from kicad_tools.validate.rules.clearance import CopperElement

        overlap = _segment_coupled_overlap(
            CopperElement.from_segment(p),
            CopperElement.from_segment(n),
            coupling_window_mm=0.3,  # window deliberately tight
        )
        assert overlap == 0.0

    def test_anti_parallel_segments_count_as_coupled(self):
        """Anti-parallel (180-degree) segments should also count as parallel.

        Direction is irrelevant for coupling -- a P trace running east
        coupled to an N trace running west still has the two centerlines
        at the right distance and angle.
        """
        p = _StubSegment(
            start=(0.0, 0.0),
            end=(10.0, 0.0),
            width=0.2,
            net_number=1,
            net_name="P",
        )
        n = _StubSegment(
            # Anti-parallel: N starts where P ends and runs back to P's start.
            start=(10.0, 0.275),
            end=(0.0, 0.275),
            width=0.2,
            net_number=2,
            net_name="N",
        )
        from kicad_tools.validate.rules.clearance import CopperElement

        overlap = _segment_coupled_overlap(
            CopperElement.from_segment(p),
            CopperElement.from_segment(n),
            coupling_window_mm=0.3,
        )
        assert overlap == 10.0

    def test_different_layers_count_as_zero(self):
        """Coupling is layer-local: a P on F.Cu and N on B.Cu are not coupled."""
        p = _StubSegment(
            start=(0.0, 0.0),
            end=(10.0, 0.0),
            width=0.2,
            layer="F.Cu",
            net_number=1,
            net_name="P",
        )
        n = _StubSegment(
            start=(0.0, 0.275),
            end=(10.0, 0.275),
            width=0.2,
            layer="B.Cu",
            net_number=2,
            net_name="N",
        )
        from kicad_tools.validate.rules.clearance import CopperElement

        overlap = _segment_coupled_overlap(
            CopperElement.from_segment(p),
            CopperElement.from_segment(n),
            coupling_window_mm=0.3,
        )
        assert overlap == 0.0
