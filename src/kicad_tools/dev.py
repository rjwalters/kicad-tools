"""
Development utilities for working with kicad-tools source code.

This module helps detect version mismatches when working with the
kicad-tools source repository while also having a pipx-installed version.

Usage in board scripts:
    from kicad_tools.dev import warn_if_stale
    warn_if_stale()  # Prints warning if installed version differs from source
"""

from __future__ import annotations

import sys
from pathlib import Path

# Cache the warning state to avoid repeated warnings
_warned = False


def get_installed_version() -> str:
    """Get the version of the installed kicad-tools package.

    Returns:
        Version string (e.g., "0.9.3") or "unknown" if not installed.
    """
    try:
        from importlib.metadata import version

        return version("kicad-tools")
    except Exception:
        return "unknown"


def get_source_version(source_dir: Path | None = None) -> str | None:
    """Get the version from pyproject.toml in the source directory.

    Args:
        source_dir: Path to the source directory containing pyproject.toml.
                   If None, searches upward from the current file.

    Returns:
        Version string (e.g., "0.9.3") or None if not found.
    """
    if source_dir is None:
        # Search upward from this file to find pyproject.toml
        current = Path(__file__).resolve()
        for parent in [current] + list(current.parents):
            pyproject = parent / "pyproject.toml"
            if pyproject.exists():
                source_dir = parent
                break

    if source_dir is None:
        return None

    pyproject_path = source_dir / "pyproject.toml"
    if not pyproject_path.exists():
        return None

    try:
        # Try tomllib (Python 3.11+) first, then tomli
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore

        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
            return data.get("project", {}).get("version")
    except Exception:
        return None


def find_source_root() -> Path | None:
    """Find the kicad-tools source root directory.

    Returns:
        Path to the source root (containing pyproject.toml) or None.
    """
    # Check if we're running from a script in a known location
    # First, check the caller's location
    import inspect

    for frame_info in inspect.stack():
        frame_path = Path(frame_info.filename).resolve()

        # Search upward for pyproject.toml with kicad-tools
        for parent in [frame_path] + list(frame_path.parents):
            pyproject = parent / "pyproject.toml"
            if pyproject.exists():
                try:
                    try:
                        import tomllib
                    except ImportError:
                        import tomli as tomllib  # type: ignore

                    with open(pyproject, "rb") as f:
                        data = tomllib.load(f)
                        if data.get("project", {}).get("name") == "kicad-tools":
                            return parent
                except Exception:
                    continue
    return None


def check_version_match(source_dir: Path | None = None) -> tuple[bool, str, str | None]:
    """Check if the installed version matches the source version.

    Args:
        source_dir: Path to source directory, or None to auto-detect.

    Returns:
        Tuple of (match, installed_version, source_version).
        - match: True if versions match or source not found
        - installed_version: The installed package version
        - source_version: The source version, or None if not found
    """
    installed = get_installed_version()

    if source_dir is None:
        source_dir = find_source_root()

    if source_dir is None:
        return True, installed, None

    source = get_source_version(source_dir)

    if source is None:
        return True, installed, None

    return installed == source, installed, source


def warn_if_stale(source_dir: Path | None = None, force: bool = False) -> bool:
    """Print a warning if the installed version differs from source.

    This is useful for board generation scripts in the repository that
    import from kicad_tools. If a user has pipx-installed kicad-tools
    but is running scripts from the source repo, the imports will use
    the pipx version which may be stale.

    Args:
        source_dir: Path to source directory, or None to auto-detect.
        force: If True, always check and warn even if already warned.

    Returns:
        True if versions match (or source not found), False if mismatch.

    Example:
        # At the top of a board generation script
        from kicad_tools.dev import warn_if_stale
        warn_if_stale()
    """
    global _warned

    if _warned and not force:
        return True

    match, installed, source = check_version_match(source_dir)

    if not match and source is not None:
        _warned = True
        print(
            "\n⚠️  Version mismatch detected!",
            file=sys.stderr,
        )
        print(
            f"   Installed: {installed}",
            file=sys.stderr,
        )
        print(
            f"   Source:    {source}",
            file=sys.stderr,
        )
        print(
            "\n   You may be running source scripts with an older pipx install.",
            file=sys.stderr,
        )
        print(
            "   To fix, reinstall from source:\n",
            file=sys.stderr,
        )
        print(
            "       pipx install --force .",
            file=sys.stderr,
        )
        print(
            "\n   Or use an editable install for development:\n",
            file=sys.stderr,
        )
        print(
            "       pipx uninstall kicad-tools",
            file=sys.stderr,
        )
        print(
            "       pip install -e '.[dev]'\n",
            file=sys.stderr,
        )
        return False

    return True


def require_source_version(source_dir: Path | None = None) -> None:
    """Raise an error if the installed version differs from source.

    This is a stricter version of warn_if_stale() that stops execution
    when a version mismatch is detected.

    Args:
        source_dir: Path to source directory, or None to auto-detect.

    Raises:
        RuntimeError: If versions don't match.
    """
    match, installed, source = check_version_match(source_dir)

    if not match and source is not None:
        raise RuntimeError(
            f"Version mismatch: installed {installed} != source {source}. "
            f"Run 'pipx install --force .' or 'pip install -e .[dev]' to update."
        )
