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
        assert len(manufacturers) == 6

        names = {m.name for m in manufacturers}
        assert "JLCPCB" in names
        assert "JLCPCB Capability Plus" in names
        assert "Seeed Fusion" in names
        assert "PCBWay" in names
        assert "OSHPark" in names
        assert "FlashPCB" in names

    def test_get_manufacturer_ids(self):
        """Test getting manufacturer IDs."""
        ids = get_manufacturer_ids()
        assert "jlcpcb" in ids
        assert "seeed" in ids
        assert "pcbway" in ids
        assert "oshpark" in ids
        assert "flashpcb" in ids

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
        assert "max_board_width_mm" in d
        assert "max_board_height_mm" in d

    def test_max_board_dimensions(self):
        """Test max board dimensions are available in design rules."""
        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=2)

        # Max board dimensions should have sensible defaults
        assert rules.max_board_width_mm > 0
        assert rules.max_board_height_mm > 0
        # Default values are 400x500mm
        assert rules.max_board_width_mm == 400.0
        assert rules.max_board_height_mm == 500.0

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

            # Check for 11 rules (standard set with condition expressions)
            rules = sexp.find_children("rule")
            assert len(rules) == 11, f"{dru_file} should have 11 rules, has {len(rules)}"


class TestMfrCLICommands:
    """Tests for mfr CLI commands."""

    def test_export_dru_default_output(self, tmp_path, monkeypatch):
        """Test export-dru outputs to current directory by default (issue #550)."""
        from kicad_tools.cli.mfr import main as mfr_main

        # Change to temp directory
        monkeypatch.chdir(tmp_path)

        # Run export-dru without -o flag
        mfr_main(["export-dru", "jlcpcb", "--layers", "4"])

        # Should create file in current directory with descriptive name
        expected_file = tmp_path / "jlcpcb-4layer-1oz.kicad_dru"
        assert expected_file.exists(), f"Expected {expected_file} to exist"

        # Verify content is valid
        content = expected_file.read_text()
        assert "(version 1)" in content
        assert "track_width" in content
        assert "clearance" in content

    def test_export_dru_with_output_path(self, tmp_path):
        """Test export-dru with explicit -o output path."""
        from kicad_tools.cli.mfr import main as mfr_main

        output_file = tmp_path / "custom_rules.kicad_dru"

        mfr_main(["export-dru", "seeed", "--layers", "2", "-o", str(output_file)])

        assert output_file.exists()
        content = output_file.read_text()
        assert "(version 1)" in content

    def test_export_dru_creates_parent_dirs(self, tmp_path):
        """Test export-dru creates parent directories if needed."""
        from kicad_tools.cli.mfr import main as mfr_main

        # Nested path that doesn't exist
        output_file = tmp_path / "nested" / "dir" / "rules.kicad_dru"

        mfr_main(["export-dru", "pcbway", "--layers", "4", "-o", str(output_file)])

        assert output_file.exists()

    def test_export_dru_different_copper_weights(self, tmp_path, monkeypatch):
        """Test export-dru with different copper weights."""
        from kicad_tools.cli.mfr import main as mfr_main

        monkeypatch.chdir(tmp_path)

        # Test 2oz copper
        mfr_main(["export-dru", "jlcpcb", "--layers", "2", "--copper", "2.0"])

        expected_file = tmp_path / "jlcpcb-2layer-2oz.kicad_dru"
        assert expected_file.exists()

    def test_export_dru_all_manufacturers(self, tmp_path, monkeypatch):
        """Test export-dru works for all manufacturers."""
        from kicad_tools.cli.mfr import main as mfr_main

        monkeypatch.chdir(tmp_path)

        manufacturers = ["jlcpcb", "seeed", "pcbway", "oshpark"]
        for mfr in manufacturers:
            mfr_main(["export-dru", mfr, "--layers", "2"])
            expected = tmp_path / f"{mfr}-2layer-1oz.kicad_dru"
            assert expected.exists(), f"Failed for {mfr}"

    def test_apply_rules_dry_run(self, tmp_path):
        """Test apply-rules command with dry-run."""
        import shutil
        from pathlib import Path

        from kicad_tools.cli.mfr import main as mfr_main

        # Copy test file to temp directory
        src_pcb = Path("boards/03-usb-joystick/usb_joystick.kicad_pcb")
        if not src_pcb.exists():
            pytest.skip("Demo PCB file not found")

        test_pcb = tmp_path / "test.kicad_pcb"
        shutil.copy(src_pcb, test_pcb)

        # Run apply-rules with dry-run
        result = mfr_main(["apply-rules", str(test_pcb), "jlcpcb", "--dry-run"])

        # Should return 0 (success) on dry-run
        assert result is None  # main() doesn't return anything on success

        # dry-run must not write a sibling .kicad_pro (issue #4097)
        assert not (tmp_path / "test.kicad_pro").exists()

    # Minimal, self-contained .kicad_pcb so these regression tests do not
    # depend on generated demo-board artifacts (which are not committed).
    _MINIMAL_PCB = "(kicad_pcb (version 20240108) (generator test))"

    def test_apply_rules_pcb_creates_kicad_pro(self, tmp_path):
        """apply-rules on a bare .kicad_pcb creates a sibling .kicad_pro.

        Regression for #4097: without a .kicad_pro, kicad-cli pcb drc falls
        back to KiCad factory defaults.  The sidecar must carry the profile's
        design_settings.rules and a Default netclass matching the tier.
        """
        import json

        from kicad_tools.cli.mfr import main as mfr_main
        from kicad_tools.manufacturers import get_profile

        test_pcb = tmp_path / "test.kicad_pcb"
        test_pcb.write_text(self._MINIMAL_PCB)

        pro_path = tmp_path / "test.kicad_pro"
        assert not pro_path.exists()  # precondition: no project file yet

        mfr_main(["apply-rules", str(test_pcb), "jlcpcb", "--layers", "4", "--copper", "1"])

        assert pro_path.exists(), "sibling .kicad_pro was not created"

        data = json.loads(pro_path.read_text())
        rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1.0)

        proj_rules = data["board"]["design_settings"]["rules"]
        assert proj_rules["min_clearance"] == pytest.approx(rules.min_clearance_mm)
        assert proj_rules["min_track_width"] == pytest.approx(rules.min_trace_width_mm)
        assert proj_rules["min_via_diameter"] == pytest.approx(rules.min_via_diameter_mm)

        # Default netclass must carry the profile clearance, not the stock 0.20
        classes = data["net_settings"]["classes"]
        default_cls = next(c for c in classes if c.get("name") == "Default")
        assert default_cls["clearance"] == pytest.approx(rules.min_clearance_mm)
        assert default_cls["track_width"] == pytest.approx(rules.min_trace_width_mm)
        assert default_cls["via_diameter"] == pytest.approx(rules.min_via_diameter_mm)
        assert default_cls["via_drill"] == pytest.approx(rules.min_via_drill_mm)

    def test_apply_rules_pcb_merges_existing_kicad_pro(self, tmp_path):
        """apply-rules updates an existing .kicad_pro without clobbering keys."""
        import json

        from kicad_tools.cli.mfr import main as mfr_main
        from kicad_tools.manufacturers import get_profile

        test_pcb = tmp_path / "test.kicad_pcb"
        test_pcb.write_text(self._MINIMAL_PCB)

        # Pre-existing project with an unrelated key that must survive.
        pro_path = tmp_path / "test.kicad_pro"
        pro_path.write_text(
            json.dumps(
                {
                    "board": {"design_settings": {"rules": {"min_clearance": 0.05}}},
                    "text_variables": {"MY_VAR": "keep-me"},
                    "sheets": [["abc", "Root"]],
                }
            )
        )

        mfr_main(["apply-rules", str(test_pcb), "jlcpcb", "--layers", "4", "--copper", "1"])

        data = json.loads(pro_path.read_text())
        rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1.0)

        # Unrelated keys preserved.
        assert data["text_variables"] == {"MY_VAR": "keep-me"}
        assert data["sheets"] == [["abc", "Root"]]

        # Rules overwritten to the new profile (not left at 0.05).
        assert data["board"]["design_settings"]["rules"]["min_clearance"] == pytest.approx(
            rules.min_clearance_mm
        )
        default_cls = next(c for c in data["net_settings"]["classes"] if c.get("name") == "Default")
        assert default_cls["clearance"] == pytest.approx(rules.min_clearance_mm)

    def test_apply_rules_pcb_output_redirect_places_sidecar(self, tmp_path):
        """--output places the .kicad_pro next to the redirected board."""
        from kicad_tools.cli.mfr import main as mfr_main

        test_pcb = tmp_path / "input.kicad_pcb"
        test_pcb.write_text(self._MINIMAL_PCB)

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        out_pcb = out_dir / "redirected.kicad_pcb"

        mfr_main(
            [
                "apply-rules",
                str(test_pcb),
                "jlcpcb",
                "--layers",
                "4",
                "--output",
                str(out_pcb),
            ]
        )

        # Sidecar lands next to the redirected board, not the input.
        assert (out_dir / "redirected.kicad_pro").exists()
        assert not (tmp_path / "input.kicad_pro").exists()

    def test_apply_rules_pro_input_writes_default_netclass(self, tmp_path):
        """apply-rules on a .kicad_pro input also writes the Default netclass.

        Previously the .kicad_pro branch set design_settings.rules but never
        touched net_settings.classes, so KiCad's clearance test kept the stock
        0.20 mm Default (issue #4097).
        """
        import json

        from kicad_tools.cli.mfr import main as mfr_main
        from kicad_tools.manufacturers import get_profile

        pro_path = tmp_path / "test.kicad_pro"
        pro_path.write_text(json.dumps({"meta": {"version": 1}}))

        mfr_main(["apply-rules", str(pro_path), "jlcpcb", "--layers", "4", "--copper", "1"])

        data = json.loads(pro_path.read_text())
        rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1.0)

        default_cls = next(c for c in data["net_settings"]["classes"] if c.get("name") == "Default")
        assert default_cls["clearance"] == pytest.approx(rules.min_clearance_mm)
        assert default_cls["track_width"] == pytest.approx(rules.min_trace_width_mm)
        assert default_cls["via_diameter"] == pytest.approx(rules.min_via_diameter_mm)
        assert default_cls["via_drill"] == pytest.approx(rules.min_via_drill_mm)

    def test_validate_command(self, tmp_path):
        """Test validate command."""
        from pathlib import Path

        from kicad_tools.cli.mfr import main as mfr_main

        src_pcb = Path("boards/03-usb-joystick/usb_joystick_routed.kicad_pcb")
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

        assert rules.min_trace_width_mm == pytest.approx(0.1524)  # 6 mil for 2oz
        assert rules.min_clearance_mm == pytest.approx(0.1524)
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


class TestDruGenerator:
    """Tests for the centralized DRU generator."""

    def test_generate_dru_returns_valid_content(self):
        """Test that generate_dru produces valid KiCad DRU content."""
        from kicad_tools.manufacturers.dru_generator import generate_dru

        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=2, copper_oz=1.0)
        content = generate_dru(rules, manufacturer_name="JLCPCB")

        assert content.startswith("(version 1)")
        assert "(rule" in content

    def test_generate_dru_has_11_rules(self):
        """Test that generate_dru produces all 11 rules."""
        from kicad_tools.manufacturers.dru_generator import generate_dru

        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=2, copper_oz=1.0)
        content = generate_dru(rules, manufacturer_name="JLCPCB")

        rule_count = content.count("(rule ")
        assert rule_count == 11, f"Expected 11 rules, got {rule_count}"

    def test_generate_dru_has_condition_expressions(self):
        """Test that generated DRU includes condition expressions."""
        from kicad_tools.manufacturers.dru_generator import generate_dru

        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=2, copper_oz=1.0)
        content = generate_dru(rules, manufacturer_name="JLCPCB")

        # Via Drill should be scoped to vias
        assert "A.Type == 'via'" in content
        # Trace Width should be scoped to tracks
        assert "A.Type == 'track'" in content
        # Silkscreen rules should be scoped to silk layer
        assert "A.Layer == 'F.Silkscreen'" in content

    def test_generate_dru_covers_solder_mask_rules(self):
        """Test that solder mask dam and clearance rules are present."""
        from kicad_tools.manufacturers.dru_generator import generate_dru

        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=2, copper_oz=1.0)
        content = generate_dru(rules, manufacturer_name="JLCPCB")

        assert "solder_mask_margin" in content
        assert "physical_hole_clearance" in content
        assert f"{rules.min_solder_mask_clearance_mm}mm" in content
        assert f"{rules.min_solder_mask_dam_mm}mm" in content

    def test_generate_dru_covers_silkscreen_height(self):
        """Test that silkscreen height rule is present."""
        from kicad_tools.manufacturers.dru_generator import generate_dru

        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=2, copper_oz=1.0)
        content = generate_dru(rules, manufacturer_name="JLCPCB")

        assert "text_height" in content
        assert f"{rules.min_silkscreen_height_mm}mm" in content

    def test_generate_dru_without_manufacturer_name(self):
        """Test generate_dru works without a manufacturer name."""
        from kicad_tools.manufacturers.dru_generator import generate_dru

        rules = DesignRules(
            min_trace_width_mm=0.127,
            min_clearance_mm=0.127,
            min_via_drill_mm=0.3,
            min_via_diameter_mm=0.6,
            min_annular_ring_mm=0.15,
        )
        content = generate_dru(rules)

        assert "(version 1)" in content
        assert '"Trace Width"' in content
        # No manufacturer suffix
        assert " - " not in content.split("\n")[1]

    def test_generate_dru_values_match_rules(self):
        """Test that generated DRU values match the DesignRules input."""
        import re

        from kicad_tools.manufacturers.dru_generator import generate_dru

        profile = get_profile("pcbway")
        rules = profile.get_design_rules(layers=4, copper_oz=1.0)
        content = generate_dru(rules, manufacturer_name="PCBWay")

        # Check track_width
        match = re.search(r"track_width \(min ([\d.]+)mm\)", content)
        assert match
        assert float(match.group(1)) == pytest.approx(rules.min_trace_width_mm)

        # Check clearance
        match = re.search(r"clearance \(min ([\d.]+)mm\)", content)
        assert match
        assert float(match.group(1)) == pytest.approx(rules.min_clearance_mm)

        # Check via_diameter
        match = re.search(r"via_diameter \(min ([\d.]+)mm\)", content)
        assert match
        assert float(match.group(1)) == pytest.approx(rules.min_via_diameter_mm)

    def test_generate_dru_all_manufacturers(self):
        """Test generate_dru works for all supported manufacturers."""
        from kicad_tools.manufacturers.dru_generator import generate_dru

        for mfr_id in get_manufacturer_ids():
            profile = get_profile(mfr_id)
            rules = profile.get_design_rules(layers=2, copper_oz=1.0)
            content = generate_dru(rules, manufacturer_name=profile.name)
            assert "(version 1)" in content, f"Failed for {mfr_id}"
            assert content.count("(rule ") == 11, f"Wrong rule count for {mfr_id}"

    def test_static_dru_files_match_dynamic_generation(self):
        """Test that static .kicad_dru files match dynamic generation output."""
        from pathlib import Path

        from kicad_tools.manufacturers.dru_generator import generate_dru

        rules_dir = Path(__file__).parent.parent / "src/kicad_tools/manufacturers/rules"

        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=2, copper_oz=1.0)
        expected = generate_dru(rules, manufacturer_name=profile.name)

        actual = (rules_dir / "jlcpcb-2layer-1oz.kicad_dru").read_text()
        assert actual == expected, "Static DRU file does not match dynamic generation"


class TestDruGeneratorAmpacity:
    """Net-scoped ampacity min-width rule emission (#4216, Part 1 of #4215)."""

    def _rules_2oz(self) -> DesignRules:
        """A 4-layer JLCPCB profile with 2 oz outer copper for a ~6.3 mm width."""
        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=4, copper_oz=2.0)
        # Force known copper weights so the derived widths are deterministic.
        rules.outer_copper_oz = 2.0
        rules.inner_copper_oz = 0.5
        return rules

    def _fused_net_class(self):
        from kicad_tools.router.rules import NetClassRouting

        return NetClassRouting(name="FUSED_LINE", target_ampacity=15.0)

    def test_pass_through_when_net_classes_none(self):
        """net_classes=None -> byte-for-byte identical to the board-wide output."""
        from kicad_tools.manufacturers.dru_generator import generate_dru

        rules = self._rules_2oz()
        without = generate_dru(rules, manufacturer_name="JLCPCB")
        with_none = generate_dru(rules, manufacturer_name="JLCPCB", net_classes=None)
        assert with_none == without

    def test_pass_through_when_no_target_ampacity(self):
        """A class with target_ampacity=None emits no ampacity rule (pass-through)."""
        from kicad_tools.manufacturers.dru_generator import generate_dru
        from kicad_tools.router.rules import NetClassRouting

        rules = self._rules_2oz()
        baseline = generate_dru(rules, manufacturer_name="JLCPCB")
        nc = NetClassRouting(name="PLAIN", target_ampacity=None)
        with_plain = generate_dru(rules, manufacturer_name="JLCPCB", net_classes=[nc])
        assert with_plain == baseline
        assert "Ampacity Min Width" not in with_plain

    def test_ampacity_rules_emitted_external_and_internal(self):
        """A target_ampacity class emits both external and internal net-scoped rules."""
        import re

        from kicad_tools.manufacturers.dru_generator import generate_dru
        from kicad_tools.physics.ampacity import width_for_current

        rules = self._rules_2oz()
        nc = self._fused_net_class()
        content = generate_dru(rules, manufacturer_name="JLCPCB", net_classes=[nc])

        # Two new rules exist, both net-scoped to FUSED_LINE.
        assert '"Ampacity Min Width (FUSED_LINE, external) - JLCPCB"' in content
        assert '"Ampacity Min Width (FUSED_LINE, internal) - JLCPCB"' in content
        assert "A.NetClass == 'FUSED_LINE'" in content

        # External condition scopes to the two outer copper layers.
        assert "(A.Layer == 'F.Cu' || A.Layer == 'B.Cu')" in content
        # Internal condition excludes the outer copper layers.
        assert "A.Layer != 'F.Cu' && A.Layer != 'B.Cu'" in content

        # Widths derive from IPC-2221 against the board copper weights.
        expected_ext = width_for_current(15.0, copper_weight_oz=2.0, layer="external")
        expected_int = width_for_current(15.0, copper_weight_oz=0.5, layer="internal")

        ext_block = content.split("external) - JLCPCB", 1)[1]
        ext_match = re.search(r"track_width \(min ([\d.]+)mm\)", ext_block)
        assert ext_match
        assert float(ext_match.group(1)) == pytest.approx(expected_ext, abs=0.01)

        int_block = content.split("internal) - JLCPCB", 1)[1]
        int_match = re.search(r"track_width \(min ([\d.]+)mm\)", int_block)
        assert int_match
        assert float(int_match.group(1)) == pytest.approx(expected_int, abs=0.01)

        # The external golden value (2 oz, 15 A, 10 C) is ~6.29 mm.
        assert float(ext_match.group(1)) == pytest.approx(6.29, abs=0.05)

    def test_ampacity_and_impedance_emit_separate_rules(self):
        """A class with both target_ampacity and target_single_impedance keeps
        the ampacity rules separate (not merged) -- per the Out-of-Scope note.
        """
        from kicad_tools.manufacturers.dru_generator import generate_dru
        from kicad_tools.router.rules import NetClassRouting

        rules = self._rules_2oz()
        nc = NetClassRouting(
            name="FUSED_LINE",
            target_ampacity=15.0,
            target_single_impedance=50.0,
        )
        content = generate_dru(rules, manufacturer_name="JLCPCB", net_classes=[nc])
        # Two (and only two) ampacity rules; the DRU generator does not emit
        # impedance rules (that lives in the router sizing path), so we only
        # assert the ampacity pair is present and distinct.
        assert content.count("Ampacity Min Width (FUSED_LINE") == 2

    def test_multiple_net_classes(self):
        """Only classes with target_ampacity set produce rules."""
        from kicad_tools.manufacturers.dru_generator import generate_dru
        from kicad_tools.router.rules import NetClassRouting

        rules = self._rules_2oz()
        classes = [
            NetClassRouting(name="FUSED_LINE", target_ampacity=15.0),
            NetClassRouting(name="SIGNAL", target_ampacity=None),
            NetClassRouting(name="MOTOR", target_ampacity=8.0),
        ]
        content = generate_dru(rules, manufacturer_name="JLCPCB", net_classes=classes)
        assert content.count("Ampacity Min Width (FUSED_LINE") == 2
        assert content.count("Ampacity Min Width (MOTOR") == 2
        assert "SIGNAL" not in content

    def test_ampacity_dru_accepted_by_kicad_cli(self, tmp_path):
        """Synthetic round-trip: the net-scoped DRU is accepted by kicad-cli DRC.

        Verifies the ``A.NetClass`` condition token is valid KiCad custom-rule
        syntax by running ``kicad-cli pcb drc`` against a minimal board with
        the generated .kicad_dru.  If kicad-cli is unavailable, falls back to
        asserting the DRU text is well-formed.
        """
        import shutil
        import subprocess

        from kicad_tools.manufacturers.dru_generator import generate_dru

        rules = self._rules_2oz()
        nc = self._fused_net_class()
        dru_content = generate_dru(rules, manufacturer_name="JLCPCB", net_classes=[nc])

        kicad_cli = shutil.which("kicad-cli")
        if kicad_cli is None:
            # Fallback: assert the rule is well-formed KiCad DRU syntax.
            assert dru_content.startswith("(version 1)")
            assert "A.NetClass == 'FUSED_LINE'" in dru_content
            assert dru_content.count("(rule ") >= 13
            # Balanced parentheses per rule line group.
            assert dru_content.count("(") == dru_content.count(")")
            pytest.skip("kicad-cli not available; asserted DRU text structure only")

        # Minimal 2-layer board with one FUSED_LINE net and a short track so
        # the DRC engine actually parses and evaluates the rule condition.
        board = tmp_path / "amp.kicad_pcb"
        board.write_text(
            "(kicad_pcb (version 20240108) (generator test)\n"
            '  (net 0 "")\n'
            '  (net 1 "FUSED_LINE")\n'
            '  (net_class FUSED_LINE "" (clearance 0.2) (trace_width 0.25)\n'
            '    (add_net "FUSED_LINE"))\n'
            '  (segment (start 10 10) (end 20 10) (width 0.25) (layer "F.Cu") (net 1))\n'
            ")\n"
        )

        dru_path = tmp_path / "amp.kicad_dru"
        dru_path.write_text(dru_content)

        report = tmp_path / "drc.json"
        result = subprocess.run(
            [
                kicad_cli,
                "pcb",
                "drc",
                "--format",
                "json",
                "-o",
                str(report),
                str(board),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        combined = (result.stdout + result.stdout + result.stderr).lower()
        # The rule condition must PARSE cleanly -- KiCad reports malformed
        # rule syntax on stderr/stdout.  A rule violation is fine (exit != 0);
        # a syntax error is not.
        for bad in ("unexpected", "syntax error", "expecting", "unrecognized"):
            assert bad not in combined, (
                f"kicad-cli reported a rule-syntax problem ({bad!r}) for the "
                f"net-scoped ampacity DRU:\n{result.stdout}\n{result.stderr}"
            )
