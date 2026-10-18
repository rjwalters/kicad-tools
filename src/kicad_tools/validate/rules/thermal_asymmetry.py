"""Thermal-asymmetry DFM rule (issue #7220).

Two soldering-yield risks pass both DRC engines because they are not
clearance or connectivity defects -- they are *thermal* defects:

* **Asymmetric 2-pad passive.**  A 2-terminal SMD resistor, capacitor or
  inductor whose one terminal is tied **solid** to a zone fill on its own
  copper layer while the other terminal reaches copper only through a
  thermal relief or a trace.  The solid terminal sinks heat into the pour
  and reaches reflow temperature later than the other one; the uneven
  wetting force is the textbook tombstoning set-up.
* **Solid-connected PTH pin.**  A plated through-hole pin whose fill
  connection is solid on two or more copper layers.  Tying a barrel solidly
  to several planes is the classic way to fail IPC-A-610 barrel fill when
  the part is wave-, selective- or hand-soldered.  Exposed-pad thermal-via
  pads (same pad number as the footprint's exposed pad and inside its
  outline) are the intended exception and are exempt.

kct's own starved-thermal repair (``zones/fill_clearance.py``) creates both
shapes by forcing ``(zone_connect 2)`` per pad, so the rule exists to make
that side effect visible.

The rule is geometric.  A pad counts as connected to a pour on a layer only
when a same-net zone's committed ``filled_polygon`` on that copper layer
actually touches the pad's copper outline (:func:`clearance._pad_polygon`).
A same-net plane on an inner layer an SMD pad never reaches does not count.
The connection *mode* is the effective one: the pad's own
``(zone_connect N)`` override first, then the zone's ``connect_pads``.

Severity ``warning``; the rule id is classified advisory-quality, so plain
``kct check`` exit codes and readiness gating are unchanged.  Individual
findings are waivable via ``.kct_waivers.json`` with
``items=[<ref>, <ref>-<pad>]``.

:func:`pad_zone_contacts` and :func:`asymmetric_passives` are public so
``kct detect-mistakes``' tombstoning check reuses this geometry instead of
keeping its own copy.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kicad_tools._shapely import require_shapely

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Footprint, Pad

THERMAL_ASYMMETRY_RULE_ID = "thermal_asymmetry"

#: Effective pad-to-zone connection modes.
SOLID = "solid"
THERMAL = "thermal"
NONE = "none"

#: Reference designators of the 2-terminal passives the passive finding covers.
PASSIVE_REFERENCE = re.compile(r"^[RCL]\d", re.IGNORECASE)

#: A PTH pin is reported when it is solid-connected on at least this many layers.
MIN_SOLID_PTH_LAYERS = 2

# KiCad ``(zone_connect N)`` values (ZONE_CONNECTION).
_PAD_ZONE_CONNECT = {0: NONE, 1: THERMAL, 2: SOLID}
_ZONE_CONNECT_PADS = {"solid": SOLID, "thermal_reliefs": THERMAL, "none": NONE}


@dataclass(frozen=True)
class PadZoneContact:
    """One copper layer on which a pad's own-net zone fill touches the pad."""

    layer: str
    mode: str  # SOLID | THERMAL | NONE
    zone_net: str


@dataclass(frozen=True)
class PassiveAsymmetry:
    """A 2-pad passive with one solid and one non-solid terminal."""

    footprint: Footprint
    solid_pad: Pad
    other_pad: Pad
    solid_contacts: tuple[PadZoneContact, ...]
    other_contacts: tuple[PadZoneContact, ...]


@dataclass
class _Fill:
    net_number: int
    net_name: str
    mode: str
    polygon: Any  # prepared shapely geometry


def effective_mode(pad: Pad, zone_connect_pads: str) -> str:
    """Return the effective connection mode of ``pad`` to a zone.

    The pad's ``(zone_connect N)`` override wins; ``3`` (thermal reliefs for
    THT only) is thermal for a plated hole and solid for an SMD pad, as in
    KiCad.  Otherwise the zone's ``connect_pads`` setting applies.
    """
    override = getattr(pad, "zone_connect", None)
    if override is not None:
        if override == 3:
            return THERMAL if pad.type == "thru_hole" else SOLID
        if override in _PAD_ZONE_CONNECT:
            return _PAD_ZONE_CONNECT[override]
    return _ZONE_CONNECT_PADS.get(zone_connect_pads, THERMAL)


def _copper_layer_names(pcb: PCB) -> list[str]:
    try:
        return [layer.name for layer in pcb.copper_layers]
    except (AttributeError, TypeError):
        return ["F.Cu", "B.Cu"]


def pad_copper_layers(pad: Pad, board_layers: Iterable[str]) -> list[str]:
    """Expand ``pad.layers`` (``*.Cu``, ``F&B.Cu``) to concrete copper layers."""
    board_layers = list(board_layers)
    result: list[str] = []
    for layer in pad.layers:
        if layer == "*.Cu":
            expanded = board_layers
        elif layer == "F&B.Cu":
            expanded = ["F.Cu", "B.Cu"]
        elif layer.endswith(".Cu"):
            expanded = [layer]
        else:
            continue
        for name in expanded:
            if name not in result:
                result.append(name)
    return result


class ZoneFillIndex:
    """Same-net zone fills grouped by copper layer, ready for pad tests."""

    def __init__(self, pcb: PCB) -> None:
        require_shapely("thermal-asymmetry zone-fill geometry")
        from shapely.prepared import prep  # type: ignore[import-untyped]

        from .clearance import _collect_zone_fills

        self.board_layers = _copper_layer_names(pcb)
        zones = list(pcb.zones)
        self.by_layer: dict[str, list[_Fill]] = {}
        for layer, fills in _collect_zone_fills(pcb).items():
            for fill in fills:
                zone = zones[fill.source_zone_index] if fill.source_zone_index >= 0 else None
                if zone is None or getattr(zone, "keepout", None) is not None:
                    continue
                polygon = fill.polygon
                inflation = zone.fill_inflation()
                if inflation > 0:
                    polygon = polygon.buffer(inflation)  # type: ignore[attr-defined]
                self.by_layer.setdefault(layer, []).append(
                    _Fill(
                        net_number=fill.net_number,
                        net_name=fill.net_name,
                        mode=zone.connect_pads,
                        polygon=prep(polygon),
                    )
                )

    @property
    def empty(self) -> bool:
        return not self.by_layer

    def contacts(self, pad: Pad, footprint: Footprint) -> list[PadZoneContact]:
        """Return the layers where an own-net fill touches ``pad``'s copper."""
        if pad.net_number == 0 and not pad.net_name:
            return []
        layers = [
            layer for layer in pad_copper_layers(pad, self.board_layers) if layer in self.by_layer
        ]
        if not layers:
            return []
        from .clearance import _pad_polygon

        outline = _pad_polygon(pad, footprint)
        if outline is None:
            return []
        contacts: list[PadZoneContact] = []
        for layer in layers:
            best: PadZoneContact | None = None
            for fill in self.by_layer[layer]:
                same_net = (pad.net_number and fill.net_number == pad.net_number) or (
                    pad.net_name and fill.net_name == pad.net_name
                )
                if not same_net or not fill.polygon.intersects(outline):
                    continue
                mode = effective_mode(pad, fill.mode)
                if mode == NONE:
                    continue
                contact = PadZoneContact(layer=layer, mode=mode, zone_net=fill.net_name)
                if best is None or (mode == SOLID and best.mode != SOLID):
                    best = contact
            if best is not None:
                contacts.append(best)
        return contacts


def pad_zone_contacts(pcb: PCB, pad: Pad, footprint: Footprint) -> list[PadZoneContact]:
    """Convenience wrapper: contacts of one pad (builds a fresh fill index)."""
    return ZoneFillIndex(pcb).contacts(pad, footprint)


def _smd_copper_pads(footprint: Footprint) -> list[Pad]:
    return [
        p for p in footprint.pads if p.type == "smd" and any(l.endswith(".Cu") for l in p.layers)
    ]


def is_two_pad_passive(footprint: Footprint) -> bool:
    """True for a 2-pad SMD R/C/L (by reference designator)."""
    if not PASSIVE_REFERENCE.match(footprint.reference or ""):
        return False
    if any(p.type == "thru_hole" for p in footprint.pads):
        return False
    return len(_smd_copper_pads(footprint)) == 2


def _is_solid(contacts: Iterable[PadZoneContact]) -> bool:
    return any(c.mode == SOLID for c in contacts)


def asymmetric_passives(pcb: PCB, index: ZoneFillIndex | None = None) -> list[PassiveAsymmetry]:
    """Return every 2-pad passive with one solid and one non-solid terminal."""
    index = index if index is not None else ZoneFillIndex(pcb)
    if index.empty:
        return []
    found: list[PassiveAsymmetry] = []
    for footprint in pcb.footprints:
        if not is_two_pad_passive(footprint):
            continue
        pad_a, pad_b = _smd_copper_pads(footprint)
        contacts_a = index.contacts(pad_a, footprint)
        contacts_b = index.contacts(pad_b, footprint)
        solid_a, solid_b = _is_solid(contacts_a), _is_solid(contacts_b)
        if solid_a == solid_b:
            continue
        if solid_a:
            found.append(
                PassiveAsymmetry(footprint, pad_a, pad_b, tuple(contacts_a), tuple(contacts_b))
            )
        else:
            found.append(
                PassiveAsymmetry(footprint, pad_b, pad_a, tuple(contacts_b), tuple(contacts_a))
            )
    return found


def _connection_text(contacts: Iterable[PadZoneContact]) -> str:
    contacts = list(contacts)
    if not contacts:
        return "no zone connection (trace only)"
    parts = [f"{c.mode} to '{c.zone_net}' fill on {c.layer}" for c in contacts]
    return ", ".join(parts)


def _ep_thermal_via_pad(footprint: Footprint, pad: Pad) -> bool:
    """True when ``pad`` is a thermal via of the footprint's exposed pad."""
    from shapely.geometry import Point  # type: ignore[import-untyped]

    from .clearance import _pad_polygon, _transform_pad_position
    from .via_under_body import exposed_pads

    center = Point(_transform_pad_position(pad, footprint))
    for ep in exposed_pads(footprint):
        if ep.number != pad.number:
            continue
        outline = _pad_polygon(ep, footprint)
        if outline is not None and outline.covers(center):
            return True
    return False


class ThermalAsymmetryRule(DRCRule):
    """Flag thermally asymmetric passives and solid multi-plane PTH pins.

    Rule IDs generated:
        - thermal_asymmetry: one finding per asymmetric 2-pad passive and
          one per PTH pin solid-connected on two or more fill layers.
    """

    rule_id = THERMAL_ASYMMETRY_RULE_ID
    name = "Thermal Asymmetry"
    description = (
        "Detects 2-pad passives with one solid-to-pour and one trace/thermal terminal "
        "(tombstoning risk) and PTH pins tied solid to two or more pour layers "
        "(barrel-fill risk)"
    )

    def __init__(self, *, severity: str = "warning") -> None:
        if severity not in ("error", "warning", "info"):
            raise ValueError(f"invalid severity {severity!r}")
        self.severity = severity

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        """Check every passive and PTH pin against the committed zone fills."""
        del design_rules
        results = DRCResults()
        results.rules_checked = 1
        if not any(getattr(z, "filled_polygons", None) for z in pcb.zones):
            return results
        index = ZoneFillIndex(pcb)
        if index.empty:
            return results

        for asym in asymmetric_passives(pcb, index):
            results.add(self._passive_violation(asym))

        for footprint in pcb.footprints:
            for pad in footprint.pads:
                if pad.type != "thru_hole":
                    continue
                solid = [c for c in index.contacts(pad, footprint) if c.mode == SOLID]
                if len(solid) < MIN_SOLID_PTH_LAYERS:
                    continue
                if _ep_thermal_via_pad(footprint, pad):
                    continue
                results.add(self._pth_violation(footprint, pad, solid))
        return results

    def _passive_violation(self, asym: PassiveAsymmetry) -> DRCViolation:
        from .clearance import _transform_pad_position

        fp = asym.footprint
        solid_label = f"{fp.reference}-{asym.solid_pad.number}"
        other_label = f"{fp.reference}-{asym.other_pad.number}"
        x, y = _transform_pad_position(asym.solid_pad, fp)
        layers = sorted({c.layer for c in asym.solid_contacts if c.mode == SOLID})
        nets = tuple(n for n in (asym.solid_pad.net_name, asym.other_pad.net_name) if n)
        return DRCViolation(
            rule_id=THERMAL_ASYMMETRY_RULE_ID,
            severity=self.severity,
            message=(
                f"{fp.reference} thermal asymmetry (tombstoning risk): pad "
                f"{asym.solid_pad.number} (net '{asym.solid_pad.net_name}') is "
                f"{_connection_text(asym.solid_contacts)}; pad {asym.other_pad.number} "
                f"(net '{asym.other_pad.net_name}') has "
                f"{_connection_text(asym.other_contacts)} -- give the solid terminal a "
                f"thermal relief (or balance the other terminal)"
            ),
            location=(round(x, 3), round(y, 3)),
            layer=layers[0] if layers else None,
            items=(fp.reference, solid_label, other_label),
            nets=nets,
        )

    def _pth_violation(
        self, footprint: Footprint, pad: Pad, solid: list[PadZoneContact]
    ) -> DRCViolation:
        from .clearance import _transform_pad_position

        label = f"{footprint.reference}-{pad.number}"
        x, y = _transform_pad_position(pad, footprint)
        layers = ", ".join(c.layer for c in solid)
        return DRCViolation(
            rule_id=THERMAL_ASYMMETRY_RULE_ID,
            severity=self.severity,
            message=(
                f"PTH pin {label} (net '{pad.net_name}', drill {pad.drill:.2f}mm) is "
                f"connected solid to zone fill on {len(solid)} layers ({layers}); "
                f"barrel fill is at risk when wave-, selective- or hand-soldered -- "
                f"use thermal reliefs on this pin"
            ),
            location=(round(x, 3), round(y, 3)),
            layer=solid[0].layer,
            actual_value=float(len(solid)),
            required_value=float(MIN_SOLID_PTH_LAYERS - 1),
            items=(footprint.reference, label),
            nets=(pad.net_name,) if pad.net_name else (),
        )
