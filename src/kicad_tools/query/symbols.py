"""Symbol query classes for fluent filtering.

Provides query interface for schematic symbol instances.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from .base import BaseQuery

if TYPE_CHECKING:
    from ..schema.symbol import SymbolInstance


class SymbolQuery(BaseQuery["SymbolInstance"]):
    """Query interface for symbol instances.

    Extends BaseQuery with symbol-specific convenience methods.

    Example:
        query = SymbolQuery(symbols)
        u1 = query.by_reference("U1")
        caps = query.capacitors().all()
        power = query.filter(lib_id__startswith="power:").all()
    """

    def by_reference(self, reference: str) -> Optional["SymbolInstance"]:
        """Get symbol by reference designator.

        Args:
            reference: Reference designator (e.g., "U1", "R1", "C1")

        Returns:
            Symbol with matching reference, or None

        Example:
            u1 = query.by_reference("U1")
        """
        return self.filter(reference=reference).first()

    def by_lib_id(self, lib_id: str) -> "SymbolQuery":
        """Filter by library ID.

        Args:
            lib_id: Library identifier (e.g., "Device:R", "MCU_ST:STM32F4")

        Returns:
            Query filtered to matching library ID

        Example:
            resistors = query.by_lib_id("Device:R").all()
        """
        return self.filter(lib_id=lib_id)

    def by_value(self, value: str) -> "SymbolQuery":
        """Filter by value.

        Args:
            value: Component value (e.g., "10k", "100nF")

        Returns:
            Query filtered to matching value

        Example:
            caps_100nf = query.by_value("100nF").all()
        """
        return self.filter(value=value)

    def by_footprint(self, footprint: str) -> "SymbolQuery":
        """Filter by footprint.

        Args:
            footprint: Footprint name or pattern

        Returns:
            Query filtered to matching footprint

        Example:
            smd_0402 = query.by_footprint("Resistor_SMD:R_0402_1005Metric").all()
        """
        return self.filter(footprint=footprint)

    def capacitors(self) -> "SymbolQuery":
        """Filter to capacitors (C* references).

        Returns:
            Query filtered to capacitors
        """
        return self.filter(reference__startswith="C")

    def resistors(self) -> "SymbolQuery":
        """Filter to resistors (R* references).

        Returns:
            Query filtered to resistors
        """
        return self.filter(reference__startswith="R")

    def inductors(self) -> "SymbolQuery":
        """Filter to inductors (L* references).

        Returns:
            Query filtered to inductors
        """
        return self.filter(reference__startswith="L")

    def ics(self) -> "SymbolQuery":
        """Filter to ICs (U* references).

        Returns:
            Query filtered to ICs
        """
        return self.filter(reference__startswith="U")

    def connectors(self) -> "SymbolQuery":
        """Filter to connectors (J* references).

        Returns:
            Query filtered to connectors
        """
        return self.filter(reference__startswith="J")

    def transistors(self) -> "SymbolQuery":
        """Filter to transistors (Q* references).

        Returns:
            Query filtered to transistors
        """
        return self.filter(reference__startswith="Q")

    def diodes(self) -> "SymbolQuery":
        """Filter to diodes (D* references).

        Returns:
            Query filtered to diodes
        """
        return self.filter(reference__startswith="D")

    def power_symbols(self) -> "SymbolQuery":
        """Filter to power symbols.

        Returns:
            Query filtered to power symbols (lib_id starts with "power:")
        """
        return self.filter(lib_id__startswith="power:")

    def non_power(self) -> "SymbolQuery":
        """Filter out power symbols.

        Returns:
            Query excluding power symbols
        """
        return self.exclude(lib_id__startswith="power:")

    def in_bom(self) -> "SymbolQuery":
        """Filter to components included in BOM.

        Returns:
            Query filtered to in_bom=True and not DNP
        """
        return self.filter(in_bom=True, dnp=False)

    def dnp(self) -> "SymbolQuery":
        """Filter to Do Not Place components.

        Returns:
            Query filtered to DNP symbols
        """
        return self.filter(dnp=True)

    def on_board(self) -> "SymbolQuery":
        """Filter to components that should be on the PCB.

        Returns:
            Query filtered to on_board=True
        """
        return self.filter(on_board=True)


class SymbolList(list):
    """List subclass with query methods for symbols.

    Extends list to provide backward compatibility while adding
    fluent query capabilities. All normal list operations work.

    Example:
        # Old style (still works)
        for sym in sch.symbols:
            print(sym.reference)

        # New fluent style
        u1 = sch.symbols.by_reference("U1")
        caps = sch.symbols.filter(value="100nF")

        # Query object for complex filters
        power = sch.symbols.query().filter(lib_id__startswith="power:").all()
    """

    def query(self) -> SymbolQuery:
        """Return a query object for fluent filtering.

        Use this for complex queries or when you need the full
        query API (e.g., chaining, ordering).

        Returns:
            SymbolQuery for this list

        Example:
            power_ics = sch.symbols.query() \\
                .filter(reference__startswith="U") \\
                .exclude(lib_id__startswith="power:") \\
                .order_by("reference") \\
                .all()
        """
        return SymbolQuery(list(self))

    def by_reference(self, reference: str) -> Optional["SymbolInstance"]:
        """Get symbol by reference (shortcut).

        Args:
            reference: Reference designator

        Returns:
            Symbol with matching reference, or None
        """
        return self.query().by_reference(reference)

    def by_lib_id(self, lib_id: str) -> List["SymbolInstance"]:
        """Get symbols by library ID (shortcut).

        Args:
            lib_id: Library identifier

        Returns:
            List of matching symbols
        """
        return self.query().by_lib_id(lib_id).all()

    def by_value(self, value: str) -> List["SymbolInstance"]:
        """Get symbols by value (shortcut).

        Args:
            value: Component value

        Returns:
            List of matching symbols
        """
        return self.query().by_value(value).all()

    def filter(self, **kwargs) -> List["SymbolInstance"]:
        """Filter symbols (shortcut, returns list).

        For chained filtering, use .query().filter(...).filter(...)

        Args:
            **kwargs: Field lookups

        Returns:
            List of matching symbols
        """
        return self.query().filter(**kwargs).all()

    def exclude(self, **kwargs) -> List["SymbolInstance"]:
        """Exclude symbols (shortcut, returns list).

        Args:
            **kwargs: Field lookups to exclude

        Returns:
            List of non-matching symbols
        """
        return self.query().exclude(**kwargs).all()

    def capacitors(self) -> List["SymbolInstance"]:
        """Get all capacitors (shortcut)."""
        return self.query().capacitors().all()

    def resistors(self) -> List["SymbolInstance"]:
        """Get all resistors (shortcut)."""
        return self.query().resistors().all()

    def ics(self) -> List["SymbolInstance"]:
        """Get all ICs (shortcut)."""
        return self.query().ics().all()

    def connectors(self) -> List["SymbolInstance"]:
        """Get all connectors (shortcut)."""
        return self.query().connectors().all()

    def power_symbols(self) -> List["SymbolInstance"]:
        """Get all power symbols (shortcut)."""
        return self.query().power_symbols().all()

    def non_power(self) -> List["SymbolInstance"]:
        """Get all non-power symbols (shortcut)."""
        return self.query().non_power().all()

    def in_bom(self) -> List["SymbolInstance"]:
        """Get symbols in BOM (shortcut)."""
        return self.query().in_bom().all()

    def references(self) -> List[str]:
        """Get list of all reference designators.

        Returns:
            Sorted list of reference designators
        """
        refs = [sym.reference for sym in self if hasattr(sym, "reference")]
        return sorted(refs)
