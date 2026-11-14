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

Each case names the fixture board it runs on. Quoted coordinates must be sheet
positions of that board's copper; ``location`` must be one too, or ``None`` for
a finding that deliberately has none (``path_ampacity``'s ambiguous-path error,
whose message is then its only coordinate surface -- Issue #8928).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_tools.router.current_paths import CurrentPathSpec, PathEndpoint
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.checker import DRCChecker
from tests.test_reinforcement_incomplete import _ambiguous_skipped_board
from tests.test_reinforcement_incomplete import _spec as _wire_spec

_ORIGIN = (90.0, 90.0)

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


def _file_board(tmp_path: Path) -> PCB:
    """The via / zero-length segment board above, loaded from disk."""
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_BOARD)
    pcb = PCB.load(str(path))
    assert tuple(pcb.board_origin) == pytest.approx(_ORIGIN)
    return pcb


def _ambiguous_board(tmp_path: Path) -> PCB:
    """Loop copper on a wire-reinforced net, board_origin (48.5, 55) (Issue #8928).

    The path resolves ``ambiguous`` with a loop witness and blocking branches,
    and its wire cannot be located, so ``reinforcement_incomplete`` emits a
    ``[skipped]`` quoting the witness and ``path_ampacity`` an ambiguous-path
    error quoting it too.
    """
    pcb = _ambiguous_skipped_board(center=True)
    assert tuple(pcb.board_origin) != (0.0, 0.0)
    return pcb


def _sheet_copper_points(pcb: PCB) -> list[tuple[float, float]]:
    """Every segment end, via and pad centre of ``pcb``, in sheet coordinates."""
    ox, oy = pcb.board_origin
    pts = [p for s in pcb.segments for p in (s.start, s.end)]
    pts += [v.position for v in pcb.vias]
    pts += [fp.position for fp in pcb.footprints]
    return [(x + ox, y + oy) for x, y in pts]


# ``(check_* method, DRCChecker kwargs, board fixture, message filter,
# quotes coordinates in its text, carries a location)``.
_CASES = [
    pytest.param(
        "check_via_type_capability", {}, _file_board, "", True, True, id="via_type_capability"
    ),
    pytest.param(
        "check_zero_length_segments", {}, _file_board, "", True, True, id="zero_length_segment"
    ),
    pytest.param(
        "check_reinforcement_incomplete",
        {"current_path_specs": [_SPEC]},
        _file_board,
        "",
        # This fixture's path is ``unresolved`` (stale endpoints), whose
        # reason carries no witness. An ``ambiguous`` [skipped] does quote
        # coordinates -- see the ``_ambiguous`` case below.
        False,
        True,
        id="reinforcement_incomplete_unresolved",
    ),
    pytest.param(
        "check_reinforcement_incomplete",
        {"current_path_specs": [_wire_spec()]},
        _ambiguous_board,
        "",
        True,  # the [skipped] message quotes the loop witness
        True,
        id="reinforcement_incomplete_ambiguous",
    ),
    pytest.param(
        "check_path_ampacity",
        {"current_path_specs": [_wire_spec()]},
        _ambiguous_board,
        "is ambiguous",
        True,  # the witness is this finding's only coordinate surface
        False,  # location=None by design (out of scope for #8928)
        id="path_ampacity_ambiguous",
    ),
]


def _quoted(violations) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for v in violations:
        for text in (v.message, *v.items):
            out += [(float(a), float(b)) for a, b in _COORD.findall(text)]
    return out


def _is_one_of(point, candidates) -> bool:
    return any(point == pytest.approx(c, abs=0.01) for c in candidates)


@pytest.mark.parametrize(
    ("method", "kwargs", "board", "select", "quotes_coords", "has_location"), _CASES
)
def test_reported_coordinates_are_sheet_absolute(
    tmp_path: Path,
    method: str,
    kwargs: dict,
    board,
    select: str,
    quotes_coords: bool,
    has_location: bool,
) -> None:
    pcb = board(tmp_path)
    sheet = _sheet_copper_points(pcb)
    checker = DRCChecker(pcb, layers=4, **kwargs)
    violations = [v for v in getattr(checker, method)().violations if select in v.message]
    assert violations, "fixture must trigger the rule"

    # ``location`` -- the field the KiCad GUI and ``kct check`` navigate to.
    for v in violations:
        if not has_location:
            assert v.location is None, f"{v.rule_id} grew a location; update this case"
            continue
        assert v.location is not None, f"{v.rule_id} reported no location"
        assert _is_one_of(v.location, sheet), (
            f"{v.rule_id} location {v.location} is not a sheet position of the "
            f"fixture's copper (board_origin={tuple(pcb.board_origin)})"
        )

    # ``message`` / ``items`` text.
    quoted = _quoted(violations)
    if quotes_coords:
        assert quoted, "rule quoted no coordinates; fixture/regex out of date"
    for q in quoted:
        assert _is_one_of(q, sheet), (
            f"quoted {q} is not a sheet position of the fixture's copper "
            f"(board_origin={tuple(pcb.board_origin)})"
        )
