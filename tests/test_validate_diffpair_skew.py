"""Tests for the validate-side diff-pair skew producer wiring (Issue #2675).

Mirrors the pattern from
``tests/test_validate_diffpair_routing_continuity.py``
(``TestEngagementDriftPrevention``) -- this is the sister Phase 2.5c
producer-side wiring for the ``diffpair_length_skew`` DRC rule
(Phase 3J / Issue #2649).

Covers:

- ``derive_skew_data(pcb, None)`` -> empty result (standalone-CLI graceful
  no-op).
- One-sided-declaration policy (P-only or N-only in net_class_map) ->
  same engaged behaviour as :func:`derive_engagement_state`.
- **Drift-prevention**: ``derive_skew_data`` on a PCB byte-for-byte
  matches :meth:`DiffPairLengthTracker.get_all_skews` on the
  router-internal :class:`Route` form of the same physical routing.
- Pairs where one half has no geometry are omitted (graceful
  degradation mirroring tracker semantics).
- Via-traversing routes: PCB-side measurement matches router-side when
  ``board_thickness_mm`` is supplied (no router context-only state).
- Drift-prevention: ``DEFAULT_SKEW_TOLERANCE_MM`` matches
  ``NetClassRouting.effective_skew_tolerance`` default (already covered
  by ``test_validate_diffpair_length_skew.py``; we mirror here so a
  future change touching only one of the two paths fires this test
  too).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.router.primitives import Route

# ---------------------------------------------------------------------------
# Stubs (mirror test_validate_diffpair_routing_continuity.py)
# ---------------------------------------------------------------------------


@dataclass
class _StubNet:
    number: int
    name: str


@dataclass
class _StubSegment:
    """PCB-schema-shape segment stub.

    Matches :class:`kicad_tools.schema.pcb.Segment`:
    ``start: tuple[float, float]`` / ``end: tuple[float, float]`` (NOT
    the router-internal ``x1/y1/x2/y2`` fields).
    """

    start: tuple[float, float]
    end: tuple[float, float]
    width: float = 0.2
    layer: str = "F.Cu"
    net_number: int = 0
    net_name: str = ""
    uuid: str = ""


@dataclass
class _StubVia:
    """PCB-schema-shape via stub.

    Matches :class:`kicad_tools.schema.pcb.Via`: ``layers: list[str]`` of
    KiCad layer name strings (e.g., ``["F.Cu", "B.Cu"]``), NOT the
    router-internal ``tuple[CopperLayer, CopperLayer]``.
    """

    position: tuple[float, float] = (0.0, 0.0)
    size: float = 0.6
    drill: float = 0.3
    layers: list[str] = field(default_factory=lambda: ["F.Cu", "B.Cu"])
    net_number: int = 0
    net_name: str = ""
    uuid: str = ""


@dataclass
class _StubPCB:
    """Minimal PCB stub used by :func:`derive_skew_data`.

    Implements ``nets``, ``segments_in_net``, ``vias_in_net`` -- the
    only attributes/methods consulted by the helper.
    """

    _nets: dict[int, _StubNet] = field(default_factory=dict)
    _segments: list[_StubSegment] = field(default_factory=list)
    _vias: list[_StubVia] = field(default_factory=list)

    @property
    def nets(self) -> dict[int, _StubNet]:
        return self._nets

    def segments_in_net(self, net_number: int):
        for seg in self._segments:
            if seg.net_number == net_number:
                yield seg

    def vias_in_net(self, net_number: int):
        for via in self._vias:
            if via.net_number == net_number:
                yield via


def _make_pair_pcb(
    *,
    p_net: int = 4,
    n_net: int = 5,
    p_name: str = "USB_D+",
    n_name: str = "USB_D-",
    p_len_mm: float | None = None,
    n_len_mm: float | None = None,
) -> _StubPCB:
    """Construct a stub PCB with a single horizontal segment for each net.

    Segments are placed on F.Cu at y=0 (positive) and y=1 (negative)
    starting at x=0.  Pass ``None`` for either length to omit that
    half's geometry (test the "one-side-unrouted" path).
    """
    nets: dict[int, _StubNet] = {
        0: _StubNet(0, ""),
        p_net: _StubNet(p_net, p_name),
        n_net: _StubNet(n_net, n_name),
    }
    segs: list[_StubSegment] = []
    if p_len_mm is not None:
        segs.append(
            _StubSegment(
                start=(0.0, 0.0),
                end=(p_len_mm, 0.0),
                net_number=p_net,
                net_name=p_name,
            )
        )
    if n_len_mm is not None:
        segs.append(
            _StubSegment(
                start=(0.0, 1.0),
                end=(n_len_mm, 1.0),
                net_number=n_net,
                net_name=n_name,
            )
        )
    return _StubPCB(_nets=nets, _segments=segs)


# ---------------------------------------------------------------------------
# derive_skew_data tests
# ---------------------------------------------------------------------------


class TestDeriveSkewDataNoOp:
    """Standalone-CLI graceful no-op paths."""

    def test_no_net_class_map_returns_empty(self):
        """``derive_skew_data(pcb, None)`` -> empty tuple.

        Preserves the standalone ``kct check`` graceful-no-op contract
        (mirrors ``derive_engagement_state(pcb, None)``).
        """
        from kicad_tools.validate.diffpair_skew import derive_skew_data

        pcb = _make_pair_pcb(p_len_mm=10.0, n_len_mm=10.0)
        skew_data, threshold_map = derive_skew_data(pcb, None)
        assert skew_data == {}
        assert threshold_map == {}

    def test_empty_net_class_map_returns_empty(self):
        """``derive_skew_data(pcb, {})`` -> empty result (idempotent with None)."""
        from kicad_tools.validate.diffpair_skew import derive_skew_data

        pcb = _make_pair_pcb(p_len_mm=10.0, n_len_mm=10.0)
        skew_data, threshold_map = derive_skew_data(pcb, {})
        assert skew_data == {}
        assert threshold_map == {}

    def test_pcb_with_no_nets_returns_empty(self):
        """Edge case: a PCB whose net table has only the empty net 0."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.diffpair_skew import derive_skew_data

        nc = NetClassRouting(name="HIGH_SPEED", coupled_routing=True)
        net_class_map = {"USB_D+": nc, "USB_D-": nc}

        pcb = _StubPCB(_nets={0: _StubNet(0, "")})
        skew_data, threshold_map = derive_skew_data(pcb, net_class_map)
        assert skew_data == {}
        assert threshold_map == {}


class TestDeriveSkewDataBasic:
    """Basic measurement + per-pair tolerance assignment."""

    def test_symmetric_pair_zero_skew(self):
        """Equal-length P and N -> skew_mm == 0.0."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.diffpair_skew import derive_skew_data

        nc = NetClassRouting(name="HIGH_SPEED", coupled_routing=True)
        net_class_map = {"USB_D+": nc, "USB_D-": nc}

        pcb = _make_pair_pcb(p_len_mm=10.0, n_len_mm=10.0)
        skew_data, threshold_map = derive_skew_data(pcb, net_class_map)

        # One entry, keyed on (p_name, n_name).
        assert ("USB_D+", "USB_D-") in skew_data
        assert skew_data[("USB_D+", "USB_D-")] == 0.0

        # Threshold map populated for the engaged pair (default 0.5).
        assert (4, 5) in threshold_map
        assert threshold_map[(4, 5)] == 0.5

    def test_asymmetric_pair_returns_absolute_skew(self):
        """Length-mismatched P (10mm) / N (12mm) -> skew_mm == 2.0."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.diffpair_skew import derive_skew_data

        nc = NetClassRouting(name="HIGH_SPEED", coupled_routing=True)
        net_class_map = {"USB_D+": nc, "USB_D-": nc}

        pcb = _make_pair_pcb(p_len_mm=10.0, n_len_mm=12.0)
        skew_data, _ = derive_skew_data(pcb, net_class_map)

        assert ("USB_D+", "USB_D-") in skew_data
        assert skew_data[("USB_D+", "USB_D-")] == 2.0

    def test_per_class_skew_tolerance_override_propagates(self):
        """``skew_tolerance_mm`` on the net class reaches threshold_map."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.diffpair_skew import derive_skew_data

        nc = NetClassRouting(
            name="USB2_HS",
            coupled_routing=True,
            skew_tolerance_mm=3.0,  # USB 2.0 HS budget
        )
        net_class_map = {"USB_D+": nc, "USB_D-": nc}

        pcb = _make_pair_pcb(p_len_mm=10.0, n_len_mm=10.0)
        _, threshold_map = derive_skew_data(pcb, net_class_map)

        assert threshold_map[(4, 5)] == 3.0

    def test_one_sided_declaration_policy(self):
        """P-only declaration -> still produces skew_data + threshold_map.

        Mirrors the one-sided-declaration policy in
        :func:`derive_engagement_state` (diffpair_engagement.py:179-187):
        a net class declared on the positive side alone still gates the
        pair, and the tolerance comes from the declared side.
        """
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.diffpair_skew import derive_skew_data

        nc = NetClassRouting(
            name="USB2_HS",
            coupled_routing=True,
            skew_tolerance_mm=3.0,
        )
        # Only positive side declared (USB_D+ in map, USB_D- absent).
        net_class_map = {"USB_D+": nc}

        pcb = _make_pair_pcb(p_len_mm=10.0, n_len_mm=12.0)
        skew_data, threshold_map = derive_skew_data(pcb, net_class_map)

        # The detector still pairs USB_D+/USB_D- via suffix inference;
        # the threshold comes from the positive side's declared class.
        assert ("USB_D+", "USB_D-") in skew_data
        assert skew_data[("USB_D+", "USB_D-")] == 2.0
        assert threshold_map[(4, 5)] == 3.0

    def test_n_only_declaration_falls_back_to_negative_side(self):
        """N-only declaration -> threshold comes from negative side."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.diffpair_skew import derive_skew_data

        nc = NetClassRouting(
            name="USB2_HS",
            coupled_routing=True,
            skew_tolerance_mm=3.0,
        )
        # Only negative side declared.
        net_class_map = {"USB_D-": nc}

        pcb = _make_pair_pcb(p_len_mm=10.0, n_len_mm=12.0)
        skew_data, threshold_map = derive_skew_data(pcb, net_class_map)

        assert ("USB_D+", "USB_D-") in skew_data
        assert threshold_map[(4, 5)] == 3.0


class TestDeriveSkewDataUnroutedHalf:
    """Pairs where one half has no geometry are omitted (graceful degradation)."""

    def test_unrouted_positive_omitted(self):
        """P has no segments -> pair omitted from skew_data."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.diffpair_skew import derive_skew_data

        nc = NetClassRouting(name="HIGH_SPEED", coupled_routing=True)
        net_class_map = {"USB_D+": nc, "USB_D-": nc}

        pcb = _make_pair_pcb(p_len_mm=None, n_len_mm=10.0)  # P unrouted
        skew_data, threshold_map = derive_skew_data(pcb, net_class_map)

        # Pair is omitted (no half can be unrouted while the other has
        # an arbitrary skew).  This mirrors DiffPairLengthTracker.get_all_skews.
        assert skew_data == {}
        assert threshold_map == {}

    def test_unrouted_negative_omitted(self):
        """N has no segments -> pair omitted from skew_data."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.diffpair_skew import derive_skew_data

        nc = NetClassRouting(name="HIGH_SPEED", coupled_routing=True)
        net_class_map = {"USB_D+": nc, "USB_D-": nc}

        pcb = _make_pair_pcb(p_len_mm=10.0, n_len_mm=None)  # N unrouted
        skew_data, threshold_map = derive_skew_data(pcb, net_class_map)

        assert skew_data == {}
        assert threshold_map == {}


# ---------------------------------------------------------------------------
# Drift-prevention tests (the core property tested in this issue).
# ---------------------------------------------------------------------------


class TestSkewDriftPrevention:
    """``derive_skew_data`` MUST match :meth:`DiffPairLengthTracker.get_all_skews`.

    Builds the same physical routing in both forms (router-internal
    Route + PCB-schema segments) and asserts byte-for-byte equality of
    the resulting skew dicts.  If a future change touches
    segment-length computation in one place but not the other, this
    test fires.

    Three scenarios:

    - Symmetric pair (skew = 0).
    - Asymmetric pair (skew > 0).
    - Via-traversing pair (validates ``board_thickness_mm`` parity).
    """

    def _build_both_forms(
        self,
        *,
        p_segments: list[tuple[tuple[float, float], tuple[float, float]]],
        n_segments: list[tuple[tuple[float, float], tuple[float, float]]],
        p_vias_layers: list[list[str]] | None = None,
        n_vias_layers: list[list[str]] | None = None,
        p_net: int = 4,
        n_net: int = 5,
        p_name: str = "USB_D+",
        n_name: str = "USB_D-",
    ) -> tuple[_StubPCB, Route, Route]:
        """Return (pcb_stub, p_route, n_route) for the same physical routing."""
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.primitives import Route, Segment, Via

        # PCB-schema-shape stub.
        pcb_segments: list[_StubSegment] = []
        for start, end in p_segments:
            pcb_segments.append(
                _StubSegment(
                    start=start,
                    end=end,
                    net_number=p_net,
                    net_name=p_name,
                )
            )
        for start, end in n_segments:
            pcb_segments.append(
                _StubSegment(
                    start=start,
                    end=end,
                    net_number=n_net,
                    net_name=n_name,
                )
            )

        pcb_vias: list[_StubVia] = []
        if p_vias_layers:
            for layers in p_vias_layers:
                pcb_vias.append(_StubVia(layers=list(layers), net_number=p_net, net_name=p_name))
        if n_vias_layers:
            for layers in n_vias_layers:
                pcb_vias.append(_StubVia(layers=list(layers), net_number=n_net, net_name=n_name))

        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                p_net: _StubNet(p_net, p_name),
                n_net: _StubNet(n_net, n_name),
            },
            _segments=pcb_segments,
            _vias=pcb_vias,
        )

        # Router-internal Route shape.
        _layer_lookup = {
            "F.Cu": Layer.F_CU,
            "B.Cu": Layer.B_CU,
            "In1.Cu": Layer.IN1_CU,
            "In2.Cu": Layer.IN2_CU,
        }

        p_route = Route(
            net=p_net,
            net_name=p_name,
            segments=[
                Segment(
                    x1=start[0],
                    y1=start[1],
                    x2=end[0],
                    y2=end[1],
                    width=0.2,
                    layer=Layer.F_CU,
                    net=p_net,
                    net_name=p_name,
                )
                for start, end in p_segments
            ],
            vias=[
                Via(
                    x=0.0,
                    y=0.0,
                    drill=0.3,
                    diameter=0.6,
                    layers=(
                        _layer_lookup[layers[0]],
                        _layer_lookup[layers[-1]],
                    ),
                    net=p_net,
                    net_name=p_name,
                )
                for layers in (p_vias_layers or [])
            ],
        )
        n_route = Route(
            net=n_net,
            net_name=n_name,
            segments=[
                Segment(
                    x1=start[0],
                    y1=start[1],
                    x2=end[0],
                    y2=end[1],
                    width=0.2,
                    layer=Layer.F_CU,
                    net=n_net,
                    net_name=n_name,
                )
                for start, end in n_segments
            ],
            vias=[
                Via(
                    x=0.0,
                    y=0.0,
                    drill=0.3,
                    diameter=0.6,
                    layers=(
                        _layer_lookup[layers[0]],
                        _layer_lookup[layers[-1]],
                    ),
                    net=n_net,
                    net_name=n_name,
                )
                for layers in (n_vias_layers or [])
            ],
        )
        return pcb, p_route, n_route

    def _make_detected_pair(
        self,
        p_id: int,
        p_name: str,
        n_id: int,
        n_name: str,
    ):
        """Construct a DetectedPair matching the routes."""
        from kicad_tools.router.diffpair import (
            DifferentialPair,
            DifferentialPairType,
            DifferentialSignal,
        )
        from kicad_tools.router.diffpair_detection import (
            DetectedPair,
            DetectionSource,
        )

        return DetectedPair(
            pair=DifferentialPair(
                name="USB_D",
                positive=DifferentialSignal(
                    net_name=p_name,
                    net_id=p_id,
                    base_name="USB_D",
                    polarity="P",
                    notation="plus_minus",
                ),
                negative=DifferentialSignal(
                    net_name=n_name,
                    net_id=n_id,
                    base_name="USB_D",
                    polarity="N",
                    notation="plus_minus",
                ),
                pair_type=DifferentialPairType.USB2,
            ),
            source=DetectionSource.EXPLICIT,
        )

    def test_symmetric_pair_byte_for_byte_match(self):
        """Equal-length pair: both paths return the same {("P", "N"): 0.0}."""
        from kicad_tools.router.diffpair_length import DiffPairLengthTracker
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.diffpair_skew import derive_skew_data

        pcb, p_route, n_route = self._build_both_forms(
            p_segments=[((0.0, 0.0), (10.0, 0.0))],
            n_segments=[((0.0, 1.0), (10.0, 1.0))],
        )

        # Router-side: record routes via tracker.
        tracker = DiffPairLengthTracker()
        tracker.record_routes(
            routes=[p_route, n_route],
            detected_pairs=[self._make_detected_pair(4, "USB_D+", 5, "USB_D-")],
        )
        tracker_skews = tracker.get_all_skews()

        # Validate-side: re-derive from PCB + net class map.
        nc = NetClassRouting(name="HIGH_SPEED", coupled_routing=True)
        net_class_map = {"USB_D+": nc, "USB_D-": nc}
        rederived_skews, _ = derive_skew_data(pcb, net_class_map)

        assert rederived_skews == tracker_skews, (
            "drift-prevention AC: validator-side derive_skew_data must match "
            "producer-side DiffPairLengthTracker.get_all_skews byte-for-byte "
            "for the same physical routing"
        )
        # Sanity: zero skew for symmetric pair.
        assert rederived_skews[("USB_D+", "USB_D-")] == 0.0

    def test_asymmetric_pair_byte_for_byte_match(self):
        """Length-mismatched pair: both paths return the same skew."""
        from kicad_tools.router.diffpair_length import DiffPairLengthTracker
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.diffpair_skew import derive_skew_data

        pcb, p_route, n_route = self._build_both_forms(
            p_segments=[((0.0, 0.0), (10.0, 0.0))],
            n_segments=[((0.0, 1.0), (12.5, 1.0))],
        )

        tracker = DiffPairLengthTracker()
        tracker.record_routes(
            routes=[p_route, n_route],
            detected_pairs=[self._make_detected_pair(4, "USB_D+", 5, "USB_D-")],
        )
        tracker_skews = tracker.get_all_skews()

        nc = NetClassRouting(name="HIGH_SPEED", coupled_routing=True)
        net_class_map = {"USB_D+": nc, "USB_D-": nc}
        rederived_skews, _ = derive_skew_data(pcb, net_class_map)

        assert rederived_skews == tracker_skews
        # Sanity: 2.5 mm skew expected.
        assert abs(rederived_skews[("USB_D+", "USB_D-")] - 2.5) < 1e-9

    def test_via_traversing_pair_byte_for_byte_match(self):
        """Via on both halves: PCB-side measurement matches router-side when
        ``board_thickness_mm`` is supplied to both paths.

        Validates that the PCB-side via length formula (using
        :meth:`CopperLayer.from_kicad_name`) produces the same result as
        the router-side formula (using the enum directly).
        """
        from kicad_tools.router.diffpair_length import DiffPairLengthTracker
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.diffpair_skew import derive_skew_data

        pcb, p_route, n_route = self._build_both_forms(
            p_segments=[((0.0, 0.0), (10.0, 0.0))],
            n_segments=[((0.0, 1.0), (10.0, 1.0))],
            p_vias_layers=[["F.Cu", "B.Cu"]],  # F.Cu->B.Cu through-via on P
            n_vias_layers=[],  # no via on N
        )

        # Both sides supply the same board_thickness_mm.
        board_thickness_mm = 1.6
        num_copper_layers = 2

        tracker = DiffPairLengthTracker()
        tracker.record_routes(
            routes=[p_route, n_route],
            detected_pairs=[self._make_detected_pair(4, "USB_D+", 5, "USB_D-")],
            board_thickness_mm=board_thickness_mm,
            num_copper_layers=num_copper_layers,
        )
        tracker_skews = tracker.get_all_skews()

        nc = NetClassRouting(name="HIGH_SPEED", coupled_routing=True)
        net_class_map = {"USB_D+": nc, "USB_D-": nc}
        rederived_skews, _ = derive_skew_data(
            pcb,
            net_class_map,
            board_thickness_mm=board_thickness_mm,
            num_copper_layers=num_copper_layers,
        )

        assert rederived_skews == tracker_skews, (
            "via-traversing drift-prevention: PCB-side via length formula "
            "must produce the same result as router-side"
        )
        # Sanity: P has +1.6mm via length, so skew is 1.6 mm.
        assert abs(rederived_skews[("USB_D+", "USB_D-")] - 1.6) < 1e-9


# ---------------------------------------------------------------------------
# Constants drift-prevention (already in test_validate_diffpair_length_skew.py;
# mirrored here so the contract is asserted from both producer + rule sides).
# ---------------------------------------------------------------------------


class TestDefaultSkewToleranceDriftPrevention:
    """``DEFAULT_SKEW_TOLERANCE_MM`` MUST equal ``effective_skew_tolerance`` default.

    If a future change touches one constant without the other, this
    drift-prevention test fires.  Mirrors the equivalent test in
    ``tests/test_validate_diffpair_length_skew.py``.
    """

    def test_module_default_matches_accessor_default(self):
        import inspect

        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.rules.diffpair_length_skew import (
            DEFAULT_SKEW_TOLERANCE_MM,
        )

        sig = inspect.signature(NetClassRouting.effective_skew_tolerance)
        accessor_default = sig.parameters["default"].default

        assert accessor_default == DEFAULT_SKEW_TOLERANCE_MM, (
            "DEFAULT_SKEW_TOLERANCE_MM in validate/rules/diffpair_length_skew "
            "must match the default arg of NetClassRouting.effective_skew_tolerance "
            "(both 0.5 mm).  If you changed one, change the other."
        )


# ---------------------------------------------------------------------------
# Integration: DRCChecker.check_diffpair_length_skew uses the new wiring.
# ---------------------------------------------------------------------------


class TestCheckerIntegration:
    """End-to-end: DRCChecker.check_diffpair_length_skew picks up the wiring."""

    def test_no_net_class_map_remains_no_op(self):
        """Standalone-CLI invocation (no net_class_map) -> 0 violations, 0 rules_checked.

        AC #4: graceful degradation contract preserved.
        """
        from kicad_tools.validate.checker import DRCChecker

        pcb = _make_pair_pcb(p_len_mm=10.0, n_len_mm=12.0)
        # No net_class_map -> rule is no-op.
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_diffpair_length_skew()

        assert len(results.violations) == 0
        assert results.rules_checked == 0

    def test_with_net_class_map_fires_when_over_tolerance(self):
        """With net_class_map + over-tolerance pair -> rule fires."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.checker import DRCChecker

        # 5mm skew at 0.5mm default tolerance -> firing.
        pcb = _make_pair_pcb(p_len_mm=10.0, n_len_mm=15.0)
        nc = NetClassRouting(name="HIGH_SPEED", coupled_routing=True)
        net_class_map = {"USB_D+": nc, "USB_D-": nc}

        checker = DRCChecker(
            pcb,
            manufacturer="jlcpcb",
            layers=2,
            net_class_map=net_class_map,
        )
        results = checker.check_diffpair_length_skew()

        assert results.rules_checked == 1
        assert results.rules_checked_by_rule.get("diffpair_length_skew") == 1
        assert len(results.violations) == 1
        v = results.violations[0]
        assert v.rule_id == "diffpair_length_skew"
        assert v.actual_value == 5.0
        assert v.required_value == 0.5

    def test_with_net_class_map_passes_when_under_tolerance(self):
        """With net_class_map + within-tolerance pair -> no fire, but rule
        was exercised (rules_checked > 0).
        """
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.checker import DRCChecker

        # 0.3mm skew at 0.5mm default tolerance -> no fire.
        pcb = _make_pair_pcb(p_len_mm=10.0, n_len_mm=10.3)
        nc = NetClassRouting(name="HIGH_SPEED", coupled_routing=True)
        net_class_map = {"USB_D+": nc, "USB_D-": nc}

        checker = DRCChecker(
            pcb,
            manufacturer="jlcpcb",
            layers=2,
            net_class_map=net_class_map,
        )
        results = checker.check_diffpair_length_skew()

        assert results.rules_checked == 1
        assert results.rules_checked_by_rule.get("diffpair_length_skew") == 1
        assert len(results.violations) == 0

    def test_un_engaged_pair_does_not_fire_even_with_skew(self):
        """``coupled_routing=False`` -> pair is not engaged, rule does not fire."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.checker import DRCChecker

        pcb = _make_pair_pcb(p_len_mm=10.0, n_len_mm=15.0)  # 5mm skew
        nc = NetClassRouting(name="DEFAULT", coupled_routing=False)
        net_class_map = {"USB_D+": nc, "USB_D-": nc}

        checker = DRCChecker(
            pcb,
            manufacturer="jlcpcb",
            layers=2,
            net_class_map=net_class_map,
        )
        results = checker.check_diffpair_length_skew()

        # Pair is not engaged -> rule short-circuits, no checks.
        assert results.rules_checked == 0
        assert len(results.violations) == 0
