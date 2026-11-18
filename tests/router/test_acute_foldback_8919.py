"""Acute two-segment fold-back detection and repair (Issue #8919).

The router emitted degree-2 vertices where a track overshoots and turns back
135 degrees, leaving the open 45-degree wedge JLC's DFM tool flagged on the
#8840 calibration board and ``kct check``'s ``sharp_trace_corner`` rule gates.

Three geometries measured on board 04 (``origin/main`` @ ``94177481d``) are
pinned here as literal fixtures rather than regenerated, because PR #8854 owns
board 04's copper:

===========  ======  =========================  ==================  ==========
fold-back    layer   vertex                     legs (short/long)   ratio
===========  ======  =========================  ==================  ==========
``USER_LED`` F.Cu    (35.162, 25.450) (regen)   0.7000 / 0.9023     1.29
``SWCLK``    F.Cu    (33.7500, 17.1375)         0.7000 / 0.8485     1.21
``NRST``     B.Cu    (27.3375, 22.2500)         1.3125 / 5.7276     4.36
===========  ======  =========================  ==================  ==========

Only ``NRST`` clears ``|VB| >= sqrt(2) * |VA|``, so the issue body's
perpendicular construction repairs *it alone* -- the other two, including the
single reproducible current-router instance (``USER_LED``), need the
perpendicular-to-the-long-leg move that is feasible for every ratio.  Both
sub-cases are pinned below.

The two vertices the gate deliberately exempts on committed board 04 --
``USER_LED`` width-filled and ``GND`` buried under a same-net disc -- must be
left untouched, so the pass never churns copper the gate does not flag.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.explain.checks.acid_trap import (
    ANGLE_TOLERANCE_DEG,
    MIN_CORNER_ANGLE_DEG,
    MIN_TRACE_ANGLE_DEG,
)
from kicad_tools.router.acute_foldback import (
    MAX_GATING_ANGLE_DEG,
    CopperDisc,
    find_foldbacks,
    plan_foldback_repairs,
    repair_foldbacks,
    wedge_is_open,
)
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

Point = tuple[float, float]


def seg(
    p: Point,
    q: Point,
    *,
    width: float = 0.2,
    net: int = 5,
    layer: Layer = Layer.F_CU,
    net_name: str = "SIG",
) -> Segment:
    """A trace segment from *p* to *q*."""
    return Segment(
        x1=p[0],
        y1=p[1],
        x2=q[0],
        y2=q[1],
        width=width,
        layer=layer,
        net=net,
        net_name=net_name,
    )


def clear(*_args: object) -> bool:
    """A collision model that approves every candidate."""
    return True


def blocked(*_args: object) -> bool:
    """A collision model that vetoes every candidate."""
    return False


def total_length(segments: list[Segment]) -> float:
    return sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in segments)


def endpoints(segments: list[Segment]) -> set[Point]:
    """Rounded endpoint set, for connectivity assertions."""
    out: set[Point] = set()
    for s in segments:
        out.add((round(s.x1, 4), round(s.y1, 4)))
        out.add((round(s.x2, 4), round(s.y2, 4)))
    return out


# Measured board-04 geometries (see module docstring).
USER_LED_REGEN = [
    seg((35.162, 24.750), (35.162, 25.450), net_name="USER_LED"),
    seg((35.162, 25.450), (35.800, 24.812), net_name="USER_LED"),
]

SWCLK_COMMITTED = [
    seg((33.7500, 17.8375), (33.7500, 17.1375), net_name="SWCLK"),
    seg((33.7500, 17.1375), (34.3500, 17.7375), net_name="SWCLK"),
    seg((34.3500, 17.7375), (34.3500, 18.3000), net_name="SWCLK"),
]

NRST_COMMITTED = [
    seg(
        (28.6500, 22.2500),
        (27.3375, 22.2500),
        width=0.15,
        layer=Layer.B_CU,
        net_name="NRST",
    ),
    seg((27.3375, 22.2500), (31.3875, 26.3000), layer=Layer.B_CU, net_name="NRST"),
    seg((31.3875, 26.3000), (34.7000, 26.3000), layer=Layer.B_CU, net_name="NRST"),
]


# --------------------------------------------------------------------------
# Detector parity with sharp_trace_corner
# --------------------------------------------------------------------------


class TestDetectorParity:
    """The detector's thresholds come from ``acid_trap``, not a second copy."""

    def test_gating_angle_is_derived_not_restated(self):
        assert MAX_GATING_ANGLE_DEG == MIN_TRACE_ANGLE_DEG - ANGLE_TOLERANCE_DEG
        assert pytest.approx(89.0) == MAX_GATING_ANGLE_DEG

    def test_rounded_right_angle_is_not_a_foldback(self):
        """A joint just under 90 deg is a rounded corner, not an acid trap."""
        angle = math.radians(89.5)
        legs = [
            seg((0.0, 0.0), (2.0, 0.0)),
            seg((0.0, 0.0), (2.0 * math.cos(angle), 2.0 * math.sin(angle))),
        ]
        assert find_foldbacks(legs) == []

    def test_obtuse_corner_untouched(self):
        """A 135 deg corner -- the shape the repair aims for -- is not gating."""
        legs = [seg((0.0, 0.0), (1.0, 0.0)), seg((1.0, 0.0), (2.0, 1.0))]
        assert find_foldbacks(legs) == []
        out, result = repair_foldbacks(legs, path_is_clear=clear)
        assert (result.repaired, result.skipped) == (0, 0)
        assert out == legs

    def test_collinear_doubling_back_untouched(self):
        """Below ``MIN_CORNER_ANGLE_DEG`` it is a collinear overlap, not a corner."""
        legs = [seg((0.0, 0.0), (-2.0, 0.0)), seg((0.0, 0.0), (1.0, 0.0))]
        assert pytest.approx(1.0) == MIN_CORNER_ANGLE_DEG
        assert find_foldbacks(legs) == []

    def test_branch_junction_is_not_a_two_segment_foldback(self):
        """A degree-3 vertex is a different defect class (issue #8919 scope)."""
        legs = [
            seg((0.0, 0.0), (0.0, 2.0)),
            seg((0.0, 0.0), (2.0, 2.0)),
            seg((0.0, 0.0), (0.0, -2.0)),
        ]
        assert find_foldbacks(legs) == []
        out, result = repair_foldbacks(legs, path_is_clear=clear)
        assert (result.repaired, result.skipped) == (0, 0)
        assert out == legs

    def test_different_layers_do_not_form_a_foldback(self):
        legs = [
            seg((0.0, 0.0), (0.0, 1.0)),
            seg((0.0, 0.0), (1.0, 1.0), layer=Layer.B_CU),
        ]
        assert find_foldbacks(legs) == []

    def test_different_nets_do_not_form_a_foldback(self):
        legs = [seg((0.0, 0.0), (0.0, 1.0)), seg((0.0, 0.0), (1.0, 1.0), net=6)]
        assert find_foldbacks(legs) == []

    def test_net0_copper_is_ignored(self):
        legs = [
            seg((0.0, 0.0), (0.0, 1.0), net=0),
            seg((0.0, 0.0), (1.0, 1.0), net=0),
        ]
        assert find_foldbacks(legs) == []

    def test_wedge_is_open_matches_s0_formula(self):
        """``s0 = (w1 + w2) / (4 sin(theta/2))`` -- the gate's own threshold."""
        theta = 45.0
        s0 = (0.2 + 0.2) / (4 * math.sin(math.radians(theta) / 2))
        assert s0 == pytest.approx(0.2613, abs=1e-4)
        long_leg = seg((0.0, 0.0), (0.0, 0.7))
        just_short = seg(
            (0.0, 0.0),
            (
                (s0 - 0.01) * math.sin(math.radians(theta)),
                (s0 - 0.01) * math.cos(math.radians(theta)),
            ),
        )
        assert not wedge_is_open(long_leg, just_short, (0.0, 0.0), theta)
        just_long = seg(
            (0.0, 0.0),
            (
                (s0 + 0.01) * math.sin(math.radians(theta)),
                (s0 + 0.01) * math.cos(math.radians(theta)),
            ),
        )
        assert wedge_is_open(long_leg, just_long, (0.0, 0.0), theta)


# --------------------------------------------------------------------------
# Exemptions: copper the gate does not flag must not be churned
# --------------------------------------------------------------------------


class TestExemptionsLeaveCopperUntouched:
    """Board 04's two correctly-exempted vertices must be left alone."""

    def test_width_filled_hairpin_untouched(self):
        """Committed ``USER_LED``: min leg 0.2121 <= s0 0.2613 -- no open wedge."""
        legs = [
            seg((35.1625, 24.7500), (35.1625, 25.4500), net_name="USER_LED"),
            seg((35.1625, 25.4500), (35.3125, 25.3000), net_name="USER_LED"),
            seg((35.3125, 25.3000), (35.8000, 24.8125), net_name="USER_LED"),
        ]
        assert math.dist((35.1625, 25.4500), (35.3125, 25.3000)) == pytest.approx(0.2121, abs=1e-4)
        assert find_foldbacks(legs) == []
        out, result = repair_foldbacks(legs, path_is_clear=clear)
        assert (result.repaired, result.skipped) == (0, 0)
        assert out == legs

    def test_pad_via_buried_vertex_untouched(self):
        """Committed ``GND``: buried under a same-net disc r=1.000 at (11.15, 10.0)."""
        legs = [
            seg((11.8500, 10.0000), (11.1500, 10.0000), net_name="GND"),
            seg((11.8500, 10.0000), (10.2400, 11.6100), net_name="GND"),
        ]
        # Without the disc the vertex is an open 45 deg wedge ...
        assert len(find_foldbacks(legs)) == 1
        # ... and under same-net pad/via copper the gate skips it, so we do too.
        discs = [
            CopperDisc(
                x=11.1500,
                y=10.0000,
                radius=1.000,
                layers=frozenset({Layer.F_CU, Layer.B_CU}),
                net=5,
            )
        ]
        assert find_foldbacks(legs, discs) == []
        out, result = repair_foldbacks(legs, discs=discs, path_is_clear=clear)
        assert (result.repaired, result.skipped) == (0, 0)
        assert out == legs

    def test_foreign_net_disc_does_not_exempt(self):
        legs = [
            seg((11.8500, 10.0000), (11.1500, 10.0000)),
            seg((11.8500, 10.0000), (10.2400, 11.6100)),
        ]
        discs = [
            CopperDisc(
                x=11.1500,
                y=10.0000,
                radius=1.000,
                layers=frozenset({Layer.F_CU, Layer.B_CU}),
                net=99,
            )
        ]
        assert len(find_foldbacks(legs, discs)) == 1

    def test_through_via_buries_an_inner_layer_vertex(self):
        """``*.Cu`` / outer-pair copper spans the inner layers of a 4-layer stack."""
        legs = [
            seg((5.0, 5.0), (5.0, 5.7), layer=Layer.IN1_CU),
            seg((5.0, 5.0), (5.7, 5.7), layer=Layer.IN1_CU),
        ]
        assert len(find_foldbacks(legs)) == 1
        through = CopperDisc(
            x=5.0,
            y=5.0,
            radius=0.3,
            layers=frozenset({Layer.F_CU, Layer.B_CU}),
            net=5,
        )
        assert find_foldbacks(legs, [through]) == []
        front_only = CopperDisc(x=5.0, y=5.0, radius=0.3, layers=frozenset({Layer.F_CU}), net=5)
        assert len(find_foldbacks(legs, [front_only])) == 1


# --------------------------------------------------------------------------
# Repair: both 45-degree sub-cases
# --------------------------------------------------------------------------


class TestPerpendicularRepair:
    """The issue body's construction, where ``|VB| >= sqrt(2) * |VA|`` fits."""

    def test_nrst_uses_the_body_construction(self):
        """Ratio 4.36 -- the one measured fold-back the body's formula fits."""
        fbs = find_foldbacks(NRST_COMMITTED)
        assert len(fbs) == 1
        fb = fbs[0]
        assert fb.vertex == pytest.approx((27.3375, 22.2500))
        assert fb.angle_deg == pytest.approx(45.0, abs=1e-6)
        assert fb.short_length == pytest.approx(1.3125, abs=1e-4)
        assert fb.long_length == pytest.approx(5.7276, abs=1e-4)
        assert fb.long_length >= math.sqrt(2) * fb.short_length

        out, result = repair_foldbacks(NRST_COMMITTED, path_is_clear=clear)
        assert (result.repaired, result.skipped) == (1, 0)
        assert result.repairs[0].construction == "perpendicular-to-short-leg"
        # |VP| = |VA| * sqrt(2) = 1.8562 along the diagonal long leg, i.e.
        # P = (28.6500, 23.5625) -- the point the issue's own analysis names.
        assert (28.6500, 23.5625) in endpoints(out)
        assert find_foldbacks(out) == []

    def test_perpendicular_move_keeps_both_far_ends(self):
        out, _ = repair_foldbacks(NRST_COMMITTED, path_is_clear=clear)
        # A (short-leg terminal) and the far end of the long leg are fixed.
        assert (28.6500, 22.2500) in endpoints(out)
        assert (31.3875, 26.3000) in endpoints(out)

    def test_repair_never_grows_copper(self):
        for fixture in (USER_LED_REGEN, SWCLK_COMMITTED, NRST_COMMITTED):
            out, result = repair_foldbacks(fixture, path_is_clear=clear)
            assert result.repaired == 1
            assert total_length(out) <= total_length(fixture) + 1e-9


class TestShortLongRatioFallback:
    """``|VB| < sqrt(2) * |VA|``: the body's construction cannot be expressed."""

    @pytest.mark.parametrize(
        ("name", "fixture", "vertex", "ratio"),
        [
            ("USER_LED", USER_LED_REGEN, (35.162, 25.450), 1.289),
            ("SWCLK", SWCLK_COMMITTED, (33.7500, 17.1375), 1.212),
        ],
    )
    def test_infeasible_ratio_still_repaired(self, name, fixture, vertex, ratio):
        fbs = find_foldbacks(fixture)
        assert len(fbs) == 1, name
        fb = fbs[0]
        assert fb.vertex == pytest.approx(vertex)
        assert fb.angle_deg == pytest.approx(45.0, abs=1e-6)
        # The body's construction needs |VB| >= sqrt(2) |VA|; this one does not
        # reach it, which is why a pass implementing only that formula would
        # have repaired none of the reproducible fold-backs.
        assert fb.long_length / fb.short_length == pytest.approx(ratio, abs=1e-3)
        assert fb.long_length < math.sqrt(2) * fb.short_length

        out, result = repair_foldbacks(fixture, path_is_clear=clear)
        assert (result.repaired, result.skipped) == (1, 0), name
        assert result.repairs[0].construction == "perpendicular-to-long-leg"
        assert find_foldbacks(out) == []

    def test_user_led_is_the_deciding_criterion(self):
        """The single confirmed current-router fold-back ends up wedge-free.

        Issue #8919's deciding acceptance criterion: the fresh-regen
        ``USER_LED`` geometry must leave no gating fold-back.
        """
        out, result = repair_foldbacks(USER_LED_REGEN, path_is_clear=clear)
        assert result.repaired == 1
        assert find_foldbacks(out) == []
        # Both original far ends survive, so the net is no less connected.
        assert (35.162, 24.750) in endpoints(out)
        assert (35.800, 24.812) in endpoints(out)
        # Every emitted segment is still on the {0, 45, 90, 135} angle set.
        for s in out:
            dx, dy = round(s.x2 - s.x1, 4), round(s.y2 - s.y1, 4)
            assert abs(dx) < 1e-9 or abs(dy) < 1e-9 or abs(abs(dx) - abs(dy)) < 1e-9

    def test_perpendicular_to_long_leg_makes_a_right_angle(self):
        """``t = |VA| cos(theta)`` puts a 90 deg corner at ``P``."""
        out, result = repair_foldbacks(USER_LED_REGEN, path_is_clear=clear)
        assert result.repairs[0].construction == "perpendicular-to-long-leg"
        new = list(result.repairs[0].added)
        assert len(new) == 2
        p = (new[0].x2, new[0].y2)
        assert p == pytest.approx((35.5120, 25.1000), abs=1e-4)
        v1 = (new[0].x1 - p[0], new[0].y1 - p[1])
        v2 = (new[1].x2 - p[0], new[1].y2 - p[1])
        dot = v1[0] * v2[0] + v1[1] * v2[1]
        assert dot == pytest.approx(0.0, abs=1e-9)

    def test_corner_at_the_long_leg_far_end_is_unchanged(self):
        """``P`` lies on ``V->B``, so the angle at ``B`` cannot move."""
        before = find_foldbacks(SWCLK_COMMITTED)[0]
        out, _ = repair_foldbacks(SWCLK_COMMITTED, path_is_clear=clear)
        b = (round(before.b[0], 4), round(before.b[1], 4))
        assert b in endpoints(out)
        touching = [s for s in out if min(math.dist(b, s.start), math.dist(b, s.end)) < 1e-6]
        assert len(touching) == 2


class TestDetourCollapseFallback:
    """When no perpendicular move survives, the two-segment detour collapses."""

    def test_collapse_used_when_perpendicular_moves_are_vetoed(self):
        """Only the straight ``A``->``B`` path is clear; the pass finds it."""
        collapse = [
            seg((35.162, 24.750), (35.2240, 24.8120), net_name="USER_LED"),
            seg((35.2240, 24.8120), (35.800, 24.8120), net_name="USER_LED"),
        ]
        allowed = {
            (round(s.x1, 4), round(s.y1, 4), round(s.x2, 4), round(s.y2, 4)) for s in collapse
        }

        def only_collapse(x1, y1, x2, y2, _layer, _width, _net):
            return (round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)) in allowed

        out, result = repair_foldbacks(USER_LED_REGEN, path_is_clear=only_collapse)
        assert (result.repaired, result.skipped) == (1, 0)
        assert result.repairs[0].construction.startswith("detour-collapse")
        assert find_foldbacks(out) == []
        assert total_length(out) < total_length(USER_LED_REGEN)


# --------------------------------------------------------------------------
# Rejection paths: a vetoed repair is counted, never forced
# --------------------------------------------------------------------------


class TestVetoedRepairsAreCountedNotForced:
    def test_clearance_veto_rejects_and_counts(self):
        """A deep wedge can hold foreign copper -- then nothing is rewritten."""
        out, result = repair_foldbacks(USER_LED_REGEN, path_is_clear=blocked)
        assert (result.repaired, result.skipped) == (0, 1)
        assert result.repairs == []
        assert out == USER_LED_REGEN

    def test_foreign_copper_in_the_wedge_is_not_forced(self):
        """Every candidate crossing the wedge is refused; the fold-back stays."""
        calls: list[tuple[float, float, float, float]] = []

        def wedge_occupied(x1, y1, x2, y2, _layer, _width, _net):
            calls.append((x1, y1, x2, y2))
            # Refuse anything inside the wedge's bounding box, which is where
            # every candidate must pass.
            return False

        out, result = repair_foldbacks(SWCLK_COMMITTED, path_is_clear=wedge_occupied)
        assert calls, "the collision model must actually be consulted"
        assert (result.repaired, result.skipped) == (0, 1)
        assert out == SWCLK_COMMITTED

    def test_no_collision_model_is_a_noop(self):
        """Like ``shorten_detour_pass``, the pass needs a collision model."""
        out, result = repair_foldbacks(USER_LED_REGEN, path_is_clear=None)
        assert (result.repaired, result.skipped) == (0, 0)
        assert out == USER_LED_REGEN

    def test_lost_pad_anchor_rejects_the_repair(self):
        """A same-net pad mid-leg must stay on the copper after the rewrite.

        The anchor sits 0.5 mm along ``V->B`` -- on the stretch of the long leg
        every candidate removes, and off all of their replacements -- so the
        rewrite would strand it and is refused instead.
        """
        fb = find_foldbacks(NRST_COMMITTED)[0]
        step = 0.5 / math.sqrt(2)
        anchor = (fb.vertex[0] + step, fb.vertex[1] + step)
        # Precondition: the anchor really is on the copper being replaced.
        assert math.isclose(
            math.dist(fb.vertex, anchor) + math.dist(anchor, fb.b),
            math.dist(fb.vertex, fb.b),
            abs_tol=1e-9,
        )
        discs = [
            CopperDisc(
                x=anchor[0],
                y=anchor[1],
                radius=0.1,
                layers=frozenset({Layer.B_CU}),
                net=5,
            )
        ]
        # The anchor does not bury the vertex, so the fold-back is still gating.
        assert len(find_foldbacks(NRST_COMMITTED, discs)) == 1
        out, result = repair_foldbacks(NRST_COMMITTED, discs=discs, path_is_clear=clear)
        assert (result.repaired, result.skipped) == (0, 1)
        assert out == NRST_COMMITTED

    def test_preserved_copper_leg_is_never_rewritten(self):
        """``--preserve-existing`` copper is visible but immutable."""
        fixed = USER_LED_REGEN[1]
        out, result = repair_foldbacks(
            USER_LED_REGEN,
            path_is_clear=clear,
            is_mutable=lambda s: s is not fixed,
        )
        assert (result.repaired, result.skipped) == (0, 1)
        assert out == USER_LED_REGEN

    def test_plan_is_deterministic(self):
        first = plan_foldback_repairs(list(SWCLK_COMMITTED), path_is_clear=clear)
        second = plan_foldback_repairs(list(SWCLK_COMMITTED), path_is_clear=clear)
        assert [r.construction for r in first.repairs] == [r.construction for r in second.repairs]
        assert [(s.x1, s.y1, s.x2, s.y2) for r in first.repairs for s in r.added] == [
            (s.x1, s.y1, s.x2, s.y2) for r in second.repairs for s in r.added
        ]


class TestNoNewAcuteCorner:
    """A repair must not trade one gate failure for another."""

    def test_rewritten_neighbourhood_is_rechecked(self):
        """``A``'s other connection is considered, not just the vertex."""
        # A short leg whose far end continues in a direction that would form a
        # new acute corner with the perpendicular move's new segment.
        legs = [
            seg((0.0, 0.0), (0.0, 1.0)),
            seg((0.0, 0.0), (1.2, 1.2)),
            seg((0.0, 1.0), (-0.3, 1.3)),
        ]
        out, result = repair_foldbacks(legs, path_is_clear=clear)
        # Whatever the pass chose, the result carries no gating corner at all.
        assert find_foldbacks(out) == []
        assert result.repaired + result.skipped >= 1

    def test_all_measured_fixtures_end_wedge_free(self):
        for fixture in (USER_LED_REGEN, SWCLK_COMMITTED, NRST_COMMITTED):
            out, result = repair_foldbacks(fixture, path_is_clear=clear)
            assert result.repaired == 1
            assert find_foldbacks(out) == []


# --------------------------------------------------------------------------
# cleanup_artifacts wiring (Step 6)
# --------------------------------------------------------------------------


def _router() -> Autorouter:
    return Autorouter(width=60.0, height=50.0, origin_x=0.0, origin_y=0.0)


class TestCleanupArtifactsStep6:
    """Step 6 of ``cleanup_artifacts``, on every emission path."""

    def test_stats_keys_always_present(self):
        router = _router()
        router.routes = [
            Route(
                net=5,
                net_name="SIG",
                segments=[seg((10.0, 10.0), (12.0, 10.0))],
            )
        ]
        stats = router.cleanup_artifacts()
        assert stats["acute_foldbacks_repaired"] == 0
        assert stats["acute_foldbacks_skipped"] == 0

    def test_repairs_the_reproducible_user_led_foldback(self):
        router = _router()
        router.routes = [Route(net=5, net_name="USER_LED", segments=list(USER_LED_REGEN))]
        stats = router.cleanup_artifacts()
        assert stats["acute_foldbacks_repaired"] == 1
        assert stats["acute_foldbacks_skipped"] == 0
        assert find_foldbacks(router.routes[0].segments) == []

    def test_to_sexp_runs_the_repair(self):
        """``to_sexp`` calls ``cleanup_artifacts``, so every emitter gets it."""
        router = _router()
        router.routes = [Route(net=5, net_name="USER_LED", segments=list(USER_LED_REGEN))]
        router.to_sexp()
        assert find_foldbacks(router.routes[0].segments) == []

    def test_per_net_connectivity_is_unchanged(self):
        router = _router()
        router.routes = [Route(net=5, net_name="SWCLK", segments=list(SWCLK_COMMITTED))]
        before = Autorouter._count_net_components(router.routes)
        stats = router.cleanup_artifacts()
        after = Autorouter._count_net_components(router.routes)
        assert stats["acute_foldbacks_repaired"] == 1
        assert after == before

    def test_buried_vertex_untouched_through_a_real_via(self):
        """A same-net via over the vertex exempts it, as the gate does."""
        router = _router()
        legs = [
            seg((11.8500, 10.0000), (11.1500, 10.0000), net_name="GND"),
            seg((11.8500, 10.0000), (10.2400, 11.6100), net_name="GND"),
        ]
        router.routes = [
            Route(
                net=5,
                net_name="GND",
                segments=legs,
                vias=[
                    Via(
                        x=11.8500,
                        y=10.0000,
                        drill=0.3,
                        diameter=0.8,
                        layers=(Layer.F_CU, Layer.B_CU),
                        net=5,
                    )
                ],
            )
        ]
        stats = router.cleanup_artifacts()
        assert stats["acute_foldbacks_repaired"] == 0
        assert stats["acute_foldbacks_skipped"] == 0
        assert router.routes[0].segments == legs

    def test_width_filled_hairpin_untouched_end_to_end(self):
        router = _router()
        legs = [
            seg((35.1625, 24.7500), (35.1625, 25.4500), net_name="USER_LED"),
            seg((35.1625, 25.4500), (35.3125, 25.3000), net_name="USER_LED"),
            seg((35.3125, 25.3000), (35.8000, 24.8125), net_name="USER_LED"),
        ]
        router.routes = [Route(net=5, net_name="USER_LED", segments=list(legs))]
        stats = router.cleanup_artifacts()
        assert stats["acute_foldbacks_repaired"] == 0
        assert router.routes[0].segments == legs

    def test_repair_spanning_two_routes_of_one_net(self):
        """A degree-2 vertex can be the seam between two ``Route`` objects."""
        router = _router()
        router.routes = [
            Route(
                net=5,
                net_name="USER_LED",
                segments=[USER_LED_REGEN[0]],
            ),
            Route(
                net=5,
                net_name="USER_LED",
                segments=[USER_LED_REGEN[1]],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert stats["acute_foldbacks_repaired"] == 1
        all_segments = [s for r in router.routes for s in r.segments]
        assert find_foldbacks(all_segments) == []

    def test_unrepairable_foldback_is_counted_and_announced(self, capsys, monkeypatch):
        """A clearance-vetoed fold-back is reported, not silently left behind."""
        import kicad_tools.router.acute_foldback as af

        router = _router()
        router.routes = [Route(net=5, net_name="USER_LED", segments=list(USER_LED_REGEN))]
        real_plan = af.plan_foldback_repairs
        monkeypatch.setattr(
            "kicad_tools.router.acute_foldback.plan_foldback_repairs",
            lambda segments, **kw: real_plan(segments, **{**kw, "path_is_clear": blocked}),
        )
        stats = router.cleanup_artifacts()
        assert stats["acute_foldbacks_repaired"] == 0
        assert stats["acute_foldbacks_skipped"] == 1
        assert router.routes[0].segments == USER_LED_REGEN
        assert "NOT repairable" in capsys.readouterr().out

    def test_no_routes_is_a_noop(self):
        router = _router()
        router.routes = []
        stats = router.cleanup_artifacts()
        assert stats["acute_foldbacks_repaired"] == 0
        assert stats["acute_foldbacks_skipped"] == 0


# --------------------------------------------------------------------------
# Issue #8930: repairs on different nets must clear each other, not just the
# pre-repair copper the collision model was bound to
# --------------------------------------------------------------------------

#: ``trace_clearance`` the exact-clearance model below enforces (mm).
_CLEARANCE = 0.2


def _edge_gap(a: Segment, b: Segment) -> float:
    """Exact edge-to-edge gap between two trace segments (centreline distance
    less both half widths); an independent oracle, not the router's kernel."""

    def point_to_seg(p: Point, q: Point, r: Point) -> float:
        dx, dy = r[0] - q[0], r[1] - q[1]
        t = max(0.0, min(1.0, ((p[0] - q[0]) * dx + (p[1] - q[1]) * dy) / (dx * dx + dy * dy)))
        return math.dist(p, (q[0] + t * dx, q[1] + t * dy))

    def cross(o: Point, p: Point, q: Point) -> float:
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])

    d1, d2 = cross(b.start, b.end, a.start), cross(b.start, b.end, a.end)
    d3, d4 = cross(a.start, a.end, b.start), cross(a.start, a.end, b.end)
    if d1 * d2 < 0 and d3 * d4 < 0:
        centre = 0.0
    else:
        centre = min(
            point_to_seg(a.start, b.start, b.end),
            point_to_seg(a.end, b.start, b.end),
            point_to_seg(b.start, a.start, a.end),
            point_to_seg(b.end, a.start, a.end),
        )
    return centre - a.width / 2 - b.width / 2


def _snapshot_model(copper: list[Segment]):
    """An exact-clearance ``path_is_clear`` bound to *copper* as it stands now.

    This is what ``_repair_acute_foldbacks`` hands the planner: the checker is
    built once from the pre-repair grid, so it never learns about a chord the
    plan itself adds.
    """

    def path_is_clear(x1, y1, x2, y2, layer, width, net):
        cand = seg((x1, y1), (x2, y2), width=width, net=net, layer=layer)
        return all(
            _edge_gap(cand, other) >= _CLEARANCE
            for other in copper
            if other.net != net and other.layer == layer
        )

    return path_is_clear


#: Two net's fold-backs found by randomized search (seed 4242): each clears the
#: other's *original* copper by >= 0.2 mm, and each one's perpendicular chord
#: clears the other's original copper -- but the two chords end 0.1689 mm apart.
_NET5 = [
    seg((0.4393, 0.4393), (0.0, 0.0), width=0.25, net=5, net_name="A"),
    seg((0.0, 0.0), (1.0622, 0.0), width=0.15, net=5, net_name="A"),
]
_NET6 = [
    seg((0.6233, -0.7643), (0.265, -0.406), width=0.25, net=6, net_name="B"),
    seg((0.265, -0.406), (1.7782, -0.406), width=0.15, net=6, net_name="B"),
]


class TestCrossNetRepairsClearEachOther:
    """Item 2 of Issue #8930: a stale collision model across ``(net, layer)`` groups."""

    def _scene(self) -> list[Segment]:
        return _NET5 + _NET6

    @staticmethod
    def _cross_net_violations(copper: list[Segment]) -> list[float]:
        return [
            _edge_gap(a, b)
            for i, a in enumerate(copper)
            for b in copper[i + 1 :]
            if a.net != b.net and a.layer == b.layer and _edge_gap(a, b) < _CLEARANCE - 1e-4
        ]

    def test_scene_is_clean_before_repair(self):
        """The fixture is a legal board: the original copper already clears."""
        assert self._cross_net_violations(self._scene()) == []

    def test_snapshot_model_alone_accepts_two_chords_too_close(self):
        """The mechanism: without ``clearance`` both repairs are accepted."""
        scene = self._scene()
        out, result = repair_foldbacks(scene, path_is_clear=_snapshot_model(scene))
        assert result.repaired == 2
        violations = self._cross_net_violations(out)
        assert len(violations) == 1
        assert violations[0] == pytest.approx(0.1689, abs=1e-3)

    def test_clearance_makes_the_second_repair_see_the_first(self):
        scene = self._scene()
        out, result = repair_foldbacks(
            scene, path_is_clear=_snapshot_model(scene), clearance=_CLEARANCE
        )
        assert self._cross_net_violations(out) == []
        # Net 5 is planned first and keeps its chord; net 6 falls through to a
        # construction that clears it, so both fold-backs are still repaired.
        assert (result.repaired, result.skipped) == (2, 0)
        by_net = {r.net: r.construction for r in result.repairs}
        stale = repair_foldbacks(scene, path_is_clear=_snapshot_model(scene))[1]
        stale_by_net = {r.net: r.construction for r in stale.repairs}
        assert by_net[5] == stale_by_net[5]
        assert by_net[6] != stale_by_net[6]

    def test_clearance_rejects_and_counts_when_no_construction_clears(self):
        """No surviving candidate: rejected and counted, never forced."""
        scene = self._scene()
        base = _snapshot_model(scene)

        def only_stale_chord_for_net6(x1, y1, x2, y2, layer, width, net):
            # Net 6 may only reproduce the chord that is too close to net 5's.
            if net == 6 and (round(x1, 4), round(y1, 4)) != (0.6233, -0.7643):
                return False
            return base(x1, y1, x2, y2, layer, width, net)

        result = plan_foldback_repairs(
            _NET5 + _NET6, path_is_clear=only_stale_chord_for_net6, clearance=_CLEARANCE
        )
        assert [r.net for r in result.repairs] == [5]
        assert result.skipped == 1

    def test_plan_with_clearance_is_deterministic(self):
        scene = self._scene()
        runs = [
            plan_foldback_repairs(scene, path_is_clear=_snapshot_model(scene), clearance=_CLEARANCE)
            for _ in range(2)
        ]
        shape = [
            [(r.net, r.construction, [s.start + s.end for s in r.added]) for r in run.repairs]
            for run in runs
        ]
        assert shape[0] == shape[1]
        # Sorted (net, layer) iteration: the lower net id is planned first.
        assert [r.net for r in runs[0].repairs] == [5, 6]

    def test_same_net_chords_need_no_clearance(self):
        """Clearance applies across nets only: a same-net plan is unchanged."""
        legs = [seg((0.0, 0.0), (0.0, 1.0)), seg((0.0, 0.0), (1.0, 1.0))]
        without = repair_foldbacks(legs, path_is_clear=clear)
        with_clearance = repair_foldbacks(legs, path_is_clear=clear, clearance=_CLEARANCE)
        assert without[1].repaired == with_clearance[1].repaired == 1
        assert without[0] == with_clearance[0]

    def test_other_layer_chord_is_ignored(self):
        scene = _NET5 + [
            seg(s.start, s.end, width=s.width, net=6, layer=Layer.B_CU, net_name="B") for s in _NET6
        ]
        out, result = repair_foldbacks(scene, path_is_clear=clear, clearance=_CLEARANCE)
        assert (result.repaired, result.skipped) == (2, 0)

    def test_router_passes_the_grid_clearance_to_the_planner(self, monkeypatch):
        """``_repair_acute_foldbacks`` wires ``trace_clearance`` through."""
        import kicad_tools.router.acute_foldback as af

        router = _router()
        router.routes = [Route(net=5, net_name="USER_LED", segments=list(USER_LED_REGEN))]
        seen: dict[str, object] = {}
        real_plan = af.plan_foldback_repairs

        def spy(segments, **kw):
            seen.update(kw)
            return real_plan(segments, **kw)

        monkeypatch.setattr("kicad_tools.router.acute_foldback.plan_foldback_repairs", spy)
        router.cleanup_artifacts()
        assert seen["clearance"] == pytest.approx(float(router.grid.rules.trace_clearance))


# --------------------------------------------------------------------------
# Issue #9210: copper a later same-group repair removed must not veto
# a later group's repair
# --------------------------------------------------------------------------

#: Net 5: two fold-backs chained so that round 2 supersedes a chord round 1
#: added.  Round 1 (detour-collapse at (0.24, -0.299)) adds
#: (0.112,0.725)-(0.332,0.945)-(0.531,0.945); round 2 (detour-collapse at
#: (0.531, 0.945)) removes the second of those, (0.332,0.945)-(0.531,0.945).
_SUPERSEDING_NET5 = [
    seg((0.0, 0.0), (0.531, 0.945), net=5, net_name="A"),
    seg((0.531, 0.945), (0.24, -0.299), net=5, net_name="A"),
    seg((0.24, -0.299), (0.112, 0.725), net=5, net_name="A"),
]
#: Net 6 (planned after net 5): a fold-back whose collapse chord lands within
#: clearance (0.14 mm gap < 0.2 mm) of the superseded chord, yet clears every
#: segment net 5 actually leaves behind.  Found by randomized search (seed 11).
_LATER_NET6 = [
    seg((0.534, -0.001), (0.288, 1.355), net=6, net_name="B"),
    seg((0.288, 1.355), (0.702, 0.651), net=6, net_name="B"),
]


class TestSupersededChordDoesNotVeto:
    """Issue #9210: ``accepted_added`` must drop segments a later repair removed."""

    def _scene(self) -> list[Segment]:
        return _SUPERSEDING_NET5 + _LATER_NET6

    def test_scene_supersedes_a_chord_within_the_group(self):
        """Fixture check: net 5's second repair removes a segment its first added."""
        result = plan_foldback_repairs(_SUPERSEDING_NET5, path_is_clear=clear)
        assert [r.net for r in result.repairs] == [5, 5]
        added = {id(s) for s in result.repairs[0].added}
        assert any(id(s) in added for s in result.repairs[1].removed)

    def test_later_group_repair_is_accepted(self):
        """The chord net 5 superseded is not copper any more, so it cannot veto."""
        scene = self._scene()
        # ``clear`` as the base model isolates the plan's own veto list: the
        # only thing that can decline net 6 is the copper the plan added.
        out, result = repair_foldbacks(scene, path_is_clear=clear, clearance=_CLEARANCE)
        assert [r.net for r in result.repairs] == [5, 5, 6]
        assert (result.repaired, result.skipped) == (3, 0)
        assert TestCrossNetRepairsClearEachOther._cross_net_violations(out) == []
