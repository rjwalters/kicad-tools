"""Exact-equivalence controls for the iterative SExp traversal (#5240).

``SExp.find`` / ``SExp.find_all`` / ``SExp.iter_all`` were rewritten from a
recursive-generator walk (one Python generator frame allocated *per node per
tree level* -- ``yield self; for child: yield from child.iter_all()``) to an
explicit-stack pre-order DFS. Profiling a 5x reload of the committed routed
board 03 fixture (``scripts/research/bench_sexp_traversal.py``) attributes
roughly a sixth of ``PCB.load()`` wall time to these three methods combined
(``find_all`` 1.33s, ``find`` 0.86s, ``iter_all`` 0.52s tottime of 19.76s
total across 5 loads) -- unsurprising given 287 call sites across
``schema/``, ``drc/``, ``zones/``, ``router/`` and ``lvs/``.

This is a pure work-reduction refactor, not an approximation: the emitted
node sequence, first-match and full-match sets are defined by the same
pre-order walk either way. These tests pin that claim down against the
pre-change recursive implementation (reproduced verbatim below as
``_ReferenceSExp``) rather than assuming it, on both real committed board
files and randomly generated synthetic trees (mixed branching factor, depth,
duplicate names, and attribute queries).
"""

from __future__ import annotations

import random
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from kicad_tools.sexp.parser import SExp, parse_file

REPO_ROOT = Path(__file__).resolve().parents[1]

_REAL_FIXTURES = (
    "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb",
    "boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb",
    "boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb",
    "boards/03-usb-joystick/output/usb_joystick.kicad_sch",
)


# ---------------------------------------------------------------------------
# Reference implementation: the pre-#5240 recursive-generator walk, verbatim
# (operating on the *same* SExp instances -- only the traversal strategy is
# under test, not node identity or construction).
# ---------------------------------------------------------------------------


class _ReferenceSExp:
    """Namespace holding the pre-change recursive traversal as free functions."""

    @staticmethod
    def iter_all(node: SExp):
        yield node
        for child in node.children:
            yield from _ReferenceSExp.iter_all(child)

    @staticmethod
    def find(node: SExp, name: str, **attrs: Any) -> SExp | None:
        for child in node.children:
            for candidate in _ReferenceSExp.iter_all(child):
                if candidate.name == name:
                    if all(node._match_attr(candidate, k, v) for k, v in attrs.items()):
                        return candidate
        return None

    @staticmethod
    def find_all(node: SExp, name: str, **attrs: Any) -> list[SExp]:
        results: list[SExp] = []
        for child in node.children:
            for candidate in _ReferenceSExp.iter_all(child):
                if candidate.name == name:
                    if all(node._match_attr(candidate, k, v) for k, v in attrs.items()):
                        results.append(candidate)
        return results


# ---------------------------------------------------------------------------
# Synthetic corpus
# ---------------------------------------------------------------------------

_NAMES = ("pad", "segment", "via", "zone", "footprint", "net", "at", "layer", "width")
_ATTR_NAMES = ("net", "layer")
_ATTR_VALUES = ("F.Cu", "B.Cu", 0, 1, 2, "GND")


def _random_tree(rng: random.Random, depth: int, max_children: int) -> SExp:
    name = rng.choice(_NAMES)
    node = SExp(name=name)
    if depth <= 0:
        return node
    n = rng.randint(0, max_children)
    for _ in range(n):
        if rng.random() < 0.3:
            node.append(SExp(value=rng.choice((1, 2.5, "leaf", "F.Cu"))))
        else:
            node.append(_random_tree(rng, depth - 1, max_children))
    return node


@lru_cache(maxsize=1)
def _synthetic_trees() -> tuple[SExp, ...]:
    rng = random.Random(5240)
    return tuple(_random_tree(rng, depth=rng.randint(2, 6), max_children=5) for _ in range(30))


@lru_cache(maxsize=1)
def _real_trees() -> tuple[SExp, ...]:
    # Parsed once per test-process and reused (not mutated) by every test
    # below -- the fixtures are large enough (up to 120k nodes) that
    # re-parsing per test would itself dominate this file's runtime.
    trees = []
    for rel in _REAL_FIXTURES:
        path = REPO_ROOT / rel
        if path.exists():
            trees.append(parse_file(path))
    return tuple(trees)


@lru_cache(maxsize=1)
def _corpus() -> tuple[SExp, ...]:
    return _real_trees() + _synthetic_trees()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_corpus_non_vacuous():
    corpus = _corpus()
    assert len(corpus) >= 30
    # At least the real fixtures must be present so this isn't silently
    # degraded to synthetic-only coverage.
    assert len(_real_trees()) == len(_REAL_FIXTURES), (
        "expected all committed board fixtures to exist and parse"
    )


def test_iter_all_matches_reference():
    for tree in _corpus():
        expected = list(_ReferenceSExp.iter_all(tree))
        actual = list(tree.iter_all())
        # SExp defines no __eq__, so this is an identity comparison: the
        # explicit-stack walk must yield the exact same node objects in the
        # exact same order as the recursive-generator reference.
        assert [id(n) for n in actual] == [id(n) for n in expected]


def _names_for(tree: SExp, rng: random.Random, cap: int) -> list[str]:
    """All names for small (synthetic) trees; a bounded random sample for
    large real board trees (whose reference recursive-generator walk is
    O(nodes) *per name* -- exhaustive coverage of ~130 names against a
    120k-node board would dominate this test file's runtime, which is
    self-defeating for a CI-runtime issue)."""
    names = sorted({node.name for node in tree.iter_all() if node.name is not None})
    if len(names) <= cap:
        return names
    return rng.sample(names, cap)


def test_find_matches_reference_for_sampled_names():
    rng = random.Random(5242)
    for tree in _corpus():
        for name in _names_for(tree, rng, cap=8):
            expected = _ReferenceSExp.find(tree, name)
            actual = tree.find(name)
            assert (expected is None) == (actual is None)
            if expected is not None:
                assert actual is expected  # same node object, not just an equal one


def test_find_all_matches_reference_for_sampled_names():
    rng = random.Random(5243)
    for tree in _corpus():
        for name in _names_for(tree, rng, cap=8):
            expected = _ReferenceSExp.find_all(tree, name)
            actual = tree.find_all(name)
            assert [id(n) for n in actual] == [id(n) for n in expected]


def test_find_and_find_all_with_attrs_match_reference():
    rng = random.Random(5241)
    # Bounded draws *per tree* (not exhaustive over names): each draw runs
    # the O(nodes) reference find_all twice, so this is deliberately capped
    # to keep the file's total runtime from scaling with corpus size.
    draws_per_tree = 3
    for tree in _corpus():
        names = [node.name for node in tree.iter_all() if node.name is not None]
        if not names:
            continue
        for _ in range(draws_per_tree):
            name = rng.choice(names)
            attr = rng.choice(_ATTR_NAMES)
            value = rng.choice(_ATTR_VALUES)
            expected_one = _ReferenceSExp.find(tree, name, **{attr: value})
            actual_one = tree.find(name, **{attr: value})
            assert (expected_one is None) == (actual_one is None)
            if expected_one is not None:
                assert actual_one is expected_one

            expected_all = _ReferenceSExp.find_all(tree, name, **{attr: value})
            actual_all = tree.find_all(name, **{attr: value})
            assert [id(n) for n in actual_all] == [id(n) for n in expected_all]


def test_find_returns_none_and_find_all_returns_empty_for_absent_name():
    tree = SExp(name="root", children=[SExp(name="leaf", value=None)])
    assert tree.find("does_not_exist") is None
    assert tree.find_all("does_not_exist") == []


def test_iter_all_on_leaf_yields_only_self():
    leaf = SExp(value="atom")
    assert list(leaf.iter_all()) == [leaf]


@pytest.mark.parametrize("rel", _REAL_FIXTURES)
def test_find_all_on_real_board_is_non_empty_for_common_tags(rel: str):
    path = REPO_ROOT / rel
    if not path.exists():
        pytest.skip(f"fixture missing: {rel}")
    tree = parse_file(path)
    # Every committed board/schematic file has at least a version tag.
    assert tree.find("version") is not None
