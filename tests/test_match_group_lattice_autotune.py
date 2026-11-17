"""Lattice auto-engaged match-group length tuning (Issue #6674).

Without ``--length-match-groups`` the lattice flow now tunes every declared
match group whose spread exceeds the limit ``kct check`` enforces, to that
same limit.  Grid is not auto-engaged, and the explicit flag keeps its
legacy ``length_critical`` gating on every engine.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

from kicad_tools.cli.route_cmd import _match_group_tuning_engage
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.match_group_length import MatchGroup, MatchGroupSource
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import NetClassRouting
from kicad_tools.validate.match_group_skew import resolve_group_tolerance_mm


def _straight_route(net_id: int, name: str, length_mm: float, y: float) -> Route:
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


def _router(lengths: list[float], *, tolerance_mm: float = 0.1) -> tuple[Autorouter, MatchGroup]:
    """A 4-net single-ended group whose class is NOT ``length_critical``."""
    names = {i + 1: f"A{i}" for i in range(len(lengths))}
    ar = Autorouter(width=80.0, height=80.0)
    ar.net_names = dict(names)
    ar.routes = [
        _straight_route(net_id, name, lengths[net_id - 1], y=10.0 * (net_id - 1))
        for net_id, name in names.items()
    ]
    nc = NetClassRouting(
        name="ADDR",
        length_critical=False,
        length_match_group="ADDR_BUS",
        length_match_tolerance_mm=tolerance_mm,
    )
    ar.net_class_map = dict.fromkeys(names.values(), nc)
    group = MatchGroup(
        name="ADDR_BUS",
        net_ids=sorted(names),
        tolerance=tolerance_mm,
        source=MatchGroupSource.LEGACY_API,
    )
    return ar, group


def _args(**kw) -> argparse.Namespace:
    base = {"route_engine": "lattice", "length_match_groups": False}
    base.update(kw)
    return argparse.Namespace(**base)


class TestEngageSelection:
    def test_lattice_without_flag_auto_engages_on_checker_limit(self):
        ar, _ = _router([22.0, 20.0, 18.0, 18.0])
        assert _match_group_tuning_engage(_args(), ar) == "checker_limit"

    def test_lattice_without_declarations_does_not_engage(self):
        ar, _ = _router([22.0, 20.0, 18.0, 18.0])
        ar.net_class_map = {n: NetClassRouting(name="DEFAULT") for n in ar.net_class_map}
        assert _match_group_tuning_engage(_args(), ar) is None

    def test_grid_does_not_auto_engage(self):
        """(c) grid is frozen for pair-adjacent features until #6448."""
        ar, _ = _router([22.0, 20.0, 18.0, 18.0])
        assert _match_group_tuning_engage(_args(route_engine="grid"), ar) is None

    def test_explicit_flag_keeps_legacy_policy_on_grid(self):
        """(d) ``--length-match-groups`` still works on grid."""
        ar, _ = _router([22.0, 20.0, 18.0, 18.0])
        args = _args(route_engine="grid", length_match_groups=True)
        assert _match_group_tuning_engage(args, ar) == "length_critical"

    def test_explicit_flag_keeps_legacy_policy_on_lattice(self):
        ar, _ = _router([22.0, 20.0, 18.0, 18.0])
        assert _match_group_tuning_engage(_args(length_match_groups=True), ar) == (
            "length_critical"
        )


class TestCheckerLimitEngagement:
    def test_out_of_tolerance_group_is_tuned_without_length_critical(self):
        """(a) a non-length-critical class is tuned, to the checker's limit."""
        from kicad_tools.router import match_group_tuning as mgt

        ar, group = _router([22.0, 20.0, 18.0, 18.0])
        seen: dict[str, object] = {}
        real = mgt.tune_match_group_v2

        def spy(*args, **kwargs):
            seen.update(kwargs)
            return real(*args, **kwargs)

        with patch.object(mgt, "tune_match_group_v2", spy):
            results = ar.apply_match_group_tuning(
                detected_groups=[group], verbose=False, engage="checker_limit"
            )

        assert seen["length_critical"] is True
        assert seen["tolerance_mm"] == resolve_group_tolerance_mm(
            group, ar.net_names, ar.net_class_map
        )
        reasons = {res.reason for (_r, res) in results["ADDR_BUS"].values()}
        assert "tuned" in reasons
        assert "not_length_critical" not in reasons

    def test_legacy_engage_still_declines_non_length_critical(self):
        ar, group = _router([22.0, 20.0, 18.0, 18.0])
        results = ar.apply_match_group_tuning(detected_groups=[group], verbose=False)
        reasons = {res.reason for (_r, res) in results["ADDR_BUS"].values()}
        assert reasons == {"not_length_critical"}

    def test_within_tolerance_group_is_byte_identical(self, capsys):
        """(b) a group inside the checker's limit is never handed to the tuner."""
        from kicad_tools.router import match_group_tuning as mgt

        ar, group = _router([20.0, 20.05, 20.0, 20.02], tolerance_mm=0.1)
        before = list(ar.routes)
        before_segments = [r.segments for r in ar.routes]

        with patch.object(mgt, "tune_match_group_v2") as tuner:
            results = ar.apply_match_group_tuning(
                detected_groups=[group], verbose=True, engage="checker_limit"
            )

        tuner.assert_not_called()
        assert results == {"ADDR_BUS": {}}
        assert ar.routes == before
        assert all(a is b for a, b in zip(ar.routes, before, strict=True))
        assert [r.segments for r in ar.routes] == before_segments
        assert "within limit" in capsys.readouterr().out

    def test_unrouted_member_is_reported_not_tuned(self, capsys):
        ar, group = _router([22.0, 20.0, 18.0, 18.0])
        ar.routes = ar.routes[:-1]
        results = ar.apply_match_group_tuning(
            detected_groups=[group], verbose=True, engage="checker_limit"
        )
        assert results == {"ADDR_BUS": {}}
        assert "unrouted" in capsys.readouterr().out

    def test_spread_counts_pairs_as_average_like_the_checker(self):
        """A pair lane contributes (L_P + L_N) / 2, as derive_group_skew_data does."""
        routes = {
            1: _straight_route(1, "CLK_P", 20.0, 0.0),
            2: _straight_route(2, "CLK_N", 22.0, 1.0),
            3: _straight_route(3, "D0_P", 21.0, 10.0),
            4: _straight_route(4, "D0_N", 21.0, 11.0),
        }
        group = MatchGroup(
            name="LANES",
            net_ids=[],
            pair_ids=[(1, 2), (3, 4)],
            tolerance=0.1,
            source=MatchGroupSource.LEGACY_API,
        )
        spread = Autorouter._checker_group_spread_mm(group, routes, 1.6, 4)
        assert spread == 0.0


class TestToleranceResolverShared:
    def test_pair_only_group_anchors_on_first_p_leg(self):
        nc = NetClassRouting(name="MIPI", length_match_tolerance_mm=0.05)
        group = MatchGroup(
            name="MIPI_CSI_LANES",
            net_ids=[],
            pair_ids=[(5, 6)],
            tolerance=0.05,
            source=MatchGroupSource.LEGACY_API,
        )
        assert resolve_group_tolerance_mm(group, {5: "CLK_P", 6: "CLK_N"}, {"CLK_P": nc}) == 0.05

    def test_unknown_class_falls_back_to_checker_default(self):
        from kicad_tools.validate.rules.match_group_length_skew import (
            DEFAULT_MATCH_GROUP_TOLERANCE_MM,
        )

        group = MatchGroup(
            name="G", net_ids=[1, 2], tolerance=0.1, source=MatchGroupSource.LEGACY_API
        )
        assert resolve_group_tolerance_mm(group, {1: "X"}, {}) == DEFAULT_MATCH_GROUP_TOLERANCE_MM
