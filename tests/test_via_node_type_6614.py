"""``via_node_type`` reads a raw ``(via ...)`` node's type keyword (Issue #6614)."""

from __future__ import annotations

import pytest

from kicad_tools.core.layers import kicad_read_via_layers, via_node_type
from kicad_tools.sexp import parse_string


@pytest.mark.parametrize(
    "text,expected",
    [
        ('(via (at 1 2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))', None),
        ('(via blind (at 1 2) (size 0.6) (drill 0.3) (layers "F.Cu" "In1.Cu"))', "blind"),
        ('(via buried (at 1 2) (layers "In1.Cu" "In2.Cu"))', "buried"),
        ('(via micro (at 1 2) (layers "F.Cu" "In1.Cu"))', "micro"),
    ],
)
def test_via_node_type(text, expected):
    assert via_node_type(parse_string(text)) == expected


def test_raw_keywordless_partial_node_reads_as_through():
    node = parse_string('(via (at 1 2) (layers "In1.Cu" "F.Cu") (net 1))')
    layers_node = node.find("layers")
    raw = tuple(v for v in layers_node.values if isinstance(v, str))
    assert kicad_read_via_layers(raw, via_node_type(node)) == (("F.Cu", "B.Cu"), True)
