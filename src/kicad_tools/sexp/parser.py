#!/usr/bin/env python3
"""
KiCad S-expression Parser and Generator

A robust parser for KiCad schematic and PCB files that supports:
- Full round-trip editing (parse → modify → serialize)
- XPath-like queries for finding elements
- Proper handling of strings, numbers, and nested structures

Performance optimizations in this module:
- Uses __slots__ on SExp for reduced memory and faster attribute access
- Optimized Parser with minimal function call overhead
- Pre-compiled character sets for fast whitespace/delimiter detection

Usage:
    from kicad_tools.sexp import SExp, parse_file, parse_string

    # Parse a schematic
    doc = parse_file("project.kicad_sch")

    # Find elements
    symbols = doc.find_all("symbol")
    dac = doc.find("symbol", lib_id="Audio:PCM5122PW")

    # Modify
    dac["property", "Value"].value = "PCM5122"

    # Write back
    doc.write("project.kicad_sch")
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any


class SExp:
    """
    S-expression node representing a KiCad element.

    Can be either:
    - An atom (string, number, or symbol)
    - A list starting with a name followed by children

    Examples:
        (wire (pts (xy 10 20) (xy 30 40)))
        → SExp(name="wire", children=[SExp(name="pts", ...)])

        "Hello World"
        → SExp(name=None, value="Hello World")

        123.45
        → SExp(name=None, value=123.45)

    Uses __slots__ for memory efficiency and faster attribute access.

    Position tracking:
        When parsed with track_positions=True, nodes include _line and _column
        attributes (1-indexed) pointing to their start position in the source file.
    """

    __slots__ = ("name", "children", "value", "_inline", "_original_str", "_line", "_column")

    def __init__(
        self,
        name: str | None = None,
        children: list[SExp] | None = None,
        value: str | int | float | None = None,
        _inline: bool = False,
        _original_str: str | None = None,
        _line: int = 0,
        _column: int = 0,
    ):
        if name is not None and value is not None:
            raise ValueError("SExp cannot have both name and value")
        self.name = name
        self.children = children if children is not None else []
        self.value = value
        self._inline = _inline
        self._original_str = _original_str
        self._line = _line
        self._column = _column

    @property
    def line(self) -> int:
        """Line number (1-indexed) where this element starts. 0 if not tracked."""
        return self._line

    @property
    def column(self) -> int:
        """Column number (1-indexed) where this element starts. 0 if not tracked."""
        return self._column

    @property
    def has_position(self) -> bool:
        """Check if this node has position information."""
        return self._line > 0 and self._column > 0

    @property
    def is_atom(self) -> bool:
        """True if this is a leaf node (string, number, symbol)."""
        return self.name is None and not self.children

    @property
    def is_list(self) -> bool:
        """True if this is a list node."""
        return self.name is not None or bool(self.children)

    def __getitem__(self, key: str | int | tuple) -> SExp:
        """
        Access children by name or index.

        Examples:
            node["property"]           # First child named "property"
            node["property", "Value"]  # Property with specific attribute
            node[0]                    # First child
        """
        if isinstance(key, int):
            return self.children[key]

        if isinstance(key, tuple):
            # Multi-level query: node["property", "Value"]
            result = self
            for k in key:
                result = result[k]
            return result

        # String key - find first child with matching name
        for child in self.children:
            if child.name == key:
                return child

        # Build helpful error message with available child names
        child_names = [c.name for c in self.children if c.name]
        unique_names = sorted(set(child_names))

        if unique_names:
            # Truncate if too many
            if len(unique_names) > 10:
                shown = unique_names[:10]
                suffix = f", ... and {len(unique_names) - 10} more"
            else:
                shown = unique_names
                suffix = ""
            available = f"Available: {', '.join(shown)}{suffix}"
        else:
            available = "No named children"

        node_desc = f"'{self.name}'" if self.name else "root"
        raise KeyError(f"No child named '{key}' in {node_desc}. {available}")

    def get(self, key: str, default: Any = None) -> SExp | None:
        """Get child by name, returning default if not found."""
        try:
            return self[key]
        except KeyError:
            return default

    def find(self, name: str, **attrs) -> SExp | None:
        """
        Find first descendant matching name and attributes.

        Note: This searches descendants only, not self.

        Example:
            doc.find("symbol", lib_id="Audio:PCM5122PW")
        """
        # Search descendants only (not self)
        for child in self.children:
            for node in child.iter_all():
                if node.name == name:
                    if all(self._match_attr(node, k, v) for k, v in attrs.items()):
                        return node
        return None

    def find_all(self, name: str, **attrs) -> list[SExp]:
        """Find all descendants matching name and attributes.

        Note: This searches descendants only, not self. To include self,
        use iter_all() directly.
        """
        results = []
        # Search descendants only (not self) - iterate children and their descendants
        for child in self.children:
            for node in child.iter_all():
                if node.name == name:
                    if all(self._match_attr(node, k, v) for k, v in attrs.items()):
                        results.append(node)
        return results

    def _match_attr(self, node: SExp, attr: str, value: Any) -> bool:
        """Check if node has attribute matching value."""
        child = node.get(attr)
        if child is None:
            # Check if it's stored as a direct child value
            return any(c.is_atom and c.value == value for c in node.children)

        # Check child's first atom value
        if child.children and child.children[0].is_atom:
            return child.children[0].value == value
        return False

    def iter_all(self) -> Iterator[SExp]:
        """Iterate over this node and all descendants."""
        yield self
        for child in self.children:
            yield from child.iter_all()

    def append(self, child: SExp) -> SExp:
        """Add a child node."""
        self.children.append(child)
        return child

    def remove(self, child: SExp) -> bool:
        """Remove a child node."""
        try:
            self.children.remove(child)
            return True
        except ValueError:
            return False

    def get_atoms(self) -> list[str | int | float]:
        """Get all atom values from direct children."""
        return [c.value for c in self.children if c.is_atom]

    def get_first_atom(self) -> str | int | float | None:
        """Get the first atom value from children."""
        for c in self.children:
            if c.is_atom:
                return c.value
        return None

    def set_atom(self, index: int, value: str | int | float):
        """Set the atom value at given index."""
        atom_idx = 0
        for i, c in enumerate(self.children):
            if c.is_atom:
                if atom_idx == index:
                    self.children[i] = SExp(value=value)
                    return
                atom_idx += 1
        raise IndexError(f"No atom at index {index}")

    def to_string(self, indent: int = 0, compact: bool = False) -> str:
        """
        Serialize to S-expression string matching KiCad format.

        Args:
            indent: Current indentation level
            compact: If True, minimize whitespace
        """
        if self.is_atom:
            return self._format_atom()

        if not self.name and not self.children:
            return "()"

        # KiCad uses 2 spaces for indentation
        tab = "  "
        tabs = tab * indent

        # Check if should render inline
        if compact or self._should_inline():
            parts = [self.name] if self.name else []
            parts.extend(c.to_string(compact=True) for c in self.children)
            return "(" + " ".join(parts) + ")"

        # Multi-line formatting matching KiCad style
        lines = []

        # Opening with name (names are never quoted in KiCad format)
        if self.name:
            lines.append(f"{tabs}({self.name}")
        else:
            lines.append(f"{tabs}(")

        # Determine if this node forces structured children on separate lines
        force_structured_on_lines = self.name in {
            "kicad_sch",
            "kicad_pcb",
            "lib_symbols",
            "symbol",
            "footprint",
            "title_block",
            "sheet",
            "sheet_instances",
            "instances",
            "project",
            "effects",
            "general",
            "layers",
            "layer",
            "stackup",
            "setup",
            "pcbplotparams",
            "gr_rect",
            "gr_circle",
            "gr_line",
            "gr_text",
            "gr_arc",
            "gr_poly",
            "zone",
            "segment",
            "via",
            "pad",
            "fp_text",
            "fp_line",
            "fp_circle",
            "net",
            "property",
            "wire",
            "junction",
            "label",
            "hierarchical_label",
            "stroke",
            "font",
        }

        # Track if we've started putting things on new lines
        started_new_lines = False

        for _i, child in enumerate(self.children):
            if child.is_atom:
                if indent == 0 or started_new_lines:
                    # Already on new lines, continue that way
                    lines.append(f"{tabs}{tab}{child.to_string(compact=True)}")
                    started_new_lines = True
                else:
                    # Atoms go on same line as parent opener
                    lines[-1] += " " + child.to_string(compact=True)
            elif child._should_inline():
                if indent == 0:
                    # Root level: each child on own line
                    lines.append(f"{tabs}{tab}{child.to_string(compact=True)}")
                    started_new_lines = True
                elif force_structured_on_lines or started_new_lines:
                    # Structured nodes get their own lines
                    lines.append(f"{tabs}{tab}{child.to_string(compact=True)}")
                    started_new_lines = True
                else:
                    # Inline children on same line
                    lines[-1] += " " + child.to_string(compact=True)
            else:
                # Complex children always on new lines
                child_str = child.to_string(indent=indent + 1)
                lines.append(child_str)
                started_new_lines = True

        # Closing paren
        last_line = lines[-1]
        if last_line.rstrip().endswith(")") or indent == 0:
            # Previous child ended with ), or root level - put closing on new line
            lines.append(f"{tabs})")
        else:
            # Atoms on same line, close inline
            lines[-1] += ")"

        return "\n".join(lines)

    def _should_inline(self) -> bool:
        """Determine if this node should be rendered inline."""
        if not self.children:
            return True

        # Never inline top-level or major structural elements
        never_inline = {
            # Top-level containers
            "kicad_sch",
            "kicad_pcb",
            "lib_symbols",
            "symbol",
            "footprint",
            "title_block",
            "sheet",
            "sheet_instances",
            "instances",
            "project",
            "path",
            "property",
            "effects",
            "font",
            "pin",
            "rectangle",
            "fill",
            "polyline",
            "arc",
            "circle",
            "text",
            "wire",
            "junction",
            "label",
            "hierarchical_label",
            "global_label",
            "no_connect",
            # PCB-specific
            "general",
            "layers",
            "layer",
            "stackup",
            "setup",
            "pcbplotparams",
            "net",
            "gr_rect",
            "gr_circle",
            "gr_line",
            "gr_text",
            "zone",
            "segment",
            "via",
            "pad",
            "fp_text",
            "fp_line",
            "fp_circle",
            # Common nested structures
            "stroke",
        }
        if self.name in never_inline:
            return False

        # Simple nodes with only atoms (and short enough)
        if all(c.is_atom for c in self.children):
            total_len = len(self.name or "") + sum(
                len(self._format_value(c.value)) + 1 for c in self.children
            )
            return total_len < 60

        # Known inline elements in KiCad
        inline_names = {
            "xy",
            "at",
            "size",
            "stroke",
            "width",
            "type",
            "color",
            "diameter",
            "length",
            "thickness",
            "hide",
            "name",
            "number",
            "uuid",
            "justify",
            "start",
            "end",
            "mid",
            "pts",
            "exclude_from_sim",
            "in_bom",
            "on_board",
            "dnp",
            "fields_autoplaced",
            "pin_numbers",
            "pin_names",
            "offset",
        }
        if self.name in inline_names:
            # But only if children are simple
            if all(c.is_atom or c._should_inline() for c in self.children):
                inline_str = (
                    "("
                    + self.name
                    + " "
                    + " ".join(c.to_string(compact=True) for c in self.children)
                    + ")"
                )
                return len(inline_str) < 80
            return False

        return False

    def _format_value(self, value) -> str:
        """Format a value for length calculation."""
        if value is None:
            return ""
        if isinstance(value, str):
            if self._needs_quoting(value):
                return f'"{value}"'
            return value
        return str(value)

    def _format_atom(self) -> str:
        """Format an atom value."""
        if self.value is None:
            return ""
        if isinstance(self.value, str):
            # Check if needs quoting
            if self._needs_quoting(self.value):
                escaped = self.value.replace("\\", "\\\\").replace('"', '\\"')
                escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
                return f'"{escaped}"'
            return self.value
        # For numbers, use original string if available (for round-trip preservation)
        if self._original_str is not None:
            return self._original_str
        if isinstance(self.value, float):
            # Format floats without unnecessary decimals
            if self.value == int(self.value):
                return str(int(self.value))
            return f"{self.value:.6g}"
        return str(self.value)

    @staticmethod
    def _is_valid_name(s: str) -> bool:
        """Check if string is a valid unquoted S-expression name/identifier."""
        if not s:
            return False
        # Names can't start with a digit or dash (would look like number)
        if s[0].isdigit() or s[0] == "-":
            return False
        # Names can't contain special chars that need quoting
        if any(c in s for c in ' \t\n\r"()'):
            return False
        # Names can contain _ : / and other identifier-like chars
        return True

    def _needs_quoting(self, s: str) -> bool:
        """Check if string value needs to be quoted when serialized."""
        if not s:
            return True

        # Known unquoted keywords in KiCad format
        unquoted_keywords = {
            # Boolean-like
            "yes",
            "no",
            "true",
            "false",
            # Visibility
            "hide",
            "show",
            # Fill types
            "none",
            "outline",
            "background",
            "solid",
            # Stroke types
            "default",
            "dash",
            "dash_dot",
            "dash_dot_dot",
            "dot",
            # Justify
            "left",
            "right",
            "center",
            "top",
            "bottom",
            "mirror",
            # Pin types
            "input",
            "output",
            "bidirectional",
            "tri_state",
            "passive",
            "free",
            "unspecified",
            "power_in",
            "power_out",
            "open_collector",
            "open_emitter",
            "no_connect",
            "line",
            "inverted",
            "clock",
            "inverted_clock",
            "input_low",
            "clock_low",
            "output_low",
            "edge_clock_high",
            "non_logic",
            # Layer type keywords in PCB
            "signal",
            "power",
            "user",
            "mixed",
            "jumper",
            # Pad types
            "thru_hole",
            "smd",
            "connect",
            "np_thru_hole",
            # Pad shapes
            "rect",
            "oval",
            "circle",
            "roundrect",
            "trapezoid",
            "custom",
            # fp_text types
            "reference",
            "value",
            # Zone connection types
            "thermal_reliefs",
            "full",
            # Zone fill modes
            "hatch",
            "hatched",
            # Zone hatch types
            "edge",
            # Via types
            "blind",
            "micro",
            "through",
            # Arc/curve modes
            "arc",
            "start",
            "mid",
            "end",
            # Text effects
            "italic",
            "bold",
            # Module/footprint attributes
            "through_hole",
            "virtual",
            "exclude_from_pos_files",
            "exclude_from_bom",
            "board_only",
            "dnp",
            # Net class
            "clearance",
            "trace_width",
            "via_dia",
            "via_drill",
            "uvia_dia",
            "uvia_drill",
            "diff_pair_width",
            "diff_pair_gap",
        }

        if s in unquoted_keywords:
            return False

        # Don't quote hex numbers
        if s.startswith("0x") or s.startswith("0X"):
            return False

        # Quote everything else - KiCad uses quoted strings liberally
        return True

    def __repr__(self) -> str:
        if self.is_atom:
            return f"SExp(value={self.value!r})"
        if self.name:
            return f"SExp(name={self.name!r}, children=[{len(self.children)} items])"
        return f"SExp(children=[{len(self.children)} items])"

    # =========================================================================
    # Backward compatibility with core/sexp.py API
    # These properties and methods provide compatibility with the older API
    # that used 'tag' and 'values' instead of 'name' and 'children'.
    # =========================================================================

    @property
    def tag(self) -> str | None:
        """Alias for 'name' - provides backward compatibility with core/sexp.py."""
        return self.name

    @property
    def values(self) -> list:
        """
        Return children in the format used by core/sexp.py.

        In the old API, values was a mixed list of primitives (str, int, float)
        and SExp nodes. This property returns children in that format for
        backward compatibility.
        """
        result = []
        for child in self.children:
            if child.is_atom:
                result.append(child.value)
            else:
                result.append(child)
        return result

    def get_value(self, index: int = 0) -> str | int | float | SExp | None:
        """Get a value by index (0 = first value after tag).

        For atoms, returns the primitive value. For non-atoms, returns the SExp.
        Provides backward compatibility with core/sexp.py.
        """
        if 0 <= index < len(self.children):
            child = self.children[index]
            if child.is_atom:
                return child.value
            return child
        return None

    def get_string(self, index: int = 0) -> str | None:
        """Get a string value by index."""
        val = self.get_value(index)
        return str(val) if val is not None else None

    def get_int(self, index: int = 0) -> int | None:
        """Get an integer value by index."""
        val = self.get_value(index)
        if isinstance(val, int):
            return val
        if isinstance(val, str):
            try:
                return int(val)
            except ValueError:
                return None
        return None

    def get_float(self, index: int = 0) -> float | None:
        """Get a float value by index."""
        val = self.get_value(index)
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            try:
                return float(val)
            except ValueError:
                return None
        return None

    def iter_children(self) -> Iterator[SExp]:
        """Iterate over child SExp nodes (skipping atoms)."""
        for child in self.children:
            if not child.is_atom:
                yield child

    def has_tag(self, tag: str) -> bool:
        """Check if any direct child has the given tag/name."""
        return self.find_child(tag) is not None

    def find_child(self, tag: str) -> SExp | None:
        """Find the first direct child with the given tag/name.

        Unlike find(), this only searches direct children, not descendants.
        Provides backward compatibility with core/sexp.py's find() behavior.
        """
        for child in self.children:
            if child.name == tag:
                return child
        return None

    def find_children(self, tag: str) -> list[SExp]:
        """Find all direct children with the given tag/name.

        Unlike find_all(), this only searches direct children, not descendants.
        Provides backward compatibility with core/sexp.py's find_all() behavior.
        """
        return [child for child in self.children if child.name == tag]

    def add(self, value: SExp | str | int | float) -> SExp:
        """Add a value and return self for chaining.

        Provides backward compatibility with core/sexp.py's add() method.
        """
        if isinstance(value, SExp):
            self.children.append(value)
        else:
            self.children.append(SExp(value=value))
        return self

    def set_value(self, index: int, value: str | int | float) -> None:
        """Set a value at the given index.

        Provides backward compatibility with core/sexp.py's set_value() method.
        """
        while len(self.children) <= index:
            self.children.append(SExp(value=""))
        self.children[index] = SExp(value=value)

    def remove_child(self, tag: str) -> bool:
        """Remove the first child with the given tag/name. Returns True if found.

        Provides backward compatibility with core/sexp.py's remove_child() method.
        """
        for i, child in enumerate(self.children):
            if child.name == tag:
                del self.children[i]
                return True
        return False

    # Convenience constructors
    @classmethod
    def atom(cls, value: str | int | float) -> SExp:
        """Create an atom node."""
        return cls(value=value)

    @classmethod
    def list(cls, name: str, *children: SExp | str | int | float) -> SExp:
        """Create a list node."""
        node = cls(name=name)
        for child in children:
            if isinstance(child, SExp):
                node.children.append(child)
            else:
                node.children.append(cls(value=child))
        return node

    def to_source_position(
        self,
        file_path: Path,
        element_type: str = "",
        element_ref: str = "",
        position_mm: tuple[float, float] | None = None,
        layer: str | None = None,
    ):
        """Create a SourcePosition from this node's location.

        Requires the node to have been parsed with track_positions=True.

        Args:
            file_path: Path to the KiCad file (not stored on node)
            element_type: Type of element (e.g., "symbol", "track")
            element_ref: Reference designator (e.g., "U1", "net-GND")
            position_mm: Optional board coordinates as (x, y)
            layer: Optional layer name (e.g., "F.Cu")

        Returns:
            SourcePosition instance, or None if no position info available

        Example::

            doc = Document.load("board.kicad_pcb", track_positions=True)
            symbol = doc.find("symbol")
            if symbol.has_position:
                pos = symbol.to_source_position(
                    doc.path,
                    element_type="symbol",
                    element_ref="U1",
                )
        """
        from kicad_tools.exceptions import SourcePosition

        if not self.has_position:
            return None

        return SourcePosition(
            file_path=file_path,
            line=self._line,
            column=self._column,
            element_type=element_type or self.name or "",
            element_ref=element_ref,
            position_mm=position_mm,
            layer=layer,
        )


# Pre-compiled character sets for faster parsing
_WHITESPACE = frozenset(" \t\n\r")
_ATOM_TERMINATORS = frozenset(" \t\n\r()")
_COMMENT_CHARS = frozenset("#;")
_ESCAPE_MAP = {"n": "\n", "t": "\t", "r": "\r"}


class Parser:
    """S-expression parser for KiCad files.

    Optimized for performance with:
    - Pre-compiled character sets for O(1) membership tests
    - Reduced function call overhead in hot paths
    - Efficient string building for quoted strings

    Position tracking:
        When track_positions=True, parsed nodes include line and column
        information for error reporting and source mapping.
    """

    __slots__ = ("text", "pos", "length", "_track_positions", "_line_starts")

    def __init__(self, text: str, track_positions: bool = False):
        self.text = text
        self.pos = 0
        self.length = len(text)
        self._track_positions = track_positions
        # Pre-compute line start positions for efficient line/column lookup
        if track_positions:
            self._line_starts = [0]
            for i, char in enumerate(text):
                if char == "\n":
                    self._line_starts.append(i + 1)
        else:
            self._line_starts = []

    def _get_position(self, pos: int) -> tuple[int, int]:
        """Convert byte position to (line, column), both 1-indexed."""
        if not self._track_positions:
            return (0, 0)

        # Binary search to find the line
        line_starts = self._line_starts
        lo, hi = 0, len(line_starts)
        while lo < hi:
            mid = (lo + hi) // 2
            if line_starts[mid] <= pos:
                lo = mid + 1
            else:
                hi = mid
        line = lo  # 1-indexed line number
        column = pos - line_starts[line - 1] + 1  # 1-indexed column
        return (line, column)

    def parse(self) -> SExp:
        """Parse the entire document."""
        self._skip_whitespace()
        result = self._parse_expr()
        self._skip_whitespace()
        if self.pos < self.length:
            raise ParseError(f"Unexpected content at position {self.pos}")
        return result

    def _parse_expr(self) -> SExp:
        """Parse a single S-expression."""
        # Inline whitespace skip for hot path
        text = self.text
        length = self.length
        pos = self.pos
        while pos < length and text[pos] in _WHITESPACE:
            pos += 1
        # Skip comments
        while pos < length and text[pos] in _COMMENT_CHARS:
            while pos < length and text[pos] != "\n":
                pos += 1
            while pos < length and text[pos] in _WHITESPACE:
                pos += 1
        self.pos = pos

        if pos >= length:
            raise ParseError("Unexpected end of input")

        # Record start position for position tracking
        start_pos = pos
        char = text[pos]

        if char == "(":
            node = self._parse_list()
        elif char == '"':
            node = self._parse_string_node()
        else:
            node = self._parse_atom()

        # Set position if tracking is enabled
        if self._track_positions:
            line, column = self._get_position(start_pos)
            node._line = line
            node._column = column

        return node

    def _parse_list(self) -> SExp:
        """Parse a list (name children...)."""
        text = self.text
        length = self.length
        self.pos += 1  # Skip opening paren
        pos = self.pos

        # Inline whitespace skip
        while pos < length and text[pos] in _WHITESPACE:
            pos += 1
        while pos < length and text[pos] in _COMMENT_CHARS:
            while pos < length and text[pos] != "\n":
                pos += 1
            while pos < length and text[pos] in _WHITESPACE:
                pos += 1
        self.pos = pos

        if pos >= length:
            raise ParseError("Unexpected end of input in list")

        char = text[pos]
        if char == ")":
            self.pos = pos + 1
            return SExp()  # Empty list

        # Optimization: Try to parse first element as simple name directly
        # This avoids creating an intermediate SExp for the common case
        if char != "(" and char != '"':
            # Likely an unquoted atom - parse directly as potential name
            start = pos
            while pos < length and text[pos] not in _ATOM_TERMINATORS:
                pos += 1
            self.pos = pos
            if pos > start:
                token = text[start:pos]
                # Check if it's a valid name (most common case)
                if token and not token[0].isdigit() and token[0] != "-":
                    # Valid name - create node directly with name
                    node = SExp(name=token)
                else:
                    # Try as number
                    first_char = token[0]
                    if first_char.isdigit() or (first_char == "-" and len(token) > 1):
                        # Numeric - use as name (for layer definitions like (0 "F.Cu"))
                        node = SExp(name=token)
                    else:
                        # Something else - make anonymous list with this as first child
                        node = SExp(children=[SExp(value=token)])
            else:
                raise ParseError(f"Expected atom at position {pos}")
        else:
            # First element is quoted string or nested list
            first = self._parse_expr()
            first_value = first.value
            if first.name is None and not first.children:  # is_atom
                if isinstance(first_value, (int, float)):
                    node = SExp(name=str(first_value))
                else:
                    node = SExp(children=[first])
            else:
                node = SExp(children=[first])

        # Parse remaining children - use local variables for speed
        children = node.children
        while True:
            # Inline whitespace skip
            pos = self.pos
            while pos < length and text[pos] in _WHITESPACE:
                pos += 1
            while pos < length and text[pos] in _COMMENT_CHARS:
                while pos < length and text[pos] != "\n":
                    pos += 1
                while pos < length and text[pos] in _WHITESPACE:
                    pos += 1
            self.pos = pos

            if pos >= length:
                raise ParseError("Unexpected end of input, expected ')'")

            if text[pos] == ")":
                self.pos = pos + 1
                break

            children.append(self._parse_expr())

        return node

    def _parse_string_node(self) -> SExp:
        """Parse a quoted string and return as SExp node."""
        return SExp(value=self._parse_string())

    def _parse_string(self) -> str:
        """Parse a quoted string."""
        text = self.text
        length = self.length
        pos = self.pos + 1  # Skip opening quote

        # Fast path: find if there are any escape sequences or end quote
        # Most strings don't have escapes, so we can optimize for that case
        start = pos
        while pos < length:
            char = text[pos]
            if char == '"':
                # No escapes found - fast path
                self.pos = pos + 1
                return text[start:pos]
            elif char == "\\":
                # Has escapes - use slower path
                break
            pos += 1

        # Slow path with escape handling
        result = [text[start:pos]] if pos > start else []
        while pos < length:
            char = text[pos]
            if char == '"':
                self.pos = pos + 1
                return "".join(result)
            elif char == "\\":
                pos += 1
                if pos >= length:
                    raise ParseError("Unexpected end of input in escape sequence")
                escaped = text[pos]
                result.append(_ESCAPE_MAP.get(escaped, escaped))
            else:
                result.append(char)
            pos += 1

        raise ParseError("Unterminated string")

    def _parse_atom(self) -> SExp:
        """Parse an unquoted atom (symbol or number)."""
        text = self.text
        length = self.length
        start = self.pos
        pos = start

        # Use local variable and frozenset for faster iteration
        while pos < length and text[pos] not in _ATOM_TERMINATORS:
            pos += 1

        self.pos = pos

        if pos == start:
            raise ParseError(f"Expected atom at position {pos}")

        token = text[start:pos]

        # Try to parse as number, but preserve original string for round-trip
        # Check for likely number patterns first to avoid exception overhead
        first_char = token[0]
        if first_char.isdigit() or (first_char == "-" and len(token) > 1):
            try:
                if "." in token or "e" in token or "E" in token:
                    node = SExp(value=float(token))
                else:
                    node = SExp(value=int(token))
                node._original_str = token
                return node
            except ValueError:
                pass
        return SExp(value=token)

    def _skip_whitespace(self):
        """Skip whitespace and comments."""
        text = self.text
        length = self.length
        pos = self.pos

        while pos < length:
            char = text[pos]
            if char in _WHITESPACE:
                pos += 1
            elif char in _COMMENT_CHARS:
                # Skip to end of line
                while pos < length and text[pos] != "\n":
                    pos += 1
            else:
                break

        self.pos = pos


class ParseError(ValueError):
    """Error during S-expression parsing.

    Inherits from ValueError for backward compatibility with code that
    catches ValueError for parse errors.
    """

    pass


# Backward compatibility aliases
SExpParser = Parser


class SExpSerializer:
    """Serializer for S-expressions to KiCad format.

    Provides backward compatibility with core/sexp.py's SExpSerializer class.
    """

    def __init__(self, indent: str = "  ", newline_threshold: int = 1):
        """
        Args:
            indent: String to use for each indentation level (ignored - uses KiCad format)
            newline_threshold: Put children on new lines if more than this many (ignored)
        """
        self.indent = indent
        self.newline_threshold = newline_threshold

    def serialize(self, sexp: SExp) -> str:
        """Serialize an SExp to string."""
        return sexp.to_string() + "\n"


def parse_string(text: str, track_positions: bool = False) -> SExp:
    """Parse an S-expression string.

    Args:
        text: The S-expression text to parse
        track_positions: If True, track line/column positions for each node

    Returns:
        The parsed SExp tree
    """
    return Parser(text, track_positions=track_positions).parse()


# Backward compatibility alias
parse_sexp = parse_string


def serialize_sexp(sexp: SExp, indent: str = "  ") -> str:
    """Serialize an SExp tree to text.

    Provides backward compatibility with core/sexp.py's serialize_sexp() function.

    Args:
        sexp: The SExp tree to serialize
        indent: String to use for each indentation level (ignored - uses KiCad format)

    Returns:
        Serialized S-expression string
    """
    return sexp.to_string()


def parse_file(path: str | Path, track_positions: bool = False) -> SExp:
    """Parse an S-expression file.

    Args:
        path: Path to the file to parse
        track_positions: If True, track line/column positions for each node

    Returns:
        The parsed SExp tree
    """
    text = Path(path).read_text(encoding="utf-8")
    return Parser(text, track_positions=track_positions).parse()


class Document:
    """
    A KiCad document (schematic or PCB) with round-trip editing support.

    Usage:
        doc = Document.load("project.kicad_sch")
        doc.root.find("symbol", lib_id="Audio:PCM5122PW")
        doc.save()  # or doc.save("new_file.kicad_sch")
    """

    def __init__(self, root: SExp, path: Path | None = None):
        self.root = root
        self.path = Path(path) if path else None

    @classmethod
    def load(cls, path: str | Path, track_positions: bool = False) -> Document:
        """Load a document from file.

        Args:
            path: Path to the KiCad file to load
            track_positions: If True, track line/column positions for each node
        """
        path = Path(path)
        root = parse_file(path, track_positions=track_positions)
        return cls(root, path)

    def save(self, path: str | Path | None = None):
        """Save document to file."""
        save_path = Path(path) if path else self.path
        if save_path is None:
            raise ValueError("No path specified for save")
        save_path.write_text(self.root.to_string(), encoding="utf-8")

    def find(self, name: str, **attrs) -> SExp | None:
        """Find first element matching name and attributes."""
        return self.root.find(name, **attrs)

    def find_all(self, name: str, **attrs) -> list[SExp]:
        """Find all elements matching name and attributes."""
        return self.root.find_all(name, **attrs)


if __name__ == "__main__":
    # Test with a simple example
    test_sexp = """
    (kicad_sch
        (version 20231120)
        (generator "test")
        (symbol
            (lib_id "Audio:PCM5122PW")
            (at 100 100 0)
            (property "Reference" "U1"
                (at 100 90 0)
            )
        )
        (wire
            (pts (xy 10 20) (xy 30 40))
        )
    )
    """

    print("Parsing test S-expression...")
    doc = parse_string(test_sexp)

    print(f"\nRoot: {doc}")
    print(f"Version: {doc['version'].get_first_atom()}")
    print(f"Generator: {doc['generator'].get_first_atom()}")

    symbol = doc.find("symbol")
    print(f"\nFound symbol: {symbol}")
    print(f"  lib_id: {symbol['lib_id'].get_first_atom()}")
    print(f"  at: {symbol['at'].get_atoms()}")

    wire = doc.find("wire")
    print(f"\nFound wire: {wire}")

    print("\n--- Serialized back ---")
    print(doc.to_string())
