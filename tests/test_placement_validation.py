"""Tests for kicad_tools.validate.placement module."""

from pathlib import Path

import pytest  # noqa: F401 - used for fixtures

from kicad_tools.validate.placement import (
    BOMPlacementVerifier,
    PlacementResult,
    PlacementStatus,
)


class TestPlacementStatus:
    """Tests for PlacementStatus dataclass."""

    def test_placed_component(self):
        """Test creating a status for a placed component."""
        status = PlacementStatus(
            reference="R1",
            value="10k",
            footprint="Resistor_SMD:R_0402",
            in_bom=True,
            in_pcb=True,
            is_placed=True,
            position=(50.0, 50.0),
            layer="F.Cu",
            issues=(),
        )
        assert status.reference == "R1"
        assert status.is_placed
        assert not status.has_issues
        assert not status.is_error

    def test_missing_component(self):
        """Test creating a status for a missing component."""
        status = PlacementStatus(
            reference="C5",
            value="100nF",
            footprint="Capacitor_SMD:C_0402",
            in_bom=True,
            in_pcb=False,
            is_placed=False,
            position=None,
            layer=None,
            issues=("Component missing from PCB",),
        )
        assert not status.in_pcb
        assert not status.is_placed
        assert status.has_issues
        assert status.is_error

    def test_unplaced_at_origin(self):
        """Test creating a status for component at origin."""
        status = PlacementStatus(
            reference="U1",
            value="STM32",
            footprint="TSSOP-20",
            in_bom=True,
            in_pcb=True,
            is_placed=False,
            position=(0.0, 0.0),
            layer="F.Cu",
            issues=("Component at origin (not placed on board)",),
        )
        assert status.in_pcb
        assert not status.is_placed
        assert status.has_issues
        assert status.is_error

    def test_to_dict(self):
        """Test conversion to dictionary."""
        status = PlacementStatus(
            reference="R1",
            value="10k",
            footprint="R_0402",
            in_bom=True,
            in_pcb=True,
            is_placed=True,
            position=(100.0, 200.0),
            layer="F.Cu",
            issues=(),
        )
        d = status.to_dict()
        assert d["reference"] == "R1"
        assert d["value"] == "10k"
        assert d["position"] == [100.0, 200.0]
        assert d["is_placed"] is True
        assert d["issues"] == []


class TestPlacementResult:
    """Tests for PlacementResult class."""

    def test_empty_result(self):
        """Test empty result."""
        result = PlacementResult()
        assert result.total_count == 0
        assert result.placed_count == 0
        assert result.unplaced_count == 0
        assert result.all_placed
        assert len(result) == 0

    def test_all_placed(self):
        """Test result with all components placed."""
        statuses = [
            PlacementStatus(
                reference="R1",
                value="10k",
                footprint="R_0402",
                in_bom=True,
                in_pcb=True,
                is_placed=True,
                position=(50.0, 50.0),
                layer="F.Cu",
                issues=(),
            ),
            PlacementStatus(
                reference="C1",
                value="100nF",
                footprint="C_0402",
                in_bom=True,
                in_pcb=True,
                is_placed=True,
                position=(60.0, 60.0),
                layer="F.Cu",
                issues=(),
            ),
        ]
        result = PlacementResult(statuses=statuses)
        assert result.total_count == 2
        assert result.placed_count == 2
        assert result.unplaced_count == 0
        assert result.all_placed
        assert len(result.placed) == 2
        assert len(result.unplaced) == 0

    def test_mixed_placement(self):
        """Test result with some components not placed."""
        statuses = [
            PlacementStatus(
                reference="R1",
                value="10k",
                footprint="R_0402",
                in_bom=True,
                in_pcb=True,
                is_placed=True,
                position=(50.0, 50.0),
                layer="F.Cu",
                issues=(),
            ),
            PlacementStatus(
                reference="C1",
                value="100nF",
                footprint="C_0402",
                in_bom=True,
                in_pcb=False,
                is_placed=False,
                position=None,
                layer=None,
                issues=("Component missing from PCB",),
            ),
            PlacementStatus(
                reference="U1",
                value="STM32",
                footprint="TSSOP-20",
                in_bom=True,
                in_pcb=True,
                is_placed=False,
                position=(0.0, 0.0),
                layer="F.Cu",
                issues=("Component at origin (not placed on board)",),
            ),
        ]
        result = PlacementResult(statuses=statuses)
        assert result.total_count == 3
        assert result.placed_count == 1
        assert result.unplaced_count == 2
        assert result.missing_count == 1
        assert not result.all_placed
        assert len(result.placed) == 1
        assert len(result.unplaced) == 2
        assert len(result.missing) == 1
        assert len(result.at_origin) == 1

    def test_to_dict(self):
        """Test conversion to dictionary."""
        statuses = [
            PlacementStatus(
                reference="R1",
                value="10k",
                footprint="R_0402",
                in_bom=True,
                in_pcb=True,
                is_placed=True,
                position=(50.0, 50.0),
                layer="F.Cu",
                issues=(),
            ),
        ]
        result = PlacementResult(statuses=statuses)
        d = result.to_dict()
        assert d["all_placed"] is True
        assert d["total"] == 1
        assert d["placed"] == 1
        assert d["unplaced"] == 0
        assert d["missing"] == 0
        assert len(d["statuses"]) == 1

    def test_summary(self):
        """Test summary generation."""
        statuses = [
            PlacementStatus(
                reference="R1",
                value="10k",
                footprint="R_0402",
                in_bom=True,
                in_pcb=True,
                is_placed=True,
                position=(50.0, 50.0),
                layer="F.Cu",
                issues=(),
            ),
        ]
        result = PlacementResult(statuses=statuses)
        summary = result.summary()
        assert "ALL PLACED" in summary
        assert "1/1" in summary


class TestBOMPlacementVerifier:
    """Tests for BOMPlacementVerifier class."""

    def test_verifier_with_all_placed(self, minimal_schematic: Path, minimal_pcb: Path):
        """Test verifier when all components are placed."""
        verifier = BOMPlacementVerifier(minimal_schematic, minimal_pcb)
        result = verifier.verify()

        # The minimal fixtures have R1 in both schematic and PCB at position 100,100
        assert result.total_count >= 1
        assert result.all_placed or result.placed_count >= 1

    def test_verifier_detects_missing(self, tmp_path: Path):
        """Test verifier detects components missing from PCB."""
        # Create schematic with R1 and C1
        schematic_content = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "R1")
          (unit 1)
        )
      )
    )
  )
  (symbol
    (lib_id "Device:C")
    (at 150 100 0)
    (uuid "00000000-0000-0000-0000-000000000005")
    (property "Reference" "C1" (at 150 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 150 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Capacitor_SMD:C_0402_1005Metric" (at 150 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 150 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000006"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000007"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "C1")
          (unit 1)
        )
      )
    )
  )
)
"""
        # Create PCB with only R1 (C1 is missing)
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 50 50)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu"))
  )
)
"""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(schematic_content)
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(pcb_content)

        verifier = BOMPlacementVerifier(sch_file, pcb_file)
        result = verifier.verify()

        assert result.missing_count == 1
        assert not result.all_placed

        # Find the missing component
        missing = [s for s in result.statuses if s.reference == "C1"]
        assert len(missing) == 1
        assert not missing[0].in_pcb
        assert "missing" in missing[0].issues[0].lower()

    def test_verifier_detects_at_origin(self, tmp_path: Path):
        """Test verifier detects components at origin (unplaced)."""
        # Create schematic with U1
        schematic_content = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "U1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "IC" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Package_SO:TSSOP-20" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "U1")
          (unit 1)
        )
      )
    )
  )
)
"""
        # Create PCB with U1 at origin (0, 0) - unplaced
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (footprint "Package_SO:TSSOP-20"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 0 0)
    (property "Reference" "U1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "IC" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu"))
  )
)
"""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(schematic_content)
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(pcb_content)

        verifier = BOMPlacementVerifier(sch_file, pcb_file)
        result = verifier.verify()

        # Component should be detected as at origin (unplaced)
        assert len(result.at_origin) == 1
        assert not result.all_placed

        # Find the unplaced component
        at_origin = result.at_origin[0]
        assert at_origin.reference == "U1"
        assert at_origin.in_pcb
        assert not at_origin.is_placed
        assert "origin" in at_origin.issues[0].lower()

    def test_get_unplaced_convenience(self, minimal_schematic: Path, minimal_pcb: Path):
        """Test get_unplaced convenience method."""
        verifier = BOMPlacementVerifier(minimal_schematic, minimal_pcb)
        unplaced = verifier.get_unplaced()
        # Should return a list (may be empty if all placed)
        assert isinstance(unplaced, list)

    def test_sort_key(self):
        """Test reference designator sorting."""
        # Use the static method directly
        sort_key = BOMPlacementVerifier._sort_key

        # Test basic sorting
        assert sort_key("R1") == ("R", 1)
        assert sort_key("R10") == ("R", 10)
        assert sort_key("C23") == ("C", 23)
        assert sort_key("U1") == ("U", 1)

        # Test sorting order
        refs = ["R10", "R1", "R2", "C1", "U1"]
        sorted_refs = sorted(refs, key=sort_key)
        assert sorted_refs == ["C1", "R1", "R2", "R10", "U1"]

    def test_repr(self, minimal_schematic: Path, minimal_pcb: Path):
        """Test string representation."""
        verifier = BOMPlacementVerifier(minimal_schematic, minimal_pcb)
        repr_str = repr(verifier)
        assert "BOMPlacementVerifier" in repr_str
        assert "bom_items=" in repr_str
        assert "pcb_footprints=" in repr_str
