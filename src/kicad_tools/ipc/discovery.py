"""Auto-discover running KiCad instances and their IPC socket paths.

KiCad 9.0+ writes its NNG socket path to a well-known location. This module
searches those locations to find running KiCad instances.

Discovery strategy:
1. Check ``KICAD_IPC_SOCKET`` environment variable (explicit override)
2. Search platform-specific runtime directories for socket files
3. Fall back to explicit socket path argument

Supported platforms:
- macOS: ``$TMPDIR/kicad/`` or ``/tmp/kicad/``
- Linux: ``$XDG_RUNTIME_DIR/kicad/`` or ``/tmp/kicad/``
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class KiCadInstance:
    """Represents a discovered running KiCad instance.

    Attributes:
        socket_path: Path to the NNG IPC socket.
        pid: Process ID of the KiCad instance, if known.
        version: KiCad version string, if known.
    """

    socket_path: Path
    pid: int | None = None
    version: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:
        parts = [f"KiCad@{self.socket_path}"]
        if self.pid:
            parts.append(f"pid={self.pid}")
        if self.version:
            parts.append(f"v{self.version}")
        return " ".join(parts)


def _get_search_dirs() -> list[Path]:
    """Return platform-specific directories to search for KiCad sockets.

    Returns:
        List of directories to search, in priority order.
    """
    dirs: list[Path] = []

    if sys.platform == "darwin":
        # macOS: $TMPDIR is per-user (e.g., /var/folders/.../T/)
        tmpdir = os.environ.get("TMPDIR", "/tmp")
        dirs.append(Path(tmpdir) / "kicad")
        dirs.append(Path("/tmp") / "kicad")
    elif sys.platform == "win32":
        # Windows: %LOCALAPPDATA%\kicad or %TEMP%\kicad
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            dirs.append(Path(local_appdata) / "kicad")
        temp = os.environ.get("TEMP", os.environ.get("TMP", ""))
        if temp:
            dirs.append(Path(temp) / "kicad")
    else:
        # Linux: XDG_RUNTIME_DIR is the standard location
        xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", "")
        if xdg_runtime:
            dirs.append(Path(xdg_runtime) / "kicad")
        dirs.append(Path("/tmp") / "kicad")

    return dirs


def discover_socket(explicit_path: str | Path | None = None) -> Path | None:
    """Find the socket path for a running KiCad instance.

    Args:
        explicit_path: If provided, use this path directly instead of
            auto-discovering. The path is validated to exist.

    Returns:
        Path to the NNG socket if found, None otherwise.
    """
    # 1. Explicit path takes priority
    if explicit_path is not None:
        path = Path(explicit_path)
        if path.exists():
            return path
        return None

    # 2. Environment variable override
    env_socket = os.environ.get("KICAD_IPC_SOCKET")
    if env_socket:
        path = Path(env_socket)
        if path.exists():
            return path

    # 3. Search platform-specific directories
    for search_dir in _get_search_dirs():
        if not search_dir.is_dir():
            continue
        # KiCad creates socket files with .sock extension
        for sock_file in sorted(search_dir.glob("*.sock")):
            return sock_file
        # Also check for plain socket files (no extension)
        for entry in sorted(search_dir.iterdir()):
            if _is_socket(entry):
                return entry

    return None


def discover_instances() -> list[KiCadInstance]:
    """Find all running KiCad instances with active IPC sockets.

    Returns:
        List of discovered KiCad instances, possibly empty.
    """
    instances: list[KiCadInstance] = []
    seen_paths: set[Path] = set()

    # Check environment variable first
    env_socket = os.environ.get("KICAD_IPC_SOCKET")
    if env_socket:
        path = Path(env_socket)
        if path.exists():
            instances.append(KiCadInstance(socket_path=path))
            seen_paths.add(path.resolve())

    # Search platform directories
    for search_dir in _get_search_dirs():
        if not search_dir.is_dir():
            continue

        for entry in sorted(search_dir.iterdir()):
            resolved = entry.resolve()
            if resolved in seen_paths:
                continue

            if entry.suffix == ".sock" or _is_socket(entry):
                instances.append(KiCadInstance(socket_path=entry))
                seen_paths.add(resolved)

    return instances


def _is_socket(path: Path) -> bool:
    """Check if a path is a Unix domain socket.

    Args:
        path: Path to check.

    Returns:
        True if the path is a socket file.
    """
    try:
        import stat

        return stat.S_ISSOCK(path.stat().st_mode)
    except (OSError, ValueError):
        return False
