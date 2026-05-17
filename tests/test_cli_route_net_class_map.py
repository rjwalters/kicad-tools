"""Tests for the ``kct route --net-class-map`` flag (Issue #2996).

The flag mirrors ``kct check --net-class-map``: it accepts a path to a
JSON sidecar (produced by :func:`net_class_map_to_dict`) and merges the
rich ``NetClassRouting`` declarations into the autorouter's
name-pattern-classified ``net_class_map`` at routing time.

Without this flag, ``--differential-pairs`` falls back to NetClassRouting
defaults; on board-07 (matchgroup-test) under JLCPCB tier-1 rules that
produces ~20K ``diffpair_clearance_intra`` violations because per-pair
``intra_pair_clearance`` overrides never reach the pathfinder.

Coverage:

1. **AC #1: flag declared on the route subcommand** in both the unified
   ``cli/parser.py`` parser and the standalone ``route_cmd.py`` parser.
2. **AC #6: missing file** returns exit 1 with a clear stderr message
   (parity with the ``kct check`` error paths).
3. **Forwarding**: ``run_route_command`` propagates the sidecar path to
   the inner ``route_cmd.main`` argv (mirrors the
   ``--length-match-groups`` plumbing pattern).
4. **Error paths**: malformed JSON and structurally-invalid sidecars
   return exit 1.
"""

from __future__ import annotations

import contextlib
import json
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# =============================================================================
# AC #1: Flag declared in both parsers
# =============================================================================


class TestFlagDefinedInBothParsers:
    """The flag must appear in both the unified and standalone parsers."""

    def test_net_class_map_in_unified_parser_help(self):
        """``kct route --help`` (unified parser) lists ``--net-class-map``."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                parser.parse_args(["route", "--help"])
        help_text = help_output.getvalue()
        assert "--net-class-map" in help_text

    def test_net_class_map_in_route_cmd_help(self):
        """``route_cmd.main(['--help'])`` lists ``--net-class-map``."""
        from kicad_tools.cli.route_cmd import main as route_main

        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                route_main(["--help"])
        help_text = help_output.getvalue()
        assert "--net-class-map" in help_text

    def test_net_class_map_parses_via_unified_parser(self):
        """``kct route --net-class-map PATH`` is parseable via the unified parser."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            ["route", "test.kicad_pcb", "--net-class-map", "/tmp/sidecar.json"]
        )
        assert args.net_class_map == "/tmp/sidecar.json"

    def test_net_class_map_default_none_in_unified_parser(self):
        """When omitted, ``net_class_map`` defaults to ``None``."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "test.kicad_pcb"])
        assert args.net_class_map is None


# =============================================================================
# Forwarding: run_route_command -> route_cmd.main
# =============================================================================


def _base_args(**overrides) -> SimpleNamespace:
    """Build a minimal args namespace mirroring ``run_route_command``'s needs."""
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
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestForwarding:
    """``run_route_command`` forwards the new flag when set, omits otherwise."""

    def test_net_class_map_forwarded_when_set(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _base_args(net_class_map="/tmp/sidecar.json")
        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)
            call_args = mock_main.call_args[0][0]
            assert "--net-class-map" in call_args
            idx = call_args.index("--net-class-map")
            assert call_args[idx + 1] == "/tmp/sidecar.json"

    def test_net_class_map_not_forwarded_when_none(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _base_args(net_class_map=None)
        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)
            call_args = mock_main.call_args[0][0]
            assert "--net-class-map" not in call_args


# =============================================================================
# AC #6: Error paths return exit 1
# =============================================================================


# Minimal PCB used to drive the route_cmd loader far enough that it
# encounters the --net-class-map flag.  Borrowed from
# tests/test_cli_check_net_class_map.py to keep this file self-contained.
MINIMAL_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
    (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "USB_D+")
  (net 2 "USB_D-")
  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
)
"""


@pytest.fixture
def minimal_pcb(tmp_path: Path) -> Path:
    p = tmp_path / "minimal.kicad_pcb"
    p.write_text(MINIMAL_PCB)
    return p


class TestNetClassMapErrorPaths:
    """The route_cmd loader rejects bad sidecars with exit 1."""

    def test_missing_file_returns_1(self, minimal_pcb: Path, capsys, tmp_path: Path):
        """Missing sidecar -> exit 1 with a clear stderr message (AC #6)."""
        from kicad_tools.cli.route_cmd import main as route_main

        missing = tmp_path / "does_not_exist.json"
        result = route_main(
            [
                str(minimal_pcb),
                "--dry-run",
                "--quiet",
                "--net-class-map",
                str(missing),
                "--output",
                str(tmp_path / "out.kicad_pcb"),
            ]
        )
        assert result == 1
        captured = capsys.readouterr()
        assert "net-class-map" in captured.err
        assert "not found" in captured.err

    def test_malformed_json_returns_1(self, minimal_pcb: Path, tmp_path: Path, capsys):
        """Malformed JSON -> exit 1 with parsing error on stderr."""
        from kicad_tools.cli.route_cmd import main as route_main

        bad = tmp_path / "bad.json"
        bad.write_text("not { valid json")
        result = route_main(
            [
                str(minimal_pcb),
                "--dry-run",
                "--quiet",
                "--net-class-map",
                str(bad),
                "--output",
                str(tmp_path / "out.kicad_pcb"),
            ]
        )
        assert result == 1
        captured = capsys.readouterr()
        assert "JSON" in captured.err or "parsing" in captured.err

    def test_invalid_structure_returns_1(self, minimal_pcb: Path, tmp_path: Path, capsys):
        """Dict-without-name entries -> exit 1 with invalid-structure stderr."""
        from kicad_tools.cli.route_cmd import main as route_main

        bad = tmp_path / "bad.json"
        # Entry missing the required 'name' field -> NetClassRouting.from_dict
        # raises ValueError -> error path returns 1.
        bad.write_text(json.dumps({"USB_D+": {"priority": 1}}))
        result = route_main(
            [
                str(minimal_pcb),
                "--dry-run",
                "--quiet",
                "--net-class-map",
                str(bad),
                "--output",
                str(tmp_path / "out.kicad_pcb"),
            ]
        )
        assert result == 1
        captured = capsys.readouterr()
        assert "net-class-map" in captured.err or "invalid" in captured.err.lower()


# =============================================================================
# Merge semantics: rich fields actually land on the router's net_class_map
# =============================================================================


class TestRouterNetClassMapMerge:
    """When the sidecar is supplied, the rich fields land on the router's map.

    This is the load-bearing contract for Issue #2996: without the merge
    onto ``router.net_class_map``, the per-pair / per-group fields
    (intra_pair_clearance, coupled_routing, length_match_group, ...) do
    not project through to the routing-time pathfinder.
    """

    def test_router_net_class_map_includes_sidecar_intra_pair_clearance(
        self,
        minimal_pcb: Path,
        tmp_path: Path,
    ):
        """After ``load_pcb_for_routing`` + sidecar merge, the router has
        the sidecar's ``intra_pair_clearance`` for USB_D+/USB_D-.

        We exercise the merge directly (rather than through the full
        ``route_main`` dispatch) to keep the test fast and deterministic.
        """
        from kicad_tools.router import DesignRules, load_pcb_for_routing
        from kicad_tools.router.rules import net_class_map_from_dict

        sidecar = {
            "USB_D+": {
                "name": "HighSpeed",
                "coupled_routing": True,
                "diffpair_partner": "USB_D-",
                "intra_pair_clearance": 0.10,
            },
            "USB_D-": {
                "name": "HighSpeed",
                "coupled_routing": True,
                "diffpair_partner": "USB_D+",
                "intra_pair_clearance": 0.10,
            },
        }
        sidecar_path = tmp_path / "ncm.json"
        sidecar_path.write_text(json.dumps(sidecar))

        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
        )
        router, _ = load_pcb_for_routing(
            str(minimal_pcb), skip_nets=[], rules=rules, validate_drc=False
        )

        # Mirror the route_cmd merge path.
        loaded = net_class_map_from_dict(json.loads(sidecar_path.read_text()))
        router.net_class_map.update(loaded)

        # The rich field is now present on the router's map.
        assert "USB_D+" in router.net_class_map
        assert router.net_class_map["USB_D+"].intra_pair_clearance == pytest.approx(0.10)
        assert router.net_class_map["USB_D-"].intra_pair_clearance == pytest.approx(0.10)
        assert router.net_class_map["USB_D+"].diffpair_partner == "USB_D-"
        assert router.net_class_map["USB_D-"].diffpair_partner == "USB_D+"
