"""
Net tracing operations for KiCad schematics.

Provides functions to trace electrical connections and analyze nets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from ..schema.schematic import Schematic
from ..schema.wire import Wire

# Tolerance for point matching (in mm)
POINT_TOLERANCE = 0.1


def points_equal(
    p1: Tuple[float, float], p2: Tuple[float, float], tol: float = POINT_TOLERANCE
) -> bool:
    """Check if two points are equal within tolerance."""
    return abs(p1[0] - p2[0]) < tol and abs(p1[1] - p2[1]) < tol


def point_on_wire(point: Tuple[float, float], wire: Wire, tol: float = POINT_TOLERANCE) -> bool:
    """Check if a point lies on a wire segment."""
    return wire.contains_point(point, tol)


@dataclass
class NetConnection:
    """A connection point on a net."""

    point: Tuple[float, float]
    type: str  # "pin", "wire_end", "junction", "label"
    reference: str = ""  # Symbol reference or label text
    pin_number: str = ""  # Pin number if type is "pin"
    uuid: str = ""


@dataclass
class Net:
    """A traced net with all its connections."""

    name: str  # Net name from label, or auto-generated
    connections: List[NetConnection] = field(default_factory=list)
    wires: List[Wire] = field(default_factory=list)
    has_label: bool = False

    @property
    def pin_count(self) -> int:
        """Number of pins connected to this net."""
        return sum(1 for c in self.connections if c.type == "pin")

    @property
    def symbol_refs(self) -> Set[str]:
        """Set of symbol references connected to this net."""
        return {c.reference for c in self.connections if c.type == "pin" and c.reference}

    def __repr__(self) -> str:
        return f"Net({self.name!r}, {self.pin_count} pins, {len(self.wires)} wires)"


class NetTracer:
    """
    Traces nets through a schematic.

    This analyzes connectivity by following wires between pins, junctions,
    and labels to determine which components are electrically connected.
    """

    def __init__(self, schematic: Schematic):
        self.sch = schematic
        self._build_pin_map()
        self._build_wire_graph()

    def _build_pin_map(self) -> None:
        """Build a map of positions to symbol pins."""
        self.pin_positions: Dict[Tuple[float, float], List[NetConnection]] = {}

        for sym in self.sch.symbols:
            # Get symbol position
            sym_pos = sym.position
            _sym_rot = sym.rotation  # noqa: F841 - reserved for pin offset calculations

            # For now, we'll use a simplified approach:
            # Pins are at the symbol position (we'd need library info for exact positions)
            # This is a limitation - full pin positions require parsing the symbol library
            for pin in sym.pins:
                conn = NetConnection(
                    point=sym_pos,  # Simplified - actual pin position would be offset
                    type="pin",
                    reference=sym.reference,
                    pin_number=pin.number,
                    uuid=pin.uuid,
                )
                key = (round(sym_pos[0], 1), round(sym_pos[1], 1))
                if key not in self.pin_positions:
                    self.pin_positions[key] = []
                self.pin_positions[key].append(conn)

    def _build_wire_graph(self) -> None:
        """Build a graph of wire connections."""
        self.wire_endpoints: Dict[Tuple[float, float], List[Wire]] = {}

        for wire in self.sch.wires:
            for point in [wire.start, wire.end]:
                key = (round(point[0], 1), round(point[1], 1))
                if key not in self.wire_endpoints:
                    self.wire_endpoints[key] = []
                self.wire_endpoints[key].append(wire)

    def get_label_at(self, point: Tuple[float, float]) -> Optional[str]:
        """Get a label at or near a point."""
        for lbl in self.sch.labels:
            if points_equal(lbl.position, point):
                return lbl.text

        for lbl in self.sch.global_labels:
            if points_equal(lbl.position, point):
                return lbl.text

        for lbl in self.sch.hierarchical_labels:
            if points_equal(lbl.position, point):
                return lbl.text

        return None

    def trace_from_point(
        self,
        start: Tuple[float, float],
        visited_wires: Optional[Set[str]] = None,
    ) -> Net:
        """
        Trace a net starting from a point.

        Follows wires through junctions to find all connected points.
        """
        if visited_wires is None:
            visited_wires = set()

        net = Net(name="")
        points_to_visit = [start]
        visited_points: Set[Tuple[float, float]] = set()

        while points_to_visit:
            point = points_to_visit.pop()
            key = (round(point[0], 1), round(point[1], 1))

            if key in visited_points:
                continue
            visited_points.add(key)

            # Check for label at this point
            label = self.get_label_at(point)
            if label:
                net.name = label
                net.has_label = True
                net.connections.append(
                    NetConnection(
                        point=point,
                        type="label",
                        reference=label,
                        uuid="",
                    )
                )

            # Check for junction at this point
            for junc in self.sch.junctions:
                if points_equal(junc.position, point):
                    net.connections.append(
                        NetConnection(
                            point=point,
                            type="junction",
                            uuid=junc.uuid,
                        )
                    )
                    break

            # Find wires at this point
            if key in self.wire_endpoints:
                for wire in self.wire_endpoints[key]:
                    if wire.uuid in visited_wires:
                        continue
                    visited_wires.add(wire.uuid)
                    net.wires.append(wire)

                    # Add the other endpoint to visit
                    if points_equal(wire.start, point):
                        points_to_visit.append(wire.end)
                    else:
                        points_to_visit.append(wire.start)

        # Generate name if no label found
        if not net.name:
            net.name = f"Net_{hash(tuple(sorted(visited_points))) & 0xFFFF:04X}"

        return net

    def trace_all_nets(self) -> List[Net]:
        """Trace all nets in the schematic."""
        nets = []
        visited_wires: Set[str] = set()

        # Start from each wire that hasn't been visited
        for wire in self.sch.wires:
            if wire.uuid not in visited_wires:
                net = self.trace_from_point(wire.start, visited_wires)
                if net.wires:  # Only include nets with wires
                    nets.append(net)

        # Also trace from labels that might not be connected to wires yet
        for lbl in self.sch.labels:
            already_traced = any(
                any(points_equal(lbl.position, c.point) for c in n.connections) for n in nets
            )
            if not already_traced:
                net = Net(name=lbl.text, has_label=True)
                net.connections.append(
                    NetConnection(
                        point=lbl.position,
                        type="label",
                        reference=lbl.text,
                        uuid=lbl.uuid,
                    )
                )
                nets.append(net)

        return nets

    def find_net_by_label(self, label_text: str) -> Optional[Net]:
        """Find a net by its label text."""
        # Find the label position
        label_pos = None

        for lbl in self.sch.labels:
            if lbl.text == label_text:
                label_pos = lbl.position
                break

        for lbl in self.sch.global_labels:
            if lbl.text == label_text:
                label_pos = lbl.position
                break

        for lbl in self.sch.hierarchical_labels:
            if lbl.text == label_text:
                label_pos = lbl.position
                break

        if label_pos:
            return self.trace_from_point(label_pos)

        return None


def trace_nets(schematic: Schematic) -> List[Net]:
    """Convenience function to trace all nets in a schematic."""
    tracer = NetTracer(schematic)
    return tracer.trace_all_nets()


def find_net(schematic: Schematic, label: str) -> Optional[Net]:
    """Convenience function to find a net by label."""
    tracer = NetTracer(schematic)
    return tracer.find_net_by_label(label)
