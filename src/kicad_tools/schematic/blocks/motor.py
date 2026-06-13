"""Motor control circuit blocks: half-bridges, gate drivers, current sensing."""

from typing import TYPE_CHECKING

from ._stub_helpers import _emit_pin_net_stub
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
        mosfet_symbol: str = "Device:Q_NMOS",
        bootstrap_cap: str | None = None,
        bootstrap_diode: str = "1N4148",
        cap_ref_prefix: str = "C",
        diode_ref_prefix: str = "D",
        cap_ref_start: int | None = None,
        diode_ref_start: int | None = None,
        hs_ls_spacing: float = 40,
        gate_hs_net: str | None = None,
        gate_ls_net: str | None = None,
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
            gate_hs_net: Optional net name to label at the high-side gate pin.
                When provided, a short stub wire is drawn from the gate pin
                outward (to the left, away from the MOSFET body) and the
                label is placed on the stub endpoint so ERC's label-on-wire
                check is satisfied (see issue #2980). When ``None`` (the
                default), no label/stub is emitted and behavior is
                unchanged.
            gate_ls_net: Optional net name to label at the low-side gate
                pin. Same semantics as ``gate_hs_net``.
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

        # Optional gate-net labels.  KiCad's label-only connectivity requires
        # the label coordinate to lie on a wire endpoint or segment; without
        # a stub, labels at the bare gate pin float and trigger ERC's
        # ``isolated_pin_label`` cascade (see issue #2980).  We draw a
        # one-grid (2.54 mm) stub from each gate pin to the left (gates on
        # ``Device:Q_NMOS`` exit the symbol to the left) and place the
        # label on the stub endpoint.  External callers still wire to the
        # ``GATE_HS``/``GATE_LS`` ports, which remain at the pin positions.
        #
        # Use the generic ``_emit_pin_net_stub`` helper to gain collision
        # detection + auto-shift + raise-on-both-sides semantics (issue
        # #3015).  Pass ``x_center=hs_gate[0] + 1.0`` so the primary side
        # is always *left* (preserves PR #2985 behavior) while still
        # allowing auto-shift to the right when the left endpoint would
        # land on a foreign wire.
        if gate_hs_net is not None:
            _emit_pin_net_stub(
                sch,
                hs_gate,
                hs_gate[0] + 1.0,
                gate_hs_net,
                self.ports,
                block_label="HalfBridge ",
            )
        if gate_ls_net is not None:
            _emit_pin_net_stub(
                sch,
                ls_gate,
                ls_gate[0] + 1.0,
                gate_ls_net,
                self.ports,
                block_label="HalfBridge ",
            )

    def connect_to_rails(
        self,
        vin_rail_y: float,
        gnd_rail_y: float,
        add_junctions: bool = True,
        inline_shunt: "CurrentSenseShunt | None" = None,
    ) -> None:
        """
        Connect half-bridge to power rails.

        Args:
            vin_rail_y: Y coordinate of VIN (motor voltage) rail
            gnd_rail_y: Y coordinate of ground rail
            add_junctions: Whether to add junction markers
            inline_shunt: Optional :class:`CurrentSenseShunt` placed in
                series between the low-side MOSFET source and GND.  When
                provided, the LS-source-to-GND wire that this helper
                normally emits is suppressed, because that wire would
                short-circuit the shunt (the LS source belongs on the
                shunt's IN+ side, not directly on GND).  The caller is
                responsible for wiring the LS source to the shunt's
                ``IN_POS`` port and for routing the shunt to GND (either
                via ``CurrentSenseShunt.connect_to_rails`` or a Kelvin-
                sense label).  When ``None`` (the default), behavior is
                unchanged: a direct LS-source-to-GND wire is emitted.
                See issue #3383 (board 05 ISENSE_X+ shunt-bridge).
        """
        sch = self.schematic

        # Connect HS drain to VIN rail
        vin_pos = self._vin_pos
        sch.add_wire(vin_pos, (vin_pos[0], vin_rail_y), warn_on_collision=False)

        # Connect LS source to GND rail -- unless an in-line shunt sits
        # between the LS source and GND, in which case this wire would
        # short the shunt (issue #3383).  When ``inline_shunt`` is
        # provided, we skip the LS-source-to-GND wire and let the caller
        # wire LS source to the shunt's IN+ side and the shunt's IN-
        # side to GND.
        gnd_pos = self._gnd_pos
        if inline_shunt is None:
            sch.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y), warn_on_collision=False)

        if add_junctions:
            sch.add_junction(vin_pos[0], vin_rail_y)
            if inline_shunt is None:
                sch.add_junction(gnd_pos[0], gnd_rail_y)

        # Connect bootstrap diode anode to VIN rail
        if self.has_bootstrap:
            boot_vin = self.ports["VIN_BOOT"]
            sch.add_wire(boot_vin, (boot_vin[0], vin_rail_y), warn_on_collision=False)
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
        mosfet_symbol: str = "Device:Q_NMOS",
        bootstrap_cap: str | None = None,
        phase_labels: list[str] | None = None,
        phase_spacing: float = 75,
        hs_ls_spacing: float = 40,
        gate_hs_nets: list[str] | None = None,
        gate_ls_nets: list[str] | None = None,
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
            gate_hs_nets: Optional list of per-phase high-side gate net
                names (length must equal ``len(phase_labels)``).  When
                provided, each phase's :class:`HalfBridge` emits a stub
                wire + label at its HS gate pin so ERC sees a real
                connection (see issue #2980).  When ``None`` (the
                default), no labels are emitted.
            gate_ls_nets: Optional list of per-phase low-side gate net
                names.  Same semantics as ``gate_hs_nets``.

        Raises:
            ValueError: If ``gate_hs_nets`` or ``gate_ls_nets`` is provided
                and its length does not match the phase count.
        """
        super().__init__(sch, x, y)

        if phase_labels is None:
            phase_labels = ["A", "B", "C"]

        num_phases = len(phase_labels)
        if gate_hs_nets is not None and len(gate_hs_nets) != num_phases:
            raise ValueError(
                f"gate_hs_nets length ({len(gate_hs_nets)}) must match "
                f"phase_labels length ({num_phases})"
            )
        if gate_ls_nets is not None and len(gate_ls_nets) != num_phases:
            raise ValueError(
                f"gate_ls_nets length ({len(gate_ls_nets)}) must match "
                f"phase_labels length ({num_phases})"
            )

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
                gate_hs_net=gate_hs_nets[i] if gate_hs_nets is not None else None,
                gate_ls_net=gate_ls_nets[i] if gate_ls_nets is not None else None,
            )
            self.half_bridges.append(hb)

            # Add phase output label with connecting wire
            phase_pos = hb.port("VOUT")
            label_x = phase_pos[0] + 10
            # Add wire from phase output to label position
            sch.add_wire(phase_pos, (label_x, phase_pos[1]))
            sch.add_label(f"PHASE_{label}", label_x, phase_pos[1], rotation=0)

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
        inline_shunts: "list[CurrentSenseShunt | None] | None" = None,
    ) -> None:
        """
        Connect all half-bridges to power rails.

        Args:
            vin_rail_y: Y coordinate of VIN (motor voltage) rail
            gnd_rail_y: Y coordinate of ground rail
            add_junctions: Whether to add junction markers
            inline_shunts: Optional per-phase :class:`CurrentSenseShunt`
                instances (one per phase, in ``phase_labels`` order; use
                ``None`` for phases without an in-line shunt).  When the
                entry for a given phase is non-``None``, the
                corresponding half-bridge's LS-source-to-GND wire is
                suppressed -- see ``HalfBridge.connect_to_rails`` for
                details and rationale (issue #3383).  Length must equal
                the number of phases (``len(self.half_bridges)``).  When
                ``None`` (the default), every half-bridge emits its
                direct LS-source-to-GND wire as before.

        Raises:
            ValueError: If ``inline_shunts`` is provided and its length
                does not match the number of phases.
        """
        if inline_shunts is not None and len(inline_shunts) != len(self.half_bridges):
            raise ValueError(
                f"inline_shunts length ({len(inline_shunts)}) must equal "
                f"number of phases ({len(self.half_bridges)})"
            )
        for i, hb in enumerate(self.half_bridges):
            shunt = inline_shunts[i] if inline_shunts is not None else None
            hb.connect_to_rails(
                vin_rail_y,
                gnd_rail_y,
                add_junctions,
                inline_shunt=shunt,
            )


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
        sch.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y), warn_on_collision=False)

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


class BootstrapCapacitorArray(CircuitBlock):
    """
    N-phase array of bootstrap capacitors for driver-IC topologies.

    Each phase has a single bootstrap capacitor between its high-side
    bootstrap node (e.g. ``BST_A``) and the corresponding switch-node
    return (e.g. ``PHASE_A``).  Unlike :class:`HalfBridge`'s built-in
    bootstrap (which adds a diode for discrete-MOSFET designs), this
    block targets driver ICs that already have an internal bootstrap
    pin and only need the external cap.

    Schematic (3-phase example):
        BST_A ─┬─    BST_B ─┬─    BST_C ─┬─
               │            │            │
              [C1]         [C2]         [C3]
               │            │            │
        PHASE_A┴     PHASE_B┴     PHASE_C┴

    Ports per phase ``i`` in ``[0, phases)``:
        - ``HIGH_<label>``: Bootstrap node (cap pin 1).
        - ``PHASE_<label>``: Switch-node return (cap pin 2).

    Components are stored as ``self.caps`` (list[Symbol]) and keyed in
    ``self.components`` as ``C_BOOT_<label>``.

    The block does NOT call any rail-connect helper -- wiring is left
    to the caller, consistent with :class:`DecouplingCaps`.

    Example:
        from kicad_tools.schematic.blocks import (
            create_bootstrap_capacitor_array,
        )

        boot = create_bootstrap_capacitor_array(
            sch, x=80, y=60,
            phases=3,
            value="100nF",
            cap_ref_start=12,        # C12, C13, C14
            high_nets=["BST_A", "BST_B", "BST_C"],
            phase_nets=["PHASE_A", "PHASE_B", "PHASE_C"],
        )
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        phases: int = 3,
        value: str = "100nF",
        phase_labels: list[str] | None = None,
        high_nets: list[str] | None = None,
        phase_nets: list[str] | None = None,
        cap_ref_start: int = 1,
        cap_ref_prefix: str = "C",
        cap_symbol: str = "Device:C",
        cap_spacing: float = 10,
        cap_footprint: str | None = None,
        auto_footprint: bool = False,
    ):
        """
        Create an N-phase bootstrap capacitor array.

        Args:
            sch: Schematic to add to.
            x: X coordinate of the first capacitor.
            y: Y coordinate of all capacitors.
            phases: Number of bootstrap capacitors (default 3).
            value: Capacitor value, applied to every cap (default "100nF").
            phase_labels: Per-phase labels.  Defaults to
                ``["A", "B", "C"][:phases]`` when ``phases <= 3``,
                otherwise ``[str(i) for i in range(phases)]``.
            high_nets: Optional per-phase net names for the high
                (bootstrap) side.  If provided, ``add_label`` is called
                at each cap's pin 1.  Length must equal ``phases``.
            phase_nets: Optional per-phase net names for the
                switch-node return.  If provided, ``add_label`` is
                called at each cap's pin 2.  Length must equal
                ``phases``.
            cap_ref_start: Reference number for the first capacitor
                (default 1).
            cap_ref_prefix: Reference prefix (default "C").
            cap_symbol: KiCad symbol for capacitors (default "Device:C").
            cap_spacing: Horizontal spacing between caps (default 10).
            cap_footprint: Explicit footprint string (e.g.,
                ``"Capacitor_SMD:C_0805_2012Metric"``) forwarded to each
                bootstrap cap's ``add_symbol`` call.  When ``None`` (default),
                no explicit footprint is set, preserving back-compat for
                existing callers.  Takes precedence over ``auto_footprint``
                when both are provided.  Mirrors the convention used by
                :class:`DecouplingCaps` and the regulator blocks.
            auto_footprint: If ``True``, forwarded as ``auto_footprint=True``
                to each bootstrap cap's ``add_symbol`` call so the schematic's
                footprint-selector profile chooses a footprint based on the
                cap value.  Default ``False`` preserves back-compat.

        Raises:
            ValueError: If ``phases`` < 1, or if ``phase_labels``,
                ``high_nets``, or ``phase_nets`` have mismatched length.
        """
        super().__init__(sch, x, y)

        if phases < 1:
            raise ValueError(f"phases must be >= 1, got {phases}")

        # Default labels: A/B/C for small phase counts, integers otherwise
        if phase_labels is None:
            if phases <= 3:
                phase_labels = ["A", "B", "C"][:phases]
            else:
                phase_labels = [str(i) for i in range(phases)]
        elif len(phase_labels) != phases:
            raise ValueError(f"phase_labels length {len(phase_labels)} != phases {phases}")

        if high_nets is not None and len(high_nets) != phases:
            raise ValueError(f"high_nets length {len(high_nets)} != phases {phases}")
        if phase_nets is not None and len(phase_nets) != phases:
            raise ValueError(f"phase_nets length {len(phase_nets)} != phases {phases}")

        self.phases = phases
        self.phase_labels = phase_labels
        self.value = value
        self.caps: list = []
        self.components = {}
        self.ports = {}

        # Build add_symbol kwargs once -- mirrors the DecouplingCaps pattern
        # at blocks/power/passives.py:64-66 so the bootstrap caps inherit a
        # footprint (explicit or auto-selected) rather than landing in the
        # schematic with an empty footprint field (sibling fix to PR #3016 /
        # issue #3009; see issue #3017).
        add_kwargs: dict = {"auto_footprint": auto_footprint}
        if cap_footprint is not None:
            add_kwargs["footprint"] = cap_footprint

        for i, label in enumerate(phase_labels):
            cap_x = x + i * cap_spacing
            cap_ref = f"{cap_ref_prefix}{cap_ref_start + i}"
            cap = sch.add_symbol(cap_symbol, cap_x, y, cap_ref, value, **add_kwargs)
            self.caps.append(cap)
            self.components[f"C_BOOT_{label}"] = cap

            high_pos = cap.pin_position("1")
            phase_pos = cap.pin_position("2")

            self.ports[f"HIGH_{label}"] = high_pos
            self.ports[f"PHASE_{label}"] = phase_pos

            # Optionally drive labels for net naming.  ERC requires labels
            # to attach to wires, so we draw a short stub from each pin
            # to the label position (matches ThreePhaseInverter convention).
            if high_nets is not None:
                stub_x = high_pos[0]
                stub_y = high_pos[1] - 2.54
                sch.add_wire(high_pos, (stub_x, stub_y), warn_on_collision=False)
                sch.add_label(high_nets[i], stub_x, stub_y, rotation=0)
            if phase_nets is not None:
                stub_x = phase_pos[0]
                stub_y = phase_pos[1] + 2.54
                sch.add_wire(phase_pos, (stub_x, stub_y), warn_on_collision=False)
                sch.add_label(phase_nets[i], stub_x, stub_y, rotation=0)


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
        bootstrap_caps: str | None = "100nF",
        bypass_caps: list[str] | None = None,
        cap_ref_start: int = 1,
        pin_nets: dict[str, str] | None = None,
        bypass_cap_footprint: str | None = None,
        auto_footprint: bool = False,
        bootstrap_cap_footprint: str | None = None,
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
            bootstrap_caps: Bootstrap capacitor value per phase (None to omit).
                When non-None, an internal :class:`BootstrapCapacitorArray`
                is composed and exposed via ``self._bootstrap_block``.
            bypass_caps: Bypass capacitor values (default: ["10uF", "100nF"])
            cap_ref_start: Starting reference number for capacitors
            pin_nets: Optional mapping of driver-IC pin name or number to
                net label.  For each entry, a one-grid (2.54 mm) stub wire
                is drawn from the pin away from the symbol center and the
                net label is placed on the stub endpoint so KiCad's
                label-on-wire ERC check is satisfied (see issue #2980).
                When ``None`` (the default), no labels are emitted and
                behavior is unchanged.  The mapping keys must be valid
                pin identifiers for the resolved ``driver_symbol``
                (e.g. ``"UHSG"`` or ``"32"`` for ``Driver_Motor:DRV8308``).
                For every entry, an alias port is also added under the
                net name so callers can retrieve real pin coordinates via
                ``block.port("<net>")``.
            bypass_cap_footprint: Explicit footprint string (e.g.,
                ``"Capacitor_SMD:C_0805_2012Metric"``) forwarded to each
                bypass cap's ``add_symbol`` call.  When ``None`` (default),
                no explicit footprint is set, preserving back-compat for
                existing callers.  Takes precedence over ``auto_footprint``
                when both are provided.  Mirrors the convention used by
                :class:`DecouplingCaps` and the regulator blocks.
            auto_footprint: If ``True``, forwarded as ``auto_footprint=True``
                to each bypass cap's ``add_symbol`` call AND to the internal
                :class:`BootstrapCapacitorArray` instantiation (when
                ``bootstrap_caps`` is not ``None``) so the schematic's
                footprint-selector profile chooses a footprint based on the
                cap value.  Default ``False`` preserves back-compat.
            bootstrap_cap_footprint: Explicit footprint string forwarded to
                the internal :class:`BootstrapCapacitorArray` instantiation
                so its caps inherit a footprint rather than landing in the
                schematic with an empty footprint field (see issue #3017).
                When ``None`` (default), falls back to ``bypass_cap_footprint``
                if that is provided; otherwise no explicit footprint is set,
                preserving back-compat.  Only applies when ``bootstrap_caps``
                is not ``None``.
        """
        super().__init__(sch, x, y)
        self.driver_type = driver_type

        if bypass_caps is None:
            bypass_caps = ["10uF", "100nF"]

        # Select symbol based on driver type
        if driver_symbol is None:
            if driver_type == "3-phase":
                driver_symbol = "Driver_Motor:DRV8308"
            else:
                driver_symbol = "Driver_FET:IR2110"

        # Add the driver IC symbol
        self.driver = sch.add_symbol(driver_symbol, x, y, ref, value)

        self.components = {"DRIVER": self.driver}

        # Number of phases
        num_phases = 3 if driver_type == "3-phase" else 1
        phase_labels = ["A", "B", "C"][:num_phases]

        # Add bootstrap capacitors via BootstrapCapacitorArray composition.
        # We expose self.bootstrap_caps as the underlying caps list for
        # back-compat with tests that assert len(driver.bootstrap_caps) == N.
        self._bootstrap_block: BootstrapCapacitorArray | None = None
        if bootstrap_caps is not None:
            # Resolve the bootstrap footprint: explicit ``bootstrap_cap_footprint``
            # wins; otherwise fall back to ``bypass_cap_footprint`` (matches the
            # common board-05 reality where bootstrap and bypass caps share the
            # same 0805 package).  When neither is set the BootstrapCapacitorArray
            # receives ``None`` and behaves identically to pre-#3017 callers.
            resolved_bootstrap_footprint = (
                bootstrap_cap_footprint
                if bootstrap_cap_footprint is not None
                else bypass_cap_footprint
            )
            self._bootstrap_block = BootstrapCapacitorArray(
                sch,
                x=x - 20,
                y=y - 15,
                phases=num_phases,
                value=bootstrap_caps,
                phase_labels=phase_labels,
                cap_ref_start=cap_ref_start,
                cap_footprint=resolved_bootstrap_footprint,
                auto_footprint=auto_footprint,
            )
            self.bootstrap_caps = self._bootstrap_block.caps
            # Merge bootstrap components into our components dict
            for name, comp in self._bootstrap_block.components.items():
                self.components[name] = comp
        else:
            self.bootstrap_caps = []

        # Add bypass capacitors.
        # Build add_symbol kwargs once -- mirrors the DecouplingCaps pattern
        # at blocks/power/passives.py:64-66 so the bypass caps inherit a
        # footprint (explicit or auto-selected) rather than landing in the
        # schematic with an empty footprint field (see issue #3009).
        bypass_add_kwargs: dict = {"auto_footprint": auto_footprint}
        if bypass_cap_footprint is not None:
            bypass_add_kwargs["footprint"] = bypass_cap_footprint
        # The starting ref must skip only as many caps as the bootstrap
        # array actually consumed.  When ``bootstrap_caps is None`` the
        # bootstrap array is empty, so the bypass caps start at
        # ``cap_ref_start`` directly.  Deriving the offset from
        # ``len(self.bootstrap_caps)`` (rather than from ``num_phases``)
        # ties the skip to actual consumption and is drift-resistant if
        # ``BootstrapCapacitorArray`` ever changes its ref-consumption
        # contract.  See issue #3008.
        self.bypass_caps = []
        bypass_start = cap_ref_start + len(self.bootstrap_caps)
        for i, cap_value in enumerate(bypass_caps):
            cap_ref = f"C{bypass_start + i}"
            cap_x = x + 20 + i * 10
            cap_y = y - 15
            cap = sch.add_symbol("Device:C", cap_x, cap_y, cap_ref, cap_value, **bypass_add_kwargs)
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

        # Re-expose bootstrap-array ports so callers can wire HIGH_<label>
        # / PHASE_<label> to BST_<label> / PHASE_<label> nets.
        if self._bootstrap_block is not None:
            for name, pos in self._bootstrap_block.ports.items():
                # Avoid clobbering driver ports if names ever collide
                if name not in self.ports:
                    self.ports[name] = pos

        # Optional pin-net labels.  KiCad's label-only connectivity requires
        # the label coordinate to lie on a wire endpoint or segment; without
        # a stub, labels placed at the bare pin float and trigger ERC's
        # ``isolated_pin_label`` cascade.  For each ``pin_nets`` entry we
        # resolve the real pin position via ``self.driver.pin_position``
        # (supporting either pin names or pin numbers), draw a one-grid
        # (2.54 mm) horizontal stub *away from the symbol center* (left
        # for pins on the symbol's left edge, right otherwise), and place
        # the label on the stub endpoint.  See issue #2980.
        #
        # For every labelled pin we also add an alias port keyed by the
        # net name, exposing the pin's real coordinates so callers can
        # wire to ``block.port("<net>")`` instead of relying on the
        # historical placeholder port coordinates.
        if pin_nets is not None:
            for pin_key, net_name in pin_nets.items():
                pin_pos = self.driver.pin_position(pin_key)
                # Delegate to ``_emit_pin_net_stub`` which performs the
                # stub-direction heuristic + collision-aware auto-shift
                # (raises ``ValueError`` if both sides collide; see issue
                # #3015).
                _emit_pin_net_stub(
                    sch,
                    pin_pos,
                    x,
                    net_name,
                    self.ports,
                    block_label="GateDriverBlock ",
                )

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


def create_bootstrap_capacitor_array(
    sch: "Schematic",
    x: float,
    y: float,
    phases: int = 3,
    value: str = "100nF",
    phase_labels: list[str] | None = None,
    high_nets: list[str] | None = None,
    phase_nets: list[str] | None = None,
    cap_ref_start: int = 1,
    cap_ref_prefix: str = "C",
    cap_symbol: str = "Device:C",
    cap_spacing: float = 10,
    cap_footprint: str | None = None,
    auto_footprint: bool = False,
) -> BootstrapCapacitorArray:
    """
    Create an N-phase bootstrap capacitor array (driver-IC topology).

    This is a thin wrapper around :class:`BootstrapCapacitorArray`
    that provides a discoverable factory entry point alongside the
    other ``create_*`` helpers in this module.

    Args:
        sch: Schematic to add to.
        x: X coordinate of the first capacitor.
        y: Y coordinate of all capacitors.
        phases: Number of bootstrap capacitors (default 3).
        value: Capacitor value, applied to every cap (default "100nF").
        phase_labels: Per-phase labels.  Defaults to A/B/C for
            ``phases <= 3``, integer strings otherwise.
        high_nets: Optional per-phase net names for the high
            (bootstrap) side (e.g. ``["BST_A", "BST_B", "BST_C"]``).
            Length must equal ``phases``.
        phase_nets: Optional per-phase net names for the switch-node
            return.  Length must equal ``phases``.
        cap_ref_start: Reference number for the first capacitor
            (default 1).
        cap_ref_prefix: Reference prefix (default "C").
        cap_symbol: KiCad symbol for capacitors (default "Device:C").
        cap_spacing: Horizontal spacing between caps (default 10).
        cap_footprint: Explicit footprint string (e.g.,
            ``"Capacitor_SMD:C_0805_2012Metric"``) forwarded to each
            bootstrap cap's ``add_symbol`` call.  When ``None`` (default),
            no explicit footprint is set, preserving back-compat for
            existing callers.  Takes precedence over ``auto_footprint``
            when both are provided.  See issue #3017.
        auto_footprint: If ``True``, forwarded as ``auto_footprint=True``
            to each bootstrap cap's ``add_symbol`` call so the schematic's
            footprint-selector profile chooses a footprint based on the
            cap value.  Default ``False`` preserves back-compat.

    Returns:
        :class:`BootstrapCapacitorArray` instance.
    """
    return BootstrapCapacitorArray(
        sch,
        x,
        y,
        phases=phases,
        value=value,
        phase_labels=phase_labels,
        high_nets=high_nets,
        phase_nets=phase_nets,
        cap_ref_start=cap_ref_start,
        cap_ref_prefix=cap_ref_prefix,
        cap_symbol=cap_symbol,
        cap_spacing=cap_spacing,
        cap_footprint=cap_footprint,
        auto_footprint=auto_footprint,
    )


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


class GateDriveResistorArray(CircuitBlock):
    """
    Series gate-drive (slew-rate) resistor array.

    A bank of N series resistors placed in the path between a gate-driver
    output and the gate of a power MOSFET. Each resistor controls the
    switching slew rate for one channel:

        - Too little series R causes ringing/EMI and shoot-through risk.
        - Too much series R increases switching loss and dead-time
          requirements.

    Schematic (3-channel):

        IN_1 ──[R_GATE_1]── OUT_1
        IN_2 ──[R_GATE_2]── OUT_2
        IN_3 ──[R_GATE_3]── OUT_3

    Ports:
        - IN_1..IN_N: Driver-side inputs.
        - OUT_1..OUT_N: MOSFET-gate-side outputs.
        - When ``input_nets`` / ``output_nets`` are provided, alias ports
          named after the suffix of each net are also exposed (e.g. for
          ``input_nets=["GATE_DRV_AH"]`` an alias ``IN_AH`` is added).

    Example:
        from kicad_tools.schematic.blocks import create_gate_drive_resistor_array

        gate_r = create_gate_drive_resistor_array(
            sch, x=300, y=120,
            channels=3,
            value="22",
            ref_start=20,
            input_nets=["GATE_DRV_AH", "GATE_DRV_BH", "GATE_DRV_CH"],
            output_nets=["GATE_AH", "GATE_BH", "GATE_CH"],
        )
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        channels: int = 3,
        value: str = "10",
        *,
        ref_start: int = 1,
        ref_prefix: str = "R",
        resistor_symbol: str = "Device:R",
        resistor_package: str = "0805",
        spacing: float = 10.0,
        input_nets: list[str] | None = None,
        output_nets: list[str] | None = None,
    ):
        """
        Create a gate-drive resistor array.

        Args:
            sch: Schematic to add components to.
            x: X coordinate of first resistor.
            y: Y coordinate (center of the array).
            channels: Number of resistor channels (one per gate signal).
            value: Resistor value in ohms (e.g. ``"10"``, ``"22"``, ``"47"``).
                Typical range is 10-47 Ω for IRLZ44N-class gates.
            ref_start: Starting reference number for resistors.
            ref_prefix: Reference designator prefix (default ``"R"``).
            resistor_symbol: KiCad symbol for resistor.
            resistor_package: Footprint package (e.g. ``"0805"``, ``"0603"``).
            spacing: Horizontal spacing between resistors (schematic units).
            input_nets: Optional list of input net names (length must equal
                ``channels``). When provided, ``add_label`` is called at each
                resistor input pin and an alias port is added.
            output_nets: Optional list of output net names (length must equal
                ``channels``). When provided, ``add_label`` is called at each
                resistor output pin and an alias port is added.

        Raises:
            ValueError: If ``input_nets`` or ``output_nets`` is provided and
                its length does not equal ``channels``.
        """
        super().__init__(sch, x, y)

        if channels < 1:
            raise ValueError(f"channels must be >= 1, got {channels}")

        if input_nets is not None and len(input_nets) != channels:
            raise ValueError(
                f"input_nets length ({len(input_nets)}) must equal channels ({channels})"
            )
        if output_nets is not None and len(output_nets) != channels:
            raise ValueError(
                f"output_nets length ({len(output_nets)}) must equal channels ({channels})"
            )

        self.channels = channels
        self.value = value
        self.resistor_package = resistor_package
        self.resistors = []
        self.components = {}
        self.ports = {}

        for i in range(channels):
            res_x = x + i * spacing
            ref = f"{ref_prefix}{ref_start + i}"
            resistor = sch.add_symbol(
                resistor_symbol,
                res_x,
                y,
                ref,
                value,
                properties={"Package": resistor_package},
            )
            self.resistors.append(resistor)
            self.components[f"R_GATE_{i + 1}"] = resistor

            # Pin 1 = input (driver side), Pin 2 = output (MOSFET-gate side).
            in_pos = resistor.pin_position("1")
            out_pos = resistor.pin_position("2")

            in_port_name = f"IN_{i + 1}"
            out_port_name = f"OUT_{i + 1}"
            self.ports[in_port_name] = in_pos
            self.ports[out_port_name] = out_pos

            # Optional net labels — emit short horizontal stub wires from
            # each pin so the label has a wire to anchor to. KiCad's
            # label-only connectivity requires the label coordinate to lie
            # on a wire endpoint or segment; without these stubs, labels
            # in columns beyond the first one float (see issue #2968).
            # The stub length matches one schematic grid (2.54 mm).
            #
            # Pattern mirrors ``create_3phase_inverter`` (~line 350):
            #     sch.add_wire(pos, (label_x, pos[1]))
            #     sch.add_label(name, label_x, pos[1], rotation=0)
            #
            # Alias ports are keyed by the net suffix (the trailing token
            # after the last ``_``).
            STUB = 2.54
            if input_nets is not None:
                in_net = input_nets[i]
                in_label_x = in_pos[0] - STUB
                sch.add_wire(in_pos, (in_label_x, in_pos[1]))
                sch.add_label(in_net, in_label_x, in_pos[1], rotation=0)
                alias = f"IN_{_net_suffix(in_net)}"
                self.ports[alias] = in_pos

            if output_nets is not None:
                out_net = output_nets[i]
                out_label_x = out_pos[0] + STUB
                sch.add_wire(out_pos, (out_label_x, out_pos[1]))
                sch.add_label(out_net, out_label_x, out_pos[1], rotation=0)
                alias = f"OUT_{_net_suffix(out_net)}"
                self.ports[alias] = out_pos


def _net_suffix(net_name: str) -> str:
    """Extract the suffix used for alias port naming.

    The suffix is the trailing token after the last underscore. For
    ``"GATE_DRV_AH"`` it returns ``"AH"``; for ``"GATE_AH"`` it returns
    ``"AH"``; for a single-token name it returns the whole name.
    """
    return net_name.rsplit("_", 1)[-1] if "_" in net_name else net_name


def create_gate_drive_resistor_array(
    sch: "Schematic",
    x: float,
    y: float,
    channels: int = 3,
    value: str = "10",
    *,
    ref_start: int = 1,
    ref_prefix: str = "R",
    resistor_symbol: str = "Device:R",
    resistor_package: str = "0805",
    spacing: float = 10.0,
    input_nets: list[str] | None = None,
    output_nets: list[str] | None = None,
) -> GateDriveResistorArray:
    """
    Create an N-channel series gate-drive (slew-rate) resistor array.

    A series resistor (commonly 10-47 Ω) belongs in the path from each
    gate-driver IC output to its MOSFET gate. This factory produces an
    array sized to ``channels``, intended to be spliced in between the
    driver and the MOSFETs.

    Args:
        sch: Schematic to add components to.
        x: X coordinate of first resistor.
        y: Y coordinate (center of the array).
        channels: Number of resistor channels (one per gate signal).
            Common values: 1 (low-side switch), 2 (half-bridge HS+LS),
            3 (3-phase HS only), 6 (3-phase HS+LS).
        value: Resistor value in ohms (e.g. ``"10"``, ``"22"``, ``"47"``).
        ref_start: Starting reference number.
        ref_prefix: Reference designator prefix (default ``"R"``).
        resistor_symbol: KiCad symbol for resistor.
        resistor_package: Footprint package (e.g. ``"0805"``, ``"0603"``).
        spacing: Horizontal spacing between resistors.
        input_nets: Optional list of input net names (length == ``channels``).
            Each emits an ``add_label`` call at the corresponding input pin
            and adds an alias port (e.g. ``IN_AH``).
        output_nets: Optional list of output net names (length == ``channels``).

    Returns:
        ``GateDriveResistorArray`` instance.

    Example:
        # 3-phase driver -> resistor array -> MOSFET gates (HS only).
        from kicad_tools.schematic.blocks import create_gate_drive_resistor_array

        gate_r = create_gate_drive_resistor_array(
            sch, x=300, y=120,
            channels=3, value="22", ref_start=20,
            input_nets=["GATE_DRV_AH", "GATE_DRV_BH", "GATE_DRV_CH"],
            output_nets=["GATE_AH", "GATE_BH", "GATE_CH"],
        )

    Raises:
        ValueError: If ``input_nets`` or ``output_nets`` length does not
            equal ``channels``.
    """
    return GateDriveResistorArray(
        sch,
        x,
        y,
        channels=channels,
        value=value,
        ref_start=ref_start,
        ref_prefix=ref_prefix,
        resistor_symbol=resistor_symbol,
        resistor_package=resistor_package,
        spacing=spacing,
        input_nets=input_nets,
        output_nets=output_nets,
    )


class BackToBackFETPair(CircuitBlock):
    """Two N-channel MOSFETs in source-tied (common-source) back-to-back configuration.

    This is the canonical bidirectional-blocking switch topology: two
    N-FETs sit with their sources tied together (the *Kelvin* / common-
    source node) and their drains face outward.  Because each FET's
    body diode is oriented towards its own drain, the pair blocks
    current in *both* directions when both gates are off (a single FET
    would conduct via its body diode when the drain-source voltage
    reverses).

    This is intentionally **NOT** a ``HalfBridge``.  In a half-bridge,
    the high-side source ties to the low-side drain (the *phase* node),
    and the body-diode orientation creates a freewheeling path for
    motor inductance.  The semantics, port set, and downstream usage
    differ — please don't reach for ``HalfBridge`` here.

    Schematic:

        DRAIN_A ──┐
                 [QA]      ← gate GATE_A
        SOURCE   ─┤
                  │ (sources tied — common-source / Kelvin reference)
        SOURCE   ─┤
                 [QB]      ← gate GATE_B
        DRAIN_B ──┘

    Typical use:
    -   Bidirectional supercap-bank discharge switch (softstart rev B):
        DRAIN_A → bank+, DRAIN_B → bus return, GATE_A == GATE_B driven
        by a single gate-driver HO output, SOURCE is the Kelvin point
        for current-sense return AND the driver's COM/HS reference.
    -   Battery-protection back-to-back FET (charge/discharge blocking).
    -   Any "true off" load switch where leakage through a body diode
        is unacceptable.

    Ports:
        - DRAIN_A: First FET drain (e.g., wired to bank+)
        - DRAIN_B: Second FET drain (e.g., wired to bus return)
        - GATE_A: First FET gate (typically driven from gate driver HO)
        - GATE_B: Second FET gate (often tied to GATE_A in driver
          topologies that drive both FETs from a single output)
        - SOURCE: Common-source / Kelvin reference node (the driver's
          COM/HS pin should tie here via a dedicated short trace —
          this is *the* layout-critical net in this topology)

    Thermal metadata:
        Each FET is tagged with ``Thermal_Rth_JC=0.5`` and
        ``Power_Dissipation=5W`` so ThermalAnalyzer integration
        treats it like the existing ``HalfBridge`` FETs.

    Example:
        from kicad_tools.schematic.blocks import BackToBackFETPair

        pair = BackToBackFETPair(
            sch, x=150, y=80,
            ref_a="Q1A", ref_b="Q1B",
            mosfet_value="IRFB4110",
        )
        sch.add_wire(pair.port("DRAIN_A"), bank_pos_pos)
        sch.add_wire(pair.port("DRAIN_B"), bus_pos)
        sch.add_label("SRC_POS", *pair.port("SOURCE"))
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        ref_a: str = "Q1A",
        ref_b: str = "Q1B",
        mosfet_value: str = "IRFB4110",
        mosfet_symbol: str = "Device:Q_NMOS",
        mosfet_footprint: str = "Package_TO_SOT_THT:TO-220-3_Vertical",
        fet_spacing: float = 40.0,
        kelvin_label: str | None = None,
        kelvin_trace_hint: bool = True,
    ):
        """Create a back-to-back N-FET pair with common-source / Kelvin tie.

        Args:
            sch: Schematic to add to.
            x: X coordinate of the FET pair center.
            y: Y coordinate of the *first* (upper) FET center; the
                second FET sits ``fet_spacing`` mm below.
            ref_a: Reference designator for FET A (the upper one).
            ref_b: Reference designator for FET B (the lower one).
            mosfet_value: MOSFET part number / value (default IRFB4110
                — TI 100V N-FET used in softstart rev B).
            mosfet_symbol: KiCad symbol for N-channel MOSFET.
            mosfet_footprint: Footprint for both FETs.
            fet_spacing: Vertical spacing between the two FET centers
                (mm).  Defaults to 40 to leave room for a labeled
                Kelvin tie wire and a junction marker.
            kelvin_label: Optional net name to label at the common-
                source / Kelvin tie node.  When provided, a label is
                placed on the source-tie wire so ERC sees the named
                net.
            kelvin_trace_hint: When True (default), draws a horizontal
                stub from the Kelvin node outward to provide a hand-
                off point for the gate driver's COM/HS Kelvin trace.
                Set False if the recipe will draw its own Kelvin
                routing.
        """
        super().__init__(sch, x, y)

        # Place FET A (upper)
        self.fet_a = sch.add_symbol(
            mosfet_symbol,
            x,
            y,
            ref_a,
            mosfet_value,
            footprint=mosfet_footprint,
            properties={"Thermal_Rth_JC": "0.5", "Power_Dissipation": "5W"},
        )

        # Place FET B (lower), oriented to flip the source upward so
        # both sources meet at the common-source node.  ``Device:Q_NMOS``
        # with rotation=180 puts the source on top and the drain on the
        # bottom — this is the back-to-back orientation we want.
        b_y = y + fet_spacing
        self.fet_b = sch.add_symbol(
            mosfet_symbol,
            x,
            b_y,
            ref_b,
            mosfet_value,
            rotation=180,
            footprint=mosfet_footprint,
            properties={"Thermal_Rth_JC": "0.5", "Power_Dissipation": "5W"},
        )

        self.components = {
            "Q_A": self.fet_a,
            "Q_B": self.fet_b,
        }

        # Pin positions.  With FET A unrotated and FET B at rotation=180,
        # FET A's source faces down toward FET B's source — exactly the
        # back-to-back topology we need.
        a_drain = self.fet_a.pin_position("D")
        a_source = self.fet_a.pin_position("S")
        a_gate = self.fet_a.pin_position("G")
        b_drain = self.fet_b.pin_position("D")
        b_source = self.fet_b.pin_position("S")
        b_gate = self.fet_b.pin_position("G")

        # Tie the two sources together with an orthogonal Z-route (issue
        # #3347).  ``Device:Q_NMOS`` places the source pin offset +2.54 mm
        # from the symbol origin in library X; with FET B rotated 180°
        # that offset flips to -2.54 mm.  The two source pins therefore
        # sit at *different* X coordinates (typically 5.08 mm apart) — a
        # single ``add_wire(a_source, b_source)`` produced a diagonal
        # segment that KiCad rendered as a ~10° slope and crossed
        # neighbouring rails.
        #
        # Route as three orthogonal segments instead:
        #   1. a_source -> (a_source.x, mid_y)   (vertical down)
        #   2. (a_source.x, mid_y) -> (b_source.x, mid_y)  (horizontal)
        #   3. (b_source.x, mid_y) -> b_source   (vertical to B)
        #
        # The Kelvin junction node sits at the A-side of the horizontal
        # bend so the existing rightward hint stub (and label, if any)
        # remain in their previous positions — preserving the block's
        # public ``SOURCE`` port geometry.
        kelvin_node = (a_source[0], (a_source[1] + b_source[1]) / 2)
        bend_b = (b_source[0], kelvin_node[1])
        sch.add_wire(a_source, kelvin_node)
        if b_source[0] != a_source[0]:
            sch.add_wire(kelvin_node, bend_b)
            sch.add_wire(bend_b, b_source)
        else:
            # Degenerate case: sources happen to share X (e.g. a custom
            # symbol with symmetric source/drain placement).  Skip the
            # zero-length horizontal hop and keep the route a clean
            # single vertical.
            sch.add_wire(kelvin_node, b_source)
        sch.add_junction(kelvin_node[0], kelvin_node[1])

        # Optional Kelvin trace hint — a short stub to the right of the
        # tie node, terminating in a junction.  This gives the recipe a
        # hand-off point to route the dedicated Kelvin trace to the
        # gate driver's COM/HS pin.
        if kelvin_trace_hint:
            hint_end = (kelvin_node[0] + 5.0, kelvin_node[1])
            sch.add_wire(kelvin_node, hint_end)
            kelvin_port = hint_end
        else:
            kelvin_port = kelvin_node

        # Optional Kelvin label
        if kelvin_label is not None:
            sch.add_label(kelvin_label, kelvin_port[0], kelvin_port[1])

        self.ports = {
            "DRAIN_A": a_drain,
            "DRAIN_B": b_drain,
            "GATE_A": a_gate,
            "GATE_B": b_gate,
            "SOURCE": kelvin_port,
        }

        # Metadata for downstream tools
        self.kelvin_node = kelvin_node
        self.mosfet_value = mosfet_value


class UCC27211GateDriver(CircuitBlock):
    """TI UCC27211 half-bridge gate driver with UVLO, Miller-immunity, Kelvin-source ref.

    The UCC27211 is a SOIC-8 half-bridge driver with separate LI/HI
    logic inputs and LO/HO gate outputs, 4A peak drive, integrated
    bootstrap diode + bootstrap node (HB), and a 7.4 V UVLO (under-
    voltage lockout) trip on VDD.  UVLO + ~50 V/ns dV/dt Miller
    immunity are *intrinsic IC properties* — they don't need any
    external parts beyond the standard supply bypass + bootstrap.

    This block places the IC + supply decoupling (bulk + bypass) +
    bootstrap capacitor + bootstrap-to-HS connection.  It exposes the
    Kelvin source reference (the driver's VSS / HS pin pair) as an
    explicit ``KELVIN_SOURCE`` port so the recipe can wire it
    *directly* to the back-to-back FET pair's ``SOURCE`` port — making
    the Kelvin tie an explicit net rather than relying on power-GND
    pour to carry it.

    Failsafe note (softstart rev B):
        The UCC27211 has no dedicated EN pin (per TI datasheet);
        failsafe shutdown is achieved by pulling LI/HI inputs low.
        The block exposes ``failsafe_pulldown_node`` as a reference
        for callers (P2) to wire 2N7002 drains to.  See the orchestrator
        Q8 resolution in issue #3343.

    Ports:
        - VDD: Driver supply input (typically 12 V VGATE rail)
        - VSS: Driver ground reference (NOTE: ties to Kelvin source,
          NOT power GND; see KELVIN_SOURCE below)
        - LI: Low-side input (from MCU)
        - HI: High-side input (from MCU)
        - LO: Low-side gate output
        - HO: High-side gate output
        - HB: Bootstrap capacitor node (top plate of C_BOOT)
        - HS: High-side return / switch node (bottom plate of C_BOOT,
          and tied to the back-to-back pair's common source)
        - KELVIN_SOURCE: Convenience alias for VSS (the explicit
          Kelvin tie point — wire this to the FET pair's SOURCE port)
        - failsafe_pulldown_node: Tuple of (LI_pos, HI_pos) intended
          for failsafe pull-down attachment.  Not a Port; access via
          ``self.failsafe_pulldown_node``.

    Metadata (for downstream tools):
        - ``self.uvlo_trip_v = 7.4``: UVLO trip voltage (datasheet).
        - ``self.peak_drive_a = 4.0``: Peak gate drive current.
        - ``self.dvdt_immunity_v_per_ns = 50``: Miller immunity rating.

    Example:
        from kicad_tools.schematic.blocks import (
            BackToBackFETPair, UCC27211GateDriver,
        )

        # The custom UCC27211 symbol must be registered with the schematic.
        from pathlib import Path
        sch = Schematic(
            "softstart",
            local_symbol_libs=[Path("symbols/softstart_custom.kicad_sym")],
        )

        pair = BackToBackFETPair(sch, x=150, y=80, ref_a="Q1A", ref_b="Q1B")
        drv = UCC27211GateDriver(
            sch, x=110, y=110, ref="U5",
            vgate_net="VGATE",
            li_net="GATE_POS_B", hi_net="GATE_POS_A",
        )
        # Tie driver Kelvin reference to pair common-source
        sch.add_wire(drv.port("KELVIN_SOURCE"), pair.port("SOURCE"))
    """

    UVLO_TRIP_V = 7.4
    PEAK_DRIVE_A = 4.0
    DVDT_IMMUNITY_V_PER_NS = 50

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        ref: str = "U5",
        vgate_net: str = "VGATE",
        kelvin_source_net: str | None = None,
        li_net: str | None = None,
        hi_net: str | None = None,
        lo_net: str | None = None,
        ho_net: str | None = None,
        hb_net: str | None = None,
        hs_net: str | None = None,
        bootstrap_cap_value: str = "100nF",
        vcc_bulk_cap_value: str = "10uF",
        vcc_bypass_cap_value: str = "100nF",
        driver_symbol: str = "softstart_custom:UCC27211",
        driver_footprint: str = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
        cap_ref_prefix: str = "C",
        cap_ref_start: int = 1,
    ):
        """Place a UCC27211 with bootstrap + supply decoupling.

        Args:
            sch: Schematic to add to (must have the UCC27211 symbol
                registered via ``local_symbol_libs`` — see class
                docstring).
            x: X coordinate of the driver IC center.
            y: Y coordinate of the driver IC center.
            ref: Reference designator for the IC.
            vgate_net: Net name for the 12 V driver supply rail.
            kelvin_source_net: Optional net name to label at the
                KELVIN_SOURCE port (also tied to the IC's VSS pin).
                Use the same net name as the back-to-back pair's
                common-source net so the recipe's intent (Kelvin tie)
                is preserved in the netlist.
            li_net, hi_net: Optional net names for the LI/HI input
                pins; when provided, stub-labels are emitted so
                callers (notably the failsafe pull-down circuit) can
                bind to these nets by name.
            lo_net, ho_net: Optional net names for the LO/HO output
                pins.
            hb_net, hs_net: Optional net names for the HB/HS bootstrap
                nodes.
            bootstrap_cap_value: Bootstrap capacitor value (HB ↔ HS).
            vcc_bulk_cap_value: VDD bulk decoupling capacitor.
            vcc_bypass_cap_value: VDD high-frequency bypass capacitor.
            driver_symbol: KiCad lib_id for the UCC27211 symbol.  The
                default ``softstart_custom:UCC27211`` requires the
                schematic to have its ``local_symbol_libs`` pointed at
                ``boards/external/softstart/symbols/softstart_custom.kicad_sym``.
            driver_footprint: SOIC-8 footprint for the IC.
            cap_ref_prefix: Reference prefix for the three caps.
            cap_ref_start: Starting cap reference number.
        """
        super().__init__(sch, x, y)

        # Place the driver IC
        self.driver = sch.add_symbol(
            driver_symbol,
            x,
            y,
            ref,
            "UCC27211",
            footprint=driver_footprint,
        )
        self.components = {"U_DRV": self.driver}

        # Get IC pin positions
        vdd = self.driver.pin_position("4")  # VDD
        vss = self.driver.pin_position("7")  # VSS (Kelvin ref)
        li = self.driver.pin_position("6")
        hi = self.driver.pin_position("5")
        lo = self.driver.pin_position("8")
        ho = self.driver.pin_position("2")
        hb = self.driver.pin_position("1")
        hs = self.driver.pin_position("3")

        # Bootstrap capacitor between HB and HS (right side of IC)
        c_boot_ref = f"{cap_ref_prefix}{cap_ref_start}"
        c_boot_x = x + 20
        c_boot_y = (hb[1] + hs[1]) / 2
        self.c_boot = sch.add_symbol(
            "Device:C",
            c_boot_x,
            c_boot_y,
            c_boot_ref,
            bootstrap_cap_value,
            rotation=90,
        )
        self.components["C_BOOT"] = self.c_boot

        # Wire HB → C_BOOT top, HS → C_BOOT bottom
        boot_top = self.c_boot.pin_position("1")
        boot_bot = self.c_boot.pin_position("2")
        sch.add_wire(hb, (boot_top[0], hb[1]))
        sch.add_wire((boot_top[0], hb[1]), boot_top)
        sch.add_wire(hs, (boot_bot[0], hs[1]))
        sch.add_wire((boot_bot[0], hs[1]), boot_bot)

        # VDD bulk + bypass caps (left side of IC, sharing a return rail)
        c_bulk_ref = f"{cap_ref_prefix}{cap_ref_start + 1}"
        c_bypass_ref = f"{cap_ref_prefix}{cap_ref_start + 2}"
        c_bulk_x = x - 20
        c_bypass_x = x - 10
        c_y = y - 15
        self.c_vcc_bulk = sch.add_symbol(
            "Device:C", c_bulk_x, c_y, c_bulk_ref, vcc_bulk_cap_value, rotation=0
        )
        self.c_vcc_bypass = sch.add_symbol(
            "Device:C", c_bypass_x, c_y, c_bypass_ref, vcc_bypass_cap_value, rotation=0
        )
        self.components["C_VCC_BULK"] = self.c_vcc_bulk
        self.components["C_VCC_BYPASS"] = self.c_vcc_bypass

        # Optional net labels at IC pins (short stubs to land labels on wires)
        STUB = 2.54

        def _stub_label(pin_pos: tuple[float, float], net: str, direction: str = "left"):
            """Emit a short stub + label so ERC sees a named net at the pin."""
            if direction == "left":
                end = (pin_pos[0] - STUB, pin_pos[1])
            elif direction == "right":
                end = (pin_pos[0] + STUB, pin_pos[1])
            elif direction == "up":
                end = (pin_pos[0], pin_pos[1] - STUB)
            else:  # down
                end = (pin_pos[0], pin_pos[1] + STUB)
            sch.add_wire(pin_pos, end)
            sch.add_label(net, end[0], end[1])

        if li_net is not None:
            _stub_label(li, li_net, "left")
        if hi_net is not None:
            _stub_label(hi, hi_net, "left")
        if lo_net is not None:
            _stub_label(lo, lo_net, "right")
        if ho_net is not None:
            _stub_label(ho, ho_net, "right")
        if hb_net is not None:
            _stub_label(hb, hb_net, "right")
        if hs_net is not None:
            _stub_label(hs, hs_net, "right")
        if kelvin_source_net is not None:
            _stub_label(vss, kelvin_source_net, "down")

        # Ports
        self.ports = {
            "VDD": vdd,
            "VSS": vss,
            "LI": li,
            "HI": hi,
            "LO": lo,
            "HO": ho,
            "HB": hb,
            "HS": hs,
            "KELVIN_SOURCE": vss,  # alias — VSS *is* the Kelvin ref pin
        }

        # Failsafe attachment metadata: (LI position, HI position).
        # Per Q8 resolution in issue #3343, the failsafe topology is
        # (a) pull LI/HI low via 2N7002s gated on NRST.  P2 wires
        # 2N7002 drains to these positions.
        self.failsafe_pulldown_node = {
            "LI": li,
            "HI": hi,
        }

        # Metadata for downstream tools
        self.uvlo_trip_v = self.UVLO_TRIP_V
        self.peak_drive_a = self.PEAK_DRIVE_A
        self.dvdt_immunity_v_per_ns = self.DVDT_IMMUNITY_V_PER_NS
