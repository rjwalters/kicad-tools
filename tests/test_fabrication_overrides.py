"""Tests for the validated per-board fabrication-override contract (#5006).

``kct check --emit-drc-constraints`` and the manufacturing export path both
emit ``.kicad_pro`` / ``.kicad_dru`` sidecars straight from a manufacturer
profile's conservative ``DesignRules`` defaults, silently overwriting any
board-reviewed, actually-fab-verified floor (e.g. JLC's published 0.45mm
pad-hole spacing vs. the profile's conservative 0.5mm default). This module
provides the shared, validated override contract; these tests cover both
retention of a valid override and rejection of unsafe ones.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.manufacturers import get_profile
from kicad_tools.manufacturers.fabrication_overrides import (
    FabricationOverride,
    UnsafeFabricationOverrideError,
    apply_fabrication_overrides,
    discover_fabrication_overrides_sidecar,
    fabrication_overrides_sidecar_candidates,
    load_fabrication_overrides,
    resolve_pcb_fabrication_overrides,
    validate_fabrication_override,
)

JLC_TIER1_MFR = "jlcpcb-tier1"
REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_03 = REPO_ROOT / "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb"


def _write_sidecar(directory: Path, **entry) -> Path:
    """Write a ``fabrication_overrides.json`` sidecar with one entry."""
    defaults = {
        "value": 0.45,
        "manufacturer": JLC_TIER1_MFR,
        "source": "https://jlcpcb.com/capabilities/pcb-capabilities/",
        "reason": "Published pad-hole minimum; matches reviewed geometry",
    }
    defaults.update(entry)
    sidecar = directory / "fabrication_overrides.json"
    sidecar.write_text(
        json.dumps({"fabrication_overrides": {"min_hole_to_hole_mm": defaults}}),
        encoding="utf-8",
    )
    return sidecar


def _rules():
    profile = get_profile(JLC_TIER1_MFR)
    return profile.get_design_rules(layers=4, copper_oz=1.0)


def _valid_override(**kwargs) -> FabricationOverride:
    defaults = {
        "field": "min_hole_to_hole_mm",
        "value": 0.45,
        "manufacturer_id": JLC_TIER1_MFR,
        "source": "https://jlcpcb.com/capabilities/pcb-capabilities/",
        "reason": "Published pad-hole minimum; matches reviewed geometry",
    }
    defaults.update(kwargs)
    return FabricationOverride(**defaults)


# ---------------------------------------------------------------------------
# Retention of a valid override
# ---------------------------------------------------------------------------


def test_valid_override_is_retained():
    rules = _rules()
    assert rules.min_hole_to_hole_mm == 0.5  # conservative profile default

    overridden = apply_fabrication_overrides(
        rules, [_valid_override()], manufacturer_id=JLC_TIER1_MFR
    )

    assert overridden.min_hole_to_hole_mm == 0.45
    # Nothing else changed.
    assert overridden.min_trace_width_mm == rules.min_trace_width_mm
    assert overridden.min_via_drill_mm == rules.min_via_drill_mm


def test_no_overrides_is_a_no_op_returning_the_same_object():
    rules = _rules()
    assert apply_fabrication_overrides(rules, [], manufacturer_id=JLC_TIER1_MFR) is rules


def test_validate_fabrication_override_accepts_valid_override():
    # Must not raise.
    validate_fabrication_override(_valid_override(), manufacturer_id=JLC_TIER1_MFR)


def test_valid_override_also_accepted_for_base_jlcpcb_profile():
    profile = get_profile("jlcpcb")
    rules = profile.get_design_rules(layers=2, copper_oz=1.0)
    overridden = apply_fabrication_overrides(
        rules,
        [_valid_override(manufacturer_id="jlcpcb")],
        manufacturer_id="jlcpcb",
    )
    assert overridden.min_hole_to_hole_mm == 0.45


# ---------------------------------------------------------------------------
# Rejection of unsafe overrides
# ---------------------------------------------------------------------------


def test_rejects_value_below_verified_floor():
    with pytest.raises(UnsafeFabricationOverrideError, match="below the verified"):
        validate_fabrication_override(_valid_override(value=0.1), manufacturer_id=JLC_TIER1_MFR)


def test_rejects_unrecognized_field():
    with pytest.raises(UnsafeFabricationOverrideError, match="not an overridable"):
        validate_fabrication_override(
            _valid_override(field="max_board_width_mm", value=1000.0),
            manufacturer_id=JLC_TIER1_MFR,
        )


def test_rejects_missing_source():
    with pytest.raises(UnsafeFabricationOverrideError, match="source"):
        validate_fabrication_override(_valid_override(source=""), manufacturer_id=JLC_TIER1_MFR)


def test_rejects_missing_reason():
    with pytest.raises(UnsafeFabricationOverrideError, match="reason"):
        validate_fabrication_override(_valid_override(reason="   "), manufacturer_id=JLC_TIER1_MFR)


def test_rejects_manufacturer_mismatch():
    with pytest.raises(UnsafeFabricationOverrideError, match="scoped to manufacturer"):
        validate_fabrication_override(
            _valid_override(manufacturer_id="oshpark"), manufacturer_id=JLC_TIER1_MFR
        )


def test_rejects_field_with_no_registered_floor():
    # min_annular_ring_mm is overridable in principle but has no entry in
    # the verified-floor registry for jlcpcb-tier1 -- must fail closed, not
    # silently accept an unvalidated override.
    with pytest.raises(UnsafeFabricationOverrideError, match="no independently verified"):
        validate_fabrication_override(
            _valid_override(field="min_annular_ring_mm", value=0.1),
            manufacturer_id=JLC_TIER1_MFR,
        )


def test_apply_rejects_whole_batch_on_one_unsafe_override():
    rules = _rules()
    with pytest.raises(UnsafeFabricationOverrideError):
        apply_fabrication_overrides(
            rules,
            [_valid_override(), _valid_override(field="min_hole_to_hole_mm", value=0.01)],
            manufacturer_id=JLC_TIER1_MFR,
        )


# ---------------------------------------------------------------------------
# Sidecar loading
# ---------------------------------------------------------------------------


def test_load_fabrication_overrides_round_trips(tmp_path: Path):
    sidecar = tmp_path / "fabrication_overrides.json"
    sidecar.write_text(
        json.dumps(
            {
                "fabrication_overrides": {
                    "min_hole_to_hole_mm": {
                        "value": 0.45,
                        "manufacturer": JLC_TIER1_MFR,
                        "source": "https://jlcpcb.com/capabilities/pcb-capabilities/",
                        "reason": "Published pad-hole minimum",
                    }
                }
            }
        )
    )

    overrides = load_fabrication_overrides(sidecar)

    assert len(overrides) == 1
    assert overrides[0].field == "min_hole_to_hole_mm"
    assert overrides[0].value == 0.45
    assert overrides[0].manufacturer_id == JLC_TIER1_MFR

    # And it survives the full validate + apply pipeline.
    rules = _rules()
    overridden = apply_fabrication_overrides(rules, overrides, manufacturer_id=JLC_TIER1_MFR)
    assert overridden.min_hole_to_hole_mm == 0.45


def test_load_fabrication_overrides_rejects_malformed_json(tmp_path: Path):
    sidecar = tmp_path / "fabrication_overrides.json"
    sidecar.write_text("{not valid json")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_fabrication_overrides(sidecar)


def test_load_fabrication_overrides_rejects_missing_top_level_key(tmp_path: Path):
    sidecar = tmp_path / "fabrication_overrides.json"
    sidecar.write_text(json.dumps({"something_else": {}}))
    with pytest.raises(ValueError, match="fabrication_overrides"):
        load_fabrication_overrides(sidecar)


def test_load_fabrication_overrides_rejects_entry_missing_keys(tmp_path: Path):
    sidecar = tmp_path / "fabrication_overrides.json"
    sidecar.write_text(
        json.dumps(
            {
                "fabrication_overrides": {
                    "min_hole_to_hole_mm": {"value": 0.45},
                }
            }
        )
    )
    with pytest.raises(ValueError, match="must be an object with keys"):
        load_fabrication_overrides(sidecar)


# ---------------------------------------------------------------------------
# Sidecar discovery
# ---------------------------------------------------------------------------


def test_discover_fabrication_overrides_sidecar_finds_bare_file_next_to_pcb(
    tmp_path: Path,
):
    pcb_path = tmp_path / "board.kicad_pcb"
    pcb_path.write_text("(kicad_pcb)")
    sidecar = tmp_path / "fabrication_overrides.json"
    sidecar.write_text(json.dumps({"fabrication_overrides": {}}))

    assert discover_fabrication_overrides_sidecar(pcb_path) == sidecar


def test_discover_fabrication_overrides_sidecar_finds_output_subdir(tmp_path: Path):
    board_dir = tmp_path / "board"
    output_dir = board_dir / "output"
    output_dir.mkdir(parents=True)
    pcb_path = output_dir / "board_routed.kicad_pcb"
    pcb_path.write_text("(kicad_pcb)")
    sidecar = output_dir / "fabrication_overrides.json"
    sidecar.write_text(json.dumps({"fabrication_overrides": {}}))

    assert discover_fabrication_overrides_sidecar(pcb_path) == sidecar


def test_discover_fabrication_overrides_sidecar_returns_none_when_absent(tmp_path: Path):
    pcb_path = tmp_path / "board.kicad_pcb"
    pcb_path.write_text("(kicad_pcb)")
    assert discover_fabrication_overrides_sidecar(pcb_path) is None


def test_fabrication_overrides_sidecar_candidates_probe_order(tmp_path: Path):
    pcb_path = tmp_path / "board.kicad_pcb"
    candidates = fabrication_overrides_sidecar_candidates(pcb_path)
    assert candidates == [
        tmp_path / "fabrication_overrides.json",
        tmp_path / "output" / "fabrication_overrides.json",
        tmp_path.parent / "output" / "fabrication_overrides.json",
    ]


# ---------------------------------------------------------------------------
# resolve_pcb_fabrication_overrides: the shared discover+load+apply helper
# every native-emission / Python-DRC call site uses.
# ---------------------------------------------------------------------------


def test_resolve_no_sidecar_is_a_silent_no_op(tmp_path: Path):
    pcb_path = tmp_path / "board.kicad_pcb"
    pcb_path.write_text("(kicad_pcb)")
    rules = _rules()

    resolved, message = resolve_pcb_fabrication_overrides(
        pcb_path, rules, manufacturer_id=JLC_TIER1_MFR
    )

    assert resolved is rules
    assert message is None


def test_resolve_valid_sidecar_is_applied(tmp_path: Path):
    pcb_path = tmp_path / "board.kicad_pcb"
    pcb_path.write_text("(kicad_pcb)")
    _write_sidecar(tmp_path)
    rules = _rules()

    resolved, message = resolve_pcb_fabrication_overrides(
        pcb_path, rules, manufacturer_id=JLC_TIER1_MFR
    )

    assert resolved.min_hole_to_hole_mm == 0.45
    assert message is not None
    assert message.startswith("applied ")
    assert "min_hole_to_hole_mm" in message


def test_resolve_unsafe_sidecar_falls_back_and_reports_ignoring(tmp_path: Path):
    pcb_path = tmp_path / "board.kicad_pcb"
    pcb_path.write_text("(kicad_pcb)")
    _write_sidecar(tmp_path, value=0.1)  # below the verified floor
    rules = _rules()

    resolved, message = resolve_pcb_fabrication_overrides(
        pcb_path, rules, manufacturer_id=JLC_TIER1_MFR
    )

    assert resolved.min_hole_to_hole_mm == rules.min_hole_to_hole_mm  # unchanged
    assert message is not None
    assert message.startswith("ignoring ")
    assert "below the verified" in message


def test_resolve_normalizes_manufacturer_alias_before_matching_sidecar(tmp_path: Path):
    """A raw ``--mfr`` alias must resolve the same as the canonical id.

    Regression guard: the three native-emission call sites resolve a
    manufacturer id differently before calling this shared entry point --
    some pass ``profile.id`` (already canonical), one (``kct check``) can
    pass the raw, possibly-aliased ``--mfr`` string. A sidecar always
    declares the canonical id, so an alias must not be rejected as a
    manufacturer mismatch.
    """
    pcb_path = tmp_path / "board.kicad_pcb"
    pcb_path.write_text("(kicad_pcb)")
    _write_sidecar(tmp_path, manufacturer="jlcpcb")  # sidecar declares canonical id
    profile = get_profile("jlcpcb")
    rules = profile.get_design_rules(layers=2, copper_oz=1.0)

    resolved, message = resolve_pcb_fabrication_overrides(
        pcb_path,
        rules,
        manufacturer_id="jlc",  # alias for "jlcpcb"
    )

    assert resolved.min_hole_to_hole_mm == 0.45
    assert message is not None
    assert message.startswith("applied ")


def test_resolve_malformed_sidecar_falls_back_and_reports_ignoring(tmp_path: Path):
    pcb_path = tmp_path / "board.kicad_pcb"
    pcb_path.write_text("(kicad_pcb)")
    (tmp_path / "fabrication_overrides.json").write_text("{not valid json")
    rules = _rules()

    resolved, message = resolve_pcb_fabrication_overrides(
        pcb_path, rules, manufacturer_id=JLC_TIER1_MFR
    )

    assert resolved is rules
    assert message is not None
    assert message.startswith("ignoring ")


# ---------------------------------------------------------------------------
# kct check --emit-drc-constraints: retention of a reviewed override, and
# rejection of an unsafe one, both by construction through the CLI (#5006).
# ---------------------------------------------------------------------------


@pytest.fixture
def board_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "board.kicad_pcb"
    dst.write_text(BOARD_03.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


@pytest.mark.skipif(not BOARD_03.exists(), reason="board 03 routed PCB not available")
def test_check_emit_retains_valid_reviewed_override(board_copy: Path, capsys):
    from kicad_tools.cli.check_cmd import main

    _write_sidecar(board_copy.parent)

    main(
        [
            str(board_copy),
            "--mfr",
            JLC_TIER1_MFR,
            "--emit-drc-constraints",
            "--allow-incomplete",
        ]
    )

    err = capsys.readouterr().err
    assert "applied fabrication-overrides sidecar" in err

    pro = json.loads(board_copy.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
    assert pro["board"]["design_settings"]["rules"]["min_hole_to_hole"] == 0.45


@pytest.mark.skipif(not BOARD_03.exists(), reason="board 03 routed PCB not available")
def test_check_emit_rejects_unsafe_override_keeps_profile_default(board_copy: Path, capsys):
    from kicad_tools.cli.check_cmd import main

    _write_sidecar(board_copy.parent, value=0.1)
    default_floor = get_profile(JLC_TIER1_MFR).get_design_rules().min_hole_to_hole_mm

    main(
        [
            str(board_copy),
            "--mfr",
            JLC_TIER1_MFR,
            "--emit-drc-constraints",
            "--allow-incomplete",
        ]
    )

    err = capsys.readouterr().err
    assert "WARNING: ignoring fabrication-overrides sidecar" in err
    assert "below the verified" in err

    pro = json.loads(board_copy.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
    assert pro["board"]["design_settings"]["rules"]["min_hole_to_hole"] == default_floor


@pytest.mark.skipif(not BOARD_03.exists(), reason="board 03 routed PCB not available")
def test_check_python_drc_and_native_emission_agree_on_override(board_copy: Path):
    """The SAME resolved floor reaches both the checker and the sidecars.

    Regression guard for the exact defect in #5006: before this contract, an
    override would have to be applied twice (once for the Python check, once
    for native emission) or not at all -- risking the two engines silently
    disagreeing on the same board.
    """
    from kicad_tools.cli.check_cmd import main
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker

    _write_sidecar(board_copy.parent)

    main(
        [
            str(board_copy),
            "--mfr",
            JLC_TIER1_MFR,
            "--emit-drc-constraints",
            "--allow-incomplete",
        ]
    )

    pro = json.loads(board_copy.with_suffix(".kicad_pro").read_text(encoding="utf-8"))

    # Native emission carries the override. ``min_hole_to_hole`` is a
    # ``.kicad_pro`` project setting only -- KiCad's native DRC has no
    # ``.kicad_dru`` custom-rule constraint for hole-to-hole spacing, so
    # the ``.kicad_pro`` value is the sole native artifact this override
    # can (and must) reach.
    assert pro["board"]["design_settings"]["rules"]["min_hole_to_hole"] == 0.45

    # ...and re-resolving the checker with the SAME sidecar in place lands on
    # the identical floor the Python DRC path used.
    layers = len(PCB.load(board_copy).copper_layers)
    checker = DRCChecker(PCB.load(board_copy), manufacturer=JLC_TIER1_MFR, layers=layers)
    resolved, _ = resolve_pcb_fabrication_overrides(
        board_copy, checker.design_rules, manufacturer_id=JLC_TIER1_MFR
    )
    assert resolved.min_hole_to_hole_mm == 0.45


# ---------------------------------------------------------------------------
# Manufacturing export path (ManufacturingPackage._write_drc_constraints):
# the owner's #5006 comment flagged this as still needed alongside `kct
# check`, since the same profile-default overwrite reproduces there.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not BOARD_03.exists(), reason="board 03 routed PCB not available")
def test_manufacturing_export_retains_valid_reviewed_override(board_copy: Path):
    from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage
    from kicad_tools.export.preflight import PreflightConfig

    _write_sidecar(board_copy.parent)

    config = ManufacturingConfig(
        output_dir=board_copy.parent / "manufacturing",
        include_bom=False,
        include_pnp=False,
        include_gerbers=False,
        include_report=False,
        include_project_zip=False,
        include_readme=False,
        include_manifest=False,
        preflight=PreflightConfig(skip_drc=True, skip_erc=True),
    )
    pkg = ManufacturingPackage(pcb_path=board_copy, manufacturer=JLC_TIER1_MFR, config=config)
    result = pkg.export(config.output_dir)

    assert not any("could not" in w.lower() for w in result.warnings)
    pro = json.loads(board_copy.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
    assert pro["board"]["design_settings"]["rules"]["min_hole_to_hole"] == 0.45


@pytest.mark.skipif(not BOARD_03.exists(), reason="board 03 routed PCB not available")
def test_manufacturing_export_rejects_unsafe_override(board_copy: Path):
    from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage
    from kicad_tools.export.preflight import PreflightConfig

    _write_sidecar(board_copy.parent, value=0.1)
    default_floor = get_profile(JLC_TIER1_MFR).get_design_rules().min_hole_to_hole_mm

    config = ManufacturingConfig(
        output_dir=board_copy.parent / "manufacturing",
        include_bom=False,
        include_pnp=False,
        include_gerbers=False,
        include_report=False,
        include_project_zip=False,
        include_readme=False,
        include_manifest=False,
        preflight=PreflightConfig(skip_drc=True, skip_erc=True),
    )
    pkg = ManufacturingPackage(pcb_path=board_copy, manufacturer=JLC_TIER1_MFR, config=config)
    result = pkg.export(config.output_dir)

    assert any("ignoring fabrication-overrides sidecar" in w for w in result.warnings)
    pro = json.loads(board_copy.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
    assert pro["board"]["design_settings"]["rules"]["min_hole_to_hole"] == default_floor


# ---------------------------------------------------------------------------
# kct route's sidecar emission (_write_drc_constraint_sidecars): the third
# native-emission call site sharing the exact same contract (#5006).
# ---------------------------------------------------------------------------


def test_route_sidecar_emission_retains_valid_reviewed_override(tmp_path: Path):
    from kicad_tools.cli.route_cmd import _write_drc_constraint_sidecars

    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)")
    _write_sidecar(tmp_path)

    _write_drc_constraint_sidecars(board, JLC_TIER1_MFR, layers=4, quiet=True)

    pro = json.loads(board.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
    assert pro["board"]["design_settings"]["rules"]["min_hole_to_hole"] == 0.45


def test_route_sidecar_emission_rejects_unsafe_override(tmp_path: Path, capsys):
    from kicad_tools.cli.route_cmd import _write_drc_constraint_sidecars

    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)")
    _write_sidecar(tmp_path, value=0.1)
    default_floor = get_profile(JLC_TIER1_MFR).get_design_rules().min_hole_to_hole_mm

    _write_drc_constraint_sidecars(board, JLC_TIER1_MFR, layers=4, quiet=False)

    out = capsys.readouterr().out
    assert "ignoring fabrication-overrides sidecar" in out
    pro = json.loads(board.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
    assert pro["board"]["design_settings"]["rules"]["min_hole_to_hole"] == default_floor


# ---------------------------------------------------------------------------
# kct mfr apply-rules: the fourth native-emission call site named in this
# module's own docstring, sharing the exact same contract (#5006).
# ---------------------------------------------------------------------------


def test_mfr_apply_rules_retains_valid_reviewed_override(tmp_path: Path):
    from kicad_tools.cli.mfr import main as mfr_main

    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)")
    _write_sidecar(tmp_path)

    mfr_main(["apply-rules", str(board), JLC_TIER1_MFR, "--layers", "4"])

    pro = json.loads(board.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
    assert pro["board"]["design_settings"]["rules"]["min_hole_to_hole"] == 0.45


def test_mfr_apply_rules_rejects_unsafe_override(tmp_path: Path, capsys):
    from kicad_tools.cli.mfr import main as mfr_main

    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)")
    _write_sidecar(tmp_path, value=0.1)
    default_floor = get_profile(JLC_TIER1_MFR).get_design_rules().min_hole_to_hole_mm

    mfr_main(["apply-rules", str(board), JLC_TIER1_MFR, "--layers", "4"])

    out = capsys.readouterr().out
    assert "ignoring fabrication-overrides sidecar" in out
    pro = json.loads(board.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
    assert pro["board"]["design_settings"]["rules"]["min_hole_to_hole"] == default_floor
