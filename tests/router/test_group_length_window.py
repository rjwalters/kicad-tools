"""Length-window group routing for similar-length nets without a naming convention.

Issue #9621 (row 6 residual of the #9601 router-failure-mode audit): nets of
similar MST length that do NOT share a ``DATA[0..5]``-style name get no group
treatment, and within a group the members route in id order rather than
escape-feasibility order.  ``--group-length-window <pct>`` clusters such nets
into synthetic ``BusGroup``s (reusing the bus machinery -- no new engine) and
orders each cluster's members fewest-open-pad-directions first.

Coverage here:

* :func:`~kicad_tools.router.bus.group_by_length_window` -- clustering edge
  cases (window boundaries, single-net clusters dropped, exclusion, naming).
* :func:`~kicad_tools.router.bus.order_group_by_escape` -- the pure
  escape-order on a hand-built case.
* :meth:`BusRouter.detect_length_window_groups` / ``_net_escape_openness``
  on a real ``Autorouter`` (power nets, named buses and single-pad nets
  excluded; escape openness differentiated by pad congestion).
* The CLI warnings: ``--group-length-window`` under the escalation ladders
  and under ``--strategy negotiated``, and without ``--bus-routing``.
* A congested-board measurement (native-gated) -- see
  ``TestCongestedBoardMeasurement`` for the reported numbers.
"""

from __future__ import annotations

import argparse
import contextlib
import io

import pytest

from kicad_tools.router.bus import (
    BusGroup,
    BusRoutingConfig,
    BusSignal,
    group_by_length_window,
    order_group_by_escape,
)
from kicad_tools.router.bus_routing import BusRouter
from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import is_cpp_available
from tests.route_cli import route_args

native = pytest.mark.skipif(not is_cpp_available(), reason="native router required")


# ---------------------------------------------------------------------------
# group_by_length_window: pure clustering
# ---------------------------------------------------------------------------


class TestGroupByLengthWindow:
    def test_similar_lengths_form_one_group(self):
        nets = {1: ("RAIL_A", 24.0), 2: ("RAIL_B", 25.0), 3: ("RAIL_C", 24.5)}
        groups = group_by_length_window(nets, 5.0)
        assert len(groups) == 1
        assert groups[0].name == "LEN1"
        assert [s.net_name for s in groups[0].signals] == ["RAIL_A", "RAIL_B", "RAIL_C"]

    def test_disjoint_windows_form_separate_groups(self):
        nets = {
            1: ("SHORT_A", 10.0),
            2: ("SHORT_B", 10.4),
            3: ("LONG_A", 80.0),
            4: ("LONG_B", 82.0),
        }
        groups = group_by_length_window(nets, 5.0)
        assert [g.name for g in groups] == ["LEN1", "LEN2"]
        assert [s.net_name for s in groups[0].signals] == ["SHORT_A", "SHORT_B"]
        assert [s.net_name for s in groups[1].signals] == ["LONG_A", "LONG_B"]

    def test_boundary_member_is_inclusive(self):
        # ref 10.0, window 20% -> a net at exactly 12.0 joins the cluster.
        nets = {1: ("SEED", 10.0), 2: ("EDGE", 12.0)}
        groups = group_by_length_window(nets, 20.0)
        assert len(groups) == 1
        assert [s.net_name for s in groups[0].signals] == ["SEED", "EDGE"]

    def test_just_outside_window_seeds_new_cluster_and_drops_singles(self):
        # ref 10.0, window 20% -> 12.5 > 12.0 seeds a second, single-member
        # cluster, which is dropped (min_width=2).
        nets = {1: ("SEED", 10.0), 2: ("FAR", 12.5)}
        assert group_by_length_window(nets, 20.0) == []

    def test_single_net_clusters_dropped(self):
        nets = {1: ("A", 10.0), 2: ("B", 10.0), 3: ("LONE", 50.0)}
        groups = group_by_length_window(nets, 5.0)
        assert len(groups) == 1
        assert [s.net_name for s in groups[0].signals] == ["A", "B"]

    def test_min_width_larger_than_any_cluster(self):
        nets = {1: ("A", 10.0), 2: ("B", 10.1), 3: ("C", 30.0), 4: ("D", 30.1)}
        assert group_by_length_window(nets, 5.0, min_width=3) == []

    def test_all_nets_in_one_window(self):
        nets = {i: (f"N{i}", 20.0 + 0.01 * i) for i in range(1, 9)}
        groups = group_by_length_window(nets, 50.0)
        assert len(groups) == 1
        assert groups[0].width == 8

    def test_zero_length_nets_group_together(self):
        nets = {1: ("A", 0.0), 2: ("B", 0.0), 3: ("C", 10.0)}
        groups = group_by_length_window(nets, 20.0)
        assert len(groups) == 1
        assert [s.net_name for s in groups[0].signals] == ["A", "B"]

    def test_exclude_removes_nets(self):
        nets = {1: ("A", 10.0), 2: ("B", 10.0), 3: ("C", 10.0)}
        groups = group_by_length_window(nets, 5.0, exclude={2})
        assert [s.net_name for s in groups[0].signals] == ["A", "C"]

    def test_negative_pct_raises(self):
        with pytest.raises(ValueError, match=">= 0"):
            group_by_length_window({1: ("A", 1.0)}, -1.0)

    def test_empty_input(self):
        assert group_by_length_window({}, 20.0) == []

    def test_member_indices_are_net_id_order(self):
        # Pre-escape-order indices are deterministic (ascending net id) so
        # order_group_by_escape has a stable tie-break to work from.
        nets = {7: ("SEVEN", 10.0), 2: ("TWO", 10.0), 5: ("FIVE", 10.0)}
        groups = group_by_length_window(nets, 5.0)
        assert groups[0].get_net_ids() == [2, 5, 7]

    def test_zero_pct_groups_exact_ties_only(self):
        nets = {1: ("A", 10.0), 2: ("B", 10.0), 3: ("C", 10.05)}
        groups = group_by_length_window(nets, 0.0)
        assert len(groups) == 1
        assert [s.net_name for s in groups[0].signals] == ["A", "B"]


# ---------------------------------------------------------------------------
# order_group_by_escape: pure escape ordering (hand-built case)
# ---------------------------------------------------------------------------


def _hand_group() -> BusGroup:
    return BusGroup(
        name="LEN1",
        signals=[
            BusSignal("RAIL_A", 1, "LEN1", 0, "length-window"),
            BusSignal("RAIL_B", 2, "LEN1", 1, "length-window"),
            BusSignal("RAIL_C", 3, "LEN1", 2, "length-window"),
            BusSignal("RAIL_D", 4, "LEN1", 3, "length-window"),
        ],
    )


class TestOrderGroupByEscape:
    def test_fewest_open_directions_first(self):
        # Hand-built case: RAIL_C is boxed in on three sides, RAIL_A on two,
        # RAIL_B and RAIL_D are free.  The tightest escapes claim their
        # corridor first.
        openness = {1: 4, 2: 8, 3: 2, 4: 8}
        ordered = order_group_by_escape(_hand_group(), openness)
        assert ordered.get_net_ids() == [3, 1, 2, 4]
        assert [s.net_name for s in ordered.signals] == ["RAIL_C", "RAIL_A", "RAIL_B", "RAIL_D"]

    def test_indices_are_reranked_from_zero(self):
        ordered = order_group_by_escape(_hand_group(), {1: 4, 2: 8, 3: 2, 4: 8})
        assert [s.index for s in ordered.signals] == [0, 1, 2, 3]

    def test_ties_keep_incoming_index_order(self):
        ordered = order_group_by_escape(_hand_group(), {1: 5, 2: 5, 3: 5, 4: 5})
        assert ordered.get_net_ids() == [1, 2, 3, 4]

    def test_missing_openness_routes_last(self):
        ordered = order_group_by_escape(_hand_group(), {3: 1, 4: 2})
        assert ordered.get_net_ids() == [3, 4, 1, 2]

    def test_input_group_unmodified(self):
        group = _hand_group()
        order_group_by_escape(group, {1: 4, 2: 8, 3: 2, 4: 8})
        assert group.get_net_ids() == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# BusRouter.detect_length_window_groups / _net_escape_openness on a real grid
# ---------------------------------------------------------------------------


def _tight_row_router() -> Autorouter:
    """A 0.8 mm-pitch source row fanning out to a wide target row.

    The DDR-byte-lane shape (#3438/#4051): the middle pads of the tight row
    are flanked on both sides by neighbour halos, so their escape openness is
    lower than the row-edge pads'.
    """
    router = Autorouter(50, 20, force_python=True)
    src, dst = [], []
    for i in range(6):
        y = 7.0 + i * 0.8
        net = i + 1
        name = f"RAIL_{chr(65 + i)}"
        src.append({"number": str(i + 1), "x": 6, "y": y, "width": 0.5, "height": 0.5, "net": net, "net_name": name})
        dst.append(
            {"number": str(i + 1), "x": 44, "y": y, "width": 0.9, "height": 0.9, "net": net, "net_name": name}
        )
    router.add_component("U1", src)
    router.add_component("U2", dst)
    return router


class TestDetectLengthWindowGroups:
    def test_one_group_same_length_no_naming_convention(self):
        router = _tight_row_router()
        config = BusRoutingConfig(enabled=True, length_window=30.0)
        groups = BusRouter(router).detect_length_window_groups(config, exclude=set())
        assert len(groups) == 1
        assert groups[0].name == "LEN1"
        assert groups[0].width == 6
        assert sorted(s.net_name for s in groups[0].signals) == [
            "RAIL_A",
            "RAIL_B",
            "RAIL_C",
            "RAIL_D",
            "RAIL_E",
            "RAIL_F",
        ]

    def test_escape_order_within_group(self):
        router = _tight_row_router()
        bus = BusRouter(router)
        config = BusRoutingConfig(enabled=True, length_window=30.0)
        (group,) = bus.detect_length_window_groups(config, exclude=set())
        openness = {s.net_id: bus._net_escape_openness(s.net_id) for s in group.signals}
        # Members are ascending in openness; the tight middle pads escape
        # before the free row-edge pads.
        values = [openness[n] for n in group.get_net_ids()]
        assert values == sorted(values)
        assert min(values) < max(values)

    def test_power_nets_excluded(self):
        router = _tight_row_router()
        router.add_component(
            "PWR",
            [
                {"number": "1", "x": 6, "y": 16, "width": 1.0, "height": 1.0, "net": 7, "net_name": "+3V3"},
                {"number": "2", "x": 44, "y": 16, "width": 1.0, "height": 1.0, "net": 7, "net_name": "+3V3"},
            ],
        )
        config = BusRoutingConfig(enabled=True, length_window=90.0)
        groups = BusRouter(router).detect_length_window_groups(config, exclude=set())
        names = [s.net_name for g in groups for s in g.signals]
        assert "+3V3" not in names

    def test_name_detected_bus_members_excluded(self):
        router = _tight_row_router()
        router.add_component(
            "U3",
            [
                {"number": "1", "x": 6, "y": 16, "width": 0.9, "height": 0.9, "net": 7, "net_name": "DATA_0"},
                {"number": "2", "x": 44, "y": 16, "width": 0.9, "height": 0.9, "net": 7, "net_name": "DATA_0"},
                {"number": "3", "x": 6, "y": 17, "width": 0.9, "height": 0.9, "net": 8, "net_name": "DATA_1"},
                {"number": "4", "x": 44, "y": 17, "width": 0.9, "height": 0.9, "net": 8, "net_name": "DATA_1"},
            ],
        )
        bus = BusRouter(router)
        named = bus.detect_buses(2)
        named_ids = {n for g in named for n in g.get_net_ids()}
        config = BusRoutingConfig(enabled=True, length_window=90.0)
        groups = bus.detect_length_window_groups(config, exclude=named_ids)
        names = [s.net_name for g in groups for s in g.signals]
        assert "DATA_0" not in names and "DATA_1" not in names
        # The named bus itself is still detected alongside.
        assert [g.name for g in named] == ["DATA"]

    def test_single_pad_nets_skipped(self):
        router = Autorouter(30, 20, force_python=True)
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 5, "y": 5, "width": 1.0, "height": 1.0, "net": 1, "net_name": "SOLO"},
                {"number": "2", "x": 5, "y": 10, "width": 1.0, "height": 1.0, "net": 2, "net_name": "OTHER"},
                {"number": "3", "x": 25, "y": 10, "width": 1.0, "height": 1.0, "net": 2, "net_name": "OTHER"},
            ],
        )
        config = BusRoutingConfig(enabled=True, length_window=90.0)
        groups = BusRouter(router).detect_length_window_groups(config, exclude=set())
        names = [s.net_name for g in groups for s in g.signals]
        assert "SOLO" not in names

    def test_none_window_disables(self):
        router = _tight_row_router()
        config = BusRoutingConfig(enabled=True)
        assert BusRouter(router).detect_length_window_groups(config, exclude=set()) == []

    def test_out_of_window_outlier_not_grouped(self):
        router = _tight_row_router()
        router.add_component(
            "U4",
            [
                {"number": "1", "x": 6, "y": 17.5, "width": 0.9, "height": 0.9, "net": 7, "net_name": "DISTANT"},
                {"number": "2", "x": 48, "y": 17.5, "width": 0.9, "height": 0.9, "net": 7, "net_name": "DISTANT"},
            ],
        )
        config = BusRoutingConfig(enabled=True, length_window=5.0)
        groups = BusRouter(router).detect_length_window_groups(config, exclude=set())
        names = [s.net_name for g in groups for s in g.signals]
        assert "DISTANT" not in names


# ---------------------------------------------------------------------------
# route_all_with_buses integration: synthetic groups route through the bus path
# ---------------------------------------------------------------------------


class TestRouteAllWithBusesLengthWindow:
    def test_log_names_group_and_members(self):
        router = _tight_row_router()
        config = BusRoutingConfig(enabled=True, length_window=30.0)
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            router.route_all_with_buses(config)
        out = sink.getvalue()
        # The detection log names the synthetic group and every member.
        assert "LEN1[5:0]" in out
        for name in ("RAIL_A", "RAIL_B", "RAIL_C", "RAIL_D", "RAIL_E", "RAIL_F"):
            assert name in out

    def test_all_nets_routed_via_group_path(self):
        router = _tight_row_router()
        config = BusRoutingConfig(enabled=True, length_window=30.0)
        with contextlib.redirect_stdout(io.StringIO()):
            router.route_all_with_buses(config)
        routed = {route.net for route in router.routes}
        assert routed == {1, 2, 3, 4, 5, 6}

    def test_no_named_bus_still_routes_with_length_window(self):
        # Pre-#9621 a board with no name-detected bus fell back to plain
        # route_all; the synthetic group must keep the bus path alive.
        router = _tight_row_router()
        assert BusRouter(router).detect_buses(2) == []
        config = BusRoutingConfig(enabled=True, length_window=30.0)
        with contextlib.redirect_stdout(io.StringIO()):
            router.route_all_with_buses(config)
        assert {route.net for route in router.routes} == {1, 2, 3, 4, 5, 6}


# ---------------------------------------------------------------------------
# CLI: flag parsing + ignored-flag warnings
# ---------------------------------------------------------------------------


class TestCliFlag:
    def test_flag_parses(self):
        args = route_args(
            [
                "board.kicad_pcb",
                "--strategy",
                "basic",
                "--bus-routing",
                "--group-length-window",
                "20",
            ]
        )
        assert args.group_length_window == 20.0

    def test_flag_defaults_off(self):
        args = route_args(["board.kicad_pcb", "--strategy", "basic"])
        assert args.group_length_window is None

    def test_escalation_ladder_warns_on_stderr(self, capsys):
        from kicad_tools.cli.route_cmd import _warn_escalation_flags_ignored

        args = argparse.Namespace(
            progressive_clearance=False, bus_routing=True, group_length_window=20.0
        )
        _warn_escalation_flags_ignored(args)
        _warn_escalation_flags_ignored(args)  # guarded: once per run
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "--bus-routing, --group-length-window ignored" in captured.err
        assert captured.err.count("warning:") == 1

    def test_escalation_ladder_warns_on_zero_window(self, capsys):
        # ``0.0`` is a set value, not a falsy "absent".
        from kicad_tools.cli.route_cmd import _warn_escalation_flags_ignored

        args = argparse.Namespace(
            progressive_clearance=False, bus_routing=False, group_length_window=0.0
        )
        _warn_escalation_flags_ignored(args)
        captured = capsys.readouterr()
        assert "--group-length-window ignored" in captured.err

    def test_escalation_ladder_silent_without_flags(self, capsys):
        from kicad_tools.cli.route_cmd import _warn_escalation_flags_ignored

        _warn_escalation_flags_ignored(
            argparse.Namespace(progressive_clearance=False, bus_routing=False)
        )
        _warn_escalation_flags_ignored(argparse.Namespace())
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err == ""

    def test_negotiated_warns_on_stderr(self, capsys):
        from kicad_tools.cli.route_cmd import _warn_bus_flags_ignored_on_negotiated

        args = argparse.Namespace(bus_routing=True, group_length_window=20.0)
        _warn_bus_flags_ignored_on_negotiated(args)
        _warn_bus_flags_ignored_on_negotiated(args)  # guarded: once per run
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "--bus-routing, --group-length-window ignored" in captured.err
        assert "--strategy negotiated" in captured.err
        assert captured.err.count("warning:") == 1

    def test_negotiated_warns_for_window_alone(self, capsys):
        from kicad_tools.cli.route_cmd import _warn_bus_flags_ignored_on_negotiated

        args = argparse.Namespace(bus_routing=False, group_length_window=12.5)
        _warn_bus_flags_ignored_on_negotiated(args)
        captured = capsys.readouterr()
        assert "--group-length-window ignored" in captured.err
        assert "--bus-routing" not in captured.err

    def test_negotiated_silent_without_flags(self, capsys):
        from kicad_tools.cli.route_cmd import _warn_bus_flags_ignored_on_negotiated

        _warn_bus_flags_ignored_on_negotiated(argparse.Namespace(bus_routing=False))
        _warn_bus_flags_ignored_on_negotiated(argparse.Namespace())
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err == ""


# ---------------------------------------------------------------------------
# Congested-board measurement (Issue #9621 AC)
# ---------------------------------------------------------------------------
#
# The acceptance criterion asks for an order-dependence repro ("group routing
# completes where per-net default order strands a net") OR a measured
# refutation.  Measured refutation -- on every synthetic congested geometry
# tried during implementation (through-hole wall slots down to a 3 mm gap, a
# pocketed pad cluster, and the 0.8 mm-pitch DDR-style row below), with the
# C++ backend, BOTH the default ``route_all`` order and the escape-ordered
# length-window group routed 6/6 nets; tighter slots (3 mm gap) capped BOTH
# at 4/6 and 2/6 -- capacity-bound, not order-bound.  Grouping gives no
# completion benefit on these synthetic cases.  What IS order-sensitive and
# is pinned below: the escape openness differs across the row (middle pads
# are tighter than edge pads) and the group's order differs from the plain
# priority order -- so the group ordering is real and exercised, just not
# completion-decisive on a 2-layer board this small.


@native
class TestCongestedBoardMeasurement:
    def test_escape_order_differs_from_plain_priority_and_completion_equal(self, capsys):
        router = _tight_row_router()
        bus = BusRouter(router)
        config = BusRoutingConfig(enabled=True, length_window=30.0)
        (group,) = bus.detect_length_window_groups(config, exclude=set())

        plain_order = sorted(
            (n for n in router.nets if n), key=lambda n: router._get_net_priority(n)
        )
        escape_order = group.get_net_ids()
        # The escape-freedom order is a genuine permutation that differs
        # from the default priority order on this congested row.
        assert sorted(escape_order) == plain_order == [1, 2, 3, 4, 5, 6]
        assert escape_order != plain_order

        with contextlib.redirect_stdout(io.StringIO()):
            router.route_all(timeout=60.0, per_net_timeout=20.0)
        plain_routed = {route.net for route in router.routes}

        router2 = _tight_row_router()
        with contextlib.redirect_stdout(io.StringIO()):
            router2.route_all_with_buses(config)
        grouped_routed = {route.net for route in router2.routes}

        # Measured refutation: both orders complete every net.
        assert plain_routed == {1, 2, 3, 4, 5, 6}
        assert grouped_routed == {1, 2, 3, 4, 5, 6}
