"""Historical routing repairs must use the current physical drill-overlap rule."""

import importlib.util
import shutil
from pathlib import Path
from types import SimpleNamespace

from kicad_tools.router.quantize import segment_angle_census
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.via_in_pad import ViaInPadRule

ROOT = Path(__file__).resolve().parents[1] / "boards/06-diffpair-test"


def test_legalizer_repairs_partial_drill_overlaps_without_changing_fixture(tmp_path, monkeypatch):
    for name in ("generate_pcb", "generate_schematic", "generate_design"):
        spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(__import__("sys").modules, name, module)
        spec.loader.exec_module(module)
    fixture = ROOT / "regression-fixture/diffpair_test_routed.kicad_pcb"
    original = fixture.read_bytes()
    candidate = tmp_path / fixture.name
    shutil.copy2(fixture, candidate)
    checker = ViaInPadRule()
    rules = SimpleNamespace(via_in_pad_supported=False)
    before = PCB.load(candidate)
    assert len(checker.check(before, rules).violations) == 7
    assert module._legalize_signal_vias(candidate) == 7
    after = PCB.load(candidate)
    assert not checker.check(after, rules).violations
    assert len(after.vias) == len(before.vias)
    assert not segment_angle_census(candidate)[1]
    assert fixture.read_bytes() == original
    assert [
        (f.reference, f.position, [(p.number, p.position, p.net_name) for p in f.pads])
        for f in after.footprints
    ] == [
        (f.reference, f.position, [(p.number, p.position, p.net_name) for p in f.pads])
        for f in before.footprints
    ]
