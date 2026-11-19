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
from pathlib import Path

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
from tests.route_cli import route_args

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

    def test_exact_boundary_survives_float_roundoff(self):
        # PR #9633 review: 50.0 * 1.15 evaluates to 57.49999999999999, which
        # used to drop a net at exactly 57.5 mm from its cluster.
        nets = {1: ("A", 50.0), 2: ("B", 57.5)}
        groups = group_by_length_window(nets, 15.0)
        assert len(groups) == 1
        assert [s.net_name for s in groups[0].signals] == ["A", "B"]

    def test_exact_boundary_whole_millimetres_survive_roundoff(self):
        # The review's second case: 100.0 * 1.15 == 114.99999999999999.
        nets = {1: ("A", 100.0), 2: ("B", 115.0)}
        groups = group_by_length_window(nets, 15.0)
        assert len(groups) == 1
        assert [s.net_name for s in groups[0].signals] == ["A", "B"]

    def test_just_outside_roundoff_boundary_still_excluded(self):
        # 57.6 is 0.1 mm past the 57.5 boundary -- outside even with the
        # roundoff tolerance.
        nets = {1: ("A", 50.0), 2: ("B", 57.6)}
        assert group_by_length_window(nets, 15.0) == []

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

    def test_zero_pct_window_stays_exact_under_tolerance(self):
        # PR #9633 review: the roundoff tolerance applies to positive
        # windows only -- a zero-percent window must keep the exact
        # comparison so a hair-above value does not sneak in.
        assert len(group_by_length_window({1: ("A", 50.0), 2: ("B", 50.0)}, 0.0)) == 1
        assert group_by_length_window({1: ("A", 50.0), 2: ("B", 50.000000001)}, 0.0) == []


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

    def test_missing_openness_routes_after_any_finite_count(self):
        # Openness is summed over a net's pads, so a net with enough pads can
        # carry a count past any fixed sentinel (the original 4 * 64 = 256
        # would have sorted such a net after a genuinely-missing entry).
        # The sentinel is math.inf: missing always routes last, unconditionally.
        ordered = order_group_by_escape(_hand_group(), {3: 1, 4: 300})
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
        src.append(
            {
                "number": str(i + 1),
                "x": 6,
                "y": y,
                "width": 0.5,
                "height": 0.5,
                "net": net,
                "net_name": name,
            }
        )
        dst.append(
            {
                "number": str(i + 1),
                "x": 44,
                "y": y,
                "width": 0.9,
                "height": 0.9,
                "net": net,
                "net_name": name,
            }
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
                {
                    "number": "1",
                    "x": 6,
                    "y": 16,
                    "width": 1.0,
                    "height": 1.0,
                    "net": 7,
                    "net_name": "+3V3",
                },
                {
                    "number": "2",
                    "x": 44,
                    "y": 16,
                    "width": 1.0,
                    "height": 1.0,
                    "net": 7,
                    "net_name": "+3V3",
                },
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
                {
                    "number": "1",
                    "x": 6,
                    "y": 16,
                    "width": 0.9,
                    "height": 0.9,
                    "net": 7,
                    "net_name": "DATA_0",
                },
                {
                    "number": "2",
                    "x": 44,
                    "y": 16,
                    "width": 0.9,
                    "height": 0.9,
                    "net": 7,
                    "net_name": "DATA_0",
                },
                {
                    "number": "3",
                    "x": 6,
                    "y": 17,
                    "width": 0.9,
                    "height": 0.9,
                    "net": 8,
                    "net_name": "DATA_1",
                },
                {
                    "number": "4",
                    "x": 44,
                    "y": 17,
                    "width": 0.9,
                    "height": 0.9,
                    "net": 8,
                    "net_name": "DATA_1",
                },
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
                {
                    "number": "1",
                    "x": 5,
                    "y": 5,
                    "width": 1.0,
                    "height": 1.0,
                    "net": 1,
                    "net_name": "SOLO",
                },
                {
                    "number": "2",
                    "x": 5,
                    "y": 10,
                    "width": 1.0,
                    "height": 1.0,
                    "net": 2,
                    "net_name": "OTHER",
                },
                {
                    "number": "3",
                    "x": 25,
                    "y": 10,
                    "width": 1.0,
                    "height": 1.0,
                    "net": 2,
                    "net_name": "OTHER",
                },
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
                {
                    "number": "1",
                    "x": 6,
                    "y": 17.5,
                    "width": 0.9,
                    "height": 0.9,
                    "net": 7,
                    "net_name": "DISTANT",
                },
                {
                    "number": "2",
                    "x": 48,
                    "y": 17.5,
                    "width": 0.9,
                    "height": 0.9,
                    "net": 7,
                    "net_name": "DISTANT",
                },
            ],
        )
        config = BusRoutingConfig(enabled=True, length_window=5.0)
        groups = BusRouter(router).detect_length_window_groups(config, exclude=set())
        names = [s.net_name for g in groups for s in g.signals]
        assert "DISTANT" not in names


def _rotated_pad_router(width: float, height: float, rotation: float) -> Autorouter:
    """The PR #9633 review geometry: two equal-length nets, SIG_B's source
    pad rotated at a non-cardinal angle and flanked by a small foreign pad.

    The same physical pad is authored two equivalent ways -- 2 x 0.4 mm at
    45 deg and 0.4 x 2 mm at 135 deg occupy the same board-space box -- so
    every board-space probe must agree between the encodings.
    """
    router = Autorouter(25, 16, force_python=True)
    router.add_component(
        "U1",
        [
            {
                "number": "1",
                "x": 5,
                "y": 11,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "SIG_A",
            },
            {
                "number": "2",
                "x": 5,
                "y": 5,
                "width": width,
                "height": height,
                "rotation": rotation,
                "net": 2,
                "net_name": "SIG_B",
            },
        ],
    )
    router.add_component(
        "U2",
        [
            {
                "number": "1",
                "x": 17,
                "y": 11,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "SIG_A",
            },
            {
                "number": "2",
                "x": 17,
                "y": 5,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "SIG_B",
            },
        ],
    )
    router.add_component(
        "FOREIGN",
        [
            {
                "number": "1",
                "x": 6.6,
                "y": 5,
                "width": 0.2,
                "height": 0.2,
                "net": 0,
                "net_name": "",
            },
        ],
    )
    return router


class TestRotatedPadEncodingStability:
    """PR #9633 review finding 1: rotating a pad's local axes must not
    change the escape openness (and so the group's routing order) for
    identical board-space copper."""

    def test_openness_is_encoding_independent(self):
        bus_45 = BusRouter(_rotated_pad_router(2.0, 0.4, 45.0))
        bus_135 = BusRouter(_rotated_pad_router(0.4, 2.0, 135.0))
        for net_id in (1, 2):
            assert bus_45._net_escape_openness(net_id) == bus_135._net_escape_openness(net_id)

    def test_group_order_is_encoding_independent(self):
        config = BusRoutingConfig(enabled=True, length_window=50.0)
        groups_45 = BusRouter(_rotated_pad_router(2.0, 0.4, 45.0)).detect_length_window_groups(
            config, exclude=set()
        )
        groups_135 = BusRouter(_rotated_pad_router(0.4, 2.0, 135.0)).detect_length_window_groups(
            config, exclude=set()
        )
        assert len(groups_45) == 1 and len(groups_135) == 1
        order_45 = [s.net_name for s in groups_45[0].signals]
        order_135 = [s.net_name for s in groups_135[0].signals]
        assert order_45 == order_135
        # The foreign pad makes SIG_B the tighter escape: it routes first
        # under BOTH encodings (the review measured the pre-fix bug as
        # SIG_B, SIG_A vs SIG_A, SIG_B).
        assert order_45[0] == "SIG_B"


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
        from kicad_tools.cli.route_cmd import _bus_flags_ignored_reason, _warn_bus_flags_ignored

        args = argparse.Namespace(bus_routing=True, group_length_window=20.0, strategy="negotiated")
        reason = _bus_flags_ignored_reason(args, use_adaptive_grid=False)
        assert reason == "--strategy negotiated"
        _warn_bus_flags_ignored(args, reason)
        _warn_bus_flags_ignored(args, reason)  # guarded: once per run
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "--bus-routing, --group-length-window ignored" in captured.err
        assert "--strategy negotiated" in captured.err
        assert captured.err.count("warning:") == 1

    def test_negotiated_warns_for_window_alone(self, capsys):
        from kicad_tools.cli.route_cmd import _warn_bus_flags_ignored

        args = argparse.Namespace(bus_routing=False, group_length_window=12.5)
        _warn_bus_flags_ignored(args, "--strategy negotiated")
        captured = capsys.readouterr()
        assert "--group-length-window ignored" in captured.err
        assert "--bus-routing" not in captured.err

    def test_negotiated_silent_without_flags(self, capsys):
        from kicad_tools.cli.route_cmd import _bus_flags_ignored_reason, _warn_bus_flags_ignored

        args = argparse.Namespace(bus_routing=False)
        assert _bus_flags_ignored_reason(args, use_adaptive_grid=False) is None
        _warn_bus_flags_ignored(args, "--strategy negotiated")
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err == ""

    def test_reason_names_each_preempting_dispatch(self):
        from kicad_tools.cli.route_cmd import _bus_flags_ignored_reason

        def ns(**kw):
            base = {
                "bus_routing": True,
                "strategy": "basic",
                "progressive_clearance": False,
                "multi_resolution": False,
                "two_phase": False,
                "differential_pairs": False,
            }
            base.update(kw)
            return argparse.Namespace(**base)

        # The basic single-attempt bus dispatch honours the flags: no warning.
        assert _bus_flags_ignored_reason(ns(), use_adaptive_grid=False) is None
        # Every pre-empting dispatch is named, in dispatch order.
        assert (
            _bus_flags_ignored_reason(ns(), use_adaptive_grid=True)
            == "the adaptive multi-resolution grid"
        )
        assert (
            _bus_flags_ignored_reason(ns(), use_adaptive_grid=False, use_escape_routing=True)
            == "the escape pre-phase"
        )
        assert (
            _bus_flags_ignored_reason(ns(progressive_clearance=True), use_adaptive_grid=False)
            == "--progressive-clearance"
        )
        assert (
            _bus_flags_ignored_reason(ns(multi_resolution=True), use_adaptive_grid=False)
            == "--multi-resolution"
        )
        assert (
            _bus_flags_ignored_reason(
                ns(two_phase=True, strategy="negotiated"), use_adaptive_grid=False
            )
            == "--two-phase"
        )
        assert (
            _bus_flags_ignored_reason(ns(strategy="negotiated"), use_adaptive_grid=False)
            == "--strategy negotiated"
        )
        assert (
            _bus_flags_ignored_reason(ns(differential_pairs=True), use_adaptive_grid=False)
            == "--differential-pairs"
        )
        # --two-phase under basic falls through to the bus branch: honoured.
        assert _bus_flags_ignored_reason(ns(two_phase=True), use_adaptive_grid=False) is None


FIXTURE_PCB = (
    Path(__file__).resolve().parents[1] / "fixtures" / "route_entry_paths" / "routable.kicad_pcb"
)


class TestBusWarningBeforeDispatch:
    """PR #9633 review finding 3: the bus-flags-ignored warning must fire
    before the cache lookup and the strategy dispatch, so a cache hit or an
    early dispatch return (two-phase) cannot print ``Bus: enabled`` and
    silently skip it.  Driven through the real ``route_cmd.main`` on the
    same fixture the review probed; only the routing dispatch itself is
    stubbed."""

    def _argv(self, tmp_path, *, strategy="negotiated", extra=()):
        # No ``--timeout``: a bounded timeout makes ``run_attempt`` re-exec
        # the route in a supervised SUBPROCESS, where in-process dispatch
        # mocks and capsys cannot see it (the dispatch itself is stubbed, so
        # the run is fast without a deadline).
        return [
            str(FIXTURE_PCB),
            "-o",
            str(tmp_path / "out.kicad_pcb"),
            "--quiet",
            "--backend",
            "python",
            "--grid",
            "0.1",
            "--no-auto-layers",
            "--strategy",
            strategy,
            "--bus-routing",
            "--group-length-window",
            "150",
            "--iterations",
            "1",
            *extra,
        ]

    def test_negotiated_cold_run_warns(self, tmp_path, capsys):
        from unittest import mock

        from kicad_tools.cli import route_cmd

        with mock.patch.object(Autorouter, "route_all_negotiated", return_value=[]):
            route_cmd.main(self._argv(tmp_path, extra=["--no-cache"]))
        err = capsys.readouterr().err
        assert "--bus-routing, --group-length-window ignored" in err
        assert "--strategy negotiated does not support bus-aware routing" in err
        assert err.count("warning:") == 1

    def test_cache_hit_still_warns(self, tmp_path, capsys):
        from datetime import datetime, timezone
        from unittest import mock

        from kicad_tools.cli import route_cmd
        from kicad_tools.router.cache import CachedRoutingResult, RoutingCache

        # An empty-but-valid cache entry: the hit branch restores zero
        # routes and skips the dispatch entirely -- exactly the return that
        # used to skip the warning.
        payload = RoutingCache(cache_dir=tmp_path / "cache").serialize_routes([], route_usage={})
        fake = CachedRoutingResult(
            cache_key="test",
            routes_data=payload,
            success_count=0,
            failure_count=0,
            total_segments=0,
            total_vias=0,
            compute_time_ms=1,
            created_at=datetime.now(timezone.utc),
            last_accessed=datetime.now(timezone.utc),
        )
        with (
            mock.patch.object(RoutingCache, "__init__", lambda self, *a, **k: None),
            mock.patch.object(RoutingCache, "get", return_value=fake),
            mock.patch.object(RoutingCache, "put", mock.Mock(return_value=None)),
        ):
            route_cmd.main(self._argv(tmp_path))
        err = capsys.readouterr().err
        assert "--bus-routing, --group-length-window ignored" in err
        assert "--strategy negotiated does not support bus-aware routing" in err

    def test_two_phase_cold_run_warns(self, tmp_path, capsys):
        from unittest import mock

        from kicad_tools.cli import route_cmd

        with mock.patch.object(Autorouter, "route_all_two_phase", return_value=[]):
            route_cmd.main(self._argv(tmp_path, extra=["--two-phase", "--no-cache"]))
        err = capsys.readouterr().err
        assert "--bus-routing, --group-length-window ignored" in err
        assert "--two-phase does not support bus-aware routing" in err

    def test_basic_bus_dispatch_is_the_silent_control(self, tmp_path, capsys):
        from unittest import mock

        from kicad_tools.cli import route_cmd

        with mock.patch.object(Autorouter, "route_all_with_buses", return_value=[]) as stub:
            route_cmd.main(self._argv(tmp_path, strategy="basic", extra=["--no-cache"]))
        stub.assert_called_once()  # the bus dispatch really ran
        err = capsys.readouterr().err
        assert "does not support bus-aware routing" not in err


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
#
# The measurement runs unconditionally on the pure-Python path
# (``_tight_row_router`` builds its Autorouter with ``force_python=True``, so
# the C++ backend changes nothing here); a 6-net synthetic row is cheap
# enough that skipping when the backend is absent would only make the AC's
# recorded refutation silently disappear.


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


class TestCompletionDispatchCopper:
    """What the two completion dispatches produce, not just what they parse (#9621).

    Forwarding ``--bus-routing`` to the chained ``--complete`` pass flips its
    basic-strategy dispatch from ``route_all`` to ``route_all_with_buses``.
    The bus driver never runs ``route_all``'s preamble/tail (prepare /
    pour-filter / matrix / match-group / byte-lane / detour-probe / subgrid
    prepasses and the ``BLOCKED_BY_COMPONENT`` rip-up rescue), so the swap is
    NOT copper-neutral.  ``tests/test_cli_route_chain_forwarding_8694.py``
    pins the parsed state that selects the branch; this class pins what the
    branch does -- the recorded measurement behind the ``_chain_complete_argv``
    docstring's Issue #9621 note.  If an engine change moves these numbers,
    update that docstring citation too.
    """

    @staticmethod
    def _total_length_mm(routes) -> float:
        return sum(
            ((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) ** 0.5 for r in routes for s in r.segments
        )

    def test_bus_dispatch_copper_is_not_route_all_copper(self):
        config = BusRoutingConfig(enabled=True, length_window=30.0)

        plain = _tight_row_router()
        plain._strategy = "lattice"  # the engine --complete picks for the pass
        with contextlib.redirect_stdout(io.StringIO()):
            plain_routes = plain.route_all(timeout=120.0)

        buses = _tight_row_router()
        buses._strategy = "lattice"
        with contextlib.redirect_stdout(io.StringIO()):
            bus_routes = buses.route_all_with_buses(config)

        # Both arms engaged the lattice netset negotiation, so this compares
        # the two dispatch drivers, not two engines.
        assert plain._lattice_net_routes is not None
        assert buses._lattice_net_routes is not None
        # Both dispatches close every net.
        assert {r.net for r in plain_routes} == {1, 2, 3, 4, 5, 6}
        assert {r.net for r in bus_routes} == {1, 2, 3, 4, 5, 6}

        # Measured (deterministic; force_python=True): route_all 235.310 mm /
        # 53 segments, bus dispatch 246.818 mm / 57 segments -- +4.9% total
        # trace length for the same 6/6 completion.
        assert round(self._total_length_mm(plain_routes), 3) == 235.310
        assert round(self._total_length_mm(bus_routes), 3) == 246.818
        assert sum(len(r.segments) for r in plain_routes) == 53
        assert sum(len(r.segments) for r in bus_routes) == 57
        assert self._total_length_mm(bus_routes) > self._total_length_mm(plain_routes)
