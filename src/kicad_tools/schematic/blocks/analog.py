"""Analog circuit blocks: op-amps, ADC input filters, sensor interfaces."""

import contextlib
from typing import TYPE_CHECKING, Literal

from .base import CircuitBlock

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic, SymbolInstance  # noqa: F401


class ADCInputFilterBlock(CircuitBlock):
    """
    Anti-aliasing filter for ADC input.

    Schematic (RC topology):
        IN ──[R1]──┬── OUT
                   │
                  [C1]
                   │
                  GND

    Schematic (2nd order RC):
        IN ──[R1]──┬──[R2]──┬── OUT
                   │        │
                  [C1]     [C2]
                   │        │
                  GND      GND

    Ports:
        - IN: Filter input
        - OUT: Filter output (to ADC)
        - GND: Ground reference
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        cutoff_hz: float = 10000,
        order: int = 1,
        ref_start: int = 1,
        topology: Literal["rc", "active"] = "rc",
        resistor_symbol: str = "Device:R",
        capacitor_symbol: str = "Device:C",
    ):
        """
        Create an ADC input filter.

        Args:
            sch: Schematic to add to
            x: X coordinate of input
            y: Y coordinate (center of filter)
            cutoff_hz: Cutoff frequency in Hz
            order: Filter order (1 or 2 for RC)
            ref_start: Starting reference number for components
            topology: Filter topology ("rc" or "active")
            resistor_symbol: KiCad symbol for resistors
            capacitor_symbol: KiCad symbol for capacitors
        """
        super().__init__(sch, x, y)

        # Calculate component values for given cutoff
        # For RC filter: fc = 1/(2*pi*R*C)
        # Assume standard R value and calculate C
        r_ohms = 10000  # 10k standard
        import math

        c_farads = 1 / (2 * math.pi * r_ohms * cutoff_hz)

        # Format component values
        r_value = "10k"
        if c_farads >= 1e-6:
            c_value = f"{c_farads * 1e6:.1f}uF"
        elif c_farads >= 1e-9:
            c_value = f"{c_farads * 1e9:.0f}nF"
        else:
            c_value = f"{c_farads * 1e12:.0f}pF"

        self.components = {}
        spacing = 15  # mm between stages

        # First stage
        r1 = sch.add_symbol(resistor_symbol, x, y, f"R{ref_start}", r_value)
        c1 = sch.add_symbol(capacitor_symbol, x + spacing, y + 10, f"C{ref_start}", c_value)
        self.components["R1"] = r1
        self.components["C1"] = c1

        # Wire R1 to C1
        r1_out = r1.pin_position("2")
        c1_in = c1.pin_position("1")
        sch.add_wire(r1_out, (r1_out[0], c1_in[1]))
        sch.add_wire((r1_out[0], c1_in[1]), c1_in)

        last_node = r1_out

        if order >= 2:
            # Second stage
            r2 = sch.add_symbol(resistor_symbol, x + spacing * 2, y, f"R{ref_start + 1}", r_value)
            c2 = sch.add_symbol(
                capacitor_symbol, x + spacing * 3, y + 10, f"C{ref_start + 1}", c_value
            )
            self.components["R2"] = r2
            self.components["C2"] = c2

            # Wire first stage to second stage
            r2_in = r2.pin_position("1")
            sch.add_wire(r1_out, r2_in)

            r2_out = r2.pin_position("2")
            c2_in = c2.pin_position("1")
            sch.add_wire(r2_out, (r2_out[0], c2_in[1]))
            sch.add_wire((r2_out[0], c2_in[1]), c2_in)

            last_node = r2_out

        # Define ports
        r1_in = r1.pin_position("1")
        c1_gnd = c1.pin_position("2")

        self.ports = {
            "IN": r1_in,
            "OUT": last_node,
            "GND": c1_gnd,
        }


class OpAmpBlock(CircuitBlock):
    """
    Op-amp circuit block.

    Schematic (non-inverting):
                    +VCC
                      │
                     [C+]
                      │
        IN ──[Rin]──┬─┤+
                    │ │   │──── OUT
                    │ ├─  │
                    │ │   │
                    │ [Rf]│
                    │  │  │
                    └──┴──┘
                    [Rg]
                     │
                    GND

    Ports:
        - IN: Signal input
        - OUT: Signal output
        - VCC: Positive supply
        - VEE: Negative supply (optional)
        - GND: Ground reference
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        topology: Literal["non_inverting", "inverting", "buffer"] = "non_inverting",
        gain: float = 1.0,
        ref: str = "U1",
        opamp_symbol: str = "Amplifier_Operational:TL072",
        resistor_symbol: str = "Device:R",
        capacitor_symbol: str = "Device:C",
    ):
        """
        Create an op-amp circuit block.

        Args:
            sch: Schematic to add to
            x: X coordinate of op-amp center
            y: Y coordinate of op-amp center
            topology: Circuit topology
            gain: Desired gain (for non-inverting: G = 1 + Rf/Rg)
            ref: Reference designator for op-amp
            opamp_symbol: KiCad symbol for op-amp
            resistor_symbol: KiCad symbol for resistors
            capacitor_symbol: KiCad symbol for capacitors
        """
        super().__init__(sch, x, y)

        # Place op-amp
        self.opamp = sch.add_symbol(opamp_symbol, x, y, ref, "")
        self.components = {"U": self.opamp}

        # Calculate resistor values for desired gain
        # For non-inverting: G = 1 + Rf/Rg
        # For inverting: G = -Rf/Rin
        if topology == "buffer":
            # Unity gain buffer - no external components needed
            rf_value = None
            rg_value = None
        elif topology == "non_inverting":
            # Assume Rg = 10k, calculate Rf for desired gain
            rg = 10000
            rf = rg * (gain - 1)
            rf_value = self._format_resistance(rf) if gain > 1 else None
            rg_value = "10k" if gain > 1 else None
        else:  # inverting
            rin = 10000
            rf = rin * abs(gain)
            rf_value = self._format_resistance(rf)
            rg_value = "10k"  # Rin for inverting

        # Place feedback components if needed
        if rf_value:
            r_ref_num = int(ref[1:]) if ref[1:].isdigit() else 1
            rf = sch.add_symbol(
                resistor_symbol, x + 15, y - 15, f"R{r_ref_num}", rf_value, rotation=90
            )
            self.components["Rf"] = rf

        if rg_value:
            r_ref_num = int(ref[1:]) if ref[1:].isdigit() else 1
            rg = sch.add_symbol(resistor_symbol, x - 15, y + 15, f"R{r_ref_num + 1}", rg_value)
            self.components["Rg"] = rg

        # Place decoupling capacitor
        c_ref_num = int(ref[1:]) if ref[1:].isdigit() else 1
        c_bypass = sch.add_symbol(capacitor_symbol, x + 10, y - 25, f"C{c_ref_num}", "100nF")
        self.components["C_bypass"] = c_bypass

        # Define ports (positions will depend on actual symbol)
        self.ports = {
            "IN": (x - 20, y),
            "OUT": (x + 20, y),
            "VCC": (x, y - 15),
            "GND": (x, y + 15),
        }

    @staticmethod
    def _format_resistance(r_ohms: float) -> str:
        """Format resistance value as string."""
        if r_ohms >= 1e6:
            return f"{r_ohms / 1e6:.1f}M"
        if r_ohms >= 1e3:
            return f"{r_ohms / 1e3:.1f}k"
        return f"{r_ohms:.0f}R"


class VoltageDividerSense(CircuitBlock):
    """
    Voltage divider for sensing higher voltages.

    Schematic:
        VIN ──[R1]──┬── VOUT (to ADC)
                    │
                   [R2]
                    │
                   GND

    Ports:
        - VIN: Input voltage to measure
        - VOUT: Divided output (to ADC)
        - GND: Ground reference
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        ratio: float = 11.0,
        ref_start: int = 1,
        resistor_symbol: str = "Device:R",
    ):
        """
        Create a voltage divider for sensing.

        Args:
            sch: Schematic to add to
            x: X coordinate
            y: Y coordinate of junction point
            ratio: Voltage division ratio (Vin/Vout)
            ref_start: Starting reference number
            resistor_symbol: KiCad symbol for resistors
        """
        super().__init__(sch, x, y)

        # Calculate resistor values for given ratio
        # Vout = Vin * R2/(R1+R2)
        # ratio = Vin/Vout = (R1+R2)/R2
        # If R2 = 10k, then R1 = R2 * (ratio - 1)
        r2 = 10000
        r1 = r2 * (ratio - 1)

        r1_value = self._format_resistance(r1)
        r2_value = "10k"

        # Place R1 (top resistor)
        self.r1 = sch.add_symbol(resistor_symbol, x, y - 15, f"R{ref_start}", r1_value, rotation=90)

        # Place R2 (bottom resistor)
        self.r2 = sch.add_symbol(
            resistor_symbol, x, y + 15, f"R{ref_start + 1}", r2_value, rotation=90
        )

        self.components = {"R1": self.r1, "R2": self.r2}

        # Wire R1 to R2
        r1_out = self.r1.pin_position("2")
        r2_in = self.r2.pin_position("1")
        sch.add_wire(r1_out, r2_in)

        # Define ports
        self.ports = {
            "VIN": self.r1.pin_position("1"),
            "VOUT": r1_out,  # Junction point
            "GND": self.r2.pin_position("2"),
        }

    @staticmethod
    def _format_resistance(r_ohms: float) -> str:
        """Format resistance value as string."""
        if r_ohms >= 1e6:
            return f"{r_ohms / 1e6:.1f}M"
        if r_ohms >= 1e3:
            return f"{r_ohms / 1e3:.1f}k"
        return f"{r_ohms:.0f}R"


class HallSensorInput(CircuitBlock):
    """
    Single-channel filtered Hall-sensor / open-collector GPIO input.

    Provides the canonical noise-immune input topology for digital Hall
    effect sensors (e.g. SS41, A3144) or other open-collector signals: a
    pull-up resistor to VCC plus an RC low-pass filter to GND.  The
    block is a generic GPIO input filter — it does *not* contain any
    motor power-stage components, so it lives next to ``ThermistorSense``
    in :mod:`analog` rather than in :mod:`motor`.

    Schematic:
        VCC ──[R_pull]──┬── SIGNAL_OUT (to MCU GPIO)
                        │
        SIGNAL_IN ──────┤
                        │
                      [C_filt]
                        │
                       GND

    The connector pin (or sensor open-collector output) joins the
    pull-up / filter junction at ``SIGNAL_IN``; the filtered, pulled-up
    signal is exposed at ``SIGNAL_OUT`` (electrically the same node, but
    named for clarity at the call site).

    Ports:
        - VCC: Pull-up reference rail (typically +3.3V)
        - SIGNAL_IN: Sensor / connector pin input
        - SIGNAL_OUT: Filtered output to MCU GPIO
        - GND: Ground reference (filter cap return)
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        pullup_value: str = "10k",
        filter_cap: str = "10nF",
        ref_start: int = 1,
        resistor_symbol: str = "Device:R",
        capacitor_symbol: str = "Device:C",
    ):
        """
        Create a filtered Hall-sensor / GPIO input block.

        Args:
            sch: Schematic to add to
            x: X coordinate of the junction node
            y: Y coordinate of the junction node
            pullup_value: Pull-up resistor value (default 10k)
            filter_cap: Filter capacitor value (default 10nF — gives
                ~1.6 kHz cutoff with a 10k pull-up, well above typical
                Hall commutation rates)
            ref_start: Starting reference number (R{ref_start}, C{ref_start})
            resistor_symbol: KiCad symbol for the pull-up resistor
            capacitor_symbol: KiCad symbol for the filter capacitor
        """
        super().__init__(sch, x, y)

        # Pull-up resistor (above the junction, vertical orientation)
        self.r_pull = sch.add_symbol(
            resistor_symbol, x, y - 15, f"R{ref_start}", pullup_value, rotation=90
        )

        # Filter capacitor (below the junction, vertical orientation)
        self.c_filt = sch.add_symbol(
            capacitor_symbol, x, y + 15, f"C{ref_start}", filter_cap, rotation=90
        )

        self.components = {"R_pull": self.r_pull, "C_filt": self.c_filt}

        # Wire pull-up to filter cap at the junction node.  Both
        # symbols are vertical: R pin 2 (bottom) meets C pin 1 (top).
        r_out = self.r_pull.pin_position("2")
        c_in = self.c_filt.pin_position("1")
        sch.add_wire(r_out, c_in)

        # Define ports.  SIGNAL_IN and SIGNAL_OUT are the same
        # electrical node (the pull-up / cap junction); naming them
        # separately documents the data-flow direction at the call
        # site and lets callers route the connector pin to SIGNAL_IN
        # and the MCU label to SIGNAL_OUT without ambiguity.
        self.ports = {
            "VCC": self.r_pull.pin_position("1"),
            "SIGNAL_IN": r_out,
            "SIGNAL_OUT": r_out,
            "GND": self.c_filt.pin_position("2"),
        }


class ThermistorSense(CircuitBlock):
    """
    Thermistor temperature sensing circuit.

    Schematic:
        VCC ──[R_bias]──┬── VOUT (to ADC)
                        │
                      [NTC]
                        │
                       GND

    Ports:
        - VCC: Reference voltage
        - VOUT: Analog output (to ADC)
        - GND: Ground reference
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        ntc_value: str = "10k",
        bias_value: str = "10k",
        ref_start: int = 1,
        resistor_symbol: str = "Device:R",
        thermistor_symbol: str = "Device:Thermistor_NTC",
    ):
        """
        Create a thermistor sensing circuit.

        Args:
            sch: Schematic to add to
            x: X coordinate
            y: Y coordinate of junction point
            ntc_value: NTC thermistor nominal resistance
            bias_value: Bias resistor value
            ref_start: Starting reference number
            resistor_symbol: KiCad symbol for resistors
            thermistor_symbol: KiCad symbol for thermistor
        """
        super().__init__(sch, x, y)

        # Place bias resistor (top)
        self.r_bias = sch.add_symbol(
            resistor_symbol, x, y - 15, f"R{ref_start}", bias_value, rotation=90
        )

        # Place thermistor (bottom)
        self.ntc = sch.add_symbol(
            thermistor_symbol, x, y + 15, f"TH{ref_start}", ntc_value, rotation=90
        )

        self.components = {"R_bias": self.r_bias, "NTC": self.ntc}

        # Wire bias resistor to thermistor
        r_out = self.r_bias.pin_position("2")
        ntc_in = self.ntc.pin_position("1")
        sch.add_wire(r_out, ntc_in)

        # Define ports
        self.ports = {
            "VCC": self.r_bias.pin_position("1"),
            "VOUT": r_out,  # Junction point
            "GND": self.ntc.pin_position("2"),
        }


# Factory functions


def create_adc_filter(
    sch: "Schematic",
    x: float,
    y: float,
    cutoff_hz: float = 10000,
    order: int = 1,
) -> ADCInputFilterBlock:
    """Create an ADC input anti-aliasing filter."""
    return ADCInputFilterBlock(sch, x, y, cutoff_hz=cutoff_hz, order=order)


def create_opamp_buffer(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "U1",
) -> OpAmpBlock:
    """Create a unity-gain buffer (voltage follower)."""
    return OpAmpBlock(sch, x, y, topology="buffer", gain=1.0, ref=ref)


def create_opamp_gain(
    sch: "Schematic",
    x: float,
    y: float,
    gain: float = 10.0,
    ref: str = "U1",
) -> OpAmpBlock:
    """Create a non-inverting amplifier with specified gain."""
    return OpAmpBlock(sch, x, y, topology="non_inverting", gain=gain, ref=ref)


def create_voltage_sense(
    sch: "Schematic",
    x: float,
    y: float,
    max_voltage: float = 36.0,
    adc_ref: float = 3.3,
) -> VoltageDividerSense:
    """Create a voltage divider for sensing higher voltages."""
    ratio = max_voltage / adc_ref
    return VoltageDividerSense(sch, x, y, ratio=ratio)


def create_temperature_sense(
    sch: "Schematic",
    x: float,
    y: float,
    ref_start: int = 1,
) -> ThermistorSense:
    """Create a thermistor temperature sensing circuit."""
    return ThermistorSense(sch, x, y, ref_start=ref_start)


def create_hall_sensor_input(
    sch: "Schematic",
    x: float,
    y: float,
    *,
    pullup: str = "10k",
    filter_cap: str = "10nF",
    ref_start: int = 1,
) -> HallSensorInput:
    """
    Create a filtered Hall-sensor / open-collector GPIO input.

    Defaults (10kΩ pull-up + 10nF cap to GND) implement the canonical
    noise-immune Hall input topology recommended for typical 3-phase
    BLDC commutation sensors.  Caller is expected to drive the
    returned block's ports via ``sch.add_label(...)`` / ``sch.add_wire(...)``
    in the same way as the sibling :class:`ThermistorSense` /
    :func:`create_temperature_sense` factory.

    Args:
        sch: Schematic to add to
        x: X coordinate of the junction node
        y: Y coordinate of the junction node
        pullup: Pull-up resistor value (keyword-only)
        filter_cap: Filter capacitor value (keyword-only)
        ref_start: Starting reference number for R/C designators
            (keyword-only).  The block consumes one R and one C ref;
            for multi-channel use, increment ``ref_start`` per call.
    """
    return HallSensorInput(
        sch,
        x,
        y,
        pullup_value=pullup,
        filter_cap=filter_cap,
        ref_start=ref_start,
    )


class OvercurrentComparator(CircuitBlock):
    """Open-collector comparator + threshold divider + pull-up for hardware OC trip.

    Used in motor / load-switch protection circuits where the firmware
    monitoring loop is too slow to respond to a fault: a fast analog
    comparator (LM393, open-collector output) watches a current-sense
    voltage and trips an IRQ line the moment the sense voltage crosses
    a programmed threshold.  Because the LM393 output is open-collector
    it can also be DIO-OR'd with other failsafe lines without needing
    extra logic.

    Topology:

                              VCC (+3.3V)
                               │
                              [R_PULLUP]
                               │
        SHUNT_VOLTAGE ───[+]   │
                       (LM393)─┴─── IRQ_OC (open-collector output)
        V_THRESHOLD ────[-]
            ▲
            │
            ├──[R_TH_HI]── VCC
            │
            └──[R_TH_LO]── GND

    The threshold divider sets the trip point: when SHUNT_VOLTAGE
    rises above V_THRESHOLD, the comparator output is released (high-
    Z, pulled high by R_PULLUP) → IRQ_OC asserts.

    Ports:
        - SHUNT_VOLTAGE: Positive comparator input — wire to the
          current-sense amplifier output (e.g., INA180A3 OUT pin).
        - V_THRESHOLD: Negative input — labelled, but already wired
          to the internal divider; exposed for inspection / probing.
        - IRQ_OUTPUT: Open-collector comparator output (pulled up
          to VCC via R_PULLUP).
        - VCC: Comparator supply rail.
        - GND: Comparator ground / divider return.

    Example:
        from kicad_tools.schematic.blocks import OvercurrentComparator

        oc = OvercurrentComparator(
            sch, x=200, y=80,
            ref="U6",
            shunt_voltage_node=ina180_out_pos,
            threshold_value_v=2.0,
            irq_output_pin="OC_TRIP",
        )
        sch.add_wire(oc.port("VCC"), (oc.port("VCC")[0], RAIL_3V3))
        sch.add_wire(oc.port("GND"), (oc.port("GND")[0], RAIL_GND))
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        ref: str = "U6",
        shunt_voltage_node: tuple[float, float] | None = None,
        threshold_value_v: float = 2.0,
        irq_output_pin: str | None = None,
        vcc_voltage: float = 3.3,
        comparator_symbol: str = "Comparator:LM393",
        comparator_footprint: str = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
        resistor_symbol: str = "Device:R",
        pullup_value: str = "10k",
        ref_r_start: int = 1,
        r_ref_prefix: str = "R",
    ):
        """Create an overcurrent comparator block.

        Args:
            sch: Schematic to add to.
            x: X coordinate of the comparator center.
            y: Y coordinate of the comparator center.
            ref: Reference designator for the comparator IC.
            shunt_voltage_node: Optional position tuple of the upstream
                sense amplifier's output pin.  Informational only —
                the caller is responsible for wiring SHUNT_VOLTAGE to
                this node.
            threshold_value_v: Trip threshold in volts at the comparator
                input.  The block computes R_TH_HI / R_TH_LO values to
                achieve this threshold given ``vcc_voltage`` and the
                10 kΩ baseline lower resistor.
            irq_output_pin: Optional net name for the open-collector
                output (e.g., ``"OC_TRIP"``).  When provided a label
                is emitted at the comparator OUT pin.
            vcc_voltage: VCC rail voltage used to size the threshold
                divider.  Default 3.3 V.
            comparator_symbol: KiCad symbol for the comparator
                (default ``Comparator:LM393``).
            comparator_footprint: Footprint for the IC.
            resistor_symbol: KiCad symbol for resistors.
            pullup_value: Open-collector output pull-up resistor.
            ref_r_start: Starting reference number for resistors.
            r_ref_prefix: Reference prefix for resistors.
        """
        super().__init__(sch, x, y)

        # Place the comparator IC.  LM393 is a multi-unit symbol:
        #   * unit 1 = comparator channel A (pins 1=OUT, 2=IN-, 3=IN+)
        #   * unit 2 = comparator channel B (pins 5=IN+, 6=IN-, 7=OUT)
        #   * unit 3 = power pins (4=V-, 8=V+)
        # If we only placed unit 1 then ``pin_position("4")`` and
        # ``pin_position("8")`` would silently return phantom positions
        # derived from the unit-1 outline (the LM393 library file stores
        # pin 4 at (-2.54, -7.62) inside its ``_3_1`` sub-symbol -- that
        # offset is meaningless when applied to a unit-1 placement and
        # leads to wires terminating in empty space (issue #3346).
        #
        # Place all three units; each ``SymbolInstance`` resolves
        # ``pin_position`` against its own unit's outline.  We then build
        # an internal pin-number -> instance map so the rest of this
        # block (and downstream callers) can keep calling
        # ``pin_position("4")`` and get the correct unit-3 coordinates.
        # ``SymbolInstance`` is only imported under ``TYPE_CHECKING`` so the
        # annotation must remain a string at runtime.  ``ruff`` flags this
        # as ``UP037`` but the suggested fix breaks Python <3.10 evaluation.
        self._unit_instances: "dict[int, SymbolInstance]" = {}  # noqa: UP037

        primary_unit = 1
        self.comparator = sch.add_symbol(
            comparator_symbol,
            x,
            y,
            ref,
            "LM393",
            footprint=comparator_footprint,
            unit=primary_unit,
        )
        self._unit_instances[primary_unit] = self.comparator
        self.components = {"U_CMP": self.comparator}

        # Discover the symbol's other units (if any) and place them.
        # We read this from the parsed ``SymbolDef``; falling back
        # gracefully when the schematic mock used in unit tests does not
        # expose a real ``SymbolDef`` (a bare ``Mock().symbol_def`` is
        # itself a Mock object and ``unit_count()`` returns a Mock too,
        # so we explicitly type-check the result before iterating).
        sym_def = getattr(self.comparator, "symbol_def", None)
        raw_unit_count = sym_def.unit_count() if sym_def is not None else 1
        unit_count = raw_unit_count if isinstance(raw_unit_count, int) else 1

        # Offsets for power and channel-B placements.  These match the
        # hand-tuned coordinates that PR #3345 used for softstart rev B
        # (which is now the canonical placement for an LM393 block):
        # power above the divider, channel B further right with all
        # three pins marked NC.
        unit_offsets = {
            2: (x + 50, y - 40),  # channel B (NC pins)
            3: (x + 25, y),  # power (V+, V-)
        }
        for u in range(2, unit_count + 1):
            if u not in unit_offsets:
                continue
            ux, uy = unit_offsets[u]
            inst = sch.add_symbol(
                comparator_symbol,
                ux,
                uy,
                ref,
                "LM393",
                footprint=comparator_footprint,
                unit=u,
            )
            self._unit_instances[u] = inst
            self.components[f"U_CMP_U{u}"] = inst

        # Channel B (unit 2) pins are unused on this block; mark them as
        # no-connect to keep ERC quiet.  Only attempt this for instances
        # that expose ``pin_position`` (real ``SymbolInstance``); mocked
        # schematics may not implement ``add_no_connect``.
        if 2 in self._unit_instances and hasattr(sch, "add_no_connect"):
            for pin_num in ("5", "6", "7"):
                with contextlib.suppress(Exception):
                    pos = self._unit_pin_position(pin_num)
                    sch.add_no_connect(pos[0], pos[1])

        # Threshold divider: R_TH_HI from VCC, R_TH_LO to GND, junction = V_THRESHOLD.
        # ratio = vcc/threshold = (R_TH_HI + R_TH_LO) / R_TH_LO
        # Pick R_TH_LO = 10k, then R_TH_HI = R_TH_LO * (ratio - 1).
        r_lo_ohms = 10000.0
        ratio = vcc_voltage / max(threshold_value_v, 1e-6)
        r_hi_ohms = r_lo_ohms * (ratio - 1.0)

        # Place R_TH_HI (top of divider) and R_TH_LO (bottom of divider)
        r_th_hi_ref = f"{r_ref_prefix}{ref_r_start}"
        r_th_lo_ref = f"{r_ref_prefix}{ref_r_start + 1}"
        r_pullup_ref = f"{r_ref_prefix}{ref_r_start + 2}"

        div_x = x - 25
        self.r_th_hi = sch.add_symbol(
            resistor_symbol,
            div_x,
            y - 15,
            r_th_hi_ref,
            self._format_resistance(r_hi_ohms),
            rotation=90,
        )
        self.r_th_lo = sch.add_symbol(
            resistor_symbol,
            div_x,
            y + 15,
            r_th_lo_ref,
            self._format_resistance(r_lo_ohms),
            rotation=90,
        )
        self.r_pullup = sch.add_symbol(
            resistor_symbol,
            x + 25,
            y - 15,
            r_pullup_ref,
            pullup_value,
            rotation=90,
        )
        self.components["R_TH_HI"] = self.r_th_hi
        self.components["R_TH_LO"] = self.r_th_lo
        self.components["R_PULLUP"] = self.r_pullup

        # Threshold divider junction
        r_th_hi_out = self.r_th_hi.pin_position("2")
        r_th_lo_in = self.r_th_lo.pin_position("1")
        sch.add_wire(r_th_hi_out, r_th_lo_in)
        threshold_node = (r_th_hi_out[0], (r_th_hi_out[1] + r_th_lo_in[1]) / 2)
        sch.add_junction(threshold_node[0], threshold_node[1])

        # Pull-up junction at top of R_PULLUP (this is the comparator output
        # node pulled toward VCC)
        r_pullup_top = self.r_pullup.pin_position("1")
        r_pullup_bot = self.r_pullup.pin_position("2")

        # Comparator pin layout (LM393 multi-unit): pins 1=OUT_A, 2=IN-_A,
        # 3=IN+_A live on unit 1; pins 4=V-, 8=V+ live on unit 3; pins
        # 5-7 live on unit 2 (channel B, unused here).  ``_unit_pin_position``
        # routes each lookup to the correct ``SymbolInstance`` so that
        # pin 4 / pin 8 resolve against the unit-3 outline -- the unit-1
        # fallback positions used by the previous implementation produced
        # phantom coordinates that landed in empty space (issue #3346).
        try:
            cmp_out = self._unit_pin_position("1")
            cmp_in_neg = self._unit_pin_position("2")
            cmp_in_pos = self._unit_pin_position("3")
            cmp_gnd = self._unit_pin_position("4")
            cmp_vcc = self._unit_pin_position("8")
        except Exception:
            # Fall back to symbol-relative positions if the symbol layout
            # differs (e.g. mocked schematic in unit tests that doesn't
            # mirror the LM393 unit topology).
            cmp_out = (x + 10, y - 5)
            cmp_in_neg = (x - 10, y - 5)
            cmp_in_pos = (x - 10, y + 5)
            cmp_gnd = (x, y + 15)
            cmp_vcc = (x, y - 15)

        # Wire threshold node → comparator IN- (negative input)
        sch.add_wire(threshold_node, (cmp_in_neg[0], threshold_node[1]))
        sch.add_wire((cmp_in_neg[0], threshold_node[1]), cmp_in_neg)

        # Wire comparator OUT → R_PULLUP bottom (the pull-up's GND-side
        # pin actually ties to OUT for open-collector; the top of
        # R_PULLUP ties to VCC).
        sch.add_wire(cmp_out, (r_pullup_bot[0], cmp_out[1]))
        sch.add_wire((r_pullup_bot[0], cmp_out[1]), r_pullup_bot)

        # Optional IRQ label at the open-collector output node
        if irq_output_pin is not None:
            STUB = 2.54
            stub_end = (cmp_out[0] + STUB, cmp_out[1])
            sch.add_wire(cmp_out, stub_end)
            sch.add_label(irq_output_pin, stub_end[0], stub_end[1])

        self.ports = {
            "SHUNT_VOLTAGE": cmp_in_pos,
            "V_THRESHOLD": threshold_node,
            "IRQ_OUTPUT": cmp_out,
            "VCC": cmp_vcc,
            "GND": cmp_gnd,
            # Convenience aliases for caller's rail tie-up
            "DIV_VCC": self.r_th_hi.pin_position("1"),
            "DIV_GND": self.r_th_lo.pin_position("2"),
            "PULLUP_VCC": r_pullup_top,
        }

        # Metadata
        self.threshold_value_v = threshold_value_v
        self.vcc_voltage = vcc_voltage
        self.shunt_voltage_node = shunt_voltage_node
        self.irq_output_pin = irq_output_pin

    def _unit_pin_position(self, pin_number: str) -> tuple[float, float]:
        """Return the absolute position of an LM393 pin, picking the right unit.

        Walks the comparator's parsed :class:`SymbolDef` to discover
        which unit owns ``pin_number``, then delegates to the matching
        ``SymbolInstance`` placed in :class:`__init__`.  Falls back to
        the primary (unit 1) instance if the symbol definition is not
        introspectable (unit-test mock) or the unit was never placed.

        This is what fixes the multi-unit phantom-position bug
        (issue #3346): asking the primary unit-1 instance for pin 4
        used to return a position derived from unit 1's outline plus
        unit 3's library pin offset -- two unrelated coordinate
        systems -- which landed in empty space.  Now pin 4 is looked
        up on the unit-3 instance and the returned point sits on a
        wire-able grid node.
        """
        primary = self._unit_instances.get(1) or self.comparator
        sym_def = getattr(primary, "symbol_def", None)
        if sym_def is not None and hasattr(sym_def, "get_pin_unit"):
            unit = sym_def.get_pin_unit(pin_number)
            inst = self._unit_instances.get(unit, primary)
        else:
            inst = primary
        return inst.pin_position(pin_number)

    def pin_position(self, pin_number: str) -> tuple[float, float]:
        """Public helper mirroring :meth:`SymbolInstance.pin_position`.

        Exposes the multi-unit-aware lookup so callers that previously
        held a reference to ``block.comparator`` and called
        ``comparator.pin_position(num)`` can switch to
        ``block.pin_position(num)`` and get correct results for pins on
        any unit (issue #3346).
        """
        return self._unit_pin_position(pin_number)

    @staticmethod
    def _format_resistance(r_ohms: float) -> str:
        """Format resistance value as string (R/k/M conventions)."""
        if r_ohms >= 1e6:
            return f"{r_ohms / 1e6:.1f}M"
        if r_ohms >= 1e3:
            return f"{r_ohms / 1e3:.1f}k"
        return f"{r_ohms:.0f}R"
