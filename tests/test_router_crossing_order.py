"""Unit tests for flight-line crossing-aware net ordering (Issue #5787 step 2).

Three layers, mirroring ``tests/test_cli_order_method.py``:

1. Pure combinatorics in ``kicad_tools.router.crossing_order`` -- segment
   predicate, MST skeleton, conflict profiles, ordering -- against
   hand-constructed point sets with a known answer.
2. The ``--order-method crossing`` plumbing (outer parser, inner parser, and
   ``_apply_order_method`` dispatching to the direct path rather than
   ``RoutingOptimizer``).
3. The structural claim the step-2 experiment rests on: the pre-existing
   ``--monotone-certificate-order`` / ``--bundle-river-planner`` flags cannot
   change net order without a >=5-member byte-lane match group, so they are
   identity on boards like 02 and 03.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kicad_tools.router.crossing_order import (
    CrossingProfile,
    crossing_aware_order,
    crossing_profiles,
    minimum_spanning_skeleton,
    segments_cross,
)

# ---------------------------------------------------------------------------
# segments_cross
# ---------------------------------------------------------------------------


def test_segments_cross_detects_an_x():
    assert segments_cross(((0.0, 0.0), (10.0, 10.0)), ((0.0, 10.0), (10.0, 0.0)))


def test_segments_cross_rejects_parallel_segments():
    """Parallel contention is not a forced crossing."""
    assert not segments_cross(((0.0, 0.0), (10.0, 0.0)), ((0.0, 1.0), (10.0, 1.0)))


def test_segments_cross_rejects_collinear_overlap():
    """Collinear overlap would otherwise double-count a bus."""
    assert not segments_cross(((0.0, 0.0), (10.0, 0.0)), ((5.0, 0.0), (15.0, 0.0)))


def test_segments_cross_rejects_shared_endpoint():
    assert not segments_cross(((0.0, 0.0), (10.0, 0.0)), ((0.0, 0.0), (0.0, 10.0)))


def test_segments_cross_rejects_t_touch():
    """An endpoint landing *on* the other segment is a touch, not a crossing."""
    assert not segments_cross(((0.0, 0.0), (10.0, 0.0)), ((5.0, 0.0), (5.0, 10.0)))


def test_segments_cross_rejects_t_touch_in_both_argument_orders():
    """The T-touch rejection must not depend on the argument order.

    The zero determinant lands in a different pair depending on which segment
    is passed first, so guarding only ``d1``/``d2`` made the predicate answer
    ``False`` one way and ``True`` the other for identical geometry.  Since
    :func:`crossing_profiles` always passes the lower net id's segment first,
    that leaked net ids into the crossing degree.  Grid-aligned pad centres
    make a flight line through another net's pad centre common, not
    measure-zero -- ``test_segments_cross_is_symmetric`` below uses a proper
    X-crossing, where both directions agree even with the bug present.
    """
    h = ((0.0, 0.0), (10.0, 0.0))
    v = ((5.0, 0.0), (5.0, 10.0))  # v's endpoint lies on h's interior
    assert not segments_cross(h, v)
    assert not segments_cross(v, h)


def test_crossing_profiles_degree_is_independent_of_net_ids_at_a_t_touch():
    """The same two skeletons must score the same degree either way round."""
    h = ((0.0, 0.0), (10.0, 0.0))
    v = ((5.0, 0.0), (5.0, 10.0))
    h_first = {net: p.crossings for net, p in crossing_profiles({1: [h], 2: [v]}).items()}
    v_first = {net: p.crossings for net, p in crossing_profiles({1: [v], 2: [h]}).items()}
    assert h_first == {1: 0, 2: 0}
    assert v_first == {1: 0, 2: 0}


def test_segments_cross_rejects_disjoint_segments():
    assert not segments_cross(((0.0, 0.0), (1.0, 1.0)), ((20.0, 20.0), (21.0, 21.0)))


def test_segments_cross_is_symmetric():
    a = ((0.0, 0.0), (10.0, 10.0))
    b = ((0.0, 10.0), (10.0, 0.0))
    assert segments_cross(a, b) == segments_cross(b, a)


# ---------------------------------------------------------------------------
# minimum_spanning_skeleton
# ---------------------------------------------------------------------------


def test_skeleton_of_two_pads_is_one_segment():
    assert minimum_spanning_skeleton([(0.0, 0.0), (3.0, 4.0)]) == [((0.0, 0.0), (3.0, 4.0))]


def test_skeleton_of_fewer_than_two_points_is_empty():
    assert minimum_spanning_skeleton([]) == []
    assert minimum_spanning_skeleton([(1.0, 1.0)]) == []


def test_skeleton_prefers_nearest_neighbour_chain_over_a_star():
    """Three collinear pads must chain 0-1-2, not fan out from pad 0.

    A naive "connect everything to the first pad" skeleton would emit the long
    0->2 edge and inflate every crossing count along it.
    """
    segs = minimum_spanning_skeleton([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)])
    assert segs == [((0.0, 0.0), (1.0, 0.0)), ((1.0, 0.0), (2.0, 0.0))]


def test_skeleton_has_n_minus_one_edges():
    points = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (5.0, 5.0)]
    assert len(minimum_spanning_skeleton(points)) == len(points) - 1


def test_skeleton_drops_zero_length_edges_for_duplicate_pads():
    """Stacked pads must not contribute a degenerate segment."""
    segs = minimum_spanning_skeleton([(0.0, 0.0), (0.0, 0.0), (5.0, 0.0)])
    assert segs == [((0.0, 0.0), (5.0, 0.0))]


# ---------------------------------------------------------------------------
# crossing_profiles
# ---------------------------------------------------------------------------


def _x_pair() -> dict[int, list[tuple[tuple[float, float], tuple[float, float]]]]:
    return {
        1: [((0.0, 0.0), (10.0, 10.0))],
        2: [((0.0, 10.0), (10.0, 0.0))],
    }


def test_profiles_count_a_single_crossing_on_both_nets():
    profiles = crossing_profiles(_x_pair())
    assert profiles[1] == CrossingProfile(net_id=1, degree=1, crossings=1)
    assert profiles[2] == CrossingProfile(net_id=2, degree=1, crossings=1)


def test_profiles_report_zero_for_an_isolated_net():
    skeletons = _x_pair()
    skeletons[3] = [((50.0, 50.0), (60.0, 60.0))]
    profiles = crossing_profiles(skeletons)
    assert profiles[3].degree == 0
    assert profiles[3].crossings == 0


def test_profiles_include_empty_skeleton_nets_at_degree_zero():
    """Single-pad nets must still appear, so the caller can order the full set."""
    skeletons = _x_pair()
    skeletons[9] = []
    profiles = crossing_profiles(skeletons)
    assert profiles[9].degree == 0


def test_degree_counts_distinct_neighbours_not_total_hits():
    """One neighbour crossed twice is degree 1, crossings 2."""
    skeletons = {
        # A zig-zag that the straight vertical line cuts twice.
        1: [((0.0, 0.0), (10.0, 4.0)), ((10.0, 4.0), (0.0, 8.0))],
        2: [((5.0, -5.0), (5.0, 20.0))],
    }
    profiles = crossing_profiles(skeletons)
    assert profiles[1].degree == 1
    assert profiles[1].crossings == 2
    assert profiles[2].degree == 1
    assert profiles[2].crossings == 2


# ---------------------------------------------------------------------------
# crossing_aware_order
# ---------------------------------------------------------------------------


def test_order_is_always_a_permutation():
    skeletons = _x_pair()
    skeletons[3] = [((50.0, 50.0), (60.0, 60.0))]
    priority = dict.fromkeys(skeletons, (10,))
    bands = dict.fromkeys(skeletons, 10)
    order = crossing_aware_order(skeletons, priority, bands)
    assert sorted(order) == sorted(skeletons)


def test_most_contended_net_routes_first():
    """The hub net, crossed by both others, must lead its band."""
    skeletons = {
        1: [((0.0, 5.0), (20.0, 5.0))],  # horizontal hub, crossed twice
        2: [((5.0, 0.0), (5.0, 10.0))],
        3: [((15.0, 0.0), (15.0, 10.0))],
    }
    priority = dict.fromkeys(skeletons, (10,))
    bands = dict.fromkeys(skeletons, 10)
    assert crossing_aware_order(skeletons, priority, bands)[0] == 1


def test_uncrossed_nets_sort_last_within_a_band():
    skeletons = _x_pair()
    skeletons[3] = [((50.0, 50.0), (60.0, 60.0))]
    priority = dict.fromkeys(skeletons, (10,))
    bands = dict.fromkeys(skeletons, 10)
    assert crossing_aware_order(skeletons, priority, bands)[-1] == 3


def test_class_bands_are_never_interleaved():
    """A heavily-crossed default net must not jump ahead of a clock net."""
    skeletons = {
        1: [((0.0, 5.0), (20.0, 5.0))],  # default class, crossed twice
        2: [((5.0, 0.0), (5.0, 10.0))],
        3: [((15.0, 0.0), (15.0, 10.0))],
        4: [((100.0, 0.0), (101.0, 1.0))],  # clock class, crossed by nobody
    }
    priority = {1: (10,), 2: (10,), 3: (10,), 4: (2,)}
    bands = {1: 10, 2: 10, 3: 10, 4: 2}
    order = crossing_aware_order(skeletons, priority, bands)
    assert order[0] == 4
    assert order.index(4) < order.index(1)


def test_falls_back_to_priority_key_when_nothing_crosses():
    """With no crossings the result is the caller's existing order, unchanged."""
    skeletons = {
        1: [((0.0, 0.0), (1.0, 0.0))],
        2: [((0.0, 10.0), (1.0, 10.0))],
        3: [((0.0, 20.0), (1.0, 20.0))],
    }
    priority = {1: (10, 0, 0.0, 2, 9.0), 2: (10, 0, 0.0, 2, 1.0), 3: (10, 0, 0.0, 2, 5.0)}
    bands = dict.fromkeys(skeletons, 10)
    assert crossing_aware_order(skeletons, priority, bands) == [2, 3, 1]


def test_order_is_deterministic_across_calls():
    skeletons = {
        1: [((0.0, 5.0), (20.0, 5.0))],
        2: [((5.0, 0.0), (5.0, 10.0))],
        3: [((15.0, 0.0), (15.0, 10.0))],
    }
    priority = dict.fromkeys(skeletons, (10,))
    bands = dict.fromkeys(skeletons, 10)
    first = crossing_aware_order(skeletons, priority, bands)
    assert all(crossing_aware_order(skeletons, priority, bands) == first for _ in range(3))


# ---------------------------------------------------------------------------
# --order-method crossing plumbing
# ---------------------------------------------------------------------------


def _flags_choices(parser: argparse.ArgumentParser, flag: str) -> list[str]:
    for action in parser._actions:
        if flag in action.option_strings:
            return list(action.choices or [])
    raise AssertionError(f"{flag} not declared on {parser.prog!r}")


def test_outer_parser_accepts_crossing():
    from kicad_tools.cli.parser import create_parser

    parser = create_parser()
    args = parser.parse_args(["route", "in.kicad_pcb", "--order-method", "crossing"])
    assert args.order_method == "crossing"


def test_inner_route_parser_accepts_crossing():
    from kicad_tools.cli.route_cmd import main as route_main

    captured: dict[str, argparse.ArgumentParser] = {}
    real_parse_args = argparse.ArgumentParser.parse_args

    def fake_parse_args(self, *args, **kwargs):
        if getattr(self, "prog", "") == "kicad-tools route":
            captured["parser"] = self
            raise SystemExit(0)
        return real_parse_args(self, *args, **kwargs)

    with patch.object(argparse.ArgumentParser, "parse_args", fake_parse_args):
        with pytest.raises(SystemExit):
            route_main([])

    assert "crossing" in _flags_choices(captured["parser"], "--order-method")


def test_routing_shim_forwards_crossing():
    from kicad_tools.cli.commands import routing

    args = SimpleNamespace(order_method="crossing")
    assert getattr(args, "order_method", None) == "crossing"
    # The shim builds sub_argv generically from str(args.order_method); assert the
    # choice list it forwards into actually accepts the value (the drift guard).
    assert "crossing" in _flags_choices(_inner_parser(), "--order-method")
    assert hasattr(routing, "run_route_command")


def _inner_parser() -> argparse.ArgumentParser:
    from kicad_tools.cli.route_cmd import main as route_main

    captured: dict[str, argparse.ArgumentParser] = {}
    real_parse_args = argparse.ArgumentParser.parse_args

    def fake_parse_args(self, *args, **kwargs):
        if getattr(self, "prog", "") == "kicad-tools route":
            captured["parser"] = self
            raise SystemExit(0)
        return real_parse_args(self, *args, **kwargs)

    with patch.object(argparse.ArgumentParser, "parse_args", fake_parse_args):
        with pytest.raises(SystemExit):
            route_main([])
    return captured["parser"]


class _FakePad:
    def __init__(self, x: float, y: float) -> None:
        self.x = x
        self.y = y


class _FakeRouter:
    """Minimal stand-in exposing only what ``_crossing_aware_net_order`` reads."""

    def __init__(self) -> None:
        self.pads = {
            "A": _FakePad(0.0, 5.0),
            "B": _FakePad(20.0, 5.0),
            "C": _FakePad(5.0, 0.0),
            "D": _FakePad(5.0, 10.0),
            "E": _FakePad(100.0, 0.0),
            "F": _FakePad(101.0, 1.0),
        }
        self.nets = {1: ["A", "B"], 2: ["C", "D"], 3: ["E", "F"]}
        self._forced_net_order: list[int] | None = None

    def _get_net_priority(self, net_id: int):
        return (10, 0, 0.0, 2, float(net_id), 0.0)


def test_crossing_aware_net_order_reads_pad_geometry():
    from kicad_tools.cli.route_cmd import _crossing_aware_net_order

    order = _crossing_aware_net_order(_FakeRouter())
    assert sorted(order) == [1, 2, 3]
    # Nets 1 and 2 cross each other; net 3 is isolated and must sort last.
    assert order[-1] == 3


def test_apply_order_method_crossing_skips_the_optimizer():
    """The direct path must not spend an evaluation route (#5787)."""
    from kicad_tools.cli import route_cmd

    router = _FakeRouter()
    args = SimpleNamespace(order_method="crossing")
    with patch("kicad_tools.optim.routing.RoutingOptimizer") as optimizer:
        route_cmd._apply_order_method(router, args, quiet=True)
    optimizer.assert_not_called()
    assert router._forced_net_order is not None
    assert sorted(router._forced_net_order) == [1, 2, 3]


def test_apply_order_method_is_still_a_no_op_when_unset():
    from kicad_tools.cli import route_cmd

    router = _FakeRouter()
    route_cmd._apply_order_method(router, SimpleNamespace(order_method=None), quiet=True)
    assert router._forced_net_order is None


# ---------------------------------------------------------------------------
# Layer 4 -- the escalation paths must actually APPLY the order
#
# ``_apply_order_method`` is reachable from exactly one place in
# ``_run_main_impl``: the single-attempt tail.  ``kct route`` defaults to
# ``--auto-layers``, so ``_run_main_impl`` returns into
# ``route_with_layer_escalation`` long before that tail -- which made every
# ``--order-method`` value a silent no-op on the default recipe.  The step-2
# A/B measured byte-identical copper in both arms before this was found, so
# these tests pin the wiring rather than the flag's existence.
# ---------------------------------------------------------------------------


def test_inner_parser_defaults_auto_layers_on():
    """The default recipe takes the escalation path, not the single-attempt tail."""
    parser = _inner_parser()
    args = parser.parse_args(["dummy.kicad_pcb"])
    assert args.auto_layers is True


@pytest.mark.parametrize(
    "func_name",
    [
        "route_with_layer_escalation",
        "route_with_rule_relaxation",
        "route_with_combined_escalation",
    ],
)
def test_escalation_paths_apply_the_crossing_order(func_name):
    """Each escalation entry point must call the ordering hook per attempt."""
    import inspect

    from kicad_tools.cli import route_cmd

    source = inspect.getsource(getattr(route_cmd, func_name))
    assert "_apply_crossing_order_escalation(router, args" in source, (
        f"{func_name} does not apply --order-method; the flag would be a silent "
        "no-op on this path (Issue #5787)"
    )


def test_crossing_order_escalation_installs_the_order():
    from kicad_tools.cli import route_cmd

    router = _FakeRouter()
    applied = route_cmd._apply_crossing_order_escalation(
        router, SimpleNamespace(order_method="crossing"), quiet=True
    )
    assert applied is True
    assert sorted(router._forced_net_order or []) == [1, 2, 3]


def test_crossing_order_escalation_is_noop_when_flag_absent():
    """Flag-absent path: no order installed inside the attempt loop."""
    from kicad_tools.cli import route_cmd

    router = _FakeRouter()
    applied = route_cmd._apply_crossing_order_escalation(
        router, SimpleNamespace(order_method=None), quiet=True
    )
    assert applied is False
    assert router._forced_net_order is None


@pytest.mark.parametrize("method", ["greedy", "critical_first", "congestion", "hybrid"])
@pytest.mark.parametrize(
    "dispatch_flags",
    [
        {"auto_layers": True},
        {"auto_layers": False, "adaptive_rules": True},
        {"auto_layers": True, "adaptive_rules": True},
        {"auto_pcb_size": True},
        {"auto_mfr_tier": True},
    ],
    ids=["layers", "rules", "combined", "size", "mfr_tier"],
)
def test_escalation_paths_reject_the_evaluation_route_methods(method, dispatch_flags, capsys):
    """Issue #5908 (replaces the #5787 "leaves other methods untouched" pin).

    The four ``RoutingOptimizer``-backed methods need a fresh-router factory
    the escalation attempt loops do not build, so they used to be silently
    discarded there.  They are now rejected with exit code 2 before routing
    instead of being ignored.  End-to-end dispatch coverage (zero routing /
    evaluation calls) lives in ``tests/test_cli_order_method.py``.
    """
    from kicad_tools.cli import route_cmd

    args = SimpleNamespace(
        order_method=method,
        **{
            "auto_layers": False,
            "adaptive_rules": False,
            "auto_pcb_size": False,
            "auto_mfr_tier": False,
            **dispatch_flags,
        },
    )
    assert route_cmd._validate_order_method_for_dispatch(args) == 2
    assert f"--order-method {method} is not supported" in capsys.readouterr().err


@pytest.mark.parametrize("method", [None, "crossing"])
def test_escalation_paths_accept_absent_and_crossing(method):
    from kicad_tools.cli import route_cmd

    args = SimpleNamespace(
        order_method=method,
        auto_layers=True,
        adaptive_rules=True,
        auto_pcb_size=True,
        auto_mfr_tier=True,
    )
    assert route_cmd._validate_order_method_for_dispatch(args) == 0


# ---------------------------------------------------------------------------
# The structural premise of the step-2 experiment
# ---------------------------------------------------------------------------


def test_byte_lane_priority_is_identity_without_a_qualifying_group():
    """``--monotone-certificate-order`` / ``--bundle-river-planner`` cannot reorder.

    Both flags act only through ``_apply_byte_lane_inner_priority``, which
    requires a detected match group of at least 5 members projecting onto one
    co-located row.  Boards 02 and 03 have no such group, so the step-2
    experiment measures them as structural no-ops rather than as orderings.
    """
    from kicad_tools.router.core import Autorouter

    router = Autorouter.__new__(Autorouter)
    router.net_class_map = {}
    router.net_names = {1: "N1", 2: "N2", 3: "N3", 4: "N4", 5: "N5"}
    router.nets = {1: [], 2: [], 3: [], 4: [], 5: []}
    router.pads = {}
    router.enable_byte_lane_reorder = True
    router.enable_bundle_river_planner = True
    router.enable_monotone_certificate_order = True
    router._last_monotone_certificates = {}

    net_order = [1, 2, 3, 4, 5]
    assert router._apply_byte_lane_inner_priority(list(net_order)) == net_order
