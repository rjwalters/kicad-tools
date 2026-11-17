"""
Config command for kicad-tools CLI.

Provides commands to view, initialize, and manage configuration.

Usage:
    kct config --show          Show effective configuration with sources
    kct config --init          Create template config file
    kct config get <key>       Get a specific config value
    kct config set <key> <value>  Set a config value (requires manual edit)

Machine output (``--format json``, issue #4674): every mode prints exactly one
JSON document on stdout, keyed by ``command`` (always ``"config"``) and
``action`` (``show`` / ``paths`` / ``init`` / ``get`` / ``set``).  Failures
carry an ``error`` string with the exit code unchanged.  See
``docs/reference/machine-output.md``.
"""

import argparse
import sys
from pathlib import Path
from typing import Any

from kicad_tools.cli.format_options import FORMAT_JSON, add_format_flag, emit_json
from kicad_tools.config import (
    CONFIG_FILENAMES,
    USER_CONFIG_PATH,
    Config,
    ConfigError,
    generate_template,
    get_config_paths,
)
from kicad_tools.utils import ensure_parent_dir

#: Sections and keys rendered by ``--show``, in display order.  Single source
#: of truth so the prose table and the JSON document can never drift.
_SHOWN_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("defaults", ("format", "manufacturer", "verbose", "quiet")),
    ("drc", ("strict", "layers")),
    ("export", ("output_dir", "include_dnp")),
    (
        "route",
        (
            "strategy",
            "grid_resolution",
            "trace_width",
            "clearance",
            "via_drill",
            "via_diameter",
        ),
    ),
    ("parts", ("cache_dir", "cache_ttl_days")),
)

#: C++ native backends probed by ``--show``.
_NATIVE_BACKENDS: tuple[tuple[str, str], ...] = (
    ("router", "kicad_tools.router.cpp_backend"),
    ("placement", "kicad_tools.placement.cpp_backend"),
    ("drc", "kicad_tools.drc.cpp_backend"),
)


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
    add_format_flag(parser)

    args = parser.parse_args(argv)
    fmt = getattr(args, "format", "text")

    try:
        if args.show:
            return _show_config(fmt)
        elif args.init:
            return _init_config(args.user, fmt)
        elif args.paths:
            return _show_paths(fmt)
        elif args.action == "get":
            if not args.key:
                return _fail("get", "'get' requires a key argument", fmt)
            return _get_config(args.key, fmt)
        elif args.action == "set":
            if not args.key or not args.value:
                return _fail("set", "'set' requires key and value arguments", fmt)
            return _set_config(args.key, args.value, fmt)
        else:
            # Default to showing config
            return _show_config(fmt)

    except ConfigError as e:
        action = args.action or ("init" if args.init else "paths" if args.paths else "show")
        return _fail(action, str(e), fmt)


def _fail(action: str, message: str, output_format: str, **fields: Any) -> int:
    """Report a failure in the requested format and return exit code 1."""
    if output_format == FORMAT_JSON:
        payload: dict[str, Any] = {
            "command": "config",
            "action": action,
            "error": message,
            "success": False,
        }
        payload.update(fields)
        emit_json(payload)
        return 1

    print(f"Error: {message}", file=sys.stderr)
    return 1


def _show_config(output_format: str = "text") -> int:
    """Show effective configuration with sources."""
    config = Config.load()

    sections: dict[str, dict[str, dict[str, Any]]] = {}
    for section, keys in _SHOWN_SECTIONS:
        section_obj = getattr(config, section)
        sections[section] = {
            key: {
                "value": getattr(section_obj, key),
                "source": config.get_source(f"{section}.{key}"),
            }
            for key in keys
        }

    backends = _native_backend_status()

    if output_format == FORMAT_JSON:
        emit_json(
            {
                "command": "config",
                "action": "show",
                "sections": sections,
                "native_backends": backends,
                "success": True,
            }
        )
        return 0

    print("# Effective kicad-tools configuration")
    print()

    for section, keys in _SHOWN_SECTIONS:
        print(f"[{section}]")
        for key in keys:
            entry = sections[section][key]
            _print_value(key, entry["value"], entry["source"])
        print()

    # Show native backend status
    print("[native_backends]")
    _print_native_backend_status(backends)

    return 0


def _native_backend_status() -> dict[str, dict[str, Any]]:
    """Probe the C++ native backends and return their status per backend."""
    status: dict[str, dict[str, Any]] = {}
    for name, module_path in _NATIVE_BACKENDS:
        try:
            import importlib

            mod = importlib.import_module(module_path)
            info = mod.get_backend_info()
            available = bool(info.get("available"))
            status[name] = {
                "available": available,
                "version": str(info.get("version", "unknown")) if available else None,
            }
        except Exception:
            status[name] = {"available": False, "version": None}
    return status


def _print_native_backend_status(backends: dict[str, dict[str, Any]]) -> None:
    """Print status of C++ native backends."""
    any_missing = False
    for name, info in backends.items():
        if info["available"]:
            print(f'{name} = "cpp v{info["version"]}"  # installed')
        else:
            print(f'{name} = "python (fallback)"  # NOT installed')
            any_missing = True
    if any_missing:
        print("# Run 'kct build-native' for 10-100x faster routing/placement")


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


def _show_paths(output_format: str = "text") -> int:
    """Show config file paths."""
    paths = get_config_paths()

    if output_format == FORMAT_JSON:
        emit_json(
            {
                "command": "config",
                "action": "paths",
                "project_config": {
                    "path": str(paths["project"]) if paths["project"] else None,
                    "exists": bool(paths["project"]),
                    "search_filenames": sorted(CONFIG_FILENAMES),
                },
                "user_config": {
                    "path": str(USER_CONFIG_PATH),
                    "exists": bool(paths["user"]),
                },
                "success": True,
            }
        )
        return 0

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


def _init_config(user: bool = False, output_format: str = "text") -> int:
    """Create a template config file."""
    if user:
        target = USER_CONFIG_PATH
        ensure_parent_dir(target)
    else:
        target = Path.cwd() / CONFIG_FILENAMES[0]  # .kicad-tools.toml

    scope = "user" if user else "project"

    if target.exists():
        if output_format == FORMAT_JSON:
            return _fail(
                "init",
                f"Config file already exists: {target}",
                output_format,
                path=str(target),
                scope=scope,
                created=False,
            )
        print(f"Error: Config file already exists: {target}", file=sys.stderr)
        print("Remove it first or edit manually.", file=sys.stderr)
        return 1

    template = generate_template()

    try:
        target.write_text(template)
    except OSError as e:
        if output_format == FORMAT_JSON:
            return _fail(
                "init",
                f"Error writing config file: {e}",
                output_format,
                path=str(target),
                scope=scope,
                created=False,
            )
        print(f"Error writing config file: {e}", file=sys.stderr)
        return 1

    if output_format == FORMAT_JSON:
        emit_json(
            {
                "command": "config",
                "action": "init",
                "path": str(target),
                "scope": scope,
                "created": True,
                "success": True,
            }
        )
        return 0

    print(f"Created config template: {target}")
    print()
    print("Edit the file to customize your settings.")
    print("Uncomment and modify values as needed.")
    return 0


def _get_config(key: str, output_format: str = "text") -> int:
    """Get a specific config value."""
    config = Config.load()

    # Parse the key (e.g., "defaults.format")
    parts = key.split(".")
    if len(parts) != 2:
        return _fail(
            "get", f"Invalid key format '{key}'. Use 'section.key' format.", output_format, key=key
        )

    section, attr = parts

    # Get the section object
    section_obj = getattr(config, section, None)
    if section_obj is None:
        return _fail("get", f"Unknown config section '{section}'", output_format, key=key)

    # Get the value
    if not hasattr(section_obj, attr):
        return _fail("get", f"Unknown key '{attr}' in section '{section}'", output_format, key=key)

    value = getattr(section_obj, attr)
    source = config.get_source(key)

    if output_format == FORMAT_JSON:
        emit_json(
            {
                "command": "config",
                "action": "get",
                "key": key,
                "section": section,
                "option": attr,
                "value": value,
                "source": source,
                "success": True,
            }
        )
        return 0

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


def _set_config(key: str, value: str, output_format: str = "text") -> int:
    """
    Guide user to set a config value.

    We don't modify config files directly to avoid complexity with TOML formatting.
    Instead, we show the user what to add to their config file.
    """
    # Parse the key
    parts = key.split(".")
    if len(parts) != 2:
        return _fail(
            "set", f"Invalid key format '{key}'. Use 'section.key' format.", output_format, key=key
        )

    section, attr = parts

    # Validate section
    config = Config.load()
    section_obj = getattr(config, section, None)
    if section_obj is None:
        return _fail("set", f"Unknown config section '{section}'", output_format, key=key)

    # Validate key exists
    if not hasattr(section_obj, attr):
        return _fail("set", f"Unknown key '{attr}' in section '{section}'", output_format, key=key)

    # Get current value type for formatting
    current = getattr(section_obj, attr)

    # Format value appropriately
    if isinstance(current, bool):
        if value.lower() in ("true", "1", "yes"):
            formatted = "true"
        elif value.lower() in ("false", "0", "no"):
            formatted = "false"
        else:
            return _fail("set", f"Invalid boolean value '{value}'", output_format, key=key)
    elif isinstance(current, int):
        try:
            int(value)
            formatted = value
        except ValueError:
            return _fail("set", f"Invalid integer value '{value}'", output_format, key=key)
    elif isinstance(current, float):
        try:
            float(value)
            formatted = value
        except ValueError:
            return _fail("set", f"Invalid float value '{value}'", output_format, key=key)
    else:
        formatted = f'"{value}"'

    # Show what to add
    paths = get_config_paths()
    project_config = paths["project"] or Path.cwd() / CONFIG_FILENAMES[0]

    if output_format == FORMAT_JSON:
        # `set` never writes: it reports the TOML the user must add.  The
        # `applied: false` key makes that explicit for machine callers.
        emit_json(
            {
                "command": "config",
                "action": "set",
                "key": key,
                "section": section,
                "option": attr,
                "value": formatted,
                "applied": False,
                "config_file": str(project_config),
                "toml": f"[{section}]\n{attr} = {formatted}",
                "success": True,
            }
        )
        return 0

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
