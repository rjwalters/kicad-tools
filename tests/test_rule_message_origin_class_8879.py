"""Rules report sheet-absolute coordinates on a non-zero-origin board (Issue #8879).

Class-level guard for the board-relative vs sheet-absolute bug (#8868, #8871,
#8879): every coordinate a rule reports -- quoted in ``message`` / ``items``
**and** carried in :attr:`DRCViolation.location` -- must equal the sheet
position of the offending object, not the board-relative one.

``location`` is checked through the public ``DRCChecker.check_*`` methods rather
than against a bare ``rule.check()``, because the frame contract is split
between the two: a rule emits ``location`` board-relative *by design* and
:meth:`DRCChecker._absolutize` adds ``board_origin`` back exactly once. Asserting
the sheet position on a bare rule would therefore be inverted -- it would pass
while a rule double-offsets and fail once that rule is fixed. Issue #8912 was
exactly such a double offset, missed because this guard only read message text.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_tools.router.current_paths import CurrentPathSpec, PathEndpoint
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.checker import DRCChecker

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


# A ``reinforcement_eligible`` declaration on the fixture's net, so the
# ``reinforcement_incomplete`` rule is active (it is a no-op without a
# current-paths sidecar). Its endpoints name components the fixture does not
# have, so the path is ``unresolved`` and the rule emits one ``[skipped]``.
_SPEC = CurrentPathSpec(
    name="FORCE",
    net_name="SIG",
    source=PathEndpoint("J1", "1"),
    sink=PathEndpoint("J2", "1"),
    continuous_a=10.0,
    reinforcement_eligible=True,
)

# ``(check_* method, DRCChecker kwargs, quotes coordinates in its text)``.
_CASES = [
    pytest.param("check_via_type_capability", {}, True, id="via_type_capability"),
    pytest.param("check_zero_length_segments", {}, True, id="zero_length_segment"),
    pytest.param(
        "check_reinforcement_incomplete",
        {"current_path_specs": [_SPEC]},
        False,  # the [skipped] message names a reason, not a position
        id="reinforcement_incomplete",
    ),
]


def _quoted(violations) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for v in violations:
        for text in (v.message, *v.items):
            out += [(float(a), float(b)) for a, b in _COORD.findall(text)]
    return out


@pytest.mark.parametrize(("method", "kwargs", "quotes_coords"), _CASES)
def test_reported_coordinates_are_sheet_absolute(
    tmp_path: Path, method: str, kwargs: dict, quotes_coords: bool
) -> None:
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_BOARD)
    pcb = PCB.load(str(path))
    assert tuple(pcb.board_origin) == pytest.approx(_ORIGIN)
    checker = DRCChecker(pcb, layers=4, **kwargs)
    violations = list(getattr(checker, method)().violations)
    assert violations, "fixture must trigger the rule"

    # ``location`` -- the field the KiCad GUI and ``kct check`` navigate to.
    for v in violations:
        assert v.location is not None, f"{v.rule_id} reported no location"
        assert v.location == pytest.approx(_SHEET, abs=0.01), (
            f"{v.rule_id} location {v.location} is not the sheet position "
            f"{_SHEET} (board_origin={tuple(pcb.board_origin)})"
        )

    # ``message`` / ``items`` text.
    quoted = _quoted(violations)
    if quotes_coords:
        assert quoted, "rule quoted no coordinates; fixture/regex out of date"
    for q in quoted:
        assert q == pytest.approx(_SHEET, abs=0.01)
