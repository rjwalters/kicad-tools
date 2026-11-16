"""Zone-scoped clearance rules for the ``.kicad_dru`` managed block (#8937).

The unconditional ``Clearance`` fab-floor rule in the managed block
*replaces* -- KiCad custom rules are not floors -- every larger clearance
that would otherwise govern a zone's fill: a zone's own ``(connect_pads
(clearance …))``.  Measured on the pinned toolchain (kicad-cli 10.0.6,
``kicad-cli pcb drc --refill-zones --save-board``): a pour authored at
0.3 mm filled to 0.1021 mm from a foreign THT pad with the floor-only
block, and to 0.3005 mm once a zone-scoped rule at 0.3 mm followed the
floor.  A zone-scoped condition is honoured by the zone filler itself, not
merely by DRC, and the ``A.Type`` spelling is case-insensitive ('Zone' /
'zone' / 'ZONE' all match).

This module restores the zone half of the authored clearance, the same way
``Reviewed clearance - <class>`` rules (:func:
`~kicad_tools.manufacturers.project_generator.generate_project_dru`) restore
the net-class half: one rule per ``(layer, net)`` zone target at
``max(fab floor, authored zone clearance)``, so the fab floor still binds a
zone authored *below* it.  The rules join the reviewed net-class rules in
one ascending-clearance emission -- KiCad gives the last matching rule
precedence, so a global ascending order makes the strictest matching rule
win for every pair, which is the native ``max(zone, net class)`` semantics
a flat rule file cannot otherwise express.

The condition mirrors the established ``zones/native_clearance.py`` spelling
(``A.Type == 'Zone' && A.NetName == '<n>'``, both orders), scoped by the
rule-level ``(layer ...)``; that block is a separate, unrefactored writer
(#8937 out of scope) and the two coexist.
"""

from __future__ import annotations

import json
from pathlib import Path

from kicad_tools.sexp import parse_file

#: Rule-name prefix of every generated zone-clearance rule.
ZONE_CLEARANCE_RULE_PREFIX = "Zone clearance - "


def _dru_string(value: str) -> str:
    """Escape a net name for a single-quoted DRU condition literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def zone_clearance_targets(board: Path) -> list[tuple[str, str, float]]:
    """Read every copper zone's authored ``connect_pads`` clearance.

    Returns one ``(layer, net, clearance_mm)`` triple per concrete copper
    layer of each non-keepout zone that declares an explicit
    ``(connect_pads (clearance …))`` value, collapsing zones that share a
    ``(layer, net)`` target to the strictest (largest) authored clearance.
    Zones without an explicit clearance are skipped: there is no authored
    value to restore, and the fab floor governs them exactly as before.
    Non-copper and wildcard layer lists are skipped (the DRU ``layer``
    scope needs a concrete name), as are keepouts (no copper fill).
    """
    doc = parse_file(board)
    from kicad_tools.zones.fill_clearance import _build_net_name_map, _net_key

    names = _build_net_name_map(doc)
    targets: dict[tuple[str, str], float] = {}
    for zone in doc.find_all("zone"):
        if zone.find("keepout") is not None:
            continue
        connect = zone.find("connect_pads")
        if connect is None:
            continue
        clearance_node = connect.find("clearance")
        if clearance_node is None:
            continue
        clearance = clearance_node.get_float(0)
        if clearance is None or clearance < 0:
            continue
        net = _net_key(zone.find("net"), names)
        if net is None:
            continue
        layer_node = zone.find("layer") or zone.find("layers")
        if layer_node is None:
            continue
        for layer in layer_node.values:
            if not isinstance(layer, str) or not layer.endswith(".Cu"):
                continue
            if layer.startswith("*.") or "*" in layer:
                continue
            key = (layer, net)
            targets[key] = max(targets.get(key, 0.0), clearance)
    return [(layer, net, value) for (layer, net), value in sorted(targets.items())]


def zone_condition(net: str) -> str:
    """The DRC condition selecting pairs where the ``net`` zone is a member."""
    escaped = _dru_string(net)
    return (
        f"(A.Type == 'Zone' && A.NetName == '{escaped}') || "
        f"(B.Type == 'Zone' && B.NetName == '{escaped}')"
    )


def zone_clearance_rule(layer: str, net: str, clearance: float) -> str:
    """One zone-scoped clearance rule (same grammar as the reviewed rules)."""
    name = ZONE_CLEARANCE_RULE_PREFIX + f"{net} ({layer})"
    return (
        f"(rule {json.dumps(name)}\n"
        f"  (layer {json.dumps(layer)})\n"
        f"  (condition {json.dumps(zone_condition(net))})\n"
        f"  (constraint clearance (min {clearance}mm)))\n"
    )
