"""
Tests for DRC-constraint emission on manufacturing export (issue #3719).

``kct export`` must write a sibling ``<board>.kicad_pro`` (and
``.kicad_dru``) next to the source board, populated from the target
manufacturer profile, so that ``kicad-cli pcb drc`` checks the board
against the fab's actual capabilities instead of KiCad's stricter
built-in defaults.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kicad_tools.manufacturers import (
    build_default_netclass,
    build_project_data,
    build_project_rules,
    get_profile,
    write_drc_constraints,
)
from kicad_tools.manufacturers.project_generator import (
    _NON_BLOCKING_SEVERITIES,
    merge_project_rules,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BOARD_03 = REPO_ROOT / "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb"


# ---------------------------------------------------------------------------
# build_project_rules: values come from the profile, not hardcoded
# ---------------------------------------------------------------------------


def test_project_rules_match_profile_minimums():
    profile = get_profile("jlcpcb-tier1")
    rules = profile.get_design_rules(layers=4, copper_oz=1.0)

    pro_rules = build_project_rules(rules)

    assert pro_rules["min_track_width"] == rules.min_trace_width_mm
    assert pro_rules["min_clearance"] == rules.min_clearance_mm
    assert pro_rules["min_via_diameter"] == rules.min_via_diameter_mm
    assert pro_rules["min_via_hole"] == rules.min_via_drill_mm
    assert pro_rules["min_through_hole_diameter"] == rules.min_hole_diameter_mm
    assert pro_rules["min_via_annular_width"] == rules.min_annular_ring_mm
    assert pro_rules["min_copper_edge_clearance"] == rules.min_copper_to_edge_mm
    assert pro_rules["min_hole_to_hole"] == rules.min_hole_to_edge_mm


def test_project_rules_change_with_layer_config():
    """A 4-layer profile selects finer via/drill minimums than 2-layer."""
    profile = get_profile("jlcpcb-tier1")
    rules_2l = profile.get_design_rules(layers=2, copper_oz=1.0)
    rules_4l = profile.get_design_rules(layers=4, copper_oz=1.0)

    pro_2l = build_project_rules(rules_2l)
    pro_4l = build_project_rules(rules_4l)

    # 4-layer JLCPCB allows smaller vias/holes than 2-layer
    assert pro_4l["min_via_diameter"] < pro_2l["min_via_diameter"]
    assert pro_4l["min_via_hole"] < pro_2l["min_via_hole"]


def test_project_rules_differ_by_manufacturer():
    """Changing --mfr changes the emitted rules (not hardcoded)."""
    jlc = get_profile("jlcpcb").get_design_rules(layers=2)
    osh = get_profile("oshpark").get_design_rules(layers=2)

    jlc_rules = build_project_rules(jlc)
    osh_rules = build_project_rules(osh)

    # The two fabs have different capabilities; at least one constraint
    # must differ between the emitted rule sets.
    assert jlc_rules != osh_rules


# ---------------------------------------------------------------------------
# Default netclass clearance (the load-bearing fix for false clearance errors)
# ---------------------------------------------------------------------------


def test_default_netclass_clearance_from_profile():
    profile = get_profile("jlcpcb-tier1")
    rules = profile.get_design_rules(layers=4, copper_oz=1.0)

    nc = build_default_netclass(rules)

    assert nc["name"] == "Default"
    # The applied netclass clearance must reflect the profile, not KiCad's
    # stock 0.20mm default.
    assert nc["clearance"] == rules.min_clearance_mm
    assert nc["track_width"] == rules.min_trace_width_mm
    assert nc["via_diameter"] == rules.min_via_diameter_mm


def test_build_project_data_structure():
    profile = get_profile("jlcpcb-tier1")
    rules = profile.get_design_rules(layers=4, copper_oz=1.0)

    data = build_project_data(
        rules, "myboard", manufacturer_id="jlcpcb-tier1", layers=4, copper_oz=1.0
    )

    ds = data["board"]["design_settings"]
    assert ds["rules"]["min_clearance"] == rules.min_clearance_mm
    assert ds["rule_severities"] == _NON_BLOCKING_SEVERITIES
    assert data["net_settings"]["classes"][0]["clearance"] == rules.min_clearance_mm
    assert data["meta"]["manufacturer"] == "jlcpcb-tier1"


# ---------------------------------------------------------------------------
# rule_severities downgrade non-blocking categories
# ---------------------------------------------------------------------------


def test_non_blocking_severities_downgraded():
    profile = get_profile("jlcpcb-tier1")
    rules = profile.get_design_rules(layers=2)
    data = build_project_data(rules, "b")

    sev = data["board"]["design_settings"]["rule_severities"]
    assert sev["lib_footprint_mismatch"] == "ignore"
    assert sev["isolated_copper"] == "warning"


# ---------------------------------------------------------------------------
# merge preserves an existing project's unrelated keys
# ---------------------------------------------------------------------------


def test_merge_preserves_existing_keys_and_relaxes_default_clearance():
    profile = get_profile("jlcpcb-tier1")
    rules = profile.get_design_rules(layers=4)

    existing = {
        "meta": {"filename": "b.kicad_pro"},
        "board": {
            "design_settings": {
                "rules": {"some_other_rule": 1.23},
                "defaults": {},
            }
        },
        "net_settings": {
            "classes": [
                {"name": "Default", "clearance": 0.20, "track_width": 0.25},
                {"name": "HV", "clearance": 0.5},
            ]
        },
        "custom_top_level": {"keep": True},
    }

    merge_project_rules(existing, rules)

    ds = existing["board"]["design_settings"]
    # Unrelated rule preserved
    assert ds["rules"]["some_other_rule"] == 1.23
    # Profile rule applied
    assert ds["rules"]["min_clearance"] == rules.min_clearance_mm
    # Default netclass clearance relaxed to the profile
    default_cls = next(c for c in existing["net_settings"]["classes"] if c["name"] == "Default")
    assert default_cls["clearance"] == rules.min_clearance_mm
    # Other netclass untouched
    hv = next(c for c in existing["net_settings"]["classes"] if c["name"] == "HV")
    assert hv["clearance"] == 0.5
    # Unrelated top-level key preserved
    assert existing["custom_top_level"] == {"keep": True}


# ---------------------------------------------------------------------------
# write_drc_constraints writes siblings next to the board
# ---------------------------------------------------------------------------


def test_write_drc_constraints_emits_siblings(tmp_path: Path):
    board = tmp_path / "demo.kicad_pcb"
    board.write_text("(kicad_pcb)")  # content irrelevant for this helper

    profile = get_profile("jlcpcb-tier1")
    rules = profile.get_design_rules(layers=4)

    written = write_drc_constraints(board, rules, manufacturer_id="jlcpcb-tier1", layers=4)

    pro = board.with_suffix(".kicad_pro")
    dru = board.with_suffix(".kicad_dru")
    assert pro in written and pro.exists()
    assert dru in written and dru.exists()

    data = json.loads(pro.read_text())
    assert data["board"]["design_settings"]["rules"]["min_clearance"] == rules.min_clearance_mm


# ---------------------------------------------------------------------------
# End-to-end: kct export emits a sibling .kicad_pro reflecting the profile
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not BOARD_03.exists(), reason="board 03 routed PCB not available")
def test_export_emits_sibling_kicad_pro(tmp_path: Path):
    from kicad_tools.export.manufacturing import (
        ManufacturingConfig,
        ManufacturingPackage,
    )
    from kicad_tools.export.preflight import PreflightConfig

    board = tmp_path / "usb_joystick_routed.kicad_pcb"
    shutil.copy(BOARD_03, board)

    config = ManufacturingConfig(
        output_dir=tmp_path / "manufacturing",
        include_bom=False,
        include_pnp=False,
        include_gerbers=False,
        include_report=False,
        include_project_zip=False,
        include_readme=False,
        include_manifest=False,
        preflight=PreflightConfig(skip_drc=True, skip_erc=True),
    )

    pkg = ManufacturingPackage(
        pcb_path=board,
        manufacturer="jlcpcb-tier1",
        config=config,
    )
    result = pkg.export(config.output_dir)

    pro = board.with_suffix(".kicad_pro")
    assert pro.exists(), "export must write a sibling .kicad_pro next to the board"
    assert pro in result.drc_constraint_paths

    data = json.loads(pro.read_text())
    rules = get_profile("jlcpcb-tier1").get_design_rules(layers=2, copper_oz=1.0)
    pro_rules = data["board"]["design_settings"]["rules"]
    # Board 03 is 2-layer -> 2-layer profile minimums.
    assert pro_rules["min_track_width"] == rules.min_trace_width_mm
    assert pro_rules["min_clearance"] == rules.min_clearance_mm
    assert pro_rules["min_via_diameter"] == rules.min_via_diameter_mm
    assert data["net_settings"]["classes"][0]["clearance"] == rules.min_clearance_mm
