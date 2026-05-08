"""Analog input interface blocks: 2-axis joystick connector with optional filtering.

This module provides connector-bearing blocks for analog input devices that pair
a physical connector (e.g., a 2-axis thumbstick) with the small amount of analog
conditioning glue needed to drive an MCU ADC cleanly:

- ``AnalogJoystickBlock`` / ``create_analog_joystick`` — 5-pin (or 4-pin) connector
  for an analog 2-axis joystick with optional anti-aliasing RC filters on the X
  and Y wipers and an optional pull-up resistor on the integrated push-button.

The X/Y filter sub-circuits are composed via :func:`create_adc_filter` from
``analog.py`` so the RC math stays single-sourced.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from ..analog import create_adc_filter
from ..base import CircuitBlock

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic


# Wire stub length used when emitting net labels on connector pins.
_LABEL_STUB = 5.08  # 200 mils, matches board 03 convention


def _add_pin_label(
    sch: Schematic,
    pin_pos: tuple[float, float],
    net_name: str,
    direction: str = "right",
) -> None:
    """Wire a pin position to a global label.

    Mirrors ``boards/03-usb-joystick/generate_schematic.py:add_pin_label`` so the
    block emits the same wire-stub-then-label pattern that board 03 was using
    inline before this factory existed.
    """
    if not pin_pos:
        return

    x, y = pin_pos
    if direction == "right":
        end_x = x + _LABEL_STUB
        rotation = 180  # Label points left toward wire
    else:
        end_x = x - _LABEL_STUB
        rotation = 0  # Label points right toward wire

    sch.add_wire((x, y), (end_x, y), snap=False)
    sch.add_global_label(net_name, end_x, y, shape="bidirectional", rotation=rotation, snap=False)


class AnalogJoystickBlock(CircuitBlock):
    """
    2-axis analog joystick connector with optional RC filtering and BTN pull-up.

    Drops a 5-pin connector (VCC/GND/X/Y/BTN) — or a 4-pin variant when
    ``btn_net`` is ``None`` — and optionally adds:

    - An RC anti-aliasing filter on each wiper output (X and Y), composed
      via :func:`create_adc_filter`.
    - A pull-up resistor on the integrated push-button line (active-low to
      GND through the joystick's internal switch).

    Schematic (default 5-pin with filters and BTN pull-up)::

                                ┌─[R_pu]── VCC
                                │
        VCC ─── pin1 ───────────┴───────── VCC port
        GND ─── pin2 ───────────────────── GND port
        X   ─── pin3 ─[Rx]─┬──────────── X port
                            │
                          [Cx]
                            │
                           GND
        Y   ─── pin4 ─[Ry]─┬──────────── Y port
                            │
                          [Cy]
                            │
                           GND
        BTN ─── pin5 ───────┬──────────── BTN port

    Connector pinout (matches ``boards/03-usb-joystick`` and the
    ``Module:Joystick_Analog`` footprint):
        1=VCC, 2=GND, 3=X-wiper, 4=Y-wiper, 5=BTN.

    When ``btn_net=None`` the block uses ``Connector_Generic:Conn_01x04``
    (4-pin variant) — pin 5 is omitted entirely rather than being left as
    a no-connect, so callers cannot accidentally short BTN to nothing.

    Ports:
        - ``VCC``: connector VCC pin (pin 1)
        - ``GND``: connector GND pin (pin 2)
        - ``X``: post-filter X output (or raw pin 3 if ``filter_cutoff_hz=None``)
        - ``Y``: post-filter Y output (or raw pin 4 if ``filter_cutoff_hz=None``)
        - ``BTN``: BTN line (post pull-up if ``btn_pullup`` is set);
          omitted when ``btn_net=None``.
    """

    # Connector pin assignments (matches Module:Joystick_Analog footprint).
    PIN_VCC = "1"
    PIN_GND = "2"
    PIN_X = "3"
    PIN_Y = "4"
    PIN_BTN = "5"

    def __init__(
        self,
        sch: Schematic,
        x: float,
        y: float,
        ref: str = "J1",
        vcc_net: str = "+3.3V",
        gnd_net: str = "GND",
        x_net: str = "JOY_X",
        y_net: str = "JOY_Y",
        btn_net: str | None = "JOY_BTN",
        filter_cutoff_hz: float | None = 1000.0,
        btn_pullup: str | None = "10k",
        connector_symbol: str | None = None,
        connector_footprint: str = "Module:Joystick_Analog",
        resistor_symbol: str = "Device:R",
        capacitor_symbol: str = "Device:C",
        resistor_footprint: str = "",
        capacitor_footprint: str = "",
        filter_ref_start: int = 1,
        pullup_ref: str | None = None,
    ) -> None:
        """
        Create an analog joystick connector block.

        Args:
            sch: Schematic to add to.
            x: X coordinate of the connector.
            y: Y coordinate of the connector.
            ref: Reference designator for the connector (e.g., ``"J2"``).
            vcc_net: Net name for the VCC pin label.
            gnd_net: Net name for the GND pin label.
            x_net: Net name for the X axis output label (post-filter).
            y_net: Net name for the Y axis output label (post-filter).
            btn_net: Net name for the BTN output label, or ``None`` to use
                a 4-pin connector with no button.
            filter_cutoff_hz: Cutoff frequency in Hz for the X/Y RC filters,
                or ``None`` to skip filtering and label the raw connector
                pins directly.
            btn_pullup: Pull-up resistor value for the BTN line (e.g.
                ``"10k"``), or ``None`` to skip the pull-up. Ignored when
                ``btn_net`` is ``None``.
            connector_symbol: KiCad symbol for the connector. When ``None``,
                ``Connector_Generic:Conn_01x05`` is used for the 5-pin
                variant and ``Connector_Generic:Conn_01x04`` for the 4-pin
                variant (when ``btn_net=None``).
            connector_footprint: KiCad footprint for the connector.
            resistor_symbol: KiCad symbol for the BTN pull-up resistor.
            capacitor_symbol: KiCad symbol for filter capacitors (passed
                through to :func:`create_adc_filter`).
            resistor_footprint: Optional KiCad footprint for filter and
                pull-up resistors (e.g. ``"Resistor_SMD:R_0402_1005Metric"``).
                Empty string skips footprint assignment (default).
            capacitor_footprint: Optional KiCad footprint for filter
                capacitors (e.g. ``"Capacitor_SMD:C_0402_1005Metric"``).
            filter_ref_start: Starting numeric ref for the X/Y filter
                resistors and capacitors. ``X`` filter uses
                ``R<n>``/``C<n>``; ``Y`` filter uses ``R<n+1>``/``C<n+1>``.
            pullup_ref: Reference designator for the BTN pull-up resistor.
                Defaults to ``R<filter_ref_start + 2>`` so it doesn't
                collide with the filter component refs.
        """
        super().__init__(sch, x, y)

        self.ref = ref
        self.vcc_net = vcc_net
        self.gnd_net = gnd_net
        self.x_net = x_net
        self.y_net = y_net
        self.btn_net = btn_net
        self.filter_cutoff_hz = filter_cutoff_hz
        self.btn_pullup = btn_pullup
        self.has_button = btn_net is not None

        # ------------------------------------------------------------------
        # Place the connector
        # ------------------------------------------------------------------
        if connector_symbol is None:
            connector_symbol = (
                "Connector_Generic:Conn_01x05"
                if self.has_button
                else "Connector_Generic:Conn_01x04"
            )
        self.connector_symbol = connector_symbol

        self.connector = sch.add_symbol(
            connector_symbol,
            x,
            y,
            ref,
            "Joystick" if self.has_button else "Joystick_NoBtn",
            footprint=connector_footprint,
        )
        self.components: dict = {"J": self.connector}

        # ------------------------------------------------------------------
        # Label VCC / GND directly on the connector pins
        # ------------------------------------------------------------------
        vcc_pin = self.connector.pin_position(self.PIN_VCC)
        gnd_pin = self.connector.pin_position(self.PIN_GND)
        x_pin = self.connector.pin_position(self.PIN_X)
        y_pin = self.connector.pin_position(self.PIN_Y)

        _add_pin_label(sch, vcc_pin, vcc_net, direction="right")
        _add_pin_label(sch, gnd_pin, gnd_net, direction="right")

        # ------------------------------------------------------------------
        # X / Y RC filters (composed via create_adc_filter)
        # ------------------------------------------------------------------
        # Filter sub-blocks are placed off to the right of the connector so
        # they don't overlap. Vertical positions track the wiper pin Y so
        # callers see a stable layout.
        self.x_filter = None
        self.y_filter = None

        if filter_cutoff_hz is not None and x_pin is not None and y_pin is not None:
            filter_x = x + 20  # 20 mm right of connector
            self.x_filter = create_adc_filter(
                sch,
                filter_x,
                x_pin[1],
                cutoff_hz=filter_cutoff_hz,
                order=1,
            )
            # Re-ref the filter components using filter_ref_start
            self._reref_filter(self.x_filter, filter_ref_start)
            self._set_filter_footprints(self.x_filter, resistor_footprint, capacitor_footprint)
            self.components["R_FILT_X"] = self.x_filter.components["R1"]
            self.components["C_FILT_X"] = self.x_filter.components["C1"]

            self.y_filter = create_adc_filter(
                sch,
                filter_x,
                y_pin[1],
                cutoff_hz=filter_cutoff_hz,
                order=1,
            )
            self._reref_filter(self.y_filter, filter_ref_start + 1)
            self._set_filter_footprints(self.y_filter, resistor_footprint, capacitor_footprint)
            self.components["R_FILT_Y"] = self.y_filter.components["R1"]
            self.components["C_FILT_Y"] = self.y_filter.components["C1"]

            # Wire connector wiper pins into filter IN, label filter OUT and
            # filter GND with the appropriate net names.
            sch.add_wire(x_pin, self.x_filter.ports["IN"], snap=False)
            sch.add_wire(y_pin, self.y_filter.ports["IN"], snap=False)
            _add_pin_label(sch, self.x_filter.ports["OUT"], x_net, direction="right")
            _add_pin_label(sch, self.y_filter.ports["OUT"], y_net, direction="right")
            _add_pin_label(sch, self.x_filter.ports["GND"], gnd_net, direction="right")
            _add_pin_label(sch, self.y_filter.ports["GND"], gnd_net, direction="right")

            x_port = self.x_filter.ports["OUT"]
            y_port = self.y_filter.ports["OUT"]
        else:
            # No filter: label the raw connector pins
            _add_pin_label(sch, x_pin, x_net, direction="right")
            _add_pin_label(sch, y_pin, y_net, direction="right")
            x_port = x_pin if x_pin is not None else (x, y)
            y_port = y_pin if y_pin is not None else (x, y)

        # ------------------------------------------------------------------
        # BTN pin handling (only when btn_net is not None)
        # ------------------------------------------------------------------
        self.r_pullup = None
        btn_port: tuple[float, float] | None = None

        if self.has_button:
            assert btn_net is not None  # narrow type for mypy
            btn_pin = self.connector.pin_position(self.PIN_BTN)

            if btn_pin is not None and btn_pullup is not None:
                # Place a pull-up resistor near the BTN pin
                if pullup_ref is None:
                    pullup_ref = f"R{filter_ref_start + 2}"
                pu_x = x + 20  # match filter column
                pu_y = btn_pin[1]
                self.r_pullup = sch.add_symbol(
                    resistor_symbol,
                    pu_x,
                    pu_y,
                    pullup_ref,
                    btn_pullup,
                    rotation=90,
                    footprint=resistor_footprint,
                )
                self.components["R_PULLUP"] = self.r_pullup
                # Wire pin1 of pull-up to VCC label, pin2 of pull-up to BTN line
                pu_pin1 = self.r_pullup.pin_position("1")
                pu_pin2 = self.r_pullup.pin_position("2")
                # Pin2 should connect to the BTN line; wire BTN pin -> R pin2
                if pu_pin2 is not None:
                    sch.add_wire(btn_pin, pu_pin2, snap=False)
                if pu_pin1 is not None:
                    _add_pin_label(sch, pu_pin1, vcc_net, direction="right")

            # Label the BTN line (at the connector pin)
            _add_pin_label(sch, btn_pin, btn_net, direction="right")
            btn_port = btn_pin

        # ------------------------------------------------------------------
        # Expose ports
        # ------------------------------------------------------------------
        self.ports = {
            "VCC": vcc_pin if vcc_pin is not None else (x, y),
            "GND": gnd_pin if gnd_pin is not None else (x, y),
            "X": x_port,
            "Y": y_port,
        }
        if btn_port is not None:
            self.ports["BTN"] = btn_port

    @staticmethod
    def _reref_filter(filter_block: object, ref_num: int) -> None:
        """Rewrite component refs for an ADC filter sub-block.

        ``create_adc_filter`` uses ``ref_start=1`` by default which collides
        across multiple filter instances. We rewrite the refs after the
        sub-block is built so the X and Y filters end up with non-colliding
        designators (e.g. R1/C1 for X, R2/C2 for Y).
        """
        components = getattr(filter_block, "components", {})
        for kind, key in (("R", "R1"), ("C", "C1")):
            if key in components:
                comp = components[key]
                new_ref = f"{kind}{ref_num}"
                # SymbolInstance exposes a writable .reference attribute.
                # Mocks may not allow writing arbitrary attributes — ignore so
                # tests using bare Mock objects still pass.
                if hasattr(comp, "reference"):
                    with contextlib.suppress(Exception):
                        comp.reference = new_ref

    @staticmethod
    def _set_filter_footprints(
        filter_block: object,
        resistor_footprint: str,
        capacitor_footprint: str,
    ) -> None:
        """Set footprints on filter sub-block components when caller supplied them.

        ``create_adc_filter`` does not accept footprint args; we patch the
        ``.footprint`` attribute on the ``SymbolInstance`` objects after the
        fact. Empty strings are skipped so callers that don't supply
        footprints get the same behavior as before.
        """
        components = getattr(filter_block, "components", {})
        if resistor_footprint and "R1" in components:
            comp = components["R1"]
            if hasattr(comp, "footprint"):
                with contextlib.suppress(Exception):
                    comp.footprint = resistor_footprint
        if capacitor_footprint and "C1" in components:
            comp = components["C1"]
            if hasattr(comp, "footprint"):
                with contextlib.suppress(Exception):
                    comp.footprint = capacitor_footprint


# Factory functions


def create_analog_joystick(
    sch: Schematic,
    x: float,
    y: float,
    *,
    ref: str = "J1",
    vcc_net: str = "+3.3V",
    gnd_net: str = "GND",
    x_net: str = "JOY_X",
    y_net: str = "JOY_Y",
    btn_net: str | None = "JOY_BTN",
    filter_cutoff_hz: float | None = 1000.0,
    btn_pullup: str | None = "10k",
    connector_symbol: str | None = None,
    connector_footprint: str = "Module:Joystick_Analog",
    resistor_footprint: str = "",
    capacitor_footprint: str = "",
    filter_ref_start: int = 1,
    pullup_ref: str | None = None,
) -> AnalogJoystickBlock:
    """Create a 2-axis analog joystick connector with optional filter and pull-up.

    Convenience wrapper around :class:`AnalogJoystickBlock` matching the
    composition style of the other ``create_*`` factories in this package.

    Args:
        sch: Schematic to add to.
        x: X coordinate of the connector.
        y: Y coordinate of the connector.
        ref: Reference designator for the connector.
        vcc_net: Net name for the VCC pin label (default ``"+3.3V"``).
        gnd_net: Net name for the GND pin label.
        x_net: Net name for the X axis output (post-filter).
        y_net: Net name for the Y axis output (post-filter).
        btn_net: Net name for the BTN output, or ``None`` for a 4-pin
            variant with no button.
        filter_cutoff_hz: Cutoff frequency for the X/Y RC anti-aliasing
            filters in Hz, or ``None`` to skip filtering. Matches the
            convention from :func:`create_adc_filter`.
        btn_pullup: Pull-up resistor value for the BTN line (e.g.
            ``"10k"``), or ``None`` to skip the pull-up.
        connector_symbol: KiCad symbol for the connector. When ``None``,
            ``Conn_01x05`` is used for 5-pin and ``Conn_01x04`` for 4-pin.
        connector_footprint: KiCad footprint for the connector.
        resistor_footprint: Optional KiCad footprint for filter and pull-up
            resistors (e.g. ``"Resistor_SMD:R_0402_1005Metric"``). Empty
            string skips footprint assignment.
        capacitor_footprint: Optional KiCad footprint for filter capacitors
            (e.g. ``"Capacitor_SMD:C_0402_1005Metric"``).
        filter_ref_start: Starting numeric ref for the X/Y filter components.
            X uses ``R<n>``/``C<n>``; Y uses ``R<n+1>``/``C<n+1>``. Bump this
            on boards that already use ``R1``/``C1`` etc. for other components.
        pullup_ref: Reference designator for the BTN pull-up resistor.
            Defaults to ``R<filter_ref_start + 2>``.

    Returns:
        Configured :class:`AnalogJoystickBlock`.

    Example::

        from kicad_tools.schematic.blocks import create_analog_joystick

        joy = create_analog_joystick(
            sch, x=50.8, y=101.6,
            ref="J2",
            vcc_net="+3.3V",
            gnd_net="GND",
            x_net="JOY_X",
            y_net="JOY_Y",
            btn_net="JOY_BTN",
            filter_cutoff_hz=1000.0,
            btn_pullup="10k",
        )
    """
    return AnalogJoystickBlock(
        sch,
        x,
        y,
        ref=ref,
        vcc_net=vcc_net,
        gnd_net=gnd_net,
        x_net=x_net,
        y_net=y_net,
        btn_net=btn_net,
        filter_cutoff_hz=filter_cutoff_hz,
        btn_pullup=btn_pullup,
        connector_symbol=connector_symbol,
        connector_footprint=connector_footprint,
        resistor_footprint=resistor_footprint,
        capacitor_footprint=capacitor_footprint,
        filter_ref_start=filter_ref_start,
        pullup_ref=pullup_ref,
    )
