"""Tests for the netlist-to-PCB workflow module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.operations.netlist import Netlist, NetlistComponent, NetlistNet, NetNode
from kicad_tools.schema.pcb import PCB
from kicad_tools.workflow import (
    ComponentInfo,
    NetAssignmentResult,
    PCBFromSchematic,
    PlacementResult,
    create_pcb_from_schematic,
)


class TestPCBNetManagement:
    """Tests for PCB.add_net() and related methods."""

    def test_add_net_creates_new_net(self):
        """Test that add_net creates a new net with correct number."""
        pcb = PCB.create(width=100, height=100)

        # Should start with net 0 (empty net)
        assert 0 in pcb.nets
        assert pcb.net_count == 1

        # Add a new net
        gnd = pcb.add_net("GND")
        assert gnd.number == 1
        assert gnd.name == "GND"
        assert pcb.net_count == 2

    def test_add_net_returns_existing(self):
        """Test that add_net returns existing net if name matches."""
        pcb = PCB.create(width=100, height=100)

        # Add a net
        gnd1 = pcb.add_net("GND")
        assert gnd1.number == 1

        # Adding same name should return existing net
        gnd2 = pcb.add_net("GND")
        assert gnd2.number == gnd1.number
        assert pcb.net_count == 2  # Still just 2 nets

    def test_add_multiple_nets(self):
        """Test adding multiple nets."""
        pcb = PCB.create(width=100, height=100)

        pcb.add_net("GND")
        pcb.add_net("+3V3")
        pcb.add_net("+5V")
        pcb.add_net("SDA")
        pcb.add_net("SCL")

        assert pcb.net_count == 6  # 5 new + empty net

        # Verify we can look them up
        assert pcb.get_net_by_name("GND") is not None
        assert pcb.get_net_by_name("+3V3") is not None
        assert pcb.get_net_by_name("SDA") is not None

    def test_add_net_persists_in_sexp(self, tmp_path: Path):
        """Test that added nets are saved and can be reloaded."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_net("TestNet1")
        pcb.add_net("TestNet2")

        # Save and reload
        output_path = tmp_path / "test_nets.kicad_pcb"
        pcb.save(output_path)

        reloaded = PCB.load(str(output_path))
        assert reloaded.net_count == 3  # empty + 2 new
        assert reloaded.get_net_by_name("TestNet1") is not None
        assert reloaded.get_net_by_name("TestNet2") is not None


class TestPCBNetAssignment:
    """Tests for assigning nets to footprint pads."""

    def test_assign_net_to_footprint_pad_returns_false_for_missing_footprint(self):
        """Test that assigning net to non-existent footprint returns False."""
        pcb = PCB.create(width=100, height=100)
        result = pcb.assign_net_to_footprint_pad("U1", "1", "GND")
        assert result is False

    def test_assign_nets_from_netlist_handles_missing_footprints(self):
        """Test that assign_nets_from_netlist tracks missing footprints."""
        pcb = PCB.create(width=100, height=100)

        # Create a mock netlist with components
        netlist = MagicMock(spec=Netlist)
        netlist.nets = [
            NetlistNet(
                code=1,
                name="GND",
                nodes=[NetNode(reference="U1", pin="1")],
            ),
        ]

        result = pcb.assign_nets_from_netlist(netlist)

        assert "U1" in result["missing_footprints"]
        assert len(result["assigned"]) == 0


class TestComponentInfo:
    """Tests for ComponentInfo dataclass."""

    def test_from_netlist_component(self):
        """Test creating ComponentInfo from NetlistComponent."""
        comp = NetlistComponent(
            reference="C1",
            value="100nF",
            footprint="Capacitor_SMD:C_0805_2012Metric",
            lib_id="Device:C",
        )

        # Create a mock netlist
        netlist = MagicMock(spec=Netlist)
        netlist.nets = [
            NetlistNet(
                code=1,
                name="GND",
                nodes=[NetNode(reference="C1", pin="1")],
            ),
            NetlistNet(
                code=2,
                name="+3V3",
                nodes=[NetNode(reference="C1", pin="2")],
            ),
        ]

        info = ComponentInfo.from_netlist_component(comp, netlist)

        assert info.reference == "C1"
        assert info.value == "100nF"
        assert info.footprint == "Capacitor_SMD:C_0805_2012Metric"
        assert "1" in info.pins
        assert "2" in info.pins
        assert info.nets["1"] == "GND"
        assert info.nets["2"] == "+3V3"


class TestPlacementResult:
    """Tests for PlacementResult dataclass."""

    def test_success_count(self):
        """Test success_count property."""
        result = PlacementResult(
            placed=["U1", "C1", "R1"],
            failed=[("C2", "No footprint")],
        )
        assert result.success_count == 3
        assert result.failure_count == 1


class TestNetAssignmentResult:
    """Tests for NetAssignmentResult dataclass."""

    def test_success_count(self):
        """Test success_count property."""
        result = NetAssignmentResult(
            assigned=["U1.1", "U1.2", "C1.1"],
            missing_footprints=["U2"],
            missing_pads=["R1.3"],
        )
        assert result.success_count == 3


class TestPCBFromSchematic:
    """Tests for PCBFromSchematic workflow class."""

    def test_init_with_missing_schematic(self):
        """Test that init raises FileNotFoundError for missing schematic."""
        with pytest.raises(FileNotFoundError):
            PCBFromSchematic("/nonexistent/path/schematic.kicad_sch")

    def test_create_pcb(self, tmp_path: Path):
        """Test create_pcb method."""
        # Create a mock schematic file
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("(kicad_sch (version 20231120))")

        with patch.object(
            PCBFromSchematic, "netlist", new_callable=lambda: property(lambda self: MagicMock())
        ):
            workflow = PCBFromSchematic(sch_path)
            pcb = workflow.create_pcb(width=150, height=100, layers=4)

            assert pcb is not None
            assert len(pcb.copper_layers) == 4
            assert workflow.pcb is pcb

    def test_create_pcb_default_title(self, tmp_path: Path):
        """Test that create_pcb uses schematic name as default title."""
        sch_path = tmp_path / "my_project.kicad_sch"
        sch_path.write_text("(kicad_sch (version 20231120))")

        with patch.object(
            PCBFromSchematic, "netlist", new_callable=lambda: property(lambda self: MagicMock())
        ):
            workflow = PCBFromSchematic(sch_path)
            pcb = workflow.create_pcb()

            assert pcb.title == "my_project"

    def test_add_component_without_pcb_raises(self, tmp_path: Path):
        """Test that add_component raises ValueError if no PCB created."""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("(kicad_sch (version 20231120))")

        with patch.object(
            PCBFromSchematic, "netlist", new_callable=lambda: property(lambda self: MagicMock())
        ):
            workflow = PCBFromSchematic(sch_path)

            with pytest.raises(ValueError, match="No PCB created"):
                workflow.add_component("U1", 50, 50)

    def test_assign_nets_without_pcb_raises(self, tmp_path: Path):
        """Test that assign_nets raises ValueError if no PCB created."""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("(kicad_sch (version 20231120))")

        with patch.object(
            PCBFromSchematic, "netlist", new_callable=lambda: property(lambda self: MagicMock())
        ):
            workflow = PCBFromSchematic(sch_path)

            with pytest.raises(ValueError, match="No PCB created"):
                workflow.assign_nets()

    def test_save_without_pcb_raises(self, tmp_path: Path):
        """Test that save raises ValueError if no PCB created."""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("(kicad_sch (version 20231120))")

        with patch.object(
            PCBFromSchematic, "netlist", new_callable=lambda: property(lambda self: MagicMock())
        ):
            workflow = PCBFromSchematic(sch_path)

            with pytest.raises(ValueError, match="No PCB created"):
                workflow.save(tmp_path / "output.kicad_pcb")

    def test_summary(self, tmp_path: Path):
        """Test summary method."""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("(kicad_sch (version 20231120))")

        # Create mock netlist
        mock_netlist = MagicMock(spec=Netlist)
        mock_netlist.components = [
            NetlistComponent(
                reference="U1",
                value="STM32",
                footprint="Package_QFP:LQFP-48",
                lib_id="MCU",
            ),
        ]
        mock_netlist.nets = [
            NetlistNet(code=1, name="GND", nodes=[]),
            NetlistNet(code=2, name="+3V3", nodes=[]),
        ]
        mock_netlist.power_nets = [mock_netlist.nets[0]]

        with patch.object(
            PCBFromSchematic,
            "netlist",
            new_callable=lambda: property(lambda self: mock_netlist),
        ):
            workflow = PCBFromSchematic(sch_path)
            summary = workflow.summary()

            assert summary["schematic"] == str(sch_path)
            assert summary["component_count"] == 1
            assert summary["net_count"] == 2
            assert summary["power_net_count"] == 1
            assert summary["pcb_created"] is False

            # Create PCB and check summary updates
            workflow.create_pcb()
            summary_after = workflow.summary()
            assert summary_after["pcb_created"] is True


class TestCreatePCBFromSchematic:
    """Tests for create_pcb_from_schematic convenience function."""

    def test_returns_pcb(self, tmp_path: Path):
        """Test that function returns a PCB object."""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("(kicad_sch (version 20231120))")

        # Mock the workflow
        with patch("kicad_tools.workflow.PCBFromSchematic") as MockWorkflow:
            mock_workflow = MagicMock()
            mock_pcb = PCB.create(width=100, height=100)
            mock_workflow.pcb = mock_pcb
            mock_workflow.create_pcb.return_value = mock_pcb
            mock_workflow.place_all_components.return_value = PlacementResult()
            mock_workflow.assign_nets.return_value = NetAssignmentResult()
            MockWorkflow.return_value = mock_workflow

            pcb = create_pcb_from_schematic(sch_path)

            assert pcb is mock_pcb
            mock_workflow.create_pcb.assert_called_once()
            mock_workflow.place_all_components.assert_called_once()
            mock_workflow.assign_nets.assert_called_once()

    def test_passes_parameters(self, tmp_path: Path):
        """Test that parameters are passed correctly."""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("(kicad_sch (version 20231120))")

        with patch("kicad_tools.workflow.PCBFromSchematic") as MockWorkflow:
            mock_workflow = MagicMock()
            mock_pcb = PCB.create(width=160, height=100, layers=4)
            mock_workflow.pcb = mock_pcb
            mock_workflow.create_pcb.return_value = mock_pcb
            mock_workflow.place_all_components.return_value = PlacementResult()
            mock_workflow.assign_nets.return_value = NetAssignmentResult()
            MockWorkflow.return_value = mock_workflow

            create_pcb_from_schematic(
                sch_path,
                board_size=(160, 100),
                layers=4,
                title="My Board",
                revision="2.0",
                company="Test Corp",
            )

            mock_workflow.create_pcb.assert_called_once_with(
                width=160,
                height=100,
                layers=4,
                title="My Board",
                revision="2.0",
                company="Test Corp",
            )

    def test_auto_place_false_skips_placement(self, tmp_path: Path):
        """Test that auto_place=False skips component placement."""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("(kicad_sch (version 20231120))")

        with patch("kicad_tools.workflow.PCBFromSchematic") as MockWorkflow:
            mock_workflow = MagicMock()
            mock_pcb = PCB.create(width=100, height=100)
            mock_workflow.pcb = mock_pcb
            mock_workflow.create_pcb.return_value = mock_pcb
            mock_workflow.assign_nets.return_value = NetAssignmentResult()
            MockWorkflow.return_value = mock_workflow

            create_pcb_from_schematic(sch_path, auto_place=False)

            mock_workflow.place_all_components.assert_not_called()
            mock_workflow.assign_nets.assert_called_once()


class TestWorkflowExports:
    """Tests for workflow module exports."""

    def test_all_exports_accessible(self):
        """Test that all documented exports are accessible."""
        from kicad_tools import workflow

        assert hasattr(workflow, "PCBFromSchematic")
        assert hasattr(workflow, "ComponentInfo")
        assert hasattr(workflow, "PlacementResult")
        assert hasattr(workflow, "NetAssignmentResult")
        assert hasattr(workflow, "create_pcb_from_schematic")
