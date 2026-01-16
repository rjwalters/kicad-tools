"""
Programmatic footprint generation module.

Provides classes for generating KiCad footprints programmatically,
including common package types (0402, 0603, SOT-23, QFN, etc.).
"""

from enum import Enum

from kicad_tools.sexp import SExp
from kicad_tools.sexp.builders import fmt

__all__ = [
    "PadType",
    "PadShape",
    "Layer",
    "Pad",
    "Footprint",
]


class PadType(Enum):
    """Pad mounting type."""

    SMD = "smd"
    THT = "thru_hole"
    NPTH = "np_thru_hole"
    CONNECT = "connect"


class PadShape(Enum):
    """Pad shape."""

    RECT = "rect"
    ROUNDRECT = "roundrect"
    CIRCLE = "circle"
    OVAL = "oval"
    TRAPEZOID = "trapezoid"


class Layer(Enum):
    """PCB layers."""

    F_CU = "F.Cu"
    B_CU = "B.Cu"
    F_PASTE = "F.Paste"
    B_PASTE = "B.Paste"
    F_MASK = "F.Mask"
    B_MASK = "B.Mask"
    F_SILKS = "F.SilkS"
    B_SILKS = "B.SilkS"
    F_CRTYD = "F.CrtYd"
    B_CRTYD = "B.CrtYd"
    EDGE_CUTS = "Edge.Cuts"


class Pad:
    """A copper pad on a footprint."""

    def __init__(
        self,
        number: str,
        pad_type: PadType,
        shape: PadShape,
        position: tuple[float, float],
        size: tuple[float, float],
        layers: list[Layer],
        drill: float = 0,
        roundrect_rratio: float = 0.25,
    ):
        self.number = number
        self.pad_type = pad_type
        self.shape = shape
        self.position = position
        self.size = size
        self.layers = layers
        self.drill = drill
        self.roundrect_rratio = roundrect_rratio

    def to_sexp(self) -> SExp:
        """Convert to KiCad S-expression format.

        Returns:
            SExp node representing this pad.

        Example output::

            (pad "1" smd roundrect
                (at -0.48 0)
                (size 0.56 0.62)
                (layers "F.Cu" "F.Paste" "F.Mask")
                (roundrect_rratio 0.25)
            )
        """
        # Build pad node: (pad "number" type shape ...)
        pad = SExp.list("pad", self.number, self.pad_type.value, self.shape.value)

        # Position: (at x y)
        x, y = self.position
        pad.append(SExp.list("at", fmt(x), fmt(y)))

        # Size: (size w h)
        w, h = self.size
        pad.append(SExp.list("size", fmt(w), fmt(h)))

        # Drill for THT pads: (drill d)
        if self.pad_type in (PadType.THT, PadType.NPTH) and self.drill > 0:
            pad.append(SExp.list("drill", fmt(self.drill)))

        # Layers: (layers "F.Cu" "F.Paste" "F.Mask")
        layers_node = SExp.list("layers")
        for layer in self.layers:
            layers_node.append(SExp.atom(layer.value))
        pad.append(layers_node)

        # Roundrect ratio for rounded rectangle pads
        if self.shape == PadShape.ROUNDRECT:
            pad.append(SExp.list("roundrect_rratio", fmt(self.roundrect_rratio)))

        return pad


class Footprint:
    """
    A KiCad footprint definition.

    Example::

        from kicad_tools.footprints import Footprint, Pad, PadType, PadShape, Layer

        fp = Footprint(
            name="C_0402_1005Metric",
            description="Capacitor SMD 0402 (1005 Metric)",
            tags="capacitor smd 0402",
        )
        fp.add_pad(Pad(
            number="1",
            pad_type=PadType.SMD,
            shape=PadShape.ROUNDRECT,
            position=(-0.48, 0),
            size=(0.56, 0.62),
            layers=[Layer.F_CU, Layer.F_PASTE, Layer.F_MASK],
        ))
        fp.add_pad(Pad(
            number="2",
            pad_type=PadType.SMD,
            shape=PadShape.ROUNDRECT,
            position=(0.48, 0),
            size=(0.56, 0.62),
            layers=[Layer.F_CU, Layer.F_PASTE, Layer.F_MASK],
        ))

        sexp_str = fp.to_sexp()
    """

    def __init__(
        self,
        name: str,
        description: str = "",
        tags: str = "",
        layer: Layer = Layer.F_CU,
    ):
        self.name = name
        self.description = description
        self.tags = tags
        self.layer = layer
        self.pads: list[Pad] = []
        self.silkscreen: list[str] = []
        self.courtyard: list[str] = []

    def add_pad(self, pad: Pad) -> None:
        """Add a pad to the footprint."""
        self.pads.append(pad)

    def _get_attr(self) -> str:
        """Determine footprint attribute (smd or through_hole) from pad types."""
        has_tht = any(p.pad_type in (PadType.THT, PadType.NPTH) for p in self.pads)
        has_smd = any(p.pad_type == PadType.SMD for p in self.pads)

        # If only SMD pads, return smd
        if has_smd and not has_tht:
            return "smd"
        # If any THT pads, return through_hole
        if has_tht:
            return "through_hole"
        # Default to smd if no pads yet
        return "smd"

    def to_sexp(self) -> str:
        """Convert to KiCad S-expression format (.kicad_mod).

        Returns:
            String containing the complete footprint S-expression.

        Example output::

            (footprint "C_0402_1005Metric"
                (version 20240108)
                (generator "kicad-tools")
                (layer "F.Cu")
                (descr "Capacitor SMD 0402")
                (tags "capacitor smd 0402")
                (attr smd)
                (pad "1" smd roundrect
                    (at -0.48 0)
                    (size 0.56 0.62)
                    (layers "F.Cu" "F.Paste" "F.Mask")
                    (roundrect_rratio 0.25)
                )
                ...
            )
        """
        # Build footprint node
        footprint = SExp.list("footprint", self.name)

        # Version and generator
        footprint.append(SExp.list("version", 20240108))
        footprint.append(SExp.list("generator", "kicad-tools"))

        # Layer
        footprint.append(SExp.list("layer", self.layer.value))

        # Description and tags (if provided)
        if self.description:
            footprint.append(SExp.list("descr", self.description))
        if self.tags:
            footprint.append(SExp.list("tags", self.tags))

        # Attribute (smd or through_hole)
        footprint.append(SExp.list("attr", self._get_attr()))

        # Add all pads
        for pad in self.pads:
            footprint.append(pad.to_sexp())

        return footprint.to_string()
