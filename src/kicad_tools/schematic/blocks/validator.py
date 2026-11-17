"""Connection validation for typed circuit block ports.

Validates that wired ports are electrically and protocol-compatible,
catching misconnections (e.g., I2C wired to SPI) at design time.

Example::

    from kicad_tools.schematic.blocks.validator import ConnectionValidator

    validator = ConnectionValidator()
    warnings = validator.validate_connection(usb_dp_port, spi_mosi_port)
    for w in warnings:
        print(f"[{w.severity}] {w.message}")
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .base import Port
from .interfaces import DataPort, PowerPort


class WarningSeverity(Enum):
    """Severity of a connection warning."""

    ERROR = "error"
    WARNING = "warning"


@dataclass
class ConnectionWarning:
    """A warning or error from connection validation.

    Attributes:
        severity: Whether this is an error (must fix) or warning (review).
        message: Human-readable description of the issue.
        source_port: The source port in the connection.
        target_port: The target port in the connection.
    """

    severity: WarningSeverity
    message: str
    source_port: Port
    target_port: Port


class ConnectionValidator:
    """Validate that wired ports are compatible.

    Checks interface category compatibility, protocol matching,
    and voltage range overlap between typed ports.
    """

    # Protocols that are compatible with each other
    COMPATIBLE_PROTOCOLS: dict[str, set[str]] = {
        "usb2": {"usb2", "usb3"},
        "usb3": {"usb2", "usb3"},
    }

    def validate_connection(self, source: Port, target: Port) -> list[ConnectionWarning]:
        """Validate a connection between two ports.

        Args:
            source: Source port.
            target: Target port.

        Returns:
            List of warnings/errors. Empty list means connection is valid.
        """
        warnings: list[ConnectionWarning] = []

        # Skip validation for untyped ports (no interface metadata)
        if source.interface is None and target.interface is None:
            return warnings

        # Check interface category compatibility
        if source.interface is not None and target.interface is not None:
            if not self._categories_compatible(source, target):
                warnings.append(
                    ConnectionWarning(
                        severity=WarningSeverity.ERROR,
                        message=(
                            f"Interface category mismatch: "
                            f"{source.interface.value} ({source.name}) "
                            f"-> {target.interface.value} ({target.name})"
                        ),
                        source_port=source,
                        target_port=target,
                    )
                )
                return warnings  # Category mismatch is fatal, skip further checks

        # Check protocol compatibility for data ports
        if isinstance(source, DataPort) and isinstance(target, DataPort):
            if source.protocol and target.protocol:
                if not self._protocols_compatible(source.protocol, target.protocol):
                    warnings.append(
                        ConnectionWarning(
                            severity=WarningSeverity.ERROR,
                            message=(
                                f"Protocol mismatch: "
                                f"{source.protocol} ({source.name}) "
                                f"-> {target.protocol} ({target.name})"
                            ),
                            source_port=source,
                            target_port=target,
                        )
                    )

        # Check voltage compatibility for power ports
        if isinstance(source, PowerPort) and isinstance(target, PowerPort):
            if not source.voltage_overlaps(target):
                warnings.append(
                    ConnectionWarning(
                        severity=WarningSeverity.ERROR,
                        message=(
                            f"Voltage mismatch: "
                            f"{source.voltage_min}-{source.voltage_max}V ({source.name}) "
                            f"-> {target.voltage_min}-{target.voltage_max}V ({target.name})"
                        ),
                        source_port=source,
                        target_port=target,
                    )
                )

        # Check power-to-data misconnection
        if isinstance(source, PowerPort) and isinstance(target, DataPort):
            warnings.append(
                ConnectionWarning(
                    severity=WarningSeverity.WARNING,
                    message=(f"Power port connected to data port: {source.name} -> {target.name}"),
                    source_port=source,
                    target_port=target,
                )
            )
        elif isinstance(source, DataPort) and isinstance(target, PowerPort):
            warnings.append(
                ConnectionWarning(
                    severity=WarningSeverity.WARNING,
                    message=(f"Data port connected to power port: {source.name} -> {target.name}"),
                    source_port=source,
                    target_port=target,
                )
            )

        return warnings

    def _categories_compatible(self, source: Port, target: Port) -> bool:
        """Check if interface categories are compatible."""
        return source.interface == target.interface

    def _protocols_compatible(self, source_proto: str, target_proto: str) -> bool:
        """Check if two protocols are compatible."""
        if source_proto == target_proto:
            return True
        compatible = self.COMPATIBLE_PROTOCOLS.get(source_proto, set())
        return target_proto in compatible


# ---------------------------------------------------------------------------
# Direction compatibility matrix for port matching
# ---------------------------------------------------------------------------

_DIRECTION_COMPATIBLE: dict[str, set[str]] = {
    "output": {"input", "passive", "bidirectional"},
    "input": {"output", "passive", "bidirectional"},
    "bidirectional": {"input", "output", "passive", "bidirectional"},
    "passive": {"input", "output", "passive", "bidirectional", "power"},
    "power": {"power", "passive"},
}


def _directions_compatible(a_dir: str, b_dir: str) -> bool:
    """Return True if two port directions can be wired together."""
    return b_dir in _DIRECTION_COMPATIBLE.get(a_dir, set())


def match_ports(
    source_ports: dict[str, Port],
    target_ports: dict[str, Port],
) -> list[tuple[Port, Port, list[ConnectionWarning]]]:
    """Find compatible port pairings between two blocks.

    Matching priority:
      1. Exact name match (e.g. ``VOUT`` -> ``VIN`` is *not* a name match;
         ``VOUT`` -> ``VOUT`` *is*).  Output-name aliases are also tried:
         ``VOUT`` on the source will attempt ``VIN`` on the target.
      2. Direction compatibility (output-to-input, etc.)
      3. Interface type match (same ``interface`` category).

    Each port participates in at most one pairing.

    Args:
        source_ports: Typed ports of the upstream (left / top) block.
        target_ports: Typed ports of the downstream (right / bottom) block.

    Returns:
        List of ``(source_port, target_port, warnings)`` tuples.
    """
    validator = ConnectionValidator()

    # Build a mutable pool of available target ports
    available_targets: dict[str, Port] = dict(target_ports)
    paired: list[tuple[Port, Port, list[ConnectionWarning]]] = []

    # Common output->input name aliases
    _ALIASES: dict[str, str] = {
        "VOUT": "VIN",
        "OUT": "IN",
        "TX": "RX",
        "DOUT": "DIN",
        "MOSI": "MISO",
    }

    # Pass 1: exact name or alias match
    for s_name, s_port in list(source_ports.items()):
        # Try exact name first
        if s_name in available_targets:
            t_port = available_targets.pop(s_name)
            warnings = validator.validate_connection(s_port, t_port)
            paired.append((s_port, t_port, warnings))
            continue
        # Try alias
        alias = _ALIASES.get(s_name)
        if alias and alias in available_targets:
            t_port = available_targets.pop(alias)
            warnings = validator.validate_connection(s_port, t_port)
            paired.append((s_port, t_port, warnings))

    # Collect already-paired source names
    paired_source_names = {p[0].name for p in paired}

    # Pass 2: direction + interface match for remaining ports
    for s_name, s_port in source_ports.items():
        if s_name in paired_source_names:
            continue
        best: Port | None = None
        best_score = -1
        for t_name, t_port in list(available_targets.items()):
            if not _directions_compatible(s_port.direction, t_port.direction):
                continue
            score = 0
            # Prefer same interface category
            if s_port.interface is not None and s_port.interface == t_port.interface:
                score += 2
            # Prefer same interface_type
            if s_port.interface_type is not None and s_port.interface_type == t_port.interface_type:
                score += 1
            if score > best_score:
                best_score = score
                best = t_port
        if best is not None:
            available_targets.pop(best.name)
            warnings = validator.validate_connection(s_port, best)
            paired.append((s_port, best, warnings))

    return paired
