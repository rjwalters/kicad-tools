"""Tests for netclass functionality in project files."""

import json

import pytest

from kicad_tools.core.netclass_templates import (
    AUDIO_TEMPLATE,
    DESIGN_TYPE_TEMPLATES,
    POWER_SUPPLY_TEMPLATE,
    RF_TEMPLATE,
    apply_design_template,
    get_available_design_types,
    get_design_template,
    get_netclass_summary,
)
from kicad_tools.core.project_file import (
    DEFAULT_NETCLASS_DEFINITION,
    add_netclass_definition,
    add_netclass_pattern,
    add_netclass_patterns,
    clear_netclass_definitions,
    clear_netclass_patterns,
    create_netclass_definition,
    get_net_settings,
    get_netclass_definitions,
    get_netclass_patterns,
    load_project,
    save_project,
)


class TestNetclassDefinition:
    """Tests for netclass definition creation."""

    def test_create_netclass_definition_defaults(self):
        """Test creating a netclass definition with default values."""
        definition = create_netclass_definition("Power")

        assert definition["name"] == "Power"
        assert definition["track_width"] == 0.25
        assert definition["clearance"] == 0.2
        assert definition["via_diameter"] == 0.6
        assert definition["via_drill"] == 0.3

    def test_create_netclass_definition_custom_values(self):
        """Test creating a netclass definition with custom values."""
        definition = create_netclass_definition(
            name="HighCurrent",
            track_width=0.8,
            clearance=0.25,
            via_diameter=1.0,
            via_drill=0.5,
        )

        assert definition["name"] == "HighCurrent"
        assert definition["track_width"] == 0.8
        assert definition["clearance"] == 0.25
        assert definition["via_diameter"] == 1.0
        assert definition["via_drill"] == 0.5

    def test_create_netclass_definition_with_color(self):
        """Test creating a netclass definition with custom color."""
        definition = create_netclass_definition(
            name="Power",
            pcb_color="rgba(255, 0, 0, 0.800)",
        )

        assert definition["pcb_color"] == "rgba(255, 0, 0, 0.800)"

    def test_default_netclass_definition_has_all_fields(self):
        """Test that default netclass definition has all required fields."""
        required_fields = [
            "bus_width",
            "clearance",
            "diff_pair_gap",
            "diff_pair_via_gap",
            "diff_pair_width",
            "line_style",
            "microvia_diameter",
            "microvia_drill",
            "name",
            "pcb_color",
            "schematic_color",
            "track_width",
            "via_diameter",
            "via_drill",
            "wire_width",
        ]
        for field in required_fields:
            assert field in DEFAULT_NETCLASS_DEFINITION


class TestNetSettings:
    """Tests for net_settings management."""

    def test_get_net_settings_creates_if_missing(self):
        """Test that get_net_settings creates structure if missing."""
        data = {}
        net_settings = get_net_settings(data)

        assert "net_settings" in data
        assert "classes" in net_settings
        assert "netclass_patterns" in net_settings
        assert len(net_settings["classes"]) == 1
        assert net_settings["classes"][0]["name"] == "Default"

    def test_get_net_settings_preserves_existing(self):
        """Test that get_net_settings preserves existing data."""
        data = {
            "net_settings": {
                "classes": [{"name": "Custom", "track_width": 0.5}],
                "netclass_patterns": [{"netclass": "Custom", "pattern": "VCC*"}],
            }
        }
        net_settings = get_net_settings(data)

        assert len(net_settings["classes"]) == 1
        assert net_settings["classes"][0]["name"] == "Custom"

    def test_get_netclass_definitions(self):
        """Test getting netclass definitions from project data."""
        data = {}
        classes = get_netclass_definitions(data)

        assert len(classes) == 1
        assert classes[0]["name"] == "Default"

    def test_get_netclass_patterns(self):
        """Test getting netclass patterns from project data."""
        data = {}
        patterns = get_netclass_patterns(data)

        assert patterns == []


class TestAddNetclass:
    """Tests for adding netclass definitions."""

    def test_add_netclass_definition(self):
        """Test adding a new netclass definition."""
        data = {}
        definition = add_netclass_definition(
            data,
            name="Power",
            track_width=0.5,
            clearance=0.2,
        )

        classes = get_netclass_definitions(data)
        assert len(classes) == 2  # Default + Power
        assert definition["name"] == "Power"
        assert definition["track_width"] == 0.5

    def test_add_netclass_definition_updates_existing(self):
        """Test that adding an existing netclass updates it."""
        data = {}
        add_netclass_definition(data, name="Power", track_width=0.3)
        add_netclass_definition(data, name="Power", track_width=0.5)

        classes = get_netclass_definitions(data)
        power_classes = [c for c in classes if c["name"] == "Power"]
        assert len(power_classes) == 1
        assert power_classes[0]["track_width"] == 0.5

    def test_add_netclass_pattern(self):
        """Test adding a netclass pattern."""
        data = {}
        pattern = add_netclass_pattern(data, "Power", "VCC*")

        patterns = get_netclass_patterns(data)
        assert len(patterns) == 1
        assert patterns[0]["netclass"] == "Power"
        assert patterns[0]["pattern"] == "VCC*"

    def test_add_netclass_pattern_no_duplicates(self):
        """Test that duplicate patterns are not added."""
        data = {}
        add_netclass_pattern(data, "Power", "VCC*")
        add_netclass_pattern(data, "Power", "VCC*")

        patterns = get_netclass_patterns(data)
        assert len(patterns) == 1

    def test_add_netclass_patterns_bulk(self):
        """Test adding multiple patterns at once."""
        data = {}
        add_netclass_patterns(data, "Power", ["VCC*", "+*V", "GND*"])

        patterns = get_netclass_patterns(data)
        assert len(patterns) == 3


class TestClearNetclass:
    """Tests for clearing netclass definitions and patterns."""

    def test_clear_netclass_definitions_keeps_default(self):
        """Test clearing netclass definitions keeps Default."""
        data = {}
        add_netclass_definition(data, "Power", track_width=0.5)
        add_netclass_definition(data, "Clock", track_width=0.2)

        clear_netclass_definitions(data, keep_default=True)

        classes = get_netclass_definitions(data)
        assert len(classes) == 1
        assert classes[0]["name"] == "Default"

    def test_clear_netclass_definitions_removes_all(self):
        """Test clearing all netclass definitions."""
        data = {}
        add_netclass_definition(data, "Power", track_width=0.5)

        clear_netclass_definitions(data, keep_default=False)

        classes = get_netclass_definitions(data)
        assert len(classes) == 0

    def test_clear_netclass_patterns(self):
        """Test clearing all netclass patterns."""
        data = {}
        add_netclass_patterns(data, "Power", ["VCC*", "+*V"])

        clear_netclass_patterns(data)

        patterns = get_netclass_patterns(data)
        assert len(patterns) == 0


class TestDesignTypeTemplates:
    """Tests for design type templates."""

    def test_get_available_design_types(self):
        """Test getting list of available design types."""
        types = get_available_design_types()

        assert "audio" in types
        assert "power_supply" in types
        assert "digital" in types
        assert "mixed_signal" in types
        assert "rf" in types

    def test_get_design_template(self):
        """Test getting a design template by name."""
        template = get_design_template("audio")

        assert template.name == "audio"
        assert len(template.netclasses) > 0

    def test_get_design_template_invalid(self):
        """Test getting an invalid design template."""
        with pytest.raises(ValueError) as excinfo:
            get_design_template("invalid_type")

        assert "Unknown design type" in str(excinfo.value)

    def test_audio_template_has_expected_classes(self):
        """Test that audio template has expected netclasses."""
        class_names = [nc.name for nc in AUDIO_TEMPLATE.netclasses]

        assert "Power" in class_names
        assert "Ground" in class_names
        assert "Audio" in class_names
        assert "I2S" in class_names

    def test_power_supply_template_has_high_current(self):
        """Test that power supply template has HighCurrent class."""
        class_names = [nc.name for nc in POWER_SUPPLY_TEMPLATE.netclasses]

        assert "HighCurrent" in class_names

    def test_rf_template_has_rf_class(self):
        """Test that RF template has RF class."""
        class_names = [nc.name for nc in RF_TEMPLATE.netclasses]

        assert "RF" in class_names

    def test_all_templates_have_patterns(self):
        """Test that all templates have patterns defined."""
        for name, template in DESIGN_TYPE_TEMPLATES.items():
            for nc in template.netclasses:
                # Most netclasses should have patterns (except Default)
                if nc.name != "Default":
                    assert len(nc.patterns) > 0, f"{name}.{nc.name} has no patterns"


class TestApplyDesignTemplate:
    """Tests for applying design templates to project data."""

    def test_apply_audio_template(self):
        """Test applying audio template to project data."""
        data = {}
        apply_design_template(data, "audio")

        classes = get_netclass_definitions(data)
        class_names = [c["name"] for c in classes]

        assert "Default" in class_names
        assert "Power" in class_names
        assert "Audio" in class_names
        assert "I2S" in class_names

    def test_apply_template_adds_patterns(self):
        """Test that applying template adds patterns."""
        data = {}
        apply_design_template(data, "audio")

        patterns = get_netclass_patterns(data)
        assert len(patterns) > 0

        # Check for specific patterns
        power_patterns = [p for p in patterns if p["netclass"] == "Power"]
        assert len(power_patterns) > 0

    def test_apply_template_sets_correct_widths(self):
        """Test that applying template sets correct trace widths."""
        data = {}
        apply_design_template(data, "power_supply")

        classes = get_netclass_definitions(data)
        high_current = next(c for c in classes if c["name"] == "HighCurrent")

        assert high_current["track_width"] == 0.8

    def test_get_netclass_summary(self):
        """Test getting netclass summary from project data."""
        data = {}
        apply_design_template(data, "audio")

        summary = get_netclass_summary(data)

        assert len(summary) > 1
        for nc in summary:
            assert "name" in nc
            assert "track_width" in nc
            assert "clearance" in nc
            assert "pattern_count" in nc


class TestProjectFileIntegration:
    """Integration tests for netclass handling in project files."""

    def test_save_and_load_with_netclasses(self, tmp_path):
        """Test saving and loading project file with netclasses."""
        project_path = tmp_path / "test.kicad_pro"

        # Create project with netclasses
        data = {
            "meta": {"filename": "test.kicad_pro", "version": 1},
        }
        apply_design_template(data, "audio")

        # Save
        save_project(data, project_path)

        # Load and verify
        loaded = load_project(project_path)

        assert "net_settings" in loaded
        classes = loaded["net_settings"]["classes"]
        class_names = [c["name"] for c in classes]
        assert "Audio" in class_names

    def test_netclass_patterns_roundtrip(self, tmp_path):
        """Test that netclass patterns survive save/load."""
        project_path = tmp_path / "test.kicad_pro"

        data = {"meta": {"filename": "test.kicad_pro", "version": 1}}
        add_netclass_definition(data, "Power", track_width=0.5)
        add_netclass_patterns(data, "Power", ["VCC*", "+*V", "GND*"])

        save_project(data, project_path)
        loaded = load_project(project_path)

        patterns = loaded["net_settings"]["netclass_patterns"]
        assert len(patterns) == 3

    def test_kicad_format_compatibility(self, tmp_path):
        """Test that generated net_settings matches KiCad format."""
        project_path = tmp_path / "test.kicad_pro"

        data = {"meta": {"filename": "test.kicad_pro", "version": 1}}
        apply_design_template(data, "audio")

        save_project(data, project_path)

        # Verify JSON structure matches KiCad expectations
        with open(project_path) as f:
            parsed = json.load(f)

        net_settings = parsed["net_settings"]
        assert "classes" in net_settings
        assert "meta" in net_settings
        assert net_settings["meta"]["version"] == 3
        assert "netclass_patterns" in net_settings

        # Verify class structure
        for cls in net_settings["classes"]:
            assert "name" in cls
            assert "track_width" in cls
            assert "clearance" in cls
            assert "via_diameter" in cls
            assert "via_drill" in cls
