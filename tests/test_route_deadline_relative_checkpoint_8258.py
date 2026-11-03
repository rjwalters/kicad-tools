"""Issue #8258: timeout.json must not name a moved checkpoint for a relative -o."""

import json
import os
import sys
from pathlib import Path

import pytest

from kicad_tools.cli import route_deadline


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group regression")
@pytest.mark.parametrize("relative", [True, False])
def test_checkpoint_repointed_to_quarantined_file(tmp_path, monkeypatch, relative):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "wip").mkdir()
    output = tmp_path / "wip" / "out.kicad_pcb"
    output.write_text("(kicad_pcb)")
    printed = "wip/out.kicad_pcb" if relative else str(output)
    script = f"""
import signal, time, json, os
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(os.environ["KCT_ROUTE_DEADLINE_CONTROL"]).write_text(json.dumps(dict(
    stage="routing", output={str(output)!r}, checkpoint={printed!r}, snapshot_saved=False)))
time.sleep(100)
"""
    code = route_deadline._supervise(
        [sys.executable, "-c", script], 1.5, tmp_path / "control.json", save_seconds=0.3
    )
    assert code == 124
    report = json.loads(output.with_suffix(".timeout.json").read_text())
    assert not output.exists()
    assert report["checkpoint"] == report["unverified_output"]
    assert Path(report["checkpoint"]).exists()


def _timeout_fixture(tmp_path):
    from types import SimpleNamespace

    output = tmp_path / "wip" / "out.kicad_pcb"
    output.parent.mkdir(parents=True, exist_ok=True)
    report = output.with_suffix(".timeout.json")
    report.write_text(json.dumps({"stage": "layer-escalation"}))
    target = tmp_path / "wip" / "failed.json"
    parsed = SimpleNamespace(export_failed_nets=str(target), _placement_failed_before=None)
    return parsed, {"output": str(output)}, output, report, target


@pytest.mark.parametrize("sidecar_written", [False, True])
def test_skipped_failed_export_names_only_existing_paths(tmp_path, sidecar_written):
    """AC3 (a): target not written -> note says so; every named path exists."""
    parsed, state, output, report, target = _timeout_fixture(tmp_path)
    sidecar = output.with_stem(output.stem + "_partial").with_suffix(".search_failures.json")
    if sidecar_written:
        sidecar.write_text("{}")

    route_deadline._note_skipped_failed_export(parsed, state)

    note = json.loads(report.read_text())["export_failed_nets"]
    assert note["written"] is False
    assert note["requested"] == str(target.absolute())
    assert not target.exists()
    if sidecar_written:
        assert note["search_failures"] == str(sidecar)
        assert Path(note["search_failures"]).exists()
    else:
        assert note["search_failures"] is None
        assert "before the deadline" in note["search_failures_reason"]


def test_freshly_written_failed_export_gets_no_note(tmp_path):
    """AC3 (b): target written this run -> no skipped-export note."""
    parsed, state, _output, report, target = _timeout_fixture(tmp_path)
    target.write_text("[]")

    route_deadline._note_skipped_failed_export(parsed, state)

    assert "export_failed_nets" not in json.loads(report.read_text())


def test_note_runs_after_finish_writes_failed_export(tmp_path, monkeypatch):
    """Judge #2: ``finish()`` may write the target, so the note must follow it."""
    from kicad_tools.cli import route_placement

    output = tmp_path / "wip" / "out.kicad_pcb"
    output.parent.mkdir()
    target = tmp_path / "wip" / "failed.json"
    report = output.with_suffix(".timeout.json")

    def fake_supervise(_cmd, _budget, _control, **_kw):
        report.write_text(json.dumps({"stage": "routing"}))
        return route_deadline.TIMEOUT_EXIT

    def fake_finish(parsed, _code):
        Path(parsed.export_failed_nets).write_text('["GND"]')

    monkeypatch.setattr(route_deadline, "_supervise", fake_supervise)
    monkeypatch.setattr(route_placement, "disposition_from_control", lambda _c: object())
    monkeypatch.setattr(route_placement, "finish", fake_finish)

    result = route_deadline.run_attempt(
        [
            str(tmp_path / "board.kicad_pcb"),
            "-o",
            str(output),
            "--timeout",
            "5",
            "--export-failed-nets",
            str(target),
        ]
    )

    assert result.exit_code == route_deadline.TIMEOUT_EXIT
    assert target.exists()
    assert "export_failed_nets" not in json.loads(report.read_text())
