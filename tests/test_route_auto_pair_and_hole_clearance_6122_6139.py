"""route-auto's foreign-copper gate: per-pair clearance (#6122) and holes (#6139).

Follow-ups to #6107.  The gate used to measure new copper at one board-wide
clearance, and not against drilled holes at all:

* #6139 -- a corridor strategy (``global``) wrote a track straight through a
  bare NPTH hole or slot.  KiCad reports ``hole_clearance``, and for a slot
  also ``copper_edge_clearance`` (a non-plated slot is board edge).
* #6122 -- the single clearance was too loose for an ``HV`` netclass and for
  conditional ``.kicad_dru`` rules, and too strict when an unconditional
  custom rule is below ``Default`` (KiCad lets the custom rule win).

The centrepiece is :func:`test_gate_agrees_with_kicad_cli`: one scenario per
rule, each a single new ``/B`` track or via, where the gate's verdict must
match ``kicad-cli pcb drc``'s on the same board -- except the one scenario
whose condition the gate cannot evaluate, where it must err toward refusing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.mcp.tools.routing import route_net_auto
from kicad_tools.router.board_clearance_rules import (
    BoardClearanceRules,
    DruRule,
    ItemProps,
    evaluate_condition,
    read_dru_rules,
)
from kicad_tools.router.foreign_copper import board_holes, board_required_clearance
from kicad_tools.router.layers import Layer
from kicad_tools.router.orchestrator import RoutingOrchestrator
from kicad_tools.router.primitives import Segment, Via
from kicad_tools.router.strategies import RoutingResult, RoutingStrategy
from kicad_tools.schema.pcb import PCB

FIXTURES = Path(__file__).parent / "fixtures" / "route_auto_6107"
PAD = FIXTURES / "pad_on_path.kicad_pcb"
NEAR = FIXTURES / "track_near_path.kicad_pcb"
ORIGIN = (100.0, 100.0)  # the fixtures' Edge.Cuts corner

R5_PAD = (
    '(pad "1" smd rect (at 0 0) (size 3.0 1.0) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "/A"))'
)
ROUND_NPTH = (
    '(pad "" np_thru_hole circle (at 0 0) (size 3.0 3.0) (drill 3.0) (layers "*.Cu" "*.Mask"))'
)
OVAL_NPTH = (
    '(pad "" np_thru_hole oval (at 0 0) (size 3.0 1.0) (drill oval 3.0 1.0) '
    '(layers "*.Cu" "*.Mask"))'
)
# A plated /A pad with a 0.1 mm ring round a 3.0 mm drill.
PTH_A = (
    '(pad "1" thru_hole circle (at 0 0) (size 3.2 3.2) (drill 3.0) '
    '(layers "*.Cu" "*.Mask") (net 1 "/A"))'
)
# The fleet's JLCPCB rule (boards/*/*.kicad_dru), keyed on Pad_Type.
JLC_PTH_HOLE_RULE = (
    '(rule "PTH Hole to Track" (condition "(A.Pad_Type == \'Through-hole\' && '
    "B.Type == 'Track') || (B.Pad_Type == 'Through-hole' && A.Type == 'Track')\")"
    " (constraint hole_clearance (min 0.28mm)))"
)

HV_PRO = {
    "net_settings": {
        "classes": [{"name": "Default", "clearance": 0.1}, {"name": "HV", "clearance": 0.5}],
        "netclass_patterns": [{"netclass": "HV", "pattern": "/A"}],
    }
}


def _default_pro(clearance: float) -> dict:
    return {"net_settings": {"classes": [{"name": "Default", "clearance": clearance}]}}


def _dru(*rules: str) -> str:
    return "(version 1)\n" + "\n".join(rules) + "\n"


def _board(
    tmp_path: Path,
    base: Path,
    *,
    pad: str | None = None,
    pro: dict | None = None,
    dru: str | None = None,
    name: str = "board",
) -> Path:
    text = base.read_text()
    if pad is not None:
        assert R5_PAD in text, "fixture pad line changed; update the replacement"
        text = text.replace(R5_PAD, pad)
    path = tmp_path / f"{name}.kicad_pcb"
    path.write_text(text)
    if pro is not None:
        path.with_suffix(".kicad_pro").write_text(json.dumps(pro))
    if dru is not None:
        path.with_suffix(".kicad_dru").write_text(dru)
    return path


def _orchestrator(path: Path) -> RoutingOrchestrator:
    from kicad_tools.router.io import detect_layer_stack, parse_pcb_design_rules

    text = path.read_text()
    return RoutingOrchestrator(
        pcb=PCB.load(str(path)),  # type: ignore[arg-type]
        rules=parse_pcb_design_rules(text).to_design_rules(),
        layer_stack=detect_layer_stack(text),
    )


def _b_track(x: float) -> Segment:
    """A vertical 0.2 mm /B track at board-relative x, y 2..14 (pads R3/R4)."""
    return Segment(x, 2.0, x, 14.0, 0.2, Layer.F_CU, 2, "/B")


def _gate_refuses(path: Path, segments=(), vias=()) -> tuple[bool, str]:
    orchestrator = _orchestrator(path)
    result = RoutingResult(
        success=True,
        net="/B",
        strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
        segments=list(segments),
        vias=list(vias),
    )
    orchestrator._enforce_no_foreign_shorts(result, RoutingStrategy.GLOBAL_WITH_REPAIR)
    return (not result.success), (result.error_message or "")


_DRC_TYPES = {
    "clearance",
    "hole_clearance",
    "copper_edge_clearance",
    "shorting_items",
    "tracks_crossing",
}


def _kicad_flags(cli: Path, path: Path, segments=(), vias=()) -> set[str]:
    """Write the new copper into a copy and return the copper-rule violations kicad-cli reports."""
    ox, oy = ORIGIN
    extra = [
        f"  (segment (start {s.x1 + ox} {s.y1 + oy}) (end {s.x2 + ox} {s.y2 + oy}) "
        f'(width {s.width}) (layer "F.Cu") (net 2))'
        for s in segments
    ] + [
        f"  (via (at {v.x + ox} {v.y + oy}) (size {v.diameter}) (drill {v.drill}) "
        f'(layers "F.Cu" "B.Cu") (net 2))'
        for v in vias
    ]
    text = path.read_text().rstrip()
    assert text.endswith(")")
    probe = path.with_name(path.stem + "_drc.kicad_pcb")
    probe.write_text(text[:-1] + "\n".join(extra) + "\n)\n")
    for suffix in (".kicad_pro", ".kicad_dru"):
        if path.with_suffix(suffix).exists():
            shutil.copy(path.with_suffix(suffix), probe.with_suffix(suffix))
    report = probe.with_suffix(".json")
    subprocess.run(
        [str(cli), "pcb", "drc", "--format", "json", "-o", str(report), str(probe)],
        capture_output=True,
        check=False,
        timeout=120,
    )
    doc = json.loads(report.read_text())
    return {v["type"] for v in doc.get("violations", []) if v["type"] in _DRC_TYPES}


# ---------------------------------------------------------------------------
# The gate agrees with kicad-cli
# ---------------------------------------------------------------------------

# Distances: /A's track (NEAR) has its left edge at x = 15.15; a 0.2 mm /B
# track at x has a gap of 15.05 - x.  The holes (PAD with R5.1 replaced) are
# centred on (15, 8) and reach x = 16.5; a /B track at x has a gap of x - 16.6.
SCENARIOS = [
    # id, base, pad, pro, dru, segment x, via, gate refuses, kicad flags
    # --- #6139: holes ---
    ("round-hole-crossed", PAD, ROUND_NPTH, None, None, 15.0, None, True, True),
    ("slot-crossed", PAD, OVAL_NPTH, None, None, 15.0, None, True, True),
    ("round-hole-0.20mm", PAD, ROUND_NPTH, None, None, 16.8, None, True, True),
    ("round-hole-0.30mm", PAD, ROUND_NPTH, None, None, 16.9, None, False, False),
    ("slot-0.30mm-edge", PAD, OVAL_NPTH, None, None, 16.9, None, True, True),
    ("slot-0.60mm", PAD, OVAL_NPTH, None, None, 17.2, None, False, False),
    (
        "dru-hole-rule-below-board-min",
        PAD,
        ROUND_NPTH,
        None,
        _dru('(rule "h" (constraint hole_clearance (min 0.1mm)))'),
        16.8,
        None,
        False,
        False,
    ),
    # --- #6122: per-pair clearance ---
    ("default-0.15mm", NEAR, None, None, None, 14.9, None, True, True),
    ("hv-class-0.30mm", NEAR, None, HV_PRO, None, 14.75, None, True, True),
    ("hv-class-0.60mm", NEAR, None, HV_PRO, None, 14.45, None, False, False),
    (
        "smaller-custom-rule-wins",
        NEAR,
        None,
        _default_pro(0.2),
        _dru('(rule "c" (constraint clearance (min 0.1mm)))'),
        14.9,
        None,
        False,
        False,
    ),
    (
        "conditional-netclass-rule-overrides-hv",
        NEAR,
        None,
        HV_PRO,
        _dru('(rule "hv" (condition "A.NetClass == \'HV\'") (constraint clearance (min 0.12mm)))'),
        14.9,
        None,
        False,
        False,
    ),
    (
        "conditional-netname-rule-b-side",
        NEAR,
        None,
        _default_pro(0.1),
        _dru('(rule "n" (condition "A.NetName == \'/B\'") (constraint clearance (min 0.3mm)))'),
        14.8,
        None,
        True,
        True,
    ),
    (
        "conditional-rule-not-matching",
        NEAR,
        None,
        _default_pro(0.1),
        _dru('(rule "n" (condition "A.NetName == \'/C\'") (constraint clearance (min 0.3mm)))'),
        14.8,
        None,
        False,
        False,
    ),
    (
        "last-rule-wins",
        NEAR,
        None,
        _default_pro(0.1),
        _dru(
            '(rule "a" (constraint clearance (min 0.3mm)))',
            '(rule "b" (constraint clearance (min 0.1mm)))',
        ),
        14.9,
        None,
        False,
        False,
    ),
    (
        "unevaluable-small-rule-keeps-default",
        NEAR,
        None,
        _default_pro(0.2),
        _dru(
            '(rule "u" (condition "A.intersectsArea(\'west\')") (constraint clearance (min 0.1mm)))'
        ),
        14.9,
        None,
        True,
        True,
    ),
    # Conservative by design: the gate cannot evaluate the area condition, so
    # it applies the rule's larger value; KiCad finds the rule does not match.
    (
        "unevaluable-large-rule-is-conservative",
        NEAR,
        None,
        _default_pro(0.2),
        _dru(
            '(rule "u" (condition "A.intersectsArea(\'west\')") (constraint clearance (min 0.4mm)))'
        ),
        14.75,
        None,
        True,
        False,
    ),
    # A new via's drill is measured against other nets' copper at
    # hole_clearance: a 0.6 / 0.5 mm via 0.15 mm (copper) from /A's track has
    # its drill 0.2 mm away, legal for the 0.1 mm copper rule but not 0.25 mm.
    (
        "new-via-drill-hole-clearance",
        NEAR,
        None,
        None,
        _dru('(rule "c" (constraint clearance (min 0.1mm)))'),
        None,
        (14.7, 8.0),
        True,
        True,
    ),
    # The fleet's Pad_Type-keyed hole rule: 0.28 mm from a plated drill, while
    # an NPTH hole stays at the 0.25 mm board minimum.  A /B track 0.26 mm
    # from the drill (0.16 mm from the PTH ring; Default is 0.1 mm).
    (
        "jlc-pth-hole-rule-plated",
        PAD,
        PTH_A,
        _default_pro(0.1),
        _dru(JLC_PTH_HOLE_RULE),
        16.86,
        None,
        True,
        True,
    ),
    (
        "jlc-pth-hole-rule-npth",
        PAD,
        ROUND_NPTH,
        _default_pro(0.1),
        _dru(JLC_PTH_HOLE_RULE),
        16.86,
        None,
        False,
        False,
    ),
]


@pytest.mark.parametrize(
    "base,pad,pro,dru,x,via,gate_refuses,kicad_flags",
    [pytest.param(*s[1:], id=s[0]) for s in SCENARIOS],
)
def test_gate_agrees_with_kicad_cli(
    tmp_path, base, pad, pro, dru, x, via, gate_refuses, kicad_flags
) -> None:
    path = _board(tmp_path, base, pad=pad, pro=pro, dru=dru)
    segments = [_b_track(x)] if x is not None else []
    vias = [Via(via[0], via[1], 0.5, 0.6, (Layer.F_CU, Layer.B_CU), 2, "/B")] if via else []

    refused, message = _gate_refuses(path, segments, vias)
    assert refused is gate_refuses, message

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not installed; gate verdict checked, KiCad's not")
    flags = _kicad_flags(Path(cli), path, segments, vias)
    assert bool(flags) is kicad_flags, flags


def test_hole_refusal_names_the_rule(tmp_path) -> None:
    path = _board(tmp_path, PAD, pad=OVAL_NPTH)
    refused, message = _gate_refuses(path, [_b_track(16.9)])
    assert refused
    assert "comes within 0.300 mm of drilled hole(s) (NPTH slot of pad R5" in message
    assert "0.500 mm board-edge clearance (an NPTH slot is board edge)" in message

    refused, message = _gate_refuses(path, [_b_track(15.0)])
    assert "crosses drilled hole(s)" in message and "0.250 mm hole clearance" in message


def test_hv_refusal_quotes_the_pair_requirement(tmp_path) -> None:
    path = _board(tmp_path, NEAR, pro=HV_PRO)
    refused, message = _gate_refuses(path, [_b_track(14.75)])
    assert refused
    assert "comes within 0.300 mm of net(s) '/A'" in message
    assert "below the 0.500 mm clearance that pair requires" in message


def test_own_net_plated_hole_is_exempt(tmp_path) -> None:
    """A track into its own net's through-hole pad crosses that pad's drill legally."""
    pth = (
        '(pad "1" thru_hole circle (at 0 0) (size 2.0 2.0) (drill 1.0) '
        '(layers "*.Cu" "*.Mask") (net 2 "/B"))'
    )
    path = _board(tmp_path, PAD, pad=pth)
    refused, message = _gate_refuses(path, [_b_track(15.0)])
    assert not refused, message


def test_foreign_plated_hole_is_reported_once(tmp_path) -> None:
    """A track across another net's PTH pad is a short; its drill is not a second finding."""
    pth = (
        '(pad "1" thru_hole circle (at 0 0) (size 2.0 2.0) (drill 1.0) '
        '(layers "*.Cu" "*.Mask") (net 1 "/A"))'
    )
    path = _board(tmp_path, PAD, pad=pth)
    orchestrator = _orchestrator(path)
    result = RoutingResult(
        success=True,
        net="/B",
        strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
        segments=[_b_track(15.0)],
    )
    report = orchestrator._foreign_copper_conflicts(result)
    assert [c.kind for c in report.conflicts] == ["short"]


def test_board_holes_reads_round_slot_and_via_drills(tmp_path) -> None:
    pcb = PCB.load(str(_board(tmp_path, PAD, pad=OVAL_NPTH)))
    [slot] = [h for h in board_holes(pcb) if h.owner == "pad" and "R5" in h.label]
    assert slot.slot_edge and slot.plated is False and slot.net == 0
    # 3.0 x 1.0 mm oval centred on (15, 8).
    assert slot.bbox == pytest.approx((13.5, 7.5, 16.5, 8.5))

    pcb = PCB.load(str(_board(tmp_path, PAD, pad=ROUND_NPTH, name="round")))
    [hole] = [h for h in board_holes(pcb) if "R5" in h.label]
    assert not hole.slot_edge
    assert hole.bbox == pytest.approx((13.5, 6.5, 16.5, 9.5))


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pad", [ROUND_NPTH, OVAL_NPTH], ids=["round", "slot"])
def test_global_through_a_hole_is_refused_and_not_written(tmp_path, pad) -> None:
    """The #6139 repro: ``--strategy global`` straight through R5's hole."""
    src = _board(tmp_path, PAD, pad=pad)
    out = tmp_path / "global.kicad_pcb"
    result = route_net_auto(str(src), "/B", output_path=str(out), strategy="global")
    assert result["success"] is False
    assert "drilled hole" in (result.get("error_message") or "")
    assert not out.exists()


def _drc_types(cli: Path, board: Path) -> set[str]:
    report = board.with_suffix(".json")
    subprocess.run(
        [str(cli), "pcb", "drc", "--format", "json", "-o", str(report), str(board)],
        capture_output=True,
        check=False,
        timeout=120,
    )
    doc = json.loads(report.read_text())
    return {v["type"] for v in doc.get("violations", []) if v["type"] in _DRC_TYPES}


@pytest.mark.parametrize("pad", [ROUND_NPTH, OVAL_NPTH], ids=["round", "slot"])
def test_hierarchical_routes_around_a_hole_drc_clean(tmp_path, pad) -> None:
    src = _board(tmp_path, PAD, pad=pad)
    out = tmp_path / "hier.kicad_pcb"
    result = route_net_auto(str(src), "/B", output_path=str(out), strategy="hierarchical")
    assert result["success"] is True, result.get("error_message")
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not installed")
    assert _drc_types(Path(cli), out) == set()


def test_hv_netclass_end_to_end_drc_clean(tmp_path) -> None:
    """An HV class on /A: whatever route-auto writes for /B keeps 0.5 mm from it."""
    src = _board(tmp_path, NEAR, pro=HV_PRO)
    out = tmp_path / "hv.kicad_pcb"
    result = route_net_auto(str(src), "/B", output_path=str(out), strategy="auto")
    if not result["success"]:
        assert not out.exists()
        return
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not installed")
    shutil.copy(src.with_suffix(".kicad_pro"), out.with_suffix(".kicad_pro"))
    assert _drc_types(Path(cli), out) == set()


def test_hierarchical_routes_at_the_nets_pair_requirement(tmp_path) -> None:
    """The board-loaded grid routes /B at the HV pair clearance, not Default's."""
    orchestrator = _orchestrator(_board(tmp_path, NEAR, pro=HV_PRO))
    board_nets = {n.name for n in orchestrator.pcb.nets.values() if n.name}
    path = orchestrator._board_path()
    assert path is not None
    router, _ = orchestrator._load_board_router(path, "/B", board_nets)
    assert router.rules.trace_clearance == pytest.approx(0.5)
    # The board-wide figure is still Default's.
    assert orchestrator._required_clearance() == pytest.approx(0.1)


def test_required_clearance_lets_a_smaller_custom_rule_win(tmp_path) -> None:
    path = _board(
        tmp_path,
        NEAR,
        pro=_default_pro(0.2),
        dru=_dru('(rule "c" (constraint clearance (min 0.1016mm)))'),
    )
    assert board_required_clearance(path, 0.15) == pytest.approx(0.1016)


# ---------------------------------------------------------------------------
# The rule model
# ---------------------------------------------------------------------------


def _rules(**kwargs) -> BoardClearanceRules:
    return BoardClearanceRules(**kwargs)


def test_netclass_resolution() -> None:
    rules = _rules(
        class_clearance={"Default": 0.15, "HV": 0.5, "PWR": 0.3},
        netclass_assignments={"/MAINS": "HV"},
        netclass_patterns=[("PWR", "+*V"), ("HV", "^/HV_.*$")],
    )
    assert rules.netclasses_of("/MAINS") == ("HV",)
    assert rules.netclasses_of("+5V") == ("PWR",)
    assert rules.netclasses_of("/HV_IN") == ("HV",)
    assert rules.netclasses_of("/SIG") == ("Default",)
    assert rules.netclasses_of("") == ("Default",)
    assert rules.netclasses_of(None) is None

    track = ItemProps("Track", "/SIG")
    assert rules.clearance(track, ItemProps("Pad", "/MAINS")) == pytest.approx(0.5)
    assert rules.clearance(track, ItemProps("Pad", "+5V")) == pytest.approx(0.3)
    assert rules.clearance(track, ItemProps("Pad", "/OTHER")) == pytest.approx(0.15)
    # An unknown net may be in any class.
    assert rules.clearance(track, ItemProps("Pad", None)) == pytest.approx(0.5)


def test_board_minimum_floors_netclass_but_not_custom_rules() -> None:
    a, b = ItemProps("Track", "/A"), ItemProps("Track", "/B")
    floored = _rules(class_clearance={"Default": 0.1}, min_clearance=0.2)
    assert floored.clearance(a, b) == pytest.approx(0.2)
    overridden = _rules(
        class_clearance={"Default": 0.1},
        min_clearance=0.2,
        dru_rules=[DruRule("c", None, None, (("clearance", 0.12),))],
    )
    assert overridden.clearance(a, b) == pytest.approx(0.12)


def test_layer_clause() -> None:
    rules = _rules(dru_rules=[DruRule("o", None, "outer", (("clearance", 0.4),))])
    a, b = ItemProps("Track", "/A"), ItemProps("Track", "/B")
    assert rules.clearance(a, b, "F.Cu") == pytest.approx(0.4)
    assert rules.clearance(a, b, "In1.Cu") == pytest.approx(0.2)
    assert rules.clearance(a, b, None) == pytest.approx(0.4)  # a via: unknown -> larger


def test_net_requirement_is_the_largest_pair() -> None:
    rules = _rules(
        class_clearance={"Default": 0.15, "HV": 0.5},
        netclass_assignments={"/MAINS": "HV"},
    )
    assert rules.net_requirement("/SIG", {"/OTHER"}) == pytest.approx(0.15)
    assert rules.net_requirement("/SIG", {"/OTHER", "/MAINS"}) == pytest.approx(0.5)


@pytest.mark.parametrize(
    "condition,expected",
    [
        ("A.NetClass == 'HV'", True),
        ("A.NetClass != 'HV'", False),
        ("A.NetName == '/MAINS'", True),
        ("A.NetName == '/MA*'", True),
        ("B.NetName == '/SIG' && A.Type == 'Pad'", True),
        ("A.Type == 'Track' || B.Type == 'Track'", True),
        ("!(A.Type == 'Via')", True),
        ("A.isPlated()", False),
        ("A.hasNetclass('HV')", True),
        ("A.hasNetclass('Default')", False),
        ("A.insideArea('x')", None),
        ("A.insideArea('x') || A.NetClass == 'HV'", True),
        ("A.insideArea('x') && A.NetClass == 'Default'", False),
        ("A.Layer == 'F.Cu'", None),
        ("A.NetName == '/mains'", True),  # KiCad compares case-insensitively
        ("A.NetName == '/S?G'", False),
        ("this is not ( valid", None),
        ("A.Pad_Type == 'NPTH, mechanical'", True),
        ("A.Pad_Type == 'NPTH'", False),
        # A track has no Pad_Type: KiCad's null is false under == and != alike.
        ("B.Pad_Type == 'Through-hole'", False),
        ("B.Pad_Type != 'Through-hole'", False),
        ("!(B.Pad_Type == 'Through-hole')", True),
    ],
)
def test_evaluate_condition(condition, expected) -> None:
    rules = _rules(class_clearance={"HV": 0.5}, netclass_assignments={"/MAINS": "HV"})
    a = ItemProps("Pad", "/MAINS", plated=False, pad_type="NPTH, mechanical")
    b = ItemProps("Track", "/SIG")
    assert evaluate_condition(condition, a, b, rules) is expected


def test_read_dru_rules(tmp_path) -> None:
    dru = tmp_path / "b.kicad_dru"
    dru.write_text(
        _dru(
            '(rule "hv" (layer outer) (condition "A.NetClass == \'HV\'")\n'
            "  (constraint clearance (min 0.5mm)) (constraint hole_clearance (min 0.4mm)))",
            '(rule "w" (constraint track_width (min 0.2mm)))',
            '(rule "e" (constraint edge_clearance (min 10mil)))',
        )
    )
    rules = read_dru_rules(dru)
    assert [r.name for r in rules] == ["hv", "e"]
    assert rules[0].layer == "outer"
    assert rules[0].condition == "A.NetClass == 'HV'"
    assert rules[0].value("hole_clearance") == pytest.approx(0.4)
    assert rules[1].value("edge_clearance") == pytest.approx(0.254)
    assert read_dru_rules(tmp_path / "missing.kicad_dru") == []


def test_gate_item_types_match_kicads() -> None:
    """KiCad reports an arc's ``Type`` as ``'Track'`` (kicad-cli 10.0.1)."""
    from kicad_tools.router.board_clearance_rules import item_props_for_type

    assert item_props_for_type("arc", "/A").type == "Track"
    assert item_props_for_type("pad", "/A", True, "thru_hole").pad_type == "Through-hole"
    assert item_props_for_type("track", "/A", None, "smd").pad_type is None
