"""The #9619 KiCad probe pair, at the validate-rule level, on board-file coordinates.

The black-box probe on pinned KiCad 10.0.6 (Issue #9619) accepts a 500 nm
shortfall below a 0.127 mm clearance rule and flags 600 nm.  ``kct check``'s
clearance family must reproduce that split on the coordinates a real board
file stores -- which is the interesting part: ``20.3265`` is not
binary-exact, so the measured shortfall lands ~7e-16 mm below its nominal
value and :data:`~kicad_tools.validate.rules.base.DRC_FLOAT_GUARD_MM` must
absorb it for the exactly-at-the-band verdict to match KiCad's integer-nm
comparison.

Live evidence (2026-11-19, this worktree, ``kicad/kicad:10.0`` image):
``kicad-cli pcb drc`` on these exact boards accepts 500 nm (no ``clearance``
violation) and flags 600 nm; ``kct check --only clearance`` agrees on both.
"""

from __future__ import annotations

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.base import DRC_TOLERANCE
from kicad_tools.validate.rules.clearance import ClearanceRule

MIN_CLEARANCE = 0.127
TRACK_WIDTH = 0.2
# The probe's own coordinates: track centres at y=20 and y=20 + 0.2 + gap.
# The 0.1265 board is the 500 nm case KiCad accepts; 0.1264 the 600 nm case
# it flags.  Both y values carry board-file float dust by construction.
TRACE_Y = {"500nm": 20.3265, "600nm": 20.3264}


class _Rules:
    """Minimal rule source: one general clearance floor, nothing else."""

    min_clearance_mm = MIN_CLEARANCE
    min_smd_pad_clearance_mm = None
    min_pth_hole_to_track_mm = None
    min_inner_pth_hole_to_copper_mm = None


def _probe_pcb(lower_y: float) -> PCB:
    """The probe board: two parallel 0.2 mm tracks on F.Cu, nets N1/N2."""
    from kicad_tools.sexp import parse_string

    text = (
        '(kicad_pcb (version 20241229) (generator "probe_9619")\n'
        "\t(general (thickness 1.6) (legacy_teardrops no))\n"
        '\t(paper "A4")\n'
        '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        '\t(net 0 "") (net 1 "N1") (net 2 "N2")\n'
        '\t(segment (start 20 20) (end 60 20) (width 0.2) (layer "F.Cu") (net 1))\n'
        f"\t(segment (start 20 {lower_y}) (end 60 {lower_y}) (width 0.2) "
        '(layer "F.Cu") (net 2))\n'
        ")\n"
    )
    return PCB(parse_string(text))


def test_kicad_probe_boundary_500nm_passes() -> None:
    # Nominal: the track centre sits a 500 nm shortfall below the limit.
    assert TRACE_Y["500nm"] == pytest.approx(20.0 + TRACK_WIDTH + MIN_CLEARANCE - 5e-4, abs=1e-12)
    result = ClearanceRule().check(_probe_pcb(TRACE_Y["500nm"]), _Rules())
    assert result.error_count == 0, (
        "a 500 nm shortfall is inside KiCad's probed acceptance band "
        "(#9619) and must pass even with board-file coordinate dust"
    )


def test_kicad_probe_boundary_600nm_fails() -> None:
    assert TRACE_Y["600nm"] == pytest.approx(20.0 + TRACK_WIDTH + MIN_CLEARANCE - 6e-4, abs=1e-12)
    result = ClearanceRule().check(_probe_pcb(TRACE_Y["600nm"]), _Rules())
    assert result.error_count == 1, (
        "a 600 nm shortfall is beyond KiCad's probed acceptance band "
        "(#9619) and must be flagged, as kicad-cli DRC flags it"
    )
    v = result.violations[0]
    assert v.actual_value == pytest.approx(20.3264 - 20.0 - TRACK_WIDTH, abs=1e-12)
    assert v.required_value == MIN_CLEARANCE


def test_band_guard_is_negligible_against_iu() -> None:
    """The float guard stays six orders below KiCad's integer-nm IU and six
    above the board-coordinate dust, so it cannot move any verdict a
    nanometre-resolution check could see."""
    from kicad_tools.validate.rules.base import DRC_FLOAT_GUARD_MM

    assert DRC_FLOAT_GUARD_MM < 1e-6  # well under KiCad's 1 nm IU
    assert DRC_FLOAT_GUARD_MM > 1e-12  # well above board-coordinate dust
    assert DRC_TOLERANCE + DRC_FLOAT_GUARD_MM < 1.6e-3  # band stays sub-marginal
