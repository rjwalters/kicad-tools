"""Export-bundle consistency fixes from issue #7153.

* JLCPCB Gerbers ship solder-paste layers (item 1).
* DRC reports record the manufacturer/tier they were checked against and
  preflight flags a report built for other rules; the design report splits
  the DRC error count per engine (item 2).
* README.txt names the Gerber archive by its bundle path (item 4).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.drc.report import DRCReport, parse_json_report
from kicad_tools.export.gerber import JLCPCB_PRESET, GerberExporter
from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage
from kicad_tools.export.preflight import PreflightChecker, PreflightConfig
from kicad_tools.manufacturers import canonical_manufacturer_id
from kicad_tools.report.collector import ReportDataCollector
from kicad_tools.report.generator import ReportGenerator
from kicad_tools.report.models import ReportData

REPO_ROOT = Path(__file__).resolve().parent.parent
SIMPLE_LED = REPO_ROOT / "boards" / "00-simple-led" / "output" / "simple_led_routed.kicad_pcb"

_MINIMAL_PCB = """(kicad_pcb (version 20231014) (generator "pcbnew")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 0) (end 50 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 50) (end 0 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 50) (end 0 0) (layer "Edge.Cuts") (width 0.1))
)
"""


# ---------------------------------------------------------------------------
# Item 1: paste layers
# ---------------------------------------------------------------------------


def test_jlcpcb_default_layers_include_paste(tmp_path):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_MINIMAL_PCB)
    assert JLCPCB_PRESET.config.include_solderpaste is True
    layers = GerberExporter(pcb)._get_default_layers(JLCPCB_PRESET.config)
    assert "F.Paste" in layers
    assert "B.Paste" in layers


def _paste_gerbers(zip_path: Path) -> set[str]:
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        job = json.loads(next(zf.read(n) for n in names if n.endswith(".gbrjob")).decode("utf-8"))
    job_files = {f["Path"] for f in job.get("FilesAttributes", [])}
    paste = {n for n in names if "_Paste." in n}
    # The job file lists exactly the paste Gerbers that ship.
    assert {n for n in job_files if "_Paste." in n} == paste
    return paste


@pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")
def test_jlcpcb_export_ships_paste_gerbers(tmp_path):
    """An SMD board's JLCPCB zip carries the stencil layers."""
    zip_path = GerberExporter(SIMPLE_LED).export_for_manufacturer("jlcpcb", tmp_path / "out")
    stem = SIMPLE_LED.stem
    paste = _paste_gerbers(zip_path)
    assert f"{stem}-F_Paste.gtp" in paste
    # simple_led's layer table declares B.Paste, so it is plotted too.
    assert f"{stem}-B_Paste.gbp" in paste


@pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")
def test_jlcpcb_export_without_b_paste_layer(tmp_path):
    """A board whose layer table has no B.Paste still exports cleanly."""
    src = SIMPLE_LED.read_text()
    assert '(15 "B.Paste" user)' in src
    pcb = tmp_path / "no_bpaste.kicad_pcb"
    pcb.write_text(src.replace('(15 "B.Paste" user)', ""))
    zip_path = GerberExporter(pcb).export_for_manufacturer("jlcpcb", tmp_path / "out")
    paste = _paste_gerbers(zip_path)
    assert paste == {"no_bpaste-F_Paste.gtp"}


# ---------------------------------------------------------------------------
# Item 2: DRC report provenance
# ---------------------------------------------------------------------------


def _kct_check_report(path: Path, manufacturer: str, *, overrides: bool = False) -> Path:
    data: dict = {
        "file": "board.kicad_pcb",
        "manufacturer": manufacturer,
        "layers": 2,
        "summary": {"errors": 0, "warnings": 0, "rules_checked": 4, "passed": True},
        "violations": [],
    }
    if overrides:
        data["fabrication_overrides"] = {
            "minimum_via_drill_mm": 0.15,
            "paid_factory_option": "0.15 mm minimum via hole size",
        }
    path.write_text(json.dumps(data))
    return path


def _kicad_cli_report(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "$schema": "https://schemas.kicad.org/drc.v1.json",
                "source": "board.kicad_pcb",
                "date": "2026-10-18T00:00:00+0000",
                "kicad_version": "10.0.6",
                "coordinate_units": "mm",
                "violations": [],
                "unconnected_items": [],
                "schematic_parity": [],
            }
        )
    )
    return path


def test_kct_check_report_keeps_manufacturer(tmp_path):
    report = DRCReport.load(_kct_check_report(tmp_path / "r.json", "jlcpcb-tier1", overrides=True))
    assert report.manufacturer == "jlcpcb-tier1"
    assert report.fabrication_overrides is not None
    assert report.fabrication_overrides["minimum_via_drill_mm"] == 0.15


def test_kicad_cli_report_has_no_manufacturer(tmp_path):
    report = DRCReport.load(_kicad_cli_report(tmp_path / "r.json"))
    assert report.manufacturer is None
    assert report.fabrication_overrides is None


def test_parse_json_report_blank_manufacturer_is_none():
    report = parse_json_report(json.dumps({"manufacturer": "  ", "violations": []}))
    assert report.manufacturer is None


def test_canonical_manufacturer_id_keeps_tiers():
    assert canonical_manufacturer_id("jlcpcb_tier1") == "jlcpcb-tier1"
    assert canonical_manufacturer_id("JLC") == "jlcpcb"
    assert canonical_manufacturer_id("jlcpcb-tier1") != canonical_manufacturer_id("jlcpcb")


def _drc(tmp_path: Path, report: Path, manufacturer: str, strict: bool = False):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_MINIMAL_PCB)
    checker = PreflightChecker(
        pcb_path=pcb,
        manufacturer=manufacturer,
        config=PreflightConfig(drc_report_path=str(report), strict_report_source=strict),
    )
    return checker._check_drc()


def test_tier_mismatch_warns(tmp_path):
    report = _kct_check_report(tmp_path / "drc_report.json", "jlcpcb-tier1", overrides=True)
    result = _drc(tmp_path, report, "jlcpcb")
    assert result.status == "WARN", result
    assert result.message == "DRC report was generated for jlcpcb-tier1, not jlcpcb"
    assert "fabrication_overrides" in result.details
    assert "--mfr jlcpcb-tier1" in result.details


def test_tier_mismatch_fails_strict(tmp_path):
    report = _kct_check_report(tmp_path / "drc_report.json", "jlcpcb-tier1")
    result = _drc(tmp_path, report, "jlcpcb", strict=True)
    assert result.status == "FAIL", result


def test_strict_mismatch_blocks_run_all(tmp_path):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_MINIMAL_PCB)
    _kct_check_report(tmp_path / "drc_report.json", "pcbway")
    checker = PreflightChecker(
        pcb_path=pcb,
        manufacturer="jlcpcb",
        config=PreflightConfig(skip_erc=True, strict_report_source=True),
    )
    results = checker.run_all()
    assert PreflightChecker.has_failures(results)


def test_matching_manufacturer_ok(tmp_path):
    report = _kct_check_report(tmp_path / "drc_report.json", "jlcpcb_tier1")
    result = _drc(tmp_path, report, "jlcpcb-tier1", strict=True)
    assert result.status == "OK", result


def test_kicad_cli_report_no_manufacturer_finding(tmp_path):
    report = _kicad_cli_report(tmp_path / "drc_report.json")
    result = _drc(tmp_path, report, "jlcpcb", strict=True)
    assert result.status == "OK", result


def test_mismatched_report_with_errors_still_fails(tmp_path):
    path = tmp_path / "drc_report.json"
    _kct_check_report(path, "jlcpcb-tier1")
    data = json.loads(path.read_text())
    data["violations"] = [
        {"rule_id": "clearance_pad_pad", "severity": "error", "message": "too close"}
    ]
    path.write_text(json.dumps(data))
    result = _drc(tmp_path, path, "jlcpcb")
    assert result.status == "FAIL", result
    assert "generated for jlcpcb-tier1" in result.details


def _drc_status(error_count: int, by_type: dict, geometric_ran: bool = True):
    from kicad_tools.audit.auditor import DRCStatus

    return DRCStatus(
        error_count=error_count,
        blocking_count=error_count,
        passed=error_count == 0,
        violations_by_type=by_type,
        geometric_drc_ran=geometric_ran,
    )


def test_collect_drc_splits_counts_per_engine():
    status = _drc_status(
        42,
        {
            "dimension_via_drill": 7,
            "dimension_via_diameter": 7,
            "dimension_annular_ring": 7,
            "kicad-cli:annular_width": 7,
            "kicad-cli:drill_out_of_range": 7,
            "kicad-cli:via_diameter": 7,
        },
    )
    collector = ReportDataCollector(Path("board.kicad_pcb"), manufacturer="jlcpcb")
    data = collector.collect_drc(SimpleNamespace(drc=status))
    assert data["native_error_count"] == 21
    assert data["geometric_error_count"] == 21
    assert data["manufacturer"] == "jlcpcb"
    # Verdict inputs are untouched.
    assert data["blocking_count"] == 42
    assert data["error_count"] == 42

    rendered = ReportGenerator()._render(
        ReportData(
            project_name="b", revision="1", date="2026-10-18", manufacturer="jlcpcb", drc=data
        )
    )
    section = rendered.split("## DRC Status", 1)[1].split("\\newpage", 1)[0]
    assert "(`--mfr jlcpcb` rules)" in section
    assert "| Errors: kct (`--mfr jlcpcb` rules) | 21 |" in section
    assert "| Errors: kicad-cli (geometric) | 21 |" in section
    assert "| Blocking | 42 |" in section
    assert "reported by both" in section
    # The total row must survive: ``kct board-metrics`` parses ``| Errors | N |``.
    assert "| Errors | 42 |" in section

    from kicad_tools.cli.board_metrics_cmd import _parse_report_md

    parsed = _parse_report_md(rendered, "board")
    assert parsed["drc_violations"] == 42 == data["error_count"]


def test_collect_drc_geometric_not_run():
    status = _drc_status(3, {"clearance_pad_pad": 3}, geometric_ran=False)
    collector = ReportDataCollector(Path("board.kicad_pcb"), manufacturer="pcbway")
    data = collector.collect_drc(SimpleNamespace(drc=status))
    assert data["native_error_count"] == 3
    assert data["geometric_error_count"] is None
    rendered = ReportGenerator()._render(
        ReportData(
            project_name="b", revision="1", date="2026-10-18", manufacturer="pcbway", drc=data
        )
    )
    assert "| Errors: kicad-cli (geometric) | not run |" in rendered
    assert "reported by both" not in rendered

    from kicad_tools.cli.board_metrics_cmd import _parse_report_md

    assert _parse_report_md(rendered, "board")["drc_violations"] == 3


# ---------------------------------------------------------------------------
# Item 4: README names the real Gerber path
# ---------------------------------------------------------------------------


def test_readme_lists_bundle_relative_gerber_path(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    pcb = project_dir / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    (project_dir / "board.kicad_sch").write_text("(kicad_sch)")

    from kicad_tools.export import assembly

    def fake_assembly_export(self, output_dir=None):
        od = Path(output_dir) if output_dir else self.config.output_dir
        gerber_dir = od / "gerbers"
        gerber_dir.mkdir(parents=True, exist_ok=True)
        gerber_path = gerber_dir / "gerbers.zip"
        with zipfile.ZipFile(gerber_path, "w") as zf:
            zf.writestr("board-F_Cu.gtl", "G04*\nM02*\n")
        return assembly.AssemblyPackageResult(output_dir=od, gerber_path=gerber_path)

    monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

    config = ManufacturingConfig(include_report=False, preflight=PreflightConfig(skip_all=True))
    pkg = ManufacturingPackage(pcb_path=pcb, manufacturer="jlcpcb", config=config)
    result = pkg.export(tmp_path / "output")

    content = result.readme_path.read_text()
    assert "  gerbers/gerbers.zip\n" in content
    assert "solder paste" in content
    assert result.bundle_relpath(result.assembly_result.gerber_path) == "gerbers/gerbers.zip"
    assert "Gerbers: gerbers/gerbers.zip" in str(result)
