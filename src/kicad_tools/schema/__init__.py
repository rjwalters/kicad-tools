"""KiCad schematic data models."""

from .bom import (
    BOM,
    BOMGroup,
    BOMItem,
    extract_bom,
)
from .field_geometry import (
    DEFAULT_FIELD_CLEARANCE_MM,
    default_field_positions,
    field_offset_mm,
    placed_body_bbox,
)
from .hierarchy import (
    HierarchyBuilder,
    HierarchyNode,
    SheetInstance,
    SheetPin,
    build_hierarchy,
)
from .label import GlobalLabel, HierarchicalLabel, Label
from .library import LibraryManager, LibraryPin, LibrarySymbol, SymbolLibrary
from .pcb import (
    PCB,
    BoardNetClass,
    Footprint,
    Layer,
    Net,
    Pad,
    Segment,
    Setup,
    StackupLayer,
    Via,
    Zone,
)
from .schematic import Schematic
from .symbol import SymbolInstance, SymbolPin
from .wire import Junction, NoConnect, Wire

__all__ = [
    "Schematic",
    "SymbolInstance",
    "SymbolPin",
    "Wire",
    "Junction",
    "NoConnect",
    "Label",
    "HierarchicalLabel",
    "GlobalLabel",
    "SymbolLibrary",
    "LibrarySymbol",
    "LibraryPin",
    "LibraryManager",
    "HierarchyNode",
    "SheetInstance",
    "SheetPin",
    "HierarchyBuilder",
    "build_hierarchy",
    "BOM",
    "BOMItem",
    "BOMGroup",
    "extract_bom",
    "DEFAULT_FIELD_CLEARANCE_MM",
    "default_field_positions",
    "field_offset_mm",
    "placed_body_bbox",
    "PCB",
    "Layer",
    "Net",
    "Footprint",
    "Pad",
    "Segment",
    "Via",
    "Zone",
    "Setup",
    "StackupLayer",
    "BoardNetClass",
]
