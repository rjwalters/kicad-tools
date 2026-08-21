"""Regression tests: ``zones/fill_clearance.py`` on pre-KiCad-6 ``(module ...)`` boards.

All three ``doc.find_all("footprint")`` sites in this module (feeding
``_collect_obstacles``, ``_apply_selective_pad_connection``, and
``force_solid_on_pads_by_uuid``) were blind to the legacy ``(module ...)``
container spelling: a pad that lives inside a ``(module ...)`` block was
silently invisible to every pad-aware correction in this file, even though
``schema/pcb.py`` (#4879) already parses ``(module ...)`` as a footprint
(issue #4891).
"""

from __future__ import annotations

import pytest

from kicad_tools.sexp import parse_string
from kicad_tools.zones.fill_clearance import (
    apply_foreign_pad_clearance,
    force_solid_on_pads_by_uuid,
    normalize_zone_pad_connection,
)

shapely = pytest.importorskip("shapely")

# Same board as _BOARD in test_zones_fill_clearance.py's TestForeignPadCarved,
# but with the pre-KiCad-6 ``(module ...)`` container spelling.
_LEGACY_MODULE_BOARD = """
(kicad_pcb
  (version 4)
  (net 0 "")
  (net 1 "VCC")
  (net 2 "LED_ANODE")
  (net 3 "GND")
  (module R_0603 (layer F.Cu) (at 5 5)
    (pad 1 thru_hole rect (at 0 0) (size 1.7 1.7) (drill 1.0) (layers *.Cu *.Mask) (net 3 "GND"))
  )
  (module C_0402 (layer F.Cu) (at 15 15)
    (pad 1 thru_hole rect (at 0 0) (size 1.7 1.7) (drill 1.0) (layers *.Cu *.Mask) (net 1 "VCC"))
  )
  (via (at 12 5) (size 0.6) (drill 0.3) (layers F.Cu B.Cu) (net 2 "LED_ANODE"))
  (zone
    (net "VCC")
    (layer "F.Cu")
    (uuid "test-zone")
    (hatch edge 0.5)
    (connect_pads (clearance 0.3))
    (min_thickness 0.25)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.4))
    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))
    (filled_polygon
      (layer "F.Cu")
      (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20))
    )
  )
)
"""


class TestForeignPadClearanceLegacyModuleBoards:
    """``apply_foreign_pad_clearance`` must find pads inside ``(module ...)``."""

    def test_module_footprint_pad_is_seen_as_obstacle(self):
        doc = parse_string(_LEGACY_MODULE_BOARD)
        modified = apply_foreign_pad_clearance(doc)

        # Before the fix, _collect_obstacles found zero footprints, so the
        # correction was a silent no-op (modified == 0) despite the GND pad
        # sitting squarely inside the VCC fill.
        assert modified >= 1

    def test_no_regression_on_modern_footprint_spelling(self):
        modern_board = _LEGACY_MODULE_BOARD.replace(
            "(module R_0603 (layer F.Cu) (at 5 5)",
            '(footprint "R_0603" (layer "F.Cu") (at 5 5)',
        ).replace(
            "(module C_0402 (layer F.Cu) (at 15 15)",
            '(footprint "C_0402" (layer "F.Cu") (at 15 15)',
        )
        legacy_doc = parse_string(_LEGACY_MODULE_BOARD)
        modern_doc = parse_string(modern_board)

        legacy_changed = apply_foreign_pad_clearance(legacy_doc)
        modern_changed = apply_foreign_pad_clearance(modern_doc)

        assert legacy_changed == modern_changed


class TestSelectivePadConnectionLegacyModuleBoards:
    """``normalize_zone_pad_connection``'s selective mode must reach ``(module ...)`` pads."""

    _BOARD = """
    (kicad_pcb
      (version 4)
      (net 0 "")
      (net 1 "GND")
      (module small_smd (layer F.Cu) (at 5 5)
        (pad 1 smd rect (at 0 0) (size 0.3 0.3) (layers F.Cu) (net 1 "GND")))
      (zone
        (net 1)
        (net_name "GND")
        (layer "F.Cu")
        (uuid "z1")
        (hatch edge 0.5)
        (connect_pads (clearance 0.3))
        (min_thickness 0.25)
        (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.4))
        (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))
      )
    )
    """

    def test_forces_solid_on_undersized_module_pad(self):
        doc = parse_string(self._BOARD)
        changed = normalize_zone_pad_connection(doc)

        # Before the fix, _apply_selective_pad_connection's find_all("footprint")
        # never matched the (module ...) block, so the too-small pad kept its
        # thermal relief and starved_thermal remained un-fixable.
        assert changed == 1


class TestForceSolidOnPadsByUuidLegacyModuleBoards:
    """``force_solid_on_pads_by_uuid`` must find the named pad inside ``(module ...)``."""

    _BOARD = """
    (kicad_pcb
      (version 4)
      (net 0 "")
      (net 1 "GND")
      (module lib_a (layer F.Cu) (at 5 5)
        (pad 1 smd rect (at 0 0) (size 2.0 2.0) (layers F.Cu)
          (net 1 "GND") (uuid "pad-a")))
    )
    """

    def test_forces_named_pad_solid_in_module_footprint(self):
        doc = parse_string(self._BOARD)
        changed = force_solid_on_pads_by_uuid(doc, {"pad-a"})

        assert changed == 1
