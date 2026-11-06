"""Issue #8492: optimize-traces must never invent ``Net<N>`` net names."""

import re

from kicad_tools.router.optimizer.pcb import parse_segments, parse_vias, replace_segments

_PCB = """(kicad_pcb (version 20260101)
  (segment (start 0 0) (end 5 0) (width 0.2) (layer "F.Cu") (net "GND") (uuid "a"))
  (segment (start 5 0) (end 10 0) (width 0.2) (layer "F.Cu") (net "GND") (uuid "b"))
  (segment (start 0 5) (end 5 5) (width 0.2) (layer "F.Cu") (net "+3.3V") (uuid "c"))
  (segment (start 0 8) (end 5 8) (width 0.2) (layer "F.Cu") (net 7) (uuid "d"))
  (via (at 1 1) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND") (uuid "v"))
  (via (at 2 2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 8) (uuid "w"))
)"""


def _net_refs(text):
    return sorted(re.findall(r"\(net [^)]*\)", text))


def test_name_only_roundtrip_preserves_net_refs():
    segs = parse_segments(_PCB)
    out = replace_segments(_PCB, segs, segs)
    assert _net_refs(out) == _net_refs(_PCB)
    assert "Net7" not in out and "Net8" not in out


def test_unresolvable_numeric_refs_are_not_parsed_as_invented_names():
    assert set(parse_segments(_PCB)) == {"GND", "+3.3V"}
    assert set(parse_vias(_PCB)) == {"GND"}


def test_numeric_dialect_with_header_table_still_resolves():
    text = (
        '(kicad_pcb (net 0 "") (net 7 "GND")\n'
        '  (segment (start 0 0) (end 5 0) (width 0.2) (layer "F.Cu") (net 7) (uuid "a"))\n)'
    )
    assert set(parse_segments(text)) == {"GND"}
