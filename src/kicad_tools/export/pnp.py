"""
Pick-and-place (CPL) file generator for assembly services.

Exports component placement data in manufacturer-specific formats.
"""

from __future__ import annotations

import csv
import io
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Type

if TYPE_CHECKING:
    from ..schema.pcb import Footprint


@dataclass
class PlacementData:
    """Component placement data extracted from PCB."""

    reference: str
    value: str
    footprint: str
    x: float  # mm from origin
    y: float  # mm from origin
    rotation: float  # degrees
    layer: str  # F.Cu or B.Cu


@dataclass
class PnPExportConfig:
    """Configuration for pick-and-place export."""

    # Coordinate system adjustments
    x_offset: float = 0.0
    y_offset: float = 0.0
    mirror_x: bool = False
    mirror_y: bool = False

    # Origin handling
    use_aux_origin: bool = True  # Use auxiliary axis origin

    # Filtering
    include_dnp: bool = False
    top_only: bool = False
    bottom_only: bool = False

    # Rotation adjustment (some assemblers have different 0° reference)
    rotation_offset: float = 0.0


class PnPFormatter(ABC):
    """Abstract base class for pick-and-place formatters."""

    manufacturer_id: str = ""
    manufacturer_name: str = ""

    def __init__(self, config: Optional[PnPExportConfig] = None):
        self.config = config or PnPExportConfig()

    @abstractmethod
    def format(self, placements: List[PlacementData]) -> str:
        """Format placement data to manufacturer-specific format."""
        pass

    @abstractmethod
    def get_headers(self) -> List[str]:
        """Get column headers for this format."""
        pass

    def apply_transforms(self, placement: PlacementData) -> PlacementData:
        """Apply coordinate transforms based on config."""
        x = placement.x + self.config.x_offset
        y = placement.y + self.config.y_offset

        if self.config.mirror_x:
            x = -x
        if self.config.mirror_y:
            y = -y

        rotation = (placement.rotation + self.config.rotation_offset) % 360

        return PlacementData(
            reference=placement.reference,
            value=placement.value,
            footprint=placement.footprint,
            x=x,
            y=y,
            rotation=rotation,
            layer=placement.layer,
        )

    def filter_placements(self, placements: List[PlacementData]) -> List[PlacementData]:
        """Filter placements based on config."""
        result = placements

        if self.config.top_only:
            result = [p for p in result if p.layer == "F.Cu"]
        elif self.config.bottom_only:
            result = [p for p in result if p.layer == "B.Cu"]

        return result


class JLCPCBPnPFormatter(PnPFormatter):
    """Pick-and-place formatter for JLCPCB assembly service."""

    manufacturer_id = "jlcpcb"
    manufacturer_name = "JLCPCB"

    def get_headers(self) -> List[str]:
        """JLCPCB CPL column headers."""
        return ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]

    def format(self, placements: List[PlacementData]) -> str:
        """
        Format CPL for JLCPCB.

        JLCPCB expects:
        - Designator: Reference designator
        - Val: Component value
        - Package: Footprint name
        - Mid X: X coordinate in mm
        - Mid Y: Y coordinate in mm
        - Rotation: Rotation in degrees
        - Layer: top or bottom
        """
        filtered = self.filter_placements(placements)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(self.get_headers())

        for placement in sorted(filtered, key=lambda p: p.reference):
            transformed = self.apply_transforms(placement)
            layer = "top" if transformed.layer == "F.Cu" else "bottom"
            writer.writerow([
                transformed.reference,
                transformed.value,
                transformed.footprint,
                f"{transformed.x:.4f}mm",
                f"{transformed.y:.4f}mm",
                f"{transformed.rotation:.1f}",
                layer,
            ])

        return output.getvalue()


class PCBWayPnPFormatter(PnPFormatter):
    """Pick-and-place formatter for PCBWay assembly service."""

    manufacturer_id = "pcbway"
    manufacturer_name = "PCBWay"

    def get_headers(self) -> List[str]:
        """PCBWay CPL column headers."""
        return ["Designator", "Footprint", "Mid X", "Mid Y", "Ref X", "Ref Y", "Pad X", "Pad Y", "Layer", "Rotation", "Comment"]

    def format(self, placements: List[PlacementData]) -> str:
        """
        Format CPL for PCBWay.

        PCBWay expects more detailed placement info.
        """
        filtered = self.filter_placements(placements)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(self.get_headers())

        for placement in sorted(filtered, key=lambda p: p.reference):
            transformed = self.apply_transforms(placement)
            layer = "T" if transformed.layer == "F.Cu" else "B"
            writer.writerow([
                transformed.reference,
                transformed.footprint,
                f"{transformed.x:.4f}",
                f"{transformed.y:.4f}",
                f"{transformed.x:.4f}",  # Ref X same as Mid X
                f"{transformed.y:.4f}",  # Ref Y same as Mid Y
                f"{transformed.x:.4f}",  # Pad X
                f"{transformed.y:.4f}",  # Pad Y
                layer,
                f"{transformed.rotation:.1f}",
                transformed.value,
            ])

        return output.getvalue()


class GenericPnPFormatter(PnPFormatter):
    """Generic pick-and-place formatter."""

    manufacturer_id = "generic"
    manufacturer_name = "Generic"

    def get_headers(self) -> List[str]:
        """Generic CPL column headers."""
        return ["Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side"]

    def format(self, placements: List[PlacementData]) -> str:
        """Format CPL in generic CSV format."""
        filtered = self.filter_placements(placements)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(self.get_headers())

        for placement in sorted(filtered, key=lambda p: p.reference):
            transformed = self.apply_transforms(placement)
            side = "top" if transformed.layer == "F.Cu" else "bottom"
            writer.writerow([
                transformed.reference,
                transformed.value,
                transformed.footprint,
                f"{transformed.x:.4f}",
                f"{transformed.y:.4f}",
                f"{transformed.rotation:.1f}",
                side,
            ])

        return output.getvalue()


# Registry of available formatters
PNP_FORMATTERS: Dict[str, Type[PnPFormatter]] = {
    "jlcpcb": JLCPCBPnPFormatter,
    "pcbway": PCBWayPnPFormatter,
    "generic": GenericPnPFormatter,
}


def get_pnp_formatter(manufacturer: str, config: Optional[PnPExportConfig] = None) -> PnPFormatter:
    """
    Get pick-and-place formatter for a manufacturer.

    Args:
        manufacturer: Manufacturer ID (jlcpcb, pcbway, generic)
        config: Export configuration

    Returns:
        PnPFormatter for the specified manufacturer

    Raises:
        ValueError: If manufacturer is not supported
    """
    formatter_class = PNP_FORMATTERS.get(manufacturer.lower())
    if formatter_class is None:
        available = ", ".join(PNP_FORMATTERS.keys())
        raise ValueError(f"Unknown manufacturer: {manufacturer}. Available: {available}")
    return formatter_class(config)


def extract_placements(footprints: List["Footprint"]) -> List[PlacementData]:
    """
    Extract placement data from PCB footprints.

    Args:
        footprints: List of Footprint objects from PCB

    Returns:
        List of PlacementData for assembly
    """
    placements = []
    for fp in footprints:
        # Skip virtual or excluded footprints
        if getattr(fp, "exclude_from_pos_files", False):
            continue

        x, y = fp.position
        placements.append(PlacementData(
            reference=fp.reference,
            value=fp.value,
            footprint=fp.name,
            x=x,
            y=y,
            rotation=fp.rotation,
            layer=fp.layer,
        ))

    return placements


def export_pnp(
    footprints: List["Footprint"],
    manufacturer: str = "generic",
    config: Optional[PnPExportConfig] = None,
) -> str:
    """
    Export pick-and-place file.

    Args:
        footprints: List of Footprint objects from PCB
        manufacturer: Manufacturer ID
        config: Export configuration

    Returns:
        Formatted CPL as CSV string
    """
    placements = extract_placements(footprints)
    formatter = get_pnp_formatter(manufacturer, config)
    return formatter.format(placements)
