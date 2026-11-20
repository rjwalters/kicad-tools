r"""The measured silk text box covers the #9754 punctuation glyphs.

Issue #9754 probed the twenty printable-ASCII punctuation glyphs left
unmeasured by #9515 (``# @ & $ * = < > [ ] { } " , ; | ^ ` ~ \``) on the
pinned kicad-cli 10.0.6 image with the same gerber-vertex ± aperture-radius
method (see the provenance comment above ``_TEXT_CHAR_ADVANCE_QUANTA``).
Every one advances at most 27 quanta (``@``), so the 28-quanta default is a
measured bound over the full printable-ASCII set -- the refutation outcome
the issue names as complete.  The measured advances and bearings joined the
tables, which this file pins; a punctuation run's box now hugs its ink
instead of the 28-quanta default.
"""

from __future__ import annotations

import string

import pytest

pytest.importorskip("shapely")

from kicad_tools.validate.rules import silkscreen as silk  # noqa: E402

#: (glyph, advance quanta, (left, right) bearings) -- raw probe measurements
#: at the 1.0 mm / 0.15 mm probe on kicad-cli 10.0.6.
MEASURED_GLYPHS = [
    ("#", 21, (2, 2)),
    ("@", 27, (4, 4)),
    ("&", 26, (5, 4)),
    ("$", 20, (4, 4)),
    ("*", 16, (3, 3)),
    ("=", 26, (5, 5)),
    ("<", 26, (5, 5)),
    (">", 26, (5, 5)),
    ("[", 14, (6, 3)),
    ("]", 14, (3, 6)),
    ("{", 14, (4, 3)),
    ("}", 14, (3, 4)),
    ('"', 16, (4, 4)),
    (",", 10, (4, 4)),
    (";", 10, (4, 4)),
    ("|", 20, (10, 10)),
    ("^", 12, (2, 2)),
    ("~", 15, (2, 3)),
    ("\\", 14, (-2, -2)),
    ("`", 8, (2, 3)),
]

#: (text, measured ink width mm) for the single- and ten-glyph probe strings.
_MEASURED_INK = {
    "#": (0.9595, 9.9595),
    "@": (1.0548, 12.6262),
    "&": (0.9595, 12.1024),
    "$": (0.7214, 9.2929),
    "*": (0.6262, 7.4833),
    "=": (0.9119, 12.0548),
    "<": (0.9119, 12.0548),
    ">": (0.9119, 12.0548),
    "[": (0.3881, 6.3881),
    "]": (0.3881, 6.3881),
    "{": (0.4833, 6.4833),
    "}": (0.4833, 6.4833),
    '"': (0.5310, 7.3881),
    ",": (0.2452, 4.5309),
    ";": (0.2452, 4.5309),
    "|": (0.1500, 8.7214),
    "^": (0.5310, 5.6738),
    "~": (0.6262, 7.0548),
    "\\": (1.0071, 7.0071),
    "`": (0.2929, 3.7214),
}

#: Every printable-ASCII glyph except the whitespace controls (``' '`` is
#: measured; tab/newline/etc. cannot appear in plotted silk text).
_PRINTABLE_ASCII = sorted((set(string.printable) - set(string.whitespace)) | {" "})


def _model_width(text: str, size: float = 1.0, thickness: float = 0.15) -> float:
    box = silk._text_bbox_geometry(text, (size, size), thickness, (0.0, 0.0))
    assert box is not None
    minx, _miny, maxx, _maxy = box.bounds
    return maxx - minx


@pytest.mark.parametrize("glyph", [g for g, _a, _b in MEASURED_GLYPHS])
@pytest.mark.parametrize("n", [1, 10])
def test_box_covers_measured_ink(glyph, n):
    ink = _MEASURED_INK[glyph][1 if n == 10 else 0]
    width = _model_width(glyph * n)
    assert width >= ink - 1e-9, f"{glyph * n!r} box {width:.4f} < measured ink {ink:.4f}"


@pytest.mark.parametrize("glyph", [g for g, _a, _b in MEASURED_GLYPHS])
@pytest.mark.parametrize("n", [1, 10])
def test_box_over_coverage_is_bounded(glyph, n):
    ink = _MEASURED_INK[glyph][1 if n == 10 else 0]
    width = _model_width(glyph * n)
    assert width <= ink + 0.011, (
        f"{glyph * n!r} box {width:.4f} over-covers measured ink {ink:.4f} "
        "by more than the measured worst case"
    )


@pytest.mark.parametrize(("glyph", "advance", "bearings"), MEASURED_GLYPHS)
def test_tables_carry_the_measured_quanta(glyph, advance, bearings):
    assert silk._TEXT_CHAR_ADVANCE_QUANTA[glyph] == advance
    assert silk._TEXT_BEARINGS[glyph] == bearings


def test_advance_table_covers_all_printable_ascii():
    """The #9754 refutation, pinned: every printable-ASCII glyph is measured."""
    missing = [c for c in _PRINTABLE_ASCII if c not in silk._TEXT_CHAR_ADVANCE_QUANTA]
    assert not missing, f"unmeasured printable-ASCII glyphs: {missing!r}"


def test_default_is_a_measured_bound_over_printable_ascii():
    """28 quanta is the widest measured advance, so the fallback for
    non-ASCII code points never under-covers a printable-ASCII string."""
    widest = max(silk._TEXT_CHAR_ADVANCE_QUANTA.items(), key=lambda kv: kv[1])
    assert widest == ("m", 28)
    assert widest[1] <= silk._TEXT_ADVANCE_DEFAULT_QUANTA


def test_bearings_never_exceed_their_advance():
    """A bearing pair must leave a non-negative inked span inside the advance
    cell -- the '|' single-stroke glyph is the measured zero-margin case, and
    '\' overhangs by exactly the measured -2/-2."""
    for glyph, advance, (left, right) in MEASURED_GLYPHS:
        assert left + right <= advance, f"{glyph!r} bearings exceed its advance"
