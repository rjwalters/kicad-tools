"""Tests for kicad_tools.manufacturers module."""

import pytest
from kicad_tools.manufacturers import (
    get_profile,
    list_manufacturers,
    get_manufacturer_ids,
    find_compatible_manufacturers,
    compare_design_rules,
    DesignRules,
    ManufacturerProfile,
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
            set_manufacturer_metadata,
            get_manufacturer_metadata,
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


class TestMfrCLICommands:
    """Tests for mfr CLI commands."""

    def test_apply_rules_dry_run(self, tmp_path):
        """Test apply-rules command with dry-run."""
        from kicad_tools.cli.mfr import main as mfr_main
        from pathlib import Path
        import shutil

        # Copy test file to temp directory
        src_pcb = Path("demo/usb_joystick/usb_joystick.kicad_pcb")
        if not src_pcb.exists():
            pytest.skip("Demo PCB file not found")

        test_pcb = tmp_path / "test.kicad_pcb"
        shutil.copy(src_pcb, test_pcb)

        # Run apply-rules with dry-run
        result = mfr_main([
            "apply-rules", str(test_pcb), "jlcpcb", "--dry-run"
        ])

        # Should return 0 (success) on dry-run
        assert result is None  # main() doesn't return anything on success

    def test_validate_command(self, tmp_path):
        """Test validate command."""
        from kicad_tools.cli.mfr import main as mfr_main
        from pathlib import Path

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
        from kicad_tools.core.sexp import SExp
        from kicad_tools.manufacturers import get_profile

        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=2)

        # Create a simple PCB sexp with a thin trace
        sexp = SExp("kicad_pcb", [
            SExp("segment", [
                SExp("width", [0.05])  # 0.05mm, below minimum
            ])
        ])

        violations = _validate_pcb_design(sexp, rules)

        # Should find the trace width violation
        assert len(violations) > 0
        assert any("TRACE_WIDTH" in v[0] for v in violations)
