"""Shared atomic-write primitive for KiCad file writers.

Issue #4898 (Part of #4880's Konnect-ideas audit, item 3): every low-level
``save_*`` writer in this codebase used a plain ``Path.write_text(...)`` --
no tmp file, no ``fsync``, no ``os.replace`` -- so a SIGKILL/OOM/power-loss
between "bytes handed to the OS" and "final flush" could leave a *torn*
(truncated or partially-written) ``.kicad_pcb`` / ``.kicad_sch`` /
``.kicad_pro`` / ``.kicad_mod`` / ``.kicad_dru`` file on disk.

``route_cmd.py``'s ``_write_routed_pcb()`` already solved this for the
terminal routed-PCB save path (Issue #2808): write to a sibling ``.tmp``
file, ``fsync`` it, then ``os.replace()`` it onto the final path -- an
atomic rename on POSIX filesystems, so a crash mid-write leaves either the
old content or the new content, never a torn mix of both. This module
extracts that same pattern into a single, reusable helper so every writer
(not just the router's own output path) gets the same guarantee.

Not a port of Konnect's ``transaction.rs`` (AGPL-3.0) -- this is the
minimal write -> fsync -> rename primitive, written from scratch, with no
WAL journal, no cross-file transaction, and no lock detection. See the
PR/issue discussion for the audit of which additional pieces of Konnect's
design were adopted vs. declined for this codebase.
"""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write_text(
    path: str | Path,
    content: str,
    *,
    encoding: str | None = "utf-8",
) -> None:
    """Atomically write ``content`` to ``path``.

    Writes to a sibling ``<path>.tmp`` file (same directory, so the final
    ``os.replace`` is guaranteed to be a same-filesystem rename -- atomic
    on POSIX), ``fsync``s it so the bytes are durable before the rename,
    then atomically replaces ``path`` with the tmp file's contents.

    If the process is killed (or the filesystem errors) at any point
    before ``os.replace`` completes, ``path`` is left with whatever
    content it had before this call -- never a truncated or partially
    written file. The ``.tmp`` file is intentionally left on disk on
    failure (not cleaned up) so the caller/operator can inspect what was
    being written; callers that need cleanup-on-failure should do so
    explicitly.

    Args:
        path: Destination file path.
        content: Text content to write.
        encoding: Encoding to use, or ``None`` to fall back to
            ``Path.write_text``'s platform-default encoding (matches the
            historical, unparameterized ``path.write_text(content)`` some
            callers relied on before this helper existed).

    Raises:
        OSError: If the write, fsync, or replace fails. On failure the
            original ``path`` (if it existed) is left untouched.
    """
    path = Path(path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")

    if encoding is None:
        tmp_path.write_text(content)
    else:
        tmp_path.write_text(content, encoding=encoding)

    with open(tmp_path, "rb") as f:
        os.fsync(f.fileno())

    os.replace(tmp_path, path)
