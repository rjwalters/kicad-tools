"""CLI surfaces quote the current-path witness in their own frame (Issue #8928).

:attr:`PathResolution.reason` is prose only; every surface that names the loop
witness renders it from the structured :attr:`PathResolution.witness` with the
origin it reports in. On a board whose ``board_origin`` is not ``(0, 0)``:

* ``kct pcb reinforce`` (JSON ``unresolved_paths[].reason``, the stderr warning
  and the text report) quotes **sheet** positions, like every other coordinate
  it emits;
* ``kct pcb current-paths-audit`` does too, in ``reason`` and in the
  ``witness`` dict (including ``blocking_branches``);
* ``pcb prune``'s ``uncut_loops[].segments`` stays **board-relative**, like the
  rest of prune's report.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from tests.test_pcb_reinforce import _reinforce_args, _write_single_pad_footprint_lib

NET = "FUSED_LINE"
# Board-relative (70, 58) -> (70, 50): the loop-closing witness segment.
_SHEET_WITNESS = "[witness: segment (118.500, 113.000)->(118.500, 105.000) on B.Cu]"
_RELATIVE_WITNESS = "segment (70.000, 58.000)->(70.000, 50.000) on B.Cu"


def _loop_board(tmp_path: Path) -> tuple[Path, Path]:
    """A B.Cu run J1 -> J2 with a loop-closing tap, centred on the sheet."""
    mod_path = _write_single_pad_footprint_lib(tmp_path)
    pcb = PCB.create(width=200, height=100, layers=4, center=True)
    pcb.add_footprint_from_file(mod_path, "J1", 20, 50)
    pcb.add_footprint_from_file(mod_path, "J2", 120, 50)
    pcb.add_trace((20, 50), (120, 50), width=2.0, layer="B.Cu", net=NET)
    for ref in ("J1", "J2"):
        pcb.assign_net_to_footprint_pad(ref, "1", NET)
    pcb.add_trace((50, 50), (50, 58), width=0.25, layer="B.Cu", net=NET)
    pcb.add_trace((50, 58), (70, 58), width=0.25, layer="B.Cu", net=NET)
    pcb.add_trace((70, 58), (70, 50), width=0.25, layer="B.Cu", net=NET)
    pcb_path = tmp_path / "board.kicad_pcb"
    pcb.save(pcb_path)
    assert tuple(PCB.load(pcb_path).board_origin) == pytest.approx((48.5, 55.0))
    sidecar = tmp_path / "current_paths.json"
    sidecar.write_text(
        json.dumps(
            {
                "paths": [
                    {
                        "name": "FORCE",
                        "net": NET,
                        "source": {"ref": "J1", "pad": "1"},
                        "sink": {"ref": "J2", "pad": "1"},
                        "continuous_a": 10.0,
                        "reinforcement_eligible": True,
                    }
                ]
            }
        )
    )
    return pcb_path, sidecar


def test_reinforce_json_and_warning_quote_sheet_witness(tmp_path, capsys):
    from kicad_tools.cli.commands.pcb import run_pcb_command

    pcb_path, sidecar = _loop_board(tmp_path)
    rc = run_pcb_command(
        _reinforce_args(pcb_path, current_paths=str(sidecar), format="json", dry_run=True)
    )
    assert rc == 0
    captured = capsys.readouterr()
    (entry,) = json.loads(captured.out)["unresolved_paths"]
    assert entry["status"] == "ambiguous"
    assert _SHEET_WITNESS in entry["reason"]
    assert "(70.000, 58.000)" not in entry["reason"]
    assert _SHEET_WITNESS in captured.err and "Warning" in captured.err


def test_reinforce_text_report_quotes_sheet_witness(tmp_path, capsys):
    from kicad_tools.cli.commands.pcb import run_pcb_command

    pcb_path, sidecar = _loop_board(tmp_path)
    rc = run_pcb_command(_reinforce_args(pcb_path, current_paths=str(sidecar), dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Unresolved current paths" in out
    assert "FORCE: ambiguous -- " in out
    assert _SHEET_WITNESS in out
    assert "(70.000, 58.000)" not in out


def test_current_paths_audit_quotes_sheet_witness(tmp_path, capsys):
    from kicad_tools.cli.commands.pcb import run_pcb_command

    pcb_path, sidecar = _loop_board(tmp_path)
    args = argparse.Namespace(
        pcb_command="current-paths-audit",
        pcb=str(pcb_path),
        current_paths=str(sidecar),
        format="json",
    )
    assert run_pcb_command(args) == 0
    (res,) = json.loads(capsys.readouterr().out)["resolutions"]
    assert res["status"] == "ambiguous"
    assert _SHEET_WITNESS in res["reason"]
    assert res["witness"]["start"] == pytest.approx([118.5, 113.0])
    branches = res["witness"]["blocking_branches"]
    assert branches, "fixture must carry blocking branches"
    # Every branch end lies inside the sheet-frame outline, not board-relative.
    for b in branches:
        for x, y in (b["start"], b["end"]):
            assert 48.5 <= x <= 248.5 and 55.0 <= y <= 155.0

    args.format = "text"
    assert run_pcb_command(args) == 0
    assert _SHEET_WITNESS in capsys.readouterr().out


def test_prune_uncut_loop_witness_stays_board_relative(tmp_path):
    from kicad_tools.pcb_prune import audit_loops_left
    from kicad_tools.router.current_paths import load_current_path_specs

    pcb_path, sidecar = _loop_board(tmp_path)
    pcb = PCB.load(pcb_path)
    (loop,) = audit_loops_left(pcb, load_current_path_specs(sidecar))
    assert loop["segments"] == [_RELATIVE_WITNESS]
    assert loop["witness"]["start"] == [70.0, 58.0]
