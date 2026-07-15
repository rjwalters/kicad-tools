"""CLI-surface tests for partial-route reporting (Issue #4165).

``run_route_auto_command`` must exit non-zero and print a
``partially routed ... k/n pads connected`` message when the orchestrator
demotes a multi-pad net to a partial route, rather than the old silent
``Routed net successfully`` + exit 0.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from kicad_tools.cli.commands.routing import run_route_auto_command


def _args(**overrides):
    base = {
        "pcb": "/tmp/does-not-matter.kicad_pcb",
        "net": "NET1",
        "output": None,
        "strategy": "global",
        "no_repair": False,
        "no_via_resolution": False,
        "dry_run": False,
        "region": None,
        "allow_partial": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_partial_result_exits_nonzero_and_reports_kn(capsys):
    partial = {
        "success": False,
        "partial": True,
        "net_name": "NET1",
        "pads_connected": 2,
        "pads_total": 3,
        "strategy_used": "GLOBAL_WITH_REPAIR",
        "warnings": [],
    }
    with patch("kicad_tools.mcp.tools.routing.route_net_auto", return_value=partial):
        rc = run_route_auto_command(_args())

    assert rc == 1
    err = capsys.readouterr().err
    assert "partially routed" in err
    assert "2/3 pads connected" in err
    # Guides the user toward the completing strategy.
    assert "hierarchical" in err


def test_partial_without_allow_partial_notes_copper_not_saved(capsys):
    partial = {
        "success": False,
        "partial": True,
        "net_name": "NET1",
        "pads_connected": 2,
        "pads_total": 3,
        "strategy_used": "GLOBAL_WITH_REPAIR",
        "warnings": [],
    }
    with patch("kicad_tools.mcp.tools.routing.route_net_auto", return_value=partial):
        rc = run_route_auto_command(_args(allow_partial=False))

    assert rc == 1
    err = capsys.readouterr().err
    assert "NOT saved" in err


def test_partial_with_allow_partial_reports_saved_segments(capsys):
    partial = {
        "success": False,
        "partial": True,
        "net_name": "NET1",
        "pads_connected": 2,
        "pads_total": 3,
        "strategy_used": "GLOBAL_WITH_REPAIR",
        "segments_written": 4,
        "warnings": [],
    }
    with patch("kicad_tools.mcp.tools.routing.route_net_auto", return_value=partial):
        rc = run_route_auto_command(_args(allow_partial=True))

    assert rc == 1  # still non-zero: the net is incomplete
    err = capsys.readouterr().err
    assert "Partial copper saved: 4 segments" in err


def test_full_success_still_exits_zero(capsys):
    ok = {
        "success": True,
        "partial": False,
        "net_name": "NET1",
        "strategy_used": "HIERARCHICAL_DIFF_PAIR",
        "metrics": {"total_length_mm": 12.0, "via_count": 0},
        "warnings": [],
    }
    with patch("kicad_tools.mcp.tools.routing.route_net_auto", return_value=ok):
        rc = run_route_auto_command(_args(strategy="auto"))

    assert rc == 0
    out = capsys.readouterr().out
    assert "successfully" in out.lower()


def test_hard_failure_exits_nonzero_without_partial_message(capsys):
    fail = {
        "success": False,
        "partial": False,
        "net_name": "NET1",
        "error_message": "Global router failed to find corridor assignment",
        "warnings": [],
    }
    with patch("kicad_tools.mcp.tools.routing.route_net_auto", return_value=fail):
        rc = run_route_auto_command(_args())

    assert rc == 1
    err = capsys.readouterr().err
    assert "Routing failed for net" in err
    assert "partially routed" not in err
