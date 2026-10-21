"""
Hierarchical schematic models.

Represents sheet hierarchy and connections between sheets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.core.sheet_properties import SHEET_FILE_KEYS, SHEET_NAME_KEYS
from kicad_tools.sexp import SExp, parse_string

logger = logging.getLogger(__name__)


@dataclass
class HierarchicalLabelInfo:
    """
    Information about a hierarchical label in a schematic.

    Hierarchical labels connect to sheet pins in the parent sheet.
    """

    name: str
    shape: str  # "input", "output", "bidirectional", "passive"
    position: tuple[float, float]
    rotation: float
    uuid: str

    @classmethod
    def from_sexp(cls, sexp: SExp) -> HierarchicalLabelInfo:
        """Parse from S-expression."""
        name = sexp.get_string(0) or ""

        # Get shape (defaults to "input")
        shape = "input"
        if shape_node := sexp.find("shape"):
            shape = shape_node.get_string(0) or "input"

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
            shape=shape,
            position=pos,
            rotation=rot,
            uuid=uuid,
        )


@dataclass
class SheetPin:
    """
    A hierarchical pin on a sheet symbol.

    Sheet pins connect to hierarchical labels in the child sheet.
    """

    name: str
    direction: str  # "input", "output", "bidirectional", "passive"
    position: tuple[float, float]
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
    position: tuple[float, float]
    size: tuple[float, float]
    pins: list[SheetPin] = field(default_factory=list)

    @property
    def input_pins(self) -> list[SheetPin]:
        """Get all input pins."""
        return [p for p in self.pins if p.direction == "input"]

    @property
    def output_pins(self) -> list[SheetPin]:
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
            if prop_name in SHEET_NAME_KEYS:
                name = prop_value
            elif prop_name in SHEET_FILE_KEYS:
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
    path: str  # Absolute, resolved path to the schematic file
    uuid: str
    children: list[HierarchyNode] = field(default_factory=list)
    sheets: list[SheetInstance] = field(default_factory=list)  # Sheet instances in this schematic
    hierarchical_labels: list[str] = field(
        default_factory=list
    )  # Label names (for backward compat)
    hierarchical_label_info: list[HierarchicalLabelInfo] = field(
        default_factory=list
    )  # Full label details
    parent: HierarchyNode | None = field(default=None, repr=False)
    # KiCad instance path of this placement: ``/<root uuid>`` for the root,
    # then one ``/<sheet symbol uuid>`` per level, i.e. the ``(path ...)``
    # key of the ``(instances ...)`` blocks for symbols on this placement.
    # Two placements of one sheet file share ``path`` but differ here.
    instance_path: str = ""

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

    def find_by_name(self, name: str) -> HierarchyNode | None:
        """Find a child node by name."""
        for child in self.children:
            if child.name == name:
                return child
            found = child.find_by_name(name)
            if found:
                return found
        return None

    def find_by_path(self, path: str) -> HierarchyNode | None:
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

    def all_nodes(self) -> list[HierarchyNode]:
        """Get all nodes in the hierarchy (including self).

        One node per sheet *placement*: a sheet file placed twice appears
        twice, each time with its full subtree (issue #7616). Callers that
        act on files rather than placements (editing, per-file checks)
        should use :meth:`unique_file_nodes` instead.
        """
        result = [self]
        for child in self.children:
            result.extend(child.all_nodes())
        return result

    def unique_file_nodes(self) -> list[HierarchyNode]:
        """Get one node per distinct sheet file, in :meth:`all_nodes` order.

        The first placement of each file is kept. Use this when acting on
        file contents (rewriting a sheet, validating its sheet pins), so a
        repeated sheet is not processed once per placement.
        """
        seen: set[str] = set()
        result = []
        for node in self.all_nodes():
            if node.path in seen:
                continue
            seen.add(node.path)
            result.append(node)
        return result


class HierarchyBuilder:
    """
    Builds a hierarchy tree from schematic files.
    """

    def __init__(self, base_path: str, strict_root: bool = False):
        """
        Initialize with the base path for resolving relative filenames.

        Args:
            base_path: Directory containing the root schematic
            strict_root: Raise if the root schematic cannot be loaded instead
                of returning an empty node. Sub-sheets stay tolerant.
        """
        self.base_path = Path(base_path)
        self.strict_root = strict_root
        # Resolved path -> first node built from that file. Doubles as a
        # parse cache: later placements of the same file reuse its parsed
        # sheets and labels instead of re-reading it.
        self.loaded_files: dict[str, HierarchyNode] = {}
        # Resolved paths on the current recursion stack (the cycle guard).
        self._ancestors: set[str] = set()

    def build(self, root_schematic: str) -> HierarchyNode:
        """
        Build the hierarchy tree starting from the root schematic.

        Args:
            root_schematic: Path to the root .kicad_sch file. A relative
                path is taken relative to the current working directory;
                nothing else in the tree depends on the working directory.

        Returns:
            Root HierarchyNode of the hierarchy tree. Every node's ``path``
            is an absolute, resolved path.
        """
        root_path = Path(root_schematic).resolve()
        self.base_path = root_path.parent
        self._ancestors = set()

        return self._load_schematic(
            str(root_path), "Root", None, base_dir=root_path.parent, sheet_uuid=None
        )

    def _load_schematic(
        self,
        path: str,
        name: str,
        parent: HierarchyNode | None,
        *,
        base_dir: Path | None = None,
        sheet_uuid: str | None = None,
    ) -> HierarchyNode:
        """Load a schematic and recursively load its children.

        ``path`` is the sheet's file name as written in the containing sheet.
        A relative name is resolved against ``base_dir``, the folder of the
        sheet file that contains the sheet symbol (KiCad's rule). It is never
        resolved against the working directory (issue #7614).

        Every placement gets its own node with its full subtree, even when
        the file was already loaded for an earlier placement (issue #7616).
        Only a true cycle (the file is already one of this node's ancestors)
        stops the recursion, with a childless node.

        ``sheet_uuid`` is the UUID of the sheet symbol placing this file
        (``None`` for the root); it extends the parent's instance path.
        """
        if base_dir is None:
            base_dir = self.base_path
        p = Path(path)
        full_path = (p if p.is_absolute() else base_dir / p).resolve()
        # The key is the resolved absolute path, so both spellings of one
        # file dedupe.
        path_str = str(full_path)

        def _instance_path(file_uuid: str) -> str:
            if parent is None:
                return f"/{file_uuid}"
            return f"{parent.instance_path.rstrip('/')}/{sheet_uuid or ''}"

        cached = self.loaded_files.get(path_str)

        if path_str in self._ancestors:
            # Circular reference: the file includes itself or an ancestor.
            logger.warning(
                "Sheet %r (file %r) is its own ancestor in %s; not descending",
                name,
                path,
                path_str,
            )
            return HierarchyNode(
                name=name,
                path=path_str,
                uuid=cached.uuid if cached else "",
                sheets=[],
                hierarchical_labels=cached.hierarchical_labels if cached else [],
                hierarchical_label_info=cached.hierarchical_label_info if cached else [],
                parent=parent,
                instance_path=_instance_path(cached.uuid if cached else ""),
            )

        if cached is not None:
            # Another placement of an already-parsed file: reuse the parse.
            node = HierarchyNode(
                name=name,
                path=path_str,
                uuid=cached.uuid,
                sheets=list(cached.sheets),
                hierarchical_labels=cached.hierarchical_labels,
                hierarchical_label_info=cached.hierarchical_label_info,
                parent=parent,
                instance_path=_instance_path(cached.uuid),
            )
            self._load_children(node, full_path)
            return node

        # Parse the schematic
        try:
            text = full_path.read_text()
            sexp = parse_string(text)
        except Exception as exc:
            if parent is None and self.strict_root:
                raise
            if parent is not None:
                logger.warning(
                    "Sub-sheet %r (file %r) could not be loaded from %s: %s",
                    name,
                    path,
                    path_str,
                    exc,
                )
            # Return empty node if file can't be loaded
            return HierarchyNode(
                name=name,
                path=path_str,
                uuid="",
                parent=parent,
                instance_path=_instance_path(""),
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
        h_label_info = []
        for label_sexp in sexp.find_all("hierarchical_label"):
            label_text = label_sexp.get_string(0)
            if label_text:
                h_labels.append(label_text)
                h_label_info.append(HierarchicalLabelInfo.from_sexp(label_sexp))

        # Create node
        node = HierarchyNode(
            name=name,
            path=path_str,
            uuid=uuid,
            sheets=sheets,
            hierarchical_labels=h_labels,
            hierarchical_label_info=h_label_info,
            parent=parent,
            instance_path=_instance_path(uuid),
        )

        self.loaded_files[path_str] = node
        self._load_children(node, full_path)
        return node

    def _load_children(self, node: HierarchyNode, full_path: Path) -> None:
        """Load ``node``'s sub-sheets, relative to its own folder."""
        self._ancestors.add(node.path)
        try:
            for sheet in node.sheets:
                if not sheet.filename:
                    logger.warning(
                        "Sheet %r in %s has no file property (Sheetfile / Sheet file); skipping",
                        sheet.name,
                        node.path,
                    )
                    continue
                child = self._load_schematic(
                    sheet.filename,
                    sheet.name,
                    node,
                    base_dir=full_path.parent,
                    sheet_uuid=sheet.uuid,
                )
                node.children.append(child)
        finally:
            self._ancestors.discard(node.path)


def build_hierarchy(root_schematic: str, strict_root: bool = False) -> HierarchyNode:
    """
    Build a hierarchy tree from a root schematic.

    Args:
        root_schematic: Path to the root .kicad_sch file
        strict_root: Raise if the root cannot be loaded (missing or
            unparseable) instead of returning an empty root node. Failing
            sub-sheets are always tolerated.

    Returns:
        Root HierarchyNode
    """
    builder = HierarchyBuilder(str(Path(root_schematic).parent), strict_root=strict_root)
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
