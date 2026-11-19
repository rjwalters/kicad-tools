"""``kct check`` rules against custom-pad primitive copper (Issue #9498).

The stock ``Package_TO_SOT_SMD:SOT-89-3`` pad 2 is a ``custom`` pad: a 1.5 x
0.9 mm rectangular anchor plus a zero-width filled ``gr_poly`` tab reaching to
+3.8625 mm along the pad's local x axis.  The fixture below mirrors that
structure (the same dimensions PR #9470's review verified against the pinned
image's library footprint).  Before #9498 the pad was modelled by its anchor
alone in ``clearance`` and silk-over-copper and dropped from
``tht_smd_spacing`` / ``physical_copper_gap`` coverage, so a real short or
clearance violation against the tab copper read as clean.

Each probe case from the issue's evidence table is asserted at all 8 probe
placements (0/90/180/30 degrees, front and back).  The kicad-cli side of the
table was established on the pinned image by the issue's probe; these tests
pin the ``kct check`` side.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from kicad_tools.cli.check_cmd import main

# Pad-local geometry (mm), mirroring the stock SOT-89-3 tab pad: anchor
# 1.5 x 0.9 centred on the pad origin, tab from x=0.5 to x=3.8625 (well past
# the anchor edge at 0.75).  The probe's 0.2 mm different-net track is
# reproduced by TRACK_WIDTH.
PAD_AT = (-1.8625, 0.0)
TAB_NEAR, TAB_FAR, TAB_HALF_HEIGHT = 0.5, 3.8625, 0.45
TRACK_WIDTH = 0.2

# All 8 probe placements: 4 rotations x front/back.
PLACEMENTS = [
    pytest.param(0.0, "F", id="0-front"),
    pytest.param(90.0, "F", id="90-front"),
    pytest.param(180.0, "F", id="180-front"),
    pytest.param(30.0, "F", id="30-front"),
    pytest.param(0.0, "B", id="0-back"),
    pytest.param(90.0, "B", id="90-back"),
    pytest.param(180.0, "B", id="180-back"),
    pytest.param(30.0, "B", id="30-back"),
]

# The refused-primitive fixture: the most common stock-library refusal after
# stroked gr_poly (issue evidence), and the one #9501 tracks first.
GR_CIRCLE_PRIMITIVE = "(gr_circle (center 1.0 0.0) (width 0.2) (fill yes))"

TAB_POLY = (
    f"(gr_poly (pts (xy {TAB_NEAR} -{TAB_HALF_HEIGHT}) (xy {TAB_FAR} -{TAB_HALF_HEIGHT}) "
    f"(xy {TAB_FAR} {TAB_HALF_HEIGHT}) (xy {TAB_NEAR} {TAB_HALF_HEIGHT})) (width 0) (fill yes))"
)


def _rot(x: float, y: float, angle_deg: float) -> tuple[float, float]:
    """KiCad's footprint transform: the stored angle is negated."""
    a = math.radians(-angle_deg)
    return x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)


def _board(
    tmp_path: Path,
    *,
    rotation: float,
    side: str,
    track_local: tuple[float, float] | None = None,
    tht_pad: bool = False,
    silk_local: tuple[float, float] | None = None,
    primitive: str | None = None,
) -> Path:
    """A probe board: the SOT-89-3 fixture at ``rotation`` on ``side``.

    ``track_local`` places a different-net 0.2 mm track from a small SMD pad
    at pad-local (7.0, track_y) to the given pad-local point.  ``tht_pad``
    adds a through-hole pad 1.2 mm from the tab's side edge in its own
    footprint; ``silk_local`` puts a silk text at that pad-local point.
    ``primitive`` overrides the tab's gr_poly (refused-shape tests).
    """
    cu, mask, silk = f"{side}.Cu", f"{side}.Mask", f"{side}.SilkS"
    fx, fy = 110.0, 110.0
    ax, ay = _rot(*PAD_AT, rotation)
    anchor = (fx + ax, fy + ay)  # board position of the custom pad's anchor

    def at(p: tuple[float, float]) -> tuple[float, float]:
        rx, ry = _rot(p[0], p[1], rotation)
        return anchor[0] + rx, anchor[1] + ry

    tab = TAB_POLY if primitive is None else primitive
    parts = [
        f"""(footprint "Package_TO_SOT_SMD:SOT-89-3" (layer "{cu}") (at {fx} {fy} {rotation})
  (property "Reference" "Q1" (at 0 -3.5) (layer "{silk}"))
  (pad "1" smd rect (at -1.5 -1.5 {rotation}) (size 1.0 0.5) (layers "{cu}") (net 1 "GND"))
  (pad "2" smd custom (at {PAD_AT[0]} {PAD_AT[1]} {rotation}) (size 1.5 0.9)
   (layers "{cu}" "{mask}") (net 2 "SIG")
   (options (clearance outline) (anchor rect))
   (primitives {tab}))
  (pad "3" smd rect (at -1.5 1.5 {rotation}) (size 1.0 0.5) (layers "{cu}") (net 3 "VCC")))"""
    ]
    tail: list[str] = []
    if track_local is not None:
        tx, ty = track_local
        r1 = at((7.0, ty))
        end = at((tx, ty))
        parts.append(
            f"""(footprint "Resistor_SMD:R_0603" (layer "{cu}") (at {r1[0]:.6f} {r1[1]:.6f})
  (property "Reference" "R1" (at 0 0) (layer "{silk}"))
  (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "{cu}") (net 4 "NSIG")))"""
        )
        tail.append(
            f"(segment (start {r1[0]:.6f} {r1[1]:.6f}) (end {end[0]:.6f} {end[1]:.6f})"
            f' (width {TRACK_WIDTH}) (layer "{cu}") (net 4))'
        )
    if tht_pad:
        tp = at((5.8125, 2.0))
        parts.append(
            f"""(footprint "Connector_PinHeader_2.54mm:PinHeader_1x01" (layer "{cu}")
  (at {tp[0]:.6f} {tp[1]:.6f})
  (property "Reference" "J9" (at 0 0) (layer "{silk}"))
  (pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.9)
   (layers "*.Cu" "*.Mask") (net 5 "VT")))"""
        )
    if silk_local is not None:
        st = at(silk_local)
        tail.append(
            f'(gr_text "TAB" (at {st[0]:.6f} {st[1]:.6f}) (layer "{silk}")'
            " (effects (font (size 1.0 1.0) (thickness 0.15))))"
        )
    text = f"""(kicad_pcb (version 20240108) (generator "test")
 (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
 (setup (pad_to_mask_clearance 0))
 (net 0 "") (net 1 "GND") (net 2 "SIG") (net 3 "VCC") (net 4 "NSIG") (net 5 "VT")
 (gr_rect (start 90 90) (end 130 130) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
 {"".join(parts)}
 {"".join(tail)})"""
    path = tmp_path / f"sot89-{rotation}-{side}.kicad_pcb"
    path.write_text(text)
    return path


def _run(board: Path, capsys, *extra: str) -> dict:
    capsys.readouterr()
    main([str(board), "--mfr", "jlcpcb", "--layers", "2", "--format", "json", *extra])
    out = capsys.readouterr().out
    assert out.strip(), "kct check must emit a report, not abort with empty stdout"
    return json.loads(out)


def _violations(report: dict, rule_id: str) -> list[dict]:
    return [v for v in report["violations"] if v["rule_id"] == rule_id]


def _clearance_on_custom_pad(report: dict) -> list[dict]:
    """Clearance findings whose items or message name the custom pad (Q1-2)."""
    return [
        v
        for v in report["violations"]
        if v["rule_id"].startswith("clearance")
        and ("Q1-2" in v["message"] or any("Q1-2" in i for i in v["items"]))
    ]


# The five probe cases, as the different-net track's pad-local endpoint and
# the edge-to-edge gap it leaves against the tab (None: clean).  The side-gap
# case runs alongside the tab at the listed y; the rest stay on its axis.
_CASES = [
    pytest.param((2.8625, 0.0), 0.0, True, id="inside-tab-short"),
    pytest.param((4.0125, 0.0), 0.05, False, id="end-gap-0.05"),
    pytest.param((1.0, 0.6), 0.05, False, id="side-gap-0.05"),
    pytest.param((4.0725, 0.0), 0.11, False, id="end-gap-0.11"),
    pytest.param((4.4625, 0.0), None, False, id="clear-0.5"),
]
_SIDE_GAP_Y = 0.6  # 0.45 (tab half height) + 0.05 + half the 0.2 mm track


@pytest.mark.parametrize(("rotation", "side"), PLACEMENTS)
@pytest.mark.parametrize(("track_local", "expected_gap", "expect_short"), _CASES)
def test_probe_case_matches_kicad_cli_finding(
    tmp_path, capsys, rotation, side, track_local, expected_gap, expect_short
):
    """A clearance error against the tab at all 8 placements; 0.5 mm stays clean."""
    track = (track_local[0], _SIDE_GAP_Y if track_local[1] else 0.0)
    report = _run(
        _board(tmp_path, rotation=rotation, side=side, track_local=track),
        capsys,
        "--only",
        "clearance",
    )
    found = _clearance_on_custom_pad(report)
    if expected_gap is None:
        assert not found, "0.5 mm from the tab is clean at every placement"
        return
    assert found, f"expected a clearance finding against the tab (gap {expected_gap})"
    assert all(v["severity"] == "error" for v in found)
    if expect_short:
        assert any("SHORT" in v["message"] for v in found), "overlap must promote to SHORT"
    else:
        assert all(v["actual_value"] is not None and v["actual_value"] < 0.127 for v in found), (
            f"every finding must measure below the 0.127 mm floor, not {found}"
        )


@pytest.mark.parametrize(("rotation", "side"), PLACEMENTS)
def test_silk_over_tab_is_reported(tmp_path, capsys, rotation, side):
    """A silk item over the tab: ``silk_over_copper`` warning naming the pad."""
    report = _run(
        _board(tmp_path, rotation=rotation, side=side, silk_local=(2.2, 0.0)),
        capsys,
        "--only",
        "silkscreen",
    )
    found = [v for v in _violations(report, "silk_over_copper") if "Q1 pad 2" in v["message"]]
    assert found, "silk over the tab must be reported, not silently cleared"
    assert all(v["severity"] == "warning" for v in found)


@pytest.mark.parametrize(("rotation", "side"), PLACEMENTS)
def test_silk_clear_of_tab_stays_clean(tmp_path, capsys, rotation, side):
    """Control: the same text 6 mm past the tab reports nothing for the pad."""
    report = _run(
        _board(tmp_path, rotation=rotation, side=side, silk_local=(9.0, 2.5)),
        capsys,
        "--only",
        "silkscreen",
    )
    assert not [v for v in _violations(report, "silk_over_copper") if "Q1 pad 2" in v["message"]]


@pytest.mark.parametrize(("rotation", "side"), PLACEMENTS)
def test_tht_pad_near_tab_is_judged(tmp_path, capsys, rotation, side):
    """A THT pad ~1.2 mm from the tab is an error (danger limit 2.032 mm)."""
    report = _run(
        _board(tmp_path, rotation=rotation, side=side, tht_pad=True),
        capsys,
        "--only",
        "tht_smd_spacing",
    )
    found = [
        v
        for v in _violations(report, "tht_smd_spacing")
        if any(i.startswith("Q1:2") for i in v["items"])
    ]
    assert found, "THT-to-custom-tab spacing must be measured, not dropped"
    assert all(v["severity"] == "error" for v in found)
    assert all(1.0 < v["actual_value"] < 2.032 for v in found)


@pytest.mark.parametrize(("rotation", "side"), PLACEMENTS)
def test_short_against_tab_across_rules(tmp_path, capsys, rotation, side):
    """One board, all four rule families: short, silk and THT all reported.

    The default (no ``--only``) run aborts on the custom pad itself -- that is
    issue #9468 / PR #9470's scope, not this issue's -- so the combined run
    selects the rule families this issue changed.
    """
    report = _run(
        _board(
            tmp_path,
            rotation=rotation,
            side=side,
            track_local=(2.8625, 0.0),
            tht_pad=True,
            silk_local=(2.2, 0.0),
        ),
        capsys,
        "--only",
        "clearance,silkscreen,tht_smd_spacing",
    )
    assert _clearance_on_custom_pad(report), "combined run must catch the tab short"
    assert _violations(report, "silk_over_copper")
    assert _violations(report, "tht_smd_spacing")


def test_physical_copper_gap_sees_tab(tmp_path):
    """``physical_copper_gap`` measures a 0.1 mm slit against the tab."""
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.sexp import parse_string
    from kicad_tools.validate.rules.physical_gap import check_physical_copper_gap

    board = _board(
        tmp_path,
        rotation=30.0,
        side="F",
        track_local=(TAB_FAR + 0.10 + TRACK_WIDTH / 2, 0.0),
    )
    results = check_physical_copper_gap(PCB(parse_string(board.read_text())), 0.3)
    gaps = [v for v in results.violations if v.rule_id == "physical_copper_gap"]
    assert gaps, "a 0.1 mm slit to the tab must be measured, not missed"
    assert all(v.actual_value < 0.15 for v in gaps)
    assert not [v for v in results.violations if v.rule_id == "physical_copper_gap_incomplete"]


def test_refused_primitive_reports_named_incomplete_findings(tmp_path, capsys):
    """AC2: each rule names the pad and the refusing primitive kind."""
    board = _board(
        tmp_path,
        rotation=30.0,
        side="F",
        track_local=(2.8625, 0.0),
        tht_pad=True,
        silk_local=(2.2, 0.0),
        primitive=GR_CIRCLE_PRIMITIVE,
    )
    report = _run(
        board,
        capsys,
        "--only",
        "clearance,silkscreen,tht_smd_spacing,physical_copper_gap",
        "--physical-copper-gap",
        "0.3",
    )
    expected = {
        "clearance_incomplete": "info",
        "silk_over_copper_incomplete": "info",
        "tht_smd_spacing_incomplete": "info",
        # The physical-gap module's existing incomplete channel is error.
        "physical_copper_gap_incomplete": "error",
    }
    for rule_id, severity in expected.items():
        found = _violations(report, rule_id)
        assert found, f"{rule_id} must be reported for the refused custom pad"
        assert all(v["severity"] == severity for v in found)
        assert all("gr_circle" in v["message"] for v in found), (
            f"{rule_id} must name the refusing primitive kind"
        )
        assert all("Q1" in v["message"] for v in found), f"{rule_id} must name the pad"


def test_custom_pad_without_primitives_is_measured(tmp_path, capsys):
    """An anchor-only custom pad (no primitives) has exact copper: measured."""
    board = _board(tmp_path, rotation=0.0, side="F", primitive="")
    report = _run(board, capsys, "--only", "tht_smd_spacing")
    assert not _violations(report, "tht_smd_spacing_incomplete"), (
        "a custom pad with no primitives is its exact anchor, not missing coverage"
    )
