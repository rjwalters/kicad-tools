"""
Footprint data structures and KiCad .kicad_mod export.

This module provides data classes representing KiCad footprint elements
and the ability to serialize them to valid .kicad_mod files.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from kicad_tools.utils import ensure_parent_dir


@dataclass
class Pad:
    """A footprint pad (SMD or through-hole)."""

    name: str
    pad_type: Literal["smd", "thru_hole", "np_thru_hole", "connect"] = "smd"
    shape: Literal["circle", "rect", "oval", "roundrect", "trapezoid", "custom"] = "roundrect"
    x: float = 0.0
    y: float = 0.0
    width: float = 1.0
    height: float = 1.0
    drill: float | None = None  # For through-hole pads
    layers: tuple[str, ...] = ("F.Cu", "F.Paste", "F.Mask")
    roundrect_ratio: float = 0.25  # For roundrect pads

    def to_sexp(self) -> str:
        """Convert to KiCad S-expression format."""
        lines = [f'\t(pad "{self.name}" {self.pad_type} {self.shape}']
        lines.append(f"\t\t(at {_fmt(self.x)} {_fmt(self.y)})")
        lines.append(f"\t\t(size {_fmt(self.width)} {_fmt(self.height)})")

        if self.drill is not None:
            lines.append(f"\t\t(drill {_fmt(self.drill)})")

        layers_str = " ".join(f'"{layer}"' for layer in self.layers)
        lines.append(f"\t\t(layers {layers_str})")

        if self.shape == "roundrect":
            lines.append(f"\t\t(roundrect_rratio {self.roundrect_ratio})")

        lines.append("\t)")
        return "\n".join(lines)


@dataclass
class GraphicLine:
    """A graphic line on a footprint layer."""

    start_x: float
    start_y: float
    end_x: float
    end_y: float
    layer: str = "F.SilkS"
    width: float = 0.12

    def to_sexp(self) -> str:
        """Convert to KiCad S-expression format."""
        return f"""\t(fp_line
\t\t(start {_fmt(self.start_x)} {_fmt(self.start_y)})
\t\t(end {_fmt(self.end_x)} {_fmt(self.end_y)})
\t\t(stroke
\t\t\t(width {self.width})
\t\t\t(type solid)
\t\t)
\t\t(layer "{self.layer}")
\t)"""


@dataclass
class GraphicRect:
    """A graphic rectangle on a footprint layer."""

    start_x: float
    start_y: float
    end_x: float
    end_y: float
    layer: str = "F.SilkS"
    width: float = 0.12
    fill: bool = False

    def to_sexp(self) -> str:
        """Convert to KiCad S-expression format."""
        fill_str = "solid" if self.fill else "none"
        return f"""\t(fp_rect
\t\t(start {_fmt(self.start_x)} {_fmt(self.start_y)})
\t\t(end {_fmt(self.end_x)} {_fmt(self.end_y)})
\t\t(stroke
\t\t\t(width {self.width})
\t\t\t(type solid)
\t\t)
\t\t(fill {fill_str})
\t\t(layer "{self.layer}")
\t)"""


@dataclass
class GraphicCircle:
    """A graphic circle on a footprint layer."""

    center_x: float
    center_y: float
    radius: float
    layer: str = "F.SilkS"
    width: float = 0.12
    fill: bool = False

    def to_sexp(self) -> str:
        """Convert to KiCad S-expression format."""
        # KiCad uses center + end point on circumference
        fill_str = "solid" if self.fill else "none"
        return f"""\t(fp_circle
\t\t(center {_fmt(self.center_x)} {_fmt(self.center_y)})
\t\t(end {_fmt(self.center_x + self.radius)} {_fmt(self.center_y)})
\t\t(stroke
\t\t\t(width {self.width})
\t\t\t(type solid)
\t\t)
\t\t(fill {fill_str})
\t\t(layer "{self.layer}")
\t)"""


@dataclass
class GraphicArc:
    """A graphic arc on a footprint layer."""

    start_x: float
    start_y: float
    mid_x: float
    mid_y: float
    end_x: float
    end_y: float
    layer: str = "F.SilkS"
    width: float = 0.12

    def to_sexp(self) -> str:
        """Convert to KiCad S-expression format."""
        return f"""\t(fp_arc
\t\t(start {_fmt(self.start_x)} {_fmt(self.start_y)})
\t\t(mid {_fmt(self.mid_x)} {_fmt(self.mid_y)})
\t\t(end {_fmt(self.end_x)} {_fmt(self.end_y)})
\t\t(stroke
\t\t\t(width {self.width})
\t\t\t(type solid)
\t\t)
\t\t(layer "{self.layer}")
\t)"""


@dataclass
class GraphicText:
    """A text element on a footprint layer."""

    text_type: Literal["reference", "value", "user"]
    text: str
    x: float
    y: float
    layer: str = "F.SilkS"
    font_size: float = 1.0
    font_thickness: float = 0.15
    hide: bool = False

    def to_sexp(self) -> str:
        """Convert to KiCad S-expression format."""
        hide_str = " hide" if self.hide else ""
        return f"""\t(fp_text {self.text_type} "{self.text}"
\t\t(at {_fmt(self.x)} {_fmt(self.y)})
\t\t(layer "{self.layer}"{hide_str})
\t\t(effects
\t\t\t(font
\t\t\t\t(size {self.font_size} {self.font_size})
\t\t\t\t(thickness {self.font_thickness})
\t\t\t)
\t\t)
\t)"""


@dataclass
class Footprint:
    """
    A KiCad footprint with pads, graphics, and metadata.

    This class represents a complete footprint that can be exported
    to a .kicad_mod file.
    """

    name: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    attr: Literal["smd", "through_hole"] = "smd"
    pads: list[Pad] = field(default_factory=list)
    graphics: list = field(default_factory=list)  # Lines, rects, circles, arcs, text

    def add_pad(
        self,
        name: str,
        x: float,
        y: float,
        width: float,
        height: float,
        pad_type: str = "smd",
        shape: str = "roundrect",
        drill: float | None = None,
        layers: tuple[str, ...] | None = None,
    ) -> "Footprint":
        """Add a pad to the footprint."""
        if layers is None:
            if pad_type == "smd":
                layers = ("F.Cu", "F.Paste", "F.Mask")
            else:
                layers = ("*.Cu", "*.Mask")

        self.pads.append(
            Pad(
                name=name,
                pad_type=pad_type,
                shape=shape,
                x=x,
                y=y,
                width=width,
                height=height,
                drill=drill,
                layers=layers,
            )
        )
        return self

    def add_line(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        layer: str = "F.SilkS",
        width: float = 0.12,
    ) -> "Footprint":
        """Add a line to the footprint."""
        self.graphics.append(
            GraphicLine(
                start_x=start[0],
                start_y=start[1],
                end_x=end[0],
                end_y=end[1],
                layer=layer,
                width=width,
            )
        )
        return self

    def add_rect(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        layer: str = "F.SilkS",
        width: float = 0.12,
        fill: bool = False,
    ) -> "Footprint":
        """Add a rectangle to the footprint."""
        self.graphics.append(
            GraphicRect(
                start_x=start[0],
                start_y=start[1],
                end_x=end[0],
                end_y=end[1],
                layer=layer,
                width=width,
                fill=fill,
            )
        )
        return self

    def add_circle(
        self,
        center: tuple[float, float],
        radius: float,
        layer: str = "F.SilkS",
        width: float = 0.12,
        fill: bool = False,
    ) -> "Footprint":
        """Add a circle to the footprint."""
        self.graphics.append(
            GraphicCircle(
                center_x=center[0],
                center_y=center[1],
                radius=radius,
                layer=layer,
                width=width,
                fill=fill,
            )
        )
        return self

    def add_text(
        self,
        text_type: str,
        text: str,
        position: tuple[float, float],
        layer: str = "F.SilkS",
        font_size: float = 1.0,
        hide: bool = False,
    ) -> "Footprint":
        """Add text to the footprint."""
        self.graphics.append(
            GraphicText(
                text_type=text_type,
                text=text,
                x=position[0],
                y=position[1],
                layer=layer,
                font_size=font_size,
                hide=hide,
            )
        )
        return self

    def to_sexp(self) -> str:
        """Convert footprint to KiCad S-expression format."""
        lines = [f'(footprint "{self.name}"']
        lines.append("\t(version 20241229)")
        lines.append('\t(generator "kicad_tools")')
        lines.append('\t(layer "F.Cu")')

        if self.description:
            lines.append(f'\t(descr "{self.description}")')

        if self.tags:
            tags_str = " ".join(self.tags)
            lines.append(f'\t(tags "{tags_str}")')

        # Reference and Value properties (always present)
        ref_y = self._get_ref_position()
        lines.append('\t(property "Reference" "REF**"')
        lines.append(f"\t\t(at 0 {_fmt(ref_y)} 0)")
        lines.append('\t\t(layer "F.SilkS")')
        lines.append("\t\t(effects")
        lines.append("\t\t\t(font")
        lines.append("\t\t\t\t(size 1 1)")
        lines.append("\t\t\t\t(thickness 0.15)")
        lines.append("\t\t\t)")
        lines.append("\t\t)")
        lines.append("\t)")

        lines.append(f'\t(property "Value" "{self.name}"')
        lines.append(f"\t\t(at 0 {_fmt(-ref_y)} 0)")
        lines.append('\t\t(layer "F.Fab")')
        lines.append("\t\t(effects")
        lines.append("\t\t\t(font")
        lines.append("\t\t\t\t(size 1 1)")
        lines.append("\t\t\t\t(thickness 0.15)")
        lines.append("\t\t\t)")
        lines.append("\t\t)")
        lines.append("\t)")

        # Attribute
        lines.append(f"\t(attr {self.attr})")

        # Graphics
        for graphic in self.graphics:
            lines.append(graphic.to_sexp())

        # Pads
        for pad in self.pads:
            lines.append(pad.to_sexp())

        lines.append(")")
        return "\n".join(lines)

    def _get_ref_position(self) -> float:
        """Calculate reference text position based on footprint size."""
        if not self.pads:
            return -2.0

        min_y = min(p.y - p.height / 2 for p in self.pads)
        return min_y - 1.5

    def save(self, filepath: str | Path) -> None:
        """Save footprint to a .kicad_mod file."""
        filepath = Path(filepath)
        ensure_parent_dir(filepath).write_text(self.to_sexp())


def _fmt(val: float) -> str:
    """Format a float value, removing trailing zeros."""
    if val == int(val):
        return str(int(val))
    # Round to 3 decimal places
    rounded = round(val, 3)
    if rounded == int(rounded):
        return str(int(rounded))
    return str(rounded)
