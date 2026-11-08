"""``kct export`` must not write unmeetable/waived ampacity floors (#8612)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from kicad_tools.export.manufacturing import (
    ManufacturingConfig,
    ManufacturingPackage,
    ManufacturingResult,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_OUT = REPO_ROOT / "boards" / "05-bldc-motor-controller" / "output"
PCB_NAME = "bldc_controller_routed.kicad_pcb"


def _stage(tmp_path: Path) -> Path:
    for name in (PCB_NAME, "bldc_controller_routed.kicad_pro", "net_class_map.json"):
        shutil.copy(BOARD_OUT / name, tmp_path / name)
    return tmp_path / PCB_NAME


def _export(pcb: Path, **cfg) -> tuple[str, ManufacturingResult]:
    result = ManufacturingResult(output_dir=pcb.parent)
    ManufacturingPackage(pcb, config=ManufacturingConfig(**cfg))._write_drc_constraints(result)
    return pcb.with_suffix(".kicad_dru").read_text(encoding="utf-8"), result


def _amp(dru: str) -> str:
    return "\n".join(ManufacturingPackage._ampacity_block_text(dru))


def _waive(pcb: Path, net: str, rule: str = "ampacity") -> None:
    data = {
        "version": 2,
        "waivers": [{"rule": rule, "nets": [net], "reason": "wire reinforced", "issue": "x#1"}],
    }
    (pcb.parent / ".kct_waivers.json").write_text(json.dumps(data))


def test_unwaived_nets_keep_floors(tmp_path: Path) -> None:
    pcb = _stage(tmp_path)
    pcb.with_suffix(".kicad_dru").write_text("(version 1)\n")
    dru, result = _export(pcb)
    assert "Ampacity Min Width" in dru
    assert any("Export modified" in w for w in result.warnings)


def test_waived_net_gets_no_rule(tmp_path: Path) -> None:
    pcb = _stage(tmp_path)
    baseline, _ = _export(pcb)
    assert "A.NetName == 'VIN'" in _amp(baseline)
    pcb.with_suffix(".kicad_dru").unlink()
    _waive(pcb, "VIN")
    dru, _ = _export(pcb)
    assert "A.NetName == 'VIN'" not in _amp(dru)
    assert "A.NetName == 'VM'" in _amp(dru)


def test_malformed_waivers_keep_floors(tmp_path: Path) -> None:
    pcb = _stage(tmp_path)
    (tmp_path / ".kct_waivers.json").write_text("{nope")
    dru, result = _export(pcb)
    assert "A.NetName == 'VIN'" in _amp(dru)
    assert any("waivers sidecar" in w for w in result.warnings)


def test_no_ampacity_rules_flag(tmp_path: Path) -> None:
    dru, _ = _export(_stage(tmp_path), emit_ampacity_rules=False)
    assert "Ampacity Min Width" not in dru


def test_designer_clearance_rule_skips_ampacity(tmp_path: Path) -> None:
    pcb = _stage(tmp_path)
    pcb.with_suffix(".kicad_dru").write_text(
        "(version 1)\n"
        '(rule "Reviewed clearance - HV"\n'
        "  (condition \"A.NetName == 'VM'\")\n"
        "  (constraint clearance (min 0.5mm)))\n"
    )
    dru, result = _export(pcb)
    assert "Ampacity Min Width" not in dru
    assert "Reviewed clearance - HV" in dru
    assert any("designer's own clearance" in w for w in result.warnings)


def test_oversize_minimum_skipped_with_warning(tmp_path: Path) -> None:
    pcb = _stage(tmp_path)
    data = json.loads((tmp_path / "net_class_map.json").read_text())
    data["VM"]["target_ampacity"] = 2000.0
    (tmp_path / "net_class_map.json").write_text(json.dumps(data))
    dru, result = _export(pcb)
    assert "2000A" not in dru
    assert any("can never be met" in w and "2000A" in w for w in result.warnings)
    assert "Ampacity Min Width" in dru  # the 0.5 A nets are still floored
