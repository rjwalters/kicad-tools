"""Issue #8092: a thin Kelvin sense tap beside a via-fed force trunk is not a fanout."""

from dataclasses import replace

import pytest

from kicad_tools.router.current_paths import (
    audit_current_paths,
    reinforcement_eligible_segment_ids,
    resolve_current_path,
)
from tests.test_current_paths import _add_pad_footprint
from tests.test_current_paths_via_array import fixture, spec


def tap_board(*, tap_width=0.2, tap_pad=True, rejoin=False, same_layer=False):
    pcb = fixture()
    pcb._segments = []
    pcb._vias = []
    # Force trunk: short wide stub -> via -> wide opposite-layer trunk.
    pcb.add_trace((20, 50), (20, 53), width=2.0, layer="F.Cu", net="NET1")
    pcb.add_via(20, 53, net="NET1", dedupe=False)
    pcb.add_trace((20, 53), (120, 50), width=3.0, layer="B.Cu", net="NET1")
    # Sense tap: pad -> stub -> via -> opposite layer -> sense-IC pad.
    if same_layer:
        pcb.add_trace((21, 50), (60, 40), width=tap_width, layer="F.Cu", net="NET1")
    else:
        pcb.add_trace((21, 50), (21, 52), width=tap_width, layer="F.Cu", net="NET1")
        pcb.add_via(21, 52, net="NET1", dedupe=False)
        pcb.add_trace((21, 52), (60, 40), width=tap_width, layer="B.Cu", net="NET1")
    if tap_pad:
        _add_pad_footprint(pcb, ref="SENSE", x=60, y=40, net="NET1")
    if rejoin:
        pcb.add_trace((60, 40), (100, 51.5), width=tap_width, layer="B.Cu", net="NET1")
    return pcb


def reversed_spec():
    s = spec()
    return replace(s, source=s.sink, sink=s.source)


@pytest.mark.parametrize("declaration", [spec, reversed_spec])
@pytest.mark.parametrize("same_layer", [False, True])
def test_sense_tap_beside_via_fed_trunk_resolves(declaration, same_layer):
    pcb = tap_board(same_layer=same_layer)
    result = resolve_current_path(pcb, declaration())
    assert result.ok, result.reason


def test_tap_segments_are_listed_but_never_force_evidence():
    pcb = tap_board()
    result = resolve_current_path(pcb, spec())
    assert result.ok, result.reason
    tap_ids = {id(s) for s in pcb.segments if s.width == 0.2}
    assert tap_ids and {id(s) for s in result.sense_taps} == tap_ids
    assert not tap_ids & {id(s) for s in result.segments}
    assert not tap_ids & reinforcement_eligible_segment_ids(pcb, [spec()])
    assert not audit_current_paths(pcb, [spec()]).fully_covered


def test_tap_on_both_endpoint_pads_resolves():
    pcb = tap_board()
    pcb.add_trace((120, 50), (121, 50), width=0.2, layer="B.Cu", net="NET1")
    pcb.add_trace((121, 50), (140, 60), width=0.2, layer="B.Cu", net="NET1")
    _add_pad_footprint(pcb, ref="SENSE2", x=140, y=60, net="NET1")
    assert resolve_current_path(pcb, spec()).ok


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rejoin": True},
        {"tap_width": 2.0},
        {"tap_pad": False},
    ],
    ids=["rejoins_trunk", "as_wide_as_trunk", "dangling"],
)
def test_non_tap_shapes_stay_ambiguous(kwargs):
    pcb = tap_board(**kwargs)
    result = resolve_current_path(pcb, spec())
    assert result.status == "ambiguous"
    assert not result.sense_taps


def test_two_thin_taps_on_one_pad_stay_ambiguous():
    pcb = tap_board()
    pcb.add_trace((19, 50), (19, 52), width=0.2, layer="F.Cu", net="NET1")
    pcb.add_via(19, 52, net="NET1", dedupe=False)
    pcb.add_trace((19, 52), (60, 30), width=0.2, layer="B.Cu", net="NET1")
    _add_pad_footprint(pcb, ref="SENSE3", x=60, y=30, net="NET1")
    assert resolve_current_path(pcb, spec()).status == "ambiguous"
