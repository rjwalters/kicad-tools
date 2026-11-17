"""Precharge subsystem block: inrush-limited connect via small N-FET + series resistor.

Used in supercapacitor and large-bus systems to limit inrush current
before the main switching FETs close.  Topology: a small N-channel
MOSFET (e.g., AO3400 SOT-23) sits between the input rail and the
target node, with a series current-limiting resistor (typically a
5W axial through-hole part) sized to bound the precharge current
to a safe value.

This block lives in :mod:`kicad_tools.schematic.blocks.power` because
inrush limiting is a power-supply pattern and is reusable beyond the
softstart board (any DC bus that wants a "soft connect" before the
main FET turns on).
"""

from typing import TYPE_CHECKING

from ..base import CircuitBlock

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic


class PrechargeSubsystem(CircuitBlock):
    """Series-resistor + small N-FET inrush limiter.

    Topology:

        MAIN_DRIVE ──[R_PRECHARGE]──┬── TARGET (= main-FET drain)
                                    │
                              D ────┘
                             [Q]    ← gate driven from MCU GPIO
                              S
                              │
                             GND (or shared return)

    Schematic flow: ``main_drive_node`` enters through the precharge
    resistor, runs through the small N-FET, and exits to the
    ``target_node`` (typically the drain side of the main switching
    FET).  When the MCU asserts ``monitor_pin`` (called ``gate_net``
    on entry; the orchestrator named it ``monitor_pin`` to emphasize
    that this is also the MCU's view of the precharge controller),
    the FET turns on and current ramps the target node up at a rate
    bounded by the series resistor.

    Reference part defaults (softstart rev B BOM):
        - Resistor: 100Ω 5W axial (Vishay PR03 family or generic
          ceramic-bodied axial — JLCPCB stocks these).  P25.40mm
          horizontal mounting.
        - FET: AO3400A (Alpha & Omega 30V 4.7A N-FET, SOT-23-3).
          The stock KiCad symbol ``Transistor_FET:AO3400A`` is used.

    Ports:
        - MAIN_DRIVE: Input node (typically the upstream rail)
        - TARGET: Output node (typically the main-FET drain)
        - MONITOR: MCU GPIO that drives the FET gate (the orchestrator
          term — see class docstring; also accessible via ``port("GATE")``
          for legacy callers)
        - GND: FET source return (usually the local signal ground)
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        main_drive_node: tuple[float, float] | None = None,
        target_node: tuple[float, float] | None = None,
        resistor_value: str = "100R",
        monitor_pin: tuple[float, float] | None = None,
        ref_q: str = "Q5",
        ref_r: str = "R20",
        fet_value: str = "AO3400",
        fet_symbol: str = "Transistor_FET:AO3400A",
        fet_footprint: str = "Package_TO_SOT_SMD:SOT-23",
        resistor_symbol: str = "Device:R",
        resistor_footprint: str = (
            "Resistor_THT:R_Axial_DIN0617_L17.0mm_D6.0mm_P25.40mm_Horizontal"
        ),
        resistor_power: str = "5W",
        component_spacing: float = 20.0,
        monitor_label: str | None = None,
    ):
        """Create a precharge subsystem.

        Args:
            sch: Schematic to add to.
            x: X coordinate of the precharge resistor.
            y: Y coordinate of the resistor / FET row.
            main_drive_node: Optional position tuple for the upstream
                rail; informational only (the block exposes the
                resistor's input pin via ``port("MAIN_DRIVE")`` for
                the recipe to wire externally).
            target_node: Optional position tuple for the downstream
                target node; informational only.
            resistor_value: Value string for the precharge resistor
                (default ``"100R"`` — 100 ohms).
            monitor_pin: Optional position tuple for the MCU GPIO
                output pin that drives the FET gate.  Informational —
                actual wiring is done by the caller.
            ref_q: Reference designator for the FET.
            ref_r: Reference designator for the resistor.
            fet_value: FET part number / value.
            fet_symbol: KiCad symbol for the FET.
            fet_footprint: Footprint for the FET.
            resistor_symbol: KiCad symbol for the resistor.
            resistor_footprint: Footprint for the resistor (default is
                the rev-B BOM's axial 100Ω 5W horizontal-mount part).
            resistor_power: Power rating string (stored as a custom
                property on the resistor — used by thermal/BOM tools).
            component_spacing: Horizontal spacing between the resistor
                center and the FET center (mm).
            monitor_label: Optional net name to label at the FET gate
                pin so ERC sees a named net there.
        """
        super().__init__(sch, x, y)

        # Place the precharge resistor (left side)
        self.r_precharge = sch.add_symbol(
            resistor_symbol,
            x,
            y,
            ref_r,
            resistor_value,
            footprint=resistor_footprint,
            properties={"Power_Rating": resistor_power},
        )

        # Place the precharge FET (right of the resistor)
        fet_x = x + component_spacing
        self.q_precharge = sch.add_symbol(
            fet_symbol,
            fet_x,
            y,
            ref_q,
            fet_value,
            footprint=fet_footprint,
        )

        self.components = {
            "R_PRECHARGE": self.r_precharge,
            "Q_PRECHARGE": self.q_precharge,
        }

        # Wire resistor output → FET drain
        r_out = self.r_precharge.pin_position("2")
        fet_drain = self.q_precharge.pin_position("D")
        sch.add_wire(r_out, (fet_drain[0], r_out[1]))
        sch.add_wire((fet_drain[0], r_out[1]), fet_drain)

        # FET gate (optional label)
        fet_gate = self.q_precharge.pin_position("G")
        if monitor_label is not None:
            STUB = 2.54
            stub_end = (fet_gate[0] - STUB, fet_gate[1])
            sch.add_wire(fet_gate, stub_end)
            sch.add_label(monitor_label, stub_end[0], stub_end[1])

        # Ports
        r_in = self.r_precharge.pin_position("1")
        fet_source = self.q_precharge.pin_position("S")
        self.ports = {
            "MAIN_DRIVE": r_in,
            "TARGET": fet_source,
            "MONITOR": fet_gate,
            "GATE": fet_gate,  # legacy alias
            "GND": fet_source,  # alias — caller decides whether to tie to GND
        }

        # Informational metadata (positions passed in by caller, for
        # downstream layout / autorouter use)
        self.main_drive_node = main_drive_node
        self.target_node = target_node
        self.monitor_pin = monitor_pin
        self.resistor_value = resistor_value
        self.resistor_power = resistor_power
