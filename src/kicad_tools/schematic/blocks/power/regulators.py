"""Voltage regulator circuit blocks: LDO and buck converters."""

from typing import TYPE_CHECKING

from ..base import CircuitBlock
from ...exceptions import PinNotFoundError
from ..interfaces import PowerPort

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic, SymbolInstance


def _resolve_pin(symbol: "SymbolInstance", names: list[str]) -> tuple[str, tuple[float, float]]:
    """Try a list of pin names in order, return the first one that exists.

    Used to support LDO families that use different pin name conventions
    (e.g., XC6206 / AP2204 use ``VIN`` / ``VOUT``, while AMS1117 / AP1117
    use ``VI`` / ``VO``).

    Args:
        symbol: Symbol instance to query.
        names: Candidate pin names to try, in priority order.

    Returns:
        Tuple of ``(resolved_pin_name, (x, y))``.

    Raises:
        PinNotFoundError: If none of the candidate names match a pin.
    """
    last_error: PinNotFoundError | None = None
    for name in names:
        try:
            return name, symbol.pin_position(name)
        except PinNotFoundError as exc:  # noqa: PERF203 - small list of fallbacks
            last_error = exc
    # Re-raise the last error so the caller sees a useful message
    assert last_error is not None
    raise last_error


class LDOBlock(CircuitBlock):
    """
    Low Dropout Regulator with input and output capacitors.

    Schematic:
        VIN ──┬── [C_in] ──┬── LDO ──┬── [C_out1] ──┬── [C_out2] ──┬── VOUT
              │            │    │    │              │              │
              └────────────┼────┴────┼──────────────┼──────────────┘
                           │   EN    │              │
        GND ───────────────┴─────────┴──────────────┴───────────────

    Ports:
        - VIN: Input voltage
        - VOUT: Output voltage
        - GND: Ground
        - EN: Enable (optional, usually tied to VIN)

    Power Domain Support:
        Use the `domain` parameter to create domain-specific power nets:
        - domain="" (default): Uses standard power symbols (+3V3, GND)
        - domain="A": Creates analog domain (+3V3A, AGND)
        - domain="D": Creates digital domain (+3V3D, DGND)
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        ref: str = "U1",
        value: str = "LDO",
        ldo_symbol: str = "Regulator_Linear:XC6206PxxxMR",
        input_cap: str = "10uF",
        output_caps: list[str] = None,
        cap_ref_start: int = 1,
        en_tied_to_vin: bool = True,
        domain: str = "",
        output_voltage: str = "3V3",
        auto_footprint: bool = False,
        pin_nets: dict[str, str] | None = None,
    ):
        """
        Create an LDO power supply block.

        Args:
            sch: Schematic to add to
            x: X coordinate of LDO center
            y: Y coordinate of LDO center
            ref: LDO reference designator
            value: LDO value label
            ldo_symbol: KiCad symbol for LDO
            input_cap: Input capacitor value
            output_caps: List of output capacitor values
            cap_ref_start: Starting reference number for caps
            en_tied_to_vin: If True, tie EN pin to VIN
            domain: Power domain identifier ("" for generic, "A" for analog, "D" for digital)
            output_voltage: Output voltage string (e.g., "3V3", "5V") for net naming
            auto_footprint: If True, automatically select footprint for caps based on value
            pin_nets: Optional mapping of LDO-IC pin name or number to net
                label.  For each entry, a one-grid (2.54 mm) stub wire
                is drawn from the pin away from the symbol center and the
                net label is placed on the stub endpoint so KiCad's
                label-on-wire ERC check is satisfied (see issue #2980,
                mirrors the ``GateDriverBlock`` pattern introduced in
                PR #2985).  When ``None`` (the default), no labels are
                emitted and behavior is unchanged.  Useful for binding
                ``VI``/``VO``/``GND`` to power-rail net names (e.g.
                ``{"VI": "+5V", "VO": "+3.3V"}``) so power-input ERC
                reads them as driven.  For every entry, an alias port is
                also added under the net name so callers can retrieve
                real pin coordinates via ``block.port("<net>")``.
        """
        super().__init__(sch, x, y)
        self.domain = domain
        self.output_voltage = output_voltage

        if output_caps is None:
            output_caps = ["10uF", "100nF"]

        # Spacing constants
        cap_spacing = 15
        input_cap_offset = -20  # Left of LDO
        output_cap_offset = 20  # Right of LDO

        # Place LDO
        self.ldo = sch.add_symbol(ldo_symbol, x, y, ref, value)

        # Place input capacitor
        c_in_x = x + input_cap_offset
        c_in_ref = f"C{cap_ref_start}"
        self.input_cap = sch.add_symbol(
            "Device:C", c_in_x, y + 15, c_in_ref, input_cap, auto_footprint=auto_footprint
        )

        # Place output capacitors
        self.output_caps = []
        for i, cap_value in enumerate(output_caps):
            c_out_x = x + output_cap_offset + i * cap_spacing
            c_out_ref = f"C{cap_ref_start + 1 + i}"
            cap = sch.add_symbol(
                "Device:C", c_out_x, y + 15, c_out_ref, cap_value, auto_footprint=auto_footprint
            )
            self.output_caps.append(cap)

        # Store all components
        self.components = {
            "LDO": self.ldo,
            "C_IN": self.input_cap,
        }
        for i, cap in enumerate(self.output_caps):
            self.components[f"C_OUT{i + 1}"] = cap

        # Get LDO pin positions.
        # Different LDO families use different pin names:
        #   XC6206PxxxMR, AP2204K, MCP1700: VIN / VOUT
        #   AMS1117 / AP1117 (Regulator_Linear): VI / VO
        # Try the canonical name first, then fall back to short aliases so
        # the same block works with AMS1117 footprints used on motor boards.
        self._vin_pin, vin_pos = _resolve_pin(self.ldo, ["VIN", "VI", "IN"])
        self._vout_pin, vout_pos = _resolve_pin(self.ldo, ["VOUT", "VO", "OUT"])
        self._gnd_pin = "GND"
        gnd_pos = self.ldo.pin_position("GND")

        # EN pin is optional -- 3-pin LDOs (e.g., XC6206PxxxMR, AMS1117) lack it
        try:
            en_pos = self.ldo.pin_position("EN")
        except PinNotFoundError:
            en_pos = None

        # Define ports
        self.ports = {
            "VIN": vin_pos,
            "VOUT": vout_pos,
            "GND": gnd_pos,
        }
        if en_pos is not None:
            self.ports["EN"] = en_pos

        # Register typed ports with power interface metadata
        self.typed_ports = {
            "VIN": PowerPort(
                name="VIN",
                x=vin_pos[0],
                y=vin_pos[1],
                direction="input",
            ),
            "VOUT": PowerPort(
                name="VOUT",
                x=vout_pos[0],
                y=vout_pos[1],
                direction="output",
            ),
            "GND": PowerPort(
                name="GND",
                x=gnd_pos[0],
                y=gnd_pos[1],
                direction="passive",
                voltage_min=0.0,
                voltage_max=0.0,
            ),
        }

        # Tie EN to VIN if requested and EN pin exists
        if en_tied_to_vin and en_pos is not None:
            # Connect EN to VIN (vertical wire)
            sch.add_wire(en_pos, (en_pos[0], vin_pos[1]))

        # Optional pin-net labels.  KiCad's label-only connectivity requires
        # the label coordinate to lie on a wire endpoint or segment; without
        # a stub, labels placed at the bare pin float and trigger ERC's
        # ``isolated_pin_label`` cascade.  For each ``pin_nets`` entry we
        # resolve the real pin position via ``self.ldo.pin_position``
        # (supporting either pin names or pin numbers), draw a one-grid
        # (2.54 mm) horizontal stub *away from the symbol center* (left
        # for pins on the symbol's left edge, right otherwise), and place
        # the label on the stub endpoint.  Mirrors the ``GateDriverBlock``
        # pattern from PR #2985; see issues #2980 and #2994.
        if pin_nets is not None:
            STUB = 2.54
            for pin_key, net_name in pin_nets.items():
                pin_pos = self.ldo.pin_position(pin_key)
                # Stub away from the symbol center.  When the pin lies
                # exactly on the center column we default to stubbing right.
                if pin_pos[0] < x:
                    label_x = pin_pos[0] - STUB
                else:
                    label_x = pin_pos[0] + STUB
                sch.add_wire(pin_pos, (label_x, pin_pos[1]), warn_on_collision=False)
                sch.add_label(net_name, label_x, pin_pos[1], rotation=0)
                # Expose the pin's real coordinate under the net name so
                # external wiring can reach it.  Do not overwrite an
                # existing port by the same name (preserves back-compat).
                if net_name not in self.ports:
                    self.ports[net_name] = pin_pos

    def connect_to_rails(
        self,
        vin_rail_y: float,
        vout_rail_y: float,
        gnd_rail_y: float,
        extend_vout_rail_to: float = None,
    ):
        """
        Connect LDO and caps to power rails.

        Args:
            vin_rail_y: Y coordinate of input voltage rail
            vout_rail_y: Y coordinate of output voltage rail
            gnd_rail_y: Y coordinate of ground rail
            extend_vout_rail_to: If set, extend VOUT rail to this X coordinate
        """
        sch = self.schematic

        # Connect LDO VIN to input rail (use resolved pin name for AMS1117 compat)
        sch.wire_to_rail(self.ldo, self._vin_pin, vin_rail_y)

        # Connect LDO VOUT to output rail
        sch.wire_to_rail(self.ldo, self._vout_pin, vout_rail_y)

        # Connect LDO GND to ground rail
        sch.wire_to_rail(self.ldo, self._gnd_pin, gnd_rail_y)

        # Wire input cap
        sch.wire_decoupling_cap(self.input_cap, vin_rail_y, gnd_rail_y)

        # Wire output caps
        for cap in self.output_caps:
            sch.wire_decoupling_cap(cap, vout_rail_y, gnd_rail_y)

        # Extend VOUT rail if requested
        if extend_vout_rail_to is not None:
            vout_pos = self.ldo.pin_position(self._vout_pin)
            sch.add_rail(vout_rail_y, vout_pos[0], extend_vout_rail_to)

    def get_vout_net_name(self) -> str:
        """Get the domain-specific output voltage net name.

        Returns:
            Net name based on domain and output voltage:
            - domain="": "+3V3" (generic)
            - domain="A": "+3V3A" (analog)
            - domain="D": "+3V3D" (digital)
        """
        if self.domain:
            return f"+{self.output_voltage}{self.domain}"
        return f"+{self.output_voltage}"

    def get_gnd_net_name(self) -> str:
        """Get the domain-specific ground net name.

        Returns:
            Ground net name based on domain:
            - domain="": "GND" (generic)
            - domain="A": "AGND" (analog)
            - domain="D": "DGND" (digital)
        """
        if self.domain:
            return f"{self.domain}GND"
        return "GND"

    def add_power_labels(
        self,
        vout_rail_y: float,
        gnd_rail_y: float,
        label_x_offset: float = -10,
    ):
        """Add domain-specific global labels for power nets.

        This method adds global labels instead of power symbols, enabling
        proper domain isolation (analog vs digital power planes).

        Args:
            vout_rail_y: Y coordinate where VOUT label should be placed
            gnd_rail_y: Y coordinate where GND label should be placed
            label_x_offset: X offset from LDO center for labels

        Example:
            ldo = LDOBlock(sch, x=100, y=50, domain="A", output_voltage="3V3")
            ldo.add_power_labels(vout_rail_y=30, gnd_rail_y=80)
            # Creates global labels: "+3V3A" and "AGND"
        """
        sch = self.schematic
        label_x = self.x + label_x_offset

        # Add output voltage global label (power input shape for consumers)
        vout_name = self.get_vout_net_name()
        sch.add_global_label(vout_name, label_x, vout_rail_y, shape="input", rotation=0)

        # Add ground global label (passive for bidirectional current flow)
        gnd_name = self.get_gnd_net_name()
        sch.add_global_label(gnd_name, label_x, gnd_rail_y, shape="passive", rotation=0)


class BuckConverter(CircuitBlock):
    """
    Switching buck (step-down) converter with configurable components.

    Creates a complete buck converter subcircuit for efficient voltage
    regulation from higher to lower voltages. Supports asynchronous
    topology (external Schottky diode) and synchronous topology
    (internal low-side FET).

    Schematic (async topology):
        VIN ──┬── [C_in] ──┬── U1 ──┬── [L1] ──┬── [C_out] ──┬── VOUT
              │            │   │    │          │             │
              │            │  FB    │    SW    │             │
              │            │   │   [D1]        │             │
              │            │  [R1]  │          │             │
              │            │   │   [R2]        │             │
              │            │   │    │          │             │
        GND ──┴────────────┴───┴────┴──────────┴─────────────┴──

    Schematic (sync topology - simplified, no external diode):
        VIN ──┬── [C_in] ──┬── U1 ──┬── [L1] ──┬── [C_out] ──┬── VOUT
              │            │   │    │          │             │
              │            │  FB   SW          │             │
              │            │   │    │          │             │
              │            │  [R1] [R2]        │             │
              │            │   │    │          │             │
        GND ──┴────────────┴───┴────┴──────────┴─────────────┴──

    Ports:
        - VIN: Input voltage
        - VOUT: Output voltage
        - GND: Ground
        - SW: Switch node (for debug/monitoring)
        - FB: Feedback node (for adjustable versions)

    Example:
        from kicad_tools.schematic.blocks import BuckConverter

        # Create 24V to 5V buck converter
        buck = BuckConverter(
            sch, x=100, y=100,
            ref="U1",
            value="LM2596-5.0",
            input_voltage=24,
            output_voltage=5,
            input_cap="100uF",
            output_cap="220uF",
            inductor="33uH",
            diode="SS34",
        )

        # Connect to rails
        buck.connect_to_rails(
            vin_rail_y=30,
            vout_rail_y=50,
            gnd_rail_y=200,
        )
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        ref: str = "U1",
        value: str = "LM2596-5.0",
        regulator_symbol: str = "Regulator_Switching:LM2596S-5",
        input_voltage: float = 24.0,
        output_voltage: float = 5.0,
        topology: str = "async",
        input_cap: str = "100uF",
        output_cap: str = "220uF",
        inductor: str = "33uH",
        diode: str = "SS34",
        feedback_divider: bool = False,
        r_top: str = "10k",
        r_bottom: str = "3.3k",
        cap_ref_start: int = 1,
        inductor_ref: str = "L1",
        diode_ref: str = "D1",
        auto_footprint: bool = False,
        pin_nets: dict[str, str] | None = None,
    ):
        """
        Create a buck converter power supply block.

        Args:
            sch: Schematic to add to
            x: X coordinate of regulator center
            y: Y coordinate of regulator center
            ref: Regulator reference designator
            value: Regulator value label (e.g., "LM2596-5.0", "MP1584EN")
            regulator_symbol: KiCad symbol for the regulator IC
            input_voltage: Input voltage in volts
            output_voltage: Output voltage in volts
            topology: "async" (external Schottky diode) or "sync" (internal FET)
            input_cap: Input capacitor value (e.g., "100uF")
            output_cap: Output capacitor value (e.g., "220uF")
            inductor: Inductor value (e.g., "33uH", "47uH")
            diode: Schottky diode part number (for async topology)
            feedback_divider: If True, add feedback resistor divider (for adjustable versions)
            r_top: Top feedback resistor value (if feedback_divider=True)
            r_bottom: Bottom feedback resistor value (if feedback_divider=True)
            cap_ref_start: Starting reference number for capacitors
            inductor_ref: Reference designator for inductor
            diode_ref: Reference designator for diode
            auto_footprint: If True, automatically select footprints
            pin_nets: Optional mapping of regulator-IC pin name or number to
                net label.  For each entry, a one-grid (2.54 mm) stub wire
                is drawn from the pin away from the symbol center and the
                net label is placed on the stub endpoint so KiCad's
                label-on-wire ERC check is satisfied (see issue #2980,
                mirrors the ``GateDriverBlock`` pattern introduced in
                PR #2985).  When ``None`` (the default), no labels are
                emitted and behavior is unchanged.  Useful for declaring
                static-tied feedback pins (e.g. ``"FB": "+5V"`` for a
                fixed-output LM2596 variant) or for binding ``VIN``/``GND``
                to power-rail net names so power-input ERC reads them as
                driven.  For every entry, an alias port is also added
                under the net name so callers can retrieve real pin
                coordinates via ``block.port("<net>")``.
        """
        super().__init__(sch, x, y)
        self.input_voltage = input_voltage
        self.output_voltage = output_voltage
        self.topology = topology
        self.has_feedback_divider = feedback_divider

        # Spacing constants
        input_cap_offset = -25  # Left of regulator
        output_cap_offset = 50  # Right of regulator
        inductor_offset = 25  # Between regulator and output cap
        diode_y_offset = 20  # Below switch node

        # Place regulator IC
        self.regulator = sch.add_symbol(regulator_symbol, x, y, ref, value)

        # Place input capacitor
        c_in_x = x + input_cap_offset
        c_in_ref = f"C{cap_ref_start}"
        self.input_cap = sch.add_symbol(
            "Device:C_Polarized", c_in_x, y + 15, c_in_ref, input_cap, auto_footprint=auto_footprint
        )

        # Place inductor
        l_x = x + inductor_offset
        self.inductor = sch.add_symbol(
            "Device:L", l_x, y, inductor_ref, inductor, auto_footprint=auto_footprint
        )

        # Place output capacitor
        c_out_x = x + output_cap_offset
        c_out_ref = f"C{cap_ref_start + 1}"
        self.output_cap = sch.add_symbol(
            "Device:C_Polarized",
            c_out_x,
            y + 15,
            c_out_ref,
            output_cap,
            auto_footprint=auto_footprint,
        )

        # Store all components
        self.components = {
            "REGULATOR": self.regulator,
            "C_IN": self.input_cap,
            "L": self.inductor,
            "C_OUT": self.output_cap,
        }

        # Place diode for async topology
        if topology == "async":
            d_x = x + inductor_offset
            d_y = y + diode_y_offset
            self.diode = sch.add_symbol(
                "Device:D_Schottky",
                d_x,
                d_y,
                diode_ref,
                diode,
                rotation=90,
                auto_footprint=auto_footprint,
            )
            self.components["D"] = self.diode

        # Add feedback divider if requested (for adjustable versions)
        if feedback_divider:
            fb_x = x + output_cap_offset + 15
            r_top_ref = f"R{cap_ref_start}"
            r_bottom_ref = f"R{cap_ref_start + 1}"

            self.r_fb_top = sch.add_symbol(
                "Device:R", fb_x, y + 5, r_top_ref, r_top, auto_footprint=auto_footprint
            )
            self.r_fb_bottom = sch.add_symbol(
                "Device:R", fb_x, y + 20, r_bottom_ref, r_bottom, auto_footprint=auto_footprint
            )
            self.components["R_FB_TOP"] = self.r_fb_top
            self.components["R_FB_BOTTOM"] = self.r_fb_bottom

            # Wire feedback divider internally
            r_top_2 = self.r_fb_top.pin_position("2")
            r_bottom_1 = self.r_fb_bottom.pin_position("1")
            sch.add_wire(r_top_2, r_bottom_1)

        # Get regulator pin positions (depends on actual symbol pinout)
        # Common pins: VIN, VOUT/SW, GND, FB, ON/OFF
        # Note: Pin names vary by symbol; we'll use generic approach
        try:
            vin_pos = self.regulator.pin_position("VIN")
        except KeyError:
            vin_pos = self.regulator.pin_position("IN")

        try:
            sw_pos = self.regulator.pin_position("OUT")
        except KeyError:
            try:
                sw_pos = self.regulator.pin_position("SW")
            except KeyError:
                sw_pos = self.regulator.pin_position("VOUT")

        try:
            gnd_pos = self.regulator.pin_position("GND")
        except KeyError:
            gnd_pos = self.regulator.pin_position("VSS")

        # Get optional FB pin
        try:
            fb_pos = self.regulator.pin_position("FB")
            has_fb_pin = True
        except KeyError:
            fb_pos = None
            has_fb_pin = False

        # Get inductor positions
        l_in = self.inductor.pin_position("1")
        l_out = self.inductor.pin_position("2")

        # Wire regulator SW/OUT to inductor input
        sch.add_wire(sw_pos, l_in)

        # Wire diode for async topology (cathode to SW node, anode to GND)
        if topology == "async":
            d_cathode = self.diode.pin_position("K")

            # Connect diode cathode to switch node
            sch.add_wire(d_cathode, (l_in[0], d_cathode[1]))
            sch.add_wire((l_in[0], d_cathode[1]), l_in)
            sch.add_junction(l_in[0], l_in[1])

        # Get capacitor positions
        c_out_pos = self.output_cap.pin_position("1")

        # Wire inductor output to output capacitor
        sch.add_wire(l_out, (l_out[0], c_out_pos[1]))
        sch.add_wire((l_out[0], c_out_pos[1]), c_out_pos)
        sch.add_junction(l_out[0], c_out_pos[1])

        # Wire feedback divider to output if present
        if feedback_divider:
            r_top_1 = self.r_fb_top.pin_position("1")
            # Connect top of divider to VOUT
            sch.add_wire(r_top_1, (r_top_1[0], c_out_pos[1]))
            sch.add_junction(r_top_1[0], c_out_pos[1])

            # Connect FB divider midpoint to FB pin if available
            if has_fb_pin and fb_pos:
                fb_node = self.r_fb_top.pin_position("2")
                sch.add_wire(fb_pos, (fb_node[0], fb_pos[1]))
                sch.add_wire((fb_node[0], fb_pos[1]), fb_node)

        # Define ports
        self.ports = {
            "VIN": vin_pos,
            "VOUT": c_out_pos,
            "GND": gnd_pos,
            "SW": l_in,  # Switch node
        }
        if has_fb_pin and fb_pos:
            self.ports["FB"] = fb_pos

        # Store internal positions for rail connections
        self._sw_node = l_in
        self._vout_node = c_out_pos

        # Optional pin-net labels.  KiCad's label-only connectivity requires
        # the label coordinate to lie on a wire endpoint or segment; without
        # a stub, labels placed at the bare pin float and trigger ERC's
        # ``isolated_pin_label`` cascade.  For each ``pin_nets`` entry we
        # resolve the real pin position via ``self.regulator.pin_position``
        # (supporting either pin names or pin numbers), draw a one-grid
        # (2.54 mm) horizontal stub *away from the symbol center* (left
        # for pins on the symbol's left edge, right otherwise), and place
        # the label on the stub endpoint.  Mirrors the ``GateDriverBlock``
        # pattern from PR #2985; see issues #2980 and #2994.
        if pin_nets is not None:
            STUB = 2.54
            for pin_key, net_name in pin_nets.items():
                pin_pos = self.regulator.pin_position(pin_key)
                # Stub away from the symbol center.  When the pin lies
                # exactly on the center column we default to stubbing right.
                if pin_pos[0] < x:
                    label_x = pin_pos[0] - STUB
                else:
                    label_x = pin_pos[0] + STUB
                sch.add_wire(pin_pos, (label_x, pin_pos[1]), warn_on_collision=False)
                sch.add_label(net_name, label_x, pin_pos[1], rotation=0)
                # Expose the pin's real coordinate under the net name so
                # external wiring can reach it.  Do not overwrite an
                # existing port by the same name (preserves back-compat).
                if net_name not in self.ports:
                    self.ports[net_name] = pin_pos

    def connect_to_rails(
        self,
        vin_rail_y: float,
        vout_rail_y: float,
        gnd_rail_y: float,
        add_junctions: bool = True,
    ):
        """
        Connect buck converter and caps to power rails.

        Args:
            vin_rail_y: Y coordinate of input voltage rail
            vout_rail_y: Y coordinate of output voltage rail
            gnd_rail_y: Y coordinate of ground rail
            add_junctions: Whether to add junction markers
        """
        sch = self.schematic

        # Connect regulator VIN to input rail
        vin_pos = self.ports["VIN"]
        sch.add_wire(vin_pos, (vin_pos[0], vin_rail_y), warn_on_collision=False)

        # Connect regulator GND to ground rail
        gnd_pos = self.ports["GND"]
        sch.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y), warn_on_collision=False)

        # Wire input cap
        sch.wire_decoupling_cap(self.input_cap, vin_rail_y, gnd_rail_y)

        # Wire output cap
        sch.wire_decoupling_cap(self.output_cap, vout_rail_y, gnd_rail_y)

        # Connect VOUT to output rail
        vout_pos = self.ports["VOUT"]
        sch.add_wire(vout_pos, (vout_pos[0], vout_rail_y), warn_on_collision=False)

        # Connect diode anode to GND (async topology)
        if self.topology == "async":
            d_anode = self.diode.pin_position("A")
            sch.add_wire(d_anode, (d_anode[0], gnd_rail_y), warn_on_collision=False)
            if add_junctions:
                sch.add_junction(d_anode[0], gnd_rail_y)

        # Connect feedback divider bottom to GND if present
        if self.has_feedback_divider:
            r_bottom_2 = self.r_fb_bottom.pin_position("2")
            sch.add_wire(r_bottom_2, (r_bottom_2[0], gnd_rail_y), warn_on_collision=False)
            if add_junctions:
                sch.add_junction(r_bottom_2[0], gnd_rail_y)

        if add_junctions:
            sch.add_junction(vin_pos[0], vin_rail_y)
            sch.add_junction(gnd_pos[0], gnd_rail_y)
            sch.add_junction(vout_pos[0], vout_rail_y)

    def get_efficiency_estimate(self) -> float:
        """
        Get estimated efficiency for this buck converter.

        Returns:
            Estimated efficiency as a decimal (0.80-0.95 typical).
            Actual efficiency depends on load current, switching frequency,
            and component quality.
        """
        # Rough efficiency estimate based on topology and voltage ratio
        duty_cycle = self.output_voltage / self.input_voltage

        if self.topology == "sync":
            # Synchronous buck typically 90-95% efficient
            base_efficiency = 0.92
        else:
            # Async buck typically 80-90% efficient (diode losses)
            base_efficiency = 0.85

        # Efficiency drops at extreme duty cycles
        if duty_cycle < 0.1 or duty_cycle > 0.9:
            return base_efficiency * 0.95

        return base_efficiency


# Factory functions


def create_3v3_ldo(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "U1",
    cap_ref_start: int = 1,
    domain: str = "",
) -> LDOBlock:
    """Create a 3.3V LDO block with standard capacitors.

    Args:
        sch: Schematic to add to
        x: X coordinate of LDO center
        y: Y coordinate of LDO center
        ref: LDO reference designator
        cap_ref_start: Starting reference number for capacitors
        domain: Power domain identifier ("" for generic, "A" for analog, "D" for digital)

    Returns:
        LDOBlock with domain-aware power net naming

    Example:
        # Create analog and digital 3.3V LDOs
        analog_ldo = create_3v3_ldo(sch, 50, 50, "U1", domain="A")
        digital_ldo = create_3v3_ldo(sch, 150, 50, "U2", domain="D")

        # Wire to rails and add domain-specific labels
        analog_ldo.connect_to_rails(vin_rail_y=30, vout_rail_y=40, gnd_rail_y=80)
        analog_ldo.add_power_labels(vout_rail_y=40, gnd_rail_y=80)
        # Creates: +3V3A and AGND global labels

        digital_ldo.connect_to_rails(vin_rail_y=30, vout_rail_y=40, gnd_rail_y=80)
        digital_ldo.add_power_labels(vout_rail_y=40, gnd_rail_y=80)
        # Creates: +3V3D and DGND global labels
    """
    return LDOBlock(
        sch,
        x,
        y,
        ref=ref,
        value="XC6206-3.3V",
        ldo_symbol="Regulator_Linear:XC6206PxxxMR",
        input_cap="10uF",
        output_caps=["10uF", "100nF"],
        cap_ref_start=cap_ref_start,
        domain=domain,
        output_voltage="3V3",
    )


def create_5v_buck(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "U1",
    input_voltage: float = 24.0,
    cap_ref_start: int = 1,
    diode_ref: str = "D1",
) -> BuckConverter:
    """
    Create a 5V buck converter with sensible defaults.

    Common configuration for 12V or 24V to 5V conversion using
    the LM2596-5.0 fixed-output regulator.

    Args:
        sch: Schematic to add to
        x: X coordinate of regulator center
        y: Y coordinate of regulator center
        ref: Regulator reference designator
        input_voltage: Input voltage (typically 12V or 24V)
        cap_ref_start: Starting reference number for capacitors
        diode_ref: Reference designator for the Schottky diode

    Returns:
        BuckConverter instance configured for 5V output.

    Example:
        buck = create_5v_buck(sch, x=100, y=100, input_voltage=24)
        buck.connect_to_rails(vin_rail_y=30, vout_rail_y=50, gnd_rail_y=200)
    """
    return BuckConverter(
        sch,
        x,
        y,
        ref=ref,
        value="LM2596-5.0",
        regulator_symbol="Regulator_Switching:LM2596S-5",
        input_voltage=input_voltage,
        output_voltage=5.0,
        topology="async",
        input_cap="100uF",
        output_cap="220uF",
        inductor="33uH",
        diode="SS34",
        feedback_divider=False,
        cap_ref_start=cap_ref_start,
        diode_ref=diode_ref,
    )


def create_3v3_buck(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "U1",
    input_voltage: float = 12.0,
    cap_ref_start: int = 1,
) -> BuckConverter:
    """
    Create a 3.3V buck converter with sensible defaults.

    Common configuration for 5V or 12V to 3.3V conversion using
    the LM2596-3.3 fixed-output regulator.

    Args:
        sch: Schematic to add to
        x: X coordinate of regulator center
        y: Y coordinate of regulator center
        ref: Regulator reference designator
        input_voltage: Input voltage (typically 5V or 12V)
        cap_ref_start: Starting reference number for capacitors

    Returns:
        BuckConverter instance configured for 3.3V output.

    Example:
        buck = create_3v3_buck(sch, x=100, y=100, input_voltage=12)
        buck.connect_to_rails(vin_rail_y=30, vout_rail_y=50, gnd_rail_y=200)
    """
    return BuckConverter(
        sch,
        x,
        y,
        ref=ref,
        value="LM2596-3.3",
        regulator_symbol="Regulator_Switching:LM2596S-3.3",
        input_voltage=input_voltage,
        output_voltage=3.3,
        topology="async",
        input_cap="100uF",
        output_cap="220uF",
        inductor="33uH",
        diode="SS34",
        feedback_divider=False,
        cap_ref_start=cap_ref_start,
    )


def create_12v_buck(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "U1",
    input_voltage: float = 24.0,
    cap_ref_start: int = 1,
) -> BuckConverter:
    """
    Create a 12V buck converter with sensible defaults.

    Common configuration for 24V or 48V to 12V conversion using
    the LM2596-12 fixed-output regulator.

    Args:
        sch: Schematic to add to
        x: X coordinate of regulator center
        y: Y coordinate of regulator center
        ref: Regulator reference designator
        input_voltage: Input voltage (typically 24V or 48V)
        cap_ref_start: Starting reference number for capacitors

    Returns:
        BuckConverter instance configured for 12V output.

    Example:
        buck = create_12v_buck(sch, x=100, y=100, input_voltage=24)
        buck.connect_to_rails(vin_rail_y=30, vout_rail_y=50, gnd_rail_y=200)
    """
    return BuckConverter(
        sch,
        x,
        y,
        ref=ref,
        value="LM2596-12",
        regulator_symbol="Regulator_Switching:LM2596S-12",
        input_voltage=input_voltage,
        output_voltage=12.0,
        topology="async",
        input_cap="100uF",
        output_cap="330uF",
        inductor="68uH",
        diode="SS54",
        feedback_divider=False,
        cap_ref_start=cap_ref_start,
    )


# ---------------------------------------------------------------------------
# Cascaded buck → LDO power tree
# ---------------------------------------------------------------------------

# Configuration table mapping (vin, v_mid, vout) to (buck_part, ldo_part)
# tuples. Each tuple is (value, symbol, ldo_input_cap, ldo_output_caps).
#
# The buck side keeps the existing per-rail factory defaults
# (e.g. LM2596-5.0 with 100uF/220uF/33uH/SS34) — see ``create_5v_buck``,
# ``create_12v_buck``. The LDO side is what differs from ``create_3v3_ldo``:
# board 05 uses AMS1117-3.3 with a single 10uF in *and* out, not the
# XC6206 10uF/100nF pair.
_BUCK_PART_TABLE: dict[tuple[float, float, float], tuple[str, str]] = {
    # (vin, v_mid, vout) -> (buck_value, buck_symbol)
    (24.0, 5.0, 3.3): ("LM2596-5.0", "Regulator_Switching:LM2596S-5"),
    (12.0, 5.0, 3.3): ("LM2596-5.0", "Regulator_Switching:LM2596S-5"),
    (48.0, 12.0, 5.0): ("LM2596-12", "Regulator_Switching:LM2596S-12"),
}

_LDO_PART_TABLE: dict[tuple[float, float], tuple[str, str, str, list[str]]] = {
    # (v_mid, vout) -> (ldo_value, ldo_symbol, ldo_input_cap, ldo_output_caps)
    (5.0, 3.3): ("AMS1117-3.3", "Regulator_Linear:AMS1117-3.3", "10uF", ["10uF"]),
    (12.0, 5.0): ("AMS1117-5.0", "Regulator_Linear:AMS1117-5.0", "10uF", ["10uF"]),
}

# Buck-stage component defaults keyed on (v_mid). Allows the (48, 12, 5)
# combination to pick a heavier inductor / diode than the 5V case.
_BUCK_COMPONENTS_TABLE: dict[float, dict[str, str]] = {
    # v_mid -> dict of buck component values
    5.0: {"input_cap": "100uF", "output_cap": "220uF", "inductor": "33uH", "diode": "SS34"},
    12.0: {"input_cap": "100uF", "output_cap": "330uF", "inductor": "68uH", "diode": "SS54"},
}


class DualSupplyCascade(CircuitBlock):
    """Cascaded buck → LDO power tree (high-V in, mid-V intermediate, low-V clean out).

    Composes a :class:`BuckConverter` and an :class:`LDOBlock` with a shared
    mid-voltage rail. The buck stage is chosen for *efficiency* (24V→5V step
    down with ~85% efficiency) and the LDO stage is chosen for a *clean*
    low-noise final rail (5V→3.3V at ~66% linear-regulator efficiency).

    Use this block when you have a high-voltage input (12V/24V/48V) that
    needs to feed both a power-hungry mid-voltage rail (e.g., 5V gate
    drivers, motor logic) and a quiet low-voltage rail (e.g., 3.3V MCU,
    ADC reference). A pure buck would be more efficient but noisier; a
    pure LDO from 24V would dissipate ~21V × I_load as heat.

    Schematic topology::

        VIN ──[BuckConverter]── V_MID ──[LDOBlock]── VOUT
              │   |   |    |          │  |  |   │
              C3  L1  D2   C4         C5 U2 C6  │
              U1                                │
        GND ──┴───┴───┴────┴──────────┴──┴──┴───┴── GND

    The subclass owns both child blocks and exposes them as ``.buck`` and
    ``.ldo`` so callers can drill into stage-specific details (e.g., tying
    the LM2596 ON/OFF pin to GND for always-on operation).

    Ports:
        - ``VIN``: high-voltage input (matches buck VIN)
        - ``V_MID``: intermediate rail (buck VOUT == LDO VIN)
        - ``VOUT``: clean low-voltage output (matches LDO VOUT)
        - ``GND``: ground (shared with both stages)

    See Also:
        :class:`BuckConverter` for stage-level buck configuration.
        :class:`LDOBlock` for stage-level LDO configuration.
        :func:`create_dual_supply_cascade` for a higher-level factory with
        sensible defaults driven by ``(vin, v_mid, vout)``.
    """

    def __init__(
        self,
        sch: "Schematic",
        x_buck: float,
        x_ldo: float,
        y: float,
        *,
        vin: float,
        v_mid: float,
        vout: float,
        # Buck stage configuration
        buck_ref: str = "U1",
        buck_value: str = "LM2596-5.0",
        buck_symbol: str = "Regulator_Switching:LM2596S-5",
        buck_topology: str = "async",
        buck_input_cap: str = "100uF",
        buck_output_cap: str = "220uF",
        buck_inductor: str = "33uH",
        buck_diode: str = "SS34",
        buck_diode_ref: str = "D2",
        buck_inductor_ref: str = "L1",
        # LDO stage configuration
        ldo_ref: str = "U2",
        ldo_value: str = "AMS1117-3.3",
        ldo_symbol: str = "Regulator_Linear:AMS1117-3.3",
        ldo_input_cap: str = "10uF",
        ldo_output_caps: list[str] | None = None,
        ldo_en_tied_to_vin: bool = True,
        # Numbering
        cap_ref_start: int = 3,
        auto_footprint: bool = False,
        # Per-stage pin_nets passthrough (issue #2994)
        buck_pin_nets: dict[str, str] | None = None,
        ldo_pin_nets: dict[str, str] | None = None,
    ):
        """Create a dual-supply cascade with explicit per-stage configuration.

        Args:
            sch: Schematic to add components to.
            x_buck: X coordinate of the buck regulator centre.
            x_ldo: X coordinate of the LDO centre.
            y: Y coordinate shared by both stages.
            vin: Buck input voltage in volts (informational, used for
                efficiency estimate).
            v_mid: Intermediate voltage (buck output == LDO input) in volts.
            vout: Final clean output voltage in volts.
            buck_ref: Buck regulator reference designator (e.g. ``"U1"``).
            buck_value: Buck regulator value label (e.g. ``"LM2596-5.0"``).
            buck_symbol: KiCad symbol for the buck regulator IC.
            buck_topology: ``"async"`` or ``"sync"``.
            buck_input_cap: Buck input capacitor value.
            buck_output_cap: Buck output capacitor value.
            buck_inductor: Buck inductor value.
            buck_diode: Buck Schottky diode part number (async only).
            buck_diode_ref: Buck diode reference designator.
            buck_inductor_ref: Buck inductor reference designator.
            ldo_ref: LDO reference designator (e.g. ``"U2"``).
            ldo_value: LDO value label (e.g. ``"AMS1117-3.3"``).
            ldo_symbol: KiCad symbol for the LDO IC.
            ldo_input_cap: LDO input capacitor value.
            ldo_output_caps: List of LDO output capacitor values (default
                ``["10uF"]`` matching AMS1117 board-05 topology).
            ldo_en_tied_to_vin: If True and the LDO has an EN pin, tie it
                to VIN.
            cap_ref_start: Starting reference number for capacitors. The
                buck stage uses ``C{n}`` and ``C{n+1}``; the LDO stage uses
                ``C{n+2}``...``C{n+1+len(output_caps)+1}``. Defaults to 3
                so the cascade fits the board-05 numbering (C3-C6).
            auto_footprint: If True, automatically select footprints for
                capacitors based on value.
            buck_pin_nets: Optional mapping passed through to the buck
                stage's :class:`BuckConverter` ``pin_nets`` kwarg.  See
                :class:`BuckConverter` for details.  When ``None`` (the
                default), no per-pin labels are emitted on the buck stage
                and behavior is unchanged.
            ldo_pin_nets: Optional mapping passed through to the LDO
                stage's :class:`LDOBlock` ``pin_nets`` kwarg.  See
                :class:`LDOBlock` for details.  When ``None`` (the
                default), no per-pin labels are emitted on the LDO stage
                and behavior is unchanged.
        """
        super().__init__(sch, x_buck, y)
        self.vin = vin
        self.v_mid = v_mid
        self.vout = vout

        if ldo_output_caps is None:
            ldo_output_caps = ["10uF"]

        # ----- Buck stage -----
        # Buck owns C{cap_ref_start} (input cap) and C{cap_ref_start+1}
        # (output cap), plus L1 (inductor) and D2 (Schottky for async).
        self.buck = BuckConverter(
            sch,
            x=x_buck,
            y=y,
            ref=buck_ref,
            value=buck_value,
            regulator_symbol=buck_symbol,
            input_voltage=vin,
            output_voltage=v_mid,
            topology=buck_topology,
            input_cap=buck_input_cap,
            output_cap=buck_output_cap,
            inductor=buck_inductor,
            diode=buck_diode,
            feedback_divider=False,
            cap_ref_start=cap_ref_start,
            inductor_ref=buck_inductor_ref,
            diode_ref=buck_diode_ref,
            auto_footprint=auto_footprint,
            pin_nets=buck_pin_nets,
        )

        # ----- LDO stage -----
        # LDO uses C{cap_ref_start+2} (input cap) and C{cap_ref_start+3..}
        # (output caps). cap_ref_start in LDOBlock is the *input* cap index.
        ldo_cap_ref_start = cap_ref_start + 2
        self.ldo = LDOBlock(
            sch,
            x=x_ldo,
            y=y,
            ref=ldo_ref,
            value=ldo_value,
            ldo_symbol=ldo_symbol,
            input_cap=ldo_input_cap,
            output_caps=ldo_output_caps,
            cap_ref_start=ldo_cap_ref_start,
            en_tied_to_vin=ldo_en_tied_to_vin,
            output_voltage=_voltage_string(vout),
            auto_footprint=auto_footprint,
            pin_nets=ldo_pin_nets,
        )

        # ----- Composed component dictionary -----
        # Re-export the underlying components with prefixed keys so callers
        # can iterate (e.g., for BOM generation).
        self.components = {}
        for k, v in self.buck.components.items():
            self.components[f"BUCK_{k}"] = v
        for k, v in self.ldo.components.items():
            self.components[f"LDO_{k}"] = v

        # ----- Composed ports -----
        # VIN comes from buck; VOUT comes from LDO; V_MID is the buck VOUT
        # / LDO VIN shared rail; GND is shared.
        self.ports = {
            "VIN": self.buck.ports["VIN"],
            "V_MID": self.buck.ports["VOUT"],  # == buck output node
            "VOUT": self.ldo.ports["VOUT"],
            "GND": self.buck.ports["GND"],
        }

        # Typed ports: only the LDO populates typed_ports today, so the
        # cascade synthesizes its own typed view from each side. This keeps
        # the cascade composable with future ``buck & ldo`` operator paths.
        self.typed_ports = {
            "VIN": PowerPort(
                name="VIN",
                x=self.buck.ports["VIN"][0],
                y=self.buck.ports["VIN"][1],
                direction="input",
            ),
            "V_MID": PowerPort(
                name="V_MID",
                x=self.buck.ports["VOUT"][0],
                y=self.buck.ports["VOUT"][1],
                direction="bidirectional",
            ),
            "VOUT": PowerPort(
                name="VOUT",
                x=self.ldo.ports["VOUT"][0],
                y=self.ldo.ports["VOUT"][1],
                direction="output",
            ),
            "GND": PowerPort(
                name="GND",
                x=self.buck.ports["GND"][0],
                y=self.buck.ports["GND"][1],
                direction="passive",
                voltage_min=0.0,
                voltage_max=0.0,
            ),
        }

    def connect_to_rails(
        self,
        vin_rail_y: float,
        v_mid_rail_y: float,
        vout_rail_y: float,
        gnd_rail_y: float,
    ) -> None:
        """Wire each child stage to its rails.

        The buck stage connects ``VIN`` -> ``vin_rail_y``,
        ``VOUT`` -> ``v_mid_rail_y``, and ``GND`` -> ``gnd_rail_y``.
        The LDO stage connects ``VIN`` -> ``v_mid_rail_y`` (the shared
        intermediate rail), ``VOUT`` -> ``vout_rail_y``, and
        ``GND`` -> ``gnd_rail_y``.

        Args:
            vin_rail_y: Y coordinate of the buck-input rail.
            v_mid_rail_y: Y coordinate of the buck-out / LDO-in shared rail.
            vout_rail_y: Y coordinate of the final clean output rail.
            gnd_rail_y: Y coordinate of the ground rail.
        """
        # Buck: input -> intermediate, share GND
        self.buck.connect_to_rails(
            vin_rail_y=vin_rail_y,
            vout_rail_y=v_mid_rail_y,
            gnd_rail_y=gnd_rail_y,
        )
        # LDO: intermediate -> output, share GND
        self.ldo.connect_to_rails(
            vin_rail_y=v_mid_rail_y,
            vout_rail_y=vout_rail_y,
            gnd_rail_y=gnd_rail_y,
        )

    def get_efficiency_estimate(self) -> float:
        """Total cascade efficiency as a decimal.

        Computed as ``buck_efficiency * ldo_efficiency`` where the LDO
        efficiency is the linear-regulator approximation
        ``vout / v_mid`` (no quiescent current modelled).

        For the canonical 24V → 5V → 3.3V case this is approximately
        ``0.85 * (3.3 / 5.0) ≈ 0.561`` — i.e. ~56% wall-to-load. Useful
        as a quick sanity check during design.

        Returns:
            Estimated total efficiency, in [0.0, 1.0].
        """
        buck_eff = self.buck.get_efficiency_estimate()
        ldo_eff = self.vout / self.v_mid if self.v_mid > 0 else 0.0
        return buck_eff * ldo_eff


def _voltage_string(v: float) -> str:
    """Format a voltage as a net-name-friendly string (e.g. 3.3 -> '3V3')."""
    # Map common rails to their conventional net-name forms
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v))}V"
    integer_part = int(v)
    fractional = round((v - integer_part) * 10)
    return f"{integer_part}V{fractional}"


def create_dual_supply_cascade(
    sch: "Schematic",
    x_buck: float,
    x_ldo: float,
    y: float,
    *,
    vin: float = 24.0,
    v_mid: float = 5.0,
    vout: float = 3.3,
    cap_ref_start: int = 3,
    buck_ref: str = "U1",
    ldo_ref: str = "U2",
    buck_diode_ref: str = "D2",
    buck_inductor_ref: str = "L1",
    auto_footprint: bool = False,
    buck_pin_nets: dict[str, str] | None = None,
    ldo_pin_nets: dict[str, str] | None = None,
) -> DualSupplyCascade:
    """Create a buck → LDO cascade with sensible defaults from a config table.

    Defaults to the board-05 case (24V → 5V buck → 3.3V LDO) with the
    LM2596-5.0 + AMS1117-3.3 part pair. Other supported voltage triples
    are listed below; callers needing a non-listed combination should
    instantiate :class:`DualSupplyCascade` directly with explicit
    ``buck_symbol`` / ``ldo_symbol`` overrides.

    Supported ``(vin, v_mid, vout)`` combinations:

    +-------------------+---------------+-------------------+
    | (vin, v_mid, vout)| Buck part     | LDO part          |
    +===================+===============+===================+
    | (24, 5, 3.3)      | LM2596-5.0    | AMS1117-3.3       |
    +-------------------+---------------+-------------------+
    | (12, 5, 3.3)      | LM2596-5.0    | AMS1117-3.3       |
    +-------------------+---------------+-------------------+
    | (48, 12, 5)       | LM2596-12     | AMS1117-5.0       |
    +-------------------+---------------+-------------------+

    Args:
        sch: Schematic to add components to.
        x_buck: X coordinate of the buck regulator.
        x_ldo: X coordinate of the LDO.
        y: Y coordinate shared by both stages.
        vin: Buck input voltage in volts.
        v_mid: Intermediate voltage in volts.
        vout: Final output voltage in volts.
        cap_ref_start: Starting capacitor reference number. Defaults to 3
            (board-05 case where C1/C2 are reserved for the input
            connector). Buck owns ``C{n}``/``C{n+1}``; LDO owns
            ``C{n+2}``...
        buck_ref: Buck regulator reference designator.
        ldo_ref: LDO reference designator.
        buck_diode_ref: Buck Schottky diode reference designator.
        buck_inductor_ref: Buck inductor reference designator.
        auto_footprint: If True, auto-select footprints for capacitors.
        buck_pin_nets: Optional ``pin_nets`` mapping passed through to the
            buck stage.  See :class:`BuckConverter` for details.
        ldo_pin_nets: Optional ``pin_nets`` mapping passed through to the
            LDO stage.  See :class:`LDOBlock` for details.

    Returns:
        Configured :class:`DualSupplyCascade` instance.

    Raises:
        ValueError: If ``(vin, v_mid, vout)`` is not in the supported
            table.

    Example:
        Board 05 setup::

            cascade = create_dual_supply_cascade(
                sch, x_buck=80, x_ldo=140, y=100,
                vin=24.0, v_mid=5.0, vout=3.3,
                cap_ref_start=3,
            )
            cascade.connect_to_rails(
                vin_rail_y=RAIL_VMOTOR,
                v_mid_rail_y=RAIL_5V,
                vout_rail_y=RAIL_3V3,
                gnd_rail_y=RAIL_GND,
            )

            # Drill in for board-specific tweaks
            on_off = cascade.buck.regulator.pin_position("~{ON}/OFF")
    """
    key = (float(vin), float(v_mid), float(vout))
    if key not in _BUCK_PART_TABLE:
        supported = ", ".join(
            f"({int(v_in_)}, {v_mid_:g}, {vout_:g})" for (v_in_, v_mid_, vout_) in _BUCK_PART_TABLE
        )
        raise ValueError(
            f"Unsupported (vin, v_mid, vout) = ({vin}, {v_mid}, {vout}). "
            f"Supported combinations: {supported}. "
            f"For other voltages, instantiate DualSupplyCascade directly "
            f"with explicit buck_symbol and ldo_symbol arguments."
        )

    buck_value, buck_symbol = _BUCK_PART_TABLE[key]
    ldo_key = (float(v_mid), float(vout))
    if ldo_key not in _LDO_PART_TABLE:
        # This should not be reachable because the buck table already
        # implies a valid LDO key — guard anyway for future extensions.
        raise ValueError(f"No LDO mapping for (v_mid, vout) = ({v_mid}, {vout})")
    ldo_value, ldo_symbol, ldo_input_cap, ldo_output_caps = _LDO_PART_TABLE[ldo_key]

    buck_components = _BUCK_COMPONENTS_TABLE.get(
        float(v_mid),
        {"input_cap": "100uF", "output_cap": "220uF", "inductor": "33uH", "diode": "SS34"},
    )

    return DualSupplyCascade(
        sch,
        x_buck=x_buck,
        x_ldo=x_ldo,
        y=y,
        vin=vin,
        v_mid=v_mid,
        vout=vout,
        buck_ref=buck_ref,
        buck_value=buck_value,
        buck_symbol=buck_symbol,
        buck_topology="async",
        buck_input_cap=buck_components["input_cap"],
        buck_output_cap=buck_components["output_cap"],
        buck_inductor=buck_components["inductor"],
        buck_diode=buck_components["diode"],
        buck_diode_ref=buck_diode_ref,
        buck_inductor_ref=buck_inductor_ref,
        ldo_ref=ldo_ref,
        ldo_value=ldo_value,
        ldo_symbol=ldo_symbol,
        ldo_input_cap=ldo_input_cap,
        ldo_output_caps=list(ldo_output_caps),
        ldo_en_tied_to_vin=True,
        cap_ref_start=cap_ref_start,
        auto_footprint=auto_footprint,
        buck_pin_nets=buck_pin_nets,
        ldo_pin_nets=ldo_pin_nets,
    )
