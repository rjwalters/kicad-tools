"""Tests for configuration file support."""

import sys
import warnings

import pytest

from kicad_tools.config import (
    Config,
    ConfigError,
    DefaultsConfig,
    DrcConfig,
    ExportConfig,
    PartsConfig,
    RouteConfig,
    _find_project_config,
    _load_toml_file,
    generate_template,
    get_config_paths,
)


class TestConfigDataclasses:
    """Test configuration dataclass defaults."""

    def test_defaults_config_defaults(self):
        """DefaultsConfig has correct defaults."""
        config = DefaultsConfig()
        assert config.format == "table"
        assert config.manufacturer is None
        assert config.verbose is False
        assert config.quiet is False

    def test_drc_config_defaults(self):
        """DrcConfig has correct defaults."""
        config = DrcConfig()
        assert config.strict is False
        assert config.layers == 2

    def test_export_config_defaults(self):
        """ExportConfig has correct defaults."""
        config = ExportConfig()
        assert config.output_dir == "./manufacturing"
        assert config.include_dnp is False

    def test_route_config_defaults(self):
        """RouteConfig has correct defaults."""
        config = RouteConfig()
        assert config.strategy == "negotiated"
        assert config.grid_resolution == 0.1
        assert config.trace_width == 0.2
        assert config.clearance == 0.2
        assert config.via_drill == 0.3
        assert config.via_diameter == 0.6

    def test_parts_config_defaults(self):
        """PartsConfig has correct defaults."""
        config = PartsConfig()
        assert config.cache_dir == "~/.cache/kicad-tools/lcsc"
        assert config.cache_ttl_days == 7

    def test_config_defaults(self):
        """Config has correct nested defaults."""
        config = Config()
        assert isinstance(config.defaults, DefaultsConfig)
        assert isinstance(config.drc, DrcConfig)
        assert isinstance(config.export, ExportConfig)
        assert isinstance(config.route, RouteConfig)
        assert isinstance(config.parts, PartsConfig)


class TestConfigDiscovery:
    """Test config file discovery."""

    def test_find_project_config_in_current_dir(self, tmp_path):
        """Find config in current directory."""
        config_file = tmp_path / ".kicad-tools.toml"
        config_file.write_text("[defaults]\nformat = 'json'\n")

        result = _find_project_config(tmp_path)
        assert result == config_file

    def test_find_project_config_alternate_name(self, tmp_path):
        """Find config with alternate filename."""
        config_file = tmp_path / "kicad-tools.toml"
        config_file.write_text("[defaults]\nformat = 'json'\n")

        result = _find_project_config(tmp_path)
        assert result == config_file

    def test_find_project_config_prefers_hidden(self, tmp_path):
        """Hidden .kicad-tools.toml is preferred over kicad-tools.toml."""
        (tmp_path / "kicad-tools.toml").write_text("[defaults]\nformat = 'csv'\n")
        hidden = tmp_path / ".kicad-tools.toml"
        hidden.write_text("[defaults]\nformat = 'json'\n")

        result = _find_project_config(tmp_path)
        assert result == hidden

    def test_find_project_config_walks_up(self, tmp_path):
        """Find config by walking up directory tree."""
        parent_config = tmp_path / ".kicad-tools.toml"
        parent_config.write_text("[defaults]\nformat = 'json'\n")

        subdir = tmp_path / "src" / "deep"
        subdir.mkdir(parents=True)

        result = _find_project_config(subdir)
        assert result == parent_config

    def test_find_project_config_stops_at_git(self, tmp_path):
        """Stop searching at .git directory (don't go above it)."""
        # Create a structure: parent/project/.git
        # Config in parent should NOT be found when starting from project
        parent = tmp_path / "parent"
        parent.mkdir()
        project = parent / "project"
        project.mkdir()
        (project / ".git").mkdir()

        # Put config in parent (above .git)
        (parent / ".kicad-tools.toml").write_text("[defaults]\n")

        result = _find_project_config(project)
        # Should NOT find config above .git
        assert result is None

    def test_find_project_config_finds_in_git_root(self, tmp_path):
        """Find config at same level as .git."""
        (tmp_path / ".git").mkdir()
        config_file = tmp_path / ".kicad-tools.toml"
        config_file.write_text("[defaults]\n")

        result = _find_project_config(tmp_path)
        assert result == config_file

    def test_find_project_config_not_found(self, tmp_path):
        """Return None when no config found."""
        (tmp_path / ".git").mkdir()  # Stop searching here
        result = _find_project_config(tmp_path)
        assert result is None


class TestLoadToml:
    """Test TOML file loading."""

    def test_load_valid_toml(self, tmp_path):
        """Load valid TOML file."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
[defaults]
format = "json"
manufacturer = "jlcpcb"

[route]
trace_width = 0.3
"""
        )

        result = _load_toml_file(config_file)
        assert result["defaults"]["format"] == "json"
        assert result["defaults"]["manufacturer"] == "jlcpcb"
        assert result["route"]["trace_width"] == 0.3

    def test_load_invalid_toml(self, tmp_path):
        """Raise ConfigError on invalid TOML."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("invalid [ toml syntax")

        with pytest.raises(ConfigError, match="Invalid TOML"):
            _load_toml_file(config_file)

    def test_load_missing_file(self, tmp_path):
        """Raise ConfigError on missing file."""
        missing = tmp_path / "nonexistent.toml"

        with pytest.raises(ConfigError, match="Cannot read"):
            _load_toml_file(missing)


class TestConfigLoad:
    """Test Config.load() method."""

    def test_load_defaults_only(self, tmp_path, monkeypatch):
        """Load returns defaults when no config files exist."""
        # Create empty project with .git to stop searching
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)

        # Ensure user config doesn't exist
        monkeypatch.setattr("kicad_tools.config.USER_CONFIG_PATH", tmp_path / "no-exist.toml")

        config = Config.load(tmp_path)
        assert config.defaults.format == "table"
        assert config.defaults.manufacturer is None
        assert config.route.strategy == "negotiated"

    def test_load_project_config(self, tmp_path, monkeypatch):
        """Load project config."""
        (tmp_path / ".git").mkdir()
        config_file = tmp_path / ".kicad-tools.toml"
        config_file.write_text(
            """
[defaults]
format = "json"
manufacturer = "jlcpcb"
"""
        )

        monkeypatch.setattr("kicad_tools.config.USER_CONFIG_PATH", tmp_path / "no-exist.toml")

        config = Config.load(tmp_path)
        assert config.defaults.format == "json"
        assert config.defaults.manufacturer == "jlcpcb"

    def test_load_user_config(self, tmp_path, monkeypatch):
        """Load user config."""
        (tmp_path / ".git").mkdir()

        user_config = tmp_path / "user-config.toml"
        user_config.write_text(
            """
[defaults]
verbose = true

[route]
trace_width = 0.25
"""
        )

        monkeypatch.setattr("kicad_tools.config.USER_CONFIG_PATH", user_config)

        config = Config.load(tmp_path)
        assert config.defaults.verbose is True
        assert config.route.trace_width == 0.25

    def test_project_overrides_user(self, tmp_path, monkeypatch):
        """Project config overrides user config."""
        (tmp_path / ".git").mkdir()

        user_config = tmp_path / "user-config.toml"
        user_config.write_text('[defaults]\nformat = "csv"\nmanufacturer = "pcbway"\n')

        project_config = tmp_path / ".kicad-tools.toml"
        project_config.write_text('[defaults]\nformat = "json"\n')

        monkeypatch.setattr("kicad_tools.config.USER_CONFIG_PATH", user_config)

        config = Config.load(tmp_path)
        # Project overrides user
        assert config.defaults.format == "json"
        # User value preserved when not in project
        assert config.defaults.manufacturer == "pcbway"

    def test_get_source_tracking(self, tmp_path, monkeypatch):
        """Track source of each config value."""
        (tmp_path / ".git").mkdir()

        user_config = tmp_path / "user-config.toml"
        user_config.write_text('[defaults]\nformat = "csv"\n')

        project_config = tmp_path / ".kicad-tools.toml"
        project_config.write_text("[route]\ntrace_width = 0.3\n")

        monkeypatch.setattr("kicad_tools.config.USER_CONFIG_PATH", user_config)

        config = Config.load(tmp_path)

        # From user config
        assert "user-config.toml" in config.get_source("defaults.format")

        # From project config
        assert ".kicad-tools.toml" in config.get_source("route.trace_width")

        # Default (not in any file)
        assert config.get_source("drc.strict") == "default"


class TestConfigWarnings:
    """Test warnings for unknown config keys."""

    def test_warn_unknown_section(self, tmp_path, monkeypatch):
        """Warn on unknown top-level section."""
        (tmp_path / ".git").mkdir()
        config_file = tmp_path / ".kicad-tools.toml"
        config_file.write_text('[unknown_section]\nkey = "value"\n')

        monkeypatch.setattr("kicad_tools.config.USER_CONFIG_PATH", tmp_path / "no-exist.toml")

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Config.load(tmp_path)

            assert len(w) == 1
            assert "unknown_section" in str(w[0].message)

    def test_warn_unknown_key_in_section(self, tmp_path, monkeypatch):
        """Warn on unknown key within known section."""
        (tmp_path / ".git").mkdir()
        config_file = tmp_path / ".kicad-tools.toml"
        config_file.write_text('[defaults]\nunknown_key = "value"\n')

        monkeypatch.setattr("kicad_tools.config.USER_CONFIG_PATH", tmp_path / "no-exist.toml")

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Config.load(tmp_path)

            assert len(w) == 1
            assert "defaults.unknown_key" in str(w[0].message)


class TestGenerateTemplate:
    """Test template generation."""

    def test_generate_template_valid_toml(self):
        """Generated template is valid TOML."""
        template = generate_template()

        # Should be valid TOML (all commented out, but parseable)
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib

        # Template has all lines commented, so parse will return empty dict
        # but should not raise
        result = tomllib.loads(template)
        assert isinstance(result, dict)

    def test_generate_template_has_sections(self):
        """Template has all configuration sections."""
        template = generate_template()
        assert "[defaults]" in template
        assert "[drc]" in template
        assert "[export]" in template
        assert "[route]" in template
        assert "[parts]" in template

    def test_generate_template_documents_options(self):
        """Template documents key configuration options."""
        template = generate_template()
        assert "format" in template
        assert "manufacturer" in template
        assert "trace_width" in template
        assert "strategy" in template


class TestGetConfigPaths:
    """Test get_config_paths function."""

    def test_returns_dict_with_keys(self, tmp_path, monkeypatch):
        """Returns dict with user and project keys."""
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("kicad_tools.config.USER_CONFIG_PATH", tmp_path / "no-exist.toml")

        paths = get_config_paths()
        assert "user" in paths
        assert "project" in paths

    def test_returns_none_for_missing_files(self, tmp_path, monkeypatch):
        """Returns None for missing config files."""
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("kicad_tools.config.USER_CONFIG_PATH", tmp_path / "no-exist.toml")

        paths = get_config_paths()
        assert paths["user"] is None
        assert paths["project"] is None

    def test_returns_paths_for_existing_files(self, tmp_path, monkeypatch):
        """Returns paths for existing config files."""
        (tmp_path / ".git").mkdir()
        project_config = tmp_path / ".kicad-tools.toml"
        project_config.write_text("[defaults]\n")

        user_config = tmp_path / "user.toml"
        user_config.write_text("[defaults]\n")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("kicad_tools.config.USER_CONFIG_PATH", user_config)

        paths = get_config_paths()
        assert paths["user"] == user_config
        assert paths["project"] == project_config


class TestConfigIntegration:
    """Integration tests for config loading."""

    def test_full_config_merge(self, tmp_path, monkeypatch):
        """Test full config merge from multiple sources."""
        (tmp_path / ".git").mkdir()

        user_config = tmp_path / "user.toml"
        user_config.write_text(
            """
[defaults]
format = "csv"
manufacturer = "pcbway"
verbose = true

[route]
strategy = "basic"
trace_width = 0.15
"""
        )

        project_config = tmp_path / ".kicad-tools.toml"
        project_config.write_text(
            """
[defaults]
format = "json"

[drc]
strict = true
layers = 4

[route]
grid_resolution = 0.5
"""
        )

        monkeypatch.setattr("kicad_tools.config.USER_CONFIG_PATH", user_config)

        config = Config.load(tmp_path)

        # Project overrides user
        assert config.defaults.format == "json"

        # User value preserved when not in project
        assert config.defaults.manufacturer == "pcbway"
        assert config.defaults.verbose is True

        # Project values
        assert config.drc.strict is True
        assert config.drc.layers == 4

        # Mixed route config
        assert config.route.strategy == "basic"  # from user
        assert config.route.grid_resolution == 0.5  # from project
        assert config.route.trace_width == 0.15  # from user

    def test_config_with_all_types(self, tmp_path, monkeypatch):
        """Test config with all value types."""
        (tmp_path / ".git").mkdir()

        config_file = tmp_path / ".kicad-tools.toml"
        config_file.write_text(
            """
[defaults]
format = "json"
manufacturer = "jlcpcb"
verbose = true
quiet = false

[drc]
strict = true
layers = 4

[route]
strategy = "monte-carlo"
grid_resolution = 0.125
trace_width = 0.254
clearance = 0.127
via_drill = 0.4
via_diameter = 0.8

[parts]
cache_dir = "/custom/cache"
cache_ttl_days = 30
"""
        )

        monkeypatch.setattr("kicad_tools.config.USER_CONFIG_PATH", tmp_path / "no-exist.toml")

        config = Config.load(tmp_path)

        # String values
        assert config.defaults.format == "json"
        assert config.defaults.manufacturer == "jlcpcb"

        # Boolean values
        assert config.defaults.verbose is True
        assert config.defaults.quiet is False
        assert config.drc.strict is True

        # Integer values
        assert config.drc.layers == 4
        assert config.parts.cache_ttl_days == 30

        # Float values
        assert config.route.grid_resolution == 0.125
        assert config.route.trace_width == 0.254
