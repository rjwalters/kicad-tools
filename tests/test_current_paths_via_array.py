"""Physical fanout contraction preserves every full-current member."""

import math
from dataclasses import replace

import pytest

from kicad_tools.router.current_paths import (
    CurrentPathSpec,
    PathEndpoint,
    audit_current_paths,
    reinforcement_eligible_segment_ids,
    resolve_current_path,
)
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.path_ampacity import PathAmpacityRule
from tests.test_current_paths import _add_pad_footprint
from tests.test_path_ampacity_check import _design_rules_2oz


def fixture(*, depth=2):
    pcb = PCB.create(layers=4)
    _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
    pad = pcb.get_footprint("J1").pads[0]
    pad.type, pad.shape, pad.size, pad.layers = "smd", "rect", (2.4, 2.4), ["F.Cu"]
    _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="NET1")
    for x in (19.2, 20, 20.8):
        pcb.add_trace((x, 50), (x, 50 + depth), width=0.6, layer="F.Cu", net="NET1")
        pcb.add_via(x, 50 + depth, net="NET1")
    pcb.add_trace((19.2, 50 + depth), (20.8, 50 + depth), width=2, layer="B.Cu", net="NET1")
    pcb.add_trace((20.8, 50 + depth), (120, 50), width=3, layer="B.Cu", net="NET1")
    return pcb


def spec():
    return CurrentPathSpec(
        name="force",
        net_name="NET1",
        source=PathEndpoint("J1", "1"),
        sink=PathEndpoint("J2", "1"),
        continuous_a=3,
        reinforcement_eligible=True,
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_all_array_members_are_checked_audited_and_eligible(reverse):
    pcb = fixture()
    declaration = spec()
    if reverse:
        declaration = replace(declaration, source=declaration.sink, sink=declaration.source)
    result = resolve_current_path(pcb, declaration)
    assert result.ok, result.reason
    assert {id(s) for s in result.segments} == {id(s) for s in pcb.segments}
    assert audit_current_paths(pcb, [declaration]).fully_covered
    assert reinforcement_eligible_segment_ids(pcb, [declaration]) == {id(s) for s in pcb.segments}
    assert not reinforcement_eligible_segment_ids(
        pcb, [replace(declaration, reinforcement_eligible=False)]
    )


def test_narrow_omitted_leg_fails_at_full_declared_current():
    pcb = fixture()
    for s in pcb.segments:
        s.width = 8
    pcb.segments[0].width = 0.01
    high = spec()
    low = replace(high, name="low", continuous_a=0.001)
    result = PathAmpacityRule([high, low]).check(pcb, _design_rules_2oz())
    assert len(result.errors) == 1
    assert "force" in result.errors[0].items
    assert result.errors[0].actual_value == 0.01
    assert result.errors[0].required_value > 0.6


@pytest.mark.parametrize(
    "mutation",
    [
        "removed_via",
        "wrong_span",
        "wrong_net",
        "off_pad",
        "removed_trunk",
        "extra_exit",
        "long_stub",
        "foreign_pad",
        "extra_cycle",
    ],
)
def test_unproved_fanout_remains_nonresolved(mutation):
    pcb = fixture(depth=20 if mutation == "long_stub" else 2)
    if mutation == "removed_via":
        pcb._vias.pop(1)
    elif mutation == "wrong_span":
        pcb.vias[1].layers = ["F.Cu", "In1.Cu"]
    elif mutation == "wrong_net":
        n = pcb.add_net("OTHER")
        pcb.vias[1].net_number = n.number
        pcb.vias[1].net_name = "OTHER"
    elif mutation == "off_pad":
        pcb.segments[1].start = (20, 47)
    elif mutation == "removed_trunk":
        pcb._segments.pop(3)
    elif mutation == "extra_exit":
        pcb.add_trace((20, 52), (20, 55), width=0.2, layer="B.Cu", net="NET1")
    elif mutation == "foreign_pad":
        _add_pad_footprint(pcb, ref="TP", x=20, y=52, net="NET1")
    elif mutation == "extra_cycle":
        pcb.add_trace((120, 50), (125, 55), width=0.2, layer="B.Cu", net="NET1")
        pcb.add_trace((125, 55), (115, 55), width=0.2, layer="B.Cu", net="NET1")
        pcb.add_trace((115, 55), (120, 50), width=0.2, layer="B.Cu", net="NET1")
    assert not resolve_current_path(pcb, spec()).ok
    assert not reinforcement_eligible_segment_ids(pcb, [spec()])


@pytest.mark.parametrize("delta,expected", [(0, True), (0.001, False)])
def test_explicit_pad_diagonal_locality_boundary(delta, expected):
    assert resolve_current_path(fixture(depth=math.hypot(2.4, 2.4) + delta), spec()).ok is expected


@pytest.mark.parametrize("contact", ["track", "pad", "via", "inner_track"])
def test_width_only_external_contacts_reject_array(contact):
    pcb = fixture()
    if contact == "track":
        # Centerlines stay 0.5 mm apart; their 0.6 mm copper strokes overlap.
        pcb.add_trace((18.7, 51.5), (18.7, 54), width=0.6, layer="F.Cu", net="NET1")
    elif contact == "pad":
        _add_pad_footprint(pcb, ref="EXTRA", x=18.7, y=51.5, net="NET1")
        pad = pcb.get_footprint("EXTRA").pads[0]
        pad.type, pad.shape, pad.size, pad.layers = "smd", "circle", (0.6, 0.6), ["F.Cu"]
    elif contact == "via":
        pcb.add_via(18.7, 51.5, net="NET1", size=0.6)
    else:
        # A barrel's annulus can touch inner-layer copper without a center contact.
        pcb.add_trace((18.7, 51.8), (18.7, 54), width=0.6, layer="In1.Cu", net="NET1")
    result = resolve_current_path(pcb, spec())
    assert not result.ok
    assert not result.segments
    assert not reinforcement_eligible_segment_ids(pcb, [spec()])


def test_remote_copper_does_not_expand_local_contact_scope():
    pcb = fixture()
    pcb.add_trace((10, 51.5), (10, 54), width=0.6, layer="F.Cu", net="NET1")
    assert resolve_current_path(pcb, spec()).ok


@pytest.mark.parametrize("unsupported", ["arc", "padstack", "zone"])
def test_uninventoried_copper_disables_array_proof(unsupported):
    from kicad_tools.schema.pcb import Zone
    from kicad_tools.sexp import parse_string

    pcb = fixture()
    net = pcb.get_net_by_name("NET1")
    if unsupported == "arc":
        pcb._sexp.append(
            parse_string(
                f'(arc (start 18 51) (mid 19 52) (end 18 53) (width 0.6) (layer "F.Cu") (net {net.number}))'
            )
        )
    elif unsupported == "padstack":
        pcb.get_footprint("J1").pads[0]._sexp_node = parse_string(
            '(pad "1" smd rect (padstack (mode custom)))'
        )
    else:
        pcb._zones.append(
            Zone(net.number, "NET1", "F.Cu", polygon=[(18, 51), (20, 51), (20, 53), (18, 53)])
        )
    assert not resolve_current_path(pcb, spec()).ok


@pytest.mark.parametrize("reverse", [False, True])
def test_proved_load_exits_preserve_array_evidence(reverse):
    pcb = fixture()
    _add_pad_footprint(pcb, ref="LOAD", x=20, y=60, net="NET1")
    pcb.add_trace((20, 52), (20, 60), width=0.2, layer="B.Cu", net="NET1")
    declaration = spec()
    if reverse:
        declaration = replace(declaration, source=declaration.sink, sink=declaration.source)
    result = resolve_current_path(pcb, declaration)
    assert result.ok, result.reason
    assert {id(s) for s in pcb.segments[:5]} <= {id(s) for s in result.segments}
    assert id(pcb.segments[-1]) not in {id(s) for s in result.segments}


def test_load_tree_with_dangling_branch_is_unproved():
    pcb = fixture()
    _add_pad_footprint(pcb, ref="LOAD", x=20, y=60, net="NET1")
    pcb.add_trace((20, 52), (20, 60), width=0.2, layer="B.Cu", net="NET1")
    pcb.add_trace((20, 60), (25, 65), width=0.2, layer="B.Cu", net="NET1")
    assert not resolve_current_path(pcb, spec()).ok


def test_single_via_tap_arm_is_not_a_fanout():
    """Issue #4980: one via-tap arm plus one ordinary trunk arm must resolve.

    A hub with exactly one arm that happens to drop through a via is not
    "a fanout" -- ``_endpoint_via_array``'s own proof correctly rejects it
    (it needs >= 2 legs), but the endpoint-fanout fallback in
    ``resolve_current_path`` must not treat the mere presence of *one* via
    anywhere on the hub's arms as evidence of a *damaged* parallel array.
    This shape (a force trunk plus a single sense/feedback tap through one
    via to an inner layer) is completely ordinary and ships on real boards
    (see the ``VOUT_PRE``/``RSH1.1`` real-board regression in
    ``tests/test_current_paths.py::TestPhysicalLayerGraph::
    test_board09_ordinary_branch_endpoint_is_not_a_fanout``).
    """
    pcb = fixture()
    pcb._segments = []
    pcb._vias = []
    # Force trunk: straight from J1 to J2, no via at all.
    pcb.add_trace(("J1", "1"), ("J2", "1"), width=2.0, layer="F.Cu", net="NET1")
    # Unrelated sense/feedback tap: a single via-tap arm off the same J1
    # pad, landing on a completely different pad via B.Cu.
    pcb.add_trace((20, 50), (20, 52), width=0.2, layer="F.Cu", net="NET1")
    # dedupe=False: the fixture's own vias (already cleared from `_vias`
    # above) left dedup keys at these exact reused coordinates -- without
    # this, `add_via` silently treats the new via as an already-seen
    # duplicate and skips it.
    pcb.add_via(20, 52, net="NET1", dedupe=False)
    pcb.add_trace((20, 52), (60, 60), width=0.2, layer="B.Cu", net="NET1")
    _add_pad_footprint(pcb, ref="TAP", x=60, y=60, net="NET1")
    result = resolve_current_path(pcb, spec())
    assert result.ok, result.reason


def test_two_unrelated_via_arms_still_ambiguous_without_array_proof():
    """Two candidate via-legs that fail the full array proof stay ambiguous.

    Unlike the single-via-tap case above, a hub with *two* independent
    candidate via-array legs is exactly the "might be a damaged fanout"
    shape the fallback exists to catch -- even though these two legs go
    off in unrelated directions (never a real parallel-via motif), the
    fallback's job is only to detect the *suspicious shape*, not to prove
    or disprove a specific current split.
    """
    pcb = fixture()
    pcb._segments = []
    pcb._vias = []
    pcb.add_trace(("J1", "1"), ("J2", "1"), width=2.0, layer="F.Cu", net="NET1")
    # Two short, non-parallel via-tap arms off the same hub -- each looks
    # like an array leg in isolation, but they never converge on one
    # receiving trunk.
    pcb.add_trace((20, 50), (20, 52), width=0.2, layer="F.Cu", net="NET1")
    # dedupe=False: see the comment in test_single_via_tap_arm_is_not_a_fanout.
    pcb.add_via(20, 52, net="NET1", dedupe=False)
    pcb.add_trace((20, 52), (60, 60), width=0.2, layer="B.Cu", net="NET1")
    _add_pad_footprint(pcb, ref="TAP1", x=60, y=60, net="NET1")

    pcb.add_trace((21, 49), (21, 47), width=0.2, layer="F.Cu", net="NET1")
    pcb.add_via(21, 47, net="NET1", dedupe=False)
    pcb.add_trace((21, 47), (60, 40), width=0.2, layer="B.Cu", net="NET1")
    _add_pad_footprint(pcb, ref="TAP2", x=60, y=40, net="NET1")

    assert not resolve_current_path(pcb, spec()).ok


@pytest.mark.parametrize("net_token", ["1", '"NET1"'])
def test_same_net_custom_via_padstack_is_unproved(net_token):
    from kicad_tools.sexp import parse_string

    pcb = fixture()
    pcb._sexp.append(parse_string("(via (at 19.2 52) (net 1) (padstack (mode custom)))"))
    assert not resolve_current_path(pcb, spec()).ok


def test_off_angle_annular_overlap_is_not_lost_to_polygon_chords():
    pcb = fixture()
    angle = math.pi / 64
    # Two radius-0.3 annuli overlap by 0.1 micrometre, halfway between
    # polygon vertices. Their inscribed default buffers would be disjoint.
    pcb.add_via(
        19.2 - 0.5999 * math.cos(angle),
        52 + 0.5999 * math.sin(angle),
        net="NET1",
        size=0.6,
    )
    pcb.vias[-1].layers = ["In1.Cu", "In2.Cu"]
    assert not resolve_current_path(pcb, spec()).ok
