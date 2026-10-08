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

Out of scope: copper-to-zone-fill clearance (the zone rules own it) and custom
``.kicad_dru`` expressions, which are not evaluated as netclass rules.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.core.project_clearance import ProjectClearanceError, authored_netclass_clearances

from ..spatial import candidate_pairs
from ..violations import DRCResults, DRCViolation
from .base import DRC_TOLERANCE
from .clearance import (
    _COLOCATION_EPSILON_MM,
    ClearanceRule,
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
    max_floor = max(floors.values())
    rule = ClearanceRule()
    for layer in pcb.copper_layers:
        elements = rule._collect_elements(pcb, layer.name)
        bounds = [element.bounds() for element in elements]
        for i, j in candidate_pairs(bounds, max_floor + DRC_TOLERANCE):
            a, b = elements[i], elements[j]
            required_class = pair_floor(floors, a.net_number, b.net_number)
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
            required = max(fab_floor, required_class)
            owner = max((a, b), key=lambda e: floors.get(e.net_number, 0.0))
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
