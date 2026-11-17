"""DRC violation locations must be reported in sheet-absolute coordinates.

Regression tests for issue #4025.

``PCB.load()`` -> ``PCB._detect_board_origin()`` detects the board outline's
corner in sheet-absolute coordinates and subtracts it in place from every
footprint/segment/via/zone coordinate, giving the rest of kicad_tools a
consistent *board-relative* frame to reason in.  The pure-Python DRC rules
build ``DRCViolation.location`` from those already-shifted attributes, so
without a correction ``kct check`` reported violation coordinates offset by
``-board_origin`` from what a human sees in the KiCad GUI or what
``kicad-cli pcb drc`` reports (which are both sheet-absolute -- the literal
``(at ...)`` values in the ``.kicad_pcb`` file).

Because boards are no longer at a uniform origin (#4015 centers each board
independently), the offset is different per board -- any hardcoded frame
assumption is guaranteed wrong.  These tests assert that the reported
locations equal the sheet-absolute coordinates in the file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCChecker

# Two parallel F.Cu tracks on different nets, 0.15 mm apart (below the JLCPCB
# 2-layer 0.2 mm trace-to-trace floor), placed on a board whose Edge.Cuts rect
# starts at a deliberately non-trivial, non-zero origin (37, 41).  The tracks
# run from x=100 to x=110 at y=100 and y=100.15 in *sheet-absolute* file
# coordinates -- so the true clearance-violation midpoint is (105, 100.075).
#
# After ``PCB.load()`` the loader shifts these into the board-relative frame
# (subtracting board_origin = (37, 41)); a naive rule would report the
# violation at (105-37, 100.075-41) == (68, 59.075).  The fix must report the
# sheet-absolute (105, 100.075) instead.
_ORIGIN_X = 37.0
_ORIGIN_Y = 41.0
_EXPECTED_ABS_X = 105.0
_EXPECTED_ABS_Y = 100.075

_PCB_NONZERO_ORIGIN = f"""\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3V3")
  (gr_rect (start {_ORIGIN_X} {_ORIGIN_Y}) (end 180 160)
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts")
    (uuid "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1 "GND") (uuid "seg-gnd1"))
  (segment (start 100 100.15) (end 110 100.15) (width 0.25) (layer "F.Cu") (net 2 "+3V3") (uuid "seg-3v3"))
)
"""


def _write(tmp_path: Path, content: str) -> PCB:
    pcb_path = tmp_path / "board.kicad_pcb"
    pcb_path.write_text(content)
    return PCB.load(pcb_path)


def test_board_origin_is_nonzero(tmp_path: Path):
    """Sanity guard: the fixture actually exercises a non-zero origin."""
    pcb = _write(tmp_path, _PCB_NONZERO_ORIGIN)
    assert pcb.board_origin == pytest.approx((_ORIGIN_X, _ORIGIN_Y))


def test_clearance_violation_location_is_sheet_absolute(tmp_path: Path):
    """A clearance violation reports the sheet-absolute midpoint (issue #4025).

    Verifies the coordinate a user reads out of ``kct check`` lands on the
    defect in the KiCad GUI / matches ``kicad-cli pcb drc`` -- i.e. it equals
    the file's ``(at ...)`` frame, NOT the internal board-relative frame.
    """
    pcb = _write(tmp_path, _PCB_NONZERO_ORIGIN)

    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2, copper_oz=1.0)
    results = checker.check_clearances()

    clearance_violations = [v for v in results.violations if v.location is not None]
    assert clearance_violations, "expected at least one located clearance violation"

    v = clearance_violations[0]
    assert v.location is not None
    lx, ly = v.location

    # Sheet-absolute midpoint of the two tracks, matching the raw file coords.
    assert lx == pytest.approx(_EXPECTED_ABS_X, abs=0.01)
    assert ly == pytest.approx(_EXPECTED_ABS_Y, abs=0.01)

    # And explicitly NOT the board-relative value the bug used to report.
    assert lx != pytest.approx(_EXPECTED_ABS_X - _ORIGIN_X, abs=0.01)
    assert ly != pytest.approx(_EXPECTED_ABS_Y - _ORIGIN_Y, abs=0.01)


def test_check_all_clearance_location_is_sheet_absolute(tmp_path: Path):
    """The full ``check_all()`` aggregation preserves sheet-absolute coords.

    Guards the primary aggregation path (the one behind the Python API and,
    structurally, the ``kct check`` CLI dispatch loop) -- not just the single
    ``check_clearances()`` entry point.
    """
    pcb = _write(tmp_path, _PCB_NONZERO_ORIGIN)

    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2, copper_oz=1.0)
    results = checker.check_all()

    located = [
        v
        for v in results.violations
        if v.rule_id.startswith("clearance") and v.location is not None
    ]
    assert located, "expected a located clearance violation from check_all()"

    lx, ly = located[0].location  # type: ignore[misc]
    assert lx == pytest.approx(_EXPECTED_ABS_X, abs=0.01)
    assert ly == pytest.approx(_EXPECTED_ABS_Y, abs=0.01)


def test_pad_grid_location_not_double_shifted(tmp_path: Path):
    """``pad_grid`` locations are already sheet-absolute -- do not shift them.

    ``check_pad_grid_alignment`` sources its coordinates from
    ``router.io.load_pads_for_analysis``, which parses the raw ``.kicad_pcb``
    text directly and never passes through ``_detect_board_origin``.  Adding
    ``board_origin`` to those would double-shift them, so ``_absolutize`` must
    NOT be applied to that rule.  This regression asserts the pad_grid path is
    untouched: any reported pad_grid location stays within the board's
    sheet-absolute footprint bounds (well away from a double-shifted value,
    which would land at absolute + board_origin).
    """
    # A footprint whose single pad sits off the alignment grid, on the same
    # non-zero-origin board.  The pad is at sheet-absolute (100.037, 100.0).
    pcb_content = f"""\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (gr_rect (start {_ORIGIN_X} {_ORIGIN_Y}) (end 180 160)
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts")
    (uuid "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"))
  (footprint "test:R"
    (layer "F.Cu")
    (uuid "cccccccc-cccc-cccc-cccc-cccccccccccc")
    (at 100.037 100)
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
  )
)
"""
    pcb = _write(tmp_path, pcb_content)

    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2, copper_oz=1.0)
    results = checker.check_pad_grid_alignment(auto_derive_threshold=False)

    located = [v for v in results.violations if v.location is not None]
    for v in located:
        lx, ly = v.location  # type: ignore[misc]
        # A double-shifted pad_grid location would be pushed to
        # ~ (100.037 + 37, 100 + 41); assert it stays in the sheet-absolute
        # band around the real pad instead.
        assert lx < _ORIGIN_X + 100.0
        assert ly < _ORIGIN_Y + 100.0
