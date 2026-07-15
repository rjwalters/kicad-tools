"""Tests for the off-board placement preflight in ``kct route`` (issue #4156).

A footprint placed outside the Edge.Cuts outline can never route (its nets
fail outright), and the resulting low completion percentage is
indistinguishable from congestion.  ``kct route`` therefore aborts (exit 2)
before any router work when the placement is off-board, with ``--allow-offboard``
as the explicit escape hatch.

Boards are built fully synthetically as S-expression strings that draw the
Edge.Cuts outline as a graphics ``gr_rect`` (as ``kct create-pcb`` and KiCad
itself do) -- NOT as copper ``segment`` elements.  This mirrors the
``--sync-check`` advisory-banner tests in ``test_netlist_sync_gate.py`` and
exercises the real ``get_board_outline`` path the original bug hid behind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import main as route_main


def _board(footprint_at: tuple[float, float]) -> str:
    """Synthetic board: graphics ``gr_rect`` outline + one R_0402 footprint.

    Outline is (100,100)-(120,110) in sheet coordinates, i.e. board-relative
    (0,0)-(20,10) once the origin is detected.  ``footprint_at`` is the
    sheet-absolute ``(x, y)`` of the single footprint.
    """
    fx, fy = footprint_at
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "NET1")
  (gr_rect (start 100 100) (end 120 110)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at {fx} {fy})
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
  )
)
"""


@pytest.fixture
def offboard_pcb(tmp_path: Path) -> Path:
    """Board whose single footprint is shifted +20mm off the north edge."""
    pcb = tmp_path / "offboard.kicad_pcb"
    pcb.write_text(_board((110, 130)))
    return pcb


@pytest.fixture
def inside_pcb(tmp_path: Path) -> Path:
    """Board whose single footprint sits fully inside the outline."""
    pcb = tmp_path / "inside.kicad_pcb"
    pcb.write_text(_board((110, 105)))
    return pcb


class TestRouteOffboardGate:
    def test_offboard_aborts_before_routing(self, offboard_pcb: Path, capsys):
        """Off-board placement aborts with exit 2 and a descriptive message."""
        rc = route_main([str(offboard_pcb), "--dry-run"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "outside Edge.Cuts" in err
        assert "placement invalid" in err
        # Names the count and points at the diagnostic command.
        assert "1 footprint" in err
        assert "kct placement check" in err

    def test_allow_offboard_bypasses_gate(self, offboard_pcb: Path, capsys):
        """--allow-offboard proceeds past the gate (dry-run succeeds)."""
        rc = route_main([str(offboard_pcb), "--dry-run", "--allow-offboard"])
        # The preflight is skipped; the dry run itself returns 0.
        assert rc == 0
        assert "placement invalid" not in capsys.readouterr().err

    def test_inside_board_is_unaffected(self, inside_pcb: Path, capsys):
        """A fully-inside board never triggers the preflight."""
        rc = route_main([str(inside_pcb), "--dry-run"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "placement invalid" not in captured.err
        assert "outside Edge.Cuts" not in captured.err


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
