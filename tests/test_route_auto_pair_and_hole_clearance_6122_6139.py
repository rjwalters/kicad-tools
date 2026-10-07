"""route-auto's foreign-copper gate: per-pair clearance (#6122), holes (#6139)
and the #6150 follow-ups.

Follow-ups to #6107.  The gate used to measure new copper at one board-wide
clearance, and not against drilled holes at all:

* #6139 -- a corridor strategy (``global``) wrote a track straight through a
  bare NPTH hole or slot.  KiCad reports ``hole_clearance``, and for a slot
  also ``copper_edge_clearance`` (a non-plated slot is board edge).
* #6122 -- the single clearance was too loose for an ``HV`` netclass and for
  conditional ``.kicad_dru`` rules, and too strict when an unconditional
  custom rule is below ``Default`` (KiCad lets the custom rule win).
* #6150 -- ``(severity ignore)`` rules, ``.kicad_dru`` files KiCad discards
  whole, ``A.Layer``, and the ``hole_to_hole``, ``physical_clearance`` and
  ``physical_hole_clearance`` constraints.

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
    KICAD_CONSTRAINT_KEYWORDS,
    BoardClearanceRules,
    DruRule,
    ItemProps,
    evaluate_condition,
    parse_dru,
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

# physical_clearance holds within a net too (KiCad flags /B's track against
# its own R3 / R4 pads), so the parity rules are scoped away from /B's own
# copper.
PHYS_A = (
    "(version 1)\n(rule \"p\" (condition \"A.NetName == '/A' || B.NetName == '/A'\")"
    " (constraint physical_clearance (min 0.3mm)))\n"
)
PHYS_NPTH = (
    "(version 1)\n"
    '(rule "h" (constraint hole_clearance (min 0.01mm)))\n'
    "(rule \"p\" (condition \"(A.Type == 'Track' && B.Pad_Type == 'NPTH, mechanical') || "
    "(B.Type == 'Track' && A.Pad_Type == 'NPTH, mechanical')\")"
    " (constraint physical_clearance (min 0.3mm)))\n"
)
# hole_clearance down to 0.01 mm, so a via's copper may come close to a hole
# and only hole_to_hole is measured.
H_SMALL_RULE = '(rule "h" (constraint hole_clearance (min 0.01mm)))'
H_SMALL = "(version 1)\n" + H_SMALL_RULE + "\n"

HV_PRO = {
    "net_settings": {
        "classes": [{"name": "Default", "clearance": 0.1}, {"name": "HV", "clearance": 0.5}],
        "netclass_patterns": [{"netclass": "HV", "pattern": "/A"}],
    }
}


# A plated /B pad: a 1.0 mm drill in a 2.0 mm ring, centred on (15, 8).
PTH_B = (
    '(pad "1" thru_hole circle (at 0 0) (size 2.0 2.0) (drill 1.0) '
    '(layers "*.Cu" "*.Mask") (net 2 "/B"))'
)
# Not a KiCad constraint: kicad-cli 10.0.1 discards the whole file (#4999).
SOLDER_MASK_MARGIN_RULE = (
    '(rule "Solder Mask Clearance - jlcpcb" (constraint solder_mask_margin (min 0.05mm)))'
)


#: ``kicad_flags`` of a scenario KiCad judges differently by release: flagged
#: from this ``kicad-cli`` version on, not before (#6196).  KiCad 10.0.2 is
#: where pads began inheriting their footprint's properties -- ``Layer``
#: here, ``Reference`` for the SMD pad floor
#: (``dru_generator.SMD_PAD_CLEARANCE_MIN_KICAD_VERSION``).
PAD_LAYER_SINCE = (10, 0, 2)


def _kicad_cli_version(cli: Path) -> tuple[int, ...] | None:
    from kicad_tools.cli.runner import get_kicad_version
    from kicad_tools.manufacturers.dru_generator import parse_kicad_cli_version

    return parse_kicad_cli_version(get_kicad_version(cli))


def _phys_hole(condition: str, mm: float = 0.4) -> str:
    return _dru(
        f'(rule "p" (condition "{condition}") (constraint physical_hole_clearance (min {mm}mm)))'
    )


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
    "clearance",  # physical_clearance violations are reported as this too
    "hole_clearance",  # ... and physical_hole_clearance ones as this
    "copper_edge_clearance",
    "hole_to_hole",
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
    # id, base, pad, pro, dru, segment x, via (x, y[, drill]), gate refuses, kicad flags
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
    # --- #6150: (severity ignore) wins and switches the check off for the
    # pair; it does not fall through to a lower rule or the default ---
    (
        "ignore-rule-overrides-default",
        NEAR,
        None,
        _default_pro(0.2),
        _dru('(rule "i" (constraint clearance (min 0.05mm)) (severity ignore))'),
        14.9,
        None,
        False,
        False,
    ),
    (
        "ignore-rule-not-enforced",
        NEAR,
        None,
        _default_pro(0.1),
        _dru('(rule "i" (constraint clearance (min 0.5mm)) (severity ignore))'),
        14.9,
        None,
        False,
        False,
    ),
    (
        "ignore-rule-shadows-earlier-rule",
        NEAR,
        None,
        _default_pro(0.1),
        _dru(
            '(rule "a" (constraint clearance (min 0.3mm)))',
            '(rule "i" (constraint clearance (min 0.05mm)) (severity ignore))',
        ),
        14.9,
        None,
        False,
        False,
    ),
    (
        "warning-rule-still-enforced",
        NEAR,
        None,
        _default_pro(0.1),
        _dru('(rule "w" (constraint clearance (min 0.3mm)) (severity warning))'),
        14.9,
        None,
        True,
        True,
    ),
    (
        "ignore-hole-rule",
        PAD,
        ROUND_NPTH,
        None,
        _dru('(rule "h" (constraint hole_clearance (min 0.05mm)) (severity ignore))'),
        16.8,
        None,
        False,
        False,
    ),
    # --- #6150: a .kicad_dru KiCad cannot parse is discarded whole ---
    (
        "malformed-dru-discarded",
        NEAR,
        None,
        _default_pro(0.1),
        '(version 1)\n(rule "c" (constraint clearance (min 0.3mm))\n',
        14.9,
        None,
        False,
        False,
    ),
    (
        "unknown-constraint-discards-dru",
        NEAR,
        None,
        _default_pro(0.1),
        _dru('(rule "c" (constraint clearance (min 0.3mm)))', SOLDER_MASK_MARGIN_RULE),
        14.9,
        None,
        False,
        False,
    ),
    # --- #6150: physical_clearance (other nets' copper and bare holes) ---
    ("physical-clearance-0.15mm", NEAR, None, _default_pro(0.1), PHYS_A, 14.9, None, True, True),
    ("physical-clearance-0.35mm", NEAR, None, _default_pro(0.1), PHYS_A, 14.7, None, False, False),
    (
        "physical-clearance-npth-0.20mm",
        PAD,
        ROUND_NPTH,
        _default_pro(0.1),
        PHYS_NPTH,
        16.8,
        None,
        True,
        True,
    ),
    (
        "physical-clearance-npth-0.35mm",
        PAD,
        ROUND_NPTH,
        _default_pro(0.1),
        PHYS_NPTH,
        16.95,
        None,
        False,
        False,
    ),
    # --- #6150: physical_hole_clearance, and A.Layer ---
    (
        "physical-hole-0.30mm",
        PAD,
        ROUND_NPTH,
        _default_pro(0.1),
        _phys_hole("A.Type == 'Track' || B.Type == 'Track'"),
        16.9,
        None,
        True,
        True,
    ),
    (
        "physical-hole-0.45mm",
        PAD,
        ROUND_NPTH,
        _default_pro(0.1),
        _phys_hole("A.Type == 'Track' || B.Type == 'Track'"),
        17.05,
        None,
        False,
        False,
    ),
    # The project generator's "Hole to Edge" rule: B.Layer of a track is its
    # copper layer, of a pad KiCad's null -- never Edge.Cuts.
    (
        "generator-hole-to-edge-rule",
        PAD,
        ROUND_NPTH,
        _default_pro(0.1),
        _phys_hole("(A.Type == 'via' || A.Type == 'pad') && B.Layer == 'Edge.Cuts'"),
        16.9,
        None,
        False,
        False,
    ),
    (
        "track-layer-wildcard",
        PAD,
        ROUND_NPTH,
        _default_pro(0.1),
        _phys_hole("A.Layer == 'F.*' || B.Layer == 'F.*'"),
        16.9,
        None,
        True,
        True,
    ),
    (
        "track-layer-case-sensitive",
        PAD,
        ROUND_NPTH,
        _default_pro(0.1),
        _phys_hole("A.Layer == 'f.cu' || B.Layer == 'f.cu'"),
        16.9,
        None,
        False,
        False,
    ),
    # A pad's Layer is KiCad's null on 10.0.1 and the pad's own layer from
    # 10.0.2 (#6196: CI's 10.0.6 flags this, 10.0.1 does not).  The gate
    # cannot know which kicad-cli will judge the board, so it refuses.
    (
        "pad-layer-depends-on-kicad-release",
        PAD,
        ROUND_NPTH,
        _default_pro(0.1),
        _phys_hole("A.Type == 'pad' && A.Layer != 'Edge.Cuts'"),
        16.9,
        None,
        True,
        PAD_LAYER_SINCE,
    ),
    # ... but a comparison false under both readings stays decided: an SMD
    # pad on F.Cu is never on B.Cu (0.2 mm gap, 0.3 mm rule not applied) ...
    (
        "smd-pad-layer-is-not-another-layer",
        PAD,
        None,
        _default_pro(0.1),
        _dru(
            "(rule \"p\" (condition \"A.Type == 'pad' && A.Layer == 'B.Cu'\") "
            "(constraint clearance (min 0.3mm)))"
        ),
        16.8,
        None,
        False,
        False,
    ),
    # ... and no pad is on Edge.Cuts, so the generator's "Hole to Edge" rule
    # never applies to a new via beside a pad's hole (0.30 mm drill gap).
    (
        "generator-hole-to-edge-rule-via-beside-pad",
        PAD,
        ROUND_NPTH,
        None,
        _dru(
            H_SMALL_RULE,
            "(rule \"e\" (condition \"(A.Type == 'via' || A.Type == 'pad') && "
            "B.Layer == 'Edge.Cuts'\") (constraint physical_hole_clearance (min 0.4mm)))",
        ),
        None,
        (16.95, 8.0, 0.3),
        False,
        False,
    ),
    # A new via's drill, 0.2 mm from /A's track, against copper.
    (
        "new-via-drill-physical-hole",
        NEAR,
        None,
        _default_pro(0.1),
        _dru(
            '(rule "h" (constraint hole_clearance (min 0.1mm)))',
            "(rule \"p\" (condition \"A.Type == 'Via' || B.Type == 'Via'\") "
            "(constraint physical_hole_clearance (min 0.25mm)))",
        ),
        None,
        (14.7, 8.0),
        True,
        True,
    ),
    # --- #6150: hole_to_hole.  A 0.6 / 0.3 mm via beside R5's 3 mm NPTH
    # hole (edge at x = 16.5): drill edge to drill edge is x - 16.65 ---
    ("hole-to-hole-0.20mm", PAD, ROUND_NPTH, None, H_SMALL, None, (16.85, 8.0, 0.3), True, True),
    (
        "hole-to-hole-0.30mm",
        PAD,
        ROUND_NPTH,
        None,
        H_SMALL,
        None,
        (16.95, 8.0, 0.3),
        False,
        False,
    ),
    (
        "hole-to-hole-pro-minimum",
        PAD,
        ROUND_NPTH,
        {"board": {"design_settings": {"rules": {"min_hole_to_hole": 0.5}}}},
        H_SMALL,
        None,
        (16.95, 8.0, 0.3),
        True,
        True,
    ),
    (
        "hole-to-hole-dru-rule",
        PAD,
        ROUND_NPTH,
        None,
        _dru(H_SMALL_RULE, '(rule "hh" (constraint hole_to_hole (min 0.1mm)))'),
        None,
        (16.85, 8.0, 0.3),
        False,
        False,
    ),
    (
        "hole-to-hole-ignored",
        PAD,
        ROUND_NPTH,
        None,
        _dru(
            H_SMALL_RULE,
            '(rule "hh" (constraint hole_to_hole (min 0.5mm)) (severity ignore))',
        ),
        None,
        (16.85, 8.0, 0.3),
        False,
        False,
    ),
    # hole_to_hole holds within a net: the via is in /B's own PTH pad.
    ("hole-to-hole-own-net", PAD, PTH_B, None, H_SMALL, None, (15.85, 8.0, 0.3), True, True),
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
    drill = via[2] if via and len(via) > 2 else 0.5
    vias = [Via(via[0], via[1], drill, 0.6, (Layer.F_CU, Layer.B_CU), 2, "/B")] if via else []

    refused, message = _gate_refuses(path, segments, vias)
    assert refused is gate_refuses, message

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not installed; gate verdict checked, KiCad's not")
    if isinstance(kicad_flags, tuple):
        version = _kicad_cli_version(Path(cli))
        assert version is not None, "kicad-cli version unreadable"
        kicad_flags = version >= kicad_flags
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
        # A pad's Layer is KiCad's null on 10.0.1, its own layer from 10.0.2
        # (#6196); this one may be on any copper layer, never Edge.Cuts ...
        ("A.Layer == 'F.Cu'", None),
        ("A.Layer != 'F.Cu'", None),
        ("A.Layer == 'Edge.Cuts'", False),
        ("A.Layer != 'Edge.Cuts'", None),
        ("!(A.Layer == 'Edge.Cuts')", True),
        # ... a track's is its layer, unknown here.
        ("B.Layer == 'F.Cu'", None),
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


def test_pad_layer_condition_is_false_only_under_both_releases() -> None:
    """#6196: 10.0.1 reads a pad's Layer as null, 10.0.2+ as the pad's layer."""
    smd = ItemProps("Pad", "/A", False, "SMD", layer="F.Cu")
    track = ItemProps("Track", "/B", layer="B.Cu")
    assert evaluate_condition("A.Layer == 'F.Cu'", smd, track) is None  # true on 10.0.2+
    assert evaluate_condition("A.Layer == '*.Cu'", smd, track) is None
    assert evaluate_condition("A.Layer == 'B.Cu'", smd, track) is False
    assert evaluate_condition("A.Layer == 'f.cu'", smd, track) is False  # case-sensitive
    assert evaluate_condition("A.Layer != 'F.Cu'", smd, track) is False
    assert evaluate_condition("A.Layer != 'B.Cu'", smd, track) is None  # true on 10.0.2+
    # The gate's pad items keep their single copper layer; a via has none.
    from kicad_tools.router.board_clearance_rules import item_props_for_type

    assert item_props_for_type("pad", "/A", False, "smd", "F.Cu").layer == "F.Cu"
    assert item_props_for_type("via", "/A", True, None, "F.Cu").layer is None


def test_track_layer_condition() -> None:
    track = ItemProps("Track", "/SIG", layer="B.Cu")
    via = ItemProps("Via", "/SIG", True)
    assert evaluate_condition("A.Layer == 'B.Cu'", track, via) is True
    assert evaluate_condition("A.Layer == '*.Cu'", track, via) is True
    assert evaluate_condition("A.Layer == 'b.cu'", track, via) is False  # case-sensitive
    assert evaluate_condition("A.Layer != 'F.Cu'", track, via) is True
    assert evaluate_condition("B.Layer == 'F.Cu'", track, via) is False  # a via's is null
    assert evaluate_condition("B.Layer != 'F.Cu'", track, via) is False


# ---------------------------------------------------------------------------
# #6150: severity, discarded .kicad_dru files
# ---------------------------------------------------------------------------


def test_ignore_rule_settles_the_pair_at_zero() -> None:
    a, b = ItemProps("Track", "/A"), ItemProps("Track", "/B")
    rules = _rules(
        class_clearance={"Default": 0.2},
        dru_rules=[
            DruRule("big", None, None, (("clearance", 0.5),)),
            DruRule("i", None, None, (("clearance", 0.05),), "ignore"),
        ],
    )
    assert rules.clearance(a, b) == 0.0
    # An ignore rule whose condition is unknown may or may not switch the
    # check off: the larger reading (the default) is kept.
    unknown = _rules(
        class_clearance={"Default": 0.2},
        dru_rules=[DruRule("i", "A.insideArea('x')", None, (("clearance", 0.5),), "ignore")],
    )
    assert unknown.clearance(a, b) == pytest.approx(0.2)


def test_parse_dru_reads_severity_and_new_constraints(tmp_path) -> None:
    dru = tmp_path / "b.kicad_dru"
    dru.write_text(
        _dru(
            '(rule "i" (constraint clearance (min 0.1mm)) (severity ignore))',
            '(rule "w" (constraint hole_to_hole (min 0.3mm)) (severity warning))',
            '(rule "p" (constraint physical_clearance (min 0.2mm))'
            " (constraint physical_hole_clearance (min 0.25mm)))",
        )
    )
    rules, error = parse_dru(dru)
    assert error is None
    assert [(r.name, r.severity, r.ignored) for r in rules] == [
        ("i", "ignore", True),
        ("w", "warning", False),
        ("p", None, False),
    ]
    assert rules[1].value("hole_to_hole") == pytest.approx(0.3)
    assert rules[2].value("physical_hole_clearance") == pytest.approx(0.25)


@pytest.mark.parametrize(
    "keyword", ["mechanical_clearance", "mechanical_hole_clearance", "solder_mask_sliver"]
)
def test_parse_dru_accepts_kicad_keywords_the_gate_once_dropped(tmp_path, keyword) -> None:
    """kicad-cli 10.0.1 accepts and enforces these; the gate must not discard the file."""
    assert keyword in KICAD_CONSTRAINT_KEYWORDS
    dru = tmp_path / "b.kicad_dru"
    dru.write_text(
        _dru(
            '(rule "c" (constraint clearance (min 0.3mm)))',
            f'(rule "k" (constraint {keyword} (min 0.2mm)))',
        )
    )
    rules, error = parse_dru(dru)
    assert error is None
    assert rules[0].name == "c"


def test_mechanical_constraints_alias_the_physical_ones(tmp_path) -> None:
    dru = tmp_path / "b.kicad_dru"
    dru.write_text(
        _dru(
            '(rule "m" (constraint mechanical_clearance (min 0.4mm))'
            " (constraint mechanical_hole_clearance (min 0.5mm)))",
        )
    )
    rules, error = parse_dru(dru)
    assert error is None
    assert rules[0].constraints == (
        ("physical_clearance", 0.4),
        ("physical_hole_clearance", 0.5),
    )
    a, b = ItemProps("Track", "/A"), ItemProps("Pad", "/B")
    mech = _rules(dru_rules=rules)
    phys = _rules(
        dru_rules=[
            DruRule(
                "p", None, None, (("physical_clearance", 0.4), ("physical_hole_clearance", 0.5))
            )
        ]
    )
    assert mech.physical_clearance(a, b) == pytest.approx(0.4)
    assert mech.physical_clearance(a, b) == phys.physical_clearance(a, b)
    assert mech.physical_hole_clearance(a, b) == phys.physical_hole_clearance(a, b)
    assert mech.physical_hole_clearance(a, b) == pytest.approx(0.5)


@pytest.mark.parametrize(
    "text,reason",
    [
        ('(version 1)\n(rule "c" (constraint clearance (min 0.3mm))\n', "does not parse"),
        (_dru(SOLDER_MASK_MARGIN_RULE), "unknown constraint 'solder_mask_margin'"),
        (
            _dru('(rule "x" (constraint hole_size (min 0.1mm)) (severity info))'),
            "unknown severity 'info'",
        ),
    ],
    ids=["syntax", "constraint", "severity"],
)
def test_dru_kicad_discards_is_reported(tmp_path, caplog, text, reason) -> None:
    dru = tmp_path / "b.kicad_dru"
    dru.write_text(_dru('(rule "ok" (constraint clearance (min 0.3mm)))') + text)
    rules, error = parse_dru(dru)
    assert rules == [] and error is not None and reason in error
    with caplog.at_level("WARNING"):
        assert read_dru_rules(dru) == []
    assert "kicad-cli ignores every custom rule" in caplog.text
    assert parse_dru(tmp_path / "missing.kicad_dru") == ([], None)


def test_route_auto_warns_about_a_discarded_dru(tmp_path, capsys) -> None:
    """The gate falls back to the netclasses, as kicad-cli does, and says so."""
    path = _board(
        tmp_path,
        NEAR,
        pro=_default_pro(0.1),
        dru=_dru('(rule "c" (constraint clearance (min 0.3mm)))', SOLDER_MASK_MARGIN_RULE),
    )
    orchestrator = _orchestrator(path)
    rules = orchestrator._clearance_rules()
    assert rules is not None and rules.dru_rules == []
    assert "solder_mask_margin" in (rules.dru_error or "")
    err = capsys.readouterr().err
    assert "Warning: board.kicad_dru: rule 'Solder Mask Clearance - jlcpcb'" in err
    orchestrator._clearance_rules()  # cached: warned once
    assert "Warning" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# #6150: hole to hole, crossing a hole, physical-clearance messages
# ---------------------------------------------------------------------------


def test_two_new_vias_too_close_are_refused(tmp_path) -> None:
    """KiCad checks hole_to_hole between two vias of the same net."""
    path = _board(tmp_path, PAD, pro=_default_pro(0.1))
    vias = [
        Via(5.0, 8.0, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "/B"),
        Via(5.4, 8.0, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "/B"),
    ]
    refused, message = _gate_refuses(path, vias=vias)
    assert refused
    assert "drills a via that comes within 0.100 mm of drilled hole(s)" in message
    assert "drill of new via at (105.400, 108.000)" in message
    assert "0.250 mm hole-to-hole clearance" in message
    assert "(0 segment(s), 1 via(s))" in message

    refused, message = _gate_refuses(
        path, vias=[vias[0], Via(5.6, 8.0, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "/B")]
    )
    assert not refused, message

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not installed; gate verdict checked, KiCad's not")
    assert _kicad_flags(Path(cli), path, vias=vias) == {"hole_to_hole"}


def test_crossing_a_hole_is_refused_even_when_ignored(tmp_path) -> None:
    """KiCad accepts it under an ``ignore`` rule, but the drill would sever the
    track: the gate errs toward refusing."""
    path = _board(
        tmp_path,
        PAD,
        pad=ROUND_NPTH,
        dru=_dru('(rule "h" (constraint hole_clearance (min 0.25mm)) (severity ignore))'),
    )
    refused, message = _gate_refuses(path, [_b_track(15.0)])
    assert refused and "crosses drilled hole(s)" in message
    refused, message = _gate_refuses(path, [_b_track(16.7)])
    assert not refused, message


def test_physical_refusals_name_the_rule(tmp_path) -> None:
    path = _board(tmp_path, NEAR, pro=_default_pro(0.1), dru=PHYS_A)
    refused, message = _gate_refuses(path, [_b_track(14.9)])
    assert refused
    assert "comes within 0.150 mm of track (115.250, 104.000)-(115.250, 112.000) on F.Cu" in message
    assert "below the 0.300 mm physical clearance" in message

    path = _board(
        tmp_path,
        PAD,
        pad=ROUND_NPTH,
        pro=_default_pro(0.1),
        dru=_phys_hole("A.Type == 'Track' || B.Type == 'Track'"),
        name="hole",
    )
    refused, message = _gate_refuses(path, [_b_track(16.9)])
    assert refused
    assert "comes within 0.300 mm of drilled hole(s) (NPTH hole of pad R5" in message
    assert "0.400 mm physical hole clearance" in message


def test_net_requirement_includes_physical_clearance() -> None:
    rules = _rules(
        class_clearance={"Default": 0.15},
        dru_rules=[
            DruRule(
                "p",
                "A.NetName == '/MAINS' || B.NetName == '/MAINS'",
                None,
                (("physical_clearance", 0.6),),
            )
        ],
    )
    assert rules.net_requirement("/SIG", {"/OTHER"}) == pytest.approx(0.15)
    assert rules.net_requirement("/SIG", {"/MAINS"}) == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# #6150 item 5: the board 06 / 07 regression fixtures
# ---------------------------------------------------------------------------

BOARDS = Path(__file__).resolve().parent.parent / "boards"
REGRESSION_FIXTURES = [
    BOARDS / "06-diffpair-test/regression-fixture/diffpair_test_routed.kicad_pcb",
    BOARDS / "07-matchgroup-test/regression-fixture/matchgroup_test_routed.kicad_pcb",
]


@pytest.mark.parametrize("pcb", REGRESSION_FIXTURES, ids=["board06", "board07"])
def test_regression_fixture_drus_are_current(pcb) -> None:
    """Regenerated with the project generator: no stale 0.4 mm hole_clearance
    rule labelled "Hole to Edge", nothing kicad-cli discards the file over."""
    rules = BoardClearanceRules.from_board(pcb)
    assert rules.dru_error is None
    assert rules.dru_rules
    [hole_to_edge] = [r for r in rules.dru_rules if r.name.startswith("Hole to Edge")]
    assert hole_to_edge.value("hole_clearance") is None
    assert hole_to_edge.value("physical_hole_clearance") == pytest.approx(0.4)
    # It is a hole-to-Edge.Cuts rule: it never reaches a track or a via.
    hole = ItemProps("Pad", "", False, "NPTH, mechanical")
    for item in (ItemProps("Track", "/X", layer="F.Cu"), ItemProps("Via", "/X", True)):
        assert rules.physical_hole_clearance(item, hole, "F.Cu") == 0.0


@pytest.mark.parametrize("pcb", REGRESSION_FIXTURES, ids=["board06", "board07"])
def test_gate_accepts_every_routed_net_of_the_regression_fixtures(pcb) -> None:
    """Each net's routed copper, fed back through the gate as if it were new,
    is accepted -- with the stale DRU the gate refused 16 of board 06's 26
    nets and 1 of board 07's 30 (#6150)."""
    from kicad_tools.router.io import detect_layer_stack, parse_pcb_design_rules

    text = pcb.read_text()
    orchestrator = RoutingOrchestrator(
        pcb=PCB.load(str(pcb)),  # type: ignore[arg-type]
        rules=parse_pcb_design_rules(text).to_design_rules(),
        layer_stack=detect_layer_stack(text),
    )
    layers = {layer.kicad_name: layer for layer in Layer}
    by_net: dict[int, tuple[list, list]] = {}
    for s in orchestrator.pcb.segments:
        if s.net_number:
            (x1, y1), (x2, y2) = s.start, s.end
            by_net.setdefault(s.net_number, ([], []))[0].append(
                Segment(x1, y1, x2, y2, s.width, layers[s.layer], s.net_number, s.net_name)
            )
    for v in orchestrator.pcb.vias:
        if v.net_number:
            by_net.setdefault(v.net_number, ([], []))[1].append(
                Via(
                    *v.position, v.drill, v.size, (Layer.F_CU, Layer.B_CU), v.net_number, v.net_name
                )
            )
    copper, holes = list(orchestrator._board_copper()), list(orchestrator._board_holes())
    assert len(by_net) >= 26
    refused = []
    for net, (segments, vias) in by_net.items():
        # The net's routed copper is what is fed in as new: take it off the board.
        orchestrator._board_copper_cache = [
            i for i in copper if not (i.net == net and i.kind in ("track", "arc", "via"))
        ]
        orchestrator._board_holes_cache = [
            i for i in holes if not (i.net == net and i.owner == "via")
        ]
        name = (segments or vias)[0].net_name
        result = RoutingResult(
            success=True,
            net=name,
            strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
            segments=segments,
            vias=vias,
        )
        if orchestrator._foreign_copper_conflicts(result):
            refused.append(name)
    assert refused == []
