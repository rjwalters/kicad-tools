"""KiCad PCB data models.

Provides classes for parsing and manipulating KiCad PCB files (.kicad_pcb).
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

logger = logging.getLogger(__name__)

from kicad_tools.sexp import SExp

from ..core.sexp_file import load_footprint, load_pcb, save_pcb
from ..core.version import KICAD_BOARD_FORMAT_VERSION
from ..footprints.library_path import (
    detect_kicad_library_path,
    guess_standard_library,
    parse_library_id,
)

if TYPE_CHECKING:
    from ..manufacturers import DesignRules
    from ..query.footprints import FootprintList

# Default regex for detecting power/ground net names.
# Matches names like GND, +3V3, +5V, VCC, VDD, VBUS, or names starting with '+'.
_DEFAULT_POWER_NET_PATTERN = re.compile(
    r"^(\+|GND|GNDA|GNDPWR|VCC|VDD|VBUS|VSS|V[0-9])",
    re.IGNORECASE,
)


def _is_power_net(name: str, pattern: re.Pattern[str] | None = None) -> bool:
    """Return True if *name* looks like a power or ground net.

    Uses *pattern* if provided, otherwise falls back to the built-in
    heuristic ``_DEFAULT_POWER_NET_PATTERN``.
    """
    if not name:
        return False
    pat = pattern if pattern is not None else _DEFAULT_POWER_NET_PATTERN
    return pat.search(name) is not None


def _segment_span_intersects_region(
    start: tuple[float, float],
    end: tuple[float, float],
    region: tuple[float, float, float, float],
) -> bool:
    """Return True if the segment ``start``->``end`` crosses the axis-aligned box.

    All coordinates are board-relative.  ``region`` is a normalized
    ``(x1, y1, x2, y2)`` box with ``x1 <= x2`` and ``y1 <= y2``.  Uses the
    Liang-Barsky parametric clip test.  This is only used to decide whether a
    both-endpoints-outside segment should be *reported* as boundary-skipped
    (it is never itself modified), so a conservative True on tangency is fine.
    """
    x1, y1, x2, y2 = region
    sx, sy = start
    ex, ey = end
    dx = ex - sx
    dy = ey - sy
    # Degenerate (zero-length) segment: treat as a point-in-box test.
    if dx == 0.0 and dy == 0.0:
        return x1 <= sx <= x2 and y1 <= sy <= y2
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, sx - x1), (dx, x2 - sx), (-dy, sy - y1), (dy, y2 - sy)):
        if p == 0.0:
            if q < 0.0:
                return False  # Parallel to this edge and outside the slab.
            continue
        r = q / p
        if p < 0.0:
            if r > t1:
                return False
            if r > t0:
                t0 = r
        else:
            if r < t0:
                return False
            if r < t1:
                t1 = r
    return t0 <= t1


# ---------------------------------------------------------------------------
# Power-rail name canonicalisation (issue #3302)
# ---------------------------------------------------------------------------
#
# KiCad's stock ``power:`` library uses ``V`` as a decimal separator for
# sub-volt fractional rails (``+3V3``, ``+1V8``, ``+2V5``) while the
# kicad-tools netlist-sync convention emits the same rails with a decimal
# point (``+3.3V``, ``+1.8V``, ``+2.5V``). The two forms refer to the
# same electrical net but the strings differ, so a naive set-difference
# between schematic-label names and PCB-net names sees a spurious add
# and a spurious remove for every fractional rail.
#
# ``_POWER_RAIL_ALIAS_RE`` recognises the canonical-form pair and
# ``canonicalize_power_net`` rewrites both forms to a single canonical
# string (``+N.MV``, no decimal-point removal) so the two sides of a
# drift comparison agree without affecting non-power names. Whole-volt
# rails (``+5V`` / ``+5.0V``) are also normalised; this keeps the table
# closed and avoids drift surprises on boards that emit ``+5.0V``.
#
# The table is deliberately limited to the ``+N`` and ``+N.M`` rails
# called out in the schema convention documentation
# (``src/kicad_tools/schematic/models/elements_mixin.py:394-397``). It
# is *not* a general fuzzy net matcher; non-matching names are returned
# unchanged.

# ``+3V3`` / ``+3.3V`` / ``+3.0V`` (whole volt with explicit fraction)
_POWER_RAIL_V_DECIMAL_RE = re.compile(r"^([+\-])(\d+)V(\d+)$")
_POWER_RAIL_DOT_DECIMAL_RE = re.compile(r"^([+\-])(\d+)\.(\d+)V$")
_POWER_RAIL_WHOLE_RE = re.compile(r"^([+\-])(\d+)V$")


def canonicalize_power_net(name: str) -> str:
    """Return the canonical form of a power-rail net name.

    The canonical form is ``+N.MV`` for fractional rails (``+3V3`` ->
    ``+3.3V``) and ``+NV`` for whole-volt rails (``+5.0V`` -> ``+5V``).
    Names that don't match the power-rail-alias grammar are returned
    unchanged, so the function is a pure no-op for signal names like
    ``BOOT0``, ``LED_K`` or ``USB_CC1``.

    Examples:
        ``+3V3``    -> ``+3.3V``
        ``+3.3V``   -> ``+3.3V``
        ``+1V8``    -> ``+1.8V``
        ``+5V``     -> ``+5V``
        ``+5.0V``   -> ``+5V``
        ``-3V3``    -> ``-3.3V``
        ``VBUS``    -> ``VBUS`` (no change)
        ``BOOT0``   -> ``BOOT0`` (no change)
    """
    if not name:
        return name
    m = _POWER_RAIL_V_DECIMAL_RE.match(name)
    if m:
        sign, whole, frac = m.group(1), m.group(2), m.group(3)
        # Strip a trailing zero fraction (``+3V0`` -> ``+3V`` canonical).
        if frac == "0":
            return f"{sign}{whole}V"
        return f"{sign}{whole}.{frac}V"
    m = _POWER_RAIL_DOT_DECIMAL_RE.match(name)
    if m:
        sign, whole, frac = m.group(1), m.group(2), m.group(3)
        # Whole-volt rail expressed with explicit ``.0`` fraction.
        if frac == "0":
            return f"{sign}{whole}V"
        return f"{sign}{whole}.{frac}V"
    m = _POWER_RAIL_WHOLE_RE.match(name)
    if m:
        sign, whole = m.group(1), m.group(2)
        return f"{sign}{whole}V"
    return name


def canonicalize_power_nets(names: set[str]) -> set[str]:
    """Apply :func:`canonicalize_power_net` to every name in a set.

    Returns a new set; the input is not mutated. Non-power-rail names
    pass through unchanged so the caller can treat the result as a
    drop-in replacement for the original set.
    """
    return {canonicalize_power_net(n) for n in names}


@dataclass
class Layer:
    """PCB layer definition."""

    number: int
    name: str
    type: str  # signal, power, user


@dataclass
class Net:
    """PCB net definition."""

    number: int
    name: str


@dataclass
class Pad:
    """Component pad."""

    number: str
    type: str  # smd, thru_hole
    shape: str  # roundrect, rect, circle, oval
    position: tuple[float, float]
    size: tuple[float, float]
    layers: list[str]
    net_number: int = 0
    net_name: str = ""
    drill: float = 0.0
    solder_mask_margin: float | None = None
    uuid: str = ""
    # Per-pad rotation in degrees (the optional third token of ``(at x y angle)``).
    # KiCad stores this angle in the ABSOLUTE board frame -- it already includes
    # the parent footprint's rotation, so ``pad.rotation`` IS the pad's absolute
    # orientation (do NOT add ``footprint.rotation`` on top of it). Note the
    # asymmetry with ``position``, which is stored footprint-local and must be
    # rotated by ``footprint.rotation`` to reach board coordinates. Defaults to 0.
    rotation: float = 0.0
    # Corner-radius ratio for ``roundrect`` pads: radius = rratio * min(w, h).
    # KiCad's default is 0.25 when ``(roundrect_rratio ...)`` is absent.
    roundrect_rratio: float = 0.25

    @property
    def net(self) -> int:
        """Net ID (alias for net_number for API consistency)."""
        return self.net_number

    @classmethod
    def from_sexp(cls, sexp: SExp) -> Pad | None:
        """Parse pad from S-expression."""
        pad = cls(
            number=sexp.get_string(0) or "",
            type=sexp.get_string(1) or "",
            shape=sexp.get_string(2) or "",
            position=(0.0, 0.0),
            size=(0.0, 0.0),
            layers=[],
        )

        # Position (and optional per-pad rotation as the third token)
        if at := sexp.find("at"):
            x = at.get_float(0) or 0.0
            y = at.get_float(1) or 0.0
            pad.position = (x, y)
            pad.rotation = at.get_float(2) or 0.0

        # Size
        if size := sexp.find("size"):
            w = size.get_float(0) or 0.0
            h = size.get_float(1) or w
            pad.size = (w, h)

        # Corner radius ratio for roundrect pads (default 0.25 when absent)
        if rratio := sexp.find("roundrect_rratio"):
            value = rratio.get_float(0)
            if value is not None:
                pad.roundrect_rratio = value

        # Layers
        if layers := sexp.find("layers"):
            pad.layers = [
                layers.get_string(i) or ""
                for i in range(len(layers.values))
                if isinstance(layers.values[i], str)
            ]

        # Net — handles both (net N "name") and (net "name") formats.
        # KiCad 10 may emit (net "name") without a numeric net number.
        if net := sexp.find("net"):
            first_int = net.get_int(0)
            if first_int is not None:
                # Traditional format: (net N "name")
                pad.net_number = first_int
                pad.net_name = net.get_string(1) or ""
            else:
                # KiCad 10 name-only format: (net "name")
                pad.net_number = 0
                pad.net_name = net.get_string(0) or ""

        # Drill
        if drill := sexp.find("drill"):
            pad.drill = drill.get_float(0) or 0.0

        # Solder mask margin (per-pad override)
        if mask_margin := sexp.find("solder_mask_margin"):
            pad.solder_mask_margin = mask_margin.get_float(0)

        # UUID
        if uuid := sexp.find("uuid"):
            pad.uuid = uuid.get_string(0) or ""

        return pad


@dataclass
class FootprintText:
    """Text element within a footprint (fp_text).

    Used for reference designators, values, and user text on footprints.
    Contains font information for silkscreen validation.
    """

    text_type: str  # reference, value, user
    text: str
    position: tuple[float, float]
    layer: str
    font_size: tuple[float, float]  # (width, height) in mm
    font_thickness: float  # stroke thickness in mm
    uuid: str = ""
    hidden: bool = False

    @classmethod
    def from_sexp(cls, sexp: SExp) -> FootprintText:
        """Parse footprint text from S-expression."""
        text_type = sexp.get_string(0) or ""
        text = sexp.get_string(1) or ""

        fp_text = cls(
            text_type=text_type,
            text=text,
            position=(0.0, 0.0),
            layer="",
            font_size=(1.0, 1.0),
            font_thickness=0.15,
        )

        # Position
        if at := sexp.find("at"):
            x = at.get_float(0) or 0.0
            y = at.get_float(1) or 0.0
            fp_text.position = (x, y)

        # Layer
        if layer := sexp.find("layer"):
            fp_text.layer = layer.get_string(0) or ""

        # UUID
        if uuid := sexp.find("uuid"):
            fp_text.uuid = uuid.get_string(0) or ""

        # Effects (font size and thickness)
        if effects := sexp.find("effects"):
            if effects.find("hide"):
                fp_text.hidden = True
            if font := effects.find("font"):
                if size := font.find("size"):
                    w = size.get_float(0) or 1.0
                    h = size.get_float(1) or w
                    fp_text.font_size = (w, h)
                if thickness := font.find("thickness"):
                    fp_text.font_thickness = thickness.get_float(0) or 0.15

        return fp_text

    @classmethod
    def _from_property_sexp(cls, sexp: SExp, text_type: str) -> FootprintText:
        """Parse footprint text from property S-expression (KiCad 8+ format).

        Property nodes have a different structure than fp_text nodes:
        (property "Reference" "U1" (at 0 -4) (layer "F.SilkS") (effects ...))
        """
        text = sexp.get_string(1) or ""

        fp_text = cls(
            text_type=text_type,
            text=text,
            position=(0.0, 0.0),
            layer="",
            font_size=(1.0, 1.0),
            font_thickness=0.15,
        )

        # Position
        if at := sexp.find("at"):
            x = at.get_float(0) or 0.0
            y = at.get_float(1) or 0.0
            fp_text.position = (x, y)

        # Layer
        if layer := sexp.find("layer"):
            fp_text.layer = layer.get_string(0) or ""

        # UUID
        if uuid := sexp.find("uuid"):
            fp_text.uuid = uuid.get_string(0) or ""

        # Hidden check - property format uses (hide yes) directly on the property
        if hide := sexp.find("hide"):
            hide_val = hide.get_string(0)
            fp_text.hidden = hide_val == "yes"

        # Effects (font size and thickness)
        if effects := sexp.find("effects"):
            if effects.find("hide"):
                fp_text.hidden = True
            if font := effects.find("font"):
                if size := font.find("size"):
                    w = size.get_float(0) or 1.0
                    h = size.get_float(1) or w
                    fp_text.font_size = (w, h)
                if thickness := font.find("thickness"):
                    fp_text.font_thickness = thickness.get_float(0) or 0.15

        return fp_text

    @property
    def font_height(self) -> float:
        """Font height in mm (used for minimum text height checks)."""
        return self.font_size[1]


@dataclass
class FootprintGraphic:
    """Graphic element within a footprint (fp_line, fp_rect, fp_circle, fp_arc).

    Used for silkscreen outlines and markings on footprints.
    """

    graphic_type: str  # line, rect, circle, arc
    layer: str
    stroke_width: float  # in mm
    start: tuple[float, float] = (0.0, 0.0)
    end: tuple[float, float] = (0.0, 0.0)
    center: tuple[float, float] | None = None
    radius: float | None = None
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp, graphic_type: str) -> FootprintGraphic:
        """Parse footprint graphic from S-expression."""
        graphic = cls(
            graphic_type=graphic_type,
            layer="",
            stroke_width=0.0,
        )

        # Layer
        if layer := sexp.find("layer"):
            graphic.layer = layer.get_string(0) or ""

        # Stroke width
        if stroke := sexp.find("stroke"):
            if width := stroke.find("width"):
                graphic.stroke_width = width.get_float(0) or 0.0

        # Start/end points (for line, rect)
        if start := sexp.find("start"):
            graphic.start = (start.get_float(0) or 0.0, start.get_float(1) or 0.0)
        if end := sexp.find("end"):
            graphic.end = (end.get_float(0) or 0.0, end.get_float(1) or 0.0)

        # Center/radius (for circle)
        if center := sexp.find("center"):
            graphic.center = (center.get_float(0) or 0.0, center.get_float(1) or 0.0)
        if end := sexp.find("end"):
            # For circles, end is a point on the circumference
            pass

        # UUID
        if uuid := sexp.find("uuid"):
            graphic.uuid = uuid.get_string(0) or ""

        return graphic


@dataclass
class GraphicText:
    """Board-level text element (gr_text).

    Used for board markings, labels, and silkscreen text not tied to footprints.
    """

    text: str
    position: tuple[float, float]
    layer: str
    font_size: tuple[float, float]  # (width, height) in mm
    font_thickness: float  # stroke thickness in mm
    uuid: str = ""
    hidden: bool = False

    @classmethod
    def from_sexp(cls, sexp: SExp) -> GraphicText:
        """Parse graphic text from S-expression."""
        text = sexp.get_string(0) or ""

        gr_text = cls(
            text=text,
            position=(0.0, 0.0),
            layer="",
            font_size=(1.0, 1.0),
            font_thickness=0.15,
        )

        # Position
        if at := sexp.find("at"):
            gr_text.position = (at.get_float(0) or 0.0, at.get_float(1) or 0.0)

        # Layer
        if layer := sexp.find("layer"):
            gr_text.layer = layer.get_string(0) or ""

        # UUID
        if uuid := sexp.find("uuid"):
            gr_text.uuid = uuid.get_string(0) or ""

        # Effects (font size and thickness)
        if effects := sexp.find("effects"):
            if effects.find("hide"):
                gr_text.hidden = True
            if font := effects.find("font"):
                if size := font.find("size"):
                    w = size.get_float(0) or 1.0
                    h = size.get_float(1) or w
                    gr_text.font_size = (w, h)
                if thickness := font.find("thickness"):
                    gr_text.font_thickness = thickness.get_float(0) or 0.15

        return gr_text

    @property
    def font_height(self) -> float:
        """Font height in mm (used for minimum text height checks)."""
        return self.font_size[1]


@dataclass
class BoardGraphic:
    """Board-level graphic element (gr_line, gr_rect, gr_circle, gr_arc).

    Used for board outlines, silkscreen graphics, and other board-level drawings.
    """

    graphic_type: str  # line, rect, circle, arc
    layer: str
    stroke_width: float  # in mm
    start: tuple[float, float] = (0.0, 0.0)
    end: tuple[float, float] = (0.0, 0.0)
    center: tuple[float, float] | None = None
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp, graphic_type: str) -> BoardGraphic:
        """Parse board graphic from S-expression."""
        graphic = cls(
            graphic_type=graphic_type,
            layer="",
            stroke_width=0.0,
        )

        # Layer
        if layer := sexp.find("layer"):
            graphic.layer = layer.get_string(0) or ""

        # Stroke width
        if stroke := sexp.find("stroke"):
            if width := stroke.find("width"):
                graphic.stroke_width = width.get_float(0) or 0.0

        # Start/end points
        if start := sexp.find("start"):
            graphic.start = (start.get_float(0) or 0.0, start.get_float(1) or 0.0)
        if end := sexp.find("end"):
            graphic.end = (end.get_float(0) or 0.0, end.get_float(1) or 0.0)

        # Center (for circle/arc)
        if center := sexp.find("center"):
            graphic.center = (center.get_float(0) or 0.0, center.get_float(1) or 0.0)

        # UUID
        if uuid := sexp.find("uuid"):
            graphic.uuid = uuid.get_string(0) or ""

        return graphic


@dataclass
class Footprint:
    """PCB component footprint.

    The ``position``, ``rotation``, and ``layer`` attributes are backed by
    a ``__setattr__`` override that keeps the underlying S-expression node
    in sync.  After a :class:`PCB` is fully constructed (parsing and
    board-origin detection complete), the PCB links each ``Footprint`` to
    its S-expression node via :pyattr:`_sexp_node` and stores the board
    origin offset in :pyattr:`_board_origin`.  From that point on, any
    assignment to ``position``, ``rotation``, or ``layer`` is
    automatically reflected in the S-expression tree that
    :meth:`PCB.save` serialises.
    """

    name: str
    layer: str
    position: tuple[float, float]
    rotation: float
    reference: str
    value: str
    pads: list[Pad] = field(default_factory=list)
    texts: list[FootprintText] = field(default_factory=list)
    graphics: list[FootprintGraphic] = field(default_factory=list)
    uuid: str = ""
    description: str = ""
    tags: str = ""
    attr: str = ""  # smd, through_hole
    exclude_from_pos_files: bool = False
    exclude_from_bom: bool = False
    locked: bool = False
    dnp: bool = False
    properties: dict[str, str] = field(default_factory=dict)
    _sexp_node: SExp | None = field(default=None, repr=False, compare=False)
    _board_origin: tuple[float, float] = field(
        default=(0.0, 0.0),
        repr=False,
        compare=False,
    )
    # Tokens inside ``(attr ...)`` that the parser does not yet model
    # (e.g. ``board_only``, ``allow_missing_courtyard``,
    # ``allow_soldermask_bridges``). Preserved verbatim so that the
    # ``__setattr__`` rebuild of the ``(attr ...)`` block does not
    # silently drop them on round-trip.
    _attr_unknown_tokens: list[str] = field(
        default_factory=list,
        repr=False,
        compare=False,
    )

    # Set of attr-block tokens the parser models explicitly. Anything
    # else that appears as an atom child of ``(attr ...)`` is captured
    # in ``_attr_unknown_tokens`` and re-emitted verbatim.
    _ATTR_KNOWN_TOKENS: ClassVar[frozenset[str]] = frozenset(
        {
            "smd",
            "through_hole",
            "exclude_from_pos_files",
            "exclude_from_bom",
            "locked",
            "dnp",
        }
    )

    _ATTR_SYNCED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "attr",
            "exclude_from_pos_files",
            "exclude_from_bom",
            "locked",
            "dnp",
            "_attr_unknown_tokens",
        }
    )

    # ------------------------------------------------------------------
    # __setattr__ override -- syncs position/rotation/layer to _sexp_node
    # ------------------------------------------------------------------

    def __setattr__(self, name: str, value: object) -> None:
        # Always store the Python value first via the default mechanism.
        super().__setattr__(name, value)

        # Bail out during __init__ (before _sexp_node exists) or when
        # there is no backing S-expression node.
        sexp_node: SExp | None = self.__dict__.get("_sexp_node")
        if sexp_node is None:
            return

        # Direct children only for the (at)/(layer) lookups below:
        # properties, fp_texts and pads carry their own (at)/(layer)
        # nodes, and a recursive find() would mutate a descendant's
        # node if the footprint-level one were absent (issue #3602).
        if name == "position":
            at_node = sexp_node.find_child("at")
            if at_node is not None:
                x, y = value  # type: ignore[unpacking-non-sequence]
                # ``fp.position`` stores board-relative coordinates but the
                # S-expression ``(at ...)`` node uses sheet-absolute values.
                # Add the board origin offset to convert.
                origin = self.__dict__.get("_board_origin", (0.0, 0.0))
                at_node.set_value(0, x + origin[0])
                at_node.set_value(1, y + origin[1])

        elif name == "rotation":
            at_node = sexp_node.find_child("at")
            if at_node is not None:
                if len(at_node.children) >= 3:
                    at_node.set_value(2, value)
                elif value != 0.0:
                    at_node.add(value)

        elif name == "layer":
            layer_node = sexp_node.find_child("layer")
            if layer_node is not None:
                layer_node.set_value(0, value)

        elif name in self._ATTR_SYNCED_FIELDS:
            # Any change to a field that maps into the (attr ...) block
            # rebuilds that block from the current Python state. Unknown
            # tokens captured at parse time (board_only,
            # allow_missing_courtyard, allow_soldermask_bridges, ...)
            # are preserved verbatim via ``_attr_unknown_tokens`` so we
            # don't drop tokens the parser doesn't yet model.
            self._sync_attr_node()

    def _sync_attr_node(self) -> None:
        """Rebuild the ``(attr ...)`` child of ``_sexp_node`` from Python state.

        Mirrors the position/rotation/layer sync pattern in
        :meth:`__setattr__`. Removes the existing ``(attr ...)`` node
        (if any), then re-emits it from the current values of
        ``attr``, ``exclude_from_pos_files``, ``exclude_from_bom``,
        ``dnp``, and any ``_attr_unknown_tokens`` captured during
        parse. If no flags are set and no unknown tokens are present,
        no ``(attr ...)`` node is emitted (matches KiCad's canonical
        "no flags" form).

        ``locked`` is emitted as a top-level ``(locked yes)`` child of
        the footprint, NEVER as an in-attr ``locked`` token: KiCad 10's
        kicad-cli rejects the legacy KiCad-6 ``(attr smd locked)`` form
        with "Failed to load board", which silently breaks zone fill,
        DRC and gerber export for any board re-saved through the schema
        layer (issue #3457). Parsing still accepts both forms (see
        :meth:`from_sexp`), so loading a legacy file and saving it
        migrates the lock to the modern form.
        """
        sexp_node: SExp | None = self.__dict__.get("_sexp_node")
        if sexp_node is None:
            return

        # Remove any existing (attr ...) so we can rebuild it cleanly.
        # find_children() searches direct children only -- we want to
        # avoid stripping nested (attr ...) inside fp_text/property,
        # which don't currently exist but could appear in future
        # KiCad versions.
        for existing in list(sexp_node.find_children("attr")):
            sexp_node.remove(existing)

        # Remove any existing top-level (locked ...) so we can re-emit
        # it from Python state below. Direct children only -- pads can
        # legitimately carry their own (locked yes), which must not be
        # stripped here.
        for existing in list(sexp_node.find_children("locked")):
            sexp_node.remove(existing)

        # Defensive: 'locked' is a modeled token (parsed into
        # ``fp.locked``), but if it ever leaks into the unknown-token
        # list it must not be echoed back into (attr ...) -- KiCad 10
        # rejects that form.
        unknown = [
            token
            for token in (self.__dict__.get("_attr_unknown_tokens", []) or [])
            if token != "locked"
        ]

        # Only emit (attr ...) if there is something to say.
        if self.attr or self.dnp or self.exclude_from_pos_files or self.exclude_from_bom or unknown:
            attr_node = SExp.list("attr")
            # Canonical KiCad order: <type> [board_only]
            # [exclude_from_pos_files] [exclude_from_bom]
            # [allow_missing_courtyard] [dnp]. We emit modeled tokens in
            # a stable order and append unknown tokens at the end --
            # KiCad accepts any ordering of these atoms, so this is a
            # safe simplification.
            if self.attr:
                attr_node.add(self.attr)  # 'smd' or 'through_hole'
            if self.exclude_from_pos_files:
                attr_node.add("exclude_from_pos_files")
            if self.exclude_from_bom:
                attr_node.add("exclude_from_bom")
            if self.dnp:
                attr_node.add("dnp")
            for token in unknown:
                attr_node.add(token)

            sexp_node.append(attr_node)

        # Modern lock form: top-level (locked yes), the only form
        # KiCad 10's kicad-cli accepts (issue #3410 / #3457).
        if self.locked:
            sexp_node.append(SExp.list("locked", "yes"))

    @classmethod
    def from_sexp(cls, sexp: SExp) -> Footprint:
        """Parse footprint from S-expression.

        Note: ``_sexp_node`` is intentionally **not** set here.  It is
        linked later by :meth:`PCB._link_footprint_sexp_nodes` after the
        board origin has been detected and footprint positions have been
        converted to board-relative coordinates.  This prevents the
        ``__setattr__`` sync from writing stale values during parsing.
        """
        name = sexp.get_string(0) or ""

        fp = cls(
            name=name,
            layer="F.Cu",
            position=(0.0, 0.0),
            rotation=0.0,
            reference="",
            value="",
            pads=[],
            texts=[],
            graphics=[],
        )

        # NOTE: all footprint-level scalar tokens below use
        # find_child() (direct children only), NOT the recursive
        # find(). Descendants legitimately carry same-named tokens --
        # pads have (at)/(uuid)/(locked), properties and fp_texts have
        # (at)/(layer)/(uuid) -- and a recursive lookup would misread
        # a descendant's token as the footprint's own whenever the
        # footprint-level token is absent (issue #3602).

        # Layer
        if layer := sexp.find_child("layer"):
            fp.layer = layer.get_string(0) or "F.Cu"

        # Position
        if at := sexp.find_child("at"):
            x = at.get_float(0) or 0.0
            y = at.get_float(1) or 0.0
            rot = at.get_float(2) or 0.0
            fp.position = (x, y)
            fp.rotation = rot

        # UUID
        if uuid := sexp.find_child("uuid"):
            fp.uuid = uuid.get_string(0) or ""

        # Description and tags
        if descr := sexp.find_child("descr"):
            fp.description = descr.get_string(0) or ""
        if tags := sexp.find_child("tags"):
            fp.tags = tags.get_string(0) or ""

        # Modern lock form: a top-level ``(locked yes)`` child (KiCad 9/10).
        # KiCad 10's kicad-cli REJECTS the legacy ``locked`` token inside
        # ``(attr ...)`` ("Failed to load board"), so generators that need
        # kicad-cli interop (zone fill, DRC, gerber export) emit the
        # top-level form instead -- without this branch the router's
        # anchor logic (``getattr(fp, "locked", False)``, issue #2845)
        # would silently stop seeing those footprints as locked
        # (issue #3410).
        #
        # Direct children only: PADS can carry their own ``(locked
        # yes)``, and the recursive find() previously misread a
        # pad-level lock as the footprint's, so unlocking a footprint
        # with locked pads didn't persist across save/reload
        # (issue #3602).
        if locked_node := sexp.find_child("locked"):
            if (locked_node.get_string(0) or "yes") in ("yes", "true"):
                fp.locked = True

        if attr := sexp.find_child("attr"):
            # The footprint *type* token (``smd`` / ``through_hole``)
            # is optional in KiCad's emitted form. When KiCad omits the
            # type, the first atom is a flag (e.g. ``(attr
            # exclude_from_pos_files exclude_from_bom)``). Detect this
            # by only accepting known type tokens at index 0.
            first_token = attr.get_string(0) or ""
            if first_token in ("smd", "through_hole"):
                fp.attr = first_token
                token_start_idx = 1
            else:
                fp.attr = ""
                token_start_idx = 0
            # Parse additional attribute flags (e.g., exclude_from_pos_files, dnp).
            # Tokens we don't model (board_only, allow_missing_courtyard,
            # allow_soldermask_bridges, ...) are captured verbatim into
            # _attr_unknown_tokens so the (attr ...) rebuild in
            # _sync_attr_node() can re-emit them on save without
            # silent data loss.
            unknown_tokens: list[str] = []
            for i in range(token_start_idx, len(attr.children)):
                token = attr.get_string(i)
                if token is None:
                    continue
                if token == "exclude_from_pos_files":
                    fp.exclude_from_pos_files = True
                elif token == "exclude_from_bom":
                    fp.exclude_from_bom = True
                elif token == "locked":
                    fp.locked = True
                elif token == "dnp":
                    fp.dnp = True
                else:
                    unknown_tokens.append(token)
            if unknown_tokens:
                fp._attr_unknown_tokens = unknown_tokens

        # Reference and value from fp_text (KiCad 7 format)
        for fp_text_sexp in sexp.find_all("fp_text"):
            fp_text = FootprintText.from_sexp(fp_text_sexp)
            fp.texts.append(fp_text)
            # Also set reference/value for convenience
            if fp_text.text_type == "reference":
                fp.reference = fp_text.text
            elif fp_text.text_type == "value":
                fp.value = fp_text.text

        # Reference and value from property (KiCad 8+ format)
        for prop in sexp.find_all("property"):
            prop_name = prop.get_string(0)
            prop_value = prop.get_string(1) or ""
            if prop_name == "Reference":
                fp.reference = prop_value
                # Also create FootprintText for validation
                fp_text = FootprintText._from_property_sexp(prop, "reference")
                fp.texts.append(fp_text)
            elif prop_name == "Value":
                fp.value = prop_value
                # Also create FootprintText for validation
                fp_text = FootprintText._from_property_sexp(prop, "value")
                fp.texts.append(fp_text)
            elif prop_name not in ("Reference", "Value", "Footprint"):
                # Store additional properties (LCSC, MPN, Manufacturer, etc.)
                fp.properties[prop_name] = prop_value

        # Pads
        for pad_sexp in sexp.find_all("pad"):
            pad = Pad.from_sexp(pad_sexp)
            if pad:
                fp.pads.append(pad)

        # Graphics (fp_line, fp_rect, fp_circle, fp_arc)
        for graphic_type in ("line", "rect", "circle", "arc"):
            for graphic_sexp in sexp.find_all(f"fp_{graphic_type}"):
                graphic = FootprintGraphic.from_sexp(graphic_sexp, graphic_type)
                fp.graphics.append(graphic)

        return fp


@dataclass
class Segment:
    """PCB trace segment."""

    start: tuple[float, float]
    end: tuple[float, float]
    width: float
    layer: str
    net_number: int
    net_name: str = ""
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> Segment:
        """Parse segment from S-expression."""
        seg = cls(
            start=(0.0, 0.0),
            end=(0.0, 0.0),
            width=0.0,
            layer="",
            net_number=0,
        )

        if start := sexp.find("start"):
            seg.start = (start.get_float(0) or 0.0, start.get_float(1) or 0.0)
        if end := sexp.find("end"):
            seg.end = (end.get_float(0) or 0.0, end.get_float(1) or 0.0)
        if width := sexp.find("width"):
            seg.width = width.get_float(0) or 0.0
        if layer := sexp.find("layer"):
            seg.layer = layer.get_string(0) or ""
        # Net — handles both (net N "name") and (net "name") formats.
        # KiCad 10 may emit (net "name") without a numeric net number.
        if net := sexp.find("net"):
            first_int = net.get_int(0)
            if first_int is not None:
                # Traditional format: (net N "name") or (net N)
                seg.net_number = first_int
                seg.net_name = net.get_string(1) or ""
            else:
                # KiCad 10 name-only format: (net "name")
                seg.net_number = 0
                seg.net_name = net.get_string(0) or ""
        if uuid := sexp.find("uuid"):
            seg.uuid = uuid.get_string(0) or ""

        return seg

    def to_sexp(self) -> SExp:
        """Convert segment to S-expression for serialization."""
        seg_sexp = SExp.list("segment")
        seg_sexp.append(SExp.list("start", self.start[0], self.start[1]))
        seg_sexp.append(SExp.list("end", self.end[0], self.end[1]))
        seg_sexp.append(SExp.list("width", self.width))
        seg_sexp.append(SExp.list("layer", self.layer))
        seg_sexp.append(SExp.list("net", self.net_number))
        if not self.uuid:
            self.uuid = str(uuid.uuid4())
        seg_sexp.append(SExp.list("uuid", self.uuid))
        return seg_sexp


@dataclass
class Via:
    """PCB via."""

    position: tuple[float, float]
    size: float
    drill: float
    layers: list[str]
    net_number: int
    net_name: str = ""
    uuid: str = ""
    # Issue #3124 (folds in #3118 prerequisite): preserve the optional
    # leading via-type token ``(via micro ...)`` / ``(via blind ...)`` /
    # ``(via buried ...)`` through parse + emit so micro vias added by
    # the router (or read from an upstream PCB) survive serialization.
    # ``None`` => standard through-hole via.  The serializer mirrors
    # :func:`kicad_tools.sexp.builders.via_node` exactly.
    via_type: str | None = None

    @classmethod
    def from_sexp(cls, sexp: SExp) -> Via:
        """Parse via from S-expression."""
        via = cls(
            position=(0.0, 0.0),
            size=0.0,
            drill=0.0,
            layers=[],
            net_number=0,
        )

        # Issue #3124: detect the optional leading via-type token.  KiCad
        # emits ``(via micro ...)`` / ``(via blind ...)`` / ``(via buried
        # ...)`` with the type as a bare atom immediately after ``via``.
        # We only preserve the token (no semantic handling for blind/
        # buried -- that's out of scope for this issue and #3118).
        # ``sexp.values[0]`` is a string atom for these forms; for a
        # standard through-hole via the first child is the ``(at ...)``
        # list, which appears as an SExp in ``values``.
        if sexp.values and isinstance(sexp.values[0], str):
            token = sexp.values[0]
            if token in ("micro", "blind", "buried"):
                via.via_type = token

        if at := sexp.find("at"):
            via.position = (at.get_float(0) or 0.0, at.get_float(1) or 0.0)
        if size := sexp.find("size"):
            via.size = size.get_float(0) or 0.0
        if drill := sexp.find("drill"):
            via.drill = drill.get_float(0) or 0.0
        if layers := sexp.find("layers"):
            via.layers = [
                layers.get_string(i) or ""
                for i in range(len(layers.values))
                if isinstance(layers.values[i], str)
            ]
        # Net — handles both (net N "name") and (net "name") formats.
        # KiCad 10 may emit (net "name") without a numeric net number.
        if net := sexp.find("net"):
            first_int = net.get_int(0)
            if first_int is not None:
                # Traditional format: (net N "name") or (net N)
                via.net_number = first_int
                via.net_name = net.get_string(1) or ""
            else:
                # KiCad 10 name-only format: (net "name")
                via.net_number = 0
                via.net_name = net.get_string(0) or ""
        if uuid := sexp.find("uuid"):
            via.uuid = uuid.get_string(0) or ""

        return via

    def to_sexp(self) -> SExp:
        """Convert via to S-expression for serialization.

        When :attr:`via_type` is set (``"micro"`` / ``"blind"`` /
        ``"buried"``) the token is emitted immediately after ``via`` to
        match KiCad's format and survive a load + save round-trip
        (issue #3124).
        """
        via_sexp = SExp.list("via")
        if self.via_type:
            via_sexp.append(SExp.atom(self.via_type))
        via_sexp.append(SExp.list("at", self.position[0], self.position[1]))
        via_sexp.append(SExp.list("size", self.size))
        via_sexp.append(SExp.list("drill", self.drill))
        # Build layers list
        layers_sexp = SExp.list("layers", *self.layers)
        via_sexp.append(layers_sexp)
        via_sexp.append(SExp.list("net", self.net_number))
        if not self.uuid:
            self.uuid = str(uuid.uuid4())
        via_sexp.append(SExp.list("uuid", self.uuid))
        return via_sexp


@dataclass
class Zone:
    """PCB copper pour zone.

    Represents a copper fill zone with boundary polygon and thermal relief settings.
    Zones are used for ground planes, power planes, and copper pours.
    """

    net_number: int
    net_name: str
    layer: str
    uuid: str = ""
    name: str = ""
    # Boundary polygon points (x, y) in mm
    polygon: list[tuple[float, float]] = field(default_factory=list)
    # Filled polygon regions after DRC (may differ from boundary due to clearances)
    filled_polygons: list[list[tuple[float, float]]] = field(default_factory=list)
    # Layer of each filled polygon, parallel to ``filled_polygons``.
    # KiCad writes ``(filled_polygon (layer "F.Cu") (pts ...))`` per fill;
    # multi-layer zones emit one filled_polygon per layer, so the zone-level
    # ``layer`` field alone cannot locate fill copper.  Entries may be
    # missing/empty for programmatically constructed zones -- use
    # :meth:`filled_polygon_layer`, which falls back to the zone-level
    # ``layer``.  See Issue #3527.
    filled_polygon_layers: list[str] = field(default_factory=list)
    # Zone fill priority (higher priority fills later, on top of lower priority)
    priority: int = 0
    # Minimum copper thickness in mm
    min_thickness: float = 0.2
    # Clearance to pads/traces of other nets in mm
    clearance: float = 0.2
    # Thermal relief gap (antipad) in mm
    thermal_gap: float = 0.3
    # Thermal relief spoke (bridge) width in mm
    thermal_bridge_width: float = 0.3
    # Pad connection type: "thermal_reliefs", "solid", "none"
    connect_pads: str = "thermal_reliefs"
    # Fill type: "solid" or "hatch"
    fill_type: str = "solid"
    # Whether zone is filled (has copper)
    is_filled: bool = False

    @classmethod
    def from_sexp(cls, sexp: SExp) -> Zone:
        """Parse zone from S-expression.

        Parses KiCad zone definitions including:
        - Net assignment (net, net_name)
        - Layer and name
        - Boundary polygon points
        - Filled polygon regions (actual copper after DRC)
        - Thermal relief parameters (gap, bridge width)
        - Connection type (thermal, solid, none)
        - Priority and minimum thickness
        """
        zone = cls(
            net_number=0,
            net_name="",
            layer="",
        )

        # Basic properties — handles both (net N "name") and (net "name") formats.
        # KiCad 9 may emit (net "name") without a numeric net number.
        if net := sexp.find("net"):
            first_int = net.get_int(0)
            if first_int is not None:
                zone.net_number = first_int
            else:
                zone.net_number = 0
                # Name-only format: (net "GND") — store name from net node
                zone.net_name = net.get_string(0) or ""
        if net_name := sexp.find("net_name"):
            zone.net_name = net_name.get_string(0) or ""
        if layer := sexp.find("layer"):
            zone.layer = layer.get_string(0) or ""
        if uuid := sexp.find("uuid"):
            zone.uuid = uuid.get_string(0) or ""
        if name := sexp.find("name"):
            zone.name = name.get_string(0) or ""

        # Priority
        if priority := sexp.find("priority"):
            zone.priority = priority.get_int(0) or 0

        # Minimum thickness
        if min_thickness := sexp.find("min_thickness"):
            zone.min_thickness = min_thickness.get_float(0) or 0.2

        # Connect pads - can be (connect_pads yes) or (connect_pads (clearance X))
        # or (connect_pads thru_hole_only (clearance X)) etc.
        if connect_pads := sexp.find("connect_pads"):
            # Check for connection type keyword
            first_val = connect_pads.get_string(0)
            if first_val == "no":
                zone.connect_pads = "none"
            elif first_val == "yes":
                zone.connect_pads = "solid"
            elif first_val == "thru_hole_only":
                zone.connect_pads = "thermal_reliefs"
            else:
                # Default thermal reliefs if just clearance specified
                zone.connect_pads = "thermal_reliefs"

            # Extract clearance if present
            if clearance := connect_pads.find("clearance"):
                zone.clearance = clearance.get_float(0) or 0.2

        # Fill settings - (fill yes/no (thermal_gap X) (thermal_bridge_width X))
        if fill := sexp.find("fill"):
            first_val = fill.get_string(0)
            zone.is_filled = first_val == "yes"

            if thermal_gap := fill.find("thermal_gap"):
                zone.thermal_gap = thermal_gap.get_float(0) or 0.3
            if thermal_bridge := fill.find("thermal_bridge_width"):
                zone.thermal_bridge_width = thermal_bridge.get_float(0) or 0.3
            if mode := fill.find("mode"):
                fill_mode = mode.get_string(0)
                if fill_mode == "hatch":
                    zone.fill_type = "hatch"

        # Parse boundary polygon - (polygon (pts (xy X Y) ...))
        if polygon := sexp.find("polygon"):
            zone.polygon = cls._parse_polygon_pts(polygon)

        # Parse filled polygons - (filled_polygon (layer X) (pts (xy X Y) ...))
        for filled_poly in sexp.find_all("filled_polygon"):
            points = cls._parse_polygon_pts(filled_poly)
            if points:
                zone.filled_polygons.append(points)
                fp_layer = ""
                if fp_layer_node := filled_poly.find("layer"):
                    fp_layer = fp_layer_node.get_string(0) or ""
                zone.filled_polygon_layers.append(fp_layer or zone.layer)

        return zone

    def filled_polygon_layer(self, index: int) -> str:
        """Return the copper layer of ``filled_polygons[index]``.

        Uses the per-fill ``(layer ...)`` value when it was parsed from the
        S-expression and falls back to the zone-level ``layer`` for zones
        constructed programmatically (whose ``filled_polygon_layers`` list
        may be shorter than ``filled_polygons`` or contain empty strings).
        """
        if index < len(self.filled_polygon_layers) and self.filled_polygon_layers[index]:
            return self.filled_polygon_layers[index]
        return self.layer

    @staticmethod
    def _parse_polygon_pts(polygon_sexp: SExp) -> list[tuple[float, float]]:
        """Parse polygon points from (pts (xy X Y) ...) structure."""
        points: list[tuple[float, float]] = []

        if pts := polygon_sexp.find("pts"):
            for xy in pts.find_all("xy"):
                x = xy.get_float(0) or 0.0
                y = xy.get_float(1) or 0.0
                points.append((x, y))

        return points


@dataclass
class GraphicLine:
    """PCB graphic line element (gr_line).

    Used for board outlines on Edge.Cuts layer and other graphic elements.
    """

    start: tuple[float, float]
    end: tuple[float, float]
    layer: str
    width: float = 0.1
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> GraphicLine:
        """Parse graphic line from S-expression."""
        line = cls(
            start=(0.0, 0.0),
            end=(0.0, 0.0),
            layer="",
        )

        if start := sexp.find("start"):
            line.start = (start.get_float(0) or 0.0, start.get_float(1) or 0.0)
        if end := sexp.find("end"):
            line.end = (end.get_float(0) or 0.0, end.get_float(1) or 0.0)
        if layer := sexp.find("layer"):
            line.layer = layer.get_string(0) or ""
        if width := sexp.find("width"):
            line.width = width.get_float(0) or 0.1
        if stroke := sexp.find("stroke"):
            # KiCad 8+ uses stroke instead of width
            if stroke_width := stroke.find("width"):
                line.width = stroke_width.get_float(0) or 0.1
        if uuid := sexp.find("uuid"):
            line.uuid = uuid.get_string(0) or ""

        return line


@dataclass
class GraphicArc:
    """PCB graphic arc element (gr_arc).

    Used for curved board outlines on Edge.Cuts layer.
    """

    start: tuple[float, float]
    mid: tuple[float, float]
    end: tuple[float, float]
    layer: str
    width: float = 0.1
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> GraphicArc:
        """Parse graphic arc from S-expression."""
        arc = cls(
            start=(0.0, 0.0),
            mid=(0.0, 0.0),
            end=(0.0, 0.0),
            layer="",
        )

        if start := sexp.find("start"):
            arc.start = (start.get_float(0) or 0.0, start.get_float(1) or 0.0)
        if mid := sexp.find("mid"):
            arc.mid = (mid.get_float(0) or 0.0, mid.get_float(1) or 0.0)
        if end := sexp.find("end"):
            arc.end = (end.get_float(0) or 0.0, end.get_float(1) or 0.0)
        if layer := sexp.find("layer"):
            arc.layer = layer.get_string(0) or ""
        if width := sexp.find("width"):
            arc.width = width.get_float(0) or 0.1
        if stroke := sexp.find("stroke"):
            # KiCad 8+ uses stroke instead of width
            if stroke_width := stroke.find("width"):
                arc.width = stroke_width.get_float(0) or 0.1
        if uuid := sexp.find("uuid"):
            arc.uuid = uuid.get_string(0) or ""

        return arc


@dataclass
class EdgeContour:
    """A group of connected Edge.Cuts graphic elements forming a contour.

    Each contour is either a closed polygon (board outline) or a small
    shape such as a mounting hole.  The ``sexp_nodes`` list stores the
    raw S-expression nodes so the contour can be removed from the PCB.
    """

    index: int
    element_count: int
    bbox: tuple[float, float, float, float]  # (min_x, min_y, max_x, max_y)
    is_mounting_hole: bool = False
    sexp_nodes: list = field(default_factory=list)  # list[SExp]

    @property
    def bbox_area(self) -> float:
        """Bounding box area in mm^2."""
        return (self.bbox[2] - self.bbox[0]) * (self.bbox[3] - self.bbox[1])

    @property
    def bbox_width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def bbox_height(self) -> float:
        return self.bbox[3] - self.bbox[1]


@dataclass
class StackupLayer:
    """Stackup layer definition."""

    name: str
    type: str  # copper, prepreg, core, solder mask, silk screen
    thickness: float = 0.0
    material: str = ""
    epsilon_r: float = 0.0


@dataclass
class Setup:
    """PCB setup/design rules."""

    stackup: list[StackupLayer] = field(default_factory=list)
    pad_to_mask_clearance: float = 0.0
    copper_finish: str = ""
    aux_axis_origin: tuple[float, float] = (0.0, 0.0)


# Paper sizes in mm (width, height) - KiCad uses landscape orientation
PAPER_SIZES: dict[str, tuple[float, float]] = {
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
    "A": (279.4, 215.9),  # US Letter
    "B": (431.8, 279.4),  # US Tabloid
    "C": (558.8, 431.8),
    "D": (863.6, 558.8),
    "E": (1117.6, 863.6),
}


class PCB:
    """KiCad PCB document.

    Parses .kicad_pcb files and provides access to:
    - Board outline and dimensions
    - Layers and stackup
    - Nets
    - Footprints (components)
    - Traces (segments)
    - Vias
    - Zones (copper pours)
    """

    def __init__(self, sexp: SExp, path: str | Path | None = None):
        """Initialize from parsed S-expression data.

        Args:
            sexp: Parsed S-expression data
            path: Optional path to the PCB file (used for export operations)

        Raises:
            TypeError: If sexp is a string or Path instead of a parsed SExp
        """
        if isinstance(sexp, (str, Path)):
            raise TypeError(
                f"PCB() expects a parsed SExp, not a file path. "
                f"Use PCB.load({str(sexp)!r}) instead."
            )
        self._sexp = sexp
        self._path: Path | None = Path(path) if path else None
        self._layers: dict[int, Layer] = {}
        self._nets: dict[int, Net] = {}
        self._footprints: list[Footprint] = []
        self._segments: list[Segment] = []
        self._vias: list[Via] = []
        self._zones: list[Zone] = []
        self._graphic_lines: list[GraphicLine] = []
        self._graphic_arcs: list[GraphicArc] = []
        self._texts: list[GraphicText] = []
        self._graphics: list[BoardGraphic] = []
        self._setup: Setup | None = None
        self._title_block: dict[str, str] = {}
        self._board_origin: tuple[float, float] = (0.0, 0.0)
        self._parse()
        self._detect_board_origin()
        self._link_footprint_sexp_nodes()

    @classmethod
    def load(cls, path: str | Path) -> PCB:
        """Load PCB from file.

        Args:
            path: Path to .kicad_pcb file

        Returns:
            PCB instance with path stored for export operations
        """
        path = Path(path)
        sexp = load_pcb(str(path))
        return cls(sexp, path)

    @classmethod
    def create(
        cls,
        width: float = 100.0,
        height: float = 100.0,
        layers: int = 2,
        title: str = "",
        revision: str = "1.0",
        company: str = "",
        board_date: str | None = None,
        paper: str = "A4",
        center: bool = True,
    ) -> PCB:
        """Create a new blank PCB from scratch.

        This creates a minimal but valid KiCad PCB file with:
        - Board outline on Edge.Cuts layer (centered on drawing sheet by default)
        - Layer definitions (2 or 4 copper layers)
        - Basic design rules
        - Title block information

        Args:
            width: Board width in mm (default 100.0)
            height: Board height in mm (default 100.0)
            layers: Number of copper layers (2 or 4, default 2)
            title: Board title for title block
            revision: Board revision (default "1.0")
            company: Company name for title block
            board_date: Date string (default: today's date in YYYY-MM-DD format)
            paper: Paper size for drawing sheet (default "A4"). Supported sizes:
                   A4, A3, A2, A1, A0, A (US Letter), B, C, D, E
            center: If True, center the board on the drawing sheet (default True).
                    If False, place the board at origin (0, 0).

        Returns:
            A new PCB instance ready for adding footprints and traces.

        Raises:
            ValueError: If layers is not 2 or 4
            ValueError: If paper size is not recognized

        Example:
            >>> pcb = PCB.create(width=160, height=100, layers=4, title="My Board")
            >>> pcb.save("my_board.kicad_pcb")
        """
        if layers not in (2, 4):
            raise ValueError(f"Layers must be 2 or 4, got {layers}")

        if paper not in PAPER_SIZES:
            raise ValueError(
                f"Unknown paper size '{paper}'. "
                f"Supported sizes: {', '.join(sorted(PAPER_SIZES.keys()))}"
            )

        if board_date is None:
            board_date = date.today().isoformat()

        # Calculate board origin based on centering preference
        if center:
            paper_width, paper_height = PAPER_SIZES[paper]
            origin_x = (paper_width - width) / 2
            origin_y = (paper_height - height) / 2
        else:
            origin_x, origin_y = 0.0, 0.0

        sexp = cls._build_blank_pcb_sexp(
            width=width,
            height=height,
            layers=layers,
            title=title,
            revision=revision,
            company=company,
            board_date=board_date,
            paper=paper,
            origin_x=origin_x,
            origin_y=origin_y,
        )
        return cls(sexp)

    @staticmethod
    def _build_blank_pcb_sexp(
        width: float,
        height: float,
        layers: int,
        title: str,
        revision: str,
        company: str,
        board_date: str,
        paper: str,
        origin_x: float,
        origin_y: float,
    ) -> SExp:
        """Build the S-expression for a blank PCB."""
        pcb = SExp.list("kicad_pcb")

        # Version and generator info
        pcb.append(SExp.list("version", KICAD_BOARD_FORMAT_VERSION))
        pcb.append(SExp.list("generator", "kicad_tools"))
        # generator_version is a strict-typed string field in KiCad; emit the
        # value as a quoted atom so kicad-cli accepts the file even though
        # "10.0" textually parses as a number.
        pcb.append(SExp.list("generator_version", SExp.quoted_atom("10.0")))

        # General settings
        pcb.append(
            SExp.list(
                "general",
                SExp.list("thickness", 1.6),
                SExp.list("legacy_teardrops", "no"),
            )
        )

        # Paper size
        pcb.append(SExp.list("paper", paper))

        # Title block
        pcb.append(
            SExp.list(
                "title_block",
                SExp.list("title", title),
                SExp.list("date", board_date),
                SExp.list("rev", revision),
                SExp.list("company", company),
            )
        )

        # Layers
        pcb.append(PCB._build_layers_sexp(layers))

        # Setup with design rules
        pcb.append(PCB._build_setup_sexp(layers))

        # Empty net (required)
        pcb.append(SExp.list("net", 0, ""))

        # Board outline on Edge.Cuts (four gr_line segments)
        for line in PCB._build_board_outline_sexp(width, height, origin_x, origin_y):
            pcb.append(line)

        return pcb

    @staticmethod
    def _build_layers_sexp(num_layers: int) -> SExp:
        """Build the layers definition S-expression."""
        layers_node = SExp.list("layers")

        # Copper layers
        layers_node.append(SExp.list("0", "F.Cu", "signal"))
        if num_layers == 4:
            layers_node.append(SExp.list("1", "In1.Cu", "signal"))
            layers_node.append(SExp.list("2", "In2.Cu", "signal"))
        layers_node.append(SExp.list("31", "B.Cu", "signal"))

        # Technical layers (always present)
        layers_node.append(SExp.list("32", "B.Adhes", "user", "B.Adhesive"))
        layers_node.append(SExp.list("33", "F.Adhes", "user", "F.Adhesive"))
        layers_node.append(SExp.list("34", "B.Paste", "user"))
        layers_node.append(SExp.list("35", "F.Paste", "user"))
        layers_node.append(SExp.list("36", "B.SilkS", "user", "B.Silkscreen"))
        layers_node.append(SExp.list("37", "F.SilkS", "user", "F.Silkscreen"))
        layers_node.append(SExp.list("38", "B.Mask", "user"))
        layers_node.append(SExp.list("39", "F.Mask", "user"))
        layers_node.append(SExp.list("40", "Dwgs.User", "user", "User.Drawings"))
        layers_node.append(SExp.list("44", "Edge.Cuts", "user"))
        layers_node.append(SExp.list("46", "B.CrtYd", "user", "B.Courtyard"))
        layers_node.append(SExp.list("47", "F.CrtYd", "user", "F.Courtyard"))
        layers_node.append(SExp.list("48", "B.Fab", "user"))
        layers_node.append(SExp.list("49", "F.Fab", "user"))

        return layers_node

    @staticmethod
    def _build_setup_sexp(num_layers: int) -> SExp:
        """Build the setup/design rules S-expression."""
        setup = SExp.list("setup")

        # Stackup for multi-layer boards
        if num_layers == 4:
            stackup = SExp.list("stackup")
            stackup.append(SExp.list("layer", "F.SilkS", SExp.list("type", "Top Silk Screen")))
            stackup.append(SExp.list("layer", "F.Paste", SExp.list("type", "Top Solder Paste")))
            stackup.append(
                SExp.list(
                    "layer",
                    "F.Mask",
                    SExp.list("type", "Top Solder Mask"),
                    SExp.list("thickness", 0.01),
                )
            )
            stackup.append(
                SExp.list(
                    "layer", "F.Cu", SExp.list("type", "copper"), SExp.list("thickness", 0.035)
                )
            )
            stackup.append(
                SExp.list(
                    "layer",
                    "dielectric 1",
                    SExp.list("type", "prepreg"),
                    SExp.list("thickness", 0.2),
                    SExp.list("material", "FR4"),
                    SExp.list("epsilon_r", 4.5),
                    SExp.list("loss_tangent", 0.02),
                )
            )
            stackup.append(
                SExp.list(
                    "layer", "In1.Cu", SExp.list("type", "copper"), SExp.list("thickness", 0.035)
                )
            )
            stackup.append(
                SExp.list(
                    "layer",
                    "dielectric 2",
                    SExp.list("type", "core"),
                    SExp.list("thickness", 1.0),
                    SExp.list("material", "FR4"),
                    SExp.list("epsilon_r", 4.5),
                    SExp.list("loss_tangent", 0.02),
                )
            )
            stackup.append(
                SExp.list(
                    "layer", "In2.Cu", SExp.list("type", "copper"), SExp.list("thickness", 0.035)
                )
            )
            stackup.append(
                SExp.list(
                    "layer",
                    "dielectric 3",
                    SExp.list("type", "prepreg"),
                    SExp.list("thickness", 0.2),
                    SExp.list("material", "FR4"),
                    SExp.list("epsilon_r", 4.5),
                    SExp.list("loss_tangent", 0.02),
                )
            )
            stackup.append(
                SExp.list(
                    "layer", "B.Cu", SExp.list("type", "copper"), SExp.list("thickness", 0.035)
                )
            )
            stackup.append(
                SExp.list(
                    "layer",
                    "B.Mask",
                    SExp.list("type", "Bottom Solder Mask"),
                    SExp.list("thickness", 0.01),
                )
            )
            stackup.append(SExp.list("layer", "B.Paste", SExp.list("type", "Bottom Solder Paste")))
            stackup.append(SExp.list("layer", "B.SilkS", SExp.list("type", "Bottom Silk Screen")))
            stackup.append(SExp.list("copper_finish", "ENIG"))
            stackup.append(SExp.list("dielectric_constraints", "no"))
            setup.append(stackup)

        # Basic design rules
        setup.append(SExp.list("pad_to_mask_clearance", 0))

        return setup

    @staticmethod
    def _build_board_outline_sexp(
        width: float, height: float, origin_x: float, origin_y: float
    ) -> list[SExp]:
        """Build a rectangular board outline on Edge.Cuts layer.

        Emits four ``gr_line`` segments forming a closed rectangle rather than
        a single ``gr_rect``.  Downstream placement/route tooling (and some of
        this package's own DSN/routing paths) expect ``gr_line`` boundary
        segments, and the PCB parser already ingests ``gr_line`` into
        ``_graphic_lines`` (used by both ``board_size`` and board-origin
        detection via their gr_line fallbacks).

        Corners are walked clockwise:
        ``(ox,oy) -> (ox+w,oy) -> (ox+w,oy+h) -> (ox,oy+h) -> (ox,oy)``.

        Returns:
            A list of four ``gr_line`` S-expressions.
        """
        ox, oy = origin_x, origin_y
        corners = [
            (ox, oy),
            (ox + width, oy),
            (ox + width, oy + height),
            (ox, oy + height),
        ]

        lines: list[SExp] = []
        for i in range(4):
            start = corners[i]
            end = corners[(i + 1) % 4]
            lines.append(
                SExp.list(
                    "gr_line",
                    SExp.list("start", start[0], start[1]),
                    SExp.list("end", end[0], end[1]),
                    SExp.list("stroke", SExp.list("width", 0.1), SExp.list("type", "default")),
                    SExp.list("layer", "Edge.Cuts"),
                    SExp.list("uuid", str(uuid.uuid4())),
                )
            )
        return lines

    def _parse(self):
        """Parse the PCB data structure."""
        for child in self._sexp.iter_children():
            tag = child.tag
            if tag is None:
                continue  # Skip unnamed list nodes

            if tag == "layers":
                self._parse_layers(child)
            elif tag == "net":
                self._parse_net(child)
            elif tag == "footprint":
                fp = Footprint.from_sexp(child)
                self._footprints.append(fp)
            elif tag == "segment":
                seg = Segment.from_sexp(child)
                self._segments.append(seg)
            elif tag == "via":
                via = Via.from_sexp(child)
                self._vias.append(via)
            elif tag == "zone":
                zone = Zone.from_sexp(child)
                self._zones.append(zone)
            elif tag == "gr_line":
                line = GraphicLine.from_sexp(child)
                self._graphic_lines.append(line)
                # Also surface as a BoardGraphic so the public read path
                # (``graphics`` / ``graphics_on_layer``) sees gr_line outlines
                # (e.g. the four-segment Edge.Cuts boundary emitted by
                # ``PCB.create``).  Without this, gr_line boards would be
                # invisible to ``graphics_on_layer``.
                self._graphics.append(BoardGraphic.from_sexp(child, "line"))
            elif tag == "gr_arc":
                arc = GraphicArc.from_sexp(child)
                self._graphic_arcs.append(arc)
                self._graphics.append(BoardGraphic.from_sexp(child, "arc"))
            elif tag == "setup":
                self._parse_setup(child)
            elif tag == "title_block":
                self._parse_title_block(child)
            elif tag == "gr_text":
                text = GraphicText.from_sexp(child)
                self._texts.append(text)
            elif tag in ("gr_rect", "gr_circle"):
                graphic_type = tag[3:]  # Remove "gr_" prefix
                graphic = BoardGraphic.from_sexp(child, graphic_type)
                self._graphics.append(graphic)

        # Post-parse fixup: recover net_number from name for KiCad 10 name-only
        # format.  KiCad 10 may emit inline (net "name") without a numeric ID,
        # but the header declarations (net N "name") are always present, so we
        # can rebuild the number from the name.
        self._fixup_net_numbers()

    def _parse_layers(self, sexp: SExp):
        """Parse layer definitions."""
        for child in sexp.iter_children():
            # Layers are stored as (N "name" type)
            if len(child.values) >= 1:
                # The tag is the layer number as string
                try:
                    number = int(child.tag)
                except ValueError:
                    continue
                name = child.get_string(0) or ""
                layer_type = child.get_string(1) or "user"
                self._layers[number] = Layer(number, name, layer_type)

    def _parse_net(self, sexp: SExp):
        """Parse net definition."""
        net_num = sexp.get_int(0) or 0
        net_name = sexp.get_string(1) or ""
        self._nets[net_num] = Net(net_num, net_name)

    def _fixup_net_numbers(self) -> None:
        """Reconcile net_number and net_name using PCB header declarations.

        Handles three cases:

        1. **KiCad 10 name-only format** -- inline references are
           ``(net "name")`` without a numeric ID.  We resolve ``net_number``
           from ``net_name`` using the header's ``(net N "name")`` map.

        2. **Traditional number-only format** -- inline references are
           ``(net N)`` without an inline name string.  We resolve
           ``net_name`` from ``net_number`` using the same header map.

        3. **KiCad 10 ``--save-board`` format (no header table)** -- KiCad
           10.0.4's ``kicad-cli pcb drc --refill-zones --save-board`` deletes
           the entire top-level ``(net N "name")`` table *and* rewrites every
           inline ref to name-only ``(net "name")`` form.  With no header to
           recover numbers from, we synthesize the table from the inline
           names (see :meth:`_synthesize_net_table`) before running the
           recovery loop, otherwise ``self._nets`` stays empty and every
           element silently collapses to ``net_number=0``.
        """
        # KiCad 10 --save-board: no header table survived. Synthesize one from
        # the inline name-only references so the recovery loop below has a map.
        if not self._nets:
            self._synthesize_net_table()

        # Build bidirectional lookups from the header declarations
        name_to_number: dict[str, int] = {}
        for net in self._nets.values():
            if net.name and net.number != 0:
                name_to_number[net.name] = net.number

        if not name_to_number and not self._nets:
            return

        # Fix pads
        for fp in self._footprints:
            for pad in fp.pads:
                if pad.net_number == 0 and pad.net_name:
                    pad.net_number = name_to_number.get(pad.net_name, 0)
                elif pad.net_number != 0 and not pad.net_name:
                    net = self._nets.get(pad.net_number)
                    if net:
                        pad.net_name = net.name

        # Fix segments
        for seg in self._segments:
            if seg.net_number == 0 and seg.net_name:
                seg.net_number = name_to_number.get(seg.net_name, 0)
            elif seg.net_number != 0 and not seg.net_name:
                net = self._nets.get(seg.net_number)
                if net:
                    seg.net_name = net.name

        # Fix vias
        for via in self._vias:
            if via.net_number == 0 and via.net_name:
                via.net_number = name_to_number.get(via.net_name, 0)
            elif via.net_number != 0 and not via.net_name:
                net = self._nets.get(via.net_number)
                if net:
                    via.net_name = net.name

        # Fix zones
        for zone in self._zones:
            if zone.net_number == 0 and zone.net_name:
                zone.net_number = name_to_number.get(zone.net_name, 0)
            elif zone.net_number != 0 and not zone.net_name:
                net = self._nets.get(zone.net_number)
                if net:
                    zone.net_name = net.name

    def _synthesize_net_table(self) -> None:
        """Rebuild ``self._nets`` from inline name-only references.

        KiCad 10.0.4's ``kicad-cli pcb drc --refill-zones --save-board``
        deletes the top-level ``(net N "name")`` header table entirely and
        rewrites every inline reference to name-only ``(net "name")`` form.
        Without the header, :meth:`_parse_net` never populates
        ``self._nets``, so the fixup loop in :meth:`_fixup_net_numbers` has
        no map and every element silently keeps ``net_number=0`` while its
        name is (correctly) preserved -- a false-clean connectivity model.

        This method reconstructs the table from the inline names:

        * Net 0 is reserved for the "no net" sentinel (empty name), matching
          KiCad's own convention.
        * Any *surviving* numeric reference (e.g. an inline ``(net 3 "GND")``
          that escaped the name-only rewrite) is honored, so its name keeps
          its original number.
        * Remaining named nets are assigned numbers deterministically, in
          first-seen order across pads -> segments -> vias -> zones, filling
          the lowest unused positive integers.  This is stable across loads
          of the same file.

        The synthesized ``(net N "name")`` declarations are also written back
        into ``self._sexp`` (after the ``layers`` node, where KiCad keeps the
        net table) so that ``PCB.save()`` round-trips a canonical file KiCad
        can re-open.

        No-op if a header table already exists or if there are no named
        inline references to recover from.
        """
        if self._nets:
            return

        # Iterate elements in a stable order and collect (name, surviving_num).
        name_to_number: dict[str, int] = {"": 0}
        reserved: set[int] = {0}
        ordered_names: list[str] = []

        def observe(name: str, number: int) -> None:
            if not name:
                return
            if name not in name_to_number:
                ordered_names.append(name)
                name_to_number[name] = 0  # placeholder, resolved below
            # Honor a surviving numeric ref (nonzero) if we have not already
            # locked a nonzero number for this name.
            if number and name_to_number[name] == 0:
                name_to_number[name] = number
                reserved.add(number)

        for fp in self._footprints:
            for pad in fp.pads:
                observe(pad.net_name, pad.net_number)
        for seg in self._segments:
            observe(seg.net_name, seg.net_number)
        for via in self._vias:
            observe(via.net_name, via.net_number)
        for zone in self._zones:
            observe(zone.net_name, zone.net_number)

        if not ordered_names:
            return  # nothing to synthesize (empty board / no named nets)

        # Assign the lowest unused positive integers to names that had no
        # surviving numeric ref, in first-seen order.
        next_number = 1
        for name in ordered_names:
            if name_to_number[name] != 0:
                continue  # already fixed by a surviving numeric ref
            while next_number in reserved:
                next_number += 1
            name_to_number[name] = next_number
            reserved.add(next_number)

        # Populate self._nets (including the net 0 "" sentinel) ...
        self._nets[0] = Net(0, "")
        for name in ordered_names:
            number = name_to_number[name]
            self._nets[number] = Net(number, name)

        # ... and write the canonical header table back into self._sexp so
        # PCB.save() emits a file KiCad can re-open.
        self._write_net_declarations()

    def _write_net_declarations(self) -> None:
        """Insert ``(net N "name")`` header declarations into ``self._sexp``.

        Writes one node per entry in ``self._nets`` (sorted by number),
        placed immediately after the ``layers`` node -- the position KiCad
        uses for the net table.  Falls back to appending at the end of the
        top-level children if there is no ``layers`` node.
        """
        nodes = [SExp.list("net", number, self._nets[number].name) for number in sorted(self._nets)]
        if not nodes:
            return

        # Find the layers node to anchor the insertion; KiCad orders the net
        # table right after it (following setup, but before footprints).
        insert_index: int | None = None
        for i, child in enumerate(self._sexp.children):
            if child.name in ("layers", "setup"):
                insert_index = i + 1
        if insert_index is None:
            insert_index = len(self._sexp.children)

        for offset, node in enumerate(nodes):
            self._sexp.insert(insert_index + offset, node)

    def _parse_setup(self, sexp: SExp):
        """Parse setup/design rules."""
        setup = Setup()

        if stackup := sexp.find("stackup"):
            setup.stackup = self._parse_stackup(stackup)

        if clearance := sexp.find("pad_to_mask_clearance"):
            setup.pad_to_mask_clearance = clearance.get_float(0) or 0.0

        if aux_origin := sexp.find("aux_axis_origin"):
            x = aux_origin.get_float(0) or 0.0
            y = aux_origin.get_float(1) or 0.0
            setup.aux_axis_origin = (x, y)

        self._setup = setup

    def _parse_stackup(self, sexp: SExp) -> list[StackupLayer]:
        """Parse stackup definition."""
        layers = []

        for child in sexp.iter_children():
            if child.tag == "layer":
                layer = StackupLayer(
                    name=child.get_string(0) or "",
                    type="",
                )

                if type_node := child.find("type"):
                    layer.type = type_node.get_string(0) or ""
                if thick := child.find("thickness"):
                    layer.thickness = thick.get_float(0) or 0.0
                if mat := child.find("material"):
                    layer.material = mat.get_string(0) or ""
                if eps := child.find("epsilon_r"):
                    layer.epsilon_r = eps.get_float(0) or 0.0

                layers.append(layer)
            elif child.tag == "copper_finish":
                pass  # Store globally if needed

        return layers

    def _parse_title_block(self, sexp: SExp):
        """Parse title block."""
        for child in sexp.iter_children():
            value = child.get_string(0) or ""
            self._title_block[child.tag] = value

    def _detect_board_origin(self) -> None:
        """Detect board origin from Edge.Cuts graphics.

        For PCBs created with PCB.create(center=True), the board outline
        (gr_rect on Edge.Cuts) starts at an offset from (0, 0). This method
        detects that offset so footprint positions can be specified relative
        to the board corner.

        Sets self._board_origin to the start position of the first gr_rect
        found on Edge.Cuts, or (0, 0) if none found.

        Coordinate-space invariant
        --------------------------
        After ``_detect_board_origin()`` runs:

        * ``Footprint.position`` (and pad-derived coordinates such as those
          returned by :meth:`get_pad_position`) are in **board-relative**
          coordinates -- i.e. relative to ``self._board_origin``.
        * ``Segment.start`` / ``Segment.end``, ``Via.position``, and the
          ``Zone.polygon`` / ``Zone.filled_polygons`` vertex lists are also
          in **board-relative** coordinates after this method runs.

        Storing every consumer-visible coordinate in the same (board-relative)
        space means analyzers such as :class:`NetStatusAnalyzer` and
        :mod:`kicad_tools.validate.connectivity` can compare pad positions
        directly against segment endpoints and zone polygons without
        coordinate-space conversion -- fixing the off-by-``board_origin``
        bug that previously caused every signal net on a centered board to
        be reported as ``incomplete``.

        The underlying S-expression tree (``self._sexp``) is left in
        sheet-absolute coordinates as required by the KiCad file format.
        :meth:`add_trace`, :meth:`add_via`, and :meth:`save` are responsible
        for adding ``self._board_origin`` back when writing new copper
        primitives to the tree.
        """
        origin = (0.0, 0.0)

        # Look for gr_rect on Edge.Cuts layer - this is how PCB.create() makes outlines
        for graphic in self._graphics:
            if graphic.layer == "Edge.Cuts" and graphic.graphic_type == "rect":
                origin = graphic.start
                break
        else:
            # Fallback: check for gr_line forming a rectangle on Edge.Cuts
            # Find the minimum x, y coordinates from all Edge.Cuts lines
            edge_lines = [line for line in self._graphic_lines if line.layer == "Edge.Cuts"]
            if edge_lines:
                min_x = min(min(line.start[0], line.end[0]) for line in edge_lines)
                min_y = min(min(line.start[1], line.end[1]) for line in edge_lines)
                origin = (min_x, min_y)

        self._board_origin = origin

        # Convert all copper primitives from sheet-absolute to board-relative
        # coordinates so consumers (analyzers, validators, optimizers) see a
        # consistent coordinate space.  The S-expression tree retains the
        # original sheet-absolute values; new primitives appended through
        # add_trace()/add_via() add the origin back when writing to the tree.
        if origin != (0.0, 0.0):
            ox, oy = origin
            # Footprints: existing behavior (preserved for backward compat).
            for fp in self._footprints:
                abs_x, abs_y = fp.position
                fp.position = (abs_x - ox, abs_y - oy)

            # Segments: convert both endpoints.
            for seg in self._segments:
                sx, sy = seg.start
                ex, ey = seg.end
                seg.start = (sx - ox, sy - oy)
                seg.end = (ex - ox, ey - oy)

            # Vias: convert position.
            for via in self._vias:
                vx, vy = via.position
                via.position = (vx - ox, vy - oy)

            # Zones: convert boundary polygon AND every filled polygon.
            for zone in self._zones:
                if zone.polygon:
                    zone.polygon = [(x - ox, y - oy) for x, y in zone.polygon]
                if zone.filled_polygons:
                    zone.filled_polygons = [
                        [(x - ox, y - oy) for x, y in poly] for poly in zone.filled_polygons
                    ]

    def _link_footprint_sexp_nodes(self) -> None:
        """Attach S-expression back-references to parsed Footprint objects.

        Called after ``_detect_board_origin()`` so that the ``__setattr__``
        sync is only active once positions have been converted to
        board-relative coordinates and the board origin is known.

        Each ``Footprint`` receives:
        * ``_sexp_node`` -- the S-expression ``(footprint ...)`` node from
          the tree that :meth:`save` serialises.
        * ``_board_origin`` -- the board origin offset so that the setter
          can convert board-relative positions back to sheet-absolute
          when writing to the S-expression.
        """
        # Build a UUID -> index lookup for fast matching.
        fp_by_uuid: dict[str, int] = {}
        for idx, fp in enumerate(self._footprints):
            if fp.uuid:
                fp_by_uuid[fp.uuid] = idx

        # Walk the top-level S-expression children and match footprint
        # nodes to parsed Footprint objects by UUID (preferred) or by
        # iteration order (fallback for legacy files without UUIDs).
        order_idx = 0
        for child in self._sexp.iter_children():
            if child.tag != "footprint":
                continue

            # Direct child only: pads/properties carry their own (uuid)
            # nodes, and a recursive find() would match a descendant's
            # uuid when the footprint-level one is absent (issue #3602).
            uuid_node = child.find_child("uuid")
            child_uuid = uuid_node.get_string(0) if uuid_node else None

            fp_idx: int | None = None
            if child_uuid and child_uuid in fp_by_uuid:
                fp_idx = fp_by_uuid[child_uuid]
            elif order_idx < len(self._footprints):
                fp_idx = order_idx

            if fp_idx is not None:
                fp = self._footprints[fp_idx]
                # Use object.__setattr__ to avoid triggering sync before
                # both _sexp_node and _board_origin are in place.
                object.__setattr__(fp, "_board_origin", self._board_origin)
                object.__setattr__(fp, "_sexp_node", child)

                # Migrate the legacy KiCad-6 in-attr 'locked' token to
                # the modern top-level ``(locked yes)`` form. KiCad 10's
                # kicad-cli rejects the legacy form ("Failed to load
                # board"), so a load->save round-trip must not echo the
                # original token back via raw-sexp passthrough (issue
                # #3457). ``fp.locked`` was already set by from_sexp;
                # rebuilding the attr block from Python state drops the
                # in-attr token and emits the top-level node.
                attr_nodes = child.find_children("attr")
                if attr_nodes and any(
                    atom.value == "locked" for atom in attr_nodes[0].children if atom.is_atom
                ):
                    fp._sync_attr_node()

            order_idx += 1

    # Public accessors

    @property
    def title(self) -> str:
        """Board title."""
        return self._title_block.get("title", "")

    @property
    def revision(self) -> str:
        """Board revision."""
        return self._title_block.get("rev", "")

    @property
    def date(self) -> str:
        """Board date."""
        return self._title_block.get("date", "")

    @property
    def board_origin(self) -> tuple[float, float]:
        """Board origin offset from drawing sheet origin.

        When a PCB is created with center=True (the default), the board outline
        is placed at an offset from (0, 0) to center it on the drawing sheet.
        This property returns that offset.

        Footprint positions specified via update_footprint_position() and
        add_footprint() are relative to this board origin, so users can specify
        positions relative to the board corner rather than the sheet origin.

        Returns:
            Tuple (x, y) of the board origin in mm.
            For a centered board, this is typically ((paper_width - board_width) / 2,
            (paper_height - board_height) / 2).
            For a non-centered board or loaded PCB without detectable origin, (0, 0).
        """
        return self._board_origin

    @property
    def board_size(self) -> tuple[float, float]:
        """Board dimensions (width, height) in mm.

        Computes the board size from the Edge.Cuts outline.  For a gr_rect
        outline (as created by ``PCB.create()``), the size is derived from
        the rectangle's start and end coordinates.  For outlines composed
        of gr_line segments, the bounding box of all Edge.Cuts geometry is
        used.

        Returns:
            Tuple (width, height) in mm.  Returns (0.0, 0.0) if no
            Edge.Cuts geometry is found.
        """
        # Try gr_rect first (standard board outline from PCB.create)
        for graphic in self._graphics:
            if graphic.layer == "Edge.Cuts" and graphic.graphic_type == "rect":
                width = abs(graphic.end[0] - graphic.start[0])
                height = abs(graphic.end[1] - graphic.start[1])
                return (width, height)

        # Fallback: bounding box of all Edge.Cuts line segments
        edge_lines = [line for line in self._graphic_lines if line.layer == "Edge.Cuts"]
        if edge_lines:
            xs = [coord for line in edge_lines for coord in (line.start[0], line.end[0])]
            ys = [coord for line in edge_lines for coord in (line.start[1], line.end[1])]
            return (max(xs) - min(xs), max(ys) - min(ys))

        return (0.0, 0.0)

    def _edge_cuts_bbox_sexp(self) -> tuple[float, float, float, float] | None:
        """Compute the Edge.Cuts bounding box in sheet-absolute coordinates.

        Walks ``self._sexp`` directly (the source of truth that ``save()``
        serialises) rather than the in-memory, board-relative collections,
        so the returned box is in the same coordinate space as the values
        :meth:`page_fit` rewrites.

        Returns:
            ``(min_x, min_y, max_x, max_y)`` of all graphics on the
            ``Edge.Cuts`` layer, or ``None`` if no Edge.Cuts geometry is
            found.
        """
        xs: list[float] = []
        ys: list[float] = []

        def _on_edge_cuts(node: SExp) -> bool:
            layer_node = node.find_child("layer")
            return bool(layer_node and layer_node.get_string(0) == "Edge.Cuts")

        for child in self._sexp.iter_children():
            if child.tag not in (
                "gr_rect",
                "gr_line",
                "gr_arc",
                "gr_circle",
                "gr_poly",
                "gr_curve",
            ):
                continue
            if not _on_edge_cuts(child):
                continue
            for coord_tag in ("start", "end", "mid", "center"):
                for n in child.find_children(coord_tag):
                    x, y = n.get_float(0), n.get_float(1)
                    if x is not None and y is not None:
                        xs.append(x)
                        ys.append(y)
            # gr_poly / gr_curve carry a (pts (xy ...)) child.
            for pts in child.find_all("pts"):
                for xy in pts.find_children("xy"):
                    x, y = xy.get_float(0), xy.get_float(1)
                    if x is not None and y is not None:
                        xs.append(x)
                        ys.append(y)

        if not xs or not ys:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def _edge_cuts_poly_chains_sexp(self) -> list[list[tuple[float, float]]]:
        """Collect ``gr_poly``/``gr_curve`` Edge.Cuts vertex chains.

        ``gr_poly`` and ``gr_curve`` graphics are not parsed into the in-memory
        ``_graphic_lines``/``_graphic_arcs``/``_graphics`` collections, so this
        walks ``self._sexp`` directly (like :meth:`_edge_cuts_bbox_sexp`) and
        returns each polygon/curve's ordered ``(pts (xy ...))`` vertex list in
        **sheet-absolute** coordinates.

        Returns:
            A list of vertex chains; each chain is an ordered list of
            ``(x, y)`` tuples.  Empty if no ``gr_poly``/``gr_curve`` Edge.Cuts
            geometry is present.
        """

        def _on_edge_cuts(node: SExp) -> bool:
            layer_node = node.find_child("layer")
            return bool(layer_node and layer_node.get_string(0) == "Edge.Cuts")

        chains: list[list[tuple[float, float]]] = []
        for child in self._sexp.iter_children():
            if child.tag not in ("gr_poly", "gr_curve"):
                continue
            if not _on_edge_cuts(child):
                continue
            chain: list[tuple[float, float]] = []
            for pts in child.find_all("pts"):
                for xy in pts.find_children("xy"):
                    x, y = xy.get_float(0), xy.get_float(1)
                    if x is not None and y is not None:
                        chain.append((x, y))
            if chain:
                chains.append(chain)
        return chains

    @staticmethod
    def _translate_coord_node(node: SExp, dx_nm: int, dy_nm: int) -> None:
        """Translate the first two atoms (X, Y) of a coordinate node in place.

        Used for ``(at X Y ...)``, ``(start X Y)``, ``(end X Y)``,
        ``(mid X Y)``, ``(center X Y)`` and ``(xy X Y)`` nodes.  Any trailing
        atoms (e.g. a rotation angle on ``(at X Y ANGLE)``) are left
        untouched.

        The translation delta is supplied in **integer nanometres** (KiCad's
        native storage grid: mm to 6 decimals = 1 nm).  The delta is added to
        each base coordinate as an exact-on-grid mm offset (``delta_nm / 1e6``)
        **without re-snapping the base coordinate first**, so every point in
        the board is shifted by the *identical* delta.  This makes the
        transform a true rigid translation: it is both distance- AND
        angle-preserving (a 45-degree segment stays exactly 45 degrees).

        Re-snapping each base coordinate independently (``round(x * 1e6)``)
        would shift the two endpoints of a segment by slightly different
        amounts when they carry differing sub-nanometre fractions, tilting
        otherwise-exact 45-degree copper off-angle.  Off-45 copper is
        non-manufacturable, so we must NOT do that here.

        The shifted value is written as a 6-decimal **string** (trailing
        zeros stripped) rather than a Python ``float``: the SExp serializer
        formats bare floats with ``%.6g`` (6 *significant* digits), which
        silently truncates a coordinate like ``147.9252`` to ``147.925`` --
        moving one endpoint of a 45-degree segment and tilting it off-angle.
        Emitting the full 6-decimal text preserves the nm grid exactly, so
        both endpoints shift by the identical delta and the angle is kept.
        """
        x, y = node.get_float(0), node.get_float(1)
        if x is None or y is None:
            return
        node.set_value(0, PCB._format_coord_mm(x + dx_nm / 1_000_000))
        node.set_value(1, PCB._format_coord_mm(y + dy_nm / 1_000_000))

    @staticmethod
    def _format_coord_mm(value: float) -> str:
        """Format an mm coordinate on the KiCad nm grid (6 decimals).

        Rounds to 6 decimal places (1 nm) and strips trailing zeros so the
        text matches KiCad's own output (e.g. ``148`` not ``148.000000`` and
        ``147.9252`` not ``147.925200``).  Returned as a string to bypass the
        SExp serializer's lossy ``%.6g`` float formatting.
        """
        text = f"{round(value, 6):.6f}".rstrip("0").rstrip(".")
        return text if text not in ("", "-0") else "0"

    def _translate_item_sexp(self, node: SExp, dx_nm: int, dy_nm: int) -> None:
        """Recursively translate all coordinate nodes within an item.

        Applies to every ``at`` / ``start`` / ``end`` / ``mid`` / ``center`` /
        ``xy`` descendant of ``node``.  This is correct for board-level items
        (``segment``, ``via``, ``zone`` incl. ``filled_polygon`` vertices,
        ``gr_*`` graphics, ``gr_text`` / ``text`` / ``dimension``) whose
        coordinates are all sheet-absolute.

        NOTE: this MUST NOT be called on a ``footprint`` node, whose internal
        pad/text ``at`` nodes are footprint-relative.  Use
        :meth:`_translate_footprint_sexp` for footprints.
        """
        coord_tags = {"at", "start", "end", "mid", "center", "xy"}
        if node.tag in coord_tags:
            self._translate_coord_node(node, dx_nm, dy_nm)
        for child in node.iter_children():
            self._translate_item_sexp(child, dx_nm, dy_nm)

    def _translate_footprint_sexp(self, node: SExp, dx_nm: int, dy_nm: int) -> None:
        """Translate a footprint by shifting only its top-level ``(at ...)``.

        A footprint's position is its direct-child ``(at X Y [ANGLE])`` node;
        all pad and graphic ``at`` nodes nested inside are relative to that
        anchor and must be left untouched.
        """
        at_node = node.find_child("at")
        if at_node is not None:
            self._translate_coord_node(at_node, dx_nm, dy_nm)

    def page_fit(self, margin: float = 5.0) -> tuple[float, float]:
        """Resize the drawing sheet to fit the board with a uniform margin.

        Computes the Edge.Cuts bounding box, sets ``(paper "User" W H)`` where
        ``W = bbox_width + 2*margin`` and ``H = bbox_height + 2*margin``, then
        translates ALL board items so the board sits at ``(margin, margin)`` in
        the new sheet space -- i.e. centered with a uniform margin all around.

        The board's interactive viewer (KiCanvas) fits its camera to the whole
        drawing sheet, so a tight ``User`` page makes the board fill and center
        the frame instead of appearing tiny in an A4 page.

        This is a pure geometric transform: every item shifts together, so
        relative spacing -- and therefore routing and DRC validity -- is
        preserved.  No re-routing is required.

        The transform operates directly on ``self._sexp`` (the authoritative
        tree that :meth:`save` serialises), then re-runs
        :meth:`_detect_board_origin` so the in-memory board-relative view stays
        consistent.

        Args:
            margin: Margin around the board in mm (default 5.0).

        Returns:
            ``(new_width, new_height)`` of the paper in mm.

        Raises:
            ValueError: If no Edge.Cuts geometry is found to fit the page to.
        """
        bbox = self._edge_cuts_bbox_sexp()
        if bbox is None:
            raise ValueError("page_fit() requires an Edge.Cuts board outline; none found.")
        min_x, min_y, max_x, max_y = bbox
        bbox_w = max_x - min_x
        bbox_h = max_y - min_y

        # Snap the translation delta to the KiCad nm grid (mm to 6 decimals)
        # ONCE here, then add this single on-grid delta to every coordinate
        # (see _translate_coord_node).  Every point shifts by the identical
        # delta, so the transform is a true rigid translation -- both
        # distance- AND angle-preserving (45-degree copper stays 45 degrees).
        dx_nm = round((margin - min_x) * 1_000_000)
        dy_nm = round((margin - min_y) * 1_000_000)

        # Translate every positioned top-level item in the tree.
        for child in self._sexp.iter_children():
            tag = child.tag
            if tag == "footprint":
                self._translate_footprint_sexp(child, dx_nm, dy_nm)
            elif tag in (
                "segment",
                "via",
                "arc",
                "zone",
                "gr_line",
                "gr_arc",
                "gr_rect",
                "gr_circle",
                "gr_poly",
                "gr_curve",
                "gr_text",
                "text",
                "dimension",
                "target",
            ):
                self._translate_item_sexp(child, dx_nm, dy_nm)

        # Rewrite the (paper ...) node to a tight User page.
        new_w = round(bbox_w + 2 * margin, 6)
        new_h = round(bbox_h + 2 * margin, 6)
        paper_node = self._sexp.find_child("paper")
        if paper_node is not None:
            self._sexp.remove(paper_node)
        new_paper = SExp.list("paper", SExp.quoted_atom("User"), new_w, new_h)
        # Keep paper near the front of the document, where KiCad writes it.
        version_node = self._sexp.find_child("version")
        if version_node is not None:
            self._sexp.insert_after("version", new_paper)
        else:
            self._sexp.insert(0, new_paper)

        # Re-derive the in-memory board-relative coordinate view from the
        # mutated tree so analyzers/optimizers stay consistent.  Reset the
        # collections first since _parse() appends rather than replaces.
        self._layers = {}
        self._nets = {}
        self._footprints = []
        self._segments = []
        self._vias = []
        self._zones = []
        self._graphic_lines = []
        self._graphic_arcs = []
        self._texts = []
        self._graphics = []
        self._setup = None
        self._title_block = {}
        self._parse()
        self._board_origin = (0.0, 0.0)
        self._detect_board_origin()
        self._link_footprint_sexp_nodes()

        return (new_w, new_h)

    @property
    def layers(self) -> dict[int, Layer]:
        """Layer definitions."""
        return self._layers

    @property
    def copper_layers(self) -> list[Layer]:
        """Copper layers only."""
        return [layer for layer in self._layers.values() if layer.type in ("signal", "power")]

    @property
    def nets(self) -> dict[int, Net]:
        """Net definitions."""
        return self._nets

    def get_net(self, number: int) -> Net | None:
        """Get net by number."""
        return self._nets.get(number)

    def get_net_by_name(self, name: str) -> Net | None:
        """Get net by name."""
        for net in self._nets.values():
            if net.name == name:
                return net
        return None

    @property
    def footprints(self) -> FootprintList:
        """All footprints.

        Returns a FootprintList which extends list with query methods:
            pcb.footprints.by_reference("U1")
            pcb.footprints.filter(layer="F.Cu")
            pcb.footprints.query().smd().on_top().all()

        Backward compatible - all list operations still work.
        """
        # Import here to avoid circular import
        from ..query.footprints import FootprintList

        return FootprintList(self._footprints)

    def get_footprint(self, reference: str) -> Footprint | None:
        """Get footprint by reference designator."""
        for fp in self._footprints:
            if fp.reference == reference:
                return fp
        return None

    def _find_footprint_sexp(self, reference: str) -> SExp | None:
        """Find a footprint S-expression node by reference designator.

        Searches both KiCad 7 (fp_text) and KiCad 8+ (property) formats.

        Args:
            reference: Footprint reference designator (e.g., "R1", "U1")

        Returns:
            The footprint SExp node if found, None otherwise.
        """
        for fp_sexp in self._sexp.find_all("footprint"):
            ref_value = None

            # KiCad 7 format: fp_text with type "reference"
            for fp_text in fp_sexp.find_all("fp_text"):
                if fp_text.get_string(0) == "reference":
                    ref_value = fp_text.get_string(1)
                    break

            # KiCad 8+ format: property with name "Reference"
            if not ref_value:
                for prop in fp_sexp.find_all("property"):
                    if prop.get_string(0) == "Reference":
                        ref_value = prop.get_string(1)
                        break

            if ref_value == reference:
                return fp_sexp

        return None

    def remove_footprint(self, reference: str) -> bool:
        """Remove a footprint from the PCB by reference designator.

        Removes the footprint from both the S-expression tree (for
        persistence via save()) and the in-memory ``_footprints`` list.

        Args:
            reference: Footprint reference designator (e.g., "R1", "U1")

        Returns:
            True if the footprint was found and removed, False otherwise.

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> pcb.remove_footprint("C1")
            True
            >>> pcb.get_footprint("C1") is None
            True
        """
        # Remove from the S-expression tree
        fp_sexp = self._find_footprint_sexp(reference)
        if fp_sexp is None:
            return False

        self._sexp.remove(fp_sexp)

        # Remove from the in-memory list
        self._footprints = [fp for fp in self._footprints if fp.reference != reference]

        return True

    def footprint_has_traces(self, reference: str) -> bool:
        """Check whether a footprint has any routed traces connected to its pads.

        Examines each pad of the footprint and checks if any trace segment
        in the same net touches the pad position (within 0.01mm tolerance).

        Args:
            reference: Footprint reference designator (e.g., "R1", "C1")

        Returns:
            True if at least one pad has a connected trace segment, False
            if the footprint has no traces or does not exist.
        """
        import math

        fp = self.get_footprint(reference)
        if not fp:
            return False

        for pad in fp.pads:
            if pad.net_number == 0:
                continue
            pos = self.get_pad_position(reference, pad.number)
            if not pos:
                continue
            for seg in self.segments_in_net(pad.net_number):
                tolerance = 0.01  # mm
                start_dist = math.sqrt((seg.start[0] - pos[0]) ** 2 + (seg.start[1] - pos[1]) ** 2)
                end_dist = math.sqrt((seg.end[0] - pos[0]) ** 2 + (seg.end[1] - pos[1]) ** 2)
                if start_dist < tolerance or end_dist < tolerance:
                    return True

        return False

    def remove_segments(self, segments: list[Segment]) -> int:
        """Remove specific trace segments from the PCB.

        Removes matching segments from both the S-expression tree (for
        persistence via save()) and the in-memory ``_segments`` list.
        Segments are matched by UUID when available, falling back to
        matching by start/end coordinates and layer.

        Args:
            segments: List of Segment objects to remove.

        Returns:
            The number of segments actually removed.

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> segs = list(pcb.segments_in_net(5))
            >>> removed = pcb.remove_segments(segs)
            >>> print(f"Removed {removed} segments")
        """
        if not segments:
            return 0

        # Build lookup sets for fast matching
        uuids_to_remove: set[str] = set()
        # Fallback: match by (start, end, layer) for segments without UUIDs.
        # Segment coordinates are board-relative, but the S-expression tree
        # stores them in sheet-absolute form, so offset by board_origin when
        # building the lookup key.
        coords_to_remove: set[tuple[float, float, float, float, str]] = set()
        ox, oy = self._board_origin

        for seg in segments:
            if seg.uuid:
                uuids_to_remove.add(seg.uuid)
            else:
                coords_to_remove.add(
                    (
                        seg.start[0] + ox,
                        seg.start[1] + oy,
                        seg.end[0] + ox,
                        seg.end[1] + oy,
                        seg.layer,
                    )
                )

        # Remove from S-expression tree
        removed_count = 0
        sexp_to_remove = []
        for child in self._sexp.children:
            if child.is_atom or child.name != "segment":
                continue

            # Check UUID match
            uuid_node = child.find("uuid")
            if uuid_node:
                seg_uuid = uuid_node.get_string(0) or ""
                if seg_uuid in uuids_to_remove:
                    sexp_to_remove.append(child)
                    continue

            # Fallback: coordinate match
            start_node = child.find("start")
            end_node = child.find("end")
            layer_node = child.find("layer")
            if start_node and end_node and layer_node:
                key = (
                    start_node.get_float(0) or 0.0,
                    start_node.get_float(1) or 0.0,
                    end_node.get_float(0) or 0.0,
                    end_node.get_float(1) or 0.0,
                    layer_node.get_string(0) or "",
                )
                if key in coords_to_remove:
                    sexp_to_remove.append(child)

        for node in sexp_to_remove:
            self._sexp.remove(node)
            removed_count += 1

        # Remove from in-memory list
        remaining = []
        for seg in self._segments:
            should_remove = False
            if seg.uuid and seg.uuid in uuids_to_remove:
                should_remove = True
            elif not seg.uuid:
                key = (seg.start[0], seg.start[1], seg.end[0], seg.end[1], seg.layer)
                if key in coords_to_remove:
                    should_remove = True
            if not should_remove:
                remaining.append(seg)
        self._segments = remaining

        return removed_count

    def footprints_on_layer(self, layer: str) -> Iterator[Footprint]:
        """Get footprints on a specific layer."""
        for fp in self._footprints:
            if fp.layer == layer:
                yield fp

    @property
    def segments(self) -> list[Segment]:
        """All trace segments."""
        return self._segments

    @segments.setter
    def segments(self, value: list[Segment]) -> None:
        """Prevent direct assignment to segments.

        Raises:
            AttributeError: Always raised to prevent silent data loss.
                Modifying segments directly does not update the S-expression
                tree used by save(), causing changes to be silently discarded.
        """
        raise AttributeError(
            "Cannot modify segments directly. Changes would not persist to save(). "
            "Use add_trace() to add segments, or reload the PCB after modifying "
            "the file with merge_routes_into_pcb()."
        )

    def segments_on_layer(self, layer: str) -> Iterator[Segment]:
        """Get segments on a specific layer."""
        for seg in self._segments:
            if seg.layer == layer:
                yield seg

    def segments_in_net(self, net_number: int) -> Iterator[Segment]:
        """Get segments in a specific net."""
        for seg in self._segments:
            if seg.net_number == net_number:
                yield seg

    @property
    def vias(self) -> list[Via]:
        """All vias."""
        return self._vias

    @vias.setter
    def vias(self, value: list[Via]) -> None:
        """Prevent direct assignment to vias.

        Raises:
            AttributeError: Always raised to prevent silent data loss.
                Modifying vias directly does not update the S-expression
                tree used by save(), causing changes to be silently discarded.
        """
        raise AttributeError(
            "Cannot modify vias directly. Changes would not persist to save(). "
            "Use add_via() to add vias, or reload the PCB after modifying "
            "the file with merge_routes_into_pcb()."
        )

    def vias_in_net(self, net_number: int) -> Iterator[Via]:
        """Get vias in a specific net."""
        for via in self._vias:
            if via.net_number == net_number:
                yield via

    @property
    def zones(self) -> list[Zone]:
        """All zones (copper pours)."""
        return self._zones

    @property
    def graphic_lines(self) -> list[GraphicLine]:
        """All graphic lines."""
        return self._graphic_lines

    @property
    def graphic_arcs(self) -> list[GraphicArc]:
        """All graphic arcs."""
        return self._graphic_arcs

    @property
    def texts(self) -> list[GraphicText]:
        """All board-level text elements (gr_text)."""
        return self._texts

    def texts_on_layer(self, layer: str) -> Iterator[GraphicText]:
        """Get text elements on a specific layer."""
        for text in self._texts:
            if text.layer == layer:
                yield text

    @property
    def graphics(self) -> list[BoardGraphic]:
        """All board-level graphic elements (gr_line, gr_rect, etc.)."""
        return self._graphics

    @property
    def graphic_items(self) -> Iterator[GraphicLine | GraphicArc | BoardGraphic]:
        """All board-level graphic items (lines, arcs, rects, circles).

        Yields all graphic elements from Edge.Cuts and other layers.
        Used for board outline calculations and layer analysis.

        ``gr_line`` and ``gr_arc`` elements are emitted once, as their richer
        typed forms (:class:`GraphicLine` / :class:`GraphicArc`).  They are
        also mirrored into ``_graphics`` as :class:`BoardGraphic` for the
        ``graphics`` / ``graphics_on_layer`` read path, so those line/arc
        mirrors are skipped here to avoid double-counting.
        """
        yield from self._graphic_lines
        yield from self._graphic_arcs
        for graphic in self._graphics:
            if graphic.graphic_type not in ("line", "arc"):
                yield graphic

    def graphics_on_layer(self, layer: str) -> Iterator[BoardGraphic]:
        """Get graphic elements on a specific layer."""
        for graphic in self._graphics:
            if graphic.layer == layer:
                yield graphic

    @staticmethod
    def _rect_to_segments(
        start: tuple[float, float], end: tuple[float, float]
    ) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        """Decompose a rectangle (start, end corners) into 4 line segments."""
        x1, y1 = start
        x2, y2 = end
        return [
            ((x1, y1), (x2, y1)),  # top
            ((x2, y1), (x2, y2)),  # right
            ((x2, y2), (x1, y2)),  # bottom
            ((x1, y2), (x1, y1)),  # left
        ]

    def get_board_outline(self) -> list[tuple[float, float]]:
        """Extract board outline polygon from Edge.Cuts layer.

        Returns an ordered list of (x, y) points forming the board outline.
        Handles ``gr_line``, ``gr_arc``, ``gr_rect``, ``gr_poly``, and
        ``gr_curve`` elements on the Edge.Cuts layer.  Arc segments are
        approximated by their start and end points; polygon/curve outlines
        contribute their vertex chain (``(pts (xy ...))``).

        ``gr_poly``/``gr_curve`` Edge.Cuts geometry is common for rounded
        corners, chamfers, and any outline drawn with KiCad's polygon tool.
        Those elements are not surfaced through the in-memory
        ``_graphic_lines``/``_graphic_arcs``/``_graphics`` collections, so they
        are recovered by walking ``self._sexp`` directly (mirroring
        :meth:`_edge_cuts_bbox_sexp`).  Without this, a board whose outline is
        a single ``gr_poly`` would silently return ``[]``.

        Returns:
            List of (x, y) coordinate tuples in mm. Empty list if no outline found.
        """
        # Collect all Edge.Cuts segments
        edge_lines = [line for line in self._graphic_lines if line.layer == "Edge.Cuts"]
        edge_arcs = [arc for arc in self._graphic_arcs if arc.layer == "Edge.Cuts"]
        edge_rects = [
            g for g in self._graphics if g.layer == "Edge.Cuts" and g.graphic_type == "rect"
        ]
        # gr_poly / gr_curve are not parsed into the in-memory collections, so
        # recover their vertex chains straight off the S-expression tree.
        poly_chains = self._edge_cuts_poly_chains_sexp()

        if not edge_lines and not edge_arcs and not edge_rects and not poly_chains:
            return []

        # Build a list of all line segments (including arc endpoints)
        segments: list[tuple[tuple[float, float], tuple[float, float]]] = []

        for line in edge_lines:
            segments.append((line.start, line.end))

        for arc in edge_arcs:
            # For arcs, include start->mid and mid->end as approximation
            segments.append((arc.start, arc.mid))
            segments.append((arc.mid, arc.end))

        for rect in edge_rects:
            segments.extend(self._rect_to_segments(rect.start, rect.end))

        # Each gr_poly / gr_curve contributes edges between consecutive
        # vertices (and a closing edge back to the first vertex) so the shared
        # segment-stitching logic below can walk them like any other outline.
        for chain in poly_chains:
            for i in range(len(chain) - 1):
                segments.append((chain[i], chain[i + 1]))
            if len(chain) >= 3:
                segments.append((chain[-1], chain[0]))

        if not segments:
            return []

        # Build ordered polygon by connecting segments
        # Start with the first segment
        polygon: list[tuple[float, float]] = [segments[0][0], segments[0][1]]
        used = {0}

        # Keep finding the next connected segment
        while len(used) < len(segments):
            current_end = polygon[-1]
            found = False

            for i, (start, end) in enumerate(segments):
                if i in used:
                    continue

                # Check if this segment connects to current end
                if self._points_close(current_end, start):
                    polygon.append(end)
                    used.add(i)
                    found = True
                    break
                elif self._points_close(current_end, end):
                    polygon.append(start)
                    used.add(i)
                    found = True
                    break

            if not found:
                # No more connected segments found
                break

        # Transform from sheet-absolute to board-relative coordinates
        # so that outline coordinates match footprint positions (which are
        # converted to board-relative in _detect_board_origin).
        ox, oy = self._board_origin
        if ox != 0.0 or oy != 0.0:
            polygon = [(x - ox, y - oy) for x, y in polygon]

        return polygon

    @staticmethod
    def _points_close(
        p1: tuple[float, float], p2: tuple[float, float], tolerance: float = 0.001
    ) -> bool:
        """Check if two points are within tolerance distance."""
        dx = p1[0] - p2[0]
        dy = p1[1] - p2[1]
        return (dx * dx + dy * dy) < (tolerance * tolerance)

    def get_board_outline_segments(
        self,
    ) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        """Get board outline as a list of line segments.

        Returns all Edge.Cuts graphic elements as line segments.
        Handles gr_line, gr_arc, and gr_rect elements.
        More useful for distance calculations than the polygon.

        Returns:
            List of ((x1, y1), (x2, y2)) tuples representing line segments.
        """
        segments: list[tuple[tuple[float, float], tuple[float, float]]] = []

        for line in self._graphic_lines:
            if line.layer == "Edge.Cuts":
                segments.append((line.start, line.end))

        for arc in self._graphic_arcs:
            if arc.layer == "Edge.Cuts":
                # Approximate arc with two segments through midpoint
                segments.append((arc.start, arc.mid))
                segments.append((arc.mid, arc.end))

        for graphic in self._graphics:
            if graphic.layer == "Edge.Cuts" and graphic.graphic_type == "rect":
                segments.extend(self._rect_to_segments(graphic.start, graphic.end))

        # Transform from sheet-absolute to board-relative coordinates
        # so that outline segments match footprint positions (which are
        # converted to board-relative in _detect_board_origin).
        ox, oy = self._board_origin
        if ox != 0.0 or oy != 0.0:
            segments = [((x1 - ox, y1 - oy), (x2 - ox, y2 - oy)) for (x1, y1), (x2, y2) in segments]

        return segments

    # ------------------------------------------------------------------
    # Edge.Cuts contour analysis and manipulation
    # ------------------------------------------------------------------

    _MOUNTING_HOLE_AREA_THRESHOLD = 25.0  # mm^2

    def list_edge_contours(self) -> list[EdgeContour]:
        """Group Edge.Cuts graphic elements into connected contours.

        Each contour is a set of connected graphic elements (gr_line,
        gr_arc, gr_rect, gr_circle) on the Edge.Cuts layer.  A gr_rect
        or gr_circle is always its own contour; gr_line/gr_arc elements
        are chained by endpoint proximity.

        Small contours (bounding-box area < 25 mm^2) are flagged as
        mounting holes.

        Returns:
            Ordered list of ``EdgeContour`` objects with index, bounding
            box, element count, and sexp node references.
        """
        # Collect Edge.Cuts sexp nodes, keyed by identity, with endpoints
        # Each entry: (sexp_node, endpoints_list)
        # endpoints_list items are (x, y) tuples at segment ends
        edge_elements: list[tuple[SExp, list[tuple[float, float]]]] = []

        for child in self._sexp.children:
            if child.is_atom:
                continue
            tag = child.tag
            if tag not in ("gr_line", "gr_arc", "gr_rect", "gr_circle"):
                continue
            layer_node = child.find("layer")
            if not layer_node:
                continue
            layer_name = layer_node.get_string(0) or ""
            if layer_name != "Edge.Cuts":
                continue

            endpoints: list[tuple[float, float]] = []
            if tag in ("gr_line", "gr_arc"):
                s = child.find("start")
                e = child.find("end")
                if s and e:
                    endpoints = [
                        (s.get_float(0) or 0.0, s.get_float(1) or 0.0),
                        (e.get_float(0) or 0.0, e.get_float(1) or 0.0),
                    ]
            elif tag in ("gr_rect", "gr_circle"):
                # Self-contained shape -- not chainable with lines/arcs
                endpoints = []  # sentinel: standalone contour

            edge_elements.append((child, endpoints))

        if not edge_elements:
            return []

        # Partition into groups.
        # Standalone shapes (gr_rect, gr_circle) are each their own group.
        # Line/arc elements are chained by endpoint proximity.
        standalone: list[list[int]] = []
        chainable_indices: list[int] = []
        for idx, (node, eps) in enumerate(edge_elements):
            tag = node.tag
            if tag in ("gr_rect", "gr_circle"):
                standalone.append([idx])
            else:
                chainable_indices.append(idx)

        # Union-Find for chainable elements
        parent = {i: i for i in chainable_indices}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        tolerance = 0.001
        for i_pos, i in enumerate(chainable_indices):
            eps_i = edge_elements[i][1]
            for j in chainable_indices[i_pos + 1 :]:
                eps_j = edge_elements[j][1]
                for pi in eps_i:
                    for pj in eps_j:
                        dx = pi[0] - pj[0]
                        dy = pi[1] - pj[1]
                        if (dx * dx + dy * dy) < (tolerance * tolerance):
                            union(i, j)

        # Group chainable elements by root
        from collections import defaultdict

        chain_groups: dict[int, list[int]] = defaultdict(list)
        for i in chainable_indices:
            chain_groups[find(i)].append(i)

        all_groups = standalone + list(chain_groups.values())

        # Build EdgeContour objects
        contours: list[EdgeContour] = []
        for group_idx, group in enumerate(all_groups):
            nodes = [edge_elements[i][0] for i in group]
            all_points = self._collect_edge_points(nodes)
            if not all_points:
                continue
            xs = [p[0] for p in all_points]
            ys = [p[1] for p in all_points]
            bbox = (min(xs), min(ys), max(xs), max(ys))
            area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
            contour = EdgeContour(
                index=group_idx,
                element_count=len(nodes),
                bbox=bbox,
                is_mounting_hole=area < self._MOUNTING_HOLE_AREA_THRESHOLD,
                sexp_nodes=nodes,
            )
            contours.append(contour)

        # Re-index
        for idx, c in enumerate(contours):
            c.index = idx
        return contours

    @staticmethod
    def _collect_edge_points(nodes: list[SExp]) -> list[tuple[float, float]]:
        """Collect all coordinate points from a list of sexp graphic nodes."""
        points: list[tuple[float, float]] = []
        for node in nodes:
            for attr in ("start", "end", "mid", "center"):
                n = node.find(attr)
                if n:
                    points.append((n.get_float(0) or 0.0, n.get_float(1) or 0.0))
        return points

    def remove_edge_contour(self, index: int) -> bool:
        """Remove an Edge.Cuts contour by index.

        Removes the graphic sexp nodes belonging to the contour from the
        S-expression tree and from the in-memory ``_graphic_lines``,
        ``_graphic_arcs``, and ``_graphics`` lists.

        Args:
            index: Contour index as returned by ``list_edge_contours()``.

        Returns:
            True if the contour was found and removed, False otherwise.
        """
        contours = self.list_edge_contours()
        target = None
        for c in contours:
            if c.index == index:
                target = c
                break
        if target is None:
            return False

        # Remove from sexp tree
        for node in target.sexp_nodes:
            self._sexp.remove(node)

        # Clean in-memory lists by matching UUID
        target_uuids: set[str] = set()
        for node in target.sexp_nodes:
            uuid_n = node.find("uuid")
            if uuid_n:
                u = uuid_n.get_string(0) or ""
                if u:
                    target_uuids.add(u)

        if target_uuids:
            self._graphic_lines = [gl for gl in self._graphic_lines if gl.uuid not in target_uuids]
            self._graphic_arcs = [ga for ga in self._graphic_arcs if ga.uuid not in target_uuids]
            self._graphics = [g for g in self._graphics if g.uuid not in target_uuids]
        else:
            # Fallback: re-parse the in-memory lists from sexp
            self._graphic_lines = []
            self._graphic_arcs = []
            self._graphics = []
            for child in self._sexp.children:
                if child.is_atom:
                    continue
                tag = child.tag
                if tag == "gr_line":
                    self._graphic_lines.append(GraphicLine.from_sexp(child))
                    self._graphics.append(BoardGraphic.from_sexp(child, "line"))
                elif tag == "gr_arc":
                    self._graphic_arcs.append(GraphicArc.from_sexp(child))
                    self._graphics.append(BoardGraphic.from_sexp(child, "arc"))
                elif tag in ("gr_rect", "gr_circle"):
                    graphic_type = tag[3:]
                    self._graphics.append(BoardGraphic.from_sexp(child, graphic_type))

        return True

    def replace_outline(
        self,
        origin_x: float,
        origin_y: float,
        width: float,
        height: float,
    ) -> int:
        """Replace all outline contours with a rectangle.

        Removes every Edge.Cuts contour whose bounding-box area is above
        the mounting-hole threshold, then inserts a new rectangular outline
        (four ``gr_line`` Edge.Cuts segments) at the given origin and size.
        Mounting-hole contours are preserved.

        Args:
            origin_x: X coordinate of top-left corner (mm).
            origin_y: Y coordinate of top-left corner (mm).
            width: Board width (mm).
            height: Board height (mm).

        Returns:
            Number of outline contours removed.
        """
        contours = self.list_edge_contours()
        removed = 0
        # Remove outlines (non-mounting-hole contours)
        # Process in reverse index order so earlier indices stay valid
        for c in sorted(contours, key=lambda c: c.index, reverse=True):
            if not c.is_mounting_hole:
                for node in c.sexp_nodes:
                    self._sexp.remove(node)
                removed += 1

        # Insert new outline (four gr_line Edge.Cuts segments)
        for line in PCB._build_board_outline_sexp(width, height, origin_x, origin_y):
            self._sexp.append(line)

        # Rebuild in-memory lists
        self._graphic_lines = []
        self._graphic_arcs = []
        self._graphics = []
        for child in self._sexp.children:
            if child.is_atom:
                continue
            tag = child.tag
            if tag == "gr_line":
                self._graphic_lines.append(GraphicLine.from_sexp(child))
                self._graphics.append(BoardGraphic.from_sexp(child, "line"))
            elif tag == "gr_arc":
                self._graphic_arcs.append(GraphicArc.from_sexp(child))
                self._graphics.append(BoardGraphic.from_sexp(child, "arc"))
            elif tag in ("gr_rect", "gr_circle"):
                graphic_type = tag[3:]
                self._graphics.append(BoardGraphic.from_sexp(child, graphic_type))

        return removed

    @property
    def setup(self) -> Setup | None:
        """Board setup/design rules."""
        return self._setup

    # Statistics

    @property
    def footprint_count(self) -> int:
        """Number of footprints."""
        return len(self._footprints)

    @property
    def segment_count(self) -> int:
        """Number of trace segments.

        Counts top-level ``(segment ...)`` nodes in the S-expression tree
        so the result always reflects the actual file content, even if the
        in-memory ``_segments`` list has drifted.
        """
        return sum(
            1 for child in self._sexp.children if not child.is_atom and child.name == "segment"
        )

    @property
    def via_count(self) -> int:
        """Number of vias.

        Counts top-level ``(via ...)`` nodes in the S-expression tree
        so the result always reflects the actual file content, even if the
        in-memory ``_vias`` list has drifted.
        """
        return sum(1 for child in self._sexp.children if not child.is_atom and child.name == "via")

    @property
    def zone_count(self) -> int:
        """Number of zones (copper pours).

        Counts top-level ``(zone ...)`` nodes in the S-expression tree
        so the result always reflects the actual file content, even if the
        in-memory ``_zones`` list has drifted.

        Cross-validates against the in-memory ``_zones`` list and logs a
        warning if the two disagree, which aids diagnosis of counting
        discrepancies.
        """
        sexp_count = sum(
            1 for child in self._sexp.children if not child.is_atom and child.name == "zone"
        )
        mem_count = len(self._zones)
        if sexp_count != mem_count:
            logger.warning(
                "Zone count mismatch: S-expression tree has %d zone nodes "
                "but in-memory list has %d entries",
                sexp_count,
                mem_count,
            )
        return sexp_count

    @property
    def net_count(self) -> int:
        """Number of nets."""
        return len(self._nets)

    def total_trace_length(self, layer: str | None = None) -> float:
        """Calculate total trace length in mm."""
        import math

        total = 0.0
        for seg in self._segments:
            if layer is None or seg.layer == layer:
                dx = seg.end[0] - seg.start[0]
                dy = seg.end[1] - seg.start[1]
                total += math.sqrt(dx * dx + dy * dy)
        return total

    def summary(self) -> dict:
        """Get board summary statistics.

        Via and zone counts are derived from the S-expression tree (the
        authoritative on-disk representation) rather than the in-memory
        cache lists, ensuring accuracy even after in-place modifications
        by other tools.
        """
        w, h = self.board_size
        return {
            "title": self.title,
            "revision": self.revision,
            "width_mm": round(w, 2),
            "height_mm": round(h, 2),
            "area_mm2": round(w * h, 2),
            "copper_layers": len(self.copper_layers),
            "footprints": self.footprint_count,
            "nets": self.net_count,
            "segments": self.segment_count,
            "vias": self.via_count,
            "zones": self.zone_count,
            "trace_length_mm": round(self.total_trace_length(), 2),
        }

    # Modification methods

    def update_footprint_position(
        self,
        reference: str,
        x: float,
        y: float,
        rotation: float | None = None,
    ) -> bool:
        """
        Update a footprint's position in the underlying S-expression.

        Coordinates are relative to the board origin. For centered boards
        (created with center=True, the default), the board origin is offset
        from the drawing sheet origin. This method automatically applies
        that offset so users can specify positions relative to the board
        corner (0, 0 = top-left of board outline).

        Args:
            reference: Reference designator (e.g., "U1")
            x: New X position in mm, relative to board origin
            y: New Y position in mm, relative to board origin
            rotation: New rotation in degrees (optional)

        Returns:
            True if footprint was found and updated
        """
        # Find the footprint in the parsed data
        fp = self.get_footprint(reference)
        if not fp:
            return False

        # The Footprint.__setattr__ override automatically syncs the
        # board-relative position to the S-expression ``(at ...)`` node,
        # adding the board origin offset.  For footprints with a linked
        # _sexp_node this is all that's needed.
        fp.position = (x, y)
        if rotation is not None:
            fp.rotation = rotation

        if fp._sexp_node is not None:
            return True

        # Fallback: walk the top-level S-expression tree when there is no
        # back-reference (should not happen for footprints parsed via
        # from_sexp, but kept for safety).
        abs_x = x + self._board_origin[0]
        abs_y = y + self._board_origin[1]

        for child in self._sexp.iter_children():
            if child.tag != "footprint":
                continue

            # Check if this is the right footprint by looking at reference
            ref_value = None

            # KiCad 7 format: fp_text with type "reference"
            for fp_text in child.find_all("fp_text"):
                if fp_text.get_string(0) == "reference":
                    ref_value = fp_text.get_string(1)
                    break

            # KiCad 8+ format: property with name "Reference"
            if not ref_value:
                for prop in child.find_all("property"):
                    if prop.get_string(0) == "Reference":
                        ref_value = prop.get_string(1)
                        break

            if ref_value != reference:
                continue

            # Found the footprint, update its 'at' node with sheet-absolute coords
            at_node = child.find("at")
            if at_node:
                at_node.set_value(0, abs_x)
                at_node.set_value(1, abs_y)
                if rotation is not None:
                    # Handle cases where rotation may or may not exist
                    if len(at_node.children) >= 3:
                        at_node.set_value(2, rotation)
                    elif rotation != 0.0:
                        # Use add() instead of values.append() since values
                        # is a read-only property that returns a new list
                        at_node.add(rotation)
            return True

        return False

    def update_footprint_reference(
        self,
        old_reference: str,
        new_reference: str,
    ) -> bool:
        """
        Update a footprint's reference designator.

        Renames the reference designator in both the parsed footprint object
        and the underlying S-expression tree. Handles both KiCad 7 (fp_text)
        and KiCad 8+ (property) formats.

        Args:
            old_reference: Current reference designator (e.g., "R1")
            new_reference: New reference designator (e.g., "R100")

        Returns:
            True if footprint was found and updated, False if the old
            reference was not found or the new reference already exists.
        """
        # Check that the old reference exists
        fp = self.get_footprint(old_reference)
        if not fp:
            return False

        # Check for collision with existing reference
        if old_reference != new_reference and self.get_footprint(new_reference):
            return False

        # Update the S-expression tree
        for child in self._sexp.iter_children():
            if child.tag != "footprint":
                continue

            fp_ref = self._get_footprint_reference(child)
            if fp_ref != old_reference:
                continue

            # Update KiCad 7 format: fp_text with type "reference"
            for fp_text in child.find_all("fp_text"):
                if fp_text.get_string(0) == "reference":
                    fp_text.set_value(1, new_reference)

            # Update KiCad 8+ format: property with name "Reference"
            for prop in child.find_all("property"):
                if prop.get_string(0) == "Reference":
                    prop.set_value(1, new_reference)

            # Update parsed footprint object
            fp.reference = new_reference
            for text in fp.texts:
                if text.text_type == "reference":
                    text.text = new_reference

            return True

        return False

    def update_footprint_value(
        self,
        reference: str,
        new_value: str,
    ) -> bool:
        """
        Update a footprint's value field.

        Updates the value in both the parsed footprint object and the
        underlying S-expression tree. Handles both KiCad 7 (fp_text)
        and KiCad 8+ (property) formats.

        Args:
            reference: Reference designator of the footprint (e.g., "R1")
            new_value: New value string (e.g., "4.7k")

        Returns:
            True if footprint was found and updated, False if the
            reference was not found.
        """
        # Check that the reference exists
        fp = self.get_footprint(reference)
        if not fp:
            return False

        # Update the S-expression tree
        for child in self._sexp.iter_children():
            if child.tag != "footprint":
                continue

            fp_ref = self._get_footprint_reference(child)
            if fp_ref != reference:
                continue

            # Update KiCad 7 format: fp_text with type "value"
            for fp_text in child.find_all("fp_text"):
                if fp_text.get_string(0) == "value":
                    fp_text.set_value(1, new_value)

            # Update KiCad 8+ format: property with name "Value"
            for prop in child.find_all("property"):
                if prop.get_string(0) == "Value":
                    prop.set_value(1, new_value)

            # Update parsed footprint object
            fp.value = new_value
            for text in fp.texts:
                if text.text_type == "value":
                    text.text = new_value

            return True

        return False

    # Silkscreen management methods

    def set_reference_visibility(
        self,
        reference: str | None = None,
        *,
        visible: bool = True,
        pattern: str | None = None,
    ) -> int:
        """
        Set visibility of reference designators on silkscreen.

        Can target a specific reference, all references, or references matching
        a glob pattern.

        Args:
            reference: Specific reference designator (e.g., "U1"). If None,
                      applies to all footprints (or those matching pattern).
            visible: True to show, False to hide the reference designator.
            pattern: Glob pattern to match references (e.g., "C*" for all
                    capacitors, "U?" for single-digit ICs). Ignored if
                    reference is specified.

        Returns:
            Number of references updated.

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> # Hide all reference designators
            >>> pcb.set_reference_visibility(visible=False)
            >>> # Hide just capacitors
            >>> pcb.set_reference_visibility(pattern="C*", visible=False)
            >>> # Show specific reference
            >>> pcb.set_reference_visibility("U1", visible=True)
        """
        import fnmatch

        count = 0

        # Determine which references to update
        refs_to_update: set[str] = set()
        if reference is not None:
            refs_to_update.add(reference)
        elif pattern is not None:
            for fp in self._footprints:
                if fnmatch.fnmatch(fp.reference, pattern):
                    refs_to_update.add(fp.reference)
        else:
            # All footprints
            refs_to_update = {fp.reference for fp in self._footprints}

        # Update S-expression tree
        for child in self._sexp.iter_children():
            if child.tag != "footprint":
                continue

            # Get reference from this footprint
            fp_ref = self._get_footprint_reference(child)
            if fp_ref not in refs_to_update:
                continue

            # Update visibility in fp_text nodes (KiCad 7 format)
            for fp_text in child.find_all("fp_text"):
                if fp_text.get_string(0) == "reference":
                    self._set_text_visibility(fp_text, visible)
                    count += 1

            # Update visibility in property nodes (KiCad 8+ format)
            for prop in child.find_all("property"):
                if prop.get_string(0) == "Reference":
                    self._set_text_visibility(prop, visible)
                    count += 1

        # Update parsed footprint objects
        for fp in self._footprints:
            if fp.reference in refs_to_update:
                for text in fp.texts:
                    if text.text_type == "reference":
                        text.hidden = not visible

        return count

    def _get_footprint_reference(self, fp_sexp: SExp) -> str:
        """Extract reference designator from footprint S-expression."""
        # Try KiCad 7 format first (fp_text)
        for fp_text in fp_sexp.find_all("fp_text"):
            if fp_text.get_string(0) == "reference":
                return fp_text.get_string(1) or ""

        # Try KiCad 8+ format (property)
        for prop in fp_sexp.find_all("property"):
            if prop.get_string(0) == "Reference":
                return prop.get_string(1) or ""

        return ""

    def _set_text_visibility(self, text_sexp: SExp, visible: bool) -> None:
        """Set visibility on a text S-expression node."""
        effects = text_sexp.find("effects")
        if effects is None:
            # Create effects node if needed
            effects = SExp.list("effects")
            font = SExp.list("font")
            font.append(SExp.list("size", 1.0, 1.0))
            font.append(SExp.list("thickness", 0.15))
            effects.append(font)
            text_sexp.append(effects)

        # Find existing hide node
        hide_node = effects.find("hide")

        if visible:
            # Remove hide node if present
            if hide_node is not None:
                effects.remove(hide_node)
        else:
            # Add hide node if not present
            if hide_node is None:
                effects.append(SExp.list("hide", "yes"))

    def move_reference(
        self,
        reference: str,
        offset: tuple[float, float] = (0.0, 0.0),
        *,
        absolute: tuple[float, float] | None = None,
        layer: str | None = None,
    ) -> bool:
        """
        Move a reference designator's silkscreen text.

        Args:
            reference: Reference designator to move (e.g., "U1").
            offset: (dx, dy) offset from current position in mm.
                   Ignored if absolute is specified.
            absolute: Absolute (x, y) position in mm, relative to the
                     footprint origin. If specified, offset is ignored.
            layer: Optional new layer (e.g., "F.SilkS", "F.Fab").
                  If None, layer is unchanged.

        Returns:
            True if reference was found and updated.

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> # Move U1 reference up by 5mm
            >>> pcb.move_reference("U1", offset=(0, -5))
            >>> # Move to absolute position
            >>> pcb.move_reference("U1", absolute=(2.0, -3.0))
            >>> # Move to fab layer (hidden from manufacturing)
            >>> pcb.move_reference("U1", layer="F.Fab")
        """
        # Find footprint in S-expression tree
        for child in self._sexp.iter_children():
            if child.tag != "footprint":
                continue

            fp_ref = self._get_footprint_reference(child)
            if fp_ref != reference:
                continue

            # Found the footprint - update reference text
            updated = False

            # Update fp_text nodes (KiCad 7 format)
            for fp_text in child.find_all("fp_text"):
                if fp_text.get_string(0) == "reference":
                    self._move_text_element(fp_text, offset, absolute, layer)
                    updated = True

            # Update property nodes (KiCad 8+ format)
            for prop in child.find_all("property"):
                if prop.get_string(0) == "Reference":
                    self._move_text_element(prop, offset, absolute, layer)
                    updated = True

            # Update parsed footprint object
            if updated:
                for fp in self._footprints:
                    if fp.reference == reference:
                        for text in fp.texts:
                            if text.text_type == "reference":
                                if absolute is not None:
                                    text.position = absolute
                                else:
                                    text.position = (
                                        text.position[0] + offset[0],
                                        text.position[1] + offset[1],
                                    )
                                if layer is not None:
                                    text.layer = layer
                        break

            return updated

        return False

    def _move_text_element(
        self,
        text_sexp: SExp,
        offset: tuple[float, float],
        absolute: tuple[float, float] | None,
        layer: str | None,
    ) -> None:
        """Move a text element's position and optionally change its layer."""
        # Update position
        at_node = text_sexp.find("at")
        if at_node is None:
            # Create at node with default position
            at_node = SExp.list("at", 0.0, 0.0)
            text_sexp.append(at_node)

        if absolute is not None:
            at_node.set_value(0, absolute[0])
            at_node.set_value(1, absolute[1])
        else:
            current_x = at_node.get_float(0) or 0.0
            current_y = at_node.get_float(1) or 0.0
            at_node.set_value(0, current_x + offset[0])
            at_node.set_value(1, current_y + offset[1])

        # Update layer if specified
        if layer is not None:
            layer_node = text_sexp.find("layer")
            if layer_node is not None:
                layer_node.set_value(0, layer)
            else:
                text_sexp.append(SExp.list("layer", layer))

    def set_silkscreen_font(
        self,
        size: float | tuple[float, float] = 1.0,
        thickness: float = 0.15,
        *,
        pattern: str | None = None,
        text_types: tuple[str, ...] = ("reference",),
    ) -> int:
        """
        Set font size for silkscreen text on all footprints.

        Args:
            size: Font size in mm. Can be a single value (used for both
                 width and height) or a (width, height) tuple.
            thickness: Stroke thickness in mm.
            pattern: Glob pattern to match references (e.g., "C*" for all
                    capacitors). If None, applies to all footprints.
            text_types: Which text types to update. Default is ("reference",).
                       Can include "reference", "value", "user".

        Returns:
            Number of text elements updated.

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> # Set smaller font for all references
            >>> pcb.set_silkscreen_font(size=0.8, thickness=0.15)
            >>> # Set font for just capacitor values
            >>> pcb.set_silkscreen_font(
            ...     size=0.6, pattern="C*", text_types=("value",)
            ... )
        """
        import fnmatch

        if isinstance(size, (int, float)):
            font_size = (float(size), float(size))
        else:
            font_size = size

        count = 0

        # Determine which references to update
        refs_to_update: set[str] = set()
        if pattern is not None:
            for fp in self._footprints:
                if fnmatch.fnmatch(fp.reference, pattern):
                    refs_to_update.add(fp.reference)
        else:
            refs_to_update = {fp.reference for fp in self._footprints}

        # Update S-expression tree
        for child in self._sexp.iter_children():
            if child.tag != "footprint":
                continue

            fp_ref = self._get_footprint_reference(child)
            if fp_ref not in refs_to_update:
                continue

            # Update fp_text nodes (KiCad 7 format)
            for fp_text in child.find_all("fp_text"):
                text_type = fp_text.get_string(0)
                if text_type in text_types:
                    self._set_text_font(fp_text, font_size, thickness)
                    count += 1

            # Update property nodes (KiCad 8+ format)
            for prop in child.find_all("property"):
                prop_name = prop.get_string(0)
                if (prop_name == "Reference" and "reference" in text_types) or (
                    prop_name == "Value" and "value" in text_types
                ):
                    self._set_text_font(prop, font_size, thickness)
                    count += 1

        # Update parsed footprint objects
        for fp in self._footprints:
            if fp.reference in refs_to_update:
                for text in fp.texts:
                    if text.text_type in text_types:
                        text.font_size = font_size
                        text.font_thickness = thickness

        return count

    def _set_text_font(
        self,
        text_sexp: SExp,
        size: tuple[float, float],
        thickness: float,
    ) -> None:
        """Set font properties on a text S-expression node."""
        effects = text_sexp.find("effects")
        if effects is None:
            effects = SExp.list("effects")
            text_sexp.append(effects)

        font = effects.find("font")
        if font is None:
            font = SExp.list("font")
            effects.append(font)

        # Update or create size node
        size_node = font.find("size")
        if size_node is not None:
            size_node.set_value(0, size[0])
            size_node.set_value(1, size[1])
        else:
            font.append(SExp.list("size", size[0], size[1]))

        # Update or create thickness node
        thickness_node = font.find("thickness")
        if thickness_node is not None:
            thickness_node.set_value(0, thickness)
        else:
            font.append(SExp.list("thickness", thickness))

    def move_references_to_layer(
        self,
        layer: str,
        *,
        pattern: str | None = None,
    ) -> int:
        """
        Move all reference designators to a different layer.

        Useful for moving references to the fabrication layer (F.Fab)
        so they don't appear on manufactured silkscreen.

        Args:
            layer: Target layer (e.g., "F.Fab", "F.SilkS").
            pattern: Glob pattern to match references (e.g., "C*" for all
                    capacitors). If None, applies to all footprints.

        Returns:
            Number of references moved.

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> # Move all references to fab layer
            >>> pcb.move_references_to_layer("F.Fab")
            >>> # Move just capacitor references to fab
            >>> pcb.move_references_to_layer("F.Fab", pattern="C*")
        """
        import fnmatch

        count = 0

        # Determine which references to update
        refs_to_update: set[str] = set()
        if pattern is not None:
            for fp in self._footprints:
                if fnmatch.fnmatch(fp.reference, pattern):
                    refs_to_update.add(fp.reference)
        else:
            refs_to_update = {fp.reference for fp in self._footprints}

        # Update S-expression tree
        for child in self._sexp.iter_children():
            if child.tag != "footprint":
                continue

            fp_ref = self._get_footprint_reference(child)
            if fp_ref not in refs_to_update:
                continue

            # Update fp_text nodes (KiCad 7 format)
            for fp_text in child.find_all("fp_text"):
                if fp_text.get_string(0) == "reference":
                    layer_node = fp_text.find("layer")
                    if layer_node is not None:
                        layer_node.set_value(0, layer)
                    else:
                        fp_text.append(SExp.list("layer", layer))
                    count += 1

            # Update property nodes (KiCad 8+ format)
            for prop in child.find_all("property"):
                if prop.get_string(0) == "Reference":
                    layer_node = prop.find("layer")
                    if layer_node is not None:
                        layer_node.set_value(0, layer)
                    else:
                        prop.append(SExp.list("layer", layer))
                    count += 1

        # Update parsed footprint objects
        for fp in self._footprints:
            if fp.reference in refs_to_update:
                for text in fp.texts:
                    if text.text_type == "reference":
                        text.layer = layer

        return count

    def validate_silkscreen(
        self,
        design_rules: DesignRules | None = None,
    ) -> list[dict]:
        """
        Validate silkscreen elements and return issues.

        Checks for common silkscreen problems including:
        - Text height too small for manufacturing
        - Line width too thin
        - Silkscreen overlapping exposed pads

        Args:
            design_rules: Manufacturing design rules. If None, uses
                         default JLCPCB-compatible rules.

        Returns:
            List of issue dictionaries with keys:
            - type: Issue type (e.g., "text_height", "over_pad")
            - reference: Reference designator or element identifier
            - description: Human-readable description
            - location: (x, y) position in mm
            - layer: Layer name

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> issues = pcb.validate_silkscreen()
            >>> for issue in issues:
            ...     print(f"{issue['type']}: {issue['reference']} - {issue['description']}")
        """
        from ..manufacturers import DesignRules as DR
        from ..validate.rules.silkscreen import check_all_silkscreen

        if design_rules is None:
            # Use default JLCPCB-compatible rules
            design_rules = DR(
                min_trace_width_mm=0.127,
                min_clearance_mm=0.127,
                min_via_drill_mm=0.3,
                min_via_diameter_mm=0.5,
                min_annular_ring_mm=0.127,
                min_silkscreen_width_mm=0.15,
                min_silkscreen_height_mm=0.8,
            )

        results = check_all_silkscreen(self, design_rules)

        issues = []
        for violation in results.violations:
            issues.append(
                {
                    "type": violation.rule_id.replace("silkscreen_", ""),
                    "reference": violation.items[0] if violation.items else "",
                    "description": violation.message,
                    "location": violation.location,
                    "layer": violation.layer,
                }
            )

        return issues

    def add_footprint_from_file(
        self,
        kicad_mod_path: str | Path,
        reference: str,
        x: float,
        y: float,
        rotation: float = 0.0,
        layer: str = "F.Cu",
        value: str = "",
    ) -> Footprint:
        """
        Add a footprint from a .kicad_mod file to the PCB.

        Loads a footprint from a KiCad footprint file and adds it to the PCB
        at the specified position with the given reference designator.

        Coordinates are relative to the board origin. For centered boards
        (created with center=True, the default), the board origin is offset
        from the drawing sheet origin. This method automatically applies
        that offset so users can specify positions relative to the board
        corner (0, 0 = top-left of board outline).

        Args:
            kicad_mod_path: Path to the .kicad_mod footprint file
            reference: Reference designator for the component (e.g., "U1", "C1")
            x: X position in mm, relative to board origin
            y: Y position in mm, relative to board origin
            rotation: Rotation angle in degrees (default: 0)
            layer: Layer to place footprint on ("F.Cu" or "B.Cu", default: "F.Cu")
            value: Component value (e.g., "100nF", "10k")

        Returns:
            The Footprint object that was added to the PCB

        Raises:
            FileNotFoundError: If the footprint file doesn't exist
            FileFormatError: If the file is not a valid footprint

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> fp = pcb.add_footprint_from_file(
            ...     "MyFootprints.pretty/SOT-23.kicad_mod",
            ...     reference="U1",
            ...     x=50.0,
            ...     y=30.0,
            ...     rotation=90,
            ...     value="LM317"
            ... )
        """
        # Load the footprint from file
        fp_sexp = load_footprint(kicad_mod_path)

        # Generate a new UUID for this footprint instance
        new_uuid = str(uuid.uuid4())

        # Update the UUID in the footprint
        uuid_node = fp_sexp.find("uuid")
        if uuid_node:
            uuid_node.set_value(0, new_uuid)
        else:
            # Add UUID if not present
            fp_sexp.append(SExp.list("uuid", new_uuid))

        # Update layer first (at node must come after layer)
        layer_node = fp_sexp.find("layer")
        if layer_node:
            layer_node.set_value(0, layer)
        else:
            # Create layer node - library footprints always have this, but be safe
            layer_node = SExp.list("layer", layer)
            fp_sexp.append(layer_node)

        # Update position (at x y rotation)
        # Library footprints (.kicad_mod) don't have a top-level (at) node -
        # that's only present in placed footprints within a PCB file.
        # We must create the (at) node and insert it immediately after (layer)
        # for KiCad to recognize it properly.
        at_node = fp_sexp.find("at")
        if at_node:
            # Remove existing at node - we'll insert a fresh one in the correct position
            fp_sexp.remove(at_node)

        # Apply board origin offset to convert board-relative to sheet-absolute
        abs_x = x + self._board_origin[0]
        abs_y = y + self._board_origin[1]

        # Create new at node with position (sheet-absolute coordinates)
        at_sexp = SExp.list("at", abs_x, abs_y)
        if rotation != 0.0:
            at_sexp.add(rotation)

        # Find layer node's index and insert at node immediately after it
        layer_index = None
        for i, child in enumerate(fp_sexp.children):
            if not child.is_atom and child.name == "layer":
                layer_index = i
                break

        if layer_index is not None:
            fp_sexp.children.insert(layer_index + 1, at_sexp)
        else:
            # Fallback: append to end (shouldn't happen for valid footprints)
            fp_sexp.append(at_sexp)

        # Rewrite each pad's angle to ABSOLUTE (board-frame) rotation.
        #
        # KiCad stores a pad's ``(at x y ANGLE)`` with the angle expressed in
        # the ABSOLUTE board frame -- it already includes the parent
        # footprint's rotation. A library (.kicad_mod) footprint is authored at
        # rotation 0, so its pad angles are footprint-local. When we place that
        # footprint at a non-zero ``rotation`` we must fold that rotation into
        # every pad angle so the emitted board matches KiCad's convention.
        #
        # Without this, KiCad (and kicad-cli DRC) renders elongated pads
        # (e.g. TSSOP 1.475mm pads) unrotated relative to their pin row,
        # producing phantom shorting / solder-mask-bridge violations and
        # geometrically wrong gerber apertures for every rotated footprint
        # (issue #3902).
        if rotation != 0.0:
            for pad in fp_sexp.find_all("pad"):
                pad_at = pad.find("at")
                if pad_at is None:
                    continue
                local_angle = pad_at.get_float(2) or 0.0
                abs_angle = (local_angle + rotation) % 360
                pad_at.set_value(2, abs_angle)

        # Update reference and value - try KiCad 8+ property format first
        ref_updated = False
        val_updated = False

        for prop in fp_sexp.find_all("property"):
            prop_name = prop.get_string(0)
            if prop_name == "Reference":
                # Use quoted_atom so values that look numeric (e.g. a
                # reference "1") still serialize quoted. A bare numeric
                # property value makes the board unloadable in kicad-cli.
                prop.children[1] = SExp.quoted_atom(reference)
                ref_updated = True
            elif prop_name == "Value":
                # Use quoted_atom so unit-less numeric values (e.g. "470",
                # "0", "100") serialize as (property "Value" "470") rather
                # than the bare token (property "Value" 470), which
                # kicad-cli rejects with "Failed to load board".
                prop.children[1] = SExp.quoted_atom(value)
                val_updated = True

        # Fall back to KiCad 7 fp_text format
        for fp_text in fp_sexp.find_all("fp_text"):
            text_type = fp_text.get_string(0)
            if text_type == "reference" and not ref_updated:
                fp_text.children[1] = SExp.quoted_atom(reference)
                ref_updated = True
            elif text_type == "value" and not val_updated:
                fp_text.children[1] = SExp.quoted_atom(value)
                val_updated = True

        # If reference/value weren't found, add them as KiCad 8+ properties
        if not ref_updated:
            # Quote the value so a numeric reference doesn't serialize bare.
            ref_prop = SExp.list("property", "Reference", SExp.quoted_atom(reference))
            ref_prop.append(SExp.list("at", 0.0, -1.5))
            ref_prop.append(SExp.list("layer", layer.replace(".Cu", ".SilkS")))
            ref_prop.append(SExp.list("uuid", str(uuid.uuid4())))
            effects = SExp.list("effects")
            font = SExp.list("font")
            font.append(SExp.list("size", 1.0, 1.0))
            font.append(SExp.list("thickness", 0.15))
            effects.append(font)
            ref_prop.append(effects)
            fp_sexp.append(ref_prop)

        if not val_updated:
            # Quote the value so a unit-less numeric value doesn't serialize bare.
            val_prop = SExp.list("property", "Value", SExp.quoted_atom(value))
            val_prop.append(SExp.list("at", 0.0, 1.5))
            val_prop.append(SExp.list("layer", layer.replace(".Cu", ".Fab")))
            val_prop.append(SExp.list("uuid", str(uuid.uuid4())))
            effects = SExp.list("effects")
            font = SExp.list("font")
            font.append(SExp.list("size", 1.0, 1.0))
            font.append(SExp.list("thickness", 0.15))
            effects.append(font)
            val_prop.append(effects)
            fp_sexp.append(val_prop)

        # Append footprint to PCB S-expression tree
        self._sexp.append(fp_sexp)

        # Parse and add to internal footprints list
        footprint = Footprint.from_sexp(fp_sexp)
        # Store board-relative position in the footprint object for API consistency.
        # _sexp_node is not yet set, so this won't sync to the S-expression
        # (the S-expression already has the correct sheet-absolute coordinates).
        footprint.position = (x, y)

        # Link the S-expression node so that future mutations to position,
        # rotation, or layer are automatically persisted.
        object.__setattr__(footprint, "_board_origin", self._board_origin)
        object.__setattr__(footprint, "_sexp_node", fp_sexp)

        self._footprints.append(footprint)

        return footprint

    def add_footprint(
        self,
        library_id: str,
        reference: str,
        x: float,
        y: float,
        rotation: float = 0.0,
        layer: str = "F.Cu",
        value: str = "",
    ) -> Footprint:
        """
        Add a footprint from KiCad standard libraries to the PCB.

        Loads a footprint from KiCad's standard library installation and adds
        it to the PCB at the specified position.

        Coordinates are relative to the board origin. For centered boards
        (created with center=True, the default), the board origin is offset
        from the drawing sheet origin. This method automatically applies
        that offset so users can specify positions relative to the board
        corner (0, 0 = top-left of board outline).

        Args:
            library_id: Footprint identifier in "Library:Footprint" format
                       (e.g., "Capacitor_SMD:C_0805_2012Metric")
                       If library is omitted, it will be guessed from the footprint name.
            reference: Reference designator for the component (e.g., "U1", "C1")
            x: X position in mm, relative to board origin
            y: Y position in mm, relative to board origin
            rotation: Rotation angle in degrees (default: 0)
            layer: Layer to place footprint on ("F.Cu" or "B.Cu", default: "F.Cu")
            value: Component value (e.g., "100nF", "10k")

        Returns:
            The Footprint object that was added to the PCB

        Raises:
            FileNotFoundError: If the footprint cannot be found in the library
            ValueError: If the library path cannot be detected

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> fp = pcb.add_footprint(
            ...     library_id="Capacitor_SMD:C_0805_2012Metric",
            ...     reference="C1",
            ...     x=50.0,
            ...     y=30.0,
            ...     value="100nF"
            ... )

            # With automatic library guessing
            >>> fp = pcb.add_footprint(
            ...     library_id="C_0402_1005Metric",  # Will guess Capacitor_SMD
            ...     reference="C2",
            ...     x=60.0,
            ...     y=30.0,
            ...     value="10nF"
            ... )
        """
        # Parse library_id to extract library name and footprint name
        library_name, footprint_name = parse_library_id(library_id)

        # If no library specified, try to guess it
        if library_name is None:
            library_name = guess_standard_library(footprint_name)
            if library_name is None:
                raise ValueError(
                    f"Cannot determine library for footprint '{footprint_name}'. "
                    "Please specify the library explicitly using 'Library:Footprint' format."
                )

        # Detect KiCad library path
        lib_paths = detect_kicad_library_path()
        if not lib_paths.found:
            raise ValueError(
                "KiCad footprint library path not found. "
                "Set KICAD_FOOTPRINT_DIR environment variable or install KiCad."
            )

        # Get the footprint file path (with fallback search across all libraries)
        fp_path = lib_paths.get_footprint_file(library_name, footprint_name)
        if fp_path is None:
            raise FileNotFoundError(
                f"Footprint '{footprint_name}' not found in library '{library_name}'. "
                f"Searched in: {lib_paths.footprints_path} "
                f"(also searched all available libraries as fallback)"
            )

        # Delegate to add_footprint_from_file
        return self.add_footprint_from_file(
            kicad_mod_path=fp_path,
            reference=reference,
            x=x,
            y=y,
            rotation=rotation,
            layer=layer,
            value=value,
        )

    def add_net(self, net_name: str) -> Net:
        """
        Add a new net to the PCB.

        If a net with the same name already exists, returns the existing net.

        Args:
            net_name: Name of the net (e.g., "GND", "+3V3", "Net-U1-Pad1")

        Returns:
            The Net object that was added or already existed

        Example:
            >>> pcb = PCB.create(width=100, height=100)
            >>> gnd = pcb.add_net("GND")
            >>> print(gnd.number, gnd.name)
            1 GND
        """
        # Check if net already exists
        existing = self.get_net_by_name(net_name)
        if existing:
            return existing

        # Find the next available net number
        next_num = max(self._nets.keys(), default=0) + 1

        # Create the net object
        net = Net(number=next_num, name=net_name)
        self._nets[next_num] = net

        # Add to the S-expression tree - insert after the last net, not at the end
        # KiCad requires nets to be declared before footprints
        net_sexp = SExp.list("net", next_num, net_name)

        # Find the position of the last net in the S-expression
        last_net_index = -1
        for i, child in enumerate(self._sexp.children):
            if child.name == "net":
                last_net_index = i

        if last_net_index >= 0:
            # Insert after the last net
            self._sexp.children.insert(last_net_index + 1, net_sexp)
        else:
            # No nets found (shouldn't happen since net 0 is always present)
            # Find the first footprint and insert before it
            first_footprint_index = -1
            for i, child in enumerate(self._sexp.children):
                if child.name == "footprint":
                    first_footprint_index = i
                    break

            if first_footprint_index >= 0:
                self._sexp.children.insert(first_footprint_index, net_sexp)
            else:
                # No footprints either, just append
                self._sexp.append(net_sexp)

        return net

    def get_pad_position(self, reference: str, pad_number: str) -> tuple[float, float] | None:
        """
        Get the absolute board position of a pad on a footprint.

        Calculates the absolute position by combining the footprint position
        with the pad's local offset, accounting for footprint rotation.

        Args:
            reference: Footprint reference designator (e.g., "U1", "C1")
            pad_number: Pad number/name (e.g., "1", "2", "A1")

        Returns:
            Tuple of (x, y) in mm if found, None if footprint or pad not found

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> pos = pcb.get_pad_position("U1", "1")
            >>> print(f"Pad at ({pos[0]:.2f}, {pos[1]:.2f})")
        """

        fp = self.get_footprint(reference)
        if not fp:
            return None

        from kicad_tools.core.geometry import rotate_pad_offset

        for pad in fp.pads:
            if pad.number == pad_number:
                # Rotate pad offset around footprint origin using KiCad's
                # negated-angle convention (see core.geometry.rotate_pad_offset).
                pad_x, pad_y = pad.position
                rotated_x, rotated_y = rotate_pad_offset(pad_x, pad_y, fp.rotation)

                # Add footprint position
                abs_x = fp.position[0] + rotated_x
                abs_y = fp.position[1] + rotated_y
                return (abs_x, abs_y)

        return None

    def add_trace(
        self,
        start: tuple[float, float] | tuple[str, str],
        end: tuple[float, float] | tuple[str, str],
        width: float = 0.25,
        layer: str = "F.Cu",
        net: str | None = None,
        waypoints: list[tuple[float, float]] | None = None,
    ) -> list[Segment]:
        """
        Add a trace (one or more segments) between two points or pads.

        Routes a trace from start to end, optionally through waypoints.
        When pad references are used, the net is automatically determined.

        Args:
            start: Start position as (x, y) tuple or pad reference as (reference, pad_number)
            end: End position as (x, y) tuple or pad reference as (reference, pad_number)
            width: Trace width in mm (default 0.25)
            layer: Copper layer name (default "F.Cu")
            net: Net name for the trace. Auto-detected from pads if not specified.
            waypoints: Optional list of (x, y) intermediate points

        Returns:
            List of Segment objects that were created

        Raises:
            ValueError: If pad references are invalid or positions cannot be determined

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> # Route between two pads
            >>> pcb.add_trace(("U1", "1"), ("C1", "1"), width=0.25, layer="F.Cu")
            >>> # Route between coordinates with waypoints
            >>> pcb.add_trace((50, 30), (60, 30), waypoints=[(55, 35)])
            >>> pcb.save("board.kicad_pcb")
        """
        # Resolve start position
        if isinstance(start[0], str):
            ref, pad = start[0], start[1]
            start_pos = self.get_pad_position(ref, pad)
            if start_pos is None:
                raise ValueError(f"Cannot find pad {pad} on footprint {ref}")
            # Auto-detect net from pad if not specified
            if net is None:
                fp = self.get_footprint(ref)
                if fp:
                    for p in fp.pads:
                        if p.number == pad and p.net_name:
                            net = p.net_name
                            break
        else:
            start_pos = (float(start[0]), float(start[1]))

        # Resolve end position
        if isinstance(end[0], str):
            ref, pad = end[0], end[1]
            end_pos = self.get_pad_position(ref, pad)
            if end_pos is None:
                raise ValueError(f"Cannot find pad {pad} on footprint {ref}")
            # Auto-detect net from pad if not specified
            if net is None:
                fp = self.get_footprint(ref)
                if fp:
                    for p in fp.pads:
                        if p.number == pad and p.net_name:
                            net = p.net_name
                            break
        else:
            end_pos = (float(end[0]), float(end[1]))

        # Get net number
        net_number = 0
        if net:
            net_obj = self.add_net(net)
            net_number = net_obj.number

        # Build list of points: start -> waypoints -> end
        points = [start_pos]
        if waypoints:
            points.extend(waypoints)
        points.append(end_pos)

        # Create segments between consecutive points.  Inputs are in
        # board-relative coordinates (see _detect_board_origin docstring);
        # the underlying S-expression must store sheet-absolute coords, so
        # we add the board origin offset when constructing the sexp node.
        ox, oy = self._board_origin
        segments = []
        for i in range(len(points) - 1):
            seg = Segment(
                start=points[i],
                end=points[i + 1],
                width=width,
                layer=layer,
                net_number=net_number,
                uuid=str(uuid.uuid4()),
            )
            segments.append(seg)
            self._segments.append(seg)
            if ox != 0.0 or oy != 0.0:
                # Build sheet-absolute sexp without mutating the Python object.
                seg_sexp = SExp.list("segment")
                seg_sexp.append(SExp.list("start", seg.start[0] + ox, seg.start[1] + oy))
                seg_sexp.append(SExp.list("end", seg.end[0] + ox, seg.end[1] + oy))
                seg_sexp.append(SExp.list("width", seg.width))
                seg_sexp.append(SExp.list("layer", seg.layer))
                seg_sexp.append(SExp.list("net", seg.net_number))
                seg_sexp.append(SExp.list("uuid", seg.uuid))
                self._sexp.append(seg_sexp)
            else:
                self._sexp.append(seg.to_sexp())

        return segments

    def add_via(
        self,
        x: float,
        y: float,
        size: float = 0.6,
        drill: float = 0.3,
        layers: tuple[str, str] = ("F.Cu", "B.Cu"),
        net: str | None = None,
    ) -> Via:
        """
        Add a via at the specified position.

        Vias connect traces between copper layers. Default parameters create
        a standard through-hole via suitable for most designs.

        Args:
            x: X position in mm
            y: Y position in mm
            size: Via pad size in mm (default 0.6)
            drill: Via drill diameter in mm (default 0.3)
            layers: Tuple of layer names to connect (default ("F.Cu", "B.Cu"))
            net: Net name for the via (optional)

        Returns:
            The Via object that was created

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> pcb.add_via(50, 30, net="GND")
            >>> pcb.save("board.kicad_pcb")
        """
        net_number = 0
        if net:
            net_obj = self.add_net(net)
            net_number = net_obj.number

        via = Via(
            position=(x, y),
            size=size,
            drill=drill,
            layers=list(layers),
            net_number=net_number,
            uuid=str(uuid.uuid4()),
        )
        self._vias.append(via)

        # Input position is board-relative (matches Footprint.position and
        # add_trace inputs); the S-expression form requires sheet-absolute.
        ox, oy = self._board_origin
        if ox != 0.0 or oy != 0.0:
            via_sexp = SExp.list("via")
            via_sexp.append(SExp.list("at", via.position[0] + ox, via.position[1] + oy))
            via_sexp.append(SExp.list("size", via.size))
            via_sexp.append(SExp.list("drill", via.drill))
            via_sexp.append(SExp.list("layers", *via.layers))
            via_sexp.append(SExp.list("net", via.net_number))
            via_sexp.append(SExp.list("uuid", via.uuid))
            self._sexp.append(via_sexp)
        else:
            self._sexp.append(via.to_sexp())

        return via

    def routing_status(self) -> dict:
        """
        Get routing statistics for the PCB.

        Returns information about traces, vias, and unrouted connections
        (ratsnest) that can be used to assess routing completion.

        Returns:
            Dictionary with routing statistics:
            - segments: Number of trace segments
            - vias: Number of vias
            - trace_length_mm: Total trace length in mm
            - nets_with_traces: Set of net numbers that have traces
            - unrouted_pads: List of (reference, pad, net) for pads without traces

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> status = pcb.routing_status()
            >>> print(f"Segments: {status['segments']}, Vias: {status['vias']}")
            >>> print(f"Total trace length: {status['trace_length_mm']:.1f} mm")
        """
        import math

        # Count segments and calculate total length
        total_length = 0.0
        nets_with_traces: set[int] = set()

        for seg in self._segments:
            dx = seg.end[0] - seg.start[0]
            dy = seg.end[1] - seg.start[1]
            total_length += math.sqrt(dx * dx + dy * dy)
            if seg.net_number > 0:
                nets_with_traces.add(seg.net_number)

        # Add vias to nets with traces
        for via in self._vias:
            if via.net_number > 0:
                nets_with_traces.add(via.net_number)

        # Find unrouted pads (pads with nets that have no traces)
        unrouted_pads = []
        for fp in self._footprints:
            for pad in fp.pads:
                if pad.net_number > 0 and pad.net_number not in nets_with_traces:
                    unrouted_pads.append((fp.reference, pad.number, pad.net_name))

        return {
            "segments": self.segment_count,
            "vias": self.via_count,
            "trace_length_mm": total_length,
            "nets_with_traces": nets_with_traces,
            "unrouted_pads": unrouted_pads,
        }

    def get_ratsnest(self) -> list[dict]:
        """
        Get the ratsnest (unrouted connections) for the PCB.

        Returns a list of connections that need to be routed, showing which
        pads need to be connected together on each net.

        Returns:
            List of dictionaries, each containing:
            - net: Net name
            - net_number: Net number
            - pads: List of (reference, pad_number, x, y) tuples for pads in the net

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> for connection in pcb.get_ratsnest():
            ...     print(f"{connection['net']}: {len(connection['pads'])} pads")
        """
        # Group pads by net
        nets_pads: dict[int, list[tuple[str, str, float, float]]] = {}

        for fp in self._footprints:
            for pad in fp.pads:
                if pad.net_number > 0:
                    pos = self.get_pad_position(fp.reference, pad.number)
                    if pos:
                        if pad.net_number not in nets_pads:
                            nets_pads[pad.net_number] = []
                        nets_pads[pad.net_number].append((fp.reference, pad.number, pos[0], pos[1]))

        # Build result with net names
        result = []
        for net_num, pads in nets_pads.items():
            if len(pads) >= 2:  # Only include nets with multiple pads
                net = self.get_net(net_num)
                net_name = net.name if net else ""
                result.append(
                    {
                        "net": net_name,
                        "net_number": net_num,
                        "pads": pads,
                    }
                )

        return result

    def assign_net_to_footprint_pad(
        self,
        reference: str,
        pad_number: str,
        net_name: str,
    ) -> bool:
        """
        Assign a net to a specific pad on a footprint.

        This updates both the in-memory footprint data and the underlying
        S-expression tree for persistence.

        Args:
            reference: Footprint reference designator (e.g., "U1", "C1")
            pad_number: Pad number/name (e.g., "1", "2", "A1")
            net_name: Name of the net to assign (will be created if doesn't exist)

        Returns:
            True if the pad was found and updated, False otherwise

        Example:
            >>> pcb = PCB.create(width=100, height=100)
            >>> pcb.add_footprint("Capacitor_SMD:C_0805_2012Metric", "C1", 50, 50)
            >>> pcb.assign_net_to_footprint_pad("C1", "1", "GND")
            True
        """
        # Find the footprint in parsed data
        fp = self.get_footprint(reference)
        if not fp:
            return False

        # Ensure net exists and get its number
        net = self.add_net(net_name)

        # Update the in-memory pad
        pad_found = False
        for pad in fp.pads:
            if pad.number == pad_number:
                pad.net_number = net.number
                pad.net_name = net.name
                pad_found = True
                break

        if not pad_found:
            return False

        # Update the S-expression tree
        for fp_sexp in self._sexp.find_all("footprint"):
            # Find the matching footprint by reference
            ref_value = None

            # KiCad 7 format: fp_text with type "reference"
            for fp_text in fp_sexp.find_all("fp_text"):
                if fp_text.get_string(0) == "reference":
                    ref_value = fp_text.get_string(1)
                    break

            # KiCad 8+ format: property with name "Reference"
            if not ref_value:
                for prop in fp_sexp.find_all("property"):
                    if prop.get_string(0) == "Reference":
                        ref_value = prop.get_string(1)
                        break

            if ref_value != reference:
                continue

            # Found the footprint, now find the pad
            for pad_sexp in fp_sexp.find_all("pad"):
                if pad_sexp.get_string(0) == pad_number:
                    # Remove existing net node if present
                    net_node = pad_sexp.find("net")
                    if net_node:
                        pad_sexp.remove(net_node)

                    # Add new net node
                    new_net_node = SExp.list("net", net.number, net.name)
                    pad_sexp.append(new_net_node)
                    return True

        return False

    def assign_nets_from_netlist(
        self,
        netlist,
        pin_to_pad_map: dict[tuple[str, str], str] | None = None,
    ) -> dict[str, list[str]]:
        """
        Assign nets to all footprint pads based on netlist connectivity.

        Iterates through all nets in the netlist and assigns them to the
        corresponding pads on footprints in the PCB.

        When the netlist is produced by the pure-Python fallback path
        (``build_netlist_from_schematic``), the ``node.pin`` values are
        schematic symbol pin numbers.  These may differ from the footprint
        pad numbers when alternate pin-to-pad mappings are in effect.
        Callers can supply *pin_to_pad_map* to translate schematic pin
        numbers to footprint pad numbers before assignment.

        Args:
            netlist: A Netlist object containing connectivity information
            pin_to_pad_map: Optional mapping from ``(reference, pin_number)``
                to ``pad_number``.  When provided, each ``node.pin`` is
                looked up in the map before being used as a pad number.
                If a key is not found in the map, ``node.pin`` is used
                as-is (identity fallback).

        Returns:
            Dictionary with statistics:
            - "assigned": List of successfully assigned pads (format: "REF.PIN")
            - "missing_footprints": List of references not found in PCB
            - "missing_pads": List of pads not found (format: "REF.PIN")

        Example:
            >>> from kicad_tools.operations.netlist import Netlist
            >>> netlist = Netlist.load("project.kicad_net")
            >>> pcb = PCB.create(width=100, height=100)
            >>> # ... add footprints ...
            >>> result = pcb.assign_nets_from_netlist(netlist)
            >>> print(f"Assigned {len(result['assigned'])} pads")
        """
        stats: dict[str, list[str]] = {
            "assigned": [],
            "missing_footprints": [],
            "missing_pads": [],
        }

        # Track which footprints we've warned about
        warned_refs: set[str] = set()

        for net in netlist.nets:
            # Skip the empty net (net 0)
            if not net.name:
                continue

            for node in net.nodes:
                ref = node.reference
                pin = node.pin

                # Resolve schematic pin number to footprint pad number
                if pin_to_pad_map is not None:
                    pad_number = pin_to_pad_map.get((ref, pin), pin)
                else:
                    pad_number = pin

                # Check if footprint exists
                fp = self.get_footprint(ref)
                if not fp:
                    if ref not in warned_refs:
                        stats["missing_footprints"].append(ref)
                        warned_refs.add(ref)
                    continue

                # Assign net to pad using the resolved pad number
                if self.assign_net_to_footprint_pad(ref, pad_number, net.name):
                    stats["assigned"].append(f"{ref}.{pad_number}")
                else:
                    stats["missing_pads"].append(f"{ref}.{pin}")

        return stats

    @property
    def path(self) -> Path | None:
        """Path to the PCB file (if loaded from file or saved).

        This is used by export methods to locate the PCB file for kicad-cli.
        Returns None if the PCB was created in memory and never saved.
        """
        return self._path

    def strip_traces(
        self,
        *,
        nets: list[str] | None = None,
        layers: list[str] | None = None,
        keep_zones: bool = True,
        exclude_power: bool = False,
        power_pattern: re.Pattern[str] | None = None,
        remove_orphan_vias: bool = False,
        region: tuple[float, float, float, float] | None = None,
    ) -> dict[str, int]:
        """Remove trace segments and vias from the PCB.

        This method strips routing from the PCB while preserving component
        placement, zones (optionally), and other board elements. Useful for
        re-routing a board from scratch with different routing strategies
        or design rules.

        Args:
            nets: Optional list of net names to strip. If None, strips all nets.
                  When specified, only segments/vias belonging to these nets
                  are removed.
            layers: Optional list of layer names to strip (e.g. ``["In1.Cu"]``).
                    When provided, only segments on the listed layers are
                    candidates for removal.  Vias are removed only when ALL of
                    their layers are in the strip set.  When both *nets* and
                    *layers* are given they are ANDed together.
            keep_zones: If True (default), preserve copper pour zones.
                        If False, remove zones as well.
            exclude_power: If True, power/ground nets are never stripped
                           even when they match other filters.  Defaults to
                           ``False`` for backward compatibility; the CLI
                           defaults to excluding power nets.
            power_pattern: Optional compiled regex overriding the built-in
                           power-net heuristic used when *exclude_power* is
                           ``True``.
            remove_orphan_vias: If True, remove vias that no longer connect
                                to any remaining segment on either of their
                                layers after layer-filtered stripping.
                                Only meaningful when *layers* is specified.
            region: Optional ``(x1, y1, x2, y2)`` bounding box (board-relative
                    mm coordinates) that spatially bounds the strip.  When
                    given, only geometry *inside* the box is removed and the
                    filter is ANDed with *nets* / *layers* / *exclude_power*:

                    * A **via** is stripped only when its ``(at x y)`` point is
                      inside the box.
                    * A **segment** fully inside the box is removed; a segment
                      fully outside is kept unchanged.  A segment with exactly
                      one endpoint inside is *clipped* at the box boundary --
                      the inside portion is removed and the outside portion is
                      kept as a shortened segment (see ``segments_clipped`` in
                      the returned stats).  A segment with both endpoints
                      outside is kept unchanged even if its span happens to
                      cross the box (a "skip-and-report" fallback -- these are
                      counted in ``segments_boundary_skipped``), because
                      splitting a segment into two outside pieces would leave a
                      dangling gap with no via/pad termination inside the box.
                    * **Zones** are polygons; region-based zone removal only
                      triggers (with ``keep_zones=False``) when the zone's
                      entire polygon is contained in the box (conservative --
                      no polygon clipping is performed).

                    The box is normalized internally, so inverted/degenerate
                    coordinates are tolerated by this method; the CLI validates
                    and rejects ``x1 >= x2`` / ``y1 >= y2`` before calling.

        Returns:
            Dictionary with counts of removed elements:
            - "segments": Number of trace segments removed (includes the
              inside portion of clipped segments)
            - "vias": Number of vias removed
            - "zones": Number of zones removed (0 if keep_zones=True)
            - "segments_clipped": Number of boundary-crossing segments that
              were clipped to their outside portion (0 when *region* is None)
            - "segments_boundary_skipped": Number of segments spanning the box
              with both endpoints outside that were left untouched (0 when
              *region* is None)

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> stats = pcb.strip_traces()
            >>> print(f"Removed {stats['segments']} segments and {stats['vias']} vias")
            >>> pcb.save("board-stripped.kicad_pcb")

            # Strip only specific nets
            >>> stats = pcb.strip_traces(nets=["GND", "VCC"])

            # Strip everything including zones
            >>> stats = pcb.strip_traces(keep_zones=False)

            # Strip only inner-layer signal traces (power excluded by default)
            >>> stats = pcb.strip_traces(layers=["In1.Cu", "In2.Cu"])
        """
        # Build set of net numbers to strip (if filtering by net name)
        net_numbers_to_strip: set[int] | None = None
        if nets is not None:
            net_numbers_to_strip = set()
            for net_name in nets:
                for net_num, net in self._nets.items():
                    if net.name == net_name:
                        net_numbers_to_strip.add(net_num)
                        break

        # Build layer set for fast membership tests
        layer_set: set[str] | None = None
        if layers is not None:
            layer_set = set(layers)

        # Normalize the region bbox (board-relative coords) so x1<x2, y1<y2.
        # SExp start/end/at nodes store sheet-absolute coords (board-relative +
        # board_origin), so we convert them to board-relative before comparing.
        region_box: tuple[float, float, float, float] | None = None
        if region is not None:
            rx1, ry1, rx2, ry2 = region
            region_box = (min(rx1, rx2), min(ry1, ry2), max(rx1, rx2), max(ry1, ry2))
        ox_region, oy_region = self._board_origin

        def _point_in_region(abs_x: float, abs_y: float) -> bool:
            """True if a sheet-absolute point falls inside the region box."""
            if region_box is None:
                return True
            rel_x = abs_x - ox_region
            rel_y = abs_y - oy_region
            x1, y1, x2, y2 = region_box
            return x1 <= rel_x <= x2 and y1 <= rel_y <= y2

        def _clip_point_to_region(
            inside: tuple[float, float], outside: tuple[float, float]
        ) -> tuple[float, float]:
            """Intersect the inside->outside segment with the region boundary.

            Both points are sheet-absolute.  Returns the sheet-absolute point
            on the box boundary (the closest crossing to *inside*).  The
            returned point becomes the new terminal of the kept outside piece.
            """
            assert region_box is not None
            x1, y1, x2, y2 = region_box
            ix, iy = inside[0] - ox_region, inside[1] - oy_region
            oxp, oyp = outside[0] - ox_region, outside[1] - oy_region
            dx = oxp - ix
            dy = oyp - iy
            # Find the smallest t in (0, 1] where the segment exits the box.
            t_best = 1.0
            if dx > 0:
                t_best = min(t_best, (x2 - ix) / dx)
            elif dx < 0:
                t_best = min(t_best, (x1 - ix) / dx)
            if dy > 0:
                t_best = min(t_best, (y2 - iy) / dy)
            elif dy < 0:
                t_best = min(t_best, (y1 - iy) / dy)
            t_best = max(0.0, min(1.0, t_best))
            cx = ix + dx * t_best
            cy = iy + dy * t_best
            # Convert back to sheet-absolute coordinates.
            return (cx + ox_region, cy + oy_region)

        # Build set of power-net numbers for exclusion
        power_net_numbers: set[int] = set()
        if exclude_power:
            for net_num, net in self._nets.items():
                if _is_power_net(net.name, power_pattern):
                    power_net_numbers.add(net_num)

        def _net_num_from_child(child: SExp) -> int:
            net_node = child.find("net")
            if net_node:
                return net_node.get_int(0) or 0
            return 0

        def _should_strip_net(net_num: int) -> bool:
            """Return True if *net_num* passes the net filter."""
            if net_numbers_to_strip is not None and net_num not in net_numbers_to_strip:
                return False
            if exclude_power and net_num in power_net_numbers:
                return False
            return True

        removed_segments = 0
        removed_vias = 0
        removed_zones = 0
        clipped_segments = 0
        boundary_skipped_segments = 0

        # Track clipped-segment geometry so the parsed self._segments view can
        # be updated in lock-step with the S-expression tree below.  Maps the
        # segment's uuid -> new board-relative (start, end) endpoints.
        clipped_geometry: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {}

        # Filter S-expression children to remove segments, vias, and optionally zones
        new_children = []
        for child in self._sexp.children:
            should_remove = False

            if child.name == "segment":
                net_num = _net_num_from_child(child)
                passes_net = _should_strip_net(net_num)
                passes_layer = True
                if passes_net and layer_set is not None:
                    layer_node = child.find("layer")
                    seg_layer = (layer_node.get_string(0) or "") if layer_node else ""
                    passes_layer = seg_layer in layer_set

                if passes_net and passes_layer:
                    if region_box is None:
                        # No spatial bound: existing whole-segment behavior.
                        should_remove = True
                    else:
                        # Spatially bounded: classify by endpoint membership.
                        start_node = child.find("start")
                        end_node = child.find("end")
                        if start_node and end_node:
                            sx = start_node.get_float(0) or 0.0
                            sy = start_node.get_float(1) or 0.0
                            ex = end_node.get_float(0) or 0.0
                            ey = end_node.get_float(1) or 0.0
                            start_in = _point_in_region(sx, sy)
                            end_in = _point_in_region(ex, ey)
                            if start_in and end_in:
                                # Fully inside the box — remove entirely.
                                should_remove = True
                            elif start_in != end_in:
                                # Boundary-crossing — clip to the outside piece.
                                inside_pt = (sx, sy) if start_in else (ex, ey)
                                outside_pt = (ex, ey) if start_in else (sx, sy)
                                clip_pt = _clip_point_to_region(inside_pt, outside_pt)
                                # Rewrite the surviving segment: the outside
                                # endpoint stays, the inside endpoint moves to
                                # the region boundary.
                                if start_in:
                                    start_node.set_value(0, clip_pt[0])
                                    start_node.set_value(1, clip_pt[1])
                                    abs_start = clip_pt
                                    abs_end = (ex, ey)
                                else:
                                    end_node.set_value(0, clip_pt[0])
                                    end_node.set_value(1, clip_pt[1])
                                    abs_start = (sx, sy)
                                    abs_end = clip_pt
                                clipped_segments += 1
                                uuid_node = child.find("uuid")
                                seg_uuid = uuid_node.get_string(0) if uuid_node else None
                                if seg_uuid:
                                    # Record board-relative endpoints (subtract
                                    # board origin) for the parsed-view sync.
                                    clipped_geometry[seg_uuid] = (
                                        (abs_start[0] - ox_region, abs_start[1] - oy_region),
                                        (abs_end[0] - ox_region, abs_end[1] - oy_region),
                                    )
                            else:
                                # Both endpoints outside the box.  The span may
                                # still cross the box, but splitting it would
                                # leave a dangling gap with no termination
                                # inside; per the documented policy we skip and
                                # report it instead of silently mangling copper.
                                if _segment_span_intersects_region(
                                    (sx - ox_region, sy - oy_region),
                                    (ex - ox_region, ey - oy_region),
                                    region_box,
                                ):
                                    boundary_skipped_segments += 1

                if should_remove:
                    removed_segments += 1

            elif child.name == "via":
                net_num = _net_num_from_child(child)
                if _should_strip_net(net_num):
                    if layer_set is not None:
                        # Remove via only if ALL its layers are in the strip set
                        layers_node = child.find("layers")
                        if layers_node:
                            via_layers = [
                                layers_node.get_string(i) or ""
                                for i in range(len(layers_node.values))
                                if isinstance(layers_node.values[i], str)
                            ]
                            if via_layers and all(vl in layer_set for vl in via_layers):
                                should_remove = True
                    elif net_numbers_to_strip is not None:
                        should_remove = True
                    else:
                        should_remove = True

                # A via is a point: it is only stripped when its (at x y)
                # position falls inside the region box (ANDed with the above).
                if should_remove and region_box is not None:
                    at_node = child.find("at")
                    if at_node is None:
                        should_remove = False
                    else:
                        vx = at_node.get_float(0) or 0.0
                        vy = at_node.get_float(1) or 0.0
                        should_remove = _point_in_region(vx, vy)

                if should_remove:
                    removed_vias += 1

            elif child.name == "zone" and not keep_zones:
                net_num = _net_num_from_child(child)
                if _should_strip_net(net_num):
                    if layer_set is not None:
                        # Only remove zones on specified layers
                        layer_node = child.find("layer")
                        if layer_node:
                            zone_layer = layer_node.get_string(0) or ""
                            if zone_layer in layer_set:
                                should_remove = True
                    elif net_numbers_to_strip is not None:
                        should_remove = True
                    else:
                        should_remove = True

                # Region: zones are polygons, not lines.  Phase 1 does NOT clip
                # zone polygons; a zone is only removed when its entire boundary
                # polygon is contained in the region box (conservative).
                if should_remove and region_box is not None:
                    poly_node = child.find("polygon")
                    pts_node = poly_node.find("pts") if poly_node else None
                    if pts_node is None:
                        should_remove = False
                    else:
                        xy_nodes = pts_node.find_all("xy")
                        if not xy_nodes:
                            should_remove = False
                        else:
                            should_remove = all(
                                _point_in_region(xy.get_float(0) or 0.0, xy.get_float(1) or 0.0)
                                for xy in xy_nodes
                            )

                if should_remove:
                    removed_zones += 1

            if not should_remove:
                new_children.append(child)

        # Update the S-expression tree
        self._sexp.children = new_children

        # --- Orphan via removal ------------------------------------------------
        # After stripping layer-filtered segments, detect vias that no longer
        # connect to any remaining segment on either of their layers.
        removed_orphan_vias = 0
        if remove_orphan_vias and layer_set is not None:
            # Build a set of (position, layer) pairs from remaining segments
            remaining_endpoints: set[tuple[float, float, str]] = set()
            for child in self._sexp.children:
                if child.name == "segment":
                    layer_node = child.find("layer")
                    seg_layer = (layer_node.get_string(0) or "") if layer_node else ""
                    start_node = child.find("start")
                    end_node = child.find("end")
                    if start_node:
                        sx = start_node.get_float(0) or 0.0
                        sy = start_node.get_float(1) or 0.0
                        remaining_endpoints.add((round(sx, 4), round(sy, 4), seg_layer))
                    if end_node:
                        ex = end_node.get_float(0) or 0.0
                        ey = end_node.get_float(1) or 0.0
                        remaining_endpoints.add((round(ex, 4), round(ey, 4), seg_layer))

            new_children2 = []
            for child in self._sexp.children:
                if child.name == "via":
                    at_node = child.find("at")
                    layers_node = child.find("layers")
                    if at_node and layers_node:
                        vx = round(at_node.get_float(0) or 0.0, 4)
                        vy = round(at_node.get_float(1) or 0.0, 4)
                        via_layers = [
                            layers_node.get_string(i) or ""
                            for i in range(len(layers_node.values))
                            if isinstance(layers_node.values[i], str)
                        ]
                        # Via is orphan if NONE of its layers have a segment endpoint at its position
                        connected = any((vx, vy, vl) in remaining_endpoints for vl in via_layers)
                        if not connected:
                            removed_orphan_vias += 1
                            removed_vias += 1
                            continue
                new_children2.append(child)
            self._sexp.children = new_children2

        # Update internal state to reflect the changes
        def _seg_matches(seg: Segment) -> bool:
            """Return True if segment should be KEPT."""
            if exclude_power and seg.net_number in power_net_numbers:
                return True
            if net_numbers_to_strip is not None and seg.net_number not in net_numbers_to_strip:
                return True
            if layer_set is not None and seg.layer not in layer_set:
                return True
            return False

        def _via_matches(via: Via) -> bool:
            """Return True if via should be KEPT."""
            if exclude_power and via.net_number in power_net_numbers:
                return True
            if net_numbers_to_strip is not None and via.net_number not in net_numbers_to_strip:
                return True
            if layer_set is not None and not all(vl in layer_set for vl in via.layers):
                return True
            # Region: keep the via if its point is outside the box.
            if region_box is not None:
                x1, y1, x2, y2 = region_box
                vx, vy = via.position
                if not (x1 <= vx <= x2 and y1 <= vy <= y2):
                    return True
            return False

        def _zone_matches(zone: Zone) -> bool:
            """Return True if zone should be KEPT."""
            if exclude_power and zone.net_number in power_net_numbers:
                return True
            if net_numbers_to_strip is not None and zone.net_number not in net_numbers_to_strip:
                return True
            if layer_set is not None and zone.layer not in layer_set:
                return True
            # Region: keep the zone unless its whole polygon is inside the box.
            if region_box is not None:
                x1, y1, x2, y2 = region_box
                poly = zone.polygon or []
                if not poly or not all(x1 <= px <= x2 and y1 <= py <= y2 for px, py in poly):
                    return True
            return False

        def _region_keeps_segment(seg: Segment) -> bool:
            """Return True if *region* alone would preserve (not fully strip) seg.

            A segment is fully removed only when both endpoints are inside the
            box.  Crossing segments are clipped (their endpoints are rewritten
            below), so they are "kept" here in the sense of surviving the list.
            """
            if region_box is None:
                return False
            x1, y1, x2, y2 = region_box
            start_in = x1 <= seg.start[0] <= x2 and y1 <= seg.start[1] <= y2
            end_in = x1 <= seg.end[0] <= x2 and y1 <= seg.end[1] <= y2
            return not (start_in and end_in)

        def _seg_matches_with_region(seg: Segment) -> bool:
            """Return True if the segment should be KEPT (region-aware)."""
            if _seg_matches(seg):
                return True
            # Passed the net/layer/power filters — apply the spatial bound.
            if region_box is not None and _region_keeps_segment(seg):
                return True
            return False

        # Rewrite clipped segments' parsed endpoints (board-relative) so the
        # parsed view matches the S-expression tree.
        if clipped_geometry:
            for seg in self._segments:
                new_geom = clipped_geometry.get(seg.uuid)
                if new_geom is not None:
                    seg.start, seg.end = new_geom

        # Apply filtering helpers
        has_any_filter = (
            net_numbers_to_strip is not None
            or layer_set is not None
            or exclude_power
            or region_box is not None
        )
        if has_any_filter:
            self._segments = [seg for seg in self._segments if _seg_matches_with_region(seg)]
            self._vias = [via for via in self._vias if _via_matches(via)]
            if not keep_zones:
                self._zones = [zone for zone in self._zones if _zone_matches(zone)]
        else:
            self._segments = []
            self._vias = []
            if not keep_zones:
                self._zones = []

        # Handle orphan via removal in internal state
        if remove_orphan_vias and layer_set is not None and removed_orphan_vias > 0:
            remaining_ep_set: set[tuple[float, float, str]] = set()
            for seg in self._segments:
                remaining_ep_set.add((round(seg.start[0], 4), round(seg.start[1], 4), seg.layer))
                remaining_ep_set.add((round(seg.end[0], 4), round(seg.end[1], 4), seg.layer))
            self._vias = [
                v
                for v in self._vias
                if any(
                    (round(v.position[0], 4), round(v.position[1], 4), vl) in remaining_ep_set
                    for vl in v.layers
                )
            ]

        return {
            "segments": removed_segments,
            "vias": removed_vias,
            "zones": removed_zones,
            "segments_clipped": clipped_segments,
            "segments_boundary_skipped": boundary_skipped_segments,
        }

    def save(self, path: str | Path | None = None) -> None:
        """
        Save the PCB to a file.

        Args:
            path: Path to save to (.kicad_pcb). If None, uses the original
                  path from load() or the last save location.

        Raises:
            ValueError: If no path provided and PCB has no stored path
        """
        if path is None:
            if self._path is None:
                raise ValueError(
                    "No path specified and PCB has no stored path. "
                    "Provide a path or use PCB.load() to load from a file."
                )
            path = self._path
        else:
            path = Path(path)
            self._path = path

        save_pcb(self._sexp, path)

    # =========================================================================
    # Manufacturing Export Methods
    # =========================================================================

    def export_gerbers(
        self,
        output_dir: str | Path,
        *,
        manufacturer: str = "jlcpcb",
        layers: list[str] | None = None,
        include_drill: bool = True,
        create_zip: bool = False,
    ) -> Path:
        """
        Export Gerber files for PCB fabrication.

        Uses kicad-cli to generate Gerber files with manufacturer-specific settings.

        Args:
            output_dir: Directory for output files
            manufacturer: Manufacturer preset ("jlcpcb", "pcbway", "oshpark")
            layers: Specific layers to export (default: all copper + required layers)
            include_drill: Include drill files (default: True)
            create_zip: Create a zip archive of all files (default: False)

        Returns:
            Path to output directory (or zip file if create_zip=True)

        Raises:
            ValueError: If PCB has no stored path (save first)
            ExportError: If kicad-cli fails

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> pcb.export_gerbers("./gerbers", manufacturer="jlcpcb")
            >>> # Or with zip output
            >>> pcb.export_gerbers("./output", manufacturer="jlcpcb", create_zip=True)
        """
        from ..export import GerberConfig, GerberExporter

        pcb_path = self._require_path("export_gerbers")

        exporter = GerberExporter(pcb_path)

        if manufacturer.lower() in ("jlcpcb", "pcbway", "oshpark"):
            result = exporter.export_for_manufacturer(
                manufacturer.lower(),
                output_dir,
            )
        else:
            config = GerberConfig(
                output_dir=Path(output_dir),
                layers=layers or [],
                generate_drill=include_drill,
                create_zip=create_zip,
            )
            result = exporter.export(config, output_dir)

        return result

    def export_drill(
        self,
        output_dir: str | Path,
        *,
        format: str = "excellon",
        units: str = "mm",
        merge_pth_npth: bool = False,
    ) -> Path:
        """
        Export drill files (Excellon format).

        Args:
            output_dir: Directory for output files
            format: Drill format ("excellon" or "gerber_x2")
            units: Units ("mm" or "inch")
            merge_pth_npth: Merge plated and non-plated holes (default: False)

        Returns:
            Path to output directory containing drill files

        Raises:
            ValueError: If PCB has no stored path
            ExportError: If kicad-cli fails

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> pcb.export_drill("./gerbers", merge_pth_npth=False)
        """
        from ..export import GerberConfig, GerberExporter

        pcb_path = self._require_path("export_drill")

        config = GerberConfig(
            output_dir=Path(output_dir),
            generate_drill=True,
            drill_format=format,
            merge_pth_npth=merge_pth_npth,
            # Don't generate gerbers, only drill
            layers=[],
            include_edge_cuts=False,
            include_silkscreen=False,
            include_soldermask=False,
            include_solderpaste=False,
        )

        exporter = GerberExporter(pcb_path)
        return exporter.export(config, output_dir)

    def export_bom(
        self,
        output: str | Path,
        *,
        schematic_path: str | Path | None = None,
        format: str = "csv",
        manufacturer: str = "generic",
    ) -> Path:
        """
        Export Bill of Materials (BOM).

        Generates a BOM from the associated schematic file.

        Args:
            output: Output file path
            schematic_path: Path to schematic file. If not provided, looks for
                           a .kicad_sch file with the same name as the PCB.
            format: Output format ("csv", "jlcpcb", "pcbway", "seeed")
            manufacturer: Manufacturer format preset

        Returns:
            Path to generated BOM file

        Raises:
            ValueError: If schematic not found
            ExportError: If BOM generation fails

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> pcb.export_bom("bom.csv")
            >>> # JLCPCB format
            >>> pcb.export_bom("bom_jlcpcb.csv", format="jlcpcb")
        """
        from ..export import BOMExportConfig
        from ..export import export_bom as _export_bom
        from ..schema.bom import extract_bom

        # Find schematic
        if schematic_path is None:
            pcb_path = self._require_path("export_bom")
            schematic_path = pcb_path.with_suffix(".kicad_sch")
            if not schematic_path.exists():
                raise ValueError(
                    f"Schematic not found at {schematic_path}. Provide schematic_path explicitly."
                )
        else:
            schematic_path = Path(schematic_path)
            if not schematic_path.exists():
                raise ValueError(f"Schematic not found: {schematic_path}")

        # Extract BOM from schematic
        bom = extract_bom(schematic_path)
        items = bom.items

        # Determine manufacturer format
        mfr = manufacturer.lower()
        if format.lower() in ("jlcpcb", "pcbway", "seeed"):
            mfr = format.lower()

        config = BOMExportConfig()
        bom_csv = _export_bom(items, mfr, config)

        # Write to file
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(bom_csv)

        return output_path

    def export_placement(
        self,
        output: str | Path,
        *,
        format: str = "csv",
        manufacturer: str = "generic",
        side: str | None = None,
    ) -> Path:
        """
        Export pick-and-place (CPL) file for SMT assembly.

        Args:
            output: Output file path
            format: Output format ("csv", "jlcpcb", "pcbway")
            manufacturer: Manufacturer format preset
            side: Export only "top" or "bottom" side (default: both)

        Returns:
            Path to generated placement file

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> pcb.export_placement("placement.csv")
            >>> # JLCPCB format
            >>> pcb.export_placement("cpl_jlcpcb.csv", format="jlcpcb")
        """
        from ..export import export_pnp as _export_pnp

        footprints = list(self.footprints)

        # Filter by side if specified
        if side:
            layer = "F.Cu" if side.lower() == "top" else "B.Cu"
            footprints = [fp for fp in footprints if fp.layer == layer]

        # Determine manufacturer format
        mfr = manufacturer.lower()
        if format.lower() in ("jlcpcb", "pcbway"):
            mfr = format.lower()

        # Pass config=None so the formatter resolves the effective config —
        # the single source of truth (issues #3616/#3618).  Synthesizing a
        # bare PnPExportConfig() here would defeat manufacturer defaults such
        # as JLCPCB's exclude_tht=True, shipping THT rows in the CPL.
        pnp_csv = _export_pnp(footprints, mfr, config=None)

        # Write to file
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(pnp_csv)

        return output_path

    def export_manufacturing(
        self,
        output_dir: str | Path,
        *,
        manufacturer: str = "jlcpcb",
        schematic_path: str | Path | None = None,
        include_assembly: bool = True,
        create_zip: bool = True,
    ) -> dict[str, str | None]:
        """
        Export complete manufacturing package.

        Generates all files needed for PCB fabrication and assembly:
        - Gerber files (copper, silkscreen, solder mask, outline)
        - Drill files (PTH and NPTH)
        - BOM (if include_assembly=True)
        - Pick-and-place/CPL (if include_assembly=True)

        Args:
            output_dir: Directory for output files
            manufacturer: Target manufacturer ("jlcpcb", "pcbway", "oshpark", "seeed")
            schematic_path: Path to schematic (required for BOM/assembly)
            include_assembly: Include BOM and placement files (default: True)
            create_zip: Create zip archive ready for upload (default: True)

        Returns:
            Dictionary with paths to generated files:
            {
                "gerbers": "./output/gerbers.zip",
                "drill": "./output/gerbers.zip",  # Included in gerbers
                "bom": "./output/bom.csv",
                "placement": "./output/cpl.csv",
                "zip": "./output/manufacturing.zip"
            }

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> result = pcb.export_manufacturing("./manufacturing", manufacturer="jlcpcb")
            >>> print(f"Upload {result['zip']} to JLCPCB")
        """
        from ..export import AssemblyConfig, AssemblyPackage

        pcb_path = self._require_path("export_manufacturing")
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        # Find schematic if needed
        if include_assembly:
            if schematic_path is None:
                schematic_path = pcb_path.with_suffix(".kicad_sch")
            else:
                schematic_path = Path(schematic_path)

            if not schematic_path.exists():
                include_assembly = False

        # Configure and export
        config = AssemblyConfig(
            output_dir=output_path,
            include_bom=include_assembly,
            include_pnp=include_assembly,
            include_gerbers=True,
        )

        pkg = AssemblyPackage(
            pcb_path=pcb_path,
            schematic_path=schematic_path if include_assembly else None,
            manufacturer=manufacturer,
            config=config,
        )
        pkg_result = pkg.export(output_path)

        # Build result dictionary
        result: dict[str, str | None] = {
            "gerbers": str(pkg_result.gerber_path) if pkg_result.gerber_path else None,
            "drill": str(pkg_result.gerber_path) if pkg_result.gerber_path else None,
            "bom": str(pkg_result.bom_path) if pkg_result.bom_path else None,
            "placement": str(pkg_result.pnp_path) if pkg_result.pnp_path else None,
        }

        # Create combined zip if requested
        if create_zip:
            import zipfile

            zip_path = output_path / f"{manufacturer}_manufacturing.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for file_path in output_path.iterdir():
                    if file_path.is_file() and file_path != zip_path:
                        zf.write(file_path, file_path.name)
                # Include gerber subdirectory if exists
                gerber_dir = output_path / "gerbers"
                if gerber_dir.is_dir():
                    for file_path in gerber_dir.iterdir():
                        if file_path.is_file():
                            zf.write(file_path, f"gerbers/{file_path.name}")

            result["zip"] = str(zip_path)
        else:
            result["zip"] = None

        return result

    def export_gerbers_zip(
        self,
        output: str | Path,
        *,
        manufacturer: str = "jlcpcb",
        include_drill: bool = True,
    ) -> Path:
        """
        Export Gerbers and drill files as a single zip archive.

        Convenience method for quick export of fabrication files ready
        for upload to PCB manufacturers.

        Args:
            output: Output zip file path
            manufacturer: Manufacturer preset ("jlcpcb", "pcbway", "oshpark")
            include_drill: Include drill files in zip (default: True)

        Returns:
            Path to generated zip file

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> pcb.export_gerbers_zip("gerbers.zip", manufacturer="jlcpcb")
        """
        import tempfile

        from ..export import GerberExporter

        pcb_path = self._require_path("export_gerbers_zip")
        output_path = Path(output)

        # Export to temp directory, then zip
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            exporter = GerberExporter(pcb_path)
            exporter.export_for_manufacturer(manufacturer.lower(), temp_path)

            # Create zip
            import zipfile

            output_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for file_path in temp_path.iterdir():
                    if file_path.is_file():
                        zf.write(file_path, file_path.name)

        return output_path

    def _require_path(self, method_name: str) -> Path:
        """Ensure PCB has a stored path for export operations."""
        if self._path is None:
            raise ValueError(
                f"{method_name}() requires a PCB file path. "
                "Either load the PCB with PCB.load() or save it first with pcb.save()."
            )
        return self._path

    def import_from_netlist(
        self,
        netlist,
        placement_start: tuple[float, float] = (10.0, 10.0),
        placement_spacing: float = 15.0,
        columns: int = 10,
    ) -> dict[str, list[str]]:
        """
        Import footprints and assign nets from a netlist.

        Adds all footprints referenced in the netlist to the PCB and
        assigns net connections to their pads. Footprints are placed
        in a grid pattern starting from the placement_start position.

        Coordinates are relative to the board origin. For centered boards
        (created with center=True, the default), the board origin is offset
        from the drawing sheet origin. This method automatically applies
        that offset so users can specify positions relative to the board
        corner (0, 0 = top-left of board outline).

        Args:
            netlist: A Netlist object containing components and connectivity
            placement_start: Starting (x, y) position for footprint placement,
                            relative to board origin
            placement_spacing: Spacing between footprints in mm
            columns: Number of footprints per row in the grid

        Returns:
            Dictionary with statistics:
            - "footprints_added": List of references successfully added
            - "footprints_skipped": List of references skipped (no footprint spec)
            - "footprints_failed": List of references that failed to add
            - "nets_assigned": Number of pad-net assignments made
            - "nets_failed": List of failed pad-net assignments (format: "REF.PIN")

        Example:
            >>> from kicad_tools.operations.netlist import Netlist
            >>> netlist = Netlist.load("project.kicad_net")
            >>> pcb = PCB.create(width=100, height=100)
            >>> result = pcb.import_from_netlist(netlist)
            >>> print(f"Added {len(result['footprints_added'])} footprints")
        """
        stats: dict[str, list[str]] = {
            "footprints_added": [],
            "footprints_skipped": [],
            "footprints_failed": [],
            "nets_assigned": [],
            "nets_failed": [],
        }

        # Track grid position for footprint placement
        x, y = placement_start
        col = 0

        # Add footprints from netlist components
        for comp in netlist.components:
            ref = comp.reference
            value = comp.value
            footprint_id = comp.footprint

            # Skip components without footprint specification
            if not footprint_id:
                stats["footprints_skipped"].append(ref)
                continue

            # Skip if footprint already exists
            if self.get_footprint(ref):
                stats["footprints_skipped"].append(ref)
                continue

            try:
                self.add_footprint(
                    library_id=footprint_id,
                    reference=ref,
                    x=x,
                    y=y,
                    rotation=0.0,
                    layer="F.Cu",
                    value=value,
                )
                stats["footprints_added"].append(ref)

                # Advance to next grid position
                col += 1
                if col >= columns:
                    col = 0
                    x = placement_start[0]
                    y += placement_spacing
                else:
                    x += placement_spacing

            except (FileNotFoundError, ValueError) as e:
                # Footprint not found in library or invalid
                stats["footprints_failed"].append(f"{ref}: {e}")

        # Assign nets to pads
        net_result = self.assign_nets_from_netlist(netlist)
        stats["nets_assigned"] = net_result["assigned"]
        stats["nets_failed"] = net_result["missing_pads"]

        return stats

    def import_from_schematic(
        self,
        schematic_path: str | Path,
        placement_start: tuple[float, float] = (10.0, 10.0),
        placement_spacing: float = 15.0,
        columns: int = 10,
    ) -> dict[str, list[str]]:
        """
        Import footprints and assign nets from a schematic file.

        Exports a netlist from the schematic using kicad-cli, then imports
        all footprints and assigns net connections. This is the programmatic
        equivalent of KiCad's "Update PCB from Schematic" (F8) operation.

        Coordinates are relative to the board origin. For centered boards
        (created with center=True, the default), the board origin is offset
        from the drawing sheet origin. This method automatically applies
        that offset so users can specify positions relative to the board
        corner (0, 0 = top-left of board outline).

        Args:
            schematic_path: Path to the .kicad_sch schematic file
            placement_start: Starting (x, y) position for footprint placement,
                            relative to board origin
            placement_spacing: Spacing between footprints in mm
            columns: Number of footprints per row in the grid

        Returns:
            Dictionary with statistics (same as import_from_netlist)

        Raises:
            FileNotFoundError: If schematic file or kicad-cli not found
            RuntimeError: If netlist export fails

        Example:
            >>> pcb = PCB.create(width=160, height=100)
            >>> result = pcb.import_from_schematic("project.kicad_sch")
            >>> print(f"Added {len(result['footprints_added'])} footprints")
            >>> pcb.save("project.kicad_pcb")
        """
        from ..operations.netlist import export_netlist

        # Export netlist from schematic
        netlist = export_netlist(schematic_path)

        # Import using the netlist
        return self.import_from_netlist(
            netlist,
            placement_start=placement_start,
            placement_spacing=placement_spacing,
            columns=columns,
        )

    @classmethod
    def from_schematic(
        cls,
        schematic_path: str | Path,
        width: float = 100.0,
        height: float = 100.0,
        layers: int = 2,
        placement_start: tuple[float, float] = (10.0, 10.0),
        placement_spacing: float = 15.0,
        columns: int = 10,
    ) -> tuple[PCB, dict[str, list[str]]]:
        """
        Create a new PCB from a schematic file.

        Creates a blank PCB with the specified dimensions, then imports
        all footprints and net assignments from the schematic.

        Coordinates are relative to the board origin. For centered boards
        (the default), the board origin is offset from the drawing sheet origin.
        This method automatically applies that offset so users can specify
        positions relative to the board corner (0, 0 = top-left of board outline).

        Args:
            schematic_path: Path to the .kicad_sch schematic file
            width: Board width in mm
            height: Board height in mm
            layers: Number of copper layers (2 or 4)
            placement_start: Starting (x, y) position for footprint placement,
                            relative to board origin
            placement_spacing: Spacing between footprints in mm
            columns: Number of footprints per row in the grid

        Returns:
            Tuple of (PCB instance, import statistics dict)

        Raises:
            FileNotFoundError: If schematic file or kicad-cli not found
            RuntimeError: If netlist export fails
            ValueError: If layers is not 2 or 4

        Example:
            >>> pcb, stats = PCB.from_schematic(
            ...     "project.kicad_sch",
            ...     width=160,
            ...     height=100,
            ...     layers=4
            ... )
            >>> print(f"Created PCB with {len(stats['footprints_added'])} components")
            >>> pcb.save("project.kicad_pcb")
        """
        # Create blank PCB
        pcb = cls.create(width=width, height=height, layers=layers)

        # Import from schematic
        stats = pcb.import_from_schematic(
            schematic_path,
            placement_start=placement_start,
            placement_spacing=placement_spacing,
            columns=columns,
        )

        return pcb, stats

    # =========================================================================
    # Collision Detection and DRC Methods
    # =========================================================================

    def check_placement_collision(
        self,
        reference: str,
        x: float,
        y: float,
        rotation: float | None = None,
        *,
        clearance: float = 0.2,
        courtyard_margin: float = 0.25,
    ):
        """
        Check if placing a component at the given position would cause a collision.

        This temporarily updates the component's position in memory, checks for
        conflicts, then restores the original position.

        Args:
            reference: Reference designator of the component to check (e.g., "U1")
            x: Proposed X position in mm
            y: Proposed Y position in mm
            rotation: Proposed rotation in degrees (optional, uses current if None)
            clearance: Minimum pad clearance in mm (default: 0.2)
            courtyard_margin: Courtyard margin in mm (default: 0.25)

        Returns:
            CollisionResult with collision details if any, or no_collision if safe

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> result = pcb.check_placement_collision("U1", x=50, y=50)
            >>> if result.has_collision:
            ...     print(f"Would collide with: {result.other_ref}")
            ...     print(f"Clearance needed: {result.required_clearance}mm")
            ...     print(f"Actual clearance: {result.actual_clearance}mm")
        """
        from ..placement import CollisionResult, DesignRules, PlacementAnalyzer

        # Find the footprint
        fp = self.get_footprint(reference)
        if not fp:
            return CollisionResult(
                has_collision=False,
                message=f"Component {reference} not found",
            )

        # Save original position
        orig_x, orig_y = fp.position
        orig_rot = fp.rotation

        # Temporarily update position (in memory only)
        fp.position = (x, y)
        if rotation is not None:
            fp.rotation = rotation

        try:
            # Create analyzer and check conflicts
            analyzer = PlacementAnalyzer()
            rules = DesignRules(
                min_pad_clearance=clearance,
                courtyard_margin=courtyard_margin,
            )

            # Load this PCB's components
            analyzer._load_pcb_from_instance(self, courtyard_margin)

            # Check all pairs involving this component
            components = analyzer.get_components()
            target_comp = next((c for c in components if c.reference == reference), None)

            if not target_comp:
                return CollisionResult.no_collision()

            for other_comp in components:
                if other_comp.reference == reference:
                    continue

                # Check for conflicts between target and other
                conflicts = analyzer._check_pair((target_comp, other_comp), rules)

                if conflicts:
                    # Return the first conflict found
                    return CollisionResult.from_conflict(conflicts[0])

            return CollisionResult.no_collision()

        finally:
            # Restore original position
            fp.position = (orig_x, orig_y)
            fp.rotation = orig_rot

    def validate_placements(
        self,
        placements: dict[str, tuple[float, float, float]],
        *,
        clearance: float = 0.2,
        courtyard_margin: float = 0.25,
    ):
        """
        Validate a batch of proposed placements before committing.

        Checks all proposed placements for conflicts with each other and
        with existing components.

        Args:
            placements: Dictionary mapping reference to (x, y, rotation) tuples
                e.g., {"U1": (50, 50, 0), "C1": (52, 50, 90), ...}
            clearance: Minimum pad clearance in mm (default: 0.2)
            courtyard_margin: Courtyard margin in mm (default: 0.25)

        Returns:
            PlacementValidationResult with all detected issues

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> placements = {"U1": (50, 50, 0), "C1": (52, 50, 0)}
            >>> result = pcb.validate_placements(placements)
            >>> for issue in result.collisions:
            ...     print(f"{issue.ref1} <-> {issue.ref2}: {issue.violation_type}")
        """
        from ..placement import (
            DesignRules,
            PlacementAnalyzer,
            PlacementCollision,
            PlacementValidationResult,
        )

        # Save original positions
        original_positions: dict[str, tuple[float, float, float]] = {}
        for ref in placements:
            fp = self.get_footprint(ref)
            if fp:
                original_positions[ref] = (fp.position[0], fp.position[1], fp.rotation)

        # Apply proposed positions temporarily
        for ref, (x, y, rot) in placements.items():
            fp = self.get_footprint(ref)
            if fp:
                fp.position = (x, y)
                fp.rotation = rot

        try:
            # Run conflict analysis
            analyzer = PlacementAnalyzer()
            rules = DesignRules(
                min_pad_clearance=clearance,
                courtyard_margin=courtyard_margin,
            )

            analyzer._load_pcb_from_instance(self, courtyard_margin)
            conflicts = analyzer._find_conflicts_internal(rules)

            # Build result
            collisions = [PlacementCollision.from_conflict(c) for c in conflicts]

            return PlacementValidationResult(
                is_valid=len(collisions) == 0,
                total_placements=len(placements),
                collision_count=len(collisions),
                collisions=collisions,
            )

        finally:
            # Restore original positions
            for ref, (x, y, rot) in original_positions.items():
                fp = self.get_footprint(ref)
                if fp:
                    fp.position = (x, y)
                    fp.rotation = rot

    def run_drc(
        self,
        *,
        clearance: float = 0.2,
        courtyard_margin: float = 0.25,
        edge_clearance: float = 0.3,
        hole_to_hole: float = 0.5,
    ):
        """
        Run design rule check on the current PCB state.

        Checks for placement conflicts including:
        - Pad clearance violations
        - Courtyard overlaps
        - Edge clearance violations
        - Hole-to-hole violations

        Args:
            clearance: Minimum pad clearance in mm (default: 0.2)
            courtyard_margin: Courtyard margin in mm (default: 0.25)
            edge_clearance: Minimum edge clearance in mm (default: 0.3)
            hole_to_hole: Minimum hole-to-hole distance in mm (default: 0.5)

        Returns:
            DRCResult with all violations and summary counts

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> result = pcb.run_drc()
            >>> print(f"Clearance violations: {result.clearance_count}")
            >>> print(f"Courtyard overlaps: {result.courtyard_count}")
            >>> for violation in result.violations:
            ...     print(f"{violation.type}: {violation.description}")
        """
        from ..placement import (
            ConflictType,
            DesignRules,
            DRCResult,
            DRCViolation,
            PlacementAnalyzer,
        )

        analyzer = PlacementAnalyzer()
        rules = DesignRules(
            min_pad_clearance=clearance,
            courtyard_margin=courtyard_margin,
            min_edge_clearance=edge_clearance,
            min_hole_to_hole=hole_to_hole,
        )

        analyzer._load_pcb_from_instance(self, courtyard_margin)
        conflicts = analyzer._find_conflicts_internal(rules)

        # Convert to DRC violations and count by type
        violations = []
        clearance_count = 0
        courtyard_count = 0
        edge_count = 0
        hole_count = 0

        for conflict in conflicts:
            violations.append(DRCViolation.from_conflict(conflict))

            if conflict.type == ConflictType.PAD_CLEARANCE:
                clearance_count += 1
            elif conflict.type == ConflictType.COURTYARD_OVERLAP:
                courtyard_count += 1
            elif conflict.type == ConflictType.EDGE_CLEARANCE:
                edge_count += 1
            elif conflict.type == ConflictType.HOLE_TO_HOLE:
                hole_count += 1

        return DRCResult(
            passed=len(violations) == 0,
            violation_count=len(violations),
            clearance_count=clearance_count,
            courtyard_count=courtyard_count,
            edge_clearance_count=edge_count,
            hole_to_hole_count=hole_count,
            violations=violations,
        )

    def set_design_rules(
        self,
        clearance: float = 0.2,
        courtyard_clearance: float = 0.25,
        silkscreen_clearance: float = 0.15,
        edge_clearance: float = 0.3,
        hole_to_hole: float = 0.5,
    ):
        """
        Set design rules for collision detection and DRC.

        These rules are stored on the PCB instance and used as defaults
        for collision checking and DRC operations.

        Args:
            clearance: Minimum pad clearance in mm (default: 0.2)
            courtyard_clearance: Courtyard margin in mm (default: 0.25)
            silkscreen_clearance: Silkscreen clearance in mm (default: 0.15)
            edge_clearance: Minimum edge clearance in mm (default: 0.3)
            hole_to_hole: Minimum hole-to-hole distance in mm (default: 0.5)

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> pcb.set_design_rules(
            ...     clearance=0.2,
            ...     courtyard_clearance=0.25,
            ...     silkscreen_clearance=0.15
            ... )
            >>> # Now collision checks use these rules by default
            >>> result = pcb.run_drc()
        """
        from ..placement import DesignRules

        self._design_rules = DesignRules(
            min_pad_clearance=clearance,
            courtyard_margin=courtyard_clearance,
            min_edge_clearance=edge_clearance,
            min_hole_to_hole=hole_to_hole,
        )

    def place_footprint_safe(
        self,
        reference: str,
        x: float,
        y: float,
        rotation: float | None = None,
        *,
        min_clearance: float = 0.2,
        auto_adjust: bool = True,
        max_adjustment: float = 5.0,
    ) -> tuple[bool, tuple[float, float] | None, str]:
        """
        Place a footprint with automatic collision avoidance.

        Attempts to place the footprint at the given position. If a collision
        would occur and auto_adjust is True, tries to find a nearby position
        that avoids the collision.

        Args:
            reference: Reference designator of the component (e.g., "U1")
            x: Desired X position in mm
            y: Desired Y position in mm
            rotation: Rotation in degrees (optional)
            min_clearance: Minimum clearance to maintain in mm (default: 0.2)
            auto_adjust: If True, automatically adjust position to avoid collision
            max_adjustment: Maximum distance to adjust position in mm (default: 5.0)

        Returns:
            Tuple of:
            - success: True if placement succeeded (with or without adjustment)
            - final_position: (x, y) of final position, or None if failed
            - message: Description of what happened

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> success, pos, msg = pcb.place_footprint_safe(
            ...     "C1", x=50, y=50, min_clearance=0.2
            ... )
            >>> if success:
            ...     print(f"Placed at {pos}")
            >>> else:
            ...     print(f"Failed: {msg}")
        """
        fp = self.get_footprint(reference)
        if not fp:
            return False, None, f"Component {reference} not found"

        # Check if proposed position is clear
        result = self.check_placement_collision(reference, x, y, rotation, clearance=min_clearance)

        if not result.has_collision:
            # Position is clear, apply it
            self.update_footprint_position(reference, x, y, rotation)
            return True, (x, y), "Placed at requested position"

        if not auto_adjust:
            return (
                False,
                None,
                f"Collision with {result.other_ref}: {result.message}",
            )

        # Try to find a clear position nearby
        import math

        # Try positions in expanding circles
        for radius in [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0]:
            if radius > max_adjustment:
                break

            for angle in range(0, 360, 45):
                rad = math.radians(angle)
                test_x = x + radius * math.cos(rad)
                test_y = y + radius * math.sin(rad)

                test_result = self.check_placement_collision(
                    reference, test_x, test_y, rotation, clearance=min_clearance
                )

                if not test_result.has_collision:
                    self.update_footprint_position(reference, test_x, test_y, rotation)
                    return (
                        True,
                        (test_x, test_y),
                        f"Adjusted by {radius:.1f}mm to avoid collision with {result.other_ref}",
                    )

        return (
            False,
            None,
            f"Could not find clear position within {max_adjustment}mm",
        )
