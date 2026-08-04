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


# =============================================================================
# Issue #4622: preserved (non-routed) nets are resolvable on a filtered pass
# =============================================================================


def _preserved_route(net: int, net_name: str):
    """A minimal preserved ``Route`` as ``load_pcb_for_routing`` builds them."""
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    return Route(
        net=net,
        net_name=net_name,
        segments=[Segment(x1=0.0, y1=0.0, x2=1.0, y2=0.0, width=0.2, net=net, layer=Layer.F_CU)],
    )


def _stub_router_with_preserved(
    net_names: dict[int, str], preserved: dict[int, str]
) -> SimpleNamespace:
    """``_stub_router`` plus the ``existing_routes`` a preserved pass carries.

    ``net_names`` is the *routable* set (what survives ``--nets`` /
    ``--skip-nets``); ``preserved`` is the ``{net_id: net_name}`` of copper
    reloaded from the input board.
    """
    return SimpleNamespace(
        net_names=dict(net_names),
        net_class_map={},
        existing_routes=[_preserved_route(n, name) for n, name in preserved.items()],
    )


class TestPreservedNetResolutionDomain:
    """A filtered pass must still resolve sidecar keys for PRESERVED nets.

    Under ``--nets`` / ``--skip-nets`` the loader rewrites every skipped net's
    pads to ``net_num = 0``, so those nets never enter ``router.net_names``.
    Resolving only against ``net_names`` therefore dropped every sidecar entry
    naming a preserved net (``merged 0/N sidecar entries``) and preserved
    copper fell back to the DRU floor.

    The fix resolves in **two phases** -- routable first, then the still
    unmatched keys against the preserved-only names -- which is strictly
    additive and so cannot turn an already-resolved key ambiguous.
    """

    def test_preserved_only_key_resolves(self, capsys):
        """The reported defect: a key naming a preserved net now applies."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        router = _stub_router_with_preserved({5: "NODE_A"}, {1: "LINE_A", 2: "LINE_B"})
        loaded = {
            "LINE_A": _sidecar_entry("HV", clearance=1.0),
            "LINE_B": _sidecar_entry("HV", clearance=1.0),
        }
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        _apply_net_class_map_sidecar(router, args, quiet=True)

        assert router.net_class_map["LINE_A"].clearance == pytest.approx(1.0)
        assert router.net_class_map["LINE_B"].clearance == pytest.approx(1.0)
        assert "WARNING" not in capsys.readouterr().err

    def test_merged_count_reports_preserved_matches(self, capsys):
        """The ``merged N/M`` line counts preserved matches (was ``0/4``)."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        router = _stub_router_with_preserved(
            {5: "NODE_A"}, {1: "LINE_A", 2: "LINE_B", 3: "LINE_C", 4: "LINE_D"}
        )
        loaded = {
            name: _sidecar_entry("HV", clearance=1.0)
            for name in ("LINE_A", "LINE_B", "LINE_C", "LINE_D")
        }
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        _apply_net_class_map_sidecar(router, args, quiet=False)

        out = capsys.readouterr()
        assert "merged 4/4 sidecar entries" in out.out
        assert "WARNING" not in out.err

    def test_routable_key_still_resolves(self, capsys):
        """A key that resolved before the change still resolves identically."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        router = _stub_router_with_preserved({5: "/NODE_A"}, {1: "LINE_A"})
        loaded = {"NODE_A": _sidecar_entry("Sig", priority=3)}
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        _apply_net_class_map_sidecar(router, args, quiet=True)

        assert router.net_class_map["/NODE_A"].priority == 3
        assert "WARNING" not in capsys.readouterr().err

    def test_bare_key_matches_prefixed_preserved_net(self, capsys):
        """Hierarchical preserved names ('/FUSED_LINE') take a bare key too."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        router = _stub_router_with_preserved({5: "NODE_A"}, {1: "/FUSED_LINE"})
        loaded = {"FUSED_LINE": _sidecar_entry("Heavy", priority=5)}
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        _apply_net_class_map_sidecar(router, args, quiet=True)

        assert router.net_class_map["/FUSED_LINE"].priority == 5
        assert "FUSED_LINE" not in router.net_class_map
        assert "WARNING" not in capsys.readouterr().err

    def test_key_matching_neither_domain_still_warns(self, capsys):
        """Hazard 3: widening the domain must not silence genuine typos."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        router = _stub_router_with_preserved({5: "NODE_A"}, {1: "LINE_A"})
        loaded = {
            "LINE_A": _sidecar_entry("HV", clearance=1.0),
            "LINE_TYPO": _sidecar_entry("HV", clearance=1.0),
        }
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        # quiet=True is the softstart-rev-B condition: the warning must
        # still reach stderr even with --quiet.
        _apply_net_class_map_sidecar(router, args, quiet=True)

        assert router.net_class_map["LINE_A"].clearance == pytest.approx(1.0)
        assert "LINE_TYPO" not in router.net_class_map
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "1/2 entries matched" in err
        assert "LINE_TYPO" in err

    def test_unmatched_hint_can_name_a_preserved_net(self, capsys):
        """Nearest-name hints draw on BOTH domains, so preserved nets show up."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        router = _stub_router_with_preserved({5: "NODE_A"}, {1: "/HV/FUSED_LINE"})
        loaded = {"FUSED_LIN": _sidecar_entry("Heavy", priority=5)}  # typo
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        _apply_net_class_map_sidecar(router, args, quiet=True)

        err = capsys.readouterr().err
        assert "0/1 entries matched" in err
        assert "/HV/FUSED_LINE" in err

    def test_suffix_collision_does_not_regress_routable_match(self, capsys):
        """Hazard 2: a routable match must NOT become ambiguous.

        ``resolve_net_key`` matches on the suffix after the last ``/``, so a
        naive union of the routable and preserved domains would make the bare
        key ``LINE`` ambiguous between routable ``/HV/LINE`` and preserved
        ``/LV/LINE`` -- and ambiguous keys are applied to *neither* net.  That
        would silently drop the clearance of a net that IS being routed this
        pass.  Two-phase resolution keeps the phase-1 match.
        """
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        router = _stub_router_with_preserved({5: "/HV/LINE"}, {1: "/LV/LINE"})
        loaded = {"LINE": _sidecar_entry("HV", clearance=2.0)}
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        _apply_net_class_map_sidecar(router, args, quiet=True)

        # The routable net keeps its clearance; the preserved net is untouched.
        assert router.net_class_map["/HV/LINE"].clearance == pytest.approx(2.0)
        assert "/LV/LINE" not in router.net_class_map
        err = capsys.readouterr().err
        assert "AMBIGUOUS" not in err
        assert "WARNING" not in err

    def test_ambiguous_within_preserved_domain_applied_to_neither(self, capsys):
        """A key ambiguous among PRESERVED nets is still applied to neither."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        router = _stub_router_with_preserved({5: "NODE_A"}, {1: "/HV/LINE", 2: "/LV/LINE"})
        loaded = {"LINE": _sidecar_entry("HV", clearance=2.0)}
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        _apply_net_class_map_sidecar(router, args, quiet=True)

        assert "/HV/LINE" not in router.net_class_map
        assert "/LV/LINE" not in router.net_class_map
        err = capsys.readouterr().err
        assert "AMBIGUOUS" in err
        assert "0/1 entries matched" in err

    def test_missing_existing_routes_attribute_tolerated(self, capsys):
        """Hazard 1: a router with no ``existing_routes`` must not blow up."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        router = _stub_router({1: "/FUSED_LINE"})  # no existing_routes attribute
        assert not hasattr(router, "existing_routes")
        loaded = {"FUSED_LINE": _sidecar_entry("Heavy", priority=5)}
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        _apply_net_class_map_sidecar(router, args, quiet=True)

        assert router.net_class_map["/FUSED_LINE"].priority == 5
        assert "WARNING" not in capsys.readouterr().err

    def test_empty_existing_routes_is_a_no_op(self, capsys):
        """An unfiltered pass (no preserved copper) behaves exactly as before."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        router = _stub_router_with_preserved({1: "/FUSED_LINE"}, {})
        loaded = {
            "FUSED_LINE": _sidecar_entry("Heavy", priority=5),
            "TYPO": _sidecar_entry("Heavy", priority=5),
        }
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        _apply_net_class_map_sidecar(router, args, quiet=True)

        assert router.net_class_map["/FUSED_LINE"].priority == 5
        err = capsys.readouterr().err
        assert "1/2 entries matched" in err

    def test_preserved_net_also_routable_is_not_double_counted(self, capsys):
        """A net that is BOTH routable and preserved resolves once, via phase 1."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        # ``--preserve-existing`` without a net filter loads every net's copper,
        # so ``existing_routes`` and ``net_names`` overlap completely.
        router = _stub_router_with_preserved({1: "LINE_A"}, {1: "LINE_A"})
        loaded = {"LINE_A": _sidecar_entry("HV", clearance=1.0)}
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        _apply_net_class_map_sidecar(router, args, quiet=False)

        out = capsys.readouterr()
        assert router.net_class_map["LINE_A"].clearance == pytest.approx(1.0)
        assert "merged 1/1 sidecar entries" in out.out
        assert "WARNING" not in out.err

    def test_route_without_net_name_is_skipped(self, capsys):
        """A preserved ``Route`` carrying no ``net_name`` never enters the domain."""
        from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar

        router = _stub_router_with_preserved({5: "NODE_A"}, {1: "LINE_A"})
        router.existing_routes[0].net_name = None
        loaded = {"LINE_A": _sidecar_entry("HV", clearance=1.0)}
        args = SimpleNamespace(_loaded_net_class_map=loaded)

        _apply_net_class_map_sidecar(router, args, quiet=True)

        assert router.net_class_map == {}
        assert "0/1 entries matched" in capsys.readouterr().err


# =============================================================================
# Issue #4587: KiCad layer NAMES in the sidecar resolve at preload
# =============================================================================

# 4-layer variant of MINIMAL_PCB.  ``--layers auto`` must detect four copper
# layers so that "B.Cu" resolves to grid index 3 (NOT CopperLayer.B_CU == 5).
MINIMAL_PCB_4LAYER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
    (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "PGND")
  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
)
"""


@pytest.fixture
def minimal_pcb_4layer(tmp_path: Path) -> Path:
    p = tmp_path / "minimal4.kicad_pcb"
    p.write_text(MINIMAL_PCB_4LAYER)
    return p


def _run_route_preload(pcb: Path, sidecar: Path, tmp_path: Path, *extra_args: str) -> int:
    """Drive ``route_cmd.main`` far enough to exercise the sidecar preload."""
    from kicad_tools.cli.route_cmd import main as route_main

    return route_main(
        [
            str(pcb),
            "--dry-run",
            "--quiet",
            "--net-class-map",
            str(sidecar),
            "--output",
            str(tmp_path / "out.kicad_pcb"),
            *extra_args,
        ]
    )


class TestLayerNamePreloadResolution:
    """A layer-name sidecar is normalized at preload, before any routing.

    Regression for #4587: ``kct route --route-engine grid`` died on the first
    net with ``invalid literal for int() with base 10: 'In1.Cu'`` -- AFTER the
    escape and global phases had already burned minutes -- whenever an
    ampacity-bearing class declared its ``avoid_layers`` as KiCad layer names.
    """

    def _capture_preload(self, pcb: Path, sidecar: Path, tmp_path: Path, *extra_args: str):
        """Run the preload with a spy that records the resolved map + stack.

        The spy delegates to the real ``net_class_map_from_dict``, so this is a
        genuine end-to-end check of the CLI plumbing (stack selection + kwarg
        forwarding), not a mock of the code under test.
        """
        from kicad_tools.router import rules as _rules

        real = _rules.net_class_map_from_dict
        captured: dict = {}

        def spy(data, layer_stack=None):
            result = real(data, layer_stack=layer_stack)
            captured["layer_stack"] = layer_stack
            captured["map"] = result
            return result

        with patch.object(_rules, "net_class_map_from_dict", spy):
            rc = _run_route_preload(pcb, sidecar, tmp_path, *extra_args)
        return rc, captured

    def test_ampacity_class_with_layer_names_does_not_crash(
        self, minimal_pcb_4layer: Path, tmp_path: Path, capsys
    ):
        """The exact repro shape: names + ``target_ampacity`` on a 4-layer board."""
        sidecar = tmp_path / "ncm.json"
        sidecar.write_text(
            json.dumps(
                {
                    "PGND": {
                        "name": "HV",
                        "preferred_layers": ["F.Cu", "B.Cu"],
                        "avoid_layers": ["In1.Cu", "In2.Cu"],
                        "target_ampacity": 15.0,
                    }
                }
            )
        )
        rc, captured = self._capture_preload(minimal_pcb_4layer, sidecar, tmp_path)

        assert rc != 1 or "invalid literal for int()" not in capsys.readouterr().err
        nc = captured["map"]["PGND"]
        # Names are gone before any engine touches the map.
        assert nc.avoid_layers == [1, 2]
        # B.Cu is the board's LAST copper index on this 4-layer stack.
        assert nc.preferred_layers == [0, 3]
        # And the #4587 crash site is now clean.
        assert nc.hard_avoided_layer_indices() == frozenset({1, 2})

    def test_auto_detected_stack_resolves_b_cu_to_three(
        self, minimal_pcb_4layer: Path, tmp_path: Path
    ):
        """``--layers auto`` detects the board's four copper layers."""
        sidecar = tmp_path / "ncm.json"
        sidecar.write_text(json.dumps({"PGND": {"name": "HV", "avoid_layers": ["B.Cu"]}}))
        _rc, captured = self._capture_preload(
            minimal_pcb_4layer, sidecar, tmp_path, "--layers", "auto"
        )
        assert captured["layer_stack"] is not None
        assert captured["layer_stack"].num_layers == 4
        assert captured["map"]["PGND"].avoid_layers == [3]

    def test_explicit_layers_flag_selects_the_stack(self, minimal_pcb_4layer: Path, tmp_path: Path):
        """``--layers 2`` resolves B.Cu to 1; ``--layers 6`` resolves it to 5."""
        sidecar = tmp_path / "ncm.json"
        sidecar.write_text(json.dumps({"PGND": {"name": "HV", "avoid_layers": ["B.Cu"]}}))

        _rc, two = self._capture_preload(minimal_pcb_4layer, sidecar, tmp_path, "--layers", "2")
        assert two["map"]["PGND"].avoid_layers == [1]

        _rc, six = self._capture_preload(minimal_pcb_4layer, sidecar, tmp_path, "--layers", "6")
        assert six["map"]["PGND"].avoid_layers == [5]

    def test_mixed_int_and_name_forms_in_one_file(self, minimal_pcb_4layer: Path, tmp_path: Path):
        """A file mixing ``[3]`` and ``["In1.Cu", "In2.Cu"]`` loads."""
        sidecar = tmp_path / "ncm.json"
        sidecar.write_text(
            json.dumps(
                {
                    "PGND": {
                        "name": "HV",
                        "preferred_layers": [3],
                        "avoid_layers": ["In1.Cu", "In2.Cu"],
                        "target_ampacity": 15.0,
                    }
                }
            )
        )
        _rc, captured = self._capture_preload(minimal_pcb_4layer, sidecar, tmp_path)
        assert captured["map"]["PGND"].preferred_layers == [3]
        assert captured["map"]["PGND"].avoid_layers == [1, 2]

    def test_unrecognized_token_exits_1_at_preload(
        self, minimal_pcb_4layer: Path, tmp_path: Path, capsys
    ):
        """A bogus layer token fails at preload with the structured message."""
        sidecar = tmp_path / "ncm.json"
        sidecar.write_text(json.dumps({"PGND": {"name": "HV", "avoid_layers": ["Top"]}}))

        rc = _run_route_preload(minimal_pcb_4layer, sidecar, tmp_path)

        assert rc == 1
        err = capsys.readouterr().err
        assert "invalid net-class-map structure" in err
        assert "Top" in err  # names the offending value
        assert "PGND" in err  # names the owning net-class key

    def test_layer_absent_from_stack_exits_1(
        self, minimal_pcb_4layer: Path, tmp_path: Path, capsys
    ):
        """``In3.Cu`` on a 4-layer board is a misconfiguration, not layer 3."""
        sidecar = tmp_path / "ncm.json"
        sidecar.write_text(json.dumps({"PGND": {"name": "HV", "avoid_layers": ["In3.Cu"]}}))

        rc = _run_route_preload(minimal_pcb_4layer, sidecar, tmp_path)

        assert rc == 1
        err = capsys.readouterr().err
        assert "invalid net-class-map structure" in err
        assert "In3.Cu" in err

    def test_integer_sidecar_is_unchanged(self, minimal_pcb_4layer: Path, tmp_path: Path):
        """No-op guard: an integer-valued sidecar preloads exactly as before."""
        sidecar = tmp_path / "ncm.json"
        sidecar.write_text(
            json.dumps(
                {
                    "PGND": {
                        "name": "HV",
                        "preferred_layers": [0, 3],
                        "avoid_layers": [1, 2],
                        "target_ampacity": 15.0,
                    }
                }
            )
        )
        _rc, captured = self._capture_preload(minimal_pcb_4layer, sidecar, tmp_path)
        assert captured["map"]["PGND"].preferred_layers == [0, 3]
        assert captured["map"]["PGND"].avoid_layers == [1, 2]
