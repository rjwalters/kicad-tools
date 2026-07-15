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


# =============================================================================
# Issue #4149: hierarchical '/' prefix normalization + zero-match diagnostic
# =============================================================================


def _stub_router(net_names: dict[int, str]) -> SimpleNamespace:
    """A lightweight router stand-in for ``_apply_net_class_map_sidecar``.

    The helper only touches ``router.net_names`` (board net names) and
    ``router.net_class_map`` (the mutable overrides dict), so we avoid
    constructing a full Autorouter for these fast, deterministic tests.
    """
    return SimpleNamespace(net_names=dict(net_names), net_class_map={})


def _sidecar_entry(name: str, **fields):
    """Build a ``NetClassRouting`` for a synthetic sidecar entry."""
    from kicad_tools.router.rules import NetClassRouting

    return NetClassRouting.from_dict({"name": name, **fields})


class TestHierarchicalPrefixNormalization:
    """Bare sidecar keys must resolve against '/'-prefixed board nets.

    Mirrors the softstart-rev-B incident: label-derived nets carry KiCad's
    root-sheet prefix (``/FUSED_LINE``) while power-symbol nets stay bare
    (``GND``).  A bare-keyed sidecar previously matched zero prefixed nets
    silently; the fix normalizes on the sheet-local suffix and warns on
    genuine misconfiguration.
    """

    def test_bare_key_matches_prefixed_net(self, capsys):
        """AC #1: a bare key resolves to the '/'-prefixed board net, no warning."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        router = _stub_router({1: "/FUSED_LINE", 2: "/PGND", 3: "GND", 4: "+3.3V"})
        loaded = {
            "FUSED_LINE": _sidecar_entry("Heavy", priority=5),
            "PGND": _sidecar_entry("Heavy", priority=5),
            "GND": _sidecar_entry("Power", priority=4),
        }
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        _apply_net_class_map_sidecar(router, args, quiet=True)

        # Overrides landed under the board's actual (prefixed) net names,
        # which is what core.py's ``net_class_map.get(net_name)`` looks up.
        assert router.net_class_map["/FUSED_LINE"].priority == 5
        assert router.net_class_map["/PGND"].priority == 5
        assert router.net_class_map["GND"].priority == 4
        # Bare keys must NOT leak into the map when a prefixed net matched.
        assert "FUSED_LINE" not in router.net_class_map
        assert "PGND" not in router.net_class_map

        # No misconfiguration warning when everything resolves.
        err = capsys.readouterr().err
        assert "WARNING" not in err

    def test_exact_bare_match_unchanged(self, capsys):
        """AC #5: bare key vs bare board net still resolves (no regression)."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        router = _stub_router({1: "GND", 2: "+3.3V"})
        loaded = {"GND": _sidecar_entry("Power", priority=4)}
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        _apply_net_class_map_sidecar(router, args, quiet=True)

        assert router.net_class_map["GND"].priority == 4
        assert "WARNING" not in capsys.readouterr().err

    def test_zero_match_warns_with_nearest_hint(self, capsys):
        """AC #2: a typo key warns with a nearest-name hint; others stay silent."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        router = _stub_router({1: "/FUSED_LINE", 2: "GND"})
        loaded = {
            "FUSED_LINE": _sidecar_entry("Heavy", priority=5),
            "FUSED_LIN": _sidecar_entry("Heavy", priority=5),  # typo
        }
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        _apply_net_class_map_sidecar(router, args, quiet=True)

        # The good key still applied.
        assert router.net_class_map["/FUSED_LINE"].priority == 5
        # The typo did not.
        assert "FUSED_LIN" not in router.net_class_map

        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "1/2 entries matched" in err
        assert "FUSED_LIN" in err
        assert "/FUSED_LINE" in err  # nearest-name hint

    def test_full_zero_match_aggregate_warning(self, capsys):
        """AC #3: all-bare keys vs all-prefixed nets -> aggregate warning line."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        # Simulate a genuinely unresolvable sidecar: bare keys with no
        # matching suffix on the board at all.
        router = _stub_router({1: "/SHEET/OTHER_A", 2: "/SHEET/OTHER_B"})
        loaded = {
            "MISSING_A": _sidecar_entry("Heavy", priority=5),
            "MISSING_B": _sidecar_entry("Heavy", priority=5),
        }
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        _apply_net_class_map_sidecar(router, args, quiet=True)

        assert router.net_class_map == {}
        err = capsys.readouterr().err
        assert "0/2 entries matched" in err

    def test_ambiguous_key_applied_to_neither(self, capsys):
        """AC #4: bare key matching both /A and A -> ambiguous warning, no apply."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        router = _stub_router({1: "/A", 2: "A", 3: "GND"})
        loaded = {
            "A": _sidecar_entry("Heavy", priority=5),
            "GND": _sidecar_entry("Power", priority=4),
        }
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        _apply_net_class_map_sidecar(router, args, quiet=True)

        # Neither candidate for the ambiguous key gets the override.
        assert "A" not in router.net_class_map
        assert "/A" not in router.net_class_map
        # The unambiguous key still resolves.
        assert router.net_class_map["GND"].priority == 4

        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "AMBIGUOUS" in err
        assert "/A" in err and "A" in err

    def test_warning_not_suppressed_by_quiet(self, capsys):
        """AC: --quiet must NOT suppress the misconfiguration warning."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        router = _stub_router({1: "/FUSED_LINE"})
        loaded = {"TYPO_KEY": _sidecar_entry("Heavy", priority=5)}
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        # quiet=True is the softstart-rev-B condition; the warning must
        # still reach stderr.
        _apply_net_class_map_sidecar(router, args, quiet=True)

        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "TYPO_KEY" in err

    def test_no_op_when_flag_absent(self, capsys):
        """No sidecar loaded -> no changes, no output."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        router = _stub_router({1: "/FUSED_LINE"})
        args = SimpleNamespace(_loaded_net_class_map=None)

        _apply_net_class_map_sidecar(router, args, quiet=True)

        assert router.net_class_map == {}
        assert capsys.readouterr().err == ""

    def test_user_supplied_prefix_still_matches(self, capsys):
        """A user who writes the full '/'-prefixed key still resolves exactly."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        router = _stub_router({1: "/FUSED_LINE"})
        loaded = {"/FUSED_LINE": _sidecar_entry("Heavy", priority=5)}
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        _apply_net_class_map_sidecar(router, args, quiet=True)

        assert router.net_class_map["/FUSED_LINE"].priority == 5
        assert "WARNING" not in capsys.readouterr().err
