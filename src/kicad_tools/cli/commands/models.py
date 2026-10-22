"""``kct models`` command handlers (issue #7683).

* ``kct models sync [--sparse]`` -- fetch the pinned, digest-verified subset
  of KiCad's stock 3D models into the local cache.
* ``kct models status`` -- report whether the cache is complete.
* ``kct models generate`` / ``kct models families`` -- parametric bodies for
  footprints with no stock model (issue #7687; see :mod:`kicad_tools.modelgen`).
"""

from __future__ import annotations

import json
import shutil
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from argparse import Namespace

__all__ = ["run_models_command"]


def run_models_command(args: Namespace) -> int:
    """Handle ``kct models`` and its sub-actions."""
    from kicad_tools.pcb import models3d_pinned as pinned

    subcommand = getattr(args, "models_command", None)
    if subcommand == "generate":
        return _run_generate(args)
    if subcommand == "families":
        return _run_families(args)
    if subcommand not in ("sync", "status"):
        print("Usage: kct models <command> [options]")
        print("Commands: sync, status, generate, families")
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


def _run_families(args: Namespace) -> int:
    from kicad_tools.modelgen import FAMILIES
    from kicad_tools.modelgen.cadquery_backend import available

    fmt = getattr(args, "models_format", "text")
    rows: list[dict[str, Any]] = [
        {
            "family": fam.name,
            "description": fam.description,
            "lib_patterns": list(fam.lib_patterns),
            "params": [{"name": p.name, "help": p.help, "default": p.default} for p in fam.params],
        }
        for fam in FAMILIES.values()
    ]
    if fmt == "json":
        print(json.dumps({"cadquery": available(), "families": rows}, indent=2))
        return 0
    state = "installed" if available() else "NOT installed (pip install 'kicad-tools[models]')"
    print(f"CadQuery extra: {state}; --placeholder works without it\n")
    for row in rows:
        print(f"{row['family']}: {row['description']}")
        for p in row["params"]:
            print(f"    {p['name']:<22} {p['help']} [default: {p['default']}]")
    return 0


def _run_generate(args: Namespace) -> int:
    from pathlib import Path

    from kicad_tools.modelgen import ExtraMissingError, ParamError, generate_model, parse_params

    fmt = getattr(args, "models_format", "text")
    target = args.models_target

    def opt_path(value: str | None) -> Path | None:
        return Path(value).expanduser() if value else None

    try:
        params = parse_params(getattr(args, "models_params", None))
        if getattr(args, "models_backend", "builtin") == "kicad-library-tools":
            from kicad_tools.modelgen.bridge import BridgeError, run_external_generator
            from kicad_tools.modelgen.generate import default_output_dir

            try:
                bridged = run_external_generator(
                    target,
                    params,
                    opt_path(getattr(args, "models_out", None)) or default_output_dir(),
                    command=getattr(args, "models_bridge_cmd", None),
                )
            except BridgeError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            if fmt == "json":
                print(json.dumps({"files": [str(p) for p in bridged.files]}, indent=2))
            else:
                for path in bridged.files:
                    print(f"wrote {path} (external kicad-library-tools; see provenance sidecar)")
            return 0

        result = generate_model(
            target,
            params=params,
            family=getattr(args, "models_family", None),
            footprint=opt_path(getattr(args, "models_footprint", None)),
            pcb=opt_path(getattr(args, "models_pcb", None)),
            reference=getattr(args, "models_ref", None),
            placeholder=bool(getattr(args, "models_placeholder", False)),
            out_dir=opt_path(getattr(args, "models_out", None)),
            name=getattr(args, "models_name", None),
            register=bool(getattr(args, "models_register", False)),
            attach=bool(getattr(args, "models_attach", False)),
        )
    except ExtraMissingError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except (ParamError, LookupError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    prov = result.provenance
    if fmt == "json":
        print(
            json.dumps(
                {
                    "step_path": str(result.path),
                    "provenance_path": str(result.provenance_path),
                    "registered": str(result.registered) if result.registered else None,
                    "attached": result.attached,
                    **prov,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    kind = "placeholder" if prov["placeholder"] else f"family {prov['family']}"
    print(f"wrote {result.path} ({kind}, {prov['backend']})")
    print(f"  params: {json.dumps(prov['params'], sort_keys=True)}")
    if prov.get("inside_courtyard") is False:
        print("  warning: the body extends past the footprint courtyard", file=sys.stderr)
    if result.registered:
        print(f"  registered in {result.registered}")
    if result.attached:
        print(f"  attached to {', '.join(result.attached)}")
    if prov["placeholder"]:
        print("  note: placeholder bodies are audited as 'placeholder' (still a coverage gap)")
    return 0
