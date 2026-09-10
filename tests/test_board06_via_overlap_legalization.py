"""Historical routing repairs must use the current physical drill-overlap rule."""

import importlib.util
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.router.quantize import segment_angle_census
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.via_in_pad import ViaInPadRule

ROOT = Path(__file__).resolve().parents[1] / "boards/06-diffpair-test"


@pytest.fixture
def recipe(monkeypatch):
    for name in ("generate_pcb", "generate_schematic", "generate_design"):
        spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    return module


def test_legalizer_repairs_partial_drill_overlaps_without_changing_fixture(tmp_path, recipe):
    fixture = ROOT / "regression-fixture/diffpair_test_routed.kicad_pcb"
    original = fixture.read_bytes()
    candidate = tmp_path / fixture.name
    shutil.copy2(fixture, candidate)
    checker = ViaInPadRule()
    rules = SimpleNamespace(via_in_pad_supported=False)
    before = PCB.load(candidate)
    assert len(checker.check(before, rules).violations) == 7
    assert recipe._legalize_signal_vias(candidate) == 7
    identities = re.findall(r'\(uuid "([^"]+)"\)', candidate.read_text())
    assert len(identities) == len(set(identities))
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


def write_overlap_board(path, count=12, pad_size=2):
    nodes = [
        "(kicad_pcb (version 20240108) (generator pcbnew)",
        '(general (thickness 1.6)) (layers (0 "F.Cu" signal) (31 "B.Cu" signal))',
        "(gr_rect (start 98.5 47.5) (end 198.5 127.5) "
        '(stroke (width .1) (type default)) (fill none) (layer "Edge.Cuts"))',
    ]
    for i in range(count):
        x, y = 110 + 15 * (i % 4), 60 + 18 * (i // 4)
        net = i + 1
        nodes.extend(
            [
                f'(net {net} "N{net}")',
                f'(footprint "test" (layer "F.Cu") (at {x} {y}) '
                f'(property "Reference" "R{net}") '
                f'(pad "1" smd rect (at 0 0) (size {pad_size} {pad_size}) (layers "F.Cu" "F.Mask") '
                f'(net {net} "N{net}")))',
                f"(via (at {x + 1.05} {y}) (size .6) (drill .25) "
                f'(layers "F.Cu" "B.Cu") (net {net}) '
                f'(uuid "00000000-0000-4000-8000-{net:012d}"))',
            ]
        )
    path.write_text("\n".join(nodes) + "\n)\n")


def test_legalizer_does_not_stop_after_eight_repairs(tmp_path, recipe):
    path = tmp_path / "many.kicad_pcb"
    write_overlap_board(path)
    checker, rules = ViaInPadRule(), SimpleNamespace(via_in_pad_supported=False)
    assert len(checker.check(PCB.load(path), rules).violations) == 12
    assert recipe._legalize_signal_vias(path) == 12
    assert not checker.check(PCB.load(path), rules).violations
    assert len(PCB.load(path).vias) == 12
    assert not segment_angle_census(path)[1]


def test_legalizer_reports_overlap_without_a_legal_escape(tmp_path, recipe):
    path = tmp_path / "blocked.kicad_pcb"
    # A broad SMT land surrounds every candidate in the bounded search.
    write_overlap_board(path, count=1, pad_size=10)
    original = path.read_bytes()
    with pytest.raises(RuntimeError, match="1 unrepaired via-in-pad"):
        recipe._legalize_signal_vias(path)
    assert path.read_bytes() == original
