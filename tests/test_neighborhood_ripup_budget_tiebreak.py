"""Regression tests for Issue #3230 budget-aware rip-up tie-break.

These tests verify the new ``NegotiatedRouter.score_foreign_budget_overlap``
helper and that ``neighborhood_ripup`` consumes its output as a strict
tie-break after the primary stuck-nets count.  The tie-break only fires
when two blockers have the same primary score; non-softstart boards
whose blocker counts differ see identical pre-#3230 ordering.

Background: The negotiated rip-up loop's blocker-selection heuristic
previously scored candidates by "how many stuck nets does this blocker
block".  When two candidates tied (typical on a softstart-like 4-pad
cluster where multiple blockers obstruct all four stuck nets), the
sort fell through to dict iteration order, which on the C++ backend
plus PYTHONHASHSEED varies and is uncorrelated with the per-pad
channel budget (#3143).  This caused the rip-up cohort to cycle on
the same N nets without escaping the local minimum (stagnation
watchdog trips).

The fix adds the foreign-budget overlap as a secondary sort key:
blockers whose routes occupy MORE cells inside foreign
``PadChannelBudget`` rectangles are preferred for rip-up, because
they are the "channel squatters" most likely to be the cause of the
stalemate.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

from kicad_tools.router.algorithms.negotiated import NegotiatedRouter


@dataclass
class FakeBudget:
    """Stand-in for ``router_cpp.PadChannelBudget``.

    The real C++ binding struct exposes ``gx1/gy1/gx2/gy2/layer/source_net``
    as plain attributes.  This dataclass duck-types against the
    ``score_foreign_budget_overlap`` consumer (which uses ``getattr``).
    """

    gx1: int
    gy1: int
    gx2: int
    gy2: int
    source_net: int
    layer: int = -1


@dataclass
class FakeLayer:
    """Stand-in for ``primitives.Layer`` enum."""

    value: int


@dataclass
class FakeSegment:
    """Stand-in for ``primitives.Segment`` -- only the fields the
    scorer touches (world coords + layer)."""

    x1: float
    y1: float
    x2: float
    y2: float
    layer: FakeLayer


@dataclass
class FakeRoute:
    """Stand-in for ``primitives.Route``."""

    net: int
    segments: list[FakeSegment]


def _make_neg_router_with_grid():
    """Build a NegotiatedRouter whose grid does a 1:1 world->grid map.

    The scorer calls ``self.grid.world_to_grid(seg.x1, seg.y1)`` and
    ``self.grid.layer_to_index(seg.layer.value)``.  Stub both so the
    test inputs are easy to reason about: world coords ARE grid cells,
    and layer values pass through unchanged.
    """

    mock_grid = MagicMock()
    mock_grid.world_to_grid.side_effect = lambda x, y: (int(x), int(y))
    mock_grid.layer_to_index.side_effect = lambda v: int(v)
    mock_router = MagicMock()
    # Default: no budgets configured (matches pre-#3143 / Python backend).
    mock_router._pad_channel_budgets = []
    neg = NegotiatedRouter(mock_grid, mock_router, MagicMock(), {})
    return neg


class TestScoreForeignBudgetOverlap:
    """Direct tests for the ``score_foreign_budget_overlap`` helper."""

    def test_empty_budgets_returns_zero_scores(self):
        """No budget data -> all scores are zero (AC #5 A/B knob: when
        ``KCT_DISABLE_PAD_BUDGETS=1`` clears the budget list, this is
        the path the helper takes)."""
        neg = _make_neg_router_with_grid()
        scores = neg.score_foreign_budget_overlap(
            candidate_nets=[10, 20, 30],
            net_routes={
                10: [FakeRoute(10, [FakeSegment(0, 0, 5, 0, FakeLayer(0))])],
                20: [FakeRoute(20, [FakeSegment(5, 5, 10, 5, FakeLayer(0))])],
            },
            pad_channel_budgets=[],
        )
        assert scores == {10: 0, 20: 0, 30: 0}

    def test_empty_candidates_returns_empty_dict(self):
        """No candidates -> empty result."""
        neg = _make_neg_router_with_grid()
        scores = neg.score_foreign_budget_overlap(
            candidate_nets=[],
            net_routes={},
            pad_channel_budgets=[FakeBudget(0, 0, 10, 10, source_net=1)],
        )
        assert scores == {}

    def test_route_inside_own_budget_scores_zero(self):
        """A net's route inside ITS OWN budget rectangle must NOT be
        penalised -- the budget exists specifically to reserve the
        channel for this net."""
        neg = _make_neg_router_with_grid()
        budgets = [FakeBudget(0, 0, 10, 10, source_net=7)]
        net_routes = {
            7: [FakeRoute(7, [FakeSegment(2, 2, 8, 8, FakeLayer(-1))])],
        }
        scores = neg.score_foreign_budget_overlap([7], net_routes, budgets)
        assert scores[7] == 0

    def test_route_inside_foreign_budget_scores_positive(self):
        """A net camped inside a budget belonging to a DIFFERENT net
        must produce a positive score.  Three sample cells per segment
        (endpoints + midpoint) all fall inside the rectangle, giving
        a count of 3."""
        neg = _make_neg_router_with_grid()
        # Budget owned by net 1, rectangle (0,0)-(10,10).
        budgets = [FakeBudget(0, 0, 10, 10, source_net=1, layer=-1)]
        # Net 99's route runs entirely inside that rectangle.
        net_routes = {
            99: [FakeRoute(99, [FakeSegment(2, 2, 8, 8, FakeLayer(0))])],
        }
        scores = neg.score_foreign_budget_overlap([99], net_routes, budgets)
        assert scores[99] == 3  # endpoint + midpoint + endpoint

    def test_layer_filter_respected(self):
        """A budget pinned to a specific layer must not score routes
        on a different layer."""
        neg = _make_neg_router_with_grid()
        # Budget on layer 0 only.
        budgets = [FakeBudget(0, 0, 10, 10, source_net=1, layer=0)]
        # Net 99's route is on layer 1 (different layer).
        net_routes = {
            99: [FakeRoute(99, [FakeSegment(2, 2, 8, 8, FakeLayer(1))])],
        }
        scores = neg.score_foreign_budget_overlap([99], net_routes, budgets)
        assert scores[99] == 0

    def test_layer_any_matches_any_segment_layer(self):
        """A budget with layer=-1 (all layers) must match segments on
        any layer."""
        neg = _make_neg_router_with_grid()
        budgets = [FakeBudget(0, 0, 10, 10, source_net=1, layer=-1)]
        # Net 99's route is on layer 3.
        net_routes = {
            99: [FakeRoute(99, [FakeSegment(2, 2, 8, 8, FakeLayer(3))])],
        }
        scores = neg.score_foreign_budget_overlap([99], net_routes, budgets)
        assert scores[99] == 3

    def test_route_outside_budget_scores_zero(self):
        """A route entirely outside any budget rectangle scores zero."""
        neg = _make_neg_router_with_grid()
        budgets = [FakeBudget(0, 0, 5, 5, source_net=1)]
        # Net 99's route is far outside (20..30, 20..30).
        net_routes = {
            99: [FakeRoute(99, [FakeSegment(20, 20, 30, 30, FakeLayer(0))])],
        }
        scores = neg.score_foreign_budget_overlap([99], net_routes, budgets)
        assert scores[99] == 0


class TestFindBlockingNetsRelaxedTieBreak:
    """Verify that ``neighborhood_ripup``'s sort uses the budget signal
    as a secondary key when the primary stuck-nets count ties.

    This is the curator's recommended fix shape for Issue #3230 --
    minimally invasive (tie-break only), preserves pre-#3230 ordering
    on non-softstart boards whose blocker counts don't tie, and
    activates the per-pad channel budget signal exactly when the
    rip-up loop would otherwise have cycled on the same N nets.
    """

    def test_tie_break_prefers_higher_foreign_overlap(self):
        """When two blockers obstruct the same number of stuck nets,
        the one parked in MORE foreign budget rectangles should be
        ripped first.

        Setup mirrors a softstart-like 4-pad cluster:
        - Blockers 100, 200 each block one stuck net (tie at count=1).
        - Budget rectangle 0..5,0..5 is owned by net 999 (foreign to
          both blockers).
        - Blocker 100's route enters the rectangle (overlap=3).
        - Blocker 200's route stays outside (overlap=0).
        - Expected: 100 is ripped first.
        """
        neg = _make_neg_router_with_grid()
        # The C++ backend exposes pad-channel budgets on the wrapped
        # router.  Use the same attribute name (``_pad_channel_budgets``)
        # the production code reads via ``getattr``.
        neg.router._pad_channel_budgets = [
            FakeBudget(0, 0, 5, 5, source_net=999, layer=-1),
        ]
        # Stub find_blocking_nets_relaxed to return tied scores.
        neg.find_blocking_nets_relaxed = MagicMock(
            return_value={100: 1, 200: 1},
        )
        # Stub rip_up_nets / route_net_negotiated to capture call order.
        ripped_nets: list[int] = []

        def _record_rip(nets, _routes, _master):
            ripped_nets.extend(nets)

        neg.rip_up_nets = MagicMock(side_effect=_record_rip)
        # Make every re-route attempt fail so the loop tries each
        # candidate in order without short-circuiting on success.
        neg.route_net_negotiated = MagicMock(return_value=[])

        # Net routes: 100 is inside the foreign budget; 200 is far away.
        net_routes: dict[int, list] = {
            100: [
                FakeRoute(
                    100,
                    [FakeSegment(2, 2, 4, 4, FakeLayer(0))],
                ),
            ],
            200: [
                FakeRoute(
                    200,
                    [FakeSegment(20, 20, 30, 30, FakeLayer(0))],
                ),
            ],
        }

        # max_attempts=1 so we only consume the first sorted candidate.
        neg.neighborhood_ripup(
            failed_nets=[10],
            net_routes=net_routes,
            routes_list=[],
            pads_by_net={10: [MagicMock(), MagicMock()]},
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            max_attempts=1,
            initial_radius_factor=1.0,
            escalation_factor=1.0,
        )

        # The first call to rip_up_nets must have included net 100
        # (the higher-overlap blocker) as the chosen blocker_net.
        # ``rip_up_nets`` is called with the *neighborhood* including
        # the blocker, so 100 must appear in the first call's args.
        assert neg.rip_up_nets.call_count >= 1
        first_call_nets = list(neg.rip_up_nets.call_args_list[0].args[0])
        assert 100 in first_call_nets, (
            f"Expected blocker 100 (high foreign-budget overlap) to be "
            f"chosen first, but first rip-up was {first_call_nets}"
        )

    def test_no_tie_break_when_counts_differ(self):
        """When blocker counts differ, the budget signal does NOT
        override the primary key -- the higher-count blocker is
        chosen regardless of foreign-budget overlap.  This preserves
        pre-#3230 behaviour on boards whose blocker counts don't tie."""
        neg = _make_neg_router_with_grid()
        # Foreign budget owned by net 999.
        neg.router._pad_channel_budgets = [
            FakeBudget(0, 0, 5, 5, source_net=999, layer=-1),
        ]
        # Net 100 blocks 1 stuck net; net 200 blocks 5 stuck nets.
        neg.find_blocking_nets_relaxed = MagicMock(
            return_value={100: 1, 200: 5},
        )
        neg.rip_up_nets = MagicMock()
        neg.route_net_negotiated = MagicMock(return_value=[])

        # Net 100 is parked in foreign budget (high overlap);
        # net 200 is far away (overlap=0).
        # Without the primary-key precedence, 100 would be chosen
        # (higher overlap).  With it, 200 must be chosen (higher count).
        net_routes: dict[int, list] = {
            100: [
                FakeRoute(
                    100,
                    [FakeSegment(2, 2, 4, 4, FakeLayer(0))],
                ),
            ],
            200: [
                FakeRoute(
                    200,
                    [FakeSegment(20, 20, 30, 30, FakeLayer(0))],
                ),
            ],
        }

        neg.neighborhood_ripup(
            failed_nets=[10],
            net_routes=net_routes,
            routes_list=[],
            pads_by_net={10: [MagicMock(), MagicMock()]},
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            max_attempts=1,
            initial_radius_factor=1.0,
            escalation_factor=1.0,
        )

        first_call_nets = list(neg.rip_up_nets.call_args_list[0].args[0])
        assert 200 in first_call_nets, (
            f"Expected blocker 200 (higher stuck-nets count) to be "
            f"chosen first regardless of foreign-budget overlap, but "
            f"first rip-up was {first_call_nets}"
        )

    def test_empty_budget_list_preserves_pre_3230_ordering(self):
        """When the budget list is empty (Python backend OR
        ``KCT_DISABLE_PAD_BUDGETS=1`` cleared it at the pre-pass), the
        secondary key collapses to zero for every blocker and the
        tertiary deterministic key (-net_id) takes over.  Verifies
        AC #5: the A/B knob is a clean no-op when budgets are absent."""
        neg = _make_neg_router_with_grid()
        # No budgets configured at all.
        neg.router._pad_channel_budgets = []
        # Tied counts.
        neg.find_blocking_nets_relaxed = MagicMock(
            return_value={100: 1, 200: 1},
        )
        neg.rip_up_nets = MagicMock()
        neg.route_net_negotiated = MagicMock(return_value=[])

        net_routes: dict[int, list] = {
            100: [
                FakeRoute(
                    100,
                    [FakeSegment(2, 2, 4, 4, FakeLayer(0))],
                ),
            ],
            200: [
                FakeRoute(
                    200,
                    [FakeSegment(20, 20, 30, 30, FakeLayer(0))],
                ),
            ],
        }

        neg.neighborhood_ripup(
            failed_nets=[10],
            net_routes=net_routes,
            routes_list=[],
            pads_by_net={10: [MagicMock(), MagicMock()]},
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            max_attempts=1,
            initial_radius_factor=1.0,
            escalation_factor=1.0,
        )

        # With overlap all-zero and ties broken by -net_id (descending
        # net id first), the chosen blocker should be the higher net
        # id (200).  This is the deterministic fallback when budgets
        # are not consulted.
        first_call_nets = list(neg.rip_up_nets.call_args_list[0].args[0])
        assert 200 in first_call_nets, (
            f"Expected deterministic -net_id fallback to choose 200 "
            f"when budget data is absent, but first rip-up was "
            f"{first_call_nets}"
        )
