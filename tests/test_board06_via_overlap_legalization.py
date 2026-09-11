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


@pytest.mark.parametrize(
    "failure", ["split", "legalize", "refill", "pour", "clearance", "interrupt"]
)
def test_finalization_restores_copper_and_fills_on_every_failure(
    tmp_path, recipe, monkeypatch, failure
):
    path = tmp_path / "candidate.kicad_pcb"
    original = b"original copper and matching zone fills\n"
    path.write_bytes(original)
    calls = []

    def signature(candidate):
        calls.append("clearance")
        if failure == "clearance" and candidate.read_bytes() != original:
            return {("new foreign-copper violation",)}
        return set()

    def split(candidate, before):
        candidate.write_bytes(b"split copper with stale fills\n")
        if failure == "split":
            raise RuntimeError("split failed after writing")
        return 1

    def legalize(candidate):
        candidate.write_bytes(b"partially legalized copper with stale fills\n")
        if failure == "legalize":
            raise RuntimeError("1 unrepaired via-in-pad overlap")
        if failure == "interrupt":
            raise KeyboardInterrupt
        return 1

    def refill(*args, **kwargs):
        calls.append("refill")
        path.write_bytes(b"legalized copper and new fills\n")
        return SimpleNamespace(returncode=1 if failure == "refill" else 0)

    def audit(tag):
        calls.append("pour")
        return failure != "pour"

    monkeypatch.setattr(recipe, "_clearance_signature", signature)
    monkeypatch.setattr(recipe, "_repair_pour_connectivity", lambda p, nets: (0, 0))
    monkeypatch.setattr(recipe, "_split_offangle_chords", split)
    monkeypatch.setattr(recipe, "_legalize_signal_vias", legalize)
    monkeypatch.setattr(recipe.subprocess, "run", refill)
    with pytest.raises(KeyboardInterrupt if failure == "interrupt" else RuntimeError):
        recipe._finalize_signal_copper(path, ["fill", str(path)], audit)
    assert path.read_bytes() == original
    assert calls.count("refill") <= 1  # rollback must not invoke a failing filler again
    if failure == "refill":
        assert "pour" not in calls  # stale/failed fills cannot pass the physical gate


def test_finalization_commits_only_after_successful_physical_gates(tmp_path, recipe, monkeypatch):
    path = tmp_path / "candidate.kicad_pcb"
    path.write_bytes(b"original")
    calls = []
    monkeypatch.setattr(recipe, "_clearance_signature", lambda p: {("existing",)})
    monkeypatch.setattr(recipe, "_split_offangle_chords", lambda p, before: 0)

    def legalize(candidate):
        candidate.write_bytes(b"new copper")
        return 1

    def refill(*args, **kwargs):
        assert path.read_bytes() == b"new copper"
        path.write_bytes(b"new copper and matching fills")
        calls.append("refill")
        return SimpleNamespace(returncode=0)

    def audit(tag):
        assert path.read_bytes() == b"new copper and matching fills"
        calls.append("audit")
        return True

    monkeypatch.setattr(recipe, "_legalize_signal_vias", legalize)
    monkeypatch.setattr(recipe.subprocess, "run", refill)
    recipe._finalize_signal_copper(path, ["fill", str(path)], audit)
    assert path.read_bytes() == b"new copper and matching fills"
    assert calls == ["refill", "audit"]


def test_restarted_repair_allocator_preserves_existing_identities(recipe):
    recipe._reset_repair_uuid_counter()
    existing = [recipe._generate_uuid() for _ in range(4)]
    recipe._reset_repair_uuid_counter()
    recipe._reserve_repair_uuids("\n".join(f'(uuid "{identity}")' for identity in existing))
    resumed = [recipe._generate_uuid() for _ in range(4)]
    assert not set(existing) & set(resumed)
    assert len(set(resumed)) == 4
    recipe._reset_repair_uuid_counter()
    recipe._reserve_repair_uuids("\n".join(f'(uuid "{identity}")' for identity in existing))
    assert [recipe._generate_uuid() for _ in range(4)] == resumed


@pytest.mark.parametrize("refill_fails", [False, True])
def test_finalization_recovers_pour_cut_by_relocated_copper(
    tmp_path, recipe, monkeypatch, refill_fails
):
    path = tmp_path / "candidate.kicad_pcb"
    path.write_bytes(b"original")
    monkeypatch.setattr(recipe, "_clearance_signature", lambda p: set())
    monkeypatch.setattr(recipe, "_split_offangle_chords", lambda p, before: 0)
    monkeypatch.setattr(recipe, "_legalize_signal_vias", lambda p: 1)
    calls = []

    def refill(*args, **kwargs):
        calls.append("refill")
        if path.read_bytes() == b"bridge":
            path.write_bytes(b"bridge and fills")
            return SimpleNamespace(returncode=1 if refill_fails else 0)
        path.write_bytes(b"legalized copper with disconnected pour")
        return SimpleNamespace(returncode=0)

    def repair(candidate, nets):
        assert candidate.read_bytes() == b"legalized copper with disconnected pour"
        assert nets == recipe.POUR_NETS
        candidate.write_bytes(b"bridge")
        calls.append("repair")
        return (0, 1)

    def audit(tag):
        return path.read_bytes() == b"bridge and fills"

    monkeypatch.setattr(recipe.subprocess, "run", refill)
    monkeypatch.setattr(recipe, "_repair_pour_connectivity", repair)
    if refill_fails:
        with pytest.raises(RuntimeError, match="pour re-fill failed"):
            recipe._finalize_signal_copper(path, ["fill"], audit)
        assert path.read_bytes() == b"original"
    else:
        recipe._finalize_signal_copper(path, ["fill"], audit)
        assert path.read_bytes() == b"bridge and fills"
    assert calls == ["refill", "repair", "refill"]
