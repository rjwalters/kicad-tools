"""The silk text box covers KiCad's plotted ink (Issue #9515).

The pre-#9515 model (``0.7 * size * n + thickness``) under-covered KiCad's
plotted stroke-font ink -- badly for wide glyphs and digit runs -- so a pad or
silk item inside KiCad's ink but outside the box was silently missed by
``silk_over_copper`` / ``silk_overlap`` / ``silk_hole_overlap``.  The measured
advance table in ``silkscreen.py`` replaced it; this file pins the table to the
measured ink widths.

Measured numbers come from plotting each probe ``gr_text`` on F.SilkS with
kicad-cli 10.0.6 (the pinned CI image) and scanning the gerber stroke vertices
plus the round aperture diameter; see the provenance comment above
``_TEXT_CHAR_ADVANCE_QUANTA``.
"""

from __future__ import annotations

import pytest

pytest.importorskip("shapely")

from kicad_tools.validate.rules import silkscreen as silk  # noqa: E402

#: (text, size, thickness, measured ink width mm) -- raw probe measurements.
MEASURED_INK = [
    # repeated wide/narrow glyphs, length 1 and 10
    ("M", 1.0, 0.15, 0.8167),
    ("x", 1.0, 0.15, 0.6738),
    ("MMMMMMMMMM", 1.0, 0.15, 11.1024),
    ("mmmmmmmmmm", 1.0, 0.15, 13.0071),
    ("WWWWWWWWWW", 1.0, 0.15, 11.2929),
    ("0000000000", 1.0, 0.15, 9.2929),
    ("xxxxxxxxxx", 1.0, 0.15, 7.9595),
    ("AAAAAAAAAA", 1.0, 0.15, 8.5310),
    # mixed-class and refdes-like strings
    ("ABCD", 1.0, 0.15, 3.7214),
    ("ABCD1234", 1.0, 0.15, 7.5786),
    ("LED4", 1.0, 0.15, 3.4357),
    ("R101", 1.0, 0.15, 3.5786),
    ("C22", 1.0, 0.15, 2.6738),
    ("A A", 1.0, 0.15, 2.4357),
    # the exactly-tight measured case: '_' ink equals its advance
    ("___", 1.0, 0.15, 2.4357),
    # trim-decision boundary glyphs as string enders: the trim set is decided
    # by the last glyph's measured ink (see _MEASURED_SINGLE_GLYPH_INK), so
    # each boundary glyph is pinned here as a string ender too.  't' (-0.0405)
    # and 'e' (-0.2310) are the pair whose trim decision the frozenset
    # originally got backwards (Issue #9515 review).
    ("tttt", 1.0, 0.15, 2.2452),
    ("Test", 1.0, 0.15, 2.9595),
    ("Reset", 1.0, 0.15, 3.9119),
    ("Hot", 1.0, 0.15, 2.3405),
    ("eeee", 1.0, 0.15, 3.1976),
    ("Done", 1.0, 0.15, 3.3881),
    ("TTTT", 1.0, 0.15, 3.0071),
    ("VVVV", 1.0, 0.15, 3.3881),
    ("YYYY", 1.0, 0.15, 3.3881),
    ("AAAA", 1.0, 0.15, 3.3881),
    ("ffff", 1.0, 0.15, 2.2452),
    ("jjjj", 1.0, 0.15, 1.8167),
    ("////", 1.0, 0.15, 4.1500),
    ("____", 1.0, 0.15, 3.1976),
    # size and thickness scaling probes
    ("MMMM", 2.0, 0.30, 8.4905),
    ("MMMM", 1.0, 0.30, 4.3452),
    ("MMMM", 0.8, 0.12, 3.3962),
]

#: Measured plotted ink of each glyph as a single-character string at the
#: 1.0 mm / 0.15 mm probe (same gerber-scan method as MEASURED_INK).  This is
#: the raw data the end-trim decision is derived from: with
#: ``b = ink - advance`` (advance = size * quanta / 21), a glyph is trimmed
#: iff ``b < -0.10 mm``.  Space is absent -- a lone space plots no ink.
_MEASURED_SINGLE_GLYPH_INK = {
    "A": 0.8167,
    "B": 0.7214,
    "C": 0.7690,
    "D": 0.7214,
    "E": 0.6262,
    "F": 0.6262,
    "G": 0.7690,
    "H": 0.7214,
    "I": 0.1500,
    "J": 0.5310,
    "K": 0.7214,
    "L": 0.6262,
    "M": 0.8167,
    "N": 0.7214,
    "O": 0.8167,
    "P": 0.7214,
    "Q": 0.8643,
    "R": 0.7214,
    "S": 0.7214,
    "T": 0.7214,
    "U": 0.7214,
    "V": 0.8167,
    "W": 1.0071,
    "X": 0.8167,
    "Y": 0.8167,
    "Z": 0.8167,
    "0": 0.7214,
    "1": 0.7214,
    "2": 0.7690,
    "3": 0.7690,
    "4": 0.7690,
    "5": 0.7214,
    "6": 0.7214,
    "7": 0.8167,
    "8": 0.7214,
    "9": 0.7214,
    "a": 0.6262,
    "b": 0.6262,
    "c": 0.6262,
    "d": 0.6262,
    "e": 0.6262,
    "f": 0.5310,
    "g": 0.6262,
    "h": 0.5786,
    "i": 0.2452,
    "j": 0.3881,
    "k": 0.5310,
    "l": 0.2929,
    "m": 1.0071,
    "n": 0.5786,
    "o": 0.6738,
    "p": 0.6262,
    "q": 0.6262,
    "r": 0.4357,
    "s": 0.5786,
    "t": 0.5310,
    "u": 0.5786,
    "v": 0.6262,
    "w": 0.9119,
    "x": 0.6738,
    "y": 0.6262,
    "z": 0.6738,
    "!": 0.2452,
    "%": 0.9119,
    "'": 0.2452,
    "(": 0.4357,
    ")": 0.4357,
    "+": 0.9119,
    "-": 0.9119,
    ".": 0.2452,
    "/": 1.0071,
    ":": 0.2452,
    "?": 0.6262,
    "_": 0.9119,
}


def _model_width(text: str, size: float, thickness: float) -> float:
    box = silk._text_bbox_geometry(text, (size, size), thickness, (0.0, 0.0))
    assert box is not None
    minx, _miny, maxx, _maxy = box.bounds
    return maxx - minx


@pytest.mark.parametrize(("text", "size", "thickness", "ink"), MEASURED_INK)
def test_box_covers_measured_ink(text, size, thickness, ink):
    width = _model_width(text, size, thickness)
    assert width >= ink - 1e-9, (
        f"{text!r} box {width:.4f} < measured ink {ink:.4f} at size {size} / thickness {thickness}"
    )


@pytest.mark.parametrize(("text", "size", "thickness", "ink"), MEASURED_INK)
def test_box_over_coverage_is_bounded(text, size, thickness, ink):
    """Guard against the table silently ballooning: box stays within
    +0.90 mm of the measured ink -- the measured worst case is +0.8624 mm
    ('MMMM' at 2.0 mm / 0.30 mm) plus 0.04 mm headroom."""
    width = _model_width(text, size, thickness)
    assert width <= ink + 0.90, (
        f"{text!r} box {width:.4f} over-covers measured ink {ink:.4f} "
        "by more than the measured worst case"
    )


def test_single_glyph_boxes_cover_measured_ink():
    """Every measured glyph, plotted alone, sits inside its box: coverage
    reduces to the last glyph, so this is the per-glyph form of
    ``test_box_covers_measured_ink``."""
    for glyph, ink in _MEASURED_SINGLE_GLYPH_INK.items():
        width = _model_width(glyph, 1.0, 0.15)
        assert width >= ink - 1e-9, f"{glyph!r} box {width:.4f} < measured ink {ink:.4f}"


def test_trim_set_matches_measured_end_ink():
    """Pin the end-trim set by measurement instead of by prose: a glyph is
    trimmed iff its measured plotted ink ends more than 0.10 mm short of its
    advance.  't' (b = -0.0405, untrimmed) and 'e' (b = -0.2310, trimmed)
    are the boundary pair the set originally got backwards (#9515 review);
    the shallowest trimmed glyph is -0.1357 and the shallowest untrimmed one
    is -0.0881, so the -0.10 threshold separates the measured sets with
    margin on both sides."""
    for glyph, ink in _MEASURED_SINGLE_GLYPH_INK.items():
        advance = silk._TEXT_CHAR_ADVANCE_QUANTA[glyph] / 21.0
        end_gap = ink - advance
        trimmed = glyph in silk._TEXT_SHORT_INK_END
        if trimmed:
            assert end_gap < -0.10, (
                f"{glyph!r} is trimmed but its ink ends only {end_gap:.4f} short of its advance"
            )
        else:
            assert end_gap >= -0.10, (
                f"{glyph!r} is untrimmed but its ink ends {end_gap:.4f} short of its advance"
            )


def test_underscore_tight_case_keeps_a_positive_margin():
    """'_' ink equals its advance exactly; the guard term keeps the box
    strictly wider than the plotted ink."""
    width = _model_width("_", 1.0, 0.15)
    assert width == pytest.approx(16 / 21 + 0.15 + silk._TEXT_WIDTH_GUARD_MM)
    assert width > 16 / 21 + 0.15


def test_pre_9515_factor_would_undercover_the_probes():
    """Documents the fix: the old single 0.7 factor under-covers several of
    the measured strings that the advance table covers."""
    for text, size, thickness, ink in MEASURED_INK:
        old = size * len(text) * 0.7 + thickness
        if old < ink:
            new = _model_width(text, size, thickness)
            assert new >= ink > old, f"{text!r} expected the regression to bite"
