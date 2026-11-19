"""Issue #9466: a duplicated footprint uuid never reaches the mask exemption.

The ``within_own_footprint`` exemption in ``mask_to_copper`` keys a pad's
footprint by ``parent_uuid``, while ``physical_copper_gap`` keys it by the
footprint's position in ``pcb.footprints``
(``tests/test_physical_gap_same_footprint_9420.py`` pins that side).  On a
board whose two footprints share one uuid the two keys would disagree -- so
measure whether such a board can reach ``_same_footprint_pads`` at all.

It cannot: capture (``inspect_attributed_mask_geometry``) refuses any tree
whose uuid values are noncanonical or repeated, before native attribution
runs.  A hand-edited or verbatim-copied footprint uuid therefore never
aliases inside the exemption -- the board is refused outright with
``coverage="incomplete"``, and on every board that *is* assessed the
footprint uuids are unique, so the two keys cannot disagree.  This file pins
that measurement so a future relaxation of the uuid gate cannot silently
reopen the aliasing the gap side was hardened against.
"""

import pytest

from kicad_tools.validate.mask_copper import (
    MaskCopperPolicy,
    check_mask_to_copper,
)
from kicad_tools.validate.mask_copper_geometry import inspect_attributed_mask_geometry

POLICY = MaskCopperPolicy(
    clearance_mm=0.09,
    source="order",
    process="process",
    revision="rev",
    warning_clearance_mm=0.15,
)

#: Canonical uuid shared by both footprints -- the aliasing input of #9466.
SHARED_FOOTPRINT_UUID = "80f92f63-7d6e-418b-978d-200000220000"

BOARD_HEADER = """(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6))
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
      (net 0 "") (net 1 "GND")
"""

# Same geometry as the gap-side pin: two same-net 1 mm pads 0.19 mm apart,
# one per footprint -- exactly the pair the exemption would downgrade.
GAP = 0.19


def _footprint(ref, x, uuid_, pad_uuid):
    return (
        f'(footprint "RF_Module:ESP32-C3-WROOM-02" (layer "F.Cu") (at {x} 0)\n'
        f'  (property "Reference" "{ref}")\n'
        f'  (pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu") '
        f'(net 1 "GND") (uuid "{pad_uuid}"))\n'
        f'  (uuid "{uuid_}")\n'
        f")"
    )


def _shared_uuid_board():
    return (
        BOARD_HEADER
        + _footprint("U1", 0, SHARED_FOOTPRINT_UUID, "1745e8e7-cb05-4e0c-be57-60ff31465fb9")
        + "\n"
        + _footprint("U2", 1 + GAP, SHARED_FOOTPRINT_UUID, "b531e0f4-5d47-4a72-8a54-a3eaafb85b86")
        + ")"
    )


def _write_board(tmp_path):
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_shared_uuid_board())
    return path


def test_two_footprints_sharing_one_uuid_are_refused_before_attribution(tmp_path):
    path = _write_board(tmp_path)
    with pytest.raises(ValueError, match="Noncanonical or duplicate source UUID"):
        inspect_attributed_mask_geometry(str(path))


def test_mask_to_copper_reports_the_refusal_and_measures_nothing(tmp_path):
    path = _write_board(tmp_path)
    assessment = check_mask_to_copper(str(path), POLICY)
    assert assessment.coverage == "incomplete"
    assert any(
        "Noncanonical or duplicate source UUID: " + SHARED_FOOTPRINT_UUID in reason
        for reason in assessment.reasons
    )
    # Nothing was measured, so no cross-footprint pair can be downgraded to
    # within_own_footprint by the shared parent_uuid.
    assert assessment.measurements == []
