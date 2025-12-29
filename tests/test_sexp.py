"""Tests for S-expression parser."""

from kicad_tools.core.sexp import SExp, parse_sexp
from kicad_tools.core.sexp_file import serialize_sexp


def test_parse_simple():
    """Parse a simple S-expression."""
    text = '(test "value")'
    sexp = parse_sexp(text)
    assert sexp.tag == "test"
    assert sexp.get_string(0) == "value"


def test_parse_nested():
    """Parse nested S-expressions."""
    text = '(outer (inner "value"))'
    sexp = parse_sexp(text)
    assert sexp.tag == "outer"
    inner = sexp.find("inner")
    assert inner is not None
    assert inner.get_string(0) == "value"


def test_parse_numbers():
    """Parse numeric values."""
    text = "(point 1.5 -2.3)"
    sexp = parse_sexp(text)
    assert sexp.tag == "point"
    assert sexp.get_float(0) == 1.5
    assert sexp.get_float(1) == -2.3


def test_find_all():
    """Find all matching children."""
    text = "(root (item 1) (item 2) (other 3))"
    sexp = parse_sexp(text)
    items = sexp.find_all("item")
    assert len(items) == 2


def test_serialize():
    """Serialize S-expression back to string."""
    text = '(test "value")'
    sexp = parse_sexp(text)
    result = serialize_sexp(sexp)
    assert "test" in result
    assert "value" in result
