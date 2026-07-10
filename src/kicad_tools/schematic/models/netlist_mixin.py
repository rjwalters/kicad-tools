"""
Schematic Netlist Mixin

Provides netlist extraction and connectivity query functionality.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class PinRef:
    """Reference to a specific pin on a symbol.

    Attributes:
        symbol_ref: The symbol's reference designator (e.g., "R1", "U3")
        pin: The pin number or name (e.g., "1", "VDD", "PA0")
    """

    symbol_ref: str
    pin: str

    def __str__(self) -> str:
        return f"{self.symbol_ref}.{self.pin}"


def _instance_owns_pin(sym, pin) -> bool:
    """Return True if a placed ``SymbolInstance`` owns ``pin``.

    Multi-unit KiCad symbols (e.g. the LM393 dual comparator) share a
    single ``SymbolDef`` — and therefore a single ``symbol_def.pins``
    list containing *every* unit's pins — across several placed
    ``SymbolInstance`` rows that share a ``reference`` but differ in
    their ``unit`` field.  Connectivity must attribute each pin to the
    instance that actually carries it, otherwise a unit-2-only pin gets
    positioned against a unit-1 instance's placement anchor and produces
    "phantom" nets from unit-1 geometry (issue #4020).

    A pin belongs to an instance when its ``pin.unit`` matches the
    instance's ``unit`` field, or when it is a unit-0 "common to all
    units" pin (shared package power pins, and single-unit symbols whose
    pins are all tagged ``unit=0``).  This mirrors ``_pin_matches_unit``
    in :meth:`SymbolInstance.pin_position` and the per-unit filter in
    :meth:`Schematic._check_unconnected_pins` (issue #3349), keeping the
    three code paths from drifting apart.
    """
    pin_unit = getattr(pin, "unit", 0)
    sym_unit = getattr(sym, "unit", 1)
    return pin_unit == 0 or pin_unit == sym_unit


class SchematicNetlistMixin:
    """Mixin providing netlist extraction and query operations for Schematic class."""

    def _build_connectivity_graph(self) -> tuple[dict, dict, dict]:
        """Build a connectivity graph using Union-Find.

        Returns:
            Tuple of (parent dict, point_to_net_names dict, point_to_pins dict)
            - parent: Union-Find parent mapping for connectivity
            - point_to_net_names: Maps points to their net names (from labels/power symbols)
            - point_to_pins: Maps points to PinRef objects at that location
        """
        parent = {}
        point_to_net_names: dict[tuple, list[str]] = {}
        point_to_pins: dict[tuple, list[PinRef]] = {}

        def find(p):
            """Find root of point p in Union-Find structure."""
            if p not in parent:
                parent[p] = p
            if parent[p] != p:
                parent[p] = find(parent[p])  # Path compression
            return parent[p]

        def union(p1, p2):
            """Union two points in the connectivity graph."""
            r1, r2 = find(p1), find(p2)
            if r1 != r2:
                parent[r1] = r2

        # Build wire segments list for T-junction detection
        wire_segments = []
        for wire in self.wires:
            p1 = (round(wire.x1, 2), round(wire.y1, 2))
            p2 = (round(wire.x2, 2), round(wire.y2, 2))
            wire_segments.append((p1, p2))
            # Connect wire endpoints
            union(p1, p2)

        # Helper to check if point is on a wire segment
        def point_on_segment(point: tuple, seg_start: tuple, seg_end: tuple) -> bool:
            """Check if a point lies on a line segment (for orthogonal wires)."""
            px, py = point
            x1, y1 = seg_start
            x2, y2 = seg_end
            if x1 == x2 == px:  # Vertical segment
                return min(y1, y2) < py < max(y1, y2)
            if y1 == y2 == py:  # Horizontal segment
                return min(x1, x2) < px < max(x1, x2)
            return False

        def connect_to_wire(pos: tuple, *, endpoint_only: bool = False) -> None:
            """Connect a position to any wire it touches.

            ``endpoint_only=True`` implements KiCad's pin-connection
            semantics: a symbol/power pin connects to a wire only when a
            wire *endpoint* lands on the pin position (or a junction dot
            sits there — junctions share the same coordinate key so the
            union happens via the junction loop above).  A wire passing
            straight *through* a pin position mid-span does NOT connect
            in KiCad, and treating it as connected falsely merged
            unrelated nets (e.g. board 05's PHASE_C motor wire crossing
            U10's SWDIO pin produced phantom LVS shorts — the KiCad
            netlister keeps them separate).  Labels keep the mid-span
            attach (``endpoint_only=False``): a label placed anywhere
            along a wire names that wire in KiCad.
            """
            for seg_start, seg_end in wire_segments:
                if pos in (seg_start, seg_end) or (
                    not endpoint_only and point_on_segment(pos, seg_start, seg_end)
                ):
                    union(pos, seg_start)
                    break

        # Connect junctions to wires
        for junc in self.junctions:
            junc_pos = (round(junc.x, 2), round(junc.y, 2))
            for seg_start, seg_end in wire_segments:
                if junc_pos in (seg_start, seg_end) or point_on_segment(
                    junc_pos, seg_start, seg_end
                ):
                    union(junc_pos, seg_start)
                    union(junc_pos, seg_end)

        # Connect symbol pins to wires and track pin locations
        #
        # ``symbol_def.pins`` lists every unit's pins, shared across all
        # placed instances of a multi-unit symbol.  Only contribute the
        # pins this instance actually owns (``_instance_owns_pin``) so a
        # unit-2 pin is never registered at a unit-1 instance's position
        # — the phantom-net / duplicate-PinRef bug of issue #4020.  Unit-0
        # "common" pins are still contributed by every placed instance
        # (each at its own resolved coordinate) because connectivity, not
        # a one-shot flag, is being computed here.
        for sym in self.symbols:
            for pin in sym.symbol_def.pins:
                if not _instance_owns_pin(sym, pin):
                    continue
                pos = sym.pin_position(pin.number)
                pos_rounded = (round(pos[0], 2), round(pos[1], 2))

                # Track this pin at this position
                if pos_rounded not in point_to_pins:
                    point_to_pins[pos_rounded] = []
                point_to_pins[pos_rounded].append(PinRef(symbol_ref=sym.reference, pin=pin.number))

                # Connect to wires (KiCad: pins connect at wire endpoints
                # or junctions only, never to a wire crossing mid-span)
                connect_to_wire(pos_rounded, endpoint_only=True)

        # Connect power symbols and track their net names
        for pwr in self.power_symbols:
            pwr_pos = (round(pwr.x, 2), round(pwr.y, 2))
            # Power symbol net name comes from lib_id (e.g., "power:+3.3V" -> "+3.3V")
            net_name = pwr.lib_id.split(":")[1] if ":" in pwr.lib_id else pwr.lib_id

            if pwr_pos not in point_to_net_names:
                point_to_net_names[pwr_pos] = []
            point_to_net_names[pwr_pos].append(net_name)

            # Power symbols connect through their pin — same endpoint-or-
            # junction rule as symbol pins.
            connect_to_wire(pwr_pos, endpoint_only=True)

        # Connect local labels and track their net names
        for label in self.labels:
            label_pos = (round(label.x, 2), round(label.y, 2))

            if label_pos not in point_to_net_names:
                point_to_net_names[label_pos] = []
            point_to_net_names[label_pos].append(label.text)

            connect_to_wire(label_pos)

        # Connect global labels and track their net names
        for gl in self.global_labels:
            gl_pos = (round(gl.x, 2), round(gl.y, 2))

            if gl_pos not in point_to_net_names:
                point_to_net_names[gl_pos] = []
            point_to_net_names[gl_pos].append(gl.text)

            connect_to_wire(gl_pos)

        # Connect hierarchical labels and track their net names
        for hl in self.hier_labels:
            hl_pos = (round(hl.x, 2), round(hl.y, 2))

            if hl_pos not in point_to_net_names:
                point_to_net_names[hl_pos] = []
            point_to_net_names[hl_pos].append(hl.text)

            connect_to_wire(hl_pos)

        return parent, point_to_net_names, point_to_pins

    def extract_netlist(self, hierarchical: bool = False) -> dict[str, list[PinRef]]:
        """Extract netlist from schematic.

        Analyzes schematic connectivity to build a mapping from net names
        to the pins connected to each net.

        Args:
            hierarchical: When ``True``, walk every sub-sheet referenced
                by this schematic and return a single merged netlist
                covering the whole hierarchy. Sheet-pin / hierarchical-
                label connections in the parent context resolve to the
                parent's net name. Requires the schematic to have been
                loaded via :meth:`Schematic.load` so the source file
                path is known. When ``False`` (default), behaviour is
                byte-identical to previous releases: only the root
                sheet's wires, symbols, labels, and power symbols are
                considered.

        Returns:
            Dict mapping net names to list of connected pins.
            Net names are derived from:
            - Local labels (Label)
            - Global labels (GlobalLabel)
            - Hierarchical labels (HierarchicalLabel)
            - Power symbols (e.g., "+3.3V", "GND")
            - Auto-generated names for unnamed nets ("Net-(R1-1)")

        Raises:
            ValueError: If ``hierarchical=True`` is passed on a Schematic
                that was not loaded from disk (no file path is available
                to walk sub-sheets from).

        Example:
            >>> netlist = sch.extract_netlist()
            >>> print(netlist["+3.3V"])
            [PinRef(symbol_ref='U1', pin='VDD'), PinRef(symbol_ref='C1', pin='1')]
        """
        if hierarchical:
            # Lazy import to avoid an upward dependency from
            # schematic/models/ on the operations/ layer. The walker
            # already implements per-sheet extract_netlist() plus
            # sheet-pin / hierarchical-label merging across sheets.
            from pathlib import Path

            from kicad_tools.operations.netlist import (
                _collect_hierarchy_components,
            )

            saved_path = getattr(self, "_saved_path", None)
            if saved_path is None:
                raise ValueError(
                    "extract_netlist(hierarchical=True) requires the "
                    "Schematic to have been loaded from disk via "
                    "Schematic.load(path); no file path is available "
                    "to walk sub-sheets."
                )

            _, net_dict = _collect_hierarchy_components(Path(saved_path), "/")
            return net_dict

        parent, point_to_net_names, point_to_pins = self._build_connectivity_graph()

        def find(p):
            """Find root with path compression."""
            if p not in parent:
                parent[p] = p
            if parent[p] != p:
                parent[p] = find(parent[p])
            return parent[p]

        # Group all points by their root (connected component)
        root_to_points: dict[tuple, list[tuple]] = {}
        all_points = set(parent.keys()) | set(point_to_pins.keys()) | set(point_to_net_names.keys())
        for point in all_points:
            root = find(point)
            if root not in root_to_points:
                root_to_points[root] = []
            root_to_points[root].append(point)

        # Build netlist: map net names to pins
        netlist: dict[str, list[PinRef]] = {}

        for root, points in root_to_points.items():
            # Collect all pins in this connected component
            pins_in_net = []
            for point in points:
                if point in point_to_pins:
                    pins_in_net.extend(point_to_pins[point])

            if not pins_in_net:
                continue  # Skip nets with no pins

            # Collect all net names for this connected component
            net_names = []
            for point in points:
                if point in point_to_net_names:
                    net_names.extend(point_to_net_names[point])

            # Determine the net name
            if net_names:
                # Use the first label/power symbol name
                # Prefer power symbol names if present (they're more canonical)
                net_name = net_names[0]
            else:
                # Auto-generate net name from first pin
                first_pin = pins_in_net[0]
                net_name = f"Net-({first_pin.symbol_ref}-{first_pin.pin})"

            # Add pins to netlist
            if net_name not in netlist:
                netlist[net_name] = []
            netlist[net_name].extend(pins_in_net)

        return netlist

    def _resolve_pin_instance(self, symbol_ref: str, pin: str):
        """Return the placed ``SymbolInstance`` that owns ``pin``.

        For multi-unit symbols the same ``reference`` is placed once per
        unit; a given pin number lives on exactly one unit.  This selects
        the instance whose ``unit`` owns the requested pin so a pin's net
        is resolved against that unit's real placement, not the first
        instance with a matching reference (issue #4020).

        ``pin`` may be a pin number or a pin name.  Matching prefers an
        instance that both matches the reference and owns the pin under
        :func:`_instance_owns_pin`; if none does (e.g. the owning unit is
        not placed, or the pin is a unit-0 common), the first
        matching-reference instance is returned, preserving single-unit
        behaviour.  Returns ``None`` if no instance matches the reference.
        """
        fallback = None
        for sym in self.symbols:
            if sym.reference != symbol_ref:
                continue
            if fallback is None:
                fallback = sym
            for p in sym.symbol_def.pins:
                if (p.number == pin or p.name == pin) and _instance_owns_pin(sym, p):
                    return sym
        return fallback

    def get_net_for_pin(self, symbol_ref: str, pin: str) -> str | None:
        """Get the net name connected to a symbol's pin.

        Args:
            symbol_ref: Symbol reference designator (e.g., "R1", "U3")
            pin: Pin number or name (e.g., "1", "VDD")

        Returns:
            Net name if the pin is connected to a named net, None if floating.
            For unnamed nets, returns auto-generated name like "Net-(R1-1)".

        Example:
            >>> net = sch.get_net_for_pin("U1", "VDD")
            >>> print(net)
            '+3.3V'
        """
        # Find the placed instance that owns this pin.  A multi-unit
        # symbol shares a ``reference`` across several instances (one per
        # unit); the requested pin lives on exactly one of them.  Pick the
        # instance whose ``unit`` owns the pin rather than the first
        # matching reference, otherwise a unit-2 pin resolves against
        # unit-1's placement and returns a phantom net (issue #4020).
        # Fall back to the first matching-reference instance when no
        # unit-owning instance is placed, preserving single-unit behaviour.
        symbol = self._resolve_pin_instance(symbol_ref, pin)

        if symbol is None:
            return None

        # Find the pin on the symbol and get its position
        pin_pos = None
        for p in symbol.symbol_def.pins:
            if p.number == pin or p.name == pin:
                pos = symbol.pin_position(p.number)
                pin_pos = (round(pos[0], 2), round(pos[1], 2))
                break

        if pin_pos is None:
            return None

        # Build connectivity and find what net this pin is on
        parent, point_to_net_names, point_to_pins = self._build_connectivity_graph()

        def find(p):
            if p not in parent:
                parent[p] = p
            if parent[p] != p:
                parent[p] = find(parent[p])
            return parent[p]

        # Find the root of this pin's position
        pin_root = find(pin_pos)

        # Look for net names in the same connected component
        for point, net_names in point_to_net_names.items():
            if find(point) == pin_root and net_names:
                return net_names[0]

        # No named net - check if connected to anything
        # If connected to other pins, return auto-generated name
        for point, pins in point_to_pins.items():
            if find(point) == pin_root and point != pin_pos:
                return f"Net-({symbol_ref}-{pin})"

        # Pin is floating (not connected to anything)
        return None

    def pins_on_net(self, net_name: str) -> list[PinRef]:
        """Get all pins connected to a net.

        Args:
            net_name: Net name (e.g., "+3.3V", "SWDIO", "Net-(R1-1)")

        Returns:
            List of PinRef objects for all pins on the net.
            Returns empty list if net doesn't exist.

        Example:
            >>> pins = sch.pins_on_net("+3.3V")
            >>> for pin in pins:
            ...     print(f"{pin.symbol_ref} pin {pin.pin}")
            U1 pin VDD
            C1 pin 1
        """
        netlist = self.extract_netlist()
        return netlist.get(net_name, [])

    def are_connected(self, symbol1: str, pin1: str, symbol2: str, pin2: str) -> bool:
        """Check if two pins are on the same net.

        Args:
            symbol1: First symbol's reference designator
            pin1: First symbol's pin number or name
            symbol2: Second symbol's reference designator
            pin2: Second symbol's pin number or name

        Returns:
            True if both pins are connected to the same net, False otherwise.

        Example:
            >>> if sch.are_connected("U1", "VO", "C1", "1"):
            ...     print("Regulator output connected to capacitor")
        """
        # Find the placed instance that owns each pin.  For multi-unit
        # symbols the reference is shared across units, so select the
        # unit-owning instance rather than the first reference match —
        # otherwise two pins on different units resolve to the same
        # unit-1 geometry and report a false connection (issue #4020).
        sym1 = self._resolve_pin_instance(symbol1, pin1)
        sym2 = self._resolve_pin_instance(symbol2, pin2)

        if sym1 is None or sym2 is None:
            return False

        # Find pin positions
        pos1 = pos2 = None
        for p in sym1.symbol_def.pins:
            if p.number == pin1 or p.name == pin1:
                pos = sym1.pin_position(p.number)
                pos1 = (round(pos[0], 2), round(pos[1], 2))
                break

        for p in sym2.symbol_def.pins:
            if p.number == pin2 or p.name == pin2:
                pos = sym2.pin_position(p.number)
                pos2 = (round(pos[0], 2), round(pos[1], 2))
                break

        if pos1 is None or pos2 is None:
            return False

        # Build connectivity graph
        parent, _, _ = self._build_connectivity_graph()

        def find(p):
            if p not in parent:
                parent[p] = p
            if parent[p] != p:
                parent[p] = find(parent[p])
            return parent[p]

        # Check if both pins have the same root
        return find(pos1) == find(pos2)
