"""Equivalence tests for ``NetStatusAnalyzer._build_segment_components`` (#5240).

``_build_segment_components(strict=True)`` used to decide segment adjacency
with a naive nested loop calling ``gi.intersects(gj)`` for every same-layer
segment pair -- O(n^2) GEOS calls per net. Profiling
``scripts/ci/check_diffpair_coverage.py`` (board 06's re-route + coverage
gate, the CI job named "Diff-Pair Routing Regression") showed this is the
dominant Python-side cost of the post-route connectivity/pour audit:
``_geoms_touch`` was called ~7.2M times across the gate's two ``analyze()``
passes, ~35s cumulative.

The fix (mirroring #5488's identical pattern for
``boards/06-diffpair-test/generate_design.py:_audit_pour_nets``) replaces the
nested loop with a per-layer ``shapely.strtree.STRtree`` bulk self-join using
the SAME ``"intersects"`` predicate, just spatially pruned via the tree's
bounding-box index first. This module pins byte-for-byte equivalence between
the original nested-loop algorithm (reimplemented here as a reference) and
the optimized method on randomized synthetic segment layouts, plus a direct
before/after timing comparison on a larger synthetic net.
"""

from __future__ import annotations

import math
import random
import time
from pathlib import Path

import pytest

from kicad_tools.analysis.net_status import NetStatusAnalyzer
from kicad_tools.schema.pcb import Segment

shapely = pytest.importorskip("shapely")

MINIMAL_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "SIG")
)
"""


def _analyzer(tmp_path: Path) -> NetStatusAnalyzer:
    """A strict-mode analyzer backed by an (otherwise empty) synthetic board.

    ``_build_segment_components`` only needs ``self.strict`` and the segment
    polygon cache, so an empty board is a valid host for exercising it
    directly against synthetic segment lists.
    """
    path = tmp_path / "board.kicad_pcb"
    path.write_text(MINIMAL_PCB)
    return NetStatusAnalyzer(path, strict=True)


def _reference_build_segment_components(
    analyzer: NetStatusAnalyzer, segments: list[Segment]
) -> list[set[int]]:
    """Reimplementation of the pre-#5240 O(n^2) nested-loop algorithm.

    Kept independent of the production method (not merely calling it) so a
    regression in the optimized path cannot silently make the reference
    match itself.
    """
    if not segments:
        return []

    polys = [analyzer._segment_poly(seg) for seg in segments]

    segment_graph: dict[int, set[int]] = {i: set() for i in range(len(segments))}
    for i, seg_a in enumerate(segments):
        for j, seg_b in enumerate(segments):
            if i != j:
                connected = seg_a.layer == seg_b.layer and analyzer._geoms_touch(polys[i], polys[j])
                if connected:
                    segment_graph[i].add(j)
                    segment_graph[j].add(i)

    visited: set[int] = set()
    components: list[set[int]] = []
    for i in range(len(segments)):
        if i in visited:
            continue
        component: set[int] = set()
        queue = [i]
        while queue:
            seg_idx = queue.pop()
            if seg_idx in visited:
                continue
            visited.add(seg_idx)
            component.add(seg_idx)
            queue.extend(segment_graph[seg_idx] - visited)
        components.append(component)
    return components


def _random_segment(rng: random.Random, layers: list[str]) -> Segment:
    x0 = rng.uniform(-5.0, 5.0)
    y0 = rng.uniform(-5.0, 5.0)
    angle = rng.uniform(0.0, 2 * math.pi)
    length = rng.uniform(0.05, 3.0)
    x1 = x0 + length * math.cos(angle)
    y1 = y0 + length * math.sin(angle)
    width = rng.choice([0.15, 0.2, 0.25, 0.5])
    layer = rng.choice(layers)
    return Segment(
        start=(round(x0, 4), round(y0, 4)),
        end=(round(x1, 4), round(y1, 4)),
        width=width,
        layer=layer,
        net_number=1,
        net_name="SIG",
    )


@pytest.mark.parametrize("trial", range(60))
def test_build_segment_components_matches_reference(tmp_path: Path, trial: int) -> None:
    """Randomized equivalence: optimized method == independent brute-force."""
    rng = random.Random(1000 + trial)
    layers = ["F.Cu", "B.Cu"] if trial % 2 == 0 else ["F.Cu", "In1.Cu", "B.Cu"]
    n = rng.randint(0, 45)
    segments = [_random_segment(rng, layers) for _ in range(n)]

    analyzer = _analyzer(tmp_path)
    reference = _reference_build_segment_components(analyzer, segments)

    # Fresh analyzer (and thus a fresh polygon cache) for the optimized call
    # so neither run can piggyback on the other's cached geometry.
    analyzer2 = _analyzer(tmp_path)
    optimized = analyzer2._build_segment_components(segments)

    ref_sets = {frozenset(c) for c in reference}
    opt_sets = {frozenset(c) for c in optimized}
    assert ref_sets == opt_sets, (
        f"trial {trial}: component partitions differ\n"
        f"reference={sorted(sorted(c) for c in ref_sets)}\n"
        f"optimized={sorted(sorted(c) for c in opt_sets)}"
    )
    # Every segment index appears in exactly one component either way.
    assert sorted(i for c in optimized for i in c) == list(range(n))


def test_build_segment_components_empty_and_singleton(tmp_path: Path) -> None:
    analyzer = _analyzer(tmp_path)
    assert analyzer._build_segment_components([]) == []

    seg = _random_segment(random.Random(1), ["F.Cu"])
    result = analyzer._build_segment_components([seg])
    assert result == [{0}]


def test_build_segment_components_degenerate_zero_length_segment(tmp_path: Path) -> None:
    """A zero-length segment must not crash the tree build (None-poly path)."""
    analyzer = _analyzer(tmp_path)
    degenerate = Segment(start=(1.0, 1.0), end=(1.0, 1.0), width=0.2, layer="F.Cu", net_number=1)
    normal = _random_segment(random.Random(2), ["F.Cu"])
    # Should not raise, regardless of whether the degenerate segment's
    # polygon comes back None or a valid (possibly tiny) shape.
    result = analyzer._build_segment_components([degenerate, normal])
    assert sorted(i for c in result for i in c) == [0, 1]


@pytest.mark.timeout(60)
def test_build_segment_components_faster_on_larger_net(tmp_path: Path) -> None:
    """Local before/after timing sanity check on a larger synthetic net.

    Not a strict CI-median claim (single shared-host measurement) -- just a
    guard that the spatial-pruning path does not regress to (or worse than)
    the O(n^2) reference on a segment count representative of board 06's
    denser nets (GND has ~205 segments on the committed regression fixture).
    """
    rng = random.Random(42)
    layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    segments = [_random_segment(rng, layers) for _ in range(200)]

    analyzer_ref = _analyzer(tmp_path)
    t0 = time.perf_counter()
    reference = _reference_build_segment_components(analyzer_ref, segments)
    reference_elapsed = time.perf_counter() - t0

    analyzer_opt = _analyzer(tmp_path)
    t0 = time.perf_counter()
    optimized = analyzer_opt._build_segment_components(segments)
    optimized_elapsed = time.perf_counter() - t0

    ref_sets = {frozenset(c) for c in reference}
    opt_sets = {frozenset(c) for c in optimized}
    assert ref_sets == opt_sets

    # The optimized path must not be slower than the naive reference on this
    # workload (random, mostly-disjoint segments spread across 4 layers).
    assert optimized_elapsed <= reference_elapsed, (
        f"expected optimized ({optimized_elapsed:.4f}s) <= reference ({reference_elapsed:.4f}s)"
    )
