"""``kct drc`` schematic parity: PASSED / FAILED / NOT RUN (Issue #7077).

kicad-cli prints ``Failed to fetch schematic netlist for parity tests.``, exits
0 and writes ``"schematic_parity": []`` when ``--schematic-parity`` was asked
for but no annotated schematic netlist exists.  When parity does run, its
findings land in the ``schematic_parity`` array, which ``DRCReport`` used to
drop.  Either way ``kct drc`` used to read the board as parity-clean.

Most tests mock the kicad-cli subprocess; the last two run the real kicad-cli
on a scratch copy of board 00 and are skipped when it is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli import drc_cmd, drc_summary, runner
from kicad_tools.cli import main as cli_main
from kicad_tools.drc import Severity, parse_json_report
from kicad_tools.drc.parity import (
    PARITY_FAILED,
    PARITY_NOT_RUN,
    PARITY_PASSED,
    PARITY_REASON_KICAD_CLI_SKIPPED,
    PARITY_REASON_REPORT_UNKNOWN,
    parity_reason_no_schematic,
    schematic_parity_status,
)

PARITY_LINE = (
    "Failed to fetch schematic netlist for parity tests.\n"
    "Schematic parity tests require a fully annotated schematic.\n"
)

REPORT = {
    "source": "noparity.kicad_pcb",
    "date": "2026-10-17T00:00:00+0000",
    "kicad_version": "10.0.6",
    "coordinate_units": "mm",
    "violations": [],
    "unconnected_items": [],
    "schematic_parity": [],
}

WARNING_VIOLATION = {
    "type": "track_dangling",
    "description": "Track has unconnected end",
    "severity": "warning",
    "items": [{"description": "Track [A] on F.Cu", "pos": {"x": 5.0, "y": 10.0}}],
}

# Shape recorded from kicad-cli 10.0.6 on boards/00-simple-led with its
# schematic alongside (two ``net_conflict`` warnings).
PARITY_WARNING = {
    "description": "Pad net (LED_ANODE) doesn't match net given by schematic (/LED_ANODE)",
    "items": [
        {
            "description": "PTH pad 2 [LED_ANODE] of D1",
            "pos": {"x": 157.27, "y": 87.5},
            "uuid": "a0ca3eb3-a60b-5458-9531-e7b0a329e7d2",
        }
    ],
    "severity": "warning",
    "type": "net_conflict",
}

PARITY_ERROR = {
    "description": "Missing footprint R9",
    "items": [{"description": "Footprint R9", "pos": {"x": 1.0, "y": 2.0}}],
    "severity": "error",
    "type": "missing_footprint",
}

STATUS_PREFIX = "Schematic parity: "

#: Issue #7147: the ``pcb`` fixture has no schematic, so run_drc names that
#: cause rather than the generic kicad-cli reason.
NO_SCH_REASON = parity_reason_no_schematic("noparity.kicad_pcb")


@pytest.fixture
def pcb(tmp_path: Path) -> Path:
    path = tmp_path / "noparity.kicad_pcb"
    path.write_text("(kicad_pcb (version 20240108))\n")
    return path


def _fake_kicad_cli(
    monkeypatch, *, stdout: str = "", stderr: str = "", violations=None, parity=None
):
    """Patch the kicad-cli subprocess; record each argv it was given."""
    calls: list[list[str]] = []

    def fake_run(build_cmd, kicad_cli, *, refill_zones, timeout=None, text=False):
        argv = build_cmd(False)
        calls.append(argv)
        out = Path(argv[argv.index("--output") + 1])
        report = dict(
            REPORT,
            violations=list(violations or []),
            schematic_parity=list(parity or []),
        )
        out.write_text(json.dumps(report))
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=stderr), False

    monkeypatch.setattr(runner, "run_kicad_drc_command", fake_run)
    monkeypatch.setattr(runner, "find_kicad_cli", lambda: Path("/fake/kicad-cli"))
    monkeypatch.setattr(drc_cmd, "find_kicad_cli", lambda: Path("/fake/kicad-cli"))
    monkeypatch.setattr(drc_summary, "find_kicad_cli", lambda: Path("/fake/kicad-cli"))
    return calls


def _status_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith(STATUS_PREFIX)]


# --- run_drc: the run signal -------------------------------------------------


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_run_drc_flags_parity_skipped_on_either_stream(monkeypatch, pcb, stream):
    _fake_kicad_cli(monkeypatch, **{stream: PARITY_LINE})
    result = runner.run_drc(pcb)
    assert result.success is True  # success is unchanged (#6724 semantics)
    assert result.parity_skipped is True


def test_run_drc_not_flagged_when_netlist_fetched(monkeypatch, pcb):
    _fake_kicad_cli(monkeypatch, stdout="Found 0 violations\n")
    result = runner.run_drc(pcb)
    assert result.success is True
    assert result.parity_skipped is False


def test_run_drc_never_flags_without_parity_requested(monkeypatch, pcb):
    calls = _fake_kicad_cli(monkeypatch, stdout=PARITY_LINE)
    result = runner.run_drc(pcb, schematic_parity=False)
    assert "--schematic-parity" not in calls[0]
    assert result.parity_skipped is False


def test_parity_skipped_in_output_ignores_non_strings():
    assert runner.parity_skipped_in_output(None, b"Failed to fetch schematic netlist") is False
    assert runner.parity_skipped_in_output("", PARITY_LINE) is True


# --- DRCReport parsing + the shared status helper ----------------------------


def test_report_parses_schematic_parity_into_its_own_collection():
    data = dict(REPORT, violations=[WARNING_VIOLATION], schematic_parity=[PARITY_WARNING] * 2)
    report = parse_json_report(json.dumps(data))
    assert len(report.schematic_parity) == 2
    assert all(v.severity == Severity.WARNING for v in report.schematic_parity)
    assert all(v.type_str == "net_conflict" for v in report.schematic_parity)
    # Not merged into the geometric collection (the #4498 rule).
    assert [v.type_str for v in report.violations] == ["track_dangling"]
    assert report.schematic_parity_ran is None  # loaded from disk: unknown


def test_status_helper_vocabulary():
    warn = parse_json_report(json.dumps(dict(REPORT, schematic_parity=[PARITY_WARNING])))
    err = parse_json_report(json.dumps(dict(REPORT, schematic_parity=[PARITY_ERROR])))

    assert schematic_parity_status([], True).status == PARITY_PASSED
    not_run = schematic_parity_status([], False)
    assert (not_run.status, not_run.reason) == (PARITY_NOT_RUN, PARITY_REASON_KICAD_CLI_SKIPPED)
    unknown = schematic_parity_status([], None)
    assert (unknown.status, unknown.reason) == (PARITY_NOT_RUN, PARITY_REASON_REPORT_UNKNOWN)
    assert unknown.checked is False

    # Findings prove parity ran, whatever the run signal says.
    failed = schematic_parity_status(warn.schematic_parity, None)
    assert failed.status == PARITY_FAILED
    assert (failed.error_count, failed.warning_count) == (0, 1)
    assert failed.status_line() == "Schematic parity: FAILED (1 issue(s))"
    assert schematic_parity_status(err.schematic_parity, True).error_count == 1

    # All findings waived -> PASSED, with the waived count shown.
    warn.schematic_parity[0].waived = True
    waived = schematic_parity_status(warn.schematic_parity, None)
    assert waived.status == PARITY_PASSED
    assert waived.status_line() == "Schematic parity: PASSED [1 waived]"


# --- kct drc on a PCB: NOT RUN -----------------------------------------------


@pytest.mark.parametrize("fmt", ["table", "summary"])
def test_not_run_text_formats_exit_2(monkeypatch, capsys, pcb, fmt):
    _fake_kicad_cli(monkeypatch, stdout=PARITY_LINE)
    rc = drc_cmd.main([str(pcb), "--format", fmt])
    out = capsys.readouterr().out
    assert rc == 2  # NOT RUN never passes
    assert _status_lines(out) == [f"Schematic parity: NOT RUN ({NO_SCH_REASON})"]
    assert "DRC PASSED" not in out


def test_not_run_table_verdict_is_incomplete(monkeypatch, capsys, pcb):
    _fake_kicad_cli(monkeypatch, stdout=PARITY_LINE)
    drc_cmd.main([str(pcb)])
    out = capsys.readouterr().out
    assert out.rstrip().splitlines()[-1] == "DRC INCOMPLETE - Schematic parity NOT RUN"


def test_not_run_with_warnings_is_still_incomplete(monkeypatch, capsys, pcb):
    _fake_kicad_cli(monkeypatch, stdout=PARITY_LINE, violations=[WARNING_VIOLATION])
    rc = drc_cmd.main([str(pcb)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "DRC INCOMPLETE - Schematic parity NOT RUN; review warnings" in out


@pytest.mark.parametrize("fmt", ["table", "summary", "json"])
def test_allow_incomplete_opts_out(monkeypatch, capsys, pcb, fmt):
    _fake_kicad_cli(monkeypatch, stdout=PARITY_LINE)
    rc = drc_cmd.main([str(pcb), "--format", fmt, "--allow-incomplete"])
    out = capsys.readouterr().out
    assert rc == 0
    if fmt == "json":
        assert json.loads(out)["schematic_parity"]["status"] == PARITY_NOT_RUN
    else:
        # The status stays truthful; only the exit code changes.
        assert "NOT RUN" in _status_lines(out)[0]


def test_not_run_json(monkeypatch, capsys, pcb):
    _fake_kicad_cli(monkeypatch, stderr=PARITY_LINE)
    rc = drc_cmd.main([str(pcb), "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert data["schematic_parity"] == {
        "status": PARITY_NOT_RUN,
        "checked": False,
        "reason": NO_SCH_REASON,
        "error_count": 0,
        "warning_count": 0,
        "schematic": None,
        "issues": [],
    }
    assert data["schematic_parity_checked"] is False  # alias derived from status
    assert data["summary"]["parity_errors"] == 0
    assert data["summary"]["parity_warnings"] == 0


def test_strict_warnings_take_precedence_over_not_run(monkeypatch, pcb):
    _fake_kicad_cli(monkeypatch, stdout=PARITY_LINE, violations=[WARNING_VIOLATION])
    assert drc_cmd.main([str(pcb), "--strict", "--allow-incomplete"]) == 2


# --- kct drc on a PCB: parity ran --------------------------------------------


@pytest.mark.parametrize("fmt", ["table", "summary", "json"])
def test_parity_ran_clean_passes(monkeypatch, capsys, pcb, fmt):
    _fake_kicad_cli(monkeypatch, stdout="Found 0 violations\n")
    rc = drc_cmd.main([str(pcb), "--format", fmt])
    out = capsys.readouterr().out
    assert rc == 0
    if fmt == "json":
        data = json.loads(out)
        assert data["schematic_parity"]["status"] == PARITY_PASSED
        assert data["schematic_parity_checked"] is True
    else:
        assert _status_lines(out) == ["Schematic parity: PASSED"]
    if fmt == "table":
        assert "DRC PASSED - No violations found" in out


@pytest.mark.parametrize("fmt", ["table", "summary", "json"])
def test_parity_warning_findings_reported(monkeypatch, capsys, pcb, fmt):
    _fake_kicad_cli(monkeypatch, parity=[PARITY_WARNING, PARITY_WARNING])
    rc = drc_cmd.main([str(pcb), "--format", fmt])
    out = capsys.readouterr().out
    assert rc == 0  # warnings gate only under --strict
    if fmt == "json":
        data = json.loads(out)
        assert data["schematic_parity"]["status"] == PARITY_FAILED
        assert data["schematic_parity"]["warning_count"] == 2
        assert len(data["schematic_parity"]["issues"]) == 2
        assert data["schematic_parity"]["issues"][0]["severity"] == "warning"
        assert data["summary"]["parity_warnings"] == 2
        assert data["schematic_parity_checked"] is True
        assert data["violations"] == []  # not merged into geometric findings
    else:
        assert _status_lines(out) == ["Schematic parity: FAILED (2 issue(s))"]
        assert "SCHEMATIC PARITY (2 issue(s)):" in out
        assert "net_conflict" in out
    if fmt == "table":
        assert "DRC WARNING - Review warnings" in out
        assert "DRC PASSED" not in out


def test_parity_warning_findings_exit_2_under_strict(monkeypatch, pcb):
    _fake_kicad_cli(monkeypatch, parity=[PARITY_WARNING])
    assert drc_cmd.main([str(pcb), "--strict"]) == 2


@pytest.mark.parametrize("fmt", ["table", "json"])
def test_parity_error_findings_exit_1(monkeypatch, capsys, pcb, fmt):
    _fake_kicad_cli(monkeypatch, parity=[PARITY_ERROR])
    rc = drc_cmd.main([str(pcb), "--format", fmt, "--allow-incomplete"])
    out = capsys.readouterr().out
    assert rc == 1
    if fmt == "json":
        data = json.loads(out)
        assert data["summary"]["parity_errors"] == 1
        assert data["summary"]["errors"] == 0  # geometric count unchanged
    else:
        assert "DRC FAILED" in out


def test_waiver_applies_to_parity_findings(monkeypatch, capsys, pcb, tmp_path):
    _fake_kicad_cli(monkeypatch, parity=[PARITY_ERROR])
    waiver = tmp_path / "w.json"
    waiver.write_text(
        json.dumps(
            {
                "version": 2,
                "waivers": [
                    {
                        "rule": "missing_footprint",
                        "items": ["R9"],
                        "reason": "DNP part",
                        "issue": "#1",
                    }
                ],
            }
        )
    )
    rc = drc_cmd.main([str(pcb), "--waivers", str(waiver)])
    out = capsys.readouterr().out
    assert rc == 0
    assert _status_lines(out) == ["Schematic parity: PASSED [1 waived]"]


def test_mfr_shares_parity_exit_gate(monkeypatch, capsys, pcb):
    """Issue #7170 reversed #7077's separate --mfr rule: parity NOT RUN is
    INCOMPLETE (2) in --mfr mode too, unless --allow-incomplete."""
    _fake_kicad_cli(monkeypatch, stdout=PARITY_LINE)
    rc = drc_cmd.main([str(pcb), "--mfr", "jlcpcb"])
    out = capsys.readouterr().out
    assert rc == 2
    assert _status_lines(out) == [f"Schematic parity: NOT RUN ({NO_SCH_REASON})"]
    assert drc_cmd.main([str(pcb), "--mfr", "jlcpcb", "--allow-incomplete"]) == 0


def test_kct_drc_uses_shared_kicad_cli_locator(monkeypatch, pcb):
    """The kicad-cli found by find_kicad_cli() is the one invoked."""
    calls = _fake_kicad_cli(monkeypatch)
    monkeypatch.setattr(drc_cmd, "find_kicad_cli", lambda: Path("/located/kicad-cli"))
    drc_cmd.main([str(pcb), "--format", "json"])
    assert calls[0][0] == "/located/kicad-cli"


def test_kct_drc_forwards_allow_incomplete(monkeypatch, pcb):
    """``kct drc --allow-incomplete`` reaches drc_cmd (the #3159 drift trap)."""
    _fake_kicad_cli(monkeypatch, stdout=PARITY_LINE)
    assert cli_main(["drc", str(pcb)]) == 2
    assert cli_main(["drc", str(pcb), "--allow-incomplete"]) == 0


# --- report input ------------------------------------------------------------


def _no_kicad_cli(monkeypatch):
    def boom(*_a, **_k):  # a pre-existing report must not run kicad-cli
        raise AssertionError("kicad-cli must not run for report input")

    monkeypatch.setattr(runner, "run_kicad_drc_command", boom)


@pytest.mark.parametrize("fmt", ["table", "summary", "json"])
def test_report_input_empty_parity_is_not_run(monkeypatch, capsys, tmp_path, fmt):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(REPORT))
    _no_kicad_cli(monkeypatch)
    rc = drc_cmd.main([str(report_path), "--format", fmt])
    out = capsys.readouterr().out
    assert rc == 2  # an empty array cannot tell clean from never-ran
    if fmt == "json":
        data = json.loads(out)
        assert data["schematic_parity"]["status"] == PARITY_NOT_RUN
        assert data["schematic_parity"]["reason"] == PARITY_REASON_REPORT_UNKNOWN
        assert data["schematic_parity_checked"] is False
    else:
        assert _status_lines(out) == [f"Schematic parity: NOT RUN ({PARITY_REASON_REPORT_UNKNOWN})"]
    assert drc_cmd.main([str(report_path), "--format", fmt, "--allow-incomplete"]) == 0


def test_report_input_with_parity_findings_is_failed(monkeypatch, capsys, tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(dict(REPORT, schematic_parity=[PARITY_ERROR])))
    _no_kicad_cli(monkeypatch)
    rc = drc_cmd.main([str(report_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert _status_lines(out) == ["Schematic parity: FAILED (1 issue(s))"]


# --- kct drc-summary (same helper) -------------------------------------------


def test_drc_summary_shows_status_line(monkeypatch, capsys, pcb):
    _fake_kicad_cli(monkeypatch, stdout=PARITY_LINE)
    drc_summary.main([str(pcb)])
    out = capsys.readouterr().out
    assert [line.strip() for line in out.splitlines() if STATUS_PREFIX in line] == [
        f"Schematic parity: NOT RUN ({NO_SCH_REASON})"
    ]
    assert "VERDICT: INCOMPLETE - Schematic parity NOT RUN" in out


def test_drc_summary_json_parity_findings(monkeypatch, capsys, pcb):
    _fake_kicad_cli(monkeypatch, parity=[PARITY_WARNING, PARITY_WARNING])
    drc_summary.main([str(pcb), "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert data["schematic_parity"]["status"] == PARITY_FAILED
    assert data["schematic_parity"]["warning_count"] == 2
    assert data["verdict"] == "SCHEMATIC PARITY FAILED - 2 issue(s)"


# --- real kicad-cli (board 00) -----------------------------------------------

BOARD00 = Path(__file__).resolve().parents[1] / "boards" / "00-simple-led"


def _board00_copy(tmp_path: Path, *, with_schematic: bool) -> Path:
    if runner.find_kicad_cli() is None:
        pytest.skip("kicad-cli not installed")
    src = BOARD00 / "output"
    for suffix in ("kicad_pcb", "kicad_pro", "kicad_dru"):
        f = src / f"simple_led_routed.{suffix}"
        if f.exists():
            shutil.copy(f, tmp_path / f.name)
    if with_schematic:
        shutil.copy(src / "simple_led.kicad_sch", tmp_path / "simple_led_routed.kicad_sch")
    return tmp_path / "simple_led_routed.kicad_pcb"


@pytest.mark.integration
def test_real_kicad_cli_parity_findings_are_reported(tmp_path, capsys):
    """Board 00 + its schematic: kicad-cli 10.0.6 finds 2 ``net_conflict`` warnings."""
    pcb = _board00_copy(tmp_path, with_schematic=True)
    drc_cmd.main([str(pcb), "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    parity = data["schematic_parity"]
    assert parity["status"] == PARITY_FAILED
    assert parity["checked"] is True
    assert parity["warning_count"] >= 1
    assert {i["type_str"] for i in parity["issues"]} == {"net_conflict"}


@pytest.mark.integration
def test_real_kicad_cli_no_schematic_is_not_run(tmp_path, capsys):
    pcb = _board00_copy(tmp_path, with_schematic=False)
    rc = drc_cmd.main([str(pcb), "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert data["schematic_parity"]["status"] == PARITY_NOT_RUN
    assert data["schematic_parity"]["reason"] == parity_reason_no_schematic(pcb.name)
    if data["summary"]["errors"] == 0:
        assert rc == 2
