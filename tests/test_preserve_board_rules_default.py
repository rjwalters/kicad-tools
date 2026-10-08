"""Stricter authored DRC minima win by default (Issue #6191).

``KCT_PRESERVE_BOARD_RULES`` is tri-state:

* unset / empty -- keep stricter minima (``max(authored, profile)``),
  overwrite severities, relax an untouched template ``Default`` netclass,
  and emit ``Reviewed clearance`` DRU rules only above the floor;
* ``1`` / ``true`` -- the #5023 opt-in, unchanged (covered in
  ``test_drc_constraints_export.py`` / ``test_check_emit_dru.py``);
* ``0`` / ``false`` -- the legacy profile overwrite.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

import pytest

from kicad_tools.core.project_file import DEFAULT_NETCLASS_DEFINITION, create_minimal_project
from kicad_tools.manufacturers import get_profile, write_drc_constraints
from kicad_tools.manufacturers.project_generator import (
    PRESERVE_MODE_FULL,
    PRESERVE_MODE_OFF,
    PRESERVE_MODE_STRICTER,
    TEMPLATE_DEFAULT_NETCLASS_SIGNATURES,
    generate_project_dru,
    is_template_default_netclass,
    merge_project_rules,
    preserve_board_rules_mode,
)


@pytest.fixture
def rules():
    # jlcpcb-tier1 4L: clearance / track 0.1016, via 0.45 / 0.2.
    return get_profile("jlcpcb-tier1").get_design_rules(layers=4)


def _project(flag: str | None = None, default_cls: dict | None = None) -> dict:
    data: dict = {
        "board": {
            "design_settings": {
                "rules": {"min_clearance": 0.15, "min_track_width": 0.15, "min_via_hole": 0.25},
                "defaults": {"clearance_min": 0.15, "via_min_diameter": 0.5},
                "rule_severities": {"isolated_copper": "error"},
            }
        },
        "net_settings": {
            "classes": [
                default_cls
                if default_cls is not None
                else {
                    "name": "Default",
                    "clearance": 0.15,
                    "track_width": 0.16,
                    "via_diameter": 0.5,
                    "via_drill": 0.22,
                },
                {"name": "HV", "clearance": 0.8},
            ]
        },
        "text_variables": {},
    }
    if flag is not None:
        data["text_variables"]["KCT_PRESERVE_BOARD_RULES"] = flag
    return data


def _minima(data: dict) -> dict:
    settings = data["board"]["design_settings"]
    return {
        "rules": settings["rules"],
        "defaults": settings["defaults"],
        "classes": data["net_settings"]["classes"],
    }


# ---------------------------------------------------------------------------
# Mode parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, PRESERVE_MODE_STRICTER),
        ("", PRESERVE_MODE_STRICTER),
        ("  ", PRESERVE_MODE_STRICTER),
        ("1", PRESERVE_MODE_FULL),
        ("true", PRESERVE_MODE_FULL),
        ("TRUE", PRESERVE_MODE_FULL),
        ("Yes", PRESERVE_MODE_FULL),
        ("on", PRESERVE_MODE_FULL),
        ("0", PRESERVE_MODE_OFF),
        ("false", PRESERVE_MODE_OFF),
        ("False", PRESERVE_MODE_OFF),
        ("NO", PRESERVE_MODE_OFF),
        (" off ", PRESERVE_MODE_OFF),
    ],
)
def test_mode_parsing(value, expected):
    data = {} if value is None else {"text_variables": {"KCT_PRESERVE_BOARD_RULES": value}}
    assert preserve_board_rules_mode(data) == expected


def test_unrecognised_mode_warns_and_uses_default(caplog):
    data = {"text_variables": {"KCT_PRESERVE_BOARD_RULES": "maybe"}}
    with caplog.at_level(logging.WARNING, logger="kicad_tools.manufacturers.project_generator"):
        assert preserve_board_rules_mode(data) == PRESERVE_MODE_STRICTER
    assert "KCT_PRESERVE_BOARD_RULES" in caplog.text and "maybe" in caplog.text


# ---------------------------------------------------------------------------
# Default mode: stricter authored minima win
# ---------------------------------------------------------------------------


def test_default_keeps_stricter_authored_minima(rules):
    data = merge_project_rules(_project(), rules)
    settings = data["board"]["design_settings"]
    assert settings["rules"]["min_clearance"] == 0.15
    assert settings["rules"]["min_track_width"] == 0.15
    assert settings["rules"]["min_via_hole"] == 0.25
    # Keys the project did not author take the profile value.
    assert settings["rules"]["min_via_diameter"] == rules.min_via_diameter_mm
    assert settings["defaults"]["clearance_min"] == 0.15
    assert settings["defaults"]["via_min_diameter"] == 0.5
    assert settings["defaults"]["track_min_width"] == rules.min_trace_width_mm
    default_cls = data["net_settings"]["classes"][0]
    assert default_cls == {
        "name": "Default",
        "clearance": 0.15,
        "track_width": 0.16,
        "via_diameter": 0.5,
        "via_drill": 0.22,
    }
    # Severities are still overwritten in the default mode.
    assert settings["rule_severities"]["isolated_copper"] == "warning"
    assert settings["rule_severities"]["lib_footprint_mismatch"] == "ignore"


def test_default_still_tightens_values_below_the_fab_floor(rules):
    data = {
        "board": {
            "design_settings": {
                "rules": {"min_clearance": 0.01, "min_via_hole": 0.05},
                "defaults": {"clearance_min": 0.01},
            }
        },
        "net_settings": {
            "classes": [
                {
                    "name": "Default",
                    "clearance": 0.01,
                    "track_width": 0.02,
                    "via_diameter": 0.1,
                    "via_drill": 0.05,
                }
            ]
        },
    }
    merge_project_rules(data, rules)
    settings = data["board"]["design_settings"]
    assert settings["rules"]["min_clearance"] == rules.min_clearance_mm
    assert settings["rules"]["min_via_hole"] == rules.min_via_drill_mm
    assert settings["defaults"]["clearance_min"] == rules.min_clearance_mm
    cls = data["net_settings"]["classes"][0]
    assert cls["clearance"] == rules.min_clearance_mm
    assert cls["track_width"] == rules.min_trace_width_mm
    assert cls["via_diameter"] == rules.min_via_diameter_mm
    assert cls["via_drill"] == rules.min_via_drill_mm


# ---------------------------------------------------------------------------
# Template-signature exemption
# ---------------------------------------------------------------------------


def _signature_cls(signature) -> dict:
    clearance, track, via_dia, via_drill = signature
    return {
        "name": "Default",
        "clearance": clearance,
        "track_width": track,
        "via_diameter": via_dia,
        "via_drill": via_drill,
    }


KCT_TEMPLATE = tuple(
    DEFAULT_NETCLASS_DEFINITION[k]
    for k in ("clearance", "track_width", "via_diameter", "via_drill")
)


@pytest.mark.parametrize(
    "signature",
    [
        pytest.param(KCT_TEMPLATE, id="kct-template"),
        pytest.param((0.2, 0.25, 0.6, 0.3), id="kct-pre-5654-template"),
        pytest.param((0.2, 0.25, 0.8, 0.4), id="kicad-5-6-stock"),
        pytest.param((0.2, 0.2, 0.6, 0.3), id="kicad-7-plus-stock"),
    ],
)
def test_template_default_netclass_is_relaxed_by_default(rules, signature):
    assert tuple(signature) in TEMPLATE_DEFAULT_NETCLASS_SIGNATURES
    assert is_template_default_netclass(_signature_cls(signature))
    data = merge_project_rules(_project(default_cls=_signature_cls(signature)), rules)
    cls = data["net_settings"]["classes"][0]
    assert cls["clearance"] == rules.min_clearance_mm
    assert cls["track_width"] == rules.min_trace_width_mm
    assert cls["via_diameter"] == rules.min_via_diameter_mm
    assert cls["via_drill"] == rules.min_via_drill_mm
    # The exemption is scoped to the Default netclass; design_settings.rules
    # are still authored.
    assert data["board"]["design_settings"]["rules"]["min_clearance"] == 0.15


def test_template_signature_tolerates_float_noise(rules):
    assert is_template_default_netclass(_signature_cls((0.2 + 1e-9, 0.25, 0.6, 0.3 - 1e-9)))


@pytest.mark.parametrize(
    "signature",
    [
        pytest.param((0.21, 0.25, 0.6, 0.3), id="near-miss-clearance"),
        pytest.param((0.2, 0.25, 0.6, 0.35), id="near-miss-drill"),
    ],
)
def test_near_miss_default_netclass_is_preserved(rules, signature):
    assert not is_template_default_netclass(_signature_cls(signature))
    data = merge_project_rules(_project(default_cls=_signature_cls(signature)), rules)
    assert data["net_settings"]["classes"][0] == _signature_cls(signature)


def test_partial_default_netclass_is_not_a_template_signature(rules):
    cls = {"name": "Default", "clearance": 0.2, "track_width": 0.25}
    assert not is_template_default_netclass(cls)
    data = merge_project_rules(_project(default_cls=cls), rules)
    out = data["net_settings"]["classes"][0]
    assert out["clearance"] == 0.2
    assert out["track_width"] == 0.25
    assert out["via_diameter"] == rules.min_via_diameter_mm


@pytest.mark.parametrize("flag", ["1", "true"])
def test_full_preserve_still_keeps_template_signature_default(rules, flag):
    data = merge_project_rules(
        _project(flag, default_cls=_signature_cls((0.2, 0.25, 0.6, 0.3))), rules
    )
    assert data["net_settings"]["classes"][0] == _signature_cls((0.2, 0.25, 0.6, 0.3))
    assert data["board"]["design_settings"]["rule_severities"]["isolated_copper"] == "error"


# ---------------------------------------------------------------------------
# Explicit opt-out restores the legacy overwrite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["0", "false", "OFF", "no"])
def test_opt_out_restores_legacy_overwrite(rules, flag):
    data = merge_project_rules(_project(flag), rules)
    settings = data["board"]["design_settings"]
    assert settings["rules"]["min_clearance"] == rules.min_clearance_mm
    assert settings["rules"]["min_track_width"] == rules.min_trace_width_mm
    assert settings["rules"]["min_via_hole"] == rules.min_via_drill_mm
    assert settings["defaults"]["clearance_min"] == rules.min_clearance_mm
    assert settings["defaults"]["via_min_diameter"] == rules.min_via_diameter_mm
    assert settings["rule_severities"]["isolated_copper"] == "warning"
    cls = data["net_settings"]["classes"][0]
    assert cls["clearance"] == rules.min_clearance_mm
    assert cls["track_width"] == rules.min_trace_width_mm
    assert cls["via_diameter"] == rules.min_via_diameter_mm
    assert cls["via_drill"] == rules.min_via_drill_mm
    # Non-Default classes are never touched by merge.
    assert data["net_settings"]["classes"][1] == {"name": "HV", "clearance": 0.8}


# ---------------------------------------------------------------------------
# .kicad_dru: scalar floors and reviewed per-class clearance
# ---------------------------------------------------------------------------


def test_default_dru_floors_honour_stricter_native_minima(rules):
    data = merge_project_rules(_project(), rules)
    dru = generate_project_dru(rules, data)
    assert '(rule "Clearance"\n  (constraint clearance (min 0.15mm)))' in dru
    assert f"(min {rules.min_clearance_mm}mm)" not in dru.split('(rule "Clearance"')[1][:60]
    # Default sits AT the floor -> redundant, not emitted; HV is above it.
    assert '"Reviewed clearance - Default"' not in dru
    assert '"Reviewed clearance - HV"' in dru
    assert "(constraint clearance (min 0.8mm))" in dru


def test_default_dru_skips_reviewed_rules_at_or_below_floor(rules):
    data = merge_project_rules(_project(), rules)
    data["net_settings"]["classes"].append({"name": "Low", "clearance": 0.12})
    dru = generate_project_dru(rules, data)
    assert '"Reviewed clearance - Low"' not in dru
    assert '"Reviewed clearance - Default"' not in dru


@pytest.mark.parametrize("flag", ["1", "true"])
def test_full_preserve_dru_still_emits_every_reviewed_rule(rules, flag):
    data = merge_project_rules(_project(flag), rules)
    dru = generate_project_dru(rules, data)
    assert dru.index('"Reviewed clearance - Default"') < dru.index('"Reviewed clearance - HV"')


@pytest.mark.parametrize("flag", ["0", "false"])
def test_opt_out_dru_uses_plain_profile_floors(rules, flag):
    data = _project(flag)
    dru = generate_project_dru(rules, data)
    assert f'(rule "Clearance"\n  (constraint clearance (min {rules.min_clearance_mm}mm)))' in dru
    assert "Reviewed clearance" not in dru
    no_project = generate_project_dru(rules, {"text_variables": {"KCT_PRESERVE_BOARD_RULES": "0"}})
    assert dru == no_project


def test_default_pour_clearance_reaches_written_dru(tmp_path: Path, rules):
    """The #6095 / PR #6157 case: native zone fill reads the DRU floor."""
    board = tmp_path / "pour.kicad_pcb"
    board.write_text("(kicad_pcb)")
    board.with_suffix(".kicad_pro").write_text(json.dumps(_project()))
    write_drc_constraints(board, rules, manufacturer_id="jlcpcb-tier1", layers=4)
    first = board.with_suffix(".kicad_dru").read_bytes()
    assert b'(rule "Clearance - jlcpcb-tier1"\n  (constraint clearance (min 0.15mm)))' in first
    data = json.loads(board.with_suffix(".kicad_pro").read_text())
    assert data["board"]["design_settings"]["rules"]["min_clearance"] == 0.15
    # Idempotent on re-export.
    write_drc_constraints(board, rules, manufacturer_id="jlcpcb-tier1", layers=4)
    assert board.with_suffix(".kicad_dru").read_bytes() == first


# ---------------------------------------------------------------------------
# Regeneration-path guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_id", ["jlcpcb-tier1", "jlcpcb", "pcbway"])
def test_minimal_project_minima_identical_under_default_and_opt_out(profile_id):
    """A freshly generated project has nothing authored to preserve."""
    rules = get_profile(profile_id).get_design_rules(layers=4)
    template = create_minimal_project("fresh.kicad_pro")

    default = merge_project_rules(copy.deepcopy(template), rules)
    opted_out = copy.deepcopy(template)
    opted_out.setdefault("text_variables", {})["KCT_PRESERVE_BOARD_RULES"] = "0"
    merge_project_rules(opted_out, rules)

    assert _minima(default) == _minima(opted_out)
    assert (
        default["board"]["design_settings"]["rule_severities"]
        == opted_out["board"]["design_settings"]["rule_severities"]
    )
    assert generate_project_dru(rules, default) == generate_project_dru(rules, opted_out)


# ---------------------------------------------------------------------------
# Caller override (board 04's reviewed looser process floors)
# ---------------------------------------------------------------------------


def test_caller_override_relaxes_floors_an_earlier_kct_pass_wrote(tmp_path: Path):
    """A reviewed looser process must not be blocked by kct's own stricter write."""
    from dataclasses import replace

    stock = get_profile("jlcpcb-tier1").get_design_rules(layers=2)
    looser = replace(stock, min_via_drill_mm=0.15, min_via_diameter_mm=0.30)
    board = tmp_path / "process.kicad_pcb"
    board.write_text("(kicad_pcb)")
    write_drc_constraints(board, stock, layers=2)

    write_drc_constraints(board, looser, layers=2)
    sticky = json.loads(board.with_suffix(".kicad_pro").read_text())
    assert sticky["board"]["design_settings"]["rules"]["min_via_hole"] == stock.min_via_drill_mm

    write_drc_constraints(board, looser, layers=2, preserve_board_rules="0")
    data = json.loads(board.with_suffix(".kicad_pro").read_text())
    assert data["board"]["design_settings"]["rules"]["min_via_hole"] == 0.15
    assert data["board"]["design_settings"]["rules"]["min_via_diameter"] == 0.30
    # The override is not persisted into the project.
    assert "KCT_PRESERVE_BOARD_RULES" not in data.get("text_variables", {})
    assert "(constraint hole_size (min 0.15mm))" in board.with_suffix(".kicad_dru").read_text()


def test_caller_override_beats_project_text_variable(rules):
    data = merge_project_rules(_project("1"), rules, preserve_board_rules="off")
    assert data["board"]["design_settings"]["rules"]["min_clearance"] == rules.min_clearance_mm
    assert preserve_board_rules_mode(_project("0"), "true") == PRESERVE_MODE_FULL
