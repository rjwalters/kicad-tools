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

ERC-marker net exclusion
------------------------

KiCad's netlister exports the ``PWR_FLAG`` virtual symbol as if it were
an ordinary net, even though the symbol carries no electrical
connection -- its sole purpose is to silence the *Input Power pin not
driven by any Output Power pin* ERC error.  Because the resulting net
name starts with ``PWR``, the name-based classifier in
:mod:`kicad_tools.router.net_class` matches it as :class:`NetClass.POWER`
and would otherwise produce a spurious zone.  See
:func:`_is_erc_marker_net` and the ``ERC_MARKER_NET_PATTERNS`` constant
below for the filter that excludes such names from the pour-net set.
The schematic-side analogue lives in
``src/kicad_tools/cli/sch_connectivity.py`` (``"PWR_FLAG is an ERC
annotation, not a real net name"``).

Split-ground designs (multiple distinct ``NetClass.GROUND`` nets, e.g.
mixed-signal boards with both ``GNDA`` and ``GNDD``) are detected
automatically by the layer/priority allocator in
``kicad_tools.zones.generator._assign_layers_for_pour_nets``.  On a
4-layer stackup the two ground domains receive dedicated inner layers
(``In1.Cu`` / ``In2.Cu``) and the power tree is moved to ``F.Cu``; on
a 2-layer stackup each ground gets a distinct priority on ``B.Cu`` to
avoid the "zero copper" override that occurs when zones share both
layer and priority.  See that function's docstring for the full rule.

Geometric outline partition (#2771)
-----------------------------------

The layer/priority allocator alone is not sufficient when N≥2 zones
share a single layer (the common case on 2-layer stackups, and the
fallback path on 4-layer stackups with 3+ power nets).  Distinct
priorities prevent the explicit zero-copper warning, but KiCad's fill
resolver still awards the entire overlapping region to the highest
priority zone, so siblings receive zero usable copper.

To make every zone produce real copper, this module delegates to
``kicad_tools.zones.generator.auto_create_zones_for_pour_nets``, which
runs the **outline allocator** (``_compute_pour_outlines``) after layer
assignment.  Zones that share a layer with one or more siblings get a
per-net bounding-box outline (default 1.5 mm margin around the net's
pads, clipped to the board outline), while zones that are the only zone
on their layer keep the full board outline so return-path planes stay
continuous.  See that function's docstring for the contract.
"""

from __future__ import annotations

import re
from pathlib import Path

# ERC-only marker nets that must never be poured.  These are synthetic
# net names emitted by KiCad's netlister for symbols whose sole purpose
# is to silence ERC, not to carry copper.  The schematic-side connectivity
# walker has the equivalent carve-out in
# ``src/kicad_tools/cli/sch_connectivity.py`` (search for
# ``"PWR_FLAG is an ERC annotation"``).
#
# Patterns are anchored regular expressions; ``_is_erc_marker_net`` uses
# ``re.match`` (anchors at the start) so each entry is tested against
# the full net name.
ERC_MARKER_NET_PATTERNS: tuple[str, ...] = (
    r"^PWR_FLAG$",  # KiCad's stock power:PWR_FLAG symbol
    r"^#FLG(?:\d*|_.*|$)",  # Reference-designator spelling (#FLG, #FLG01, #FLG_VBUS)
    r".*_FLAG$",  # User-named flag variants (e.g., +3V3_FLAG, VBUS_FLAG)
)


def _is_erc_marker_net(name: str) -> bool:
    """Return True when *name* is an ERC-only marker, not a real net.

    See module docstring for context.  Used by :func:`auto_pour_if_missing`
    to skip such nets when building the pour-net set, and by the
    ``kct route`` skip-pour-nets path to avoid the misleading
    ``Auto-skip: PWR_FLAG (pour nets — use zone fill)`` log line.
    """
    return any(re.match(p, name) for p in ERC_MARKER_NET_PATTERNS)


def classify_pour_candidates(
    net_names: dict[int, str],
):
    """Classify a board's nets into pour candidates and signal nets.

    Shared helper used by both :func:`auto_pour_if_missing` and the
    ``kct build`` zones step (:func:`kicad_tools.cli.build_cmd._run_step_zones`).
    Both call sites must agree on the all-power-board guard so a board
    whose only nets are POWER/GROUND is *never* given zones for every
    net -- doing so converts every net into a pour-skip target and the
    router then has nothing to route (see issue #2740).

    Args:
        net_names: Mapping of net id -> net name from the PCB file.

    Returns:
        A 3-tuple ``(pour_nets, signal_net_count, is_all_power_board)``:

        * ``pour_nets`` -- list of ``(name, NetClass)`` tuples for nets
          classified as POWER or GROUND, excluding ERC-marker nets such
          as ``PWR_FLAG`` (which carry no copper).
        * ``signal_net_count`` -- count of nets that are *not* pour
          candidates (i.e., not POWER/GROUND, not ERC markers, or
          unclassified).  Used by the all-power-board guard.
        * ``is_all_power_board`` -- True when ``pour_nets`` is non-empty
          and ``signal_net_count == 0``.  Callers should skip zone
          creation entirely when this is True so the router can route
          every net as a signal.
    """
    from kicad_tools.router.net_class import NetClass, auto_classify_nets

    classifications = auto_classify_nets(net_names)

    pour_nets: list[tuple[str, NetClass]] = []
    signal_net_count = 0
    for net_id, classification in classifications.items():
        net_name = net_names[net_id]
        # ERC-marker nets (PWR_FLAG and friends) carry no copper; the
        # name-based classifier reports them as POWER but they must not
        # be poured.  Treat them as if they did not exist for the
        # all-power guard below -- a board whose only "power" net is
        # PWR_FLAG should not be mistaken for a power-only design.
        if _is_erc_marker_net(net_name):
            continue
        if classification.net_class in (NetClass.POWER, NetClass.GROUND):
            pour_nets.append((net_name, classification.net_class))
        else:
            signal_net_count += 1

    # Count unclassified nets (those that didn't meet confidence threshold)
    # as signal nets -- they are certainly not power/ground.
    unclassified = len(net_names) - len(classifications)
    signal_net_count += unclassified

    is_all_power_board = bool(pour_nets) and signal_net_count == 0
    return pour_nets, signal_net_count, is_all_power_board


def _detect_uninset_zones(
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


def _find_zone_span(text: str, start: int) -> int | None:
    """Return the index just past the matching close paren of a ``(zone``.

    *start* must point at the ``(`` of the ``(zone`` token.  Walks the
    string counting parens (skipping over those inside double-quoted
    strings) and returns the index immediately after the matching
    closing paren, or ``None`` if the parens are unbalanced.
    """
    depth = 0
    in_string = False
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                # Skip escaped character inside string
                i += 2
                continue
            if ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return None


def _remove_zones_for_nets(pcb_path: Path, net_names: set[str]) -> None:
    """Remove zone definitions for the given net names from the PCB file.

    Uses a balanced-paren scan over the full file text so that zone
    blocks spanning multiple lines (the format KiCad's writer
    produces) are matched correctly.
    """
    if not net_names:
        return

    pcb_text = pcb_path.read_text()

    # Build a single regex that matches ``(net_name "X")`` or
    # ``(net "X")`` (KiCad 9 name-only form) for any of *net_names*.
    escaped = "|".join(re.escape(n) for n in net_names)
    inner_pattern = re.compile(rf'\((?:net_name|net)\s+"(?:{escaped})"\)')

    # Iterate every ``(zone`` opener in the file and decide whether to
    # remove that span.  We walk forward across the text rebuilding it
    # rather than relying on per-line state.
    out_parts: list[str] = []
    cursor = 0
    zone_start_pattern = re.compile(r"\(zone\b")
    for m in zone_start_pattern.finditer(pcb_text):
        start = m.start()
        if start < cursor:
            # This opener lies inside a span we already removed (e.g. a
            # nested ``(zone`` reference, which KiCad does not emit, but
            # be defensive).
            continue
        end = _find_zone_span(pcb_text, start)
        if end is None:
            # Unbalanced parens -- bail out to avoid corrupting the
            # file; leave the rest of the text untouched.
            break
        block = pcb_text[start:end]
        if inner_pattern.search(block):
            # Drop this zone block from the output.  Also strip any
            # trailing whitespace/newline that immediately follows so
            # we don't leave a dangling blank line.
            out_parts.append(pcb_text[cursor:start])
            trailing = end
            while trailing < len(pcb_text) and pcb_text[trailing] in (
                " ",
                "\t",
            ):
                trailing += 1
            if trailing < len(pcb_text) and pcb_text[trailing] == "\n":
                trailing += 1
            cursor = trailing
        # else: keep the block; cursor unchanged so it gets emitted on
        # the next iteration's slice.

    out_parts.append(pcb_text[cursor:])
    pcb_path.write_text("".join(out_parts))


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
    # 2. Classify nets + apply all-power-board guard
    # ------------------------------------------------------------------
    # NOTE: the all-power guard must mirror the one in
    # :func:`kicad_tools.cli.build_cmd._run_step_zones` (kct build's
    # zone-creation step).  Both call sites share ``classify_pour_candidates``
    # so the two cannot drift.  Drifting once caused issue #2740, where
    # ``kct build`` created zones for every net on board 01 (VIN/VOUT/GND
    # all-power), the auto-skip step then skipped every net as a pour
    # net, ``nets_to_route`` became 0, the router reported 100%
    # completion trivially, and the build silently shipped an empty PCB.
    pour_nets, _signal_net_count, is_all_power_board = classify_pour_candidates(net_names)

    if not pour_nets:
        return 0, []

    # ------------------------------------------------------------------
    # 3. Board-level guard: skip if ALL nets are power/ground
    # ------------------------------------------------------------------
    if is_all_power_board:
        if not quiet:
            print("Auto-pour: skipped (all nets are power/ground — routing as signals instead)")
        return 0, []

    # ------------------------------------------------------------------
    # 4. Idempotency: filter out nets that already have zones
    #    When edge_clearance is specified, also detect existing zones
    #    whose boundaries match the board outline exactly (no inset)
    #    and remove them so they can be regenerated with proper inset.
    # ------------------------------------------------------------------
    nets_with_zones: set[str] = set()
    # KiCad 7/8 format: (zone ... (net_name "GND") ...)
    for zm in re.finditer(r'\(zone\s+.*?\(net_name\s+"([^"]+)"\)', pcb_text, re.DOTALL):
        nets_with_zones.add(zm.group(1))
    # KiCad 9 name-only format: (zone ... (net "GND") ...)
    for zm in re.finditer(r'\(zone\s[^)]*\(net\s+"([^"]+)"\)', pcb_text):
        nets_with_zones.add(zm.group(1))

    # When edge_clearance is specified, check whether existing zones
    # have boundaries that lack proper inset from the board edge.
    # If so, remove those zones so they are regenerated with inset.
    nets_needing_reinset: set[str] = set()
    if edge_clearance and edge_clearance > 0 and nets_with_zones:
        nets_needing_reinset = _detect_uninset_zones(pcb_path, edge_clearance)
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

    new_pour_nets = [(name, cls) for name, cls in pour_nets if name not in nets_with_zones]

    if not new_pour_nets:
        return 0, []

    # ------------------------------------------------------------------
    # 5. Create zones via the shared generator
    # ------------------------------------------------------------------
    count = auto_create_zones_for_pour_nets(pcb_path, new_pour_nets, edge_clearance=edge_clearance)

    names = [name for name, _ in new_pour_nets]
    if not quiet and count > 0:
        print(f"Auto-pour: created {count} zone(s) for {', '.join(sorted(names))}")

    return count, names
