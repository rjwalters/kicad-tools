"""Tests for the pipeline-level connectivity invariant (issue #2596).

These tests cover the helpers in
:mod:`kicad_tools.router.connectivity_invariant` and the post-phase
revert-on-regression behaviour that wraps :class:`TraceOptimizer` and
:func:`drc_verify_and_nudge` in the route CLI.

Test outline (per the issue's test plan):

1. ``test_optimize_preserves_multi_pad_net_connectivity`` -- a Y-junction
   net stored as multiple ``Route`` objects must remain pad-to-pad
   connected after the full pipeline (optimize + nudge + invariant).
2. ``test_optimize_reverts_when_pad_drops`` -- a hostile optimiser that
   drops a branch must trigger a revert; the routes match the snapshot
   afterwards.
3. ``test_strict_mode_raises_on_regression`` -- same fixture as (2) but
   with ``strict=True`` raises :class:`ConnectivityRegressionError`.
4. ``test_drc_nudge_does_not_regress_connectivity`` -- a 2-pad net
   whose nudge displaces a pad-side endpoint is reverted.
5. ``test_invariant_does_not_revert_legitimate_segment_reduction`` --
   collapsing collinear segments is fine: pad connectivity preserved,
   no revert.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

import pytest

from kicad_tools.router.connectivity_invariant import (
    ConnectivityRegressionError,
    build_multi_pad_net_pads,
    enforce_connectivity_invariant,
    snapshot_connectivity,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment

# ---------------------------------------------------------------------------
# Lightweight Autorouter stub -- enough for the helpers to introspect.
# ---------------------------------------------------------------------------


@dataclass
class _StubAutorouter:
    """Minimal stand-in for ``Autorouter`` (matches drc_nudge tests)."""

    routes: list[Route] = field(default_factory=list)
    pads: dict = field(default_factory=dict)
    nets: dict = field(default_factory=dict)
    net_names: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _seg(
    x1: float, y1: float, x2: float, y2: float, *, net: int = 1, net_name: str = "VOUT"
) -> Segment:
    return Segment(
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        width=0.2,
        layer=Layer.F_CU,
        net=net,
        net_name=net_name,
    )


def _pad(x: float, y: float, *, ref: str, net: int = 1, net_name: str = "VOUT") -> Pad:
    return Pad(
        x=x,
        y=y,
        width=0.5,
        height=0.5,
        net=net,
        net_name=net_name,
        layer=Layer.F_CU,
        ref=ref,
        pin="1",
    )


def _make_y_junction_router() -> _StubAutorouter:
    """A 3-pad VOUT net stored as **two** Route objects sharing a junction.

    Geometry:

        pad_a (0,0) -+- pad_b (10,0)
                     |
                  pad_c (5,5)

    Route 1: arm A + arm B (single chain through junction (5, 0)).
    Route 2: arm C (junction -> pad_c).

    Pads on different Route objects is exactly the scenario the
    per-route guard misses (issue #2596: AUDIO_R has 6 pads spread
    across multiple Routes).
    """
    pad_a = _pad(0.0, 0.0, ref="A")
    pad_b = _pad(10.0, 0.0, ref="B")
    pad_c = _pad(5.0, 5.0, ref="C")

    # Route 1: kinked AB so the optimiser has work to do but won't
    # legitimately disconnect a pad.
    route_ab = Route(
        net=1,
        net_name="VOUT",
        segments=[
            _seg(0.0, 0.0, 2.0, 0.0),
            _seg(2.0, 0.0, 5.0, 0.0),
            _seg(5.0, 0.0, 7.0, 0.0),
            _seg(7.0, 0.0, 10.0, 0.0),
        ],
        vias=[],
    )
    # Route 2: arm C from junction up to pad_c.
    route_c = Route(
        net=1,
        net_name="VOUT",
        segments=[
            _seg(5.0, 0.0, 5.0, 2.0),
            _seg(5.0, 2.0, 5.0, 5.0),
        ],
        vias=[],
    )

    router = _StubAutorouter(
        routes=[route_ab, route_c],
        pads={
            ("U1", "A"): pad_a,
            ("U1", "B"): pad_b,
            ("U1", "C"): pad_c,
        },
        nets={1: [("U1", "A"), ("U1", "B"), ("U1", "C")]},
        net_names={1: "VOUT"},
    )
    return router


def _make_two_pad_router() -> _StubAutorouter:
    """A 2-pad net used for the nudge-regression test."""
    pad_a = _pad(0.0, 0.0, ref="A", net_name="SIG")
    pad_b = _pad(10.0, 0.0, ref="B", net_name="SIG")
    route = Route(
        net=2,
        net_name="SIG",
        segments=[_seg(0.0, 0.0, 10.0, 0.0, net=2, net_name="SIG")],
        vias=[],
    )
    return _StubAutorouter(
        routes=[route],
        pads={("U2", "A"): pad_a, ("U2", "B"): pad_b},
        nets={2: [("U2", "A"), ("U2", "B")]},
        net_names={2: "SIG"},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBuildMultiPadNetPads:
    """Sanity checks for the ``build_multi_pad_net_pads`` helper."""

    def test_includes_multi_pad_nets(self):
        router = _make_y_junction_router()
        out = build_multi_pad_net_pads(router)
        assert 1 in out
        assert len(out[1]) == 3

    def test_skips_single_pad_nets(self):
        router = _StubAutorouter(
            pads={("U1", "1"): _pad(0.0, 0.0, ref="U1")},
            nets={3: [("U1", "1")]},
            net_names={3: "ORPHAN"},
        )
        assert build_multi_pad_net_pads(router) == {}

    def test_filters_by_explicit_id_set(self):
        router = _make_y_junction_router()
        # Add a second 2-pad net so we can filter it out.
        router.nets[5] = [("X", "1"), ("X", "2")]
        router.pads[("X", "1")] = _pad(20.0, 0.0, ref="X", net=5, net_name="OTHER")
        router.pads[("X", "2")] = _pad(30.0, 0.0, ref="X", net=5, net_name="OTHER")
        router.net_names[5] = "OTHER"
        out = build_multi_pad_net_pads(router, multi_pad_net_ids={1})
        assert set(out.keys()) == {1}


class TestPipelinePreservesYJunction:
    """The flagship test: AUDIO_R-style multi-Route net stays connected."""

    def test_optimize_preserves_multi_pad_net_connectivity(self):
        """Y-junction net split across two Routes stays fully connected.

        On ``main`` the per-route guard inside
        :class:`TraceOptimizer.optimize_route` happily merges the four
        collinear segments of arm A+B into a single ``(0,0) -> (10,0)``
        segment, dropping the ``(5, 0)`` apex vertex that arm C joins
        at.  The result is a multi-pad-net regression even though both
        Routes look fine in isolation.  This test asserts the
        pipeline-level invariant catches that case, so the **final**
        ``validate_net_connectivity`` result is ``connected=True``
        regardless of whether the underlying optimiser was correct or
        the guard had to revert.

        The test fails on ``main`` (no pipeline guard exists) and
        passes after the fix.
        """
        from kicad_tools.router.observability import validate_net_connectivity
        from kicad_tools.router.optimizer import TraceOptimizer

        router = _make_y_junction_router()
        snapshot = snapshot_connectivity(router)
        # Pre-optimise: net 1 must already be fully connected.
        assert snapshot.pre_connectivity[1]["connected"] is True

        optimizer = TraceOptimizer()
        router.routes = [optimizer.optimize_route(r) for r in router.routes]

        # Without the guard, the per-route optimiser drops the
        # ``(5, 0)`` apex by merging arm A+B into a single segment.
        # The pipeline-level invariant must detect the regression and
        # either revert or (in strict mode) raise.  In default mode the
        # final connectivity must be restored.
        enforce_connectivity_invariant(
            router,
            snapshot,
            phase="optimize",
            strict=False,
            quiet=True,
        )

        post = validate_net_connectivity(router.routes, snapshot.net_pads)
        assert post[1]["connected"] is True, (
            "Pipeline guard must restore Y-junction connectivity "
            "(issue #2596): post-phase pad count was "
            f"{post[1]['connected_pads']}/{post[1]['total_pads']}"
        )
        assert post[1]["connected_pads"] == 3


class TestRevertOnRegression:
    """Scenario (2) and (3): hostile optimiser drops a branch."""

    def _hostile_optimize(self, router: _StubAutorouter) -> None:
        """Simulate a buggy optimiser that drops an entire Route.

        Picks the second Route (arm C in the Y-junction fixture) and
        clears its segments, leaving pad_c stranded.  Mirrors the
        AUDIO_R-style regression where a multi-Route net loses a pad
        because the per-route guard cannot see across Route boundaries.
        """
        # Drop arm C entirely.  Router.routes is mutated in place.
        new_routes = []
        for r in router.routes:
            # Identify the C arm by checking its segments touch (5, 5).
            if any(abs(s.x2 - 5.0) < 1e-3 and abs(s.y2 - 5.0) < 1e-3 for s in r.segments):
                # Drop it -- this is the bug we want to detect.
                continue
            new_routes.append(r)
        router.routes = new_routes

    def test_optimize_reverts_when_pad_drops(self):
        router = _make_y_junction_router()
        snapshot = snapshot_connectivity(router)
        assert snapshot.pre_connectivity[1]["connected"] is True

        # Save a deep copy of the pre-phase state for later equality.
        pre_routes = copy.deepcopy(router.routes)

        self._hostile_optimize(router)
        # Sanity: we really did drop arm C.
        assert len(router.routes) == 1

        result = enforce_connectivity_invariant(
            router,
            snapshot,
            phase="optimize",
            strict=False,
            quiet=True,
        )

        assert 1 in result.regressed_nets
        assert result.reverted is True
        # Routes should have been restored from the snapshot.  Compare
        # by counts and segment endpoints since deep-copy identity
        # would differ.
        assert len(router.routes) == len(pre_routes)
        seg_count_post = sum(len(r.segments) for r in router.routes)
        seg_count_pre = sum(len(r.segments) for r in pre_routes)
        assert seg_count_post == seg_count_pre
        # Connectivity is restored.
        from kicad_tools.router.observability import validate_net_connectivity

        post = validate_net_connectivity(router.routes, snapshot.net_pads)
        assert post[1]["connected"] is True

    def test_strict_mode_raises_on_regression(self):
        router = _make_y_junction_router()
        snapshot = snapshot_connectivity(router)

        self._hostile_optimize(router)

        with pytest.raises(ConnectivityRegressionError) as excinfo:
            enforce_connectivity_invariant(
                router,
                snapshot,
                phase="optimize",
                strict=True,
                quiet=True,
            )
        assert excinfo.value.phase == "optimize"
        assert 1 in excinfo.value.result.regressed_nets


class TestNudgeRegression:
    """Scenario (4): nudge displaces a pad endpoint and is reverted."""

    def test_drc_nudge_does_not_regress_connectivity(self):
        """Simulate a nudge that translates the trace away from the pads.

        We do not invoke ``drc_verify_and_nudge`` directly because that
        requires DRC violations to act on; instead we manually move the
        endpoints of the only segment to model the post-nudge state.
        The invariant should detect the drop and revert.
        """
        router = _make_two_pad_router()
        snapshot = snapshot_connectivity(router)
        assert snapshot.pre_connectivity[2]["connected"] is True

        # Manually displace the segment by a large perpendicular amount
        # so neither endpoint is within the 2 mm proximity that
        # validate_net_connectivity uses to bind a pad to a segment
        # endpoint.
        only_segment = router.routes[0].segments[0]
        only_segment.y1 += 5.0
        only_segment.y2 += 5.0

        result = enforce_connectivity_invariant(
            router,
            snapshot,
            phase="nudge",
            strict=False,
            quiet=True,
        )
        assert 2 in result.regressed_nets
        assert result.reverted is True
        # Segment is restored to its original pad-aligned coordinates.
        restored = router.routes[0].segments[0]
        assert restored.y1 == pytest.approx(0.0)
        assert restored.y2 == pytest.approx(0.0)


class TestNoRevertOnLegitimateReduction:
    """Scenario (5): merging collinear segments must not trigger a revert.

    The optimiser commonly halves the segment count by collapsing
    collinear chains; that is *correct* behaviour and the invariant
    must not flag it.  We construct a 2-pad net with a redundant
    midpoint and run the real :class:`TraceOptimizer`.
    """

    def test_invariant_does_not_revert_legitimate_segment_reduction(self):
        from kicad_tools.router.optimizer import TraceOptimizer

        router = _make_two_pad_router()
        # Replace the single-segment route with a 2-segment chain so
        # merge_collinear has something to collapse.
        router.routes[0].segments = [
            _seg(0.0, 0.0, 5.0, 0.0, net=2, net_name="SIG"),
            _seg(5.0, 0.0, 10.0, 0.0, net=2, net_name="SIG"),
        ]
        snapshot = snapshot_connectivity(router)
        pre_segments = sum(len(r.segments) for r in router.routes)

        optimizer = TraceOptimizer()
        router.routes = [optimizer.optimize_route(r) for r in router.routes]
        post_segments = sum(len(r.segments) for r in router.routes)

        # Sanity: the merge must really have reduced segment count.
        assert post_segments < pre_segments

        result = enforce_connectivity_invariant(
            router,
            snapshot,
            phase="optimize",
            strict=False,
            quiet=True,
        )
        assert result.regressed_nets == set()
        assert result.reverted is False
        # Connectivity preserved.
        from kicad_tools.router.observability import validate_net_connectivity

        post = validate_net_connectivity(router.routes, snapshot.net_pads)
        assert post[2]["connected"] is True


class TestPerNetDiff:
    """The result's per-net diff must list every multi-pad net checked."""

    def test_per_net_diff_includes_all_multi_pad_nets(self):
        router = _make_y_junction_router()
        snapshot = snapshot_connectivity(router)
        # No mutation: just enforce.
        result = enforce_connectivity_invariant(
            router,
            snapshot,
            phase="optimize",
            strict=False,
            quiet=True,
        )
        assert 1 in result.per_net_diff
        pre_pads, post_pads, total_pads, name, regressed = result.per_net_diff[1]
        assert total_pads == 3
        assert pre_pads == 3 and post_pads == 3
        assert name == "VOUT"
        assert regressed is False
