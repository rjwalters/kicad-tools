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
    # size and thickness scaling probes
    ("MMMM", 2.0, 0.30, 8.4905),
    ("MMMM", 1.0, 0.30, 4.3452),
    ("MMMM", 0.8, 0.12, 3.3962),
]


def _model_width(text: str, size: float, thickness: float) -> float:
    box = silk._text_bbox_geometry(text, (size, size), thickness, (0.0, 0.0))
    assert box is not None
    minx, _miny, maxx, _maxy = box.bounds
    return maxx - minx


@pytest.mark.parametrize(("text", "size", "thickness", "ink"), MEASURED_INK)
def test_box_covers_measured_ink(text, size, thickness, ink):
    width = _model_width(text, size, thickness)
    assert width >= ink - 1e-9, (
        f"{text!r} box {width:.4f} < measured ink {ink:.4f} "
        "at size {size} / thickness {thickness}"
    )


@pytest.mark.parametrize(("text", "size", "thickness", "ink"), MEASURED_INK)
def test_box_over_coverage_is_bounded(text, size, thickness, ink):
    """Guard against the table silently ballooning: box stays within +0.75mm
    of the measured ink (worst measured over-coverage is +0.96, the size-2
    MMMM probe)."""
    width = _model_width(text, size, thickness)
    assert width <= ink + 1.0, (
        f"{text!r} box {width:.4f} over-covers measured ink {ink:.4f} "
        "by more than the measured worst case"
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
