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


def auto_pour_if_missing(
    pcb_path: Path,
    *,
    quiet: bool = False,
) -> tuple[int, list[str]]:
    """Auto-create copper pours for power-classified nets that lack zones.

    Idempotent: skips nets that already have zones.  Skips boards where
    *every* net is power/ground-classified (small designs do not benefit
    from pours, and skipping all nets removes them from routing entirely).

    Args:
        pcb_path: Path to .kicad_pcb file (modified **in place**).
        quiet: Suppress informational output.

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

    new_pour_nets = [
        (name, cls) for name, cls in pour_nets if name not in nets_with_zones
    ]

    if not new_pour_nets:
        return 0, []

    # ------------------------------------------------------------------
    # 5. Create zones via the shared generator
    # ------------------------------------------------------------------
    count = auto_create_zones_for_pour_nets(pcb_path, new_pour_nets)

    names = [name for name, _ in new_pour_nets]
    if not quiet and count > 0:
        print(f"Auto-pour: created {count} zone(s) for {', '.join(sorted(names))}")

    return count, names
