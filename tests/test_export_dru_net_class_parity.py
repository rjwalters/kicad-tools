"""``kct export`` must keep the net-class-map ampacity rules (#8393)."""

from __future__ import annotations

import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_OUT = REPO_ROOT / "boards" / "05-bldc-motor-controller" / "output"
PCB_NAME = "bldc_controller_routed.kicad_pcb"


def _stage(tmp_path: Path, *, sidecar: bool) -> Path:
    for name in (PCB_NAME, "bldc_controller_routed.kicad_pro"):
        shutil.copy(BOARD_OUT / name, tmp_path / name)
    if sidecar:
        shutil.copy(BOARD_OUT / "net_class_map.json", tmp_path / "net_class_map.json")
    return tmp_path / PCB_NAME


def _export_dru(pcb: Path) -> str:
    from kicad_tools.export.manufacturing import ManufacturingPackage, ManufacturingResult

    result = ManufacturingResult(output_dir=pcb.parent)
    ManufacturingPackage(pcb)._write_drc_constraints(result)
    return pcb.with_suffix(".kicad_dru").read_text(encoding="utf-8")


def _emit_dru(pcb: Path) -> str:
    from kicad_tools.cli.check_cmd import (
        _discover_net_class_map_sidecar,
        _emit_drc_sidecars,
        resolve_net_class_map_for_board,
    )
    from kicad_tools.manufacturers import get_profile
    from kicad_tools.router.rules import net_class_map_from_path
    from kicad_tools.schema.pcb import PCB

    pcb_obj = PCB.load(str(pcb))
    ncm = net_class_map_from_path(_discover_net_class_map_sidecar(pcb), pcb_path=pcb)
    ncm, _ = resolve_net_class_map_for_board(ncm, pcb_obj)
    layers = len(pcb_obj.copper_layers)
    rules = get_profile("jlcpcb").get_design_rules(layers=layers, copper_oz=1.0)
    # Real ``kct check`` repoints ampacity at the declared stackup copper.
    from dataclasses import replace

    from kicad_tools.cli.check_cmd import stackup_copper_oz

    outer, inner = stackup_copper_oz(pcb_obj)
    rules = replace(
        rules,
        outer_copper_oz=outer if outer is not None else rules.outer_copper_oz,
        inner_copper_oz=inner if inner is not None else rules.inner_copper_oz,
    )

    class _Checker:
        design_rules = rules

    _emit_drc_sidecars(
        pcb,
        _Checker(),
        manufacturer_id="jlcpcb",
        layers=layers,
        copper_oz=1.0,
        net_class_map=ncm,
        emit_both=True,
    )
    return pcb.with_suffix(".kicad_dru").read_text(encoding="utf-8")


def test_export_matches_emit_dru_with_sidecar(tmp_path: Path) -> None:
    pcb = _stage(tmp_path, sidecar=True)
    emitted = _emit_dru(pcb)
    pcb.with_suffix(".kicad_dru").unlink()
    exported = _export_dru(pcb)
    assert exported == emitted


def test_export_without_sidecar_unchanged(tmp_path: Path) -> None:
    pcb = _stage(tmp_path, sidecar=False)
    dru = _export_dru(pcb)
    assert "Motor power" not in dru


def test_dedupe_net_classes_one_per_name() -> None:
    from kicad_tools.manufacturers.project_generator import dedupe_net_classes
    from kicad_tools.router.rules import NetClassRouting

    def _c(name: str, amps: float | None = 3.0) -> NetClassRouting:
        return NetClassRouting(name=name, target_ampacity=amps)

    # One entry per (class, target); nets sharing a target are grouped.
    got = dedupe_net_classes(
        {"a": _c("p"), "b": _c("p"), "c": _c("q"), "d": _c("q", None), "e": _c("p", 5.0)}
    )
    assert [(c.name, c.target_ampacity, c.net_scope) for c in got] == [
        ("p", 3.0, ("a", "b")),
        ("p", 5.0, ("e",)),
        ("q", 3.0, ("c",)),
    ]
    # Entries without a target are dropped.
    assert dedupe_net_classes({"d": _c("q", None)}) == []
    assert dedupe_net_classes({}) is None


def test_sidecar_rules_present_in_export(tmp_path: Path) -> None:
    """Guards against the parity test passing vacuously (both sides empty)."""
    assert "Motor power" in _export_dru(_stage(tmp_path, sidecar=True))
