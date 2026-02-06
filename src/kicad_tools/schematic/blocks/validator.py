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
