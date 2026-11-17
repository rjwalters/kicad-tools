"""Physical drill slots must retain their extent, orientation and offset."""

import math
import pickle
from types import SimpleNamespace

import pytest

from kicad_tools.router.via_in_pad_eligibility import (
    component_holes_from_document,
    resolve_component_hole_context,
)
from kicad_tools.schema.pcb import Footprint, Pad
from kicad_tools.sexp import parse_string


def holes(drill="oval 0.6 2.1", rotation=0, footprint_rotation=0):
    pad = Pad.from_sexp(
        parse_string(
            f'(pad "" np_thru_hole oval (at 0 0 {rotation}) (size 1 3) '
            f'(drill {drill}) (layers "*.Cu" "*.Mask"))'
        )
    )
    fp = Footprint("slot", "F.Cu", (10, 20), footprint_rotation, "", "", pads=[pad])
    return component_holes_from_document(SimpleNamespace(footprints=[fp]))


@pytest.mark.parametrize("angle", [0, 37, 90, 180, 270])
@pytest.mark.parametrize("footprint_angle", [0, 53, 180])
def test_slot_clearance_is_exact_and_uses_absolute_pad_rotation(angle, footprint_angle):
    census = holes(rotation=angle, footprint_rotation=footprint_angle)
    # Slot spine ends at y=.75; its cap ends at1.05. Via radius=.1,
    # measured axial clearance at y=1.5 is therefore .35 (ineligible).
    a = math.radians(angle)
    context = resolve_component_hole_context(
        10 + 1.5 * math.sin(a), 20 + 1.5 * math.cos(a), 0.2, all_pads=census
    )
    assert context.known
    assert context.nearest_distance_mm == pytest.approx(0.35)
    # A point beside the middle clears the narrow width, avoiding an
    # unnecessarily pessimistic enclosing-circle approximation.
    context = resolve_component_hole_context(
        10 + math.cos(a), 20 - math.sin(a), 0.2, all_pads=census
    )
    assert context.known
    assert context.nearest_distance_mm == pytest.approx(0.6)


@pytest.mark.parametrize("drill", ["oval 0 2", "oval -1 2", "oval 0.6", "oval bad 2", "oval nan 2"])
def test_malformed_slot_is_unknown(drill):
    result = resolve_component_hole_context(100, 100, 0.2, all_pads=holes(drill))
    assert not result.known


@pytest.mark.parametrize("drill", ["oval 2.1 0.6 (offset 1 0)", "0.6 (offset 1 0)"])
def test_offset_keeps_hole_at_pad_anchor_through_worker_serialization(drill):
    census = pickle.loads(pickle.dumps(holes(drill, rotation=90)))
    assert census[0].x == pytest.approx(10)
    assert census[0].y == pytest.approx(20)
    result = resolve_component_hole_context(11, 20, 0.2, all_pads=census)
    assert result.known
    assert result.nearest_distance_mm == pytest.approx(0.6)


def test_malformed_offset_is_unknown():
    result = resolve_component_hole_context(100, 100, 0.2, all_pads=holes("0.6 (offset bad 0)"))
    assert not result.known


@pytest.mark.parametrize("angle", [0, 37, 90, 180, 270])
@pytest.mark.parametrize("offset", [False, True])
def test_nudge_guards_preserve_slot_tip_and_side(angle, offset):
    from kicad_tools.router.drc_nudge import (
        _build_via_dest_context,
        _via_destination_blocked,
        _via_pad_process_findings,
    )
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Via

    census = holes(
        "oval 0.6 2.1 (offset 1 0)" if offset else "oval 0.6 2.1",
        rotation=angle,
        footprint_rotation=53,
    )
    router = SimpleNamespace(
        _loaded_component_holes=census,
        all_pads=[],
        pads={},
        routes=[],
        existing_routes=[],
        rules=SimpleNamespace(min_hole_to_hole=0.25, min_drill_clearance=0.25, manufacturer=None),
    )
    a = math.radians(angle)
    cx, cy = census[0].x, census[0].y
    via = Via(x=cx + 3, y=cy + 3, drill=0.2, diameter=0.4, layers=(Layer.F_CU, Layer.B_CU), net=1)
    context = _build_via_dest_context(via, router, (-1, -1, -1, -1))
    # Tip gap .05 violates the .25 floor; side gap .60 is legal.
    for x, y, blocked in (
        (cx + 1.2 * math.sin(a), cy + 1.2 * math.cos(a), True),
        (cx + math.cos(a), cy - math.sin(a), False),
    ):
        reason = _via_destination_blocked(via, x, y, context, required_clearance=0.1)
        assert (reason == "via_pad_dest_hole_to_hole") == blocked
        via.x, via.y = x, y
        findings = _via_pad_process_findings(via, router)
        assert bool(findings) == blocked
        if blocked:
            assert any(kind == "hole" and gap == pytest.approx(0.05) for kind, _, gap in findings)


@pytest.mark.parametrize("drill", ["oval 0 2", "oval bad 2", "oval nan 2", "0.6 (offset bad 0)"])
def test_nudge_guards_refuse_unknown_slot_geometry(drill):
    from kicad_tools.router.drc_nudge import (
        _build_via_dest_context,
        _via_destination_blocked,
        _via_pad_process_findings,
    )
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Via

    router = SimpleNamespace(
        _loaded_component_holes=holes(drill),
        all_pads=[],
        pads={},
        routes=[],
        existing_routes=[],
        rules=SimpleNamespace(min_hole_to_hole=0.25, min_drill_clearance=0.25, manufacturer=None),
    )
    via = Via(x=100, y=100, drill=0.2, diameter=0.4, layers=(Layer.F_CU, Layer.B_CU), net=1)
    context = _build_via_dest_context(via, router, (-1, -1, -1, -1))
    assert (
        _via_destination_blocked(via, 101, 101, context, required_clearance=0.1)
        == "via_pad_dest_unknown_holes"
    )
    assert ("unknown_component_holes", 0, 0.0) in _via_pad_process_findings(via, router)


def test_offset_slot_matches_native_effective_hole_shape():
    # Native KiCad 10.0.6 fixture: footprint53deg, absolute pad37deg,
    # offset(.4,.2). Hole center(10,20), unlike copper ShapePos
    # (10.439817,19.919001); actual capsule endpoint gap at(9.4,20).
    census = holes("oval 0.6 2.1 (offset 0.4 0.2)", rotation=37, footprint_rotation=53)
    assert (census[0].x, census[0].y) == pytest.approx((10, 20))
    result = resolve_component_hole_context(9.4, 20, 0.2, all_pads=census)
    assert result.known
    assert result.nearest_distance_mm == pytest.approx(0.079181, abs=2e-6)


def test_offset_circle_keeps_native_hole_anchor():
    census = holes("0.6 (offset 0.4 0.2)", rotation=37, footprint_rotation=53)
    assert (census[0].x, census[0].y) == pytest.approx((10, 20))
    result = resolve_component_hole_context(9.4, 20, 0.2, all_pads=census)
    assert result.known
    assert result.nearest_distance_mm == pytest.approx(0.2)


def test_transaction_rejects_slot_tip_and_restores_via_and_trace(monkeypatch):
    from kicad_tools.router import drc_nudge
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment, Via
    from kicad_tools.router.rules import DesignRules

    router = Autorouter(30, 30, rules=DesignRules(min_hole_to_hole=0.25, min_drill_clearance=0.25))
    router._loaded_component_holes = holes()
    via = Via(x=13, y=20, drill=0.2, diameter=0.4, layers=(Layer.F_CU, Layer.B_CU), net=1)
    trace = Segment(13, 20, 14, 20, 0.1, Layer.F_CU, 1)
    router.routes = [Route(net=1, net_name="SIG", vias=[via], segments=[trace])]

    # Force a bad proposal to exercise the transaction's independent
    # post-check, even if the destination pre-check is bypassed.
    def propose(*args, **kwargs):
        via.x, via.y = 10, 21.2
        trace.x1, trace.y1 = via.x, via.y
        return True

    monkeypatch.setattr(drc_nudge, "_try_nudge_via_pad", propose)
    result = drc_nudge.DRCNudgeResult()
    assert not drc_nudge._try_nudge_via_pad_transaction(
        via, (12.5, 19.5, 13.5, 20.5), router, 5, result=result
    )
    assert (via.x, via.y) == (13, 20)
    assert (trace.x1, trace.y1, trace.x2, trace.y2) == (13, 20, 14, 20)
    assert result.skipped["via_pad_destination_blocked"] == 1
