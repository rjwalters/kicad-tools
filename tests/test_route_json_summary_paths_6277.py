"""Every ``kct route --format json`` path emits one routing-summary document (#6277).

``--no-auto-layers`` used to take the direct (non-escalation) path, whose JSON
emitter sat inside the ``if not quiet`` banner chain and only ran on a PARTIAL
outcome; stdout then carried only ``{"exit_code": .., "verdict": ..}``.  The
default (escalation) path printed the full ``print_routing_diagnostics_json``
document.  Both must now have the same shape, with one final verdict.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kicad_tools.cli import main as kct_main

PCB = Path(__file__).resolve().parent / "fixtures" / "projects" / "test_project.kicad_pcb"

# Fast, deterministic flags: no DRC / pour / optimise stages, which cannot
# change what is written to stdout.
BASE = [
    "--format",
    "json",
    "--skip-drc",
    "--no-optimize",
    "--no-sync-check",
    "--no-auto-pour",
    "--no-placement-feedback",
    "--oracle-rounds",
    "0",
    "--no-current-paths",
]

PATHS = {
    "direct (--no-auto-layers)": ["--no-auto-layers"],
    "direct, quiet": ["--no-auto-layers", "--quiet"],
    "direct, basic strategy": ["--no-auto-layers", "--strategy", "basic"],
    "direct, monte-carlo strategy": [
        "--no-auto-layers",
        "--strategy",
        "monte-carlo",
        "--mc-trials",
        "1",
    ],
    "escalation (default)": [],
    "escalation, pinned layers": ["--starting-layers", "2", "--max-layers", "2"],
    "escalation, basic strategy": ["--strategy", "basic"],
}


def _route(tmp_path: Path, capsys, extra: list[str]) -> tuple[int, dict]:
    pcb = tmp_path / PCB.name
    shutil.copy(PCB, pcb)
    rc = kct_main(["route", str(pcb), "-o", str(tmp_path / "out.kicad_pcb"), *BASE, *extra])
    out = capsys.readouterr().out
    return rc, json.loads(out)  # the whole of stdout is one document (#5938)


@pytest.mark.parametrize("extra", list(PATHS.values()), ids=list(PATHS))
def test_every_route_path_emits_the_full_summary(tmp_path, capsys, extra) -> None:
    rc, doc = _route(tmp_path, capsys, extra)

    assert "summary" in doc, f"minimal fallback document on this path: {doc}"
    summary = doc["summary"]
    for key in ("nets_requested", "nets_routed", "verdict"):
        assert key in summary, key
    for key in ("successful_routes", "failed_routes", "placement_disposition"):
        assert key in doc, key
    # Exactly one final verdict, and it agrees with the exit code.
    assert summary["verdict"] in {"success", "failed"}
    assert "verdict" not in doc
    assert (summary["verdict"] == "success") == (rc == 0)


def test_direct_and_escalation_documents_share_a_shape(tmp_path, capsys) -> None:
    _, direct = _route(tmp_path, capsys, ["--no-auto-layers"])
    _, escalated = _route(tmp_path, capsys, [])

    assert set(direct) >= set(escalated) - {"routing_plan"}
    assert set(direct["summary"]) == set(escalated["summary"])
