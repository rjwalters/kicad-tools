"""Protection circuit blocks: ESD, overcurrent, reverse polarity."""

from typing import TYPE_CHECKING, Literal

from ._pins import diode_pin_position
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
                # ``Device:D_TVS`` is bidirectional (A1/A2); the helper maps
                # anode/cathode onto pins 2/1 when the names are absent.
                "GND": diode_pin_position(tvs, "A"),  # Anode to ground
            }

            # Wire signal path
            sch.add_wire(self.ports["IN"], (x, y))
            sch.add_wire((x, y), self.ports["OUT"])

            # Wire TVS cathode to signal path
            cathode = diode_pin_position(tvs, "K")
            sch.add_wire(cathode, (x, y))
        else:
            # Multi-channel ESD array (e.g. USBLC6-2SC6): each channel is a
            # through-line -- an I/O pin on the left of the symbol and its
            # twin on the right -- so the array sits in the signal path.
            esd = sch.add_symbol(esd_array_symbol, x, y, ref, "")
            self.components["U_ESD"] = esd

            lines = self._io_lines(esd)
            if channels > len(lines):
                raise ValueError(
                    f"{esd_array_symbol} provides {len(lines)} ESD channel(s); {channels} requested"
                )
            for i, (left, right) in enumerate(lines[:channels], start=1):
                self.ports[f"IN{i}"] = esd.pin_position(left)
                self.ports[f"OUT{i}"] = esd.pin_position(right)

            self.ports["GND"] = esd.pin_position(self._rail_pin(esd, "GND"))
            self.ports["VCC"] = esd.pin_position(self._rail_pin(esd, "VBUS", "VCC", "VDD"))

    @staticmethod
    def _io_lines(esd) -> list[tuple[str, str]]:
        """Pair each ESD array I/O pin with its same-named twin.

        Returns ``(left_pin_number, right_pin_number)`` per channel, ordered
        top to bottom; ``USBLC6-2SC6`` has two such channels (pins 1/6, 3/4).
        """
        groups: dict[str, list] = {}
        for pin in esd.symbol_def.pins:
            if pin.name.upper().startswith("I/O"):
                groups.setdefault(pin.name, []).append(pin)
        lines = []
        for pins in groups.values():
            if len(pins) == 2:
                left, right = sorted(pins, key=lambda p: esd.pin_position(p.number)[0])
                lines.append((left.number, right.number))
        lines.sort(key=lambda pair: esd.pin_position(pair[0])[1])
        return lines

    @staticmethod
    def _rail_pin(esd, *names: str) -> str:
        """Number of the first pin whose name matches one of ``names``."""
        for name in names:
            for pin in esd.symbol_def.pins:
                if pin.name.upper() == name:
                    return pin.number
        raise ValueError(f"ESD array symbol has none of the pins {names}")


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
        mosfet_symbol: str = "Device:Q_PMOS",
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
            mosfet_symbol: KiCad symbol for P-FET (pins named G/S/D).
                KiCad 10 replaced the per-pinout ``Q_PMOS_GSD`` variants with
                the single generic ``Device:Q_PMOS`` (issue #7399).
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
        thermal_symbol: str = "Device:Fuse",
    ):
        """
        Create a thermal cutoff block.

        Args:
            sch: Schematic to add to
            x: X coordinate
            y: Y coordinate
            temp_rating: Temperature rating string
            ref: Reference designator
            thermal_symbol: KiCad symbol for thermal cutoff.  KiCad 10 ships
                no thermal-cutoff (TCO) symbol, so the default is the generic
                two-terminal ``Device:Fuse`` -- a TCO is a one-shot thermal
                fuse, and the value field carries the temperature rating
                (issue #7399).  Pass a project symbol to override.
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
