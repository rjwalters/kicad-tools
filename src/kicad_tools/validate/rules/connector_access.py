"""Connector mating / edge-access DRC rule (Issue #4613).

A board can pass every electrical and geometric gate with a jack or
receptacle buried mid-board where no plug can physically reach it.  The
motivating incident: a fully-certified board shipped with a
``Connector_Audio:Jack_3.5mm_CUI_SJ-3523-SMT_Horizontal`` whose courtyard
sat 10.66 mm inside the board outline -- ~10 mm of solid FR4 in front of
the barrel opening, so no 3.5 mm plug could ever be inserted.

This module implements a two-tier, precision-first check:

* **Tier A** -- ``connector_edge_access`` (``warning`` severity,
  default-on).  Only *rigid-plug, panel-entry* connector families whose
  mating direction is derivable from KiCad's own library naming are
  flagged (see :data:`EDGE_ACCESS_FAMILIES`).  Pin headers, JST/Molex
  wire-to-board, IDC, and FFC/FPC connectors mate with flexible cables or
  stacked boards and are legitimately placed mid-board, so they are
  deliberately **excluded** -- a heuristic that flags every connector
  would drown the one real finding in noise and get the whole category
  skipped.  Names containing ``_Vertical`` are always skipped (they mate
  upward and need vertical clearance, not edge access).

* **Tier B** -- ``connector_edge_distance`` (``info`` severity, gated by
  the checker's measurement/verbose flags).  An inventory row for *every*
  ``Connector_*`` footprint carrying its courtyard-to-nearest-edge
  distance in ``actual_value``, so a human reviewing ``kct check
  --format json`` / ``--verbose`` output has the numbers in front of
  them even for connectors the warning tier does not judge.

Geometry: the footprint's courtyard polygon is resolved through
:func:`kicad_tools.geometry.courtyard._courtyard_polygon` (which applies
the KiCad negated-rotation transform, issue #3739); footprints without
courtyard geometry fall back to the transformed pad bounding box, and
pad-less footprints to the footprint origin point.  The reported distance
is the shapely distance from that geometry to the nearest ``Edge.Cuts``
segment -- exactly ``0.0`` for a connector at or overhanging the edge
(including a connector straddling the outline).  Internal cutouts are
also ``Edge.Cuts`` segments, so a connector mating through a panel
cutout correctly passes.

Directionality ("which side of the courtyard is the mating face and is
there FR4 in the way") is out of scope for v1 -- it needs per-footprint
mating-face metadata that footprints do not carry today (see
``optim/edge_placement.py`` / #4450, where the hint is caller-supplied).
Nearest-edge distance alone would have caught the shipped defect
(10.66 mm >> 3.0 mm).

Suppression: deliberate internal connectors (test points, mezzanine
stacks) are waived via the general ``.kct_waivers.json`` mechanism
(issue #4417) -- violations carry ``items=(reference,)`` so an entry like
``{"rule": "connector_edge_access", "items": ["J3"], ...}`` matches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kicad_tools._shapely import require_shapely
from kicad_tools.geometry.courtyard import _courtyard_polygon, _fp_transform

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Footprint

# Rule id for the Tier-A default-on warning (rigid-plug panel-entry
# connector too far from every board edge to be mateable).  Matches the
# ``rule`` field expected in ``.kct_waivers.json`` entries.
CONNECTOR_EDGE_ACCESS_RULE_ID = "connector_edge_access"

# Rule id for the Tier-B info-severity connector inventory row.
CONNECTOR_EDGE_DISTANCE_RULE_ID = "connector_edge_distance"

# Maximum courtyard-to-nearest-edge distance (mm) for a panel-entry
# connector before the edge-access warning fires.  A flush or overhanging
# mating face gives ~0 mm; courtyard margins add ~0.25-1 mm; 3.0 mm
# absorbs both while still catching the motivating incident's 10.66 mm by
# 3.5x.  Deliberately a module constant, NOT a CLI flag -- the inner/outer
# ``kct check`` parser flag sets are drift-guarded
# (``tests/test_cli_parser_drift.py``), so tuning this is a code change.
CONNECTOR_EDGE_ACCESS_MAX_MM = 3.0

# Floating-point guard band for the threshold comparison (0.1 micron),
# matching ``validate/rules/edge.py``.
_EPSILON_MM = 1e-4

# Mating-direction condition tokens for :data:`EDGE_ACCESS_FAMILIES`.
#
# ``_HORIZONTAL_SUFFIX``: the family needs edge access only when KiCad's
# footprint name carries the ``_Horizontal`` suffix (the library's own
# mating-direction signal).  ``_ALWAYS``: every member of the family is
# inherently horizontal-mating (barrel jacks, card edges), including
# names that omit the suffix.
_HORIZONTAL_SUFFIX = "horizontal-suffix"
_ALWAYS = "always"

# Tier-A family table: library prefix (the part of ``Footprint.name``
# before ``:``) -> mating-direction condition.  Extending coverage to a
# new rigid-plug panel-entry family is a one-line diff here.
#
# Deliberately EXCLUDED (flexible-cable / board-stacking families that
# are legitimately mid-board): ``Connector_PinHeader*``,
# ``Connector_PinSocket*``, ``Connector_JST``, ``Connector_Molex``,
# ``Connector_Wire``, ``Connector_FFC-FPC``, ``Connector_IDC``,
# ``Connector_Generic``.
EDGE_ACCESS_FAMILIES: dict[str, str] = {
    "Connector_Audio": _HORIZONTAL_SUFFIX,
    "Connector_USB": _HORIZONTAL_SUFFIX,
    "Connector_BarrelJack": _ALWAYS,
    "Connector_RJ": _HORIZONTAL_SUFFIX,
    "Connector_Card": _ALWAYS,
}

# Library-prefix marker for the Tier-B inventory: every footprint whose
# library name starts with this is a connector worth inventorying.
_CONNECTOR_LIB_PREFIX = "Connector_"


def _library_prefix(footprint_name: str) -> str:
    """Return the KiCad library prefix of a ``Lib:Footprint`` id.

    Names without a ``:`` separator (bare footprint names) have no
    library prefix and return the empty string.
    """
    lib, sep, _rest = footprint_name.partition(":")
    return lib if sep else ""


def _needs_edge_access(lib: str, footprint_name: str) -> bool:
    """True when ``footprint_name`` belongs to a Tier-A panel-entry family.

    ``_Vertical`` names are always skipped (they mate upward);
    ``_HORIZONTAL_SUFFIX`` families additionally require the
    ``_Horizontal`` marker in the name.
    """
    mode = EDGE_ACCESS_FAMILIES.get(lib)
    if mode is None:
        return False
    if "_Vertical" in footprint_name:
        return False
    if mode == _HORIZONTAL_SUFFIX and "_Horizontal" not in footprint_name:
        return False
    return True


class ConnectorEdgeAccessRule(DRCRule):
    """Check that rigid-plug panel-entry connectors can reach a board edge.

    Rule IDs generated:
        - connector_edge_access: Tier-A warning -- a horizontal-mating
          connector's courtyard is farther than
          :data:`CONNECTOR_EDGE_ACCESS_MAX_MM` from every ``Edge.Cuts``
          segment, so a mating plug likely cannot reach it.
        - connector_edge_distance: Tier-B info inventory row (one per
          ``Connector_*`` footprint, gated by ``emit_inventory``) carrying
          the measured distance in ``actual_value``.
    """

    rule_id = CONNECTOR_EDGE_ACCESS_RULE_ID
    name = "Connector Edge Access"
    description = "Check that panel-entry connectors have board-edge access for their mating plug"

    def __init__(
        self,
        emit_inventory: bool = False,
        max_edge_distance_mm: float = CONNECTOR_EDGE_ACCESS_MAX_MM,
    ) -> None:
        """Configure the rule.

        Args:
            emit_inventory: When True, emit the Tier-B info-severity
                ``connector_edge_distance`` row for every ``Connector_*``
                footprint (the checker wires this to its
                measurement/verbose gate).
            max_edge_distance_mm: Tier-A warning threshold; overridable
                for tests only (no CLI surface).
        """
        self.emit_inventory = emit_inventory
        self.max_edge_distance_mm = max_edge_distance_mm

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check connector edge access against the board outline.

        Args:
            pcb: The PCB to check
            design_rules: Design rules from the manufacturer profile
                (unused; the threshold is a module constant)

        Returns:
            DRCResults containing edge-access warnings and (when
            ``emit_inventory``) per-connector distance info rows.  Empty
            when the board has no ``Edge.Cuts`` outline (mirrors
            ``EdgeClearanceRule``).
        """
        del design_rules  # threshold is a module constant, not a mfr rule
        results = DRCResults()

        outline_segments = pcb.get_board_outline_segments()
        if not outline_segments:
            # No board outline defined; edge distance is meaningless.
            return results

        require_shapely("connector edge-access check")
        from shapely.geometry import LineString  # type: ignore[import-untyped]

        outline_lines = [
            LineString([seg_start, seg_end]) for seg_start, seg_end in outline_segments
        ]

        for footprint in pcb.footprints:
            lib = _library_prefix(footprint.name)
            if not lib.startswith(_CONNECTOR_LIB_PREFIX):
                continue

            geometry = self._footprint_geometry(footprint)
            distance = min(geometry.distance(line) for line in outline_lines)

            if self.emit_inventory:
                results.add(
                    DRCViolation(
                        rule_id=CONNECTOR_EDGE_DISTANCE_RULE_ID,
                        severity="info",
                        message=(
                            f"{footprint.reference} ({footprint.name}): "
                            f"{distance:.2f}mm from nearest board edge"
                        ),
                        location=footprint.position,
                        layer=footprint.layer,
                        actual_value=distance,
                        items=(footprint.reference,),
                    )
                )

            if (
                _needs_edge_access(lib, footprint.name)
                and distance > self.max_edge_distance_mm + _EPSILON_MM
            ):
                results.add(
                    DRCViolation(
                        rule_id=CONNECTOR_EDGE_ACCESS_RULE_ID,
                        severity="warning",
                        message=(
                            f"{footprint.reference} ({footprint.name}) is a "
                            f"horizontal-mating connector {distance:.2f}mm from the "
                            f"nearest board edge (max {self.max_edge_distance_mm:.2f}mm "
                            f"for plug access) -- a mating plug may not physically "
                            f"reach it; waive via .kct_waivers.json if intentional"
                        ),
                        location=footprint.position,
                        layer=footprint.layer,
                        actual_value=distance,
                        required_value=self.max_edge_distance_mm,
                        items=(footprint.reference,),
                    )
                )

        results.rules_checked += 1
        return results

    @staticmethod
    def _footprint_geometry(footprint: Footprint):
        """Return a shapely geometry approximating the footprint extent.

        Preference order:

        1. The real courtyard polygon (``F.CrtYd`` then ``B.CrtYd``),
           resolved through :func:`_courtyard_polygon` so position and the
           KiCad negated-rotation transform are honored (#3739).  The
           footprint's own side is tried first so a back-side connector
           with only a ``B.CrtYd`` resolves correctly.
        2. The bounding box of the transformed pad extents (each pad
           center expanded by half its larger dimension -- a conservative
           axis-aligned approximation).
        3. The footprint origin point (pad-less, courtyard-less
           footprints): no crash, no silent skip.
        """
        from shapely.geometry import Point, Polygon, box

        sides = ("B", "F") if footprint.layer.startswith("B.") else ("F", "B")
        for side in sides:
            polygon = _courtyard_polygon(footprint, side, Polygon)
            if polygon is not None:
                return polygon

        if footprint.pads:
            transform = _fp_transform(footprint)
            xs: list[float] = []
            ys: list[float] = []
            for pad in footprint.pads:
                cx, cy = transform(pad.position)
                half = max(pad.size) / 2.0
                xs.extend((cx - half, cx + half))
                ys.extend((cy - half, cy + half))
            return box(min(xs), min(ys), max(xs), max(ys))

        return Point(footprint.position)
