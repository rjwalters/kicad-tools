"""Attach-zone exemption from router-class clearance floors (Issue #8048).

Operator ruling 2026-10-28 (#8029): the #4506 *attach zone* -- the copper
attaching to a rated part, whose own pin pitch dictates the spacing -- is a real
clearance exemption for the router's own net-class floors, in every engine that
judges them.  Four consumers share the one definition in this module:

* the emitted ``.kicad_dru`` (:func:`merge_attach_zone_block`),
* ``kct check``'s ``netclass_clearance_copper`` (:meth:`FloorZone.exempts`),
* the router's post-route floor backstop
  (:func:`kicad_tools.router.authored_clearance.attach_zone_floor_waiver`),
* the routed-board writer that decides which zones exist
  (:func:`build_floor_zones`).

The zone shape
--------------
The router's search zone is the bounding box of a footprint's connected pads
plus a margin.  A ``.kicad_dru`` condition cannot say "inside this rectangle",
but it can say ``A.intersectsCourtyard('U1')``.  Per the ruling the router side
shrinks to whatever the DRU can say, never the reverse, so a *floor* zone is:

* **footprint-scoped** -- the copper of BOTH objects of the pair intersects the
  courtyard of footprint ``ref`` (measured on kicad-cli 10.0.7:
  ``intersectsCourtyard`` is true for tracks, vias and pads on every copper
  layer, regardless of which layer the courtyard is on);
* **net-scoped** -- both nets are pad nets of that footprint, and neither
  carries a designer-authored class (a designer minimum is never relaxed);
* **layer-scoped** (#4699) -- a net with only SMD pads here is a member on the
  layers its pads occupy.  A through-hole net is a member on every layer, and
  a via is layer-agnostic, exactly as the search treats them.  (kicad-cli
  reports a through-hole pad's ``Layer`` as its first copper layer only, so no
  layer clause is written for such a net.)

A footprint with no resolvable courtyard has no floor zone: the DRU has no
expression for it.

What the relaxation is
----------------------
Only the router-class floor is lifted.  The emitted rule drops the clearance of
a pair inside a zone to the board-wide base (the manufacturer floor merged with
the authored project minimum and the ``Default`` class), never below it.

The rules go in a managed block appended after every other clearance rule
(later rules win in KiCad), in every preserve mode.  A board whose unmanaged
``.kicad_dru`` text already carries a clearance constraint of its own gets no
block: that constraint is a designer minimum a later rule would override.
``kct check`` applies the exemption only when the sibling ``.kicad_dru``
carries the block, so the two engines always judge the same board by the same
rule set.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.schema.pcb import Footprint

#: Sentinel markers of the kct-owned attach-zone block.
DRU_ATTACH_ZONE_BLOCK_BEGIN = (
    "# BEGIN kct attach-zone router-class exemptions (Issue #8048) -- managed, do not edit"
)
DRU_ATTACH_ZONE_BLOCK_END = "# END kct attach-zone router-class exemptions"

#: Rule-name prefix of every generated rule.
ATTACH_ZONE_RULE_PREFIX = "kct attach zone - "

#: KiCad's "this pad exists on every copper layer" wildcard.
ALL_COPPER_LAYERS = "*.Cu"

_DRU_VERSION_HEADER = "(version 1)"

#: A designer-authored ``(constraint clearance ...)`` in unmanaged DRU text.
_CLEARANCE_CONSTRAINT_RE = re.compile(r"\(\s*constraint\s+clearance\b")


def _dru_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


@dataclass(frozen=True)
class ZoneItem:
    """One piece of copper, as a floor zone judges it.

    ``shape`` is a shapely geometry in the zone's frame.  ``layer`` is the
    copper layer being judged (``None`` for a layer-agnostic via).  ``kind`` is
    ``"segment"``, ``"pad"`` or ``"via"``.
    """

    shape: Any
    net: str
    layer: str | None
    kind: str


@dataclass(frozen=True)
class FloorZone:
    """A footprint's router-class clearance exemption (see the module docstring)."""

    ref: str
    #: ``(net name, copper layers)``; a net with :data:`ALL_COPPER_LAYERS`
    #: among its layers is a member on every layer.
    members: tuple[tuple[str, frozenset[str]], ...]
    #: The footprint's courtyard polygon (shapely), in the footprint frame.
    courtyard: Any = field(compare=False, hash=False, repr=False, default=None)

    def member_layers(self, net: str) -> frozenset[str] | None:
        for name, layers in self.members:
            if name == net:
                return layers
        return None

    def _item_ok(self, item: ZoneItem) -> bool:
        layers = self.member_layers(item.net)
        if layers is None:
            return False
        if item.kind == "via" or ALL_COPPER_LAYERS in layers or not layers:
            return True
        return item.layer in layers

    def exempts(self, a: ZoneItem, b: ZoneItem) -> bool:
        """True when this zone lifts the router-class floor between ``a`` and ``b``."""
        if a.net == b.net or not self._item_ok(a) or not self._item_ok(b):
            return False
        return bool(self.courtyard.intersects(a.shape) and self.courtyard.intersects(b.shape))

    def shifted(self, dx: float, dy: float) -> FloorZone:
        """The zone translated by ``(dx, dy)`` (board-relative -> sheet-absolute)."""
        from shapely.affinity import translate  # type: ignore[import-untyped]

        return FloorZone(self.ref, self.members, translate(self.courtyard, dx, dy))

    def condition(self) -> str:
        """The DRC condition true for the pairs this zone exempts."""
        ref = _dru_string(self.ref)
        return (
            f"A.intersectsCourtyard('{ref}') && B.intersectsCourtyard('{ref}') && "
            f"{self._side_term('A')} && {self._side_term('B')}"
        )

    def _side_term(self, side: str) -> str:
        terms = []
        for net, layers in self.members:
            name = f"{side}.NetName == '{_dru_string(net)}'"
            if not layers or ALL_COPPER_LAYERS in layers:
                terms.append(f"({name})")
                continue
            on = " || ".join(f"{side}.Layer == '{_dru_string(layer)}'" for layer in sorted(layers))
            terms.append(f"({name} && ({side}.Type == 'Via' || {on}))")
        return "(" + " || ".join(terms) + ")"


def zones_exempt(zones: Iterable[FloorZone], a: ZoneItem, b: ZoneItem) -> bool:
    """True when any zone lifts the router-class floor between ``a`` and ``b``."""
    return any(zone.exempts(a, b) for zone in zones)


def eligible_net_names(project: dict | None, net_names: Iterable[str]) -> set[str]:
    """Nets that may be zone members: no designer-authored class (never relaxed).

    A net is eligible when it has no authored class at all, or its class is one
    kct declared from the router's map (``meta.kct_router_netclasses``).  A
    project the resolver cannot model yields no eligible net -- fail closed.
    """
    names = set(net_names)
    if project is None:
        return names
    from kicad_tools.core.project_clearance import (
        ProjectClearanceError,
        authored_netclass_clearances,
    )

    from .project_generator import router_netclass_provenance

    try:
        authored = authored_netclass_clearances(project, names)
    except ProjectClearanceError:
        return set()
    router_classes = set(router_netclass_provenance(project)[0])
    return {
        name
        for name in names
        if name not in authored or authored[name].source_class in router_classes
    }


def build_floor_zones(
    footprints: Iterable[Footprint],
    eligible: Callable[[str], bool] | Iterable[str] | None = None,
) -> tuple[FloorZone, ...]:
    """The floor zones of a board's footprints, sorted by reference.

    ``eligible`` restricts membership (see :func:`eligible_net_names`); ``None``
    admits every pad net.  Footprints with a blank or duplicated reference, no
    resolvable courtyard, or fewer than two member nets have no zone.
    """
    from shapely.geometry import Polygon  # type: ignore[import-untyped]

    from kicad_tools.geometry.courtyard import _courtyard_polygon

    if eligible is None:
        allowed: Callable[[str], bool] = lambda _name: True  # noqa: E731
    elif callable(eligible):
        allowed = eligible
    else:
        names = set(eligible)
        allowed = names.__contains__

    footprints = list(footprints)
    counts: dict[str, int] = {}
    for fp in footprints:
        counts[fp.reference] = counts.get(fp.reference, 0) + 1

    zones: list[FloorZone] = []
    for fp in footprints:
        if not fp.reference or counts[fp.reference] != 1:
            continue
        layers_by_net: dict[str, set[str]] = {}
        for pad in fp.pads:
            net = pad.net_name
            if not net or not allowed(net):
                continue
            copper = {layer for layer in (pad.layers or ()) if str(layer).endswith(".Cu")}
            layers_by_net.setdefault(net, set()).update(copper)
        if len(layers_by_net) < 2:
            continue
        side = "B" if str(fp.layer).startswith("B.") else "F"
        courtyard = _courtyard_polygon(fp, side, Polygon)
        if courtyard is None:
            continue
        members = tuple((net, frozenset(layers)) for net, layers in sorted(layers_by_net.items()))
        zones.append(FloorZone(fp.reference, members, courtyard))
    return tuple(sorted(zones, key=lambda zone: zone.ref))


# ---------------------------------------------------------------------------
# .kicad_dru emission
# ---------------------------------------------------------------------------


def render_attach_zone_rule(zone: FloorZone, clearance: float) -> str:
    """One relaxed clearance rule for ``zone`` (same grammar as the class rules)."""
    return (
        f"(rule {json.dumps(ATTACH_ZONE_RULE_PREFIX + zone.ref)}\n"
        f"  (condition {json.dumps(zone.condition())})\n"
        f"  (constraint clearance (min {clearance}mm)))"
    )


def has_attach_zone_block(dru_text: str | None) -> bool:
    """Whether ``.kicad_dru`` content carries the managed attach-zone block."""
    return bool(dru_text) and DRU_ATTACH_ZONE_BLOCK_BEGIN in (dru_text or "")


def strip_attach_zone_block(text: str) -> str:
    """``text`` without the managed attach-zone block."""
    return re.sub(
        r"\n*"
        + re.escape(DRU_ATTACH_ZONE_BLOCK_BEGIN)
        + r".*?"
        + re.escape(DRU_ATTACH_ZONE_BLOCK_END),
        "",
        text,
        flags=re.DOTALL,
    )


def merge_attach_zone_block(
    existing: str | None, zones: Sequence[FloorZone], clearance: float
) -> str | None:
    """Merge the attach-zone block into ``.kicad_dru`` content.

    Same contract as :func:`~kicad_tools.manufacturers.netclass_dru.merge_netclass_block`:
    zones present -> replace any block with a fresh one appended at the end;
    none -> remove a stale block, otherwise return ``existing`` unchanged.
    The block is appended last so it outranks every earlier clearance rule.
    Idempotent.
    """
    had_block = existing is not None and DRU_ATTACH_ZONE_BLOCK_BEGIN in existing
    base = strip_attach_zone_block(existing).lstrip("\n") if had_block and existing else existing
    if not zones:
        if not had_block or base is None:
            return existing
        return base.rstrip("\n") + "\n" if base.strip() else ""

    body = [
        "# Copper attaching to a rated part is spaced by that part's own pin pitch:",
        "# inside a footprint's courtyard, between its pad nets, the router's net-class",
        "# floor drops to the board-wide base clearance. Designer minima are untouched.",
        *(render_attach_zone_rule(zone, clearance) for zone in zones),
    ]
    block = f"{DRU_ATTACH_ZONE_BLOCK_BEGIN}\n" + "\n".join(body) + f"\n{DRU_ATTACH_ZONE_BLOCK_END}"
    if base is None or not base.strip():
        return f"{_DRU_VERSION_HEADER}\n\n{block}\n"
    prefix = base.rstrip("\n") + "\n"
    if not re.search(r"^\s*\(version\b", base, re.MULTILINE):
        prefix = f"{_DRU_VERSION_HEADER}\n" + prefix
    return f"{prefix}\n{block}\n"


def has_designer_clearance_rule(dru_text: str | None) -> bool:
    """Whether unmanaged ``.kicad_dru`` text carries its own clearance constraint."""
    if not dru_text:
        return False
    from .project_generator import _strip_managed_dru_blocks

    return _CLEARANCE_CONSTRAINT_RE.search(_strip_managed_dru_blocks(dru_text)) is not None


ATTACH_ZONE_ACTIVE = "active"
ATTACH_ZONE_SUPPRESSED = "suppressed"
ATTACH_ZONE_NOT_APPLICABLE = "not_applicable"
SUPPRESSED_BY_DESIGNER_RULE = "designer_clearance_rule"


def attach_zone_exemption_status(
    dru_text: str | None, router_classes_declared: bool
) -> tuple[str, str | None]:
    """``(status, reason)`` for the attach-zone exemption on a ``.kicad_dru`` (#8131).

    The one decision point shared by the DRU writer and ``kct check``:
    ``active`` when the managed block is present, ``suppressed`` (reason
    ``designer_clearance_rule``) when router classes are declared but the
    designer's own clearance rule keeps the block out (#8060), otherwise
    ``not_applicable``.  Whether the board has any zone is the caller's
    refinement: a ``suppressed`` answer with no zone is ``not_applicable``.
    """
    if has_attach_zone_block(dru_text):
        return ATTACH_ZONE_ACTIVE, None
    if router_classes_declared and has_designer_clearance_rule(dru_text):
        return ATTACH_ZONE_SUPPRESSED, SUPPRESSED_BY_DESIGNER_RULE
    return ATTACH_ZONE_NOT_APPLICABLE, None
