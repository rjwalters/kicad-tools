"""Tests for the interior-marooned edge-connector placement check (issue #4450).

``PlacementAnalyzer`` (the ``kct placement check`` path) flags off-board
connectors — USB, barrel jack, RJ45, card-edge, cable headers — whose courtyard
is fully inside the board outline but stands off from the nearest edge by more
than ``DesignRules.edge_connector_max_inset`` mm.  Such a connector cannot be
reached by a cable: the motivating case is board-03's USB-C ``J1``, placed 8 mm
inside the north edge with its mouth facing the board interior.

The board outline is drawn as a graphics ``gr_rect`` only (no copper Edge.Cuts
segments), matching what ``kct create-pcb`` and KiCad itself emit, and the
detected board origin at (100, 100) puts the outline at board-relative
(0, 0)-(80, 60).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.placement import PlacementAnalyzer
from kicad_tools.placement.analyzer import DesignRules
from kicad_tools.placement.conflict import ConflictType

# Board outline: sheet (100,100)-(180,160) -> board-relative (0,0)-(80,60).
_OUTLINE = """  (gr_rect (start 100 100) (end 180 160)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
"""


def _connector_fp(
    ref: str,
    x: float,
    y: float,
    uuid_n: int,
    fp_name: str = "Connector_USB:USB_C_Receptacle_GCT_USB4105",
) -> str:
    """A small USB-C-like connector footprint (~9mm-wide pad span) at sheet (x, y)."""
    return f"""  (footprint "{fp_name}"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000{uuid_n:02d}")
    (at {x} {y})
    (property "Reference" "{ref}" (at 0 -3 0) (layer "F.SilkS"))
    (property "Value" "USB-C" (at 0 3 0) (layer "F.Fab"))
    (pad "A1" smd rect (at -4 0) (size 1.0 2.0) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VBUS"))
    (pad "A12" smd rect (at 4 0) (size 1.0 2.0) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
"""


def _resistor_fp(ref: str, x: float, y: float, uuid_n: int) -> str:
    """A small SMD resistor (non-connector) at sheet (x, y)."""
    return f"""  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000{uuid_n:02d}")
    (at {x} {y})
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VBUS"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
"""


def _board(*footprints: str) -> str:
    body = "".join(footprints)
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
  (net 1 "VBUS")
{_OUTLINE}{body})
"""


def _write(tmp_path: Path, name: str, content: str) -> Path:
    pcb_file = tmp_path / name
    pcb_file.write_text(content)
    return pcb_file


def _edge_connector_conflicts(conflicts):
    return [c for c in conflicts if c.type == ConflictType.EDGE_CONNECTOR_PLACEMENT]


class TestEdgeConnectorPlacement:
    """Interior-marooned edge-connector detection in PlacementAnalyzer."""

    def test_interior_marooned_connector_flagged(self, tmp_path: Path):
        """A USB-C connector 8mm inside the north edge is flagged."""
        # J1 at sheet (140, 108) -> board-relative (40, 8); courtyard top edge
        # ~6.75mm inside the outline (well past the 2mm default threshold).
        pcb = _write(tmp_path, "marooned.kicad_pcb", _board(_connector_fp("J1", 140, 108, 1)))

        conflicts = PlacementAnalyzer().find_conflicts(pcb)
        flagged = _edge_connector_conflicts(conflicts)

        assert len(flagged) == 1
        c = flagged[0]
        assert c.component1 == "J1"
        assert c.component2 == "top_edge"
        assert c.actual_clearance is not None and c.actual_clearance > 2.0
        assert "interior" in c.message.lower()

    def test_edge_placed_connector_not_flagged(self, tmp_path: Path):
        """A connector flush against the north edge is not flagged."""
        # J1 at sheet (140, 101.5) -> board-relative (40, 1.5); courtyard top
        # edge only 0.25mm from the outline.
        pcb = _write(tmp_path, "edge.kicad_pcb", _board(_connector_fp("J1", 140, 101.5, 1)))

        conflicts = PlacementAnalyzer().find_conflicts(pcb)

        assert _edge_connector_conflicts(conflicts) == []

    def test_non_connector_interior_part_not_flagged(self, tmp_path: Path):
        """A resistor sitting in the board interior is not an edge connector."""
        pcb = _write(tmp_path, "resistor.kicad_pcb", _board(_resistor_fp("R1", 140, 130, 1)))

        conflicts = PlacementAnalyzer().find_conflicts(pcb)

        assert _edge_connector_conflicts(conflicts) == []

    def test_marooned_connector_not_double_reported_as_off_board(self, tmp_path: Path):
        """The interior connector is fully on-board, so OFF_BOARD must not fire."""
        pcb = _write(tmp_path, "marooned2.kicad_pcb", _board(_connector_fp("J1", 140, 108, 1)))

        conflicts = PlacementAnalyzer().find_conflicts(pcb)

        assert len(_edge_connector_conflicts(conflicts)) == 1
        assert [c for c in conflicts if c.type == ConflictType.OFF_BOARD] == []

    def test_edge_overhang_connector_not_flagged_by_this_check(self, tmp_path: Path):
        """A connector whose courtyard overhangs the edge is an OFF_BOARD case, not this one."""
        # J1 at sheet (140, 100) -> board-relative (40, 0); courtyard extends
        # above the north edge (min_y ~ -1.25), so it is "outside" and handled
        # by _check_off_board rather than the interior-marooned check.
        pcb = _write(tmp_path, "overhang.kicad_pcb", _board(_connector_fp("J1", 140, 100, 1)))

        conflicts = PlacementAnalyzer().find_conflicts(pcb)

        assert _edge_connector_conflicts(conflicts) == []

    def test_threshold_is_configurable(self, tmp_path: Path):
        """Raising edge_connector_max_inset above the actual inset suppresses the flag."""
        pcb = _write(tmp_path, "config.kicad_pcb", _board(_connector_fp("J1", 140, 108, 1)))

        # Default rules flag it; a 10mm tolerance (> the ~6.75mm inset) does not.
        assert len(_edge_connector_conflicts(PlacementAnalyzer().find_conflicts(pcb))) == 1
        loose = DesignRules(edge_connector_max_inset=10.0)
        assert _edge_connector_conflicts(PlacementAnalyzer().find_conflicts(pcb, loose)) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
