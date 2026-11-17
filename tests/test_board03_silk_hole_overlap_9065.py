"""Board 03 routed silkscreen clears every drilled hole (Issue #9065).

PR #9061 shipped ``kct fix-silkscreen --clip-to-holes`` and
``kct place-silk-refs --over-holes`` but did not call them from board 03's
recipe, leaving three of #9054's acceptance criteria undelivered.  The
recipe now runs both tools (their library equivalents) after routing and
hard-gates ``silk_hole_overlap == 0`` -- a gate the main-only
``board-03-end-to-end`` CI job exercises on every push.  These tests pin
the committed routed board so the normal PR suite keeps the count at zero
as well: 5 ``silk_hole_overlap`` findings on main as of 2026-11-17, 0
after the recipe gained the silk step, with ``silk_pad_clearance`` and
``pin1_marker_*`` unchanged (#9065 observed counts).
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.manufacturers import get_profile  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules.silkscreen import check_all_silkscreen  # noqa: E402

BOARD = Path(__file__).resolve().parent.parent / "boards" / "03-usb-joystick"
ROUTED = BOARD / "output" / "usb_joystick_routed.kicad_pcb"


def _silk_counts() -> Counter:
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    return Counter(v.rule_id for v in check_all_silkscreen(PCB.load(ROUTED), rules).violations)


def test_routed_silk_hole_overlap_is_zero():
    assert _silk_counts().get("silk_hole_overlap", 0) == 0


def test_silk_pad_clearance_count_unchanged():
    """The clip/move must not trade hole overlap for new pad findings."""
    assert _silk_counts().get("silk_pad_clearance", 0) == 21


def test_pin1_marker_findings_unchanged_at_zero():
    from kicad_tools.validate.checker import DRCChecker

    results = DRCChecker(PCB.load(ROUTED), manufacturer="jlcpcb", layers=4).check_pin1_markers()
    counts = Counter(v.rule_id for v in results.violations)
    assert counts.get("pin1_marker_missing", 0) == 0
    assert counts.get("pin1_marker_obscured", 0) == 0
