#!/usr/bin/env python3
"""
KiCad S-expression Parser and Generator

A robust parser for KiCad schematic and PCB files that supports:
- Full round-trip editing (parse → modify → serialize)
- XPath-like queries for finding elements
- Proper handling of strings, numbers, and nested structures

Usage:
    from kicad_sexp import SExp, parse_file, parse_string

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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional, Union


@dataclass
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
    """

    name: Optional[str] = None
    children: list[SExp] = field(default_factory=list)
    value: Optional[Union[str, int, float]] = None

    # For preserving formatting
    _inline: bool = False  # Render on single line
    _original_str: Optional[str] = None  # Original string representation for round-trip

    def __post_init__(self):
        if self.name is not None and self.value is not None:
            raise ValueError("SExp cannot have both name and value")

    @property
    def is_atom(self) -> bool:
        """True if this is a leaf node (string, number, symbol)."""
        return self.name is None and not self.children

    @property
    def is_list(self) -> bool:
        """True if this is a list node."""
        return self.name is not None or bool(self.children)

    def __getitem__(self, key: Union[str, int, tuple]) -> SExp:
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

    def get(self, key: str, default: Any = None) -> Optional[SExp]:
        """Get child by name, returning default if not found."""
        try:
            return self[key]
        except KeyError:
            return default

    def find(self, name: str, **attrs) -> Optional[SExp]:
        """
        Find first descendant matching name and attributes.

        Example:
            doc.find("symbol", lib_id="Audio:PCM5122PW")
        """
        for node in self.iter_all():
            if node.name == name:
                if all(self._match_attr(node, k, v) for k, v in attrs.items()):
                    return node
        return None

    def find_all(self, name: str, **attrs) -> list[SExp]:
        """Find all descendants matching name and attributes."""
        results = []
        for node in self.iter_all():
            if node.name == name:
                if all(self._match_attr(node, k, v) for k, v in attrs.items()):
                    results.append(node)
        return results

    def _match_attr(self, node: SExp, attr: str, value: Any) -> bool:
        """Check if node has attribute matching value."""
        child = node.get(attr)
        if child is None:
            # Check if it's stored as a direct child value
            for c in node.children:
                if c.is_atom and c.value == value:
                    return True
            return False

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

    def get_atoms(self) -> list[Union[str, int, float]]:
        """Get all atom values from direct children."""
        return [c.value for c in self.children if c.is_atom]

    def get_first_atom(self) -> Optional[Union[str, int, float]]:
        """Get the first atom value from children."""
        for c in self.children:
            if c.is_atom:
                return c.value
        return None

    def set_atom(self, index: int, value: Union[str, int, float]):
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

        for i, child in enumerate(self.children):
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
            "fill",
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
            "user",
            # Zone connection types
            "thermal_reliefs",
            "full",
            # Zone fill modes
            "hatch",
            "hatched",
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
            "smd",
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

    # Convenience constructors
    @classmethod
    def atom(cls, value: Union[str, int, float]) -> SExp:
        """Create an atom node."""
        return cls(value=value)

    @classmethod
    def list(cls, name: str, *children: Union[SExp, str, int, float]) -> SExp:
        """Create a list node."""
        node = cls(name=name)
        for child in children:
            if isinstance(child, SExp):
                node.children.append(child)
            else:
                node.children.append(cls(value=child))
        return node


class Parser:
    """S-expression parser for KiCad files."""

    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.length = len(text)

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
        self._skip_whitespace()

        if self.pos >= self.length:
            raise ParseError("Unexpected end of input")

        char = self.text[self.pos]

        if char == "(":
            return self._parse_list()
        elif char == '"':
            return SExp(value=self._parse_string())
        else:
            return self._parse_atom()

    def _parse_list(self) -> SExp:
        """Parse a list (name children...)."""
        assert self.text[self.pos] == "("
        self.pos += 1

        self._skip_whitespace()

        if self.pos >= self.length:
            raise ParseError("Unexpected end of input in list")

        if self.text[self.pos] == ")":
            self.pos += 1
            return SExp()  # Empty list

        # First element could be name or value
        first = self._parse_expr()

        # If first is a simple unquoted atom, treat it as the list name
        if first.is_atom and isinstance(first.value, str) and SExp._is_valid_name(first.value):
            node = SExp(name=first.value)
        else:
            # Anonymous list with first element as child
            node = SExp()
            node.children.append(first)

        # Parse remaining children
        while True:
            self._skip_whitespace()

            if self.pos >= self.length:
                raise ParseError("Unexpected end of input, expected ')'")

            if self.text[self.pos] == ")":
                self.pos += 1
                break

            node.children.append(self._parse_expr())

        return node

    def _parse_string(self) -> str:
        """Parse a quoted string."""
        assert self.text[self.pos] == '"'
        self.pos += 1

        result = []
        while self.pos < self.length:
            char = self.text[self.pos]

            if char == '"':
                self.pos += 1
                return "".join(result)
            elif char == "\\":
                self.pos += 1
                if self.pos >= self.length:
                    raise ParseError("Unexpected end of input in escape sequence")
                escaped = self.text[self.pos]
                if escaped == "n":
                    result.append("\n")
                elif escaped == "t":
                    result.append("\t")
                elif escaped == "r":
                    result.append("\r")
                else:
                    result.append(escaped)
            else:
                result.append(char)

            self.pos += 1

        raise ParseError("Unterminated string")

    def _parse_atom(self) -> SExp:
        """Parse an unquoted atom (symbol or number)."""
        start = self.pos

        while self.pos < self.length:
            char = self.text[self.pos]
            if char in " \t\n\r()":
                break
            self.pos += 1

        if self.pos == start:
            raise ParseError(f"Expected atom at position {self.pos}")

        token = self.text[start : self.pos]

        # Try to parse as number, but preserve original string for round-trip
        try:
            if "." in token or "e" in token.lower():
                node = SExp(value=float(token))
                node._original_str = token  # Preserve for round-trip
                return node
            node = SExp(value=int(token))
            node._original_str = token  # Preserve for round-trip
            return node
        except ValueError:
            return SExp(value=token)

    def _skip_whitespace(self):
        """Skip whitespace and comments."""
        while self.pos < self.length:
            char = self.text[self.pos]

            if char in " \t\n\r":
                self.pos += 1
            elif char == "#":
                # Skip to end of line
                while self.pos < self.length and self.text[self.pos] != "\n":
                    self.pos += 1
            else:
                break


class ParseError(Exception):
    """Error during S-expression parsing."""

    pass


def parse_string(text: str) -> SExp:
    """Parse an S-expression string."""
    return Parser(text).parse()


def parse_file(path: str | Path) -> SExp:
    """Parse an S-expression file."""
    text = Path(path).read_text(encoding="utf-8")
    return Parser(text).parse()


class Document:
    """
    A KiCad document (schematic or PCB) with round-trip editing support.

    Usage:
        doc = Document.load("project.kicad_sch")
        doc.root.find("symbol", lib_id="Audio:PCM5122PW")
        doc.save()  # or doc.save("new_file.kicad_sch")
    """

    def __init__(self, root: SExp, path: Optional[Path] = None):
        self.root = root
        self.path = Path(path) if path else None

    @classmethod
    def load(cls, path: str | Path) -> Document:
        """Load a document from file."""
        path = Path(path)
        root = parse_file(path)
        return cls(root, path)

    def save(self, path: Optional[str | Path] = None):
        """Save document to file."""
        save_path = Path(path) if path else self.path
        if save_path is None:
            raise ValueError("No path specified for save")
        save_path.write_text(self.root.to_string(), encoding="utf-8")

    def find(self, name: str, **attrs) -> Optional[SExp]:
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
