"""Independent native material witnesses for the complete exposure checker.

Set KCT_MASK_NATIVE_PYTHON to a JSON argv for KiCad's Python interpreter.
The selected native CLI and Python must both see the scratch paths unchanged.
"""

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point, Polygon

from kicad_tools.validate.mask_copper import (
    MaskCopperPolicy,
    MaskEscapeIntent,
    check_mask_to_copper,
)
from tests._mask_gerbonara_oracle import read_native_gerber
from tests.test_mask_export_geometry import NATIVE_CASES, _board

POLICY = MaskCopperPolicy(0.1, "fixture process specification", "test process", "rev1")


@pytest.fixture
def native_options(tmp_path):
    from kicad_tools.cli.runner import find_kicad_cli

    if not os.environ.get("KCT_MASK_NATIVE_PYTHON") or find_kicad_cli() is None:
        pytest.skip("KiCad CLI and explicit native Python required")
    pytest.importorskip("gerbonara", minversion="1.6.3")
    return {
        "native_python_command": json.loads(os.environ["KCT_MASK_NATIVE_PYTHON"]),
        "artifact_dir": tmp_path / "native",
        "scratch_dir": tmp_path,
    }


# A factory-style rule file, the shape a fabricator ships with an order: a
# handful of silkscreen clearance rules alongside the plain copper clearance
# that native attribution has always accepted. Nothing here changes plotted
# mask or copper geometry, so attribution must be identical to the same board
# without the sidecar (issue #5818).
FACTORY_SILK_DRU = """(version 1)
(rule "Factory silk to pads"
  (condition "A.Type == 'Pad'")
  (constraint silk_clearance (min 0.15mm)))
(rule "Factory silk to holes"
  (condition "A.Type == 'Via'")
  (constraint silk_clearance (min 0.2mm)))
(rule "Factory silk to footprint courtyards"
  (condition "A.Type == 'Footprint'")
  (constraint silk_clearance (min 0.1mm)))
(rule "Factory copper clearance"
  (constraint clearance (min 0.15mm)))
"""

# The kct-managed fab-floor vocabulary (#8948): ``physical_hole_clearance``,
# ``text_thickness`` and ``text_height``, the constraints every shipped tier
# file and the managed ``.kicad_dru`` block carry on top of the silk rules
# above. Shaped like the tier files themselves, and with
# ``physical_hole_clearance`` deliberately appearing in two rules to pin that
# a repeated constraint name yields no duplicated report entry.
FACTORY_FAB_FLOOR_DRU = """(version 1)
(rule "Factory hole to edge"
  (condition "(A.Type == 'via' || A.Type == 'pad') && B.Layer == 'Edge.Cuts'")
  (constraint physical_hole_clearance (min 0.4mm)))
(rule "Factory via hole to edge"
  (condition "A.Type == 'via' && B.Layer == 'Edge.Cuts'")
  (constraint physical_hole_clearance (min 0.5mm)))
(rule "Factory silkscreen width"
  (condition "A.Type == 'text' && (A.Layer == 'F.Silkscreen' || A.Layer == 'B.Silkscreen')")
  (constraint text_thickness (min 0.15mm)))
(rule "Factory silkscreen height"
  (condition "A.Type == 'text' && (A.Layer == 'F.Silkscreen' || A.Layer == 'B.Silkscreen')")
  (constraint text_height (min 1.0mm)))
"""

# Constraint names the allowlist admits for factory and kct-managed rule
# files; the parity tests must show none of them ever reaches the report.
ADMITTED_FACTORY_CONSTRAINTS = (
    "silk_clearance",
    "physical_hole_clearance",
    "text_thickness",
    "text_height",
    "diff_pair_gap",
    "diff_pair_uncoupled",
    "skew",
    "via_count",
    "disallow",
)

# The kct-managed router diff-pair vocabulary (#6443, admitted by #8973):
# ``diff_pair_gap`` (one rule per coupled pair) plus the pair limits
# ``diff_pair_uncoupled``, ``skew`` and ``via_count``, shaped on what
# netclass_dru.py actually writes. A pair KiCad recognises by suffix
# (CLK_P/CLK_N) is selected with ``A.inDiffPair`` and gets all three limits,
# ``skew`` carrying ``(within_diff_pairs)``; a router-recorded pair no KiCad
# suffix recognises (X_POS/X_NEG, #6429) is selected by net name and gets
# only the bare ``skew`` and ``via_count`` -- KiCad never evaluates
# ``diff_pair_gap`` or ``diff_pair_uncoupled`` for it, so neither is written.
# ``skew`` and ``via_count`` each appear in two rules, pinning that a
# repeated constraint name yields no duplicated report entry.
FACTORY_PAIR_LIMIT_DRU = """(version 1)
(rule "kct diff pair gap - CLK_P/CLK_N"
  (condition "A.NetName == 'CLK_P' || A.NetName == 'CLK_N'")
  (constraint diff_pair_gap (min 0.2mm)))
(rule "kct diff pair uncoupled - CLK_P/CLK_N"
  (condition "A.inDiffPair('CLK_')")
  (constraint diff_pair_uncoupled (max 5mm)))
(rule "kct diff pair skew - CLK_P/CLK_N"
  (condition "A.inDiffPair('CLK_')")
  (constraint skew (max 0.5mm) (within_diff_pairs)))
(rule "kct diff pair vias - CLK_P/CLK_N"
  (condition "A.inDiffPair('CLK_')")
  (constraint via_count (max 4)))
(rule "kct diff pair skew - X_POS/X_NEG"
  (condition "A.NetName == 'X_POS' || A.NetName == 'X_NEG'")
  (constraint skew (max 1mm)))
(rule "kct diff pair vias - X_POS/X_NEG"
  (condition "A.NetName == 'X_POS' || A.NetName == 'X_NEG'")
  (constraint via_count (max 6)))
"""

# The kct-managed keepout vocabulary (#6039, admitted by #8973): one
# ``disallow`` rule per rule area, shaped on keepout_dru.py's own rendering,
# carrying both the two-item item list tests/test_keepout_dru.py pins and
# the five-item wide form -- and the explicit ``(layer ...)`` clause a
# single-copper-layer area earns.
FACTORY_KEEPOUT_DRU = """(version 1)
# BEGIN kct keepout rules (Issue #6039) -- managed, do not edit
# Generated from the board's keepout rule areas, board-level (Issue #6039)
# and footprint-embedded (Issue #6087).
# kicad-cli 10.0.1 does not enforce rule areas on its own; these
# explicit intersectsArea rules make `kicad-cli pcb drc` report them.
(rule "kct keepout wall [00000000-0000-0000-0000-000000000004]"
  (condition "A.intersectsArea('00000000-0000-0000-0000-000000000004')")
  (constraint disallow track via))
(rule "kct keepout fence [00000000-0000-0000-0000-000000000005]"
  (layer "F.Cu")
  (condition "A.intersectsArea('00000000-0000-0000-0000-000000000005')")
  (constraint disallow track via pad zone footprint))
# END kct keepout rules
"""

# Two pads and an escaping conductor: enough plotted mask and copper for the
# per-object/full-layer parity comparison to be meaningful, small enough to
# export twice without inflating the native gate's runtime.
PARITY_BODY = """(net 1 "N")
      (footprint "P" (layer "F.Cu") (at 10 10)
       (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "N"))
       (pad "2" smd roundrect (at 2 0) (size 1 1) (roundrect_rratio .25) (layers "F.Cu" "F.Mask")))
      (segment (start 10.9 9) (end 10.9 11) (width .1) (layer "F.Cu") (net 1)
       (uuid "00000000-0000-0000-0000-000000000099"))"""

# PARITY_BODY plus the two features whose managed blocks the pair-limit and
# keepout fixtures mirror (#8973): a KiCad-recognised diff pair
# (CLK_P/CLK_N) beside a router-recorded one (X_POS/X_NEG, #6429), and two
# keepout rule areas whose flags forbid a short (track via) and a wide
# (track via pad zone footprint) item list. Auto-assigned source UUIDs
# follow document order -- footprint 1, pads 2/3, zones 4/5 -- and the
# keepout rules above reference those zone UUIDs exactly as keepout_dru.py
# would render them.
RULE_AREA_PAIR_BODY = """(net 0 "") (net 1 "N") (net 2 "CLK_P") (net 3 "CLK_N") (net 4 "X_POS") (net 5 "X_NEG")
      (footprint "P" (layer "F.Cu") (at 10 10)
       (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "N"))
       (pad "2" smd roundrect (at 2 0) (size 1 1) (roundrect_rratio .25) (layers "F.Cu" "F.Mask")))
      (segment (start 10.9 9) (end 10.9 11) (width .1) (layer "F.Cu") (net 1)
       (uuid "00000000-0000-0000-0000-000000000099"))
      (segment (start 12.5 9.2) (end 12.5 10.8) (width .1) (layer "F.Cu") (net 2)
       (uuid "00000000-0000-0000-0000-0000000000a1"))
      (segment (start 12.7 9.2) (end 12.7 10.8) (width .1) (layer "F.Cu") (net 3)
       (uuid "00000000-0000-0000-0000-0000000000a2"))
      (segment (start 13.5 9.2) (end 13.5 10.8) (width .1) (layer "F.Cu") (net 4)
       (uuid "00000000-0000-0000-0000-0000000000a3"))
      (segment (start 13.7 9.2) (end 13.7 10.8) (width .1) (layer "F.Cu") (net 5)
       (uuid "00000000-0000-0000-0000-0000000000a4"))
      (zone (net 0) (net_name "") (name "wall") (layers "F.Cu" "B.Cu")
       (hatch edge 0.5) (keepout (tracks not_allowed) (vias not_allowed))
       (polygon (pts (xy 14.5 9) (xy 15.5 9) (xy 15.5 11) (xy 14.5 11))))
      (zone (net 0) (net_name "") (name "fence") (layers "F.Cu")
       (hatch edge 0.5)
       (keepout (tracks not_allowed) (vias not_allowed) (pads not_allowed)
        (copperpour not_allowed) (footprints not_allowed))
       (polygon (pts (xy 16 9) (xy 17 9) (xy 17 11) (xy 16 11))))"""


def _dru_board(directory, body, rules=None, *, setup="(pad_to_mask_clearance .05)"):
    """Write a fixture board, optionally with a same-stem ``.kicad_dru``."""
    directory.mkdir(parents=True, exist_ok=True)
    path = _board(directory / f"{directory.name}.kicad_pcb", body, setup=setup)
    if rules is not None:
        path.with_suffix(".kicad_dru").write_text(rules, encoding="utf-8")
    return path


def _attribution(result):
    """Per-object attribution coverage: identity -> plotted geometry per layer."""
    return {
        identity: dict(item["layers_wkt"])
        for identity, item in result.geometry_provenance["source_objects"].items()
    }


def test_factory_silk_clearance_rules_leave_native_attribution_unchanged(tmp_path, native_options):
    """A geometry-neutral silk rule context must not invalidate attribution.

    Issue #5818: ``silk_clearance`` was missing from the custom-rule safe set,
    so an ordinary factory ``.kicad_dru`` made an otherwise-passing board fail
    with ``Native object rule context unsupported: silk_clearance``.
    """
    plain = _dru_board(tmp_path / "plain", PARITY_BODY)
    ruled = _dru_board(tmp_path / "ruled", PARITY_BODY, FACTORY_SILK_DRU)
    rules = ruled.with_suffix(".kicad_dru")
    before = (ruled.read_bytes(), rules.read_bytes())

    plain_result = check_mask_to_copper(plain, POLICY, **native_options)
    ruled_result = check_mask_to_copper(ruled, POLICY, **native_options)

    assert plain_result.coverage == "complete", plain_result.reasons
    assert ruled_result.coverage == "complete", ruled_result.reasons
    assert not any("silk_clearance" in reason for reason in ruled_result.reasons)
    # Equivalent outcome, equivalent plotted material, equivalent attribution.
    assert ruled_result.passed is plain_result.passed
    assert (
        ruled_result.geometry_provenance["export"]["layers_wkt"]
        == plain_result.geometry_provenance["export"]["layers_wkt"]
    )
    assert _attribution(ruled_result) == _attribution(plain_result)
    # The rule file is still bound into provenance and still left untouched --
    # accepting the context is not the same as ignoring the sidecar.
    assert (
        ruled_result.binding.rules_sha256 == hashlib.sha256(FACTORY_SILK_DRU.encode()).hexdigest()
    )
    assert plain_result.binding.rules_sha256 is None
    assert (ruled.read_bytes(), rules.read_bytes()) == before


def test_factory_fab_floor_rules_leave_native_attribution_unchanged(tmp_path, native_options):
    """The kct-managed fab-floor constraints must not invalidate attribution.

    Issue #8948: ``physical_hole_clearance``, ``text_thickness`` and
    ``text_height`` are emitted by every shipped tier file and by kct's own
    managed ``.kicad_dru`` block, so a board carrying them failed with
    ``Native object rule context unsupported`` despite their being measured
    minima over already-plotted material. Same proof pattern as the
    ``silk_clearance`` case above.
    """
    plain = _dru_board(tmp_path / "plain", PARITY_BODY)
    ruled = _dru_board(tmp_path / "ruled", PARITY_BODY, FACTORY_FAB_FLOOR_DRU)
    rules = ruled.with_suffix(".kicad_dru")
    before = (ruled.read_bytes(), rules.read_bytes())

    plain_result = check_mask_to_copper(plain, POLICY, **native_options)
    ruled_result = check_mask_to_copper(ruled, POLICY, **native_options)

    assert plain_result.coverage == "complete", plain_result.reasons
    assert ruled_result.coverage == "complete", ruled_result.reasons
    for name in ADMITTED_FACTORY_CONSTRAINTS:
        assert not any(name in reason for reason in ruled_result.reasons)
    # Equivalent outcome, equivalent plotted material, equivalent attribution.
    assert ruled_result.passed is plain_result.passed
    assert (
        ruled_result.geometry_provenance["export"]["layers_wkt"]
        == plain_result.geometry_provenance["export"]["layers_wkt"]
    )
    assert _attribution(ruled_result) == _attribution(plain_result)
    # The rule file is still bound into provenance and still left untouched --
    # accepting the context is not the same as ignoring the sidecar.
    assert (
        ruled_result.binding.rules_sha256
        == hashlib.sha256(FACTORY_FAB_FLOOR_DRU.encode()).hexdigest()
    )
    assert plain_result.binding.rules_sha256 is None
    assert (ruled.read_bytes(), rules.read_bytes()) == before


def test_factory_pair_limit_rules_leave_native_attribution_unchanged(tmp_path, native_options):
    """The kct-managed diff-pair rules must not invalidate attribution.

    Issue #8973: ``diff_pair_gap``, ``diff_pair_uncoupled``, ``skew`` and
    ``via_count`` are written into the same managed ``.kicad_dru`` for every
    router-coupled pair (#6443), so a board carrying them failed with
    ``Native object rule context unsupported`` despite their being measured
    minima/maxima over already-routed material. Same proof pattern as the
    fab-floor case above; the body carries both a KiCad-recognised pair and
    a router-recorded one, so both emitted rule shapes are exercised.
    """
    plain = _dru_board(tmp_path / "plain", RULE_AREA_PAIR_BODY)
    ruled = _dru_board(tmp_path / "ruled", RULE_AREA_PAIR_BODY, FACTORY_PAIR_LIMIT_DRU)
    rules = ruled.with_suffix(".kicad_dru")
    before = (ruled.read_bytes(), rules.read_bytes())

    plain_result = check_mask_to_copper(plain, POLICY, **native_options)
    ruled_result = check_mask_to_copper(ruled, POLICY, **native_options)

    assert plain_result.coverage == "complete", plain_result.reasons
    assert ruled_result.coverage == "complete", ruled_result.reasons
    for name in ("diff_pair_gap", "diff_pair_uncoupled", "skew", "via_count"):
        assert not any(name in reason for reason in ruled_result.reasons)
    # Equivalent outcome, equivalent plotted material, equivalent attribution.
    assert ruled_result.passed is plain_result.passed
    assert (
        ruled_result.geometry_provenance["export"]["layers_wkt"]
        == plain_result.geometry_provenance["export"]["layers_wkt"]
    )
    assert _attribution(ruled_result) == _attribution(plain_result)
    # The rule file is still bound into provenance and still left untouched --
    # accepting the context is not the same as ignoring the sidecar.
    assert (
        ruled_result.binding.rules_sha256
        == hashlib.sha256(FACTORY_PAIR_LIMIT_DRU.encode()).hexdigest()
    )
    assert plain_result.binding.rules_sha256 is None
    assert (ruled.read_bytes(), rules.read_bytes()) == before


def test_full_managed_sidecar_with_keepouts_leaves_native_attribution_unchanged(
    tmp_path, native_options
):
    """A keepout rule area's ``disallow`` rules must not invalidate attribution.

    Issue #8973: ``disallow`` is not a dimensional minimum but a prohibition
    on item *presence* inside a referenced rule area (#6039) -- DRC reports
    items there; it never moves, resizes, removes or adds plotted geometry,
    which is exactly what the parity below proves rather than asserts. The
    board carries two rule areas (mirrored by the keepout block, short and
    wide item lists both) and a router-coupled diff pair, and the sidecar
    carries every factory and kct-managed block together the way a real
    generated board's does, so the admitted names are exercised beside each
    other, not only in isolation.
    """
    managed = (
        FACTORY_SILK_DRU + FACTORY_FAB_FLOOR_DRU + FACTORY_PAIR_LIMIT_DRU + FACTORY_KEEPOUT_DRU
    )
    plain = _dru_board(tmp_path / "plain", RULE_AREA_PAIR_BODY)
    ruled = _dru_board(tmp_path / "ruled", RULE_AREA_PAIR_BODY, managed)
    rules = ruled.with_suffix(".kicad_dru")
    before = (ruled.read_bytes(), rules.read_bytes())

    plain_result = check_mask_to_copper(plain, POLICY, **native_options)
    ruled_result = check_mask_to_copper(ruled, POLICY, **native_options)

    assert plain_result.coverage == "complete", plain_result.reasons
    assert ruled_result.coverage == "complete", ruled_result.reasons
    for name in ADMITTED_FACTORY_CONSTRAINTS:
        assert not any(name in reason for reason in ruled_result.reasons)
    # Equivalent outcome, equivalent plotted material, equivalent attribution.
    assert ruled_result.passed is plain_result.passed
    assert (
        ruled_result.geometry_provenance["export"]["layers_wkt"]
        == plain_result.geometry_provenance["export"]["layers_wkt"]
    )
    assert _attribution(ruled_result) == _attribution(plain_result)
    # The rule file is still bound into provenance and still left untouched --
    # accepting the context is not the same as ignoring the sidecar.
    assert ruled_result.binding.rules_sha256 == hashlib.sha256(managed.encode()).hexdigest()
    assert plain_result.binding.rules_sha256 is None
    assert (ruled.read_bytes(), rules.read_bytes()) == before


@pytest.mark.parametrize(
    "constraint", ["zone_connection (with solid)", "thermal_relief_gap (min 0.3mm)"]
)
def test_geometry_altering_rule_context_still_fails_beside_silk_rules(
    tmp_path, native_options, constraint
):
    """The allowlist additions are narrow: unsupported contexts still fail closed.

    ``zone_connection`` and ``thermal_relief_gap`` genuinely change the filled
    shape native plotting produces, so they must keep being reported even when
    the same rule file also carries every accepted factory and kct-managed
    constraint. Only the plot-shaping context is reported; the
    geometry-neutral names beside it never appear (#8948, #8973).
    """
    rules = (
        FACTORY_SILK_DRU
        + FACTORY_FAB_FLOOR_DRU
        + FACTORY_PAIR_LIMIT_DRU
        + FACTORY_KEEPOUT_DRU
        + f'(rule "Fab fill" (constraint {constraint}))\n'
    )
    path = _dru_board(tmp_path / "altering", RULE_AREA_PAIR_BODY, rules)

    result = check_mask_to_copper(path, POLICY, **native_options)

    name = constraint.split()[0]
    assert result.coverage == "incomplete"
    assert not result.passed
    assert f"Native object rule context unsupported: {name}" in result.reasons
    for admitted in ADMITTED_FACTORY_CONSTRAINTS:
        assert not any(admitted in reason for reason in result.reasons)


def test_silk_rules_do_not_suppress_an_unsupported_native_graphic(tmp_path, native_options):
    """A silk rule file must not blanket-excuse other attribution gaps."""
    identity = "00000000-0000-0000-0000-000000000098"
    body = (
        PARITY_BODY
        + f'''(gr_line (start 14 9) (end 14 11) (stroke (width .2) (type dash))
          (layer "F.Mask") (uuid "{identity}"))'''
    )
    path = _dru_board(tmp_path / "dashed", body, FACTORY_SILK_DRU)

    result = check_mask_to_copper(path, POLICY, **native_options)

    assert result.coverage == "incomplete"
    assert f"{identity}: dashed native graphic attribution unsupported" in result.reasons
    assert not any("silk_clearance" in reason for reason in result.reasons)


@pytest.mark.parametrize("case", ["custom_rotated", "chamfer", "padstack", "graphics_text", "zone"])
def test_advanced_native_shapes_complete_and_match_independent_material(
    tmp_path, native_options, case
):
    path = _board(
        tmp_path / f"{case}.kicad_pcb", NATIVE_CASES[case], setup="(pad_to_mask_clearance .05)"
    )
    before = path.read_bytes()
    result = check_mask_to_copper(path, POLICY, **native_options)
    assert result.coverage == "complete", result.reasons
    assert result.passed, result.to_dict()
    assert path.read_bytes() == before
    for layer in ("F.Mask", "B.Mask", "F.Cu", "B.Cu"):
        oracle = read_native_gerber(
            native_options["artifact_dir"] / f"{case}-{layer.replace('.', '_')}.gbr"
        )
        from shapely import wkt

        actual = wkt.loads(result.geometry_provenance["export"]["layers_wkt"][layer])
        assert actual.difference(oracle.buffer(0.000005)).is_empty
        assert oracle.difference(actual.buffer(0.000005)).is_empty


@pytest.mark.parametrize("case", ["passing", "unintended", "connected", "overlapped"])
def test_final_native_intent_and_exposure_outcomes(tmp_path, native_options, case):
    if case == "connected":
        start, end = (10.4, 10), (12, 10)
    elif case == "overlapped":
        start, end = (9.8, 10), (10.2, 10)
    else:
        x = 10.9 if case == "passing" else 10.58
        start, end = (x, 9), (x, 11)
    trace_uuid = "00000000-0000-0000-0000-000000000099"
    body = f'''(net 1 "N")
      (footprint "P" (layer "F.Cu") (at 10 10)
       (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "N")))
      (segment (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) (width .1) (layer "F.Cu") (net 1) (uuid "{trace_uuid}"))'''
    path = _board(tmp_path / f"{case}.kicad_pcb", body, setup="(pad_to_mask_clearance .1)")
    result = check_mask_to_copper(path, POLICY, **native_options)
    assert result.coverage == "complete", result.reasons
    assert result.passed is (case == "passing"), result.to_dict()
    # Independent Gerbonara interpretation of actual native mask material,
    # intersected with this fixture's exact authored straight conductor capsule.
    mask = read_native_gerber(native_options["artifact_dir"] / f"{case}-F_Mask.gbr")
    conductor = LineString([start, end]).buffer(0.05, quad_segs=4096)
    exposed = mask.intersection(conductor).area
    if case == "passing":
        assert exposed == 0
    else:
        measurement = next(m for m in result.measurements if m.conductor_uuid == trace_uuid)
        assert measurement.exposed_area_mm2 == pytest.approx(exposed, abs=0.000002)
        assert measurement.disposition == "violation"
    if case == "connected":
        owner = "00000000-0000-0000-0000-000000000002"
        declaration = MaskEscapeIntent(
            result.binding, owner, trace_uuid, "F.Mask", "explicit reviewed escape"
        )
        intended = check_mask_to_copper(path, POLICY, [declaration], **native_options)
        assert intended.passed, intended.to_dict()
        assert intended.measurements[0].exposed_area_mm2 == pytest.approx(exposed, abs=0.000002)
        assert intended.measurements[0].disposition == "intentional"


def test_native_merged_bridge_exposes_third_copper_with_multiple_contributors(
    tmp_path, native_options
):
    path = _board(
        tmp_path / "merged.kicad_pcb",
        NATIVE_CASES["merged"],
        setup="(pad_to_mask_clearance 0) (solder_mask_min_width .3)",
    )
    result = check_mask_to_copper(path, POLICY, **native_options)
    assert result.coverage == "complete", result.reasons
    assert not result.passed
    bridge = [
        m
        for m in result.measurements
        if m.opening_kind == "merged_region" and m.exposed_area_mm2 > 0.01
    ]
    assert bridge
    assert all(len(m.opening_uuids) >= 2 and m.disposition == "violation" for m in bridge)
    oracle = read_native_gerber(native_options["artifact_dir"] / "merged-F_Mask.gbr")
    assert oracle.contains(Point(10, 10))


@pytest.mark.parametrize("margin,web", [(0.05, 0), (-0.05, 0), (0.05, 0.2), (-0.05, 0.2)])
def test_native_padstack_plot_branch_context(tmp_path, native_options, margin, web):
    path = _board(
        tmp_path / "stack-context.kicad_pcb",
        NATIVE_CASES["padstack"],
        setup=f"(pad_to_mask_clearance {margin}) (solder_mask_min_width {web})",
    )
    result = check_mask_to_copper(path, POLICY, **native_options)
    assert result.coverage == "complete", result.reasons
    assert result.passed, result.to_dict()
    # Native direct-mode setter/read asymmetry leaves the padstack mask at
    # copper size; web-merging uses a distinct polygon expansion path.
    oracle = read_native_gerber(native_options["artifact_dir"] / "stack-context-B_Mask.gbr")
    if web == 0 or margin < 0:
        assert oracle.area == pytest.approx(6, abs=0.00001)
    elif margin > 0:
        assert oracle.area > 6


@pytest.mark.parametrize("case", ["custom", "back_roundrect", "negative"])
def test_native_advanced_and_mask_defined_owner_never_hide_neighbor(tmp_path, native_options, case):
    layer = "B.Cu" if case == "back_roundrect" else "F.Cu"
    if case == "custom":
        body = NATIVE_CASES["custom_rotated"]
        start, end = (11.55, 8.79), (11.65, 8.79)
        margin = 0.05
    else:
        side = layer.replace(".Cu", ".Mask")
        body = f'''(footprint "P" (layer "{layer}") (at 10 10 37)
          (pad "1" smd roundrect (at 0 0 37) (size 2 1) (roundrect_rratio .25)
           (layers "{layer}" "{side}")))'''
        start, end = (9.95, 10), (10.05, 10)
        margin = -0.1 if case == "negative" else 0.05
    identity = "00000000-0000-0000-0000-000000000099"
    body += f'(segment (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) (width .05) (layer "{layer}") (net 0) (uuid "{identity}"))'
    path = _board(tmp_path / f"{case}.kicad_pcb", body, setup=f"(pad_to_mask_clearance {margin})")
    result = check_mask_to_copper(path, POLICY, **native_options)
    assert result.coverage == "complete", result.reasons
    assert not result.passed
    finding = next(m for m in result.measurements if m.conductor_uuid == identity)
    oracle = read_native_gerber(
        native_options["artifact_dir"] / f"{case}-{layer.replace('.Cu', '_Mask')}.gbr"
    )
    expected = oracle.intersection(LineString([start, end]).buffer(0.025, quad_segs=4096)).area
    assert finding.exposed_area_mm2 == pytest.approx(expected, abs=0.000002)
    assert finding.exposed_area_mm2 > 0.001
    assert finding.mask_defined is (case == "negative")
    if case == "negative":
        assert finding.negative_expansion
        assert finding.margin_provenance == "board:pad_to_mask_clearance=-0.1"


@pytest.mark.parametrize("front,back", [("yes", "no"), ("no", "yes"), ("yes", "yes"), ("no", "no")])
def test_native_via_tenting_controls_each_actual_mask_side(tmp_path, native_options, front, back):
    via = "00000000-0000-0000-0000-000000000001"
    body = f'(via (at 10 10) (size .6) (drill .3) (layers "F.Cu" "B.Cu") (tenting (front {front}) (back {back})) (uuid "{via}"))'
    path = _board(tmp_path / "via.kicad_pcb", body)
    result = check_mask_to_copper(path, POLICY, **native_options)
    assert result.coverage == "complete", result.reasons
    assert result.passed, result.to_dict()
    for side, tented in [("F", front), ("B", back)]:
        oracle = read_native_gerber(native_options["artifact_dir"] / f"via-{side}_Mask.gbr")
        assert oracle.is_empty is (tented == "yes")


def test_native_stored_plot_policy_default_frame(tmp_path, native_options):
    from kicad_tools.validate.mask_export_geometry import MaskExportOptions

    path = _board(
        tmp_path / "stored.kicad_pcb",
        NATIVE_CASES["padstack"],
        setup="(pcbplotparams (disableapertmacros false) (usegerberextensions true))",
    )
    result = check_mask_to_copper(
        path, POLICY, options=MaskExportOptions(board_plot_params=True), **native_options
    )
    assert result.coverage == "complete", result.reasons
    assert result.passed


@pytest.mark.parametrize(
    "footprint_margin,pad_margin,expected,source",
    [
        (None, None, 0.1, "board:"),
        (0.2, None, 0.2, "footprint:"),
        (0.2, 0, 0, "object:"),
        (0.2, -0.1, -0.1, "object:"),
    ],
)
def test_final_native_expansion_precedence(
    tmp_path, native_options, footprint_margin, pad_margin, expected, source
):
    fp_override = "" if footprint_margin is None else f"(solder_mask_margin {footprint_margin})"
    pad_override = "" if pad_margin is None else f"(solder_mask_margin {pad_margin})"
    body = f"""(footprint "P" (layer "F.Cu") (at 10 10) {fp_override}
      (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") {pad_override}))"""
    path = _board(tmp_path / "precedence.kicad_pcb", body, setup="(pad_to_mask_clearance .1)")
    result = check_mask_to_copper(path, POLICY, **native_options)
    assert result.coverage == "complete", result.reasons
    assert result.passed
    pad = result.geometry_provenance["source_objects"]["00000000-0000-0000-0000-000000000002"]
    assert pad["margin_mm"]["F.Mask"] == expected
    assert pad["margin_source"].startswith(source)
    oracle = read_native_gerber(native_options["artifact_dir"] / "precedence-F_Mask.gbr")
    assert oracle.bounds == pytest.approx(
        (9.5 - expected, 9.5 - expected, 10.5 + expected, 10.5 + expected), abs=0.000005
    )


def test_native_repeated_pad_numbers_do_not_share_owner_exclusion(tmp_path, native_options):
    body = """(footprint "P" (layer "F.Cu") (at 10 10)
      (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask"))
      (pad "1" smd rect (at .4 0) (size 1 1) (layers "F.Cu" "F.Mask")))"""
    path = _board(tmp_path / "repeated.kicad_pcb", body)
    result = check_mask_to_copper(path, POLICY, **native_options)
    assert result.coverage == "complete", result.reasons
    assert not result.passed
    first = "00000000-0000-0000-0000-000000000002"
    second = "00000000-0000-0000-0000-000000000003"
    pairs = {(m.opening_uuids, m.conductor_uuid) for m in result.measurements}
    assert ((first,), second) in pairs
    assert ((second,), first) in pairs
    assert all(m.exposed_area_mm2 > 0.59 for m in result.measurements)


def test_actual_native_cli_serializes_exposure_and_report_binding(tmp_path, native_options, capsys):
    from kicad_tools.cli.check_cmd import main
    from kicad_tools.drc.report import DRCReport

    body = """(footprint "P" (layer "F.Cu") (at 10 10)
      (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask")))
      (segment (start 10.58 9) (end 10.58 11) (width .1) (layer "F.Cu") (net 0)
       (uuid "00000000-0000-0000-0000-000000000099"))"""
    path = _board(tmp_path / "cli.kicad_pcb", body, setup="(pad_to_mask_clearance .1)")
    config = tmp_path / "request.json"
    config.write_text(
        json.dumps(
            {
                "schema": "kct.mask-copper-request.v1",
                "policy": POLICY.to_dict(),
                "native": {
                    key: str(value) if isinstance(value, Path) else value
                    for key, value in native_options.items()
                },
            }
        )
    )
    report_path = tmp_path / "report.json"
    assert (
        main(
            [
                str(path),
                "--drc-only",
                "--only",
                "mask_to_copper",
                "--mask-copper-config",
                str(config),
                "--format",
                "json",
                "--output",
                str(report_path),
            ]
        )
        == 2
    )
    stdout = json.loads(capsys.readouterr().out)
    assessment = stdout["mask_copper_assessments"][0]
    assert assessment["coverage"] == "complete"
    assert not stdout["summary"]["passed"]
    assert assessment["measurements"][0]["exposed_area_mm2"] > 0
    report = DRCReport.load(report_path)
    assert not report.passed
    assert json.loads(json.dumps(report.mask_copper_assessments[0].to_dict())) == assessment


@pytest.mark.parametrize("defect", ["missing", "duplicate"])
def test_final_native_identity_defects_cannot_pass(tmp_path, native_options, defect):
    body = """(footprint "P" (layer "F.Cu") (at 10 10)
      (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask")))"""
    path = _board(tmp_path / "identity.kicad_pcb", body)
    raw = path.read_text()
    pad_uuid = "00000000-0000-0000-0000-000000000002"
    if defect == "duplicate":
        raw = raw.replace(pad_uuid, "00000000-0000-0000-0000-000000000001")
    else:
        raw = raw.replace(f'(uuid "{pad_uuid}")', "")
    path.write_text(raw)
    result = check_mask_to_copper(path, POLICY, **native_options)
    assert result.coverage == "incomplete"
    assert not result.passed
    assert any(
        "uuid" in reason.lower() or "identity" in reason.lower() for reason in result.reasons
    ), result.reasons


@pytest.mark.parametrize("covered_by_pad", [False, True])
def test_uninventoried_native_target_cannot_hide_in_copper_union(
    tmp_path, native_options, covered_by_pad
):
    identity = "00000000-0000-0000-0000-000000000099"
    pad = """(footprint "P" (layer "F.Cu") (at 10 10)
      (pad "1" smd rect (at 0 0) (size 4 4) (layers "F.Cu" "F.Mask")))"""
    target = f'''(target plus (at 10 10) (size 1) (width .1)
      (layer "F.Cu") (uuid "{identity}"))'''
    path = _board(tmp_path / "target.kicad_pcb", (pad if covered_by_pad else "") + target)
    before = path.read_bytes()
    result = check_mask_to_copper(path, POLICY, **native_options)
    # The target-only parameter proves this native object really plots copper;
    # the covered parameter prevents union parity from hiding its omission.
    copper = read_native_gerber(native_options["artifact_dir"] / "target-F_Cu.gbr")
    assert copper.area > 0
    assert path.read_bytes() == before
    assert result.coverage == "incomplete", result.to_dict()
    assert not result.passed
    assert any(identity in reason and "uninventoried" in reason for reason in result.reasons)


# ---------------------------------------------------------------------------
# Interpreter resolution and worker-launch wiring (Issue #8946), hermetic:
# the native export and the worker subprocess are faked, so the command
# plumbing is exercised on every host without any KiCad installation.
# ---------------------------------------------------------------------------

_PAD_BODY = """(footprint "P1" (layer "F.Cu")
      (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0)
        (layers "*.Cu" "*.Mask")))"""

_QUALIFIED_VERSION = "10.0.6"


def _fake_native_launch(monkeypatch, tmp_path, native_version=_QUALIFIED_VERSION):
    """Fake the export and the worker subprocess; capture the worker argv.

    Returns ``(board, seen)`` where ``seen["cmd"]`` is the argv the native
    per-object worker was launched with and the output file it was told to
    write has been filled with a well-formed qualified payload.
    """
    import subprocess

    from kicad_tools.validate import mask_copper_geometry as mcg
    from kicad_tools.validate.mask_export_geometry import ExportedMaskGeometry
    from tests.test_mask_export_geometry import _board

    board = _board(tmp_path / "board.kicad_pcb", _PAD_BODY)
    digest = "a" * 64
    monkeypatch.setattr(
        mcg,
        "inspect_exported_mask_geometry",
        lambda *a, **k: ExportedMaskGeometry(
            source_sha256=digest,
            project_sha256=None,
            rules_sha256=None,
            native_version=native_version,
            export_identity={},
        ),
    )
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        Path(cmd[-1]).write_text(
            json.dumps(
                {
                    "schema": "kct.native-mask-objects.v1",
                    "source_sha256": digest,
                    "native_version": native_version,
                    "errors": [],
                    "objects": {},
                    "max_error_mm": 0.0,
                }
            )
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mcg.subprocess, "run", fake_run)
    return board, seen


def _forbid_discovery(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("discovery must not run when the command is already resolved")

    monkeypatch.setattr("kicad_tools.zones.placement_fill.find_kicad_python", fail)


def test_native_worker_launches_under_explicit_command(monkeypatch, tmp_path):
    """An explicit native_python_command wins unchanged, and provenance records it."""
    from kicad_tools.validate.mask_copper_geometry import (
        inspect_attributed_mask_geometry,
    )

    board, seen = _fake_native_launch(monkeypatch, tmp_path)
    _forbid_discovery(monkeypatch)
    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.docker_fallback_enabled", lambda: True)
    command = ["/explicit/python3", "-I"]
    result = inspect_attributed_mask_geometry(
        board, native_python_command=command, scratch_dir=tmp_path
    )
    assert seen["cmd"][:2] == command
    # Executed argv is untouched; the recorded form is host-independent (#9055).
    assert result.provenance["native_python_command"] == ["python3"]


def test_native_worker_launches_in_pinned_container(monkeypatch, tmp_path):
    """With KCT_KICAD_DOCKER=1 the worker runs as --entrypoint python3 in the image.

    One bind of the stage directory makes the staged worker, board, request
    and output all visible at their host paths; the config home is the pinned
    process-shared one.
    """
    from kicad_tools.validate.mask_copper_geometry import (
        inspect_attributed_mask_geometry,
    )

    board, seen = _fake_native_launch(monkeypatch, tmp_path)
    _forbid_discovery(monkeypatch)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.docker_fallback_enabled", lambda: True)
    monkeypatch.setattr(
        "kicad_tools.validate.mask_copper_geometry.shutil.which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.process_config_home", lambda: cfg)
    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain._synced_model_dir", lambda: None)
    result = inspect_attributed_mask_geometry(board, scratch_dir=tmp_path)
    cmd = seen["cmd"]
    entry = cmd.index("--entrypoint")
    assert cmd[entry + 1] == "python3"
    stage = Path(cmd[-1]).parent
    assert f"{stage}:{stage}" in cmd
    assert f"KICAD_CONFIG_HOME={cfg}" in cmd
    # Executed argv keeps every mount; the recorded form drops them all and
    # keeps only the pinned image and the entrypoint (#9055).
    from kicad_tools.cli.kicad_toolchain import docker_image

    assert result.provenance["native_python_command"] == [
        "docker",
        "run",
        docker_image(),
        "python3",
    ]
    assert [Path(p).parent for p in cmd[-4:]] == [stage] * 4


def test_recorded_provenance_is_stable_across_stage_dirs(monkeypatch, tmp_path):
    """Two docker runs with different temp dirs record identical bytes (#9055).

    Per-run stage mounts and config homes must not reach the recorded
    provenance, so neither the provenance dict nor the ``export_profile_sha256``
    binding hash (which folds it in) may vary between runs.
    """
    from kicad_tools.cli.kicad_toolchain import docker_image
    from kicad_tools.validate.mask_copper import source_binding
    from kicad_tools.validate.mask_copper_geometry import (
        inspect_attributed_mask_geometry,
    )

    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.docker_fallback_enabled", lambda: True)
    monkeypatch.setattr(
        "kicad_tools.validate.mask_copper_geometry.shutil.which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain._synced_model_dir", lambda: None)
    provenance = []
    bindings = []
    for run in ("stage-one", "stage-two"):
        scratch = tmp_path / run
        scratch.mkdir()
        cfg = scratch / "cfg"
        cfg.mkdir()
        monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.process_config_home", lambda: cfg)
        board, _ = _fake_native_launch(monkeypatch, tmp_path / run)
        _forbid_discovery(monkeypatch)
        result = inspect_attributed_mask_geometry(board, scratch_dir=scratch)
        assert result.provenance["native_python_command"] == [
            "docker",
            "run",
            docker_image(),
            "python3",
        ]
        provenance.append(result.provenance)
        bindings.append(source_binding(result).export_profile_sha256)
    assert provenance[0] == provenance[1]
    assert bindings[0] == bindings[1]


def test_native_worker_discovers_pcbnew_interpreter(monkeypatch, tmp_path):
    """Without an explicit command or docker, discovery probes a plain pcbnew import."""
    from kicad_tools.validate.mask_copper_geometry import (
        inspect_attributed_mask_geometry,
    )

    board, seen = _fake_native_launch(monkeypatch, tmp_path)
    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.docker_fallback_enabled", lambda: False)
    probes = []

    def fake_find(probe):
        probes.append(probe)
        return Path("/opt/kicad/bin/python3")

    monkeypatch.setattr("kicad_tools.zones.placement_fill.find_kicad_python", fake_find)
    result = inspect_attributed_mask_geometry(board, scratch_dir=tmp_path)
    assert seen["cmd"][0] == "/opt/kicad/bin/python3"
    assert probes == ["import pcbnew"]
    # Recorded form keeps only the executable name (#9055); execution unchanged.
    assert result.provenance["native_python_command"] == ["python3"]


def test_unqualified_discovered_interpreter_still_fails_version_binding(monkeypatch, tmp_path):
    """A weak pcbnew-only probe never widens what a report can claim."""
    from kicad_tools.validate.mask_copper_geometry import (
        inspect_attributed_mask_geometry,
    )

    board, seen = _fake_native_launch(monkeypatch, tmp_path, native_version="9.0.1")
    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.docker_fallback_enabled", lambda: False)
    monkeypatch.setattr(
        "kicad_tools.zones.placement_fill.find_kicad_python",
        lambda probe: Path("/opt/old/bin/python3"),
    )
    result = inspect_attributed_mask_geometry(board, scratch_dir=tmp_path)
    assert any("qualified KiCad plot profile" in e for e in result.errors)


def test_resolve_native_python_explicit_wins(monkeypatch, tmp_path):
    from kicad_tools.validate.mask_copper_geometry import (
        _resolve_native_python_command,
    )

    _forbid_discovery(monkeypatch)
    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.docker_fallback_enabled", lambda: True)
    assert _resolve_native_python_command(["/explicit/python3"], tmp_path) == (
        ["/explicit/python3"],
        None,
    )


def test_resolve_native_python_container_argv(monkeypatch, tmp_path):
    from kicad_tools.validate.mask_copper_geometry import (
        _resolve_native_python_command,
    )

    _forbid_discovery(monkeypatch)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.docker_fallback_enabled", lambda: True)
    monkeypatch.setattr(
        "kicad_tools.validate.mask_copper_geometry.shutil.which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.process_config_home", lambda: cfg)
    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain._synced_model_dir", lambda: None)
    command, reason = _resolve_native_python_command(None, tmp_path / "stage")
    assert reason is None
    entry = command.index("--entrypoint")
    assert command[entry + 1] == "python3"
    assert f"{tmp_path / 'stage'}:{tmp_path / 'stage'}" in command
    assert f"KICAD_CONFIG_HOME={cfg}" in command


def test_resolve_native_python_container_needs_docker_binary(monkeypatch, tmp_path):
    """KCT_KICAD_DOCKER=1 without a docker executable falls back to discovery."""
    import kicad_tools.validate.mask_copper_geometry as mcg

    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.docker_fallback_enabled", lambda: True)
    monkeypatch.setattr(mcg.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        "kicad_tools.zones.placement_fill.find_kicad_python",
        lambda probe: Path("/found/python3"),
    )
    assert mcg._resolve_native_python_command(None, tmp_path) == (
        ["/found/python3"],
        None,
    )


def test_resolve_native_python_unavailable_reason_names_the_fix(monkeypatch, tmp_path):
    from kicad_tools.validate.mask_copper_geometry import (
        _resolve_native_python_command,
    )

    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.docker_fallback_enabled", lambda: False)
    monkeypatch.setattr("kicad_tools.zones.placement_fill.find_kicad_python", lambda probe: None)
    command, reason = _resolve_native_python_command(None, tmp_path)
    assert command is None
    assert "ModuleNotFoundError" not in reason
    assert "native_python_command" in reason
    assert "KCT_KICAD_DOCKER" in reason
    assert "Install KiCad" in reason


# ---------------------------------------------------------------------------
# A refilled pour pinch must repair, not drop the zone (Issue #9072)
# ---------------------------------------------------------------------------

#: Board 05's refilled GND pours hand the reader ring-self-intersecting fills:
#: the outline walks a zero-width neck out and back over the same segment (the
#: real F.Cu region traverses y=92.449496 from x=176.035 to x=177.35 twice,
#: vertices 8469/8470 and 8541/8542), which shapely rejects as
#: ``Ring Self-intersection`` even though the fill's area is unambiguous.
#: This is the same topology scaled down: a lower slab and a finger joined
#: through the doubled corridor, area 4 x 2 + 2 x 2 = 12.
_POUR_PINCH = [
    [0, 0],
    [4, 0],
    [4, 2],
    [1, 2],
    [3, 2],
    [3, 4],
    [1, 4],
    [1, 2],
    [0, 2],
    [0, 0],
]

_ZONE_BODY = """(zone (net 0) (net_name "GND") (layer "F.Cu") (hatch edge .5)
  (connect_pads (clearance .2)) (min_thickness .1) (filled_areas_thickness no) (fill yes)
  (polygon (pts (xy 10 10) (xy 14 10) (xy 14 13) (xy 10 13)))
  (filled_polygon (layer "F.Cu") (pts (xy 10 10) (xy 14 10) (xy 14 13) (xy 10 13))))"""


def test_refilled_zone_fill_pinch_repairs_instead_of_dropping(monkeypatch, tmp_path):
    """A pinched zone fill keeps its layer geometry and the assessment complete.

    The extraction used to drop the whole layer for such a zone
    (``invalid native object polygon``) and then flag the per-object/full-layer
    mismatch, capping every board 05 finding at a provisional warning.
    """
    from shapely.validation import explain_validity

    from kicad_tools.validate.gerber_geometry import repair_self_touching_polygon
    from kicad_tools.validate.mask_copper_geometry import inspect_attributed_mask_geometry

    pinched = Polygon(_POUR_PINCH)
    assert not pinched.is_valid
    assert "Ring Self-intersection" in explain_validity(pinched)
    repaired = repair_self_touching_polygon(pinched)
    assert repaired is not None and repaired.is_valid
    assert repaired.area == pytest.approx(12.0)

    zone_uuid = str(uuid.UUID(int=1))
    board = _board(tmp_path / "board.kicad_pcb", _ZONE_BODY)
    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.docker_fallback_enabled", lambda: False)
    digest = "a" * 64

    import subprocess

    from kicad_tools.validate import mask_copper_geometry as mcg
    from kicad_tools.validate.mask_export_geometry import ExportedMaskGeometry

    monkeypatch.setattr(
        mcg,
        "inspect_exported_mask_geometry",
        lambda *a, **k: ExportedMaskGeometry(
            source_sha256=digest,
            project_sha256=None,
            rules_sha256=None,
            native_version=_QUALIFIED_VERSION,
            export_identity={},
        ),
    )

    def fake_run(cmd, **kw):
        Path(cmd[-1]).write_text(
            json.dumps(
                {
                    "schema": "kct.native-mask-objects.v1",
                    "source_sha256": digest,
                    "native_version": _QUALIFIED_VERSION,
                    "errors": [],
                    "max_error_mm": 0.0,
                    "objects": {
                        zone_uuid: {
                            "kind": "ZONE",
                            "net": "GND",
                            "margin_mm": {},
                            "layers": {"F.Cu": [{"shell": _POUR_PINCH, "holes": []}]},
                        }
                    },
                }
            )
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mcg.subprocess, "run", fake_run)
    result = inspect_attributed_mask_geometry(
        board, native_python_command=["/host/python3"], scratch_dir=tmp_path
    )
    assert not [e for e in result.errors if "invalid native object polygon" in e], result.errors
    assert not [e for e in result.errors if "material mismatch" in e], result.errors
    geometry = result.objects[zone_uuid]["layers_geometry"]["F.Cu"]
    assert geometry.is_valid
    assert geometry.area == pytest.approx(12.0)
