"""Transactional publication controls; native Board 07 qualification is separate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli import relocate_with_refill as repair
from kicad_tools.manufacturers import get_profile
from kicad_tools.schema.pcb import PCB


@pytest.fixture
def board(tmp_path):
    source = Path(__file__).parent / "fixtures/via_relocation/hole_floor.kicad_pcb"
    path = tmp_path / "board.kicad_pcb"
    text = (
        source.read_text()
        .replace("10.4421", "10.47")
        .replace(
            '(footprint "Test:Pad" (layer "F.Cu") (at 10 10)',
            '(footprint "Test:Pad" (layer "F.Cu") (at 10 10) (property "Reference" "START" (at 0 -1) (layer "F.SilkS"))',
        )
    )
    # A second signal pad makes cutting the retained escape a physical open.
    text = (
        text.rstrip()[:-1]
        + """
      (footprint "Test:End" (layer "F.Cu") (at 12 10)
        (property "Reference" "END" (at 0 -1) (layer "F.SilkS"))
        (pad "1" smd rect (at 0 0) (size .3 .3) (layers "F.Cu") (net 1 "SIG")))
    )"""
    )
    path.write_text(text)
    path.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "board": {"design_settings": {"rules": {"min_hole_clearance": 0.25}}},
            }
        )
    )
    path.with_suffix(".kicad_dru").write_text("(version 1)\n")
    return path


def _rules():
    return get_profile("jlcpcb").get_design_rules(layers=4)


def _bytes(board):
    return {
        suffix: board.with_suffix(suffix).read_bytes()
        for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru")
    }


def _empty():
    return {"violations": [], "unconnected_items": []}


def _violation(uid, kind="clearance"):
    return {"type": kind, "items": [{"uuid": uid}]}


def test_equal_counts_with_different_identities_are_new():
    old = {"violations": [_violation("first")], "unconnected_items": []}
    new = {"violations": [_violation("second")], "unconnected_items": []}
    assert repair._violation_identities(new) - repair._violation_identities(old)
    new["violations"] = [_violation("first", "hole_clearance")]
    assert repair._violation_identities(new) - repair._violation_identities(old)
    new["violations"] = [_violation("first"), _violation("first")]
    assert (
        sum((repair._violation_identities(new) - repair._violation_identities(old)).values()) == 1
    )


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"violations": [], "unconnected_items": None},
        {"violations": [{"type": "clearance", "items": []}], "unconnected_items": []},
    ],
)
def test_missing_native_evidence_is_rejected(bad):
    with pytest.raises((KeyError, ValueError, TypeError)):
        repair._violation_identities(bad)


def test_stages_sidecars_and_publishes_only_after_validation(board, monkeypatch):
    original = _bytes(board)
    calls = []

    def native(path, executable):
        assert path != board and path.name == board.name
        assert _bytes(board) == original
        assert path.with_suffix(".kicad_pro").read_bytes() == original[".kicad_pro"]
        assert path.with_suffix(".kicad_dru").read_bytes() == original[".kicad_dru"]
        calls.append(path)
        return _empty()

    monkeypatch.setattr(repair, "_native_refill", native)
    result = repair.relocate_in_pad_vias_with_refill(board, _rules(), kicad_cli=Path("fake"))
    assert len(result.relocation.moved) == 1
    assert [path.parent.name for path in calls] == ["baseline", "candidate"]
    assert all(not path.exists() for path in calls)
    assert board.read_bytes() != original[".kicad_pcb"]
    assert board.with_suffix(".kicad_pro").read_bytes() == original[".kicad_pro"]
    assert board.with_suffix(".kicad_dru").read_bytes() == original[".kicad_dru"]
    published = board.read_bytes()
    assert not repair.relocate_in_pad_vias_with_refill(board, _rules()).relocation.changed
    assert board.read_bytes() == published


@pytest.mark.parametrize(
    "failure", ["native", "identity", "clearance", "stub", "connectivity", "publish"]
)
def test_failed_transaction_preserves_all_source_bytes(board, monkeypatch, failure):
    original = _bytes(board)

    def native(path, executable):
        if path.parent.name == "baseline":
            return {"violations": [_violation("old")], "unconnected_items": []}
        if failure == "native":
            raise RuntimeError("native failed")
        if failure == "identity":
            return {"violations": [_violation("different")], "unconnected_items": []}
        pcb = PCB.load(path)
        if failure == "clearance":
            x, y = pcb.vias[0].position
            pcb.add_trace((x, y), (x, y + 1), width=0.2, layer="F.Cu", net="OTHER")
            pcb.save(path)
        if failure == "stub":
            x, y = PCB.load(board).vias[0].position
            # Outside the old via envelope and clear of the new via: only
            # checking the complete added stub catches this crossing.
            pcb.add_trace(
                (x + 0.33, y - 0.2), (x + 0.33, y + 0.2), width=0.01, layer="F.Cu", net="OTHER"
            )
            pcb.save(path)
        if failure == "connectivity":
            for node in list(pcb._sexp.children):
                if node.name == "segment":
                    end = node.find("end")
                    if [float(v) for v in end.get_atoms()] == [12, 10]:
                        pcb._sexp.children.remove(node)
            PCB(pcb._sexp, path=path).save(path)
        return _empty()

    monkeypatch.setattr(repair, "_native_refill", native)
    if failure == "publish":

        def fail_replace(*args):
            raise OSError("replace failed")

        monkeypatch.setattr(repair.os, "replace", fail_replace)
    expected = {
        "native": "native failed",
        "identity": "native violation identities",
        "clearance": "Refilled via",
        "stub": "Refilled stub",
        "connectivity": "physical connectivity partitions",
        "publish": "replace failed",
    }
    with pytest.raises((RuntimeError, OSError), match=expected[failure]):
        repair.relocate_in_pad_vias_with_refill(board, _rules(), kicad_cli=Path("fake"))
    assert _bytes(board) == original
    assert not list(board.parent.glob(f".{board.name}.*"))


def test_native_nonzero_rejected_even_if_report_exists(board, monkeypatch):
    import subprocess

    board.with_suffix(".drc.json").write_text(json.dumps(_empty()))
    monkeypatch.setattr(
        repair.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "failure")
    )
    with pytest.raises(RuntimeError, match="Native zone refill failed"):
        repair._native_refill(board, Path("fake"))


def test_source_edits_during_native_run_are_not_overwritten(board, monkeypatch):
    original = _bytes(board)
    edited = original[".kicad_pro"] + b"\n"

    def native(path, executable):
        if path.parent.name == "candidate":
            board.with_suffix(".kicad_pro").write_bytes(edited)
        return _empty()

    monkeypatch.setattr(repair, "_native_refill", native)
    with pytest.raises(RuntimeError, match="Source changed"):
        repair.relocate_in_pad_vias_with_refill(board, _rules(), kicad_cli=Path("fake"))
    assert board.read_bytes() == original[".kicad_pcb"]
    assert board.with_suffix(".kicad_pro").read_bytes() == edited
    assert board.with_suffix(".kicad_dru").read_bytes() == original[".kicad_dru"]


def test_native_name_only_zone_keeps_original_attachment_evidence(monkeypatch):
    source = PCB.load(
        Path(__file__).parent / "fixtures/via_relocation/isolated_inner_plane.kicad_pcb"
    )
    expected = {zone.uuid: zone.filled_polygons for zone in source.zones}
    assert all(expected.values())

    class EvidenceObserved(Exception):
        pass

    def inspect(candidate, rules, **kwargs):
        assert {zone.uuid: zone.filled_polygons for zone in candidate.zones} == expected
        raise EvidenceObserved

    monkeypatch.setattr(repair, "relocate_in_pad_vias", inspect)
    with pytest.raises(EvidenceObserved):
        repair._plan(source, _rules())


def test_shared_extension_reports_a_change_to_its_caller():
    from kicad_tools.cli.relocate_in_pad_vias import extend_blocked_stubs, relocate_in_pad_vias

    source = (
        Path(__file__).parent.parent
        / "boards/07-matchgroup-test/regression-fixture/matchgroup_test_routed.kicad_pcb"
    )
    pcb = PCB.load(source)
    via = next(v for v in pcb.vias if v.uuid == "0de3e62a-2521-486b-a847-963011236afa")
    assert pcb.relocate_via(via, (85.19, 56.27))
    pcb.add_trace((83.82, 56.27), via.position, width=0.2, layer="F.Cu", net="+1V8")
    pcb.add_via(84.52, 55.85, size=0.6, drill=0.2, net="+1V8")
    result = relocate_in_pad_vias(pcb, _rules(), nets={"+1V8"})
    assert result.skipped and not result.changed
    assert extend_blocked_stubs(pcb, _rules(), result, nets={"+1V8"}) == 1
    assert result.changed and not result.skipped
    assert len(result.moved) == 1
    assert result.moved[0].uuid == via.uuid
    assert (result.moved[0].new_x, result.moved[0].new_y) == via.position


def test_staging_cleanup_failure_happens_before_publication(board, monkeypatch):
    original = _bytes(board)
    real_cleanup = repair.tempfile.TemporaryDirectory.cleanup

    def failed_cleanup(directory):
        real_cleanup(directory)
        raise OSError("staging cleanup failed")

    monkeypatch.setattr(repair, "_native_refill", lambda *args: _empty())
    monkeypatch.setattr(repair.tempfile.TemporaryDirectory, "cleanup", failed_cleanup)
    with pytest.raises(OSError, match="staging cleanup failed"):
        repair.relocate_in_pad_vias_with_refill(board, _rules(), kicad_cli=Path("fake"))
    assert _bytes(board) == original


def test_symlink_uses_adjacent_project_floor_and_preserves_link(board, monkeypatch):
    alias_dir = board.parent / "project-context"
    alias_dir.mkdir()
    alias = alias_dir / "linked.kicad_pcb"
    alias.symlink_to(board)
    project = alias.with_suffix(".kicad_pro")
    project.write_text(
        json.dumps(
            {
                "board": {"design_settings": {"rules": {"min_hole_clearance": 0.3}}},
            }
        )
    )
    strict_project = project.read_bytes()
    original = board.read_bytes()
    observed = []

    def native(path, executable):
        assert path.with_suffix(".kicad_pro").read_bytes() == strict_project
        observed.append(path)
        return _empty()

    monkeypatch.setattr(repair, "_native_refill", native)
    result = repair.relocate_in_pad_vias_with_refill(alias, _rules(), kicad_cli=Path("fake"))
    assert len(result.relocation.moved) == 1 and len(observed) == 2
    assert alias.is_symlink() and alias.resolve() == board
    assert board.read_bytes() != original
    assert project.read_bytes() == strict_project
    # The 0.27 mm preferred drill gap is insufficient in the alias's project.
    assert PCB.load(board).vias[0].position != pytest.approx((10.7016, 10.0))
