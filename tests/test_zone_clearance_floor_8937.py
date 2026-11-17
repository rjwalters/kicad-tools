"""Issue #8937: the fab-floor ``.kicad_dru`` block must not lower zone clearance.

The unconditional ``Clearance`` custom rule in the managed block *replaces*
(KiCad custom rules are not floors) the clearance a zone authorises with its
own ``connect_pads (clearance …)``, so every ``kicad-cli pcb drc
--refill-zones`` filled pours at the fab minimum -- measured on a real
4-layer/1 oz JLCPCB board as a 0.1021 mm pour-to-pad gap where the design
authorised 0.3 mm.  The fix restores each zone's authored clearance with a
zone-scoped rule emitted after the floor, at ``max(fab floor, authored)``.

These tests pin:

* **the refill itself** (AC1): a fixture board with a zone authored at
  0.3 mm and the JLCPCB floors refills, on the real kicad-cli, with a
  pour-to-foreign-pad gap of at least 0.3 mm -- measured on the saved
  board's ``filled_polygon``, exactly as the issue measured the defect.
  Measured on the pinned toolchain (kicad-cli 10.0.6, ``kicad/kicad:10.0``):
  the zone-scoped condition is honoured by ``--refill-zones`` (not merely by
  DRC), the refill gap was 0.1021 mm with the floor-only block and 0.3005 mm
  with the zone arm, and the ``A.Type`` spelling is case-insensitive
  ('Zone'/'zone'/'ZONE' all match).
* **the max() arm** (AC1b): a zone authored *below* the fab floor still
  resolves to the fab floor -- the fix restores authored clearances without
  exempting zones from the floor (the issue's Option 2 would have).
* **the negative control** (AC2): a fixture with net-class clearance 0.2 mm
  and two tracks 0.15 mm apart FAILS the kicad-cli cross-gate with the
  managed block present.  A cross-gate that cannot fail proves nothing.
* **the emission** (unit level): the zone rules join the reviewed net-class
  rules in one ascending-clearance emission after the floor; the
  ``KCT_PRESERVE_BOARD_RULES`` modes; zones without an explicit clearance;
  the template-Default relaxation (the case-2 candidate) keeps the zone arm.
* **the class arm's per-layer floor** (#9015): a netclass authored between
  the 2 oz profile's split floors is emitted scoped ``(layer inner)`` at
  the authored value instead of being skipped against the outer floor;
  unsplit profiles keep the single-floor, byte-identical emission.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from shapely.geometry import Point, Polygon

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.manufacturers import get_profile, write_drc_constraints
from kicad_tools.manufacturers.project_generator import generate_project_dru
from kicad_tools.manufacturers.zone_clearance_dru import (
    zone_clearance_rule,
    zone_clearance_targets,
)
from kicad_tools.sexp import parse_file

_HEADER = """\
(kicad_pcb (version 20240108) (generator pcbnew)
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user)
   (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "GND") (net 2 "A")
  (gr_rect (start 0 0) (end 20 20) (stroke (width .1) (type default))
   (fill none) (layer "Edge.Cuts"))
"""


def _fixture(path: Path, zone_clearance: float | None) -> Path:
    """A board with one GND pour and two THT pads: GND (connected) and A.

    The A pad (1.7 mm circle at (10, 10)) is foreign copper inside the pour;
    the GND pad at (4, 4) keeps the fill connected so ``isolated_copper``
    does not add noise to the DRC report.
    """
    connect = f"(connect_pads (clearance {zone_clearance}))\n" if zone_clearance is not None else ""
    path.write_text(
        _HEADER
        + """\
  (footprint "TG" (layer "F.Cu") (at 4 4)
    (fp_text reference "U1" (at 0 -2.5) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0)
      (layers "*.Cu" "*.Mask") (net 1 "GND")))
  (footprint "TA" (layer "F.Cu") (at 10 10)
    (fp_text reference "U2" (at 0 -2.5) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0)
      (layers "*.Cu" "*.Mask") (net 2 "A")))
"""
        + f"""  (zone (net 1) (net_name "GND") (layer "F.Cu") (hatch edge 0.5)
    (priority 0)
    {connect}(min_thickness 0.25)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 2 2) (xy 18 2) (xy 18 18) (xy 2 18)))))
"""
    )
    return path


def _tracks_fixture(path: Path, gap: float = 0.15) -> Path:
    """Two different-net tracks ``gap`` mm apart, edge to edge (AC2)."""
    y2 = 10 + 0.2 + gap
    path.write_text(
        _HEADER + '  (segment (start 5 10) (end 15 10) (width 0.2) (layer "F.Cu")'
        ' (net 1) (uuid "s1"))\n'
        + f'  (segment (start 5 {y2}) (end 15 {y2}) (width 0.2) (layer "F.Cu")'
        ' (net 2) (uuid "s2"))\n' + ")\n"
    )
    return path


def _project(default_clearance: float = 0.2) -> dict:
    return {
        "meta": {"filename": "probe.kicad_pro", "version": 1},
        "board": {"design_settings": {"rules": {}}},
        "net_settings": {
            "classes": [
                {
                    "name": "Default",
                    "clearance": default_clearance,
                    "track_width": 0.2,
                    # Not a TEMPLATE_DEFAULT_NETCLASS_SIGNATURES entry, so
                    # the class reads as designer-authored and the merge
                    # keeps its clearance (a template-signature Default is
                    # relaxed to the profile by the documented #6191 policy,
                    # which is the curation's case-2 candidate, tested
                    # separately below).
                    "via_diameter": 0.7,
                    "via_drill": 0.3,
                }
            ]
        },
    }


def _write_sidecars(board: Path, project: dict) -> None:
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1.0)
    board.with_suffix(".kicad_pro").write_text(json.dumps(project))
    write_drc_constraints(board, rules, manufacturer_id="jlcpcb", layers=4)


def _pour_to_pad_gap(board: Path) -> float:
    """Minimum distance from the saved pour edge to the 1.7 mm A pad disk."""
    doc = parse_file(board)
    pad_disk = Point(10, 10).buffer(0.85, quad_segs=128)
    gaps = []
    for zone in doc.find_all("zone"):
        for poly in zone.find_all("filled_polygon"):
            pts = [
                (float(xy.values[0]), float(xy.values[1]))
                for pts in poly.find_all("pts")
                for xy in pts.find_all("xy")
            ]
            if len(pts) >= 3:
                gaps.append(Polygon(pts).distance(pad_disk))
    assert gaps, "no filled polygon on the saved board"
    return min(gaps)


def _refill(board: Path) -> dict:
    report = board.with_suffix(".kicad_dru_report.json")
    proc = subprocess.run(
        [
            str(find_kicad_cli()),
            "pcb",
            "drc",
            "--severity-all",
            "--refill-zones",
            "--save-board",
            "--format",
            "json",
            "-o",
            str(report),
            str(board),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode in (0, 1), proc.stderr
    return json.loads(report.read_text())


# ---------------------------------------------------------------------------
# AC1 / AC1b: the refill itself, on the real kicad-cli
# ---------------------------------------------------------------------------


@pytest.mark.skipif(find_kicad_cli() is None, reason="Native KiCad CLI is not installed")
def test_refill_keeps_authored_zone_clearance(tmp_path) -> None:
    board = _fixture(tmp_path / "probe.kicad_pcb", zone_clearance=0.3)
    _write_sidecars(board, _project())
    dru = board.with_suffix(".kicad_dru").read_text()
    assert "Zone clearance - GND (F.Cu)" in dru
    _refill(board)
    gap = _pour_to_pad_gap(board)
    assert gap >= 0.3 - 0.005, gap


@pytest.mark.skipif(find_kicad_cli() is None, reason="Native KiCad CLI is not installed")
def test_refill_floors_a_zone_authored_below_the_fab_minimum(tmp_path) -> None:
    """The max() arm: a 0.05 mm zone still refills at the 0.1016 mm floor."""
    board = _fixture(tmp_path / "probe.kicad_pcb", zone_clearance=0.05)
    # A sub-floor authored class, so no reviewed rule masks the floor this
    # test isolates (with a 0.2 class the pour would rightly keep 0.2).
    _write_sidecars(board, _project(default_clearance=0.1))
    dru = board.with_suffix(".kicad_dru").read_text()
    # max(0.1016, 0.05) == the floor -> the zone rule is redundant and not
    # emitted; the unconditional floor governs the refill.
    assert "Zone clearance" not in dru
    _refill(board)
    gap = _pour_to_pad_gap(board)
    assert 0.09 <= gap <= 0.11, gap


@pytest.mark.skipif(find_kicad_cli() is None, reason="Native KiCad CLI is not installed")
def test_refill_without_the_zone_arm_reproduces_the_defect(tmp_path) -> None:
    """The regression the issue measured: no zone arm -> 0.10 mm pours.

    The zone arm is what restores the authored clearance; emitting the plain
    block (as ``KCT_PRESERVE_BOARD_RULES=0`` deliberately does) reproduces
    the defect, which is why that mode is an explicit opt-in.
    """
    board = _fixture(tmp_path / "probe.kicad_pcb", zone_clearance=0.3)
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1.0)
    project = _project()
    board.with_suffix(".kicad_pro").write_text(json.dumps(project))
    write_drc_constraints(
        board, rules, manufacturer_id="jlcpcb", layers=4, preserve_board_rules="0"
    )
    dru = board.with_suffix(".kicad_dru").read_text()
    assert "Zone clearance" not in dru
    assert "Reviewed clearance" not in dru
    _refill(board)
    gap = _pour_to_pad_gap(board)
    assert gap <= 0.11, gap


# ---------------------------------------------------------------------------
# AC2: the cross-gate must be able to fail
# ---------------------------------------------------------------------------


@pytest.mark.skipif(find_kicad_cli() is None, reason="Native KiCad CLI is not installed")
def test_netclass_clearance_cross_gate_fails_on_close_tracks(tmp_path) -> None:
    board = _tracks_fixture(tmp_path / "probe.kicad_pcb", gap=0.15)
    _write_sidecars(board, _project(default_clearance=0.2))
    dru = board.with_suffix(".kicad_dru").read_text()
    assert "Reviewed clearance - Default" in dru
    report = _refill(board)
    descriptions = [v["description"] for v in report["violations"]]
    assert any("Reviewed clearance" in d for d in descriptions), descriptions


# ---------------------------------------------------------------------------
# Emission shape (unit level, no kicad-cli)
# ---------------------------------------------------------------------------


def _emitted(project: dict, zone_clearances, copper_oz: float = 1.0) -> str:
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=copper_oz)
    return generate_project_dru(
        rules, project, manufacturer_id="jlcpcb", zone_clearances=zone_clearances
    )


def test_zone_rules_join_the_ascending_emission() -> None:
    text = _emitted(
        _project(),
        [("F.Cu", "GND", 0.3), ("B.Cu", "GND", 0.3), ("In1.Cu", "PWR", 0.05)],
    )
    # In1.Cu PWR at max(0.1016, 0.05) == the floor: redundant, not emitted.
    assert "Zone clearance - GND (F.Cu)" in text
    assert "Zone clearance - GND (B.Cu)" in text
    assert "PWR" not in text
    # Ascending order with the reviewed class rules: the 0.2 class rule
    # precedes the 0.3 zone rules, so the stricter matching rule always wins.
    reviewed = text.index("Reviewed clearance - Default")
    zone_f = text.index("Zone clearance - GND (F.Cu)")
    assert reviewed < zone_f
    # The condition uses the established native-zone spelling, both orders.
    assert (
        "(A.Type == 'Zone' && A.NetName == 'GND') || "
        "(B.Type == 'Zone' && B.NetName == 'GND')" in text
    )


def test_split_floor_profile_emits_inner_zone_rules_between_the_floors() -> None:
    """The 4-layer 2 oz profile splits its floors (Issue #8836): outer
    0.15, inner 0.1016.  The same authored 0.12 mm is *between* the two:
    on the inner layer the inner floor governs, so the authored value must
    be restored; on the outer layer 0.12 is below the outer floor, which
    still binds it -- the review's measured gap had the outer floor decide
    both, skipping the inner rule and refilling In1.Cu at 0.1016."""
    text = _emitted(_project(), [("F.Cu", "GND", 0.12), ("In1.Cu", "GND", 0.12)], copper_oz=2.0)
    assert "Zone clearance - GND (In1.Cu)" in text
    assert "(min 0.12mm)" in text
    assert "Zone clearance - GND (F.Cu)" not in text


def test_split_floor_profile_still_skips_zones_at_or_below_their_own_floor() -> None:
    """Redundant means at-or-below the floor of the zone's *own* layer:
    0.08 is below the inner floor, 0.12 on F.Cu is below the outer floor,
    and the unsplit 1 oz profile keeps its single floor for every layer."""
    split = _emitted(_project(), [("In1.Cu", "PWR", 0.08), ("F.Cu", "PWR", 0.12)], copper_oz=2.0)
    assert "PWR" not in split
    unsplit = _emitted(_project(), [("In1.Cu", "PWR", 0.05)])
    assert "PWR" not in unsplit


def test_split_floor_emission_passes_the_fail_closed_gate() -> None:
    """The review's end-to-end reproduction as a regression pin: with the
    outer-floor-only arm the In1.Cu zone rule was skipped *and* the gate
    passed (the two defects cancelled into a green suite).  With the
    per-layer floor the rule is emitted and the authored baselines hold,
    while the same profile emitted without the zone arm (mode 0's plain
    block) resolves In1.Cu at the 0.1016 inner floor -- refused."""
    from kicad_tools.core.rule_loosening import RuleLooseningError
    from kicad_tools.manufacturers.dru_generator import merge_dru_floors

    authored = {"netclasses": {"Default": 0.2}, "zones": [["In1.Cu", "GND", 0.12]]}
    project = _project()
    text = _emitted(project, [("In1.Cu", "GND", 0.12)], copper_oz=2.0)
    assert "Zone clearance - GND (In1.Cu)" in text
    assert "Zone clearance - GND (In1.Cu)" in merge_dru_floors(None, text, authored=authored)

    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=2.0)
    plain = generate_project_dru(
        rules,
        project,
        manufacturer_id="jlcpcb",
        zone_clearances=[("In1.Cu", "GND", 0.12)],
        preserve_board_rules="0",
    )
    assert "Zone clearance" not in plain
    with pytest.raises(RuleLooseningError) as excinfo:
        merge_dru_floors(None, plain, authored=authored)
    assert "dru.resolved.pad_zone.In1.Cu.GND" in {item.path for item in excinfo.value.loosenings}


# ---------------------------------------------------------------------------
# Issue #9015: the class arm's skip predicate is per layer too
# ---------------------------------------------------------------------------

_REVIEWED_INNER = (
    '\n(rule "Reviewed clearance - Default"\n'
    "  (layer inner)\n"
    "  (condition \"A.NetClass == 'Default' || B.NetClass == 'Default'\")\n"
    "  (constraint clearance (min {value}mm)))\n"
)
_REVIEWED_GLOBAL = (
    '\n(rule "Reviewed clearance - Default"\n'
    "  (condition \"A.NetClass == 'Default' || B.NetClass == 'Default'\")\n"
    "  (constraint clearance (min {value}mm)))\n"
)


def test_split_floor_profile_emits_inner_scoped_class_rules_between_the_floors() -> None:
    """The class analog of the zone arm's between-the-floors case: a 0.12 mm
    Default on the 2 oz profile (outer 0.15, inner 0.1016) used to be skipped
    as redundant against the outer floor, leaving the class's inner-layer
    pairs at the 0.1016 floor; the rule is now scoped ``(layer inner)`` at
    the authored value, while the outer floor keeps binding outer pairs."""
    text = _emitted(_project(default_clearance=0.12), zone_clearances=None, copper_oz=2.0)
    assert _REVIEWED_INNER.format(value=0.12) in text
    assert _REVIEWED_GLOBAL.format(value=0.12) not in text
    # The split floors themselves are unchanged around the scoped rule.
    assert '(rule "Clearance (outer)' in text
    assert '(rule "Clearance (inner)' in text


def test_split_floor_profile_still_skips_classes_at_or_below_the_inner_floor() -> None:
    """Redundant means at-or-below the floor of the layers the rule would
    govern: nothing is emitted for a class the inner floor already binds."""
    at = _emitted(_project(default_clearance=0.1016), zone_clearances=None, copper_oz=2.0)
    below = _emitted(_project(default_clearance=0.08), zone_clearances=None, copper_oz=2.0)
    assert "Reviewed clearance" not in at
    assert "Reviewed clearance" not in below


def test_split_floor_class_at_the_outer_floor_binds_inner_layers_at_it() -> None:
    """Edge: authored exactly at the outer floor.  The rule is redundant on
    the outer layers, but the authored 0.15 still exceeds the 0.1016 inner
    floor, so it is emitted inner-scoped at 0.15 (the native per-layer max)
    where the outer-floor-only predicate used to skip it entirely."""
    text = _emitted(_project(default_clearance=0.15), zone_clearances=None, copper_oz=2.0)
    assert _REVIEWED_INNER.format(value=0.15) in text


def test_above_floor_class_keeps_the_global_rule() -> None:
    """A class above both floors keeps today's single unscoped rule: the
    per-layer max(per-layer floor, authored) is the authored value on every
    layer, so no layer scoping is needed."""
    text = _emitted(_project(default_clearance=0.2), zone_clearances=None, copper_oz=2.0)
    assert _REVIEWED_GLOBAL.format(value=0.2) in text


def test_full_mode_emits_between_floor_classes_globally_like_before() -> None:
    """Mode 1 keeps #5023's one-global-rule-per-class shape: a between-the-
    floors class rides the outer floor at ``max(outer, authored)``, never
    weaker than the stricter mode's per-layer emission."""
    project = _project(default_clearance=0.12)
    project.setdefault("text_variables", {})["KCT_PRESERVE_BOARD_RULES"] = "1"
    text = _emitted(project, zone_clearances=None, copper_oz=2.0)
    assert _REVIEWED_GLOBAL.format(value=0.15) in text


def test_unsplit_profile_class_arm_is_byte_identical_with_an_equal_inner_floor() -> None:
    """``min_inner_clearance_mm=None`` keeps the single floor (AC: unsplit
    profiles are byte-identical): pinning the inner floor equal to the
    outer one changes nothing, and the reviewed rule stays unscoped."""
    from dataclasses import replace

    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1.0)
    assert rules.min_inner_clearance_mm is None
    pinned = replace(rules, min_inner_clearance_mm=rules.min_clearance_mm)
    project = _project(default_clearance=0.2)
    single = generate_project_dru(rules, project, manufacturer_id="jlcpcb")
    assert single == generate_project_dru(pinned, project, manufacturer_id="jlcpcb")
    assert _REVIEWED_GLOBAL.format(value=0.2) in single


def test_mode_off_emits_no_reviewed_class_rules_on_the_split_profile() -> None:
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=2.0)
    plain = generate_project_dru(
        rules,
        _project(default_clearance=0.12),
        manufacturer_id="jlcpcb",
        zone_clearances=None,
        preserve_board_rules="0",
    )
    assert "Reviewed clearance" not in plain


def test_split_floor_class_arm_passes_the_fail_closed_gate() -> None:
    """The class analog of the zone arm's regression pin: with the
    outer-floor-only predicate the between-the-floors rule was dropped and
    the gate stayed green (the protection silently gone); with the per-layer
    floor the emission holds the authored baselines, while the plain
    mode-0 block resolves In1.Cu Default pairs at the 0.1016 inner floor --
    refused."""
    from kicad_tools.core.rule_loosening import RuleLooseningError
    from kicad_tools.manufacturers.dru_generator import merge_dru_floors

    authored = {"netclasses": {"Default": 0.12}, "zones": [["In1.Cu", "GND", 0.12]]}
    project = _project(default_clearance=0.12)
    text = _emitted(project, [("In1.Cu", "GND", 0.12)], copper_oz=2.0)
    assert "Reviewed clearance - Default" in text
    assert "Reviewed clearance - Default" in merge_dru_floors(None, text, authored=authored)

    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=2.0)
    plain = generate_project_dru(
        rules,
        project,
        manufacturer_id="jlcpcb",
        zone_clearances=[("In1.Cu", "GND", 0.12)],
        preserve_board_rules="0",
    )
    assert "Reviewed clearance" not in plain
    with pytest.raises(RuleLooseningError) as excinfo:
        merge_dru_floors(None, plain, authored=authored)
    assert "dru.resolved.track_track" in {item.path for item in excinfo.value.loosenings}


def test_zone_reader_skips_zones_without_an_authored_clearance(tmp_path) -> None:
    board = _fixture(tmp_path / "probe.kicad_pcb", zone_clearance=None)
    assert zone_clearance_targets(board) == []
    # And the emission is byte-identical to the arm-less block.
    with_arm = _emitted(_project(), zone_clearance_targets(board))
    without = _emitted(_project(), zone_clearances=None)
    assert with_arm == without


def test_zone_reader_takes_the_strictest_zone_per_layer_net(tmp_path) -> None:
    board = _fixture(tmp_path / "probe.kicad_pcb", zone_clearance=0.3)
    text = board.read_text()
    second = text.replace(
        "(polygon (pts (xy 2 2) (xy 18 2) (xy 18 18) (xy 2 18)))",
        "(polygon (pts (xy 2 2) (xy 18 2) (xy 18 18) (xy 2 18)))\n"
        '  (zone (net 1) (net_name "GND") (layer "F.Cu") (hatch edge 0.5)\n'
        "    (connect_pads (clearance 0.15))\n"
        "    (min_thickness 0.25)\n"
        "    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))\n"
        "    (polygon (pts (xy 6 6) (xy 8 6) (xy 8 8) (xy 6 8))))",
    )
    assert second != text
    board.write_text(second)
    assert zone_clearance_targets(board) == [("F.Cu", "GND", 0.3)]


def test_template_default_relaxation_keeps_the_zone_arm() -> None:
    """Case 2 from the curation: a template Default is relaxed to the
    profile, which drops its reviewed rule -- the zone arm must survive,
    because it reads the board, not the project."""
    project = _project(default_clearance=0.2)
    # (0.2, 0.2, 0.6, 0.3) is a TEMPLATE_DEFAULT_NETCLASS_SIGNATURES entry.
    from kicad_tools.manufacturers.project_generator import (
        TEMPLATE_DEFAULT_NETCLASS_SIGNATURES,
    )

    assert (0.2, 0.2, 0.6, 0.3) in TEMPLATE_DEFAULT_NETCLASS_SIGNATURES
    text = _emitted(project, [("F.Cu", "GND", 0.3)])
    assert "Zone clearance - GND (F.Cu)" in text


def test_full_mode_emits_zone_rules_like_class_rules() -> None:
    project = _project()
    project.setdefault("text_variables", {})["KCT_PRESERVE_BOARD_RULES"] = "1"
    text = _emitted(project, [("F.Cu", "GND", 0.05)])
    # max(0.1016, 0.05) -> the floor value, still emitted under mode 1
    # exactly as every class rule is.
    zone_rule = zone_clearance_rule("F.Cu", "GND", 0.1016)
    assert zone_rule in text


def test_zone_rule_grammar() -> None:
    assert zone_clearance_rule("F.Cu", "GND", 0.3) == (
        '(rule "Zone clearance - GND (F.Cu)"\n'
        '  (layer "F.Cu")\n'
        "  (condition \"(A.Type == 'Zone' && A.NetName == 'GND') || "
        "(B.Type == 'Zone' && B.NetName == 'GND')\")\n"
        "  (constraint clearance (min 0.3mm)))\n"
    )
