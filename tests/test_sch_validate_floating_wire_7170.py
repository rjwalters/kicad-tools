"""Issue #7170 Part B: a real floating wire stays a ``kct sch validate`` error.

``filter_phantom_wire_violations`` was deleted: it keyed on a top-level
``pos`` that KiCad 10 never emits, and fed the per-item ``pos`` KiCad 10 does
emit (at 1/100 scale) it would have dropped this very finding.  These tests
pin that a floating two-segment wire reaches the user as the
``wire_dangling`` error "Wires not connected to anything":

* the committed kicad-cli 10.0.6 ERC JSON run through ``run_erc``'s raw
  violation pipeline (kicad-cli patched out, so it always runs);
* live kicad-cli on the same schematic (skipped when kicad-cli is absent).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli import sch_validate
from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.cli.sch_validate import run_erc

FIXTURES = Path(__file__).parent / "fixtures" / "erc"
SCHEMATIC = FIXTURES / "kicad10_floating_wire_7170.kicad_sch"
ERC_JSON = FIXTURES / "kicad10_floating_wire_7170.json"

WIRE_DANGLING_MESSAGE = "Wires not connected to anything"


def test_fixture_is_real_kicad10_output_without_top_level_pos():
    """The committed JSON is kicad-cli 10 output, the shape the filter missed."""
    data = json.loads(ERC_JSON.read_text())
    assert data["kicad_version"].startswith("10.")
    violations = [v for s in data["sheets"] for v in s["violations"]]
    assert [v["type"] for v in violations].count("wire_dangling") == 1
    # KiCad 10 emits positions only per item, never at the top level.
    assert not any("pos" in v for v in violations)
    assert all("pos" in item for v in violations for item in v["items"])


def test_phantom_filter_is_gone():
    from kicad_tools.erc import cross_sheet

    assert not hasattr(cross_sheet, "filter_phantom_wire_violations")
    assert not hasattr(cross_sheet, "_build_full_wire_coordinate_set")
    assert not hasattr(sch_validate, "filter_phantom_wire_violations")


def _replay_committed_report(cmd, *args, **kwargs):
    """Stand in for kicad-cli: write the committed report to ``--output``."""
    output = cmd[cmd.index("--output") + 1]
    shutil.copyfile(ERC_JSON, output)
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def test_run_erc_keeps_floating_wire_error_from_committed_report():
    with (
        patch.object(sch_validate, "find_kicad_cli", return_value="/usr/bin/kicad-cli"),
        patch.object(sch_validate.subprocess, "run", side_effect=_replay_committed_report),
    ):
        issues = run_erc(str(SCHEMATIC))

    errors = [i for i in issues if i.severity == "error"]
    assert [i.message for i in errors] == [WIRE_DANGLING_MESSAGE]
    assert errors[0].category == "erc"
    endpoint_warnings = [i for i in issues if i.message == "Unconnected wire endpoint"]
    assert len(endpoint_warnings) == 2


@pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")
def test_live_sch_validate_reports_floating_wire(tmp_path, capsys):
    sch = tmp_path / SCHEMATIC.name
    shutil.copyfile(SCHEMATIC, sch)

    with pytest.raises(SystemExit) as excinfo:
        sch_validate.main([str(sch), "--format", "json"])

    assert excinfo.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["passed"] is False
    erc_errors = [i for i in out["issues"] if i["category"] == "erc" and i["severity"] == "error"]
    assert [i["message"] for i in erc_errors] == [WIRE_DANGLING_MESSAGE]
