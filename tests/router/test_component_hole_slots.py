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
def test_offset_rotates_with_pad_and_survives_worker_serialization(drill):
    census = pickle.loads(pickle.dumps(holes(drill, rotation=90)))
    assert census[0].x == pytest.approx(10)
    assert census[0].y == pytest.approx(19)
    result = resolve_component_hole_context(11, 19, 0.2, all_pads=census)
    assert result.known
    assert result.nearest_distance_mm == pytest.approx(0.6)


def test_malformed_offset_is_unknown():
    result = resolve_component_hole_context(100, 100, 0.2, all_pads=holes("0.6 (offset bad 0)"))
    assert not result.known
