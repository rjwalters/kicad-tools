"""MCP ``optimize_placement`` matches ``kct optimize-placement`` (issue #6253).

PR #6251 (#6020) gave the CLI a decoupling-cap affinity term and a post-optimize
snap pass. The MCP tool must run the same code, with the same opt-out, so an
agent placing parts through MCP gets the same board as one using the CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_placement_decoupling import _DECAP_PCB

pytest.importorskip("cmaes", reason="cmaes not installed")

ITERATIONS = 15


@pytest.fixture
def decap_pcb(tmp_path: Path) -> Path:
    path = tmp_path / "decap.kicad_pcb"
    path.write_text(_DECAP_PCB)
    return path


def _positions(path: Path) -> dict[str, tuple[float, float, float]]:
    from kicad_tools.schema.pcb import PCB

    return {
        fp.reference: (round(fp.position[0], 4), round(fp.position[1], 4), fp.rotation)
        for fp in PCB.load(str(path)).footprints
    }


def _run_cli(pcb: Path, out: Path, weights: dict | None, capsys) -> dict:
    from kicad_tools.cli.optimize_placement_cmd import run_optimize_placement

    run_optimize_placement(
        str(pcb),
        output_path=str(out),
        max_iterations=ITERATIONS,
        weights_json=json.dumps(weights) if weights is not None else None,
        allow_infeasible=True,
        as_json=True,
    )
    return json.loads(capsys.readouterr().out)


def _run_mcp(pcb: Path, out: Path, weights: dict | None) -> dict:
    from kicad_tools.mcp.tools.optimize_placement import optimize_placement

    result = optimize_placement(
        str(pcb),
        max_iterations=ITERATIONS,
        weights=weights,
        output_path=str(out),
    )
    assert result["success"], result
    return result


@pytest.mark.parametrize("weights", [None, {"decoupling": 0}], ids=["default", "decoupling-off"])
def test_mcp_and_cli_place_identically(decap_pcb, tmp_path, capsys, weights):
    cli_out = tmp_path / "cli.kicad_pcb"
    mcp_out = tmp_path / "mcp.kicad_pcb"
    cli_doc = _run_cli(decap_pcb, cli_out, weights, capsys)
    mcp_doc = _run_mcp(decap_pcb, mcp_out, weights)

    assert _positions(cli_out) == _positions(mcp_out)
    assert mcp_doc["decoupling"] == cli_doc["decoupling"]
    assert mcp_doc["final_score"]["total"] == pytest.approx(
        cli_doc["scores"]["final"]["total"], abs=1e-3
    )


def test_mcp_applies_decoupling_by_default(decap_pcb, tmp_path):
    on = _run_mcp(decap_pcb, tmp_path / "on.kicad_pcb", None)
    off = _run_mcp(decap_pcb, tmp_path / "off.kicad_pcb", {"decoupling": 0})

    assert [e["cap"] for e in on["decoupling"]] == ["C1"]
    assert off["decoupling"] == []
    assert off["final_score"]["breakdown"]["decoupling"] == 0
    assert on["decoupling"][0]["distance_mm"] <= 3.0
