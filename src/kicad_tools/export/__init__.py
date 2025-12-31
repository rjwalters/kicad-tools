"""
Manufacturing export tools.

Generate complete manufacturing packages for PCB assembly services:
- Gerber files (with manufacturer presets)
- BOM in manufacturer-specific formats
- Pick-and-place (CPL) files

Example::

    from kicad_tools.export import AssemblyPackage

    # Quick export for JLCPCB
    pkg = AssemblyPackage.create(
        pcb="board.kicad_pcb",
        schematic="board.kicad_sch",
        manufacturer="jlcpcb",
    )
    result = pkg.export("output/")
    print(result)

    # Or use individual exporters
    from kicad_tools.export import GerberExporter, export_bom, export_pnp

    exporter = GerberExporter("board.kicad_pcb")
    exporter.export_for_manufacturer("jlcpcb", "gerbers/")

Supported manufacturers:
- jlcpcb: JLCPCB/LCSC
- pcbway: PCBWay
- oshpark: OSH Park
- seeed: Seeed Fusion
- generic: Generic formats
"""

from .assembly import (
    AssemblyConfig,
    AssemblyPackage,
    AssemblyPackageResult,
    create_assembly_package,
)
from .bom_formats import (
    BOM_FORMATTERS,
    BOMExportConfig,
    BOMFormatter,
    GenericBOMFormatter,
    JLCPCBBOMFormatter,
    PCBWayBOMFormatter,
    SeeedBOMFormatter,
    export_bom,
    get_bom_formatter,
)
from .gerber import (
    MANUFACTURER_PRESETS,
    GerberConfig,
    GerberExporter,
    ManufacturerPreset,
    export_gerbers,
    find_kicad_cli,
)
from .pnp import (
    PNP_FORMATTERS,
    GenericPnPFormatter,
    JLCPCBPnPFormatter,
    PCBWayPnPFormatter,
    PlacementData,
    PnPExportConfig,
    PnPFormatter,
    export_pnp,
    extract_placements,
    get_pnp_formatter,
)

__all__ = [
    # Assembly package
    "AssemblyPackage",
    "AssemblyConfig",
    "AssemblyPackageResult",
    "create_assembly_package",
    # BOM
    "BOMFormatter",
    "BOMExportConfig",
    "JLCPCBBOMFormatter",
    "PCBWayBOMFormatter",
    "SeeedBOMFormatter",
    "GenericBOMFormatter",
    "BOM_FORMATTERS",
    "export_bom",
    "get_bom_formatter",
    # Gerber
    "GerberExporter",
    "GerberConfig",
    "ManufacturerPreset",
    "MANUFACTURER_PRESETS",
    "export_gerbers",
    "find_kicad_cli",
    # Pick-and-place
    "PnPFormatter",
    "PnPExportConfig",
    "PlacementData",
    "JLCPCBPnPFormatter",
    "PCBWayPnPFormatter",
    "GenericPnPFormatter",
    "PNP_FORMATTERS",
    "export_pnp",
    "extract_placements",
    "get_pnp_formatter",
]
