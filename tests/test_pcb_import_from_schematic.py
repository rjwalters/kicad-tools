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

                # The schematic is still passed positionally, but the export
                # byproduct must be routed to an explicit path outside the
                # schematic's own directory (#4763).
                mock_export.assert_called_once()
                args, kwargs = mock_export.call_args
                assert args == ("test.kicad_sch",)
                assert set(kwargs) == {"output_path"}
                output_path = Path(kwargs["output_path"])
                assert output_path.name == "test-netlist.kicad_net"
                assert output_path.parent != Path("test.kicad_sch").parent
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
        before = {p.name for p in simple_schematic.parent.iterdir()}

        # This will fail if footprints aren't in KiCad library, but tests the flow
        try:
            result = pcb.import_from_schematic(simple_schematic)

            # The export byproduct must not land in the tracked fixtures
            # directory (#4763).
            assert not list(simple_schematic.parent.glob("*-netlist.kicad_net"))
            assert {p.name for p in simple_schematic.parent.iterdir()} == before

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

        before = {p.name for p in simple_schematic.parent.iterdir()}

        try:
            pcb, stats = PCB.from_schematic(simple_schematic, width=100, height=80, layers=2)

            # The export byproduct must not land in the tracked fixtures
            # directory (#4763).
            assert not list(simple_schematic.parent.glob("*-netlist.kicad_net"))
            assert {p.name for p in simple_schematic.parent.iterdir()} == before

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


# Minimal flat schematic with two components (R1 + C1).
MINIMAL_SCHEMATIC = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402" (at 100 100 0) (effects (hide yes)))
  )
  (symbol
    (lib_id "Device:C")
    (at 120 100 0)
    (uuid "00000000-0000-0000-0000-000000000003")
    (property "Reference" "C1" (at 120 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100n" (at 120 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Capacitor_SMD:C_0402" (at 120 100 0) (effects (hide yes)))
  )
)
"""

# Minimal kicad-cli-shaped netlist matching MINIMAL_SCHEMATIC (R1 + C1).
FAKE_EXPORTED_NETLIST = """(export (version "E")
  (components
    (comp (ref "R1") (value "10k") (footprint "Resistor_SMD:R_0402"))
    (comp (ref "C1") (value "100n") (footprint "Capacitor_SMD:C_0402")))
  (nets
    (net (code "1") (name "/N1")
      (node (ref "R1") (pin "1"))
      (node (ref "C1") (pin "1")))))
"""


class TestNetlistByproductLocation:
    """``import_from_schematic`` must not litter the user's project dir (#4763).

    ``export_netlist`` defaults ``output_path`` to
    ``<schematic dir>/<stem>-netlist.kicad_net`` and never deletes it, so a
    bare call from ``PCB.import_from_schematic`` dropped an unrequested
    netlist file beside the user's schematic on every call where kicad-cli
    is installed -- which also rewrote the tracked
    ``tests/fixtures/simple_rc-netlist.kicad_net`` fixture on every test
    run.  These tests pin the explicit-temp-path threading.
    """

    @pytest.fixture
    def schematic(self, tmp_path: Path) -> Path:
        """A schematic in its own 'project' directory."""
        proj = tmp_path / "project"
        proj.mkdir()
        sch = proj / "test.kicad_sch"
        sch.write_text(MINIMAL_SCHEMATIC)
        return sch

    @pytest.fixture
    def fake_kicad_cli(self, monkeypatch) -> list[Path]:
        """Force the kicad-cli export path with a stubbed subprocess.

        The stub writes a real netlist file to whatever ``--output`` path it
        is handed, exactly like kicad-cli does -- so the byproduct lands
        wherever ``export_netlist`` decided it should, which is the behavior
        under test.  Returns the list of captured output paths.
        """
        import subprocess as _subprocess

        captured_outputs: list[Path] = []

        monkeypatch.setattr(
            "kicad_tools.operations.netlist.find_kicad_cli",
            lambda: Path("/fake/kicad-cli"),
        )

        def fake_run(cmd, *args, **kwargs):
            out = Path(cmd[cmd.index("--output") + 1])
            captured_outputs.append(out)
            out.write_text(FAKE_EXPORTED_NETLIST)
            return _subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("kicad_tools.operations.netlist.subprocess.run", fake_run)
        return captured_outputs

    def test_kicad_cli_path_is_actually_exercised(self, schematic: Path, fake_kicad_cli):
        """Guard: the stub really runs, so the assertions below mean something."""
        pcb = PCB.create(width=100, height=100)
        pcb.import_from_schematic(schematic)

        assert fake_kicad_cli, "kicad-cli export path was never taken"

    def test_no_byproduct_beside_schematic(self, schematic: Path, fake_kicad_cli):
        """No ``*-netlist.kicad_net`` is left in the schematic's directory."""
        pcb = PCB.create(width=100, height=100)
        before = {p.name for p in schematic.parent.iterdir()}

        pcb.import_from_schematic(schematic)

        stray = sorted(p.name for p in schematic.parent.glob("*-netlist.kicad_net"))
        assert not stray, f"import left byproducts beside the schematic: {stray}"
        assert {p.name for p in schematic.parent.iterdir()} == before

    def test_export_output_lands_outside_project_dir(self, schematic: Path, fake_kicad_cli):
        """The explicit output path is a temp location, not the project dir."""
        pcb = PCB.create(width=100, height=100)
        pcb.import_from_schematic(schematic)

        assert fake_kicad_cli
        for out in fake_kicad_cli:
            assert out.parent != schematic.parent, f"export wrote into the project dir: {out}"

    def test_temp_export_is_cleaned_up(self, schematic: Path, fake_kicad_cli):
        """The temp directory is torn down before ``import_from_schematic`` returns."""
        pcb = PCB.create(width=100, height=100)
        pcb.import_from_schematic(schematic)

        assert fake_kicad_cli
        for out in fake_kicad_cli:
            assert not out.exists(), f"temp netlist survived the import: {out}"
            assert not out.parent.exists(), f"temp dir survived the import: {out.parent}"

    def test_schematic_path_may_be_a_str(self, schematic: Path, fake_kicad_cli):
        """``schematic_path`` is typed ``str | Path`` -- ``.stem`` must not blow up."""
        pcb = PCB.create(width=100, height=100)
        before = {p.name for p in schematic.parent.iterdir()}

        pcb.import_from_schematic(str(schematic))

        assert fake_kicad_cli
        assert fake_kicad_cli[-1].name == "test-netlist.kicad_net"
        assert {p.name for p in schematic.parent.iterdir()} == before

    def test_netlist_data_survives_temp_routing(self, schematic: Path, fake_kicad_cli, monkeypatch):
        """``import_from_netlist`` still receives the fully parsed netlist."""
        pcb = PCB.create(width=100, height=100)

        captured = {}
        original = pcb.import_from_netlist

        def spy(netlist, **kwargs):
            captured["netlist"] = netlist
            return original(netlist, **kwargs)

        monkeypatch.setattr(pcb, "import_from_netlist", spy)
        pcb.import_from_schematic(schematic)

        netlist = captured["netlist"]
        assert {c.reference for c in netlist.components} == {"R1", "C1"}
        assert any(n.name.endswith("N1") for n in netlist.nets), (
            f"exported net missing after import: {[n.name for n in netlist.nets]}"
        )

    def test_from_schematic_leaves_no_byproduct(self, schematic: Path, fake_kicad_cli):
        """``PCB.from_schematic`` delegates to the fixed code path."""
        before = {p.name for p in schematic.parent.iterdir()}

        pcb, _stats = PCB.from_schematic(schematic, width=100, height=80, layers=2)

        assert pcb is not None
        assert fake_kicad_cli, "kicad-cli export path was never taken"
        assert not list(schematic.parent.glob("*-netlist.kicad_net"))
        assert {p.name for p in schematic.parent.iterdir()} == before
        for out in fake_kicad_cli:
            assert out.parent != schematic.parent
            assert not out.parent.exists()

    def test_python_fallback_writes_no_byproduct(self, schematic: Path, monkeypatch):
        """The pure-Python fallback path also leaves the project dir clean."""
        monkeypatch.setattr("kicad_tools.operations.netlist.find_kicad_cli", lambda: None)

        pcb = PCB.create(width=100, height=100)
        before = {p.name for p in schematic.parent.iterdir()}

        pcb.import_from_schematic(schematic)

        assert not list(schematic.parent.glob("*-netlist.kicad_net"))
        assert {p.name for p in schematic.parent.iterdir()} == before

    def test_export_failure_propagates_and_cleans_up(self, schematic: Path, monkeypatch):
        """A failing export still propagates, and the temp dir is removed."""
        seen: list[Path] = []

        def boom(sch_path, output_path=None, **kwargs):
            seen.append(Path(output_path))
            raise RuntimeError("kicad-cli failed")

        monkeypatch.setattr("kicad_tools.operations.netlist.export_netlist", boom)

        pcb = PCB.create(width=100, height=100)
        with pytest.raises(RuntimeError, match="kicad-cli failed"):
            pcb.import_from_schematic(schematic)

        assert seen, "export_netlist was never called"
        assert not seen[0].parent.exists(), f"temp dir survived the failure: {seen[0].parent}"
        assert not list(schematic.parent.glob("*-netlist.kicad_net"))
