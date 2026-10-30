"""Authored netclass clearance on routed copper (Issue #6249).

``kct route`` enforces a project's named netclass clearances (``.kicad_pro``
``net_settings``) as per-net minima (#6243): two foreign coppers must be
``max(clearance[a], clearance[b])`` apart, which is KiCad's own netclass rule.
The ``clearance`` family in :mod:`.clearance` measures copper against the
manufacturer floor only, and :mod:`.netclass_floor` checks the netclass
*declarations* against that floor, so before this module ``kct check`` could
call a board manufacturable while ``kicad-cli pcb drc`` flagged
``Clearance violation (netclass 'HV' clearance 0.5000 mm; actual 0.4500 mm)``.

Design
------
* **Same resolver as the router.**  Per-net classes come from
  :func:`kicad_tools.core.project_clearance.authored_netclass_clearances`
  (pinned by a 36-case kicad-cli oracle) and the pair requirement is
  :func:`kicad_tools.router.authored_clearance.pair_floor`, the very predicate
  the router's search, commit and final census ask.  ``Default`` is not an
  authored minimum: it stays the board-wide base owned by the fab tier, so a
  project that declares only ``Default`` reports exactly as before.
* **Geometry is ``clearance``'s.**  Copper is collected and measured with
  :class:`.clearance.CopperElement` / ``_calculate_clearance`` (the shared
  kernel), per copper layer, so this category sees the same copper the fab
  floor rule sees -- including unconnected (net 0) pads and vias on inner
  layers.  The router's ``authored_violations`` census cannot be called
  directly: it needs router ``Pad``/``Route`` objects, which a board file does
  not have.
* **Separate category.**  Findings use rule id ``netclass_clearance_copper`` so
  a fab-floor shortfall (``clearance_*``) and a designer-minimum shortfall stay
  separable.  The requirement reported is ``max(fab floor, class[a],
  class[b])``; a pair is reported here only when a *named class* is stricter
  than the fab floor (otherwise the fab-floor rule already owns it, and a class
  declared below the floor is :mod:`.netclass_floor`'s finding).  A pair that
  breaks both is reported by both.
* **Never silently skipped.**  A declaration the resolver cannot model
  (unsupported schema, pattern or assignment) yields an error
  ``netclass_clearance_unsupported`` instead of an empty result.

``KCT_PRESERVE_BOARD_RULES=0`` (opt-out mode)
---------------------------------------------
That text variable selects what kct writes into the routed board's
``.kicad_dru``; in mode ``0`` the DRU carries plain fab floors, which outrank a
named class in KiCad's DRC, so ``kicad-cli pcb drc`` stays silent about a class
violation there.  ``kct check`` still reports it, as an error, in every mode:
the class is the designer's authored statement in ``.kicad_pro`` (no mode ever
rewrites a named class), the router enforces it in every mode, and a check that
went quiet because a *different* tool's rule file was loosened would repeat
exactly the false "manufacturable" this category exists to remove.  In mode
``0`` the message says that ``kicad-cli`` may not agree, so the discrepancy is
visible rather than mysterious.  A user who truly wants the class ignored
removes or loosens it in ``.kicad_pro``.

Router-declared classes (Issue #6390)
-------------------------------------
``kct route`` declares the router's own stricter classes (``Power``, ``Clock``,
...) in the routed ``.kicad_pro`` and records them under
``meta.kct_router_netclasses``.  They are checked like any class, except that
they do not govern a pad pair inside one footprint: a USB-C connector's pins
or a QFP's corner pads sit closer than 0.2 mm by vendor design, and no re-route
can widen them.  The ``.kicad_dru`` rule kct emits for such a class carries the
same exclusion, so both engines agree.  Nor do they govern the gap between
the two halves of a coupled differential pair (``USB_D+``/``USB_D-``): the
router routes P against N at the class's ``intra_pair_clearance`` (``HighSpeed``
0.075 mm against its 0.15 mm clearance), and the emitted rule carries
``!A.isCoupledDiffPair()``.  The pair test here is
:func:`kicad_tools.manufacturers.netclass_dru.is_coupled_diff_pair`, a port of
KiCad's own name-suffix predicate, so both engines exempt exactly the same
pairs.  The fab floor (the ``clearance`` family) still applies to the pair, and
``diffpair_clearance_intra`` owns the intra-pair minimum.  Authored classes are
unaffected.

Attach zones (Issue #8048)
--------------------------
Copper attaching to a rated part is spaced by that part's own pin pitch, so a
router-class floor does not govern a pair of that footprint's pad nets inside
its courtyard (:mod:`kicad_tools.manufacturers.attach_zone_dru`).  The routed
``.kicad_dru`` carries the same exemption as a managed block, and this check
applies it only when the sibling ``.kicad_dru`` carries that block, from the
same zone definition, so ``kicad-cli`` and ``kct check`` judge a board by the
same rule set.  Designer-authored classes are never exempted.

Out of scope: copper-to-zone-fill clearance (the zone rules own it) and custom
``.kicad_dru`` expressions, which are not evaluated as netclass rules.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.core.project_clearance import ProjectClearanceError, authored_netclass_clearances
from kicad_tools.manufacturers.attach_zone_dru import (
    ATTACH_ZONE_ACTIVE,
    ATTACH_ZONE_NOT_APPLICABLE,
    ATTACH_ZONE_SUPPRESSED,
    FloorZone,
    ZoneItem,
    attach_zone_exemption_status,
    build_floor_zones,
    eligible_net_names,
    zones_exempt,
)

from ..spatial import candidate_pairs
from ..violations import DRCResults, DRCViolation
from .base import DRC_TOLERANCE
from .clearance import (
    _COLOCATION_EPSILON_MM,
    ClearanceRule,
    CopperElement,
    _calculate_clearance,
)

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB

RULE_COPPER = "netclass_clearance_copper"
RULE_UNSUPPORTED = "netclass_clearance_unsupported"
NETCLASS_CLEARANCE_RULE_IDS: tuple[str, ...] = (RULE_COPPER, RULE_UNSUPPORTED)


def load_project(pcb_path: Path) -> dict | None:
    """The sibling ``.kicad_pro`` as a dict, or ``None`` (missing/unreadable)."""
    pro_path = pcb_path.with_suffix(".kicad_pro")
    if not pro_path.is_file():
        return None
    try:
        data = json.loads(pro_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def check_netclass_clearance_copper(
    pcb_path: Path | None,
    pcb: PCB,
    design_rules: DesignRules,
) -> DRCResults:
    """Check routed copper against the project's authored netclass clearances.

    Args:
        pcb_path: Path of the ``.kicad_pcb`` under check (the sibling
            ``.kicad_pro`` is derived from it).  ``None`` is a silent no-op.
        pcb: The parsed board.
        design_rules: Active ``--mfr`` rules (supplies the fab floor).
    """
    results = DRCResults()
    for rule_id in NETCLASS_CLEARANCE_RULE_IDS:
        results.rules_checked_by_rule[rule_id] = 1
    results.rules_checked = len(NETCLASS_CLEARANCE_RULE_IDS)
    if pcb_path is None:
        return results
    project = load_project(pcb_path)
    if project is None:
        return results

    net_names = {net.number: net.name for net in pcb.nets.values() if net.name}
    try:
        authored = authored_netclass_clearances(project, net_names.values())
    except ProjectClearanceError as exc:
        results.add(
            DRCViolation(
                rule_id=RULE_UNSUPPORTED,
                severity="error",
                message=(
                    f"Netclass clearance cannot be verified: {exc}. Copper was NOT checked "
                    "against the project's netclass clearances"
                ),
                items=("net_settings",),
            )
        )
        return results

    floors = {num: authored[name].clearance for num, name in net_names.items() if name in authored}
    classes = {
        num: authored[name].source_class for num, name in net_names.items() if name in authored
    }
    fab_floor = design_rules.min_clearance_mm
    # Only classes stricter than the fab floor add a requirement to ``clearance``.
    if not any(value > fab_floor + DRC_TOLERANCE for value in floors.values()):
        return results

    from kicad_tools.manufacturers.project_generator import (
        PRESERVE_MODE_OFF,
        preserve_board_rules_mode,
    )
    from kicad_tools.router.authored_clearance import pair_floor

    opt_out = preserve_board_rules_mode(project) == PRESERVE_MODE_OFF
    note = (
        " (KCT_PRESERVE_BOARD_RULES=0: the routed .kicad_dru carries plain fab floors, "
        "so kicad-cli pcb drc may not flag this)"
        if opt_out
        else ""
    )
    # Issue #6390: classes kct declared from the router's map are a routing
    # intent for the copper the router places.  They do not govern pad pairs
    # inside one footprint (a connector's or fine-pitch package's own pin
    # gaps), nor the P/N gap of a coupled diff pair (routed at the class's
    # intra-pair clearance), exactly like the kicad-cli rule kct emits for
    # them (``isCoupledDiffPair()``, mirrored name-for-name); an authored
    # class still governs both.  Issue #6429: the pairs the router coupled
    # under names KiCad does not recognise (``X_POS``/``X_NEG``) are recorded
    # in ``meta.kct_router_diff_pairs`` and exempted by the same predicate the
    # emitted .kicad_dru rule encodes.
    from kicad_tools.manufacturers.netclass_dru import is_router_coupled_pair
    from kicad_tools.manufacturers.project_generator import (
        recorded_router_coupled_pairs,
        router_netclass_provenance,
    )

    router_classes = set(router_netclass_provenance(project)[0])
    recorded_pairs = recorded_router_coupled_pairs(project)
    package_floors = {
        num: value for num, value in floors.items() if classes[num] not in router_classes
    }
    zones, shadow_zones = _attach_zones(
        pcb_path, pcb, project, net_names.values(), bool(router_classes), results
    )
    max_floor = max(floors.values())
    rule = ClearanceRule()
    for layer in pcb.copper_layers:
        elements = rule._collect_elements(pcb, layer.name)
        bounds = [element.bounds() for element in elements]
        for i, j in candidate_pairs(bounds, max_floor + DRC_TOLERANCE):
            a, b = elements[i], elements[j]
            same_package = (
                a.element_type == "pad"
                and b.element_type == "pad"
                and a.source_footprint is not None
                and a.source_footprint is b.source_footprint
            )
            coupled = is_router_coupled_pair(a.net_name, b.net_name, recorded_pairs)
            governing = package_floors if same_package or coupled else floors
            required_class = pair_floor(governing, a.net_number, b.net_number)
            if required_class <= fab_floor + DRC_TOLERANCE:
                continue
            if a.element_type != "segment" and b.element_type != "segment":
                if any(
                    e.element_type == "via"
                    and e.explicit_layers
                    and layer.name not in e.explicit_layers
                    for e in (a, b)
                ):
                    continue
            if {a.element_type, b.element_type} == {"segment", "via"}:
                seg, via = (a, b) if a.element_type == "segment" else (b, a)
                sx1, sy1, sx2, sy2, _ = seg.geometry
                vx, vy = via.geometry[0], via.geometry[1]
                if (
                    math.hypot(sx1 - vx, sy1 - vy) < _COLOCATION_EPSILON_MM
                    or math.hypot(sx2 - vx, sy2 - vy) < _COLOCATION_EPSILON_MM
                ):
                    continue
            clearance, x, y = _calculate_clearance(a, b)
            if clearance + DRC_TOLERANCE >= required_class:
                continue
            # Issue #8048: an attach zone lifts the router-class floor (never a
            # designer's: only router-class and unclassed nets are members).
            if zones and zones_exempt(zones, _zone_item(a, layer.name), _zone_item(b, layer.name)):
                continue
            if shadow_zones and zones_exempt(
                shadow_zones, _zone_item(a, layer.name), _zone_item(b, layer.name)
            ):
                results.attach_zone_unexempted += 1
            required = max(fab_floor, required_class)
            owner = max((a, b), key=lambda e: governing.get(e.net_number, 0.0))
            cls = classes[owner.net_number]
            results.add(
                DRCViolation(
                    rule_id=RULE_COPPER,
                    severity="error",
                    message=(
                        f"{a.element_type.title()} to {b.element_type} clearance "
                        f"{clearance:.3f}mm < netclass {cls!r} minimum {required:.3f}mm{note}"
                    ),
                    location=(round(x, 3), round(y, 3)),
                    layer=layer.name,
                    actual_value=round(clearance, 4),
                    required_value=required,
                    items=(a.reference, b.reference),
                    nets=(a.net_name, b.net_name),
                )
            )
    return results


def _attach_zones(
    pcb_path: Path,
    pcb: PCB,
    project: dict,
    net_names: Iterable[str],
    router_classes_declared: bool,
    results: DRCResults,
) -> tuple[tuple[FloorZone, ...], tuple[FloorZone, ...]]:
    """``(zones in force, zones a designer rule suppressed)``; records the status (#8131).

    Zones are in force only when the sibling ``.kicad_dru`` carries the managed
    block.  When a designer clearance rule keeps the block out, the zones that
    would have existed are returned second so the caller can count the findings
    left unexempted.
    """
    try:
        dru_text = pcb_path.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return (), ()
    status, reason = attach_zone_exemption_status(dru_text, router_classes_declared)
    if status == ATTACH_ZONE_NOT_APPLICABLE:
        return (), ()
    zones = build_floor_zones(pcb.footprints, eligible_net_names(project, net_names))
    if status == ATTACH_ZONE_ACTIVE:
        if zones:
            results.attach_zone_exemption = ATTACH_ZONE_ACTIVE
        return zones, ()
    if zones:
        results.attach_zone_exemption = ATTACH_ZONE_SUPPRESSED
        results.attach_zone_exemption_reason = reason
    return (), zones


def _zone_item(element: CopperElement, layer: str) -> ZoneItem:
    """``element`` as the attach-zone predicate sees it on copper layer ``layer``."""
    from shapely.geometry import LineString, Point, box  # type: ignore[import-untyped]

    if element.element_type == "segment":
        x1, y1, x2, y2, width = element.geometry
        shape = LineString([(x1, y1), (x2, y2)]).buffer(width / 2.0)
    elif element.element_type == "via":
        shape = Point(element.geometry[0], element.geometry[1]).buffer(element.geometry[2] / 2.0)
    elif element.polygon is not None:
        shape = element.polygon
    else:
        x, y, width, height = element.geometry
        shape = box(x - width / 2.0, y - height / 2.0, x + width / 2.0, y + height / 2.0)
    return ZoneItem(shape, element.net_name, layer, element.element_type)


def netclass_pair_requirement(
    pcb_path: Path | None,
    pcb: PCB,
    fab_floor: float,
) -> Callable[[int, int], float]:
    """``(net_a, net_b) -> mm``: the copper clearance this category demands.

    Issue #7296: a post-route pass that moves copper (the pad-edge via
    relocation) must clear the same per-pair requirement this check enforces,
    not just the fab floor, or it moves a ``Power`` via to 0.155 mm from a
    signal track and this category fails the board.  The answer is
    ``max(fab_floor, pair_floor(...))`` under the governance the check uses
    for a via against other copper: a router-declared class does not govern
    the P/N gap of a coupled pair (the ``same_package`` exemption is pad-pad
    only, so it never applies to a via).  Same-net copper needs nothing.

    Raises :class:`ProjectClearanceError` when the project declares classes
    the resolver cannot model -- the caller must fail closed, exactly as the
    check reports ``netclass_clearance_unsupported`` instead of passing.
    Without a sibling ``.kicad_pro`` every foreign pair needs the fab floor.
    """
    from kicad_tools.router.authored_clearance import pair_floor

    project = load_project(pcb_path) if pcb_path is not None else None
    if project is None:
        return lambda a, b: 0.0 if a == b and a != 0 else fab_floor

    net_names = {net.number: net.name for net in pcb.nets.values() if net.name}
    authored = authored_netclass_clearances(project, net_names.values())
    floors = {num: authored[name].clearance for num, name in net_names.items() if name in authored}

    from kicad_tools.manufacturers.netclass_dru import is_router_coupled_pair
    from kicad_tools.manufacturers.project_generator import (
        recorded_router_coupled_pairs,
        router_netclass_provenance,
    )

    router_classes = set(router_netclass_provenance(project)[0])
    recorded_pairs = recorded_router_coupled_pairs(project)
    package_floors = {
        num: value
        for num, name in net_names.items()
        if name in authored
        for value in (authored[name].clearance,)
        if authored[name].source_class not in router_classes
    }

    def requirement(net_a: int, net_b: int) -> float:
        if net_a == net_b and net_a != 0:
            return 0.0
        coupled = is_router_coupled_pair(net_names.get(net_a), net_names.get(net_b), recorded_pairs)
        governing = package_floors if coupled else floors
        return max(fab_floor, pair_floor(governing, net_a, net_b))

    return requirement
