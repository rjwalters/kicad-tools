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
    _MICRO_VIA_FLOOR_ANNULAR_MM,
    _MICRO_VIA_FLOOR_DIAMETER_MM,
    _MICRO_VIA_FLOOR_HOLE_MM,
    _NON_BLOCKING_SEVERITIES,
    merge_project_rules,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BOARD_03 = REPO_ROOT / "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb"
BOARD_04 = REPO_ROOT / "boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb"


# ---------------------------------------------------------------------------
# build_project_rules: values come from the profile, not hardcoded
# ---------------------------------------------------------------------------


def test_project_rules_match_profile_minimums():
    profile = get_profile("jlcpcb-tier1")
    rules = profile.get_design_rules(layers=4, copper_oz=1.0)

    pro_rules = build_project_rules(rules)

    assert pro_rules["min_track_width"] == rules.min_trace_width_mm
    assert pro_rules["min_clearance"] == rules.min_clearance_mm
    # #3736: the built-in via *diameter* and *hole* floors stay at the
    # manufacturer STANDARD minimum so kicad-cli's built-in checks catch
    # sub-spec standard vias independently (the #3734 DRU backstop is masked
    # by the solder_mask_margin quirk).  Micro vias are exempted from those
    # two built-in checks via the dedicated min_microvia_* keys.
    assert pro_rules["min_via_diameter"] == rules.min_via_diameter_mm
    assert pro_rules["min_via_hole"] == rules.min_via_drill_mm
    assert pro_rules["min_microvia_diameter"] == _MICRO_VIA_FLOOR_DIAMETER_MM
    assert pro_rules["min_microvia_drill"] == _MICRO_VIA_FLOOR_HOLE_MM
    # annular_width has no micro-via key in KiCad 10.0.1, so its built-in
    # floor must stay at the micro minimum to avoid false positives on
    # legitimate micro vias; standard-via annular is enforced by kct check.
    assert pro_rules["min_via_annular_width"] == _MICRO_VIA_FLOOR_ANNULAR_MM
    assert pro_rules["min_through_hole_diameter"] == rules.min_hole_diameter_mm
    assert pro_rules["min_copper_edge_clearance"] == rules.min_copper_to_edge_mm
    # #3842: min_hole_to_hole maps from the dedicated drill-to-drill spec,
    # NOT the hole-to-edge spec (which is a different rule). For this profile
    # the two differ (hole_to_edge=0.4, hole_to_hole=0.5), so this also
    # guards against the old alias bug.
    assert pro_rules["min_hole_to_hole"] == rules.min_hole_to_hole_mm
    assert rules.min_hole_to_hole_mm != rules.min_hole_to_edge_mm


def test_project_rules_change_with_layer_config():
    """A 4-layer profile selects finer via/drill minimums than 2-layer.

    The built-in ``min_via_*`` floors are now pinned to the micro-via
    process minimum (Issue #3734), so the layer-dependent *standard*
    via/drill floor is reflected in the ``Default`` netclass via size
    instead -- that is what the .kicad_dru "Via Diameter" rule and KiCad's
    via-size DRC enforce for non-micro vias.
    """
    profile = get_profile("jlcpcb-tier1")
    rules_2l = profile.get_design_rules(layers=2, copper_oz=1.0)
    rules_4l = profile.get_design_rules(layers=4, copper_oz=1.0)

    nc_2l = build_default_netclass(rules_2l)
    nc_4l = build_default_netclass(rules_4l)

    # 4-layer JLCPCB allows smaller standard vias/holes than 2-layer.
    assert nc_4l["via_diameter"] < nc_2l["via_diameter"]
    assert nc_4l["via_drill"] < nc_2l["via_drill"]


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


def test_write_drc_constraints_threads_net_classes_to_dru(tmp_path: Path):
    """``net_classes`` reach ``generate_dru`` so ampacity floors match (#4375).

    Before #4375, ``write_drc_constraints`` called ``generate_dru`` WITHOUT
    ``net_classes``, silently dropping the net-scoped ampacity min-width
    rules -- so ``kicad-cli`` would not enforce the ampacity floors
    ``kct check`` evaluated.  The emitted ``.kicad_dru`` must carry the
    net-scoped rules for any class that declares a ``target_ampacity``.
    """
    from kicad_tools.manufacturers.dru_generator import generate_dru
    from kicad_tools.router.rules import NetClassRouting

    board = tmp_path / "demo.kicad_pcb"
    board.write_text("(kicad_pcb)")

    profile = get_profile("jlcpcb-tier1")
    rules = profile.get_design_rules(layers=4, copper_oz=2.0)
    net_classes = [NetClassRouting(name="POWER", target_ampacity=15.0)]

    write_drc_constraints(
        board,
        rules,
        manufacturer_id="jlcpcb-tier1",
        layers=4,
        net_classes=net_classes,
    )

    dru_text = board.with_suffix(".kicad_dru").read_text()
    # The net-scoped ampacity rules must be present...
    assert "Ampacity Min Width (POWER, external)" in dru_text
    assert "Ampacity Min Width (POWER, internal)" in dru_text
    # ...and the emitted text must be byte-identical to a direct
    # generate_dru call with the same net_classes (no drift in the wiring).
    assert dru_text == generate_dru(
        rules, manufacturer_name="jlcpcb-tier1", net_classes=net_classes
    )


def test_write_drc_constraints_without_net_classes_omits_ampacity(tmp_path: Path):
    """Omitting ``net_classes`` preserves the byte-identical board-wide DRU."""
    from kicad_tools.manufacturers.dru_generator import generate_dru

    board = tmp_path / "demo.kicad_pcb"
    board.write_text("(kicad_pcb)")

    rules = get_profile("jlcpcb-tier1").get_design_rules(layers=4)
    write_drc_constraints(board, rules, manufacturer_id="jlcpcb-tier1", layers=4)

    dru_text = board.with_suffix(".kicad_dru").read_text()
    assert "Ampacity Min Width" not in dru_text
    assert dru_text == generate_dru(rules, manufacturer_name="jlcpcb-tier1")


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
    # #3736: built-in via diameter floor stays at the standard 2-layer
    # minimum; micro vias are exempted via min_microvia_diameter.
    assert pro_rules["min_via_diameter"] == rules.min_via_diameter_mm
    assert pro_rules["min_microvia_diameter"] == _MICRO_VIA_FLOOR_DIAMETER_MM
    assert data["net_settings"]["classes"][0]["via_diameter"] == rules.min_via_diameter_mm
    assert data["net_settings"]["classes"][0]["clearance"] == rules.min_clearance_mm


# ---------------------------------------------------------------------------
# Regression (#3736): kicad-cli independently catches sub-spec STANDARD vias
# ---------------------------------------------------------------------------
#
# #3734 lowered the built-in via floors to the micro minimum for ALL vias and
# relied on the A.Via_Type != 'Micro' guarded .kicad_dru rules to gate
# standard vias.  The board-04 judge found those DRU rules are silently
# suppressed by the unconditional solder_mask_margin rule under kicad-cli
# 10.0.1, so a sub-spec STANDARD via passed kicad-cli silently.  #3736 keeps
# the built-in min_via_diameter / min_via_hole at the standard floor (built-in
# checks are NOT masked) while exempting micro vias via min_microvia_*.


def _kicad_cli() -> Path | None:
    from kicad_tools.cli.runner import find_kicad_cli

    return find_kicad_cli()


def _run_drc(cli: Path, pcb: Path, report: Path) -> str:
    import subprocess

    subprocess.run(
        [
            str(cli),
            "pcb",
            "drc",
            "--severity-error",
            str(pcb),
            "-o",
            str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return report.read_text()


@pytest.mark.skipif(not BOARD_04.exists(), reason="board 04 routed PCB not available")
@pytest.mark.skipif(_kicad_cli() is None, reason="kicad-cli not installed")
def test_subspec_standard_via_fires_under_kicad_cli(tmp_path: Path):
    """A sub-spec STANDARD via must fire via_diameter under kicad-cli on the
    full emitted ruleset (regression for #3736)."""
    cli = _kicad_cli()
    assert cli is not None

    pcb = tmp_path / "board.kicad_pcb"
    text = BOARD_04.read_text()

    # Shrink one legitimate standard 0.6 mm / 0.3 mm via to a sub-spec
    # 0.4 mm diameter (0.05 mm annular) -- below the jlcpcb-tier1 standard
    # 0.6 mm diameter / 0.15 mm annular floors.  This is NOT a micro via, so
    # it must be caught by the built-in standard-via floor.
    #
    # The via sits at board origin + (16.5, 17.15); derive the absolute
    # position from the same ``centered_origin(60, 40)`` helper board 04's
    # generator uses so this pin cannot go stale against a sheet-position
    # change (it was previously hardcoded to the historical (100, 100)
    # origin, i.e. ``(at 116.5 117.15)`` -- PR #4015 judge feedback).
    from kicad_tools.pcb.center_sheet import centered_origin

    def _fmt(v: float) -> str:
        # KiCad's minimal decimal formatting ("135" / "84.65").
        return f"{v:.6f}".rstrip("0").rstrip(".")

    ox, oy = centered_origin(60.0, 40.0)  # board 04 outline origin
    via_at = f"(at {_fmt(ox + 16.5)} {_fmt(oy + 17.15)})"
    needle = f"{via_at}\n\t\t(size 0.6)\n\t\t(drill 0.3)"
    replacement = f"{via_at}\n\t\t(size 0.4)\n\t\t(drill 0.3)"
    assert needle in text, f"expected standard via at {via_at} in board 04"
    pcb.write_text(text.replace(needle, replacement, 1))

    profile = get_profile("jlcpcb-tier1")
    rules = profile.get_design_rules(layers=4, copper_oz=1.0)
    write_drc_constraints(pcb, rules, manufacturer_id="jlcpcb-tier1", layers=4)

    report = _run_drc(cli, pcb, tmp_path / "drc.rpt")

    # The sub-spec standard via must be flagged on the FULL ruleset (which
    # includes the solder_mask_margin rule that masks the custom DRU via
    # rules).  Before #3736 this reported 0 via_diameter violations.
    assert "via_diameter" in report, (
        "sub-spec STANDARD via was not flagged by kicad-cli on the full "
        f"ruleset -- #3736 regression. Report:\n{report}"
    )


@pytest.mark.skipif(not BOARD_04.exists(), reason="board 04 routed PCB not available")
@pytest.mark.skipif(_kicad_cli() is None, reason="kicad-cli not installed")
def test_board_04_micro_vias_stay_clean_under_kicad_cli(tmp_path: Path):
    """Board 04's legitimate micro vias must stay exempt: 0 kicad-cli errors
    on the full emitted ruleset (the micro-via exemption survives #3736)."""
    cli = _kicad_cli()
    assert cli is not None

    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(BOARD_04.read_text())

    profile = get_profile("jlcpcb-tier1")
    rules = profile.get_design_rules(layers=4, copper_oz=1.0)
    write_drc_constraints(pcb, rules, manufacturer_id="jlcpcb-tier1", layers=4)

    report = _run_drc(cli, pcb, tmp_path / "drc.rpt")

    # Micro vias must not trip via_diameter / annular_width.
    assert "via_diameter" not in report, f"micro via flagged via_diameter:\n{report}"
    assert "annular_width" not in report, f"micro via flagged annular_width:\n{report}"


# ---------------------------------------------------------------------------
# Issue #3919: the route pipeline (not just export) must emit the sibling
# .kicad_pro / .kicad_dru constraint sidecars, so every downstream kicad-cli
# invocation judges with the intended manufacturer floors.
# ---------------------------------------------------------------------------


def test_route_emits_sibling_kicad_pro(monkeypatch, tmp_path: Path):
    """``run_post_route_drc`` writes a sibling .kicad_pro with the profile floors.

    Mirrors the export-side emission but through the route entry point.  The
    internal DRCChecker and geometric DRC are mocked so no real board or
    kicad-cli is required; the assertion is purely on the emitted sidecar.
    """
    import json

    import kicad_tools.drc as drc_mod
    import kicad_tools.validate as validate_mod
    from kicad_tools.cli import route_cmd
    from kicad_tools.drc.geometric import GeometricDRCResult
    from kicad_tools.schema import pcb as pcb_mod

    # Mock the internal engine + geometric DRC so the function focuses on
    # sidecar emission (no real PCB parse, no kicad-cli).
    monkeypatch.setattr(pcb_mod.PCB, "load", classmethod(lambda cls, p: object()))

    class _Results:
        error_count = 0
        warning_count = 0

    class _Checker:
        def __init__(self, *a, **k):
            pass

        def check_all(self, *a, **k):
            return _Results()

    pro_exists_at_drc = {}

    def _geo(output_path, *a, **k):
        pro_exists_at_drc["ok"] = (output_path.parent / "board.kicad_pro").exists()
        return GeometricDRCResult(ran=True, error_count=0)

    monkeypatch.setattr(validate_mod, "DRCChecker", _Checker)
    monkeypatch.setattr(drc_mod, "run_geometric_drc", _geo)

    out = tmp_path / "board.kicad_pcb"
    route_cmd.run_post_route_drc(output_path=out, manufacturer="jlcpcb-tier1", layers=4)

    pro = tmp_path / "board.kicad_pro"
    assert pro.exists()
    # ordering: the .kicad_pro existed before kicad-cli DRC ran.
    assert pro_exists_at_drc.get("ok") is True

    profile = get_profile("jlcpcb-tier1")
    rules = profile.get_design_rules(layers=4, copper_oz=1.0)
    data = json.loads(pro.read_text())
    defaults = data["board"]["design_settings"]["defaults"]
    assert defaults["clearance_min"] == rules.min_clearance_mm
    assert defaults["track_min_width"] == rules.min_trace_width_mm
