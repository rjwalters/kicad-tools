"""Conservative physical leaf proof and all-or-nothing finalizer controls."""

import importlib.util
import json
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB

BOARD = Path(__file__).resolve().parents[1] / "boards/02-charlieplex-led"


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, BOARD / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


repair = load_module("native_via_repair")
finalizer = load_module("finalize_routing")


def board(tmp_path, extra=""):
    path = tmp_path / "sample.kicad_pcb"
    path.write_text(f"""(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6)) (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
      (net 1 "GND")
      (via (at 10 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via"))
      (segment (start 10 10) (end 11 11) (width 0.5) (layer "B.Cu") (net 1) (uuid "leaf"))
      (segment (start 10 10) (end 5 10) (width 0.5) (layer "B.Cu") (net 1) (uuid "trunk"))
      {extra})""")
    return path, PCB.load(path)


def report(kind="track_dangling", identity="leaf"):
    return {"violations": [{"type": kind, "items": [{"uuid": identity}], "description": ""}]}


def test_redundant_leaf_removed_and_retained_trunk_survives(tmp_path):
    path, pcb = board(tmp_path)
    assert repair.remove_reported_leaves(pcb, report())
    pcb.save(path)
    loaded = PCB.load(path)
    assert [s.uuid for s in loaded.segments] == ["trunk"]
    assert [v.uuid for v in loaded.vias] == ["via"]


@pytest.mark.parametrize(
    "extra",
    [
        '(via (at 11 11) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "required"))',
        '(segment (start 11 11) (end 15 11) (width 0.2) (layer "B.Cu") (net 1) (uuid "required"))',
        '(footprint "test" (layer "B.Cu") (at 11 11) (pad "1" smd rect (at 0 0) (size 1 1) (layers "B.Cu") (net 1 "GND")))',
        '(gr_line (start 11 11) (end 15 11) (stroke (width 0.2) (type default)) (layer "B.Cu"))',
        '(zone (net 1) (net_name "GND") (layer "B.Cu") (hatch edge 0.5) (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20))))',
    ],
)
def test_required_or_unmodelled_contact_rejects_cleanup(tmp_path, extra):
    _, pcb = board(tmp_path, extra)
    assert not repair.remove_reported_leaves(pcb, report())
    assert len(pcb.segments) >= 2


def test_unreported_leaf_is_never_removed(tmp_path):
    _, pcb = board(tmp_path)
    assert not repair.remove_reported_leaves(pcb, report("clearance"))
    assert len(pcb.segments) == 2


def test_resolve_strictest_project_custom_and_report_floors(tmp_path):
    path, pcb = board(tmp_path)
    path.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "board": {
                    "design_settings": {"rules": {"min_clearance": 0.2, "min_hole_to_hole": 0.6}}
                },
                "net_settings": {"classes": [{"clearance": 0.25}]},
            }
        )
    )
    path.with_suffix(".kicad_dru").write_text(
        '(version 1) (rule "strict" (constraint clearance (min 12mil)))'
    )
    data = report("hole_to_hole")
    data["violations"][0]["description"] = "board setup constraints min 0.7 mm"
    assert repair.repair_floors(path, pcb, data) == pytest.approx((0.3048, 0.7))


@pytest.mark.parametrize("outcome", ["raise", "dirty", "clean"])
def test_transaction_restores_bytes_sidecars_and_cleans_scratch(tmp_path, monkeypatch, outcome):
    path, _ = board(tmp_path)
    original = path.read_bytes()
    sidecar = path.with_suffix(".kicad_pro")
    sidecar.write_bytes(b"project bytes")
    visited = []

    def mutate_then_qualify(candidate, schematic):
        visited.append(candidate.parent)
        assert candidate.with_suffix(".kicad_pro").read_bytes() == b"project bytes"
        candidate.write_bytes(b"mutated candidate including changed fills")
        if outcome == "raise":
            raise RuntimeError("native tool failed after mutation")
        return outcome == "clean"

    monkeypatch.setattr(finalizer, "_finalize_candidate", mutate_then_qualify)
    assert finalizer.finalize_routing(path) is (outcome == "clean")
    assert path.read_bytes() == (
        b"mutated candidate including changed fills" if outcome == "clean" else original
    )
    assert sidecar.read_bytes() == b"project bytes"
    assert visited and all(not p.exists() for p in visited)


@pytest.mark.parametrize("gate", ["native", "lvs", "vacuous", "binding", "vip", "geometry"])
def test_actual_candidate_gates_reject_without_replacing_original(tmp_path, monkeypatch, gate):
    from types import SimpleNamespace

    from kicad_tools.validate.rules.via_in_pad import ViaInPadRule

    monkeypatch.syspath_prepend(str(BOARD))
    import native_via_repair

    path, _ = board(tmp_path)
    original = path.read_bytes()
    monkeypatch.setattr(finalizer, "quantize_pcb_file", lambda p: None)
    monkeypatch.setattr(finalizer.subprocess, "run", lambda *args, **kw: None)
    monkeypatch.setattr(native_via_repair, "repair_report_vias", lambda *args: False)
    clean = {"violations": [], "unconnected_items": [], "schematic_parity": []}
    dirty = dict(clean, violations=[{"type": "clearance"}])
    monkeypatch.setattr(finalizer, "_native_report", lambda p: dirty if gate == "native" else clean)
    calls = []

    def lvs(*args):
        calls.append(1)
        if len(calls) == 1:
            return {"clean": True, "bound_pad_count": 2}
        return {
            "clean": gate != "lvs",
            "bound_pad_count": 0 if gate == "vacuous" else 1 if gate == "binding" else 2,
        }

    monkeypatch.setattr(finalizer, "_lvs_report", lvs)
    monkeypatch.setattr(
        ViaInPadRule,
        "check",
        lambda *args: SimpleNamespace(violations=["finding"] if gate == "vip" else []),
    )
    if gate == "geometry":
        identities = iter(["before", "after"])
        monkeypatch.setattr(finalizer, "_geometry", lambda p: next(identities))
    assert not finalizer.finalize_routing(path)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("board02-finalize-*"))


def test_native_floor_missing_rejects_repair(tmp_path):
    path, pcb = board(tmp_path)
    path.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "board": {
                    "design_settings": {"rules": {"min_clearance": 0.2, "min_hole_to_hole": 0.6}}
                },
            }
        )
    )
    with pytest.raises(ValueError, match="native clearance minimum"):
        repair.repair_floors(path, pcb, report("clearance"))
