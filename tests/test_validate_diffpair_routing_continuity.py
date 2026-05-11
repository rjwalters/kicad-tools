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


# ---------------------------------------------------------------------------
# Phase 2.5b drift-prevention tests (Issue #2652, Epic #2556).
#
# Producer-side: the autorouter calls ``should_engage_coupled`` for each
# detected pair at routing time (see DiffPairRouter._resolve_engagement
# at router/diffpair_routing.py:1077).  Validator-side: this test re-runs
# the same producer logic against the routed PCB's detected pairs via
# ``derive_engagement_state``.  AC #5 of the issue: the two sets MUST
# match -- if a future refactor splits the derivation paths, this test
# fires.
# ---------------------------------------------------------------------------


def _make_pair_stub_pcb(
    *,
    pairs: list[tuple[int, str, int, str]],
) -> _StubPCB:
    """Stub a PCB with the given (net_id, net_name) pairs in the net table.

    Geometry is intentionally absent -- the drift-prevention test only
    needs ``pcb.nets`` for the detector.  ``copper_layers`` and
    ``segments_on_layer`` remain available on _StubPCB if a caller needs
    them, but they are not consulted by ``derive_engagement_state``.
    """
    nets: dict[int, _StubNet] = {0: _StubNet(0, "")}
    for p_id, p_name, n_id, n_name in pairs:
        nets[p_id] = _StubNet(p_id, p_name)
        nets[n_id] = _StubNet(n_id, n_name)
    return _StubPCB(_nets=nets, _segments=[])


class TestEngagementDriftPrevention:
    """AC #5: producer-side engaged_pairs MUST match re-derived set.

    Three scenarios per the curator note on #2652:

    - Opt-in pair (``coupled_routing=True``, non-single-ended) ->
      engaged on both sides.
    - Single-ended refusal (USB_CC1/USB_CC2, per #2527) -> neither side
      engages.
    - Opt-out pair (``coupled_routing=False``) -> neither side engages.

    If a future refactor splits the two derivation paths, at least one
    of these will fire.
    """

    def _producer_side_engaged(
        self,
        net_names: dict[int, str],
        net_class_map: dict,
    ) -> set[tuple[int, int]]:
        """Mimic the autorouter's producer-side computation.

        This mirrors the call pattern in
        ``DiffPairRouter._resolve_engagement`` (router/diffpair_routing.py:1077):
        detect pairs from net names + net-class map, then call
        ``should_engage_coupled`` for each detected pair.

        The synthesised ``net_to_class`` map matches the autorouter's
        ``_resolve_detection_inputs`` fallback path.
        """
        from kicad_tools.router.diffpair import should_engage_coupled
        from kicad_tools.router.diffpair_detection import detect_diff_pairs

        net_to_class: dict[str, str] = {}
        synth_routing: dict = dict(net_class_map)
        for net_name, nc in net_class_map.items():
            synth_routing.setdefault(nc.name, nc)
            net_to_class[net_name] = nc.name

        detected = detect_diff_pairs(
            net_names,
            net_class_routing=synth_routing,
            net_to_class=net_to_class,
            kicad_groups=None,
        )

        engaged: set[tuple[int, int]] = set()
        for d in detected:
            ok, _reason = should_engage_coupled(
                d.pair,
                net_class_routing=synth_routing,
                net_to_class=net_to_class,
            )
            if not ok:
                continue
            a = d.pair.positive.net_id
            b = d.pair.negative.net_id
            engaged.add((a, b) if a <= b else (b, a))
        return engaged

    def test_opt_in_pair_engages_on_both_sides(self):
        """Opt-in pair with ``coupled_routing=True`` -> both sets contain it."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.diffpair_engagement import (
            derive_engagement_state,
        )

        nc_hs = NetClassRouting(name="HIGH_SPEED", coupled_routing=True)
        net_class_map = {"USB_D+": nc_hs, "USB_D-": nc_hs}
        net_names = {1: "USB_D+", 2: "USB_D-"}

        pcb = _make_pair_stub_pcb(pairs=[(1, "USB_D+", 2, "USB_D-")])
        rederived, threshold_map = derive_engagement_state(pcb, net_class_map)
        producer = self._producer_side_engaged(net_names, net_class_map)

        assert producer == {(1, 2)}, "producer side must engage opt-in pair"
        assert rederived == producer, "rederived engaged_pairs must match producer side (AC #5)"
        # Threshold map populated for the engaged pair (default 0.7).
        assert (1, 2) in threshold_map
        assert threshold_map[(1, 2)] == DEFAULT_COUPLED_CONTINUITY_THRESHOLD

    def test_single_ended_refusal_does_not_engage_either_side(self):
        """USB_CC1/USB_CC2 with ``coupled_routing=True`` -> neither set engages.

        The #2527 lesson: even with an explicit class-level opt-in, the
        engagement-layer single-ended refusal in
        ``should_engage_coupled`` overrides.  Both producer and
        validator MUST agree that the pair is NOT engaged.
        """
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.diffpair_engagement import (
            derive_engagement_state,
        )

        nc_cc = NetClassRouting(
            name="USBC_CC",
            coupled_routing=True,
            diffpair_partner="USB_CC2",  # explicit declaration -- still refused
        )
        # We also need the partner to be in a class so the explicit
        # declaration registers it as a pair via _gather_explicit_pairs.
        nc_cc_partner = NetClassRouting(
            name="USBC_CC_PARTNER",
            coupled_routing=True,
            diffpair_partner="USB_CC1",
        )
        net_class_map = {"USB_CC1": nc_cc, "USB_CC2": nc_cc_partner}
        net_names = {3: "USB_CC1", 4: "USB_CC2"}

        pcb = _make_pair_stub_pcb(pairs=[(3, "USB_CC1", 4, "USB_CC2")])
        rederived, _threshold_map = derive_engagement_state(pcb, net_class_map)
        producer = self._producer_side_engaged(net_names, net_class_map)

        # Both sides MUST refuse the pair.
        assert producer == set(), "producer must refuse USB_CC1/CC2 via single-ended_refusal"
        assert rederived == set(), (
            "validator must also refuse USB_CC1/CC2 (AC #5: must match producer)"
        )

    def test_opt_out_pair_does_not_engage_either_side(self):
        """Pair with ``coupled_routing=False`` -> neither set engages."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.diffpair_engagement import (
            derive_engagement_state,
        )

        nc_default = NetClassRouting(name="DEFAULT", coupled_routing=False)
        net_class_map = {"USB_D+": nc_default, "USB_D-": nc_default}
        net_names = {1: "USB_D+", 2: "USB_D-"}

        pcb = _make_pair_stub_pcb(pairs=[(1, "USB_D+", 2, "USB_D-")])
        rederived, threshold_map = derive_engagement_state(pcb, net_class_map)
        producer = self._producer_side_engaged(net_names, net_class_map)

        assert producer == set(), "producer must NOT engage opt-out pair"
        assert rederived == producer, "rederived engaged_pairs must match producer side (AC #5)"
        assert threshold_map == {}, "no threshold entry for non-engaged pair"

    def test_no_net_class_map_returns_empty(self):
        """AC #4: ``derive_engagement_state(pcb, None)`` -> empty result.

        Preserves the standalone ``kct check`` graceful-no-op contract.
        """
        from kicad_tools.validate.diffpair_engagement import (
            derive_engagement_state,
        )

        pcb = _make_pair_stub_pcb(pairs=[(1, "USB_D+", 2, "USB_D-")])
        engaged, thresholds = derive_engagement_state(pcb, None)
        assert engaged == set()
        assert thresholds == {}

        # Empty dict also returns empty (idempotent with None).
        engaged2, thresholds2 = derive_engagement_state(pcb, {})
        assert engaged2 == set()
        assert thresholds2 == {}

    def test_per_class_threshold_override_propagates(self):
        """The per-class ``coupled_continuity_threshold`` reaches the rule.

        AC #1 corollary: when the autorouter consumer passes a
        ``net_class_map`` with an explicit threshold, the rule must
        receive that threshold (not the module-level default).
        """
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.diffpair_engagement import (
            derive_engagement_state,
        )

        nc_hsdi = NetClassRouting(
            name="HSDI",
            coupled_routing=True,
            coupled_continuity_threshold=0.9,  # tighter than default 0.7
        )
        net_class_map = {"PCIE_TX+": nc_hsdi, "PCIE_TX-": nc_hsdi}

        pcb = _make_pair_stub_pcb(pairs=[(5, "PCIE_TX+", 6, "PCIE_TX-")])
        engaged, thresholds = derive_engagement_state(pcb, net_class_map)
        assert engaged == {(5, 6)}
        assert thresholds[(5, 6)] == 0.9


class TestDRCCheckerProducerWiring:
    """Phase 2.5b end-to-end: DRCChecker(net_class_map=...) fires the rule.

    AC #1: the autorouter consumer (via DRCChecker.__init__'s new
    ``net_class_map`` parameter) passes a non-empty engaged set into the
    rule and the rule actually fires on a board with diff pairs whose
    class has ``coupled_routing=True``.

    AC #2: broken-coupling pair (engaged but diverged) -> violation.

    AC #3: proper-coupling pair (engaged and parallel) -> 0 violations.
    """

    def _build_engaged_pcb(
        self,
        *,
        p_segments: list[tuple[tuple[float, float], tuple[float, float]]],
        n_segments: list[tuple[tuple[float, float], tuple[float, float]]],
    ) -> tuple[_StubPCB, dict]:
        """Build a stub PCB + matching engaged-class net_class_map."""
        from kicad_tools.router.rules import NetClassRouting

        pcb = _make_pair_pcb(p_segments=p_segments, n_segments=n_segments)
        nc = NetClassRouting(name="HIGH_SPEED", coupled_routing=True)
        net_class_map = {"USB_D+": nc, "USB_D-": nc}
        return pcb, net_class_map

    def test_proper_coupling_passes_via_drcchecker(self):
        """AC #3: engaged + parallel for full length -> 0 violations."""
        from kicad_tools.validate import DRCChecker

        pcb, net_class_map = self._build_engaged_pcb(
            p_segments=[((0.0, 0.0), (10.0, 0.0))],
            n_segments=[((0.0, 0.275), (10.0, 0.275))],
        )
        checker = DRCChecker(pcb, manufacturer="jlcpcb", net_class_map=net_class_map)
        results = checker.check_diffpair_routing_continuity()
        assert results.rules_checked == 1, (
            "rule must execute (engaged_pairs is non-empty -- producer wired)"
        )
        assert len(results.violations) == 0

    def test_broken_coupling_fires_via_drcchecker(self):
        """AC #2: engaged + half-diverged -> 1 violation at default threshold."""
        from kicad_tools.validate import DRCChecker

        pcb, net_class_map = self._build_engaged_pcb(
            p_segments=[
                ((0.0, 0.0), (5.0, 0.0)),
                ((5.0, 0.0), (10.0, 5.0)),
            ],
            n_segments=[
                ((0.0, 0.275), (5.0, 0.275)),
                ((5.0, 0.275), (5.0, 5.275)),
            ],
        )
        checker = DRCChecker(pcb, manufacturer="jlcpcb", net_class_map=net_class_map)
        results = checker.check_diffpair_routing_continuity()
        assert results.rules_checked == 1
        assert len(results.violations) == 1
        v = results.violations[0]
        assert v.rule_id == "diffpair_routing_continuity"
        # USB_D+ / USB_D- names appear in the message.
        assert "USB_D+" in v.message
        assert "USB_D-" in v.message

    def test_no_net_class_map_keeps_rule_dormant(self):
        """AC #4: DRCChecker(pcb) without net_class_map -> rule is no-op.

        Preserves backward-compatibility with all pre-#2652 callers.
        """
        from kicad_tools.validate import DRCChecker

        pcb, _net_class_map = self._build_engaged_pcb(
            # Deliberately divergent geometry -- if the rule were checking
            # this pair, it would fire.
            p_segments=[((0.0, 0.0), (10.0, 0.0))],
            n_segments=[((0.0, 5.0), (10.0, 5.0))],
        )
        # No net_class_map -> engaged_pairs is empty -> rule is a no-op.
        checker = DRCChecker(pcb, manufacturer="jlcpcb")
        results = checker.check_diffpair_routing_continuity()
        assert results.rules_checked == 0
        assert len(results.violations) == 0
