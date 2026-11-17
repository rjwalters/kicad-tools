"""Issue #6873: the shared "find a token outside quoted strings" helper.

Behavioural regressions for the call sites live in
``tests/test_quoted_token_scan_6873.py``.
"""

from __future__ import annotations

import pytest

from kicad_tools.sexp import (
    find_unquoted,
    has_unquoted,
    iter_unquoted,
    parse_string,
    source_span,
)


def test_find_unquoted_skips_plain_and_escaped_strings():
    text = '(a "x (pad 1)" "q \\" (pad 2)" (pad 3) "open (pad 4)'

    assert text[find_unquoted(text, "(pad ") :].startswith("(pad 3)")
    # Past the last real one, the unterminated string swallows the rest.
    assert find_unquoted(text, "(pad ", text.index("(pad 3)") + 1) == -1
    assert find_unquoted(text, "(absent ") == -1


def test_iter_unquoted_and_has_unquoted():
    text = '(zone a) (descr "(zone b)") (zone\tc)'

    assert [m.start() for m in iter_unquoted(text, r"\(zone\s")] == [0, text.rindex("(zone")]
    assert has_unquoted(text, r"\(zone\s")
    assert not has_unquoted('(descr "(zone b)")', r"\(zone\s")


@pytest.mark.parametrize(
    "prefix",
    [pytest.param("", id="fast-parser"), pytest.param('(descr "a \\" b") ', id="recursive-parser")],
)
def test_source_span_marks_exactly_the_root_children(prefix):
    text = f'(kicad_pcb {prefix}(footprint "A" (descr "(footprint B)") (at 1 2))\n  (module "C"))'

    root = parse_string(text)
    spans = [source_span(child) for child in root.children if child.children]

    assert [text[s:e] for s, e in spans if s is not None][-2:] == [
        '(footprint "A" (descr "(footprint B)") (at 1 2))',
        '(module "C")',
    ]
    assert source_span(root) is None
    assert source_span(root.children[-2].children[-1]) is None  # nested (at ...)
