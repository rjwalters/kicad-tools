"""Tests for the project module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.project import (
    CrossReferenceResult,
    MismatchedComponent,
    OrphanedFootprint,
    Project,
    UnplacedSymbol,
)


class TestUnplacedSymbol:
    """Tests for UnplacedSymbol dataclass."""

    def test_creation(self):
        symbol = UnplacedSymbol(
            reference="U1",
            value="ATmega328P",
            lib_id="MCU_Microchip_ATmega:ATmega328P-AU",
            footprint_name="Package_QFP:TQFP-32_7x7mm_P0.8mm",
        )
        assert symbol.reference == "U1"
        assert symbol.value == "ATmega328P"
        assert symbol.lib_id == "MCU_Microchip_ATmega:ATmega328P-AU"
        assert symbol.footprint_name == "Package_QFP:TQFP-32_7x7mm_P0.8mm"


class TestOrphanedFootprint:
    """Tests for OrphanedFootprint dataclass."""

    def test_creation(self):
        fp = OrphanedFootprint(
            reference="R99",
            value="10k",
            footprint_name="Resistor_SMD:R_0402_1005Metric",
            position=(100.0, 50.0),
        )
        assert fp.reference == "R99"
        assert fp.value == "10k"
        assert fp.footprint_name == "Resistor_SMD:R_0402_1005Metric"
        assert fp.position == (100.0, 50.0)


class TestMismatchedComponent:
    """Tests for MismatchedComponent dataclass."""

    def test_creation(self):
        mismatch = MismatchedComponent(
            reference="R1",
            schematic_value="10k",
            pcb_value="4.7k",
            schematic_footprint="Resistor_SMD:R_0402_1005Metric",
            pcb_footprint="Resistor_SMD:R_0603_1608Metric",
            mismatches=["value", "footprint"],
        )
        assert mismatch.reference == "R1"
        assert mismatch.schematic_value == "10k"
        assert mismatch.pcb_value == "4.7k"
        assert "value" in mismatch.mismatches
        assert "footprint" in mismatch.mismatches

    def test_default_mismatches(self):
        mismatch = MismatchedComponent(
            reference="R1",
            schematic_value="10k",
            pcb_value="10k",
            schematic_footprint="fp1",
            pcb_footprint="fp2",
        )
        assert mismatch.mismatches == []


class TestCrossReferenceResult:
    """Tests for CrossReferenceResult dataclass."""

    def test_default_values(self):
        result = CrossReferenceResult()
        assert result.matched == 0
        assert result.unplaced == []
        assert result.orphaned == []
        assert result.mismatched == []

    def test_is_clean_empty_result(self):
        result = CrossReferenceResult()
        assert result.is_clean is True

    def test_is_clean_with_matched(self):
        result = CrossReferenceResult(matched=10)
        assert result.is_clean is True

    def test_is_clean_with_unplaced(self):
        result = CrossReferenceResult(
            unplaced=[UnplacedSymbol("U1", "value", "lib", "fp")]
        )
        assert result.is_clean is False

    def test_is_clean_with_orphaned(self):
        result = CrossReferenceResult(
            orphaned=[OrphanedFootprint("R1", "10k", "fp", (0, 0))]
        )
        assert result.is_clean is False

    def test_is_clean_with_mismatched(self):
        result = CrossReferenceResult(
            mismatched=[MismatchedComponent("R1", "10k", "4.7k", "fp1", "fp2")]
        )
        assert result.is_clean is False

    def test_summary(self):
        result = CrossReferenceResult(
            matched=5,
            unplaced=[UnplacedSymbol("U1", "v", "l", "f")],
            orphaned=[
                OrphanedFootprint("R1", "10k", "fp", (0, 0)),
                OrphanedFootprint("R2", "4.7k", "fp", (10, 0)),
            ],
            mismatched=[],
        )
        summary = result.summary()
        assert summary["matched"] == 5
        assert summary["unplaced"] == 1
        assert summary["orphaned"] == 2
        assert summary["mismatched"] == 0


class TestProject:
    """Tests for Project class."""

    def test_initialization_empty(self):
        project = Project()
        assert project.project_file is None
        assert project._schematic_path is None
        assert project._pcb_path is None
        assert project._schematic is None
        assert project._pcb is None

    def test_initialization_with_paths(self, tmp_path):
        sch_path = tmp_path / "test.kicad_sch"
        pcb_path = tmp_path / "test.kicad_pcb"

        project = Project(schematic=sch_path, pcb=pcb_path)
        assert project._schematic_path == sch_path
        assert project._pcb_path == pcb_path

    def test_initialization_with_project_file(self, tmp_path):
        pro_path = tmp_path / "test.kicad_pro"

        project = Project(project_file=pro_path)
        assert project.project_file == pro_path

    def test_name_from_project_file(self, tmp_path):
        pro_path = tmp_path / "my_project.kicad_pro"
        project = Project(project_file=pro_path)
        assert project.name == "my_project"

    def test_name_from_pcb(self, tmp_path):
        pcb_path = tmp_path / "board.kicad_pcb"
        project = Project(pcb=pcb_path)
        assert project.name == "board"

    def test_name_from_schematic(self, tmp_path):
        sch_path = tmp_path / "circuit.kicad_sch"
        project = Project(schematic=sch_path)
        assert project.name == "circuit"

    def test_name_unnamed(self):
        project = Project()
        assert project.name == "unnamed"

    def test_directory_from_project_file(self, tmp_path):
        pro_path = tmp_path / "subdir" / "test.kicad_pro"
        pro_path.parent.mkdir(parents=True, exist_ok=True)
        project = Project(project_file=pro_path)
        assert project.directory == tmp_path / "subdir"

    def test_directory_from_pcb(self, tmp_path):
        pcb_path = tmp_path / "test.kicad_pcb"
        project = Project(pcb=pcb_path)
        assert project.directory == tmp_path

    def test_directory_from_schematic(self, tmp_path):
        sch_path = tmp_path / "test.kicad_sch"
        project = Project(schematic=sch_path)
        assert project.directory == tmp_path

    def test_directory_none(self):
        project = Project()
        assert project.directory is None

    def test_repr(self, tmp_path):
        sch_path = tmp_path / "test.kicad_sch"
        pcb_path = tmp_path / "test.kicad_pcb"
        project = Project(schematic=sch_path, pcb=pcb_path)

        repr_str = repr(project)
        assert "Project" in repr_str
        assert "test.kicad_sch" in repr_str
        assert "test.kicad_pcb" in repr_str


class TestProjectLoad:
    """Tests for Project.load() class method."""

    def test_load_existing_project(self, tmp_path):
        # Create project files
        pro_path = tmp_path / "test.kicad_pro"
        sch_path = tmp_path / "test.kicad_sch"
        pcb_path = tmp_path / "test.kicad_pcb"

        pro_path.write_text("{}")
        sch_path.write_text("")
        pcb_path.write_text("")

        project = Project.load(pro_path)

        assert project.project_file == pro_path
        assert project._schematic_path == sch_path
        assert project._pcb_path == pcb_path

    def test_load_project_without_schematic(self, tmp_path):
        pro_path = tmp_path / "test.kicad_pro"
        pcb_path = tmp_path / "test.kicad_pcb"

        pro_path.write_text("{}")
        pcb_path.write_text("")

        project = Project.load(pro_path)

        assert project._schematic_path is None
        assert project._pcb_path == pcb_path

    def test_load_project_without_pcb(self, tmp_path):
        pro_path = tmp_path / "test.kicad_pro"
        sch_path = tmp_path / "test.kicad_sch"

        pro_path.write_text("{}")
        sch_path.write_text("")

        project = Project.load(pro_path)

        assert project._schematic_path == sch_path
        assert project._pcb_path is None

    def test_load_nonexistent_project(self, tmp_path):
        pro_path = tmp_path / "nonexistent.kicad_pro"

        with pytest.raises(FileNotFoundError):
            Project.load(pro_path)


class TestProjectFromPCB:
    """Tests for Project.from_pcb() class method."""

    def test_from_pcb_with_related_files(self, tmp_path):
        pcb_path = tmp_path / "board.kicad_pcb"
        sch_path = tmp_path / "board.kicad_sch"
        pro_path = tmp_path / "board.kicad_pro"

        pcb_path.write_text("")
        sch_path.write_text("")
        pro_path.write_text("{}")

        project = Project.from_pcb(pcb_path)

        assert project._pcb_path == pcb_path
        assert project._schematic_path == sch_path
        assert project.project_file == pro_path

    def test_from_pcb_without_schematic(self, tmp_path):
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text("")

        project = Project.from_pcb(pcb_path)

        assert project._pcb_path == pcb_path
        assert project._schematic_path is None
        assert project.project_file is None


class TestProjectSchematicProperty:
    """Tests for Project.schematic property."""

    def test_schematic_lazy_load(self, minimal_schematic):
        project = Project(schematic=minimal_schematic)

        # First access should load the schematic
        sch = project.schematic
        assert sch is not None
        assert project._schematic is not None

    def test_schematic_returns_cached(self, minimal_schematic):
        project = Project(schematic=minimal_schematic)

        # Load schematic
        sch1 = project.schematic
        # Second access should return cached version
        sch2 = project.schematic
        assert sch1 is sch2

    def test_schematic_none_when_no_path(self):
        project = Project()
        assert project.schematic is None

    def test_schematic_none_when_file_missing(self, tmp_path):
        sch_path = tmp_path / "nonexistent.kicad_sch"
        project = Project(schematic=sch_path)
        assert project.schematic is None


class TestProjectPCBProperty:
    """Tests for Project.pcb property."""

    def test_pcb_lazy_load(self, minimal_pcb):
        project = Project(pcb=minimal_pcb)

        # First access should load the PCB
        pcb = project.pcb
        assert pcb is not None
        assert project._pcb is not None

    def test_pcb_returns_cached(self, minimal_pcb):
        project = Project(pcb=minimal_pcb)

        # Load PCB
        pcb1 = project.pcb
        # Second access should return cached version
        pcb2 = project.pcb
        assert pcb1 is pcb2

    def test_pcb_none_when_no_path(self):
        project = Project()
        assert project.pcb is None

    def test_pcb_none_when_file_missing(self, tmp_path):
        pcb_path = tmp_path / "nonexistent.kicad_pcb"
        project = Project(pcb=pcb_path)
        assert project.pcb is None


class TestProjectGetBOM:
    """Tests for Project.get_bom() method."""

    def test_get_bom_from_schematic(self, minimal_schematic):
        project = Project(schematic=minimal_schematic)
        bom = project.get_bom()
        assert bom is not None

    def test_get_bom_cached(self, minimal_schematic):
        project = Project(schematic=minimal_schematic)
        bom1 = project.get_bom()
        bom2 = project.get_bom()
        assert bom1 is bom2

    def test_get_bom_force_reload(self, minimal_schematic):
        project = Project(schematic=minimal_schematic)
        bom1 = project.get_bom()
        bom2 = project.get_bom(force_reload=True)
        # New BOM object should be created
        assert bom1 is not bom2

    def test_get_bom_none_when_no_schematic(self):
        project = Project()
        assert project.get_bom() is None


class TestProjectCrossReference:
    """Tests for Project.cross_reference() method."""

    def test_cross_reference_matching_components(self, minimal_schematic, minimal_pcb):
        project = Project(schematic=minimal_schematic, pcb=minimal_pcb)
        result = project.cross_reference()

        # Both have R1, so there should be a match
        assert result.matched >= 1

    def test_cross_reference_missing_schematic(self, minimal_pcb):
        project = Project(pcb=minimal_pcb)
        result = project.cross_reference()
        assert result.matched == 0

    def test_cross_reference_missing_pcb(self, minimal_schematic):
        project = Project(schematic=minimal_schematic)
        result = project.cross_reference()
        assert result.matched == 0


class TestProjectFindUnplaced:
    """Tests for Project.find_unplaced_symbols() method."""

    def test_find_unplaced_symbols(self, minimal_schematic, minimal_pcb):
        project = Project(schematic=minimal_schematic, pcb=minimal_pcb)
        unplaced = project.find_unplaced_symbols()
        # Should return a list
        assert isinstance(unplaced, list)


class TestProjectFindOrphaned:
    """Tests for Project.find_orphaned_footprints() method."""

    def test_find_orphaned_footprints(self, minimal_schematic, minimal_pcb):
        project = Project(schematic=minimal_schematic, pcb=minimal_pcb)
        orphaned = project.find_orphaned_footprints()
        # Should return a list
        assert isinstance(orphaned, list)


class TestProjectExportAssembly:
    """Tests for Project.export_assembly() method."""

    def test_export_assembly_requires_pcb(self):
        project = Project()

        with pytest.raises(ValueError) as excinfo:
            project.export_assembly("output/")

        assert "PCB path required" in str(excinfo.value)

    def test_export_assembly_calls_create_assembly_package(self, tmp_path, minimal_pcb):
        project = Project(pcb=minimal_pcb)
        output_dir = tmp_path / "output"

        with patch("kicad_tools.export.create_assembly_package") as mock_create:
            mock_create.return_value = MagicMock()
            project.export_assembly(output_dir, manufacturer="jlcpcb")

            mock_create.assert_called_once_with(
                pcb=minimal_pcb,
                schematic=None,
                manufacturer="jlcpcb",
                output_dir=output_dir,
            )


class TestProjectCrossReferenceDetailed:
    """More detailed tests for cross-reference functionality."""

    def test_detects_value_mismatch(self, tmp_path):
        # Create schematic with R1 value="10k"
        sch_content = """(kicad_sch
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
            (instances
              (project "test"
                (path "/00000000-0000-0000-0000-000000000001"
                  (reference "R1")
                  (unit 1)
                )
              )
            )
          )
        )
        """

        # Create PCB with R1 value="4.7k"
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (generator_version "8.0")
          (general (thickness 1.6))
          (paper "A4")
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (footprint "Resistor_SMD:R_0402_1005Metric"
            (layer "F.Cu")
            (uuid "00000000-0000-0000-0000-000000000010")
            (at 100 100)
            (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "1"))
            (property "Value" "4.7k" (at 0 0 0) (layer "F.Fab") (uuid "2"))
            (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu"))
          )
        )
        """

        sch_path = tmp_path / "test.kicad_sch"
        pcb_path = tmp_path / "test.kicad_pcb"
        sch_path.write_text(sch_content)
        pcb_path.write_text(pcb_content)

        project = Project(schematic=sch_path, pcb=pcb_path)
        result = project.cross_reference()

        # Should detect value mismatch
        assert len(result.mismatched) == 1
        assert result.mismatched[0].reference == "R1"
        assert "value" in result.mismatched[0].mismatches

    def test_detects_unplaced_symbol(self, tmp_path):
        # Create schematic with R1 and R2
        sch_content = """(kicad_sch
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
            (property "Footprint" "Resistor_SMD:R_0402" (at 100 100 0) (effects (hide yes)))
            (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
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
            (lib_id "Device:R")
            (at 120 100 0)
            (uuid "00000000-0000-0000-0000-000000000003")
            (property "Reference" "R2" (at 120 90 0) (effects (font (size 1.27 1.27))))
            (property "Value" "4.7k" (at 120 110 0) (effects (font (size 1.27 1.27))))
            (property "Footprint" "Resistor_SMD:R_0402" (at 100 100 0) (effects (hide yes)))
            (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
            (instances
              (project "test"
                (path "/00000000-0000-0000-0000-000000000001"
                  (reference "R2")
                  (unit 1)
                )
              )
            )
          )
        )
        """

        # Create PCB with only R1
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (generator_version "8.0")
          (general (thickness 1.6))
          (paper "A4")
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (footprint "Resistor_SMD:R_0402"
            (layer "F.Cu")
            (uuid "00000000-0000-0000-0000-000000000010")
            (at 100 100)
            (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "1"))
            (property "Value" "10k" (at 0 0 0) (layer "F.Fab") (uuid "2"))
            (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu"))
          )
        )
        """

        sch_path = tmp_path / "test.kicad_sch"
        pcb_path = tmp_path / "test.kicad_pcb"
        sch_path.write_text(sch_content)
        pcb_path.write_text(pcb_content)

        project = Project(schematic=sch_path, pcb=pcb_path)
        result = project.cross_reference()

        # R1 should be matched
        assert result.matched == 1
        # R2 should be unplaced
        assert len(result.unplaced) == 1
        assert result.unplaced[0].reference == "R2"

    def test_detects_orphaned_footprint(self, tmp_path):
        # Create schematic with only R1
        sch_content = """(kicad_sch
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
            (property "Footprint" "Resistor_SMD:R_0402" (at 100 100 0) (effects (hide yes)))
            (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
            (instances
              (project "test"
                (path "/00000000-0000-0000-0000-000000000001"
                  (reference "R1")
                  (unit 1)
                )
              )
            )
          )
        )
        """

        # Create PCB with R1 and R99 (orphaned)
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (generator_version "8.0")
          (general (thickness 1.6))
          (paper "A4")
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (footprint "Resistor_SMD:R_0402"
            (layer "F.Cu")
            (uuid "00000000-0000-0000-0000-000000000010")
            (at 100 100)
            (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "1"))
            (property "Value" "10k" (at 0 0 0) (layer "F.Fab") (uuid "2"))
            (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu"))
          )
          (footprint "Resistor_SMD:R_0402"
            (layer "F.Cu")
            (uuid "00000000-0000-0000-0000-000000000011")
            (at 150 100)
            (property "Reference" "R99" (at 0 0 0) (layer "F.SilkS") (uuid "3"))
            (property "Value" "unknown" (at 0 0 0) (layer "F.Fab") (uuid "4"))
            (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu"))
          )
        )
        """

        sch_path = tmp_path / "test.kicad_sch"
        pcb_path = tmp_path / "test.kicad_pcb"
        sch_path.write_text(sch_content)
        pcb_path.write_text(pcb_content)

        project = Project(schematic=sch_path, pcb=pcb_path)
        result = project.cross_reference()

        # R1 should be matched
        assert result.matched == 1
        # R99 should be orphaned
        assert len(result.orphaned) == 1
        assert result.orphaned[0].reference == "R99"
        assert result.orphaned[0].position == (150.0, 100.0)

    def test_ignores_power_symbols(self, tmp_path):
        # Create schematic with power symbol (#PWR01)
        sch_content = """(kicad_sch
          (version 20231120)
          (generator "test")
          (generator_version "8.0")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
          (symbol
            (lib_id "power:GND")
            (at 100 100 0)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Reference" "#PWR01" (at 100 90 0) (effects (font (size 1.27 1.27)) hide))
            (property "Value" "GND" (at 100 110 0) (effects (font (size 1.27 1.27))))
            (property "Footprint" "" (at 100 100 0) (effects (hide yes)))
            (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
            (instances
              (project "test"
                (path "/00000000-0000-0000-0000-000000000001"
                  (reference "#PWR01")
                  (unit 1)
                )
              )
            )
          )
        )
        """

        # Empty PCB
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (generator_version "8.0")
          (general (thickness 1.6))
          (paper "A4")
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
        )
        """

        sch_path = tmp_path / "test.kicad_sch"
        pcb_path = tmp_path / "test.kicad_pcb"
        sch_path.write_text(sch_content)
        pcb_path.write_text(pcb_content)

        project = Project(schematic=sch_path, pcb=pcb_path)
        result = project.cross_reference()

        # Power symbols should be ignored, so no unplaced symbols
        assert len(result.unplaced) == 0
