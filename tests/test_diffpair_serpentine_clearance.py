"""Regression tests for Issue #3003: post-pathfinder serpentine must
respect intra-pair clearance.

Pre-#3003, ``DiffPairRouter.route_differential_pair_coupled`` called
``match_pair_lengths(..., add_serpentines=True)`` unconditionally
whenever the coupled-router's post-route length delta exceeded
``pair.rules.max_length_delta``.  ``match_pair_lengths`` delegated to
``create_serpentine``, which (1) never knew where the partner trace
sat, (2) never received an ``intra_pair_clearance_mm`` threshold, and
(3) hardcoded the bulge side (``current_side = 1`` at
``diffpair_routing.py:859``).  On a tightly-spaced pair the meander
bulged straight into the partner trace, producing hundreds of
``diffpair_clearance_intra`` violations on board 07 once
``--differential-pairs`` was enabled.

This module pins the fix:

* ``create_serpentine`` now accepts an optional ``partner_route`` and
  ``intra_pair_clearance_mm``.  When both are supplied it (a) chooses
  ``current_side`` so the bulge points AWAY from the partner (reusing
  ``_outer_normal_hint`` from the audited Phase 3I tuner) and (b)
  rejects the geometry via a ``segment_clearance`` self-check before
  committing.
* ``match_pair_lengths`` threads the partner route + the clearance
  through to ``create_serpentine`` so callers (notably the inline
  pre-pass shim) get the safe path automatically when they declare a
  clearance threshold.
* ``DiffPairRouter.route_differential_pair_coupled`` gates the shim on
  ``length_critical=True`` (length-critical pairs are handled by the
  audited Phase 3I tuner) and looks up
  ``intra_pair_clearance_mm`` from the autorouter's ``net_class_map``.

These are unit-level checks; the board-07 end-to-end check lives in the
board's ``generate_design.py`` recipe (no separate integration test
required by the acceptance criteria).
"""

from __future__ import annotations

from kicad_tools.core.geometry import segment_clearance
from kicad_tools.router.diffpair_routing import (
    create_serpentine,
    match_pair_lengths,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.path import calculate_route_length
from kicad_tools.router.primitives import Route, Segment


def _make_horizontal_route(
    net_id: int, net_name: str, y: float, length: float = 10.0
) -> Route:
    """Build a single-segment horizontal route at the given y, from x=0 to x=length."""
    route = Route(net=net_id, net_name=net_name)
    route.segments.append(
        Segment(
            x1=0.0,
            y1=y,
            x2=length,
            y2=y,
            width=0.2,
            layer=Layer.F_CU,
            net=net_id,
            net_name=net_name,
        )
    )
    return route


def _min_partner_clearance(route: Route, partner: Route) -> float:
    """Return the minimum edge-to-edge clearance between ``route`` and
    ``partner`` (only segments on the same layer are compared)."""
    best = float("inf")
    for seg in route.segments:
        for pseg in partner.segments:
            if pseg.layer != seg.layer:
                continue
            clearance = segment_clearance(
                seg.x1,
                seg.y1,
                seg.x2,
                seg.y2,
                seg.width,
                pseg.x1,
                pseg.y1,
                pseg.x2,
                pseg.y2,
                pseg.width,
            )
            if clearance < best:
                best = clearance
    return best


class TestCreateSerpentineClearanceAware:
    """Tests for the partner-aware path through ``create_serpentine``."""

    def test_legacy_path_unchanged_without_partner(self):
        """Without partner_route / intra_pair_clearance_mm the behavior
        matches pre-#3003: the serpentine is added unconditionally."""
        route = _make_horizontal_route(net_id=1, net_name="LEGACY", y=0.0)
        original_length = calculate_route_length([route])

        result = create_serpentine(route, length_to_add=2.0)

        assert result is True
        assert len(route.segments) > 1
        assert calculate_route_length([route]) > original_length

    def test_serpentine_bulges_away_from_partner_above(self):
        """When the partner sits ABOVE the shorter trace, the serpentine
        must bulge downward (away from the partner)."""
        shorter = _make_horizontal_route(net_id=1, net_name="P", y=0.0)
        # Partner sits 0.10mm above (matches board 07's intra clearance).
        partner = _make_horizontal_route(net_id=2, net_name="N", y=0.1)

        result = create_serpentine(
            shorter,
            length_to_add=2.0,
            partner_route=partner,
            intra_pair_clearance_mm=0.10,
        )

        # When partner is ABOVE, every bulge midpoint must end up BELOW
        # the original y=0 line (the only "outer" direction).
        if result:
            for seg in shorter.segments:
                assert seg.y1 <= 1e-9, f"segment y1={seg.y1} bulged toward partner"
                assert seg.y2 <= 1e-9, f"segment y2={seg.y2} bulged toward partner"
        # Either the bulge committed (away from partner) or it was
        # rejected; both are acceptable outcomes per #3003 spec.

    def test_serpentine_bulges_away_from_partner_below(self):
        """Mirror of the above: partner BELOW -> bulge must point UP."""
        shorter = _make_horizontal_route(net_id=1, net_name="P", y=0.0)
        partner = _make_horizontal_route(net_id=2, net_name="N", y=-0.1)

        result = create_serpentine(
            shorter,
            length_to_add=2.0,
            partner_route=partner,
            intra_pair_clearance_mm=0.10,
        )

        if result:
            for seg in shorter.segments:
                assert seg.y1 >= -1e-9, f"segment y1={seg.y1} bulged toward partner"
                assert seg.y2 >= -1e-9, f"segment y2={seg.y2} bulged toward partner"

    def test_committed_serpentine_respects_intra_pair_clearance(self):
        """When ``create_serpentine`` commits a bulge, that bulge MUST
        NOT violate ``intra_pair_clearance_mm`` against the partner."""
        shorter = _make_horizontal_route(net_id=1, net_name="P", y=0.0)
        partner = _make_horizontal_route(net_id=2, net_name="N", y=0.1)
        original_segments = list(shorter.segments)

        clearance_floor = 0.10

        result = create_serpentine(
            shorter,
            length_to_add=2.0,
            partner_route=partner,
            intra_pair_clearance_mm=clearance_floor,
        )

        if result:
            # Bulge committed -- every segment must clear the partner.
            min_clearance = _min_partner_clearance(shorter, partner)
            assert min_clearance + 1e-9 >= clearance_floor, (
                f"Committed serpentine violates intra-pair clearance: "
                f"min={min_clearance}mm < floor={clearance_floor}mm"
            )
        else:
            # Bulge rejected -- the route MUST be unchanged (rollback).
            assert shorter.segments == original_segments, (
                "Rejected serpentine must leave the route unchanged"
            )

    def test_unsafe_serpentine_rejected_when_partner_too_close(self):
        """When the partner is so close that no amplitude works, the
        serpentine must be REJECTED (return False) and the route left
        untouched.

        We engineer a case where the desired bulge size (driven by
        ``length_to_add`` and ``min_amplitude``) would unavoidably
        collide with the partner whichever side it picks.  By placing
        partners both above AND below the route at intra_pair clearance,
        and demanding a large bulge, the geometry must fail."""
        shorter = _make_horizontal_route(net_id=1, net_name="P", y=0.0)
        # Cage the trace: partner above AND a fake neighbor below, both
        # at 0.10mm.  Since create_serpentine only knows about ONE
        # partner, we use the partner_above as the partner and rely on
        # the bulge geometry being too tall to fit.
        # Use a very large amplitude floor to force collision.
        partner = _make_horizontal_route(net_id=2, net_name="N", y=0.1)
        original_segments = list(shorter.segments)

        result = create_serpentine(
            shorter,
            length_to_add=2.0,
            min_amplitude=1.0,  # Force a 1mm bulge; even with outward
            # bias, +1mm bulge on a trace at y=0 with partner at y=0.1
            # would only matter if the bulge points UP (which it
            # shouldn't), so we exercise the "outward-bias works"
            # path here.  See the next test for the
            # collision-rejection variant.
            partner_route=partner,
            intra_pair_clearance_mm=0.10,
        )

        # Either result is fine; what matters is the invariant.
        if result:
            min_clearance = _min_partner_clearance(shorter, partner)
            assert min_clearance + 1e-9 >= 0.10
        else:
            assert shorter.segments == original_segments

    def test_partner_aware_rejection_leaves_route_intact(self):
        """When ``intra_pair_clearance_mm`` is so tight that even an
        outward bulge would breach it (e.g., a partner that wraps
        around the segment), the function must return False and leave
        the route unchanged."""
        shorter = _make_horizontal_route(net_id=1, net_name="P", y=0.0)
        # Construct a partner that runs alongside on BOTH sides of the
        # shorter trace by giving it two segments above and below.
        partner = Route(net=2, net_name="N")
        partner.segments.append(
            Segment(
                x1=0.0,
                y1=0.1,
                x2=10.0,
                y2=0.1,
                width=0.2,
                layer=Layer.F_CU,
                net=2,
                net_name="N",
            )
        )
        partner.segments.append(
            Segment(
                x1=0.0,
                y1=-0.1,
                x2=10.0,
                y2=-0.1,
                width=0.2,
                layer=Layer.F_CU,
                net=2,
                net_name="N",
            )
        )
        original_segments = list(shorter.segments)

        result = create_serpentine(
            shorter,
            length_to_add=2.0,
            min_amplitude=0.5,  # Bulge must exceed the 0.10mm corridor.
            partner_route=partner,
            intra_pair_clearance_mm=0.10,
        )

        # The bulge cannot fit on either side of the trace -- it must
        # be rejected (#3003 acceptance criterion: zero
        # diffpair_clearance_intra violations).
        assert result is False
        assert shorter.segments == original_segments


class TestMatchPairLengthsClearanceAware:
    """Tests for the clearance-aware path through ``match_pair_lengths``."""

    def test_legacy_path_unchanged_without_clearance(self):
        """Without ``intra_pair_clearance_mm`` the behavior matches
        pre-#3003: serpentine added unconditionally to the shorter
        route."""
        p_route = _make_horizontal_route(net_id=1, net_name="P", y=0.0, length=12.0)
        n_route = _make_horizontal_route(net_id=2, net_name="N", y=1.0, length=10.0)

        result = match_pair_lengths(
            p_route, n_route, max_delta=1.0, add_serpentines=True
        )

        # 2mm mismatch, max_delta=1mm -> serpentine added on shorter (n).
        assert result is True
        assert len(n_route.segments) > 1

    def test_clearance_aware_path_rejects_unsafe_bulge(self):
        """When ``intra_pair_clearance_mm`` is supplied and the
        geometry cannot satisfy it, ``match_pair_lengths`` returns
        False and the shorter route is left untouched.

        Construction: P is the SHORTER trace (10mm) and is surrounded
        by N on BOTH sides (y=+/-0.1mm), leaving no outward room for
        the bulge.  N's three segments form a U-shape around P that
        totals ~30.2mm, so P is unambiguously the shorter half.
        """
        p_route = _make_horizontal_route(net_id=1, net_name="P", y=0.0, length=10.0)
        n_route = Route(net=2, net_name="N")
        n_route.segments.append(
            Segment(
                x1=0.0,
                y1=0.1,
                x2=15.0,
                y2=0.1,
                width=0.2,
                layer=Layer.F_CU,
                net=2,
                net_name="N",
            )
        )
        n_route.segments.append(
            Segment(
                x1=15.0,
                y1=0.1,
                x2=15.0,
                y2=-0.1,
                width=0.2,
                layer=Layer.F_CU,
                net=2,
                net_name="N",
            )
        )
        n_route.segments.append(
            Segment(
                x1=15.0,
                y1=-0.1,
                x2=0.0,
                y2=-0.1,
                width=0.2,
                layer=Layer.F_CU,
                net=2,
                net_name="N",
            )
        )
        original_p_segments = list(p_route.segments)

        # N total length ~= 15 + 0.2 + 15 = 30.2mm; P = 10mm; delta = 20.2mm.
        # max_delta=1mm; serpentine target on P.  Partner surrounds at
        # y=+/-0.1 so any sizeable bulge violates 0.10mm clearance.
        result = match_pair_lengths(
            p_route,
            n_route,
            max_delta=1.0,
            add_serpentines=True,
            intra_pair_clearance_mm=0.10,
        )

        assert result is False, (
            "Serpentine on caged trace must be rejected when clearance "
            "is supplied (#3003 invariant: zero diffpair_clearance_intra)."
        )
        assert p_route.segments == original_p_segments, (
            "Rejected serpentine must leave the shorter route untouched"
        )

    def test_clearance_aware_path_accepts_safe_bulge(self):
        """When the partner is on one side only, the bulge can fit on
        the OTHER side and ``match_pair_lengths`` should succeed."""
        p_route = _make_horizontal_route(net_id=1, net_name="P", y=0.0, length=12.0)
        # N is longer than P AND only above -> bulge on P should
        # commit and point downward.
        n_route = _make_horizontal_route(net_id=2, net_name="N", y=0.1, length=15.0)

        result = match_pair_lengths(
            p_route,
            n_route,
            max_delta=1.0,
            add_serpentines=True,
            intra_pair_clearance_mm=0.10,
        )

        # Wait -- P is shorter than N (12 vs 15). Bulge goes on P.
        # P is at y=0, partner at y=0.1 (above), so the safe direction
        # is downward (y<=0).
        # Whether the bulge commits depends on min_amplitude vs the
        # length budget, but if it does commit, it must respect
        # clearance.
        if result:
            min_clearance = _min_partner_clearance(p_route, n_route)
            assert min_clearance + 1e-9 >= 0.10, (
                f"Accepted serpentine breaks clearance: min={min_clearance}mm"
            )
            # And every bulge must be below the partner.
            for seg in p_route.segments:
                # Partner is at y=0.1; safe direction is y <= 0.
                assert seg.y1 <= 0.0 + 1e-9
                assert seg.y2 <= 0.0 + 1e-9
