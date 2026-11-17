"""Lock or unlock footprints in a PCB by reference designator.

Sets KiCad's ``(locked yes)`` attribute on footprints so the anchor-weight
recipe (``kct optimize-placement --anchor-weight 1.0 --allow-infeasible``)
can identify perimeter anchors.  Without a CLI knob, agents previously had
to hand-edit the .kicad_pcb s-expression.

Two selection modes are supported:

* ``--refs J1,J2,...`` — explicit comma-separated reference designators.
* ``--all-perimeter`` — every footprint whose bounding box touches the
  Edge.Cuts board outline (within a small tolerance).

Mirror command ``unlock-footprints`` clears the attribute using the same
selection semantics.

Locking an already-locked footprint (or unlocking an unlocked one) is a
no-op; the command remains idempotent.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

# Default tolerance (mm) used when checking whether a footprint bbox
# touches the board outline rectangle.  Real PCBs typically place
# mounting holes and edge connectors 1-3 mm inboard for clearance, so a
# generous default makes ``--all-perimeter`` useful for the anchor-weight
# recipe.  Override with ``--perimeter-margin``.
_PERIMETER_TOUCH_TOLERANCE_MM = 2.0


def run_lock_footprints(
    pcb_path: Path,
    refs: list[str] | None = None,
    all_perimeter: bool = False,
    perimeter_margin: float | None = None,
    unlock: bool = False,
    dry_run: bool = False,
    output_path: Path | None = None,
    output_format: str = "text",
) -> int:
    """Lock (or unlock) footprints in a PCB file.

    Args:
        pcb_path: Path to .kicad_pcb file.
        refs: Reference designators to lock/unlock (e.g. ["J1", "J2"]).
        all_perimeter: If True, target all footprints whose bbox touches
            the board edge.  Mutually exclusive with ``refs``.
        perimeter_margin: Tolerance in mm for the "touches the board
            edge" test.  Defaults to ``_PERIMETER_TOUCH_TOLERANCE_MM``
            when None.  Only meaningful with ``all_perimeter=True``.
        unlock: If True, clear the locked flag instead of setting it.
        dry_run: Preview changes without writing.
        output_path: Alternative output path for the modified PCB.
        output_format: "text" or "json".

    Returns:
        Exit code (0 success, 1 error).
    """
    from kicad_tools.schema.pcb import PCB

    op = "unlock" if unlock else "lock"

    if not refs and not all_perimeter:
        _print_error(
            "Either --refs or --all-perimeter is required",
            output_format,
        )
        return 1

    if refs and all_perimeter:
        _print_error(
            "--refs and --all-perimeter are mutually exclusive",
            output_format,
        )
        return 1

    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        _print_error(f"Failed to load PCB: {e}", output_format)
        return 1

    # Resolve target reference list
    target_refs: list[str]
    if all_perimeter:
        tol = perimeter_margin if perimeter_margin is not None else _PERIMETER_TOUCH_TOLERANCE_MM
        target_refs = _find_perimeter_footprints(pcb, tolerance_mm=tol)
        if not target_refs:
            _print_error(
                "No footprints found touching the board edge (is there an Edge.Cuts outline?)",
                output_format,
            )
            return 1
    else:
        assert refs is not None
        target_refs = list(refs)

    # Validate all references exist before any modification
    missing: list[str] = []
    for ref in target_refs:
        if pcb.get_footprint(ref) is None:
            missing.append(ref)
    if missing:
        _print_error(
            f"Footprint(s) not found in PCB: {', '.join(missing)}",
            output_format,
        )
        return 1

    changes: list[dict] = []
    new_state = not unlock  # True if locking, False if unlocking
    for ref in target_refs:
        fp = pcb.get_footprint(ref)
        assert fp is not None  # validated above
        old_state = bool(getattr(fp, "locked", False))
        changes.append(
            {
                "reference": ref,
                "footprint": fp.name,
                "was_locked": old_state,
                "now_locked": new_state,
                "changed": old_state != new_state,
            }
        )

    n_changed = sum(1 for c in changes if c["changed"])

    result = {
        "pcb": str(pcb_path),
        "operation": op,
        "dry_run": dry_run,
        "selection": "all-perimeter" if all_perimeter else "refs",
        "targets": target_refs,
        "changes": changes,
        "n_changed": n_changed,
        "written": False,
    }

    if not dry_run and n_changed > 0:
        for ref in target_refs:
            fp = pcb.get_footprint(ref)
            assert fp is not None
            # Idempotent: only assign when actually changing state.  This
            # avoids invoking __setattr__ rebuild work on no-ops.
            if bool(getattr(fp, "locked", False)) != new_state:
                fp.locked = new_state

        save_path = output_path or pcb_path
        result["output"] = str(save_path)
        try:
            pcb.save(save_path)
        except Exception as e:
            _print_error(f"Failed to save PCB: {e}", output_format)
            return 1
        result["written"] = True

    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        label = f"PCB {'Unlock' if unlock else 'Lock'} Footprints{' (dry run)' if dry_run else ''}"
        print(label)
        print(f"  PCB: {pcb_path}")
        print(f"  Selection: {result['selection']}")
        print(f"  Targets: {len(target_refs)} footprint(s)")
        print()
        for c in changes:
            mark = "*" if c["changed"] else " "
            print(
                f"  {mark} {c['reference']} ({c['footprint']}): "
                f"locked {c['was_locked']} -> {c['now_locked']}"
            )
        print()
        if dry_run:
            print(f"  Would change {n_changed} footprint(s)")
        elif n_changed == 0:
            print("  No changes needed (already in desired state)")
        else:
            print(f"  Changed {n_changed} footprint(s)")
            print(f"  Saved to: {result.get('output', pcb_path)}")

    return 0


def _find_perimeter_footprints(
    pcb,
    tolerance_mm: float = _PERIMETER_TOUCH_TOLERANCE_MM,
) -> list[str]:
    """Return refs of footprints whose bbox touches the board outline.

    Uses the axis-aligned bounding box of the Edge.Cuts outline.  A
    footprint is considered to touch the perimeter when its own pad-derived
    bbox is within ``tolerance_mm`` of any edge of the board bbox.
    """
    outline = pcb.get_board_outline()
    if not outline:
        return []

    xs = [p[0] for p in outline]
    ys = [p[1] for p in outline]
    bmin_x, bmax_x = min(xs), max(xs)
    bmin_y, bmax_y = min(ys), max(ys)

    tol = tolerance_mm
    perimeter_refs: list[str] = []

    for fp in pcb.footprints:
        bbox = _footprint_bbox(fp)
        if bbox is None:
            continue
        fmin_x, fmin_y, fmax_x, fmax_y = bbox
        touches = (
            (fmin_x <= bmin_x + tol)
            or (fmax_x >= bmax_x - tol)
            or (fmin_y <= bmin_y + tol)
            or (fmax_y >= bmax_y - tol)
        )
        if touches:
            perimeter_refs.append(fp.reference)

    return perimeter_refs


def _footprint_bbox(fp) -> tuple[float, float, float, float] | None:
    """Compute (min_x, min_y, max_x, max_y) bbox for a footprint.

    Pads carry footprint-local coordinates; this rotates them by the
    footprint rotation and offsets by the footprint position to land in
    board-relative coordinates that match ``get_board_outline()``.
    Returns None if the footprint has no pads.
    """
    if not fp.pads:
        return None

    cos_a = math.cos(math.radians(fp.rotation))
    sin_a = math.sin(math.radians(fp.rotation))

    min_x = float("inf")
    min_y = float("inf")
    max_x = float("-inf")
    max_y = float("-inf")

    for pad in fp.pads:
        lx, ly = pad.position
        rx = lx * cos_a - ly * sin_a
        ry = lx * sin_a + ly * cos_a
        ax = fp.position[0] + rx
        ay = fp.position[1] + ry
        hw = pad.size[0] / 2
        hh = pad.size[1] / 2
        min_x = min(min_x, ax - hw)
        min_y = min(min_y, ay - hh)
        max_x = max(max_x, ax + hw)
        max_y = max(max_y, ay + hh)

    return (min_x, min_y, max_x, max_y)


def _print_error(message: str, output_format: str) -> None:
    """Print an error in the appropriate format."""
    if output_format == "json":
        print(json.dumps({"error": message}, indent=2))
    else:
        print(f"Error: {message}", file=sys.stderr)
