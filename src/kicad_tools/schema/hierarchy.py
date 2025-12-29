"""
Hierarchical schematic models.

Represents sheet hierarchy and connections between sheets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..core.sexp import SExp, parse_sexp


@dataclass
class SheetPin:
    """
    A hierarchical pin on a sheet symbol.

    Sheet pins connect to hierarchical labels in the child sheet.
    """

    name: str
    direction: str  # "input", "output", "bidirectional", "passive"
    position: Tuple[float, float]
    rotation: float
    uuid: str

    @classmethod
    def from_sexp(cls, sexp: SExp) -> SheetPin:
        """Parse from S-expression."""
        name = sexp.get_string(0) or ""
        direction = sexp.get_string(1) or "passive"

        pos = (0.0, 0.0)
        rot = 0.0
        uuid = ""

        if at := sexp.find("at"):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)
            rot = at.get_float(2) or 0

        if uuid_node := sexp.find("uuid"):
            uuid = uuid_node.get_string(0) or ""

        return cls(
            name=name,
            direction=direction,
            position=pos,
            rotation=rot,
            uuid=uuid,
        )


@dataclass
class SheetInstance:
    """
    A hierarchical sheet instance in a schematic.

    Represents a sub-schematic that is instantiated in the parent.
    """

    name: str  # Display name (from Sheetname property)
    filename: str  # Filename (from Sheetfile property)
    uuid: str
    position: Tuple[float, float]
    size: Tuple[float, float]
    pins: List[SheetPin] = field(default_factory=list)

    @property
    def input_pins(self) -> List[SheetPin]:
        """Get all input pins."""
        return [p for p in self.pins if p.direction == "input"]

    @property
    def output_pins(self) -> List[SheetPin]:
        """Get all output pins."""
        return [p for p in self.pins if p.direction == "output"]

    @classmethod
    def from_sexp(cls, sexp: SExp) -> SheetInstance:
        """Parse from S-expression."""
        pos = (0.0, 0.0)
        size = (50.8, 25.4)
        uuid = ""
        name = ""
        filename = ""

        if at := sexp.find("at"):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)

        if sz := sexp.find("size"):
            size = (sz.get_float(0) or 50.8, sz.get_float(1) or 25.4)

        if uuid_node := sexp.find("uuid"):
            uuid = uuid_node.get_string(0) or ""

        # Parse properties
        for prop in sexp.find_all("property"):
            prop_name = prop.get_string(0)
            prop_value = prop.get_string(1) or ""
            if prop_name == "Sheetname":
                name = prop_value
            elif prop_name == "Sheetfile":
                filename = prop_value

        # Parse pins
        pins = []
        for pin_sexp in sexp.find_all("pin"):
            pins.append(SheetPin.from_sexp(pin_sexp))

        return cls(
            name=name,
            filename=filename,
            uuid=uuid,
            position=pos,
            size=size,
            pins=pins,
        )

    def __repr__(self) -> str:
        return f"SheetInstance({self.name!r}, file={self.filename!r}, pins={len(self.pins)})"


@dataclass
class HierarchyNode:
    """
    A node in the schematic hierarchy tree.

    Represents a schematic and its children.
    """

    name: str
    path: str  # Full path to schematic file
    uuid: str
    children: List[HierarchyNode] = field(default_factory=list)
    sheets: List[SheetInstance] = field(default_factory=list)  # Sheet instances in this schematic
    hierarchical_labels: List[str] = field(default_factory=list)  # Labels in this sheet
    parent: Optional[HierarchyNode] = field(default=None, repr=False)

    @property
    def depth(self) -> int:
        """Get the depth in the hierarchy (root = 0)."""
        if self.parent is None:
            return 0
        return self.parent.depth + 1

    @property
    def is_root(self) -> bool:
        return self.parent is None

    @property
    def is_leaf(self) -> bool:
        return len(self.children) == 0

    def get_path_string(self) -> str:
        """Get the hierarchical path as a string."""
        if self.parent is None:
            return "/"
        parent_path = self.parent.get_path_string()
        if parent_path == "/":
            return f"/{self.name}"
        return f"{parent_path}/{self.name}"

    def find_by_name(self, name: str) -> Optional[HierarchyNode]:
        """Find a child node by name."""
        for child in self.children:
            if child.name == name:
                return child
            found = child.find_by_name(name)
            if found:
                return found
        return None

    def find_by_path(self, path: str) -> Optional[HierarchyNode]:
        """Find a node by hierarchical path (e.g., '/Power/Regulator')."""
        parts = [p for p in path.split("/") if p]
        current = self
        for part in parts:
            found = None
            for child in current.children:
                if child.name == part:
                    found = child
                    break
            if not found:
                return None
            current = found
        return current

    def all_nodes(self) -> List[HierarchyNode]:
        """Get all nodes in the hierarchy (including self)."""
        result = [self]
        for child in self.children:
            result.extend(child.all_nodes())
        return result


class HierarchyBuilder:
    """
    Builds a hierarchy tree from schematic files.
    """

    def __init__(self, base_path: str):
        """
        Initialize with the base path for resolving relative filenames.

        Args:
            base_path: Directory containing the root schematic
        """
        self.base_path = Path(base_path)
        self.loaded_files: Dict[str, HierarchyNode] = {}

    def build(self, root_schematic: str) -> HierarchyNode:
        """
        Build the hierarchy tree starting from the root schematic.

        Args:
            root_schematic: Path to the root .kicad_sch file

        Returns:
            Root HierarchyNode of the hierarchy tree
        """
        root_path = Path(root_schematic)
        self.base_path = root_path.parent

        return self._load_schematic(str(root_path), "Root", None)

    def _load_schematic(
        self,
        path: str,
        name: str,
        parent: Optional[HierarchyNode],
    ) -> HierarchyNode:
        """Load a schematic and recursively load its children."""
        full_path = Path(path)
        # Only join with base_path if the path doesn't already exist
        # (handles both absolute paths and paths that are already valid from CWD)
        if not full_path.exists() and not full_path.is_absolute():
            full_path = self.base_path / path

        # Check for circular references
        path_str = str(full_path)
        if path_str in self.loaded_files:
            # Return a reference to the existing node (but with different parent)
            existing = self.loaded_files[path_str]
            return HierarchyNode(
                name=name,
                path=path_str,
                uuid=existing.uuid,
                sheets=[],
                hierarchical_labels=existing.hierarchical_labels,
                parent=parent,
            )

        # Parse the schematic
        try:
            text = full_path.read_text()
            sexp = parse_sexp(text)
        except Exception:
            # Return empty node if file can't be loaded
            return HierarchyNode(
                name=name,
                path=path_str,
                uuid="",
                parent=parent,
            )

        # Get UUID
        uuid = ""
        if uuid_node := sexp.find("uuid"):
            uuid = uuid_node.get_string(0) or ""

        # Parse sheets
        sheets = []
        for sheet_sexp in sexp.find_all("sheet"):
            sheets.append(SheetInstance.from_sexp(sheet_sexp))

        # Parse hierarchical labels
        h_labels = []
        for label_sexp in sexp.find_all("hierarchical_label"):
            label_text = label_sexp.get_string(0)
            if label_text:
                h_labels.append(label_text)

        # Create node
        node = HierarchyNode(
            name=name,
            path=path_str,
            uuid=uuid,
            sheets=sheets,
            hierarchical_labels=h_labels,
            parent=parent,
        )

        self.loaded_files[path_str] = node

        # Recursively load children
        for sheet in sheets:
            child = self._load_schematic(
                sheet.filename,
                sheet.name,
                node,
            )
            node.children.append(child)

        return node


def build_hierarchy(root_schematic: str) -> HierarchyNode:
    """
    Build a hierarchy tree from a root schematic.

    Args:
        root_schematic: Path to the root .kicad_sch file

    Returns:
        Root HierarchyNode
    """
    builder = HierarchyBuilder(str(Path(root_schematic).parent))
    return builder.build(root_schematic)


def print_hierarchy_tree(node: HierarchyNode, indent: str = "") -> str:
    """
    Format a hierarchy tree as a string.

    Args:
        node: Root node to print
        indent: Current indentation

    Returns:
        Formatted tree string
    """
    lines = []

    # Print this node
    if node.is_root:
        lines.append(f"{indent}📁 {Path(node.path).name} (root)")
    else:
        lines.append(f"{indent}📄 {node.name}")

    # Print hierarchical labels
    if node.hierarchical_labels:
        for label in node.hierarchical_labels:
            lines.append(f"{indent}  ⚡ {label}")

    # Print children
    child_indent = indent + "  "
    for i, child in enumerate(node.children):
        is_last = i == len(node.children) - 1
        prefix = "└─ " if is_last else "├─ "
        child_lines = print_hierarchy_tree(child, child_indent + "  ")
        # Insert prefix on first line
        child_lines_list = child_lines.split("\n")
        if child_lines_list:
            child_lines_list[0] = f"{indent}{prefix}{child_lines_list[0].lstrip()}"
            lines.extend(child_lines_list)

    return "\n".join(lines)
