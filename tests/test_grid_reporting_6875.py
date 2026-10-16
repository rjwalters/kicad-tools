"""Routing-grid reporting, explicit-grid cell ceiling, no silent coarsening (Issue #6875).

Follow-up to #6741 (PR #6871), which made ``kct route`` honour its requested
pitch.  This covers the three remaining gaps:

1. ``kct route`` records the pitch it routed on: a ``grid`` object in
   ``--format json`` and the pitch on the text ``Grid:`` line.
2. An explicit ``--grid`` past the backend's cell ceiling stops with an error
   (``--force`` proceeds) instead of allocating until the machine runs out of
   memory.
3. ``AdaptiveAutorouter`` (``kct route-auto``), ``route_pcb`` and the MCP
   ``route_net`` tool either honour the requested pitch or report the #972
   adaptive coarsening in their return value -- never only in a log line.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kicad_tools.router import cpp_backend
from kicad_tools.router.core import Autorouter
from kicad_tools.router.grid_limits import (
    CPP_EXPLICIT_GRID_CELL_LIMIT,
    PYTHON_EXPLICIT_GRID_CELL_LIMIT,
    estimate_grid_cells,
    explicit_grid_ceiling_error,
    finest_pitch_within,
    format_grid_line,
)
from kicad_tools.router.output import get_routing_diagnostics_json
from kicad_tools.router.rules import DesignRules

GRID_KEYS = {
    "requested_mm",
    "used_mm",
    "cols",
    "rows",
    "layers",
    "cells",
    "selection",
    "coarsened",
    "coarsened_reason",
}


def _rules(grid: float = 0.1) -> DesignRules:
    return DesignRules(
        grid_resolution=grid,
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.3,
        via_diameter=0.6,
    )


def _board(width: float, height: float) -> str:
    """A 2-layer board with one 2-pad signal net, as a .kicad_pcb text."""
    return f"""(kicad_pcb (version 20240108) (generator "test")
      (general (thickness 1.6)) (paper "A4")
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
      (net 0 "") (net 1 "SIG")
      (footprint "Test:A" (layer "F.Cu") (at 10 10)
        (property "Reference" "J1" (at 0 -2) (layer "F.SilkS")
          (effects (font (size 1 1) (thickness 0.15))))
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "SIG")))
      (footprint "Test:B" (layer "F.Cu") (at 20 15)
        (property "Reference" "J2" (at 0 -2) (layer "F.SilkS")
          (effects (font (size 1 1) (thickness 0.15))))
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "SIG")))
      (gr_rect (start 0 0) (end {width} {height}) (stroke (width 0.05) (type solid))
        (fill none) (layer "Edge.Cuts")))"""


def _components() -> list[dict]:
    pad = {"number": "1", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0, "net": "SIG"}
    return [
        {"ref": "J1", "x": 10.0, "y": 10.0, "rotation": 0, "pads": [dict(pad)]},
        {"ref": "J2", "x": 20.0, "y": 15.0, "rotation": 0, "pads": [dict(pad)]},
    ]


@pytest.fixture
def python_cap(monkeypatch):
    """Make the #972 adaptive cap use its Python threshold (500k cells).

    With the C++ extension built the threshold is 50M cells, too large to
    trigger in a unit test.
    """
    monkeypatch.setattr(cpp_backend, "is_cpp_available", lambda: False)


# ---------------------------------------------------------------------------
# 1. Autorouter.grid_report / JSON ``grid`` block / text ``Grid:`` line
# ---------------------------------------------------------------------------


def test_grid_report_records_adaptive_coarsening(python_cap):
    router = Autorouter(100, 80, rules=_rules(0.1), force_python=True)

    report = router.grid_report()

    assert set(report) == GRID_KEYS
    assert report["requested_mm"] == pytest.approx(0.1)
    assert report["used_mm"] > 0.1
    assert report["coarsened"] is True
    assert "Issue #972" in report["coarsened_reason"]
    assert report["cells"] == report["cols"] * report["rows"] * report["layers"]


def test_grid_report_pinned_pitch_is_not_coarsened(python_cap):
    router = Autorouter(100, 80, rules=_rules(0.1), force_python=True, adaptive_grid=False)

    report = router.grid_report("explicit")

    assert report["used_mm"] == pytest.approx(0.1)
    assert report["coarsened"] is False
    assert report["coarsened_reason"] is None
    assert report["selection"] == "explicit"
    assert (report["cols"], report["rows"], report["layers"]) == (1001, 801, 2)


def test_diagnostics_json_carries_grid_block():
    router = Autorouter(30, 20, rules=_rules(0.1), force_python=True, adaptive_grid=False)

    doc = get_routing_diagnostics_json(router, {}, 0, grid_selection="auto")

    assert set(doc["grid"]) == GRID_KEYS
    assert doc["grid"]["selection"] == "auto"
    assert doc["grid"]["used_mm"] == pytest.approx(0.1)
    json.dumps(doc)  # stays serializable


def test_diagnostics_json_omits_grid_for_router_doubles():
    """A MagicMock router must not put a non-serializable ``grid`` value in the JSON."""
    from kicad_tools.router.output import _grid_report_block

    assert _grid_report_block(MagicMock(), "explicit") is None


def test_text_grid_line_names_pitch_and_coarsening():
    report = {
        "requested_mm": 0.1,
        "used_mm": 0.35,
        "cols": 561,
        "rows": 446,
        "layers": 2,
        "cells": 500_412,
        "selection": None,
        "coarsened": True,
        "coarsened_reason": "adaptive cell cap",
    }
    line = format_grid_line(report)
    assert line.startswith("0.35mm pitch, 561x446x2 = 500,412 cells")
    assert "coarsened from the requested 0.1mm: adaptive cell cap" in line

    pinned = dict(report, used_mm=0.1, coarsened=False, selection="explicit")
    assert format_grid_line(pinned) == "0.1mm pitch (explicit), 561x446x2 = 500,412 cells"


# ---------------------------------------------------------------------------
# 2. Explicit-grid cell ceiling
# ---------------------------------------------------------------------------


def test_ceiling_limits_are_per_backend():
    # 100 x 80 mm, 6 layers, 0.05 mm -> 2001 x 1601 x 6 = 19.2M: under both.
    assert (
        explicit_grid_ceiling_error(
            width=100, height=80, resolution=0.05, num_layers=6, backend="python"
        )
        is None
    )
    # 0.02 mm -> 120M cells: over both.
    msg = explicit_grid_ceiling_error(
        width=100, height=80, resolution=0.02, num_layers=6, backend="python"
    )
    assert msg is not None
    assert "120,054,006" in msg
    assert f"{PYTHON_EXPLICIT_GRID_CELL_LIMIT:,}" in msg
    assert "kct build-native" in msg
    assert "--grid auto" in msg
    assert "--force" in msg

    cpp_msg = explicit_grid_ceiling_error(
        width=100, height=80, resolution=0.02, num_layers=6, backend="cpp"
    )
    assert cpp_msg is not None
    assert f"{CPP_EXPLICIT_GRID_CELL_LIMIT:,}" in cpp_msg
    assert "kct build-native" not in cpp_msg


def test_suggested_coarser_pitch_fits():
    pitch = finest_pitch_within(195, 155, 6, PYTHON_EXPLICIT_GRID_CELL_LIMIT)
    assert estimate_grid_cells(195, 155, pitch, 6) <= PYTHON_EXPLICIT_GRID_CELL_LIMIT
    assert estimate_grid_cells(195, 155, pitch - 0.005, 6) > PYTHON_EXPLICIT_GRID_CELL_LIMIT


@pytest.mark.parametrize(
    ("width", "height", "grid", "layers"),
    [
        # Committed recipes with an explicit --grid (worst-case layer count).
        (50, 55, 0.1, 6),  # board 02, --grid 0.1 (if the ladder ran to 6)
        (80, 60, 0.05, 4),  # board 03, --grid 0.05 --layers 4
        (60, 40, 0.05, 2),  # board 04, --grid 0.05 --layers 2
        (90, 70, 0.075, 4),  # board 06 assembled demo, --grid 0.075
        (100, 65, 0.05, 6),  # board 09 routing investigation, --grid 0.05
    ],
)
def test_committed_recipes_stay_under_the_python_ceiling(width, height, grid, layers):
    assert estimate_grid_cells(width, height, grid, layers) < PYTHON_EXPLICIT_GRID_CELL_LIMIT


def _run_route(tmp_path, *extra: str) -> tuple[int, str]:
    import contextlib
    import io

    from kicad_tools.cli.route_cmd import main

    pcb = tmp_path / "big.kicad_pcb"
    pcb.write_text(_board(195, 155))
    err = io.StringIO()
    out = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        rc = main(
            [
                str(pcb),
                "-o",
                str(tmp_path / "out.kicad_pcb"),
                "--layers",
                "2",
                "--no-auto-layers",
                "--backend",
                "python",
                "--dry-run",
                *extra,
            ]
        )
    return rc, err.getvalue() + out.getvalue()


def test_cli_refuses_explicit_grid_over_the_ceiling(tmp_path):
    rc, text = _run_route(tmp_path, "--grid", "0.02")
    assert rc == 1
    assert "Issue #6875" in text
    assert "151,160,002" in text  # 9751 x 7751 x 2


def test_cli_force_routes_at_the_requested_pitch(tmp_path):
    rc, text = _run_route(tmp_path, "--grid", "0.02", "--force")
    assert rc == 0
    assert "Issue #6875" not in text


def test_cli_auto_grid_is_not_subject_to_the_ceiling(tmp_path):
    rc, text = _run_route(tmp_path, "--grid", "auto", "--max-cells", "500000")
    assert rc == 0
    assert "Issue #6875" not in text


# ---------------------------------------------------------------------------
# 3. Other routing paths
# ---------------------------------------------------------------------------


def test_route_pcb_reports_coarsening_in_stats(python_cap, capsys):
    from kicad_tools.router.io import route_pcb

    _, stats = route_pcb(100, 80, _components(), {"SIG": 1}, rules=_rules(0.1))

    assert stats["grid"]["coarsened"] is True
    assert stats["grid"]["requested_mm"] == pytest.approx(0.1)
    assert "not the requested 0.1mm" in capsys.readouterr().out


def test_route_pcb_passes_pitch_through_when_asked(python_cap):
    from kicad_tools.router.io import route_pcb

    _, stats = route_pcb(100, 80, _components(), {"SIG": 1}, rules=_rules(0.1), adaptive_grid=False)

    assert stats["grid"]["coarsened"] is False
    assert stats["grid"]["used_mm"] == pytest.approx(0.1)


def test_adaptive_autorouter_reports_coarsening(python_cap):
    from kicad_tools.router.adaptive import AdaptiveAutorouter

    adaptive = AdaptiveAutorouter(
        100, 80, _components(), {"SIG": 1}, rules=_rules(0.1), max_layers=2, verbose=False
    )
    result = adaptive.route()

    assert result.grid is not None
    assert result.grid["coarsened"] is True
    assert result.grid_warning is not None
    assert "not the requested 0.1mm" in result.grid_warning


def test_adaptive_autorouter_passes_pitch_through_when_asked(python_cap):
    from kicad_tools.router.adaptive import AdaptiveAutorouter

    adaptive = AdaptiveAutorouter(
        100,
        80,
        _components(),
        {"SIG": 1},
        rules=_rules(0.1),
        max_layers=2,
        verbose=False,
        adaptive_grid=False,
    )
    result = adaptive.route()

    assert result.grid["used_mm"] == pytest.approx(0.1)
    assert result.grid_warning is None


def test_route_auto_hierarchical_surfaces_grid_warning(monkeypatch):
    """``kct route-auto``'s hierarchical strategy copies the warning to its result."""
    from kicad_tools.router.orchestrator import RoutingOrchestrator

    orch = RoutingOrchestrator(pcb=MagicMock(), rules=_rules(0.1), backend="cpu")
    monkeypatch.setattr(orch, "_route_hierarchical_on_board", lambda *a, **k: None)
    warning = "Routed on a 0.35mm grid, not the requested 0.1mm: cap (Issue #6875)"
    orch._hierarchical = SimpleNamespace(
        route=lambda: SimpleNamespace(
            routes=[], converged=True, grid_warning=warning, nets_routed=1, nets_requested=1
        )
    )

    result = orch._route_hierarchical("SIG", None, [MagicMock(), MagicMock()])

    assert warning in result.warnings
    assert warning in result.to_dict()["warnings"]


def test_mcp_route_net_reports_grid_and_coarsening(python_cap, tmp_path):
    pytest.importorskip("pydantic")
    from kicad_tools.mcp.tools.routing import route_net

    pcb = tmp_path / "big.kicad_pcb"
    pcb.write_text(_board(100, 80))

    result = route_net(str(pcb), "SIG", output_path=str(tmp_path / "out.kicad_pcb"))

    assert result.grid is not None
    assert set(result.grid) == GRID_KEYS
    payload = result.to_dict()
    assert payload["grid"] == result.grid
    # 100 x 80 mm at the board's 0.1 mm pitch is 1.6M cells, past the
    # Python cap: the coarsening must be in the return value, not just a log.
    assert result.grid["requested_mm"] == pytest.approx(0.1)
    assert result.grid["coarsened"] is True
    assert any("not the requested 0.1mm" in w for w in result.warnings)
    assert payload["warnings"] == result.warnings
