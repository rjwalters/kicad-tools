"""
Pick-and-place (CPL) file generator for assembly services.

Exports component placement data in manufacturer-specific formats.
"""

from __future__ import annotations

import csv
import io
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.exceptions import ConfigurationError

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
    exclude_tht: bool = False  # Exclude through-hole components from CPL
    top_only: bool = False
    bottom_only: bool = False

    # Rotation adjustment (some assemblers have different 0° reference)
    rotation_offset: float = 0.0


class PnPFormatter(ABC):
    """Abstract base class for pick-and-place formatters."""

    manufacturer_id: str = ""
    manufacturer_name: str = ""

    def __init__(
        self,
        config: PnPExportConfig | None = None,
        rotation_corrections: dict[str, float] | None = None,
    ):
        self.config = config or PnPExportConfig()
        self.rotation_corrections: dict[str, float] = rotation_corrections or {}

    @abstractmethod
    def format(self, placements: list[PlacementData]) -> str:
        """Format placement data to manufacturer-specific format."""
        pass

    @abstractmethod
    def get_headers(self) -> list[str]:
        """Get column headers for this format."""
        pass

    def apply_transforms(self, placement: PlacementData) -> PlacementData:
        """Apply coordinate transforms and per-footprint rotation corrections.

        When ``rotation_corrections`` is populated (e.g. from a
        manufacturer preset), the footprint name is matched against the
        correction database and the corresponding offset is added to the
        component rotation **before** the global ``rotation_offset``.
        """
        from kicad_tools.manufacturers.base import match_rotation_correction

        x = placement.x + self.config.x_offset
        y = placement.y + self.config.y_offset

        if self.config.mirror_x:
            x = -x
        if self.config.mirror_y:
            y = -y

        # Per-footprint rotation correction
        fp_correction = match_rotation_correction(placement.footprint, self.rotation_corrections)

        rotation = (placement.rotation + fp_correction + self.config.rotation_offset) % 360

        return PlacementData(
            reference=placement.reference,
            value=placement.value,
            footprint=placement.footprint,
            x=x,
            y=y,
            rotation=rotation,
            layer=placement.layer,
        )

    def filter_placements(self, placements: list[PlacementData]) -> list[PlacementData]:
        """Filter placements based on config."""
        result = placements

        if self.config.top_only:
            result = [p for p in result if p.layer == "F.Cu"]
        elif self.config.bottom_only:
            result = [p for p in result if p.layer == "B.Cu"]

        return result


class JLCPCBPnPFormatter(PnPFormatter):
    """Pick-and-place formatter for JLCPCB assembly service.

    JLCPCB's standard assembly service is SMT-only, so through-hole
    components are excluded from the CPL by default.  Pass
    ``PnPExportConfig(exclude_tht=False)`` to include them.
    """

    manufacturer_id = "jlcpcb"
    manufacturer_name = "JLCPCB"

    def __init__(
        self,
        config: PnPExportConfig | None = None,
        rotation_corrections: dict[str, float] | None = None,
    ):
        if config is None:
            config = PnPExportConfig(exclude_tht=True)
        super().__init__(config, rotation_corrections)

    def get_headers(self) -> list[str]:
        """JLCPCB CPL column headers."""
        return ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]

    def format(self, placements: list[PlacementData]) -> str:
        """
        Format CPL for JLCPCB.

        JLCPCB expects:
        - Designator: Reference designator
        - Val: Component value
        - Package: Footprint name
        - Mid X: X coordinate in mm
        - Mid Y: Y coordinate in mm
        - Rotation: Rotation in degrees
        - Layer: Top or Bottom
        """
        filtered = self.filter_placements(placements)

        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(self.get_headers())

        for placement in sorted(filtered, key=lambda p: p.reference):
            transformed = self.apply_transforms(placement)
            layer = "Top" if transformed.layer == "F.Cu" else "Bottom"
            writer.writerow(
                [
                    transformed.reference,
                    transformed.value,
                    transformed.footprint,
                    f"{transformed.x:.4f}mm",
                    f"{transformed.y:.4f}mm",
                    f"{transformed.rotation:.1f}",
                    layer,
                ]
            )

        return output.getvalue()


class PCBWayPnPFormatter(PnPFormatter):
    """Pick-and-place formatter for PCBWay assembly service."""

    manufacturer_id = "pcbway"
    manufacturer_name = "PCBWay"

    def get_headers(self) -> list[str]:
        """PCBWay CPL column headers."""
        return [
            "Designator",
            "Footprint",
            "Mid X",
            "Mid Y",
            "Ref X",
            "Ref Y",
            "Pad X",
            "Pad Y",
            "Layer",
            "Rotation",
            "Comment",
        ]

    def format(self, placements: list[PlacementData]) -> str:
        """
        Format CPL for PCBWay.

        PCBWay expects more detailed placement info.
        """
        filtered = self.filter_placements(placements)

        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(self.get_headers())

        for placement in sorted(filtered, key=lambda p: p.reference):
            transformed = self.apply_transforms(placement)
            layer = "T" if transformed.layer == "F.Cu" else "B"
            writer.writerow(
                [
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
                ]
            )

        return output.getvalue()


class SeeedPnPFormatter(PnPFormatter):
    """Pick-and-place formatter for Seeed Fusion assembly service."""

    manufacturer_id = "seeed"
    manufacturer_name = "Seeed Fusion"

    def get_headers(self) -> list[str]:
        """Seeed Fusion CPL column headers."""
        return ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]

    def format(self, placements: list[PlacementData]) -> str:
        """
        Format CPL for Seeed Fusion.

        Seeed Fusion accepts a standard CSV pick-and-place format
        with millimetre coordinates and Top/Bottom layer designation.
        """
        filtered = self.filter_placements(placements)

        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(self.get_headers())

        for placement in sorted(filtered, key=lambda p: p.reference):
            transformed = self.apply_transforms(placement)
            layer = "Top" if transformed.layer == "F.Cu" else "Bottom"
            writer.writerow(
                [
                    transformed.reference,
                    transformed.value,
                    transformed.footprint,
                    f"{transformed.x:.4f}mm",
                    f"{transformed.y:.4f}mm",
                    f"{transformed.rotation:.1f}",
                    layer,
                ]
            )

        return output.getvalue()


class GenericPnPFormatter(PnPFormatter):
    """Generic pick-and-place formatter."""

    manufacturer_id = "generic"
    manufacturer_name = "Generic"

    def get_headers(self) -> list[str]:
        """Generic CPL column headers."""
        return ["Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side"]

    def format(self, placements: list[PlacementData]) -> str:
        """Format CPL in generic CSV format."""
        filtered = self.filter_placements(placements)

        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(self.get_headers())

        for placement in sorted(filtered, key=lambda p: p.reference):
            transformed = self.apply_transforms(placement)
            side = "top" if transformed.layer == "F.Cu" else "bottom"
            writer.writerow(
                [
                    transformed.reference,
                    transformed.value,
                    transformed.footprint,
                    f"{transformed.x:.4f}",
                    f"{transformed.y:.4f}",
                    f"{transformed.rotation:.1f}",
                    side,
                ]
            )

        return output.getvalue()


# Registry of available formatters
PNP_FORMATTERS: dict[str, type[PnPFormatter]] = {
    "jlcpcb": JLCPCBPnPFormatter,
    "pcbway": PCBWayPnPFormatter,
    "seeed": SeeedPnPFormatter,
    "generic": GenericPnPFormatter,
}


def get_pnp_formatter(
    manufacturer: str,
    config: PnPExportConfig | None = None,
    rotation_corrections: dict[str, float] | None = None,
) -> PnPFormatter:
    """
    Get pick-and-place formatter for a manufacturer.

    Args:
        manufacturer: Manufacturer ID (jlcpcb, pcbway, generic)
        config: Export configuration
        rotation_corrections: Per-footprint rotation corrections (pattern -> degrees).
            When provided, these are applied during ``apply_transforms()``.

    Returns:
        PnPFormatter for the specified manufacturer

    Raises:
        ValueError: If manufacturer is not supported
    """
    formatter_class = PNP_FORMATTERS.get(manufacturer.lower())
    if formatter_class is None:
        available = list(PNP_FORMATTERS.keys())
        raise ConfigurationError(
            f"Unknown manufacturer: {manufacturer}",
            context={"manufacturer": manufacturer, "available": available},
            suggestions=[f"Use one of: {', '.join(available)}"],
        )
    return formatter_class(config, rotation_corrections)


def extract_placements(
    footprints: list[Footprint],
    config: PnPExportConfig | None = None,
) -> list[PlacementData]:
    """
    Extract placement data from PCB footprints.

    Args:
        footprints: List of Footprint objects from PCB
        config: Optional export config for filtering (exclude_tht, include_dnp)

    Returns:
        List of PlacementData for assembly
    """
    config = config or PnPExportConfig()
    placements = []
    for fp in footprints:
        # Skip footprints excluded from position files
        if getattr(fp, "exclude_from_pos_files", False):
            continue

        # Skip DNP (Do Not Place) footprints unless explicitly included
        if not config.include_dnp and getattr(fp, "dnp", False):
            continue

        # Skip through-hole footprints when exclude_tht is enabled
        if config.exclude_tht and getattr(fp, "attr", "") == "through_hole":
            continue

        x, y = fp.position
        placements.append(
            PlacementData(
                reference=fp.reference,
                value=fp.value,
                footprint=fp.name,
                x=x,
                y=y,
                rotation=fp.rotation,
                layer=fp.layer,
            )
        )

    return placements


def _ref_sort_key(reference: str) -> tuple[str, int, str]:
    """Natural sort key for reference designators (R6 before R20)."""
    match = re.match(r"([A-Za-z_]*)(\d*)(.*)", reference)
    if match is None:  # pragma: no cover - regex always matches
        return (reference, -1, "")
    prefix, digits, rest = match.groups()
    return (prefix, int(digits) if digits else -1, rest)


def extract_tht_exclusions(
    footprints: list[Footprint],
    config: PnPExportConfig | None = None,
) -> list[PlacementData]:
    """Return the through-hole placements the CPL's THT filter drops.

    These are the components an SMT-only assembler must hand-solder
    (or wave/selective-solder): they are present on the board and in
    the BOM, but :func:`extract_placements` omits them from the
    pick-and-place file when ``config.exclude_tht`` is enabled.

    The same pre-filters as :func:`extract_placements` apply
    (``exclude_from_pos_files``, DNP), so the returned set is exactly
    the difference between the unfiltered and THT-filtered CPLs.

    Args:
        footprints: List of Footprint objects from PCB
        config: Effective export config.  When ``exclude_tht`` is
            False (or config is None), no parts are excluded from the
            CPL, so this returns an empty list.

    Returns:
        PlacementData for each excluded THT component, in natural
        reference order (R6 before R20).
    """
    config = config or PnPExportConfig()
    if not config.exclude_tht:
        return []

    excluded = []
    for fp in footprints:
        if getattr(fp, "exclude_from_pos_files", False):
            continue
        if not config.include_dnp and getattr(fp, "dnp", False):
            continue
        if getattr(fp, "attr", "") != "through_hole":
            continue

        x, y = fp.position
        excluded.append(
            PlacementData(
                reference=fp.reference,
                value=fp.value,
                footprint=fp.name,
                x=x,
                y=y,
                rotation=fp.rotation,
                layer=fp.layer,
            )
        )

    return sorted(excluded, key=lambda p: _ref_sort_key(p.reference))


def group_tht_exclusions(placements: list[PlacementData]) -> list[dict]:
    """Group excluded THT placements for report/README presentation.

    Groups by (value, footprint) and returns BOM-style rows::

        [{"value": ..., "footprint": ..., "qty": N, "refs": "D1, D2"}]

    Rows are ordered by the natural sort key of each group's first
    reference, and references within a row are naturally sorted.
    """
    groups: dict[tuple[str, str], list[str]] = {}
    for p in placements:
        groups.setdefault((p.value, p.footprint), []).append(p.reference)

    rows = []
    for (value, footprint), refs in groups.items():
        refs_sorted = sorted(refs, key=_ref_sort_key)
        rows.append(
            {
                "value": value,
                "footprint": footprint,
                "qty": len(refs_sorted),
                "refs": ", ".join(refs_sorted),
            }
        )

    rows.sort(key=lambda row: _ref_sort_key(row["refs"].split(", ")[0]))
    return rows


def get_aux_origin(pcb_path: str | Path) -> tuple[float, float]:
    """Read auxiliary axis origin from PCB setup section.

    The auxiliary axis origin is set by the user in KiCad (Place -> Drill/Place
    File Origin) and is used as the coordinate reference for manufacturing
    output files (Gerbers, drill files, pick-and-place).

    Args:
        pcb_path: Path to the .kicad_pcb file

    Returns:
        Tuple (x, y) of the auxiliary origin in mm, or (0.0, 0.0) if not set.
    """
    from ..schema.pcb import PCB

    pcb = PCB.load(pcb_path)
    setup = pcb.setup
    if setup is None:
        return (0.0, 0.0)
    return setup.aux_axis_origin


def export_pnp(
    footprints: list[Footprint],
    manufacturer: str = "generic",
    config: PnPExportConfig | None = None,
    pcb_path: str | Path | None = None,
    rotation_corrections: dict[str, float] | None = None,
) -> str:
    """
    Export pick-and-place file.

    Args:
        footprints: List of Footprint objects from PCB
        manufacturer: Manufacturer ID
        config: Export configuration
        pcb_path: Optional path to the .kicad_pcb file. When provided and
            config.use_aux_origin is True (the default), the auxiliary axis
            origin is read from the PCB and subtracted from all component
            coordinates so that output positions are relative to the
            board's manufacturing origin.
        rotation_corrections: Per-footprint rotation corrections
            (pattern -> degrees).  When provided, these are applied
            during formatting via ``PnPFormatter.apply_transforms()``.

    Returns:
        Formatted CPL as CSV string
    """
    # Resolve the effective config through the formatter FIRST so
    # manufacturer defaults (e.g., JLCPCB defaults to exclude_tht=True)
    # apply regardless of which branch below runs.  The formatter is the
    # single source of truth for the effective config; synthesizing a
    # config here would silently drop those defaults (issue #3618).
    formatter = get_pnp_formatter(manufacturer, config, rotation_corrections)
    config = formatter.config

    # Auto-apply auxiliary origin offset when a PCB path is provided
    if pcb_path is not None and config.use_aux_origin:
        aux_x, aux_y = get_aux_origin(pcb_path)
        if aux_x != 0.0 or aux_y != 0.0:
            # dataclasses.replace preserves every other field of the
            # effective config (exclude_tht, include_dnp, ...).
            config = replace(
                config,
                x_offset=config.x_offset - aux_x,
                y_offset=config.y_offset - aux_y,
            )
            formatter = get_pnp_formatter(manufacturer, config, rotation_corrections)

    placements = extract_placements(footprints, formatter.config)
    return formatter.format(placements)
