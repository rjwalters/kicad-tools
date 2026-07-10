"""Add 3D model references to PCB footprints from the installed KiCad libraries.

``kct pcb add-3d-models`` patches missing ``(model ...)`` nodes into a
``.kicad_pcb`` so ``kicad-cli pcb render`` (and the KiCad 3D viewer) shows
component bodies instead of a bare board.  The model references are copied
verbatim from the installed KiCad footprint libraries (``.kicad_mod``
sources), so offsets/rotations match what KiCad itself would embed.

The patch is pure text insertion scoped to ``(model ...)`` metadata — no
copper, placement, zone, or net bytes change, so DRC results are identical
before and after.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def run_add_3d_models(
    pcb_path: Path,
    output_path: Path | None = None,
    lib_path: Path | None = None,
    allow_variants: bool = True,
    dry_run: bool = False,
    output_format: str = "text",
) -> int:
    """Patch missing 3D model refs into *pcb_path*.

    Args:
        pcb_path: Path to the ``.kicad_pcb`` file.
        output_path: Alternative output path (default: patch in place).
        lib_path: Explicit KiCad footprints directory (default: auto-detect
            the installed KiCad libraries).
        allow_variants: Accept same-library footprint-name variants when the
            exact name is missing (e.g. ``QFN-24-1EP_4x4mm_P0.5mm`` matches
            the ``..._EP2.6x2.6mm`` variant — the model body is identical).
        dry_run: Report what would be inserted without writing.
        output_format: ``"text"`` or ``"json"``.

    Returns:
        Exit code (0 success, 1 error).
    """
    from kicad_tools.footprints.library_path import detect_kicad_library_path
    from kicad_tools.pcb.models3d import add_model_refs

    library_paths = detect_kicad_library_path(config_override=lib_path)
    if not library_paths.found:
        msg = (
            "KiCad footprint libraries not found (install KiCad or pass "
            "--lib-path / set KICAD_FOOTPRINT_DIR)"
        )
        if output_format == "json":
            print(json.dumps({"error": msg}))
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return 1

    try:
        report = add_model_refs(
            pcb_path,
            output_path=output_path,
            library_paths=library_paths,
            allow_variants=allow_variants,
            dry_run=dry_run,
        )
    except Exception as e:
        if output_format == "json":
            print(json.dumps({"error": str(e)}))
        else:
            print(f"Error: {e}", file=sys.stderr)
        return 1

    dest = output_path if output_path is not None else pcb_path
    result = {
        "input": str(pcb_path),
        "output": str(dest) if (report.changed and not dry_run) else None,
        "dry_run": dry_run,
        "libraries": str(library_paths.footprints_path),
        "patched": report.patched,
        "already_present": report.already_present,
        "unresolved": report.unresolved,
        "no_model_in_library": report.no_model_in_library,
        "variant_matches": report.variant_matches,
        "substitution_matches": report.substitution_matches,
    }

    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        label = " (dry run)" if dry_run else ""
        print(f"PCB Add 3D Models{label}")
        print(f"  Input:     {pcb_path}")
        print(f"  Libraries: {library_paths.footprints_path}")
        print()
        if report.patched:
            verb = "Would add" if dry_run else "Added"
            print(f"  {verb} model refs to {len(report.patched)} footprint(s):")
            for lib_id in report.patched:
                print(f"    {lib_id}")
        else:
            print("  No footprints needed patching.")
        if report.variant_matches:
            print("  Same-library variant models used (visual match):")
            for lib_id, stem in sorted(report.variant_matches.items()):
                print(f"    {lib_id} -> {stem}")
        if report.substitution_matches:
            print("  Cross-library substitution models used (curated equivalent):")
            for lib_id, sub in sorted(report.substitution_matches.items()):
                print(f"    {lib_id} -> {sub}")
        if report.already_present:
            print(f"  Already had models: {len(report.already_present)}")
        if report.no_model_in_library:
            print(
                f"  Library footprint has no model: "
                f"{', '.join(sorted(set(report.no_model_in_library)))}"
            )
        if report.unresolved:
            print(
                f"  Not in installed libraries (skipped): "
                f"{', '.join(sorted(set(report.unresolved)))}"
            )
        if report.changed and not dry_run:
            print()
            print(f"  Saved to: {dest}")

    return 0
