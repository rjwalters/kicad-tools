"""
Bill of Materials (BOM) generation.

Extracts component information from schematics for manufacturing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .hierarchy import build_hierarchy
from .schematic import Schematic


@dataclass
class BOMItem:
    """A single component in the BOM."""

    reference: str
    value: str
    footprint: str
    lib_id: str
    datasheet: str = ""
    description: str = ""
    manufacturer: str = ""
    mpn: str = ""  # Manufacturer Part Number
    lcsc: str = ""  # LCSC Part Number
    dnp: bool = False
    in_bom: bool = True

    # Additional properties from schematic
    properties: Dict[str, str] = field(default_factory=dict)

    @property
    def is_power_symbol(self) -> bool:
        """Check if this is a power symbol (not a real component)."""
        return self.lib_id.startswith("power:")

    @property
    def is_virtual(self) -> bool:
        """Check if this is a virtual component (not placed on PCB)."""
        return not self.in_bom or self.is_power_symbol


@dataclass
class BOMGroup:
    """A group of identical components."""

    value: str
    footprint: str
    items: List[BOMItem] = field(default_factory=list)

    @property
    def quantity(self) -> int:
        return len(self.items)

    @property
    def references(self) -> str:
        """Comma-separated list of references."""
        refs = sorted(
            self.items,
            key=lambda x: (
                x.reference[0] if x.reference else "",
                int("".join(filter(str.isdigit, x.reference)) or "0"),
            ),
        )
        return ", ".join(item.reference for item in refs)

    @property
    def lcsc(self) -> str:
        """Get LCSC part number (from first item with one)."""
        for item in self.items:
            if item.lcsc:
                return item.lcsc
        return ""

    @property
    def mpn(self) -> str:
        """Get manufacturer part number (from first item with one)."""
        for item in self.items:
            if item.mpn:
                return item.mpn
        return ""

    @property
    def description(self) -> str:
        """Get description (from first item with one)."""
        for item in self.items:
            if item.description:
                return item.description
        return ""


@dataclass
class BOM:
    """Complete Bill of Materials."""

    items: List[BOMItem] = field(default_factory=list)
    source: str = ""  # Source schematic path

    @property
    def total_components(self) -> int:
        return len([i for i in self.items if not i.is_virtual and not i.dnp])

    @property
    def unique_parts(self) -> int:
        return len(self.grouped())

    @property
    def dnp_count(self) -> int:
        return len([i for i in self.items if i.dnp])

    def grouped(self, by: str = "value+footprint") -> List[BOMGroup]:
        """
        Group items by specified criteria.

        Args:
            by: Grouping mode - "value+footprint", "value", "footprint", "mpn"

        Returns:
            List of BOMGroup
        """
        groups: Dict[str, BOMGroup] = {}

        for item in self.items:
            # Skip virtual components
            if item.is_virtual:
                continue

            # Create group key
            if by == "value+footprint":
                key = f"{item.value}|{item.footprint}"
            elif by == "value":
                key = item.value
            elif by == "footprint":
                key = item.footprint
            elif by == "mpn":
                key = item.mpn or f"{item.value}|{item.footprint}"
            else:
                key = f"{item.value}|{item.footprint}"

            if key not in groups:
                groups[key] = BOMGroup(
                    value=item.value,
                    footprint=item.footprint,
                )
            groups[key].items.append(item)

        # Sort groups by reference prefix then number
        return sorted(
            groups.values(),
            key=lambda g: (
                g.items[0].reference[0] if g.items else "",
                g.value,
            ),
        )

    def filter(
        self,
        include_dnp: bool = False,
        reference_pattern: Optional[str] = None,
    ) -> BOM:
        """
        Return a filtered BOM.

        Args:
            include_dnp: Include DNP components
            reference_pattern: Filter by reference pattern (e.g., "R*", "U*")

        Returns:
            New filtered BOM
        """
        import fnmatch

        filtered = []
        for item in self.items:
            # Skip DNP unless requested
            if item.dnp and not include_dnp:
                continue

            # Skip virtual
            if item.is_virtual:
                continue

            # Apply reference filter
            if reference_pattern and not fnmatch.fnmatch(item.reference, reference_pattern):
                continue

            filtered.append(item)

        return BOM(items=filtered, source=self.source)


def extract_bom_from_schematic(schematic: Schematic) -> List[BOMItem]:
    """Extract BOM items from a single schematic."""
    items = []

    for sym in schematic.symbols:
        # Skip power symbols
        if sym.lib_id.startswith("power:"):
            continue

        # Extract standard properties
        item = BOMItem(
            reference=sym.reference,
            value=sym.value,
            footprint=sym.footprint,
            lib_id=sym.lib_id,
            datasheet=sym.datasheet,
            dnp=sym.dnp,
            in_bom=sym.in_bom,
        )

        # Extract additional properties
        for name, prop in sym.properties.items():
            value = prop.value

            # Map common property names
            if name.lower() in ("description", "desc"):
                item.description = value
            elif name.lower() in ("manufacturer", "mfr", "mfg"):
                item.manufacturer = value
            elif name.lower() in ("mpn", "mfr_pn", "manufacturer_pn", "pn"):
                item.mpn = value
            elif name.lower() in ("lcsc", "lcsc_pn", "lcsc part", "jlc", "jlcpcb"):
                item.lcsc = value
            else:
                item.properties[name] = value

        items.append(item)

    return items


def extract_bom_hierarchical(root_schematic: str) -> BOM:
    """
    Extract BOM from a hierarchical schematic.

    Traverses all sheets and collects components.

    Args:
        root_schematic: Path to the root .kicad_sch file

    Returns:
        Complete BOM with all components
    """
    hierarchy = build_hierarchy(root_schematic)
    all_items = []

    for node in hierarchy.all_nodes():
        try:
            sch = Schematic.load(node.path)
            items = extract_bom_from_schematic(sch)
            all_items.extend(items)
        except Exception:
            # Skip sheets that can't be loaded
            pass

    return BOM(items=all_items, source=root_schematic)


def extract_bom(schematic_path: str, hierarchical: bool = True) -> BOM:
    """
    Extract BOM from a schematic.

    Args:
        schematic_path: Path to .kicad_sch file
        hierarchical: If True, include all sub-sheets

    Returns:
        BOM object
    """
    if hierarchical:
        return extract_bom_hierarchical(schematic_path)
    else:
        sch = Schematic.load(schematic_path)
        items = extract_bom_from_schematic(sch)
        return BOM(items=items, source=schematic_path)
