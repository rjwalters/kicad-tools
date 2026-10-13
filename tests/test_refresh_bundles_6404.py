"""``scripts/boards/refresh_bundles_6404.py``: refill, re-plot and re-pin stages (Issue #6404)."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "boards" / "refresh_bundles_6404.py"

ZONE = """\t(zone
\t\t(net 1)
\t\t(net_name "GND")
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(filled_polygon
\t\t\t(layer "In1.Cu")
\t\t\t(pts
\t\t\t\t(xy 0 0) (xy 1 0) (xy 1 1)
\t\t\t)
\t\t)
\t)
"""
PCB = "(kicad_pcb\n\t(version 20250114)\n\t(via (at 1 2) (net 1))\n" + ZONE + ")\n"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("refresh_bundles_6404_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_bundle_boards_agree_with_sibling_scripts(mod):
    for board in mod.BUNDLES:
        assert board in mod.rr.BOARDS
        assert board in mod.netclasses.BOARDS
        assert Path(mod.rr.BOARDS[board].routed_pcb).name == mod.netclasses.BOARDS[board][0]


def test_without_zones_cuts_only_zone_blocks(mod):
    stripped = mod.without_zones(PCB)
    assert "(zone" not in stripped
    assert "(via (at 1 2) (net 1))" in stripped
    assert mod.zone_blocks(PCB) == [ZONE.strip()]


def test_tokens_ignore_formatting_only(mod):
    reformatted = ZONE.replace("\n\t\t\t\t", " ").replace("\t", "  ")
    assert mod._tokens(ZONE) == mod._tokens(reformatted)
    assert mod._tokens(ZONE) != mod._tokens(ZONE.replace("(xy 1 1)", "(xy 1 2)"))


def _staged(tmp_path: Path) -> tuple[Path, object]:
    board = tmp_path / "99-fake"
    (board / "output").mkdir(parents=True)
    (board / "output" / "board_routed.kicad_pcb").write_text(PCB)
    return board, type("Cfg", (), {"routed_pcb": "output/board_routed.kicad_pcb"})()


def _fake_refill(mod, monkeypatch, transform):
    def refill(pcb: Path) -> None:
        pcb.write_text(transform(pcb.read_text()))

    monkeypatch.setattr(mod.netclasses, "refill_zones_in_place", refill)
    monkeypatch.setattr(mod, "fill_areas", lambda pcb: {"In1.Cu": 0.5})


def test_refill_keeps_pcb_bytes_when_fills_are_current(mod, tmp_path, monkeypatch):
    board, cfg = _staged(tmp_path)
    # kicad-cli re-serializes the zone; the fill itself is unchanged.
    _fake_refill(mod, monkeypatch, lambda t: t.replace("\n\t\t\t\t(xy", " (xy"))
    record = mod.stage_refill(board, cfg)
    assert (board / cfg.routed_pcb).read_text() == PCB
    assert record["changed"] is False and record["fill_identical"] is True
    assert record["before_sha256"] == record["after_sha256"]
    assert record["deltas_mm2"] == {"In1.Cu": 0.0}


def test_refill_replaces_stale_fills(mod, tmp_path, monkeypatch):
    board, cfg = _staged(tmp_path)
    _fake_refill(mod, monkeypatch, lambda t: t.replace("(xy 1 1)", "(xy 0.9 0.9)"))
    record = mod.stage_refill(board, cfg)
    assert "(xy 0.9 0.9)" in (board / cfg.routed_pcb).read_text()
    assert record["changed"] is True
    assert record["before_sha256"] != record["after_sha256"]


def test_refill_refuses_edits_outside_zone_blocks(mod, tmp_path, monkeypatch):
    board, cfg = _staged(tmp_path)
    _fake_refill(mod, monkeypatch, lambda t: t.replace("(at 1 2)", "(at 1 3)"))
    with pytest.raises(mod.GateFailure, match="outside its \\(zone"):
        mod.stage_refill(board, cfg)
    assert (board / cfg.routed_pcb).read_text() == PCB


def _zone_clearance() -> dict:
    return {
        "type": "clearance",
        "severity": "error",
        "items": [
            {"description": "Via [+3V3_A] on F.Cu - B.Cu"},
            {"description": "Zone [GND] on In1.Cu, priority 0"},
        ],
    }


def test_saved_fill_findings_count_zone_clearance(mod):
    report = {
        "violations": [
            _zone_clearance(),
            {"type": "silk_overlap", "severity": "warning", "items": []},
        ],
        "unconnected_items": [{}],
    }
    assert mod.saved_fill_findings(report) == {
        "errors": 1,
        "zone_clearance": 1,
        "unconnected": 1,
    }
    assert mod.saved_fill_findings({"violations": [], "unconnected_items": []}) == {
        "errors": 0,
        "zone_clearance": 0,
        "unconnected": 0,
    }


def _fake_drc(mod, monkeypatch, violations: list[dict]) -> None:
    def run(cmd, **_):
        out = Path(cmd[cmd.index("--output") + 1])
        out.write_text(json.dumps({"violations": violations, "unconnected_items": []}))
        return type("Proc", (), {"returncode": 5 if violations else 0, "stderr": ""})()

    monkeypatch.setattr(mod, "run", run)


def _fake_board(root: Path) -> Path:
    board = root / "99-fake"
    mfg = board / "output" / "manufacturing"
    mfg.mkdir(parents=True)
    (board / "output" / "board.kicad_sch").write_text("sch")
    (board / "output" / "board_routed.kicad_pcb").write_text(PCB)
    with zipfile.ZipFile(mfg / "kicad_project.zip", "w") as z:
        z.writestr("board_routed.kicad_pcb", "old pcb")
    (mfg / "manifest.json").write_text(json.dumps({"version": "1.0", "files": {}}) + "\n")
    (board / "output" / "readiness.json").write_text(
        json.dumps({"status": "ready", "blockers": [], "metrics": {}, "inputs": {}}) + "\n"
    )
    return board


def test_stale_saved_fills_publish_nothing(mod, tmp_path, monkeypatch):
    """The no-refill DRC gate refuses a board whose saved fills fail native DRC."""
    board = _fake_board(tmp_path)
    before = {p: p.read_bytes() for p in board.rglob("*") if p.is_file()}
    monkeypatch.setitem(
        mod.rr.BOARDS,
        board.name,
        mod.rr.BoardConfig(
            routed_pcb="output/board_routed.kicad_pcb",
            schematic="output/board.kicad_sch",
            tier="jlcpcb",
            erc=(),
            archives=(("output/manufacturing/kicad_project.zip", ("output",)),),
        ),
    )
    monkeypatch.setitem(mod.BUNDLES, board.name, mod.BundleConfig(manufacturer="jlcpcb"))
    monkeypatch.setattr(
        mod,
        "stage_refill",
        lambda staged, cfg: {"changed": False, "fill_identical": True},
    )
    _fake_drc(mod, monkeypatch, [_zone_clearance()])

    def must_not_replot(*_):
        raise AssertionError("re-plotted from stale fills")

    monkeypatch.setattr(mod, "stage_replot", must_not_replot)
    with pytest.raises(mod.GateFailure, match="saved zone fills fail native DRC"):
        mod.refresh_bundle(board, "2026-10-13T00:00:00+00:00")
    assert {p: p.read_bytes() for p in board.rglob("*") if p.is_file()} == before


def test_clean_saved_fills_pass_the_gate(mod, tmp_path, monkeypatch):
    board, cfg = _staged(tmp_path)
    _fake_drc(mod, monkeypatch, [])
    report = json.loads(mod.gate_saved_fill_drc(board, cfg))
    assert report["violations"] == []


def test_archive_is_current(mod, tmp_path):
    (tmp_path / "a.txt").write_text("a")
    archive = tmp_path / "x.zip"
    mod.rr.write_deterministic_zip(archive, {"a.txt": b"a"})
    assert mod.archive_is_current(archive, [tmp_path])
    (tmp_path / "a.txt").write_text("changed")
    assert not mod.archive_is_current(archive, [tmp_path])
    mod.rr.write_deterministic_zip(archive, {"missing.txt": b"m"})
    assert not mod.archive_is_current(archive, [tmp_path])


def test_write_targets_never_adds_files(mod, tmp_path):
    (tmp_path / "kept.json").write_text("{}")
    mod._write_targets(tmp_path, ("kept.json",), b"new")
    assert (tmp_path / "kept.json").read_bytes() == b"new"
    with pytest.raises(mod.GateFailure, match="not part of the committed board"):
        mod._write_targets(tmp_path, ("new.json",), b"x")


def _fake_build(root: Path, dru: str) -> Path:
    build = root / "build"
    validation = build / "validation"
    validation.mkdir(parents=True)
    (build / "build.json").write_text("{}")
    (validation / "validation.json").write_text(json.dumps({"status": "pass", "errors": []}))
    for directory, stem in ((build, "sdram_demo_routed"), (validation, "checked")):
        (directory / f"{stem}.kicad_pcb").write_text(PCB)
        (directory / f"{stem}.kicad_pro").write_text("{}")
        (directory / f"{stem}.kicad_dru").write_text(dru)
    return build


def test_board07_evidence_refuses_evidence_under_other_rules(mod, tmp_path):
    board, cfg = _staged(tmp_path)
    routed = board / cfg.routed_pcb
    routed.with_suffix(".kicad_pro").write_text("{}")
    routed.with_suffix(".kicad_dru").write_text("(rule class)")
    build = _fake_build(tmp_path, "(rule default-only)")
    with pytest.raises(mod.GateFailure, match="different .kicad_dru"):
        mod.stage_board07_evidence(board, cfg, build)
    assert routed.read_text() == PCB


def test_board07_evidence_kept_when_current(mod, tmp_path):
    board, cfg = _staged(tmp_path)
    routed = board / cfg.routed_pcb
    routed.with_suffix(".kicad_pro").write_text("{}")
    routed.with_suffix(".kicad_dru").write_text("(rule class)")
    build = _fake_build(tmp_path, "(rule class)")
    readiness = board / "output" / "readiness"
    readiness.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        shutil.copyfile(build / "validation" / f"checked{suffix}", readiness / f"checked{suffix}")
    (readiness / "checked.kicad_sch").write_text("sch")
    (build / "validation" / "checked.kicad_sch").write_text("sch")
    assert mod.stage_board07_evidence(board, cfg, build) == []


def test_board07_validate_fills_under_the_published_router_classes(mod):
    """``validate.py`` declares the same router classes as the #6390 sidecar script."""
    spec = importlib.util.spec_from_file_location(
        "board07_validate_6404", REPO / "boards/07-matchgroup-test/real_design/validate.py"
    )
    validate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validate)
    pcb = REPO / "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb"
    ours = validate.router_net_classes(
        pcb, REPO / "boards/07-matchgroup-test/real_design/authored-source/net_class_map.json"
    )
    theirs = mod.netclasses.router_net_class_map(pcb)
    assert {k: v.name for k, v in ours.items()} == {k: v.name for k, v in theirs.items()}
    assert ours == theirs
