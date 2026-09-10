"""Tests for the board-metrics extractor (issue #3676).

Covers:
* full extraction against a synthetic report.md + manifest.json fixture,
* graceful handling of missing/unparseable report fields (status=partial),
* the no-artifacts path (status=no_artifacts),
* BOM fallback for part_count,
* render-path attachment and relativity,
* emit_board_json writing to the default and overridden paths,
* the CLI main() single-board, --all and --dry-run modes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli.board_metrics_cmd import (
    SCHEMA_VERSION,
    emit_board_json,
    extract_board_metrics,
    main,
)

# A trimmed but faithful copy of board 05's report.md structure.
SAMPLE_REPORT_MD = """---
title: "bldc_controller_routed"
subtitle: "Design Report"
author: "kicad-tools 0.13.0"
---

## Board Summary

| Property | Value |
|----------|-------|
| Layers | 4 copper (F.Cu, In1.Cu, In2.Cu, B.Cu) |
| Footprints | 55 (40 SMD, 11 THT, 4 other) |
| Nets | 52 |
| Board Size | 80.0 x 100.0 mm |

## Design Overview

### Theory of Operation

3-Phase Brushless DC Motor Driver

### Power Architecture

**Power Rails**: +24V

## DRC Status

| Metric | Count |
|--------|-------|
| Errors | 0 |
| Warnings | 0 |

## Routing Status

| Metric | Value |
|--------|-------|
| Signal Net Completion | 82.1% (32/39) |
| Overall Completion | 84.6% |

## Cost Estimate

| Metric | Per Board (estimated) |
|--------|-------|
| **Total (estimated)** | **~9.16 USD** |
| Batch Quantity | 5 |
| Batch Total (estimated) | ~45.78 USD |
"""

SAMPLE_MANIFEST = {
    "version": 1,
    "generated_at": "2026-06-12T05:03:41.535120+00:00",
    "board": {
        "name": "bldc_controller_routed",
        "pcb_file": "bldc_controller_routed.kicad_pcb",
    },
}


def _make_board(
    tmp_path: Path,
    slug: str = "05-bldc-motor-controller",
    *,
    report: str | None = SAMPLE_REPORT_MD,
    manifest: dict | None = None,
    bom: str | None = None,
    package: bool = False,
    renders: list[str] | None = None,
    make_mfg: bool = True,
    lvs: dict | None = None,
    lvs_raw: str | None = None,
) -> Path:
    """Build a synthetic board directory tree and return it.

    ``lvs``: if provided, writes ``output/lvs.json`` with this dict content.
    ``lvs_raw``: if provided, writes raw text to ``output/lvs.json`` (used to
    inject malformed JSON for error-handling tests). Mutually exclusive with
    ``lvs``.
    """
    board_dir = tmp_path / slug
    output_dir = board_dir / "output"
    output_dir.mkdir(parents=True)

    if make_mfg:
        mfg = output_dir / "manufacturing"
        mfg.mkdir()
        if report is not None:
            (mfg / "report.md").write_text(report)
        if manifest is not None:
            (mfg / "manifest.json").write_text(json.dumps(manifest))
        if bom is not None:
            (mfg / "bom_jlcpcb.csv").write_text(bom)
        if package:
            (mfg / "kicad_project.zip").write_bytes(b"PK\x03\x04zip")

    if renders:
        renders_dir = output_dir / "renders"
        renders_dir.mkdir()
        for name in renders:
            (renders_dir / name).write_bytes(b"\x89PNG")

    if lvs is not None and lvs_raw is not None:
        raise ValueError("lvs and lvs_raw are mutually exclusive")
    if lvs is not None:
        (output_dir / "lvs.json").write_text(json.dumps(lvs))
    elif lvs_raw is not None:
        (output_dir / "lvs.json").write_text(lvs_raw)

    return board_dir


def test_extract_full_metrics(tmp_path: Path):
    board = _make_board(tmp_path, manifest=SAMPLE_MANIFEST, package=True)
    m = extract_board_metrics(board)

    assert m["schema_version"] == SCHEMA_VERSION
    assert m["slug"] == "05-bldc-motor-controller"
    assert m["status"] == "partial"
    assert m["name"] == "bldc_controller_routed"
    assert m["layer_count"] == 4
    assert m["board_size_mm"] == {"width": 80.0, "height": 100.0}
    assert m["part_count"] == 55
    assert m["nets_routed_pct"] == 82.1
    assert m["drc_violations"] == 0
    assert m["cost"] == {
        "per_board_usd": 9.16,
        "batch_qty": 5,
        "batch_total_usd": 45.78,
    }
    assert m["manufacturing_package"] == "manufacturing/kicad_project.zip"
    assert m["manifest_generated_at"] == "2026-06-12T05:03:41.535120+00:00"
    assert "3-Phase Brushless DC Motor Driver" in m["description"]
    # generated_at is an ISO-8601 string.
    assert isinstance(m["generated_at"], str) and "T" in m["generated_at"]


def test_no_artifacts_path(tmp_path: Path):
    # Board with an output dir but no manufacturing subdir.
    board = _make_board(tmp_path, slug="00-simple-led", make_mfg=False)
    m = extract_board_metrics(board)

    assert m["status"] == "no_artifacts"
    assert m["slug"] == "00-simple-led"
    # Identity fields present; metric fields omitted (not null).
    for omitted in ("layer_count", "nets_routed_pct", "drc_violations", "name"):
        assert omitted not in m


def test_no_output_dir_at_all(tmp_path: Path):
    # Board directory exists but has no output/ at all.
    board = tmp_path / "00-simple-led"
    board.mkdir()
    m = extract_board_metrics(board)
    assert m["status"] == "no_artifacts"
    assert "renders" not in m


def test_partial_when_report_missing(tmp_path: Path):
    # manufacturing/ exists but report.md is absent -> partial.
    board = _make_board(tmp_path, report=None, manifest=SAMPLE_MANIFEST)
    m = extract_board_metrics(board)

    assert m["status"] == "partial"
    assert m["name"] == "bldc_controller_routed"  # from manifest
    assert "layer_count" not in m  # no report to parse


def test_unparseable_fields_omitted(tmp_path: Path):
    # report.md present but missing several rows -> those fields omitted,
    # no current readiness evidence, so status remains partial.
    sparse_report = """---
title: "sparse_board"
---

## Board Summary

| Property | Value |
|----------|-------|
| Layers | 2 copper (F.Cu, B.Cu) |
"""
    board = _make_board(tmp_path, report=sparse_report)
    m = extract_board_metrics(board)

    assert m["status"] == "partial"
    assert m["layer_count"] == 2
    for omitted in ("board_size_mm", "part_count", "nets_routed_pct", "cost"):
        assert omitted not in m
    # No DRC Status section -> drc_violations omitted.
    assert "drc_violations" not in m


def test_status_downgraded_to_partial_when_drc_violations(tmp_path: Path):
    # A board whose report parses fine but still has DRC errors is NOT
    # manufacturable, so status must NOT be "ok" — it downgrades to "partial".
    # The gallery renders status=="ok" as the "Ready" badge (#3717).
    drc_report = SAMPLE_REPORT_MD.replace(
        "| Errors | 0 |",
        "| Errors | 14 |",
    )
    board = _make_board(tmp_path, report=drc_report)
    m = extract_board_metrics(board)

    assert m["drc_violations"] == 14
    assert m["status"] == "partial"


def test_zero_drc_without_current_readiness_is_partial(tmp_path: Path):
    # Zero historical DRC alone does not establish current readiness.
    board = _make_board(tmp_path)
    m = extract_board_metrics(board)

    assert m["drc_violations"] == 0
    assert m["status"] == "partial"


def test_bom_fallback_for_part_count(tmp_path: Path):
    # report.md without a Footprints row falls back to BOM data-row count.
    report_no_footprints = """---
title: "fallback_board"
---

## Board Summary

| Property | Value |
|----------|-------|
| Layers | 2 copper (F.Cu, B.Cu) |
"""
    bom = "Comment,Designator,Footprint,LCSC\n100nF,C1,C_0402,C123\n10k,R1,R_0402,C456\n"
    board = _make_board(tmp_path, report=report_no_footprints, bom=bom)
    m = extract_board_metrics(board)

    assert m["part_count"] == 2  # two data rows, header dropped


def test_render_paths_attached_when_present(tmp_path: Path):
    board = _make_board(
        tmp_path,
        renders=["pcb-front.svg", "pcb-back.svg", "3d-front.png", "3d-back.png"],
    )
    m = extract_board_metrics(board)

    assert m["renders"] == {
        "pcb_front": "renders/pcb-front.svg",
        "pcb_back": "renders/pcb-back.svg",
        "3d_front": "renders/3d-front.png",
        "3d_back": "renders/3d-back.png",
    }
    # Paths resolve relative to output/board.json.
    for rel in m["renders"].values():
        assert (board / "output" / rel).is_file()


def test_render_paths_partial(tmp_path: Path):
    # Only some renders exist -> only those keys appear.
    board = _make_board(tmp_path, renders=["pcb-front.svg"])
    m = extract_board_metrics(board)
    assert m["renders"] == {"pcb_front": "renders/pcb-front.svg"}


def test_renders_omitted_when_absent(tmp_path: Path):
    board = _make_board(tmp_path)
    m = extract_board_metrics(board)
    assert "renders" not in m


def test_corrupt_manifest_does_not_crash(tmp_path: Path):
    board = _make_board(tmp_path)
    (board / "output" / "manufacturing" / "manifest.json").write_text("{not json")
    m = extract_board_metrics(board)
    # Missing readiness evidence keeps status partial; name is omitted.
    assert m["status"] == "partial"
    assert "name" not in m


def test_emit_board_json_default_path(tmp_path: Path):
    board = _make_board(tmp_path, manifest=SAMPLE_MANIFEST)
    out = emit_board_json(board)

    assert out == board / "output" / "board.json"
    assert out.is_file()
    data = json.loads(out.read_text())
    assert data["slug"] == "05-bldc-motor-controller"
    assert data["status"] == "partial"


def test_emit_board_json_override_path(tmp_path: Path):
    board = _make_board(tmp_path)
    target = tmp_path / "custom" / "out.json"
    out = emit_board_json(board, target)

    assert out == target
    assert target.is_file()


def test_main_single_board(tmp_path: Path, capsys):
    board = _make_board(tmp_path, manifest=SAMPLE_MANIFEST)
    rc = main([str(board)])
    assert rc == 0
    assert (board / "output" / "board.json").is_file()
    out = capsys.readouterr().out
    assert "05-bldc-motor-controller" in out
    assert "partial" in out


def test_main_dry_run_writes_nothing(tmp_path: Path, capsys):
    board = _make_board(tmp_path)
    rc = main([str(board), "--dry-run"])
    assert rc == 0
    assert not (board / "output" / "board.json").exists()
    # stdout is valid JSON.
    data = json.loads(capsys.readouterr().out)
    assert data["slug"] == "05-bldc-motor-controller"


def test_main_all_mode(tmp_path: Path, capsys):
    boards_dir = tmp_path / "boards"
    boards_dir.mkdir()
    # Two boards: one full, one no-artifacts.
    _make_board(boards_dir, slug="05-bldc-motor-controller", manifest=SAMPLE_MANIFEST)
    _make_board(boards_dir, slug="00-simple-led", make_mfg=False)

    rc = main(["--all", "--boards-dir", str(boards_dir)])
    assert rc == 0

    full = json.loads(
        (boards_dir / "05-bldc-motor-controller" / "output" / "board.json").read_text()
    )
    minimal = json.loads((boards_dir / "00-simple-led" / "output" / "board.json").read_text())
    assert full["status"] == "partial"
    assert minimal["status"] == "no_artifacts"


def test_main_all_descends_external(tmp_path: Path):
    boards_dir = tmp_path / "boards"
    boards_dir.mkdir()
    external = boards_dir / "external"
    external.mkdir()
    _make_board(external, slug="softstart", manifest=SAMPLE_MANIFEST)

    rc = main(["--all", "--boards-dir", str(boards_dir)])
    assert rc == 0
    assert (external / "softstart" / "output" / "board.json").is_file()


def test_main_missing_board_dir_errors(tmp_path: Path):
    rc = main([str(tmp_path / "does-not-exist")])
    assert rc == 1


def test_main_requires_board_or_all(capsys):
    with pytest.raises(SystemExit):
        main([])


# ── LVS sourcing (#3749) ───────────────────────────────────────────────────


def test_lvs_clean_when_lvs_json_present_and_clean(tmp_path: Path):
    # Saved clean LVS is preserved, but readiness still needs current evidence.
    board = _make_board(
        tmp_path,
        lvs={
            "$schema": "https://kicad-tools.org/schemas/lvs/v1.json",
            "clean": True,
            "mismatches": [],
        },
    )
    m = extract_board_metrics(board)
    assert m["lvs_clean"] is True
    assert m["lvs_mismatches"] == 0
    assert m["status"] == "partial"


def test_lvs_dirty_downgrades_status_to_partial(tmp_path: Path):
    # An LVS-dirty board: lvs_clean=False, status downgrades to partial even
    # when DRC is clean and report.md parses.
    board = _make_board(
        tmp_path,
        lvs={
            "$schema": "https://kicad-tools.org/schemas/lvs/v1.json",
            "clean": False,
            "mismatches": [
                {"ref": "D1", "pad": "1", "schematic_net": "LED_ANODE", "pcb_net": "GND"},
                {"ref": "D1", "pad": "2", "schematic_net": "GND", "pcb_net": "LED_ANODE"},
            ],
        },
    )
    m = extract_board_metrics(board)
    assert m["lvs_clean"] is False
    assert m["lvs_mismatches"] == 2
    # An explicit LVS mismatch downgrades status, mirroring DRC-violation logic.
    assert m["status"] == "partial"


def test_lvs_copper_mismatches_counted(tmp_path: Path):
    # #4012 (board 07 shape): label leg clean (mismatches=[]) but the copper
    # comparator reports real opens.  lvs_mismatches must count the copper
    # leg too -- rendering "0 mismatches" for a clean=false report would be
    # dishonest -- and the dirty verdict still downgrades status.
    board = _make_board(
        tmp_path,
        lvs={
            "$schema": "https://kicad-tools.org/schemas/lvs/v1.json",
            "clean": False,
            "mismatches": [],
            "copper_mismatches": [
                {"kind": "open", "net_a": n, "net_b": n, "pad_a": "U1.1", "pad_b": "U2.1"}
                for n in ("DQ3", "DQ4", "MIPI_DAT0_N", "TMDS_D0_N", "TMDS_D1_N")
            ],
            "copper_vacuous": False,
            "copper_bound_pad_count": 244,
        },
    )
    m = extract_board_metrics(board)
    assert m["lvs_clean"] is False
    assert m["lvs_mismatches"] == 5
    assert m["status"] == "partial"


def test_vacuous_lvs_json_treated_as_lvs_not_run(tmp_path: Path):
    # A vacuous lvs.json (#4006: schematic bound zero pins, copper_vacuous
    # true) carries NO evidence either way -- both LVS fields are OMITTED
    # ("LVS not run" on the site) and status is not downgraded.
    board = _make_board(
        tmp_path,
        lvs={
            "$schema": "https://kicad-tools.org/schemas/lvs/v1.json",
            "clean": False,
            "mismatches": [],
            "copper_mismatches": [
                {
                    "kind": "vacuous",
                    "net_a": "<no-schematic-evidence>",
                    "net_b": "<no-schematic-evidence>",
                    "pad_a": "bound_pads=0",
                    "pad_b": "board_pads=198",
                }
            ],
            "copper_vacuous": True,
            "copper_bound_pad_count": 0,
        },
    )
    m = extract_board_metrics(board)
    assert "lvs_clean" not in m
    assert "lvs_mismatches" not in m
    assert m["status"] == "partial"


def test_legacy_vacuous_clean_true_lvs_json_not_rendered_clean(tmp_path: Path):
    # Defense-in-depth (#4006): a pre-guard artifact claiming clean=true
    # while self-identifying zero bound pads must NOT surface lvs_clean=true.
    board = _make_board(
        tmp_path,
        lvs={
            "$schema": "https://kicad-tools.org/schemas/lvs/v1.json",
            "clean": True,
            "mismatches": [],
            "copper_mismatches": [],
            "copper_bound_pad_count": 0,
        },
    )
    m = extract_board_metrics(board)
    assert "lvs_clean" not in m
    assert "lvs_mismatches" not in m


def test_lvs_fields_omitted_when_lvs_json_absent(tmp_path: Path):
    # No lvs.json on disk -> both LVS fields are OMITTED (never null), and
    # readiness evidence is still required for status ok.
    board = _make_board(tmp_path)
    m = extract_board_metrics(board)
    assert "lvs_clean" not in m
    assert "lvs_mismatches" not in m
    # Without current readiness evidence, the historical report is partial.
    assert m["status"] == "partial"


def test_lvs_fields_omitted_when_lvs_json_malformed(tmp_path: Path):
    # Malformed JSON in lvs.json -> warning logged, fields omitted, no crash.
    board = _make_board(tmp_path, lvs_raw="{not json")
    m = extract_board_metrics(board)
    assert "lvs_clean" not in m
    assert "lvs_mismatches" not in m
    assert m["status"] == "partial"


@pytest.fixture
def development_board(tmp_path):
    import hashlib

    import yaml

    from kicad_tools.schema.pcb import PCB

    board = tmp_path / "development"
    output = board / "output"
    output.mkdir(parents=True)
    PCB.create(width=30, height=20, layers=4).save(output / "actual.kicad_pcb")
    project = {
        "project": {
            "name": "Development",
            "description": "Unqualified prototype",
            "artifacts": {"pcb": "output/actual.kicad_pcb"},
        }
    }
    (board / "project.kct").write_text(yaml.safe_dump(project))
    report = output / "native-drc.json"
    report.write_text(
        json.dumps({"violations": [], "unconnected_items": [{"type": "unconnected_items"}]})
    )
    inputs = {
        p.relative_to(board).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [output / "actual.kicad_pcb", board / "project.kct", report]
    }
    (output / "readiness.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "assembly",
                "status": "blocked",
                "checked_at": "2026-09-10T00:00:00Z",
                "blockers": ["Routing incomplete"],
                "checks": [],
                "inputs": inputs,
                "evidence": {"native_drc": "output/native-drc.json"},
            }
        )
    )
    return board


def test_development_metadata_preserves_blocked_evidence(development_board):
    before = {p: p.read_bytes() for p in development_board.rglob("*") if p.is_file()}
    result = extract_board_metrics(development_board)
    assert result["status"] == "partial"
    assert result["name"] == "Development"
    assert result["description"] == "Unqualified prototype"
    assert result["layer_count"] == 4
    assert result["part_count"] == 0
    assert result["board_size_mm"] == {"width": 30, "height": 20}
    assert result["readiness"]["blockers"] == ["Routing incomplete"]
    assert result["drc_violations"] == 0
    assert result["native_drc_unconnected_items"] == 1
    assert "lvs_clean" not in result
    assert result["sources"]["pcb"]["path"] == "output/actual.kicad_pcb"
    assert before == {p: p.read_bytes() for p in development_board.rglob("*") if p.is_file()}


def test_development_stale_evidence_keeps_static_metadata(development_board):
    pcb = development_board / "output/actual.kicad_pcb"
    pcb.write_text(pcb.read_text() + "\n")
    result = extract_board_metrics(development_board)
    assert result["layer_count"] == 4
    assert result["readiness"]["status"] == "unverified"
    assert "drc_violations" not in result
    assert "native_drc_unconnected_items" not in result


@pytest.mark.parametrize("scenario", ["missing_explicit", "ambiguous", "malformed"])
def test_development_never_guesses_artifacts(development_board, scenario):
    pcb = development_board / "output/actual.kicad_pcb"
    if scenario == "missing_explicit":
        pcb.rename(pcb.with_name("different.kicad_pcb"))
    elif scenario == "ambiguous":
        (development_board / "project.kct").unlink()
        pcb.with_name("other.kicad_pcb").write_bytes(pcb.read_bytes())
    else:
        pcb.write_text("(not_a_board)")
    result = extract_board_metrics(development_board)
    assert "part_count" not in result
    assert "layer_count" not in result
    assert result["diagnostics"]
    assert result["status"] != "ok"


def test_development_unambiguous_fallback(development_board):
    (development_board / "project.kct").unlink()
    result = extract_board_metrics(development_board)
    assert result["layer_count"] == 4
    assert result["sources"]["pcb"]["selection"] == "unambiguous_output"


@pytest.mark.parametrize(
    "scenario", ["unbound_pcb", "unbound_report", "invalid_report", "ambiguous_report"]
)
def test_development_requires_complete_native_binding(development_board, scenario):
    import hashlib

    output = development_board / "output"
    path = output / "readiness.json"
    readiness = json.loads(path.read_text())
    if scenario == "unbound_pcb":
        del readiness["inputs"]["output/actual.kicad_pcb"]
    elif scenario == "unbound_report":
        del readiness["inputs"]["output/native-drc.json"]
    elif scenario == "invalid_report":
        report = output / "native-drc.json"
        report.write_text(json.dumps({"violations": []}))
        readiness["inputs"]["output/native-drc.json"] = hashlib.sha256(
            report.read_bytes()
        ).hexdigest()
    else:
        readiness.pop("evidence")
        second = output / "placement-drc.json"
        second.write_bytes((output / "native-drc.json").read_bytes())
        readiness["inputs"]["output/placement-drc.json"] = hashlib.sha256(
            second.read_bytes()
        ).hexdigest()
    path.write_text(json.dumps(readiness))
    result = extract_board_metrics(development_board)
    assert result["status"] == "partial"
    assert "drc_violations" not in result
    assert "native_drc_unconnected_items" not in result
    assert any("Native DRC" in message for message in result["diagnostics"])


def test_development_missing_outline_does_not_report_zero_size(development_board):
    from kicad_tools.schema.pcb import PCB

    path = development_board / "output/actual.kicad_pcb"
    pcb = PCB.load(path)
    for child in list(pcb._sexp.iter_children()):
        if child.tag.startswith("gr_"):
            pcb._sexp.children.remove(child)
    pcb.save(path)
    result = extract_board_metrics(development_board)
    assert result["layer_count"] == 4
    assert "board_size_mm" not in result
    assert result["diagnostics"]


def test_development_schematic_only_and_invalid_project(tmp_path):
    board = tmp_path / "schematic-only"
    output = board / "output"
    output.mkdir(parents=True)
    (output / "only.kicad_sch").write_text("(kicad_sch (version 20250114) (generator test))")
    result = extract_board_metrics(board)
    assert result["status"] == "partial"
    assert result["sources"]["schematic"]["path"] == "output/only.kicad_sch"
    assert "part_count" not in result
    (board / "project.kct").write_text("project: [broken")
    result = extract_board_metrics(board)
    assert result["status"] == "no_artifacts"
    assert not result["sources"]
    assert result["diagnostics"]


def test_development_cli_dry_run_leaves_sources_unchanged(development_board, capsys):
    before = {p: p.read_bytes() for p in development_board.rglob("*") if p.is_file()}
    assert main([str(development_board), "--format", "json", "--dry-run"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["boards"][0]["metrics"]["status"] == "partial"
    assert before == {p: p.read_bytes() for p in development_board.rglob("*") if p.is_file()}
    assert not (development_board / "output/manufacturing").exists()


def test_development_curved_outline_omits_inexact_dimensions(development_board):
    pcb = development_board / "output/actual.kicad_pcb"
    text = pcb.read_text().rstrip()
    pcb.write_text(
        text[:-1]
        + '\n(gr_circle (center 10 10) (end 15 10) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts")))'
    )
    result = extract_board_metrics(development_board)
    assert "board_size_mm" not in result
    assert any("curved/unsupported" in d for d in result["diagnostics"])


def test_development_explicit_source_cannot_escape_board(development_board):
    import yaml

    project = development_board / "project.kct"
    data = yaml.safe_load(project.read_text())
    outside = development_board.parent / "outside.kicad_pcb"
    outside.write_bytes((development_board / "output/actual.kicad_pcb").read_bytes())
    data["project"]["artifacts"]["pcb"] = "../outside.kicad_pcb"
    project.write_text(yaml.safe_dump(data))
    result = extract_board_metrics(development_board)
    assert "part_count" not in result
    assert any("outside-board" in d for d in result["diagnostics"])
