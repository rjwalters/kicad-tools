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


def test_board07_validate_fills_under_the_published_router_classes(mod, tmp_path):
    """``validate.py`` declares the same router classes as the #6390 sidecar script.

    ``validate.check`` reads the class map ``build_source.py`` publishes: the
    authored fixture's copy with route-time length matching turned on (Issue
    #7423), so the expected map is derived through that same recipe step.
    """
    real_design = REPO / "boards/07-matchgroup-test/real_design"
    spec = importlib.util.spec_from_file_location(
        "board07_validate_6404", real_design / "validate.py"
    )
    validate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validate)
    spec = importlib.util.spec_from_file_location(
        "board07_build_source_6404", real_design / "build_source.py"
    )
    build_source = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_source)
    published = tmp_path / "net_class_map.json"
    shutil.copyfile(real_design / "authored-source/net_class_map.json", published)
    build_source.enable_length_matching(published)
    pcb = REPO / "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb"
    ours = validate.router_net_classes(pcb, published)
    theirs = mod.netclasses.router_net_class_map(pcb)
    assert {k: v.name for k, v in ours.items()} == {k: v.name for k, v in theirs.items()}
    assert ours == theirs


CPL_HEADER = "Designator,Val,Package,Mid X,Mid Y,Rotation,Layer\n"


def _cpl(x, y, rotation="0.0", ref="R1"):
    return f"{CPL_HEADER}{ref},10k,R_0805,{x}mm,{y}mm,{rotation},Top\n".encode()


def _pcb(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text('(kicad_pcb (footprint "R" (at 110 60) (property "Reference" "R1")))')
    return pcb


def test_cpl_gate_accepts_frame_only_refresh(mod, tmp_path):
    mod.check_fresh_cpl(_cpl(15, 8), _cpl("110.0000", "-60.0000"), _pcb(tmp_path))
    mod.check_fresh_cpl(_cpl(110, -60), _cpl(110, -60), _pcb(tmp_path))


def test_cpl_gate_still_refuses_other_changes(mod, tmp_path):
    pcb = _pcb(tmp_path)
    for fresh in (
        _cpl(110, -60, rotation="180.0"),  # not coordinates only
        _cpl(110, -60, ref="R2"),  # designator changed
        _cpl(16, 9),  # coordinates move but do not match the PCB frame
    ):
        with pytest.raises(mod.GateFailure, match="placement changed"):
            mod.check_fresh_cpl(_cpl(15, 8, ref="R1"), fresh, pcb)


def test_bundle_unpacks_gate_native_drc_tuple(mod, tmp_path, monkeypatch):
    """Issue #7348: gate_native_drc returns (errors, raw); the metric must stay an int."""
    board = _fake_board(tmp_path)
    out = board / "output"
    for name in ("drc_report.json", "native-drc.json"):
        (out / name).write_text("old")
    metrics = {"drc_violations": 0, "nets_routed_pct": 100.0, "lvs_clean": True}
    (out / "readiness.json").write_text(
        json.dumps({"status": "ready", "blockers": [], "metrics": metrics, "inputs": {}}) + "\n"
    )
    cfg = mod.rr.BoardConfig(
        routed_pcb="output/board_routed.kicad_pcb",
        schematic="output/board.kicad_sch",
        tier="jlcpcb",
        drc_targets=("output/drc_report.json", "output/native-drc.json"),
        erc=(),
        archives=(),
    )
    monkeypatch.setitem(mod.rr.BOARDS, board.name, cfg)
    monkeypatch.setitem(mod.BUNDLES, board.name, mod.BundleConfig(manufacturer="jlcpcb"))
    monkeypatch.setattr(mod, "stage_refill", lambda s, c: {"changed": False})
    monkeypatch.setattr(mod, "gate_saved_fill_drc", lambda s, c: b"{}")
    monkeypatch.setattr(mod, "bundle_is_stale", lambda s, c: False)
    monkeypatch.setattr(mod.rr, "load_refresher", lambda *a: lambda mfg: None)
    monkeypatch.setattr(mod.rr, "rebuild_archive", lambda *a: [])
    monkeypatch.setattr(mod.rr, "gate_native_drc", lambda *a: (0, b'{"native": true}'))
    monkeypatch.setattr(mod.rr, "gate_kct_check", lambda *a: (True, 0))
    monkeypatch.setattr(mod.rr, "gate_net_status", lambda *a: 100.0)
    monkeypatch.setattr(mod.rr, "gate_sync", lambda *a: None)
    import kicad_tools.cli.board_readiness as br

    monkeypatch.setattr(br, "read_readiness", lambda p: {"status": "ready", "metrics": {}})

    published = mod.refresh_bundle(board, "2026-10-18T00:00:00+00:00")

    assert "output/drc_report.json" in published
    assert (out / "drc_report.json").read_bytes() == b'{"native": true}'
    sources = json.loads((out / "readiness.json").read_text())["report_sources"]
    assert set(sources) == {"output/drc_report.json", "output/native-drc.json"}


# --------------------------------------------------------------------------- Issue #7353
_CHECK_SCRIPT = """\
import json, sys
from pathlib import Path

warnings = int(Path(__file__).with_name("warnings.txt").read_text())
meta = {k: {"status": "PASSED"} for k in ("drc", "erc", "manifest")}
meta["lvs"] = {"status": "PASSED", "mismatches": [], "copper_mismatches": []}
report = {"summary": {"errors": 0, "warnings": warnings}, "meta_checks": meta}
Path(sys.argv[2]).write_text(json.dumps(report))
sys.exit(2 if warnings else 0)
"""


def _repin_board(mod, tmp_path, monkeypatch, *, warnings: int | None = None) -> Path:
    """A fake bundle board whose every external gate is stubbed, so only wiring is under test."""
    board = _fake_board(tmp_path)
    out = board / "output"
    for name in ("drc_report.json", "native-drc.json"):
        (out / name).write_text("old")
    check_script = None
    if warnings is not None:
        (board / "check_board.py").write_text(_CHECK_SCRIPT)
        (board / "warnings.txt").write_text(str(warnings))
        check_script = "check_board.py"
    cfg = mod.rr.BoardConfig(
        routed_pcb="output/board_routed.kicad_pcb",
        schematic="output/board.kicad_sch",
        tier="jlcpcb",
        drc_targets=("output/drc_report.json", "output/native-drc.json"),
        erc=(),
        archives=(),
        check_script=check_script,
    )
    monkeypatch.setitem(mod.rr.BOARDS, board.name, cfg)
    monkeypatch.setitem(mod.BUNDLES, board.name, mod.BundleConfig(manufacturer="jlcpcb"))
    monkeypatch.setattr(mod, "stage_refill", lambda s, c: {"changed": False})
    monkeypatch.setattr(mod, "gate_saved_fill_drc", lambda s, c: b"{}")
    monkeypatch.setattr(mod, "bundle_is_stale", lambda s, c: False)
    monkeypatch.setattr(mod.rr, "load_refresher", lambda *a: lambda mfg: None)
    monkeypatch.setattr(mod.rr, "rebuild_archive", lambda *a: [])
    monkeypatch.setattr(mod.rr, "gate_native_drc", lambda *a: (0, b'{"refilled": true}'))
    monkeypatch.setattr(mod.rr, "gate_kct_check", lambda *a: (True, 0))
    monkeypatch.setattr(mod.rr, "gate_net_status", lambda *a: 100.0)
    monkeypatch.setattr(mod.rr, "gate_sync", lambda *a: None)
    import kicad_tools.cli.board_readiness as br

    monkeypatch.setattr(br, "read_readiness", lambda p: {"status": "ready", "metrics": {}})
    return board


def test_refresh_bundle_delegates_the_post_staging_tail(mod):
    """The re-pin tail has one copy: ``refresh_bundle`` calls ``repin_staged``."""
    import inspect

    source = inspect.getsource(mod.refresh_bundle)
    assert "rr.repin_staged(" in source
    for gate in (
        "gate_native_drc",
        "gate_kct_check",
        "gate_net_status",
        "gate_sync",
        "compare_metrics",
        "read_readiness",
    ):
        assert gate not in source, f"refresh_bundle calls {gate} instead of repin_staged"


def test_bundle_refuses_when_the_board_local_check_fails(mod, tmp_path, monkeypatch):
    """``check_script`` is honoured by the bundle driver; the generic check must not run."""
    board = _repin_board(mod, tmp_path, monkeypatch, warnings=1)

    def generic_check_must_not_run(*_):
        raise AssertionError("kct check ran although the board has a check hook")

    monkeypatch.setattr(mod.rr, "gate_kct_check", generic_check_must_not_run)
    before = {p: p.read_bytes() for p in board.rglob("*") if p.is_file()}
    with pytest.raises(mod.GateFailure, match="check_board.py"):
        mod.refresh_bundle(board, "2026-10-18T00:00:00+00:00")
    assert {p: p.read_bytes() for p in board.rglob("*") if p.is_file()} == before


def test_drc_targets_hold_the_refilled_report_and_have_one_writer(mod, tmp_path, monkeypatch):
    """Every ``drc_targets`` file is the refilled raw report, recorded in ``report_sources``."""
    for name, bundle in mod.BUNDLES.items():
        assert not set(bundle.saved_drc_reports) & set(mod.rr.BOARDS[name].drc_targets), name

    board = _repin_board(mod, tmp_path, monkeypatch)
    mod.refresh_bundle(board, "2026-10-18T00:00:00+00:00")

    cfg = mod.rr.BOARDS[board.name]
    for target in cfg.drc_targets:
        assert (board / target).read_bytes() == b'{"refilled": true}'
    sources = json.loads((board / "output" / "readiness.json").read_text())["report_sources"]
    pcb_sha = mod.sha256(board / cfg.routed_pcb)
    assert sources == dict.fromkeys(cfg.drc_targets, pcb_sha)


def test_known_issue_in_the_bundle_matches_the_readiness_refresh(mod, tmp_path, monkeypatch):
    """``refresh_bundle(known_issue=...)`` stages the same evidence file ``refresh`` does."""
    advisory = "7220:one-sided thermal:advisory"
    evidence = "output/readiness/known-issues.json"
    checked_at = "2026-10-18T00:00:00+00:00"

    bundle_board = _repin_board(mod, tmp_path / "bundle", monkeypatch)
    published = mod.refresh_bundle(bundle_board, checked_at, known_issue=[advisory])

    refresh_board = _repin_board(mod, tmp_path / "refresh", monkeypatch)
    mod.rr.refresh(refresh_board, checked_at, known_issue=[advisory])

    assert evidence in published
    assert (bundle_board / evidence).read_bytes() == (refresh_board / evidence).read_bytes()
    assert (bundle_board / "output" / "readiness.json").read_bytes() == (
        refresh_board / "output" / "readiness.json"
    ).read_bytes()


# --------------------------------------------------------------------------- PDFs (#7675)
def test_replot_covers_every_committed_pdf(mod):
    """A whole-bundle re-plot rewrites every committed PDF (Issue #7675).

    Board 07's title-block edit re-plotted the assembly PDFs but not
    ``schematic.pdf``, so the bundle shipped the old title as ready.
    """
    for board, bundle in mod.BUNDLES.items():
        mfg = REPO / "boards" / board / "output" / "manufacturing"
        committed = {p.relative_to(mfg).as_posix() for p in mfg.rglob("*.pdf")}
        if bundle.gerber_members:
            # Board 04 re-plots only Gerber members; it ships no schematic PDF.
            assert "schematic.pdf" not in committed and bundle.schematic_pdfs == ()
            continue
        replotted = {
            *(m for m in bundle.export_members if m.endswith(".pdf")),
            *(name for name, _ in bundle.assembly_pdfs),
            *bundle.schematic_pdfs,
        }
        assert committed == replotted, board


def test_replot_plots_schematic_pdf_from_the_published_schematic(mod, tmp_path, monkeypatch):
    import kicad_tools.cli.readiness_cmd as readiness_cmd

    staged = tmp_path / "07-matchgroup-test"
    mfg = staged / "output" / "manufacturing"
    mfg.mkdir(parents=True)
    (mfg / "schematic.pdf").write_bytes(b"old")
    cfg = mod.rr.BOARDS["07-matchgroup-test"]
    bundle = mod.BundleConfig(
        manufacturer="jlcpcb", export_members=(), assembly_pdfs=(), renders=None
    )
    calls = []

    def fake_export(schematic, output):
        calls.append(schematic)
        output.write_bytes(b"new")
        return readiness_cmd.EngineRun(ok=True)

    monkeypatch.setattr(readiness_cmd, "_export_schematic_pdf", fake_export)
    monkeypatch.setattr(
        mod, "run", lambda cmd, **kw: mod.subprocess.CompletedProcess(cmd, 0, "", "")
    )
    (staged / cfg.routed_pcb).write_text(PCB)
    written = mod.stage_replot(staged, cfg, bundle)
    assert calls == [staged / cfg.schematic]
    assert (mfg / "schematic.pdf").read_bytes() == b"new"
    assert written == ["output/manufacturing/schematic.pdf"]


# --------------------------------------------------------------------------- board 04 (#7357)
def test_board04_config_reuses_the_shared_pipeline(mod):
    bundle, cfg = mod.BUNDLES["04-stm32-devboard"], mod.rr.BOARDS["04-stm32-devboard"]
    # The board-local check hook is what repin_staged's gate runs, not bare kct check.
    assert cfg.check_script == "check_manufacturing.py"
    # Only the copper and front-silk members follow the saved fills.
    assert bundle.export_members == () and bundle.assembly_pdfs == () and bundle.renders is None
    assert [p.search("b-F_Cu.gbr") is not None for p in bundle.gerber_members] == [True, False]
    assert [p.search("b-F_Silkscreen.gbr") is not None for p in bundle.gerber_members] == [
        False,
        True,
    ]
    assert bundle.recipe_drc == "generate_design.py"
    for target in (*bundle.parity_drc_reports, *bundle.fill_consistency, *bundle.check_reports):
        assert (REPO / "boards/04-stm32-devboard" / target).is_file()


def test_board04_legacy_writers_are_gone():
    boards = REPO / "scripts" / "boards"
    assert not (boards / "regenerate_board_04_6269.py").exists()
    assert not (boards / "regenerate_board_04_6404.py").exists()
    assert "NOT re-pinned" not in (boards / "regenerate_boards_03_04_6076.py").read_text()


def _parity_bundle(mod, tmp_path, monkeypatch, report: dict):
    board, cfg = _staged(tmp_path)
    committed = board / "output" / "native-drc.json"
    committed.write_text(json.dumps({"date": "old", "violations": [], "unconnected_items": []}))
    bundle = mod.BundleConfig(manufacturer="jlcpcb", parity_drc_reports=("output/native-drc.json",))

    def run(cmd, **_):
        Path(cmd[cmd.index("--output") + 1]).write_text(json.dumps(report))
        return type("Proc", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(mod, "run", run)
    return board, cfg, bundle, committed


def test_parity_drc_refuses_a_finding_and_writes_nothing(mod, tmp_path, monkeypatch):
    seeded = {"date": "new", "violations": [{"type": "clearance", "severity": "error"}]}
    board, cfg, bundle, committed = _parity_bundle(mod, tmp_path, monkeypatch, seeded)
    before = committed.read_bytes()
    with pytest.raises(mod.GateFailure, match="schematic parity"):
        mod.gate_parity_drc(board, cfg, bundle)
    assert committed.read_bytes() == before


def test_parity_drc_keeps_committed_report_when_only_the_date_differs(mod, tmp_path, monkeypatch):
    clean = {"date": "new", "violations": [], "unconnected_items": []}
    board, cfg, bundle, committed = _parity_bundle(mod, tmp_path, monkeypatch, clean)
    before = committed.read_bytes()
    mod.gate_parity_drc(board, cfg, bundle)
    assert committed.read_bytes() == before
