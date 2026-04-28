"""Detect stale and duplicate net names in KiCad PCB files.

KiCad's net naming convention changed across versions:
  - Old style: ``Net-(C11-Pad2)``
  - New style: ``Net-(C11-2)``

When a PCB is round-tripped through different KiCad versions, both naming
conventions can coexist for the same logical net.  One variant will have
traces/vias (the "active" net) while the other will be empty (the "stale"
net).  Pads pointing at the stale net are effectively unrouted.

This module detects such duplicate pairs and can optionally fix them by
reassigning stale pad references to the active net.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

# Patterns for auto-generated net names across KiCad versions.
# Old style: Net-(C11-Pad2)
_OLD_STYLE_RE = re.compile(r"^Net-\((\w+)-Pad(\w+)\)$")
# New style: Net-(C11-2)
_NEW_STYLE_RE = re.compile(r"^Net-\((\w+)-(\w+)\)$")


@dataclass
class AffectedPad:
    """A pad that references a stale net."""

    footprint_ref: str
    pad_number: str
    current_net: str


@dataclass
class StaleNetGroup:
    """A group of nets that refer to the same logical connection.

    Exactly one net is "active" (has segments/vias) and the others are
    "stale" (no routing).  Pads assigned to a stale net need reassignment.
    """

    active_net_name: str
    active_net_number: int
    stale_net_name: str
    stale_net_number: int
    active_segment_count: int
    active_via_count: int
    affected_pads: list[AffectedPad] = field(default_factory=list)


def _canonical_key(reference: str, pad_id: str) -> tuple[str, str]:
    """Return a canonical (reference, pad_id) tuple for grouping."""
    return (reference, pad_id)


def find_stale_nets(pcb: PCB) -> list[StaleNetGroup]:
    """Detect stale/duplicate net names in a PCB.

    Scans all ``Net-(REF-PadN)`` and ``Net-(REF-N)`` header declarations,
    groups them by (reference, pad_id), and identifies which variant is
    active (has routing) vs stale (no routing).

    Named nets (e.g. ``GND``, ``+3.3V``) are never flagged.

    Args:
        pcb: A loaded PCB instance.

    Returns:
        List of :class:`StaleNetGroup` objects, one per duplicate pair found.
    """
    # Step 1: Parse all auto-generated net names and group by canonical key.
    # Each entry maps (ref, pad_id) -> list of (net_number, net_name, style)
    canonical_groups: dict[tuple[str, str], list[tuple[int, str, str]]] = {}

    for net_number, net in pcb.nets.items():
        name = net.name
        if not name:
            continue

        # Try old-style pattern first
        m = _OLD_STYLE_RE.match(name)
        if m:
            ref, pad_id = m.group(1), m.group(2)
            key = _canonical_key(ref, pad_id)
            canonical_groups.setdefault(key, []).append(
                (net_number, name, "old")
            )
            continue

        # Try new-style pattern
        m = _NEW_STYLE_RE.match(name)
        if m:
            ref, pad_id = m.group(1), m.group(2)
            # Skip if pad_id starts with "Pad" -- that would be old-style
            # already matched above (this shouldn't happen but be safe)
            if pad_id.startswith("Pad"):
                continue
            key = _canonical_key(ref, pad_id)
            canonical_groups.setdefault(key, []).append(
                (net_number, name, "new")
            )
            continue

    # Step 2: Filter to groups with more than one net (duplicates).
    results: list[StaleNetGroup] = []

    for key, entries in canonical_groups.items():
        if len(entries) < 2:
            continue

        # Count segments and vias for each net to determine active vs stale.
        scored: list[tuple[int, int, int, str]] = []  # (segs, vias, num, name)
        for net_num, net_name, style in entries:
            seg_count = sum(1 for _ in pcb.segments_in_net(net_num))
            via_count = sum(1 for _ in pcb.vias_in_net(net_num))
            scored.append((seg_count, via_count, net_num, net_name))

        # Sort by total routing (segments + vias), descending. The one with
        # the most routing is "active".
        scored.sort(key=lambda x: (x[0] + x[1]), reverse=True)

        active_segs, active_vias, active_num, active_name = scored[0]

        # Every other entry in this group is stale.
        for seg_count, via_count, stale_num, stale_name in scored[1:]:
            # Collect pads that reference the stale net.
            affected: list[AffectedPad] = []
            for fp in pcb.footprints:
                for pad in fp.pads:
                    if pad.net_number == stale_num:
                        affected.append(
                            AffectedPad(
                                footprint_ref=fp.reference,
                                pad_number=pad.number,
                                current_net=stale_name,
                            )
                        )

            results.append(
                StaleNetGroup(
                    active_net_name=active_name,
                    active_net_number=active_num,
                    stale_net_name=stale_name,
                    stale_net_number=stale_num,
                    active_segment_count=active_segs,
                    active_via_count=active_vias,
                    affected_pads=affected,
                )
            )

    # Sort results by stale net name for deterministic output.
    results.sort(key=lambda g: g.stale_net_name)
    return results


def fix_stale_nets(pcb: PCB, groups: list[StaleNetGroup]) -> int:
    """Reassign pads from stale nets to the active net.

    For each :class:`StaleNetGroup`, reassigns every affected pad's net
    reference from the stale net to the active net using
    :meth:`PCB.assign_net_to_footprint_pad`.

    Args:
        pcb: A loaded PCB instance (will be modified in place).
        groups: List of stale net groups from :func:`find_stale_nets`.

    Returns:
        Number of pads successfully reassigned.
    """
    fixed_count = 0
    for group in groups:
        for pad in group.affected_pads:
            success = pcb.assign_net_to_footprint_pad(
                pad.footprint_ref,
                pad.pad_number,
                group.active_net_name,
            )
            if success:
                fixed_count += 1
    return fixed_count
