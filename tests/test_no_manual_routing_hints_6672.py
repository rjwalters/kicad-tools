"""Issue #6672: routing hints must point at automation, never at hand-routing.

Project policy: kicad-tools never tells the user (or an agent) to finish
routing by hand.  Hints name a concrete ``kct`` command/flag or say to file
the net as a router gap.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

from kicad_tools.mcp.tools.routing import _generate_suggestions
from kicad_tools.router.fine_pitch import (
    ComponentGridAnalysis,
    FinePitchReport,
    FinePitchSeverity,
    _generate_recommendations,
)

_MANUAL_ROUTING = re.compile(r"manual(ly)?\s+rout|rout\w*\s+(by hand|manually)", re.IGNORECASE)

_SRC = Path(__file__).resolve().parents[1] / "src" / "kicad_tools"


def _high_severity_report() -> FinePitchReport:
    comp = ComponentGridAnalysis(
        ref="U1",
        package_type="QFN-32",
        pin_count=32,
        pin_pitch=0.4,
        off_grid_count=20,
        off_grid_percentage=62.5,
        severity=FinePitchSeverity.HIGH,
        recommendations=_generate_recommendations(0.4, 0.1, 62.5, FinePitchSeverity.HIGH),
    )
    return FinePitchReport(
        components=[comp],
        grid_resolution=0.1,
        trace_width=0.2,
        clearance=0.2,
        total_pads=32,
        total_off_grid=20,
        affected_net_count=10,
    )


def test_fine_pitch_recommendations_point_at_automation() -> None:
    recs = _generate_recommendations(0.4, 0.1, 62.5, FinePitchSeverity.CRITICAL)
    assert not any(_MANUAL_ROUTING.search(r) for r in recs), recs
    assert any("--fine-pitch-clearance" in r for r in recs), recs


def test_fine_pitch_report_warnings_point_at_automation() -> None:
    text = _high_severity_report().format_warnings(verbose=True)
    assert "Recommendations:" in text
    assert not _MANUAL_ROUTING.search(text), text
    assert "kct net-status --why" in text
    assert "--fine-pitch-clearance" in text


def test_mcp_route_net_suggestions_point_at_automation() -> None:
    net_status = SimpleNamespace(net_name="SIG1")
    suggestions = _generate_suggestions(net_status, [{}, {}, {}], pcb=None)
    assert not any(_MANUAL_ROUTING.search(s) for s in suggestions), suggestions
    assert any("kct net-status --why" in s for s in suggestions), suggestions


def test_no_manual_routing_string_literals_in_src() -> None:
    """No string constant (docstrings excluded) under src/ suggests hand-routing."""
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Bare string statements are docstrings (module/class/function and
        # attribute docstrings) -- documentation, not user-facing output.
        docstrings = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and _MANUAL_ROUTING.search(node.value)
            ):
                offenders.append(f"{path.relative_to(_SRC)}:{node.lineno}: {node.value!r}")
    assert not offenders, "\n".join(offenders)
