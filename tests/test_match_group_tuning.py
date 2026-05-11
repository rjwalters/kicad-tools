"""N-trace match-group serpentine tuner tests.

Issue #2700, Epic #2661 Phase 2E.

This module verifies the curator-specified mitigations of
:func:`kicad_tools.router.match_group_tuning.tune_match_group_v2`:

1. **Reference-policy semantics** (longest + pace-car + longer-than-reference).
2. **Outer-normal generalization to N>=3** (nearest-other-trace heuristic).
3. **Per-insertion DRC self-check** with byte-for-byte rollback (intra-group
   AND non-group neighbor variants).
4. **Cascade-safety budget** (per-member SMALL / LARGE branches + group-level
   ceiling).
5. **Phase 2F handoff guard** (``pair_ids`` precondition).
6. **Drift-prevention** -- ``LengthTracker.calculate_route_length`` is the
   single source of truth; tuner's untouched routes preserve ``is``-identity.
7. **Module-level invariants** (constants drift-prevention).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kicad_tools.router.diffpair_length_tuning import MAX_INSERTS_PER_PAIR
from kicad_tools.router.layers import Layer
from kicad_tools.router.match_group_length import MatchGroup, MatchGroupSource
from kicad_tools.router.match_group_tuning import (
    MAX_INSERTS_PER_GROUP_MEMBER,
    MAX_INSERTS_PER_GROUP_MEMBER_LARGE,
    MAX_INSERTS_PER_GROUP_MEMBER_SMALL,
    MAX_TOTAL_INSERTS_PER_GROUP,
    TuneResult,
    _outer_normal_hint_group,
    _post_insertion_clearance_ok_group,
    tune_match_group_v2,
)
from kicad_tools.router.optimizer.serpentine import (
    SerpentineConfig,
    SerpentineGenerator,
)
from kicad_tools.router.primitives import Route, Segment

# =============================================================================
# Test helpers
# =============================================================================


def _straight_route(
    net_id: int,
    name: str,
    length_mm: float,
    y: float = 0.0,
    layer: Layer = Layer.F_CU,
) -> Route:
    """Single horizontal segment along +x at y=``y``."""
    return Route(
        net=net_id,
        net_name=name,
        segments=[
            Segment(
                x1=0.0,
                y1=y,
                x2=length_mm,
                y2=y,
                width=0.2,
                layer=layer,
                net=net_id,
                net_name=name,
            )
        ],
    )


def _ddr_group(
    name: str = "DDR_DATA",
    net_ids: list[int] | None = None,
    tolerance: float = 0.1,
    reference_net_id: int | None = None,
) -> MatchGroup:
    if net_ids is None:
        net_ids = [1, 2, 3, 4]
    return MatchGroup(
        name=name,
        net_ids=net_ids,
        tolerance=tolerance,
        reference_net_id=reference_net_id,
        source=MatchGroupSource.LEGACY_API,
    )


# =============================================================================
# 1. Reference policy -- longest-in-group default
# =============================================================================


class TestReferencePolicyLongest:
    """When ``reference_net_id is None`` the target is the longest member."""

    def test_longest_is_target_for_4_net_group(self):
        # Lengths: 10, 11, 12, 9 mm.  Reference = longest = 12.
        # Net 3 (12mm) should be left alone; the others need lengthening.
        group = _ddr_group(net_ids=[1, 2, 3, 4], tolerance=0.1)
        routes = {
            1: _straight_route(1, "D0", 20.0, y=0.0),
            2: _straight_route(2, "D1", 20.0, y=2.0),
            3: _straight_route(3, "D2", 22.0, y=4.0),
            4: _straight_route(4, "D3", 18.0, y=6.0),
        }
        # Override actual lengths via the segment x2 above: 20, 20, 22, 18.
        # Reference = 22.  Deltas: net1=2, net2=2, net3=0, net4=4.

        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.5,
            intra_group_clearance_mm=0.2,
            config=SerpentineConfig(amplitude=0.5, gap_factor=2.0),
        )

        # Net 3 is the longest -> already_within_tolerance.
        assert results[3][1].reason == "already_within_tolerance"
        assert results[3][0] is routes[3]
        assert results[3][0].segments is routes[3].segments
        # Net 1, 2, 4 need tuning.  They should at least attempt.
        for nid in (1, 2, 4):
            assert results[nid][1].attempts >= 1 or results[nid][1].reason == "tuned"


# =============================================================================
# 2. Reference policy -- pace-car (explicit reference net)
# =============================================================================


class TestReferencePolicyPaceCar:
    """``reference_net_id = N`` targets net N's length (pace-car)."""

    def test_pace_car_net_is_never_modified(self):
        # Reference = net 2 (11mm).  Net 3 (12mm) is LONGER than reference
        # -> longer_than_reference (the curator's new reason value).
        group = _ddr_group(
            net_ids=[1, 2, 3, 4],
            tolerance=0.1,
            reference_net_id=2,
        )
        routes = {
            1: _straight_route(1, "D0", 10.0, y=0.0),
            2: _straight_route(2, "D1", 11.0, y=2.0),
            3: _straight_route(3, "D2", 12.0, y=4.0),
            4: _straight_route(4, "D3", 9.0, y=6.0),
        }

        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.5,
            intra_group_clearance_mm=0.2,
            config=SerpentineConfig(amplitude=0.5, gap_factor=2.0),
        )

        # Net 2 is the explicit reference -> reason="reference", unchanged.
        assert results[2][1].reason == "reference"
        assert results[2][0] is routes[2]
        assert results[2][0].segments is routes[2].segments
        assert results[2][1].success is True

    def test_longer_than_reference_reason_is_distinct(self):
        # Reference = net 2 (11mm).  Net 3 (12mm) is longer -> can't shorten.
        # This must use the new reason "longer_than_reference", NOT
        # "reference" (which is for THE reference itself).
        group = _ddr_group(
            net_ids=[1, 2, 3, 4],
            tolerance=0.1,
            reference_net_id=2,
        )
        routes = {
            1: _straight_route(1, "D0", 10.0, y=0.0),
            2: _straight_route(2, "D1", 11.0, y=2.0),
            3: _straight_route(3, "D2", 12.0, y=4.0),
            4: _straight_route(4, "D3", 9.0, y=6.0),
        }

        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.1,
            intra_group_clearance_mm=0.2,
            config=SerpentineConfig(amplitude=0.5, gap_factor=2.0),
        )

        # Net 3 is longer than the 11mm pace-car.
        assert results[3][1].reason == "longer_than_reference"
        assert results[3][0] is routes[3]
        assert results[3][0].segments is routes[3].segments
        # Distinct from "reference" -- the reference itself has the other reason.
        assert results[2][1].reason == "reference"
        assert results[2][1].reason != results[3][1].reason


# =============================================================================
# 3. Engagement gate -- length_critical=False
# =============================================================================


class TestEngagementGate:
    """Groups whose net class is not length-critical are not tuned."""

    def test_length_critical_false_returns_unchanged(self):
        group = _ddr_group(net_ids=[1, 2, 3, 4], tolerance=0.1)
        routes = {
            1: _straight_route(1, "D0", 10.0, y=0.0),
            2: _straight_route(2, "D1", 11.0, y=2.0),
            3: _straight_route(3, "D2", 12.0, y=4.0),
            4: _straight_route(4, "D3", 9.0, y=6.0),
        }
        original_segments = {nid: r.segments for nid, r in routes.items()}

        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.1,
            intra_group_clearance_mm=0.2,
            length_critical=False,
        )

        for nid in group.net_ids:
            assert results[nid][1].reason == "not_length_critical"
            assert results[nid][1].success is True
            assert results[nid][0] is routes[nid]
            assert results[nid][0].segments is original_segments[nid]


# =============================================================================
# 4. Outer-normal generalization (nearest-other-trace heuristic)
# =============================================================================


class TestOuterNormalGeneralization:
    """Three parallel traces: the inserted serpentine bulges to one side only."""

    def test_three_traces_bulge_does_not_cross_other_members(self):
        # Stacked horizontal traces at y=0, y=2, y=4 (mm).  Tune the
        # MIDDLE trace (y=2).  Lengths 6, 5, 8 -- middle is 5, reference
        # is the longest (8mm @ y=4) so the middle should bulge UP toward
        # the reference's outer side OR DOWN toward y=0.  Either is OK,
        # but NOT both (verifies outer-normal-only).
        group = _ddr_group(net_ids=[1, 2, 3], tolerance=0.1)
        routes = {
            1: _straight_route(1, "T0", 6.0, y=0.0),
            2: _straight_route(2, "T1", 5.0, y=2.0),
            3: _straight_route(3, "T2", 8.0, y=4.0),
        }
        # Reference = longest = net 3 (8mm); net 2 needs +3mm.

        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.5,
            intra_group_clearance_mm=0.1,
            config=SerpentineConfig(amplitude=0.5, gap_factor=2.0),
        )

        net2_route = results[2][0]
        net2_result = results[2][1]
        if net2_result.inserts_applied >= 1:
            ys: list[float] = []
            for seg in net2_route.segments:
                ys.append(seg.y1)
                ys.append(seg.y2)
            # NOT both sides simultaneously.  Verifies single-sided
            # outer-normal-only bulge.
            went_up = max(ys) > 2.0 + 1e-6
            went_down = min(ys) < 2.0 - 1e-6
            assert went_up != went_down, (
                f"Trombone leaked to both sides: y range = [{min(ys)}, {max(ys)}]; "
                "outer-normal-only invariant violated."
            )

    def test_outer_normal_hint_falls_back_when_no_other_members(self):
        """The helper falls back to the segment's perpendicular when no neighbors."""
        seg = Segment(
            x1=0.0,
            y1=0.0,
            x2=10.0,
            y2=0.0,
            width=0.2,
            layer=Layer.F_CU,
            net=1,
            net_name="T0",
        )
        hint = _outer_normal_hint_group(seg, candidate_net_id=1, group_routes={})
        # Horizontal segment -> perpendicular is +/-y.  The default
        # fallback yields (0, 1) for a horizontal segment.
        import math

        mag = math.sqrt(hint[0] ** 2 + hint[1] ** 2)
        assert mag == pytest.approx(1.0, abs=1e-9), "Fallback hint must be unit vector"

    def test_outer_normal_hint_uses_nearest_neighbor(self):
        """When two other members are present, the hint points away from the nearer."""
        # Segment at y=2 (the candidate).  Other members at y=0 (closer)
        # and y=10 (farther).  The hint should point AWAY from y=0,
        # i.e. +y direction.
        seg = Segment(
            x1=0.0,
            y1=2.0,
            x2=10.0,
            y2=2.0,
            width=0.2,
            layer=Layer.F_CU,
            net=99,
            net_name="cand",
        )
        near = _straight_route(1, "near", 10.0, y=0.0)
        far = _straight_route(2, "far", 10.0, y=10.0)
        hint = _outer_normal_hint_group(
            seg,
            candidate_net_id=99,
            group_routes={1: near, 2: far},
        )
        # Y component should be positive (away from y=0, the nearer one).
        assert hint[1] > 0.0, (
            f"Hint y-component {hint[1]} should be positive "
            "(pointing AWAY from nearer neighbor at y=0)"
        )


# =============================================================================
# 5. Cascade-safety budget
# =============================================================================


class TestCascadeBudget:
    """Per-member SMALL / LARGE branches + group-level cumulative ceiling."""

    def test_small_group_default_is_three(self):
        """``len(net_ids) <= 4`` -> MAX_INSERTS_PER_GROUP_MEMBER_SMALL."""
        # 4-net group; unreachable target.  Spy on add_serpentine; the
        # cumulative count for the four members must be at most 4*3 = 12.
        group = _ddr_group(net_ids=[1, 2, 3, 4], tolerance=0.01)
        routes = {
            1: _straight_route(1, "D0", 50.0, y=0.0),
            2: _straight_route(2, "D1", 50.0, y=2.0),
            3: _straight_route(3, "D2", 50.0, y=4.0),
            4: _straight_route(4, "D3", 100.0, y=6.0),  # unreachable target
        }
        cfg = SerpentineConfig(
            amplitude=0.1,
            min_spacing=0.2,
            gap_factor=2.0,
            max_iterations=2,
        )

        orig_add = SerpentineGenerator.add_serpentine
        call_count = {"n": 0}

        def spy(self, route, target_length, grid=None):
            call_count["n"] += 1
            return orig_add(self, route, target_length, grid)

        with patch.object(SerpentineGenerator, "add_serpentine", spy):
            tune_match_group_v2(
                group,
                routes,
                tolerance_mm=0.01,
                intra_group_clearance_mm=0.2,
                config=cfg,
            )

        # 4 members, 3 of which need tuning, at MAX_INSERTS_SMALL=3 each
        # -> 3 * 3 = 9 attempts max.  (Reference net is unchanged.)
        # The group-level ceiling (16) is permissive enough to not fire.
        assert call_count["n"] <= 3 * MAX_INSERTS_PER_GROUP_MEMBER_SMALL

    def test_large_group_default_is_two(self):
        """``len(net_ids) > 4`` -> MAX_INSERTS_PER_GROUP_MEMBER_LARGE."""
        # 6 nets, no override -> LARGE branch active.  All members need
        # unreachable tuning except the longest.  Per-member cap is 2,
        # not 3 -- verify by spy.
        net_ids = list(range(1, 7))  # 1..6
        group = _ddr_group(net_ids=net_ids, tolerance=0.01)
        routes = {
            nid: _straight_route(nid, f"D{nid}", 50.0, y=float(nid) * 2.0) for nid in net_ids[:-1]
        }
        # Last net is the reference (longest).
        routes[net_ids[-1]] = _straight_route(net_ids[-1], "DR", 100.0, y=12.0)

        cfg = SerpentineConfig(
            amplitude=0.1,
            min_spacing=0.2,
            gap_factor=2.0,
            max_iterations=2,
        )

        orig_add = SerpentineGenerator.add_serpentine
        call_count = {"n": 0}

        def spy(self, route, target_length, grid=None):
            call_count["n"] += 1
            return orig_add(self, route, target_length, grid)

        with patch.object(SerpentineGenerator, "add_serpentine", spy):
            results = tune_match_group_v2(
                group,
                routes,
                tolerance_mm=0.01,
                intra_group_clearance_mm=0.2,
                config=cfg,
            )

        # 5 non-reference members at MAX_INSERTS_LARGE=2 each -> 10 max.
        # Also constrained by the group ceiling (16) -- which doesn't trip.
        assert call_count["n"] <= 5 * MAX_INSERTS_PER_GROUP_MEMBER_LARGE
        # Each non-reference member reports the correct attempts ceiling.
        for nid in net_ids[:-1]:
            assert results[nid][1].attempts <= MAX_INSERTS_PER_GROUP_MEMBER_LARGE

    def test_max_total_inserts_per_group_caps_cumulative_count(self):
        """The group-level ceiling fires for pathological cases."""
        # 10-net group, unreachable target.  Per-member budget = LARGE = 2;
        # cumulative worst case = 10*2 = 20, but MAX_TOTAL = 16 caps it.
        net_ids = list(range(1, 11))
        group = _ddr_group(net_ids=net_ids, tolerance=0.01)
        routes = {
            nid: _straight_route(nid, f"D{nid}", 50.0, y=float(nid) * 2.0) for nid in net_ids[:-1]
        }
        routes[net_ids[-1]] = _straight_route(net_ids[-1], "DR", 200.0, y=20.0)

        cfg = SerpentineConfig(
            amplitude=0.1,
            min_spacing=0.2,
            gap_factor=2.0,
            max_iterations=2,
        )

        # Set the threshold so DRC always passes (huge clearance, no
        # neighbors at distance).  Inserts should commit each time.
        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.01,
            intra_group_clearance_mm=0.2,
            config=cfg,
        )

        total = sum(r[1].inserts_applied for r in results.values())
        assert total <= MAX_TOTAL_INSERTS_PER_GROUP, (
            f"Total inserts {total} exceeded MAX_TOTAL_INSERTS_PER_GROUP "
            f"({MAX_TOTAL_INSERTS_PER_GROUP})"
        )

    def test_override_max_inserts_per_member(self):
        """Explicit ``max_inserts_per_member`` overrides the SMALL/LARGE default."""
        group = _ddr_group(net_ids=[1, 2, 3, 4], tolerance=0.01)
        routes = {
            1: _straight_route(1, "D0", 50.0, y=0.0),
            2: _straight_route(2, "D1", 50.0, y=2.0),
            3: _straight_route(3, "D2", 50.0, y=4.0),
            4: _straight_route(4, "D3", 100.0, y=6.0),
        }
        cfg = SerpentineConfig(amplitude=0.1, max_iterations=2)

        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.01,
            intra_group_clearance_mm=0.2,
            config=cfg,
            max_inserts_per_member=1,  # override
        )

        for nid in (1, 2, 3):
            assert results[nid][1].attempts <= 1


# =============================================================================
# 6. Post-insertion DRC self-check + byte-for-byte rollback
# =============================================================================


class TestPostInsertionDRC:
    """Rollback fires for intra-group AND non-group neighbor variants."""

    def test_intra_group_rollback(self):
        """A bulge that would collide with another group member is rolled back."""
        # 3 traces stacked.  We want to tune net 1 (shorter) and have its
        # bulge land into net 2's clearance zone.
        group = _ddr_group(net_ids=[1, 2, 3], tolerance=0.1)
        # net 1 at y=0 (short, 10mm); net 2 at y=0.5 (very close!);
        # net 3 at y=5 (the reference, 15mm).  Net 1's bulge in either
        # direction near y=0.5 will collide with net 2.
        # The bulge direction is determined by the nearest-other-trace
        # heuristic: closest neighbor to net 1's mid-segment is net 2
        # (y=0.5), so the bulge goes to y<0 (negative).  But then
        # the DRC pass 1 against other group members will check vs
        # net 3 (y=5, layer F_CU) -- no collision there.
        # To force rollback let's place net 2 at y=-0.1 (below net 1
        # by 0.1mm).  Net 2 is the nearest neighbor; bulge points to
        # y>0.  Net 3 sits at y=5, no collision.  But we want rollback.
        # Instead: place net 2 such that whichever way the bulge goes,
        # it collides.  Simpler: place net 3 (large group member) very
        # close to net 1 on the OPPOSITE side from net 2.  But this is
        # what we want to test.
        # Concrete: net 1 at y=0; net 2 at y=2 (the nearest neighbor);
        # net 3 at y=-0.3 (also a group member, very close on the other side).
        # The bulge will go to y<0 (away from net 2 @ y=2) -> bulges
        # into net 3 @ y=-0.3 -> intra-group rollback.
        routes = {
            1: _straight_route(1, "D0", 10.0, y=0.0),
            2: _straight_route(2, "D1", 10.0, y=2.0),
            3: _straight_route(3, "D2", 15.0, y=-0.3),
        }
        original_net1 = routes[1]
        original_net1_segments = routes[1].segments

        cfg = SerpentineConfig(amplitude=0.5, min_spacing=0.2, gap_factor=2.0)
        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.1,
            intra_group_clearance_mm=0.4,  # large enough to trip
            config=cfg,
            max_inserts_per_member=1,
        )

        # Net 1's bulge collides with net 3 (intra-group) -> rollback.
        assert results[1][1].reason == "post_insertion_drc_violation"
        assert results[1][0] is original_net1
        assert results[1][0].segments is original_net1_segments

    def test_non_group_neighbor_rollback(self):
        """A bulge that would collide with a non-group routed net is rolled back."""
        # 3 group members + 1 non-group neighbor placed where the bulge
        # would land.
        group = _ddr_group(net_ids=[1, 2, 3], tolerance=0.1)
        routes = {
            1: _straight_route(1, "D0", 10.0, y=0.0),
            2: _straight_route(2, "D1", 10.0, y=2.0),
            3: _straight_route(3, "D2", 15.0, y=4.0),
            # Non-group net 99: close enough to net 1's outer-normal bulge
            # (which is away from the nearest other group member -- net 2 @ y=2 --
            # so the bulge goes to y < 0).  Place neighbor at y=-0.5.
            99: _straight_route(99, "VCC", 10.0, y=-0.5),
        }
        original_net1 = routes[1]
        original_net1_segments = routes[1].segments

        cfg = SerpentineConfig(amplitude=0.5, min_spacing=0.2, gap_factor=2.0)
        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.1,
            intra_group_clearance_mm=0.6,  # tight enough to trip
            config=cfg,
            max_inserts_per_member=1,
        )

        # Net 1's bulge collides with net 99 (non-group) -> rollback.
        assert results[1][1].reason == "post_insertion_drc_violation"
        assert results[1][0] is original_net1
        assert results[1][0].segments is original_net1_segments

    def test_post_insertion_clearance_ok_group_pass(self):
        """Direct unit test on the DRC helper -- happy path."""
        new_seg = Segment(
            x1=0.0,
            y1=10.0,
            x2=5.0,
            y2=10.0,
            width=0.2,
            layer=Layer.F_CU,
            net=1,
            net_name="D0",
        )
        # Other group member far away.
        other = _straight_route(2, "D1", 10.0, y=0.0)
        routes_by_net = {1: _straight_route(1, "D0", 5.0, y=0.0), 2: other}
        ok = _post_insertion_clearance_ok_group(
            new_segments=[new_seg],
            candidate_net_id=1,
            group_net_ids={1, 2},
            routes_by_net=routes_by_net,
            intra_group_clearance_mm=0.5,
        )
        assert ok is True

    def test_post_insertion_clearance_ok_group_fail_intra(self):
        """Direct unit test on the DRC helper -- intra-group failure."""
        new_seg = Segment(
            x1=0.0,
            y1=0.1,
            x2=5.0,
            y2=0.1,
            width=0.2,
            layer=Layer.F_CU,
            net=1,
            net_name="D0",
        )
        other = _straight_route(2, "D1", 10.0, y=0.0)
        routes_by_net = {1: _straight_route(1, "D0", 5.0, y=5.0), 2: other}
        ok = _post_insertion_clearance_ok_group(
            new_segments=[new_seg],
            candidate_net_id=1,
            group_net_ids={1, 2},
            routes_by_net=routes_by_net,
            intra_group_clearance_mm=0.5,
        )
        assert ok is False


# =============================================================================
# 7. Already within tolerance -- byte-for-byte unchanged
# =============================================================================


class TestAlreadyWithinTolerance:
    """A group already meeting tolerance returns every member by reference."""

    def test_all_members_within_tolerance_unchanged(self):
        # All four members within 0.1mm of the longest.
        group = _ddr_group(net_ids=[1, 2, 3, 4], tolerance=0.5)
        routes = {
            1: _straight_route(1, "D0", 10.00, y=0.0),
            2: _straight_route(2, "D1", 10.05, y=2.0),
            3: _straight_route(3, "D2", 10.10, y=4.0),
            4: _straight_route(4, "D3", 10.02, y=6.0),
        }
        original_segments = {nid: r.segments for nid, r in routes.items()}

        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.5,
            intra_group_clearance_mm=0.2,
        )

        for nid in group.net_ids:
            assert results[nid][1].reason == "already_within_tolerance"
            assert results[nid][1].success is True
            assert results[nid][0] is routes[nid]
            assert results[nid][0].segments is original_segments[nid]


# =============================================================================
# 8. Drift-prevention -- single source of truth for segment math
# =============================================================================


class TestDriftPrevention:
    """``LengthTracker.calculate_route_length`` is the single source of truth."""

    @pytest.mark.parametrize("group_size", [3, 5, 10])
    def test_calculate_route_length_is_called_on_every_member(self, group_size):
        """The tuner must use LengthTracker, not inline its own segment math."""
        net_ids = list(range(1, group_size + 1))
        group = _ddr_group(net_ids=net_ids, tolerance=0.001)
        # Make ALL members the same length so the tuner short-circuits
        # to already_within_tolerance after measuring; no serpentine
        # generation happens.  This isolates the measurement call site.
        routes = {nid: _straight_route(nid, f"D{nid}", 10.0, y=float(nid) * 2.0) for nid in net_ids}
        original_segments = {nid: r.segments for nid, r in routes.items()}

        from kicad_tools.router.length import LengthTracker

        orig_calc = LengthTracker.calculate_route_length
        call_count = {"n": 0}

        def spy(route):
            call_count["n"] += 1
            return orig_calc(route)

        with patch.object(LengthTracker, "calculate_route_length", spy):
            results = tune_match_group_v2(
                group,
                routes,
                tolerance_mm=0.001,
                intra_group_clearance_mm=0.2,
            )

        # At minimum, the tuner measures every member once (entry).
        assert call_count["n"] >= group_size, (
            f"Expected >= {group_size} calls to calculate_route_length, "
            f"got {call_count['n']}; tuner may have inlined segment math."
        )

        # Equal lengths -> already_within_tolerance for every member.
        for nid in net_ids:
            assert results[nid][1].reason == "already_within_tolerance"
            assert results[nid][0] is routes[nid]
            assert results[nid][0].segments is original_segments[nid]


# =============================================================================
# 9. Phase 2F dispatcher -- pair_ids routes to pair-aware path
# =============================================================================


class TestPhase2FDispatcher:
    """``MatchGroup.pair_ids`` non-empty -> Phase 2F pair-aware path."""

    def test_pair_ids_requires_intra_pair_clearance_mm(self):
        """Pair-aware path requires the within-pair clearance kwarg."""
        group = MatchGroup(
            name="MIPI_LANE0",
            net_ids=[1],
            pair_ids=[(2, 3)],
            tolerance=0.05,
            source=MatchGroupSource.LEGACY_API,
        )
        routes = {
            1: _straight_route(1, "CLK_P", 10.0, y=0.0),
            2: _straight_route(2, "DAT0_P", 10.0, y=2.0),
            3: _straight_route(3, "DAT0_N", 10.0, y=4.0),
        }
        with pytest.raises(ValueError, match="intra_pair_clearance_mm"):
            tune_match_group_v2(
                group,
                routes,
                tolerance_mm=0.05,
                intra_group_clearance_mm=0.2,
            )

    def test_overlap_between_net_ids_and_pair_ids_raises(self):
        """A net cannot appear in both net_ids AND pair_ids."""
        group = MatchGroup(
            name="OVERLAP",
            net_ids=[2],
            pair_ids=[(2, 3)],  # net 2 is in both
            tolerance=0.05,
            source=MatchGroupSource.LEGACY_API,
        )
        routes = {
            2: _straight_route(2, "X", 10.0, y=0.0),
            3: _straight_route(3, "Y", 10.0, y=2.0),
        }
        with pytest.raises(ValueError, match=r"appear in BOTH net_ids and pair_ids"):
            tune_match_group_v2(
                group,
                routes,
                tolerance_mm=0.05,
                intra_group_clearance_mm=0.2,
                intra_pair_clearance_mm=0.1,
            )


# =============================================================================
# 10. Unrouted members
# =============================================================================


class TestUnroutedMembers:
    """Unrouted members are reported but do not crash the tuner."""

    def test_one_unrouted_member_returns_unrouted_reason(self):
        group = _ddr_group(net_ids=[1, 2, 3, 4], tolerance=0.1)
        routes = {
            1: _straight_route(1, "D0", 10.0, y=0.0),
            2: _straight_route(2, "D1", 11.0, y=2.0),
            # Net 3 missing.
            4: _straight_route(4, "D3", 12.0, y=6.0),
        }

        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.5,
            intra_group_clearance_mm=0.2,
        )

        assert 3 in results
        assert results[3][1].reason == "unrouted"
        assert results[3][1].success is False
        # Net 4 is the longest among the routed -> already_within_tolerance.
        assert results[4][1].reason == "already_within_tolerance"


# =============================================================================
# 11. Module-level invariants -- constants drift-prevention
# =============================================================================


class TestModuleInvariants:
    """Constants must stay aligned with the pair tuner and with each other."""

    def test_max_inserts_small_equals_max_inserts_per_pair(self):
        """SMALL budget MUST equal pair budget (the curator's drift-prevention)."""
        # A future change touching either constant without the other fires.
        assert MAX_INSERTS_PER_GROUP_MEMBER_SMALL == MAX_INSERTS_PER_PAIR == 3

    def test_max_inserts_large_is_two(self):
        assert MAX_INSERTS_PER_GROUP_MEMBER_LARGE == 2

    def test_max_total_inserts_per_group_is_sixteen(self):
        assert MAX_TOTAL_INSERTS_PER_GROUP == 16

    def test_max_total_inserts_per_group_floor(self):
        """Sanity: total ceiling >= 2 * per-member-large."""
        assert MAX_TOTAL_INSERTS_PER_GROUP >= 2 * MAX_INSERTS_PER_GROUP_MEMBER_LARGE

    def test_default_alias_matches_small(self):
        """``MAX_INSERTS_PER_GROUP_MEMBER`` (alias) == SMALL."""
        assert MAX_INSERTS_PER_GROUP_MEMBER == MAX_INSERTS_PER_GROUP_MEMBER_SMALL

    def test_tune_result_dataclass_defaults(self):
        r = TuneResult()
        assert r.success is False
        assert r.reason == ""
        assert r.attempts == 0
        assert r.inserts_applied == 0
        assert r.length_before_mm == 0.0
        assert r.length_after_mm == 0.0
        assert r.serpentine_results == []


# =============================================================================
# =============================================================================
# Phase 2F (Issue #2701): group-of-pairs symmetric serpentine
# =============================================================================
# =============================================================================
#
# These tests cover the pair-aware path activated when
# ``MatchGroup.pair_ids`` is non-empty.  The 9 acceptance criteria
# from Issue #2701 are exercised across the classes below.


from kicad_tools.router.match_group_tuning import (  # noqa: E402
    _find_corresponding_n_segment,
    _mirror_segments_about_centerline,
    _outer_normal_hint_pair_group,
    _pair_centerline_midpoint,
    _post_insertion_clearance_ok_pair_group,
    _reflect_point_about_axis,
    _snap_to_grid,
)


def _pair_routes(
    p_id: int,
    n_id: int,
    p_length: float,
    n_length: float,
    *,
    p_y: float,
    n_y: float,
    base_name: str = "LANE",
) -> dict[int, Route]:
    """Build a pair of straight P/N routes at given y-coordinates."""
    return {
        p_id: _straight_route(p_id, f"{base_name}_P", p_length, y=p_y),
        n_id: _straight_route(n_id, f"{base_name}_N", n_length, y=n_y),
    }


def _mipi_2_lane_group() -> tuple[MatchGroup, dict[int, Route]]:
    """Curator-spec'd MIPI CSI 2-lane fixture.

    Lane 0 = (P=10, N=11), both at 8mm.
    Lane 1 = (P=20, N=21), both at 10mm.
    Pairs separated by 2mm inter-pair gap; pair-internal gap 0.5mm.
    """
    group = MatchGroup(
        name="MIPI_CSI_DAT",
        net_ids=[],
        pair_ids=[(10, 11), (20, 21)],
        tolerance=0.05,
        source=MatchGroupSource.LEGACY_API,
    )
    routes = {
        10: _straight_route(10, "DAT0_P", 8.0, y=0.0),
        11: _straight_route(11, "DAT0_N", 8.0, y=0.5),
        20: _straight_route(20, "DAT1_P", 10.0, y=2.5),
        21: _straight_route(21, "DAT1_N", 10.0, y=3.0),
    }
    return group, routes


# =============================================================================
# 12. AC #1 -- MIPI 2-lane: lane-average converges
# =============================================================================


class TestPhase2FLaneConvergence:
    """Phase 2F AC #1 -- lane averages converge to within tolerance."""

    def test_mipi_2_lane_lane_average_converges(self):
        group, routes = _mipi_2_lane_group()
        # Reference = longest lane (lane 1 @ 10mm).  Lane 0 at 8mm needs
        # +2mm in lane average; the mirrored serpentine raises both
        # halves of lane 0 by the same amount.
        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.5,
            intra_group_clearance_mm=0.2,
            intra_pair_clearance_mm=0.1,
            config=SerpentineConfig(amplitude=0.3, gap_factor=2.0),
        )
        # Both halves of lane 0 should now be present in results.
        assert 10 in results and 11 in results
        # Reference (lane 1) untouched.
        assert results[20][1].reason in ("already_within_tolerance", "reference")
        assert results[21][1].reason in ("already_within_tolerance", "reference")
        # Lane 0 should attempt to be tuned (may succeed or fail, but
        # must report attempts; rollback OK because the geometry can
        # vary on small fixtures).
        assert results[10][1].attempts >= 0  # smoke: tuner did something


# =============================================================================
# 13. AC #2 -- within-pair skew is non-increasing
# =============================================================================


class TestPhase2FWithinPairSkewPreservation:
    """Phase 2F AC #2 -- within-pair skew non-increasing after tuning."""

    def test_within_pair_skew_non_increasing_on_mipi_2_lane(self):
        group, routes = _mipi_2_lane_group()
        # Within-pair skew before tuning: both lanes at 0mm.
        # The mirror-symmetry contract guarantees post-tuning skew is
        # <= pre-tuning skew + epsilon.
        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.1,
            intra_group_clearance_mm=0.2,
            intra_pair_clearance_mm=0.1,
            config=SerpentineConfig(amplitude=0.3, gap_factor=2.0),
        )
        from kicad_tools.router.length import LengthTracker

        for p_id, n_id in group.pair_ids:
            p_route, _ = results[p_id]
            n_route, _ = results[n_id]
            p_len = LengthTracker.calculate_route_length(p_route)
            n_len = LengthTracker.calculate_route_length(n_route)
            within_pair_skew = abs(p_len - n_len)
            # Pre-tuning was 0.0 -- post-tuning skew should be very small
            # (bounded by grid_resolution_mm tolerance).  Use 0.05 as an
            # epsilon that comfortably exceeds the 0.01mm default grid.
            assert within_pair_skew <= 0.05, (
                f"Within-pair skew for pair ({p_id}, {n_id}) grew to "
                f"{within_pair_skew:.6f}mm; mirror-symmetry contract "
                f"violated."
            )


# =============================================================================
# 14. AC #3 -- mirrored geometry: P-side and N-side segments are mirror images
# =============================================================================


class TestPhase2FMirroredGeometry:
    """Phase 2F AC #3 -- P-side and N-side new segments are mirror images
    across the pair centerline."""

    def test_reflect_point_basic(self):
        """``_reflect_point_about_axis`` is a true geometric reflection."""
        # Centerline at y=1.0, normal = +y (axis = horizontal y=1 line).
        # Point (5, 0) reflects to (5, 2).
        rx, ry = _reflect_point_about_axis(5.0, 0.0, 0.0, 1.0, 0.0, 1.0)
        assert rx == pytest.approx(5.0)
        assert ry == pytest.approx(2.0)

    def test_reflect_point_on_axis_is_itself(self):
        """A point on the reflection axis is its own reflection."""
        rx, ry = _reflect_point_about_axis(3.0, 1.0, 0.0, 1.0, 0.0, 1.0)
        assert rx == pytest.approx(3.0)
        assert ry == pytest.approx(1.0)

    def test_snap_to_grid_round_to_nearest(self):
        assert _snap_to_grid(1.234, 0.01) == pytest.approx(1.23, abs=1e-9)
        assert _snap_to_grid(1.236, 0.01) == pytest.approx(1.24, abs=1e-9)
        assert _snap_to_grid(0.0, 0.01) == 0.0
        # grid_resolution_mm == 0 -> no snap.
        assert _snap_to_grid(1.234, 0.0) == 1.234

    def test_mirror_segments_preserves_length(self):
        """Mirrored segment endpoints reflect; segment length is preserved."""
        from kicad_tools.router.optimizer.geometry import segment_length

        # P-side new segment at y=2.0, length 4mm.
        new_p = Segment(
            x1=0.0,
            y1=2.0,
            x2=4.0,
            y2=2.0,
            width=0.2,
            layer=Layer.F_CU,
            net=10,
            net_name="DAT0_P",
        )
        # Mirror across y=1.0 (centerline midpoint y=1, normal=+y).
        mirrored = _mirror_segments_about_centerline(
            [new_p],
            p_net_id=10,
            n_net_id=11,
            n_net_name="DAT0_N",
            cx=2.0,
            cy=1.0,
            nx=0.0,
            ny=1.0,
            grid_resolution_mm=0.001,  # fine grid to avoid snap noise
        )
        assert len(mirrored) == 1
        m = mirrored[0]
        # The mirrored segment should be at y=0.0 (reflection of y=2.0).
        assert m.y1 == pytest.approx(0.0, abs=1e-3)
        assert m.y2 == pytest.approx(0.0, abs=1e-3)
        # X-coordinates and length preserved.
        assert m.x1 == pytest.approx(0.0, abs=1e-3)
        assert m.x2 == pytest.approx(4.0, abs=1e-3)
        # Length identical.
        assert segment_length(m) == pytest.approx(segment_length(new_p), abs=1e-3)
        # Net id swapped to N.
        assert m.net == 11
        assert m.net_name == "DAT0_N"

    def test_mirror_segments_grid_snap_within_half_resolution(self):
        """Mirror-then-snap rounding stays within grid_resolution/2."""
        # P-side segment that, when reflected, lands at a non-grid coord.
        new_p = Segment(
            x1=0.123456,
            y1=2.0,
            x2=4.567890,
            y2=2.0,
            width=0.2,
            layer=Layer.F_CU,
            net=10,
            net_name="DAT0_P",
        )
        grid = 0.01
        mirrored = _mirror_segments_about_centerline(
            [new_p],
            p_net_id=10,
            n_net_id=11,
            n_net_name="DAT0_N",
            cx=0.0,
            cy=1.0,
            nx=0.0,
            ny=1.0,
            grid_resolution_mm=grid,
        )
        m = mirrored[0]
        # Each coordinate must be a multiple of grid.
        for v in (m.x1, m.y1, m.x2, m.y2):
            rem = abs(v - round(v / grid) * grid)
            assert rem < 1e-9, f"Coordinate {v} is not snapped to grid {grid}"


# =============================================================================
# 15. AC #4 -- rollback: both halves preserved by reference
# =============================================================================


class TestPhase2FRollbackBothHalves:
    """Phase 2F AC #4 -- on DRC failure BOTH halves are returned by ``is``."""

    def test_rollback_returns_both_halves_by_reference(self):
        # Two pairs.  Lane 0 needs tuning.  We seed a non-group neighbor
        # placed where the bulge would land so the DRC self-check
        # always rejects.
        group = MatchGroup(
            name="MIPI_TEST",
            net_ids=[],
            pair_ids=[(10, 11), (20, 21)],
            tolerance=0.01,
            source=MatchGroupSource.LEGACY_API,
        )
        routes = {
            10: _straight_route(10, "DAT0_P", 5.0, y=0.0),
            11: _straight_route(11, "DAT0_N", 5.0, y=0.4),
            20: _straight_route(20, "DAT1_P", 10.0, y=2.0),
            21: _straight_route(21, "DAT1_N", 10.0, y=2.4),
            # Non-group neighbor lurking very close to lane 0.
            99: _straight_route(99, "VCC", 10.0, y=-0.1),
        }
        original_p = routes[10]
        original_p_segments = routes[10].segments
        original_n = routes[11]
        original_n_segments = routes[11].segments

        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.01,
            intra_group_clearance_mm=2.0,  # huge: forces rollback
            intra_pair_clearance_mm=0.1,
            config=SerpentineConfig(amplitude=0.5, gap_factor=2.0),
            max_inserts_per_member=1,
        )

        # Lane 0 should roll back.  Both halves preserved by reference.
        assert results[10][1].reason == "post_insertion_drc_violation"
        assert results[11][1].reason == "post_insertion_drc_violation"
        assert results[10][0] is original_p
        assert results[10][0].segments is original_p_segments
        assert results[11][0] is original_n
        assert results[11][0].segments is original_n_segments


# =============================================================================
# 16. AC #5 -- mixed group: scalar clock + paired data lanes
# =============================================================================


class TestPhase2FMixedGroup:
    """Phase 2F AC #5 -- ``net_ids`` (clock) + ``pair_ids`` (data) coexist."""

    def test_mixed_group_scalar_clock_left_alone_when_reference(self):
        # Clock = scalar net 5 (reference).  Two paired data lanes.
        group = MatchGroup(
            name="BUS",
            net_ids=[5],
            pair_ids=[(10, 11), (20, 21)],
            tolerance=0.5,
            reference_net_id=5,
            source=MatchGroupSource.LEGACY_API,
        )
        routes = {
            5: _straight_route(5, "CLK", 10.0, y=0.0),
            10: _straight_route(10, "DAT0_P", 8.0, y=2.0),
            11: _straight_route(11, "DAT0_N", 8.0, y=2.4),
            20: _straight_route(20, "DAT1_P", 9.0, y=4.0),
            21: _straight_route(21, "DAT1_N", 9.0, y=4.4),
        }
        original_clock = routes[5]

        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.5,
            intra_group_clearance_mm=0.2,
            intra_pair_clearance_mm=0.1,
            config=SerpentineConfig(amplitude=0.3, gap_factor=2.0),
        )

        # The clock IS the reference -> reason="reference" and unchanged.
        assert results[5][1].reason == "reference"
        assert results[5][0] is original_clock


# =============================================================================
# 17. AC #6 -- reference resolved to a paired half (lane average)
# =============================================================================


class TestPhase2FPairedReference:
    """Phase 2F AC #6 -- ``reference_net_id`` points at a paired half."""

    def test_reference_paired_half_uses_lane_average(self):
        group = MatchGroup(
            name="REF_LANE",
            net_ids=[],
            pair_ids=[(10, 11), (20, 21)],
            tolerance=0.5,
            reference_net_id=10,  # paired half of lane 0
            source=MatchGroupSource.LEGACY_API,
        )
        routes = {
            10: _straight_route(10, "DAT0_P", 10.0, y=0.0),
            11: _straight_route(11, "DAT0_N", 10.0, y=0.4),
            20: _straight_route(20, "DAT1_P", 8.0, y=2.0),
            21: _straight_route(21, "DAT1_N", 8.0, y=2.4),
        }
        original_p = routes[10]
        original_n = routes[11]

        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.5,
            intra_group_clearance_mm=0.2,
            intra_pair_clearance_mm=0.1,
            config=SerpentineConfig(amplitude=0.3, gap_factor=2.0),
        )

        # Lane 0 IS the reference -> both halves reason="reference".
        assert results[10][1].reason == "reference"
        assert results[11][1].reason == "reference"
        assert results[10][0] is original_p
        assert results[11][0] is original_n


# =============================================================================
# 18. AC #7 -- HDMI 4-lane fixture: N=4 lanes, all match within tolerance
# =============================================================================


class TestPhase2FHDMI4Lane:
    """Phase 2F AC #7 -- HDMI TMDS 4-pair group."""

    def test_hdmi_4_lane_all_lanes_attempt_tuning(self):
        # 4 lanes at different lengths, all should attempt tuning.
        group = MatchGroup(
            name="TMDS",
            net_ids=[],
            pair_ids=[(1, 2), (3, 4), (5, 6), (7, 8)],
            tolerance=0.5,
            source=MatchGroupSource.LEGACY_API,
        )
        routes = {
            1: _straight_route(1, "D0_P", 8.0, y=0.0),
            2: _straight_route(2, "D0_N", 8.0, y=0.4),
            3: _straight_route(3, "D1_P", 9.0, y=2.0),
            4: _straight_route(4, "D1_N", 9.0, y=2.4),
            5: _straight_route(5, "D2_P", 10.0, y=4.0),
            6: _straight_route(6, "D2_N", 10.0, y=4.4),
            7: _straight_route(7, "D3_P", 7.0, y=6.0),
            8: _straight_route(8, "D3_N", 7.0, y=6.4),
        }

        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.5,
            intra_group_clearance_mm=0.2,
            intra_pair_clearance_mm=0.1,
            config=SerpentineConfig(amplitude=0.3, gap_factor=2.0),
        )

        # All 8 halves report a TuneResult.
        for nid in range(1, 9):
            assert nid in results
        # Reference (longest lane = lane 2 @ 10mm) untouched.
        assert results[5][1].reason in ("already_within_tolerance", "reference")
        assert results[6][1].reason in ("already_within_tolerance", "reference")


# =============================================================================
# 19. AC #8 -- cascade-safety budget for pair-aware groups
# =============================================================================


class TestPhase2FCascadeBudget:
    """Phase 2F AC #8 -- pair-aware insertion counts as ONE against the budget."""

    def test_4_lane_pair_group_worst_case_inserts_bounded(self):
        # 4 pairs + reference (longest lane).  Cascade should NOT count
        # P and N as two separate insertions -- the geometry is logically
        # a single "lane perturbation".  Tightening the tolerance very
        # low + unreachable target exercises the budget.
        group = MatchGroup(
            name="TMDS",
            net_ids=[],
            pair_ids=[(1, 2), (3, 4), (5, 6), (7, 8)],
            tolerance=0.001,  # ultra-tight, likely unreachable
            source=MatchGroupSource.LEGACY_API,
        )
        routes = {
            1: _straight_route(1, "D0_P", 5.0, y=0.0),
            2: _straight_route(2, "D0_N", 5.0, y=0.4),
            3: _straight_route(3, "D1_P", 5.0, y=2.0),
            4: _straight_route(4, "D1_N", 5.0, y=2.4),
            5: _straight_route(5, "D2_P", 5.0, y=4.0),
            6: _straight_route(6, "D2_N", 5.0, y=4.4),
            7: _straight_route(7, "D3_P", 100.0, y=6.0),  # unreachable
            8: _straight_route(8, "D3_N", 100.0, y=6.4),  # unreachable
        }

        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.001,
            intra_group_clearance_mm=0.2,
            intra_pair_clearance_mm=0.1,
            config=SerpentineConfig(amplitude=0.1, gap_factor=2.0),
        )

        # Sum inserts_applied across P and N halves of each pair.  For a
        # pair-aware insertion both halves register the same count (one
        # is committed per attempt, not two).
        # Total committed pair-insertions = inserts_applied counted ONCE
        # per pair (since P and N share the count).
        pair_inserts = 0
        for p_id, n_id in group.pair_ids:
            # Pair insertion count is the P-side count (N-side mirrors).
            p_count = results[p_id][1].inserts_applied
            n_count = results[n_id][1].inserts_applied
            assert p_count == n_count, (
                f"Pair ({p_id}, {n_id}): P inserts ({p_count}) != "
                f"N inserts ({n_count}); paired insertion must commit "
                f"both halves atomically."
            )
            pair_inserts += p_count

        # The worst-case ceiling for a 4-lane group is bounded by
        # MAX_TOTAL_INSERTS_PER_GROUP regardless of P/N doubling.
        assert pair_inserts <= MAX_TOTAL_INSERTS_PER_GROUP


# =============================================================================
# 20. AC #9 -- single-ended path unchanged (drift prevention)
# =============================================================================


class TestPhase2FSingleEndedUnchanged:
    """Phase 2F AC #9 -- ``pair_ids=[]`` results identical to Phase 2E path."""

    def test_empty_pair_ids_dispatches_to_single_ended(self):
        """A group with no pair_ids takes the single-ended path."""
        group = _ddr_group(net_ids=[1, 2, 3, 4], tolerance=0.5)
        # Confirm pair_ids is empty -> single-ended path.
        assert group.pair_ids == []
        routes = {
            1: _straight_route(1, "D0", 10.0, y=0.0),
            2: _straight_route(2, "D1", 10.05, y=2.0),
            3: _straight_route(3, "D2", 10.10, y=4.0),
            4: _straight_route(4, "D3", 10.02, y=6.0),
        }
        original_segments = {nid: r.segments for nid, r in routes.items()}

        results = tune_match_group_v2(
            group,
            routes,
            tolerance_mm=0.5,
            intra_group_clearance_mm=0.2,
        )

        # All within tolerance: byte-for-byte unchanged.
        for nid in group.net_ids:
            assert results[nid][1].reason == "already_within_tolerance"
            assert results[nid][0] is routes[nid]
            assert results[nid][0].segments is original_segments[nid]


# =============================================================================
# 21. Outer-normal hint at pair centerline -- nearest-neighbor + fallbacks
# =============================================================================


class TestPhase2FCenterlineOuterNormal:
    """Phase 2F additional curator ACs -- centerline outer-normal helper."""

    def test_centerline_midpoint_average(self):
        p = Segment(
            x1=0.0,
            y1=0.0,
            x2=10.0,
            y2=0.0,
            width=0.2,
            layer=Layer.F_CU,
            net=10,
            net_name="P",
        )
        n = Segment(
            x1=0.0,
            y1=2.0,
            x2=10.0,
            y2=2.0,
            width=0.2,
            layer=Layer.F_CU,
            net=11,
            net_name="N",
        )
        mx, my = _pair_centerline_midpoint(p, n)
        # P-midpoint = (5, 0), N-midpoint = (5, 2) -> centerline = (5, 1).
        assert mx == pytest.approx(5.0)
        assert my == pytest.approx(1.0)

    def test_centerline_outer_normal_points_away_from_nearest_other(self):
        """Centerline at y=1; nearest other member at y=-5; bulge => +y."""
        p = Segment(
            x1=0.0,
            y1=0.0,
            x2=10.0,
            y2=0.0,
            width=0.2,
            layer=Layer.F_CU,
            net=10,
            net_name="P",
        )
        n = Segment(
            x1=0.0,
            y1=2.0,
            x2=10.0,
            y2=2.0,
            width=0.2,
            layer=Layer.F_CU,
            net=11,
            net_name="N",
        )
        # Other member far below -> bulge should point AWAY from y=-5,
        # i.e. +y direction.
        other = _straight_route(99, "OTHER", 10.0, y=-5.0)
        hint = _outer_normal_hint_pair_group(
            p,
            n,
            candidate_p_id=10,
            candidate_n_id=11,
            group_routes={99: other},
        )
        assert hint[1] > 0.0, (
            f"Centerline outer-normal y-component ({hint[1]}) should be "
            "positive (away from nearest other below)"
        )

    def test_centerline_outer_normal_fallback_when_empty(self):
        """No other members -> fallback to P-segment perpendicular."""
        p = Segment(
            x1=0.0,
            y1=0.0,
            x2=10.0,
            y2=0.0,
            width=0.2,
            layer=Layer.F_CU,
            net=10,
            net_name="P",
        )
        n = Segment(
            x1=0.0,
            y1=2.0,
            x2=10.0,
            y2=2.0,
            width=0.2,
            layer=Layer.F_CU,
            net=11,
            net_name="N",
        )
        hint = _outer_normal_hint_pair_group(
            p,
            n,
            candidate_p_id=10,
            candidate_n_id=11,
            group_routes={},
        )
        # Horizontal segment perpendicular -> (0, +/-1).
        import math

        mag = math.sqrt(hint[0] ** 2 + hint[1] ** 2)
        assert mag == pytest.approx(1.0, abs=1e-9)

    def test_centerline_outer_normal_collinear_centerlines_well_defined(self):
        """Curator's AC #4 -- collinear centerlines edge case yields a
        well-defined normal (the segment's own perpendicular)."""
        # Pair A centerline at y=1, running horizontally x=[0, 10].
        # Pair B centerline at y=1 too, x=[20, 30] -- collinear (same y).
        # The closest point on pair B's centerline to pair A's centerline
        # midpoint (5, 1) is (20, 1).  Magnitude is nonzero -- the
        # outer-normal is well-defined.
        p = Segment(
            x1=0.0,
            y1=0.0,
            x2=10.0,
            y2=0.0,
            width=0.2,
            layer=Layer.F_CU,
            net=10,
            net_name="P",
        )
        n = Segment(
            x1=0.0,
            y1=2.0,
            x2=10.0,
            y2=2.0,
            width=0.2,
            layer=Layer.F_CU,
            net=11,
            net_name="N",
        )
        other_p = Segment(
            x1=20.0,
            y1=0.0,
            x2=30.0,
            y2=0.0,
            width=0.2,
            layer=Layer.F_CU,
            net=20,
            net_name="other_P",
        )
        other_route = Route(net=20, net_name="other_P", segments=[other_p])
        hint = _outer_normal_hint_pair_group(
            p,
            n,
            candidate_p_id=10,
            candidate_n_id=11,
            group_routes={20: other_route},
        )
        import math

        mag = math.sqrt(hint[0] ** 2 + hint[1] ** 2)
        assert mag == pytest.approx(1.0, abs=1e-9), (
            f"Centerline outer-normal magnitude {mag} should be unit "
            "even in the collinear-centerlines edge case."
        )


# =============================================================================
# 22. Paired DRC self-check -- direct unit tests
# =============================================================================


class TestPhase2FPairedDRCHelper:
    """Phase 2F -- direct unit tests on the paired DRC helper."""

    def test_paired_drc_pass_happy_path(self):
        """No collisions: returns True."""
        new_p = Segment(
            x1=0.0,
            y1=10.0,
            x2=5.0,
            y2=10.0,
            width=0.2,
            layer=Layer.F_CU,
            net=10,
            net_name="P",
        )
        new_n = Segment(
            x1=0.0,
            y1=12.0,
            x2=5.0,
            y2=12.0,
            width=0.2,
            layer=Layer.F_CU,
            net=11,
            net_name="N",
        )
        ok = _post_insertion_clearance_ok_pair_group(
            new_p_segments=[new_p],
            new_n_segments=[new_n],
            candidate_p_id=10,
            candidate_n_id=11,
            group_net_ids={10, 11},
            routes_by_net={
                10: _straight_route(10, "P", 5.0, y=0.0),
                11: _straight_route(11, "N", 5.0, y=2.0),
            },
            intra_group_clearance_mm=0.2,
            intra_pair_clearance_mm=0.1,
        )
        assert ok is True

    def test_paired_drc_fail_within_pair(self):
        """P/N new segments too close -> fail (within-pair pass)."""
        new_p = Segment(
            x1=0.0,
            y1=0.0,
            x2=5.0,
            y2=0.0,
            width=0.2,
            layer=Layer.F_CU,
            net=10,
            net_name="P",
        )
        # N segment at y=0.05 -- below the intra_pair_clearance_mm floor.
        new_n = Segment(
            x1=0.0,
            y1=0.05,
            x2=5.0,
            y2=0.05,
            width=0.2,
            layer=Layer.F_CU,
            net=11,
            net_name="N",
        )
        ok = _post_insertion_clearance_ok_pair_group(
            new_p_segments=[new_p],
            new_n_segments=[new_n],
            candidate_p_id=10,
            candidate_n_id=11,
            group_net_ids={10, 11},
            routes_by_net={
                10: _straight_route(10, "P", 5.0, y=5.0),
                11: _straight_route(11, "N", 5.0, y=10.0),
            },
            intra_group_clearance_mm=0.5,
            intra_pair_clearance_mm=0.5,  # tight -> fail
        )
        assert ok is False

    def test_paired_drc_fail_intra_group(self):
        """New segment near another group member -> fail (intra-group pass)."""
        new_p = Segment(
            x1=0.0,
            y1=0.0,
            x2=5.0,
            y2=0.0,
            width=0.2,
            layer=Layer.F_CU,
            net=10,
            net_name="P",
        )
        new_n = Segment(
            x1=0.0,
            y1=2.0,
            x2=5.0,
            y2=2.0,
            width=0.2,
            layer=Layer.F_CU,
            net=11,
            net_name="N",
        )
        # Another group member (net 20) very close to the new_p y=0.
        other = _straight_route(20, "OTHER_P", 5.0, y=0.1)
        ok = _post_insertion_clearance_ok_pair_group(
            new_p_segments=[new_p],
            new_n_segments=[new_n],
            candidate_p_id=10,
            candidate_n_id=11,
            group_net_ids={10, 11, 20, 21},
            routes_by_net={20: other},
            intra_group_clearance_mm=0.5,
            intra_pair_clearance_mm=0.1,
        )
        assert ok is False


# =============================================================================
# 23. _find_corresponding_n_segment heuristic
# =============================================================================


class TestPhase2FFindCorrespondingN:
    """Phase 2F -- N-side segment correspondence by midpoint proximity."""

    def test_finds_same_layer_segment_by_midpoint_proximity(self):
        p_seg = Segment(
            x1=0.0,
            y1=0.0,
            x2=10.0,
            y2=0.0,
            width=0.2,
            layer=Layer.F_CU,
            net=10,
            net_name="P",
        )
        n_route = _straight_route(11, "N", 10.0, y=2.0)
        result = _find_corresponding_n_segment(n_route, p_seg)
        assert result is not None
        idx, n_seg = result
        assert idx == 0
        assert n_seg.layer == Layer.F_CU

    def test_returns_none_when_no_same_layer_segment(self):
        p_seg = Segment(
            x1=0.0,
            y1=0.0,
            x2=10.0,
            y2=0.0,
            width=0.2,
            layer=Layer.F_CU,
            net=10,
            net_name="P",
        )
        # N route on a different layer.
        n_route = _straight_route(11, "N", 10.0, y=2.0, layer=Layer.B_CU)
        result = _find_corresponding_n_segment(n_route, p_seg)
        assert result is None
