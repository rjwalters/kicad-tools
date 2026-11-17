"""View filters never change a verdict (Issue #7230).

``kct drc --net/--type/--category/--errors-only`` and ``kct validate --sync
--errors-only`` used to compute their exit code from the *filtered* findings,
so a board with an error exited 0.  They now gate on every finding and
disclose what the filter hid (``summary.hidden_by_view_filter``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli import drc_cmd

FIXTURES = Path(__file__).parent / "fixtures" / "drc"
ONE_ERROR = FIXTURES / "view_filter_one_error_7230.json"
ERROR_AND_WARNING = FIXTURES / "view_filter_error_and_warning_7230.json"


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--net", "VCC"],
        ["--type", "silk"],
        ["--category", "cosmetic"],
        ["--errors-only"],
        ["--net", "VCC", "--format", "json"],
        ["--type", "silk", "--format", "summary"],
    ],
)
def test_filters_do_not_change_exit_code(extra):
    assert drc_cmd.main([str(ONE_ERROR), "--allow-incomplete", *extra]) == 1


def test_json_discloses_hidden(capsys):
    rc = drc_cmd.main([str(ONE_ERROR), "--allow-incomplete", "--net", "VCC", "--format", "json"])
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["violations"] == []
    assert data["summary"]["errors"] == 0
    assert data["summary"]["hidden_by_view_filter"] == {"errors": 1, "warnings": 0}


def test_json_has_no_hidden_key_when_nothing_hidden(capsys):
    drc_cmd.main([str(ONE_ERROR), "--allow-incomplete", "--format", "json"])
    assert "hidden_by_view_filter" not in json.loads(capsys.readouterr().out)["summary"]
    drc_cmd.main([str(ONE_ERROR), "--allow-incomplete", "--net", "GND", "--format", "json"])
    assert "hidden_by_view_filter" not in json.loads(capsys.readouterr().out)["summary"]


def test_table_and_summary_disclose_hidden(capsys):
    drc_cmd.main([str(ONE_ERROR), "--allow-incomplete", "--net", "VCC"])
    out = capsys.readouterr().out
    assert "1 hidden: 1 error(s), 0 warning(s)" in out
    assert "DRC PASSED" not in out
    drc_cmd.main([str(ONE_ERROR), "--allow-incomplete", "--net", "VCC", "--format", "summary"])
    assert "1 hidden: 1 error(s), 0 warning(s)" in capsys.readouterr().out


def test_strict_warning_hidden_by_errors_only_exits_2(tmp_path):
    # Remove the error so only the warning remains, then hide it.
    data = json.loads(ERROR_AND_WARNING.read_text())
    data["violations"] = [v for v in data["violations"] if v["severity"] == "warning"]
    report = tmp_path / "warn.json"
    report.write_text(json.dumps(data))
    base = [str(report), "--allow-incomplete", "--errors-only"]
    assert drc_cmd.main(base) == 0
    assert drc_cmd.main([*base, "--strict"]) == 2


def test_error_and_warning_counts_hidden_separately(capsys):
    rc = drc_cmd.main(
        [str(ERROR_AND_WARNING), "--allow-incomplete", "--net", "VCC", "--format", "json"]
    )
    assert rc == 1
    summary = json.loads(capsys.readouterr().out)["summary"]
    assert summary["hidden_by_view_filter"] == {"errors": 1, "warnings": 1}


def test_pcb_input_net_filter_exits_1(tmp_path, monkeypatch):
    """A ``.kicad_pcb`` input (kicad-cli mocked) behaves like a report."""
    from kicad_tools.drc.report import DRCReport

    report = DRCReport.load(ONE_ERROR)
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    monkeypatch.setattr(drc_cmd, "run_drc_on_pcb", lambda *a, **k: report)
    assert drc_cmd.main([str(pcb), "--allow-incomplete", "--net", "VCC"]) == 1


def _sync_result(monkeypatch, issues):
    from kicad_tools.cli import validate_sync_cmd
    from kicad_tools.validate.netlist import SyncResult

    class _V:
        def __init__(self, *a, **k):
            pass

        def validate(self):
            return SyncResult(issues=issues)

    monkeypatch.setattr(validate_sync_cmd, "NetlistValidator", _V)
    return validate_sync_cmd


def _sync_args(tmp_path, *extra):
    sch = tmp_path / "a.kicad_sch"
    pcb = tmp_path / "a.kicad_pcb"
    sch.write_text("")
    pcb.write_text("")
    return ["-s", str(sch), "-p", str(pcb), *extra]


def test_validate_sync_errors_only_strict_exits_2_on_warnings(monkeypatch, tmp_path):
    from kicad_tools.validate.netlist import SyncIssue

    warn = SyncIssue("warning", "net_mismatch", "m", "s")
    mod = _sync_result(monkeypatch, [warn])
    args = _sync_args(tmp_path, "--errors-only")
    assert mod.main(args) == 0
    assert mod.main([*args, "--strict"]) == 2


def test_validate_sync_errors_only_still_exits_1_and_discloses(monkeypatch, tmp_path, capsys):
    from kicad_tools.validate.netlist import SyncIssue

    err = SyncIssue("error", "missing_on_pcb", "m", "s", reference="R1")
    warn = SyncIssue("warning", "net_mismatch", "m", "s")
    mod = _sync_result(monkeypatch, [err, warn])
    # --errors-only keeps the error, hides the warning; error still gates.
    assert mod.main(_sync_args(tmp_path, "--errors-only", "--format", "json")) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["hidden_by_view_filter"] == {"errors": 0, "warnings": 1}


@pytest.mark.parametrize(
    ("meta_overall", "drc_only", "errors", "allow", "expected"),
    [
        ("PASSED", False, 0, False, ("pass", 0)),
        ("FAILED", False, 0, False, ("fail", 2)),
        ("INCOMPLETE", False, 0, False, ("incomplete", 2)),
        ("INCOMPLETE", False, 0, True, ("incomplete", 0)),
        (None, True, 0, False, ("pass", 0)),
        (None, True, 1, False, ("fail", 2)),
    ],
)
def test_check_verdict_matches_exit(meta_overall, drc_only, errors, allow, expected):
    from types import SimpleNamespace

    from kicad_tools.cli.check_cmd import _check_verdict

    meta = SimpleNamespace(overall=meta_overall) if meta_overall else None
    assert (
        _check_verdict(
            mask_copper_failed=False,
            strict_copper_disagreement=False,
            drc_only=drc_only,
            error_count=errors,
            warning_count=0,
            strict=False,
            allow_incomplete=allow,
            meta=meta,
        )
        == expected
    )


def test_check_json_carries_verdict_and_exit_code(drc_clean_pcb, capsys):
    from kicad_tools.cli.check_cmd import main

    rc = main([str(drc_clean_pcb), "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert data["exit_code"] == rc == 2
    assert data["verdict"] == "incomplete"
    assert data["summary"]["passed"] is True  # DRC-only meaning unchanged

    rc = main([str(drc_clean_pcb), "--format", "json", "--drc-only"])
    data = json.loads(capsys.readouterr().out)
    assert (data["verdict"], data["exit_code"], rc) == ("pass", 0, 0)
