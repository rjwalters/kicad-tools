"""Tests for PCB.import_from_schematic() and related functionality."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.operations.netlist import (
    Netlist,
    NetlistComponent,
    NetlistNet,
    NetNode,
    find_kicad_cli,
)
from kicad_tools.schema.pcb import PCB


class TestImportFromNetlist:
    """Tests for PCB.import_from_netlist() with mocked data."""

    def test_import_empty_netlist(self):
        """Test importing an empty netlist."""
        pcb = PCB.create(width=100, height=100)
        netlist = Netlist()

        result = pcb.import_from_netlist(netlist)

        assert result["footprints_added"] == []
        assert result["footprints_skipped"] == []
        assert result["footprints_failed"] == []
        assert result["nets_assigned"] == []
        assert result["nets_failed"] == []

    def test_import_skips_components_without_footprint(self):
        """Test that components without footprint specification are skipped."""
        pcb = PCB.create(width=100, height=100)
        netlist = Netlist()
        netlist.components = [
            NetlistComponent(
                reference="U1",
                value="TestIC",
                footprint="",  # No footprint
                lib_id="TestLib:TestIC",
            ),
        ]

        result = pcb.import_from_netlist(netlist)

        assert result["footprints_added"] == []
        assert "U1" in result["footprints_skipped"]
        assert len(pcb.footprints) == 0

    def test_import_skips_existing_footprints(self, tmp_path: Path):
        """Test that existing footprints are not duplicated."""
        pcb = PCB.create(width=100, height=100)

        # Mock add_footprint to add a footprint
        with patch.object(pcb, "add_footprint") as mock_add:
            # First create a mock existing footprint
            mock_fp = MagicMock()
            mock_fp.reference = "R1"
            pcb._footprints.append(mock_fp)

            netlist = Netlist()
            netlist.components = [
                NetlistComponent(
                    reference="R1",
                    value="10k",
                    footprint="Resistor_SMD:R_0603_1608Metric",
                    lib_id="Device:R",
                ),
            ]

            result = pcb.import_from_netlist(netlist)

            # Should not have called add_footprint since footprint exists
            mock_add.assert_not_called()
            assert "R1" in result["footprints_skipped"]

    def test_import_places_footprints_in_grid(self):
        """Test that footprints are placed in a grid pattern."""
        pcb = PCB.create(width=200, height=200)

        with patch.object(pcb, "add_footprint") as mock_add:
            mock_add.return_value = MagicMock()

            netlist = Netlist()
            netlist.components = [
                NetlistComponent(
                    reference=f"R{i}",
                    value="10k",
                    footprint="Resistor_SMD:R_0603_1608Metric",
                    lib_id="Device:R",
                )
                for i in range(1, 6)
            ]

            pcb.import_from_netlist(
                netlist,
                placement_start=(10.0, 10.0),
                placement_spacing=15.0,
                columns=3,
            )

            # Should have attempted to add 5 footprints
            assert mock_add.call_count == 5

            # Check grid positions from calls
            calls = mock_add.call_args_list
            # Row 1: R1 at (10, 10), R2 at (25, 10), R3 at (40, 10)
            # Row 2: R4 at (10, 25), R5 at (25, 25)
            assert calls[0].kwargs["x"] == 10.0
            assert calls[0].kwargs["y"] == 10.0
            assert calls[1].kwargs["x"] == 25.0
            assert calls[1].kwargs["y"] == 10.0
            assert calls[2].kwargs["x"] == 40.0
            assert calls[2].kwargs["y"] == 10.0
            assert calls[3].kwargs["x"] == 10.0
            assert calls[3].kwargs["y"] == 25.0
            assert calls[4].kwargs["x"] == 25.0
            assert calls[4].kwargs["y"] == 25.0

    def test_import_assigns_nets(self):
        """Test that nets are assigned to footprint pads."""
        pcb = PCB.create(width=100, height=100)

        with patch.object(pcb, "add_footprint") as mock_add:
            mock_add.return_value = MagicMock()

            with patch.object(pcb, "assign_nets_from_netlist") as mock_assign:
                mock_assign.return_value = {
                    "assigned": ["R1.1", "R1.2"],
                    "missing_footprints": [],
                    "missing_pads": [],
                }

                netlist = Netlist()
                netlist.components = [
                    NetlistComponent(
                        reference="R1",
                        value="10k",
                        footprint="Resistor_SMD:R_0603_1608Metric",
                        lib_id="Device:R",
                    ),
                ]
                netlist.nets = [
                    NetlistNet(
                        code=1,
                        name="VCC",
                        nodes=[NetNode(reference="R1", pin="1")],
                    ),
                    NetlistNet(
                        code=2,
                        name="GND",
                        nodes=[NetNode(reference="R1", pin="2")],
                    ),
                ]

                result = pcb.import_from_netlist(netlist)

                # Should have called assign_nets_from_netlist
                mock_assign.assert_called_once_with(netlist)
                assert result["nets_assigned"] == ["R1.1", "R1.2"]

    def test_import_reports_failed_footprints(self):
        """Test that failed footprint additions are reported."""
        pcb = PCB.create(width=100, height=100)

        with patch.object(pcb, "add_footprint") as mock_add:
            mock_add.side_effect = FileNotFoundError("Footprint not found")

            netlist = Netlist()
            netlist.components = [
                NetlistComponent(
                    reference="U1",
                    value="TestIC",
                    footprint="NonExistent:FP_123",
                    lib_id="TestLib:TestIC",
                ),
            ]

            result = pcb.import_from_netlist(netlist)

            assert len(result["footprints_failed"]) == 1
            assert "U1" in result["footprints_failed"][0]
            assert "Footprint not found" in result["footprints_failed"][0]


class TestImportFromSchematic:
    """Tests for PCB.import_from_schematic()."""

    def test_import_from_schematic_calls_export_netlist(self):
        """Test that import_from_schematic exports netlist and imports it."""
        pcb = PCB.create(width=100, height=100)

        mock_netlist = Netlist()
        mock_netlist.components = []
        mock_netlist.nets = []

        with patch(
            "kicad_tools.operations.netlist.export_netlist", return_value=mock_netlist
        ) as mock_export:
            with patch.object(pcb, "import_from_netlist") as mock_import:
                mock_import.return_value = {
                    "footprints_added": [],
                    "footprints_skipped": [],
                    "footprints_failed": [],
                    "nets_assigned": [],
                    "nets_failed": [],
                }

                pcb.import_from_schematic("test.kicad_sch")

                mock_export.assert_called_once_with("test.kicad_sch")
                mock_import.assert_called_once()

    def test_import_from_schematic_passes_placement_params(self):
        """Test that placement parameters are passed through."""
        pcb = PCB.create(width=100, height=100)

        mock_netlist = Netlist()

        with patch("kicad_tools.operations.netlist.export_netlist", return_value=mock_netlist):
            with patch.object(pcb, "import_from_netlist") as mock_import:
                mock_import.return_value = {
                    "footprints_added": [],
                    "footprints_skipped": [],
                    "footprints_failed": [],
                    "nets_assigned": [],
                    "nets_failed": [],
                }

                pcb.import_from_schematic(
                    "test.kicad_sch",
                    placement_start=(20.0, 30.0),
                    placement_spacing=20.0,
                    columns=5,
                )

                mock_import.assert_called_once_with(
                    mock_netlist,
                    placement_start=(20.0, 30.0),
                    placement_spacing=20.0,
                    columns=5,
                )


class TestFromSchematic:
    """Tests for PCB.from_schematic() class method."""

    def test_from_schematic_creates_pcb_and_imports(self):
        """Test that from_schematic creates a PCB and imports from schematic."""
        mock_netlist = Netlist()
        mock_netlist.components = []
        mock_netlist.nets = []

        with patch("kicad_tools.operations.netlist.export_netlist", return_value=mock_netlist):
            pcb, stats = PCB.from_schematic(
                "test.kicad_sch",
                width=160,
                height=100,
                layers=4,
            )

            assert pcb is not None
            assert len(pcb.copper_layers) == 4
            assert isinstance(stats, dict)
            assert "footprints_added" in stats

    def test_from_schematic_passes_placement_params(self):
        """Test that placement parameters are passed through."""
        mock_netlist = Netlist()
        mock_netlist.components = []
        mock_netlist.nets = []

        with patch("kicad_tools.operations.netlist.export_netlist", return_value=mock_netlist):
            pcb, stats = PCB.from_schematic(
                "test.kicad_sch",
                width=200,
                height=150,
                layers=2,
                placement_start=(25.0, 25.0),
                placement_spacing=18.0,
                columns=8,
            )

            assert pcb is not None
            assert len(pcb.copper_layers) == 2

    def test_from_schematic_invalid_layers(self):
        """Test that invalid layer count raises ValueError."""
        with pytest.raises(ValueError, match="Layers must be 2 or 4"):
            PCB.from_schematic("test.kicad_sch", layers=3)


@pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")
class TestImportFromSchematicIntegration:
    """Integration tests that require kicad-cli."""

    @pytest.fixture
    def fixtures_dir(self) -> Path:
        """Return path to test fixtures directory."""
        return Path(__file__).parent / "fixtures"

    @pytest.fixture
    def simple_schematic(self, fixtures_dir: Path) -> Path:
        """Return path to simple RC schematic."""
        return fixtures_dir / "simple_rc.kicad_sch"

    @pytest.mark.slow
    def test_import_from_actual_schematic(self, simple_schematic: Path, tmp_path: Path):
        """Test importing from an actual schematic file."""
        if not simple_schematic.exists():
            pytest.skip(f"Test fixture not found: {simple_schematic}")

        pcb = PCB.create(width=100, height=100)

        # This will fail if footprints aren't in KiCad library, but tests the flow
        try:
            result = pcb.import_from_schematic(simple_schematic)

            # Check the result has expected keys
            assert "footprints_added" in result
            assert "footprints_skipped" in result
            assert "footprints_failed" in result
            assert "nets_assigned" in result
            assert "nets_failed" in result

        except FileNotFoundError as e:
            # Expected if KiCad libraries aren't installed
            if "footprint" in str(e).lower() or "library" in str(e).lower():
                pytest.skip(f"KiCad footprint libraries not installed: {e}")
            raise

    @pytest.mark.slow
    def test_from_schematic_creates_valid_pcb(self, simple_schematic: Path, tmp_path: Path):
        """Test that from_schematic creates a valid PCB file."""
        if not simple_schematic.exists():
            pytest.skip(f"Test fixture not found: {simple_schematic}")

        try:
            pcb, stats = PCB.from_schematic(simple_schematic, width=100, height=80, layers=2)

            # Save and reload to verify it's valid
            output_path = tmp_path / "test_output.kicad_pcb"
            pcb.save(output_path)

            assert output_path.exists()

            # Reload and verify basic structure
            reloaded = PCB.load(str(output_path))
            assert len(reloaded.copper_layers) == 2

        except FileNotFoundError as e:
            if "footprint" in str(e).lower() or "library" in str(e).lower():
                pytest.skip(f"KiCad footprint libraries not installed: {e}")
            raise
