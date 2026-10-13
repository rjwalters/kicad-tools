"""``run_kct_check`` grades a board exactly like ``kct check`` (Issue #6420).

The research harness reports a ``kct check`` column that claims to be the
same number as ``kct check --mfr <tier> --allow-incomplete``. It used to run
only ``DRCChecker.check_all()``, which leaves out the CLI-level rules that
need the board *path* -- ``netclass_clearance_copper`` and ``netclass_floor``
read the sibling ``.kicad_pro``. On board 05 (lattice) that graded 0 errors
where ``kct check`` reported 6 ``netclass_clearance_copper`` errors.

The board is #6243's wall corridor: an ``HV`` class (0.5 mm) whose straight
trace keeps 0.45 mm from the wall pads, plus a ``Thin`` class whose 0.05 mm
track width is below the JLCPCB floor. ``netclass_floor``
(``netclass_track_width``) and ``netclass_clearance_copper`` both fire, and
the in-process summary must match the CLI's totals and per-rule counts.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from kicad_tools.benchmark.external import run_kct_check
from kicad_tools.cli.check_cmd import main
from kicad_tools.validate.rules.netclass_floor import NETCLASS_FLOOR_RULE_IDS
from tests.router import test_authored_netclass_board as wall


def _seg(x1: float, y1: float, x2: float, y2: float, net: int = 1) -> str:
    return f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width 0.2) (layer "F.Cu") (net {net}))\n'


def _violating_board(tmp_path: Path) -> Path:
    pcb = tmp_path / "hv.kicad_pcb"
    text = wall.board_text().rstrip()
    assert text.endswith(")")
    pcb.write_text(text[:-1] + _seg(103, wall.HV_Y, 127, wall.HV_Y) + ")\n")
    project = wall.project("hv")
    thin = wall._netclass("Thin", 0.15, 1)
    thin["track_width"] = 0.05
    project["net_settings"]["classes"].append(thin)
    pcb.with_suffix(".kicad_pro").write_text(json.dumps(project))
    return pcb


def _cli_errors_by_rule(pcb: Path, tmp_path: Path, capsys) -> tuple[Counter, dict]:
    out = tmp_path / "report.json"
    main(
        [
            str(pcb),
            "--mfr",
            "jlcpcb",
            "--allow-incomplete",
            "--format",
            "json",
            "--output",
            str(out),
        ]
    )
    capsys.readouterr()
    report = json.loads(out.read_text())
    errors = Counter(
        v["rule_id"]
        for v in report["violations"]
        if v["severity"] == "error" and not v.get("waived", False)
    )
    return errors, report


@pytest.fixture()
def board(tmp_path: Path) -> Path:
    return _violating_board(tmp_path)


def test_path_dependent_rules_are_counted(board: Path) -> None:
    summary = run_kct_check(board, manufacturer="jlcpcb", layers=None)
    assert summary.ran is True
    assert summary.errors_by_rule.get("netclass_clearance_copper", 0) > 0
    assert set(summary.errors_by_rule) & set(NETCLASS_FLOOR_RULE_IDS)
    assert summary.passed is False


def test_totals_and_per_rule_counts_match_the_cli(board: Path, tmp_path: Path, capsys) -> None:
    cli_errors, report = _cli_errors_by_rule(board, tmp_path, capsys)
    summary = run_kct_check(board, manufacturer="jlcpcb", layers=None)

    # The fixture must exercise both path-dependent rules, or parity is vacuous.
    assert cli_errors["netclass_clearance_copper"] > 0
    assert set(cli_errors) & set(NETCLASS_FLOOR_RULE_IDS)

    assert summary.errors_by_rule == dict(cli_errors)
    assert summary.error_count == sum(cli_errors.values())
    assert summary.error_count == report["summary"]["errors"]
    assert summary.warning_count == report["summary"]["warnings"]


def test_in_memory_board_has_no_project_file_to_read(board: Path) -> None:
    """A loaded ``PCB`` carries no path, so the ``.kicad_pro`` rules stay silent."""
    from kicad_tools.schema.pcb import PCB

    summary = run_kct_check(PCB.load(str(board)), manufacturer="jlcpcb", layers=None)
    assert summary.ran is True
    assert "netclass_clearance_copper" not in summary.errors_by_rule
    assert not set(summary.errors_by_rule) & set(NETCLASS_FLOOR_RULE_IDS)
