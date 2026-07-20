"""Tests for the ``kct audit`` HV/isolation (creepage) section (Issue #4333).

Phase-3 integration of the phase-1/phase-2 creepage engine into the
manufacturing-readiness audit.  The audit consumes the engine end-to-end
(``resolve_hv_nets`` + ``compute_creepage_census`` + the IEC standard-table
derivation) and folds a below-standard HV pair into the ``NOT_READY`` verdict
so ``kct audit`` exits 2 (the manufacturing-readiness gate FAILs).

Two layers of tests:

* **Behavioral** (via ``ManufacturingAudit.run()``): assert on
  ``result.isolation.*`` and rendered output.  The synthetic single-pad
  fixtures are inherently ``NOT_READY`` for unrelated reasons (unrouted nets,
  no zones), so these tests never assert the *aggregate* verdict.
* **Verdict roll-up** (``TestVerdictRollup``): construct an otherwise-clean
  ``AuditResult`` and vary only ``isolation`` to prove its verdict / exit-code
  contribution in isolation.

CI-safety: these tests use the synthetic creepage fixtures
(``tests/creepage/fixtures.py``), NEVER the local-only softstart board.  The
``L_MAINS`` net does not classify to ``HV`` by name, so a ``net_class_map``
sidecar maps it explicitly (same pattern as ``test_creepage_cli.py``).
"""

from __future__ import annotations

import json

import pytest

from kicad_tools._shapely import has_shapely
from kicad_tools.audit import AuditResult, AuditVerdict, IsolationStatus, ManufacturingAudit
from kicad_tools.cli import audit_cmd
from tests.creepage.fixtures import (
    board_close_hv_source,
    board_no_hv_source,
    board_source,
)

pytestmark = pytest.mark.skipif(not has_shapely(), reason="creepage requires shapely")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _write(tmp_path, source, name="board.kicad_pcb"):
    p = tmp_path / name
    p.write_text(source)
    return p


def _hv_map_file(tmp_path):
    p = tmp_path / "net_class_map.json"
    p.write_text(json.dumps({"L_MAINS": {"name": "HV"}}))
    return p


def _audit(pcb, ncm, **hv):
    return ManufacturingAudit(
        pcb,
        manufacturer="jlcpcb",
        net_class_map_path=ncm,
        **hv,
    )


# --------------------------------------------------------------------------
# Behavioral: section + PASS (phase-2 standard mode)
# --------------------------------------------------------------------------


def test_standard_mode_pass_section_rendered(tmp_path, capsys):
    """A compliant HV board: checked + passed, section renders derived values."""
    pcb = _write(tmp_path, board_source(with_slot=True))
    ncm = _hv_map_file(tmp_path)
    # iec60664 @ 250 V RMS, PD2, IIIa -> ~2.5 mm creepage; the ~18 mm gap clears it.
    audit = _audit(
        pcb,
        ncm,
        hv_standard="iec60664",
        hv_working_voltage=250.0,
        hv_pollution_degree=2,
        hv_material_group="IIIa",
    )
    result = audit.run()
    iso = result.isolation

    assert iso.hv_present is True
    assert iso.checked is True
    assert iso.passed is True
    assert iso.hv_nets == ["L_MAINS"]
    assert iso.standard == "iec60664"
    assert iso.required_creepage_mm is not None
    assert iso.required_clearance_mm is not None
    # Measured creepage/clearance are distinct, populated values.
    assert iso.min_creepage_mm is not None and iso.min_clearance_mm is not None
    # A compliant HV pair does NOT contribute a hard fail.
    assert not (iso.checked and not iso.passed)

    audit_cmd.output_table(result, verbose=True)
    out = capsys.readouterr().out
    assert "HV / Isolation" in out
    assert "Creepage" in out and "Clearance" in out
    assert "iec60664" in out
    # The standards disclaimer must accompany a derived-value section.
    assert "certification" in out.lower()


# --------------------------------------------------------------------------
# Behavioral: below-standard pair captured (gate-FAIL source)
# --------------------------------------------------------------------------


def test_below_standard_pair_fails_and_is_not_ready(tmp_path):
    """A too-close HV pair -> isolation FAIL -> verdict NOT_READY."""
    pcb = _write(tmp_path, board_close_hv_source())
    ncm = _hv_map_file(tmp_path)
    audit = _audit(
        pcb,
        ncm,
        hv_standard="iec60664",
        hv_working_voltage=250.0,
        hv_pollution_degree=2,
        hv_material_group="IIIa",
    )
    result = audit.run()

    assert result.isolation.checked is True
    assert result.isolation.passed is False
    assert result.isolation.failing_pairs  # at least one failing pair captured
    # Independent of the fixture's other NOT_READY reasons, isolation FAIL is a
    # hard-fail contributor, so the aggregate verdict is NOT_READY.
    assert result.verdict == AuditVerdict.NOT_READY


def test_gate_exit_code_2_on_fail(tmp_path, capsys):
    """``kct audit`` returns exit 2 (gate FAIL) for a below-standard HV pair."""
    pcb = _write(tmp_path, board_close_hv_source())
    ncm = _hv_map_file(tmp_path)
    rc = audit_cmd.main(
        [
            str(pcb),
            "--skip-erc",
            "--net-class-map",
            str(ncm),
            "--hv-standard",
            "iec60664",
            "--hv-working-voltage",
            "250",
            "--hv-pollution-degree",
            "2",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 2
    assert "HV / Isolation" in out
    assert "FAIL" in out


# --------------------------------------------------------------------------
# Behavioral: phase-1 manual --hv-min governs identically
# --------------------------------------------------------------------------


def test_phase1_min_governs_pass_and_fail(tmp_path):
    """Manual --hv-min with no standard gates pass/fail; report has no standard."""
    ncm = _hv_map_file(tmp_path)

    # PASS: 1.5 mm required, ~18 mm measured.
    pcb_ok = _write(tmp_path, board_source(with_slot=False), name="ok.kicad_pcb")
    res_ok = _audit(pcb_ok, ncm, hv_min_mm=1.5).run()
    assert res_ok.isolation.checked is True
    assert res_ok.isolation.passed is True
    assert res_ok.isolation.standard is None
    # Phase-1 dict schema: no 'standard' key in the embedded report.
    assert "standard" not in res_ok.isolation.report

    # FAIL: 100 mm required, ~18 mm measured.
    pcb_bad = _write(tmp_path, board_source(with_slot=False), name="bad.kicad_pcb")
    res_bad = _audit(pcb_bad, ncm, hv_min_mm=100.0).run()
    assert res_bad.isolation.passed is False
    assert res_bad.verdict == AuditVerdict.NOT_READY


# --------------------------------------------------------------------------
# Behavioral: HV present but no threshold -> no gate (rendered informationally)
# --------------------------------------------------------------------------


def test_hv_present_no_threshold_not_checked(tmp_path, capsys):
    """HV nets present but neither --hv-min nor --hv-standard -> not checked."""
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    result = _audit(pcb, ncm).run()
    iso = result.isolation

    assert iso.hv_present is True
    assert iso.threshold_supplied is False
    assert iso.checked is False
    assert "no isolation requirement" in iso.details.lower()

    audit_cmd.output_table(result)
    out = capsys.readouterr().out
    assert "HV / Isolation" in out
    assert "NO REQUIREMENT SPECIFIED" in out


# --------------------------------------------------------------------------
# Behavioral: no HV nets -> zero regression
# --------------------------------------------------------------------------


def test_no_hv_nets_no_section(tmp_path, capsys):
    """A non-HV board: no section, checked=False, hv_present=False."""
    pcb = _write(tmp_path, board_no_hv_source())
    ncm = _hv_map_file(tmp_path)
    result = _audit(
        pcb,
        ncm,
        hv_standard="iec60664",
        hv_working_voltage=250.0,
        hv_pollution_degree=2,
    ).run()
    iso = result.isolation

    assert iso.hv_present is False
    assert iso.checked is False
    assert iso.passed is True

    audit_cmd.output_table(result)
    out = capsys.readouterr().out
    assert "HV / Isolation" not in out


def test_no_hv_nets_json_isolation_inert(tmp_path):
    """No-HV board: JSON carries an inert isolation object (checked=false)."""
    pcb = _write(tmp_path, board_no_hv_source())
    ncm = _hv_map_file(tmp_path)
    result = _audit(pcb, ncm, hv_min_mm=1.5).run()
    data = result.to_dict()

    assert "isolation" in data
    assert data["isolation"]["checked"] is False
    assert data["isolation"]["hv_present"] is False
    assert data["isolation"]["report"] == {}
    # Pre-existing keys remain present (no JSON drift).
    for key in ("erc", "drc", "sync", "connectivity", "compatibility", "layers", "cost"):
        assert key in data


# --------------------------------------------------------------------------
# Behavioral: JSON schema -- isolation.report matches standard-mode census
# --------------------------------------------------------------------------


def test_json_isolation_report_standard_schema(tmp_path, capsys):
    """--format json embeds the standard-mode CreepageReport schema verbatim."""
    pcb = _write(tmp_path, board_source(with_slot=True))
    ncm = _hv_map_file(tmp_path)
    audit_cmd.main(
        [
            str(pcb),
            "--skip-erc",
            "--format",
            "json",
            "--net-class-map",
            str(ncm),
            "--hv-standard",
            "iec60664",
            "--hv-working-voltage",
            "250",
            "--hv-pollution-degree",
            "2",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["isolation"]["passed"] is True

    report = payload["isolation"]["report"]
    # Standard-mode schema fields (mirrors `kct creepage --format json`).
    for key in (
        "standard",
        "standard_edition",
        "required_creepage_mm",
        "required_clearance_mm",
        "creepage_provenance",
        "clearance_provenance",
        "pairs",
        "pair_count",
        "passed",
    ):
        assert key in report
    assert report["standard"] == "iec60664"
    assert report["hv_nets"] == ["L_MAINS"]
    # Per-pair clearance-vs-creepage distinction is preserved.
    assert report["pairs"]
    for pair in report["pairs"]:
        assert "clearance_mm" in pair and "creepage_mm" in pair


# --------------------------------------------------------------------------
# Behavioral: edge cases -- StandardLookupError / missing inputs degrade
# --------------------------------------------------------------------------


def test_standard_lookup_error_surfaces_not_crash(tmp_path):
    """A bad voltage (exceeds tabulated rows) -> could_not_verify, not a crash."""
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    result = _audit(
        pcb,
        ncm,
        hv_standard="iec60664",
        hv_working_voltage=999999.0,  # far above the highest tabulated row
        hv_pollution_degree=2,
    ).run()
    iso = result.isolation

    assert iso.hv_present is True
    assert iso.could_not_verify is True
    assert iso.checked is False


def test_standard_without_voltage_could_not_verify(tmp_path):
    """--hv-standard without working-voltage/PD -> could_not_verify."""
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    result = _audit(pcb, ncm, hv_standard="iec60664").run()
    iso = result.isolation

    assert iso.hv_present is True
    assert iso.could_not_verify is True
    assert iso.checked is False


# --------------------------------------------------------------------------
# Verdict roll-up: isolation's contribution proven on an otherwise-clean result
# --------------------------------------------------------------------------


class TestVerdictRollup:
    """Prove isolation's verdict/exit-code contribution in isolation.

    A default ``AuditResult`` with ``drc.geometric_drc_ran=True`` and all other
    sub-checks passing rolls up to ``READY``; varying only ``isolation`` shows
    each transition without interference from the synthetic fixtures' unrelated
    ``NOT_READY`` causes (unrouted nets, no zones).
    """

    @staticmethod
    def _clean_result() -> AuditResult:
        result = AuditResult(project_name="clean")
        # Make the DRC authoritative so the baseline is READY, not WARNING.
        result.drc.geometric_drc_ran = True
        return result

    def test_baseline_ready_no_isolation(self):
        """Zero regression: default (no HV) isolation leaves the verdict READY."""
        result = self._clean_result()
        assert result.isolation.hv_present is False
        assert result.verdict == AuditVerdict.READY

    def test_compliant_isolation_stays_ready(self):
        """A compliant, checked HV pair keeps the board READY (exit 0)."""
        result = self._clean_result()
        result.isolation = IsolationStatus(
            checked=True, hv_present=True, threshold_supplied=True, passed=True
        )
        assert result.verdict == AuditVerdict.READY

    def test_below_standard_isolation_not_ready(self):
        """A below-standard HV pair (checked, not passed) -> NOT_READY (exit 2)."""
        result = self._clean_result()
        result.isolation = IsolationStatus(
            checked=True, hv_present=True, threshold_supplied=True, passed=False
        )
        assert result.verdict == AuditVerdict.NOT_READY

    def test_hv_present_no_threshold_warns(self):
        """HV present with no requirement specified -> WARNING (no silent green)."""
        result = self._clean_result()
        result.isolation = IsolationStatus(checked=False, hv_present=True, threshold_supplied=False)
        assert result.verdict == AuditVerdict.WARNING

    def test_could_not_verify_warns(self):
        """HV present but shapely-absent / lookup error -> WARNING (fail-loud)."""
        result = self._clean_result()
        result.isolation = IsolationStatus(
            hv_present=True, threshold_supplied=True, could_not_verify=True
        )
        assert result.verdict == AuditVerdict.WARNING

    def test_exit_code_maps_from_verdict(self, tmp_path, monkeypatch, capsys):
        """The CLI exit code reflects the isolation-driven verdict.

        FAIL -> 2 (no --strict needed); compliant -> 0.
        """
        pcb = tmp_path / "b.kicad_pcb"
        pcb.write_text(board_source(with_slot=False))

        # Force a below-standard isolation FAIL through a stubbed audit run.
        fail_result = self._clean_result()
        fail_result.isolation = IsolationStatus(
            checked=True, hv_present=True, threshold_supplied=True, passed=False
        )
        monkeypatch.setattr(audit_cmd.ManufacturingAudit, "run", lambda self: fail_result)
        assert audit_cmd.main([str(pcb), "--skip-erc"]) == 2
        capsys.readouterr()

        # A compliant isolation on an otherwise-clean board exits 0.
        ok_result = self._clean_result()
        ok_result.isolation = IsolationStatus(
            checked=True, hv_present=True, threshold_supplied=True, passed=True
        )
        monkeypatch.setattr(audit_cmd.ManufacturingAudit, "run", lambda self: ok_result)
        assert audit_cmd.main([str(pcb), "--skip-erc"]) == 0
