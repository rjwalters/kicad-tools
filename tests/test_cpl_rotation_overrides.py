"""Per-project CPL rotation sidecar ``cpl_rotations.yaml`` (issue #9322)."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from kicad_tools.export.assembly import (
    AssemblyConfig,
    AssemblyPackage,
    profile_rotation_corrections,
    review_cpl_rotations,
)
from kicad_tools.export.pnp import PlacementData, export_pnp
from kicad_tools.export.rotation_overrides import (
    SIDECAR_NAME,
    RotationOverridesError,
    discover_rotation_overrides,
    is_polarised_reference,
    load_rotation_overrides,
    parse_rotation_overrides,
    resolve_rotation_correction,
)
from kicad_tools.schema.pcb import PCB

# The three parts from the issue plus a stock SOT-23 (shipped-table +180), an
# unrelated header (same footprint, different LCSC) and a bottom-side header.
_PCB = """(kicad_pcb (version 20240108) (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical" (layer "F.Cu") (at 10 10 0) (attr through_hole)
    (property "Reference" "J1") (property "Value" "5p") (property "LCSC" "C5156614")
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask")))
  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical" (layer "F.Cu") (at 20 10 270) (attr through_hole)
    (property "Reference" "J2") (property "Value" "4p") (property "LCSC" "C5116483")
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask")))
  (footprint "Connector_Audio:Jack_3.5mm_CUI_SJ-3523-SMT_Horizontal" (layer "F.Cu") (at 30 10 180) (attr smd)
    (property "Reference" "J3") (property "Value" "jack") (property "LCSC" "C4991872")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask")))
  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical" (layer "F.Cu") (at 40 10 0) (attr through_hole)
    (property "Reference" "J4") (property "Value" "5p other") (property "LCSC" "C9999999")
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask")))
  (footprint "Package_TO_SOT_SMD:SOT-23" (layer "F.Cu") (at 50 10 0) (attr smd)
    (property "Reference" "Q1") (property "Value" "npn")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask")))
  (footprint "Resistor_SMD:R_0402" (layer "F.Cu") (at 60 10 0) (attr smd)
    (property "Reference" "R1") (property "Value" "10k")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask")))
  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical" (layer "B.Cu") (at 70 10 350) (attr through_hole)
    (property "Reference" "J5") (property "Value" "5p bottom") (property "LCSC" "C5156614")
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask")))
)
"""

_SIDECAR = """\
by_lcsc:
  C5156614: 90
  C5116483: 90
  C4991872: 270
"""


@pytest.fixture
def board(tmp_path: Path) -> Path:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_PCB)
    return path


def _rotations(csv_text: str) -> dict[str, float]:
    return {r["Designator"]: float(r["Rotation"]) for r in csv.DictReader(io.StringIO(csv_text))}


def _cpl(board: Path, **kwargs) -> dict[str, float]:
    pcb = PCB.load(board)
    kwargs.setdefault("rotation_corrections", profile_rotation_corrections("jlcpcb"))
    return _rotations(export_pnp(pcb.footprints, "jlcpcb", pcb_path=board, **kwargs))


# --------------------------------------------------------------------------
# Sidecar parsing
# --------------------------------------------------------------------------


def test_parse_valid_sidecar_normalises_lcsc(tmp_path):
    raw = b"by_reference:\n  J1: 90\nby_lcsc:\n  ' c5156614 ': -90.5\n"
    overrides = parse_rotation_overrides(raw, tmp_path / SIDECAR_NAME)
    assert overrides.by_reference == {"J1": 90.0}
    assert overrides.by_lcsc == {"C5156614": -90.5}
    assert len(overrides.sha256) == 64


def test_empty_sidecar_is_valid_and_falsy(tmp_path):
    overrides = parse_rotation_overrides(b"# nothing yet\n", tmp_path / SIDECAR_NAME)
    assert not overrides


@pytest.mark.parametrize(
    "text",
    [
        "by_lcsc: [C1, C2]\n",  # wrong shape
        "by_reference:\n  J1: ninety\n",  # non-numeric
        "by_reference:\n  J1: true\n",  # bool is not degrees
        "by_reference:\n  J1: .inf\n",  # not finite
        "by_lcsc:\n  5156614: 90\n",  # not an LCSC number
        "by_ref:\n  J1: 90\n",  # typo'd section
        "- J1\n- J2\n",  # not a mapping
        "by_reference: {J1: 90\n",  # broken YAML
    ],
)
def test_invalid_sidecar_raises(tmp_path, text):
    with pytest.raises(RotationOverridesError):
        parse_rotation_overrides(text.encode(), tmp_path / SIDECAR_NAME)


def test_duplicate_lcsc_after_normalisation_rejected(tmp_path):
    with pytest.raises(RotationOverridesError, match="twice"):
        parse_rotation_overrides(b"by_lcsc:\n  C1: 90\n  c1: 180\n", tmp_path / SIDECAR_NAME)


def test_load_missing_file_is_an_error(tmp_path):
    with pytest.raises(RotationOverridesError):
        load_rotation_overrides(tmp_path / "nope.yaml")


def test_discover_sits_next_to_pcb(board):
    assert discover_rotation_overrides(board) is None
    (board.parent / SIDECAR_NAME).write_text(_SIDECAR)
    assert discover_rotation_overrides(board) == board.parent / SIDECAR_NAME


# --------------------------------------------------------------------------
# Resolution: precedence and additivity
# --------------------------------------------------------------------------


def _placement(ref="J1", footprint="Lib:FP", lcsc="") -> PlacementData:
    return PlacementData(ref, "v", footprint, 0, 0, 0, "F.Cu", lcsc=lcsc)


def test_precedence_reference_over_lcsc_over_glob(tmp_path):
    overrides = parse_rotation_overrides(
        b"by_reference:\n  J1: 10\nby_lcsc:\n  C1: 20\n", tmp_path / SIDECAR_NAME
    )
    glob = {"SOT-23*": 180.0}
    assert resolve_rotation_correction(_placement("J1", lcsc="C1"), glob, overrides) == (
        10.0,
        "by_reference",
    )
    assert resolve_rotation_correction(_placement("J2", lcsc="c1"), glob, overrides) == (
        20.0,
        "by_lcsc",
    )
    assert resolve_rotation_correction(
        _placement("Q1", "Package_TO_SOT_SMD:SOT-23"), glob, overrides
    ) == (180.0, "footprint_glob")
    assert resolve_rotation_correction(_placement("R1"), glob, overrides) == (0.0, None)


def test_sidecar_entry_replaces_glob_for_that_part(tmp_path):
    overrides = parse_rotation_overrides(b"by_reference:\n  Q1: 0\n", tmp_path / SIDECAR_NAME)
    degrees, source = resolve_rotation_correction(
        _placement("Q1", "Package_TO_SOT_SMD:SOT-23"), {"SOT-23*": 180.0}, overrides
    )
    assert (degrees, source) == (0.0, "by_reference")


def test_without_sidecar_connectors_are_unchanged(board):
    rot = _cpl(board)
    assert (rot["J1"], rot["J2"], rot["J3"]) == (0.0, 270.0, 180.0)


def test_issue_scenario_three_parts_corrected_by_lcsc(board, tmp_path):
    overrides = parse_rotation_overrides(_SIDECAR.encode(), tmp_path / SIDECAR_NAME)
    rot = _cpl(board, rotation_overrides=overrides)
    assert rot["J1"] == 90.0  # 0 + 90
    assert rot["J2"] == 0.0  # 270 + 90 wraps at 360
    assert rot["J3"] == 90.0  # 180 + 270 wraps at 360
    # The same footprint with a different LCSC number is NOT rotated.
    assert rot["J4"] == 0.0
    # Shipped-table correction still applies where the sidecar is silent.
    assert rot["Q1"] == 180.0


def test_bottom_layer_part_gets_the_same_additive_correction(board, tmp_path):
    overrides = parse_rotation_overrides(_SIDECAR.encode(), tmp_path / SIDECAR_NAME)
    rot = _cpl(board, rotation_overrides=overrides)
    assert rot["J5"] == 80.0  # 350 + 90, wrapped


def test_lcsc_from_bom_map_beats_missing_footprint_property(board, tmp_path):
    overrides = parse_rotation_overrides(b"by_lcsc:\n  C777: 90\n", tmp_path / SIDECAR_NAME)
    # J4 carries C9999999 on the footprint; the BOM's number wins.
    rot = _cpl(board, rotation_overrides=overrides, lcsc_by_reference={"J4": "C777"})
    assert rot["J4"] == 90.0


def test_footprint_without_lcsc_is_left_alone(board, tmp_path):
    overrides = parse_rotation_overrides(b"by_lcsc:\n  C1: 90\n", tmp_path / SIDECAR_NAME)
    assert _cpl(board, rotation_overrides=overrides)["R1"] == 0.0


# --------------------------------------------------------------------------
# Review: applied entries, unreviewed list, stale keys
# --------------------------------------------------------------------------


def test_review_lists_applied_and_unreviewed(board):
    (board.parent / SIDECAR_NAME).write_text("by_lcsc:\n  C5156614: 90\nby_reference:\n  Z99: 90\n")
    review = review_cpl_rotations(board, "jlcpcb")
    applied = {(a["reference"], a["source"], a["key"], a["degrees"]) for a in review.applied}
    assert ("J1", "by_lcsc", "C5156614", 90.0) in applied
    assert ("J5", "by_lcsc", "C5156614", 90.0) in applied
    assert ("Q1", "footprint_glob", "Package_TO_SOT_SMD:SOT-23", 180.0) in applied
    # Polarised parts nothing matched: J2/J3/J4. R1 is not polarised; Q1 matched.
    assert [u["reference"] for u in review.unreviewed] == ["J2", "J3", "J4"]
    assert review.sidecar_sha256 and len(review.sidecar_sha256) == 64
    assert review.unmatched_overrides == ["by_reference:Z99"]


def test_review_without_sidecar_still_lists_unreviewed(board):
    review = review_cpl_rotations(board, "jlcpcb")
    assert review.sidecar is None
    assert "J1" in [u["reference"] for u in review.unreviewed]


def test_review_rejects_invalid_sidecar(board):
    (board.parent / SIDECAR_NAME).write_text("by_lcsc: nope\n")
    with pytest.raises(RotationOverridesError):
        review_cpl_rotations(board, "jlcpcb")


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("J1", True),
        ("U12", True),
        ("D3", True),
        ("Q1", True),
        ("LED2", True),
        ("R1", False),
        ("C5", False),
        ("JP1", False),
        ("", False),
    ],
)
def test_polarised_prefixes(ref, expected):
    assert is_polarised_reference(ref) is expected


# --------------------------------------------------------------------------
# AssemblyPackage: auto-discovery, result recording, errors
# --------------------------------------------------------------------------


def _package(board: Path, **config) -> AssemblyPackage:
    return AssemblyPackage(
        board,
        None,
        "jlcpcb",
        AssemblyConfig(include_bom=False, include_gerbers=False, **config),
    )


def test_assembly_package_auto_discovers_sidecar(board, tmp_path):
    (board.parent / SIDECAR_NAME).write_text(_SIDECAR)
    result = _package(board).export(tmp_path / "out")
    assert result.success, result.errors
    rows = _rotations(result.pnp_path.read_text())
    assert (rows["J1"], rows["J2"], rows["J3"]) == (90.0, 0.0, 90.0)
    review = result.rotation_review
    assert review is not None and review.sidecar == str(board.parent / SIDECAR_NAME)
    # J1, J2, J3, J5 by LCSC plus Q1 by the shipped table.
    assert result.rotation_corrections_applied == 5
    assert "Rotation overrides" in str(result)


def test_assembly_package_without_sidecar_matches_old_behaviour(board, tmp_path):
    result = _package(board).export(tmp_path / "out")
    rows = _rotations(result.pnp_path.read_text())
    assert (rows["J1"], rows["J2"], rows["J3"]) == (0.0, 270.0, 180.0)
    assert result.rotation_corrections_applied == 1  # Q1 via the shipped table
    assert result.rotation_review.sidecar is None


def test_assembly_package_opt_out(board, tmp_path):
    (board.parent / SIDECAR_NAME).write_text(_SIDECAR)
    result = _package(board, no_rotation_overrides=True).export(tmp_path / "out")
    assert _rotations(result.pnp_path.read_text())["J1"] == 0.0


def test_assembly_package_explicit_path_must_exist(board, tmp_path):
    result = _package(board, rotation_overrides_path=tmp_path / "missing.yaml").export(
        tmp_path / "out"
    )
    assert not result.success
    assert any("CPL generation failed" in e for e in result.errors)


def test_invalid_sidecar_fails_the_cpl_instead_of_shipping_uncorrected(board, tmp_path):
    (board.parent / SIDECAR_NAME).write_text("by_reference:\n  J1: ninety\n")
    result = _package(board).export(tmp_path / "out")
    assert not result.success
    assert any("J1" in e for e in result.errors)


# --------------------------------------------------------------------------
# Readiness: provenance binding, report block, verify
# --------------------------------------------------------------------------


def _readiness_options(board: Path, out: Path):
    from types import SimpleNamespace

    return SimpleNamespace(assembly=True, pcb=board, output_dir=out, manufacturer="jlcpcb")


def _ship_bundle(board: Path, out: Path) -> None:
    """A real export, so the retained CPL/BOM match what readiness re-derives."""
    result = AssemblyPackage(
        board, None, "jlcpcb", AssemblyConfig(include_bom=False, include_gerbers=False)
    ).export(out)
    assert result.success, result.errors


def test_readiness_report_block_records_sidecar_and_review(board, tmp_path):
    import hashlib

    from kicad_tools.cli.readiness_cmd import _cpl_rotation_state

    sidecar = board.parent / SIDECAR_NAME
    sidecar.write_text(_SIDECAR)
    out = tmp_path / "out"
    _ship_bundle(board, out)
    review, error = _cpl_rotation_state(_readiness_options(board, out))
    assert error is None and review is not None
    block = review.to_report("board/" + SIDECAR_NAME)
    assert block["sidecar"] == "board/" + SIDECAR_NAME
    assert block["sidecar_sha256"] == hashlib.sha256(sidecar.read_bytes()).hexdigest()
    assert {(a["reference"], a["source"], a["degrees"]) for a in block["applied"]} >= {
        ("J1", "by_lcsc", 90.0),
        ("J2", "by_lcsc", 90.0),
        ("J3", "by_lcsc", 270.0),
    }
    assert [u["reference"] for u in block["unreviewed"]] == ["J4"]


def test_verify_passes_on_untouched_bundle_and_fails_when_sidecar_edited(board, tmp_path):
    from kicad_tools.cli.readiness_cmd import _verify_cpl_rotations

    sidecar = board.parent / SIDECAR_NAME
    sidecar.write_text(_SIDECAR)
    out = tmp_path / "out"
    _ship_bundle(board, out)
    options = _readiness_options(board, out)
    assert _verify_cpl_rotations(options) == []

    sidecar.write_text(_SIDECAR.replace("C5156614: 90", "C5156614: 180"))
    problems = _verify_cpl_rotations(options)
    assert problems and any("J1" in p and "180" in p for p in problems)


def test_verify_defers_a_removed_sidecar_to_the_hash_binding(board, tmp_path):
    from kicad_tools.cli.readiness_cmd import _verify_cpl_rotations

    (board.parent / SIDECAR_NAME).write_text(_SIDECAR)
    out = tmp_path / "out"
    _ship_bundle(board, out)
    # Removed: the CPL still carries corrections the project no longer declares.
    (board.parent / SIDECAR_NAME).unlink()
    # With no sidecar present there is nothing to re-derive; the hash binding
    # (read_readiness: "input missing") is what catches this -- see below.
    assert _verify_cpl_rotations(_readiness_options(board, out)) == []


def test_verify_reports_invalid_sidecar(board, tmp_path):
    from kicad_tools.cli.readiness_cmd import _verify_cpl_rotations

    out = tmp_path / "out"
    _ship_bundle(board, out)
    (board.parent / SIDECAR_NAME).write_text("by_lcsc: nope\n")
    problems = _verify_cpl_rotations(_readiness_options(board, out))
    assert problems and "invalid" in problems[0]


def test_verify_without_sidecar_does_not_inspect_the_cpl(board, tmp_path):
    from kicad_tools.cli.readiness_cmd import _verify_cpl_rotations

    out = tmp_path / "out"
    _ship_bundle(board, out)
    assert _verify_cpl_rotations(_readiness_options(board, out)) == []


# --------------------------------------------------------------------------
# The re-derivation must fail visibly, never silently skip (#9346)
# --------------------------------------------------------------------------


def test_unexpected_rederivation_error_is_reported_not_swallowed(board, tmp_path, monkeypatch):
    import kicad_tools.cli.readiness_cmd as readiness_cmd
    import kicad_tools.export.assembly as assembly_mod

    out = tmp_path / "out"
    _ship_bundle(board, out)

    def boom(*args, **kwargs):
        raise RuntimeError("rederive blew up")

    # Patch at the source module: _cpl_rotation_state imports the symbol at
    # call time, so the local import must see the replacement.
    monkeypatch.setattr(assembly_mod, "review_cpl_rotations", boom)
    options = _readiness_options(board, out)
    review, problem = readiness_cmd._cpl_rotation_state(options)
    assert review is None
    assert problem and "RuntimeError" in problem and "rederive blew up" in problem
    assert readiness_cmd._verify_cpl_rotations(options) == [problem]


def test_unreadable_board_reports_a_visible_skip_not_silence(board, tmp_path):
    from kicad_tools.cli.readiness_cmd import _cpl_rotation_state, _verify_cpl_rotations

    out = tmp_path / "out"
    _ship_bundle(board, out)
    # A CPL exists, so the export parsed this board once; if it no longer
    # parses the re-derivation must say so instead of passing by omission.
    board.write_text("(kicad_pcb (version 20240108)")
    review, problem = _cpl_rotation_state(_readiness_options(board, out))
    assert review is None
    assert problem and "skipped" in problem and "ParseError" in problem
    assert _verify_cpl_rotations(_readiness_options(board, out)) == [problem]


def test_report_block_records_the_skip(board, tmp_path):
    from kicad_tools.cli.readiness_cmd import _cpl_rotation_report_entry

    out = tmp_path / "out"
    _ship_bundle(board, out)
    board.write_text("(kicad_pcb (version 20240108)")
    entry = _cpl_rotation_report_entry(_readiness_options(board, out))
    assert entry is not None and "rederivation_skipped" in entry

    board.write_text(_PCB)
    entry = _cpl_rotation_report_entry(_readiness_options(board, out))
    assert entry is not None
    assert "rederivation_skipped" not in entry and "applied" in entry


def test_verify_still_reports_an_invalid_sidecar_message(board, tmp_path):
    from kicad_tools.cli.readiness_cmd import _verify_cpl_rotations

    out = tmp_path / "out"
    _ship_bundle(board, out)
    (board.parent / SIDECAR_NAME).write_text("by_lcsc: nope\n")
    problems = _verify_cpl_rotations(_readiness_options(board, out))
    assert problems and "invalid" in problems[0]


def test_readiness_rederivation_mirrors_include_tht(board, tmp_path, monkeypatch):
    import kicad_tools.export.assembly as assembly_mod
    from kicad_tools.cli.readiness_cmd import _cpl_rotation_state
    from kicad_tools.export.pnp import PnPExportConfig

    out = tmp_path / "out"
    _ship_bundle(board, out)
    captured: dict[str, object] = {}
    real = assembly_mod.review_cpl_rotations

    def spy(*args, **kwargs):
        captured["pnp_config"] = kwargs.get("pnp_config")
        return real(*args, **kwargs)

    monkeypatch.setattr(assembly_mod, "review_cpl_rotations", spy)
    _cpl_rotation_state(_readiness_options(board, out))
    assert captured["pnp_config"] is None  # no --include-tht: the fab's resolution

    options = _readiness_options(board, out)
    options.include_tht = True
    _cpl_rotation_state(options)
    assert captured["pnp_config"] == PnPExportConfig(exclude_tht=False)


def test_review_cpl_rotations_honours_exclude_tht(board):
    from kicad_tools.export.pnp import PnPExportConfig

    tht_refs = {"J1", "J2", "J4", "J5"}
    with_tht = review_cpl_rotations(board, "jlcpcb")
    assert tht_refs <= set(with_tht.rotations)
    without_tht = review_cpl_rotations(
        board, "jlcpcb", pnp_config=PnPExportConfig(exclude_tht=True)
    )
    assert not tht_refs & set(without_tht.rotations)
    assert "Q1" in without_tht.rotations


def test_resolve_pnp_config_mirrors_the_export_resolution():
    from kicad_tools.export.assembly import resolve_pnp_config
    from kicad_tools.export.pnp import PnPExportConfig

    explicit = PnPExportConfig(exclude_tht=False)
    assert resolve_pnp_config("jlcpcb", explicit) is explicit
    assert resolve_pnp_config("jlcpcb", None) is None  # aux-origin preset: left to the formatter
    assert resolve_pnp_config("oshpark", None) == PnPExportConfig(use_aux_origin=False)


def _bound_report(root: Path):
    import hashlib
    import json

    from tests.test_board_readiness import report_fixture, save

    report_fixture(root)
    sidecar = root / "output" / SIDECAR_NAME
    sidecar.write_text(_SIDECAR)
    report = json.loads((root / "output/readiness.json").read_text())
    report["inputs"][f"output/{SIDECAR_NAME}"] = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    save(root, report)
    return sidecar


def test_readiness_input_binds_sidecar_hash_missing_and_changed(tmp_path):
    from kicad_tools.cli.board_readiness import read_readiness

    sidecar = _bound_report(tmp_path)
    assert read_readiness(tmp_path)["status"] == "ready"

    sidecar.write_text(_SIDECAR + "by_reference:\n  J9: 90\n")
    changed = read_readiness(tmp_path)
    assert changed["status"] == "unverified" and "stale" in changed["blockers"][0]

    sidecar.unlink()
    missing = read_readiness(tmp_path)
    assert missing["status"] == "unverified"


def test_unlisted_rotation_sidecar_refuses_evidence(tmp_path):
    from kicad_tools.cli.board_readiness import read_readiness
    from tests.test_board_readiness import report_fixture

    report_fixture(tmp_path)
    (tmp_path / "output" / SIDECAR_NAME).write_text(_SIDECAR)
    result = read_readiness(tmp_path)
    assert result["status"] == "unverified"
    assert "omits current project or rule files" in result["blockers"][0]
