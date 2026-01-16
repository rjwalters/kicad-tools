"""Motor control circuit blocks: half-bridges, gate drivers, current sensing."""

from typing import TYPE_CHECKING

from .base import CircuitBlock

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic


class HalfBridge(CircuitBlock):
    """
    Half-bridge circuit with high-side and low-side N-channel MOSFETs.

    A half-bridge is the fundamental building block for motor drives,
    DC-DC converters, and other power electronics applications.

    Schematic:
        VIN ────────┬────────
                    │
                   [Q_HS]  (High-side MOSFET)
                    │
                    ├─── GATE_HS
                    │
        VOUT ───────┼────────  (Phase output)
                    │
                   [Q_LS]  (Low-side MOSFET)
                    │
                    ├─── GATE_LS
                    │
        GND ────────┴────────

    With bootstrap (optional):
        VIN ────────┬────────
                    │
                   [D_BOOT]──[C_BOOT]──VBOOT
                    │
                   [Q_HS]
                    │
                    ...

    Ports:
        - VIN: High-side power supply (motor voltage)
        - VOUT: Phase output (connects to motor winding)
        - GND: Ground reference
        - GATE_HS: High-side gate drive input
        - GATE_LS: Low-side gate drive input
        - VBOOT: Bootstrap voltage (if bootstrap enabled)

    Thermal Metadata:
        MOSFETs are tagged with thermal metadata for ThermalAnalyzer integration.

    Example:
        from kicad_tools.schematic.blocks import HalfBridge

        hb = HalfBridge(
            sch, x=100, y=100,
            ref_prefix="Q",
            mosfet_value="IRLZ44N",
            bootstrap_cap="100nF",
        )
        hb.connect_to_rails(vin_rail_y=30, gnd_rail_y=200)
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        ref_start: int = 1,
        ref_prefix: str = "Q",
        mosfet_value: str = "IRLZ44N",
        mosfet_symbol: str = "Device:Q_NMOS_GDS",
        bootstrap_cap: str | None = None,
        bootstrap_diode: str = "1N4148",
        cap_ref_prefix: str = "C",
        diode_ref_prefix: str = "D",
        cap_ref_start: int | None = None,
        diode_ref_start: int | None = None,
        hs_ls_spacing: float = 40,
    ):
        """
        Create a half-bridge circuit.

        Args:
            sch: Schematic to add to
            x: X coordinate of MOSFETs
            y: Y coordinate (center of high-side MOSFET)
            ref_start: Starting reference number for MOSFETs
            ref_prefix: Reference designator prefix for MOSFETs
            mosfet_value: MOSFET part number/value
            mosfet_symbol: KiCad symbol for N-channel MOSFETs
            bootstrap_cap: Bootstrap capacitor value (None to disable)
            bootstrap_diode: Bootstrap diode part number
            cap_ref_prefix: Reference prefix for bootstrap capacitor
            diode_ref_prefix: Reference prefix for bootstrap diode
            cap_ref_start: Starting reference for capacitor (defaults to ref_start)
            diode_ref_start: Starting reference for diode (defaults to ref_start)
            hs_ls_spacing: Vertical spacing between HS and LS MOSFETs
        """
        super().__init__(sch, x, y)
        self.has_bootstrap = bootstrap_cap is not None

        # Default ref numbers if not specified
        if cap_ref_start is None:
            cap_ref_start = ref_start
        if diode_ref_start is None:
            diode_ref_start = ref_start

        # Place high-side MOSFET
        hs_ref = f"{ref_prefix}{ref_start}"
        self.mosfet_hs = sch.add_symbol(
            mosfet_symbol,
            x,
            y,
            hs_ref,
            mosfet_value,
            properties={"Thermal_Rth_JC": "0.5", "Power_Dissipation": "5W"},
        )

        # Place low-side MOSFET
        ls_ref = f"{ref_prefix}{ref_start + 1}"
        ls_y = y + hs_ls_spacing
        self.mosfet_ls = sch.add_symbol(
            mosfet_symbol,
            x,
            ls_y,
            ls_ref,
            mosfet_value,
            properties={"Thermal_Rth_JC": "0.5", "Power_Dissipation": "5W"},
        )

        self.components = {
            "Q_HS": self.mosfet_hs,
            "Q_LS": self.mosfet_ls,
        }

        # Get MOSFET pin positions
        hs_drain = self.mosfet_hs.pin_position("D")
        hs_source = self.mosfet_hs.pin_position("S")
        hs_gate = self.mosfet_hs.pin_position("G")
        ls_drain = self.mosfet_ls.pin_position("D")
        ls_source = self.mosfet_ls.pin_position("S")
        ls_gate = self.mosfet_ls.pin_position("G")

        # Wire HS source to LS drain (phase output node)
        sch.add_wire(hs_source, ls_drain)
        phase_out_pos = (hs_source[0], (hs_source[1] + ls_drain[1]) / 2)

        # Add junction at phase output
        sch.add_junction(phase_out_pos[0], phase_out_pos[1])

        # Bootstrap circuit (optional)
        if self.has_bootstrap:
            # Bootstrap diode from VIN to VBOOT
            boot_diode_ref = f"{diode_ref_prefix}{diode_ref_start}"
            boot_diode_x = x - 20
            boot_diode_y = y - 15
            self.bootstrap_diode = sch.add_symbol(
                "Device:D",
                boot_diode_x,
                boot_diode_y,
                boot_diode_ref,
                bootstrap_diode,
            )
            self.components["D_BOOT"] = self.bootstrap_diode

            # Bootstrap capacitor from VBOOT to phase output
            boot_cap_ref = f"{cap_ref_prefix}{cap_ref_start}"
            boot_cap_x = x - 20
            boot_cap_y = y
            self.bootstrap_cap = sch.add_symbol(
                "Device:C",
                boot_cap_x,
                boot_cap_y,
                boot_cap_ref,
                bootstrap_cap,
            )
            self.components["C_BOOT"] = self.bootstrap_cap

            # Wire bootstrap circuit
            diode_cathode = self.bootstrap_diode.pin_position("K")
            cap_pin1 = self.bootstrap_cap.pin_position("1")
            cap_pin2 = self.bootstrap_cap.pin_position("2")

            # Diode cathode to cap positive
            sch.add_wire(diode_cathode, cap_pin1)

            # Cap negative to phase output
            sch.add_wire(cap_pin2, (cap_pin2[0], phase_out_pos[1]))
            sch.add_wire((cap_pin2[0], phase_out_pos[1]), phase_out_pos)

            # VBOOT port at diode cathode/cap positive
            vboot_pos = cap_pin1
        else:
            vboot_pos = None

        # Define ports
        self.ports = {
            "VIN": hs_drain,
            "VOUT": phase_out_pos,
            "GND": ls_source,
            "GATE_HS": hs_gate,
            "GATE_LS": ls_gate,
        }

        if self.has_bootstrap:
            self.ports["VBOOT"] = vboot_pos
            # VIN for bootstrap diode anode
            self.ports["VIN_BOOT"] = self.bootstrap_diode.pin_position("A")

        # Store for rail connections
        self._vin_pos = hs_drain
        self._gnd_pos = ls_source

    def connect_to_rails(
        self,
        vin_rail_y: float,
        gnd_rail_y: float,
        add_junctions: bool = True,
    ) -> None:
        """
        Connect half-bridge to power rails.

        Args:
            vin_rail_y: Y coordinate of VIN (motor voltage) rail
            gnd_rail_y: Y coordinate of ground rail
            add_junctions: Whether to add junction markers
        """
        sch = self.schematic

        # Connect HS drain to VIN rail
        vin_pos = self._vin_pos
        sch.add_wire(vin_pos, (vin_pos[0], vin_rail_y))

        # Connect LS source to GND rail
        gnd_pos = self._gnd_pos
        sch.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y))

        if add_junctions:
            sch.add_junction(vin_pos[0], vin_rail_y)
            sch.add_junction(gnd_pos[0], gnd_rail_y)

        # Connect bootstrap diode anode to VIN rail
        if self.has_bootstrap:
            boot_vin = self.ports["VIN_BOOT"]
            sch.add_wire(boot_vin, (boot_vin[0], vin_rail_y))
            if add_junctions:
                sch.add_junction(boot_vin[0], vin_rail_y)


class ThreePhaseInverter(CircuitBlock):
    """
    Three-phase inverter using three half-bridges.

    Commonly used for BLDC motor control, AC motor drives, and
    3-phase power conversion.

    Schematic:
        VIN ────┬────────┬────────┬────────
                │        │        │
              [HB_A]   [HB_B]   [HB_C]
                │        │        │
               U/A      V/B      W/C    (Phase outputs)
                │        │        │
        GND ────┴────────┴────────┴────────

    Ports:
        - VIN: Motor voltage supply
        - GND: Ground reference
        - PHASE_A/U: Phase A output
        - PHASE_B/V: Phase B output
        - PHASE_C/W: Phase C output
        - GATE_HS_A/B/C: High-side gate inputs
        - GATE_LS_A/B/C: Low-side gate inputs

    Example:
        from kicad_tools.schematic.blocks import ThreePhaseInverter

        inverter = ThreePhaseInverter(
            sch, x=100, y=100,
            mosfet_value="IRLZ44N",
            phase_labels=["U", "V", "W"],
        )
        inverter.connect_to_rails(vin_rail_y=30, gnd_rail_y=200)
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        ref_start: int = 1,
        ref_prefix: str = "Q",
        mosfet_value: str = "IRLZ44N",
        mosfet_symbol: str = "Device:Q_NMOS_GDS",
        bootstrap_cap: str | None = None,
        phase_labels: list[str] | None = None,
        phase_spacing: float = 75,
        hs_ls_spacing: float = 40,
    ):
        """
        Create a three-phase inverter.

        Args:
            sch: Schematic to add to
            x: X coordinate of first phase (Phase A)
            y: Y coordinate (center of high-side MOSFETs)
            ref_start: Starting reference number for MOSFETs (Q1-Q6)
            ref_prefix: Reference designator prefix for MOSFETs
            mosfet_value: MOSFET part number/value
            mosfet_symbol: KiCad symbol for N-channel MOSFETs
            bootstrap_cap: Bootstrap capacitor value (None to disable)
            phase_labels: Labels for phases (default: ["A", "B", "C"])
            phase_spacing: Horizontal spacing between phases
            hs_ls_spacing: Vertical spacing between HS and LS MOSFETs
        """
        super().__init__(sch, x, y)

        if phase_labels is None:
            phase_labels = ["A", "B", "C"]

        self.phase_labels = phase_labels
        self.half_bridges: list[HalfBridge] = []

        # Create three half-bridges
        for i, label in enumerate(phase_labels):
            hb_x = x + i * phase_spacing
            hb_ref_start = ref_start + i * 2  # Q1-Q2, Q3-Q4, Q5-Q6

            hb = HalfBridge(
                sch,
                hb_x,
                y,
                ref_start=hb_ref_start,
                ref_prefix=ref_prefix,
                mosfet_value=mosfet_value,
                mosfet_symbol=mosfet_symbol,
                bootstrap_cap=bootstrap_cap,
                cap_ref_start=i + 1 if bootstrap_cap else None,
                diode_ref_start=i + 1 if bootstrap_cap else None,
                hs_ls_spacing=hs_ls_spacing,
            )
            self.half_bridges.append(hb)

            # Add phase output label
            phase_pos = hb.port("VOUT")
            sch.add_label(f"PHASE_{label}", phase_pos[0] + 10, phase_pos[1], rotation=0)

        # Store all components
        self.components = {}
        for i, hb in enumerate(self.half_bridges):
            label = phase_labels[i]
            for name, comp in hb.components.items():
                self.components[f"{name}_{label}"] = comp

        # Define ports
        self.ports = {
            "VIN": self.half_bridges[0].port("VIN"),
            "GND": self.half_bridges[0].port("GND"),
        }

        # Add phase-specific ports
        for i, label in enumerate(phase_labels):
            hb = self.half_bridges[i]
            self.ports[f"PHASE_{label}"] = hb.port("VOUT")
            self.ports[f"GATE_HS_{label}"] = hb.port("GATE_HS")
            self.ports[f"GATE_LS_{label}"] = hb.port("GATE_LS")
            if bootstrap_cap:
                self.ports[f"VBOOT_{label}"] = hb.port("VBOOT")

    def connect_to_rails(
        self,
        vin_rail_y: float,
        gnd_rail_y: float,
        add_junctions: bool = True,
    ) -> None:
        """
        Connect all half-bridges to power rails.

        Args:
            vin_rail_y: Y coordinate of VIN (motor voltage) rail
            gnd_rail_y: Y coordinate of ground rail
            add_junctions: Whether to add junction markers
        """
        for hb in self.half_bridges:
            hb.connect_to_rails(vin_rail_y, gnd_rail_y, add_junctions)


class CurrentSenseShunt(CircuitBlock):
    """
    Low-side current sensing using a shunt resistor.

    Provides current measurement for motor control applications,
    with optional current sense amplifier.

    Schematic (basic):
        IN+ ────┬────
                │
              [R_SHUNT]
                │
        IN- ────┴──── GND

    Schematic (with amplifier):
        IN+ ────┬────────────────
                │        ┌──────┐
              [R_SHUNT]  │ CSA  │──── OUT
                │        └──────┘
        IN- ────┴────────┴────── GND

    Ports:
        - IN_POS: Positive input (from load/motor)
        - IN_NEG: Negative input (to ground)
        - GND: Ground reference
        - OUT: Amplified output (if amplifier enabled)
        - VREF: Reference voltage input for amplifier (if present)

    Example:
        from kicad_tools.schematic.blocks import CurrentSenseShunt

        sense = CurrentSenseShunt(
            sch, x=100, y=150,
            shunt_value="5mR",
            amplifier=True,
            gain=20,
        )
        sense.connect_to_rails(gnd_rail_y=200)
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        shunt_value: str = "10mR",
        shunt_package: str = "2512",
        ref_start: int = 1,
        ref_prefix: str = "R",
        resistor_symbol: str = "Device:R",
        amplifier: bool = False,
        amplifier_symbol: str = "Amplifier_Current:INA240A1",
        amplifier_ref: str = "U",
        gain: float = 20,
        amp_ref_start: int = 1,
        bypass_caps: list[str] | None = None,
        cap_ref_start: int = 1,
    ):
        """
        Create a current sense shunt circuit.

        Args:
            sch: Schematic to add to
            x: X coordinate of shunt resistor
            y: Y coordinate of shunt resistor
            shunt_value: Shunt resistor value (e.g., "5mR", "10mR", "50mR")
            shunt_package: Shunt resistor package (for power handling)
            ref_start: Starting reference number for shunt resistor
            ref_prefix: Reference designator prefix for shunt resistor
            resistor_symbol: KiCad symbol for resistor
            amplifier: If True, add a current sense amplifier
            amplifier_symbol: KiCad symbol for current sense amplifier
            amplifier_ref: Reference prefix for amplifier
            gain: Amplifier gain (for labeling purposes)
            amp_ref_start: Starting reference number for amplifier
            bypass_caps: Bypass capacitor values for amplifier (default: ["100nF"])
            cap_ref_start: Starting reference number for capacitors
        """
        super().__init__(sch, x, y)
        self.has_amplifier = amplifier
        self.shunt_value = shunt_value
        self.gain = gain

        # Place shunt resistor
        shunt_ref = f"{ref_prefix}{ref_start}"
        self.shunt = sch.add_symbol(
            resistor_symbol,
            x,
            y,
            shunt_ref,
            shunt_value,
            properties={"Package": shunt_package, "Power_Rating": "1W"},
        )

        self.components = {"R_SHUNT": self.shunt}

        # Get shunt pin positions
        shunt_pin1 = self.shunt.pin_position("1")  # IN+ side (from load)
        shunt_pin2 = self.shunt.pin_position("2")  # IN- side (to ground)

        # Define basic ports
        self.ports = {
            "IN_POS": shunt_pin1,
            "IN_NEG": shunt_pin2,
            "GND": shunt_pin2,
        }

        # Current sense amplifier (optional)
        if amplifier:
            if bypass_caps is None:
                bypass_caps = ["100nF"]

            amp_ref = f"{amplifier_ref}{amp_ref_start}"
            amp_x = x + 30
            amp_y = y

            self.amplifier = sch.add_symbol(
                amplifier_symbol,
                amp_x,
                amp_y,
                amp_ref,
                f"INA240 G={gain}",
            )
            self.components["CSA"] = self.amplifier

            # Get amplifier pin positions
            amp_inp = self.amplifier.pin_position("IN+")
            amp_inn = self.amplifier.pin_position("IN-")
            amp_out = self.amplifier.pin_position("OUT")
            amp_vs = self.amplifier.pin_position("VS")
            amp_gnd = self.amplifier.pin_position("GND")

            # Wire shunt to amplifier inputs
            sch.add_wire(shunt_pin1, amp_inp)
            sch.add_wire(shunt_pin2, amp_inn)

            # Add bypass capacitor(s)
            self.bypass_caps = []
            for i, cap_value in enumerate(bypass_caps):
                cap_ref = f"C{cap_ref_start + i}"
                cap_x = amp_x + 15 + i * 10
                cap_y = amp_y - 10
                cap = sch.add_symbol("Device:C", cap_x, cap_y, cap_ref, cap_value)
                self.bypass_caps.append(cap)
                self.components[f"C_BYPASS{i + 1}"] = cap

            # Add amplifier-specific ports
            self.ports["OUT"] = amp_out
            self.ports["VS"] = amp_vs
            self.ports["AMP_GND"] = amp_gnd

            # Store for rail connections
            self._amp_vs = amp_vs
            self._amp_gnd = amp_gnd

    def connect_to_rails(
        self,
        gnd_rail_y: float,
        vcc_rail_y: float | None = None,
        add_junctions: bool = True,
    ) -> None:
        """
        Connect current sense circuit to power rails.

        Args:
            gnd_rail_y: Y coordinate of ground rail
            vcc_rail_y: Y coordinate of VCC rail (for amplifier supply)
            add_junctions: Whether to add junction markers
        """
        sch = self.schematic

        # Connect shunt GND to rail
        gnd_pos = self.ports["GND"]
        sch.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y))

        if add_junctions:
            sch.add_junction(gnd_pos[0], gnd_rail_y)

        # Connect amplifier if present
        if self.has_amplifier and vcc_rail_y is not None:
            # Connect VS to VCC
            vs_pos = self._amp_vs
            sch.add_wire(vs_pos, (vs_pos[0], vcc_rail_y))

            # Connect amplifier GND
            amp_gnd = self._amp_gnd
            sch.add_wire(amp_gnd, (amp_gnd[0], gnd_rail_y))

            # Wire bypass caps
            for cap in self.bypass_caps:
                sch.wire_decoupling_cap(cap, vcc_rail_y, gnd_rail_y)

            if add_junctions:
                sch.add_junction(vs_pos[0], vcc_rail_y)
                sch.add_junction(amp_gnd[0], gnd_rail_y)

    def get_voltage_output(self, current_amps: float) -> float:
        """
        Calculate expected output voltage for a given current.

        Args:
            current_amps: Current through the shunt in amperes.

        Returns:
            Output voltage in volts (shunt voltage * gain if amplifier present).
        """
        # Parse shunt resistance
        shunt_ohms = self._parse_resistance(self.shunt_value)
        shunt_voltage = current_amps * shunt_ohms

        if self.has_amplifier:
            return shunt_voltage * self.gain
        return shunt_voltage

    @staticmethod
    def _parse_resistance(value: str) -> float:
        """
        Parse a resistance string to ohms.

        Supports: mR (milliohms), R (ohms), k (kilohms), M (megohms).

        Args:
            value: Resistance string like "5mR", "10mR", "100R", "10k"

        Returns:
            Resistance in ohms.
        """
        value = value.strip().upper()

        # Handle milliohm notation
        if value.endswith("MR"):
            return float(value[:-2]) / 1000

        # Handle inline R notation (e.g., "4R7" = 4.7 ohms)
        if "R" in value and not value.endswith("R"):
            parts = value.split("R")
            if len(parts) == 2:
                return float(parts[0]) + float(f"0.{parts[1]}")

        # Handle suffix notation
        if value.endswith("K"):
            return float(value[:-1]) * 1000
        elif value.endswith("M"):
            return float(value[:-1]) * 1_000_000
        elif value.endswith("R"):
            return float(value[:-1])
        else:
            return float(value)


class GateDriverBlock(CircuitBlock):
    """
    Gate driver IC block with bootstrap capacitors.

    Provides the gate drive circuitry for half-bridge or three-phase
    motor control applications.

    Schematic (3-phase example):
        VCC ──┬─────────────────────────────
              │    ┌───────────────────┐
              └────┤ VS                │
                   │                   │
        BOOT_A ────┤ HB_A    HO_A ─────┼──── GATE_HS_A
        BOOT_B ────┤ HB_B    HO_B ─────┼──── GATE_HS_B
        BOOT_C ────┤ HB_C    HO_C ─────┼──── GATE_HS_C
                   │                   │
        PWM_AH ────┤ IN_HA   LO_A ─────┼──── GATE_LS_A
        PWM_BH ────┤ IN_HB   LO_B ─────┼──── GATE_LS_B
        PWM_CH ────┤ IN_HC   LO_C ─────┼──── GATE_LS_C
                   │                   │
        PWM_AL ────┤ IN_LA             │
        PWM_BL ────┤ IN_LB             │
        PWM_CL ────┤ IN_LC             │
                   │                   │
        GND ───────┤ GND               │
                   └───────────────────┘

    Ports:
        - VCC: Logic supply voltage
        - GND: Ground reference
        - BOOT_A/B/C: Bootstrap pin connections
        - GATE_HS_A/B/C: High-side gate outputs
        - GATE_LS_A/B/C: Low-side gate outputs
        - PWM_AH/BH/CH: High-side PWM inputs
        - PWM_AL/BL/CL: Low-side PWM inputs

    Example:
        from kicad_tools.schematic.blocks import GateDriverBlock

        driver = GateDriverBlock(
            sch, x=200, y=100,
            driver_type="3-phase",
            ref="U4",
            value="DRV8301",
            bootstrap_caps="100nF",
        )
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        driver_type: str = "3-phase",
        ref: str = "U1",
        value: str = "DRV8301",
        driver_symbol: str | None = None,
        bootstrap_caps: str = "100nF",
        bypass_caps: list[str] | None = None,
        cap_ref_start: int = 1,
    ):
        """
        Create a gate driver block.

        Args:
            sch: Schematic to add to
            x: X coordinate of driver IC
            y: Y coordinate of driver IC
            driver_type: Type of driver - "3-phase" or "half-bridge"
            ref: Reference designator for driver IC
            value: Driver IC part number
            driver_symbol: KiCad symbol (auto-selected based on driver_type if None)
            bootstrap_caps: Bootstrap capacitor value per phase
            bypass_caps: Bypass capacitor values (default: ["10uF", "100nF"])
            cap_ref_start: Starting reference number for capacitors
        """
        super().__init__(sch, x, y)
        self.driver_type = driver_type

        if bypass_caps is None:
            bypass_caps = ["10uF", "100nF"]

        # Select symbol based on driver type
        if driver_symbol is None:
            if driver_type == "3-phase":
                driver_symbol = "Driver_FET:DRV8301"
            else:
                driver_symbol = "Driver_FET:IR2110"

        # Add placeholder text for the driver IC
        # (In production, would use actual driver symbol from library)
        sch.add_text(
            f"Gate Driver: {value}\nType: {driver_type}\n(Add from library)",
            x=x,
            y=y,
        )

        self.components = {}

        # Number of phases
        num_phases = 3 if driver_type == "3-phase" else 1
        phase_labels = ["A", "B", "C"][:num_phases]

        # Add bootstrap capacitors
        self.bootstrap_caps = []
        for i in range(num_phases):
            cap_ref = f"C{cap_ref_start + i}"
            cap_x = x - 20 + i * 10
            cap_y = y - 15
            cap = sch.add_symbol("Device:C", cap_x, cap_y, cap_ref, bootstrap_caps)
            self.bootstrap_caps.append(cap)
            self.components[f"C_BOOT_{phase_labels[i]}"] = cap

        # Add bypass capacitors
        self.bypass_caps = []
        bypass_start = cap_ref_start + num_phases
        for i, cap_value in enumerate(bypass_caps):
            cap_ref = f"C{bypass_start + i}"
            cap_x = x + 20 + i * 10
            cap_y = y - 15
            cap = sch.add_symbol("Device:C", cap_x, cap_y, cap_ref, cap_value)
            self.bypass_caps.append(cap)
            self.components[f"C_BYPASS{i + 1}"] = cap

        # Define placeholder ports (positions relative to driver center)
        # These would be updated with actual pin positions from the symbol
        self.ports = {
            "VCC": (x, y - 20),
            "GND": (x, y + 20),
        }

        for i, label in enumerate(phase_labels):
            offset = (i - 1) * 15  # Spread ports horizontally
            self.ports[f"BOOT_{label}"] = (x + offset - 30, y - 10)
            self.ports[f"GATE_HS_{label}"] = (x + offset + 30, y - 5)
            self.ports[f"GATE_LS_{label}"] = (x + offset + 30, y + 5)
            self.ports[f"PWM_H_{label}"] = (x + offset - 30, y + 5)
            self.ports[f"PWM_L_{label}"] = (x + offset - 30, y + 10)

    def connect_to_rails(
        self,
        vcc_rail_y: float,
        gnd_rail_y: float,
        add_junctions: bool = True,
    ) -> None:
        """
        Connect gate driver to power rails.

        Args:
            vcc_rail_y: Y coordinate of VCC rail
            gnd_rail_y: Y coordinate of ground rail
            add_junctions: Whether to add junction markers
        """
        sch = self.schematic

        # Wire bypass capacitors
        for cap in self.bypass_caps:
            sch.wire_decoupling_cap(cap, vcc_rail_y, gnd_rail_y)


# Factory functions


def create_half_bridge(
    sch: "Schematic",
    x: float,
    y: float,
    mosfet_value: str = "IRLZ44N",
    ref_start: int = 1,
    with_bootstrap: bool = False,
) -> HalfBridge:
    """
    Create a half-bridge with common defaults.

    Args:
        sch: Schematic to add to
        x: X coordinate
        y: Y coordinate
        mosfet_value: MOSFET part number
        ref_start: Starting reference number
        with_bootstrap: Include bootstrap circuit

    Returns:
        HalfBridge instance.
    """
    return HalfBridge(
        sch,
        x,
        y,
        ref_start=ref_start,
        mosfet_value=mosfet_value,
        bootstrap_cap="100nF" if with_bootstrap else None,
    )


def create_3phase_inverter(
    sch: "Schematic",
    x: float,
    y: float,
    mosfet_value: str = "IRLZ44N",
    ref_start: int = 1,
    with_bootstrap: bool = False,
) -> ThreePhaseInverter:
    """
    Create a 3-phase inverter for motor control.

    Args:
        sch: Schematic to add to
        x: X coordinate of first phase
        y: Y coordinate
        mosfet_value: MOSFET part number
        ref_start: Starting reference number
        with_bootstrap: Include bootstrap circuits

    Returns:
        ThreePhaseInverter instance.
    """
    return ThreePhaseInverter(
        sch,
        x,
        y,
        ref_start=ref_start,
        mosfet_value=mosfet_value,
        bootstrap_cap="100nF" if with_bootstrap else None,
        phase_labels=["A", "B", "C"],
    )


def create_current_sense(
    sch: "Schematic",
    x: float,
    y: float,
    shunt_value: str = "10mR",
    ref_start: int = 1,
    with_amplifier: bool = False,
    gain: float = 20,
) -> CurrentSenseShunt:
    """
    Create a current sense circuit.

    Args:
        sch: Schematic to add to
        x: X coordinate
        y: Y coordinate
        shunt_value: Shunt resistor value
        ref_start: Starting reference number
        with_amplifier: Include current sense amplifier
        gain: Amplifier gain

    Returns:
        CurrentSenseShunt instance.
    """
    return CurrentSenseShunt(
        sch,
        x,
        y,
        shunt_value=shunt_value,
        ref_start=ref_start,
        amplifier=with_amplifier,
        gain=gain,
    )
