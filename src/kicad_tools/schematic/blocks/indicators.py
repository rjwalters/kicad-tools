"""Indicator circuit blocks: LEDs."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from .base import CircuitBlock

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic


class LEDIndicator(CircuitBlock):
    """
    LED with current-limiting resistor.

    Schematic:
        VCC ──┬── [LED] ── [R] ── GND
              │
            (anode)

    Ports:
        - VCC: Power input (top of LED)
        - GND: Ground (bottom of resistor)
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        ref_prefix: str = "D",
        label: str = "LED",
        resistor_value: str = "330R",
        led_symbol: str = "Device:LED",
        resistor_symbol: str = "Device:R",
        vertical: bool = True,
        led_footprint: str = "",
        resistor_footprint: str = "",
        resistor_ref: str = "",
    ):
        """
        Create an LED indicator.

        Args:
            sch: Schematic to add to
            x: X coordinate
            y: Y coordinate (of LED center)
            ref_prefix: Reference designator prefix (e.g., "D1" or just "D")
            label: Value label for LED (e.g., "PWR", "ACT")
            resistor_value: Resistor value string
            led_symbol: KiCad symbol for LED
            resistor_symbol: KiCad symbol for resistor
            vertical: If True, LED is vertical (rotated 90°)
            led_footprint: Footprint for LED (e.g., "LED_SMD:LED_0805_2012Metric")
            resistor_footprint: Footprint for resistor (e.g., "Resistor_SMD:R_0805_2012Metric")
            resistor_ref: Explicit resistor reference (e.g., "R12"). If empty,
                derived from ref_prefix digit (e.g., "D2" -> "R2").
        """
        super().__init__(sch, x, y)

        # Parse reference prefix
        d_ref = ref_prefix if ref_prefix[-1].isdigit() else ref_prefix
        if resistor_ref:
            r_ref = resistor_ref
        else:
            r_num = ref_prefix[-1] if ref_prefix[-1].isdigit() else "1"
            r_ref = f"R{r_num}"

        # Component spacing
        led_resistor_spacing = 15  # mm between LED and resistor centers

        # Place LED
        rotation = 90 if vertical else 0
        self.led = sch.add_symbol(
            led_symbol, x, y, d_ref, label, rotation=rotation, footprint=led_footprint
        )

        # Place resistor below LED (if vertical)
        if vertical:
            r_y = y + led_resistor_spacing
        else:
            r_y = y
        self.resistor = sch.add_symbol(
            resistor_symbol, x, r_y, r_ref, resistor_value, footprint=resistor_footprint
        )

        self.components = {"LED": self.led, "R": self.resistor}

        # Wire LED cathode to resistor
        led_cathode = self.led.pin_position("K")
        r_pin1 = self.resistor.pin_position("1")
        sch.add_wire(led_cathode, r_pin1)

        # Define ports
        led_anode = self.led.pin_position("A")
        r_pin2 = self.resistor.pin_position("2")

        self.ports = {
            "VCC": led_anode,
            "GND": r_pin2,
        }

    def connect_to_rails(self, vcc_rail_y: float, gnd_rail_y: float, add_junctions: bool = True):
        """
        Connect LED to power rails.

        Args:
            vcc_rail_y: Y coordinate of VCC rail
            gnd_rail_y: Y coordinate of GND rail
            add_junctions: Whether to add junction markers
        """
        sch = self.schematic
        vcc_pos = self.ports["VCC"]
        gnd_pos = self.ports["GND"]

        # Connect anode to VCC rail
        sch.add_wire(vcc_pos, (vcc_pos[0], vcc_rail_y), warn_on_collision=False)

        # Connect resistor to GND rail
        sch.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y), warn_on_collision=False)

        if add_junctions:
            sch.add_junction(vcc_pos[0], vcc_rail_y)
            sch.add_junction(gnd_pos[0], gnd_rail_y)


# Factory functions


def create_power_led(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "D1",
) -> LEDIndicator:
    """Create a power indicator LED (green, 330R)."""
    return LEDIndicator(sch, x, y, ref_prefix=ref, label="PWR", resistor_value="330R")


def create_status_led(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "D2",
) -> LEDIndicator:
    """Create a status/debug LED (generic, 330R)."""
    return LEDIndicator(sch, x, y, ref_prefix=ref, label="STATUS", resistor_value="330R")


# ---------------------------------------------------------------------------
# Charlieplex matrix
# ---------------------------------------------------------------------------


# Default labels for pins in a charlieplex matrix (extended on demand).
_DEFAULT_PIN_LABELS = [
    "A",
    "B",
    "C",
    "D",
    "E",
    "F",
    "G",
    "H",
    "I",
    "J",
    "K",
    "L",
    "M",
    "N",
    "O",
    "P",
    "Q",
    "R",
    "S",
    "T",
    "U",
    "V",
    "W",
    "X",
    "Y",
    "Z",
]


def _default_pin_labels(pin_count: int) -> list[str]:
    """Return single-letter pin labels (A, B, ...) extended via A1, B1, ... if needed."""
    if pin_count <= len(_DEFAULT_PIN_LABELS):
        return list(_DEFAULT_PIN_LABELS[:pin_count])
    # Fall back to ordinal labels for very large matrices.
    return [f"P{i}" for i in range(pin_count)]


def _validate_led_pairs(
    pin_count: int,
    led_pairs: Sequence[tuple[int, int]],
) -> None:
    """Validate the led_pairs list against pin_count.

    Raises:
        ValueError: if any pair is invalid (self-pair, out-of-range, duplicate).
    """
    if pin_count < 2:
        raise ValueError(f"pin_count must be >= 2, got {pin_count}")

    seen: set[tuple[int, int]] = set()
    for idx, pair in enumerate(led_pairs):
        if len(pair) != 2:
            raise ValueError(
                f"led_pairs[{idx}] must be a 2-tuple (anode_idx, cathode_idx), got {pair!r}"
            )
        anode, cathode = pair
        if not (isinstance(anode, int) and isinstance(cathode, int)):
            raise ValueError(f"led_pairs[{idx}] indices must be int, got ({anode!r}, {cathode!r})")
        if anode == cathode:
            raise ValueError(
                f"led_pairs[{idx}] is a self-pair ({anode}, {cathode}); "
                "anode and cathode indices must differ"
            )
        if not (0 <= anode < pin_count) or not (0 <= cathode < pin_count):
            raise ValueError(
                f"led_pairs[{idx}] = ({anode}, {cathode}) has index outside range(0, {pin_count})"
            )
        key = (anode, cathode)
        if key in seen:
            raise ValueError(
                f"led_pairs[{idx}] duplicates pair ({anode}, {cathode}); "
                "each ordered pair may appear at most once"
            )
        seen.add(key)


class CharlieplexMatrix(CircuitBlock):
    """A charlieplexed LED matrix driven by N GPIO pins.

    Charlieplex topology drives up to ``N*(N-1)`` LEDs from N tri-state pins.
    Each pin connects through a current-limiting resistor to a shared internal
    node; LEDs are placed between pairs of these nodes (in both polarities,
    or only some, as the designer chooses). To light a particular LED, drive
    its anode-side pin HIGH, its cathode-side pin LOW, and leave all others
    in high-impedance.

    The factory takes an explicit list of ``(anode_pin_idx, cathode_pin_idx)``
    pairs — it does **not** try to enumerate them from a (rows, cols) grid,
    because the choice of which subset of N(N-1) ordered pairs to populate
    depends on layout/routing convenience and is design-specific. See
    :func:`charlieplex_pairs_for_grid` for an opinionated default mapping
    you can use as a starting point.

    Schematic style:
        Each pin's resistor connects ``LINE_<label>`` (the GPIO net) to
        ``NODE_<label>`` (the shared LED-side net) via short wire stubs and
        global labels. Each LED's anode and cathode also use wire stubs +
        global labels naming the two ``NODE_<label>`` nets.

    Ports:
        - ``LINE_<label>``: one port per GPIO (resistor pin 1 side), e.g.
          ``LINE_A``, ``LINE_B``, ...

    Components:
        - ``D{i}`` for each LED (anchor ``led_ref_start..``)
        - ``R{i}`` for each resistor (anchor ``resistor_ref_start..``)
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        *,
        pin_count: int,
        led_pairs: Sequence[tuple[int, int]],
        pin_labels: Sequence[str] | None = None,
        led_ref_start: int = 1,
        resistor_ref_start: int = 1,
        led_value: str = "LED",
        led_symbol: str = "Device:LED",
        led_footprint: str = "LED_SMD:LED_0805_2012Metric",
        resistor_value: str = "330R",
        resistor_symbol: str = "Device:R",
        resistor_footprint: str = "",
        led_grid_cols: int | None = None,
        led_spacing: tuple[float, float] = (25.4, 25.4),
        resistor_spacing: float = 12.7,
        resistor_origin: tuple[float, float] | None = None,
        wire_stub: float = 5.08,
    ) -> None:
        """Create a charlieplex matrix.

        Args:
            sch: Schematic to add components to.
            x: X coordinate of the LED grid origin (top-left LED).
            y: Y coordinate of the LED grid origin (top-left LED).
            pin_count: Number of GPIO pins driving the matrix (N).
            led_pairs: Sequence of ``(anode_pin_idx, cathode_pin_idx)`` ordered
                pairs, each with both indices in ``range(pin_count)`` and
                ``anode != cathode``. Duplicates are not allowed. The factory
                emits exactly ``len(led_pairs)`` LEDs in the order given.
            pin_labels: Per-pin label string used in net names
                (``LINE_<label>``, ``NODE_<label>``) and as a port suffix.
                Defaults to ``["A", "B", "C", ...]`` of length ``pin_count``.
            led_ref_start: First LED reference number (default ``1`` -> ``D1``).
            resistor_ref_start: First resistor reference number
                (default ``1`` -> ``R1``).
            led_value: Value field for LED symbols.
            led_symbol: KiCad library symbol for LEDs.
            led_footprint: Footprint for LED symbols.
            resistor_value: Value field for resistor symbols (e.g. ``"330R"``).
            resistor_symbol: KiCad library symbol for resistors.
            resistor_footprint: Footprint for resistor symbols. Empty string
                triggers ``auto_footprint=True`` so the schematic profile picks
                an appropriate footprint based on value.
            led_grid_cols: Number of columns in the LED grid. Defaults to
                ``ceil(sqrt(len(led_pairs)))``.
            led_spacing: ``(dx, dy)`` between LED centers (mm).
            resistor_spacing: Vertical spacing between resistor centers (mm).
            resistor_origin: Top-left position of the resistor column. Defaults
                to ``(x - 50.8, y + resistor_spacing)`` so resistors sit to the
                left of the LED grid (matching board 02's existing layout).
            wire_stub: Length of wire stub between each pin and its global
                label (mm).

        Raises:
            ValueError: If ``led_pairs`` contains a self-pair, an out-of-range
                index, or duplicates; or if ``pin_count < 2``; or if
                ``pin_labels`` is given with the wrong length.
        """
        super().__init__(sch, x, y)

        # ---- Validate inputs ------------------------------------------------
        _validate_led_pairs(pin_count, led_pairs)

        if pin_labels is None:
            pin_labels = _default_pin_labels(pin_count)
        else:
            pin_labels = list(pin_labels)
            if len(pin_labels) != pin_count:
                raise ValueError(
                    f"pin_labels must have length pin_count={pin_count}, got {len(pin_labels)}"
                )

        self.pin_count: int = pin_count
        self.pin_labels: list[str] = list(pin_labels)
        self.led_pairs: list[tuple[int, int]] = [tuple(p) for p in led_pairs]

        # ---- Layout defaults ------------------------------------------------
        if led_grid_cols is None:
            # ceil(sqrt(N)) so a 9-LED set goes into a 3x3 grid.
            from math import ceil, sqrt

            led_grid_cols = max(1, ceil(sqrt(len(self.led_pairs))))

        if resistor_origin is None:
            resistor_origin = (x - 50.8, y + resistor_spacing)

        # Effective resistor footprint policy: empty -> auto.
        use_auto_resistor_footprint = resistor_footprint == ""

        # ---- Place resistors (one per pin) ---------------------------------
        rx, ry = resistor_origin
        self._resistor_refs: list[str] = []
        for i, label in enumerate(self.pin_labels):
            r_ref = f"R{resistor_ref_start + i}"
            self._resistor_refs.append(r_ref)

            r_x = rx
            r_y = ry + i * resistor_spacing
            if use_auto_resistor_footprint:
                r = sch.add_symbol(
                    resistor_symbol,
                    x=r_x,
                    y=r_y,
                    ref=r_ref,
                    value=resistor_value,
                    auto_footprint=True,
                )
            else:
                r = sch.add_symbol(
                    resistor_symbol,
                    x=r_x,
                    y=r_y,
                    ref=r_ref,
                    value=resistor_value,
                    footprint=resistor_footprint,
                )

            self.components[r_ref] = r

            # Wire stubs + global labels at both resistor pins.
            line_net = f"LINE_{label}"
            node_net = f"NODE_{label}"
            self._add_pin_label(r.pin_position("1"), line_net, direction="left", stub=wire_stub)
            self._add_pin_label(r.pin_position("2"), node_net, direction="right", stub=wire_stub)

        # ---- Place LEDs -----------------------------------------------------
        led_dx, led_dy = led_spacing
        self._led_refs: list[str] = []
        for i, (anode_idx, cathode_idx) in enumerate(self.led_pairs):
            row = i // led_grid_cols
            col = i % led_grid_cols
            d_ref = f"D{led_ref_start + i}"
            self._led_refs.append(d_ref)

            led_x = x + col * led_dx
            led_y = y + row * led_dy

            led = sch.add_symbol(
                led_symbol,
                x=led_x,
                y=led_y,
                ref=d_ref,
                value=led_value,
                footprint=led_footprint,
            )
            self.components[d_ref] = led

            anode_label = self.pin_labels[anode_idx]
            cathode_label = self.pin_labels[cathode_idx]
            anode_net = f"NODE_{anode_label}"
            cathode_net = f"NODE_{cathode_label}"

            # LED pins: pin 1 = cathode (K), pin 2 = anode (A) in Device:LED.
            self._add_pin_label(
                led.pin_position("1"), cathode_net, direction="left", stub=wire_stub
            )
            self._add_pin_label(led.pin_position("2"), anode_net, direction="right", stub=wire_stub)

        # ---- Define ports ---------------------------------------------------
        # One LINE_<label> port per pin, located at resistor pin 1 (after the
        # wire stub — i.e. at the global-label end). Callers wire MCU pins to
        # these ports either directly or via a matching global label.
        for i, label in enumerate(self.pin_labels):
            r = self.components[self._resistor_refs[i]]
            pin1 = r.pin_position("1")
            if pin1 is not None:
                # Port is at the global-label end of the wire stub
                # (left of the resistor pin).
                self.ports[f"LINE_{label}"] = (pin1[0] - wire_stub, pin1[1])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _add_pin_label(
        self,
        pin_pos: tuple[float, float] | None,
        net_name: str,
        direction: str,
        stub: float,
    ) -> None:
        """Add a wire stub from a pin to a global label.

        Args:
            pin_pos: ``(x, y)`` of the pin (or ``None`` to skip).
            net_name: Net/label name.
            direction: ``"left"`` or ``"right"`` — which way the stub extends.
            stub: Stub length in mm.
        """
        if pin_pos is None:
            return
        sch = self.schematic
        px, py = pin_pos
        if direction == "right":
            end_x = px + stub
            rotation = 180
        else:
            end_x = px - stub
            rotation = 0
        sch.add_wire((px, py), (end_x, py))
        sch.add_global_label(net_name, end_x, py, shape="bidirectional", rotation=rotation)


def create_charlieplex_matrix(
    sch: "Schematic",
    x: float,
    y: float,
    *,
    pin_count: int,
    led_pairs: Sequence[tuple[int, int]],
    pin_labels: Sequence[str] | None = None,
    led_ref_start: int = 1,
    resistor_ref_start: int = 1,
    led_value: str = "LED",
    led_symbol: str = "Device:LED",
    led_footprint: str = "LED_SMD:LED_0805_2012Metric",
    resistor_value: str = "330R",
    resistor_symbol: str = "Device:R",
    resistor_footprint: str = "",
    led_grid_cols: int | None = None,
    led_spacing: tuple[float, float] = (25.4, 25.4),
    resistor_spacing: float = 12.7,
    resistor_origin: tuple[float, float] | None = None,
    wire_stub: float = 5.08,
) -> CharlieplexMatrix:
    """Factory for a :class:`CharlieplexMatrix`.

    See :class:`CharlieplexMatrix` for argument documentation. This is a
    thin wrapper that mirrors the style of :func:`create_power_led` and
    related factories in this module.

    Returns:
        The created :class:`CharlieplexMatrix` block.
    """
    return CharlieplexMatrix(
        sch,
        x,
        y,
        pin_count=pin_count,
        led_pairs=led_pairs,
        pin_labels=pin_labels,
        led_ref_start=led_ref_start,
        resistor_ref_start=resistor_ref_start,
        led_value=led_value,
        led_symbol=led_symbol,
        led_footprint=led_footprint,
        resistor_value=resistor_value,
        resistor_symbol=resistor_symbol,
        resistor_footprint=resistor_footprint,
        led_grid_cols=led_grid_cols,
        led_spacing=led_spacing,
        resistor_spacing=resistor_spacing,
        resistor_origin=resistor_origin,
        wire_stub=wire_stub,
    )


def charlieplex_pairs_for_grid(
    rows: int,
    cols: int,
    pin_count: int | None = None,
) -> tuple[list[tuple[int, int]], int]:
    """Return ``(led_pairs, suggested_pin_count)`` covering ``rows*cols`` LEDs.

    Picks the smallest ``N`` where ``N*(N-1) >= rows*cols``, then enumerates
    ordered pairs ``(i, j)`` for ``i != j`` in a deterministic order — first
    upper-triangle ``i < j`` (ascending by ``i`` then ``j``), then
    lower-triangle ``i > j`` (ascending by ``j`` then ``i``). This pattern
    matches board 02's mapping for a 3x3 grid (9 of 12 ordered pairs).

    The output is a starting point; designers may reorder pairs to optimize
    routing, swap polarities, or substitute different ordered pairs.

    Args:
        rows: Number of LED rows.
        cols: Number of LED columns.
        pin_count: Optional explicit pin count. If given, must satisfy
            ``pin_count * (pin_count - 1) >= rows * cols``.

    Returns:
        ``(led_pairs, suggested_pin_count)``.

    Raises:
        ValueError: If ``rows`` or ``cols`` is non-positive, or if the
            explicit ``pin_count`` is too small for ``rows*cols`` LEDs.
    """
    if rows <= 0 or cols <= 0:
        raise ValueError(f"rows and cols must be positive, got rows={rows}, cols={cols}")

    total = rows * cols

    # Smallest N where N*(N-1) >= total.
    n = 2
    while n * (n - 1) < total:
        n += 1

    if pin_count is not None:
        if pin_count * (pin_count - 1) < total:
            raise ValueError(
                f"pin_count={pin_count} can drive at most "
                f"{pin_count * (pin_count - 1)} LEDs via charlieplex topology, "
                f"need {total} for a {rows}x{cols} grid (smallest feasible N is {n})"
            )
        n = pin_count

    # Enumerate ordered pairs:
    #   First the "forward" direction for each unordered pair (i < j),
    #   then the "reverse" direction (i > j), each in row-major order.
    # This matches the board-02 ordering for the 3x3 case:
    #   (0,1),(1,0),(0,2),(2,0),(0,3),(3,0),(1,2),(2,1),(1,3)
    # ... by interleaving (i,j),(j,i) per unordered pair (i<j).
    pairs: list[tuple[int, int]] = []
    for i in range(n):
        for j in range(i + 1, n):
            pairs.append((i, j))
            if len(pairs) >= total:
                return pairs, n
            pairs.append((j, i))
            if len(pairs) >= total:
                return pairs, n

    # If we somehow exit (we shouldn't for valid n), trim/return.
    return pairs[:total], n
