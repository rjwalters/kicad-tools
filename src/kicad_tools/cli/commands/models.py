"""``kct models`` command handlers (issue #7683).

* ``kct models sync [--sparse]`` -- fetch the pinned, digest-verified subset
  of KiCad's stock 3D models into the local cache.
* ``kct models status`` -- report whether the cache is complete.
"""

from __future__ import annotations

import json
import shutil
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argparse import Namespace

__all__ = ["run_models_command"]


def run_models_command(args: Namespace) -> int:
    """Handle ``kct models`` and its sub-actions."""
    from kicad_tools.pcb import models3d_pinned as pinned

    subcommand = getattr(args, "models_command", None)
    if subcommand not in ("sync", "status"):
        print("Usage: kct models <command> [options]")
        print("Commands: sync, status")
        return 1

    try:
        manifest = pinned.load_manifest()
    except pinned.ModelSyncError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    root = pinned.cache_dir(manifest.tag)
    fmt = getattr(args, "models_format", "text")

    if subcommand == "status":
        stale = pinned.verify_cache(manifest, root)
        synced = not stale
        if fmt == "json":
            print(
                json.dumps(
                    {
                        "tag": manifest.tag,
                        "dir": str(root),
                        "files": len(manifest),
                        "missing_or_invalid": stale,
                        "synced": synced,
                    },
                    indent=2,
                )
            )
        else:
            state = "synced" if synced else f"not synced ({len(stale)} of {len(manifest)} missing)"
            print(f"3D models {manifest.tag}: {state} [{root}]")
        return 0 if synced else 1

    # sync: the manifest *is* the sparse subset; --sparse is accepted so the
    # call site documents intent (a full-library mirror is not offered).
    result = pinned.sync_models(manifest, root)
    for rel, why in sorted(result.failed.items()):
        print(f"Error: {rel}: {why}", file=sys.stderr)
    if result.downloaded or result.cached:
        root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pinned.ATTRIBUTION_PATH, root / "ATTRIBUTION.md")
    if fmt == "json":
        print(
            json.dumps(
                {
                    "tag": manifest.tag,
                    "dir": str(root),
                    "downloaded": len(result.downloaded),
                    "cached": len(result.cached),
                    "failed": result.failed,
                },
                indent=2,
            )
        )
    else:
        print(
            f"3D models {manifest.tag}: {len(result.downloaded)} downloaded, "
            f"{len(result.cached)} cached, {len(result.failed)} failed [{root}]"
        )
    return 0 if result.ok else 1
