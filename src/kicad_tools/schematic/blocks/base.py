"""Base classes for circuit blocks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic, SymbolInstance

    from kicad_tools.intent.types import InterfaceCategory

logger = logging.getLogger(__name__)


@dataclass
class Port:
    """A connection point on a circuit block.

    Ports represent named electrical connection points with position and
    optional interface metadata for type-checked connections.

    The interface metadata fields are all optional to maintain backward
    compatibility — existing blocks continue to work with position-only ports.

    Attributes:
        name: Port identifier (e.g., "VIN", "D+", "SDA").
        x: X coordinate in schematic units.
        y: Y coordinate in schematic units.
        direction: Signal direction — "input", "output", "bidirectional",
            "passive", or "power".
        interface: High-level interface category (POWER, DIFFERENTIAL, BUS, etc.).
        interface_type: Specific interface variant (e.g., "usb2_high_speed",
            "i2c_fast", "power_3v3").
        parameters: Electrical parameters (e.g., {"voltage_min": 3.0,
            "voltage_max": 3.6}).
        group: Groups related ports (e.g., "usb_data", "spi_bus").
    """

    name: str
    x: float
    y: float
    direction: str = "passive"  # input, output, bidirectional, passive, power
    interface: InterfaceCategory | None = None
    interface_type: str | None = None
    parameters: dict[str, object] | None = None
    group: str | None = None

    def pos(self) -> tuple[float, float]:
        """Get position as tuple."""
        return (self.x, self.y)


class CircuitBlock:
    """
    Base class for reusable circuit blocks.

    A circuit block represents a common subcircuit pattern that can be
    instantiated multiple times in a schematic. Each block:
    - Places its components at specified coordinates
    - Wires internal connections
    - Exposes ports for external connections

    Ports are available in two forms:
    - ``ports``: dict mapping name to (x, y) tuple (backward compatible).
    - ``typed_ports``: dict mapping name to ``Port`` object with full metadata.

    Subclasses should implement their setup logic in __init__, calling
    super().__init__(sch, x, y) first and then setting up components,
    wiring, and ports.
    """

    def __init__(
        self,
        sch: Schematic = None,
        x: float = 0,
        y: float = 0,
    ):
        """
        Initialize base attributes.

        Args:
            sch: Schematic to add components to
            x: X coordinate of block origin
            y: Y coordinate of block origin
        """
        self.schematic: Schematic = sch
        self.x: float = x
        self.y: float = y
        self.ports: dict[str, tuple[float, float]] = {}
        self.typed_ports: dict[str, Port] = {}
        self.components: dict[str, SymbolInstance] = {}

    def port(self, name: str) -> tuple[float, float]:
        """Get a port position by name."""
        if name not in self.ports:
            available = list(self.ports.keys())
            raise KeyError(f"Port '{name}' not found. Available: {available}")
        return self.ports[name]

    def get_typed_port(self, name: str) -> Port:
        """Get a typed port by name.

        Returns the full Port object with interface metadata. Falls back to
        creating a basic Port from the position tuple if no typed port exists.

        Args:
            name: Port name.

        Returns:
            Port object with position and any interface metadata.

        Raises:
            KeyError: If port name not found.
        """
        if name in self.typed_ports:
            return self.typed_ports[name]
        if name in self.ports:
            pos = self.ports[name]
            return Port(name=name, x=pos[0], y=pos[1])
        available = list(self.ports.keys())
        raise KeyError(f"Port '{name}' not found. Available: {available}")

    # ------------------------------------------------------------------
    # Algebraic composition operators
    # ------------------------------------------------------------------

    def _ensure_typed_ports(self) -> dict[str, Port]:
        """Return typed ports, synthesizing from ``ports`` if needed."""
        result: dict[str, Port] = {}
        for name in self.ports:
            result[name] = self.get_typed_port(name)
        # Merge in any typed_ports that might not be in self.ports
        for name, tp in self.typed_ports.items():
            if name not in result:
                result[name] = tp
        return result

    def __and__(self, other: CircuitBlock) -> ComposedCircuitBlock:
        """Series composition: ``a & b``.

        Connects output ports of *self* to input ports of *other* by
        matching on name/alias, then direction compatibility, then
        interface type.  The resulting block exposes the un-wired input
        ports of *self* and un-wired output ports of *other*.
        """
        return ComposedCircuitBlock(
            left=self,
            right=other,
            mode="series",
        )

    def __or__(self, other: CircuitBlock) -> ComposedCircuitBlock:
        """Parallel composition: ``a | b``.

        Places both blocks side-by-side (vertically) with shared input
        ports tied together and output ports combined under
        disambiguated names.
        """
        return ComposedCircuitBlock(
            left=self,
            right=other,
            mode="parallel",
        )


class ComposedCircuitBlock(CircuitBlock):
    """A circuit block formed by composing two child blocks.

    Supports *series* (``&``) and *parallel* (``|``) composition.

    Series (``a & b``):
        - Wires output ports of *a* to input ports of *b*.
        - Exposes un-wired input ports of *a* and un-wired output
          ports of *b*.

    Parallel (``a | b``):
        - Ties matching input ports together (shared inputs).
        - Combines output ports under disambiguated names.

    Composition is **lazy**: child blocks may be composed before a
    schematic is assigned.  Call :meth:`realize` to place components
    and draw wires into a schematic.

    Port matching uses :func:`~.validator.match_ports`.
    Validation warnings are collected in :attr:`warnings` and also
    logged at ``WARNING`` level.
    """

    # Default gap between blocks (schematic units)
    SERIES_GAP: float = 30.0
    PARALLEL_GAP: float = 40.0

    def __init__(
        self,
        left: CircuitBlock,
        right: CircuitBlock,
        mode: str = "series",
    ) -> None:
        super().__init__()
        self.left = left
        self.right = right
        self.mode = mode

        # Lazy import to avoid circular dependency
        from .validator import ConnectionWarning, match_ports

        self.warnings: list[ConnectionWarning] = []
        self._wired_pairs: list[tuple[Port, Port]] = []

        # Resolve typed ports for both children
        left_typed = left._ensure_typed_ports()
        right_typed = right._ensure_typed_ports()

        if mode == "series":
            self._compose_series(left_typed, right_typed, match_ports)
        elif mode == "parallel":
            self._compose_parallel(left_typed, right_typed, match_ports)
        else:
            raise ValueError(f"Unknown composition mode: {mode!r}")

    # ------------------------------------------------------------------
    # Series composition
    # ------------------------------------------------------------------

    def _compose_series(self, left_typed, right_typed, match_ports_fn) -> None:
        """Wire output ports of left to input ports of right."""
        # For series, only match output-like ports of left to input-like
        # ports of right.  Passive ports can participate on either side.
        _OUT_DIRS = {"output", "bidirectional", "passive"}
        _IN_DIRS = {"input", "bidirectional", "passive", "power"}
        left_sources = {n: p for n, p in left_typed.items() if p.direction in _OUT_DIRS}
        right_sinks = {n: p for n, p in right_typed.items() if p.direction in _IN_DIRS}
        pairings = match_ports_fn(left_sources, right_sinks)

        wired_left_names: set[str] = set()
        wired_right_names: set[str] = set()

        for src, tgt, warnings in pairings:
            self._wired_pairs.append((src, tgt))
            wired_left_names.add(src.name)
            wired_right_names.add(tgt.name)
            for w in warnings:
                self.warnings.append(w)
                logger.warning(
                    "Composition warning (%s -> %s): %s",
                    src.name,
                    tgt.name,
                    w.message,
                )

        # Expose un-wired ports
        for name, tp in left_typed.items():
            if name not in wired_left_names:
                self.ports[name] = tp.pos()
                self.typed_ports[name] = tp

        for name, tp in right_typed.items():
            if name not in wired_right_names:
                # Disambiguate if name already taken
                ext_name = name if name not in self.ports else f"{_block_label(self.right)}.{name}"
                self.ports[ext_name] = tp.pos()
                self.typed_ports[ext_name] = tp

    # ------------------------------------------------------------------
    # Parallel composition
    # ------------------------------------------------------------------

    def _compose_parallel(self, left_typed, right_typed, match_ports_fn) -> None:
        """Tie matching inputs together, combine outputs."""
        # Identify input vs output ports on each side
        left_inputs = {
            n: p for n, p in left_typed.items() if p.direction in ("input", "power", "passive")
        }
        right_inputs = {
            n: p for n, p in right_typed.items() if p.direction in ("input", "power", "passive")
        }
        left_outputs = {
            n: p for n, p in left_typed.items() if p.direction in ("output", "bidirectional")
        }
        right_outputs = {
            n: p for n, p in right_typed.items() if p.direction in ("output", "bidirectional")
        }

        # Shared inputs: exact-name match
        shared_input_names: set[str] = set()
        for name in left_inputs:
            if name in right_inputs:
                shared_input_names.add(name)
                # Expose once (use left's position as canonical)
                tp = left_inputs[name]
                self.ports[name] = tp.pos()
                self.typed_ports[name] = tp

        # Expose non-shared inputs
        for name, tp in left_inputs.items():
            if name not in shared_input_names and name not in self.ports:
                ext_name = f"{_block_label(self.left)}.{name}"
                self.ports[ext_name] = tp.pos()
                self.typed_ports[ext_name] = tp

        for name, tp in right_inputs.items():
            if name not in shared_input_names and name not in self.ports:
                ext_name = f"{_block_label(self.right)}.{name}"
                self.ports[ext_name] = tp.pos()
                self.typed_ports[ext_name] = tp

        # Combine outputs under disambiguated names
        for name, tp in left_outputs.items():
            ext_name = f"{_block_label(self.left)}.{name}" if name in right_outputs else name
            self.ports[ext_name] = tp.pos()
            self.typed_ports[ext_name] = tp

        for name, tp in right_outputs.items():
            ext_name = f"{_block_label(self.right)}.{name}" if name in left_outputs else name
            self.ports[ext_name] = tp.pos()
            self.typed_ports[ext_name] = tp

    # ------------------------------------------------------------------
    # Realization
    # ------------------------------------------------------------------

    def realize(self, sch: Schematic, x: float = 0, y: float = 0) -> None:
        """Place components and draw wires into a schematic.

        Recursively realizes child blocks (if they are also
        ``ComposedCircuitBlock`` instances) and draws wires between
        matched port pairs.

        Args:
            sch: Target schematic.
            x: X origin for the composed block.
            y: Y origin for the composed block.
        """
        self.schematic = sch
        self.x = x
        self.y = y

        if self.mode == "series":
            self._realize_series(sch, x, y)
        else:
            self._realize_parallel(sch, x, y)

    def _realize_series(self, sch: Schematic, x: float, y: float) -> None:
        """Realize series composition: left, then right offset to the right."""
        # Place left block
        _realize_child(self.left, sch, x, y)

        # Place right block offset to the right
        right_x = x + self.SERIES_GAP
        _realize_child(self.right, sch, right_x, y)

        # Draw wires between matched port pairs
        for src, tgt in self._wired_pairs:
            sch.add_wire(src.pos(), tgt.pos())

    def _realize_parallel(self, sch: Schematic, x: float, y: float) -> None:
        """Realize parallel composition: left on top, right below."""
        _realize_child(self.left, sch, x, y)
        _realize_child(self.right, sch, x, y + self.PARALLEL_GAP)

    @property
    def children(self) -> tuple[CircuitBlock, CircuitBlock]:
        """Return the two child blocks."""
        return (self.left, self.right)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _block_label(block: CircuitBlock) -> str:
    """Derive a short label for a block (used in disambiguated port names)."""
    cls_name = type(block).__name__
    # Strip common suffixes
    for suffix in ("Block", "Circuit"):
        if cls_name.endswith(suffix) and len(cls_name) > len(suffix):
            cls_name = cls_name[: -len(suffix)]
    return cls_name


def _realize_child(child: CircuitBlock, sch: Schematic, x: float, y: float) -> None:
    """Realize a child block, handling both composed and plain blocks."""
    if isinstance(child, ComposedCircuitBlock):
        child.realize(sch, x, y)
    else:
        # Plain CircuitBlock -- update its coordinates (it was already
        # initialized, so we just update position attributes).
        child.schematic = sch
        child.x = x
        child.y = y
