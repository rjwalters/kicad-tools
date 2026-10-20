"""Portable symlink-refusing file primitives (issue #7529).

``os.O_NOFOLLOW``, ``os.O_DIRECTORY`` and descriptor-relative ``dir_fd`` opens
are POSIX-only: on Windows the attributes do not exist and touching them raises
``AttributeError``. This module is the **only** place in ``src/`` allowed to
name those flags directly (``tests/test_jlc_upload_windows_flags.py`` enforces
it). Callers go through the helpers below, which use the real flags on POSIX
(behaviour unchanged) and fall back to ``lstat``-style checks elsewhere.

The fallbacks are check-then-open and therefore not atomic against a
concurrent swap of a path component; that is the best Windows offers.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

# Windows reparse tag for directory junctions / mount points (winnt.h).
_IO_REPARSE_TAG_MOUNT_POINT = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def supports_dir_fd() -> bool:
    """True where ``O_DIRECTORY``/``O_NOFOLLOW`` descriptor walks are available."""
    return hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")


def posix_dir_flags() -> tuple[int, int]:
    """Return ``(O_DIRECTORY, O_NOFOLLOW)``; call only when :func:`supports_dir_fd`."""
    return os.O_DIRECTORY, os.O_NOFOLLOW


def is_link(path: str | os.PathLike[str]) -> bool:
    """True for a symlink or, on Windows, a directory junction.

    ``os.path.islink`` misses junctions, which redirect a path just like a
    directory symlink. ``os.path.isjunction`` (3.12+) catches them; on older
    Pythons the ``lstat`` reparse tag is checked instead. Other reparse points
    (e.g. OneDrive cloud placeholders) are deliberately not refused.
    """
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        return bool(isjunction(path))
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return getattr(info, "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT


def open_nofollow(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
    """``os.open`` that refuses a symlinked final component.

    Uses ``O_NOFOLLOW`` where it exists; otherwise rejects a symlink or
    junction via :func:`is_link` before opening.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow and is_link(path):
        raise OSError(errno.ELOOP, "refusing to follow symlink", str(path))
    return os.open(path, flags | nofollow, mode)


def fsync_dir(path: Path) -> None:
    """fsync a directory so a new entry is durable; a no-op where unsupported."""
    directory = getattr(os, "O_DIRECTORY", 0)
    if not directory:
        return  # Windows cannot open a directory fd for fsync
    dir_fd = os.open(path, os.O_RDONLY | directory)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
