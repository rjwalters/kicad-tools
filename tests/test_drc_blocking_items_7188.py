"""One definition of "blocking DRC findings" across every entry point (Issue #7188).

Fixtures in ``tests/fixtures/drc_7188/`` are real kicad-cli 10.0.6 reports
(``pcb drc --format json --severity-all --schematic-parity``):

* **A** -- ``boards/00-simple-led/output/simple_led.kicad_pcb`` (unrouted):
  3 warning ``lib_footprint_mismatch``, **3 error ``unconnected_items``**.
* **B** -- board 00 routed plus a VCC via at (154.73, 88.75):
  **3 error ``clearance`` + 3 error ``hole_clearance``**, no opens.
* **B'** -- B's report with only the 3 ``hole_clearance`` errors kept.
* **C** -- ``boards/02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb``:
  clean.

``case_a.kicad_pcb`` / ``case_b.kicad_pcb`` are the boards behind A and B, for
the kicad-cli-free ``kct mfr validate`` path; C uses the committed board 02.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kicad_tools.drc import DRCReport, Severity, ViolationType
from kicad_tools.drc.violation import DRCViolation

FIXTURES = Path(__file__).parent / "fixtures" / "drc_7188"
REPO = Path(__file__).resolve().parent.parent
BOARD_02 = REPO / "boards/02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb"

REPORTS = {
    "A": FIXTURES / "case_a.drc.json",
    "B": FIXTURES / "case_b.drc.json",
    "B'": FIXTURES / "case_b_hole_only.drc.json",
    "C": FIXTURES / "case_c.drc.json",
}


def _open(severity: Severity = Severity.ERROR, *, waived: bool = False) -> DRCViolation:
    return DRCViolation(
        type=ViolationType.UNCONNECTED_ITEMS,
        type_str="unconnected_items",
        severity=severity,
        message="Missing connection between items",
        nets=["VCC"],
        waived=waived,
    )


def _clearance(severity: Severity = Severity.ERROR) -> DRCViolation:
    return DRCViolation(
        type=ViolationType.CLEARANCE,
        type_str="clearance",
        severity=severity,
        message="Clearance violation",
    )


def _report(**kwargs) -> DRCReport:
    return DRCReport(source_file="t.json", created_at=None, pcb_name="t.kicad_pcb", **kwargs)


# ---------------------------------------------------------------------------
# The DRCReport helpers
# ---------------------------------------------------------------------------


class TestReportHelpers:
    def test_json_shape_opens_are_blocking(self):
        report = _report(violations=[_clearance()], unconnected_items=[_open(), _open()])
        assert len(report.connectivity_errors()) == 2
        assert report.blocking_count == 3
        # #4498: the geometric-only meaning is unchanged.
        assert report.error_count == 1
        assert report.passed is False

    def test_text_shape_opens_not_double_counted(self):
        """A text ``.rpt`` keeps its opens in ``violations``; count them once."""
        report = _report(violations=[_clearance(), _open()])
        assert report.connectivity_errors() == [report.violations[1]]
        assert report.blocking_count == 2
        assert report.error_count == 2

    def test_waived_and_warning_opens_excluded(self):
        report = _report(
            unconnected_items=[_open(waived=True), _open(Severity.WARNING)],
        )
        assert report.connectivity_errors() == []
        assert report.blocking_items() == []
        assert report.blocking_count == 0
        assert len(report.connectivity_items()) == 2

    def test_schematic_parity_not_blocking(self):
        parity = DRCViolation(
            type=ViolationType.from_string("net_conflict"),
            type_str="net_conflict",
            severity=Severity.ERROR,
            message="Pad net mismatch",
        )
        assert _report(schematic_parity=[parity]).blocking_count == 0

    @pytest.mark.parametrize(
        ("case", "blocking", "errors", "opens"),
        [("A", 3, 0, 3), ("B", 6, 6, 0), ("B'", 3, 3, 0), ("C", 0, 0, 0)],
    )
    def test_cases(self, case, blocking, errors, opens):
        report = DRCReport.load(REPORTS[case])
        assert report.blocking_count == blocking
        assert report.error_count == errors
        assert len(report.connectivity_errors()) == opens

    def test_inline_copies_replaced(self):
        """The helper is the only "error-severity opens" definition in src/."""
        hits = [
            path.relative_to(REPO).as_posix()
            for path in (REPO / "src").rglob("*.py")
            if "connectivity_items() if v.is_error" in path.read_text()
        ]
        assert hits == ["src/kicad_tools/drc/report.py"]


# ---------------------------------------------------------------------------
# kicad-drc-summary
# ---------------------------------------------------------------------------


def _summary(case: str, fab: bool, capsys) -> tuple[int, dict]:
    from kicad_tools.cli.drc_summary import main

    argv = [str(REPORTS[case]), "--format", "json"]
    if fab:
        argv += ["--fab", "jlcpcb"]
    rc = main(argv)
    return rc, json.loads(capsys.readouterr().out)


class TestDrcSummary:
    @pytest.mark.parametrize("fab", [False, True])
    def test_case_a_unrouted_blocks(self, fab, capsys):
        rc, data = _summary("A", fab, capsys)
        assert rc == 1
        assert data["blocking"]["by_type"] == {"unconnected_items": 3}
        assert data["verdict"].startswith("BLOCKING")
        assert data["unconnected_by_net"]
        assert data["total_violations"] == 6  # 3 mismatches + 3 opens

    @pytest.mark.parametrize("fab", [False, True])
    def test_case_b_hole_clearance_blocks(self, fab, capsys):
        rc, data = _summary("B", fab, capsys)
        assert rc == 1
        assert data["blocking"]["by_type"] == {"clearance": 3, "hole_clearance": 3}
        assert data["counts"]["fab_acceptable"] == 0
        assert data["counts"]["warning"] == 0

    @pytest.mark.parametrize("fab", [False, True])
    def test_case_b_prime_hole_clearance_only(self, fab, capsys):
        rc, data = _summary("B'", fab, capsys)
        assert rc == 1
        assert data["blocking"]["by_type"] == {"hole_clearance": 3}
        assert data["counts"]["warning"] == 0

    @pytest.mark.parametrize("fab", [False, True])
    def test_case_c_clean(self, fab, capsys):
        rc, data = _summary("C", fab, capsys)
        assert rc == 0
        assert data["counts"]["blocking"] == 0

    def test_table_output_case_a(self, capsys):
        from kicad_tools.cli.drc_summary import main

        assert main([str(REPORTS["A"])]) == 1
        out = capsys.readouterr().out
        assert "BLOCKING (3 violations)" in out
        assert "X 3 unconnected_items" in out
        assert "VERDICT: BLOCKING" in out

    def test_fab_pass_still_demotes_error_clearance(self):
        """#1133: an error clearance above the fab floor stays FAB_ACCEPTABLE."""
        from kicad_tools.cli.drc_summary import create_summary

        v = _clearance()
        v.actual_value_mm = 0.15  # > JLCPCB 0.127
        summary = create_summary(_report(violations=[v]), manufacturer_id="jlcpcb")
        assert not summary.has_blocking
        assert summary.fab_acceptable_count == 1

    def test_warning_severity_unknown_type_stays_warning(self):
        from kicad_tools.cli.drc_summary import create_summary

        v = DRCViolation(
            type=ViolationType.DRILL_CLEARANCE,
            type_str="hole_clearance",
            severity=Severity.WARNING,
            message="Hole clearance",
        )
        summary = create_summary(_report(violations=[v]))
        assert not summary.has_blocking
        assert summary.warning_count == 1


# ---------------------------------------------------------------------------
# Project.check_drc()
# ---------------------------------------------------------------------------


class TestProjectCheckDrc:
    def _checks(self, case: str):
        from kicad_tools.project import Project

        return Project().check_drc(manufacturer="jlcpcb", report_path=REPORTS[case])

    def test_case_a_returns_opens_as_connection_fail(self):
        from kicad_tools.drc.checker import CheckResult

        checks = self._checks("A")
        assert len(checks) == 6
        conn = [c for c in checks if c.rule_name == "connection"]
        assert len(conn) == 3
        assert all(c.result == CheckResult.FAIL for c in conn)
        assert not any(c.is_compatible for c in conn)

    def test_case_b_unchanged(self):
        assert len(self._checks("B")) == 6

    def test_case_c_empty(self):
        assert self._checks("C") == []

    def test_default_keyword_keeps_old_shape(self):
        """``kct drc --mfr`` (default keyword) does not get the opens as checks."""
        from kicad_tools.drc import check_manufacturer_rules

        report = DRCReport.load(REPORTS["A"])
        checks = check_manufacturer_rules(report, "jlcpcb")
        assert not any(c.rule_name == "connection" for c in checks)


# ---------------------------------------------------------------------------
# kct mfr validate
# ---------------------------------------------------------------------------


def _validate(pcb: Path, capsys, *extra: str) -> tuple[int, dict]:
    from kicad_tools.cli.mfr import main

    try:
        main(["validate", str(pcb), "jlcpcb", "--format", "json", *extra])
        rc = 0
    except SystemExit as exc:
        rc = int(exc.code or 0)
    return rc, json.loads(capsys.readouterr().out)


class TestMfrValidate:
    def test_case_a_unrouted_fails(self, capsys):
        rc, data = _validate(FIXTURES / "case_a.kicad_pcb", capsys)
        assert rc == 1
        assert data["ok"] is False
        conn = [v for v in data["violations"] if v["rule_id"] == "connectivity"]
        assert len(conn) == 3
        assert all(v["severity"] == "error" and v["type"] and v["detail"] for v in conn)

    def test_case_b_clearance_fails(self, capsys):
        rc, data = _validate(FIXTURES / "case_b.kicad_pcb", capsys)
        assert rc == 1
        assert any(v["rule_id"].startswith("clearance") for v in data["violations"])

    def test_case_c_passes(self, capsys):
        if not BOARD_02.exists():
            pytest.skip("board 02 routed PCB not present")
        rc, data = _validate(BOARD_02, capsys)
        assert rc == 0
        assert data["ok"] is True
        assert data["violations"] == []
        assert set(data) >= {
            "file",
            "manufacturer",
            "layers",
            "copper_oz",
            "rules",
            "violations",
            "warnings",
            "ok",
        }

    def test_text_mode_no_old_success_claim(self, capsys):
        from kicad_tools.cli.mfr import main

        with pytest.raises(SystemExit) as exc:
            main(["validate", str(FIXTURES / "case_a.kicad_pcb"), "jlcpcb"])
        out = capsys.readouterr().out
        assert exc.value.code == 1
        assert "[connectivity]" in out
        assert "design meets manufacturer requirements" not in out

    def test_text_mode_clean(self, capsys):
        from kicad_tools.cli.mfr import main

        if not BOARD_02.exists():
            pytest.skip("board 02 routed PCB not present")
        main(["validate", str(BOARD_02), "jlcpcb"])
        out = capsys.readouterr().out
        assert "No errors from the kct DRC engine for JLCPCB (2-layer)." in out
        assert "kct drc" in out

    def test_sub_minimum_pad_drill_fails(self, tmp_path, capsys):
        """The #4892 pad-drill check survives the engine swap."""
        if not BOARD_02.exists():
            pytest.skip("board 02 routed PCB not present")
        pcb = tmp_path / "small_drill.kicad_pcb"
        text = BOARD_02.read_text()
        # Shrink one through-hole pad drill far below any fab minimum.
        idx = text.index("(drill ", text.index("thru_hole"))
        end = text.index(")", idx)
        pcb.write_text(text[:idx] + "(drill 0.05" + text[end:])
        rc, data = _validate(pcb, capsys)
        assert rc == 1
        assert any(v["type"] == "HOLE_SIZE" for v in data["violations"])


# ---------------------------------------------------------------------------
# kct audit DRC row
# ---------------------------------------------------------------------------


def _geometric_result(case: str):
    from kicad_tools.drc import GeometricDRCResult

    report = DRCReport.load(REPORTS[case])
    errors = report.errors
    by_type: dict[str, int] = {}
    for v in errors:
        by_type[v.type_str] = by_type.get(v.type_str, 0) + 1
    return GeometricDRCResult(
        ran=True,
        error_count=len(errors),
        by_type=by_type,
        unconnected_items=list(report.unconnected_items),
        error_violations=errors,
    )


class TestAuditDrcRow:
    def _merge(self, case: str, tmp_path, monkeypatch):
        import kicad_tools.drc as drc_mod
        from kicad_tools.audit.auditor import DRCStatus, ManufacturingAudit

        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        monkeypatch.setattr(drc_mod, "run_geometric_drc", lambda *a, **k: _geometric_result(case))
        status = DRCStatus(error_count=0, blocking_count=0, passed=True)
        ManufacturingAudit(pcb, manufacturer="jlcpcb")._merge_geometric_drc(status)
        return status

    def test_case_a_folds_kicad_cli_opens(self, tmp_path, monkeypatch):
        status = self._merge("A", tmp_path, monkeypatch)
        assert status.passed is False
        assert status.error_count == 3
        assert status.blocking_count == 3
        assert status.violations_by_type == {"kicad-cli:unconnected_items": 3}
        assert "kicad-cli: unconnected_items (3)" in status.details

    def test_case_b_geometric_only(self, tmp_path, monkeypatch):
        status = self._merge("B", tmp_path, monkeypatch)
        assert status.blocking_count == 6
        assert "kicad-cli:unconnected_items" not in status.violations_by_type

    def test_case_c_stays_clean(self, tmp_path, monkeypatch):
        status = self._merge("C", tmp_path, monkeypatch)
        assert status.passed is True
        assert status.blocking_count == 0

    def test_warning_opens_not_folded(self, tmp_path, monkeypatch):
        import kicad_tools.drc as drc_mod
        from kicad_tools.audit.auditor import DRCStatus, ManufacturingAudit
        from kicad_tools.drc import GeometricDRCResult

        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        monkeypatch.setattr(
            drc_mod,
            "run_geometric_drc",
            lambda *a, **k: GeometricDRCResult(
                ran=True, unconnected_items=[_open(Severity.WARNING)]
            ),
        )
        status = DRCStatus(error_count=0, blocking_count=0, passed=True)
        ManufacturingAudit(pcb, manufacturer="jlcpcb")._merge_geometric_drc(status)
        assert status.passed is True
        assert status.error_count == 0


# ---------------------------------------------------------------------------
# kct readiness native-DRC gate count
# ---------------------------------------------------------------------------


def test_readiness_gate_counts_only_error_opens(tmp_path, monkeypatch):
    from kicad_tools.cli import readiness_cmd

    item = {"description": "Pad 1 [VCC] of R1", "uuid": "u1", "pos": {"x": 1, "y": 1}}

    def _entry(severity: str) -> dict:
        return {
            "type": "unconnected_items",
            "severity": severity,
            "description": "Missing connection between items",
            "items": [item, {**item, "uuid": "u2"}],
        }

    data = {
        "$schema": "https://schemas.kicad.org/drc.v1.json",
        "source": "board.kicad_pcb",
        "date": "2026-10-18T00:00:00",
        "kicad_version": "10.0.6",
        "coordinate_units": "mm",
        "violations": [],
        "unconnected_items": [_entry("error"), _entry("warning")],
        "schematic_parity": [],
    }

    def fake_cli(args, output):
        output.write_text(json.dumps(data))
        return readiness_cmd.EngineRun(True)

    monkeypatch.setattr(readiness_cmd, "_run_kicad_cli", fake_cli)
    pcb = tmp_path / "board.kicad_pcb"
    shutil.copy(FIXTURES / "case_a.kicad_pcb", pcb)
    report_path = tmp_path / "native-drc.json"
    assert readiness_cmd._run_native_drc(pcb, report_path).ok
    payload = json.loads(report_path.read_text())
    assert payload["error_count"] == 1
    assert payload["by_type"] == {"unconnected_items": 1}
    # Informational field keeps meaning *every* open (schema unchanged).
    assert payload["unconnected_count"] == 2
