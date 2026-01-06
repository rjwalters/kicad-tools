"""
Net mapping for layout preservation.

Detects net name changes between schematic versions and provides
remapping for PCB trace assignments.
"""

from __future__ import annotations

from dataclasses import dataclass

from kicad_tools.layout.types import (
    MatchReason,
    NetMapping,
    OrphanedSegment,
    RemapResult,
    SegmentRemap,
)
from kicad_tools.operations.netlist import Netlist, NetlistNet
from kicad_tools.sexp import SExp


@dataclass
class PinAddress:
    """A unique pin identifier (component reference + pin number)."""

    reference: str
    pin: str

    def __hash__(self):
        return hash((self.reference, self.pin))

    def __eq__(self, other):
        if not isinstance(other, PinAddress):
            return False
        return self.reference == other.reference and self.pin == other.pin


class NetMapper:
    """
    Maps net names between two netlist versions.

    Detects exact matches, renames via pin connectivity analysis,
    and identifies removed/added nets.
    """

    def __init__(
        self,
        old_netlist: Netlist,
        new_netlist: Netlist,
        min_confidence: float = 0.5,
    ):
        """
        Initialize NetMapper.

        Args:
            old_netlist: The original netlist.
            new_netlist: The updated netlist.
            min_confidence: Minimum confidence threshold for connectivity matches.
        """
        self.old_netlist = old_netlist
        self.new_netlist = new_netlist
        self.min_confidence = min_confidence

        # Build lookup structures
        self._old_net_names: set[str] = {net.name for net in old_netlist.nets}
        self._new_net_names: set[str] = {net.name for net in new_netlist.nets}

        # Build pin-to-net mappings
        self._old_pins_by_net: dict[str, set[PinAddress]] = {}
        self._new_pins_by_net: dict[str, set[PinAddress]] = {}

        for net in old_netlist.nets:
            self._old_pins_by_net[net.name] = self._extract_pins(net)

        for net in new_netlist.nets:
            self._new_pins_by_net[net.name] = self._extract_pins(net)

    def _extract_pins(self, net: NetlistNet) -> set[PinAddress]:
        """Extract pin addresses from a net."""
        return {PinAddress(node.reference, node.pin) for node in net.nodes}

    def compute_mappings(self) -> list[NetMapping]:
        """
        Compute net name mappings between netlist versions.

        Returns:
            List of NetMapping objects describing how old nets map to new nets.
        """
        mappings: list[NetMapping] = []
        used_new_nets: set[str] = set()

        for old_name in sorted(self._old_net_names):
            # Try exact name match first
            if old_name in self._new_net_names:
                mappings.append(
                    NetMapping(
                        old_name=old_name,
                        new_name=old_name,
                        confidence=1.0,
                        match_reason=MatchReason.EXACT,
                    )
                )
                used_new_nets.add(old_name)
                continue

            # Try connectivity-based match
            best_match = self._find_by_connectivity(old_name, used_new_nets)
            if best_match:
                mappings.append(best_match)
                if best_match.new_name:
                    used_new_nets.add(best_match.new_name)
                continue

            # Net was removed
            mappings.append(
                NetMapping(
                    old_name=old_name,
                    new_name=None,
                    confidence=0.0,
                    match_reason=MatchReason.REMOVED,
                )
            )

        return mappings

    def _find_by_connectivity(self, old_name: str, used_new_nets: set[str]) -> NetMapping | None:
        """
        Find new net with matching pin connections.

        Args:
            old_name: The old net name to find a match for.
            used_new_nets: Set of new net names already used in mappings.

        Returns:
            NetMapping if a suitable match is found, None otherwise.
        """
        old_pins = self._old_pins_by_net.get(old_name, set())
        if not old_pins:
            return None

        best_match: NetMapping | None = None
        best_overlap = 0
        candidates: list[NetMapping] = []

        for new_name in self._new_net_names:
            if new_name in used_new_nets:
                continue

            new_pins = self._new_pins_by_net.get(new_name, set())
            if not new_pins:
                continue

            overlap = len(old_pins & new_pins)
            if overlap == 0:
                continue

            # Calculate confidence as Jaccard similarity
            union = len(old_pins | new_pins)
            confidence = overlap / union if union > 0 else 0

            if confidence >= self.min_confidence:
                candidate = NetMapping(
                    old_name=old_name,
                    new_name=new_name,
                    confidence=confidence,
                    match_reason=MatchReason.CONNECTIVITY,
                    shared_pins=overlap,
                )
                candidates.append(candidate)

                if overlap > best_overlap:
                    best_overlap = overlap
                    best_match = candidate

        # Check for ambiguous matches (multiple candidates with same overlap)
        if len(candidates) > 1:
            tied = [c for c in candidates if c.shared_pins == best_overlap]
            if len(tied) > 1 and best_match:
                # Mark as ambiguous but still return best guess
                best_match = NetMapping(
                    old_name=old_name,
                    new_name=best_match.new_name,
                    confidence=best_match.confidence * 0.8,  # Reduce confidence
                    match_reason=MatchReason.AMBIGUOUS,
                    shared_pins=best_overlap,
                )

        return best_match

    def get_new_nets(self) -> list[str]:
        """
        Get nets that exist in the new netlist but not in the old.

        Returns:
            List of new net names.
        """
        return sorted(self._new_net_names - self._old_net_names)

    def get_removed_nets(self) -> list[str]:
        """
        Get nets that exist in the old netlist but not in the new.

        Returns:
            List of removed net names.
        """
        removed = set()
        for old_name in self._old_net_names:
            if old_name not in self._new_net_names:
                # Check if it maps to something via connectivity
                pins = self._old_pins_by_net.get(old_name, set())
                found_mapping = False
                for new_name in self._new_net_names:
                    new_pins = self._new_pins_by_net.get(new_name, set())
                    if pins & new_pins:
                        found_mapping = True
                        break
                if not found_mapping:
                    removed.add(old_name)
        return sorted(removed)


def remap_traces(
    pcb_doc: SExp,
    mappings: list[NetMapping],
    net_id_lookup: dict[str, int] | None = None,
) -> RemapResult:
    """
    Remap trace net assignments in a PCB document.

    Args:
        pcb_doc: Parsed PCB S-expression document.
        mappings: Net mappings from NetMapper.compute_mappings().
        net_id_lookup: Optional mapping of net names to IDs in the new netlist.
                      If not provided, will be extracted from pcb_doc.

    Returns:
        RemapResult with remapped and orphaned segments.
    """
    result = RemapResult(net_mappings=mappings)

    # Build mapping lookup
    mapping_lookup: dict[str, NetMapping] = {m.old_name: m for m in mappings}

    # Build net ID lookup if not provided
    if net_id_lookup is None:
        net_id_lookup = {}
        for net_node in pcb_doc.find_all("net"):
            atoms = net_node.get_atoms()
            if len(atoms) >= 2:
                net_id = int(atoms[0])
                net_name = str(atoms[1])
                net_id_lookup[net_name] = net_id

    # Build reverse lookup (ID to name) for current PCB
    id_to_name: dict[int, str] = {}
    for net_node in pcb_doc.find_all("net"):
        atoms = net_node.get_atoms()
        if len(atoms) >= 2:
            net_id = int(atoms[0])
            net_name = str(atoms[1])
            id_to_name[net_id] = net_name

    # Process segments
    for segment in pcb_doc.find_all("segment"):
        _remap_element(segment, mapping_lookup, net_id_lookup, id_to_name, result)

    # Process vias
    for via in pcb_doc.find_all("via"):
        _remap_element(via, mapping_lookup, net_id_lookup, id_to_name, result)

    # Identify new nets
    old_names = {m.old_name for m in mappings}
    new_names = set(net_id_lookup.keys())
    result.new_nets = sorted(new_names - old_names)

    return result


def _remap_element(
    element: SExp,
    mapping_lookup: dict[str, NetMapping],
    net_id_lookup: dict[str, int],
    id_to_name: dict[int, str],
    result: RemapResult,
) -> None:
    """
    Remap a single segment or via element.

    Args:
        element: The segment or via S-expression node.
        mapping_lookup: Mapping from old net names to NetMapping.
        net_id_lookup: Mapping from net names to IDs.
        id_to_name: Mapping from current net IDs to names.
        result: RemapResult to update.
    """
    # Get current net ID
    net_node = element.find("net")
    if not net_node:
        return

    current_net_id = net_node.get_int(0)
    if current_net_id is None:
        return

    # Get UUID
    uuid_node = element.find("uuid")
    segment_uuid = uuid_node.get_string(0) if uuid_node else ""

    # Get current net name
    current_net_name = id_to_name.get(current_net_id, "")
    if not current_net_name:
        return

    # Check if we have a mapping for this net
    mapping = mapping_lookup.get(current_net_name)
    if not mapping:
        # No mapping found - this is a new net or wasn't in old netlist
        return

    if mapping.is_removed:
        # Net was removed - segment is orphaned
        result.orphaned_segments.append(
            OrphanedSegment(
                segment_uuid=segment_uuid,
                net_name=current_net_name,
                net_id=current_net_id,
                reason="Net removed from design",
            )
        )
        return

    if mapping.is_exact:
        # No change needed for exact matches
        return

    # Net was renamed - update the segment
    new_name = mapping.new_name
    if new_name is None:
        return

    new_net_id = net_id_lookup.get(new_name)
    if new_net_id is None:
        result.orphaned_segments.append(
            OrphanedSegment(
                segment_uuid=segment_uuid,
                net_name=current_net_name,
                net_id=current_net_id,
                reason=f"New net '{new_name}' not found in PCB",
            )
        )
        return

    # Update the net ID in the element
    net_node.children = [SExp(value=str(new_net_id))]

    result.remapped_segments.append(
        SegmentRemap(
            segment_uuid=segment_uuid,
            old_net_name=current_net_name,
            new_net_name=new_name,
            old_net_id=current_net_id,
            new_net_id=new_net_id,
        )
    )
