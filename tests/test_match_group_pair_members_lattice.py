"""Match-group tuning of diff-pair members on lattice output (Issue #6675).

The Phase 2F pair path mirrors a P-side serpentine onto a corresponding N host
segment.  Lattice output defeats it twice: uncoupled pairs have no parallel N
host, and coupled pairs are offset curves of one centreline whose P and N
breakpoints differ, so the #4984 guard refuses every reflection.  With
``pair_fallbacks=True`` (the lattice auto-engaged path) the tuner now retries
hosts, places centreline meanders on coupled spans and tunes uncoupled legs on
their own -- always under the pair-skew invariant -- and the router rolls back a
group it still cannot bring inside its limit.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kicad_tools.router.clearance_shapes import copper_gap, segment_shape
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.match_group_length import (
    MatchGroup,
    MatchGroupSource,
    MatchGroupTracker,
)
from kicad_tools.router.match_group_tuning import (
    MAX_SEGMENT_RETRY_CANDIDATES,
    tune_match_group_v2,
)
from kicad_tools.router.optimizer.serpentine import SerpentineConfig
from kicad_tools.router.pair_centerline_meander import (
    centerline_meander,
    find_coupled_spans,
)
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import NetClassRouting

THICKNESS = 1.6
LAYERS = 4


def _seg(x1: float, y1: float, x2: float, y2: float, net: int, layer: Layer = Layer.F_CU):
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=layer, net=net, net_name=f"N{net}")


def _route(net: int, pts: list[tuple[float, float]], vias: list[Via] | None = None) -> Route:
    return Route(
        net=net,
        net_name=f"N{net}",
        segments=[_seg(*a, *b, net) for a, b in zip(pts[:-1], pts[1:], strict=True)],
        vias=list(vias or []),
    )


def _measure(route: Route) -> float:
    return MatchGroupTracker._measure_route_total(
        route, THICKNESS, LAYERS, blind_buried_supported=False
    )


def _contiguous(route: Route) -> bool:
    return all(
        math.isclose(a.x2, b.x1, abs_tol=1e-6) and math.isclose(a.y2, b.y1, abs_tol=1e-6)
        for a, b in zip(route.segments, route.segments[1:], strict=False)
    )


def _min_pair_gap(p: Route, n: Route) -> float:
    return min(
        copper_gap(segment_shape(a), segment_shape(b))
        for a in p.segments
        for b in n.segments
        if a.layer == b.layer
    )


def _pairs_group(pairs: list[tuple[int, int]], tolerance: float = 0.05) -> MatchGroup:
    return MatchGroup(
        name="LANES",
        net_ids=[],
        pair_ids=pairs,
        tolerance=tolerance,
        source=MatchGroupSource.LEGACY_API,
    )


def _reference_pair(y: float, length: float = 20.0) -> dict[int, Route]:
    """A long, straight, coupled reference lane (nets 3/4) at ``y``."""
    return {
        3: _route(3, [(0.0, y), (length, y)]),
        4: _route(4, [(0.0, y + 0.3266), (length, y + 0.3266)]),
    }


def _tune(group: MatchGroup, routes: dict[int, Route], **kw):
    kwargs: dict[str, object] = {
        "tolerance_mm": group.tolerance,
        "intra_group_clearance_mm": 0.2,
        "intra_pair_clearance_mm": 0.1,
        "board_thickness_mm": THICKNESS,
        "num_copper_layers": LAYERS,
        "blind_buried_supported": False,
        "pair_fallbacks": True,
    }
    kwargs.update(kw)
    return tune_match_group_v2(group, routes, **kwargs)


# Lattice-style coupled pair: offset rails of one centreline at pitch 0.3266,
# turning 45 degrees left at x~10.  The outer (P) rail's miter is 0.135mm
# later than the inner (N) rail's, so no N segment has P's span.
_P_LATTICE = [(0.0, 0.0), (10.0676, 0.0), (14.0676, 4.0)]
_N_LATTICE = [(0.0, 0.3266), (9.9324, 0.3266), (13.9324, 4.3266)]


def _lattice_pair() -> dict[int, Route]:
    return {1: _route(1, _P_LATTICE), 2: _route(2, _N_LATTICE)}


class TestUncoupledPairPerLeg:
    """(a) an uncoupled pair is lengthened leg by leg."""

    def _routes(self) -> dict[int, Route]:
        via = Via(x=10.0, y=3.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=2)
        n_route = Route(
            net=2,
            net_name="N2",
            segments=[_seg(0.0, 3.0, 10.0, 3.0, 2), _seg(10.0, 3.0, 11.0, 3.0, 2, Layer.B_CU)],
            vias=[via],
        )
        return {1: _route(1, [(0.0, 0.0), (12.0, 0.0)]), 2: n_route, **_reference_pair(6.0)}

    def test_lane_reaches_target_and_skew_does_not_grow(self):
        routes = self._routes()
        original = dict(routes)  # the tuner publishes commits into ``routes``
        skew_before = abs(_measure(routes[1]) - _measure(routes[2]))
        assert skew_before > 0.5  # 2-via-free P vs via-carrying N
        target = (_measure(routes[3]) + _measure(routes[4])) / 2.0

        results = _tune(_pairs_group([(1, 2), (3, 4)]), routes, pair_skew_limits_mm={(1, 2): 0.05})

        p_route, p_res = results[1]
        n_route, n_res = results[2]
        assert p_res.reason == n_res.reason == "tuned"
        lane = (_measure(p_route) + _measure(n_route)) / 2.0
        assert lane == pytest.approx(target, abs=0.05)
        assert abs(_measure(p_route) - _measure(n_route)) <= skew_before
        # Each leg got its own meander; both stay connected.
        assert len(p_route.segments) > 1 and _contiguous(p_route)
        assert _measure(n_route) > _measure(original[2])

    def test_legacy_path_still_declines(self):
        results = _tune(_pairs_group([(1, 2), (3, 4)]), self._routes(), pair_fallbacks=False)
        assert results[1][1].reason == "no_suitable_segment"

    def test_one_sided_lengthening_is_rolled_back(self):
        """The invariant: a leg that cannot be tuned vetoes its partner's meander."""
        routes = self._routes()
        # N becomes a staircase of sub-millimetre steps: no host for a meander.
        stairs = [(0.0, 3.0)]
        for k in range(1, 16):
            stairs.append((0.7 * k, 3.0 + (0.7 if k % 2 else 0.0)))
        routes[2] = _route(2, stairs)
        original_p, original_n = routes[1], routes[2]

        results = _tune(_pairs_group([(1, 2), (3, 4)]), routes)

        assert results[1][0] is original_p
        assert results[2][0] is original_n
        assert results[1][1].reason == "rolled_back_pair_skew"


class TestLatticeCoupledPairCentreline:
    """(b) offset rails get equal length from a centreline meander."""

    def test_reflection_guard_rejects_without_fallbacks(self):
        routes = {**_lattice_pair(), **_reference_pair(6.0)}
        results = _tune(_pairs_group([(1, 2), (3, 4)]), routes, pair_fallbacks=False)
        assert results[1][1].reason == "no_suitable_segment"
        assert results[1][0] is routes[1]

    def test_equal_length_gap_kept_and_coupled(self):
        routes = {**_lattice_pair(), **_reference_pair(6.0)}
        p0, n0 = routes[1], routes[2]
        gap0 = _min_pair_gap(p0, n0)

        results = _tune(_pairs_group([(1, 2), (3, 4)]), routes)

        p_route, p_res = results[1]
        n_route, _ = results[2]
        assert p_res.reason == "tuned"
        added_p = _measure(p_route) - _measure(p0)
        added_n = _measure(n_route) - _measure(n0)
        assert added_p > 1.0
        assert added_p == pytest.approx(added_n, abs=1e-3)
        assert _contiguous(p_route) and _contiguous(n_route)
        # Gap kept: the rails never come closer than they were routed.
        assert _min_pair_gap(p_route, n_route) >= gap0 - 1e-6
        # Still coupled: every P segment has a parallel N segment inside the
        # continuity rule's 0.5 mm window.
        for ps in p_route.segments:
            assert any(
                abs((ps.x2 - ps.x1) * (ns.y2 - ns.y1) - (ps.y2 - ps.y1) * (ns.x2 - ns.x1)) < 1e-6
                and copper_gap(segment_shape(ps), segment_shape(ns)) <= 0.5
                for ns in n_route.segments
            ), ps

    def test_primitive_adds_identical_length_to_both_rails(self):
        p, n = _route(1, _P_LATTICE), _route(2, _N_LATTICE)
        span = find_coupled_spans(p, n, max_edge_gap_mm=0.5)[0]
        for side in (1, -1):
            m = centerline_meander(
                span,
                p.segments[span.p_index],
                n.segments[span.n_index],
                length_add_mm=3.0,
                amplitude_mm=1.0,
                leg_spacing_mm=0.4,
                side=side,
            )
            assert m is not None
            host_p = math.hypot(*(b - a for a, b in zip((0.0, 0.0), (10.0676, 0.0), strict=True)))
            host_n = 9.9324
            len_p = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in m.p_segments)
            len_n = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in m.n_segments)
            assert len_p - host_p == pytest.approx(3.0, abs=1e-9)
            assert len_n - host_n == pytest.approx(3.0, abs=1e-9)


class TestHostRetry:
    """(c) a failing first host falls through to the next ranked host."""

    def _routes(self) -> dict[int, Route]:
        p = Route(net=1, net_name="N1", segments=[_seg(0, 0, 9, 0, 1), _seg(9, 0, 15, 0, 1)])
        n = Route(
            net=2, net_name="N2", segments=[_seg(0, 0.5, 9, 0.5, 2), _seg(9, 0.5, 15, 0.5, 2)]
        )
        # A foreign trace under the longest host blocks its outer bulge only.
        blocker = _route(99, [(1.0, -0.5), (8.0, -0.5)])
        ref = {3: _route(3, [(0, 5), (17, 5)]), 4: _route(4, [(0, 5.5), (17, 5.5)])}
        return {1: p, 2: n, 99: blocker, **ref}

    def test_second_ranked_host_is_used(self):
        assert MAX_SEGMENT_RETRY_CANDIDATES >= 2
        routes = self._routes()
        first_p, second_p = routes[1].segments
        first_n, second_n = routes[2].segments
        config = SerpentineConfig(amplitude=0.5, gap_factor=2.0)

        legacy = _tune(
            _pairs_group([(1, 2), (3, 4)]), self._routes(), pair_fallbacks=False, config=config
        )
        assert legacy[1][1].reason == "post_insertion_drc_violation"

        results = _tune(_pairs_group([(1, 2), (3, 4)]), routes, config=config)
        p_route, p_res = results[1]
        n_route, _ = results[2]
        assert p_res.reason == "tuned"
        # The first host is untouched, the second carries the meander.
        assert any(s is first_p for s in p_route.segments)
        assert not any(s is second_p for s in p_route.segments)
        assert any(s is first_n for s in n_route.segments)
        assert not any(s is second_n for s in n_route.segments)
        assert all(s.x1 >= 9.0 - 1e-9 for s in p_route.segments[1:])


class TestGridStyleUnchanged:
    """(d) a grid-style coupled pair tunes exactly as before."""

    @staticmethod
    def _routes() -> dict[int, Route]:
        return {
            10: _route(10, [(0.0, 0.0), (8.0, 0.0)]),
            11: _route(11, [(0.0, 0.5), (8.0, 0.5)]),
            20: _route(20, [(0.0, 2.5), (10.0, 2.5)]),
            21: _route(21, [(0.0, 3.0), (10.0, 3.0)]),
        }

    def test_same_geometry_and_reasons_as_legacy(self):
        group = _pairs_group([(10, 11), (20, 21)])
        config = SerpentineConfig(amplitude=0.5, gap_factor=2.0)
        legacy = _tune(group, self._routes(), pair_fallbacks=False, config=config)
        new = _tune(group, self._routes(), config=config)
        assert legacy.keys() == new.keys()
        for net in legacy:
            (r_old, res_old), (r_new, res_new) = legacy[net], new[net]
            assert (
                res_old.reason == res_new.reason == ("tuned" if net in (10, 11) else res_old.reason)
            )
            assert [
                (round(s.x1, 9), round(s.y1, 9), round(s.x2, 9), round(s.y2, 9))
                for s in r_old.segments
            ] == [
                (round(s.x1, 9), round(s.y1, 9), round(s.x2, 9), round(s.y2, 9))
                for s in r_new.segments
            ]


class TestPairSkewInvariant:
    """(e) a pair inside its own skew limit stays inside it."""

    def test_coupled_pair_inside_limit_stays_inside(self):
        routes = {**_lattice_pair(), **_reference_pair(6.0)}
        # 0.03 mm of intra-pair skew -- inside a 0.05 mm limit, as #6664 leaves
        # it.  The outer (P) rail is the longer one; extend N to close most of
        # that.
        outer_excess = _measure(routes[1]) - _measure(routes[2])
        routes[2] = _route(2, [(-(outer_excess - 0.03), 0.3266), *_N_LATTICE[1:]])
        limit = 0.05
        skew0 = abs(_measure(routes[1]) - _measure(routes[2]))
        assert skew0 <= limit

        results = _tune(_pairs_group([(1, 2), (3, 4)]), routes, pair_skew_limits_mm={(1, 2): limit})
        assert results[1][1].reason == "tuned"
        skew1 = abs(_measure(results[1][0]) - _measure(results[2][0]))
        assert skew1 <= limit
        assert skew1 <= skew0 + 1e-3

    def test_uncoupled_pair_inside_limit_stays_inside(self):
        routes = {
            1: _route(1, [(0.0, 0.0), (12.03, 0.0)]),
            2: _route(2, [(0.0, 3.0), (12.0, 3.0)]),
            **_reference_pair(6.0),
        }
        limit = 0.05
        results = _tune(_pairs_group([(1, 2), (3, 4)]), routes, pair_skew_limits_mm={(1, 2): limit})
        assert results[1][1].reason == "tuned"
        assert abs(_measure(results[1][0]) - _measure(results[2][0])) <= limit


# ---------------------------------------------------------------------------
# Router-level: rollback guard and blind/buried consistency
# ---------------------------------------------------------------------------


def _autorouter(routes: list[Route], names: dict[int, str], tolerance: float) -> Autorouter:
    ar = Autorouter(width=80.0, height=80.0)
    ar.net_names = dict(names)
    ar.routes = list(routes)
    nc = NetClassRouting(
        name="BUS",
        length_critical=False,
        length_match_group="BUS",
        length_match_tolerance_mm=tolerance,
    )
    ar.net_class_map = dict.fromkeys(names.values(), nc)
    return ar


class TestRollbackGuard:
    def test_group_still_over_limit_is_restored(self, capsys):
        # Two singles that CAN be tuned, plus a pair whose zig-zag legs offer
        # no host at all: the group cannot clear, so nothing is committed.
        def zigzag(net: int, y: float) -> Route:
            pts = [(0.7 * k, y + (0.7 if k % 2 else 0.0)) for k in range(12)]
            return _route(net, pts)

        routes = [
            _route(1, [(0.0, 0.0), (22.0, 0.0)]),
            _route(2, [(0.0, 3.0), (18.0, 3.0)]),
            zigzag(5, 10.0),
            zigzag(6, 12.0),
        ]
        names = {1: "A0", 2: "A1", 5: "D_P", 6: "D_N"}
        ar = _autorouter(routes, names, tolerance=0.1)
        group = MatchGroup(
            name="BUS",
            net_ids=[1, 2],
            pair_ids=[(5, 6)],
            tolerance=0.1,
            source=MatchGroupSource.LEGACY_API,
        )
        before = list(ar.routes)
        before_segments = [r.segments for r in ar.routes]

        results = ar.apply_match_group_tuning(
            detected_groups=[group], verbose=True, engage="checker_limit"
        )

        assert all(a is b for a, b in zip(ar.routes, before, strict=True))
        assert [r.segments for r in ar.routes] == before_segments
        assert results["BUS"][2][1].reason == "rolled_back_still_over_limit"
        assert results["BUS"][2][0] is before[1]
        assert "STILL OVER LIMIT" in capsys.readouterr().out

    def test_group_that_clears_is_not_rolled_back(self):
        routes = [
            _route(1, [(0.0, 0.0), (22.0, 0.0)]),
            _route(2, [(0.0, 10.0), (20.0, 10.0)]),
            _route(3, [(0.0, 20.0), (18.0, 20.0)]),
        ]
        ar = _autorouter(routes, {1: "A0", 2: "A1", 3: "A2"}, tolerance=0.1)
        group = MatchGroup(
            name="BUS", net_ids=[1, 2, 3], tolerance=0.1, source=MatchGroupSource.LEGACY_API
        )
        before = list(ar.routes)

        results = ar.apply_match_group_tuning(
            detected_groups=[group], verbose=False, engage="checker_limit"
        )

        reasons = {res.reason for (_r, res) in results["BUS"].values()}
        assert "rolled_back_still_over_limit" not in reasons
        assert "tuned" in reasons
        assert any(a is not b for a, b in zip(ar.routes, before, strict=True))
        spread = Autorouter._checker_group_spread_mm(group, {r.net: r for r in ar.routes}, None, 2)
        assert spread is not None and spread <= 0.1


class TestBlindBuriedConsistency:
    """The engagement gate, rollback guard and tuner share one via model.

    Every via the router models today measures the same under both models,
    so the via measure is spied: it charges net A0 an extra 1 mm only when it
    is asked for the through-via model (``blind_buried_supported=False``).
    """

    @staticmethod
    def _spied_measure():
        real = MatchGroupTracker._measure_route_total

        def measure(
            route,
            board_thickness_mm,
            num_copper_layers,
            blind_buried_supported=True,
            length_model=None,
        ):
            length = real(
                route,
                board_thickness_mm,
                num_copper_layers,
                blind_buried_supported=blind_buried_supported,
                length_model=length_model,
            )
            if route.net == 1 and not blind_buried_supported:
                length += 1.0
            return length

        return staticmethod(measure)

    def _router(self, allow_blind: bool) -> tuple[Autorouter, MatchGroup]:
        routes = [_route(1, [(0.0, 0.0), (20.0, 0.0)]), _route(2, [(0.0, 5.0), (20.05, 5.0)])]
        ar = _autorouter(routes, {1: "A0", 2: "A1"}, tolerance=0.1)
        ar.layer_stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        ar.via_rules = SimpleNamespace(allow_blind=allow_blind, allow_buried=False)
        group = MatchGroup(
            name="BUS", net_ids=[1, 2], tolerance=0.1, source=MatchGroupSource.LEGACY_API
        )
        return ar, group

    def test_gate_and_tuner_agree_when_blind_vias_are_allowed(self):
        from kicad_tools.router import match_group_tuning as mgt

        ar, group = self._router(allow_blind=True)
        routes = {r.net: r for r in ar.routes}
        with patch.object(MatchGroupTracker, "_measure_route_total", self._spied_measure()):
            # Precondition: the two via models disagree about this group.
            blind = Autorouter._checker_group_spread_mm(
                group, routes, 1.6, 4, blind_buried_supported=True
            )
            through = Autorouter._checker_group_spread_mm(group, routes, 1.6, 4)
            assert blind is not None and through is not None
            assert blind <= 0.1 < through

            with patch.object(mgt, "tune_match_group_v2") as tuner:
                results = ar.apply_match_group_tuning(
                    detected_groups=[group], verbose=False, engage="checker_limit"
                )

        # The gate measured with the resolved (blind) model -- the one the
        # tuner would use -- found the group inside its limit, and left it.
        tuner.assert_not_called()
        assert results == {"BUS": {}}

    def test_through_via_model_gate_and_tuner_agree(self):
        from kicad_tools.router import match_group_tuning as mgt

        ar, group = self._router(allow_blind=False)
        seen: dict[str, object] = {}
        real = mgt.tune_match_group_v2

        def spy(*args, **kwargs):
            seen.update(kwargs)
            return real(*args, **kwargs)

        with (
            patch.object(MatchGroupTracker, "_measure_route_total", self._spied_measure()),
            patch.object(mgt, "tune_match_group_v2", spy),
        ):
            results = ar.apply_match_group_tuning(
                detected_groups=[group], verbose=False, engage="checker_limit"
            )
            spread_after = Autorouter._checker_group_spread_mm(
                group, {r.net: r for r in ar.routes}, seen["board_thickness_mm"], 4
            )

        assert seen["blind_buried_supported"] is False
        # Tuned against the same 1 mm the gate saw, and the guard kept it.
        assert {res.reason for (_r, res) in results["BUS"].values()} >= {"tuned"}
        assert spread_after is not None and spread_after <= 0.1
