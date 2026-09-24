"""Effective native silk-clearance behaviour, measured against real geometry.

Issue #5059.  Every assertion in this module is about what ``kicad-cli pcb
drc`` *actually reports* for a board whose geometry sits a known distance
below (or above) the factory floor -- never about the numbers we wrote into a
``.kicad_pro`` / ``.kicad_dru``.  Asserting the generated JSON/S-expression
values proves only that we serialised what we intended; it cannot detect a
constraint the native engine silently declines to apply.  That distinction is
the whole point of the issue: a project-level silk minimum was set, looked
correct in the emitted JSON, and produced **no** findings.

The measured contract, re-derived locally on KiCad CLI **10.0.1**
(macOS, 2026-09-23) with a minimal two-object board:

===========================================================  ==============
probe (identical geometry: 0.085 mm silk-to-mask gap)         native finding
===========================================================  ==============
project ``rules.min_silk_clearance`` = 0.15 mm, no ``.dru``   none
project ``rules.min_silk_clearance`` = 2.0 mm, no ``.dru``    none
same project + explicit ``(constraint silk_clearance ...)``   ``silk_over_copper``
                                                              ``clearance 0.1500 mm;
                                                              actual 0.0850 mm``
===========================================================  ==============

The project block is demonstrably loaded in all three runs:
:func:`test_project_rules_block_is_loaded_positive_control` raises
``min_clearance`` on the same fixture and native DRC reports ``board minimum
clearance 0.5000 mm``.  So "the sidecar was ignored" is ruled out as the
explanation; what is measured is narrower -- **this particular** built-in
minimum did not gate this gap.

A fourth probe (:func:`test_builtin_silk_check_fires_only_on_actual_overlap`)
shows the built-in ``silk_over_copper`` test firing with *no*
``min_silk_clearance`` key present at all, once the silk genuinely overlaps
the mask aperture, and reporting no numeric clearance/actual pair.  That is
consistent with the built-in check being an overlap test rather than a
gap-threshold test.

**Consistent with is not the same as confirmed.**  KiCad 10.0's implicit silk
rule being restricted to silk layers while the clearance provider evaluates
against the second item's layer is a *lead* for a minimal upstream
reproduction, not a root cause this repository has established.  These tests
deliberately assert only the observable behaviour, so they stay correct
whichever way that lead resolves -- and if a future KiCad release does start
honouring the built-in minimum for this gap,
:func:`test_project_min_silk_clearance_alone_misses_subfloor_gap` fails and
tells us the explicit rule has become redundant.

Related coverage: ``tests/test_factory_object_clearance.py`` exercises the
emitted ``Silk to Pad`` / PTH rules across object kinds, layers and nets.
This module covers the *coverage gap itself* -- the project-only baseline
those tests never establish.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.manufacturers import get_profile, write_drc_constraints
from kicad_tools.manufacturers.dru_generator import generate_dru
from kicad_tools.manufacturers.project_generator import build_project_rules

# --- fixture geometry -------------------------------------------------------
#
# One 1x1 mm SMD pad centred at (10, 10) with a coincident mask aperture
# (``pad_to_mask_clearance 0``), plus one vertical silk line of width
# 0.15 mm.  The pad's mask edge is at x = 10.5 and the silk stroke's near
# edge is at ``silk_centre - 0.075``, so the gap is exactly
# ``silk_centre - 10.575``.
_PAD_MASK_EDGE_X = 10.5
_SILK_HALF_WIDTH = 0.075

#: JLCPCB's published rigid-PCB silkscreen-to-pad floor (mm).
_FACTORY_SILK_FLOOR_MM = 0.15

#: Gap used for every "below the floor" probe (mm).  Matches the measured
#: Chorus example in #5059 -- a resistor outline 0.085 mm from its own pad.
_SUBFLOOR_GAP_MM = 0.085

#: Gap used for every "above the floor" probe (mm).
_CLEAR_GAP_MM = 0.16


def _silk_centre_for_gap(gap_mm: float) -> float:
    return _PAD_MASK_EDGE_X + _SILK_HALF_WIDTH + gap_mm


def _write_board(path: Path, *, silk_centre_x: float, silk_layer: str = "F.SilkS") -> Path:
    """One masked F.Cu pad and one silk line, on a 20x20 mm outline."""
    path.write_text(
        f"""(kicad_pcb (version 20240108) (generator pcbnew)
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
   (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0)) (net 0 "") (net 1 "A") (net 2 "B")
  (gr_rect (start 0 0) (end 20 20) (stroke (width .1) (type default))
   (fill none) (layer "Edge.Cuts"))
  (footprint "T" (layer "F.Cu") (at 10 10)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "A")))
  (gr_line (start {silk_centre_x} 9) (end {silk_centre_x} 11)
   (stroke (width .15) (type default)) (layer "{silk_layer}")))
"""
    )
    return path


def _write_two_pad_board(path: Path, *, gap_mm: float) -> Path:
    """Two different-net masked SMD pads ``gap_mm`` apart (positive control)."""
    path.write_text(
        f"""(kicad_pcb (version 20240108) (generator pcbnew)
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
   (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0)) (net 0 "") (net 1 "A") (net 2 "B")
  (gr_rect (start 0 0) (end 20 20) (stroke (width .1) (type default))
   (fill none) (layer "Edge.Cuts"))
  (footprint "T" (layer "F.Cu") (at 10 10)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "A")))
  (footprint "T" (layer "F.Cu") (at {11 + gap_mm} 10)
    (pad "2" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 2 "B"))))
"""
    )
    return path


def _write_project(path: Path, rules: dict[str, float]) -> Path:
    """A minimal ``.kicad_pro`` carrying only a design-rules/severity block.

    ``silk_over_copper`` is pinned to ``error`` so a finding cannot be lost to
    a severity default; ``--severity-all`` below makes the verdict independent
    of that anyway, and the tests assert the reported severity explicitly
    where it matters.
    """
    path.write_text(
        json.dumps(
            {
                "board": {
                    "design_settings": {
                        "rules": rules,
                        "rule_severities": {
                            "silk_over_copper": "error",
                            "silk_overlap": "error",
                            "silk_edge_clearance": "error",
                        },
                    }
                },
                "meta": {"filename": path.name, "version": 1},
            },
            indent=2,
        )
    )
    return path


def _run_native_drc(board: Path, report: Path) -> list[dict]:
    cli = find_kicad_cli()
    assert cli is not None  # guarded by _require_cli
    proc = subprocess.run(
        [
            str(cli),
            "pcb",
            "drc",
            "--severity-all",
            "--format",
            "json",
            "-o",
            str(report),
            str(board),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    violations = json.loads(report.read_text())["violations"]
    # A malformed custom rule makes KiCad discard the whole .kicad_dru (#4999)
    # and would turn every "no finding" assertion below into a false pass.
    assert not [v for v in violations if v["type"] == "drc_rule_error"], violations
    return violations


def _silk_findings(violations: list[dict]) -> list[dict]:
    return [v for v in violations if v["type"] in ("silk_over_copper", "silk_overlap")]


def _require_cli() -> None:
    if find_kicad_cli() is None:
        pytest.skip("Native KiCad CLI is not installed")


# ---------------------------------------------------------------------------
# The coverage gap itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "min_silk_clearance_mm",
    [
        # At the factory floor: the value a reader of the emitted .kicad_pro
        # would reasonably believe enforces the floor.
        _FACTORY_SILK_FLOOR_MM,
        # Absurdly elevated: if the built-in minimum gated this gap at all,
        # 2.0 mm could not possibly stay silent on a 0.085 mm gap.  This is
        # the probe that separates "the value is too small" from "this
        # constraint does not govern this pair".
        2.0,
    ],
    ids=["at-factory-floor", "elevated-2mm"],
)
def test_project_min_silk_clearance_alone_misses_subfloor_gap(tmp_path, min_silk_clearance_mm):
    """A project-only silk minimum does not report a 0.085 mm silk-to-pad gap.

    No ``.kicad_dru`` exists in this run, so the project's built-in minimum is
    the only thing that could produce a finding.  Measured: it produces none,
    at either value.

    If this test ever *fails*, that is good news and not a regression in this
    repository: it means the native engine has started gating this gap from
    the project block, and the explicit ``Silk to Pad`` rule emitted by
    :func:`~kicad_tools.manufacturers.dru_generator.generate_dru` may have
    become redundant.  Re-measure before changing the emitter.
    """
    _require_cli()
    board = _write_board(
        tmp_path / "probe.kicad_pcb",
        silk_centre_x=_silk_centre_for_gap(_SUBFLOOR_GAP_MM),
    )
    _write_project(
        tmp_path / "probe.kicad_pro",
        {
            "min_silk_clearance": min_silk_clearance_mm,
            "min_clearance": 0.1016,
            "min_track_width": 0.127,
        },
    )
    assert not (tmp_path / "probe.kicad_dru").exists()

    violations = _run_native_drc(board, tmp_path / "native.json")
    assert _silk_findings(violations) == [], violations


def test_project_rules_block_is_loaded_positive_control(tmp_path):
    """The project's ``rules`` block IS read -- so silence above is not "ignored".

    Without this control the preceding test is ambiguous: a sidecar that never
    reached the engine would look identical to a constraint the engine declines
    to apply.  Raising ``min_clearance`` on a board whose only feature is a
    0.12 mm different-net pad gap forces a built-in finding that names the
    board minimum, proving the same block in the same location is honoured.
    """
    _require_cli()
    board = _write_two_pad_board(tmp_path / "probe.kicad_pcb", gap_mm=0.12)
    _write_project(
        tmp_path / "probe.kicad_pro",
        {"min_clearance": 0.5, "min_silk_clearance": 2.0, "min_track_width": 0.127},
    )

    violations = _run_native_drc(board, tmp_path / "native.json")
    clearance = [v for v in violations if v["type"] == "clearance"]
    assert clearance, violations
    assert "0.5000 mm" in clearance[0]["description"], clearance[0]
    assert "0.1200 mm" in clearance[0]["description"], clearance[0]


def test_explicit_silk_clearance_rule_reports_subfloor_gap(tmp_path):
    """Byte-identical geometry + an explicit rule reports the gap numerically.

    Same board bytes and same project bytes as the "at-factory-floor" case of
    :func:`test_project_min_silk_clearance_alone_misses_subfloor_gap`; the only
    difference is the presence of a ``.kicad_dru`` carrying an explicit
    ``silk_clearance`` constraint.  The finding names both the required and the
    actual distance, which the built-in overlap check never does.
    """
    _require_cli()
    board = _write_board(
        tmp_path / "probe.kicad_pcb",
        silk_centre_x=_silk_centre_for_gap(_SUBFLOOR_GAP_MM),
    )
    _write_project(
        tmp_path / "probe.kicad_pro",
        {
            "min_silk_clearance": _FACTORY_SILK_FLOOR_MM,
            "min_clearance": 0.1016,
            "min_track_width": 0.127,
        },
    )
    (tmp_path / "probe.kicad_dru").write_text(
        "(version 1)\n"
        '(rule "Silk to Pad probe"\n'
        "  (condition \"A.Type == 'Pad' || B.Type == 'Pad'\")\n"
        f"  (constraint silk_clearance (min {_FACTORY_SILK_FLOOR_MM}mm)))\n"
    )

    violations = _run_native_drc(board, tmp_path / "native.json")
    findings = _silk_findings(violations)
    assert len(findings) == 1, violations
    description = findings[0]["description"]
    assert "Silk to Pad probe" in description, description
    assert "0.1500 mm" in description, description
    assert "0.0850 mm" in description, description


def test_builtin_silk_check_fires_only_on_actual_overlap(tmp_path):
    """The built-in silk check is an overlap test, not a gap-threshold test.

    No ``min_silk_clearance`` key is present at all and no ``.kicad_dru``
    exists, yet silk that genuinely intrudes into the mask aperture is
    reported -- with no numeric clearance/actual pair.  Together with the two
    tests above this characterises the built-in check as overlap-driven, which
    is why a sub-floor *gap* needs an explicit rule to be seen.

    This is a behavioural characterisation of KiCad, not a claim about its
    implementation.
    """
    _require_cli()
    # Silk centred inside the pad's mask aperture (aperture spans 9.5..10.5).
    board = _write_board(tmp_path / "probe.kicad_pcb", silk_centre_x=10.4)
    _write_project(
        tmp_path / "probe.kicad_pro", {"min_clearance": 0.1016, "min_track_width": 0.127}
    )

    violations = _run_native_drc(board, tmp_path / "native.json")
    findings = _silk_findings(violations)
    assert len(findings) == 1, violations
    assert findings[0]["type"] == "silk_over_copper"
    # No threshold is named, because none was applied.
    assert "mm" not in findings[0]["description"], findings[0]


# ---------------------------------------------------------------------------
# The shipped profile, end to end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gap_mm,expected",
    [(_SUBFLOOR_GAP_MM, True), (_CLEAR_GAP_MM, False)],
    ids=["below-floor", "above-floor"],
)
def test_emitted_jlcpcb_profile_gates_the_silk_floor(tmp_path, gap_mm, expected):
    """``write_drc_constraints`` delivers the floor as *behaviour*, not JSON.

    Runs the real emitter (project + DRU) rather than a hand-written rule, so
    a regression that drops or mis-scopes ``Silk to Pad`` is caught here even
    if the emitted text still "looks right".  Both directions are asserted:
    0.085 mm must be reported and 0.16 mm must not, which pins the threshold
    rather than merely proving the rule is noisy.
    """
    _require_cli()
    board = _write_board(tmp_path / "probe.kicad_pcb", silk_centre_x=_silk_centre_for_gap(gap_mm))
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1.0)
    assert rules.min_silk_to_pad_clearance_mm == _FACTORY_SILK_FLOOR_MM
    write_drc_constraints(board, rules, manufacturer_id="jlcpcb", layers=4)

    violations = _run_native_drc(board, tmp_path / "native.json")
    findings = [v for v in _silk_findings(violations) if "Silk to Pad" in v["description"]]
    assert bool(findings) == expected, violations
    if expected:
        assert f"{_SUBFLOOR_GAP_MM:.4f} mm" in findings[0]["description"], findings[0]


def test_emitted_jlcpcb_profile_silk_rule_is_scoped_to_the_same_board_side(tmp_path):
    """Back-side silk against a front-side pad is not a silk-clearance finding.

    The issue asks for *cross-layer* silk clearance -- F.SilkS against the
    F.Cu/F.Mask stack, which the built-in silk-layer-only evaluation misses.
    It does not ask for silk on one side of the board to be measured against
    copper on the other, which is not a manufacturable concern.  Without this
    negative, a rule broadened until the below-floor probe passes would look
    correct while flagging every board twice over.

    The assertion is keyed on the ``Silk to Pad`` rule NAME, so on its own it
    would also pass on any KiCad that never evaluates that rule -- green for
    exactly the reason it is meant to catch (Issue #5713, where the sibling
    ``SMD Pad Clearance`` negative did precisely that on KiCad 10.0.1).  The
    same-side positive control below is therefore part of the test, not
    decoration: the absence is only evidence once the presence is measured in
    the same run, on the same board shape and the same emitted sidecar.
    """
    _require_cli()
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1.0)

    control = _write_board(
        tmp_path / "control.kicad_pcb",
        silk_centre_x=_silk_centre_for_gap(_SUBFLOOR_GAP_MM),
        silk_layer="F.SilkS",
    )
    write_drc_constraints(control, rules, manufacturer_id="jlcpcb", layers=4)
    control_findings = [
        v
        for v in _silk_findings(_run_native_drc(control, tmp_path / "control.json"))
        if "Silk to Pad" in v["description"]
    ]
    assert control_findings, (
        "Positive control produced no 'Silk to Pad' finding: this KiCad is not evaluating "
        "the emitted rule at all, so the cross-side negative below would pass vacuously."
    )

    board = _write_board(
        tmp_path / "probe.kicad_pcb",
        silk_centre_x=_silk_centre_for_gap(_SUBFLOOR_GAP_MM),
        silk_layer="B.SilkS",
    )
    write_drc_constraints(board, rules, manufacturer_id="jlcpcb", layers=4)

    violations = _run_native_drc(board, tmp_path / "native.json")
    assert [v for v in _silk_findings(violations) if "Silk to Pad" in v["description"]] == [], (
        violations
    )


# ---------------------------------------------------------------------------
# Which sidecar carries the floor (no native CLI required)
# ---------------------------------------------------------------------------


def test_silk_floor_is_carried_by_the_dru_not_the_project_key():
    """Pins *where* the factory silk floor lives, given the measured gap.

    ``build_project_rules`` maps KiCad's ``min_silk_clearance`` key from
    ``min_solder_mask_clearance_mm`` (0.05 mm for JLCPCB), not from the
    0.15 mm silkscreen-to-pad floor -- a mapping that predates this issue
    (#3720) and is reproduced verbatim in 24 committed board/fixture
    ``.kicad_pro`` artifacts.  The native probes above measured that key to
    have no effect on a sub-floor gap in either direction, so re-pointing it
    would churn those artifacts for no measured DRC change; that question is
    tracked separately rather than settled here.

    What this test guards is the contract that actually matters: the factory
    floor reaches native DRC through the emitted ``Silk to Pad`` rule in the
    ``.kicad_dru``.  If someone deletes that rule believing the project key
    covers it, this fails.
    """
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1.0)

    emitted = generate_dru(rules, manufacturer_name="JLCPCB")
    assert 'rule "Silk to Pad - JLCPCB"' in emitted
    assert f"(constraint silk_clearance (min {_FACTORY_SILK_FLOOR_MM}mm))" in emitted

    project_rules = build_project_rules(rules)
    assert project_rules["min_silk_clearance"] == rules.min_solder_mask_clearance_mm
    assert project_rules["min_silk_clearance"] != rules.min_silk_to_pad_clearance_mm
