"""Auto-create copper pour zones for power-classified nets.

This module provides a helper that inspects a PCB file, classifies its
nets, and auto-inserts zone definitions for power/ground nets when the
file has no existing zones for them.  It is designed to be called by
``kct route`` before routing begins so that power nets are connected via
copper pours rather than routed as ordinary signal traces.

A board-level heuristic prevents small all-power designs (e.g., a simple
voltage divider whose only nets are VIN, VOUT, GND) from being
inadvertently drained of routable nets: auto-pour is only applied when
the board has at least one signal (non-power/ground) net, indicating
that the power nets are infrastructure rather than the entire design.
"""

from __future__ import annotations

import re
from pathlib import Path


def _detect_uninset_zones(
    pcb_text: str,
    pcb_path: Path,
    edge_clearance: float,
) -> set[str]:
    """Return net names of zones whose boundaries lack edge clearance inset.

    Loads the PCB via the schema layer to get zone polygons and the board
    outline, then checks whether any zone polygon vertex sits within
    ``edge_clearance`` of the board edge (with a small tolerance).
    """
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(pcb_path))
    outline = pcb.get_board_outline()
    if not outline:
        return set()

    ox, oy = pcb.board_origin

    # Build board-edge bounding box from the outline
    outline_xs = [p[0] for p in outline]
    outline_ys = [p[1] for p in outline]
    edge_x_min, edge_x_max = min(outline_xs), max(outline_xs)
    edge_y_min, edge_y_max = min(outline_ys), max(outline_ys)

    # Tolerance: vertices within (edge_clearance - epsilon) of the edge
    # are considered un-inset.  We use half the clearance as threshold
    # so that zones that were already inset (even partially) are not
    # unnecessarily regenerated.
    threshold = edge_clearance * 0.5

    nets_needing_fix: set[str] = set()
    for zone in pcb.zones:
        net_name = zone.net_name
        if not net_name:
            continue
        polygon = zone.polygon
        if not polygon:
            continue

        for px, py in polygon:
            # Convert from sheet-absolute to board-relative
            bx = px - ox
            by = py - oy
            # Check distance to each edge of the bounding box
            dist_left = bx - edge_x_min
            dist_right = edge_x_max - bx
            dist_top = by - edge_y_min
            dist_bottom = edge_y_max - by
            min_dist = min(dist_left, dist_right, dist_top, dist_bottom)
            if min_dist < threshold:
                nets_needing_fix.add(net_name)
                break  # No need to check more vertices for this zone

    return nets_needing_fix


def _remove_zones_for_nets(pcb_path: Path, net_names: set[str]) -> None:
    """Remove zone definitions for the given net names from the PCB file.

    Uses a regex-based approach to strip ``(zone ...)`` blocks whose
    ``net_name`` or ``net`` attribute matches one of *net_names*.
    """
    pcb_text = pcb_path.read_text()

    for net_name in net_names:
        escaped = re.escape(net_name)
        # KiCad 7/8: (zone ... (net_name "GND") ... )  -- top-level node
        # KiCad 9:   (zone ... (net "GND") ... )        -- top-level node
        # We match the entire (zone ...) block using a balanced-paren
        # approach: find the opening "(zone" and count parens to find
        # the matching close.
        new_lines: list[str] = []
        lines = pcb_text.split("\n")
        skip_depth = 0
        for line in lines:
            if skip_depth > 0:
                skip_depth += line.count("(") - line.count(")")
                continue
            # Detect start of a zone block for this net
            if re.search(
                rf'\(zone\s.*?\(net_name\s+"{escaped}"\)',
                line,
                re.DOTALL,
            ) or re.search(
                rf'\(zone\s[^)]*\(net\s+"{escaped}"\)',
                line,
            ):
                skip_depth = line.count("(") - line.count(")")
                continue
            new_lines.append(line)
        pcb_text = "\n".join(new_lines)

    pcb_path.write_text(pcb_text)


def auto_pour_if_missing(
    pcb_path: Path,
    *,
    quiet: bool = False,
    edge_clearance: float | None = None,
) -> tuple[int, list[str]]:
    """Auto-create copper pours for power-classified nets that lack zones.

    Idempotent: skips nets that already have zones.  Skips boards where
    *every* net is power/ground-classified (small designs do not benefit
    from pours, and skipping all nets removes them from routing entirely).

    Args:
        pcb_path: Path to .kicad_pcb file (modified **in place**).
        quiet: Suppress informational output.
        edge_clearance: Optional edge clearance in mm.  When set, zone
            boundaries are inset from the board edge by this distance
            to avoid copper-to-edge DRC violations.

    Returns:
        Tuple of ``(zones_created, pour_net_names)`` where
        *pour_net_names* lists the nets that received new zones.
    """
    from kicad_tools.router.net_class import NetClass, auto_classify_nets
    from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

    pcb_path = Path(pcb_path)
    pcb_text = pcb_path.read_text()

    # ------------------------------------------------------------------
    # 1. Build net inventory from the file header
    # ------------------------------------------------------------------
    net_names: dict[int, str] = {}
    for m in re.finditer(r'\(net\s+(\d+)\s+"([^"]+)"\)', pcb_text):
        net_num, name = int(m.group(1)), m.group(2)
        if net_num > 0:
            net_names[net_num] = name

    if not net_names:
        return 0, []

    # ------------------------------------------------------------------
    # 2. Classify nets
    # ------------------------------------------------------------------
    classifications = auto_classify_nets(net_names)

    pour_nets: list[tuple[str, NetClass]] = []
    signal_net_count = 0
    for net_id, classification in classifications.items():
        if classification.net_class in (NetClass.POWER, NetClass.GROUND):
            pour_nets.append((net_names[net_id], classification.net_class))
        else:
            signal_net_count += 1

    # Count unclassified nets (those that didn't meet confidence threshold)
    # as signal nets -- they are certainly not power/ground.
    unclassified = len(net_names) - len(classifications)
    signal_net_count += unclassified

    if not pour_nets:
        return 0, []

    # ------------------------------------------------------------------
    # 3. Board-level guard: skip if ALL nets are power/ground
    # ------------------------------------------------------------------
    if signal_net_count == 0:
        if not quiet:
            print(
                "Auto-pour: skipped (all nets are power/ground — "
                "routing as signals instead)"
            )
        return 0, []

    # ------------------------------------------------------------------
    # 4. Idempotency: filter out nets that already have zones
    #    When edge_clearance is specified, also detect existing zones
    #    whose boundaries match the board outline exactly (no inset)
    #    and remove them so they can be regenerated with proper inset.
    # ------------------------------------------------------------------
    nets_with_zones: set[str] = set()
    # KiCad 7/8 format: (zone ... (net_name "GND") ...)
    for zm in re.finditer(
        r'\(zone\s+.*?\(net_name\s+"([^"]+)"\)', pcb_text, re.DOTALL
    ):
        nets_with_zones.add(zm.group(1))
    # KiCad 9 name-only format: (zone ... (net "GND") ...)
    for zm in re.finditer(r'\(zone\s[^)]*\(net\s+"([^"]+)"\)', pcb_text):
        nets_with_zones.add(zm.group(1))

    # When edge_clearance is specified, check whether existing zones
    # have boundaries that lack proper inset from the board edge.
    # If so, remove those zones so they are regenerated with inset.
    nets_needing_reinset: set[str] = set()
    if edge_clearance and edge_clearance > 0 and nets_with_zones:
        nets_needing_reinset = _detect_uninset_zones(
            pcb_text, pcb_path, edge_clearance
        )
        if nets_needing_reinset:
            _remove_zones_for_nets(pcb_path, nets_needing_reinset)
            # Re-read the file after removal
            pcb_text = pcb_path.read_text()
            nets_with_zones -= nets_needing_reinset
            if not quiet:
                print(
                    f"Auto-pour: removing {len(nets_needing_reinset)} zone(s) "
                    f"with insufficient edge clearance for "
                    f"{', '.join(sorted(nets_needing_reinset))}"
                )

    new_pour_nets = [
        (name, cls) for name, cls in pour_nets if name not in nets_with_zones
    ]

    if not new_pour_nets:
        return 0, []

    # ------------------------------------------------------------------
    # 5. Create zones via the shared generator
    # ------------------------------------------------------------------
    count = auto_create_zones_for_pour_nets(
        pcb_path, new_pour_nets, edge_clearance=edge_clearance
    )

    names = [name for name, _ in new_pour_nets]
    if not quiet and count > 0:
        print(f"Auto-pour: created {count} zone(s) for {', '.join(sorted(names))}")

    return count, names
