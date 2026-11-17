"""``pairplan.geometry`` is a verbatim copy of the lattice primitives (#6837).

``router/pairplan`` keeps its own copies of the point/segment helpers so it
never imports the lattice engine.  Moved code computes with them, so any
drift -- even a reordered float expression -- could change routed copper.
Pinned two ways: identical function source (AST), and ``==`` results on
random and degenerate inputs.
"""

from __future__ import annotations

import ast
import inspect
import random

import pytest

from kicad_tools.router.lattice import geometry as lattice_geometry
from kicad_tools.router.pairplan import geometry as pairplan_geometry

FUNCTIONS = ("dist", "seg_pt_dist", "_orient", "segs_intersect", "seg_seg_dist")


@pytest.mark.parametrize("name", FUNCTIONS)
def test_function_source_is_identical(name: str) -> None:
    def dump(module) -> str:
        return ast.dump(ast.parse(inspect.getsource(getattr(module, name))))

    assert dump(pairplan_geometry) == dump(lattice_geometry)


def test_type_aliases_match() -> None:
    assert pairplan_geometry.Pt == lattice_geometry.Pt
    assert pairplan_geometry.Rect == lattice_geometry.Rect


def _points(rng: random.Random, n: int) -> list[tuple[float, float]]:
    pts = [(rng.uniform(-50, 50), rng.uniform(-50, 50)) for _ in range(n)]
    # Grid-snapped points produce collinear, touching and degenerate cases.
    pts += [(round(rng.uniform(-2, 2), 1), round(rng.uniform(-2, 2), 1)) for _ in range(n)]
    return pts


def test_results_are_identical_on_random_inputs() -> None:
    rng = random.Random(6837)
    pts = _points(rng, 200)
    for _ in range(5000):
        a, b, c, d = (rng.choice(pts) for _ in range(4))
        assert pairplan_geometry.dist(a, b) == lattice_geometry.dist(a, b)
        assert pairplan_geometry.seg_pt_dist(a, b, c) == lattice_geometry.seg_pt_dist(a, b, c)
        assert pairplan_geometry._orient(a, b, c) == lattice_geometry._orient(a, b, c)
        assert pairplan_geometry.segs_intersect(a, b, c, d) == lattice_geometry.segs_intersect(
            a, b, c, d
        )
        assert pairplan_geometry.seg_seg_dist(a, b, c, d) == lattice_geometry.seg_seg_dist(
            a, b, c, d
        )


def test_degenerate_segment_matches() -> None:
    p = (1.25, -3.5)
    q = (4.0, 0.75)
    assert pairplan_geometry.seg_pt_dist(p, p, q) == lattice_geometry.seg_pt_dist(p, p, q)
    assert pairplan_geometry.seg_seg_dist(p, p, q, q) == lattice_geometry.seg_seg_dist(p, p, q, q)
