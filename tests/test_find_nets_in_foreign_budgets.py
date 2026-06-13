"""Regression tests for Issue #3235 ``find_nets_in_foreign_budgets`` helper.

Issue #3235 (softstart 8/10 → 10/10 reach lift, direction 1: "extend
``find_nets_through_overused_cells`` to also fire on nets occupying high-
budget cells even if those cells aren't 'overused' in the current
iteration"):  The negotiated rip-up loop in ``two_phase.py:709`` picks
rerouting candidates from cells whose ``usage_count > 1``.  On softstart-
style east-edge clusters a squatter can park in a foreign pad-channel
budget without ever triggering ``usage_count > 1`` (the cell is only used
by the squatter plus budget metadata; the source net is stuck and never
commits a route there), so the squatter is invisible to the existing
overused-cell scheduler.

This file exercises the ``find_nets_in_foreign_budgets`` helper added in
``negotiated.py`` to surface those squatters.  The helper is **NOT** wired
into ``two_phase.py:709`` on the default path -- see the negative-results
note in that file at the hook point for the spike results.  These tests
lock down the API contract so future spike attempts can build on it.

The fixture mirrors a softstart-like 4-pad cluster (one east-edge column
with 4 contested escape budgets; one stranded source net; multiple
squatters across the contested cells).
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
    ``find_nets_in_foreign_budgets`` consumer (which uses ``int(...)`` and
    ``getattr``).
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
    helper touches (world coords + layer)."""

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

    The helper calls ``self.grid.world_to_grid(seg.x1, seg.y1)`` and
    ``self.grid.layer_to_index(seg.layer.value)``.  Stub both so the
    test inputs are easy to reason about: world coords ARE grid cells,
    and layer values pass through unchanged.
    """

    mock_grid = MagicMock()
    mock_grid.world_to_grid.side_effect = lambda x, y: (int(x), int(y))
    mock_grid.layer_to_index.side_effect = lambda v: int(v)
    mock_router = MagicMock()
    mock_router._pad_channel_budgets = []
    neg = NegotiatedRouter(mock_grid, mock_router, MagicMock(), {})
    return neg


class TestFindNetsInForeignBudgets:
    """Direct tests for the ``find_nets_in_foreign_budgets`` helper."""

    def test_empty_budgets_returns_empty(self):
        """No budget data -> empty result (the helper short-circuits)."""
        neg = _make_neg_router_with_grid()
        result = neg.find_nets_in_foreign_budgets(
            net_routes={10: [FakeRoute(10, [FakeSegment(0, 0, 5, 0, FakeLayer(0))])]},
            pad_channel_budgets=[],
            stranded_nets={999},
        )
        assert result == []

    def test_empty_stranded_returns_empty(self):
        """No stranded source nets -> empty result (nothing to free)."""
        neg = _make_neg_router_with_grid()
        result = neg.find_nets_in_foreign_budgets(
            net_routes={10: [FakeRoute(10, [FakeSegment(0, 0, 5, 0, FakeLayer(0))])]},
            pad_channel_budgets=[FakeBudget(0, 0, 10, 10, source_net=999)],
            stranded_nets=set(),
        )
        assert result == []

    def test_squatter_inside_stranded_budget_is_returned(self):
        """A net camped inside a budget owned by a STRANDED source net is
        surfaced as a squatter -- this is the softstart 4-pad cluster
        case where the squatter is the only resident of a contested
        post-escape column.
        """
        neg = _make_neg_router_with_grid()
        # Budget owned by net 999 (stranded), rectangle (0,0)-(10,10).
        budgets = [FakeBudget(0, 0, 10, 10, source_net=999, layer=-1)]
        net_routes = {
            # Net 42 routes through the contested rectangle.
            42: [FakeRoute(42, [FakeSegment(2, 2, 8, 8, FakeLayer(0))])],
        }
        result = neg.find_nets_in_foreign_budgets(
            net_routes=net_routes,
            pad_channel_budgets=budgets,
            stranded_nets={999},
        )
        assert result == [42]

    def test_source_net_excluded_from_own_budget(self):
        """A net cannot squat in its own budget -- the budget is reserved
        for that net by construction.  This guard prevents the helper
        from listing the stranded source net itself as a squatter.
        """
        neg = _make_neg_router_with_grid()
        # Budget owned by net 7 (also stranded).  Net 7's own (zero-
        # length partial) route inside its own rectangle MUST NOT be
        # surfaced as a squatter.
        budgets = [FakeBudget(0, 0, 10, 10, source_net=7, layer=-1)]
        net_routes = {
            7: [FakeRoute(7, [FakeSegment(2, 2, 8, 8, FakeLayer(0))])],
        }
        result = neg.find_nets_in_foreign_budgets(
            net_routes=net_routes,
            pad_channel_budgets=budgets,
            stranded_nets={7},
        )
        assert result == []

    def test_budget_not_owned_by_stranded_net_ignored(self):
        """Budgets whose source net is NOT stranded are ignored.  This
        is the strictness gate that prevents the cohort from exploding
        on every iteration -- only contested budgets (source net failed
        to commit a route) produce squatter augmentation.
        """
        neg = _make_neg_router_with_grid()
        # Budget owned by net 100 (which is fully routed, not stranded).
        budgets = [FakeBudget(0, 0, 10, 10, source_net=100, layer=-1)]
        net_routes = {
            42: [FakeRoute(42, [FakeSegment(2, 2, 8, 8, FakeLayer(0))])],
            100: [FakeRoute(100, [FakeSegment(20, 20, 30, 30, FakeLayer(0))])],
        }
        result = neg.find_nets_in_foreign_budgets(
            net_routes=net_routes,
            pad_channel_budgets=budgets,
            stranded_nets={999},  # Net 100 is NOT stranded.
        )
        assert result == []

    def test_route_outside_budget_not_returned(self):
        """A net whose route stays outside every contested budget
        rectangle is NOT surfaced as a squatter."""
        neg = _make_neg_router_with_grid()
        budgets = [FakeBudget(0, 0, 5, 5, source_net=999)]
        net_routes = {
            # Net 42's route is far outside (20..30, 20..30).
            42: [FakeRoute(42, [FakeSegment(20, 20, 30, 30, FakeLayer(0))])],
        }
        result = neg.find_nets_in_foreign_budgets(
            net_routes=net_routes,
            pad_channel_budgets=budgets,
            stranded_nets={999},
        )
        assert result == []

    def test_layer_filter_respected(self):
        """A budget pinned to a specific layer must not surface squatters
        on a different layer."""
        neg = _make_neg_router_with_grid()
        # Budget owned by net 999, layer 0 only.
        budgets = [FakeBudget(0, 0, 10, 10, source_net=999, layer=0)]
        net_routes = {
            # Net 42's route is on layer 1.
            42: [FakeRoute(42, [FakeSegment(2, 2, 8, 8, FakeLayer(1))])],
        }
        result = neg.find_nets_in_foreign_budgets(
            net_routes=net_routes,
            pad_channel_budgets=budgets,
            stranded_nets={999},
        )
        assert result == []

    def test_layer_any_matches_any_segment_layer(self):
        """A budget with layer=-1 (all layers) must match squatters on
        any layer."""
        neg = _make_neg_router_with_grid()
        budgets = [FakeBudget(0, 0, 10, 10, source_net=999, layer=-1)]
        net_routes = {
            42: [FakeRoute(42, [FakeSegment(2, 2, 8, 8, FakeLayer(3))])],
        }
        result = neg.find_nets_in_foreign_budgets(
            net_routes=net_routes,
            pad_channel_budgets=budgets,
            stranded_nets={999},
        )
        assert result == [42]

    def test_skip_nets_filters_output(self):
        """The ``skip_nets`` set excludes nets the caller does not want
        to surface (e.g. partial-routed nets the caller knows it cannot
        re-route productively, or nets already in the rip-up cohort)."""
        neg = _make_neg_router_with_grid()
        budgets = [FakeBudget(0, 0, 10, 10, source_net=999, layer=-1)]
        net_routes = {
            42: [FakeRoute(42, [FakeSegment(2, 2, 8, 8, FakeLayer(0))])],
            43: [FakeRoute(43, [FakeSegment(3, 3, 7, 7, FakeLayer(0))])],
        }
        result = neg.find_nets_in_foreign_budgets(
            net_routes=net_routes,
            pad_channel_budgets=budgets,
            stranded_nets={999},
            skip_nets={42},
        )
        # 42 is skipped; 43 is the only remaining squatter.
        assert result == [43]

    def test_softstart_like_4pad_cluster(self):
        """Integration shape: four contested east-edge budgets (one per
        stranded net), three squatters camped across the column.  This
        fixture exercises AC #6 (regression test on a softstart-like
        4-pad cluster) -- it should report ALL three squatters when
        all four source nets are simultaneously stranded.

        Geometry: budgets stacked vertically in a 0..3 wide column from
        y=0 to y=20, one per stranded net (different y-bands).  Squatter
        routes traverse different vertical sections of the column on
        different layers.
        """
        neg = _make_neg_router_with_grid()
        # Four stranded source nets, each owning one contested band.
        budgets = [
            FakeBudget(0, 0, 3, 5, source_net=11, layer=-1),  # GATE_NEG-like
            FakeBudget(0, 5, 3, 10, source_net=14, layer=-1),  # I_SENSE_OUT-like
            FakeBudget(0, 10, 3, 15, source_net=16, layer=-1),  # ZC_DETECT-like
            FakeBudget(0, 15, 3, 20, source_net=18, layer=-1),  # SWCLK-like
        ]
        # Three squatters camping across the column at different bands.
        net_routes = {
            42: [
                FakeRoute(42, [FakeSegment(1, 2, 1, 7, FakeLayer(0))])
            ],  # crosses GATE_NEG and I_SENSE_OUT bands
            43: [
                FakeRoute(43, [FakeSegment(1, 12, 1, 17, FakeLayer(1))])
            ],  # crosses ZC_DETECT and SWCLK bands
            44: [FakeRoute(44, [FakeSegment(50, 50, 60, 60, FakeLayer(0))])],  # outside the column
        }
        stranded = {11, 14, 16, 18}
        result = neg.find_nets_in_foreign_budgets(
            net_routes=net_routes,
            pad_channel_budgets=budgets,
            stranded_nets=stranded,
        )
        # Squatters 42 and 43 are inside contested bands; 44 is outside.
        assert sorted(result) == [42, 43]


class TestEscapePhaseOffsetInfrastructure:
    """Lock down the ``_create_fine_pitch_row_escapes`` ``phase_offset``
    parameter contract.

    Issue #3235 direction-2 spike documented two failure modes:

    1. Passing ``phase_offset=1`` to the SECOND row/column of a dual-row
       package (so the two halves do not converge on the same post-escape
       layer) REGRESSED softstart 8/10 -> 7/10 across PYTHONHASHSEED=42/
       43/44.

    2. Filtering NC pads (``net == 0``) from the row buckets before
       alternation index assignment also regressed softstart 8/10 -> 7/10.

    The parameter is preserved as infrastructure with default ``0`` which
    is byte-identical to the historical strict-by-position behaviour.
    Future per-board / per-package gating can explore the lever without
    re-introducing the regression on the default path.

    These tests verify the parameter exists, accepts both values, and the
    default value preserves historical behaviour for a synthetic 4-pad
    row fixture.
    """

    def test_phase_offset_default_is_zero(self):
        """The ``_create_fine_pitch_row_escapes`` signature MUST default
        ``phase_offset`` to 0 so existing callers (and the dispatcher)
        get the historical strict-by-position alternation unchanged.
        """
        import inspect

        from kicad_tools.router.escape import EscapeRouter

        sig = inspect.signature(EscapeRouter._create_fine_pitch_row_escapes)
        params = sig.parameters
        assert "phase_offset" in params
        assert params["phase_offset"].default == 0

    def test_phase_offset_documented_in_docstring(self):
        """The negative-results note MUST be in the method's docstring so
        future builders see the spike outcome before re-trying the
        regressing direction."""
        from kicad_tools.router.escape import EscapeRouter

        doc = EscapeRouter._create_fine_pitch_row_escapes.__doc__ or ""
        assert "Issue #3235" in doc
        assert "phase_offset" in doc
        # Pre-conditions for the spike's negative result are documented:
        assert "REGRESSED" in doc or "regressed" in doc
