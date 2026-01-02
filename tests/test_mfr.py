"""Tests for kicad_tools.manufacturers module."""

import pytest

from kicad_tools.manufacturers import (
    DesignRules,
    compare_design_rules,
    find_compatible_manufacturers,
    get_manufacturer_ids,
    get_profile,
    list_manufacturers,
)


class TestManufacturerProfiles:
    """Tests for manufacturer profile functions."""

    def test_list_manufacturers(self):
        """Test listing all manufacturers."""
        manufacturers = list_manufacturers()
        assert len(manufacturers) == 4

        names = {m.name for m in manufacturers}
        assert "JLCPCB" in names
        assert "Seeed Fusion" in names
        assert "PCBWay" in names
        assert "OSHPark" in names

    def test_get_manufacturer_ids(self):
        """Test getting manufacturer IDs."""
        ids = get_manufacturer_ids()
        assert "jlcpcb" in ids
        assert "seeed" in ids
        assert "pcbway" in ids
        assert "oshpark" in ids

    def test_get_profile_by_id(self):
        """Test getting a profile by ID."""
        profile = get_profile("jlcpcb")
        assert profile.id == "jlcpcb"
        assert profile.name == "JLCPCB"
        assert "jlcpcb.com" in profile.website

    def test_get_profile_with_alias(self):
        """Test getting a profile using an alias."""
        profile = get_profile("jlc")
        assert profile.id == "jlcpcb"

        profile = get_profile("osh")
        assert profile.id == "oshpark"

    def test_get_profile_invalid(self):
        """Test getting a profile with invalid ID."""
        with pytest.raises(ValueError, match="Unknown manufacturer"):
            get_profile("invalid_manufacturer")


class TestDesignRules:
    """Tests for design rules."""

    def test_jlcpcb_2layer_rules(self):
        """Test JLCPCB 2-layer design rules."""
        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=2, copper_oz=1.0)

        assert rules.min_trace_width_mm == pytest.approx(0.127)  # 5 mil
        assert rules.min_clearance_mm == pytest.approx(0.127)
        assert rules.min_via_drill_mm == pytest.approx(0.3)

    def test_jlcpcb_4layer_rules(self):
        """Test JLCPCB 4-layer design rules."""
        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=4, copper_oz=1.0)

        assert rules.min_trace_width_mm == pytest.approx(0.1016)  # 4 mil
        assert rules.min_via_drill_mm == pytest.approx(0.2)

    def test_jlcpcb_6layer_rules(self):
        """Test JLCPCB 6-layer design rules."""
        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=6, copper_oz=1.0)

        assert rules.min_trace_width_mm == pytest.approx(0.0889)  # 3.5 mil
        assert rules.min_clearance_mm == pytest.approx(0.0889)
        assert rules.min_via_drill_mm == pytest.approx(0.2)

    def test_pcbway_6layer_rules(self):
        """Test PCBWay 6-layer design rules."""
        profile = get_profile("pcbway")
        rules = profile.get_design_rules(layers=6, copper_oz=1.0)

        assert rules.min_trace_width_mm == pytest.approx(0.0889)  # 3.5 mil
        assert rules.min_clearance_mm == pytest.approx(0.0889)
        assert rules.min_via_drill_mm == pytest.approx(0.15)

    def test_seeed_6layer_rules(self):
        """Test Seeed 6-layer design rules."""
        profile = get_profile("seeed")
        rules = profile.get_design_rules(layers=6, copper_oz=1.0)

        assert rules.min_trace_width_mm == pytest.approx(0.127)  # 5 mil
        assert rules.min_clearance_mm == pytest.approx(0.127)
        assert rules.min_via_drill_mm == pytest.approx(0.25)

    def test_rules_to_dict(self):
        """Test converting rules to dictionary."""
        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=2)
        d = rules.to_dict()

        assert "min_trace_width_mm" in d
        assert "min_clearance_mm" in d
        assert "min_via_drill_mm" in d

    def test_compare_design_rules(self):
        """Test comparing design rules across manufacturers."""
        comparison = compare_design_rules(layers=4, copper_oz=1.0)

        assert "jlcpcb" in comparison
        assert "seeed" in comparison
        assert isinstance(comparison["jlcpcb"], DesignRules)


class TestAssembly:
    """Tests for assembly capabilities."""

    def test_jlcpcb_supports_assembly(self):
        """Test that JLCPCB supports assembly."""
        profile = get_profile("jlcpcb")
        assert profile.supports_assembly()
        assert profile.assembly is not None

    def test_oshpark_no_assembly(self):
        """Test that OSHPark doesn't support assembly."""
        profile = get_profile("oshpark")
        assert not profile.supports_assembly()
        assert profile.assembly is None


class TestPartsLibrary:
    """Tests for parts library."""

    def test_jlcpcb_lcsc_library(self):
        """Test JLCPCB LCSC parts library."""
        profile = get_profile("jlcpcb")
        assert profile.parts_library is not None
        assert profile.parts_library.name == "LCSC"

        url = profile.get_part_search_url("C123456")
        assert "lcsc.com" in url
        assert "C123456" in url

    def test_oshpark_no_library(self):
        """Test that OSHPark has no parts library."""
        profile = get_profile("oshpark")
        assert profile.parts_library is None
        assert profile.get_part_search_url("any") is None


class TestCompatibleManufacturers:
    """Tests for finding compatible manufacturers."""

    def test_find_compatible_conservative_design(self):
        """Test finding compatible manufacturers for conservative design."""
        # Very conservative design rules (6mil/6mil, 0.3mm via)
        compatible = find_compatible_manufacturers(
            trace_width_mm=0.1524,  # 6 mil
            clearance_mm=0.1524,
            via_drill_mm=0.3,
            layers=2,
            needs_assembly=False,
        )

        # All manufacturers should support this
        assert len(compatible) >= 3

    def test_find_compatible_aggressive_design(self):
        """Test finding compatible manufacturers for aggressive design."""
        # Aggressive but achievable design rules (4mil/4mil)
        compatible = find_compatible_manufacturers(
            trace_width_mm=0.1016,  # 4 mil
            clearance_mm=0.1016,
            via_drill_mm=0.2,
            layers=6,
            needs_assembly=False,
        )

        # JLCPCB and PCBWay support this
        assert len(compatible) >= 1

    def test_find_compatible_with_assembly(self):
        """Test finding manufacturers that support assembly."""
        compatible = find_compatible_manufacturers(
            trace_width_mm=0.2,
            clearance_mm=0.2,
            via_drill_mm=0.3,
            layers=2,
            needs_assembly=True,
        )

        # OSHPark should not be in the list (no assembly)
        ids = {m.id for m in compatible}
        assert "oshpark" not in ids


class TestProjectFile:
    """Tests for project file handling."""

    def test_apply_manufacturer_rules(self):
        """Test applying manufacturer rules to project data."""
        from kicad_tools.core.project_file import (
            apply_manufacturer_rules,
            get_design_settings,
        )

        data = {}
        apply_manufacturer_rules(
            data,
            min_clearance_mm=0.127,
            min_track_width_mm=0.127,
            min_via_diameter_mm=0.6,
            min_via_drill_mm=0.3,
            min_annular_ring_mm=0.15,
        )

        settings = get_design_settings(data)
        assert "rules" in settings
        assert settings["rules"]["min_clearance"] == 0.127
        assert settings["rules"]["min_track_width"] == 0.127
        assert settings["rules"]["min_via_diameter"] == 0.6

    def test_set_manufacturer_metadata(self):
        """Test setting manufacturer metadata."""
        from kicad_tools.core.project_file import (
            get_manufacturer_metadata,
            set_manufacturer_metadata,
        )

        data = {}
        set_manufacturer_metadata(
            data,
            manufacturer_id="jlcpcb",
            layers=4,
            copper_oz=1.0,
        )

        meta = get_manufacturer_metadata(data)
        assert meta["manufacturer"] == "jlcpcb"
        assert meta["layers"] == 4
        assert meta["copper_oz"] == 1.0

    def test_save_and_load_project(self, tmp_path):
        """Test saving and loading project file."""
        from kicad_tools.core.project_file import (
            apply_manufacturer_rules,
            load_project,
            save_project,
            set_manufacturer_metadata,
        )

        data = {"project": {"name": "test"}}
        apply_manufacturer_rules(
            data,
            min_clearance_mm=0.1016,
            min_track_width_mm=0.1016,
            min_via_diameter_mm=0.45,
            min_via_drill_mm=0.2,
            min_annular_ring_mm=0.125,
        )
        set_manufacturer_metadata(data, "jlcpcb", layers=4)

        # Save
        project_file = tmp_path / "test.kicad_pro"
        save_project(data, project_file)
        assert project_file.exists()

        # Load
        loaded = load_project(project_file)
        assert loaded["board"]["design_settings"]["rules"]["min_clearance"] == 0.1016
        assert loaded["meta"]["manufacturer"] == "jlcpcb"


class TestDRUFiles:
    """Tests for .kicad_dru design rules files."""

    def test_jlcpcb_6layer_dru_exists(self):
        """Test that JLCPCB 6-layer DRU file exists."""
        from pathlib import Path

        dru_path = (
            Path(__file__).parent.parent
            / "src/kicad_tools/manufacturers/rules/jlcpcb-6layer-1oz.kicad_dru"
        )
        assert dru_path.exists(), f"DRU file not found: {dru_path}"

    def test_pcbway_6layer_dru_exists(self):
        """Test that PCBWay 6-layer DRU file exists."""
        from pathlib import Path

        dru_path = (
            Path(__file__).parent.parent
            / "src/kicad_tools/manufacturers/rules/pcbway-6layer-1oz.kicad_dru"
        )
        assert dru_path.exists(), f"DRU file not found: {dru_path}"

    def test_seeed_6layer_dru_exists(self):
        """Test that Seeed 6-layer DRU file exists."""
        from pathlib import Path

        dru_path = (
            Path(__file__).parent.parent
            / "src/kicad_tools/manufacturers/rules/seeed-6layer-1oz.kicad_dru"
        )
        assert dru_path.exists(), f"DRU file not found: {dru_path}"

    def test_all_6layer_dru_files_valid(self):
        """Test that all 6-layer DRU files are valid KiCad format."""
        from pathlib import Path

        from kicad_tools.core.sexp_file import load_design_rules

        rules_dir = Path(__file__).parent.parent / "src/kicad_tools/manufacturers/rules"
        dru_files = [
            "jlcpcb-6layer-1oz.kicad_dru",
            "pcbway-6layer-1oz.kicad_dru",
            "seeed-6layer-1oz.kicad_dru",
        ]

        for dru_file in dru_files:
            dru_path = rules_dir / dru_file
            sexp = load_design_rules(dru_path)
            assert sexp.tag == "design_rules", f"{dru_file} is not valid design rules"

            # Check version
            version = sexp.find_child("version")
            assert version is not None, f"{dru_file} missing version"
            assert version.values[0] == 1, f"{dru_file} has wrong version"

            # Check for 8 rules (standard set)
            rules = sexp.find_children("rule")
            assert len(rules) == 8, f"{dru_file} should have 8 rules, has {len(rules)}"


class TestMfrCLICommands:
    """Tests for mfr CLI commands."""

    def test_apply_rules_dry_run(self, tmp_path):
        """Test apply-rules command with dry-run."""
        import shutil
        from pathlib import Path

        from kicad_tools.cli.mfr import main as mfr_main

        # Copy test file to temp directory
        src_pcb = Path("demo/usb_joystick/usb_joystick.kicad_pcb")
        if not src_pcb.exists():
            pytest.skip("Demo PCB file not found")

        test_pcb = tmp_path / "test.kicad_pcb"
        shutil.copy(src_pcb, test_pcb)

        # Run apply-rules with dry-run
        result = mfr_main(["apply-rules", str(test_pcb), "jlcpcb", "--dry-run"])

        # Should return 0 (success) on dry-run
        assert result is None  # main() doesn't return anything on success

    def test_validate_command(self, tmp_path):
        """Test validate command."""
        from pathlib import Path

        from kicad_tools.cli.mfr import main as mfr_main

        src_pcb = Path("demo/usb_joystick/usb_joystick_routed.kicad_pcb")
        if not src_pcb.exists():
            pytest.skip("Demo PCB file not found")

        # Run validate - should pass without violations
        try:
            mfr_main(["validate", str(src_pcb), "jlcpcb"])
        except SystemExit as e:
            # Should exit with 0 (success) if no violations
            assert e.code == 0 or e.code is None

    def test_validate_pcb_design(self):
        """Test PCB design validation function."""
        from kicad_tools.cli.mfr import _validate_pcb_design
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.sexp import SExp

        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=2)

        # Create a simple PCB sexp with a thin trace
        sexp = SExp.list(
            "kicad_pcb",
            SExp.list(
                "segment",
                SExp.list("width", 0.05),  # 0.05mm, below minimum
            ),
        )

        violations = _validate_pcb_design(sexp, rules)

        # Should find the trace width violation
        assert len(violations) > 0
        assert any("TRACE_WIDTH" in v[0] for v in violations)


class TestTwoLayerRules:
    """Tests for 2-layer design rules across all manufacturers."""

    def test_jlcpcb_2layer_1oz_rules(self):
        """Test JLCPCB 2-layer 1oz design rules."""
        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=2, copper_oz=1.0)

        assert rules.min_trace_width_mm == pytest.approx(0.127)  # 5 mil
        assert rules.min_clearance_mm == pytest.approx(0.127)
        assert rules.min_via_drill_mm == pytest.approx(0.3)
        assert rules.min_via_diameter_mm == pytest.approx(0.6)
        assert rules.min_annular_ring_mm == pytest.approx(0.15)
        assert rules.inner_copper_oz == 0.0  # No inner layers

    def test_jlcpcb_2layer_2oz_rules(self):
        """Test JLCPCB 2-layer 2oz design rules."""
        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=2, copper_oz=2.0)

        assert rules.min_trace_width_mm == pytest.approx(0.2032)  # 8 mil for 2oz
        assert rules.min_clearance_mm == pytest.approx(0.2032)
        assert rules.outer_copper_oz == 2.0

    def test_seeed_2layer_1oz_rules(self):
        """Test Seeed 2-layer 1oz design rules."""
        profile = get_profile("seeed")
        rules = profile.get_design_rules(layers=2, copper_oz=1.0)

        assert rules.min_trace_width_mm == pytest.approx(0.1524)  # 6 mil
        assert rules.min_clearance_mm == pytest.approx(0.1524)
        assert rules.min_via_drill_mm == pytest.approx(0.3)
        assert rules.min_copper_to_edge_mm == pytest.approx(0.5)

    def test_pcbway_2layer_1oz_rules(self):
        """Test PCBWay 2-layer 1oz design rules."""
        profile = get_profile("pcbway")
        rules = profile.get_design_rules(layers=2, copper_oz=1.0)

        assert rules.min_trace_width_mm == pytest.approx(0.127)  # 5 mil
        assert rules.min_clearance_mm == pytest.approx(0.127)
        assert rules.min_via_drill_mm == pytest.approx(0.2)  # Smaller than JLCPCB
        assert rules.min_via_diameter_mm == pytest.approx(0.4)

    def test_oshpark_2layer_rules(self):
        """Test OSHPark 2-layer standard design rules."""
        profile = get_profile("oshpark")
        rules = profile.get_design_rules(layers=2, copper_oz=1.0)

        assert rules.min_trace_width_mm == pytest.approx(0.1524)  # 6 mil
        assert rules.min_clearance_mm == pytest.approx(0.1524)
        assert rules.min_via_drill_mm == pytest.approx(0.254)  # 10 mil
        assert rules.min_via_diameter_mm == pytest.approx(0.508)  # 20 mil

    def test_all_manufacturers_have_2layer_rules(self):
        """Test that all manufacturers have 2-layer rules defined."""
        for profile in list_manufacturers():
            if 2 in profile.supported_layers:
                rules = profile.get_design_rules(layers=2, copper_oz=1.0)
                assert rules is not None
                assert rules.min_trace_width_mm > 0
                assert rules.min_clearance_mm > 0
                assert rules.inner_copper_oz == 0.0  # 2-layer has no inner layers


class TestDRUFiles2Layer:
    """Tests for 2-layer .kicad_dru rule files."""

    def test_jlcpcb_2layer_1oz_dru_exists(self):
        """Test that JLCPCB 2-layer 1oz .kicad_dru file exists."""
        from pathlib import Path

        rules_dir = Path(__file__).parent.parent / "src/kicad_tools/manufacturers/rules"
        dru_file = rules_dir / "jlcpcb-2layer-1oz.kicad_dru"
        assert dru_file.exists(), f"Missing: {dru_file}"

    def test_jlcpcb_2layer_2oz_dru_exists(self):
        """Test that JLCPCB 2-layer 2oz .kicad_dru file exists."""
        from pathlib import Path

        rules_dir = Path(__file__).parent.parent / "src/kicad_tools/manufacturers/rules"
        dru_file = rules_dir / "jlcpcb-2layer-2oz.kicad_dru"
        assert dru_file.exists(), f"Missing: {dru_file}"

    def test_seeed_2layer_1oz_dru_exists(self):
        """Test that Seeed 2-layer 1oz .kicad_dru file exists."""
        from pathlib import Path

        rules_dir = Path(__file__).parent.parent / "src/kicad_tools/manufacturers/rules"
        dru_file = rules_dir / "seeed-2layer-1oz.kicad_dru"
        assert dru_file.exists(), f"Missing: {dru_file}"

    def test_pcbway_2layer_1oz_dru_exists(self):
        """Test that PCBWay 2-layer 1oz .kicad_dru file exists."""
        from pathlib import Path

        rules_dir = Path(__file__).parent.parent / "src/kicad_tools/manufacturers/rules"
        dru_file = rules_dir / "pcbway-2layer-1oz.kicad_dru"
        assert dru_file.exists(), f"Missing: {dru_file}"

    def test_oshpark_2layer_dru_exists(self):
        """Test that OSHPark 2-layer .kicad_dru file exists."""
        from pathlib import Path

        rules_dir = Path(__file__).parent.parent / "src/kicad_tools/manufacturers/rules"
        dru_file = rules_dir / "oshpark-2layer.kicad_dru"
        assert dru_file.exists(), f"Missing: {dru_file}"

    def test_all_2layer_dru_files_valid(self):
        """Test that all 2-layer .kicad_dru files are valid."""
        from pathlib import Path

        rules_dir = Path(__file__).parent.parent / "src/kicad_tools/manufacturers/rules"
        two_layer_files = list(rules_dir.glob("*-2layer*.kicad_dru"))

        assert len(two_layer_files) >= 5, "Expected at least 5 2-layer DRU files"

        for dru_file in two_layer_files:
            content = dru_file.read_text()
            # Check basic structure
            assert "(version 1)" in content, f"{dru_file.name}: Missing version"
            assert "(rule" in content, f"{dru_file.name}: Missing rules"
            assert "track_width" in content, f"{dru_file.name}: Missing track_width"
            assert "clearance" in content, f"{dru_file.name}: Missing clearance"
            assert "hole_size" in content, f"{dru_file.name}: Missing hole_size"

    def test_dru_file_values_match_python_rules(self):
        """Test that .kicad_dru values match Python profile values."""
        import re
        from pathlib import Path

        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=2, copper_oz=1.0)

        rules_dir = Path(__file__).parent.parent / "src/kicad_tools/manufacturers/rules"
        dru_file = rules_dir / "jlcpcb-2layer-1oz.kicad_dru"
        content = dru_file.read_text()

        # Extract track_width value from DRU file
        match = re.search(r"track_width \(min ([\d.]+)mm\)", content)
        assert match, "Could not find track_width in DRU file"
        dru_trace_width = float(match.group(1))

        assert dru_trace_width == pytest.approx(rules.min_trace_width_mm), (
            f"Track width mismatch: DRU={dru_trace_width}, Python={rules.min_trace_width_mm}"
        )
