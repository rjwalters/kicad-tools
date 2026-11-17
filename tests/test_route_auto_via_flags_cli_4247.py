"""CLI-surface tests for route-auto via-geometry flags (Issue #4247).

``kct route-auto`` must expose ``--via-drill``/``--via-diameter`` (parity with
``kct route``) and forward them into ``route_net_auto``.  Absent the flags, the
values default to ``None`` so route-auto keeps deriving via geometry from the
board's net-class via constraints instead of a hardcoded constant.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from kicad_tools.cli.commands.routing import run_route_auto_command
from kicad_tools.cli.parser import create_parser


def _args(**overrides):
    base = {
        "pcb": "/tmp/does-not-matter.kicad_pcb",
        "net": "NET1",
        "output": None,
        "strategy": "auto",
        "no_repair": False,
        "no_via_resolution": False,
        "dry_run": False,
        "region": None,
        "allow_partial": False,
        "via_drill": None,
        "via_diameter": None,
        "verbose": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# --- Parser surface --------------------------------------------------------


def test_parser_accepts_via_flags():
    parser = create_parser()
    args = parser.parse_args(
        [
            "route-auto",
            "board.kicad_pcb",
            "--net",
            "GND",
            "--via-drill",
            "0.3",
            "--via-diameter",
            "0.6",
        ]
    )
    assert args.via_drill == 0.3
    assert args.via_diameter == 0.6


def test_parser_via_flags_default_none():
    """Unset flags default to None so the board-derived value is not overridden."""
    parser = create_parser()
    args = parser.parse_args(["route-auto", "board.kicad_pcb", "--net", "GND"])
    assert args.via_drill is None
    assert args.via_diameter is None


# --- Forwarding into route_net_auto ---------------------------------------


def test_flags_forwarded_to_route_net_auto():
    success = {
        "success": True,
        "net_name": "NET1",
        "strategy_used": "global",
        "metrics": {},
        "warnings": [],
    }
    with patch("kicad_tools.mcp.tools.routing.route_net_auto", return_value=success) as mock_route:
        rc = run_route_auto_command(_args(via_drill=0.3, via_diameter=0.6))

    assert rc == 0
    _, kwargs = mock_route.call_args
    assert kwargs["via_drill"] == 0.3
    assert kwargs["via_diameter"] == 0.6


def test_omitting_flags_forwards_none():
    """No via flags => None passed through, preserving board-derived defaults."""
    success = {
        "success": True,
        "net_name": "NET1",
        "strategy_used": "global",
        "metrics": {},
        "warnings": [],
    }
    with patch("kicad_tools.mcp.tools.routing.route_net_auto", return_value=success) as mock_route:
        rc = run_route_auto_command(_args())

    assert rc == 0
    _, kwargs = mock_route.call_args
    assert kwargs["via_drill"] is None
    assert kwargs["via_diameter"] is None


# --- Dry-run reporting -----------------------------------------------------


def test_dry_run_reports_explicit_override(capsys):
    rc = run_route_auto_command(_args(dry_run=True, via_drill=0.3, via_diameter=0.6))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Via drill: 0.3mm (explicit override)" in out
    assert "Via diameter: 0.6mm (explicit override)" in out


def test_dry_run_reports_board_derived_default(tmp_path, capsys):
    """Without overrides, dry-run reports the board-derived via geometry."""
    pcb_file = tmp_path / "board.kicad_pcb"
    pcb_file.write_text(
        """(kicad_pcb
  (version 20240108)
  (net_class "Default" (via_dia 0.6) (via_drill 0.3))
  (net_class "Micro" (via_dia 0.45) (via_drill 0.2))
)"""
    )
    rc = run_route_auto_command(_args(pcb=str(pcb_file), dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    # max()-aggregated board default (0.6/0.3), not the smaller Micro class.
    assert "Via drill: 0.3mm (board-derived)" in out
    assert "Via diameter: 0.6mm (board-derived)" in out
