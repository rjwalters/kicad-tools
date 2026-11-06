"""#8527: per-net ampacity targets must not become class-wide DRU floors."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

from kicad_tools.export.manufacturing import ManufacturingPackage, ManufacturingResult
from kicad_tools.manufacturers import get_profile
from kicad_tools.manufacturers.dru_generator import generate_dru
from kicad_tools.manufacturers.project_generator import dedupe_net_classes
from kicad_tools.physics.ampacity import width_for_current
from kicad_tools.router.rules import NetClassRouting


def _map(targets: dict[str, float | None], cls: str = "HV"):
    return {n: NetClassRouting(name=cls, target_ampacity=t) for n, t in targets.items()}


def test_one_net_in_class_of_five_is_net_scoped():
    nets = {f"N{i}": None for i in range(5)}
    nets["N2"] = 15.0
    classes = dedupe_net_classes(_map(nets))
    rules = get_profile("jlcpcb").get_design_rules(4, 1.0)
    dru = generate_dru(rules, "jlcpcb", net_classes=classes)
    assert "A.NetName == 'N2'" in dru
    assert "A.NetClass == 'HV'" not in dru
    for n in ("N0", "N1", "N3", "N4"):
        assert f"'{n}'" not in dru


def test_distinct_targets_get_distinct_widths():
    classes = dedupe_net_classes(_map({"A": 15.0, "B": 15.0, "C": 25.0}))
    rules = get_profile("jlcpcb").get_design_rules(4, 1.0)
    dru = generate_dru(rules, "jlcpcb", net_classes=classes)
    w15 = width_for_current(15.0, copper_weight_oz=rules.outer_copper_oz, layer="external")
    w25 = width_for_current(25.0, copper_weight_oz=rules.outer_copper_oz, layer="external")
    assert f"(min {w15:.4f}mm)" in dru and f"(min {w25:.4f}mm)" in dru
    assert "A.NetName == 'A' || A.NetName == 'B'" in dru


def test_uniform_target_listing_only_one_net_is_net_scoped():
    """The sidecar lists only the target-bearing net of a bigger netclass."""
    classes = dedupe_net_classes({"VIN": NetClassRouting(name="Power", target_ampacity=3.0)})
    assert len(classes) == 1 and classes[0].net_scope == ("VIN",)
    rules = get_profile("jlcpcb").get_design_rules(4, 1.0)
    dru = generate_dru(rules, "jlcpcb", net_classes=classes)
    assert "A.NetName == 'VIN'" in dru
    assert "A.NetClass" not in dru


def test_uniform_target_groups_nets_into_one_rule():
    classes = dedupe_net_classes(_map({"A": 5.0, "B": 5.0}))
    assert len(classes) == 1 and classes[0].net_scope == ("A", "B")
    rules = get_profile("jlcpcb").get_design_rules(4, 1.0)
    dru = generate_dru(rules, "jlcpcb", net_classes=classes)
    assert "(A.NetName == 'A' || A.NetName == 'B')" in dru
    assert "A.NetClass" not in dru


def test_two_oz_outer_gives_narrower_external_width():
    rules = replace(get_profile("jlcpcb").get_design_rules(4, 1.0), outer_copper_oz=2.0)
    dru = generate_dru(
        rules, "jlcpcb", net_classes=[NetClassRouting(name="P", target_ampacity=25.0)]
    )
    expected = width_for_current(25.0, copper_weight_oz=2.0, layer="external")
    assert f"(min {expected:.4f}mm)" in dru
    assert expected < 13


def test_net_name_with_quote_is_escaped():
    classes = dedupe_net_classes(_map({"it's": 3.0, "B": None}))
    rules = get_profile("jlcpcb").get_design_rules(2, 1.0)
    assert "A.NetName == 'it\\'s'" in generate_dru(rules, "jlcpcb", net_classes=classes)


_OUTLINE = (
    '(gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.1))\n'
    '(gr_line (start 50 0) (end 50 50) (layer "Edge.Cuts") (width 0.1))\n'
    '(gr_line (start 50 50) (end 0 50) (layer "Edge.Cuts") (width 0.1))\n'
    '(gr_line (start 0 50) (end 0 0) (layer "Edge.Cuts") (width 0.1))\n'
)


def _board(tmp_path: Path, outer_mm: float | None, target: float = 25.0) -> Path:
    """Two-layer board with net VIN, optional stackup, and a net-class-map sidecar."""
    stackup = ""
    if outer_mm is not None:
        cu = lambda n: f'(layer "{n}" (type "copper") (thickness {outer_mm}))'  # noqa: E731
        stackup = f"(setup (stackup {cu('F.Cu')} {cu('B.Cu')}))"
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20231014) (generator "pcbnew")\n'
        '  (general (thickness 1.6))\n  (paper "A4")\n'
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        f'  {stackup}\n  (net 0 "") (net 1 "VIN")\n{_OUTLINE})\n'
    )
    (tmp_path / "net_class_map.json").write_text(
        json.dumps({"VIN": {"name": "Power", "priority": 1, "target_ampacity": target}})
    )
    return pcb


def _external_width(dru: str) -> float:
    import re

    m = re.search(r"external[^\n]*\n[^\n]*\n\s*\(constraint track_width \(min ([0-9.]+)mm", dru)
    assert m, dru
    return float(m.group(1))


def test_exporter_applies_declared_two_oz_stackup(tmp_path):
    pcb = _board(tmp_path, outer_mm=0.07)  # 2 oz
    exporter = ManufacturingPackage(pcb, manufacturer="jlcpcb")
    rules = exporter._apply_stackup_copper(get_profile("jlcpcb").get_design_rules(2, 1.0))
    assert rules.outer_copper_oz == 2.0

    exporter._write_drc_constraints(ManufacturingResult(output_dir=tmp_path / "out"))
    dru = pcb.with_suffix(".kicad_dru").read_text()
    expected = width_for_current(25.0, copper_weight_oz=2.0, layer="external")
    one_oz = width_for_current(25.0, copper_weight_oz=1.0, layer="external")
    assert _external_width(dru) == float(f"{expected:.4f}") < one_oz
    assert "A.NetName == 'VIN'" in dru


def test_exporter_undeclared_stackup_keeps_profile_copper(tmp_path):
    pcb = _board(tmp_path, outer_mm=None)
    exporter = ManufacturingPackage(pcb, manufacturer="jlcpcb")
    base = get_profile("jlcpcb").get_design_rules(2, 1.0)
    assert exporter._apply_stackup_copper(base) == base


def test_exporter_stackup_failure_falls_back_to_profile(tmp_path, monkeypatch):
    pcb = _board(tmp_path, outer_mm=0.07)
    exporter = ManufacturingPackage(pcb, manufacturer="jlcpcb")
    base = get_profile("jlcpcb").get_design_rules(2, 1.0)

    def boom(*a, **k):
        raise RuntimeError("bad stackup")

    monkeypatch.setattr("kicad_tools.physics.stackup.Stackup.from_pcb", boom)
    assert exporter._apply_stackup_copper(base) == base


def test_exporter_ignores_inner_oz_on_two_layer_profile(tmp_path):
    pcb = _board(tmp_path, outer_mm=0.07)
    exporter = ManufacturingPackage(pcb, manufacturer="jlcpcb")
    base = get_profile("jlcpcb").get_design_rules(2, 1.0)
    assert base.inner_copper_oz == 0
    assert exporter._apply_stackup_copper(base).inner_copper_oz == 0


def test_export_warns_when_ampacity_block_changes(tmp_path, caplog):
    pcb = _board(tmp_path, outer_mm=0.07)
    result = ManufacturingResult(output_dir=tmp_path / "out")
    with caplog.at_level(logging.WARNING):
        ManufacturingPackage(pcb, manufacturer="jlcpcb")._write_drc_constraints(result)
    assert any("changed the ampacity rules" in w for w in result.warnings)
    assert "changed the ampacity rules" in caplog.text


def test_export_is_silent_when_ampacity_block_unchanged(tmp_path):
    pcb = _board(tmp_path, outer_mm=0.07)
    exporter = ManufacturingPackage(pcb, manufacturer="jlcpcb")
    exporter._write_drc_constraints(ManufacturingResult(output_dir=tmp_path / "out"))
    dru_first = pcb.with_suffix(".kicad_dru").read_bytes()

    again = ManufacturingResult(output_dir=tmp_path / "out")
    exporter._write_drc_constraints(again)
    assert not any("ampacity" in w for w in again.warnings)
    assert pcb.with_suffix(".kicad_dru").read_bytes() == dru_first
