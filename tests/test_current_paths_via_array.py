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
