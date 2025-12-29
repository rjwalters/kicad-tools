"""Footprint query classes for fluent filtering.

Provides query interface for PCB footprints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from .base import BaseQuery

if TYPE_CHECKING:
    from ..schema.pcb import Footprint


class FootprintQuery(BaseQuery["Footprint"]):
    """Query interface for PCB footprints.

    Extends BaseQuery with footprint-specific convenience methods.

    Example:
        query = FootprintQuery(footprints)
        u1 = query.by_reference("U1")
        smd = query.smd().all()
        top = query.on_layer("F.Cu").all()
    """

    def by_reference(self, reference: str) -> Optional["Footprint"]:
        """Get footprint by reference designator.

        Args:
            reference: Reference designator (e.g., "U1", "R1", "C1")

        Returns:
            Footprint with matching reference, or None

        Example:
            u1 = query.by_reference("U1")
        """
        return self.filter(reference=reference).first()

    def by_name(self, name: str) -> "FootprintQuery":
        """Filter by footprint name.

        Args:
            name: Footprint library name (e.g., "Resistor_SMD:R_0402_1005Metric")

        Returns:
            Query filtered to matching name

        Example:
            r0402 = query.by_name("Resistor_SMD:R_0402_1005Metric").all()
        """
        return self.filter(name=name)

    def by_value(self, value: str) -> "FootprintQuery":
        """Filter by value.

        Args:
            value: Component value (e.g., "10k", "100nF")

        Returns:
            Query filtered to matching value

        Example:
            caps_100nf = query.by_value("100nF").all()
        """
        return self.filter(value=value)

    def on_layer(self, layer: str) -> "FootprintQuery":
        """Filter by layer.

        Args:
            layer: Layer name (e.g., "F.Cu", "B.Cu")

        Returns:
            Query filtered to footprints on specified layer

        Example:
            top_side = query.on_layer("F.Cu").all()
        """
        return self.filter(layer=layer)

    def on_top(self) -> "FootprintQuery":
        """Filter to footprints on top layer (F.Cu).

        Returns:
            Query filtered to top-side footprints
        """
        return self.on_layer("F.Cu")

    def on_bottom(self) -> "FootprintQuery":
        """Filter to footprints on bottom layer (B.Cu).

        Returns:
            Query filtered to bottom-side footprints
        """
        return self.on_layer("B.Cu")

    def smd(self) -> "FootprintQuery":
        """Filter to SMD footprints.

        Returns:
            Query filtered to SMD footprints
        """
        return self.filter(attr="smd")

    def through_hole(self) -> "FootprintQuery":
        """Filter to through-hole footprints.

        Returns:
            Query filtered to through-hole footprints
        """
        return self.filter(attr="through_hole")

    def capacitors(self) -> "FootprintQuery":
        """Filter to capacitors (C* references).

        Returns:
            Query filtered to capacitors
        """
        return self.filter(reference__startswith="C")

    def resistors(self) -> "FootprintQuery":
        """Filter to resistors (R* references).

        Returns:
            Query filtered to resistors
        """
        return self.filter(reference__startswith="R")

    def ics(self) -> "FootprintQuery":
        """Filter to ICs (U* references).

        Returns:
            Query filtered to ICs
        """
        return self.filter(reference__startswith="U")

    def connectors(self) -> "FootprintQuery":
        """Filter to connectors (J* references).

        Returns:
            Query filtered to connectors
        """
        return self.filter(reference__startswith="J")

    def with_prefix(self, prefix: str) -> "FootprintQuery":
        """Filter by reference prefix.

        Args:
            prefix: Reference prefix (e.g., "R", "C", "U")

        Returns:
            Query filtered to matching prefix

        Example:
            transistors = query.with_prefix("Q").all()
        """
        return self.filter(reference__startswith=prefix)


class FootprintList(list):
    """List subclass with query methods for footprints.

    Extends list to provide backward compatibility while adding
    fluent query capabilities. All normal list operations work.

    Example:
        # Old style (still works)
        for fp in pcb.footprints:
            print(fp.reference)

        # New fluent style
        u1 = pcb.footprints.by_reference("U1")
        smd = pcb.footprints.smd()

        # Query object for complex filters
        top_ics = pcb.footprints.query() \\
            .filter(reference__startswith="U") \\
            .on_top() \\
            .order_by("reference") \\
            .all()
    """

    def query(self) -> FootprintQuery:
        """Return a query object for fluent filtering.

        Use this for complex queries or when you need the full
        query API (e.g., chaining, ordering).

        Returns:
            FootprintQuery for this list

        Example:
            smd_caps = pcb.footprints.query() \\
                .smd() \\
                .capacitors() \\
                .on_top() \\
                .all()
        """
        return FootprintQuery(list(self))

    def by_reference(self, reference: str) -> Optional["Footprint"]:
        """Get footprint by reference (shortcut).

        Args:
            reference: Reference designator

        Returns:
            Footprint with matching reference, or None
        """
        return self.query().by_reference(reference)

    def by_name(self, name: str) -> List["Footprint"]:
        """Get footprints by name (shortcut).

        Args:
            name: Footprint library name

        Returns:
            List of matching footprints
        """
        return self.query().by_name(name).all()

    def by_value(self, value: str) -> List["Footprint"]:
        """Get footprints by value (shortcut).

        Args:
            value: Component value

        Returns:
            List of matching footprints
        """
        return self.query().by_value(value).all()

    def filter(self, **kwargs) -> List["Footprint"]:
        """Filter footprints (shortcut, returns list).

        For chained filtering, use .query().filter(...).filter(...)

        Args:
            **kwargs: Field lookups

        Returns:
            List of matching footprints
        """
        return self.query().filter(**kwargs).all()

    def exclude(self, **kwargs) -> List["Footprint"]:
        """Exclude footprints (shortcut, returns list).

        Args:
            **kwargs: Field lookups to exclude

        Returns:
            List of non-matching footprints
        """
        return self.query().exclude(**kwargs).all()

    def on_layer(self, layer: str) -> List["Footprint"]:
        """Get footprints on layer (shortcut).

        Args:
            layer: Layer name

        Returns:
            List of footprints on specified layer
        """
        return self.query().on_layer(layer).all()

    def on_top(self) -> List["Footprint"]:
        """Get footprints on top layer (shortcut)."""
        return self.query().on_top().all()

    def on_bottom(self) -> List["Footprint"]:
        """Get footprints on bottom layer (shortcut)."""
        return self.query().on_bottom().all()

    def smd(self) -> List["Footprint"]:
        """Get all SMD footprints (shortcut)."""
        return self.query().smd().all()

    def through_hole(self) -> List["Footprint"]:
        """Get all through-hole footprints (shortcut)."""
        return self.query().through_hole().all()

    def capacitors(self) -> List["Footprint"]:
        """Get all capacitors (shortcut)."""
        return self.query().capacitors().all()

    def resistors(self) -> List["Footprint"]:
        """Get all resistors (shortcut)."""
        return self.query().resistors().all()

    def ics(self) -> List["Footprint"]:
        """Get all ICs (shortcut)."""
        return self.query().ics().all()

    def connectors(self) -> List["Footprint"]:
        """Get all connectors (shortcut)."""
        return self.query().connectors().all()

    def references(self) -> List[str]:
        """Get list of all reference designators.

        Returns:
            Sorted list of reference designators
        """
        refs = [fp.reference for fp in self if hasattr(fp, "reference")]
        return sorted(refs)
