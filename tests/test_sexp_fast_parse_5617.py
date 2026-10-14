"""The single-pass parse path builds the same tree as the recursive parser (Issue #5617).

``Parser.parse`` first tries ``Parser._parse_fast`` -- one ``finditer`` pass
over ``_TOKEN_RE`` with an explicit stack -- and only falls back to the
recursive ``_parse_expr`` parser when the fast path declines the input.  These
tests pin that the two produce identical trees (every ``__slots__`` field,
including value types, ``_original_str`` / ``_originally_quoted`` /
``_originally_bare`` and the retained copper-arc source text), that the fast
path is actually taken on the board 06 routed fixture this optimisation is
for, that malformed input keeps raising the recursive parser's errors, and
that ``parse`` restores the garbage collector's enabled state.
"""

from __future__ import annotations

import gc
import random
from pathlib import Path

import pytest

from kicad_tools.sexp import parser as sexp_parser
from kicad_tools.sexp.parser import ParseError, Parser, SExp

REPO = Path(__file__).resolve().parent.parent
BOARD06_ROUTED = REPO / "boards/06-diffpair-test/regression-fixture/diffpair_test_routed.kicad_pcb"
FIXTURES = [
    BOARD06_ROUTED,
    REPO / "boards/06-diffpair-test/regression-fixture/diffpair_test.kicad_sch",
    REPO / "boards/06-diffpair-test/regression-fixture/diffpair_lvs.kicad_sym",
    REPO / "boards/06-diffpair-test/regression-fixture/diffpair_test_routed.kicad_dru",
    REPO / "tests/fixtures/copper_arc_routed.kicad_pcb",
    REPO / "tests/fixtures/multiunit_test.kicad_sym",
]


def _state(node: SExp) -> list:
    """Every slot of every node, depth first, with value types."""
    out: list = []
    stack = [node]
    while stack:
        cur = stack.pop()
        out.append(
            tuple(
                (slot, type(getattr(cur, slot)).__name__, getattr(cur, slot))
                for slot in SExp.__slots__
                if slot != "children"
            )
        )
        out.append(len(cur.children))
        stack.extend(reversed(cur.children))
    return out


class _RecursiveOnlyParser(Parser):
    """The pre-#5617 behaviour: the fast path always declines."""

    def _parse_fast(self) -> SExp | None:
        return None


def _outcome(text: str, *, recursive: bool) -> tuple:
    parser = _RecursiveOnlyParser(text) if recursive else Parser(text)
    try:
        return ("ok", _state(parser.parse()))
    except ParseError as exc:
        return ("error", str(exc))


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_fixture_trees_match_recursive_parser(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"fixture missing: {path}")
    text = path.read_text(encoding="utf-8")
    assert _outcome(text, recursive=False) == _outcome(text, recursive=True)


def test_fast_path_taken_on_board06_routed_fixture() -> None:
    text = BOARD06_ROUTED.read_text(encoding="utf-8")
    assert Parser(text)._parse_fast() is not None


EDGE_CASES = [
    "",
    "   ",
    "; only a comment",
    "(",
    ")",
    "()",
    "( )",
    "(a",
    "(a))",
    "(a) b",
    "(a) ; trailing comment\n  # another\n",
    "atom",
    '"root string"',
    "(a b c)",
    "(a\tb\r\nc)",
    "(a #c\n b)",
    "(a ;c\r\n b ; z)",
    "(a b#c d;e)",
    '(a b"c d)',
    '(a "x" ; trailing',
    '(a "x\\"y" b)',
    '(a "\\n\\t\\r\\q")',
    '(a "unterminated',
    '(a "esc\\',
    '(a "")',
    '("q" x)',
    '("" x)',
    "(() x)",
    "((a) b)",
    "(1 2)",
    "(-1 x)",
    "(- x)",
    "(-)",
    "(a 1e5 -2.5 0x1 1_0 -inf 1.2.3 \u0663 \u00b2)",
    "(a\f b\u00a0c)",
    "(arc (start 0 0) (mid 1 1) (end 2 0) (width 0.2) (layer F.Cu) (net 1))",
    '(kicad_pcb (arc  (width 0.2)\n ; c\n (layer "F.Cu")) (arc (width 1)))',
]


@pytest.mark.parametrize("text", EDGE_CASES)
def test_edge_cases_match_recursive_parser(text: str) -> None:
    assert _outcome(text, recursive=False) == _outcome(text, recursive=True)


def test_seeded_fuzz_matches_recursive_parser() -> None:
    alphabet = list(' \t\n\r()"\\#;abc-1.5e9xy\u00b2\u0663')
    rng = random.Random(5617)
    for _ in range(20000):
        body = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        for text in (body, f"({body})"):
            assert _outcome(text, recursive=False) == _outcome(text, recursive=True), repr(text)


def test_parse_restores_gc_state() -> None:
    assert gc.isenabled()
    Parser("(a b)").parse()
    assert gc.isenabled()
    with pytest.raises(ParseError):
        Parser("(a").parse()
    assert gc.isenabled()

    gc.disable()
    try:
        Parser("(a b)").parse()
        assert not gc.isenabled()
        with pytest.raises(ParseError):
            Parser("(a").parse()
        assert not gc.isenabled()
    finally:
        gc.enable()


def test_position_tracking_uses_recursive_parser() -> None:
    node = sexp_parser.parse_string("(a\n  (b 1))", track_positions=True)
    child = node.children[0]
    assert (child.line, child.column) == (2, 3)
