"""
BOM export formats for different manufacturers.

Provides formatters to convert BOM data to manufacturer-specific formats.
"""

from __future__ import annotations

import csv
import io
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Type

if TYPE_CHECKING:
    from ..schema.bom import BOMItem


@dataclass
class BOMExportConfig:
    """Configuration for BOM export."""

    include_dnp: bool = False
    group_by_value: bool = True
    include_lcsc: bool = True
    include_mfr: bool = True


class BOMFormatter(ABC):
    """Abstract base class for BOM formatters."""

    # Manufacturer identifier
    manufacturer_id: str = ""
    manufacturer_name: str = ""

    def __init__(self, config: Optional[BOMExportConfig] = None):
        self.config = config or BOMExportConfig()

    @abstractmethod
    def format(self, items: List["BOMItem"]) -> str:
        """Format BOM items to manufacturer-specific format."""
        pass

    @abstractmethod
    def get_headers(self) -> List[str]:
        """Get column headers for this format."""
        pass

    def filter_items(self, items: List["BOMItem"]) -> List["BOMItem"]:
        """Filter items based on config."""
        if self.config.include_dnp:
            return items
        return [item for item in items if not getattr(item, "dnp", False)]


class JLCPCBBOMFormatter(BOMFormatter):
    """BOM formatter for JLCPCB assembly service."""

    manufacturer_id = "jlcpcb"
    manufacturer_name = "JLCPCB"

    def get_headers(self) -> List[str]:
        """JLCPCB BOM column headers."""
        return ["Comment", "Designator", "Footprint", "LCSC Part #"]

    def format(self, items: List["BOMItem"]) -> str:
        """
        Format BOM for JLCPCB.

        JLCPCB expects:
        - Comment: Component value
        - Designator: Reference designator(s), comma-separated for groups
        - Footprint: Footprint name
        - LCSC Part #: LCSC part number (e.g., C123456)
        """
        filtered = self.filter_items(items)

        if self.config.group_by_value:
            grouped = self._group_items(filtered)
        else:
            grouped = {(item.value, item.footprint, getattr(item, "lcsc", "")): [item] for item in filtered}

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(self.get_headers())

        for key, group_items in sorted(grouped.items()):
            value, footprint, lcsc = key
            designators = ",".join(sorted(item.reference for item in group_items))
            writer.writerow([value, designators, footprint, lcsc])

        return output.getvalue()

    def _group_items(self, items: List["BOMItem"]) -> Dict[tuple, List["BOMItem"]]:
        """Group items by value, footprint, and LCSC part."""
        groups: Dict[tuple, List["BOMItem"]] = {}
        for item in items:
            lcsc = getattr(item, "lcsc", "")
            key = (item.value, item.footprint, lcsc)
            if key not in groups:
                groups[key] = []
            groups[key].append(item)
        return groups


class PCBWayBOMFormatter(BOMFormatter):
    """BOM formatter for PCBWay assembly service."""

    manufacturer_id = "pcbway"
    manufacturer_name = "PCBWay"

    def get_headers(self) -> List[str]:
        """PCBWay BOM column headers."""
        return ["Item", "Designator", "Qty", "Manufacturer", "Mfr. Part #", "Description/Value", "Package/Footprint"]

    def format(self, items: List["BOMItem"]) -> str:
        """
        Format BOM for PCBWay.

        PCBWay expects:
        - Item: Row number
        - Designator: Reference designator(s)
        - Qty: Quantity
        - Manufacturer: Component manufacturer
        - Mfr. Part #: Manufacturer part number
        - Description/Value: Component value/description
        - Package/Footprint: Package size/footprint name
        """
        filtered = self.filter_items(items)

        if self.config.group_by_value:
            grouped = self._group_items(filtered)
        else:
            grouped = {(item.value, item.footprint): [item] for item in filtered}

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(self.get_headers())

        for idx, (key, group_items) in enumerate(sorted(grouped.items()), 1):
            value, footprint = key
            designators = ",".join(sorted(item.reference for item in group_items))
            qty = len(group_items)
            manufacturer = getattr(group_items[0], "manufacturer", "")
            mfr_part = getattr(group_items[0], "mfr_part", "")
            writer.writerow([idx, designators, qty, manufacturer, mfr_part, value, footprint])

        return output.getvalue()

    def _group_items(self, items: List["BOMItem"]) -> Dict[tuple, List["BOMItem"]]:
        """Group items by value and footprint."""
        groups: Dict[tuple, List["BOMItem"]] = {}
        for item in items:
            key = (item.value, item.footprint)
            if key not in groups:
                groups[key] = []
            groups[key].append(item)
        return groups


class SeeedBOMFormatter(BOMFormatter):
    """BOM formatter for Seeed Fusion assembly service."""

    manufacturer_id = "seeed"
    manufacturer_name = "Seeed Fusion"

    def get_headers(self) -> List[str]:
        """Seeed BOM column headers."""
        return ["Part/Designator", "Manufacturer Part Number/Seeed SKU", "Quantity"]

    def format(self, items: List["BOMItem"]) -> str:
        """
        Format BOM for Seeed Fusion.

        Seeed expects:
        - Part/Designator: Reference designator(s)
        - Manufacturer Part Number/Seeed SKU: MPN or Seeed OPL SKU
        - Quantity: Number of components
        """
        filtered = self.filter_items(items)

        if self.config.group_by_value:
            grouped = self._group_items(filtered)
        else:
            grouped = {item.reference: [item] for item in filtered}

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(self.get_headers())

        for key, group_items in sorted(grouped.items()):
            designators = ",".join(sorted(item.reference for item in group_items))
            mpn = getattr(group_items[0], "mfr_part", "") or getattr(group_items[0], "seeed_sku", "")
            qty = len(group_items)
            writer.writerow([designators, mpn, qty])

        return output.getvalue()

    def _group_items(self, items: List["BOMItem"]) -> Dict[str, List["BOMItem"]]:
        """Group items by MPN."""
        groups: Dict[str, List["BOMItem"]] = {}
        for item in items:
            mpn = getattr(item, "mfr_part", "") or item.value
            if mpn not in groups:
                groups[mpn] = []
            groups[mpn].append(item)
        return groups


class GenericBOMFormatter(BOMFormatter):
    """Generic BOM formatter with all available fields."""

    manufacturer_id = "generic"
    manufacturer_name = "Generic"

    def get_headers(self) -> List[str]:
        """Generic BOM column headers."""
        headers = ["Reference", "Value", "Footprint", "Quantity"]
        if self.config.include_lcsc:
            headers.append("LCSC")
        if self.config.include_mfr:
            headers.extend(["Manufacturer", "MPN"])
        return headers

    def format(self, items: List["BOMItem"]) -> str:
        """Format BOM with all available fields."""
        filtered = self.filter_items(items)

        if self.config.group_by_value:
            grouped = self._group_items(filtered)
        else:
            grouped = {item.reference: [item] for item in filtered}

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(self.get_headers())

        for key, group_items in sorted(grouped.items()):
            if isinstance(key, str):
                refs = key
            else:
                refs = ",".join(sorted(item.reference for item in group_items))

            item = group_items[0]
            row = [refs, item.value, item.footprint, len(group_items)]

            if self.config.include_lcsc:
                row.append(getattr(item, "lcsc", ""))
            if self.config.include_mfr:
                row.extend([
                    getattr(item, "manufacturer", ""),
                    getattr(item, "mfr_part", ""),
                ])

            writer.writerow(row)

        return output.getvalue()

    def _group_items(self, items: List["BOMItem"]) -> Dict[tuple, List["BOMItem"]]:
        """Group items by value and footprint."""
        groups: Dict[tuple, List["BOMItem"]] = {}
        for item in items:
            key = (item.value, item.footprint)
            if key not in groups:
                groups[key] = []
            groups[key].append(item)
        return groups


# Registry of available formatters
BOM_FORMATTERS: Dict[str, Type[BOMFormatter]] = {
    "jlcpcb": JLCPCBBOMFormatter,
    "pcbway": PCBWayBOMFormatter,
    "seeed": SeeedBOMFormatter,
    "generic": GenericBOMFormatter,
}


def get_bom_formatter(manufacturer: str, config: Optional[BOMExportConfig] = None) -> BOMFormatter:
    """
    Get BOM formatter for a manufacturer.

    Args:
        manufacturer: Manufacturer ID (jlcpcb, pcbway, seeed, generic)
        config: Export configuration

    Returns:
        BOMFormatter for the specified manufacturer

    Raises:
        ValueError: If manufacturer is not supported
    """
    formatter_class = BOM_FORMATTERS.get(manufacturer.lower())
    if formatter_class is None:
        available = ", ".join(BOM_FORMATTERS.keys())
        raise ValueError(f"Unknown manufacturer: {manufacturer}. Available: {available}")
    return formatter_class(config)


def export_bom(
    items: List["BOMItem"],
    manufacturer: str = "generic",
    config: Optional[BOMExportConfig] = None,
) -> str:
    """
    Export BOM to manufacturer-specific format.

    Args:
        items: List of BOM items
        manufacturer: Manufacturer ID
        config: Export configuration

    Returns:
        Formatted BOM as CSV string
    """
    formatter = get_bom_formatter(manufacturer, config)
    return formatter.format(items)
