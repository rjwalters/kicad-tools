"""Tests for the ``kct route --analog-nets`` / ``--auto-analog`` flags (Issue #3171).

Phase 3 of analog-aware routing.  The flags inject a priority- and
cost-boosted analog :class:`NetClassRouting` (``NET_CLASS_ANALOG``) onto the
autorouter's name-pattern-classified ``net_class_map`` for designer-named
(``--analog-nets "AUDIO_L,AUDIO_R"``) and/or auto-detected (``--auto-analog``,
via the Phase 2 ``detect_analog_nets``) analog nets.  No A*/pathfinder
changes -- the existing per-net ``priority`` (route order) and per-net
``cost_multiplier`` (cost bias) consumers do the work.

This module mirrors ``tests/test_cli_route_net_class_map.py`` (the
``--net-class-map`` precedent, Issue #2996).

Coverage:

1. **AC #1 / #2: flags declared in both parsers** -- the unified
   ``cli/parser.py`` and the standalone ``route_cmd.py`` parser, with the
   documented defaults (``analog_nets=None``, ``auto_analog=False``).
2. **AC #3: map injection** -- after the analog helper runs, the boosted
   class lands on ``router.net_class_map`` with ``priority`` < digital and
   ``cost_multiplier`` <= 0.9.
3. **AC #4: ordering** -- ``_get_net_priority(analog)[0] < _get_net_priority(digital)[0]``.
4. **AC #2: auto-detect** -- ``--auto-analog`` selects an analog net without
   an explicit list; union behaviour when both flags are set.
5. **AC #5: pour-net guard** -- a pour/ground net (``GNDA``) selected as
   analog keeps its pour semantics (not forced into the pathfinder).
6. **AC #6: forwarding** -- ``run_route_command`` propagates the flags into
   the inner ``route_cmd.main`` argv.
7. **AC #7: no-op when absent** -- without the flags the map and net-priority
   ordering are unchanged vs. baseline.
8. **Edge cases** -- empty list, unknown net name, dedup, zero-analog board.
"""

from __future__ import annotations

import contextlib
import sys
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from kicad_tools.router.core import Autorouter
from kicad_tools.router.rules import (
    NET_CLASS_ANALOG,
    NET_CLASS_DIGITAL,
    DesignRules,
)

# =============================================================================
# Helpers
# =============================================================================


def _make_router(grid_resolution: float = 0.5) -> Autorouter:
    """Create a small Autorouter for testing (mirrors test_off_grid_priority)."""
    rules = DesignRules(grid_resolution=grid_resolution)
    return Autorouter(width=40.0, height=40.0, origin_x=100.0, origin_y=100.0, rules=rules)


def _add_2pin_net(router: Autorouter, ref: str, x: float, y: float, net: int, name: str) -> None:
    """Add a simple 2-pin net so the router populates net_names/nets."""
    router.add_component(
        ref,
        [
            {"number": "1", "x": x, "y": y, "net": net, "net_name": name},
            {"number": "2", "x": x + 2.0, "y": y, "net": net, "net_name": name},
        ],
    )


def _populate_synthetic_board(router: Autorouter) -> dict[str, int]:
    """Populate a synthetic board with one audio + several digital nets.

    Returns a {net_name: net_id} mapping for convenience.
    """
    nets = {
        "AUDIO_L": 1,
        "AUDIO_R": 2,
        "D0": 3,
        "D1": 4,
        "SPI_CLK": 5,
    }
    y = 105.0
    for name, nid in nets.items():
        _add_2pin_net(router, f"U{nid}", 105.0, y, nid, name)
        y += 4.0
    return nets


def _analog_args(**overrides) -> SimpleNamespace:
    """Build a minimal args namespace exercising the analog helper."""
    base: dict[str, object] = {"analog_nets": None, "auto_analog": False}
    base.update(overrides)
    return SimpleNamespace(**base)


# =============================================================================
# AC #1 / #2: Flags declared in both parsers
# =============================================================================


class TestFlagsDefinedInBothParsers:
    """Both flags must appear in the unified and standalone parsers."""

    def test_analog_nets_in_unified_parser_help(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                parser.parse_args(["route", "--help"])
        help_text = help_output.getvalue()
        assert "--analog-nets" in help_text
        assert "--auto-analog" in help_text

    def test_analog_nets_in_route_cmd_help(self):
        from kicad_tools.cli.route_cmd import main as route_main

        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                route_main(["--help"])
        help_text = help_output.getvalue()
        assert "--analog-nets" in help_text
        assert "--auto-analog" in help_text

    def test_help_notes_guard_traces_deferred(self):
        """AC #8: the --help text flags that guard-trace generation is deferred."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                parser.parse_args(["route", "--help"])
        help_text = help_output.getvalue().lower()
        assert "guard" in help_text and "deferred" in help_text

    def test_analog_nets_parses_via_unified_parser(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            ["route", "test.kicad_pcb", "--analog-nets", "AUDIO_L,AUDIO_R", "--auto-analog"]
        )
        assert args.analog_nets == "AUDIO_L,AUDIO_R"
        assert args.auto_analog is True

    def test_defaults_in_unified_parser(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "test.kicad_pcb"])
        assert args.analog_nets is None
        assert args.auto_analog is False

    def test_analog_nets_accepted_by_route_cmd_parser(self, tmp_path, capsys):
        """``route_cmd.main`` accepts the flags (no argparse 'unrecognized')."""
        from kicad_tools.cli.route_cmd import main as route_main

        missing = tmp_path / "does_not_exist.kicad_pcb"
        # The flags must parse cleanly; the run then fails on the missing PCB
        # (exit 1) rather than argparse rejecting the flags (exit 2 / SystemExit).
        result = route_main(
            [
                str(missing),
                "--dry-run",
                "--quiet",
                "--analog-nets",
                "AUDIO_L,AUDIO_R",
                "--auto-analog",
                "--output",
                str(tmp_path / "out.kicad_pcb"),
            ]
        )
        assert result == 1
        captured = capsys.readouterr()
        assert "unrecognized arguments" not in captured.err


# =============================================================================
# AC #3: Map injection (explicit --analog-nets)
# =============================================================================


class TestExplicitAnalogNetInjection:
    """``--analog-nets`` injects the boosted class onto the router's map."""

    def test_boosted_class_lands_on_map(self):
        from kicad_tools.cli.route_cmd import _apply_analog_net_class

        router = _make_router()
        _populate_synthetic_board(router)

        _apply_analog_net_class(router, _analog_args(analog_nets="AUDIO_L,AUDIO_R"), quiet=True)

        # AC #3: priority strictly below digital, cost_multiplier <= 0.9.
        for name in ("AUDIO_L", "AUDIO_R"):
            nc = router.net_class_map[name]
            assert nc.priority < NET_CLASS_DIGITAL.priority
            assert nc.cost_multiplier <= 0.9
            assert nc.noise_sensitive is True

        # Digital nets are untouched.
        assert router.net_class_map.get("D0") != NET_CLASS_ANALOG

    def test_whitespace_and_empty_entries_stripped(self):
        from kicad_tools.cli.route_cmd import _apply_analog_net_class

        router = _make_router()
        _populate_synthetic_board(router)

        _apply_analog_net_class(
            router, _analog_args(analog_nets=" AUDIO_L , , AUDIO_R "), quiet=True
        )
        assert router.net_class_map["AUDIO_L"] == NET_CLASS_ANALOG
        assert router.net_class_map["AUDIO_R"] == NET_CLASS_ANALOG


# =============================================================================
# AC #4: Routing-order injection
# =============================================================================


class TestAnalogRoutesAheadOfDigital:
    """After injection, the analog net sorts ahead of digital nets."""

    def test_analog_net_priority_below_digital(self):
        from kicad_tools.cli.route_cmd import _apply_analog_net_class

        router = _make_router()
        nets = _populate_synthetic_board(router)

        before_audio = router._get_net_priority(nets["AUDIO_L"])[0]
        before_d0 = router._get_net_priority(nets["D0"])[0]
        # Without the flag, both classify by name pattern; AUDIO is already
        # ahead of D0 here, so we assert on the post-injection STRICT gap.

        _apply_analog_net_class(router, _analog_args(analog_nets="AUDIO_L"), quiet=True)

        after_audio = router._get_net_priority(nets["AUDIO_L"])[0]
        after_d0 = router._get_net_priority(nets["D0"])[0]

        # AC #4: analog sorts strictly ahead of digital.
        assert after_audio < after_d0
        # The boost moved (or kept) audio at the analog priority (2).
        assert after_audio == NET_CLASS_ANALOG.priority
        # Digital net ordering is unchanged by the flag.
        assert after_d0 == before_d0
        # And the audio net is now at least as early as before.
        assert after_audio <= before_audio


# =============================================================================
# AC #2: Auto-detect via detect_analog_nets
# =============================================================================


class TestAutoAnalog:
    """``--auto-analog`` selects analog nets via the Phase 2 detector."""

    def test_auto_analog_selects_audio_without_explicit_list(self):
        from kicad_tools.cli.route_cmd import _apply_analog_net_class

        router = _make_router()
        _populate_synthetic_board(router)

        _apply_analog_net_class(router, _analog_args(auto_analog=True), quiet=True)

        # detect_analog_nets classifies AUDIO_L / AUDIO_R as audio.
        assert router.net_class_map["AUDIO_L"] == NET_CLASS_ANALOG
        assert router.net_class_map["AUDIO_R"] == NET_CLASS_ANALOG
        # Pure-digital nets are not selected.
        assert router.net_class_map.get("D0") != NET_CLASS_ANALOG
        assert router.net_class_map.get("D1") != NET_CLASS_ANALOG

    def test_union_of_explicit_and_auto(self):
        """Explicit + auto are unioned; an extra analog signal is added."""
        from kicad_tools.cli.route_cmd import _apply_analog_net_class

        router = _make_router()
        nets = _populate_synthetic_board(router)
        # Add an analog signal net that detect_analog_nets recognises (VREF)
        # plus a digital net that is NOT auto-detected (CUSTOM_BUS) so the
        # explicit list is the only way it gets boosted.
        _add_2pin_net(router, "U10", 130.0, 105.0, 10, "VREF")
        _add_2pin_net(router, "U11", 130.0, 110.0, 11, "CUSTOM_BUS")

        _apply_analog_net_class(
            router,
            _analog_args(analog_nets="CUSTOM_BUS", auto_analog=True),
            quiet=True,
        )

        # Auto-detected analog: AUDIO_L/R + VREF.
        assert router.net_class_map["AUDIO_L"] == NET_CLASS_ANALOG
        assert router.net_class_map["VREF"] == NET_CLASS_ANALOG
        # Explicit-only net (not auto-detected) also boosted via the union.
        assert router.net_class_map["CUSTOM_BUS"] == NET_CLASS_ANALOG
        # Untouched digital.
        assert nets["D0"]  # sanity
        assert router.net_class_map.get("D0") != NET_CLASS_ANALOG

    def test_auto_analog_zero_analog_board_is_noop(self):
        from kicad_tools.cli.route_cmd import _apply_analog_net_class

        router = _make_router()
        # Purely digital board.
        _add_2pin_net(router, "U1", 105.0, 105.0, 1, "D0")
        _add_2pin_net(router, "U2", 105.0, 110.0, 2, "D1")
        _add_2pin_net(router, "U3", 105.0, 115.0, 3, "SPI_CLK")
        baseline = dict(router.net_class_map)

        _apply_analog_net_class(router, _analog_args(auto_analog=True), quiet=True)

        assert router.net_class_map == baseline


# =============================================================================
# AC #5: Pour-net guard
# =============================================================================


class TestPourNetGuard:
    """A pour/ground net selected as analog keeps its pour semantics."""

    def test_gnda_pour_net_not_forced_into_pathfinder(self):
        from kicad_tools.cli.route_cmd import _apply_analog_net_class
        from kicad_tools.router.rules import NetClassRouting

        router = _make_router()
        _add_2pin_net(router, "U1", 105.0, 105.0, 1, "GNDA")
        _add_2pin_net(router, "U2", 105.0, 110.0, 2, "AUDIO_L")

        # Pin GNDA to an explicit pour class so the guard has something to see.
        pour_class = NetClassRouting(
            name="AnalogGround",
            priority=1,
            is_pour_net=True,
            route_via="pour",
        )
        router.net_class_map["GNDA"] = pour_class

        _apply_analog_net_class(router, _analog_args(analog_nets="GNDA,AUDIO_L"), quiet=True)

        # GNDA pour semantics are unchanged: still the pour class, still poured.
        assert router.net_class_map["GNDA"] is pour_class
        assert router.net_class_map["GNDA"].is_pour_net is True
        assert router.net_class_map["GNDA"].route_via == "pour"
        # AUDIO_L (an ordinary pathfinder net) was still boosted.
        assert router.net_class_map["AUDIO_L"] == NET_CLASS_ANALOG

    def test_auto_analog_does_not_force_default_gnda_pour(self):
        """GNDA defaults to the pour POWER class; --auto-analog must not flip it."""
        from kicad_tools.cli.route_cmd import _apply_analog_net_class

        router = _make_router()
        _add_2pin_net(router, "U1", 105.0, 105.0, 1, "GNDA")
        _add_2pin_net(router, "U2", 105.0, 110.0, 2, "AUDIO_L")

        # GNDA maps to the pour POWER class via DEFAULT_NET_CLASS_MAP.
        assert router.net_class_map["GNDA"].is_pour_net is True

        _apply_analog_net_class(router, _analog_args(auto_analog=True), quiet=True)

        assert router.net_class_map["GNDA"].is_pour_net is True
        assert router.net_class_map["GNDA"] != NET_CLASS_ANALOG


# =============================================================================
# AC #6: Forwarding through run_route_command
# =============================================================================


def _forwarding_args(**overrides) -> SimpleNamespace:
    """Minimal args namespace for run_route_command forwarding tests."""
    base: dict[str, object] = {
        "pcb": "test.kicad_pcb",
        "output": None,
        "strategy": "negotiated",
        "skip_nets": None,
        "grid": "auto",
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
        "export_failed_nets": None,
        "differential_pairs": False,
        "diffpair_spacing": None,
        "diffpair_max_delta": None,
        "length_match_diffpairs": False,
        "length_match_groups": False,
        "strict": False,
        "net_class_map": None,
        "analog_nets": None,
        "auto_analog": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestForwarding:
    """``run_route_command`` forwards the flags when set, omits otherwise."""

    def test_analog_nets_forwarded_when_set(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _forwarding_args(analog_nets="AUDIO_L,AUDIO_R")
        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)
            call_args = mock_main.call_args[0][0]
            assert "--analog-nets" in call_args
            idx = call_args.index("--analog-nets")
            assert call_args[idx + 1] == "AUDIO_L,AUDIO_R"
            assert "--auto-analog" not in call_args

    def test_auto_analog_forwarded_when_set(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _forwarding_args(auto_analog=True)
        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)
            call_args = mock_main.call_args[0][0]
            assert "--auto-analog" in call_args
            assert "--analog-nets" not in call_args

    def test_flags_not_forwarded_when_absent(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _forwarding_args()
        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)
            call_args = mock_main.call_args[0][0]
            assert "--analog-nets" not in call_args
            assert "--auto-analog" not in call_args


# =============================================================================
# AC #7: No-op when absent + edge cases
# =============================================================================


class TestNoOpAndEdgeCases:
    """The feature is a strict no-op when absent and degrades gracefully."""

    def test_no_flags_leaves_map_and_ordering_unchanged(self):
        from kicad_tools.cli.route_cmd import _apply_analog_net_class

        router = _make_router()
        nets = _populate_synthetic_board(router)
        baseline_map = dict(router.net_class_map)
        baseline_order = {name: router._get_net_priority(nid)[0] for name, nid in nets.items()}

        _apply_analog_net_class(router, _analog_args(), quiet=True)

        assert router.net_class_map == baseline_map
        after_order = {name: router._get_net_priority(nid)[0] for name, nid in nets.items()}
        assert after_order == baseline_order

    def test_empty_analog_nets_string_is_noop(self):
        from kicad_tools.cli.route_cmd import _apply_analog_net_class

        router = _make_router()
        _populate_synthetic_board(router)
        baseline = dict(router.net_class_map)

        _apply_analog_net_class(router, _analog_args(analog_nets=""), quiet=True)

        assert router.net_class_map == baseline

    def test_unknown_net_name_ignored(self):
        from kicad_tools.cli.route_cmd import _apply_analog_net_class

        router = _make_router()
        _populate_synthetic_board(router)

        # NOT_A_NET is not on the board; AUDIO_L is.
        _apply_analog_net_class(router, _analog_args(analog_nets="NOT_A_NET,AUDIO_L"), quiet=True)

        assert "NOT_A_NET" not in router.net_class_map
        assert router.net_class_map["AUDIO_L"] == NET_CLASS_ANALOG

    def test_duplicate_explicit_and_auto_deduped(self):
        """AUDIO_L appearing in both the explicit list and auto-detect is fine."""
        from kicad_tools.cli.route_cmd import _apply_analog_net_class

        router = _make_router()
        _populate_synthetic_board(router)

        _apply_analog_net_class(
            router,
            _analog_args(analog_nets="AUDIO_L", auto_analog=True),
            quiet=True,
        )

        assert router.net_class_map["AUDIO_L"] == NET_CLASS_ANALOG


# =============================================================================
# NET_CLASS_ANALOG constant contract
# =============================================================================


class TestAnalogClassConstant:
    """The boosted class has the documented priority/cost contract."""

    def test_priority_and_cost_boost(self):
        assert NET_CLASS_ANALOG.priority == 2
        assert NET_CLASS_ANALOG.priority < NET_CLASS_DIGITAL.priority
        assert NET_CLASS_ANALOG.cost_multiplier == 0.85
        assert NET_CLASS_ANALOG.cost_multiplier <= 0.9
        assert NET_CLASS_ANALOG.noise_sensitive is True

    def test_not_a_pour_net(self):
        """The analog class is a pathfinder net, never a pour net."""
        assert NET_CLASS_ANALOG.is_pour_net is False
        assert NET_CLASS_ANALOG.route_via == "pathfinder"
