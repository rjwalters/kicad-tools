"""Rule messages quote sheet-absolute coordinates on a non-zero-origin board (Issue #8879).

Class-level guard for the board-relative vs sheet-absolute bug (#8868, #8871,
#8879): every coordinate a rule quotes in ``message`` / ``items`` must equal the
sheet position of the offending object, not the board-relative one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_tools.manufacturers import get_profile
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules import ViaTypeCapabilityRule
from kicad_tools.validate.rules.zero_length_segment import ZeroLengthSegmentRule

_ORIGIN = (90.0, 90.0)
_SHEET = (105.0, 100.0)  # sheet position of the via / zero-length segment

_BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal)
    (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "SIG")
  (gr_rect (start 90 90) (end 120 110) (stroke (width 0.1) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "e1"))
  (via blind (at 105 100) (size 0.6) (drill 0.3) (layers "F.Cu" "In1.Cu") (net 1) (uuid "v1"))
  (segment (start 105 100) (end 105 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "s1"))
)
"""

# ``(x, y)`` or ``@(x,y)`` with optional space after the comma.
_COORD = re.compile(r"\((-?\d+\.\d+),\s*(-?\d+\.\d+)\)")


def _quoted(violations) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for v in violations:
        for text in (v.message, *v.items):
            out += [(float(a), float(b)) for a, b in _COORD.findall(text)]
    return out


@pytest.mark.parametrize(
    "rule",
    [ViaTypeCapabilityRule(), ZeroLengthSegmentRule()],
    ids=["via_type_capability", "zero_length_segment"],
)
def test_quoted_coordinates_are_sheet_absolute(tmp_path: Path, rule) -> None:
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_BOARD)
    pcb = PCB.load(str(path))
    assert tuple(pcb.board_origin) == pytest.approx(_ORIGIN)
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    violations = list(rule.check(pcb, rules).violations)
    assert violations, "fixture must trigger the rule"
    quoted = _quoted(violations)
    assert quoted, "rule quoted no coordinates; fixture/regex out of date"
    for q in quoted:
        assert q == pytest.approx(_SHEET, abs=0.01)
