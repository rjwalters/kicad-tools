"""Real CLI acceptance for useful routing around invalid placement (#5348)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from tests.test_routing_placement_disposition import board_text


@pytest.mark.parametrize("entry", ["inner", "outer"])
@pytest.mark.parametrize("finite", [False, True])
@pytest.mark.parametrize("selection", ["mixed", "valid_only", "skip_invalid", "complete_noop"])
def test_partial_placement_cli_preserves_and_reports(tmp_path, entry, finite, selection):
    board = tmp_path / "mixed.kicad_pcb"
    text = board_text()
    if selection == "complete_noop":
        additions = """
        (segment (start 106 103) (end 108 103) (width 0.2) (layer "F.Cu") (net 1))
        (segment (start 108 103) (end 125 105) (width 0.2) (layer "F.Cu") (net 1))
        (segment (start 105 107) (end 110 107) (width 0.2) (layer "F.Cu") (net 2))
        (segment (start 105 109) (end 110 109) (width 0.2) (layer "F.Cu") (net 3))
        """
        text = text.rstrip()[:-1] + additions + ")"
    board.write_text(text)
    original = board.read_bytes()
    output = tmp_path / "routed.kicad_pcb"
    report = tmp_path / "complete.json"
    options = {
        "mixed": [],
        "valid_only": ["--nets", "GOOD"],
        "skip_invalid": ["--skip-nets", "BAD"],
        "complete_noop": ["--complete"],
    }[selection]
    argv = [str(board), "-o", str(output), "--complete-report", str(report), *options]
    if finite:
        argv += ["--timeout", "30"]
    code = (
        "from kicad_tools.cli.route_cmd import main; import sys; sys.exit(main(sys.argv[1:]))"
        if entry == "inner"
        else 'from kicad_tools.cli import main; import sys; sys.exit(main(["route", *sys.argv[1:]]))'
    )
    result = subprocess.run(
        [sys.executable, "-c", code, *argv],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert board.read_bytes() == original
    assert output.is_file(), result.stdout + result.stderr
    assert result.returncode == (2 if selection in {"mixed", "complete_noop"} else 0)
    parsed = PCB.load(output)
    assert any(segment.net_name == "GOOD" for segment in parsed.segments)
    before = PCB.load(board)

    def identities(pcb):
        return sorted((f.reference, p.number, p.net_name) for f in pcb.footprints for p in f.pads)

    def bad_copper(pcb):
        return sorted(
            (s.start, s.end, s.width, s.layer) for s in pcb.segments if s.net_name == "BAD"
        )

    assert identities(parsed) == identities(before)
    assert bad_copper(parsed) == bad_copper(before)
    assert len([v for v in parsed.vias if v.net_name == "BAD"]) == 1
    disposition = json.loads(report.read_text())["placement_disposition"]
    assert disposition["direct_invalid_nets"] == ["BAD"]
    assert disposition["requested_blocked_nets"] == (
        ["BAD"] if selection in {"mixed", "complete_noop"} else []
    )
    assert "GOOD" in disposition["completed_nets"]
