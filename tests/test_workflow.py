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

    @staticmethod
    def _write_multipad_footprint(tmp_path: Path) -> Path:
        """Write a synthetic footprint with a same-numbered multi-pad group.

        Models the EP/thermal-via pattern (issue #4186):
        - pad "1": a single unique SMD pad
        - pad "19": a thermal group of 3 pads sharing the number, mixing
          smd (paste) and thru_hole (PTH via) types like a real paste+PTH EP
        - pad "": a mechanical / no-connect pad with an empty number that must
          never be netted
        """
        footprint = """\
(footprint "Multipad_Test"
	(version 20240108)
	(generator "pcbnew")
	(layer "F.Cu")
	(attr smd)
	(pad "1" smd roundrect
		(at -2 0)
		(size 1 1)
		(layers "F.Cu" "F.Paste" "F.Mask")
		(roundrect_rratio 0.25)
	)
	(pad "19" smd rect
		(at 0 0)
		(size 2 2)
		(layers "F.Cu" "F.Paste" "F.Mask")
	)
	(pad "19" thru_hole circle
		(at -0.5 -0.5)
		(size 0.6 0.6)
		(drill 0.3)
		(layers "*.Cu")
	)
	(pad "19" thru_hole circle
		(at 0.5 0.5)
		(size 0.6 0.6)
		(drill 0.3)
		(layers "*.Cu")
	)
	(pad "" np_thru_hole circle
		(at 3 0)
		(size 1.5 1.5)
		(drill 1.5)
		(layers "*.Cu" "*.Mask")
	)
)
"""
        mod_path = tmp_path / "Multipad_Test.kicad_mod"
        mod_path.write_text(footprint)
        return mod_path

    def test_multipad_group_all_pads_get_net_in_memory(self, tmp_path: Path):
        """Every same-numbered pad carries the net on the in-memory objects."""
        pcb = PCB.create(width=100, height=100)
        mod_path = self._write_multipad_footprint(tmp_path)
        pcb.add_footprint_from_file(str(mod_path), reference="U1", x=50, y=50)

        result = pcb.assign_net_to_footprint_pad("U1", "19", "GND")
        assert result is True

        fp = pcb.get_footprint("U1")
        assert fp is not None
        gnd = pcb.get_net_by_name("GND")
        assert gnd is not None

        pads_19 = [p for p in fp.pads if p.number == "19"]
        # The fixture defines three pads numbered "19".
        assert len(pads_19) == 3
        # ALL of them must carry the same net after assignment.
        for pad in pads_19:
            assert pad.net_number == gnd.number
            assert pad.net_name == "GND"

    def test_multipad_group_all_pads_get_net_in_sexp(self, tmp_path: Path):
        """Every serialized (pad "19") block carries the same net node."""
        pcb = PCB.create(width=100, height=100)
        mod_path = self._write_multipad_footprint(tmp_path)
        pcb.add_footprint_from_file(str(mod_path), reference="U1", x=50, y=50)

        pcb.assign_net_to_footprint_pad("U1", "19", "GND")
        gnd = pcb.get_net_by_name("GND")
        assert gnd is not None

        # Re-serialize the board and inspect the S-expression tree directly:
        # every (pad "19" ...) node must contain the same (net N "GND").
        fp_sexp = None
        for candidate in pcb._sexp.find_all("footprint"):
            for prop in candidate.find_all("property"):
                if prop.get_string(0) == "Reference" and prop.get_string(1) == "U1":
                    fp_sexp = candidate
            for fp_text in candidate.find_all("fp_text"):
                if fp_text.get_string(0) == "reference" and fp_text.get_string(1) == "U1":
                    fp_sexp = candidate
        assert fp_sexp is not None

        pad_19_sexps = [pad for pad in fp_sexp.find_all("pad") if pad.get_string(0) == "19"]
        assert len(pad_19_sexps) == 3
        for pad_sexp in pad_19_sexps:
            net_node = pad_sexp.find("net")
            assert net_node is not None, "same-numbered pad left with no net node"
            assert net_node.get_int(0) == gnd.number
            assert net_node.get_string(1) == "GND"

    def test_multipad_unique_pad_unaffected(self, tmp_path: Path):
        """A unique-numbered pad is assigned once and not touched by the group."""
        pcb = PCB.create(width=100, height=100)
        mod_path = self._write_multipad_footprint(tmp_path)
        pcb.add_footprint_from_file(str(mod_path), reference="U1", x=50, y=50)

        assert pcb.assign_net_to_footprint_pad("U1", "1", "SIG") is True
        pcb.assign_net_to_footprint_pad("U1", "19", "GND")

        fp = pcb.get_footprint("U1")
        assert fp is not None
        sig = pcb.get_net_by_name("SIG")
        assert sig is not None

        pads_1 = [p for p in fp.pads if p.number == "1"]
        assert len(pads_1) == 1
        assert pads_1[0].net_name == "SIG"
        assert pads_1[0].net_number == sig.number

    def test_multipad_empty_number_pad_stays_unnetted(self, tmp_path: Path):
        """The empty-number NC / mechanical pad is never netted."""
        pcb = PCB.create(width=100, height=100)
        mod_path = self._write_multipad_footprint(tmp_path)
        pcb.add_footprint_from_file(str(mod_path), reference="U1", x=50, y=50)

        pcb.assign_net_to_footprint_pad("U1", "1", "SIG")
        pcb.assign_net_to_footprint_pad("U1", "19", "GND")

        fp = pcb.get_footprint("U1")
        assert fp is not None
        nc_pads = [p for p in fp.pads if p.number == ""]
        assert len(nc_pads) == 1
        assert nc_pads[0].net_number == 0

        # And its serialized form carries no (net ...) node.
        fp_sexp = None
        for candidate in pcb._sexp.find_all("footprint"):
            for prop in candidate.find_all("property"):
                if prop.get_string(0) == "Reference" and prop.get_string(1) == "U1":
                    fp_sexp = candidate
            for fp_text in candidate.find_all("fp_text"):
                if fp_text.get_string(0) == "reference" and fp_text.get_string(1) == "U1":
                    fp_sexp = candidate
        assert fp_sexp is not None
        empty_pad_sexps = [pad for pad in fp_sexp.find_all("pad") if pad.get_string(0) == ""]
        assert len(empty_pad_sexps) == 1
        assert empty_pad_sexps[0].find("net") is None


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


class TestBoardSize:
    """Tests for PCB.board_size property."""

    def test_board_size_from_create(self):
        """Test board_size returns correct dimensions from PCB.create()."""
        pcb = PCB.create(width=65, height=56.5)
        w, h = pcb.board_size
        assert w == pytest.approx(65.0)
        assert h == pytest.approx(56.5)

    def test_board_size_large_board(self):
        """Test board_size with a larger board."""
        pcb = PCB.create(width=200, height=150)
        w, h = pcb.board_size
        assert w == pytest.approx(200.0)
        assert h == pytest.approx(150.0)

    def test_board_size_no_outline(self):
        """Test board_size returns (0,0) when no Edge.Cuts outline exists."""
        # Create a PCB and clear its graphics to simulate no outline
        pcb = PCB.create(width=100, height=100)
        pcb._graphics = []
        pcb._graphic_lines = []
        w, h = pcb.board_size
        assert w == 0.0
        assert h == 0.0


class TestPlaceAllComponentsBoardAware:
    """Tests for board-aware placement in place_all_components()."""

    def _make_workflow(self, tmp_path: Path, board_w: float, board_h: float):
        """Helper to create a workflow with a PCB of given dimensions."""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("(kicad_sch (version 20231120))")

        # Build a mock netlist with several components
        mock_netlist = MagicMock(spec=Netlist)
        mock_netlist.components = [
            NetlistComponent(
                reference=f"R{i}",
                value="10k",
                footprint="Resistor_SMD:R_0805_2012Metric",
                lib_id="Device:R",
            )
            for i in range(1, 16)  # 15 components
        ]
        mock_netlist.nets = []
        mock_netlist.get_component = lambda ref: next(
            (c for c in mock_netlist.components if c.reference == ref), None
        )

        workflow = PCBFromSchematic.__new__(PCBFromSchematic)
        workflow.schematic_path = sch_path
        workflow._netlist_path = tmp_path / "test.kicad_net"
        workflow._netlist = mock_netlist
        workflow._pcb = None
        workflow._components = None

        workflow.create_pcb(width=board_w, height=board_h)
        return workflow

    def test_auto_columns_narrow_board(self, tmp_path: Path):
        """On a narrow board, auto-columns prevents horizontal overflow."""
        workflow = self._make_workflow(tmp_path, board_w=50, board_h=40)

        calls: list[tuple[str, float, float]] = []
        original_add = workflow.add_component

        def capture_add(ref, x, y, **kwargs):
            calls.append((ref, x, y))
            return original_add(ref, x, y, **kwargs)

        with patch.object(PCB, "add_footprint", return_value=MagicMock()):
            with patch.object(workflow, "add_component", side_effect=capture_add):
                result = workflow.place_all_components(spacing=15.0)

        # Usable width = 50 - 3 (start margin) - 3 (end margin) = 44
        # columns = int(44 / 15) = 2
        # So max x = 3 + 1*15 = 18, well within 50
        assert result.success_count == 15

        # Verify all x positions stay within board width
        for ref, x, _y in calls:
            assert x < 50, f"Component {ref} placed at x={x}, exceeds board width 50"

    def test_auto_columns_wide_board(self, tmp_path: Path):
        """On a wide board, more columns fit."""
        workflow = self._make_workflow(tmp_path, board_w=200, board_h=100)

        with patch.object(PCB, "add_footprint", return_value=MagicMock()):
            result = workflow.place_all_components(spacing=15.0)

        assert result.success_count == 15

    def test_explicit_columns_override(self, tmp_path: Path):
        """Explicit columns parameter overrides auto-calculation."""
        workflow = self._make_workflow(tmp_path, board_w=200, board_h=100)

        with patch.object(PCB, "add_footprint", return_value=MagicMock()):
            result = workflow.place_all_components(spacing=15.0, columns=5)

        assert result.success_count == 15

    def test_explicit_start_position(self, tmp_path: Path):
        """Explicit start_x/start_y overrides margin-based defaults."""
        workflow = self._make_workflow(tmp_path, board_w=100, board_h=100)

        with patch.object(PCB, "add_footprint", return_value=MagicMock()):
            result = workflow.place_all_components(
                start_x=10.0, start_y=10.0, spacing=15.0, columns=5
            )

        assert result.success_count == 15

    def test_default_margin(self, tmp_path: Path):
        """Default margin of 3mm is applied when start positions are not given."""
        workflow = self._make_workflow(tmp_path, board_w=100, board_h=100)

        calls = []
        original_add = workflow.add_component

        def capture_add(ref, x, y, **kwargs):
            calls.append((ref, x, y))
            return original_add(ref, x, y, **kwargs)

        with patch.object(PCB, "add_footprint", return_value=MagicMock()):
            with patch.object(workflow, "add_component", side_effect=capture_add):
                workflow.place_all_components()

        # First component should be at (margin, margin) = (3.0, 3.0)
        assert calls[0][1] == pytest.approx(3.0)
        assert calls[0][2] == pytest.approx(3.0)

    def test_custom_margin(self, tmp_path: Path):
        """Custom margin value is respected."""
        workflow = self._make_workflow(tmp_path, board_w=100, board_h=100)

        calls = []
        original_add = workflow.add_component

        def capture_add(ref, x, y, **kwargs):
            calls.append((ref, x, y))
            return original_add(ref, x, y, **kwargs)

        with patch.object(PCB, "add_footprint", return_value=MagicMock()):
            with patch.object(workflow, "add_component", side_effect=capture_add):
                workflow.place_all_components(margin=5.0)

        # First component at (5.0, 5.0)
        assert calls[0][1] == pytest.approx(5.0)
        assert calls[0][2] == pytest.approx(5.0)

        # columns = int((100 - 5 - 5) / 15) = int(6.0) = 6
        # So the 7th component (index 6) should wrap to next row
        assert calls[6][1] == pytest.approx(5.0)  # x wraps back
        assert calls[6][2] == pytest.approx(5.0 + 15.0)  # y increments

    def _make_workflow_n(self, tmp_path: Path, board_w, board_h, n: int):
        """Helper to create a workflow with ``n`` resistor components."""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text("(kicad_sch (version 20231120))")

        mock_netlist = MagicMock(spec=Netlist)
        mock_netlist.components = [
            NetlistComponent(
                reference=f"R{i}",
                value="10k",
                footprint="Resistor_SMD:R_0805_2012Metric",
                lib_id="Device:R",
            )
            for i in range(1, n + 1)
        ]
        mock_netlist.nets = []
        mock_netlist.get_component = lambda ref: next(
            (c for c in mock_netlist.components if c.reference == ref), None
        )

        workflow = PCBFromSchematic.__new__(PCBFromSchematic)
        workflow.schematic_path = sch_path
        workflow._netlist_path = tmp_path / "test.kicad_net"
        workflow._netlist = mock_netlist
        workflow._pcb = None
        workflow._components = None

        workflow.create_pcb(width=board_w, height=board_h)
        return workflow

    def test_grid_stays_within_board_height(self, tmp_path: Path):
        """Regression for #3805: parts must not overflow past board height.

        63 parts on a 65x56 board at the default 15mm spacing previously
        reached y ~= 303mm (5x the board height) because rows were unbounded.
        """
        workflow = self._make_workflow_n(tmp_path, board_w=65, board_h=56, n=63)

        calls: list[tuple[str, float, float]] = []
        original_add = workflow.add_component

        def capture_add(ref, x, y, **kwargs):
            calls.append((ref, x, y))
            return original_add(ref, x, y, **kwargs)

        with patch.object(PCB, "add_footprint", return_value=MagicMock()):
            with patch.object(workflow, "add_component", side_effect=capture_add):
                result = workflow.place_all_components(spacing=15.0)

        assert result.success_count == 63
        # Every placed footprint origin must stay inside the board outline.
        for ref, x, y in calls:
            assert 0 <= x <= 65, f"{ref} x={x} outside board width 65"
            assert 0 <= y <= 56, f"{ref} y={y} outside board height 56"

    def test_overcrowded_board_warns_and_shrinks(self, tmp_path: Path):
        """When parts cannot fit at the requested spacing, a warning is recorded."""
        workflow = self._make_workflow_n(tmp_path, board_w=65, board_h=56, n=63)

        with patch.object(PCB, "add_footprint", return_value=MagicMock()):
            result = workflow.place_all_components(spacing=15.0)

        # 63 parts at 15mm cannot fit in 65x56 -> spacing auto-shrunk + warned.
        assert result.warnings, "expected a placement warning for overcrowded board"
        assert any("spacing" in w for w in result.warnings)

    def test_explicit_columns_not_height_bounded(self, tmp_path: Path):
        """Explicit columns preserves prior (unbounded) behavior for callers."""
        workflow = self._make_workflow_n(tmp_path, board_w=65, board_h=56, n=20)

        calls: list[tuple[str, float, float]] = []
        original_add = workflow.add_component

        def capture_add(ref, x, y, **kwargs):
            calls.append((ref, x, y))
            return original_add(ref, x, y, **kwargs)

        with patch.object(PCB, "add_footprint", return_value=MagicMock()):
            with patch.object(workflow, "add_component", side_effect=capture_add):
                result = workflow.place_all_components(spacing=15.0, columns=2)

        assert result.success_count == 20
        # With 2 explicit columns, 20 parts make 10 rows: y is NOT clamped,
        # confirming explicit-columns behavior is unchanged.
        max_y = max(y for _ref, _x, y in calls)
        assert max_y == pytest.approx(3.0 + 9 * 15.0)

    def test_single_part_fits(self, tmp_path: Path):
        """A single part is placed at the start margin."""
        workflow = self._make_workflow_n(tmp_path, board_w=65, board_h=56, n=1)

        calls: list[tuple[str, float, float]] = []
        original_add = workflow.add_component

        def capture_add(ref, x, y, **kwargs):
            calls.append((ref, x, y))
            return original_add(ref, x, y, **kwargs)

        with patch.object(PCB, "add_footprint", return_value=MagicMock()):
            with patch.object(workflow, "add_component", side_effect=capture_add):
                result = workflow.place_all_components(spacing=15.0)

        assert result.success_count == 1
        assert calls[0][1] == pytest.approx(3.0)
        assert calls[0][2] == pytest.approx(3.0)


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
