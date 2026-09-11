"""Advisory presence checks for KiCad's ``~<full filename>.lck`` markers.

Ownership metadata never establishes process liveness. This check neither
acquires a lock nor prevents another process from opening a file afterward.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

LOCK_POLICY_ENV = "KCT_KICAD_LOCK_POLICY"


@dataclass(frozen=True)
class KiCadLockPresence:
    """Observed marker presence, with optional unverified ownership metadata.

    A marker whose existence cannot be inspected is conservatively treated as
    present. Empty, unreadable, and malformed markers have unknown ownership.
    """

    path: Path
    present: bool
    username: str | None = None
    hostname: str | None = None


def probe_kicad_lock(path: str | Path) -> KiCadLockPresence:
    """Inspect only the exact sibling marker; never modify it or infer liveness."""
    destination = Path(path)
    marker = destination.with_name(f"~{destination.name}.lck")
    try:
        info = marker.lstat()
    except FileNotFoundError:
        return KiCadLockPresence(marker, False)
    except OSError:
        return KiCadLockPresence(marker, True)
    if not stat.S_ISREG(info.st_mode):
        return KiCadLockPresence(marker, True)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(marker, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return KiCadLockPresence(marker, True)
            text = stream.read(8193)
        if len(text) > 8192:
            return KiCadLockPresence(marker, True)
        data = json.loads(text)
        if not isinstance(data, dict):
            return KiCadLockPresence(marker, True)
        username = data.get("username")
        hostname = data.get("hostname")
        return KiCadLockPresence(
            marker,
            True,
            username if isinstance(username, str) else None,
            hostname if isinstance(hostname, str) else None,
        )
    except (OSError, ValueError, RecursionError):
        return KiCadLockPresence(marker, True)


def check_kicad_lock(path: str | Path, *, policy: str | None = None) -> KiCadLockPresence | None:
    """Apply ``warn`` (default), ``error``, or ``ignore`` before a write.

    Explicit policy overrides the environment. An invalid value always raises
    ValueError. Ignore skips filesystem inspection. Error raises PermissionError
    before a writer touches its output; warn prints only to stderr.
    """
    selected = policy if policy is not None else os.environ.get(LOCK_POLICY_ENV, "warn")
    if selected not in ("warn", "error", "ignore"):
        raise ValueError(
            f"Invalid {LOCK_POLICY_ENV} value {selected!r}; expected warn, error or ignore"
        )
    if selected == "ignore":
        return None
    presence = probe_kicad_lock(path)
    if presence.present:
        owner = ", ".join(
            f"{name}={value!r}"
            for name, value in (("username", presence.username), ("hostname", presence.hostname))
            if value is not None
        )
        message = (
            f"{str(Path(path))!r} may be open in KiCad: lock marker {str(presence.path)!r} "
            f"({owner or 'unknown ownership'}). Marker presence does not prove a live session. "
            f"Set {LOCK_POLICY_ENV}=ignore to bypass this advisory check."
        )
        if selected == "error":
            raise PermissionError(message)
        print(f"Warning: {message}", file=sys.stderr)
    return presence
