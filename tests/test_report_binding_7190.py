"""Issue #7190: auto-discovered DRC/ERC reports must be bound to file content.

Rule: an auto-discovered report that is not bound to the current file's
content can fail a board, but can never pass it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kicad_tools.drc.report import DRCReport
from kicad_tools.drc.report_binding import (
    DRC_HASH_KEY,
    ERC_HASH_KEY,
    BindingStatus,
    binding_status,
    schematic_digest,
    stamp_report_hash,
)
from kicad_tools.erc.report import ERCReport
from kicad_tools.export.preflight import PreflightChecker, PreflightConfig
from tests.test_preflight import _create_minimal_pcb


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _kct_report(
    pcb: Path,
    *,
    errors: int = 0,
    warnings: int = 0,
    pcb_sha256: str | None = None,
) -> dict:
    violations = [
        {"rule_id": "clearance", "severity": "error", "message": f"clearance #{i}"}
        for i in range(errors)
    ] + [
        {"rule_id": "copper_sliver", "severity": "warning", "message": f"sliver #{i}"}
        for i in range(warnings)
    ]
    data: dict = {
        "file": pcb.name,
        "manufacturer": "jlcpcb",
        "summary": {"errors": errors, "warnings": warnings, "passed": errors == 0},
        "violations": violations,
    }
    if pcb_sha256 is not None:
        data["pcb_sha256"] = pcb_sha256
    return data


def _kicad_cli_drc(pcb: Path, *, errors: int = 0) -> dict:
    return {
        "$schema": "https://schemas.kicad.org/drc.v1.json",
        "source": pcb.name,
        "kicad_version": "10.0.6",
        "coordinate_units": "mm",
        "violations": [
            {
                "type": "clearance",
                "severity": "error",
                "description": "Clearance violation",
                "items": [],
            }
            for _ in range(errors)
        ],
        "unconnected_items": [],
        "schematic_parity": [],
    }


def _write_readiness(
    board_dir: Path, inputs: dict[str, str], report_sources: dict[str, str] | None = None
) -> Path:
    path = board_dir / "output" / "readiness.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {"verdict": "READY", "inputs": inputs}
    if report_sources is not None:
        data["report_sources"] = report_sources
    path.write_text(json.dumps(data))
    return path


# ---------------------------------------------------------------------------
# report_binding
# ---------------------------------------------------------------------------


class TestBindingStatus:
    def test_matching_hash_is_bound(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        report = tmp_path / "drc_report.json"
        report.write_text("{}")
        assert binding_status(report, pcb, _sha(pcb)) is BindingStatus.BOUND

    def test_hash_is_case_insensitive(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        report = tmp_path / "drc_report.json"
        report.write_text("{}")
        assert binding_status(report, pcb, _sha(pcb).upper()) is BindingStatus.BOUND

    def test_different_hash_is_mismatch(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        report = tmp_path / "drc_report.json"
        report.write_text("{}")
        assert binding_status(report, pcb, "0" * 64) is BindingStatus.MISMATCH

    def test_no_hash_is_unhashed(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        report = tmp_path / "drc_report.json"
        report.write_text("{}")
        assert binding_status(report, pcb, None) is BindingStatus.UNHASHED

    def test_missing_source_is_unhashed(self, tmp_path):
        report = tmp_path / "drc_report.json"
        report.write_text("{}")
        assert binding_status(report, None, "0" * 64) is BindingStatus.UNHASHED
        assert binding_status(report, tmp_path / "gone.kicad_pcb", "0" * 64) is (
            BindingStatus.UNHASHED
        )

    def _co_bound_board(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        board = tmp_path / "board"
        out = board / "output"
        out.mkdir(parents=True)
        pcb = _create_minimal_pcb(out, filename="b_routed.kicad_pcb")
        report = out / "drc_report.json"
        report.write_text(json.dumps(_kicad_cli_drc(pcb)))
        readiness = _write_readiness(
            board,
            {
                "output/b_routed.kicad_pcb": _sha(pcb),
                "output/drc_report.json": _sha(report),
            },
            {"output/drc_report.json": _sha(pcb)},
        )
        return pcb, report, readiness

    def test_readiness_co_binding_is_bound(self, tmp_path):
        pcb, report, _ = self._co_bound_board(tmp_path)
        assert binding_status(report, pcb, None) is BindingStatus.BOUND

    def test_readiness_co_binding_breaks_on_pcb_change(self, tmp_path):
        pcb, report, _ = self._co_bound_board(tmp_path)
        pcb.write_bytes(pcb.read_bytes() + b" ")
        assert binding_status(report, pcb, None) is BindingStatus.UNHASHED

    def test_readiness_co_binding_breaks_on_report_change(self, tmp_path):
        pcb, report, _ = self._co_bound_board(tmp_path)
        report.write_text(report.read_text() + " ")
        assert binding_status(report, pcb, None) is BindingStatus.UNHASHED

    def test_readiness_pinning_only_the_pcb_is_unhashed(self, tmp_path):
        pcb, report, readiness = self._co_bound_board(tmp_path)
        _write_readiness(readiness.parent.parent, {"output/b_routed.kicad_pcb": _sha(pcb)})
        assert binding_status(report, pcb, None) is BindingStatus.UNHASHED

    def test_readiness_pinning_only_the_report_is_unhashed(self, tmp_path):
        pcb, report, readiness = self._co_bound_board(tmp_path)
        _write_readiness(readiness.parent.parent, {"output/drc_report.json": _sha(report)})
        assert binding_status(report, pcb, None) is BindingStatus.UNHASHED

    @pytest.mark.parametrize("content", ["not json", "[]", '{"inputs": []}', ""])
    def test_malformed_readiness_is_unhashed(self, tmp_path, content):
        pcb, report, readiness = self._co_bound_board(tmp_path)
        readiness.write_text(content)
        assert binding_status(report, pcb, None) is BindingStatus.UNHASHED

    def test_recorded_mismatch_wins_over_readiness(self, tmp_path):
        """A report that records a wrong hash is stale even if readiness pins it."""
        pcb, report, _ = self._co_bound_board(tmp_path)
        assert binding_status(report, pcb, "0" * 64) is BindingStatus.MISMATCH

    def test_stamp_report_hash(self, tmp_path):
        sch = tmp_path / "b.kicad_sch"
        sch.write_text("(kicad_sch)")
        report = tmp_path / "erc_report.json"
        report.write_text(json.dumps({"source": "b.kicad_sch", "sheets": []}))
        assert stamp_report_hash(report, sch, ERC_HASH_KEY)
        data = json.loads(report.read_text())
        assert data[ERC_HASH_KEY] == _sha(sch)
        assert ERCReport.load(report).schematic_sha256 == _sha(sch)

    def test_stamp_report_hash_leaves_non_json_alone(self, tmp_path):
        sch = tmp_path / "b.kicad_sch"
        sch.write_text("(kicad_sch)")
        report = tmp_path / "erc_report.txt"
        report.write_text("** ERC report **")
        assert not stamp_report_hash(report, sch, ERC_HASH_KEY)
        assert report.read_text() == "** ERC report **"


class TestReportParsing:
    def test_kct_check_json_parses_pcb_sha256(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        report = tmp_path / "drc_report.json"
        report.write_text(json.dumps(_kct_report(pcb, pcb_sha256=_sha(pcb))))
        assert DRCReport.load(report).pcb_sha256 == _sha(pcb)

    def test_kicad_cli_json_parses_pcb_sha256(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        data = _kicad_cli_drc(pcb)
        data[DRC_HASH_KEY] = _sha(pcb)
        report = tmp_path / "drc_report.json"
        report.write_text(json.dumps(data))
        assert DRCReport.load(report).pcb_sha256 == _sha(pcb)

    def test_absent_hash_is_none(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        report = tmp_path / "drc_report.json"
        report.write_text(json.dumps(_kicad_cli_drc(pcb)))
        assert DRCReport.load(report).pcb_sha256 is None


class TestCheckOutputRecordsHash:
    def test_output_json_carries_pcb_sha256(self, drc_clean_pcb: Path, tmp_path: Path):
        from kicad_tools.cli.check_cmd import main

        output_file = tmp_path / "drc_report.json"
        assert main([str(drc_clean_pcb), "--output", str(output_file), "--allow-incomplete"]) == 0
        data = json.loads(output_file.read_text())
        assert data["pcb_sha256"] == _sha(drc_clean_pcb)
        assert data["manufacturer"]  # sits beside #7153's provenance field


# ---------------------------------------------------------------------------
# Preflight: {explicit, auto} x {BOUND, MISMATCH, UNHASHED, co-bound}
#            x {clean, warnings-only, errors} x {strict, non-strict}
# ---------------------------------------------------------------------------

BINDINGS = ["bound", "mismatch", "unhashed", "co-bound"]
VERDICTS = ["clean", "warnings", "errors"]


def _stage_drc(tmp_path: Path, binding: str, verdict: str) -> tuple[Path, Path]:
    board = tmp_path / "board"
    out = board / "output"
    out.mkdir(parents=True)
    pcb = _create_minimal_pcb(out)
    errors = 1 if verdict == "errors" else 0
    warnings = 2 if verdict == "warnings" else 0
    recorded = {"bound": _sha(pcb), "mismatch": "0" * 64}.get(binding)
    report = out / "drc_report.json"
    report.write_text(
        json.dumps(_kct_report(pcb, errors=errors, warnings=warnings, pcb_sha256=recorded))
    )
    if binding == "co-bound":
        _write_readiness(
            board,
            {"output/board.kicad_pcb": _sha(pcb), "output/drc_report.json": _sha(report)},
            {"output/drc_report.json": _sha(pcb)},
        )
    return pcb, report


def _expected_drc(explicit: bool, binding: str, verdict: str, strict: bool) -> str:
    if verdict == "errors":
        return "FAIL"  # errors always FAIL, bound or not
    if explicit or binding in ("bound", "co-bound"):
        return "OK" if verdict == "clean" else "WARN"
    if binding == "mismatch":
        return "FAIL" if strict else "WARN"
    return "WARN"  # unhashed: #7091 missing-report policy, WARN even when strict


@pytest.mark.parametrize("strict", [False, True], ids=["lenient", "strict"])
@pytest.mark.parametrize("verdict", VERDICTS)
@pytest.mark.parametrize("binding", BINDINGS)
@pytest.mark.parametrize("explicit", [False, True], ids=["auto", "explicit"])
def test_drc_preflight_matrix(tmp_path, explicit, binding, verdict, strict):
    pcb, report = _stage_drc(tmp_path, binding, verdict)
    config = PreflightConfig(
        drc_report_path=str(report) if explicit else None,
        strict_report_source=strict,
    )
    result = PreflightChecker(pcb_path=pcb, config=config)._check_drc()
    assert result.status == _expected_drc(explicit, binding, verdict, strict), result
    if not explicit and verdict != "errors" and binding == "unhashed":
        assert result.message.startswith("DRC report is not bound to this PCB (unhashed)")
        assert "DRC passed" not in result.message
    if not explicit and verdict != "errors" and binding == "mismatch":
        assert "stale" in result.message


def _stage_erc(tmp_path: Path, binding: str, verdict: str) -> tuple[Path, Path, Path]:
    board = tmp_path / "board"
    out = board / "output"
    out.mkdir(parents=True)
    pcb = _create_minimal_pcb(out)
    sch = out / "board.kicad_sch"
    sch.write_text("(kicad_sch)\n")
    sev = {"errors": "error", "warnings": "warning"}.get(verdict)
    data: dict = {
        "$schema": "https://schemas.kicad.org/erc.v1.json",
        "source": "board.kicad_sch",
        "kicad_version": "10.0.6",
        "coordinate_units": "mm",
        "sheets": [
            {
                "path": "/",
                "violations": (
                    [{"type": "pin_not_connected", "severity": sev, "description": "x"}]
                    if sev
                    else []
                ),
            }
        ],
    }
    recorded = {"bound": _sha(sch), "mismatch": "0" * 64}.get(binding)
    if recorded:
        data[ERC_HASH_KEY] = recorded
    report = out / "erc_report.json"
    report.write_text(json.dumps(data))
    if binding == "co-bound":
        _write_readiness(
            board,
            {"output/board.kicad_sch": _sha(sch), "output/erc_report.json": _sha(report)},
            {"output/erc_report.json": _sha(sch)},
        )
    return pcb, sch, report


@pytest.mark.parametrize("strict", [False, True], ids=["lenient", "strict"])
@pytest.mark.parametrize("verdict", VERDICTS)
@pytest.mark.parametrize("binding", BINDINGS)
@pytest.mark.parametrize("explicit", [False, True], ids=["auto", "explicit"])
def test_erc_preflight_matrix(tmp_path, explicit, binding, verdict, strict):
    pcb, sch, report = _stage_erc(tmp_path, binding, verdict)
    config = PreflightConfig(
        erc_report_path=str(report) if explicit else None,
        strict_report_source=strict,
    )
    result = PreflightChecker(pcb_path=pcb, schematic_path=sch, config=config)._check_erc()
    assert result.status == _expected_drc(explicit, binding, verdict, strict), result


@pytest.mark.parametrize("binding", ["unhashed", "mismatch"])
@pytest.mark.parametrize("kind", ["drc", "erc"])
def test_unbound_details_record_no_absolute_host_path(tmp_path, monkeypatch, kind, binding):
    """The details land in committed manifests (#6532): no absolute path."""
    if kind == "drc":
        pcb, report = _stage_drc(tmp_path, binding, "clean")
        result = PreflightChecker(pcb_path=pcb, config=PreflightConfig())._check_drc()
    else:
        pcb, sch, report = _stage_erc(tmp_path, binding, "clean")
        checker = PreflightChecker(pcb_path=pcb, schematic_path=sch, config=PreflightConfig())
        result = checker._check_erc()
    assert result.status == "WARN", result
    assert not result.details.startswith("/"), result.details
    monkeypatch.chdir(tmp_path)
    if kind == "drc":
        result = PreflightChecker(pcb_path=pcb, config=PreflightConfig())._check_drc()
    else:
        result = checker._check_erc()
    assert result.details.startswith(f"board/output/{report.name} records"), result.details


def test_erc_auto_without_configured_schematic_binds_via_report_source(tmp_path):
    """With no schematic configured, the report's ``source`` names it."""
    pcb, sch, report = _stage_erc(tmp_path, "bound", "clean")
    checker = PreflightChecker(pcb_path=pcb, config=PreflightConfig())
    checker.schematic_path = None
    assert checker._check_erc().status == "OK"


def test_fresh_kct_check_output_beside_pcb_is_ok(drc_clean_pcb: Path):
    """A fresh ``kct check --output drc_report.json`` beside the PCB is trusted."""
    from kicad_tools.cli.check_cmd import main

    report = drc_clean_pcb.parent / "drc_report.json"
    assert main([str(drc_clean_pcb), "--output", str(report), "--allow-incomplete"]) == 0
    config = PreflightConfig(strict_report_source=True)
    result = PreflightChecker(pcb_path=drc_clean_pcb, config=config)._check_drc()
    assert result.status in ("OK", "WARN"), result
    assert "not bound" not in result.message


# ---------------------------------------------------------------------------
# kct fleet status
# ---------------------------------------------------------------------------


class TestFleetBinding:
    def _stage(self, tmp_path: Path, *, recorded: str | None, errors: int = 0) -> Path:
        out = tmp_path / "board" / "output"
        out.mkdir(parents=True)
        pcb = _create_minimal_pcb(out, filename="b_routed.kicad_pcb")
        data = _kct_report(pcb, errors=errors)
        if recorded == "bound":
            data["pcb_sha256"] = _sha(pcb)
        elif recorded == "mismatch":
            data["pcb_sha256"] = "0" * 64
        (out / "drc_report.json").write_text(json.dumps(data))
        return pcb

    def test_bound_clean_report_exists(self, tmp_path):
        from kicad_tools.cli.fleet_cmd import _detect_drc

        drc = _detect_drc(self._stage(tmp_path, recorded="bound"), {})
        assert drc.report_exists is True
        assert drc.unbound is False

    @pytest.mark.parametrize("recorded", [None, "mismatch"])
    def test_unbound_clean_report_is_missing(self, tmp_path, recorded):
        from kicad_tools.cli.fleet_cmd import _detect_drc

        drc = _detect_drc(self._stage(tmp_path, recorded=recorded), {})
        assert drc.report_exists is False
        assert drc.unbound is True
        assert drc.to_dict()["unbound"] is True

    @pytest.mark.parametrize("recorded", [None, "mismatch"])
    def test_unbound_report_with_blocking_errors_still_blocks(self, tmp_path, recorded):
        from kicad_tools.cli.fleet_cmd import _detect_drc

        drc = _detect_drc(self._stage(tmp_path, recorded=recorded, errors=3), {})
        assert drc.report_exists is True
        assert drc.over_tolerance is True

    def _stage_erc(self, tmp_path: Path, *, bound: bool, errors: int = 0) -> Path:
        board = tmp_path / "board"
        out = board / "output"
        out.mkdir(parents=True)
        sch = out / "b.kicad_sch"
        sch.write_text("(kicad_sch)\n")
        data: dict = {
            "source": "b.kicad_sch",
            "sheets": [
                {
                    "path": "/",
                    "violations": [{"type": "x", "severity": "error"}] * errors,
                }
            ],
        }
        if bound:
            data[ERC_HASH_KEY] = _sha(sch)
        (out / "erc_report.json").write_text(json.dumps(data))
        return board

    def test_erc_bound_clean_exists(self, tmp_path):
        from kicad_tools.cli.fleet_cmd import _detect_erc

        erc = _detect_erc(self._stage_erc(tmp_path, bound=True))
        assert erc.report_exists is True

    def test_erc_unbound_clean_is_missing(self, tmp_path):
        from kicad_tools.cli.fleet_cmd import _detect_erc

        erc = _detect_erc(self._stage_erc(tmp_path, bound=False))
        assert erc.report_exists is False
        assert erc.unbound is True

    def test_erc_unbound_with_errors_still_blocks(self, tmp_path):
        from kicad_tools.cli.fleet_cmd import _detect_erc

        erc = _detect_erc(self._stage_erc(tmp_path, bound=False, errors=2))
        assert erc.report_exists is True
        assert erc.has_errors is True


# ---------------------------------------------------------------------------
# Committed fleet evidence
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "path",
    [
        "boards/00-simple-led/output/drc_report.json",
        "boards/00-simple-led/output/erc_report.json",
        "boards/02-charlieplex-led/output/drc_report.json",
        "boards/02-charlieplex-led/output/erc_report.json",
    ],
)
def test_producerless_reports_are_gone(path):
    assert not (REPO_ROOT / path).exists()


@pytest.mark.parametrize(
    "board",
    ["05-bldc-motor-controller", "06-diffpair-test", "07-matchgroup-test"],
)
def test_readiness_pinned_fleet_reports_need_provenance(board):
    """Boards 05/06/07 carry no report provenance yet, so co-pinning no longer binds.

    Issue #7277: regenerate their evidence on the pinned toolchain (producer
    records ``report_sources``) to make them BOUND again.
    """
    out = REPO_ROOT / "boards" / board / "output"
    pcb = next(out.glob("*_routed.kicad_pcb"))
    report = out / "drc_report.json"
    data = json.loads(report.read_text())
    readiness = json.loads((out / "readiness.json").read_text())
    if "report_sources" in readiness:
        expected = BindingStatus.BOUND
    else:
        expected = BindingStatus.UNHASHED
    assert binding_status(report, pcb, data.get(DRC_HASH_KEY)) is expected


# ---------------------------------------------------------------------------
# Issue #7277: provenance-narrowed fallback + hierarchical ERC binding
# ---------------------------------------------------------------------------


class TestReadinessProvenance:
    def test_pcb_edited_then_readiness_rerun_with_stale_report_is_unbound(self, tmp_path):
        """Readiness re-pins old report + new PCB, but report_sources is stale."""
        board = tmp_path / "board"
        out = board / "output"
        out.mkdir(parents=True)
        pcb = _create_minimal_pcb(out, filename="b_routed.kicad_pcb")
        report = out / "drc_report.json"
        report.write_text(json.dumps(_kicad_cli_drc(pcb)))
        old_sha = _sha(pcb)
        pcb.write_bytes(pcb.read_bytes() + b" ")  # PCB edited; report not regenerated
        _write_readiness(
            board,
            {"output/b_routed.kicad_pcb": _sha(pcb), "output/drc_report.json": _sha(report)},
            {"output/drc_report.json": old_sha},
        )
        assert binding_status(report, pcb, None) is BindingStatus.UNHASHED

    def test_co_pinning_without_provenance_is_unbound(self, tmp_path):
        board = tmp_path / "board"
        out = board / "output"
        out.mkdir(parents=True)
        pcb = _create_minimal_pcb(out, filename="b_routed.kicad_pcb")
        report = out / "drc_report.json"
        report.write_text(json.dumps(_kicad_cli_drc(pcb)))
        _write_readiness(
            board,
            {"output/b_routed.kicad_pcb": _sha(pcb), "output/drc_report.json": _sha(report)},
        )
        assert binding_status(report, pcb, None) is BindingStatus.UNHASHED


_ROOT_SHEET = '(kicad_sch (sheet (property "Sheetfile" "child.kicad_sch")))'


def _hier(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "top.kicad_sch"
    child = tmp_path / "child.kicad_sch"
    root.write_text(_ROOT_SHEET)
    child.write_text("(kicad_sch (version 1))")
    return root, child


class TestHierarchicalErc:
    def test_flat_digest_equals_file_hash(self, tmp_path):
        sch = tmp_path / "flat.kicad_sch"
        sch.write_text("(kicad_sch)")
        assert schematic_digest(sch) == _sha(sch)

    def test_hierarchy_digest_differs_from_top_file_hash(self, tmp_path):
        root, _ = _hier(tmp_path)
        assert schematic_digest(root) != _sha(root)

    def test_stamped_hierarchy_is_bound(self, tmp_path):
        root, _ = _hier(tmp_path)
        report = tmp_path / "erc_report.json"
        report.write_text("{}")
        assert stamp_report_hash(report, root, ERC_HASH_KEY)
        recorded = json.loads(report.read_text())[ERC_HASH_KEY]
        assert binding_status(report, root, recorded, hierarchical=True) is BindingStatus.BOUND

    def test_sub_sheet_edit_is_mismatch(self, tmp_path):
        root, child = _hier(tmp_path)
        report = tmp_path / "erc_report.json"
        report.write_text("{}")
        stamp_report_hash(report, root, ERC_HASH_KEY)
        recorded = json.loads(report.read_text())[ERC_HASH_KEY]
        child.write_text("(kicad_sch (version 2))")
        assert binding_status(report, root, recorded, hierarchical=True) is BindingStatus.MISMATCH

    def test_legacy_flat_hash_still_binds_flat_schematic(self, tmp_path):
        sch = tmp_path / "flat.kicad_sch"
        sch.write_text("(kicad_sch)")
        report = tmp_path / "erc_report.json"
        report.write_text("{}")
        assert binding_status(report, sch, _sha(sch), hierarchical=True) is BindingStatus.BOUND

    def test_missing_sub_sheet_is_unhashed(self, tmp_path):
        root, child = _hier(tmp_path)
        child.unlink()
        report = tmp_path / "erc_report.json"
        report.write_text("{}")
        assert binding_status(report, root, "0" * 64, hierarchical=True) is BindingStatus.UNHASHED
        assert stamp_report_hash(report, root, ERC_HASH_KEY) is False

    def test_sub_sheet_referenced_twice_is_digested_once(self, tmp_path):
        root, _ = _hier(tmp_path)
        once = schematic_digest(root)
        root.write_text(
            _ROOT_SHEET.replace("))", ')) (sheet (property "Sheetfile" "child.kicad_sch"))')
        )
        assert schematic_digest(root) != once  # top sheet bytes changed, no crash on duplicate
