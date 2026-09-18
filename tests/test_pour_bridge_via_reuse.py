"""Bounded via-hop bridge planning must reuse an existing same-net barrel.

Issue #5507.  The Board07 ``+1V2`` pad ``U4.C2`` stayed open because the
recipe's last pour-repair stage insisted on drilling a *new* via at both ends
of its bridge.  When the copper nearest the gap is already a via barrel that
is physically impossible: the barrel's own copper is a disc barely wider than
its annulus, so every retreat point either misses the disc (rejected by the
"must land on the component" guard) or lands inside it, where the fab
drill-to-drill floor rejects the drill.  The pair was skipped for every
retreat on every layer and the component was reported UNREPAIRED.

These are physical controls, not a relaxation: the predicates below model the
real rejections (a drill too close to the existing barrel, foreign copper on a
layer), and the passing cases must clear exactly the same predicates the
failing ones do.
"""

from __future__ import annotations

import math

import pytest
from shapely.geometry import Point, box

from kicad_tools.zones.pour_bridge import BridgeSide, plan_via_hop_bridge

ALL_LAYERS = frozenset({"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"})
LAYERS = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
RETREATS = (0.5, 0.9, 1.4)

# Board07's repair constants: a 0.45 mm via on a 0.25 mm drill, held off any
# other drill by the 0.5 mm edge-to-edge fab floor.
VIA_R = 0.225
VIA_DRILL_R = 0.125
MIN_HOLE_TO_HOLE = 0.5

BARREL = (10.0, 0.0)


def _barrel_side() -> BridgeSide:
    return BridgeSide(Point(BARREL).buffer(VIA_R), ALL_LAYERS, "via")


def _drill_floor_via_ok(recorded: list) -> callable:
    """A ``via_ok`` that enforces the real drill-to-drill floor at ``BARREL``."""

    def via_ok(point):
        recorded.append(point)
        return math.dist(point, BARREL) >= MIN_HOLE_TO_HOLE + 2 * VIA_DRILL_R

    return via_ok


def test_existing_barrel_is_reused_instead_of_redrilled():
    """The destination barrel terminates the bridge with no new drill."""
    recorded: list = []
    plan = plan_via_hop_bridge(
        BridgeSide(box(0, -1, 5, 1), frozenset({"In1.Cu"}), "fill"),
        _barrel_side(),
        layers=LAYERS,
        retreats=RETREATS,
        via_ok=_drill_floor_via_ok(recorded),
        path_ok=lambda a, b, lay: True,
        new_via_layers=ALL_LAYERS,
    )
    assert plan is not None
    assert plan.destination.new_via is False
    assert plan.destination.point == pytest.approx(BARREL)
    assert plan.new_vias == (plan.source.point,)
    # The one new drill is on the fill side, and no drill was ever proposed
    # inside the barrel's exclusion -- the impossible candidate is not tried
    # and then rejected, it is never constructed.
    assert all(math.dist(p, BARREL) > MIN_HOLE_TO_HOLE for p in recorded)
    assert plan.source.new_via is True
    assert box(0, -1, 5, 1).intersects(Point(plan.source.point))


def test_a_barrel_destination_is_unreachable_without_reuse():
    """Red control: requiring a drill at both ends cannot clear this pair.

    This is the pre-fix behaviour expressed directly -- for every retreat the
    old stage would have tried, the destination candidate is both off the
    barrel's copper and inside its drill exclusion.
    """
    barrel = Point(BARREL).buffer(VIA_R)
    source = box(0, -1, 5, 1)
    pa, pb = (5.0, 0.0), (BARREL[0] - VIA_R, 0.0)
    ux = (pb[0] - pa[0]) / math.dist(pa, pb)
    for back in RETREATS:
        candidate = (pb[0] + ux * back, pb[1])
        on_barrel = barrel.intersects(Point(candidate))
        drillable = math.dist(candidate, BARREL) >= MIN_HOLE_TO_HOLE + 2 * VIA_DRILL_R
        assert not (on_barrel and drillable)
    assert source.distance(barrel) > 0


def test_two_fills_keep_the_legacy_two_via_bridge():
    """Nothing changes for the ordinary fill-to-fill hop: two new vias."""
    plan = plan_via_hop_bridge(
        BridgeSide(box(0, -1, 5, 1), frozenset({"In1.Cu"}), "fill"),
        BridgeSide(box(8, -1, 13, 1), frozenset({"In1.Cu"}), "fill"),
        layers=LAYERS,
        retreats=RETREATS,
        via_ok=lambda p: True,
        path_ok=lambda a, b, lay: True,
        new_via_layers=ALL_LAYERS,
    )
    assert plan is not None
    assert plan.layer == "F.Cu"
    assert [e.new_via for e in (plan.source, plan.destination)] == [True, True]
    # Nearest retreat first, backed off the gap into each component.
    assert plan.source.point[0] == pytest.approx(4.5)
    assert plan.destination.point[0] == pytest.approx(8.5)
    assert box(0, -1, 5, 1).intersects(Point(plan.source.point))
    assert box(8, -1, 13, 1).intersects(Point(plan.destination.point))
    assert len(plan.new_vias) == 2


def test_two_barrels_bridge_with_no_new_drill_at_all():
    far = (20.0, 0.0)
    plan = plan_via_hop_bridge(
        _barrel_side(),
        BridgeSide(Point(far).buffer(VIA_R), ALL_LAYERS, "via"),
        layers=LAYERS,
        retreats=RETREATS,
        via_ok=lambda p: pytest.fail("no drill may be proposed between two barrels"),
        path_ok=lambda a, b, lay: True,
        new_via_layers=ALL_LAYERS,
    )
    assert plan is not None and plan.new_vias == ()
    assert plan.source.point == pytest.approx(BARREL)
    assert plan.destination.point == pytest.approx(far)


def test_reuse_still_obeys_foreign_copper_on_every_layer():
    """A blocked chord is still rejected -- reuse relaxes no clearance."""
    plan = plan_via_hop_bridge(
        BridgeSide(box(0, -1, 5, 1), frozenset({"In1.Cu"}), "fill"),
        _barrel_side(),
        layers=LAYERS,
        retreats=RETREATS,
        via_ok=lambda p: True,
        path_ok=lambda a, b, lay: False,
        new_via_layers=ALL_LAYERS,
    )
    assert plan is None


def test_reused_barrel_never_bridges_on_a_layer_it_does_not_reach():
    """A blind barrel only terminates a bridge on the layers it spans."""
    attempted: list = []

    def path_ok(a, b, lay):
        attempted.append(lay)
        return True

    plan = plan_via_hop_bridge(
        BridgeSide(box(0, -1, 5, 1), ALL_LAYERS, "fill"),
        BridgeSide(Point(BARREL).buffer(VIA_R), frozenset({"In1.Cu", "In2.Cu"}), "via"),
        layers=LAYERS,
        retreats=RETREATS,
        via_ok=lambda p: True,
        path_ok=path_ok,
        new_via_layers=ALL_LAYERS,
    )
    assert plan is not None and plan.layer == "In1.Cu"
    assert "F.Cu" not in attempted and "B.Cu" not in attempted


def test_rejected_candidates_leave_the_input_geometry_untouched():
    source = BridgeSide(box(0, -1, 5, 1), frozenset({"In1.Cu"}), "fill")
    destination = _barrel_side()
    before = (source.geometry.wkb, destination.geometry.wkb)
    assert (
        plan_via_hop_bridge(
            source,
            destination,
            layers=LAYERS,
            retreats=RETREATS,
            via_ok=lambda p: False,
            path_ok=lambda a, b, lay: True,
            new_via_layers=ALL_LAYERS,
        )
        is None
    )
    assert (source.geometry.wkb, destination.geometry.wkb) == before


def test_candidate_order_is_retreat_then_layer():
    """Ordering is the stage's own: source retreat, destination retreat, layer."""
    seen: list = []

    def path_ok(a, b, lay):
        seen.append((round(a[0], 3), round(b[0], 3), lay))
        return len(seen) >= 6

    plan = plan_via_hop_bridge(
        BridgeSide(box(0, -1, 5, 1), ALL_LAYERS, "fill"),
        BridgeSide(box(8, -1, 13, 1), ALL_LAYERS, "fill"),
        layers=LAYERS,
        retreats=RETREATS,
        via_ok=lambda p: True,
        path_ok=path_ok,
        new_via_layers=ALL_LAYERS,
    )
    assert plan is not None
    assert seen[:5] == [
        (4.5, 8.5, "F.Cu"),
        (4.5, 8.5, "In1.Cu"),
        (4.5, 8.5, "In2.Cu"),
        (4.5, 8.5, "B.Cu"),
        (4.5, 8.9, "F.Cu"),
    ]
    assert plan.destination.point[0] == pytest.approx(8.9)
    assert plan.layer == "In1.Cu"
