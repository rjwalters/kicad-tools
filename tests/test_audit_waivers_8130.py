"""Issue #8130: ManufacturingAudit._check_drc honours the waivers sidecar."""

import json

import pytest

import kicad_tools.drc as drc_mod
import kicad_tools.validate as validate_mod
import kicad_tools.validate.evidence as evidence_mod
from kicad_tools.audit import ManufacturingAudit
from kicad_tools.drc import GeometricDRCResult
from kicad_tools.validate.violations import DRCResults, DRCViolation


class _DummyPcb:
    pass


def _violation(rule="clearance_pad_pad", items=("C1", "R1")):
    return DRCViolation(rule_id=rule, severity="error", message="x", items=tuple(items))


def _audit(tmp_path, monkeypatch, violations):
    pcb_path = tmp_path / "board.kicad_pcb"
    pcb_path.write_text("(kicad_pcb)")

    class _Checker:
        ADVISORY_RULE_IDS = frozenset()

        def __init__(self, *a, **k):
            pass

        def check_all(self, *a, **k):
            return DRCResults(violations=list(violations))

        @classmethod
        def is_advisory_rule(cls, rule_id):
            return False

    monkeypatch.setattr(validate_mod, "DRCChecker", _Checker)
    monkeypatch.setattr(evidence_mod, "annotate_evidence", lambda *a, **k: None)
    monkeypatch.setattr(
        drc_mod, "run_geometric_drc", lambda *a, **k: GeometricDRCResult(ran=False, note="skipped")
    )
    return ManufacturingAudit(pcb_path, manufacturer="jlcpcb"), pcb_path


def _write_sidecar(pcb_path, entries):
    sidecar = pcb_path.parent / f"{pcb_path.stem}.kct-waivers.json"
    sidecar.write_text(json.dumps({"version": 2, "waivers": entries}))


def test_waived_finding_not_blocking(tmp_path, monkeypatch):
    audit, pcb_path = _audit(tmp_path, monkeypatch, [_violation()])
    _write_sidecar(
        pcb_path,
        [{"rule": "clearance_pad_pad", "items": ["C1", "R1"], "reason": "ok", "issue": "x#1"}],
    )
    status = audit._check_drc(_DummyPcb())
    assert status.blocking_count == 0
    assert status.error_count == 0
    assert status.passed is True
    assert status.waived_count == 1
    assert status.waived_by_rule["clearance_pad_pad"]["count"] == 1
    assert status.waived_by_rule["clearance_pad_pad"]["reasons"] == ["ok"]
    assert status.to_dict()["waived_count"] == 1


def test_unwaived_finding_still_blocks(tmp_path, monkeypatch):
    audit, pcb_path = _audit(tmp_path, monkeypatch, [_violation(), _violation(items=("C2", "R2"))])
    _write_sidecar(
        pcb_path,
        [{"rule": "clearance_pad_pad", "items": ["C1", "R1"], "reason": "ok", "issue": "x#1"}],
    )
    status = audit._check_drc(_DummyPcb())
    assert status.blocking_count == 1
    assert status.waived_count == 1
    assert status.passed is False


def test_waiver_for_other_rule_does_not_apply(tmp_path, monkeypatch):
    audit, pcb_path = _audit(tmp_path, monkeypatch, [_violation()])
    _write_sidecar(
        pcb_path, [{"rule": "other_rule", "items": ["C1", "R1"], "reason": "ok", "issue": "x#1"}]
    )
    status = audit._check_drc(_DummyPcb())
    assert status.blocking_count == 1
    assert status.waived_count == 0


def test_malformed_sidecar_warns_and_applies_nothing(tmp_path, monkeypatch):
    audit, pcb_path = _audit(tmp_path, monkeypatch, [_violation()])
    (tmp_path / "board.kct-waivers.json").write_text("{not json")
    status = audit._check_drc(_DummyPcb())
    assert status.blocking_count == 1
    assert status.waived_count == 0


def test_no_sidecar_unchanged(tmp_path, monkeypatch):
    audit, _ = _audit(tmp_path, monkeypatch, [_violation()])
    status = audit._check_drc(_DummyPcb())
    assert status.blocking_count == 1
    assert "waived_count" not in status.to_dict()


@pytest.mark.parametrize("n", [3])
def test_many_waived_counted(tmp_path, monkeypatch, n):
    vs = [_violation(items=(f"C{i}", "R1")) for i in range(n)]
    audit, pcb_path = _audit(tmp_path, monkeypatch, vs)
    _write_sidecar(
        pcb_path,
        [
            {"rule": "clearance_pad_pad", "items": [f"C{i}", "R1"], "reason": "r", "issue": "x#1"}
            for i in range(n)
        ],
    )
    status = audit._check_drc(_DummyPcb())
    assert status.waived_count == n and status.blocking_count == 0


def test_waived_only_errors_verdict_not_not_ready(tmp_path, monkeypatch):
    from kicad_tools.audit.auditor import AuditResult, AuditVerdict

    audit, pcb_path = _audit(tmp_path, monkeypatch, [_violation()])
    _write_sidecar(
        pcb_path,
        [{"rule": "clearance_pad_pad", "items": ["C1", "R1"], "reason": "ok", "issue": "x#1"}],
    )
    result = AuditResult(drc=audit._check_drc(_DummyPcb()))
    assert result.drc.blocking_count == 0
    assert result.verdict != AuditVerdict.NOT_READY

    # Control: the same finding without a waiver is NOT_READY.
    (tmp_path / "board.kct-waivers.json").unlink()
    result = AuditResult(drc=audit._check_drc(_DummyPcb()))
    assert result.verdict == AuditVerdict.NOT_READY


def test_stale_evidence_waiver_leaves_finding_blocking(tmp_path, monkeypatch):
    audit, pcb_path = _audit(tmp_path, monkeypatch, [_violation()])
    sidecar = pcb_path.parent / f"{pcb_path.stem}.kct-waivers.json"
    sidecar.write_text(
        json.dumps(
            {
                "version": 3,
                "waivers": [
                    {
                        "rule": "clearance_pad_pad",
                        "items": ["C1", "R1"],
                        "reason": "ok",
                        "issue": "x#1",
                        "evidence_hash": "ev2:0000000000000000",
                    }
                ],
            }
        )
    )
    status = audit._check_drc(_DummyPcb())
    assert status.blocking_count >= 1
    assert status.waived_count == 0
