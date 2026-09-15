"""Current-path contacts use physical copper, never a first matching label."""

from copy import deepcopy

import pytest

from kicad_tools.router.current_paths import (
    CurrentPathSpec,
    PathEndpoint,
    _build_graph,
    _pad_covers,
    resolve_current_path,
)
from kicad_tools.schema.pcb import PCB
from kicad_tools.schema.physical_identity import footprint_keys
from tests.test_current_paths import _add_pad_footprint


@pytest.mark.parametrize("ref", ["DUP", ""])
@pytest.mark.parametrize("uuid", [False, True])
def test_same_net_duplicate_footprints_keep_distinct_contacts(ref, uuid):
    pcb = PCB.create(width=100, height=100, center=False)
    for label, x in [("J1", 10), ("J2", 30), (ref, 40), (ref, 20)]:
        _add_pad_footprint(pcb, ref=label, x=x, y=20, net="N")
    if uuid:
        for index, fp in enumerate(pcb.footprints):
            fp.uuid = f"physical-{index}"
    # The second duplicate's local offset must be transformed using its own
    # footprint, not the first duplicate's position or pad object.
    fp = pcb.footprints[-1]
    fp.position = (20, 21)
    fp.rotation = 90
    fp.pads[0].position = (1, 0)
    pcb.add_trace((10, 20), (19.7, 20), width=0.2, layer="F.Cu", net="N")
    pcb.add_trace((20.3, 20), (30, 20), width=0.2, layer="F.Cu", net="N")
    pcb.add_trace((40, 20), (45, 20), width=0.2, layer="F.Cu", net="N")
    graph = _build_graph(pcb.segments, pcb, "N")
    keys = footprint_keys(pcb.footprints)
    assert len(graph.pads) == 4
    assert graph.pads[(keys[2], "1", 0)] != graph.pads[(keys[3], "1", 0)]
    assert not _pad_covers(pcb, ref, "1", (20, 20))
    spec = CurrentPathSpec("path", "N", PathEndpoint("J1", "1"), PathEndpoint("J2", "1"), 1)
    result = resolve_current_path(pcb, spec)
    assert result.status == "resolved", result.reason
    assert len(result.segments) == 2
    ambiguous = CurrentPathSpec("ambiguous", "N", PathEndpoint(ref, "1"), spec.sink, 1)
    result = resolve_current_path(pcb, ambiguous)
    assert result.status == "unresolved"
    assert "ambiguous" in result.reason


def test_graph_retains_each_same_number_shape_without_inventing_copper_between_them():
    pcb = PCB.create(width=100, height=100, center=False)
    _add_pad_footprint(pcb, ref="J1", x=10, y=20, net="N")
    fp = pcb.footprints[0]
    second = deepcopy(fp.pads[0])
    second.position = (10, 0)
    fp.pads.append(second)
    pcb.add_trace((10, 20), (5, 20), width=0.2, layer="F.Cu", net="N")
    pcb.add_trace((20, 20), (25, 20), width=0.2, layer="F.Cu", net="N")
    graph = _build_graph(pcb.segments, pcb, "N")
    assert len(graph.pads) == 2
    assert graph.pads[("J1", "1", 0)] != graph.pads[("J1", "1", 1)]
    assert not _pad_covers(pcb, "J1", "1", (10, 20))
    spec = CurrentPathSpec("repeated", "N", PathEndpoint("J1", "1"), PathEndpoint("J1", "1"), 1)
    result = resolve_current_path(pcb, spec)
    assert result.status == "unresolved"
    assert "multiple physical occurrences" in result.reason
