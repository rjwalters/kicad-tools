"""Tests for PCB.create() functionality."""

from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB


class TestPCBCreate:
    """Tests for creating new PCBs from scratch."""

    def test_create_default_pcb(self):
        """Test creating a PCB with default parameters."""
        pcb = PCB.create()

        # Check layers exist
        assert len(pcb.layers) > 0
        assert len(pcb.copper_layers) == 2

        # Check that there is an Edge.Cuts graphic (gr_rect for outline)
        edge_cuts_graphics = list(pcb.graphics_on_layer("Edge.Cuts"))
        assert len(edge_cuts_graphics) > 0

    def test_create_2layer_pcb(self):
        """Test creating a 2-layer PCB."""
        pcb = PCB.create(width=160, height=100, layers=2)

        # Check copper layers
        assert len(pcb.copper_layers) == 2
        layer_names = [layer.name for layer in pcb.copper_layers]
        assert "F.Cu" in layer_names
        assert "B.Cu" in layer_names
        assert "In1.Cu" not in layer_names

    def test_create_4layer_pcb(self):
        """Test creating a 4-layer PCB."""
        pcb = PCB.create(width=160, height=100, layers=4)

        # Check copper layers
        assert len(pcb.copper_layers) == 4
        layer_names = [layer.name for layer in pcb.copper_layers]
        assert "F.Cu" in layer_names
        assert "In1.Cu" in layer_names
        assert "In2.Cu" in layer_names
        assert "B.Cu" in layer_names

        # Check stackup exists
        assert pcb.setup is not None
        assert len(pcb.setup.stackup) > 0

    def test_create_with_title_block(self):
        """Test creating a PCB with title block information."""
        pcb = PCB.create(
            width=100,
            height=80,
            title="Test Board",
            revision="2.0",
            company="Test Corp",
            board_date="2024-01-15",
        )

        assert pcb.title == "Test Board"
        assert pcb.revision == "2.0"
        assert pcb.date == "2024-01-15"

    def test_create_invalid_layers(self):
        """Test that invalid layer count raises ValueError."""
        with pytest.raises(ValueError, match="Layers must be 2 or 4"):
            PCB.create(layers=3)

        with pytest.raises(ValueError, match="Layers must be 2 or 4"):
            PCB.create(layers=6)

    def test_create_and_save(self, tmp_path: Path):
        """Test creating a PCB and saving it to a file."""
        pcb = PCB.create(
            width=150,
            height=100,
            layers=2,
            title="Save Test",
        )

        # Save to file
        output_path = tmp_path / "test_board.kicad_pcb"
        pcb.save(output_path)

        # Verify file was created
        assert output_path.exists()

        # Reload and verify
        reloaded = PCB.load(str(output_path))
        assert reloaded.title == "Save Test"
        assert len(reloaded.copper_layers) == 2

    def test_create_4layer_and_save(self, tmp_path: Path):
        """Test creating a 4-layer PCB and saving it to a file."""
        pcb = PCB.create(
            width=200,
            height=150,
            layers=4,
            title="4-Layer Test",
        )

        # Save to file
        output_path = tmp_path / "test_4layer.kicad_pcb"
        pcb.save(output_path)

        # Reload and verify
        reloaded = PCB.load(str(output_path))
        assert reloaded.title == "4-Layer Test"
        assert len(reloaded.copper_layers) == 4

        # Verify stackup was preserved
        assert reloaded.setup is not None
        assert len(reloaded.setup.stackup) > 0

    def test_create_has_empty_net(self):
        """Test that created PCB has the required empty net."""
        pcb = PCB.create()

        # Should have at least net 0 (empty net)
        assert 0 in pcb.nets
        assert pcb.nets[0].name == ""

    def test_create_has_edge_cuts(self):
        """Test that created PCB has Edge.Cuts layer."""
        pcb = PCB.create(width=100, height=50)

        # Should have Edge.Cuts in layers
        layer_names = [layer.name for layer in pcb.layers.values()]
        assert "Edge.Cuts" in layer_names

        # Should have a board outline graphic on Edge.Cuts
        # Note: gr_rect is parsed as BoardGraphic, not as individual lines
        edge_cuts = list(pcb.graphics_on_layer("Edge.Cuts"))
        assert len(edge_cuts) > 0

    def test_create_date_default(self):
        """Test that created PCB has today's date by default."""
        from datetime import date as dt_date

        pcb = PCB.create()

        # Date should be today
        expected_date = dt_date.today().isoformat()
        assert pcb.date == expected_date

    def test_create_custom_date(self):
        """Test creating a PCB with a custom date."""
        pcb = PCB.create(board_date="2023-06-15")
        assert pcb.date == "2023-06-15"

    def test_create_summary(self):
        """Test that created PCB summary method works."""
        pcb = PCB.create(
            width=100,
            height=100,
            layers=2,
            title="Summary Test",
        )

        summary = pcb.summary()
        assert summary["title"] == "Summary Test"
        assert summary["copper_layers"] == 2
        assert summary["footprints"] == 0
        assert summary["nets"] == 1  # Just the empty net
        assert summary["segments"] == 0
        assert summary["vias"] == 0
