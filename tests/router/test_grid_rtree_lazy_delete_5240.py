"""Lazy R-tree segment deletion parity tests (Issue #5240).

``RoutingGrid._rtree_remove_segment`` no longer calls
``rtree.Index.delete`` (an O(index size) search) on every rip-up.  It drops
the entry from the live ledgers and tombstones it until the layer index is
compacted in bulk.  These tests pin the observable contract that change must
preserve:

* every query site maps candidate ids through ``_seg_rtree_items``, so a
  tombstoned entry must never influence a clearance verdict;
* the live ledgers (``_seg_rtree_items`` / ``_seg_rtree_count``) stay exactly
  as eager deletion left them;
* compaction physically removes the tombstones;
* re-inserting the same ``Segment`` object resurrects its entry instead of
  duplicating it, and an in-place geometry mutation (Issue #3507) drops the
  stale entry rather than leaving a wrong-place duplicate.
"""

import random

import pytest

from kicad_tools.router.grid import (
    RTREE_AVAILABLE,
    RTREE_TOMBSTONE_MIN_COMPACT,
    RoutingGrid,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(not RTREE_AVAILABLE, reason="rtree library not installed")


@pytest.fixture
def rules():
    return DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.127)


@pytest.fixture
def grid(rules):
    return RoutingGrid(width=50.0, height=50.0, rules=rules)


def _seg(x1, y1, x2, y2, net=1, layer=Layer.F_CU, width=0.2):
    return Segment(
        x1=x1, y1=y1, x2=x2, y2=y2, width=width, layer=layer, net=net, net_name=f"NET{net}"
    )


def _route(segments, net=1):
    return Route(net=net, net_name=f"NET{net}", segments=list(segments), vias=[])


def _raw_ids(grid, layer_idx, envelope):
    """Ids the raw R-tree returns for ``envelope`` (tombstones included)."""
    return list(grid._seg_rtree[layer_idx].intersection(envelope))


# ---------------------------------------------------------------------------
# Live-ledger contract
# ---------------------------------------------------------------------------


class TestLiveLedgers:
    def test_removal_updates_items_and_count(self, grid):
        seg = _seg(1.0, 1.0, 5.0, 1.0)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        grid._rtree_insert_segment(seg, layer_idx)
        assert grid._seg_rtree_count == 1
        assert id(seg) in grid._seg_rtree_items[layer_idx]

        grid._rtree_remove_segment(seg, layer_idx)
        assert grid._seg_rtree_count == 0
        assert id(seg) not in grid._seg_rtree_items[layer_idx]
        assert id(seg) not in grid._seg_rtree_envelopes[layer_idx]

    def test_double_removal_is_idempotent(self, grid):
        seg = _seg(1.0, 1.0, 5.0, 1.0)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        grid._rtree_insert_segment(seg, layer_idx)
        grid._rtree_remove_segment(seg, layer_idx)
        grid._rtree_remove_segment(seg, layer_idx)
        assert grid._seg_rtree_count == 0
        assert len(grid._seg_rtree_pending[layer_idx]) == 1

    def test_tombstone_retains_segment_so_ids_cannot_be_recycled(self, grid):
        """The tombstone must own a strong reference to the Segment.

        Entries are keyed by ``id()``; if the object were freed while its
        entry is still in the tree, CPython could hand the same address to a
        different Segment and the stale entry would masquerade as it.
        """
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        seg = _seg(1.0, 1.0, 5.0, 1.0)
        seg_id = id(seg)
        grid._rtree_insert_segment(seg, layer_idx)
        grid._rtree_remove_segment(seg, layer_idx)
        del seg
        assert seg_id in grid._seg_rtree_pending[layer_idx]
        assert id(grid._seg_rtree_pending[layer_idx][seg_id][0]) == seg_id


# ---------------------------------------------------------------------------
# Query parity: a tombstone must never change a verdict
# ---------------------------------------------------------------------------


class TestQueryParity:
    def test_ripped_segment_no_longer_blocks_clearance(self, grid):
        """A removed neighbour stops constraining, tombstone notwithstanding."""
        victim = _seg(1.0, 5.0, 9.0, 5.0, net=1)
        grid.mark_route(_route([victim], net=1))

        probe = _seg(1.0, 5.05, 9.0, 5.05, net=2)
        ok_before, _, _ = grid.validate_segment_clearance(probe, exclude_net=2)
        assert ok_before is False, "sanity: the marked neighbour must violate"

        grid.unmark_route(_route([victim], net=1))
        # The route object identity differs, so remove the segment directly.
        grid._rtree_remove_segment(victim, grid.layer_to_index(Layer.F_CU.value))
        ok_after, _, _ = grid.validate_segment_clearance(probe, exclude_net=2)
        assert ok_after is True, "tombstoned entry must not produce a violation"

    def test_random_mark_unmark_matches_a_compacted_index(self, grid, rules):
        """Differential test against an index with no tombstones at all.

        The reference grid replays the same mark/unmark sequence and then
        rebuilds its segment index from scratch (``_rebuild_segment_index``),
        which is tombstone-free by construction.  Every clearance verdict and
        reported clearance value must agree.

        The third return element (``violation_loc``) is deliberately NOT
        compared: it is assigned by whichever violating candidate is visited
        last, so it depends on R-tree candidate order -- which the R-tree and
        brute-force branches of ``validate_segment_clearance`` already
        disagree about on unmodified code.  See
        ``test_violation_location_is_already_branch_dependent`` below, which
        pins that pre-existing arbitrariness so this exclusion stays honest.
        """
        reference = RoutingGrid(width=50.0, height=50.0, rules=rules)
        rng = random.Random(5240)

        live_lazy: list[Segment] = []
        live_ref: list[Segment] = []
        for _ in range(240):
            if live_lazy and rng.random() < 0.45:
                i = rng.randrange(len(live_lazy))
                lazy_seg = live_lazy.pop(i)
                ref_seg = live_ref.pop(i)
                grid.unmark_route(_route([lazy_seg], net=lazy_seg.net))
                reference.unmark_route(_route([ref_seg], net=ref_seg.net))
            else:
                x = rng.uniform(2.0, 45.0)
                y = rng.uniform(2.0, 45.0)
                dx = rng.choice([-3.0, -1.5, 1.5, 3.0])
                net = rng.randrange(1, 6)
                coords = (x, y, x + dx, y + rng.choice([0.0, dx]))
                lazy_seg = _seg(*coords, net=net)
                ref_seg = _seg(*coords, net=net)
                grid.mark_route(_route([lazy_seg], net=net))
                reference.mark_route(_route([ref_seg], net=net))
                live_lazy.append(lazy_seg)
                live_ref.append(ref_seg)

        # Reference: rebuild so no lazily removed entry can survive in it.
        reference._rebuild_segment_index()
        assert all(not pending for pending in reference._seg_rtree_pending.values())

        for _ in range(400):
            x = rng.uniform(2.0, 45.0)
            y = rng.uniform(2.0, 45.0)
            probe = _seg(x, y, x + rng.choice([-2.0, 2.0]), y, net=rng.randrange(1, 6))
            got = grid.validate_segment_clearance(probe, exclude_net=probe.net)
            want = reference.validate_segment_clearance(probe, exclude_net=probe.net)
            assert got[0] == want[0]
            assert got[1] == pytest.approx(want[1], abs=1e-12, nan_ok=True)

    def test_violation_location_is_already_branch_dependent(self, grid):
        """``violation_loc`` is a representative, not a certified, output.

        Tombstoning changes the *order* in which surviving candidates come
        back from the R-tree, which can change which violating pair gets
        named in ``violation_loc``.  That is only acceptable because the
        field is already order-dependent: ``validate_segment_clearance``
        assigns it from whichever violating candidate is visited last, and
        its own R-tree and brute-force branches therefore disagree about it
        for the same grid on unmodified code.

        This test pins that pre-existing property on a probe with two
        violating neighbours.  ``is_valid`` and ``actual_clearance`` -- the
        outputs callers act on -- must agree between the two branches
        (Issue #3522); only the location may differ.
        """
        # Two foreign-net neighbours, both violating, at different distances.
        near = _seg(1.0, 5.00, 9.0, 5.00, net=1)
        far = _seg(1.0, 5.20, 9.0, 5.20, net=3)
        grid.mark_route(_route([near], net=1))
        grid.mark_route(_route([far], net=3))
        # Pad the index past RTREE_SEGMENT_THRESHOLD so the R-tree branch runs.
        for i in range(40):
            grid.mark_route(_route([_seg(20.0, 1.0 + 0.5 * i, 24.0, 1.0 + 0.5 * i, net=7)], net=7))

        probe = _seg(1.0, 5.10, 9.0, 5.10, net=2)
        indexed = grid.validate_segment_clearance(probe, exclude_net=2)
        grid._rtree_available = False
        try:
            brute = grid.validate_segment_clearance(probe, exclude_net=2)
        finally:
            grid._rtree_available = RTREE_AVAILABLE

        assert indexed[0] is False and brute[0] is False, "sanity: both must see a violation"
        assert indexed[0] == brute[0], "verdict is certified and must not be branch-dependent"
        assert indexed[1] == pytest.approx(brute[1], abs=1e-12), (
            "actual_clearance is certified (Issue #3522) and must not be branch-dependent"
        )
        # The location is whichever violator came last, so both branches must
        # name *a* real violating pair -- but not necessarily the same one.
        for loc in (indexed[2], brute[2]):
            assert loc is not None
            assert 1.0 <= loc[0] <= 9.0


# ---------------------------------------------------------------------------
# Compaction and re-insertion
# ---------------------------------------------------------------------------


class TestCompaction:
    def test_tombstones_are_compacted_away(self, grid):
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        segs = [_seg(1.0, 1.0 + 0.3 * i, 5.0, 1.0 + 0.3 * i, net=i + 1) for i in range(80)]
        for seg in segs:
            grid._rtree_insert_segment(seg, layer_idx)
        assert len(grid._seg_rtree_pending[layer_idx]) == 0

        removed_ids = []
        for seg in segs[:RTREE_TOMBSTONE_MIN_COMPACT]:
            removed_ids.append(id(seg))
            grid._rtree_remove_segment(seg, layer_idx)

        # Compaction fired on the last removal.
        assert grid._seg_rtree_pending[layer_idx] == {}
        surviving = set(_raw_ids(grid, layer_idx, (0.0, 0.0, 50.0, 50.0)))
        assert surviving == {id(s) for s in segs[RTREE_TOMBSTONE_MIN_COMPACT:]}
        assert surviving.isdisjoint(removed_ids)
        assert grid._seg_rtree_count == len(segs) - RTREE_TOMBSTONE_MIN_COMPACT

    def test_compaction_of_an_empty_layer_leaves_a_usable_index(self, grid):
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        segs = [_seg(1.0, 1.0 + 0.3 * i, 5.0, 1.0 + 0.3 * i, net=i + 1) for i in range(4)]
        for seg in segs:
            grid._rtree_insert_segment(seg, layer_idx)
        for seg in segs:
            grid._rtree_remove_segment(seg, layer_idx)
        grid._compact_segment_index(layer_idx)

        assert _raw_ids(grid, layer_idx, (0.0, 0.0, 50.0, 50.0)) == []
        fresh = _seg(2.0, 2.0, 6.0, 2.0, net=9)
        grid._rtree_insert_segment(fresh, layer_idx)
        assert _raw_ids(grid, layer_idx, (0.0, 0.0, 50.0, 50.0)) == [id(fresh)]

    def test_reinserting_the_same_segment_does_not_duplicate_its_entry(self, grid):
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        seg = _seg(1.0, 1.0, 5.0, 1.0)
        grid._rtree_insert_segment(seg, layer_idx)
        grid._rtree_remove_segment(seg, layer_idx)
        grid._rtree_insert_segment(seg, layer_idx)

        assert grid._seg_rtree_count == 1
        assert grid._seg_rtree_pending[layer_idx] == {}
        assert _raw_ids(grid, layer_idx, (0.0, 0.0, 50.0, 50.0)) == [id(seg)]

    def test_mutated_segment_reinsert_drops_the_stale_entry(self, grid):
        """Issue #3507: in-place geometry mutation must not leave a ghost."""
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        seg = _seg(1.0, 1.0, 5.0, 1.0)
        grid._rtree_insert_segment(seg, layer_idx)
        grid._rtree_remove_segment(seg, layer_idx)

        seg.y1 = 30.0
        seg.y2 = 30.0
        grid._rtree_insert_segment(seg, layer_idx)

        assert _raw_ids(grid, layer_idx, (0.0, 0.5, 50.0, 1.5)) == []
        assert _raw_ids(grid, layer_idx, (0.0, 29.5, 50.0, 30.5)) == [id(seg)]
        assert grid._seg_rtree_count == 1

    def test_index_rebuild_clears_pending_state(self, grid):
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        keep = _seg(1.0, 1.0, 5.0, 1.0, net=1)
        drop = _seg(1.0, 9.0, 5.0, 9.0, net=2)
        grid.mark_route(_route([keep], net=1))
        grid.mark_route(_route([drop], net=2))
        grid._rtree_remove_segment(drop, layer_idx)
        assert grid._seg_rtree_pending[layer_idx]

        grid._rebuild_segment_index()
        assert all(not pending for pending in grid._seg_rtree_pending.values())
        # Rebuild re-derives from ``self.routes``: both routes are still marked.
        assert grid._seg_rtree_count == 2

    def test_invalidate_spatial_index_clears_pending_state(self, grid):
        """Issue #2335's re-inflation path must not inherit stale tombstones.

        ``invalidate_spatial_index`` rebuilds every layer with a new
        clearance inflation.  A tombstone carried across that rebuild would
        reference an envelope computed under the *old* inflation, so the
        pending map has to be dropped with the indices themselves.
        """
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        keep = _seg(1.0, 1.0, 5.0, 1.0, net=1)
        drop = _seg(1.0, 9.0, 5.0, 9.0, net=2)
        grid.mark_route(_route([keep], net=1))
        grid.mark_route(_route([drop], net=2))
        grid._rtree_remove_segment(drop, layer_idx)
        assert grid._seg_rtree_pending[layer_idx]

        grid.rules.trace_clearance = 0.3
        grid.invalidate_spatial_index()

        assert all(not pending for pending in grid._seg_rtree_pending.values())
        # Only the still-live segment is re-indexed, at the new inflation.
        assert grid._seg_rtree_count == 1
        assert list(grid._seg_rtree_items[layer_idx]) == [id(keep)]
        assert grid._seg_rtree_envelopes[layer_idx][id(keep)] == grid._segment_envelope(
            keep, grid._rtree_clearance_inflation
        )

    def test_removal_without_a_ledger_envelope_falls_back_to_eager_delete(self, grid):
        """Defensive path: no recorded envelope ⇒ delete eagerly, don't tombstone.

        An entry whose insert-time envelope is missing cannot be tombstoned
        safely (compaction would have to guess an envelope for it), so
        ``_rtree_remove_segment`` must fall back to the pre-#5240 eager
        ``rtree.Index.delete`` rather than leaving the entry in the tree.
        """
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        seg = _seg(1.0, 1.0, 5.0, 1.0)
        grid._rtree_insert_segment(seg, layer_idx)
        # Simulate an entry indexed out of band / before the ledger existed.
        del grid._seg_rtree_envelopes[layer_idx][id(seg)]

        grid._rtree_remove_segment(seg, layer_idx)

        assert grid._seg_rtree_count == 0
        assert grid._seg_rtree_pending[layer_idx] == {}
        assert _raw_ids(grid, layer_idx, (0.0, 0.0, 50.0, 50.0)) == []
