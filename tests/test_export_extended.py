"""Extended tests for export modules (gerber, assembly, pnp, bom)."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile

import pytest

from kicad_tools.export.gerber import (
    GerberConfig,
    ManufacturerPreset,
    JLCPCB_PRESET,
    PCBWAY_PRESET,
    OSHPARK_PRESET,
    MANUFACTURER_PRESETS,
    find_kicad_cli,
    GerberExporter,
)


class TestGerberConfig:
    """Tests for GerberConfig dataclass."""

    def test_default_values(self):
        config = GerberConfig()
        assert config.output_dir is None
        assert config.create_zip is True
        assert config.zip_name == "gerbers.zip"
        assert config.layers == []
        assert config.include_edge_cuts is True
        assert config.include_silkscreen is True
        assert config.include_soldermask is True
        assert config.include_solderpaste is False
        assert config.use_protel_extensions is True
        assert config.use_aux_origin is True
        assert config.generate_drill is True
        assert config.drill_format == "excellon"

    def test_custom_values(self):
        config = GerberConfig(
            create_zip=False,
            include_solderpaste=True,
            merge_pth_npth=True,
        )
        assert config.create_zip is False
        assert config.include_solderpaste is True
        assert config.merge_pth_npth is True


class TestManufacturerPreset:
    """Tests for ManufacturerPreset dataclass."""

    def test_jlcpcb_preset(self):
        preset = JLCPCB_PRESET
        assert preset.name == "JLCPCB"
        assert preset.config.use_protel_extensions is True
        assert preset.config.include_solderpaste is False
        assert "F.Cu" in preset.layer_rename

    def test_pcbway_preset(self):
        preset = PCBWAY_PRESET
        assert preset.name == "PCBWay"
        assert preset.config.include_solderpaste is True

    def test_oshpark_preset(self):
        preset = OSHPARK_PRESET
        assert preset.name == "OSH Park"
        assert preset.config.merge_pth_npth is True
        assert preset.config.use_aux_origin is False

    def test_manufacturer_presets_dict(self):
        assert "jlcpcb" in MANUFACTURER_PRESETS
        assert "pcbway" in MANUFACTURER_PRESETS
        assert "oshpark" in MANUFACTURER_PRESETS


class TestFindKicadCli:
    """Tests for find_kicad_cli function."""

    def test_find_in_path(self):
        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/kicad-cli"
            result = find_kicad_cli()
            assert result == Path("/usr/bin/kicad-cli")

    def test_not_found_anywhere(self):
        with patch("shutil.which", return_value=None):
            with patch.object(Path, "exists", return_value=False):
                result = find_kicad_cli()
                assert result is None


class TestGerberExporter:
    """Tests for GerberExporter class."""

    @pytest.fixture
    def mock_pcb_path(self, tmp_path):
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")
        return pcb_path

    def test_init(self, mock_pcb_path):
        exporter = GerberExporter(mock_pcb_path)
        assert exporter.pcb_path == mock_pcb_path

    def test_init_string_path(self, mock_pcb_path):
        exporter = GerberExporter(str(mock_pcb_path))
        assert exporter.pcb_path == mock_pcb_path

    def test_preset_lookup(self, mock_pcb_path):
        # Test preset lookup via MANUFACTURER_PRESETS
        assert "jlcpcb" in MANUFACTURER_PRESETS
        preset = MANUFACTURER_PRESETS["jlcpcb"]
        assert preset == JLCPCB_PRESET

    def test_preset_not_found(self, mock_pcb_path):
        # Verify unknown manufacturers are not in presets
        assert "unknown_manufacturer" not in MANUFACTURER_PRESETS

    def test_init_file_not_found(self, tmp_path):
        """Test that FileNotFoundError is raised for missing PCB."""
        nonexistent = tmp_path / "nonexistent.kicad_pcb"
        with pytest.raises(FileNotFoundError):
            GerberExporter(nonexistent)

    def test_export_for_manufacturer_unknown(self, mock_pcb_path):
        """Test that unknown manufacturer raises ValueError."""
        with patch.object(GerberExporter, "__init__", lambda self, path: None):
            exporter = GerberExporter.__new__(GerberExporter)
            exporter.pcb_path = mock_pcb_path
            exporter.kicad_cli = Path("/usr/bin/kicad-cli")

            with pytest.raises(ValueError, match="Unknown manufacturer"):
                exporter.export_for_manufacturer("unknown_fab_house")


class TestGerberConfigDefaults:
    """Additional tests for GerberConfig default layer computation."""

    def test_config_all_options_disabled(self):
        """Test config with all optional layers disabled."""
        config = GerberConfig(
            include_edge_cuts=False,
            include_silkscreen=False,
            include_soldermask=False,
            include_solderpaste=False,
        )
        assert config.include_edge_cuts is False
        assert config.include_silkscreen is False
        assert config.include_soldermask is False
        assert config.include_solderpaste is False

    def test_config_drill_options(self):
        """Test drill-specific options."""
        config = GerberConfig(
            generate_drill=True,
            drill_format="gerber_x2",
            merge_pth_npth=True,
            minimal_header=True,
        )
        assert config.drill_format == "gerber_x2"
        assert config.merge_pth_npth is True
        assert config.minimal_header is True

    def test_config_format_options(self):
        """Test format options."""
        config = GerberConfig(
            subtract_soldermask=True,
            disable_aperture_macros=True,
        )
        assert config.subtract_soldermask is True
        assert config.disable_aperture_macros is True


class TestManufacturerPresetDetails:
    """More detailed tests for manufacturer presets."""

    def test_jlcpcb_layer_rename_mapping(self):
        """Test JLCPCB layer renaming dictionary."""
        preset = JLCPCB_PRESET
        assert preset.layer_rename["F.Cu"] == "F_Cu"
        assert preset.layer_rename["B.Cu"] == "B_Cu"
        assert preset.layer_rename["Edge.Cuts"] == "Edge_Cuts"

    def test_pcbway_no_layer_rename(self):
        """Test PCBWay has no layer renaming."""
        preset = PCBWAY_PRESET
        # PCBWay doesn't need layer renaming
        assert preset.layer_rename == {}

    def test_oshpark_no_layer_rename(self):
        """Test OSH Park has no layer renaming."""
        preset = OSHPARK_PRESET
        assert preset.layer_rename == {}


class TestGerberExporterMethods:
    """Tests for GerberExporter internal methods."""

    @pytest.fixture
    def mock_exporter(self, tmp_path):
        """Create a mock exporter."""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")

        with patch.object(GerberExporter, "__init__", lambda self, path: None):
            exporter = GerberExporter.__new__(GerberExporter)
            exporter.pcb_path = pcb_path
            exporter.kicad_cli = Path("/usr/bin/kicad-cli")
            return exporter

    def test_get_default_layers_full(self, mock_exporter):
        """Test default layers with all options enabled."""
        config = GerberConfig(
            include_silkscreen=True,
            include_soldermask=True,
            include_solderpaste=True,
            include_edge_cuts=True,
        )
        layers = mock_exporter._get_default_layers(config)

        assert "F.Cu" in layers
        assert "B.Cu" in layers
        assert "F.SilkS" in layers
        assert "B.SilkS" in layers
        assert "F.Mask" in layers
        assert "B.Mask" in layers
        assert "F.Paste" in layers
        assert "B.Paste" in layers
        assert "Edge.Cuts" in layers

    def test_get_default_layers_minimal(self, mock_exporter):
        """Test default layers with options disabled."""
        config = GerberConfig(
            include_silkscreen=False,
            include_soldermask=False,
            include_solderpaste=False,
            include_edge_cuts=False,
        )
        layers = mock_exporter._get_default_layers(config)

        assert "F.Cu" in layers
        assert "B.Cu" in layers
        assert "F.SilkS" not in layers
        assert "F.Mask" not in layers
        assert "Edge.Cuts" not in layers

    def test_create_zip(self, mock_exporter, tmp_path):
        """Test zip file creation."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()

        # Create some test files
        (source_dir / "test1.gbr").write_text("gerber1")
        (source_dir / "test2.gbr").write_text("gerber2")

        zip_path = source_dir / "output.zip"
        mock_exporter._create_zip(source_dir, zip_path)

        assert zip_path.exists()

        # Verify contents
        import zipfile
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            assert "test1.gbr" in names
            assert "test2.gbr" in names

    def test_create_zip_overwrites_existing(self, mock_exporter, tmp_path):
        """Test that existing zip is overwritten."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "test.gbr").write_text("gerber")

        zip_path = source_dir / "output.zip"
        zip_path.write_text("old content")

        mock_exporter._create_zip(source_dir, zip_path)

        # Should be a valid zip now
        import zipfile
        with zipfile.ZipFile(zip_path, "r") as zf:
            assert "test.gbr" in zf.namelist()


# Import assembly module
from kicad_tools.export.assembly import (
    AssemblyConfig,
    AssemblyPackageResult,
    AssemblyPackage,
)


class TestAssemblyConfig:
    """Tests for AssemblyConfig dataclass."""

    def test_default_values(self):
        config = AssemblyConfig()
        assert config.output_dir == Path("assembly")
        assert config.include_bom is True
        assert config.include_pnp is True
        assert config.include_gerbers is True
        assert config.exclude_references == []

    def test_custom_values(self):
        config = AssemblyConfig(
            output_dir=Path("/custom/output"),
            include_gerbers=False,
            exclude_references=["R1", "R2"],
        )
        assert config.output_dir == Path("/custom/output")
        assert config.include_gerbers is False
        assert config.exclude_references == ["R1", "R2"]


class TestAssemblyPackageResult:
    """Tests for AssemblyPackageResult dataclass."""

    def test_success_no_errors(self):
        result = AssemblyPackageResult(output_dir=Path("/output"))
        assert result.success is True

    def test_failure_with_errors(self):
        result = AssemblyPackageResult(
            output_dir=Path("/output"),
            errors=["Error 1", "Error 2"],
        )
        assert result.success is False

    def test_str_representation(self):
        result = AssemblyPackageResult(
            output_dir=Path("/output"),
            bom_path=Path("/output/bom.csv"),
            pnp_path=Path("/output/cpl.csv"),
        )
        s = str(result)
        assert "Assembly Package" in s
        assert "BOM" in s
        assert "CPL" in s

    def test_str_with_errors(self):
        result = AssemblyPackageResult(
            output_dir=Path("/output"),
            errors=["Something went wrong"],
        )
        s = str(result)
        assert "Errors" in s
        assert "Something went wrong" in s


class TestAssemblyPackage:
    """Tests for AssemblyPackage class."""

    @pytest.fixture
    def mock_pcb_path(self, tmp_path):
        pcb_path = tmp_path / "test.kicad_pcb"
        # Minimal valid PCB content
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
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
        pcb_path.write_text(pcb_content)
        return pcb_path

    def test_init(self, mock_pcb_path):
        pkg = AssemblyPackage(mock_pcb_path)
        assert pkg.pcb_path == mock_pcb_path


# Import PnP module
from kicad_tools.export.pnp import (
    PlacementData,
    PnPExportConfig,
    PnPFormatter,
    JLCPCBPnPFormatter,
    GenericPnPFormatter,
)


class TestPlacementData:
    """Tests for PlacementData dataclass."""

    def test_creation(self):
        data = PlacementData(
            reference="R1",
            value="10k",
            footprint="0402_1005Metric",
            x=100.0,
            y=50.0,
            rotation=90.0,
            layer="F.Cu",
        )
        assert data.reference == "R1"
        assert data.value == "10k"
        assert data.x == 100.0
        assert data.y == 50.0
        assert data.rotation == 90.0
        assert data.layer == "F.Cu"


class TestPnPExportConfig:
    """Tests for PnPExportConfig dataclass."""

    def test_default_values(self):
        config = PnPExportConfig()
        assert config.x_offset == 0.0
        assert config.y_offset == 0.0
        assert config.mirror_x is False
        assert config.mirror_y is False
        assert config.use_aux_origin is True
        assert config.include_dnp is False
        assert config.rotation_offset == 0.0

    def test_custom_values(self):
        config = PnPExportConfig(
            x_offset=10.0,
            mirror_x=True,
            rotation_offset=90.0,
        )
        assert config.x_offset == 10.0
        assert config.mirror_x is True
        assert config.rotation_offset == 90.0


class TestPnPFormatter:
    """Tests for PnPFormatter and subclasses."""

    @pytest.fixture
    def sample_placements(self):
        return [
            PlacementData("R1", "10k", "R_0402", 100.0, 50.0, 0.0, "F.Cu"),
            PlacementData("R2", "4.7k", "R_0402", 110.0, 50.0, 90.0, "F.Cu"),
            PlacementData("C1", "100nF", "C_0402", 100.0, 60.0, 0.0, "B.Cu"),
        ]

    def test_jlcpcb_formatter_headers(self):
        formatter = JLCPCBPnPFormatter()
        headers = formatter.get_headers()
        assert "Designator" in headers
        assert "Package" in headers  # JLCPCB uses "Package" not "Footprint"

    def test_jlcpcb_formatter_format(self, sample_placements):
        formatter = JLCPCBPnPFormatter()
        result = formatter.format(sample_placements)
        assert "R1" in result
        assert "R2" in result
        assert "10k" in result

    def test_generic_formatter_headers(self):
        formatter = GenericPnPFormatter()
        headers = formatter.get_headers()
        assert len(headers) > 0

    def test_apply_transforms_offset(self, sample_placements):
        config = PnPExportConfig(x_offset=10.0, y_offset=5.0)
        formatter = JLCPCBPnPFormatter(config)

        transformed = formatter.apply_transforms(sample_placements[0])
        assert transformed.x == 110.0
        assert transformed.y == 55.0

    def test_apply_transforms_mirror(self, sample_placements):
        config = PnPExportConfig(mirror_x=True, mirror_y=True)
        formatter = JLCPCBPnPFormatter(config)

        transformed = formatter.apply_transforms(sample_placements[0])
        assert transformed.x == -100.0
        assert transformed.y == -50.0

    def test_apply_transforms_rotation(self, sample_placements):
        config = PnPExportConfig(rotation_offset=90.0)
        formatter = JLCPCBPnPFormatter(config)

        transformed = formatter.apply_transforms(sample_placements[0])
        assert transformed.rotation == 90.0  # 0 + 90

    def test_filter_top_only(self, sample_placements):
        config = PnPExportConfig(top_only=True)
        formatter = JLCPCBPnPFormatter(config)

        filtered = formatter.filter_placements(sample_placements)
        assert len(filtered) == 2  # Only F.Cu parts

    def test_filter_bottom_only(self, sample_placements):
        config = PnPExportConfig(bottom_only=True)
        formatter = JLCPCBPnPFormatter(config)

        filtered = formatter.filter_placements(sample_placements)
        assert len(filtered) == 1  # Only B.Cu parts
        assert filtered[0].reference == "C1"


# Import BOM formats
from kicad_tools.export.bom_formats import (
    BOMExportConfig,
    BOMFormatter,
    JLCPCBBOMFormatter,
    GenericBOMFormatter,
)


class TestBOMExportConfig:
    """Tests for BOMExportConfig dataclass."""

    def test_default_values(self):
        config = BOMExportConfig()
        assert config.group_by_value is True
        assert config.include_dnp is False

    def test_custom_values(self):
        config = BOMExportConfig(
            group_by_value=False,
            include_dnp=True,
        )
        assert config.group_by_value is False
        assert config.include_dnp is True


class TestBOMFormatter:
    """Tests for BOMFormatter classes."""

    def test_jlcpcb_formatter_headers(self):
        formatter = JLCPCBBOMFormatter()
        headers = formatter.get_headers()
        assert "Comment" in headers
        assert "Designator" in headers
        assert "Footprint" in headers

    def test_generic_formatter_headers(self):
        formatter = GenericBOMFormatter()
        headers = formatter.get_headers()
        assert len(headers) > 0


# Additional export helper tests
class TestExportModuleImports:
    """Tests for module-level exports."""

    def test_export_init_imports(self):
        from kicad_tools.export import create_assembly_package

        assert create_assembly_package is not None

    def test_gerber_exporter_available(self):
        from kicad_tools.export.gerber import GerberExporter

        assert GerberExporter is not None

    def test_pnp_exports_available(self):
        from kicad_tools.export.pnp import PlacementData, PnPExportConfig

        assert PlacementData is not None
        assert PnPExportConfig is not None
