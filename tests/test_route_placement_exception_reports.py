"""Interrupted routing still publishes current placement diagnostics."""

import json

import pytest

from kicad_tools.cli import route_cmd, route_deadline, route_placement
from tests.test_routing_placement_disposition import board_text


@pytest.mark.parametrize("failure", ["deadline", "drc", "supervisor"])
def test_exception_reports_replace_stale_attempt(tmp_path, monkeypatch, capsys, failure):
    source = tmp_path / "source.kicad_pcb"
    source.write_text(board_text())
    output = tmp_path / "out.kicad_pcb"
    output.write_text("unverified unrelated output")
    report = tmp_path / "report.json"
    report.write_text('{"stale": true}')
    failed = tmp_path / "failed.json"
    failed.write_text('[{"net": "STALE"}]')
    argv = [
        str(source),
        "-o",
        str(output),
        "--complete-report",
        str(report),
        "--export-failed-nets",
        str(failed),
        "--format",
        "json",
        "--quiet",
    ]
    monkeypatch.delenv(route_deadline.CONTROL_ENV, raising=False)
    if failure == "supervisor":

        def supervise(command, budget, control):
            monkeypatch.setenv(route_deadline.CONTROL_ENV, str(control))
            args = route_cmd._route_parser().parse_args(argv)
            route_placement.prepare(args, source)
            return route_deadline.TIMEOUT_EXIT

        monkeypatch.setattr(route_deadline, "_supervise", supervise)
        argv += ["--timeout", "10"]
    else:
        error = (
            route_deadline.RouteDeadlineExpired()
            if failure == "deadline"
            else route_cmd.DRCConstraintPropagationError("constraint conflict")
        )

        def fail(*args):
            raise error

        monkeypatch.setattr(route_cmd, "_resolve_complete_nets", fail)
    result = route_cmd.main_with_result(argv)
    assert result.exit_code == (1 if failure == "drc" else 124)
    assert result.placement_disposition.requested_invalid_nets == frozenset({"BAD"})
    summary = json.loads(capsys.readouterr().out)
    assert summary["exit_code"] == result.exit_code
    assert summary["output_written"] is False
    assert summary["placement_disposition"]["completed_nets"] == []
    payload = json.loads(report.read_text())
    assert "stale" not in payload
    assert payload["placement_disposition"]["requested_blocked_nets"] == ["BAD"]
    assert payload["placement_disposition"]["clean_success"] is False
    assert [item["net"] for item in json.loads(failed.read_text())] == ["BAD"]
    assert output.read_text() == "unverified unrelated output"
