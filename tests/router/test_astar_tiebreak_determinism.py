"""Determinism tests for A* priority-queue tie-breaking (Issue #3144).

These tests pin down the secondary sort key applied when two A* nodes
share an identical ``f_score``.  Without an explicit tie-break, both the
Python ``CoupledNode`` / ``AStarNode`` dataclasses and the C++
``AStarNode`` struct fall through to insertion-order-dependent pop
order: ``heapq`` resolves equal-priority entries by Python structural
comparison on subsequent ``compare=True`` fields (none, prior to
#3144), and ``std::priority_queue`` resolves ties in implementation-
defined order.  Under CI load this manifests as run-to-run drift in the
explored A* path, which propagates downstream into different diff-pair
budget-classification outcomes and ultimately different DRC error
counts on board 06.

The fix adds a monotonic ``seq`` field assigned at push-time so older-
pushed nodes pop first on f-score ties.  These tests assert that
behaviour at the dataclass / struct level so a regression would be
caught fast (microseconds), without re-running the full board-06
re-route loop.
"""

from __future__ import annotations

import heapq
import itertools

import pytest

from kicad_tools.router.diffpair_routing import CoupledNode, CoupledState, GridPos
from kicad_tools.router.pathfinder import AStarNode


class TestCoupledNodeTiebreak:
    """``CoupledNode`` heap-ordering invariants (Issue #3144)."""

    @staticmethod
    def _make_state(p: tuple[int, int, int], n: tuple[int, int, int]) -> CoupledState:
        return CoupledState(GridPos(*p), GridPos(*n), (0, 0))

    def test_equal_f_score_lower_seq_wins(self) -> None:
        """Two nodes with identical ``f_score`` pop by ``seq`` order."""
        state_a = self._make_state((0, 0, 0), (1, 0, 0))
        state_b = self._make_state((2, 0, 0), (3, 0, 0))

        # Construct in REVERSE seq order; correct behaviour pops seq=0 first.
        node_high_seq = CoupledNode(1.0, 0.0, state_a, seq=10)
        node_low_seq = CoupledNode(1.0, 0.0, state_b, seq=0)

        heap: list[CoupledNode] = []
        heapq.heappush(heap, node_high_seq)
        heapq.heappush(heap, node_low_seq)

        popped = heapq.heappop(heap)
        assert popped.seq == 0
        assert popped.state.p_pos == GridPos(2, 0, 0)

    def test_pop_order_is_stable_across_runs(self) -> None:
        """A heap of equal-f_score nodes pops in monotonic-seq order.

        This is the load-bearing invariant for determinism: regardless
        of which order Python's allocator hands out memory or how
        ``CoupledState`` would hash, the pop order is fully determined
        by the ``seq`` field.  Repeat the test 10 times with the same
        construction to catch any latent state.
        """
        seq_counter = itertools.count()
        # Construct 50 nodes with identical f_score but different states.
        # Vary the state so the structural comparison fallback (which
        # raises ``TypeError`` for un-orderable ``CoupledState``) would
        # fire if ``seq`` were not the only secondary key.
        nodes: list[CoupledNode] = []
        for i in range(50):
            state = self._make_state((i, 0, 0), (i + 1, 0, 0))
            nodes.append(CoupledNode(2.5, float(i), state, seq=next(seq_counter)))

        for _ in range(10):
            heap: list[CoupledNode] = []
            for n in nodes:
                heapq.heappush(heap, n)
            popped_seqs = []
            while heap:
                popped_seqs.append(heapq.heappop(heap).seq)
            assert popped_seqs == sorted(popped_seqs)
            assert popped_seqs == list(range(50))

    def test_f_score_beats_seq(self) -> None:
        """``f_score`` is the primary key; ``seq`` only breaks ties."""
        state = self._make_state((0, 0, 0), (1, 0, 0))
        # Higher f_score but lower seq must STILL lose to lower f_score.
        node_high_f = CoupledNode(10.0, 0.0, state, seq=0)
        node_low_f = CoupledNode(1.0, 0.0, state, seq=999)

        heap: list[CoupledNode] = []
        heapq.heappush(heap, node_high_f)
        heapq.heappush(heap, node_low_f)

        assert heapq.heappop(heap).f_score == 1.0
        assert heapq.heappop(heap).f_score == 10.0

    def test_no_typeerror_on_equal_f_score(self) -> None:
        """Equal-f_score push must not raise ``TypeError``.

        Pre-#3144, comparing two equal-f_score ``CoupledNode`` instances
        fell through to the next ``compare=True`` field (none) without
        raising -- but if a future refactor accidentally made ``state``
        compared again, the dataclass would compare ``CoupledState``
        which has no ``__lt__``.  This test pins that the tie-break is
        well-defined.
        """
        state_a = self._make_state((0, 0, 0), (1, 0, 0))
        state_b = self._make_state((5, 5, 5), (6, 6, 6))
        node_a = CoupledNode(3.14, 0.0, state_a, seq=0)
        node_b = CoupledNode(3.14, 0.0, state_b, seq=1)

        heap: list[CoupledNode] = []
        # Two distinct-state nodes with identical f_score MUST be
        # comparable without errors -- this is what makes the heap
        # tractable in the coupled-A* hot loop.
        heapq.heappush(heap, node_a)
        heapq.heappush(heap, node_b)
        assert heapq.heappop(heap).seq == 0


class TestAStarNodeTiebreak:
    """``AStarNode`` heap-ordering invariants (Issue #3144)."""

    def test_equal_f_score_lower_seq_wins(self) -> None:
        """Two ``AStarNode``s with identical f_score pop by seq order."""
        node_high_seq = AStarNode(1.0, 0.0, 0, 0, 0, seq=10)
        node_low_seq = AStarNode(1.0, 0.0, 5, 5, 0, seq=0)

        heap: list[AStarNode] = []
        heapq.heappush(heap, node_high_seq)
        heapq.heappush(heap, node_low_seq)

        assert heapq.heappop(heap).seq == 0
        assert heapq.heappop(heap).seq == 10

    def test_stable_pop_order_across_pushes(self) -> None:
        """50 equal-f_score nodes pop in monotonic seq order."""
        seq_counter = itertools.count()
        nodes = [AStarNode(2.5, float(i), i, i + 1, 0, seq=next(seq_counter)) for i in range(50)]
        heap: list[AStarNode] = []
        for n in nodes:
            heapq.heappush(heap, n)

        popped_seqs = []
        while heap:
            popped_seqs.append(heapq.heappop(heap).seq)
        assert popped_seqs == list(range(50))


class TestCppAStarTiebreak:
    """C++ ``AStarNode::operator>`` tie-break invariants (Issue #3144).

    Only runs when the C++ extension is built; otherwise skipped.  Since
    the C++ tie-break is not exposed via Python bindings, we verify the
    fix indirectly by checking the ``BUILD_VERSION`` matches the post-
    #3144 expectation -- a stale ``.so`` without the tie-break would
    report the pre-#3144 version 5.
    """

    def test_cpp_build_version_includes_tiebreak_fix(self) -> None:
        """``router_cpp.BUILD_VERSION`` was bumped to 6 by Issue #3144."""
        try:
            from kicad_tools.router import router_cpp
        except ImportError:
            pytest.skip("C++ extension not built")

        assert router_cpp.BUILD_VERSION >= 6, (
            "C++ router .so predates the Issue #3144 A* tie-break fix.  "
            "Run `uv run kct build-native` to rebuild."
        )
