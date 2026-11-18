"""Move a footprint in a PCB by reference designator.

Provides a standalone command to relocate a specific footprint to new
coordinates, optionally setting a new rotation.  Supports batch mode via a
JSON map for moving multiple footprints atomically.

Coordinate convention
----------------------
By default the supplied (x, y) values are **board-relative**: they are
measured from the board origin (the top-left corner of the Edge.Cuts
outline), and the board-origin offset is added automatically when the
``(at ...)`` node is written.  This matches the rest of the placement API.

Pass ``absolute=True`` to interpret (x, y) as **absolute KiCad page
coordinates** (the same space as a footprint's raw ``(at ...)`` node).  In
that mode the board origin is subtracted before assignment so the setter's
re-addition nets out and the footprint lands at exactly (x, y) on the sheet.
On a board with no detectable outline the origin is ``(0, 0)`` and the two
modes agree.

Side (``--map`` only)
---------------------
A batch entry may carry an optional ``"side": "front" | "back"``.  When the
footprint is not already on that side it is flipped *first* (the same
KiCad-verified left/right mirror about the footprint anchor that
``pcb-modify flip`` applies), then ``x`` / ``y`` / ``rotation`` are applied.
``rotation`` is therefore the **stored KiCad footprint angle written after the
flip** (what KiCad's Properties dialog shows for the back-side part), not an
angle as seen from the front.  The flip pivots on the footprint anchor, so a
part whose anchor is pad 1 has pad 2 land on the opposite side of the anchor
from where the same angle puts it on the front.  A part already on the
requested side is left alone; an entry without ``side`` never flips.
"""

from __future__ import annotations

import json
from pathlib import Path

_VALID_SIDES = ("front", "back")


def _footprint_side(layer: str) -> str | None:
    """Return ``"front"`` / ``"back"`` for an ``F.*`` / ``B.*`` layer, else None."""
    if layer.startswith("F."):
        return "front"
    if layer.startswith("B."):
        return "back"
    return None


def _flip_footprint(pcb, reference: str) -> str | None:
    """Flip one footprint to the other side; return an error message or None.

    Reuses the exact call ``pcb-modify flip`` makes
    (``StrategyApplicator`` with a ``MIRROR_COMPONENT`` strategy) so there is a
    single flip transform.  Mutates ``pcb`` in memory; the caller saves once.
    """
    from kicad_tools.recovery.applicator import StrategyApplicator
    from kicad_tools.recovery.types import Action, Difficulty, ResolutionStrategy, StrategyType

    result = StrategyApplicator().apply_strategy(
        pcb,
        ResolutionStrategy(
            type=StrategyType.MIRROR_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=1.0,
            actions=[Action(type="mirror", target=reference)],
        ),
    )
    return None if result.success else result.message


def run_move_footprint(
    pcb_path: Path,
    reference: str | None = None,
    to: tuple[float, float] | None = None,
    rotation: float | None = None,
    batch_map: dict | None = None,
    dry_run: bool = False,
    output_path: Path | None = None,
    output_format: str = "text",
    absolute: bool = False,
    drag_endpoints: bool = False,
    drag_tolerance: float = 0.05,
) -> int:
    """Move one or more footprints in a PCB file.

    Args:
        pcb_path: Path to .kicad_pcb file.
        reference: Reference designator to move (single mode).
        to: New (x, y) position (single mode).  Board-relative by default;
            absolute page coordinates when ``absolute=True``.
        rotation: Optional new rotation in degrees.
        batch_map: JSON-parsed dict for batch mode.
        dry_run: Preview moves without modifying.
        output_path: Alternative output path for modified PCB.
        output_format: "text" or "json".
        absolute: When True, interpret (x, y) as absolute KiCad page
            coordinates (subtract the board origin before assignment) rather
            than board-relative coordinates.
        drag_endpoints: When True, translate trace-segment endpoints that were
            coincident (within ``drag_tolerance``) with each moved pad by that
            pad's delta, so routed copper follows the footprint instead of being
            stranded.  First slice is translation-only: dragging is skipped (with
            a warning) for any footprint whose ``rotation`` also changes.
        drag_tolerance: Endpoint-coincidence match radius in mm (default 0.05).

    Returns:
        Exit code (0 for success, 1 for errors).
    """
    from kicad_tools.schema.pcb import PCB

    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        _print_error(f"Failed to load PCB: {e}", output_format)
        return 1

    # Build list of moves: [(ref, x, y, rotation_or_None), ...]
    moves: list[tuple[str, float, float, float | None]] = []
    # Requested side per reference (batch ``"side"`` key); absent => no flip.
    sides: dict[str, str] = {}

    if batch_map is not None:
        for ref, spec in batch_map.items():
            if not isinstance(spec, dict) or "x" not in spec or "y" not in spec:
                _print_error(
                    f"Invalid batch entry for '{ref}': must have 'x' and 'y' keys",
                    output_format,
                )
                return 1
            rot = spec.get("rotation")
            if "side" in spec:
                side = spec["side"]
                if not isinstance(side, str) or side not in _VALID_SIDES:
                    _print_error(
                        f"Invalid side {side!r} for '{ref}': must be 'front' or 'back'",
                        output_format,
                    )
                    return 1
                sides[ref] = side
            moves.append((ref, float(spec["x"]), float(spec["y"]), rot))
    elif reference is not None and to is not None:
        moves.append((reference, to[0], to[1], rotation))
    else:
        _print_error(
            "Either --ref/--to or --map is required",
            output_format,
        )
        return 1

    coord_space = "absolute" if absolute else "board-relative"
    ox, oy = pcb.board_origin

    # The position the setter ultimately writes into the (at ...) node:
    #   - board-relative: setter adds (ox, oy), so assign (x, y) directly.
    #   - absolute: subtract (ox, oy) first so the setter's re-addition nets
    #     out, landing the footprint at exactly (x, y) on the sheet.
    def _assign_coords(x: float, y: float) -> tuple[float, float]:
        if absolute:
            return (x - ox, y - oy)
        return (x, y)

    # Validate all references exist before making any changes.  When dragging
    # endpoints, capture each pad's OLD board-relative position and its
    # board-relative translation delta BEFORE any footprint moves -- the drag
    # translates copper that terminated on the old pad position by that delta.
    move_details: list[dict] = []
    drag_plans: list[dict] = []
    for ref, x, y, rot in moves:
        fp = pcb.get_footprint(ref)
        if not fp:
            _print_error(f"Footprint {ref} not found in PCB", output_format)
            return 1
        # fp.position is stored board-relative; report old + new in the same
        # coordinate space the user requested so they are directly comparable.
        old_x, old_y = fp.position
        if absolute:
            old_x, old_y = old_x + ox, old_y + oy
        new_rotation = rot if rot is not None else fp.rotation
        old_side = _footprint_side(str(fp.layer))
        side_change = False
        if ref in sides:
            if old_side is None:
                _print_error(
                    f"Footprint {ref} is on layer '{fp.layer}', which is not a "
                    "front/back footprint layer; cannot apply 'side'",
                    output_format,
                )
                return 1
            side_change = old_side != sides[ref]
            if side_change and rot is None:
                # The flip rewrites the stored angle (theta -> 180 - theta).
                new_rotation = (180.0 - fp.rotation) % 360.0
        detail = {
            "reference": ref,
            "footprint": fp.name,
            "old_position": [old_x, old_y],
            "old_rotation": fp.rotation,
            # new_position is reported in the coordinate space the user
            # requested (see coordinate_space for which one that is).
            "new_position": [x, y],
            "new_rotation": new_rotation,
        }
        if ref in sides:
            detail["old_side"] = old_side
            detail["new_side"] = sides[ref]
            detail["side_changed"] = side_change
        move_details.append(detail)

        if drag_endpoints:
            # Board-relative delta of a pure translation: the difference between
            # the target board-relative footprint position and the current one.
            # (_assign_coords maps the user's --to into board-relative space.)
            assign_x, assign_y = _assign_coords(x, y)
            fp_old_x, fp_old_y = fp.position
            delta = (assign_x - fp_old_x, assign_y - fp_old_y)
            rotation_changed = (rot is not None and abs(rot - fp.rotation) > 1e-9) or side_change
            pads: list[dict] = []
            for pad in fp.pads:
                old_pos = pcb.get_pad_position(ref, pad.number)
                if old_pos is None:
                    continue
                pads.append(
                    {
                        "number": pad.number,
                        "net_number": pad.net_number,
                        "net_name": pad.net_name,
                        "old_pos": old_pos,
                    }
                )
            drag_plans.append(
                {
                    "delta": delta,
                    "rotation_changed": rotation_changed,
                    "pads": pads,
                }
            )

    result = {
        "pcb": str(pcb_path),
        "dry_run": dry_run,
        "coordinate_space": coord_space,
        "board_origin": [ox, oy],
        "moves": move_details,
        "moved": False,
    }
    if drag_endpoints:
        result["drag_endpoints"] = True
        result["drag_tolerance"] = drag_tolerance

    # Drag coincident trace endpoints with each moved pad.  The drag mutates the
    # in-memory PCB (and its S-expression tree) in place; it runs in dry-run too
    # so per-pad counts can be reported, but the mutations are only persisted
    # when save() is called below (skipped on dry-run).  Endpoints are matched
    # against each pad's OLD position (captured pre-move) and translated by the
    # footprint's board-relative delta.
    if drag_endpoints:
        total_dragged = 0
        for md, plan in zip(move_details, drag_plans, strict=True):
            if plan["rotation_changed"]:
                md["drag_skipped_rotation"] = True
                if md.get("side_changed"):
                    md["drag_skipped_side"] = True
                md["drag"] = []
                continue
            dx, dy = plan["delta"]
            per_pad: list[dict] = []
            for prec in plan["pads"]:
                # A pad has attached copper only if it belongs to a real net.
                # Skip unconnected pads so a passing-through trace on another
                # net is never dragged by accident.
                has_net = prec["net_number"] != 0 or bool(prec["net_name"])
                if not has_net:
                    continue
                if dx == 0.0 and dy == 0.0:
                    count = 0
                else:
                    # net_number 0 on a connected pad means a name-only board
                    # (#4416): fall back to purely geometric matching.
                    net_filter = prec["net_number"] or None
                    count = pcb.drag_trace_endpoints(
                        prec["old_pos"],
                        (dx, dy),
                        tolerance=drag_tolerance,
                        net_number=net_filter,
                    )
                total_dragged += count
                per_pad.append(
                    {
                        "pad": prec["number"],
                        "net": prec["net_name"] or prec["net_number"],
                        "endpoints_dragged": count,
                        # The "subtly-broken board" signature: a pad with copper
                        # expected but no endpoint moved with it.
                        "zero_match_warning": count == 0,
                    }
                )
            md["drag"] = per_pad
        result["endpoints_dragged"] = total_dragged

    if not dry_run:
        # Flip first so x/y/rotation below land on the already-flipped part.
        for detail in move_details:
            if detail.get("side_changed"):
                flip_ref = str(detail["reference"])
                error = _flip_footprint(pcb, flip_ref)
                if error:
                    _print_error(f"Failed to flip {flip_ref}: {error}", output_format)
                    return 1

        for ref, x, y, rot in moves:
            fp = pcb.get_footprint(ref)
            # fp existence already validated above
            fp.position = _assign_coords(x, y)  # type: ignore[union-attr]
            if rot is not None:
                # KiCad pad angles are absolute board-frame angles, while
                # pad positions remain footprint-local. Preserve each pad's
                # orientation relative to the part when changing its angle.
                rotation_delta = rot - fp.rotation  # type: ignore[union-attr]
                if rotation_delta != 0.0:
                    for pad in fp.pads:  # type: ignore[union-attr]
                        pad.rotation += rotation_delta
                fp.rotation = rot  # type: ignore[union-attr]

        result["moved"] = True

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
        label = "PCB Move Footprint (dry run)" if dry_run else "PCB Move Footprint"
        print(label)
        print(f"  PCB: {pcb_path}")
        print(f"  Coordinates: {coord_space} (board origin: {ox}, {oy})")
        print()
        for md in move_details:
            old_pos = md["old_position"]
            new_pos = md["new_position"]
            print(f"  {md['reference']} ({md['footprint']}):")
            print(
                f"    Position [{coord_space}]: "
                f"({old_pos[0]}, {old_pos[1]}) -> ({new_pos[0]}, {new_pos[1]})"
            )
            if "old_side" in md:
                verb = "flipped" if md["side_changed"] else "unchanged"
                print(f"    Side: {md['old_side']} -> {md['new_side']} ({verb})")
            if md["old_rotation"] != md["new_rotation"]:
                print(f"    Rotation: {md['old_rotation']} -> {md['new_rotation']}")
            if md.get("drag_skipped_rotation"):
                reason = "side changed" if md.get("drag_skipped_side") else "rotation changed"
                print(
                    f"    Drag endpoints: SKIPPED ({reason}; "
                    "rotation-aware dragging not supported in this slice)"
                )
            elif "drag" in md:
                for dr in md["drag"]:
                    line = (
                        f"    Drag pad {dr['pad']} (net {dr['net']}): "
                        f"{dr['endpoints_dragged']} endpoint(s)"
                    )
                    if dr["zero_match_warning"]:
                        line += "  [WARNING: attached copper but no endpoint matched]"
                    print(line)
        print()
        if dry_run:
            print("  Would move footprint(s)")
        else:
            print(f"  Moved {len(moves)} footprint(s)")
            print(f"  Saved to: {result.get('output', pcb_path)}")

    return 0


def _print_error(message: str, output_format: str) -> None:
    """Print an error in the appropriate format."""
    if output_format == "json":
        print(json.dumps({"error": message}, indent=2))
    else:
        import sys

        print(f"Error: {message}", file=sys.stderr)
