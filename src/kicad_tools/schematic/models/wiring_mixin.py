"""
Schematic Wiring Mixin

Provides specialized wiring helper methods for common circuit patterns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..logging import _log_info
from .elements import HierarchicalLabel, PowerSymbol, Wire
from .symbol import SymbolInstance

if TYPE_CHECKING:
    pass


class SchematicWiringMixin:
    """Mixin providing specialized wiring operations for Schematic class."""

    def wire_pin_to_point(
        self,
        symbol: SymbolInstance,
        pin_name: str,
        target: tuple[float, float],
        route: str = "auto",
    ) -> list[Wire]:
        """Wire a symbol pin to a target point using orthogonal routing.

        Args:
            symbol: The symbol instance
            pin_name: Name or number of the pin
            target: (x, y) destination point
            route: Routing style - "auto", "vertical_first", "horizontal_first"

        Returns:
            List of wires created
        """
        pin_pos = symbol.pin_position(pin_name)
        return self._route_orthogonal(pin_pos, target, route)

    def wire_pins(
        self, sym1: SymbolInstance, pin1: str, sym2: SymbolInstance, pin2: str, route: str = "auto"
    ) -> list[Wire]:
        """Wire two symbol pins together using orthogonal routing.

        Args:
            sym1: First symbol
            pin1: Pin name/number on first symbol
            sym2: Second symbol
            pin2: Pin name/number on second symbol
            route: Routing style - "auto", "vertical_first", "horizontal_first"

        Returns:
            List of wires created
        """
        p1 = sym1.pin_position(pin1)
        p2 = sym2.pin_position(pin2)
        return self._route_orthogonal(p1, p2, route)

    def wire_to_rail(
        self,
        symbol: SymbolInstance,
        pin_name: str,
        rail_y: float,
        extend_to_x: float = None,
        add_junction: bool = True,
    ) -> list[Wire]:
        """Connect a pin vertically to a horizontal rail.

        Args:
            symbol: The symbol instance
            pin_name: Name or number of the pin
            rail_y: Y coordinate of the horizontal rail
            extend_to_x: If set, also add horizontal wire to this X position
            add_junction: Whether to add a junction at the rail (default True)

        Returns:
            List of wires created
        """
        pin_pos = symbol.pin_position(pin_name)
        wires = []

        # Vertical wire from pin to rail
        wires.append(self.add_wire(pin_pos, (pin_pos[0], rail_y)))

        # Add junction at rail intersection
        if add_junction:
            self.add_junction(pin_pos[0], rail_y)

        # Optional horizontal extension
        if extend_to_x is not None and extend_to_x != pin_pos[0]:
            wires.append(self.add_wire((pin_pos[0], rail_y), (extend_to_x, rail_y)))

        return wires

    def add_rail(
        self, y: float, x_start: float, x_end: float, net_label: str = None, snap: bool = True
    ) -> Wire:
        """Add a horizontal power/ground rail.

        Args:
            y: Y coordinate of the rail (snapped to grid)
            x_start: Starting X coordinate (snapped to grid)
            x_end: Ending X coordinate (snapped to grid)
            net_label: Optional net label to add at the start
            snap: Whether to apply grid snapping (default: True)

        Returns:
            The wire created
        """
        wire = self.add_wire((x_start, y), (x_end, y), snap=snap)
        if net_label:
            # Use the actual snapped wire coordinates for the label
            self.add_label(net_label, wire.x1, wire.y1, rotation=0, snap=False)
        return wire

    def wire_power_to_pin(
        self,
        power_lib_id: str,
        symbol: SymbolInstance,
        pin_name: str,
        power_offset: tuple[float, float] = (0, -10),
    ) -> PowerSymbol:
        """Add a power symbol and wire it to a pin.

        Args:
            power_lib_id: Power symbol library ID (e.g., "power:+3.3V")
            symbol: Target symbol
            pin_name: Target pin name
            power_offset: (dx, dy) offset from pin for power symbol placement

        Returns:
            The power symbol created
        """
        pin_pos = symbol.pin_position(pin_name)
        power_x = pin_pos[0] + power_offset[0]
        power_y = pin_pos[1] + power_offset[1]

        pwr = self.add_power(power_lib_id, power_x, power_y)
        self.add_wire(pin_pos, (power_x, power_y))

        return pwr

    def wire_decoupling_cap(
        self,
        cap: SymbolInstance,
        power_rail_y: float,
        gnd_rail_y: float,
        add_junctions: bool = True,
    ) -> list[Wire]:
        """Wire a decoupling capacitor between power and ground rails.

        Assumes cap is vertical with pin 1 (top) to power and pin 2 (bottom) to ground.

        Args:
            cap: Capacitor symbol instance
            power_rail_y: Y coordinate of power rail
            gnd_rail_y: Y coordinate of ground rail
            add_junctions: Whether to add junction dots

        Returns:
            List of wires created
        """
        wires = []

        # Get pin positions (Device:C has pins 1 at top, 2 at bottom)
        try:
            pin1_pos = cap.pin_position("1")
            pin2_pos = cap.pin_position("2")
        except ValueError:
            # Try by name if numbered pins don't work
            pin1_pos = cap.pin_position("~")
            pin2_pos = cap.pin_position("~")

        # Wire top to power rail
        wires.append(self.add_wire(pin1_pos, (pin1_pos[0], power_rail_y)))
        if add_junctions:
            self.add_junction(pin1_pos[0], power_rail_y)

        # Wire bottom to ground rail
        wires.append(self.add_wire(pin2_pos, (pin2_pos[0], gnd_rail_y)))
        if add_junctions:
            self.add_junction(pin2_pos[0], gnd_rail_y)

        return wires

    def add_decoupling_pair(
        self,
        x: float,
        y: float,
        ic_pin: tuple[float, float],
        power_symbol: str,
        ref_100nf: str,
        ref_10uf: str,
        grid: float = 2.54,
        footprint_100nf: str = "Capacitor_SMD:C_0402_1005Metric",
        footprint_10uf: str = "Capacitor_SMD:C_0805_2012Metric",
    ) -> dict:
        """Add a decoupling capacitor pair (100nF + 10uF) with power symbol and wiring.

        This is a common pattern for IC power pins: two caps in parallel connected
        between a power symbol and an IC power pin.

        Args:
            x: X coordinate (center of cap pair)
            y: Y coordinate (center of caps)
            ic_pin: (x, y) position of the IC power pin to connect to
            power_symbol: Power symbol lib_id (e.g., "power:+3.3V")
            ref_100nf: Reference designator for 100nF cap
            ref_10uf: Reference designator for 10uF cap
            grid: Grid spacing in mm
            footprint_100nf: Footprint for 100nF cap
            footprint_10uf: Footprint for 10uF cap

        Returns:
            Dictionary with keys: cap_100nf, cap_10uf, power, wires
        """
        wires = []

        # Place capacitors side by side
        cap_100nf = self.add_symbol(
            "Device:C_Small",
            x=x - 2 * grid,
            y=y,
            ref=ref_100nf,
            value="100nF",
            footprint=footprint_100nf,
        )
        cap_10uf = self.add_symbol(
            "Device:C_Small",
            x=x + 2 * grid,
            y=y,
            ref=ref_10uf,
            value="10uF",
            footprint=footprint_10uf,
        )

        # Add power symbol above caps
        power = self.add_power(power_symbol, x=x, y=y - 3 * grid)

        # Wire from caps to power symbol (top side)
        wires.append(self.add_wire((x - 2 * grid, y - grid), (x - 2 * grid, y - 2 * grid)))
        wires.append(self.add_wire((x + 2 * grid, y - grid), (x + 2 * grid, y - 2 * grid)))
        wires.append(self.add_wire((x - 2 * grid, y - 2 * grid), (x + 2 * grid, y - 2 * grid)))
        self.add_junction(x, y - 2 * grid)

        # Wire from caps to IC pin (bottom side)
        wires.append(self.add_wire((x - 2 * grid, y + grid), (x - 2 * grid, y + 2 * grid)))
        wires.append(self.add_wire((x + 2 * grid, y + grid), (x + 2 * grid, y + 2 * grid)))
        wires.append(self.add_wire((x - 2 * grid, y + 2 * grid), (x + 2 * grid, y + 2 * grid)))
        self.add_junction(x, y + 2 * grid)

        # Wire from center of bottom bus to IC pin
        wires.append(self.add_wire((x, y + 2 * grid), ic_pin))

        _log_info(f"Added decoupling pair {ref_100nf}/{ref_10uf} at ({x}, {y}) -> IC pin")

        return {
            "cap_100nf": cap_100nf,
            "cap_10uf": cap_10uf,
            "power": power,
            "wires": wires,
        }

    def tie_pins_to_power(
        self,
        symbol: SymbolInstance,
        pin_names: list[str],
        power_symbol: str,
        x_offset: float = -4 * 2.54,
        grid: float = 2.54,
    ) -> PowerSymbol:
        """Tie multiple IC pins to a common power symbol.

        Args:
            symbol: The IC symbol instance
            pin_names: List of pin names to tie together
            power_symbol: Power symbol lib_id
            x_offset: X offset from pins for the power symbol
            grid: Grid spacing

        Returns:
            The PowerSymbol instance created
        """
        if not pin_names:
            raise ValueError("pin_names must not be empty")

        # Get all pin positions
        pin_positions = [symbol.pin_position(name) for name in pin_names]

        # Calculate center Y for power symbol
        y_coords = [p[1] for p in pin_positions]
        center_y = (min(y_coords) + max(y_coords)) / 2
        first_pin_x = pin_positions[0][0]

        # Power symbol position
        pwr_x = first_pin_x + x_offset
        pwr_y = center_y + 3 * grid if "GND" in power_symbol else center_y - 3 * grid

        # Add power symbol
        pwr = self.add_power(power_symbol, x=pwr_x, y=pwr_y)

        # Wire all pins to the common bus line
        bus_x = first_pin_x + x_offset

        for i, (pin_pos, _pin_name) in enumerate(zip(pin_positions, pin_names, strict=False)):
            # Horizontal wire from pin to bus
            self.add_wire(pin_pos, (bus_x, pin_pos[1]))

            # Vertical wire to next pin (if not last)
            if i < len(pin_positions) - 1:
                next_pin_y = pin_positions[i + 1][1]
                self.add_wire((bus_x, pin_pos[1]), (bus_x, next_pin_y))
                self.add_junction(bus_x, next_pin_y)

        # Wire from bus to power symbol
        last_pin_y = pin_positions[-1][1]
        self.add_wire((bus_x, last_pin_y), (bus_x, pwr_y))

        _log_info(f"Tied pins {pin_names} to {power_symbol}")

        return pwr

    def wire_ldo(
        self,
        ldo: SymbolInstance,
        input_rail_y: float,
        output_rail_y: float,
        gnd_rail_y: float,
        tie_en_to_vin: bool = True,
    ) -> list[Wire]:
        """Wire an LDO regulator to power rails.

        Args:
            ldo: LDO symbol instance
            input_rail_y: Y coordinate of input voltage rail
            output_rail_y: Y coordinate of output voltage rail
            gnd_rail_y: Y coordinate of ground rail
            tie_en_to_vin: Whether to tie EN pin to VIN

        Returns:
            List of wires created
        """
        wires = []

        # Get pin positions
        vin_pos = ldo.pin_position("VIN")
        vout_pos = ldo.pin_position("VOUT")
        gnd_pos = ldo.pin_position("GND")
        en_pos = ldo.pin_position("EN")

        # VIN to input rail
        wires.append(self.add_wire(vin_pos, (vin_pos[0], input_rail_y)))
        self.add_junction(vin_pos[0], input_rail_y)

        # VOUT to output rail
        wires.append(self.add_wire(vout_pos, (vout_pos[0], output_rail_y)))
        self.add_junction(vout_pos[0], output_rail_y)

        # GND to ground rail
        wires.append(self.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y)))
        self.add_junction(gnd_pos[0], gnd_rail_y)

        # EN tied to VIN if requested
        if tie_en_to_vin:
            wires.append(self.add_wire(en_pos, (en_pos[0], vin_pos[1])))
            self.add_junction(en_pos[0], vin_pos[1])

        return wires

    def _route_orthogonal(
        self, start: tuple[float, float], end: tuple[float, float], route: str = "auto"
    ) -> list[Wire]:
        """Route between two points using orthogonal (Manhattan) routing.

        Args:
            start: Starting point (x, y)
            end: Ending point (x, y)
            route: "auto", "vertical_first", or "horizontal_first"

        Returns:
            List of wires created
        """
        x1, y1 = start
        x2, y2 = end

        # If points are aligned, single wire
        if x1 == x2 or y1 == y2:
            return [self.add_wire(start, end)]

        # Choose routing direction
        if route == "auto":
            if abs(x2 - x1) < abs(y2 - y1):
                route = "horizontal_first"
            else:
                route = "vertical_first"

        if route == "horizontal_first":
            mid = (x2, y1)
        else:  # vertical_first
            mid = (x1, y2)

        return [self.add_wire(start, mid), self.add_wire(mid, end)]

    def connect_hier_label_to_pin(
        self,
        label_name: str,
        symbol: SymbolInstance,
        pin_name: str,
        label_offset: float = 15,
        shape: str = None,
    ) -> HierarchicalLabel:
        """Add a hierarchical label connected to a symbol pin.

        Args:
            label_name: Name for the hierarchical label
            symbol: Target symbol
            pin_name: Target pin name
            label_offset: Horizontal offset from pin for label placement
            shape: Override shape

        Returns:
            The hierarchical label created
        """
        pin_pos = symbol.pin_position(pin_name)

        # Determine label direction based on pin position relative to symbol center
        if pin_pos[0] < symbol.x:
            label_x = pin_pos[0] - label_offset
            auto_shape = "input"
            rotation = 0
        else:
            label_x = pin_pos[0] + label_offset
            auto_shape = "output"
            rotation = 180

        final_shape = shape if shape else auto_shape

        # Add wire from pin to label position
        self.add_wire(pin_pos, (label_x, pin_pos[1]))

        # Add the label
        return self.add_hier_label(label_name, label_x, pin_pos[1], final_shape, rotation)

    def wire_bus(
        self,
        symbols_and_pins: list[tuple[SymbolInstance, str]],
        bus_y: float = None,
        bus_x: float = None,
    ) -> list[Wire]:
        """Wire multiple pins to a common bus line.

        Either bus_y (horizontal bus) or bus_x (vertical bus) must be specified.

        Args:
            symbols_and_pins: List of (symbol, pin_name) tuples to connect
            bus_y: Y coordinate for horizontal bus
            bus_x: X coordinate for vertical bus

        Returns:
            List of wires created
        """
        if bus_y is None and bus_x is None:
            raise ValueError("Either bus_y or bus_x must be specified")

        wires = []
        bus_points = []

        for symbol, pin_name in symbols_and_pins:
            pin_pos = symbol.pin_position(pin_name)

            if bus_y is not None:
                bus_point = (pin_pos[0], bus_y)
            else:
                bus_point = (bus_x, pin_pos[1])

            wires.append(self.add_wire(pin_pos, bus_point))
            bus_points.append(bus_point)

        # Sort bus points and create bus wire
        if bus_y is not None:
            bus_points.sort(key=lambda p: p[0])
            if len(bus_points) > 1:
                wires.append(self.add_wire(bus_points[0], bus_points[-1]))
        else:
            bus_points.sort(key=lambda p: p[1])
            if len(bus_points) > 1:
                wires.append(self.add_wire(bus_points[0], bus_points[-1]))

        # Add junctions at bus connection points
        for point in bus_points:
            self.add_junction(point[0], point[1])

        return wires

    def print_symbol_pins(self, symbol: SymbolInstance, name: str = None):
        """Debug helper: Print all pin positions for a symbol."""
        display_name = name or symbol.reference
        print(f"\n{display_name} pins at ({symbol.x}, {symbol.y}) rot={symbol.rotation}:")
        for pin in symbol.symbol_def.pins:
            pos = symbol.pin_position(pin.name)
            print(f"  {pin.name} ({pin.number}): ({pos[0]:.2f}, {pos[1]:.2f})")

    def wire_ferrite_bead(self, fb: SymbolInstance, rail1_y: float, rail2_y: float) -> list[Wire]:
        """Wire a ferrite bead between two ground rails."""
        wires = []

        pin1_pos = fb.pin_position("1")
        pin2_pos = fb.pin_position("2")

        wires.append(self.add_wire(pin1_pos, (pin1_pos[0], rail1_y)))
        self.add_junction(pin1_pos[0], rail1_y)

        wires.append(self.add_wire(pin2_pos, (pin2_pos[0], rail2_y)))
        self.add_junction(pin2_pos[0], rail2_y)

        return wires

    def wire_oscillator(
        self,
        osc: SymbolInstance,
        power_rail_y: float,
        gnd_rail_y: float,
        output_label: str = None,
        tie_en_to_vdd: bool = True,
    ) -> list[Wire]:
        """Wire an oscillator to power rails.

        Args:
            osc: Oscillator symbol instance
            power_rail_y: Y coordinate of power rail
            gnd_rail_y: Y coordinate of ground rail
            output_label: If set, add a label at the output
            tie_en_to_vdd: Whether to tie EN pin to Vdd

        Returns:
            List of wires created
        """
        wires = []

        try:
            vdd_pos = osc.pin_position("Vdd")
            gnd_pos = osc.pin_position("GND")
            out_pos = osc.pin_position("OUT")
            en_pos = osc.pin_position("EN")
        except ValueError:
            # Try alternate pin names
            vdd_pos = osc.pin_position("4")
            gnd_pos = osc.pin_position("2")
            out_pos = osc.pin_position("3")
            en_pos = osc.pin_position("1")

        # Vdd to power rail
        wires.append(self.add_wire(vdd_pos, (vdd_pos[0], power_rail_y)))
        self.add_junction(vdd_pos[0], power_rail_y)

        # GND to ground rail
        wires.append(self.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y)))
        self.add_junction(gnd_pos[0], gnd_rail_y)

        # EN tied to Vdd if requested
        if tie_en_to_vdd:
            wires.extend(self._route_orthogonal(en_pos, (vdd_pos[0], power_rail_y)))
            self.add_junction(vdd_pos[0], power_rail_y)

        # Output label
        if output_label:
            self.add_label(output_label, out_pos[0] + 5, out_pos[1])
            wires.append(self.add_wire(out_pos, (out_pos[0] + 5, out_pos[1])))

        return wires
