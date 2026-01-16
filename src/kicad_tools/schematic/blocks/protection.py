"""Protection circuit blocks: ESD, overcurrent, reverse polarity."""

from typing import TYPE_CHECKING, Literal

from .base import CircuitBlock

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic


class ESDProtectionBlock(CircuitBlock):
    """
    ESD protection for I/O lines using TVS diodes.

    Schematic (single line):
        IN ──┬── OUT
             │
           [TVS]
             │
            GND

    Schematic (multi-channel array):
        IN1 ──┬── OUT1
              │
        IN2 ──┼── OUT2
              │
            [U_ESD]
              │
             GND

    Ports:
        - IN1, IN2, ...: Unprotected inputs
        - OUT1, OUT2, ...: Protected outputs
        - GND: Ground reference
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        channels: int = 1,
        ref: str = "D1",
        tvs_symbol: str = "Device:D_TVS",
        esd_array_symbol: str = "Power_Protection:USBLC6-2SC6",
    ):
        """
        Create an ESD protection block.

        Args:
            sch: Schematic to add to
            x: X coordinate
            y: Y coordinate
            channels: Number of channels to protect (1-8)
            ref: Reference designator
            tvs_symbol: KiCad symbol for single TVS diode
            esd_array_symbol: KiCad symbol for multi-channel ESD array
        """
        super().__init__(sch, x, y)

        self.components = {}
        self.ports = {}

        if channels == 1:
            # Single TVS diode
            tvs = sch.add_symbol(tvs_symbol, x, y + 10, ref, "TVS", rotation=90)
            self.components["TVS"] = tvs

            # Define ports
            self.ports = {
                "IN": (x - 10, y),
                "OUT": (x + 10, y),
                "GND": tvs.pin_position("A"),  # Anode to ground
            }

            # Wire signal path
            sch.add_wire(self.ports["IN"], (x, y))
            sch.add_wire((x, y), self.ports["OUT"])

            # Wire TVS cathode to signal path
            cathode = tvs.pin_position("K")
            sch.add_wire(cathode, (x, y))
        else:
            # Multi-channel ESD array
            esd = sch.add_symbol(esd_array_symbol, x, y, ref, "")
            self.components["U_ESD"] = esd

            # Define ports based on typical ESD array pinout
            spacing = 10
            for i in range(min(channels, 4)):  # Most arrays have 2-4 channels
                self.ports[f"IN{i + 1}"] = (x - 15, y - spacing * (i - 1))
                self.ports[f"OUT{i + 1}"] = (x + 15, y - spacing * (i - 1))

            self.ports["GND"] = (x, y + 15)
            self.ports["VCC"] = (x, y - 15)


class FuseBlock(CircuitBlock):
    """
    Fuse protection circuit.

    Schematic:
        IN ──[F1]── OUT

    Ports:
        - IN: Unprotected input
        - OUT: Protected output
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        current_rating: str = "1A",
        ref: str = "F1",
        fuse_symbol: str = "Device:Fuse",
    ):
        """
        Create a fuse protection block.

        Args:
            sch: Schematic to add to
            x: X coordinate
            y: Y coordinate
            current_rating: Fuse current rating
            ref: Reference designator
            fuse_symbol: KiCad symbol for fuse
        """
        super().__init__(sch, x, y)

        # Place fuse
        self.fuse = sch.add_symbol(fuse_symbol, x, y, ref, current_rating)
        self.components = {"F": self.fuse}

        # Define ports
        self.ports = {
            "IN": self.fuse.pin_position("1"),
            "OUT": self.fuse.pin_position("2"),
        }


class PTCResetBlock(CircuitBlock):
    """
    Resettable PTC fuse (polyfuse) protection.

    Schematic:
        IN ──[PTC]── OUT

    Ports:
        - IN: Unprotected input
        - OUT: Protected output
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        hold_current: str = "500mA",
        ref: str = "PTC1",
        ptc_symbol: str = "Device:Polyfuse",
    ):
        """
        Create a resettable PTC fuse block.

        Args:
            sch: Schematic to add to
            x: X coordinate
            y: Y coordinate
            hold_current: Hold current rating
            ref: Reference designator
            ptc_symbol: KiCad symbol for PTC
        """
        super().__init__(sch, x, y)

        # Place PTC
        self.ptc = sch.add_symbol(ptc_symbol, x, y, ref, hold_current)
        self.components = {"PTC": self.ptc}

        # Define ports
        self.ports = {
            "IN": self.ptc.pin_position("1"),
            "OUT": self.ptc.pin_position("2"),
        }


class ReversePolarityBlock(CircuitBlock):
    """
    Reverse polarity protection.

    Schematic (P-FET):
        VIN ──[Q1 P-FET]── VOUT
                 │
                 G
                 │
               [R_gate]
                 │
                GND

    Schematic (Diode):
        VIN ──[D1]── VOUT

    Ports:
        - VIN: Input voltage (unprotected)
        - VOUT: Output voltage (protected)
        - GND: Ground reference (for P-FET topology)
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        topology: Literal["diode", "pfet"] = "pfet",
        ref: str = "Q1",
        mosfet_symbol: str = "Device:Q_PMOS_GSD",
        diode_symbol: str = "Device:D_Schottky",
        resistor_symbol: str = "Device:R",
    ):
        """
        Create a reverse polarity protection block.

        Args:
            sch: Schematic to add to
            x: X coordinate
            y: Y coordinate
            topology: Protection topology ("diode" or "pfet")
            ref: Reference designator
            mosfet_symbol: KiCad symbol for P-FET
            diode_symbol: KiCad symbol for diode
            resistor_symbol: KiCad symbol for resistor
        """
        super().__init__(sch, x, y)

        self.components = {}

        if topology == "diode":
            # Simple series diode
            diode = sch.add_symbol(diode_symbol, x, y, ref if ref[0] == "D" else "D1", "")
            self.components["D"] = diode

            self.ports = {
                "VIN": diode.pin_position("A"),
                "VOUT": diode.pin_position("K"),
            }
        else:
            # P-FET topology (lower voltage drop)
            # Place P-FET
            pfet = sch.add_symbol(mosfet_symbol, x, y, ref, "", rotation=90)
            self.components["Q"] = pfet

            # Place gate resistor
            r_num = ref[1:] if ref[1:].isdigit() else "1"
            r_gate = sch.add_symbol(resistor_symbol, x, y + 15, f"R{r_num}", "10k", rotation=90)
            self.components["R_gate"] = r_gate

            # Wire gate to resistor
            gate = pfet.pin_position("G")
            r_top = r_gate.pin_position("1")
            sch.add_wire(gate, r_top)

            self.ports = {
                "VIN": pfet.pin_position("S"),  # Source (input)
                "VOUT": pfet.pin_position("D"),  # Drain (output)
                "GND": r_gate.pin_position("2"),
            }


class OvervoltageClampBlock(CircuitBlock):
    """
    Overvoltage protection using Zener clamp.

    Schematic:
        VIN ──[R_limit]──┬── VOUT (clamped)
                         │
                       [Zener]
                         │
                        GND

    Ports:
        - VIN: Input voltage
        - VOUT: Clamped output voltage
        - GND: Ground reference
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        clamp_voltage: float = 5.1,
        ref_start: int = 1,
        resistor_symbol: str = "Device:R",
        zener_symbol: str = "Device:D_Zener",
    ):
        """
        Create an overvoltage clamp block.

        Args:
            sch: Schematic to add to
            x: X coordinate
            y: Y coordinate (junction point)
            clamp_voltage: Zener voltage in Volts
            ref_start: Starting reference number
            resistor_symbol: KiCad symbol for resistor
            zener_symbol: KiCad symbol for Zener diode
        """
        super().__init__(sch, x, y)

        # Format zener value
        zener_value = f"{clamp_voltage}V"

        # Place series resistor
        self.r_limit = sch.add_symbol(resistor_symbol, x - 15, y, f"R{ref_start}", "100R")

        # Place Zener diode
        self.zener = sch.add_symbol(
            zener_symbol, x, y + 10, f"D{ref_start}", zener_value, rotation=90
        )

        self.components = {"R": self.r_limit, "D": self.zener}

        # Wire resistor to junction
        r_out = self.r_limit.pin_position("2")
        sch.add_wire(r_out, (x, y))

        # Wire Zener cathode to junction
        z_cathode = self.zener.pin_position("K")
        sch.add_wire(z_cathode, (x, y))

        # Define ports
        self.ports = {
            "VIN": self.r_limit.pin_position("1"),
            "VOUT": (x + 10, y),  # Junction point
            "GND": self.zener.pin_position("A"),
        }

        # Wire junction to output
        sch.add_wire((x, y), self.ports["VOUT"])


class ThermalCutoffBlock(CircuitBlock):
    """
    Thermal cutoff protection.

    Schematic:
        VIN ──[THERMAL_CUTOFF]── VOUT

    Ports:
        - VIN: Input (before thermal protection)
        - VOUT: Output (protected by thermal cutoff)
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        temp_rating: str = "85C",
        ref: str = "TH1",
        thermal_symbol: str = "Device:Thermal_Cutoff",
    ):
        """
        Create a thermal cutoff block.

        Args:
            sch: Schematic to add to
            x: X coordinate
            y: Y coordinate
            temp_rating: Temperature rating string
            ref: Reference designator
            thermal_symbol: KiCad symbol for thermal cutoff
        """
        super().__init__(sch, x, y)

        # Place thermal cutoff
        self.thermal = sch.add_symbol(thermal_symbol, x, y, ref, temp_rating)
        self.components = {"TH": self.thermal}

        # Define ports
        self.ports = {
            "VIN": self.thermal.pin_position("1"),
            "VOUT": self.thermal.pin_position("2"),
        }


# Factory functions


def create_esd_protection(
    sch: "Schematic",
    x: float,
    y: float,
    channels: int = 1,
    ref: str = "D1",
) -> ESDProtectionBlock:
    """Create an ESD protection block."""
    return ESDProtectionBlock(sch, x, y, channels=channels, ref=ref)


def create_fuse(
    sch: "Schematic",
    x: float,
    y: float,
    current: str = "1A",
    ref: str = "F1",
) -> FuseBlock:
    """Create a fuse protection block."""
    return FuseBlock(sch, x, y, current_rating=current, ref=ref)


def create_ptc_fuse(
    sch: "Schematic",
    x: float,
    y: float,
    hold_current: str = "500mA",
    ref: str = "PTC1",
) -> PTCResetBlock:
    """Create a resettable PTC fuse block."""
    return PTCResetBlock(sch, x, y, hold_current=hold_current, ref=ref)


def create_reverse_polarity_pfet(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "Q1",
) -> ReversePolarityBlock:
    """Create a P-FET reverse polarity protection block."""
    return ReversePolarityBlock(sch, x, y, topology="pfet", ref=ref)


def create_reverse_polarity_diode(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "D1",
) -> ReversePolarityBlock:
    """Create a diode reverse polarity protection block."""
    return ReversePolarityBlock(sch, x, y, topology="diode", ref=ref)


def create_overvoltage_clamp(
    sch: "Schematic",
    x: float,
    y: float,
    clamp_voltage: float = 5.1,
    ref_start: int = 1,
) -> OvervoltageClampBlock:
    """Create an overvoltage clamp block."""
    return OvervoltageClampBlock(sch, x, y, clamp_voltage=clamp_voltage, ref_start=ref_start)


def create_thermal_cutoff(
    sch: "Schematic",
    x: float,
    y: float,
    temp: str = "85C",
    ref: str = "TH1",
) -> ThermalCutoffBlock:
    """Create a thermal cutoff block."""
    return ThermalCutoffBlock(sch, x, y, temp_rating=temp, ref=ref)
