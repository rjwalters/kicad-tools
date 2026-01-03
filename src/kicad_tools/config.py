"""
Configuration file support for kicad-tools.

Provides hierarchical configuration loading from:
1. Project config: .kicad-tools.toml or kicad-tools.toml in project root
2. User config: ~/.config/kicad-tools/config.toml

CLI arguments override config file values, and project config overrides user config.
"""

import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None  # type: ignore[assignment]

# Config file names to search for in project directories
CONFIG_FILENAMES = [".kicad-tools.toml", "kicad-tools.toml"]

# User-level config path
USER_CONFIG_PATH = Path.home() / ".config" / "kicad-tools" / "config.toml"

# All known config keys for validation
KNOWN_KEYS = {
    "defaults": {"format", "manufacturer", "verbose", "quiet"},
    "drc": {"strict", "layers"},
    "export": {"output_dir", "include_dnp"},
    "route": {
        "strategy",
        "grid_resolution",
        "trace_width",
        "clearance",
        "via_drill",
        "via_diameter",
    },
    "parts": {"cache_dir", "cache_ttl_days"},
    "footprint_validation": {
        "kicad_library_path",
        "tolerance_mm",
        "library_mappings",
    },
}


@dataclass
class DefaultsConfig:
    """Default options for CLI commands."""

    format: str = "table"
    manufacturer: str | None = None
    verbose: bool = False
    quiet: bool = False


@dataclass
class DrcConfig:
    """DRC-specific configuration."""

    strict: bool = False
    layers: int = 2


@dataclass
class ExportConfig:
    """Export-specific configuration."""

    output_dir: str = "./manufacturing"
    include_dnp: bool = False


@dataclass
class RouteConfig:
    """Routing configuration."""

    strategy: str = "negotiated"
    grid_resolution: float = 0.1
    trace_width: float = 0.2
    clearance: float = 0.2
    via_drill: float = 0.3
    via_diameter: float = 0.6


@dataclass
class PartsConfig:
    """Parts lookup configuration."""

    cache_dir: str = "~/.cache/kicad-tools/lcsc"
    cache_ttl_days: int = 7


@dataclass
class FootprintValidationConfig:
    """Footprint validation configuration."""

    kicad_library_path: str | None = None
    tolerance_mm: float = 0.05
    library_mappings: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    """Merged configuration from all sources."""

    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    drc: DrcConfig = field(default_factory=DrcConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    route: RouteConfig = field(default_factory=RouteConfig)
    parts: PartsConfig = field(default_factory=PartsConfig)
    footprint_validation: FootprintValidationConfig = field(
        default_factory=FootprintValidationConfig
    )

    # Track which file each setting came from (for --show)
    _sources: dict = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, start_dir: Path | None = None) -> "Config":
        """
        Load configuration with precedence: project > user > defaults.

        Args:
            start_dir: Directory to start searching from (default: current directory)

        Returns:
            Merged configuration object
        """
        if start_dir is None:
            start_dir = Path.cwd()

        config = cls()
        sources: dict[str, str] = {}

        # Load user config first (lower precedence)
        if USER_CONFIG_PATH.exists():
            user_data = _load_toml_file(USER_CONFIG_PATH)
            if user_data:
                _merge_config(config, user_data, str(USER_CONFIG_PATH), sources)

        # Load project config (higher precedence)
        project_config = _find_project_config(start_dir)
        if project_config:
            project_data = _load_toml_file(project_config)
            if project_data:
                _merge_config(config, project_data, str(project_config), sources)

        config._sources = sources
        return config

    def get_source(self, key: str) -> str:
        """Get the source file for a config key."""
        return self._sources.get(key, "default")


class ConfigError(Exception):
    """Configuration-related errors."""

    pass


def _find_project_config(start_dir: Path) -> Path | None:
    """
    Find project config by walking up the directory tree.

    Stops at .git directory or filesystem root.

    Args:
        start_dir: Directory to start searching from

    Returns:
        Path to config file if found, None otherwise
    """
    current = start_dir.resolve()

    while True:
        # Check for config files in current directory
        for filename in CONFIG_FILENAMES:
            config_path = current / filename
            if config_path.is_file():
                return config_path

        # Stop at .git directory (project root)
        if (current / ".git").exists():
            break

        # Stop at filesystem root
        parent = current.parent
        if parent == current:
            break
        current = parent

    return None


def _load_toml_file(path: Path) -> dict[str, Any] | None:
    """
    Load a TOML file safely.

    Args:
        path: Path to TOML file

    Returns:
        Parsed TOML data or None on error

    Raises:
        ConfigError: If TOML is invalid
    """
    if tomllib is None:
        warnings.warn(
            "tomli package not installed. Config file support requires 'pip install tomli' for Python < 3.11.",
            stacklevel=2,
        )
        return None

    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Invalid TOML in {path}: {e}") from e
    except OSError as e:
        raise ConfigError(f"Cannot read config file {path}: {e}") from e


def _merge_config(
    config: Config, data: dict[str, Any], source: str, sources: dict[str, str]
) -> None:
    """
    Merge loaded config data into Config object.

    Args:
        config: Config object to update
        data: Raw config data from TOML
        source: Source file path (for tracking)
        sources: Dict to update with source info
    """
    # Warn about unknown top-level keys
    for key in data:
        if key not in KNOWN_KEYS:
            warnings.warn(f"Unknown config key '{key}' in {source}", stacklevel=3)

    # Merge defaults section
    if "defaults" in data:
        defaults_data = data["defaults"]
        _warn_unknown_keys(defaults_data, KNOWN_KEYS["defaults"], "defaults", source)

        if "format" in defaults_data:
            config.defaults.format = defaults_data["format"]
            sources["defaults.format"] = source
        if "manufacturer" in defaults_data:
            config.defaults.manufacturer = defaults_data["manufacturer"]
            sources["defaults.manufacturer"] = source
        if "verbose" in defaults_data:
            config.defaults.verbose = defaults_data["verbose"]
            sources["defaults.verbose"] = source
        if "quiet" in defaults_data:
            config.defaults.quiet = defaults_data["quiet"]
            sources["defaults.quiet"] = source

    # Merge drc section
    if "drc" in data:
        drc_data = data["drc"]
        _warn_unknown_keys(drc_data, KNOWN_KEYS["drc"], "drc", source)

        if "strict" in drc_data:
            config.drc.strict = drc_data["strict"]
            sources["drc.strict"] = source
        if "layers" in drc_data:
            config.drc.layers = drc_data["layers"]
            sources["drc.layers"] = source

    # Merge export section
    if "export" in data:
        export_data = data["export"]
        _warn_unknown_keys(export_data, KNOWN_KEYS["export"], "export", source)

        if "output_dir" in export_data:
            config.export.output_dir = export_data["output_dir"]
            sources["export.output_dir"] = source
        if "include_dnp" in export_data:
            config.export.include_dnp = export_data["include_dnp"]
            sources["export.include_dnp"] = source

    # Merge route section
    if "route" in data:
        route_data = data["route"]
        _warn_unknown_keys(route_data, KNOWN_KEYS["route"], "route", source)

        if "strategy" in route_data:
            config.route.strategy = route_data["strategy"]
            sources["route.strategy"] = source
        if "grid_resolution" in route_data:
            config.route.grid_resolution = route_data["grid_resolution"]
            sources["route.grid_resolution"] = source
        if "trace_width" in route_data:
            config.route.trace_width = route_data["trace_width"]
            sources["route.trace_width"] = source
        if "clearance" in route_data:
            config.route.clearance = route_data["clearance"]
            sources["route.clearance"] = source
        if "via_drill" in route_data:
            config.route.via_drill = route_data["via_drill"]
            sources["route.via_drill"] = source
        if "via_diameter" in route_data:
            config.route.via_diameter = route_data["via_diameter"]
            sources["route.via_diameter"] = source

    # Merge parts section
    if "parts" in data:
        parts_data = data["parts"]
        _warn_unknown_keys(parts_data, KNOWN_KEYS["parts"], "parts", source)

        if "cache_dir" in parts_data:
            config.parts.cache_dir = parts_data["cache_dir"]
            sources["parts.cache_dir"] = source
        if "cache_ttl_days" in parts_data:
            config.parts.cache_ttl_days = parts_data["cache_ttl_days"]
            sources["parts.cache_ttl_days"] = source

    # Merge footprint_validation section
    if "footprint_validation" in data:
        fpv_data = data["footprint_validation"]
        _warn_unknown_keys(
            fpv_data, KNOWN_KEYS["footprint_validation"], "footprint_validation", source
        )

        if "kicad_library_path" in fpv_data:
            config.footprint_validation.kicad_library_path = fpv_data["kicad_library_path"]
            sources["footprint_validation.kicad_library_path"] = source
        if "tolerance_mm" in fpv_data:
            config.footprint_validation.tolerance_mm = fpv_data["tolerance_mm"]
            sources["footprint_validation.tolerance_mm"] = source
        if "library_mappings" in fpv_data:
            config.footprint_validation.library_mappings = fpv_data["library_mappings"]
            sources["footprint_validation.library_mappings"] = source


def _warn_unknown_keys(data: dict[str, Any], known: set[str], section: str, source: str) -> None:
    """Warn about unknown keys in a config section."""
    for key in data:
        if key not in known:
            warnings.warn(f"Unknown config key '{section}.{key}' in {source}", stacklevel=4)


def generate_template() -> str:
    """
    Generate a template config file with all options documented.

    Returns:
        Template TOML string
    """
    return """# kicad-tools configuration file
# Place as .kicad-tools.toml in project root or ~/.config/kicad-tools/config.toml for user defaults

[defaults]
# Output format: table, json, csv
# format = "table"

# Default manufacturer for DRC checks: jlcpcb, pcbway, oshpark, seeed
# manufacturer = "jlcpcb"

# Enable verbose output by default
# verbose = false

# Enable quiet mode by default
# quiet = false

[drc]
# Use strict DRC checking
# strict = false

# Default number of PCB layers
# layers = 2

[export]
# Default output directory for exports
# output_dir = "./manufacturing"

# Include DNP (Do Not Populate) components in exports
# include_dnp = false

[route]
# Routing strategy: basic, negotiated, monte-carlo
# strategy = "negotiated"

# Grid resolution in mm
# grid_resolution = 0.1

# Default trace width in mm
# trace_width = 0.2

# Default clearance in mm
# clearance = 0.2

# Via drill size in mm
# via_drill = 0.3

# Via diameter in mm
# via_diameter = 0.6

[parts]
# Cache directory for LCSC parts data
# cache_dir = "~/.cache/kicad-tools/lcsc"

# Cache TTL in days
# cache_ttl_days = 7

[footprint_validation]
# Path to KiCad standard footprint library (auto-detected if not set)
# kicad_library_path = "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints"

# Tolerance for dimension comparison in mm
# tolerance_mm = 0.05

# Custom mappings from footprint names to library directories
# Useful for non-standard naming conventions
# [footprint_validation.library_mappings]
# "CustomCap_0402" = "Capacitor_SMD"
# "MyResistor_0603" = "Resistor_SMD"
"""


def get_config_paths() -> dict[str, Path | None]:
    """
    Get paths to config files that would be loaded.

    Returns:
        Dict with 'user' and 'project' keys
    """
    project_config = _find_project_config(Path.cwd())

    return {
        "user": USER_CONFIG_PATH if USER_CONFIG_PATH.exists() else None,
        "project": project_config,
    }
