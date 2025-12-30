"""
S-expression parser and serializer for KiCad files.

KiCad uses a Lisp-like S-expression format for .kicad_sch, .kicad_sym,
.kicad_pcb, and other files. This module provides parsing and serialization.

Example KiCad S-expression:
    (kicad_sch
        (version 20231120)
        (generator "eeschema")
        (symbol
            (lib_id "Device:R")
            (at 100 50 0)
            (property "Reference" "R1"
                (at 100 48 0)
            )
        )
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Union

# Type alias for S-expression values
SExpValue = Union[str, int, float, "SExp"]


@dataclass
class SExp:
    """
    Represents an S-expression node.

    An S-expression is either:
    - An atom (string, number)
    - A list of S-expressions, where the first element is typically a tag

    In KiCad format, lists usually have a tag as the first element:
        (tag value1 value2 (nested_tag ...))

    Attributes:
        tag: The first element of the list (e.g., "symbol", "property")
        values: The remaining elements (atoms or nested SExp)
    """

    tag: str
    values: List[SExpValue] = field(default_factory=list)

    def __getitem__(self, key: Union[int, str]) -> Optional[SExpValue]:
        """
        Get a value by index or find a child by tag.

        Args:
            key: Integer index or string tag name

        Returns:
            The value at index, or first child with matching tag, or None
        """
        if isinstance(key, int):
            if 0 <= key < len(self.values):
                return self.values[key]
            return None
        elif isinstance(key, str):
            return self.find(key)
        return None

    def find(self, tag: str) -> Optional[SExp]:
        """Find the first child SExp with the given tag."""
        for v in self.values:
            if isinstance(v, SExp) and v.tag == tag:
                return v
        return None

    def find_all(self, tag: str) -> List[SExp]:
        """Find all children with the given tag."""
        return [v for v in self.values if isinstance(v, SExp) and v.tag == tag]

    def get_value(self, index: int = 0) -> Optional[SExpValue]:
        """Get a value by index (0 = first value after tag)."""
        if 0 <= index < len(self.values):
            return self.values[index]
        return None

    def get_string(self, index: int = 0) -> Optional[str]:
        """Get a string value by index."""
        val = self.get_value(index)
        return str(val) if val is not None else None

    def get_int(self, index: int = 0) -> Optional[int]:
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

    def get_float(self, index: int = 0) -> Optional[float]:
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
        for v in self.values:
            if isinstance(v, SExp):
                yield v

    def has_tag(self, tag: str) -> bool:
        """Check if any child has the given tag."""
        return self.find(tag) is not None

    def add(self, value: SExpValue) -> SExp:
        """Add a value and return self for chaining."""
        self.values.append(value)
        return self

    def set_value(self, index: int, value: SExpValue) -> None:
        """Set a value at the given index."""
        while len(self.values) <= index:
            self.values.append("")
        self.values[index] = value

    def remove_child(self, tag: str) -> bool:
        """Remove the first child with the given tag. Returns True if found."""
        for i, v in enumerate(self.values):
            if isinstance(v, SExp) and v.tag == tag:
                del self.values[i]
                return True
        return False

    def __repr__(self) -> str:
        if not self.values:
            return f"SExp({self.tag!r})"
        return f"SExp({self.tag!r}, {self.values!r})"


class SExpParser:
    """Parser for S-expression format used by KiCad."""

    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.length = len(text)

    def parse(self) -> SExp:
        """Parse the entire text and return the root SExp."""
        self._skip_whitespace()
        result = self._parse_expr()
        self._skip_whitespace()
        if self.pos < self.length:
            raise ValueError(f"Unexpected content at position {self.pos}")
        return result

    def _skip_whitespace(self) -> None:
        """Skip whitespace and comments."""
        while self.pos < self.length:
            c = self.text[self.pos]
            if c in " \t\n\r":
                self.pos += 1
            elif c == ";":  # Comment to end of line
                while self.pos < self.length and self.text[self.pos] != "\n":
                    self.pos += 1
            else:
                break

    def _parse_expr(self) -> SExpValue:
        """Parse a single S-expression (atom or list)."""
        self._skip_whitespace()

        if self.pos >= self.length:
            raise ValueError("Unexpected end of input")

        c = self.text[self.pos]

        if c == "(":
            return self._parse_list()
        elif c == '"':
            return self._parse_string()
        else:
            return self._parse_atom()

    def _parse_list(self) -> SExp:
        """Parse a list: (tag value1 value2 ...)"""
        assert self.text[self.pos] == "("
        self.pos += 1
        self._skip_whitespace()

        if self.pos >= self.length:
            raise ValueError("Unexpected end of input in list")

        # Parse the tag (first element)
        if self.text[self.pos] == ")":
            # Empty list - shouldn't happen in KiCad but handle it
            self.pos += 1
            return SExp("")

        tag_value = self._parse_expr()
        # KiCad PCB files can have numeric tags (e.g., layer numbers)
        if isinstance(tag_value, (int, float)):
            tag_value = str(tag_value)

        result = SExp(tag_value)

        # Parse remaining values
        while True:
            self._skip_whitespace()
            if self.pos >= self.length:
                raise ValueError("Unexpected end of input, expected ')'")
            if self.text[self.pos] == ")":
                self.pos += 1
                break
            result.values.append(self._parse_expr())

        return result

    def _parse_string(self) -> str:
        """Parse a quoted string."""
        assert self.text[self.pos] == '"'
        self.pos += 1

        result = []
        while self.pos < self.length:
            c = self.text[self.pos]
            if c == '"':
                self.pos += 1
                return "".join(result)
            elif c == "\\":
                self.pos += 1
                if self.pos >= self.length:
                    raise ValueError("Unexpected end of input in escape sequence")
                escaped = self.text[self.pos]
                if escaped == "n":
                    result.append("\n")
                elif escaped == "t":
                    result.append("\t")
                elif escaped == "r":
                    result.append("\r")
                elif escaped == "\\":
                    result.append("\\")
                elif escaped == '"':
                    result.append('"')
                else:
                    result.append(escaped)
                self.pos += 1
            else:
                result.append(c)
                self.pos += 1

        raise ValueError("Unexpected end of input in string")

    def _parse_atom(self) -> Union[str, int, float]:
        """Parse an unquoted atom (symbol, number)."""
        start = self.pos

        while self.pos < self.length:
            c = self.text[self.pos]
            if c in ' \t\n\r()"':
                break
            self.pos += 1

        if self.pos == start:
            raise ValueError(f"Expected atom at position {self.pos}")

        token = self.text[start : self.pos]

        # Try to parse as number
        try:
            if "." in token or "e" in token.lower():
                return float(token)
            return int(token)
        except ValueError:
            return token


class SExpSerializer:
    """Serializer for S-expressions to KiCad format."""

    def __init__(self, indent: str = "  ", newline_threshold: int = 1):
        """
        Args:
            indent: String to use for each indentation level (default: 2 spaces for KiCad)
            newline_threshold: Put children on new lines if more than this many
        """
        self.indent = indent
        self.newline_threshold = newline_threshold

    def serialize(self, sexp: SExp) -> str:
        """Serialize an SExp to string."""
        lines = []
        self._serialize_node(sexp, 0, lines)
        return "\n".join(lines) + "\n"

    def _serialize_node(self, sexp: SExp, depth: int, lines: List[str]) -> None:
        """Serialize a node, handling formatting."""
        prefix = self.indent * depth

        # Determine if we should use multi-line format
        has_nested = any(isinstance(v, SExp) for v in sexp.values)
        use_multiline = has_nested or len(sexp.values) > self.newline_threshold

        # Special cases for compact formatting
        compact_tags = {
            "at",
            "size",
            "xy",
            "pts",
            "start",
            "end",
            "mid",
            "stroke",
            "fill",
            "effects",
            "font",
            "justify",
            "color",
            "uuid",
            "number",
            "name",
            "offset",
        }
        if sexp.tag in compact_tags:
            use_multiline = False

        if use_multiline:
            # Multi-line format
            opening = f"{prefix}({sexp.tag}"

            # Add simple values on the same line as the tag
            simple_values = []
            complex_values = []
            for v in sexp.values:
                if isinstance(v, SExp):
                    complex_values.append(v)
                else:
                    simple_values.append(v)

            if simple_values:
                opening += " " + " ".join(self._format_value(v) for v in simple_values)

            if complex_values:
                lines.append(opening)
                for child in complex_values:
                    self._serialize_node(child, depth + 1, lines)
                lines.append(f"{prefix})")
            else:
                lines.append(opening + ")")
        else:
            # Single-line format
            parts = [sexp.tag]
            for v in sexp.values:
                if isinstance(v, SExp):
                    parts.append(self._serialize_inline(v))
                else:
                    parts.append(self._format_value(v))
            lines.append(f"{prefix}({' '.join(parts)})")

    def _serialize_inline(self, sexp: SExp) -> str:
        """Serialize a node inline (single line)."""
        parts = [sexp.tag]
        for v in sexp.values:
            if isinstance(v, SExp):
                parts.append(self._serialize_inline(v))
            else:
                parts.append(self._format_value(v))
        return f"({' '.join(parts)})"

    def _format_value(self, value: SExpValue) -> str:
        """Format a single value."""
        if isinstance(value, str):
            # Check if we need to quote the string
            if self._needs_quoting(value):
                return self._quote_string(value)
            return value
        elif isinstance(value, float):
            # Format floats without unnecessary trailing zeros
            if value == int(value):
                return str(int(value))
            return f"{value:g}"
        elif isinstance(value, int):
            return str(value)
        elif isinstance(value, SExp):
            return self._serialize_inline(value)
        return str(value)

    def _needs_quoting(self, s: str) -> bool:
        """Check if a string needs to be quoted."""
        if not s:
            return True
        if s[0].isdigit() or s[0] == "-":
            return True
        for c in s:
            if c in ' \t\n\r()"\\':
                return True

        # Known unquoted keywords in KiCad format
        unquoted_keywords = {
            # Boolean-like
            "yes", "no", "true", "false",
            # Visibility
            "hide", "show",
            # Fill types
            "none", "outline", "background", "solid",
            # Stroke types
            "default", "dash", "dash_dot", "dash_dot_dot", "dot",
            # Justify
            "left", "right", "center", "top", "bottom", "mirror",
            # Pin types
            "input", "output", "bidirectional", "tri_state", "passive",
            "free", "unspecified", "power_in", "power_out",
            "open_collector", "open_emitter", "no_connect", "line",
            "inverted", "clock", "inverted_clock", "input_low",
            "clock_low", "output_low", "edge_clock_high", "non_logic",
            # Layer types
            "signal", "power", "user", "mixed", "jumper",
            # Pad types
            "thru_hole", "smd", "connect", "np_thru_hole",
            # Pad shapes
            "rect", "oval", "circle", "roundrect", "trapezoid", "custom",
            # fp_text types
            "reference", "value",
            # Zone types
            "thermal_reliefs", "full", "hatch", "hatched",
            # Via types
            "blind", "micro", "through",
            # Arc modes
            "arc", "start", "mid", "end",
            # Text effects
            "italic", "bold",
            # Footprint attributes
            "through_hole", "virtual", "exclude_from_pos_files",
            "exclude_from_bom", "board_only", "dnp",
        }

        # If it's a known keyword, don't quote it
        if s in unquoted_keywords:
            return False

        # Quote strings with dots (like layer names F.Cu, B.Cu)
        if "." in s:
            return True

        return False

    def _quote_string(self, s: str) -> str:
        """Quote a string with proper escaping."""
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
        return f'"{escaped}"'


def parse_sexp(text: str) -> SExp:
    """Parse S-expression text into an SExp tree."""
    return SExpParser(text).parse()


def serialize_sexp(sexp: SExp, indent: str = "  ") -> str:
    """Serialize an SExp tree to text.

    Args:
        sexp: The SExp tree to serialize
        indent: String to use for each indentation level (default: 2 spaces for KiCad format)

    Returns:
        Serialized S-expression string
    """
    return SExpSerializer(indent=indent).serialize(sexp)
