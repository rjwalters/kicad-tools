"""Net-wide routing disposition for invalid placement.

This API is independent of CLI activation. Callers supply *active* coupled groups
(from their selected pair/class constraints), not speculative name matches. Net
selection never hides a bad terminal: analyze the whole board first, then take
the intersection with the requested population.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RoutingPlacementDisposition:
    """Named populations before the router removes non-target pads.

    ``requested_nets`` excludes intentional user/plane exclusions, but includes
    placement-invalid requests. ``eligible_nets`` is the actual attempt set.
    ``invalid_nets`` includes unrequested invalid nets so filtering cannot erase
    the diagnosis. ``preserve_copper_nets`` uses authored AND effective names:
    netlist overrides change pad identities, not existing copper's identities.
    Consumers must retain ``router.placement_preserved_copper`` when replacing
    board copper; ``placement_preserved_routes`` supplies its fixed geometry.
    """

    all_nets: frozenset[str] = frozenset()
    invalid_references: frozenset[str] = frozenset()
    direct_invalid_nets: frozenset[str] = frozenset()
    coupled_invalid_nets: frozenset[str] = frozenset()
    requested_nets: frozenset[str] = frozenset()
    user_excluded_nets: frozenset[str] = frozenset()
    plane_excluded_nets: frozenset[str] = frozenset()
    unrequested_nets: frozenset[str] = frozenset()
    preserve_copper_nets: frozenset[str] = frozenset()
    check_available: bool = True
    # (reference, pad number, authored net, effective net), including no-net
    # pads. Loader checks this multiset before constructing any router state.
    pad_net_identities: tuple[tuple[str, str, str, str], ...] = ()
    invalid_footprints: frozenset[str] = frozenset()
    preserved_footprints: frozenset[str] = frozenset()
    physical_pad_net_identities: tuple[tuple[str, str, str, str, str], ...] = ()

    @property
    def invalid_nets(self) -> frozenset[str]:
        return self.direct_invalid_nets | self.coupled_invalid_nets

    @property
    def requested_invalid_nets(self) -> frozenset[str]:
        return self.requested_nets & self.invalid_nets

    @property
    def eligible_nets(self) -> frozenset[str]:
        return self.requested_nets - self.invalid_nets


def analyze_routing_placement(
    pcb_path: str | Path,
    *,
    requested_nets: Iterable[str] | None = None,
    user_excluded_nets: Iterable[str] = (),
    plane_excluded_nets: Iterable[str] = (),
    coupled_groups: Iterable[Iterable[str]] = (),
    netlist: Mapping[str, str] | None = None,
    allow_offboard: bool = False,
) -> RoutingPlacementDisposition:
    """Reuse the existing ERROR OFF_BOARD check, then resolve net populations.

    WARNING courtyard overhangs never exclude nets. A missing outline or failed
    check remains permissive, as in the established CLI gate. ``allow_offboard``
    bypasses the check while still resolving selection metadata. Region/complete
    consumers pass their selected net names; this function always considers all
    terminals when identifying invalid nets. Active coupled groups are closed
    transitively, even when a partner is unrequested or intentionally skipped.
    """
    from kicad_tools.placement import ConflictSeverity, ConflictType, PlacementAnalyzer
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.schema.physical_identity import footprint_keys, validate_netlist_selectors

    # PCB parsing is separate from the optional geometric check: unavailable
    # geometry must not erase net identities or intentional selection metadata.
    pcb = PCB.load(pcb_path)
    physical_keys = footprint_keys(pcb.footprints)
    overrides = netlist or {}
    validate_netlist_selectors(pcb.footprints, overrides)
    identities = tuple(
        sorted(
            (
                fp.reference,
                pad.number,
                pad.net_name,
                overrides.get(f"{fp.reference}.{pad.number}", pad.net_name),
            )
            for fp in pcb.footprints
            for pad in fp.pads
        )
    )
    physical_identities = tuple(
        sorted(
            (
                physical_id,
                fp.reference,
                pad.number,
                pad.net_name,
                overrides.get(f"{fp.reference}.{pad.number}", pad.net_name),
            )
            for fp, physical_id in zip(pcb.footprints, physical_keys, strict=True)
            for pad in fp.pads
        )
    )
    pads = [(ref, original, effective) for ref, _, original, effective in identities]
    all_nets = frozenset(name for _, _, name in pads if name)
    refs: frozenset[str] = frozenset()
    invalid_footprints: frozenset[str] = frozenset()
    available = True
    if not allow_offboard:
        try:
            conflicts = PlacementAnalyzer().find_conflicts(pcb_path)
            refs = frozenset(
                c.component1
                for c in conflicts
                if c.type == ConflictType.OFF_BOARD and c.severity == ConflictSeverity.ERROR
            )
            invalid_footprints = frozenset(
                c.component1_id or c.component1
                for c in conflicts
                if c.type == ConflictType.OFF_BOARD and c.severity == ConflictSeverity.ERROR
            )
        except Exception:
            available = False
    direct = frozenset(
        name
        for fp, physical_id in zip(pcb.footprints, physical_keys, strict=True)
        if physical_id in invalid_footprints
        for pad in fp.pads
        if (name := overrides.get(f"{fp.reference}.{pad.number}", pad.net_name))
    )
    invalid = set(direct)
    groups = [set(group) & all_nets for group in coupled_groups]
    while True:
        expanded = invalid | set().union(*(group for group in groups if group & invalid))
        if expanded == invalid:
            break
        invalid = expanded
    user = frozenset(user_excluded_nets) & all_nets
    plane = frozenset(plane_excluded_nets) & all_nets
    selected = all_nets if requested_nets is None else frozenset(requested_nets) & all_nets
    requested = selected - user - plane
    preserved = frozenset(
        original for _, original, effective in pads if effective in invalid and original
    ) | frozenset(invalid)
    return RoutingPlacementDisposition(
        all_nets=all_nets,
        invalid_references=refs,
        invalid_footprints=invalid_footprints,
        preserved_footprints=invalid_footprints
        | frozenset(
            physical_id
            for physical_id, _ref, _pin, authored, effective in physical_identities
            if authored in preserved or effective in preserved
        ),
        direct_invalid_nets=direct,
        coupled_invalid_nets=frozenset(invalid) - direct,
        requested_nets=requested,
        user_excluded_nets=user,
        plane_excluded_nets=plane,
        unrequested_nets=all_nets - selected - user - plane,
        preserve_copper_nets=preserved,
        check_available=available,
        pad_net_identities=identities,
        physical_pad_net_identities=physical_identities,
    )
