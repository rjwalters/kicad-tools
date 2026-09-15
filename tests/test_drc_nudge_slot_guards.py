"""Repair destinations and transaction rollback retain physical slot capsules."""

import dataclasses
import math
import pickle

import pytest

from kicad_tools.router import drc_nudge
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules
from tests.router.test_component_hole_slots import holes


def scene(angle=0, offset=0, malformed=False):
    router = Autorouter(
        width=30,
        height=30,
        rules=DesignRules(
            manufacturer="pcbway",
            min_drill_clearance=0.25,
            min_hole_to_hole=0.25,
        ),
    )
    drill = "oval 0 2" if malformed else f"oval 0.6 2.1 (offset {offset} 0)"
    router._loaded_component_holes = pickle.loads(pickle.dumps(holes(drill, rotation=angle)))
    via = Via(x=14, y=20, diameter=0.4, drill=0.2, layers=(Layer.F_CU, Layer.B_CU), net=1)
    segment = Segment(x1=14, y1=20, x2=16, y2=20, width=0.2, layer=Layer.F_CU, net=1)
    router.routes = [Route(net=1, net_name="SIG", vias=[via], segments=[segment])]
    radians = math.radians(angle)
    center = (10, 20)  # Native hole anchor is independent of copper offset.
    tip = (center[0] + 1.2 * math.sin(radians), center[1] + 1.2 * math.cos(radians))
    side = (center[0] + math.cos(radians), center[1] - math.sin(radians))
    return router, via, tip, side


@pytest.mark.parametrize("angle,offset", [(0, 0), (37, 0.4), (90, 1)])
def test_destination_and_transaction_findings_use_exact_capsule(angle, offset):
    router, via, tip, side = scene(angle, offset)
    context = drc_nudge._build_via_dest_context(via, router, (-1, -1, -1, -1))
    assert context.component_holes[0].drill == 0
    assert (
        drc_nudge._via_destination_blocked(via, *tip, context, required_clearance=0.1)
        == "via_pad_dest_hole_to_hole"
    )
    assert drc_nudge._via_destination_blocked(via, *side, context, required_clearance=0.1) is None
    via.x, via.y = tip
    findings = drc_nudge._via_pad_process_findings(via, router)
    assert len(findings) == 1
    assert next(iter(findings))[2] == pytest.approx(0.05)
    via.x, via.y = side
    assert drc_nudge._via_pad_process_findings(via, router) == set()


@pytest.mark.parametrize("legal", [False, True])
@pytest.mark.parametrize("angle,offset", [(0, 0), (37, 0.4), (90, 1)])
def test_transaction_rechecks_proposal_and_restores_exact_route(monkeypatch, legal, angle, offset):
    router, via, tip, side = scene(angle, offset)
    segment = router.routes[0].segments[0]
    before = pickle.dumps([dataclasses.asdict(route) for route in router.routes])
    target = side if legal else tip

    def proposal(*args, **kwargs):
        # Inject only the proposal: exercise the real transaction's physical
        # post-check and restoration even if an upstream guard misses it.
        via.x, via.y = target
        segment.x1, segment.y1 = target
        return True

    monkeypatch.setattr(drc_nudge, "_try_nudge_via_pad", proposal)
    accepted = drc_nudge._try_nudge_via_pad_transaction(via, (-1, -1, -1, -1), router, 10)
    assert accepted is legal
    assert router.routes[0].vias[0] is via
    assert router.routes[0].segments[0] is segment
    if legal:
        assert (via.x, via.y) == target
        assert (segment.x1, segment.y1) == target
    else:
        assert pickle.dumps([dataclasses.asdict(route) for route in router.routes]) == before


def test_unknown_slot_prevents_real_transaction_without_mutation():
    router, via, _, _ = scene(malformed=True)
    before = pickle.dumps(router.routes)
    context = drc_nudge._build_via_dest_context(via, router, (-1, -1, -1, -1))
    assert (
        drc_nudge._via_destination_blocked(via, 11, 20, context, required_clearance=0.1)
        == "via_pad_dest_unknown_holes"
    )
    assert ("unknown_component_holes", 0, 0.0) in drc_nudge._via_pad_process_findings(via, router)
    assert not drc_nudge._try_nudge_via_pad_transaction(via, (13.5, 19.5, 14.5, 20.5), router, 10)
    assert pickle.dumps(router.routes) == before
