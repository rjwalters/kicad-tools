"""
Config command for kicad-tools CLI.

Provides commands to view, initialize, and manage configuration.

Usage:
    kct config --show          Show effective configuration with sources
    kct config --init          Create template config file
    kct config get <key>       Get a specific config value
    kct config set <key> <value>  Set a config value (requires manual edit)
"""

import argparse
import sys
from pathlib import Path

from kicad_tools.config import (
    CONFIG_FILENAMES,
    USER_CONFIG_PATH,
    Config,
    ConfigError,
    generate_template,
    get_config_paths,
)
from kicad_tools.utils import ensure_parent_dir


def main(argv: list[str] | None = None) -> int:
    """Main entry point for config command."""
    parser = argparse.ArgumentParser(
        prog="kct config",
        description="Manage kicad-tools configuration",
    )

    # Mutually exclusive main actions
    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument(
        "--show",
        action="store_true",
        help="Show effective configuration with sources",
    )
    action_group.add_argument(
        "--init",
        action="store_true",
        help="Create template config file in current directory",
    )
    action_group.add_argument(
        "--paths",
        action="store_true",
        help="Show config file paths",
    )

    # Subcommand-style actions
    parser.add_argument(
        "action",
        nargs="?",
        choices=["get", "set"],
        help="Config action (get/set)",
    )
    parser.add_argument(
        "key",
        nargs="?",
        help="Config key (e.g., defaults.format)",
    )
    parser.add_argument(
        "value",
        nargs="?",
        help="Value to set",
    )

    # Options
    parser.add_argument(
        "--user",
        action="store_true",
        help="Use user config (~/.config/kicad-tools/config.toml) for --init",
    )

    args = parser.parse_args(argv)

    try:
        if args.show:
            return _show_config()
        elif args.init:
            return _init_config(args.user)
        elif args.paths:
            return _show_paths()
        elif args.action == "get":
            if not args.key:
                print("Error: 'get' requires a key argument", file=sys.stderr)
                return 1
            return _get_config(args.key)
        elif args.action == "set":
            if not args.key or not args.value:
                print("Error: 'set' requires key and value arguments", file=sys.stderr)
                return 1
            return _set_config(args.key, args.value)
        else:
            # Default to showing config
            return _show_config()

    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def _show_config() -> int:
    """Show effective configuration with sources."""
    config = Config.load()

    print("# Effective kicad-tools configuration")
    print()

    # Show defaults section
    print("[defaults]")
    _print_value("format", config.defaults.format, config.get_source("defaults.format"))
    _print_value(
        "manufacturer", config.defaults.manufacturer, config.get_source("defaults.manufacturer")
    )
    _print_value("verbose", config.defaults.verbose, config.get_source("defaults.verbose"))
    _print_value("quiet", config.defaults.quiet, config.get_source("defaults.quiet"))
    print()

    # Show drc section
    print("[drc]")
    _print_value("strict", config.drc.strict, config.get_source("drc.strict"))
    _print_value("layers", config.drc.layers, config.get_source("drc.layers"))
    print()

    # Show export section
    print("[export]")
    _print_value("output_dir", config.export.output_dir, config.get_source("export.output_dir"))
    _print_value("include_dnp", config.export.include_dnp, config.get_source("export.include_dnp"))
    print()

    # Show route section
    print("[route]")
    _print_value("strategy", config.route.strategy, config.get_source("route.strategy"))
    _print_value(
        "grid_resolution", config.route.grid_resolution, config.get_source("route.grid_resolution")
    )
    _print_value("trace_width", config.route.trace_width, config.get_source("route.trace_width"))
    _print_value("clearance", config.route.clearance, config.get_source("route.clearance"))
    _print_value("via_drill", config.route.via_drill, config.get_source("route.via_drill"))
    _print_value("via_diameter", config.route.via_diameter, config.get_source("route.via_diameter"))
    print()

    # Show parts section
    print("[parts]")
    _print_value("cache_dir", config.parts.cache_dir, config.get_source("parts.cache_dir"))
    _print_value(
        "cache_ttl_days", config.parts.cache_ttl_days, config.get_source("parts.cache_ttl_days")
    )

    return 0


def _print_value(key: str, value, source: str) -> None:
    """Print a config value with its source."""
    if isinstance(value, str):
        formatted = f'"{value}"'
    elif isinstance(value, bool):
        formatted = "true" if value else "false"
    elif value is None:
        formatted = "# not set"
    else:
        formatted = str(value)

    # Truncate long source paths
    if source != "default":
        # Show just filename for brevity
        source_display = Path(source).name
    else:
        source_display = source

    print(f"{key} = {formatted}  # from: {source_display}")


def _show_paths() -> int:
    """Show config file paths."""
    paths = get_config_paths()

    print("Config file paths:")
    print()

    print(f"User config: {USER_CONFIG_PATH}")
    if paths["user"]:
        print("  Status: exists")
    else:
        print("  Status: not found")
    print()

    print(f"Project config search: {', '.join(CONFIG_FILENAMES)}")
    if paths["project"]:
        print(f"  Found: {paths['project']}")
    else:
        print("  Status: not found")

    return 0


def _init_config(user: bool = False) -> int:
    """Create a template config file."""
    if user:
        target = USER_CONFIG_PATH
        ensure_parent_dir(target)
    else:
        target = Path.cwd() / CONFIG_FILENAMES[0]  # .kicad-tools.toml

    if target.exists():
        print(f"Error: Config file already exists: {target}", file=sys.stderr)
        print("Remove it first or edit manually.", file=sys.stderr)
        return 1

    template = generate_template()

    try:
        target.write_text(template)
        print(f"Created config template: {target}")
        print()
        print("Edit the file to customize your settings.")
        print("Uncomment and modify values as needed.")
        return 0
    except OSError as e:
        print(f"Error writing config file: {e}", file=sys.stderr)
        return 1


def _get_config(key: str) -> int:
    """Get a specific config value."""
    config = Config.load()

    # Parse the key (e.g., "defaults.format")
    parts = key.split(".")
    if len(parts) != 2:
        print(f"Error: Invalid key format '{key}'. Use 'section.key' format.", file=sys.stderr)
        return 1

    section, attr = parts

    # Get the section object
    section_obj = getattr(config, section, None)
    if section_obj is None:
        print(f"Error: Unknown config section '{section}'", file=sys.stderr)
        return 1

    # Get the value
    if not hasattr(section_obj, attr):
        print(f"Error: Unknown key '{attr}' in section '{section}'", file=sys.stderr)
        return 1

    value = getattr(section_obj, attr)
    source = config.get_source(key)

    if value is None:
        print("# not set")
    elif isinstance(value, str):
        print(value)
    elif isinstance(value, bool):
        print("true" if value else "false")
    else:
        print(value)

    # Show source if not default
    if source != "default":
        print(f"# source: {source}", file=sys.stderr)

    return 0


def _set_config(key: str, value: str) -> int:
    """
    Guide user to set a config value.

    We don't modify config files directly to avoid complexity with TOML formatting.
    Instead, we show the user what to add to their config file.
    """
    # Parse the key
    parts = key.split(".")
    if len(parts) != 2:
        print(f"Error: Invalid key format '{key}'. Use 'section.key' format.", file=sys.stderr)
        return 1

    section, attr = parts

    # Validate section
    config = Config.load()
    section_obj = getattr(config, section, None)
    if section_obj is None:
        print(f"Error: Unknown config section '{section}'", file=sys.stderr)
        return 1

    # Validate key exists
    if not hasattr(section_obj, attr):
        print(f"Error: Unknown key '{attr}' in section '{section}'", file=sys.stderr)
        return 1

    # Get current value type for formatting
    current = getattr(section_obj, attr)

    # Format value appropriately
    if isinstance(current, bool):
        if value.lower() in ("true", "1", "yes"):
            formatted = "true"
        elif value.lower() in ("false", "0", "no"):
            formatted = "false"
        else:
            print(f"Error: Invalid boolean value '{value}'", file=sys.stderr)
            return 1
    elif isinstance(current, int):
        try:
            int(value)
            formatted = value
        except ValueError:
            print(f"Error: Invalid integer value '{value}'", file=sys.stderr)
            return 1
    elif isinstance(current, float):
        try:
            float(value)
            formatted = value
        except ValueError:
            print(f"Error: Invalid float value '{value}'", file=sys.stderr)
            return 1
    else:
        formatted = f'"{value}"'

    # Show what to add
    paths = get_config_paths()
    project_config = paths["project"] or Path.cwd() / CONFIG_FILENAMES[0]

    print(f"To set {key} = {formatted}, add to your config file:")
    print()
    print(f"  File: {project_config}")
    print()
    print(f"  [{section}]")
    print(f"  {attr} = {formatted}")
    print()
    print("Or run: kct config --init")

    return 0


if __name__ == "__main__":
    sys.exit(main())
