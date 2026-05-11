"""Tests for ``--placement-feedback`` CLI plumbing.

Issue #2595: expose ``Autorouter.route_with_placement_feedback`` via
``kct route --placement-feedback`` so structurally-unrouteable boards
(e.g. chorus-test) can opt into the closed-loop placement adjuster.

These tests cover:

1. **Parser** -- the central CLI parser accepts the new flags and the
   defaults are correct (default = off, byte-identical with prior behavior).
2. **Forwarding** -- ``run_route_command`` forwards the flags to
   ``route_cmd.main`` correctly, and *does not forward* them when not set
   (the regression invariant from the acceptance criteria: boards 01-05
   must produce identical routes when ``--placement-feedback`` is not
   passed).
3. **Helpers** -- the small helper functions in route_cmd.py
   (``_parse_ref_list``, ``_auto_detect_anchored_refs``,
   ``_resolve_placement_feedback_anchors``, ``_placement_diff_path``)
   behave correctly for representative inputs.
4. **PlacementFeedbackLoop fixed_refs** -- strategies that touch any
   anchored ref are filtered out by the loop, and strategies that
   exceed ``max_movement`` are filtered out.

These are unit tests; the end-to-end "chorus-test routes 44/46 nets"
acceptance criterion is covered by an integration test landed
separately (``tests/integration/test_chorus_test_routing.py``) which
runs the full router pipeline against the actual board.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _make_base_args(**overrides):
    """Build a SimpleNamespace with all fields ``run_route_command`` reads.

    Mirrors ``TestRouteCommandAutoFixFlags._make_base_args`` -- intentionally
    duplicated rather than imported so this test file is self-contained.
    """
    defaults = {
        "pcb": "test.kicad_pcb",
        "output": None,
        "strategy": "negotiated",
        "skip_nets": None,
        "grid": "0.25",
        "trace_width": 0.2,
        "clearance": 0.15,
        "via_drill": 0.3,
        "via_diameter": 0.6,
        "mc_trials": 10,
        "iterations": 15,
        "verbose": False,
        "dry_run": True,
        "quiet": True,
        "power_nets": None,
        "layers": "auto",
        "force": False,
        "no_optimize": False,
        "auto_layers": False,
        "max_layers": 6,
        "min_completion": 0.95,
        "adaptive_rules": False,
        "min_trace": None,
        "min_clearance_floor": None,
        "manufacturer": "jlcpcb",
        "high_performance": False,
        "skip_drc": False,
        "auto_fix": False,
        "auto_fix_passes": None,
        # Issue #2595 defaults: all OFF, matching parser defaults so the
        # "not forwarded when default" invariant holds.
        "placement_feedback": False,
        "placement_feedback_budget": 3,
        "placement_feedback_max_movement": 5.0,
        "placement_feedback_anchor": None,
        "placement_feedback_no_anchor": None,
        # Issue #2606 defaults: stagnation patience matches parser
        # default (3); outer_timeout is None (disabled) by default so
        # the "not forwarded when default" invariant holds.
        "placement_feedback_stagnation_patience": 3,
        "placement_feedback_outer_timeout": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestPlacementFeedbackParser:
    """Centralized parser accepts the new flags and the defaults are correct."""

    def test_default_off(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "test.kicad_pcb"])
        assert args.placement_feedback is False
        assert args.placement_feedback_budget == 3
        assert args.placement_feedback_max_movement == 5.0
        assert args.placement_feedback_anchor is None
        assert args.placement_feedback_no_anchor is None

    def test_flag_enables(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            ["route", "test.kicad_pcb", "--placement-feedback"]
        )
        assert args.placement_feedback is True

    def test_no_flag_disables(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "route",
                "test.kicad_pcb",
                "--placement-feedback",
                "--no-placement-feedback",
            ]
        )
        assert args.placement_feedback is False

    def test_budget_parses(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "route",
                "test.kicad_pcb",
                "--placement-feedback",
                "--placement-feedback-budget",
                "5",
            ]
        )
        assert args.placement_feedback_budget == 5

    def test_max_movement_parses(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "route",
                "test.kicad_pcb",
                "--placement-feedback-max-movement",
                "2.5",
            ]
        )
        assert args.placement_feedback_max_movement == 2.5

    def test_anchor_parses(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "route",
                "test.kicad_pcb",
                "--placement-feedback-anchor",
                "U5,U7,U9",
            ]
        )
        assert args.placement_feedback_anchor == "U5,U7,U9"

    def test_help_lists_flags(self):
        import contextlib
        from io import StringIO

        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        buf = StringIO()
        with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
            parser.parse_args(["route", "--help"])
        help_text = buf.getvalue()
        assert "--placement-feedback" in help_text
        assert "--placement-feedback-budget" in help_text
        assert "--placement-feedback-max-movement" in help_text
        assert "--placement-feedback-anchor" in help_text
        # Issue #2606: new flags surface in help text.
        assert "--placement-feedback-stagnation-patience" in help_text
        assert "--placement-feedback-outer-timeout" in help_text

    # Issue #2606: stagnation-patience + outer-timeout flag parsing.

    def test_stagnation_patience_default(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "test.kicad_pcb"])
        assert args.placement_feedback_stagnation_patience == 3

    def test_stagnation_patience_parses(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "route",
                "test.kicad_pcb",
                "--placement-feedback-stagnation-patience",
                "5",
            ]
        )
        assert args.placement_feedback_stagnation_patience == 5

    def test_outer_timeout_default(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "test.kicad_pcb"])
        assert args.placement_feedback_outer_timeout is None

    def test_outer_timeout_parses(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "route",
                "test.kicad_pcb",
                "--placement-feedback-outer-timeout",
                "60.0",
            ]
        )
        assert args.placement_feedback_outer_timeout == 60.0


# ---------------------------------------------------------------------------
# Forwarding tests -- the regression invariant
# ---------------------------------------------------------------------------


class TestPlacementFeedbackForwarding:
    """``run_route_command`` forwards (or doesn't forward) the new flags.

    The acceptance criterion for issue #2595 explicitly requires:

        > boards 01-05 must produce identical routes when
        > --placement-feedback is not passed

    To preserve byte-identical behavior, the route_cmd handler must NOT
    inject any of the placement-feedback flags into the sub-argv when the
    user did not enable them.  These tests verify that invariant.
    """

    def test_not_forwarded_when_disabled(self):
        """No placement-feedback args appear when the flag is off."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = _make_base_args(placement_feedback=False)

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--placement-feedback" not in call_args
            assert "--placement-feedback-budget" not in call_args
            assert "--placement-feedback-max-movement" not in call_args
            assert "--placement-feedback-anchor" not in call_args
            assert "--placement-feedback-no-anchor" not in call_args
            # Issue #2606: at default values these must NOT appear in
            # sub-argv so the "byte-identical" invariant holds.
            assert (
                "--placement-feedback-stagnation-patience" not in call_args
            )
            assert "--placement-feedback-outer-timeout" not in call_args

    def test_forwarded_when_enabled(self):
        """--placement-feedback is forwarded when set."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = _make_base_args(placement_feedback=True)

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--placement-feedback" in call_args

    def test_budget_forwarded_when_non_default(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _make_base_args(
            placement_feedback=True, placement_feedback_budget=5
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--placement-feedback-budget" in call_args
            idx = call_args.index("--placement-feedback-budget")
            assert call_args[idx + 1] == "5"

    def test_budget_not_forwarded_when_default(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _make_base_args(
            placement_feedback=True, placement_feedback_budget=3
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--placement-feedback-budget" not in call_args

    def test_max_movement_forwarded_when_non_default(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _make_base_args(
            placement_feedback=True, placement_feedback_max_movement=2.0
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--placement-feedback-max-movement" in call_args
            idx = call_args.index("--placement-feedback-max-movement")
            assert call_args[idx + 1] == "2.0"

    def test_anchor_forwarded(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _make_base_args(
            placement_feedback=True, placement_feedback_anchor="U5,U7"
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--placement-feedback-anchor" in call_args
            idx = call_args.index("--placement-feedback-anchor")
            assert call_args[idx + 1] == "U5,U7"

    def test_no_anchor_forwarded(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _make_base_args(
            placement_feedback=True, placement_feedback_no_anchor="J3"
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--placement-feedback-no-anchor" in call_args
            idx = call_args.index("--placement-feedback-no-anchor")
            assert call_args[idx + 1] == "J3"

    # Issue #2606: stagnation-patience + outer-timeout flag forwarding.

    def test_stagnation_patience_not_forwarded_when_default(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _make_base_args(
            placement_feedback=True,
            placement_feedback_stagnation_patience=3,
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert (
                "--placement-feedback-stagnation-patience" not in call_args
            )

    def test_stagnation_patience_forwarded_when_non_default(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _make_base_args(
            placement_feedback=True,
            placement_feedback_stagnation_patience=5,
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--placement-feedback-stagnation-patience" in call_args
            idx = call_args.index(
                "--placement-feedback-stagnation-patience"
            )
            assert call_args[idx + 1] == "5"

    def test_outer_timeout_not_forwarded_when_default(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _make_base_args(
            placement_feedback=True,
            placement_feedback_outer_timeout=None,
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--placement-feedback-outer-timeout" not in call_args

    def test_outer_timeout_forwarded_when_set(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _make_base_args(
            placement_feedback=True,
            placement_feedback_outer_timeout=60.0,
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--placement-feedback-outer-timeout" in call_args
            idx = call_args.index("--placement-feedback-outer-timeout")
            assert call_args[idx + 1] == "60.0"


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class _MockFootprint:
    """Mock footprint matching the schema interface used by the helpers."""

    def __init__(self, reference: str, locked: bool = False):
        self.reference = reference
        self.locked = locked
        self.position = (0.0, 0.0)
        self.rotation = 0.0


class _MockPCB:
    def __init__(self, footprints):
        self.footprints = list(footprints)


class TestParseRefList:
    def test_none(self):
        from kicad_tools.cli.route_cmd import _parse_ref_list

        assert _parse_ref_list(None) == set()

    def test_empty(self):
        from kicad_tools.cli.route_cmd import _parse_ref_list

        assert _parse_ref_list("") == set()

    def test_single(self):
        from kicad_tools.cli.route_cmd import _parse_ref_list

        assert _parse_ref_list("U5") == {"U5"}

    def test_multiple(self):
        from kicad_tools.cli.route_cmd import _parse_ref_list

        assert _parse_ref_list("U5,U7,U9") == {"U5", "U7", "U9"}

    def test_whitespace_stripped(self):
        from kicad_tools.cli.route_cmd import _parse_ref_list

        assert _parse_ref_list(" U5 , U7 , U9 ") == {"U5", "U7", "U9"}

    def test_empty_entries_dropped(self):
        from kicad_tools.cli.route_cmd import _parse_ref_list

        assert _parse_ref_list("U5,,U7,") == {"U5", "U7"}


class TestAutoDetectAnchoredRefs:
    def test_connectors_anchored(self):
        from kicad_tools.cli.route_cmd import _auto_detect_anchored_refs

        pcb = _MockPCB(
            [
                _MockFootprint("U1"),
                _MockFootprint("J1"),
                _MockFootprint("J2"),
                _MockFootprint("R1"),
            ]
        )
        anchored = _auto_detect_anchored_refs(pcb)
        assert anchored == {"J1", "J2"}

    def test_headers_anchored(self):
        """P-prefixed headers/test-points are anchored too."""
        from kicad_tools.cli.route_cmd import _auto_detect_anchored_refs

        pcb = _MockPCB([_MockFootprint("P1"), _MockFootprint("R1")])
        anchored = _auto_detect_anchored_refs(pcb)
        assert anchored == {"P1"}

    def test_locked_anchored(self):
        """Footprints with locked=True are anchored regardless of prefix."""
        from kicad_tools.cli.route_cmd import _auto_detect_anchored_refs

        pcb = _MockPCB(
            [_MockFootprint("U5", locked=True), _MockFootprint("U7")]
        )
        anchored = _auto_detect_anchored_refs(pcb)
        assert "U5" in anchored
        assert "U7" not in anchored

    def test_passives_movable(self):
        """R*, C*, L*, U* (without locked) are NOT anchored."""
        from kicad_tools.cli.route_cmd import _auto_detect_anchored_refs

        pcb = _MockPCB(
            [
                _MockFootprint("R1"),
                _MockFootprint("C1"),
                _MockFootprint("L1"),
                _MockFootprint("U1"),
                _MockFootprint("Q1"),
                _MockFootprint("D1"),
                _MockFootprint("LED1"),
            ]
        )
        anchored = _auto_detect_anchored_refs(pcb)
        assert anchored == set()

    def test_empty_pcb(self):
        from kicad_tools.cli.route_cmd import _auto_detect_anchored_refs

        pcb = _MockPCB([])
        assert _auto_detect_anchored_refs(pcb) == set()


class TestResolvePlacementFeedbackAnchors:
    def test_combines_auto_and_user(self):
        from kicad_tools.cli.route_cmd import (
            _resolve_placement_feedback_anchors,
        )

        pcb = _MockPCB([_MockFootprint("J1"), _MockFootprint("U5")])
        args = SimpleNamespace(
            placement_feedback_anchor="U5,U7",
            placement_feedback_no_anchor=None,
        )
        anchors = _resolve_placement_feedback_anchors(pcb, args)
        assert anchors == {"J1", "U5", "U7"}

    def test_no_anchor_subtracts(self):
        from kicad_tools.cli.route_cmd import (
            _resolve_placement_feedback_anchors,
        )

        pcb = _MockPCB([_MockFootprint("J1"), _MockFootprint("J2")])
        args = SimpleNamespace(
            placement_feedback_anchor=None,
            placement_feedback_no_anchor="J2",
        )
        anchors = _resolve_placement_feedback_anchors(pcb, args)
        assert anchors == {"J1"}

    def test_no_anchor_overrides_user_anchor(self):
        """If a ref appears in both --anchor and --no-anchor, --no-anchor wins."""
        from kicad_tools.cli.route_cmd import (
            _resolve_placement_feedback_anchors,
        )

        pcb = _MockPCB([])
        args = SimpleNamespace(
            placement_feedback_anchor="U5",
            placement_feedback_no_anchor="U5",
        )
        anchors = _resolve_placement_feedback_anchors(pcb, args)
        assert anchors == set()


class TestPlacementDiffPath:
    def test_explicit_output(self, tmp_path: Path):
        from kicad_tools.cli.route_cmd import _placement_diff_path

        out = tmp_path / "routed.kicad_pcb"
        args = SimpleNamespace(output=str(out))
        diff = _placement_diff_path(args, tmp_path / "input.kicad_pcb")
        assert diff.name == "routed_placement_diff.json"
        assert diff.parent == tmp_path

    def test_default_output(self, tmp_path: Path):
        from kicad_tools.cli.route_cmd import _placement_diff_path

        pcb = tmp_path / "board.kicad_pcb"
        args = SimpleNamespace(output=None)
        diff = _placement_diff_path(args, pcb)
        # default routed name is <stem>_routed.kicad_pcb so diff is
        # <stem>_routed_placement_diff.json beside the routed PCB.
        assert diff.name == "board_routed_placement_diff.json"
        assert diff.parent == tmp_path


# ---------------------------------------------------------------------------
# PlacementFeedbackLoop fixed_refs / max_movement filtering
# ---------------------------------------------------------------------------


class TestPlacementFeedbackLoopFiltering:
    """The loop filters out strategies that touch anchored refs or exceed
    the movement budget BEFORE applying them."""

    def _make_strategy(self, ref: str, new_xy: tuple[float, float]):
        from kicad_tools.recovery import (
            Action,
            Difficulty,
            ResolutionStrategy,
            StrategyType,
        )

        return ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.85,
            actions=[
                Action(
                    type="move",
                    target=ref,
                    params={"x": new_xy[0], "y": new_xy[1]},
                )
            ],
            affected_components=[ref],
        )

    def test_strategy_touches_fixed_refs(self):
        """A strategy that moves an anchored ref is filtered out."""
        from kicad_tools.router.placement_feedback import (
            PlacementFeedbackLoop,
        )

        loop = PlacementFeedbackLoop.__new__(PlacementFeedbackLoop)
        loop.pcb = None
        loop.fixed_refs = {"J1", "U5"}
        loop.max_movement = None

        s_anchored = self._make_strategy("U5", (10.0, 10.0))
        s_movable = self._make_strategy("U7", (10.0, 10.0))

        assert loop._strategy_touches_fixed_refs(s_anchored) is True
        assert loop._strategy_touches_fixed_refs(s_movable) is False

    def test_strategy_touches_fixed_refs_via_action_target(self):
        """Falls through to action.target when affected_components is empty."""
        from kicad_tools.recovery import (
            Action,
            Difficulty,
            ResolutionStrategy,
            StrategyType,
        )
        from kicad_tools.router.placement_feedback import (
            PlacementFeedbackLoop,
        )

        loop = PlacementFeedbackLoop.__new__(PlacementFeedbackLoop)
        loop.pcb = None
        loop.fixed_refs = {"J1"}
        loop.max_movement = None

        s = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.85,
            actions=[
                Action(type="move", target="J1", params={"x": 10.0, "y": 10.0}),
            ],
            affected_components=[],  # intentionally empty
        )

        assert loop._strategy_touches_fixed_refs(s) is True

    def test_strategy_within_movement_budget_under_cap(self):
        """A 3mm move is within a 5mm cap."""
        from kicad_tools.router.placement_feedback import (
            PlacementFeedbackLoop,
        )

        pcb = _MockPCB([_MockFootprint("U7")])
        # U7 starts at (0, 0) -- move to (3, 0) is a 3mm move
        loop = PlacementFeedbackLoop.__new__(PlacementFeedbackLoop)
        loop.pcb = pcb
        loop.fixed_refs = set()
        loop.max_movement = 5.0

        s = self._make_strategy("U7", (3.0, 0.0))
        assert loop._strategy_within_movement_budget(s) is True

    def test_strategy_within_movement_budget_over_cap(self):
        """A 10mm move exceeds a 5mm cap and is rejected."""
        from kicad_tools.router.placement_feedback import (
            PlacementFeedbackLoop,
        )

        pcb = _MockPCB([_MockFootprint("U7")])
        loop = PlacementFeedbackLoop.__new__(PlacementFeedbackLoop)
        loop.pcb = pcb
        loop.fixed_refs = set()
        loop.max_movement = 5.0

        s = self._make_strategy("U7", (10.0, 0.0))
        assert loop._strategy_within_movement_budget(s) is False

    def test_strategy_within_movement_budget_disabled(self):
        """max_movement=None means anything goes."""
        from kicad_tools.router.placement_feedback import (
            PlacementFeedbackLoop,
        )

        pcb = _MockPCB([_MockFootprint("U7")])
        loop = PlacementFeedbackLoop.__new__(PlacementFeedbackLoop)
        loop.pcb = pcb
        loop.fixed_refs = set()
        loop.max_movement = None

        s = self._make_strategy("U7", (1000.0, 0.0))
        assert loop._strategy_within_movement_budget(s) is True

    def test_loop_propagates_fixed_refs_and_max_movement(self):
        """``__init__`` stores fixed_refs and max_movement on the instance."""
        from kicad_tools.router.placement_feedback import (
            PlacementFeedbackLoop,
        )

        # Use a stub router; we don't run the loop, just inspect state.
        class _StubRouter:
            pass

            def get_failed_nets(self):
                return []

        loop = PlacementFeedbackLoop(
            router=_StubRouter(),
            pcb=None,
            verbose=False,
            fixed_refs={"J1", "J2", "U5"},
            max_movement=3.0,
        )
        assert loop.fixed_refs == {"J1", "J2", "U5"}
        assert loop.max_movement == 3.0


# ---------------------------------------------------------------------------
# PlacementDiffEntry / placement diff JSON
# ---------------------------------------------------------------------------


class TestPlacementDiffEntry:
    def test_to_dict_round_trip(self):
        from kicad_tools.router import PlacementDiffEntry

        entry = PlacementDiffEntry(
            ref="U7",
            old_xy=(10.0, 20.0),
            new_xy=(13.0, 24.0),
            rotation_delta=90.0,
        )
        d = entry.to_dict()
        assert d["ref"] == "U7"
        assert d["old_xy"] == [10.0, 20.0]
        assert d["new_xy"] == [13.0, 24.0]
        assert d["rotation_delta"] == 90.0
        # 3-4-5 triangle distance is 5.0
        assert abs(d["distance_mm"] - 5.0) < 1e-6

    def test_zero_distance_when_unchanged(self):
        from kicad_tools.router import PlacementDiffEntry

        entry = PlacementDiffEntry(
            ref="C1", old_xy=(5.0, 5.0), new_xy=(5.0, 5.0)
        )
        assert entry.distance_mm == 0.0

    def test_exported_from_router_module(self):
        """PlacementDiffEntry is part of the public router API."""
        import kicad_tools.router as router_pkg

        assert "PlacementDiffEntry" in router_pkg.__all__
        assert hasattr(router_pkg, "PlacementDiffEntry")


# ---------------------------------------------------------------------------
# Issue #2620: inner-parser registration of stagnation-patience and
# outer-timeout flags.
#
# These tests guard against the regression where the top-level parser
# in parser.py registered the two flags but the inner argparse in
# route_cmd.py did not, causing kct route to fail with
# "unrecognized arguments" whenever the forwarder in commands/routing.py
# injected the flags into sub_argv.
# ---------------------------------------------------------------------------


class TestInnerParserPlacementFeedbackFlags:
    """The inner argparse in ``route_cmd.main`` must accept both flags.

    Three complementary checks:

    1. ``--help`` text mentions both flags (parser-level registration).
    2. End-to-end ``main([...])`` smoke tests don't exit with argparse
       code 2 ("unrecognized arguments").
    3. The parsed ``Namespace`` carries the values forward with the
       correct types and defaults.
    """

    def test_inner_help_lists_outer_timeout(self):
        """Issue #2620: inner --help advertises the outer-timeout flag."""
        import contextlib
        from io import StringIO

        from kicad_tools.cli.route_cmd import main as route_main

        buf = StringIO()
        with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
            route_main(["--help"])
        help_text = buf.getvalue()
        assert "--placement-feedback-outer-timeout" in help_text, (
            "inner parser help text is missing --placement-feedback-"
            "outer-timeout (Issue #2620)"
        )
        # Help wording mirrors parser.py:2370-2375 (Issue #2606 tag).
        assert "Issue #2606." in help_text

    def test_inner_help_lists_stagnation_patience(self):
        """Issue #2620: inner --help advertises the stagnation-patience flag."""
        import contextlib
        from io import StringIO

        from kicad_tools.cli.route_cmd import main as route_main

        buf = StringIO()
        with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
            route_main(["--help"])
        help_text = buf.getvalue()
        assert "--placement-feedback-stagnation-patience" in help_text, (
            "inner parser help text is missing --placement-feedback-"
            "stagnation-patience (Issue #2620)"
        )

    def test_inner_parser_accepts_outer_timeout_no_exit_2(self, tmp_path):
        """Regression: inner argparse must accept --placement-feedback-
        outer-timeout (Issue #2620).  Prior to the fix this raised
        SystemExit(2) with "unrecognized arguments".

        We point at a non-existent pcb path so ``main`` returns 1
        (file-not-found) instead of running the router.  Exit code 1
        confirms argparse accepted the flag; the prior bug returned 2.
        """
        from kicad_tools.cli import route_cmd

        # Path that does NOT exist on disk.
        missing_pcb = str(tmp_path / "does_not_exist.kicad_pcb")

        try:
            rc = route_cmd.main(
                [
                    missing_pcb,
                    "--dry-run",
                    "--placement-feedback-outer-timeout",
                    "1800",
                ]
            )
        except SystemExit as exc:
            rc = int(exc.code) if exc.code is not None else 0

        assert rc != 2, (
            "inner argparse rejected --placement-feedback-outer-timeout "
            "(Issue #2620 regression)"
        )
        # File-not-found is the expected next failure mode (rc=1) once
        # argparse accepts the flag.
        assert rc == 1, (
            f"Expected rc=1 (file-not-found) after argparse success, got rc={rc}"
        )

    def test_inner_parser_accepts_stagnation_patience_no_exit_2(self, tmp_path):
        """Regression: inner argparse must accept --placement-feedback-
        stagnation-patience (Issue #2620)."""
        from kicad_tools.cli import route_cmd

        missing_pcb = str(tmp_path / "does_not_exist.kicad_pcb")

        try:
            rc = route_cmd.main(
                [
                    missing_pcb,
                    "--dry-run",
                    "--placement-feedback-stagnation-patience",
                    "5",
                ]
            )
        except SystemExit as exc:
            rc = int(exc.code) if exc.code is not None else 0

        assert rc != 2, (
            "inner argparse rejected --placement-feedback-stagnation-patience "
            "(Issue #2620 regression)"
        )
        assert rc == 1, (
            f"Expected rc=1 (file-not-found) after argparse success, got rc={rc}"
        )

    def test_inner_parser_outer_timeout_value_and_type(self):
        """Issue #2620: parsed value is a float matching the input.

        Mirrors the registration done in route_cmd.main and asserts
        argparse type-conversion semantics for both flags.  The
        registration in route_cmd.main is the production code path;
        this test is a focused unit test that pins the type/default
        contract.
        """
        import argparse

        p = argparse.ArgumentParser()
        p.add_argument(
            "--placement-feedback-outer-timeout",
            type=float,
            default=None,
            metavar="SECONDS",
        )
        p.add_argument(
            "--placement-feedback-stagnation-patience",
            type=int,
            default=3,
            metavar="N",
        )

        args = p.parse_args(["--placement-feedback-outer-timeout", "1800"])
        assert args.placement_feedback_outer_timeout == 1800.0
        assert isinstance(args.placement_feedback_outer_timeout, float)
        assert args.placement_feedback_stagnation_patience == 3
        assert isinstance(args.placement_feedback_stagnation_patience, int)

        args = p.parse_args(["--placement-feedback-stagnation-patience", "5"])
        assert args.placement_feedback_stagnation_patience == 5
        assert isinstance(args.placement_feedback_stagnation_patience, int)
        assert args.placement_feedback_outer_timeout is None

        # Defaults preserved when neither flag is passed.
        args = p.parse_args([])
        assert args.placement_feedback_outer_timeout is None
        assert args.placement_feedback_stagnation_patience == 3

    def test_values_reach_route_with_placement_feedback(self):
        """Issue #2620: values flow through to
        ``router.route_with_placement_feedback`` via
        ``_run_placement_feedback``.  This guards against future
        plumbing regressions where the inner parser accepts the
        flags but they get dropped before reaching the loop."""
        from unittest.mock import MagicMock, patch

        from kicad_tools.cli.route_cmd import _run_placement_feedback

        # Build a stub args namespace with non-default values for both
        # flags so we can assert they reach the router.
        args = SimpleNamespace(
            placement_feedback_budget=3,
            placement_feedback_max_movement=5.0,
            placement_feedback_anchor=None,
            placement_feedback_no_anchor=None,
            placement_feedback_stagnation_patience=7,
            placement_feedback_outer_timeout=42.5,
            strategy="negotiated",
            timeout=None,
            per_net_timeout=None,
            output=None,
        )

        # Stub PCB + router.  PCB.load is patched so we don't need a
        # real file; route_with_placement_feedback is mocked so we
        # never actually run the loop -- just intercept the kwargs.
        stub_pcb = MagicMock()
        stub_pcb.footprints = []

        router = MagicMock()
        router.get_failed_nets.return_value = []
        router.route_with_placement_feedback.return_value = MagicMock(
            iterations=0,
            exit_reason="pf_converged",
            total_components_moved=0,
            failed_nets=[],
            placement_diff=[],
        )

        with (
            patch(
                "kicad_tools.schema.pcb.PCB.load", return_value=stub_pcb
            ),
            patch(
                "pathlib.Path.write_text", return_value=None
            ),
        ):
            _run_placement_feedback(
                router=router,
                pcb_path=Path("/tmp/does_not_matter.kicad_pcb"),
                args=args,
                quiet=True,
            )

        # Verify both values reached the router with correct types.
        call_kwargs = router.route_with_placement_feedback.call_args.kwargs
        assert call_kwargs["stagnation_patience"] == 7
        assert isinstance(call_kwargs["stagnation_patience"], int)
        assert call_kwargs["outer_timeout"] == 42.5
        assert isinstance(call_kwargs["outer_timeout"], float)
