"""
KiCad schematic paper-size selection.

Standard sheet sizes and helpers for auto-sizing a schematic's declared
paper to its content extent (issue #3530).  KiCad renders/prints only
what falls inside the declared sheet, so content placed beyond the paper
bounds is silently clipped in every faithful render (kicad-cli SVG/PDF
export, plotting, printing).  These helpers let the generator escalate
the declared paper size along the standard A-series ladder until the
content fits.
"""

from __future__ import annotations

#: Landscape (width, height) in mm for the standard A-series sheets KiCad
#: supports.  KiCad's default orientation for schematic sheets is
#: landscape; a ``"<size> portrait"`` paper string swaps the dimensions.
PAPER_SIZES_MM: dict[str, tuple[float, float]] = {
    "A5": (210.0, 148.0),
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
}

#: Escalation ladder walked by :func:`select_paper_for_extent`, smallest
#: to largest.  A5 is intentionally excluded from auto-escalation targets
#: (we never *shrink* a declared sheet) but is recognised as a declared
#: size.
PAPER_LADDER: tuple[str, ...] = ("A4", "A3", "A2", "A1", "A0")

#: Default clearance kept between the content extent and the sheet edge.
DEFAULT_PAPER_MARGIN_MM: float = 10.0


def paper_dimensions(paper: str) -> tuple[float, float] | None:
    """Return (width, height) in mm for a KiCad paper string.

    Handles the optional ``portrait`` suffix (``"A4 portrait"``) by
    swapping the landscape dimensions.  Returns ``None`` for paper
    strings this module does not model (e.g. ``"User"`` custom sizes,
    US ANSI sheets) — callers should skip auto-sizing in that case
    rather than guess.
    """
    parts = paper.split()
    if not parts:
        return None
    size = PAPER_SIZES_MM.get(parts[0])
    if size is None:
        return None
    if len(parts) > 1 and parts[1].lower() == "portrait":
        return (size[1], size[0])
    return size


def select_paper_for_extent(
    max_x: float,
    max_y: float,
    margin: float = DEFAULT_PAPER_MARGIN_MM,
    minimum: str = "A4",
) -> str | None:
    """Pick the smallest ladder paper that fits content extending to
    ``(max_x, max_y)`` mm with ``margin`` mm of clearance.

    The search starts at *minimum* (never selects a smaller sheet than
    the declared one) and walks ``A4 -> A3 -> A2 -> A1 -> A0``.

    Returns the paper name, or ``None`` when not even A0 fits (caller
    should warn loudly; KiCad will clip).
    """
    need_w = max_x + margin
    need_h = max_y + margin

    started = False
    for name in PAPER_LADDER:
        if name == minimum:
            started = True
        if not started:
            continue
        w, h = PAPER_SIZES_MM[name]
        if need_w <= w and need_h <= h:
            return name
    if not started:
        # *minimum* was not on the ladder (e.g. "A5" or unknown);
        # fall back to scanning the whole ladder.
        for name in PAPER_LADDER:
            w, h = PAPER_SIZES_MM[name]
            if need_w <= w and need_h <= h:
                return name
    return None
