"""Equivalence tests for the pour-repair obstacle pre-filter (issue #5240).

``boards/06-diffpair-test/generate_design.py``'s ``_repair_pour_connectivity``
validates every candidate repair via and stub against the board's whole
segment obstacle list.  Issue #5240 replaced that exhaustive per-candidate
scan with an ``STRtree`` bounding-box pre-filter
(``_AppendOnlyBboxIndex``) -- a pure *narrowing* step: the caller still runs
the identical exact ``distance(...) < CLEAR`` predicate on everything the
index hands back.

That is only safe if the index is a genuine SUPERSET of the entries within
``margin``.  The obstacle list also GROWS while the repair runs (each placed
via / stub is registered immediately), while an ``STRtree`` is immutable, so
the superset property must survive appends and the internal rebuild too.
These tests pin both properties.
"""

from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "06-diffpair-test"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - import plumbing
        raise ImportError(f"Cannot load module {name!r} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bbox_index_cls():
    """``_AppendOnlyBboxIndex`` from the board-06 recipe."""
    gp = _load_module("board_06_generate_pcb_spatial", BOARD_DIR / "generate_pcb.py")
    sys.modules["generate_pcb"] = gp
    gs = _load_module("board_06_generate_schematic_spatial", BOARD_DIR / "generate_schematic.py")
    sys.modules["generate_schematic"] = gs
    mod = _load_module("board_06_generate_design_spatial", BOARD_DIR / "generate_design.py")
    return mod._AppendOnlyBboxIndex


def _random_segment(rng: random.Random):
    """A buffered random segment shaped like the recipe's ``seg_index`` entry."""
    from shapely.geometry import LineString

    x0 = rng.uniform(0.0, 40.0)
    y0 = rng.uniform(0.0, 40.0)
    x1 = x0 + rng.uniform(-6.0, 6.0)
    y1 = y0 + rng.uniform(-6.0, 6.0)
    if (x0, y0) == (x1, y1):
        x1 += 0.01
    width = rng.choice([0.15, 0.2, 0.25, 0.3])
    return (
        LineString([(x0, y0), (x1, y1)]).buffer(width / 2.0),
        rng.choice(["GND", "VBUS", "SIG"]),
        rng.choice(["F.Cu", "B.Cu", "In1.Cu"]),
    )


def _exhaustive(entries, probe, margin):
    """The pre-#5240 predicate: every entry genuinely within ``margin``."""
    return {i for i, entry in enumerate(entries) if probe.distance(entry[0]) < margin}


def test_candidates_are_a_superset_of_the_exhaustive_scan(bbox_index_cls):
    """Randomized: pruning never drops an entry the exact predicate accepts."""
    from shapely.geometry import Point

    rng = random.Random(20260919)
    margin = 0.15
    for _ in range(200):
        entries = [_random_segment(rng) for _ in range(rng.randint(0, 60))]
        index = bbox_index_cls(entries)
        for _ in range(10):
            probe = Point(rng.uniform(-2.0, 42.0), rng.uniform(-2.0, 42.0)).buffer(0.225)
            hits = set(index.candidates(probe.bounds, margin))
            assert _exhaustive(entries, probe, margin) <= hits


def test_superset_survives_appends_and_internal_rebuilds(bbox_index_cls):
    """Entries appended after construction are still returned (and rebuilt in).

    ``_emit_seg`` keeps appending to the same list object while the repair
    runs, so the index must cover the un-indexed tail; once the tail passes
    ``_TAIL_LIMIT`` it must fold into a rebuilt tree without losing anything.
    """
    from shapely.geometry import Point

    rng = random.Random(5240)
    margin = 0.15
    entries = [_random_segment(rng) for _ in range(25)]
    index = bbox_index_cls(entries)
    appended = 0
    # Well past _TAIL_LIMIT so at least one rebuild is exercised.
    while appended < bbox_index_cls._TAIL_LIMIT * 3:
        entries.append(_random_segment(rng))
        appended += 1
        probe = Point(rng.uniform(-2.0, 42.0), rng.uniform(-2.0, 42.0)).buffer(0.225)
        hits = set(index.candidates(probe.bounds, margin))
        assert _exhaustive(entries, probe, margin) <= hits
        assert all(0 <= i < len(entries) for i in hits)
    assert index._indexed > 25, "expected at least one rebuild to fold in the tail"


def test_empty_list_is_handled(bbox_index_cls):
    """An obstacle list can legitimately start empty (no segments parsed)."""
    from shapely.geometry import Point

    entries: list = []
    index = bbox_index_cls(entries)
    assert index.candidates(Point(1.0, 1.0).buffer(0.225).bounds, 0.15) == []
    entries.append(_random_segment(random.Random(1)))
    assert index.candidates(Point(1.0, 1.0).buffer(0.225).bounds, 0.15) == [0]


def test_prefilter_actually_prunes(bbox_index_cls):
    """Sanity: on a realistically sized list the candidate set is tiny.

    Without this the superset assertions above would also pass for an index
    that returned every entry -- i.e. for no optimization at all.
    """
    from shapely.geometry import Point

    rng = random.Random(99)
    entries = [_random_segment(rng) for _ in range(2000)]
    index = bbox_index_cls(entries)
    probe = Point(20.0, 20.0).buffer(0.225)
    assert len(index.candidates(probe.bounds, 0.15)) < len(entries) // 10
