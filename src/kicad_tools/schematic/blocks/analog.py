"""Analog circuit blocks: op-amps, ADC input filters, sensor interfaces."""

from typing import TYPE_CHECKING, Literal

from .base import CircuitBlock

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic


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
