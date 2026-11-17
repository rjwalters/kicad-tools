"""Detector parity: ``acute_foldback`` vs the real ``sharp_trace_corner`` rule (Issue #8930).

``router/acute_foldback.py`` repairs fold-backs that ``kct check``'s
``sharp_trace_corner`` rule would flag, so its detector is specified to match
the rule.  The two are separate implementations -- ``wedge_is_open`` and
``_is_buried`` are each written twice, and the angle computation three times
(``AcidTrapCheck._calculate_angle`` vs ``acute_foldback._angle_at``) -- so
nothing but this module stops them drifting.

One table of geometries (``CASES``) feeds **both** sides at two levels:

* *predicate level* -- the three duplicated surfaces called directly on the
  same legs: the angle computation, the open-wedge test and the pad/via burial
  test.  The rule's side reads segments and copper out of a PCB that really
  went through ``PCB.load``; the router's side reads ``Segment`` /
  ``CopperDisc`` built from the same numbers.
* *board level* -- the rule's ``check()`` against the router's
  ``gating_corner_keys``, which must flag exactly the same vertex keys.  This
  is what catches drift in candidate discovery (junction matching, degree-3
  junctions, zero-length stubs) that the predicates alone cannot.

Every assertion is an equality between the two implementations.  No case
carries a hand-written expected verdict, so there is no second expectation
table to keep in sync -- ``TestTableIsNotVacuous`` only checks that the table
exercises both outcomes.  The mutation check that proves the test has teeth
is recorded in the PR description for Issue #8930.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

pytest.importorskip("shapely")

from kicad_tools.explain.checks.acid_trap import AcidTrapCheck  # noqa: E402
from kicad_tools.router import acute_foldback as router_side  # noqa: E402
from kicad_tools.router.acute_foldback import CopperDisc, gating_corner_keys  # noqa: E402
from kicad_tools.router.layers import Layer  # noqa: E402
from kicad_tools.router.primitives import Segment  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules import sharp_trace_corner as rule_side  # noqa: E402
from kicad_tools.validate.rules.sharp_trace_corner import SharpTraceCornerRule  # noqa: E402

Point = tuple[float, float]

SIG_NET = 5  # router net id of the segments under test
OTHER_NET = 6
_NET_NUMBER = {"SIG": 1, "OTHER": 2}  # PCB net numbers
_ROUTER_NET = {"SIG": SIG_NET, "OTHER": OTHER_NET}

_ROUTER_LAYER = {
    "F.Cu": Layer.F_CU,
    "In1.Cu": Layer.IN1_CU,
    "B.Cu": Layer.B_CU,
}

J: Point = (10.0, 10.0)  # the junction every polar case is built around


# --------------------------------------------------------------------------
# The shared geometry table
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Disc:
    """Pad/via copper, as plain numbers both sides are built from.

    ``kind`` is ``"via"`` (through via, circular), ``"smd"`` (rect pad on
    ``layer``) or ``"thru"`` (through-hole pad on ``*.Cu``).
    """

    kind: str
    x: float
    y: float
    size: tuple[float, float]
    net: str = "SIG"
    layer: str = "F.Cu"


@dataclass(frozen=True)
class Case:
    name: str
    segments: tuple[tuple[Point, Point, float], ...]
    junction: Point | None = J  # None: board-level only (no single junction)
    discs: tuple[Disc, ...] = ()
    layer: str = "F.Cu"
    note: str = ""


def leg(bearing: float, length: float, width: float = 0.2, *, inward: bool = False):
    """A leg leaving ``J`` at *bearing* degrees (or arriving at it, if *inward*)."""
    far = (
        J[0] + length * math.cos(math.radians(bearing)),
        J[1] + length * math.sin(math.radians(bearing)),
    )
    return (far, J, width) if inward else (J, far, width)


def _s0(w1: float, w2: float, angle: float) -> float:
    """The open-wedge threshold the rule documents (``(w1+w2) / (4 sin(theta/2))``)."""
    return (w1 + w2) / (4 * math.sin(math.radians(angle) / 2))


_S0_45 = _s0(0.2, 0.2, 45.0)  # 0.2613 mm

CASES: tuple[Case, ...] = (
    # -- open wedges: gated ------------------------------------------------
    Case("open_wedge_45", (leg(0, 5.0), leg(45, 3.0, inward=True))),
    Case("open_wedge_unequal_widths", (leg(0, 5.0, 0.15), leg(30, 4.0, 0.3))),
    Case("open_wedge_88_9", (leg(0, 3.0), leg(88.9, 3.0))),
    Case("open_wedge_1_0_lower_bound", (leg(0, 8.0), leg(1.0, 8.0))),
    # -- the angle window: not gated ---------------------------------------
    Case("angle_89_0_upper_bound", (leg(0, 3.0), leg(89.0, 3.0))),
    Case("right_angle_90", (leg(0, 3.0), leg(90, 3.0))),
    Case("obtuse_135", (leg(0, 3.0), leg(135, 3.0))),
    Case("straight_180", (leg(0, 3.0), leg(180, 3.0))),
    Case("below_window_0_5", (leg(0, 8.0), leg(0.5, 8.0))),
    # -- collinear doubling-back: angle 0, sin(theta/2) == 0 ---------------
    Case(
        "collinear_doubling_back",
        (leg(0, 2.0), leg(0, 1.0)),
        note="angle 0: the rule used to divide by sin(0) here",
    ),
    Case("collinear_doubling_back_unequal_widths", (leg(0, 2.0, 0.4), leg(0, 1.0, 0.1))),
    # -- width-filled hairpin: s0 boundary ---------------------------------
    Case("hairpin_well_inside_s0", (leg(0, 5.0), leg(45, 0.5 * _S0_45))),
    Case("hairpin_just_inside_s0", (leg(0, 5.0), leg(45, _S0_45 - 0.01))),
    Case("hairpin_exactly_s0", (leg(0, 5.0), leg(45, _S0_45))),
    Case("hairpin_just_outside_s0", (leg(0, 5.0), leg(45, _S0_45 + 0.01))),
    Case(
        "hairpin_both_legs_short",
        (leg(0, _S0_45 - 0.05), leg(45, _S0_45 - 0.05)),
    ),
    # -- thin leg lying inside a wide one ----------------------------------
    Case(
        "thin_leg_inside_wide_leg",
        (leg(0, 2.0, 0.5), leg(80, 0.22, 0.05)),
        note="min leg 0.22 > s0 0.2139, yet the thin leg is within the wide one's edge",
    ),
    Case("thin_leg_inside_wide_leg_reversed", (leg(80, 0.22, 0.05), leg(0, 2.0, 0.5))),
    Case(
        "thin_leg_just_outside_wide_leg",
        (leg(0, 2.0, 0.5), leg(80, 0.25, 0.05)),
        note="0.25 sin(80 deg) = 0.2462 > margin 0.225: notch is open again",
    ),
    Case("equal_width_legs_margin_zero", (leg(0, 2.0, 0.3), leg(60, 2.0, 0.3))),
    # -- burial under same-net pad/via copper ------------------------------
    Case("via_buried", (leg(0, 5.0), leg(45, 3.0)), discs=(Disc("via", *J, (0.65, 0.65)),)),
    Case(
        "via_beside_vertex_not_buried",
        (leg(0, 5.0), leg(45, 3.0)),
        discs=(Disc("via", J[0] + 0.5, J[1], (0.65, 0.65)),),
    ),
    Case(
        "smd_pad_buried",
        (leg(0, 5.0), leg(45, 3.0)),
        discs=(Disc("smd", *J, (1.6, 1.6)),),
    ),
    Case(
        "thru_pad_buried",
        (leg(0, 5.0), leg(45, 3.0)),
        discs=(Disc("thru", *J, (1.6, 1.6)),),
    ),
    Case(
        "other_net_via_does_not_bury",
        (leg(0, 5.0), leg(45, 3.0)),
        discs=(Disc("via", *J, (0.65, 0.65), net="OTHER"),),
    ),
    Case(
        "other_net_pad_does_not_bury",
        (leg(0, 5.0), leg(45, 3.0)),
        discs=(Disc("smd", *J, (1.6, 1.6), net="OTHER"),),
    ),
    Case(
        "pad_on_other_layer_does_not_bury",
        (leg(0, 5.0), leg(45, 3.0)),
        discs=(Disc("smd", *J, (1.6, 1.6), layer="B.Cu"),),
    ),
    # -- inner layers: a through via/pad spans the whole stack -------------
    Case("inner_layer_open_wedge", (leg(0, 5.0), leg(45, 3.0)), layer="In1.Cu"),
    Case(
        "inner_layer_through_via_buries",
        (leg(0, 5.0), leg(45, 3.0)),
        layer="In1.Cu",
        discs=(Disc("via", *J, (0.65, 0.65)),),
    ),
    Case(
        "inner_layer_through_pad_buries",
        (leg(0, 5.0), leg(45, 3.0)),
        layer="In1.Cu",
        discs=(Disc("thru", *J, (1.6, 1.6)),),
    ),
    Case(
        "inner_layer_surface_pad_does_not_bury",
        (leg(0, 5.0), leg(45, 3.0)),
        layer="In1.Cu",
        discs=(Disc("smd", *J, (1.6, 1.6)),),
    ),
    Case(
        "back_layer_open_wedge_front_pad_does_not_bury",
        (leg(0, 5.0), leg(45, 3.0)),
        layer="B.Cu",
        discs=(Disc("smd", *J, (1.6, 1.6)),),
    ),
    # -- branch (degree-3) junctions ---------------------------------------
    Case(
        "branch_junction_open_acute_wedge",
        (leg(0, 4.0), leg(45, 4.0), leg(180, 4.0)),
    ),
    Case(
        "branch_junction_every_acute_pair_width_filled",
        (leg(0, 4.0), leg(45, 0.1), leg(180, 4.0)),
    ),
    Case(
        "branch_junction_only_obtuse_pairs",
        (leg(0, 4.0), leg(120, 4.0), leg(240, 4.0)),
    ),
    Case(
        "branch_junction_buried",
        (leg(0, 4.0), leg(45, 4.0), leg(180, 4.0)),
        discs=(Disc("via", *J, (0.65, 0.65)),),
    ),
    # -- candidate discovery: not one junction -----------------------------
    Case(
        "stacked_duplicate_segments",
        (leg(0, 5.0), leg(0, 5.0), leg(45, 3.0)),
        note="a duplicated leg must not change the verdict",
    ),
    Case(
        "zero_length_stub_before_a_135_bend",
        (
            ((17.775, 33.0), (17.775, 32.9996), 0.2),
            ((17.775, 32.9996), (17.139, 32.3636), 0.2),
        ),
        junction=None,
    ),
    Case(
        "two_separate_open_wedges",
        (
            leg(0, 5.0),
            leg(45, 3.0),
            ((30.0, 30.0), (35.0, 30.0), 0.2),
            ((30.0, 30.0), (33.0, 33.0), 0.2),
        ),
        junction=None,
    ),
    Case(
        "chain_of_obtuse_bends",
        (
            ((0.0, 0.0), (1.0, 0.0), 0.2),
            ((1.0, 0.0), (2.0, 1.0), 0.2),
            ((2.0, 1.0), (2.0, 3.0), 0.2),
        ),
        junction=None,
    ),
)


# --------------------------------------------------------------------------
# Adapters: one table in, a PCB for the rule and Segments/CopperDiscs for the router
# --------------------------------------------------------------------------


def _f(x: float) -> str:
    return repr(float(x))


def _board_text(case: Case) -> str:
    parts = []
    for (x1, y1), (x2, y2), width in case.segments:
        parts.append(
            f"(segment (start {_f(x1)} {_f(y1)}) (end {_f(x2)} {_f(y2)}) "
            f'(width {_f(width)}) (layer "{case.layer}") (net {_NET_NUMBER["SIG"]}))'
        )
    for i, d in enumerate(case.discs):
        net = _NET_NUMBER[d.net]
        name = d.net
        if d.kind == "via":
            parts.append(
                f"(via (at {_f(d.x)} {_f(d.y)}) (size {_f(d.size[0])}) (drill 0.3) "
                f'(layers "F.Cu" "B.Cu") (net {net}))'
            )
            continue
        if d.kind == "smd":
            pad = (
                f'(pad "1" smd rect (at 0 0) (size {_f(d.size[0])} {_f(d.size[1])}) '
                f'(layers "{d.layer}") (net {net} "{name}"))'
            )
        else:
            pad = (
                f'(pad "1" thru_hole circle (at 0 0) (size {_f(d.size[0])} {_f(d.size[1])}) '
                f'(drill 0.8) (layers "*.Cu") (net {net} "{name}"))'
            )
        parts.append(
            f'(footprint "Test:FP" (layer "F.Cu") (uuid "fp{i}") (at {_f(d.x)} {_f(d.y)})\n'
            f'  (property "Reference" "P{i}" (at 0 -2 0) (layer "F.SilkS"))\n  {pad})\n'
        )
    return (
        '(kicad_pcb (version 20240108) (generator "test")\n'
        '  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (31 "B.Cu" signal) '
        '(44 "Edge.Cuts" user))\n'
        '  (net 0 "") (net 1 "SIG") (net 2 "OTHER")\n  ' + "".join(parts) + "\n)\n"
    )


def load_pcb(case: Case, tmp_path) -> PCB:
    path = tmp_path / f"{case.name}.kicad_pcb"
    path.write_text(_board_text(case))
    return PCB.load(str(path))


def router_segments(case: Case) -> list[Segment]:
    layer = _ROUTER_LAYER[case.layer]
    return [
        Segment(x1=a[0], y1=a[1], x2=b[0], y2=b[1], width=w, layer=layer, net=SIG_NET)
        for a, b, w in case.segments
    ]


def router_discs(case: Case) -> list[CopperDisc]:
    outer = frozenset({Layer.F_CU, Layer.B_CU})
    out = []
    for d in case.discs:
        layers = outer if d.kind in ("via", "thru") else frozenset({_ROUTER_LAYER[d.layer]})
        out.append(
            CopperDisc(
                x=d.x,
                y=d.y,
                radius=min(d.size) / 2,
                layers=layers,
                net=_ROUTER_NET[d.net],
            )
        )
    return out


def _key(point: Point) -> Point:
    return (round(point[0], 2), round(point[1], 2))


def rule_keys(case: Case, tmp_path) -> set[Point]:
    """Vertex keys the real ``sharp_trace_corner`` rule flags on *case*."""
    violations = SharpTraceCornerRule().check(load_pcb(case, tmp_path), None).violations
    return {_key(v.location) for v in violations}


def router_keys(case: Case) -> set[Point]:
    return gating_corner_keys(
        router_segments(case), router_discs(case), SIG_NET, _ROUTER_LAYER[case.layer]
    )


def _junction_pairs(segments: list, junction: Point) -> list[tuple]:
    """Every pair of segments touching *junction* (as the rule pairs them)."""
    touching = [
        s for s in segments if min(math.dist(junction, s.start), math.dist(junction, s.end)) < 0.015
    ]
    return [(a, b) for i, a in enumerate(touching) for b in touching[i + 1 :]]


JUNCTION_CASES = [c for c in CASES if c.junction is not None]
_IDS = [c.name for c in CASES]
_JIDS = [c.name for c in JUNCTION_CASES]


# --------------------------------------------------------------------------
# Predicate level: the three duplicated surfaces
# --------------------------------------------------------------------------


class TestPredicateParity:
    @pytest.mark.parametrize("case", JUNCTION_CASES, ids=_JIDS)
    def test_angle_computation_agrees(self, case, tmp_path):
        """``AcidTrapCheck._calculate_angle`` vs ``acute_foldback._angle_at``."""
        pcb = load_pcb(case, tmp_path)
        junction = case.junction
        check = AcidTrapCheck()
        pairs = list(
            zip(
                _junction_pairs(list(pcb.segments), junction),
                _junction_pairs(router_segments(case), junction),
                strict=True,
            )
        )
        assert pairs
        for (r1, r2), (s1, s2) in pairs:
            rule_angle = check._calculate_angle(r1, r2, junction)
            router_angle = router_side._angle_at(s1, s2, junction)
            if rule_angle is None or router_angle is None:
                assert rule_angle is None and router_angle is None
            else:
                assert router_angle == pytest.approx(rule_angle, abs=1e-9)

    @pytest.mark.parametrize("case", JUNCTION_CASES, ids=_JIDS)
    def test_open_wedge_agrees(self, case, tmp_path):
        """``_wedge_is_open`` (rule) vs ``wedge_is_open`` (router), same legs, same angle."""
        pcb = load_pcb(case, tmp_path)
        junction = case.junction
        check = AcidTrapCheck()
        rule_pairs = _junction_pairs(list(pcb.segments), junction)
        router_pairs = _junction_pairs(router_segments(case), junction)
        for (r1, r2), (s1, s2) in zip(rule_pairs, router_pairs, strict=True):
            angle = check._calculate_angle(r1, r2, junction)
            if angle is None:
                continue
            assert router_side.wedge_is_open(s1, s2, junction, angle) == (
                rule_side._wedge_is_open(r1, r2, junction, angle)
            ), f"angle {angle}"

    @pytest.mark.parametrize("case", JUNCTION_CASES, ids=_JIDS)
    def test_burial_agrees(self, case, tmp_path):
        """``_is_buried`` (rule, real ``_copper_discs``) vs ``_is_buried`` (router)."""
        pcb = load_pcb(case, tmp_path)
        rule_discs = rule_side._copper_discs(pcb)
        # Same-net discs only differ by name/id; the vertex is the probe point.
        rule_verdict = rule_side._is_buried(rule_discs, "SIG", case.layer, case.junction)
        router_verdict = router_side._is_buried(
            router_discs(case), SIG_NET, _ROUTER_LAYER[case.layer], case.junction
        )
        assert router_verdict == rule_verdict


# --------------------------------------------------------------------------
# Predicate sweep: boundaries a hand-written table can miss
# --------------------------------------------------------------------------

_ANGLES = (0.0, 0.5, 1.0, 10.0, 30.0, 45.0, 60.0, 80.0, 88.9, 89.0, 90.0, 135.0, 180.0)
_WIDTHS = ((0.2, 0.2), (0.15, 0.3), (0.3, 0.15), (0.05, 0.5), (0.25, 0.25))
_LONG_LEG = 3.0


def _sweep_params():
    for angle in _ANGLES:
        for w1, w2 in _WIDTHS:
            # Short-leg lengths straddling the width-filled boundary and the
            # thin-inside-wide boundary, plus a long one.
            s0 = _s0(w1, w2, angle) if angle > 0 else 1.0
            for short in (0.5 * s0, s0 - 1e-3, s0, s0 + 1e-3, 0.22, 0.25, _LONG_LEG):
                yield angle, w1, w2, short


@pytest.mark.parametrize("angle,w1,w2,short", list(_sweep_params()))
def test_wedge_predicate_sweep(angle, w1, w2, short):
    """Both ``wedge_is_open`` copies agree across angle x width x leg-length.

    The ``Segment`` objects are shared: the rule's predicate reads
    ``.start/.end/.width``, which the router's ``Segment`` also exposes.
    """
    long_leg = Segment(
        x1=J[0], y1=J[1], x2=J[0] + _LONG_LEG, y2=J[1], width=w1, layer=Layer.F_CU, net=SIG_NET
    )
    short_leg = Segment(
        x1=J[0],
        y1=J[1],
        x2=J[0] + short * math.cos(math.radians(angle)),
        y2=J[1] + short * math.sin(math.radians(angle)),
        width=w2,
        layer=Layer.F_CU,
        net=SIG_NET,
    )
    assert router_side.wedge_is_open(long_leg, short_leg, J, angle) == (
        rule_side._wedge_is_open(long_leg, short_leg, J, angle)
    )
    # ... and in the other argument order, which selects the other thin/wide pairing.
    assert router_side.wedge_is_open(short_leg, long_leg, J, angle) == (
        rule_side._wedge_is_open(short_leg, long_leg, J, angle)
    )


def test_collinear_doubling_back_does_not_raise_on_the_rule_side():
    """The ``angle_deg == 0`` divergence recorded on Issue #8930, resolved.

    The router copy guards ``sin(theta/2) <= 0`` and returns ``False``; the
    rule copy used to divide by it and raise ``ZeroDivisionError``.  Neither
    production call site reaches angle 0 (both gate on ``angle >= 1``), but the
    shared geometry set includes collinear doubling-back, so the rule copy now
    carries the same guard and the two agree.
    """
    a = Segment(x1=J[0], y1=J[1], x2=J[0] + 2, y2=J[1], width=0.2, layer=Layer.F_CU, net=SIG_NET)
    b = Segment(x1=J[0], y1=J[1], x2=J[0] + 1, y2=J[1], width=0.2, layer=Layer.F_CU, net=SIG_NET)
    assert rule_side._wedge_is_open(a, b, J, 0.0) is False
    assert router_side.wedge_is_open(a, b, J, 0.0) is False


# --------------------------------------------------------------------------
# Board level: the rule's ``check()`` vs the router's ``gating_corner_keys``
# --------------------------------------------------------------------------


class TestBoardParity:
    @pytest.mark.parametrize("case", CASES, ids=_IDS)
    def test_rule_and_router_flag_the_same_vertices(self, case, tmp_path):
        assert router_keys(case) == rule_keys(case, tmp_path)


class TestTableIsNotVacuous:
    """The table must exercise both outcomes, or a parity of constants would pass."""

    def test_table_has_flagged_and_unflagged_cases(self, tmp_path):
        verdicts = {c.name: bool(rule_keys(c, tmp_path)) for c in CASES}
        assert any(verdicts.values()), "no case is flagged by the rule"
        assert not all(verdicts.values()), "every case is flagged by the rule"

    @pytest.mark.parametrize(
        "exempt,flagged",
        [
            ("hairpin_just_inside_s0", "hairpin_just_outside_s0"),
            ("thin_leg_inside_wide_leg", "thin_leg_just_outside_wide_leg"),
            ("via_buried", "via_beside_vertex_not_buried"),
            ("smd_pad_buried", "other_net_pad_does_not_bury"),
            ("inner_layer_through_via_buries", "inner_layer_open_wedge"),
            ("angle_89_0_upper_bound", "open_wedge_88_9"),
        ],
    )
    def test_each_exemption_has_a_flagged_twin(self, exempt, flagged, tmp_path):
        """Each exemption sits next to a near-identical case the rule *does* flag.

        This is what makes a one-sided mutation of that exemption visible: the
        twin pair is the only thing separating "exempt" from "flagged".
        """
        by_name = {c.name: c for c in CASES}
        assert rule_keys(by_name[exempt], tmp_path) == set()
        assert rule_keys(by_name[flagged], tmp_path) != set()

    def test_thin_inside_wide_case_is_not_just_width_filled(self):
        """The thin-leg exemption case clears the ``s0`` test, so only it can exempt it."""
        case = next(c for c in CASES if c.name == "thin_leg_inside_wide_leg")
        lengths = [math.dist(start, end) for start, end, _ in case.segments]
        assert min(lengths) > _s0(0.5, 0.05, 80.0)


# --------------------------------------------------------------------------
# Known, deliberate asymmetry: pad outline vs inscribed circle
# --------------------------------------------------------------------------


ELONGATED_PAD_CASE = Case(
    "vertex_in_elongated_pad_off_centre",
    (leg(0, 5.0), leg(45, 3.0)),
    discs=(Disc("smd", J[0] - 0.5, J[1], (1.475, 0.3)),),
)


class TestKnownAsymmetry:
    def test_rule_honours_pad_outline_router_uses_the_inscribed_circle(self, tmp_path):
        """Issue #9037 taught the *rule* the pad's real outline; the router has not followed.

        A vertex 0.5 mm along a 1.475 x 0.3 mm land is under pad copper (the
        rule exempts it) but outside the inscribed circle (r = 0.15), so the
        router still treats it as a gating fold-back.  The router errs on the
        safe side -- it may try to repair copper the gate would not flag, never
        the reverse -- so this is not a parity failure, but it is the one place
        the two detectors are not equal.  This test pins the direction: when the
        router adopts the outline, it fails and should be folded into ``CASES``.
        """
        case = ELONGATED_PAD_CASE
        assert rule_keys(case, tmp_path) == set()
        assert router_keys(case) == {_key(J)}

    def test_router_is_never_narrower_than_the_rule_on_the_whole_table(self, tmp_path):
        for case in CASES:
            assert rule_keys(case, tmp_path) <= router_keys(case), case.name
