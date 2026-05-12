"""Tests for ``Autorouter.apply_match_group_tuning`` (Epic #2661 Phase 3H).

Issue #2723.  This module covers the orchestrator wrapper around
:func:`kicad_tools.router.match_group_tuning.tune_match_group_v2`:

1. **Triple-gate** -- the call chain Autorouter -> tune_match_group_v2 ->
   serpentine insert actually fires for each detected group.
2. **Pair-aware dispatch (AC6)** -- a :class:`MatchGroup` with
   ``pair_ids`` populated is routed to the Phase 2F symmetric path
   (no ``AssertionError`` from the historical entry guard).
3. **Cascade-budget exhaustion** -- a synthetic short-segment group
   exhausts the per-member budget without breaking the pipeline.
4. **Signature drift-prevention (AC8)** -- the public method's return
   type and parameter names are asserted byte-for-byte so a future
   refactor cannot silently break the CLI consumer.
5. **Net-class resolution helper** -- ``_resolve_net_class_for_group``
   prefers the explicit reference net, then scalar members, then pair
   members, before falling back to ``None``.
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.match_group_length import MatchGroup, MatchGroupSource
from kicad_tools.router.match_group_tuning import TuneResult
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import NetClassRouting

# =============================================================================
# Test helpers
# =============================================================================


def _straight_route(net_id: int, name: str, length_mm: float, y: float = 0.0) -> Route:
    """Single horizontal segment along +x at y=``y``."""
    return Route(
        net=net_id,
        net_name=name,
        segments=[
            Segment(
                x1=0.0,
                y1=y,
                x2=length_mm,
                y2=y,
                width=0.2,
                layer=Layer.F_CU,
                net=net_id,
                net_name=name,
            )
        ],
    )


def _make_autorouter_with_4_net_group() -> tuple[Autorouter, MatchGroup]:
    """Build an autorouter with a 4-net DDR-style mismatched group.

    Lengths: 22mm, 20mm, 18mm, 18mm.  Reference = longest = net 1 (22mm).
    Net 2 needs +2mm, nets 3 & 4 need +4mm to reach tolerance.  Routes are
    spaced apart in y so they have ample room for outer-normal bulges
    without immediately tripping intra-group clearance.
    """
    ar = Autorouter(width=80.0, height=80.0)
    ar.net_names = {1: "DQ0", 2: "DQ1", 3: "DQ2", 4: "DQ3"}
    ar.routes = [
        _straight_route(1, "DQ0", 22.0, y=0.0),
        _straight_route(2, "DQ1", 20.0, y=10.0),
        _straight_route(3, "DQ2", 18.0, y=20.0),
        _straight_route(4, "DQ3", 18.0, y=30.0),
    ]
    group = MatchGroup(
        name="DDR_DATA_BYTE_0",
        net_ids=[1, 2, 3, 4],
        tolerance=0.1,
        reference_net_id=1,
        source=MatchGroupSource.LEGACY_API,
    )
    return ar, group


# =============================================================================
# 1. Triple-gate: orchestrator invokes tuner for each detected group
# =============================================================================


class TestTripleGate:
    """The full call chain Autorouter -> tune_match_group_v2 -> serpentine."""

    def test_orchestrator_invokes_tuner_for_each_group(self):
        from kicad_tools.router import match_group_tuning as mgt_module

        ar, group = _make_autorouter_with_4_net_group()
        # Two groups so the spy must fire exactly twice.
        group2 = MatchGroup(
            name="DDR_DATA_BYTE_1",
            net_ids=[1, 2, 3, 4],
            tolerance=0.5,
            reference_net_id=1,
            source=MatchGroupSource.LEGACY_API,
        )
        detected = [group, group2]

        call_count = {"n": 0}
        real_tune = mgt_module.tune_match_group_v2

        def spy(*args, **kwargs):
            call_count["n"] += 1
            return real_tune(*args, **kwargs)

        with patch.object(mgt_module, "tune_match_group_v2", spy):
            results = ar.apply_match_group_tuning(
                detected_groups=detected,
                verbose=False,
            )

        assert call_count["n"] == 2, (
            f"Expected exactly 2 tune_match_group_v2 calls (one per detected "
            f"group), got {call_count['n']}"
        )
        # Each group must appear in the result keyed by name (AC8 shape).
        assert "DDR_DATA_BYTE_0" in results
        assert "DDR_DATA_BYTE_1" in results

    def test_returns_per_member_tune_results_keyed_by_net_id(self):
        ar, group = _make_autorouter_with_4_net_group()
        results = ar.apply_match_group_tuning(
            detected_groups=[group],
            verbose=False,
        )

        # Outer key = group name, inner key = net_id, value = (Route, TuneResult).
        assert "DDR_DATA_BYTE_0" in results
        per_member = results["DDR_DATA_BYTE_0"]
        assert set(per_member.keys()) == {1, 2, 3, 4}
        for net_id, (route, result) in per_member.items():
            assert isinstance(route, Route)
            assert isinstance(result, TuneResult)
            assert route.net == net_id

    def test_reference_net_returned_unchanged(self):
        """The pace-car (reference net) is never modified; same Route reference."""
        ar, group = _make_autorouter_with_4_net_group()
        original_ref_route = ar.routes[0]  # net 1 is reference
        original_ref_segments = original_ref_route.segments

        results = ar.apply_match_group_tuning(
            detected_groups=[group],
            verbose=False,
        )

        ref_route, ref_result = results["DDR_DATA_BYTE_0"][1]
        assert ref_result.reason == "reference"
        # By-reference rollback contract.
        assert ref_route is original_ref_route
        assert ref_route.segments is original_ref_segments

    def test_match_group_tracker_updated_with_post_tuning_lengths(self):
        """The pre/post bracket: tracker reflects final geometry."""
        ar, group = _make_autorouter_with_4_net_group()
        ar.apply_match_group_tuning(
            detected_groups=[group],
            verbose=False,
        )
        # The tracker must have at least one length recorded for each
        # member (it was updated both before and after the tuning loop).
        for net_id in group.net_ids:
            assert net_id in ar.match_group_tracker.lengths


# =============================================================================
# 2. AC6: pair-aware dispatch (Phase 2F symmetric serpentine path)
# =============================================================================


class TestPairAwareDispatch:
    """A MatchGroup with ``pair_ids`` populated runs through the Phase 2F path.

    The original Phase 2E entry guard at ``match_group_tuning.py`` was
    ``assert not group.pair_ids`` which would raise ``AssertionError``
    when pair_ids was non-empty.  Phase 2F (#2701, integrated by PR #2717)
    replaced that guard with an internal dispatcher; this test asserts
    the orchestrator does NOT trip the legacy guard and produces results
    keyed on each half's net id.
    """

    def test_pair_aware_dispatch(self):
        """A group with two diff-pair members reaches the symmetric path."""
        ar = Autorouter(width=80.0, height=80.0)
        # MIPI lane group: two pairs (LANE0_P/N, LANE1_P/N), no scalar
        # members.  Reference net id is unset so the longest member's
        # lane-length wins by the legacy default.
        ar.net_names = {
            10: "MIPI_DAT0_P",
            11: "MIPI_DAT0_N",
            20: "MIPI_DAT1_P",
            21: "MIPI_DAT1_N",
        }
        ar.routes = [
            _straight_route(10, "MIPI_DAT0_P", 18.0, y=0.0),
            _straight_route(11, "MIPI_DAT0_N", 18.0, y=2.0),
            _straight_route(20, "MIPI_DAT1_P", 14.0, y=10.0),
            _straight_route(21, "MIPI_DAT1_N", 14.0, y=12.0),
        ]
        group = MatchGroup(
            name="MIPI_LANES",
            net_ids=[],
            pair_ids=[(10, 11), (20, 21)],
            tolerance=0.1,
            reference_net_id=None,
            source=MatchGroupSource.LEGACY_API,
        )

        # Must NOT raise AssertionError -- the historical Phase 2E guard
        # would fire here; the Phase 2F dispatcher must take over.
        results = ar.apply_match_group_tuning(
            detected_groups=[group],
            verbose=False,
        )

        assert "MIPI_LANES" in results
        per_member = results["MIPI_LANES"]
        # Both halves of both pairs should appear in the result.
        assert set(per_member.keys()) == {10, 11, 20, 21}
        # No member's reason should be the legacy entry-guard sentinel.
        for net_id, (_route, result) in per_member.items():
            assert result.reason != "AssertionError", (
                f"net {net_id} reason={result.reason!r} -- the orchestrator "
                "should never expose the legacy Phase 2E entry-guard reason"
            )

    def test_pair_aware_dispatch_passes_intra_pair_clearance(self):
        """The orchestrator passes ``intra_pair_clearance_mm`` unconditionally.

        Phase 2F (#2701) requires this kwarg when ``pair_ids`` is non-empty;
        omitting it raises ``ValueError`` at the dispatcher.  This test
        asserts the orchestrator never triggers that ValueError.
        """
        from kicad_tools.router import match_group_tuning as mgt_module

        ar = Autorouter(width=80.0, height=80.0)
        ar.net_names = {10: "P0", 11: "N0", 20: "P1", 21: "N1"}
        ar.routes = [
            _straight_route(10, "P0", 12.0, y=0.0),
            _straight_route(11, "N0", 12.0, y=2.0),
            _straight_route(20, "P1", 10.0, y=10.0),
            _straight_route(21, "N1", 10.0, y=12.0),
        ]
        group = MatchGroup(
            name="PAIR_GROUP",
            net_ids=[],
            pair_ids=[(10, 11), (20, 21)],
            tolerance=0.1,
            source=MatchGroupSource.LEGACY_API,
        )

        observed_kwargs: dict[str, object] = {}
        real_tune = mgt_module.tune_match_group_v2

        def spy(*args, **kwargs):
            observed_kwargs.update(kwargs)
            return real_tune(*args, **kwargs)

        with patch.object(mgt_module, "tune_match_group_v2", spy):
            ar.apply_match_group_tuning(
                detected_groups=[group],
                verbose=False,
            )

        assert "intra_pair_clearance_mm" in observed_kwargs, (
            "apply_match_group_tuning must pass intra_pair_clearance_mm to "
            "tune_match_group_v2 unconditionally; Phase 2F requires it."
        )
        # Default should fall through to rules.trace_clearance.
        assert observed_kwargs["intra_pair_clearance_mm"] == ar.rules.trace_clearance


# =============================================================================
# 3. Cascade-budget exhaustion (graceful, no crash)
# =============================================================================


class TestCascadeBudgetExhaustion:
    """A short-segment group cannot fit serpentines and exhausts the budget.

    The exact reason returned depends on the per-member geometry: it may
    be ``"no_suitable_segment"`` (segment too short for any amplitude) or
    ``"exceeded_max_inserts"`` (budget burned without reaching tolerance).
    Both are valid graceful failures.  The point is that the orchestrator
    does NOT crash and returns a TuneResult for every member.
    """

    def test_no_crash_when_budget_exhausted(self):
        ar = Autorouter(width=80.0, height=80.0)
        # Three nets, very short routes -- no room for any serpentine.
        # The reference is net 1 at 5mm; nets 2 and 3 are 0.5mm each --
        # 4.5mm short of the reference, far more than any single bulge can
        # add at the segment's tiny length.
        ar.net_names = {1: "S0", 2: "S1", 3: "S2"}
        ar.routes = [
            _straight_route(1, "S0", 5.0, y=0.0),
            _straight_route(2, "S1", 0.5, y=10.0),
            _straight_route(3, "S2", 0.5, y=20.0),
        ]
        group = MatchGroup(
            name="SHORT_BUS",
            net_ids=[1, 2, 3],
            tolerance=0.05,
            reference_net_id=1,
            source=MatchGroupSource.LEGACY_API,
        )

        results = ar.apply_match_group_tuning(
            detected_groups=[group],
            verbose=False,
        )

        # Every member must appear in the result.
        per_member = results["SHORT_BUS"]
        assert set(per_member.keys()) == {1, 2, 3}
        # Net 1 is the reference, never tuned.
        assert per_member[1][1].reason == "reference"
        # Nets 2 & 3 are too short -- one of the graceful-failure reasons.
        graceful_failure_reasons = {
            "no_suitable_segment",
            "exceeded_max_inserts",
            "cascade_budget_exhausted",
            "post_insertion_drc_violation",
            "tuned",  # if the tuner happens to find a way
            "already_within_tolerance",  # tolerance might be wide enough
        }
        for nid in (2, 3):
            assert per_member[nid][1].reason in graceful_failure_reasons, (
                f"net {nid} returned unexpected reason {per_member[nid][1].reason!r}"
            )


# =============================================================================
# 4. AC8: signature drift-prevention
# =============================================================================


class TestSignatureDriftPrevention:
    """The public method's signature is asserted byte-for-byte.

    A future refactor that drops or renames a parameter, or changes the
    return-type annotation, must update this test in the same commit so
    the CLI consumer (``--length-match-groups``) cannot silently break.
    """

    def test_apply_match_group_tuning_signature(self):
        sig = inspect.signature(Autorouter.apply_match_group_tuning)
        param_names = [name for name in sig.parameters if name != "self"]
        # AC8: parameter names asserted exactly.
        assert param_names == ["detected_groups", "verbose"], (
            f"apply_match_group_tuning signature drift: {param_names}"
        )
        # `verbose` must default to True (mirrors the diff-pair sibling).
        assert sig.parameters["verbose"].default is True
        # `detected_groups` is positional-or-keyword with no default.
        assert sig.parameters["detected_groups"].default is inspect.Parameter.empty

    def test_apply_match_group_tuning_returns_dict_of_dict_of_tuple(self):
        """The return type at runtime is dict[str, dict[int, tuple[Route, TuneResult]]]."""
        ar, group = _make_autorouter_with_4_net_group()
        results = ar.apply_match_group_tuning(
            detected_groups=[group],
            verbose=False,
        )

        assert isinstance(results, dict)
        # Outer key: group name (string).
        for outer_key in results:
            assert isinstance(outer_key, str)
        # Inner: dict[int, tuple[Route, TuneResult]].
        for outer_key, inner in results.items():
            assert isinstance(inner, dict)
            for inner_key, value in inner.items():
                assert isinstance(inner_key, int)
                assert isinstance(value, tuple)
                assert len(value) == 2
                route, result = value
                assert isinstance(route, Route)
                assert isinstance(result, TuneResult)


# =============================================================================
# 5. Net-class resolution helper
# =============================================================================


class TestResolveNetClassForGroup:
    """``_resolve_net_class_for_group`` priority: ref-net > scalar > pair > None."""

    def test_returns_none_when_no_class_map(self):
        ar = Autorouter(width=10.0, height=10.0)
        ar.net_class_map = {}  # type: ignore[assignment]
        ar.net_names = {1: "FOO", 2: "BAR"}
        group = MatchGroup(name="G", net_ids=[1, 2])
        assert ar._resolve_net_class_for_group(group) is None

    def test_returns_class_keyed_by_reference_net(self):
        ar = Autorouter(width=10.0, height=10.0)
        ar.net_names = {1: "FOO", 2: "BAR"}
        nc = NetClassRouting(name="HIGH_SPEED", length_critical=True)
        ar.net_class_map = {"BAR": nc}
        group = MatchGroup(name="G", net_ids=[1, 2], reference_net_id=2)
        # Reference net is BAR -- its class wins.
        assert ar._resolve_net_class_for_group(group) is nc

    def test_falls_back_to_scalar_member_when_no_reference(self):
        ar = Autorouter(width=10.0, height=10.0)
        ar.net_names = {1: "FOO", 2: "BAR"}
        nc = NetClassRouting(name="HIGH_SPEED", length_critical=True)
        ar.net_class_map = {"FOO": nc}
        group = MatchGroup(name="G", net_ids=[1, 2], reference_net_id=None)
        # First scalar member (FOO) wins.
        assert ar._resolve_net_class_for_group(group) is nc

    def test_falls_back_to_pair_member_when_no_scalar(self):
        ar = Autorouter(width=10.0, height=10.0)
        ar.net_names = {10: "P0", 11: "N0"}
        nc = NetClassRouting(name="HIGH_SPEED", length_critical=True)
        ar.net_class_map = {"P0": nc}
        group = MatchGroup(name="G", net_ids=[], pair_ids=[(10, 11)])
        # First pair's positive half (P0) wins.
        assert ar._resolve_net_class_for_group(group) is nc

    def test_returns_none_when_no_member_in_class_map(self):
        ar = Autorouter(width=10.0, height=10.0)
        ar.net_names = {1: "FOO", 2: "BAR"}
        ar.net_class_map = {"OTHER": NetClassRouting(name="X")}
        group = MatchGroup(name="G", net_ids=[1, 2])
        assert ar._resolve_net_class_for_group(group) is None


# =============================================================================
# 6. Empty / edge-case inputs
# =============================================================================


class TestEdgeCases:
    """Empty / edge-case inputs do not crash."""

    def test_empty_detected_groups_returns_empty_dict(self):
        ar = Autorouter(width=10.0, height=10.0)
        ar.routes = []
        results = ar.apply_match_group_tuning(detected_groups=[], verbose=False)
        assert results == {}

    def test_group_with_unrouted_member_does_not_crash(self):
        ar = Autorouter(width=80.0, height=80.0)
        ar.net_names = {1: "A", 2: "B", 3: "C"}
        # Only nets 1 and 2 are routed; net 3 is in the group but unrouted.
        ar.routes = [
            _straight_route(1, "A", 10.0, y=0.0),
            _straight_route(2, "B", 8.0, y=10.0),
        ]
        group = MatchGroup(
            name="MIXED",
            net_ids=[1, 2, 3],
            tolerance=0.1,
            reference_net_id=1,
        )
        # Must not raise.
        results = ar.apply_match_group_tuning(
            detected_groups=[group],
            verbose=False,
        )
        assert "MIXED" in results
        # Net 3 is unrouted -> reason="unrouted".
        per_member = results["MIXED"]
        assert per_member[3][1].reason == "unrouted"
