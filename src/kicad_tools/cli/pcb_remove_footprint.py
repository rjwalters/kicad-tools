"""Remove a footprint from a PCB by reference designator.

Provides a standalone command to remove a specific footprint from a PCB file.
By default, refuses to remove footprints with routed traces unless --force
is specified.
"""

from __future__ import annotations

import json
from pathlib import Path


def run_remove_footprint(
    pcb_path: Path,
    reference: str,
    dry_run: bool = False,
    output_path: Path | None = None,
    force: bool = False,
    output_format: str = "text",
) -> int:
    """Remove a footprint from a PCB file.

    Args:
        pcb_path: Path to .kicad_pcb file.
        reference: Reference designator to remove (e.g., "C1").
        dry_run: Preview removal without modifying.
        output_path: Alternative output path for modified PCB.
        force: Remove even if the footprint has routed traces.
        output_format: "text" or "json".

    Returns:
        Exit code (0 for success, 1 for errors).
    """
    from kicad_tools.schema.pcb import PCB

    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        _print_error(f"Failed to load PCB: {e}", output_format)
        return 1

    # Check footprint exists
    fp = pcb.get_footprint(reference)
    if not fp:
        _print_error(f"Footprint {reference} not found in PCB", output_format)
        return 1

    footprint_name = fp.name
    value = fp.value

    # Check for connected traces
    has_traces = pcb.footprint_has_traces(reference)
    if has_traces and not force:
        _print_error(
            f"Footprint {reference} has routed traces; use --force to remove",
            output_format,
        )
        return 1

    result = {
        "pcb": str(pcb_path),
        "reference": reference,
        "footprint": footprint_name,
        "value": value,
        "has_traces": has_traces,
        "dry_run": dry_run,
        "removed": False,
    }

    if not dry_run:
        success = pcb.remove_footprint(reference)
        if not success:
            _print_error(f"Failed to remove footprint {reference}", output_format)
            return 1

        result["removed"] = True

        save_path = output_path or pcb_path
        result["output"] = str(save_path)
        try:
            pcb.save(save_path)
        except Exception as e:
            _print_error(f"Failed to save PCB: {e}", output_format)
            return 1

    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        label = "PCB Remove Footprint (dry run)" if dry_run else "PCB Remove Footprint"
        print(label)
        print(f"  PCB: {pcb_path}")
        print()
        print(f"  Reference: {reference}")
        print(f"  Footprint: {footprint_name}")
        print(f"  Value: {value}")
        print(f"  Has traces: {has_traces}")
        print()
        if dry_run:
            if has_traces and not force:
                print("  Would NOT remove (has routed traces; use --force)")
            else:
                print("  Would remove footprint")
        else:
            print(f"  Removed footprint {reference}")
            print(f"  Saved to: {result.get('output', pcb_path)}")

    return 0


def _print_error(message: str, output_format: str) -> None:
    """Print an error in the appropriate format."""
    if output_format == "json":
        print(json.dumps({"error": message}, indent=2))
    else:
        import sys

        print(f"Error: {message}", file=sys.stderr)
