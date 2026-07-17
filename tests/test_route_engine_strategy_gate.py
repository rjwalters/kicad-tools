"""Tests for the ``--route-engine mesh|lattice`` compatibility gate (#4280).

``--route-engine mesh|lattice`` used to be SILENTLY INERT under the default
``--strategy negotiated``: ``route_all_negotiated`` -> ``_route_net_negotiated``
builds a grid ``NegotiatedRouter`` directly and never consults the engine
selector, so a "lattice" run shipped grid copper identical-except-UUIDs to a
grid run (the judge's board-02 repro).  Monte-carlo/evolutionary reach the
dispatch seam but are cache-vacuous, and --two-phase / --multi-resolution /
escape routing bypass or corrupt it.

``kct route`` therefore hard-errors (exit 2) BEFORE any board loading when
``--route-engine != grid`` is combined with any strategy/modifier other than
``--strategy basic``, and force-disables escape auto-detect (with a notice)
for the supported combination.  ``--route-engine grid`` is a strict no-op
through the gate.

Boards are built fully synthetically (gr_rect Edge.Cuts outline + one 0402),
mirroring ``test_route_offboard_gate.py``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from kicad_tools.cli import main as kct_main
from kicad_tools.cli.route_cmd import (
    _resolve_escape_routing_flag,
    _validate_route_engine_strategy,
)
from kicad_tools.cli.route_cmd import main as route_main

BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "NET1")
  (gr_rect (start 100 100) (end 120 110)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 110 105)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
  )
)
"""


@pytest.fixture
def pcb(tmp_path: Path) -> Path:
    board = tmp_path / "gate.kicad_pcb"
    board.write_text(BOARD)
    return board


def _gate_args(**overrides) -> argparse.Namespace:
    """Namespace with the gate-relevant flags at their parser defaults."""
    base = {
        "route_engine": "grid",
        "strategy": "negotiated",
        "two_phase": False,
        "multi_resolution": False,
        "escape_routing": None,
        "no_escape_routing": False,
        "quiet": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# Every incompatible strategy/modifier combination (acceptance 1/5): each must
# hard-error at the CLI boundary -- no combination may silently ship grid
# copper labeled as mesh/lattice output.
INCOMPATIBLE_COMBOS = [
    pytest.param([], id="default-negotiated"),
    pytest.param(["--strategy", "negotiated"], id="explicit-negotiated"),
    pytest.param(["--strategy", "monte-carlo"], id="monte-carlo"),
    pytest.param(["--strategy", "evolutionary"], id="evolutionary"),
    pytest.param(["--strategy", "basic", "--two-phase"], id="basic-two-phase"),
    pytest.param(
        ["--strategy", "basic", "--multi-resolution"],
        id="basic-multi-resolution",
    ),
    pytest.param(
        ["--strategy", "basic", "--escape-routing"],
        id="basic-explicit-escape",
    ),
    pytest.param(
        ["--strategy", "negotiated", "--two-phase"],
        id="negotiated-two-phase",
    ),
]


class TestIncompatibleCombosHardError:
    """Acceptance 1 + 5: every (engine != grid) x incompatible combo fails loudly."""

    @pytest.mark.parametrize("engine", ["mesh", "lattice"])
    @pytest.mark.parametrize("extra", INCOMPATIBLE_COMBOS)
    def test_hard_error_no_output(self, pcb: Path, capsys, engine: str, extra: list[str]):
        rc = route_main([str(pcb), "--route-engine", engine, *extra])
        assert rc == 2
        err = capsys.readouterr().err
        assert f"--route-engine {engine}" in err
        assert "--strategy basic" in err
        assert "#4280" in err
        # The gate fires before ANY board loading or routing: no output file
        # (default <input>_routed.kicad_pcb or anything else) may be written.
        outputs = [p for p in pcb.parent.iterdir() if p != pcb]
        assert outputs == [], f"gate must not write outputs, found {outputs}"

    @pytest.mark.parametrize("engine", ["mesh", "lattice"])
    def test_gate_fires_before_file_validation(self, tmp_path: Path, capsys, engine: str):
        """Flag incompatibility is reported even for a nonexistent board --
        proof the gate runs before any filesystem/board work."""
        rc = route_main([str(tmp_path / "missing.kicad_pcb"), "--route-engine", engine])
        assert rc == 2
        err = capsys.readouterr().err
        assert "--strategy basic" in err
        assert "not found" not in err

    def test_outer_kct_entry_hard_errors(self, pcb: Path, capsys):
        """The real ``kct route`` dispatch path funnels through the same gate
        (``commands/routing.py`` forwards --route-engine and --strategy)."""
        rc = kct_main(["route", str(pcb), "--route-engine", "lattice"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "--route-engine lattice" in err
        assert "--strategy basic" in err

    def test_error_names_all_conflicts(self, pcb: Path, capsys):
        rc = route_main(
            [
                str(pcb),
                "--route-engine",
                "lattice",
                "--strategy",
                "monte-carlo",
                "--two-phase",
                "--multi-resolution",
                "--escape-routing",
            ]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "--strategy monte-carlo" in err
        assert "--two-phase" in err
        assert "--multi-resolution" in err
        assert "--escape-routing" in err


class TestGridIsUntouched:
    """Acceptance 4: the gate is a strict no-op for --route-engine grid."""

    def test_validator_noop_for_grid(self, capsys):
        """Grid returns 0 immediately and mutates NOTHING -- even with every
        incompatible modifier set (byte-identical default path)."""
        args = _gate_args(
            route_engine="grid",
            strategy="monte-carlo",
            two_phase=True,
            multi_resolution=True,
            escape_routing=True,
        )
        before = vars(args).copy()
        assert _validate_route_engine_strategy(args) == 0
        assert vars(args) == before
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    @pytest.mark.parametrize(
        "extra",
        [
            [],
            ["--route-engine", "grid"],
            ["--route-engine", "grid", "--strategy", "monte-carlo"],
            ["--route-engine", "grid", "--two-phase", "--escape-routing"],
        ],
    )
    def test_grid_cli_passes_gate(self, pcb: Path, capsys, extra: list[str]):
        """Grid runs (implicit or explicit) never trip the gate (--dry-run
        short-circuits before routing, as in test_route_offboard_gate.py)."""
        rc = route_main([str(pcb), "--dry-run", *extra])
        assert rc == 0
        captured = capsys.readouterr()
        assert "#4280" not in captured.err
        assert "Escape routing: auto-detect disabled" not in captured.out


class TestEscapeAutoDetectSuppression:
    """Acceptance 3: escape AUTO-detect is forced off (with notice) for
    engine != grid + --strategy basic."""

    @pytest.mark.parametrize("engine", ["mesh", "lattice"])
    def test_auto_escape_forced_off_with_notice(self, capsys, engine: str):
        args = _gate_args(route_engine=engine, strategy="basic")
        assert _resolve_escape_routing_flag(args) is None  # auto-detect
        assert _validate_route_engine_strategy(args) == 0
        # All dispatch sites resolve the tri-state through
        # _resolve_escape_routing_flag, so this single stamp guarantees the
        # auto-detect branch in _should_use_escape_routing is unreachable.
        assert _resolve_escape_routing_flag(args) is False
        out = capsys.readouterr().out
        assert "Escape routing: auto-detect disabled" in out
        assert engine in out

    def test_explicit_no_escape_no_notice(self, capsys):
        args = _gate_args(route_engine="lattice", strategy="basic", no_escape_routing=True)
        assert _validate_route_engine_strategy(args) == 0
        assert "Escape routing" not in capsys.readouterr().out

    def test_quiet_suppresses_notice_but_still_forces_off(self, capsys):
        args = _gate_args(route_engine="lattice", strategy="basic", quiet=True)
        assert _validate_route_engine_strategy(args) == 0
        assert _resolve_escape_routing_flag(args) is False
        assert capsys.readouterr().out == ""


class TestWorkingCombosEndToEnd:
    """Acceptance 2: --strategy basic + engine still routes end-to-end."""

    def _route(self, pcb: Path, engine: str) -> Path:
        out = pcb.parent / f"routed_{engine}.kicad_pcb"
        rc = route_main(
            [
                str(pcb),
                "--route-engine",
                engine,
                "--strategy",
                "basic",
                "--layers",
                "2",
                "--skip-drc",
                "-o",
                str(out),
            ]
        )
        assert rc == 0
        assert out.exists()
        assert "(segment" in out.read_text()
        return out

    def test_lattice_basic_routes(self, pcb: Path, capsys):
        self._route(pcb, "lattice")
        out = capsys.readouterr().out
        # The gate ran (engine recognized), suppressed auto-escape, and
        # routing completed through the lattice engine.
        assert "Escape routing: auto-detect disabled" in out

    def test_mesh_basic_routes(self, pcb: Path, capsys):
        pytest.importorskip("kicad_tools.router.router_cpp")
        self._route(pcb, "mesh")
        out = capsys.readouterr().out
        assert "Escape routing: auto-detect disabled" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
