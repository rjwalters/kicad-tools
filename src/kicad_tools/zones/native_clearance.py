"""Persist the legacy post-fill clearance target as native zone rules.

Unlike editing ``filled_polygon`` rings, these rules survive a subsequent
native refill. Native KiCad retains responsibility for real pad geometry,
thermal connections, polygon holes and island removal.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from kicad_tools.sexp import parse_file, parse_string
from kicad_tools.zones.fill_clearance import _build_net_name_map, _net_key

_BEGIN = "# BEGIN kct native zone clearance\n"
_END = "# END kct native zone clearance\n"


def write_native_zone_clearance_rules(pcb_path: Path) -> None:
    """Replace our DRU block, preserving all other factory and user rules.

    The target deliberately preserves the existing carve's conservative
    ``zone clearance + minimum thickness / 2`` distance. It does not assert
    that modern solid fills need stroke inflation. The change is which
    engine enforces this existing distance, not a relaxation of it.
    """
    doc = parse_file(pcb_path)
    names = _build_net_name_map(doc)
    targets: dict[tuple[str, str], float] = {}
    for zone in doc.find_all("zone"):
        net = _net_key(zone.find("net"), names)
        if net is None or zone.find("keepout") is not None:
            continue
        if any(char in net for char in "'\\\n\r"):
            raise ValueError("Native zone clearance cannot quote this net name safely")
        layer_node = zone.find("layer") or zone.find("layers")
        if layer_node is None:
            raise ValueError("Native zone clearance requires explicit zone layers")
        layers = [value for value in layer_node.values if isinstance(value, str)]
        connect = zone.find("connect_pads")
        clearance_node = connect.find("clearance") if connect is not None else None
        thickness_node = zone.find("min_thickness")
        clearance = clearance_node.get_float(0) if clearance_node is not None else 0.3
        thickness = thickness_node.get_float(0) if thickness_node is not None else 0.25
        if (
            clearance is None
            or thickness is None
            or not all(math.isfinite(value) and value >= 0 for value in (clearance, thickness))
        ):
            raise ValueError("Native zone clearance requires finite nonnegative dimensions")
        for layer in layers:
            if not layer.endswith(".Cu") or any(char in layer for char in "'\\\n\r"):
                raise ValueError("Native zone clearance requires concrete copper layers")
            key = (layer, net)
            targets[key] = max(targets.get(key, 0.0), clearance + thickness / 2)

    rules = []
    for (layer, net), distance in sorted(targets.items()):
        condition = (
            f"(A.Type == 'Zone' && A.NetName == '{net}') || "
            f"(B.Type == 'Zone' && B.NetName == '{net}')"
        )
        rules.append(
            f"(rule {json.dumps(f'Native zone clearance {layer} {net}')}\n"
            f"  (layer {json.dumps(layer)})\n"
            f"  (condition {json.dumps(condition)})\n"
            f"  (constraint clearance (min {distance:.9g}mm)))\n"
        )
    path = pcb_path.with_suffix(".kicad_dru")
    previous = path.read_text() if path.exists() else "(version 1)\n"
    if _BEGIN in previous:
        before, managed = previous.split(_BEGIN, 1)
        if _END not in managed:
            raise ValueError("Unterminated native zone clearance rule block")
        _, after = managed.split(_END, 1)
        previous = before + after
    # KiCad gives later custom rules precedence. Do not allow our appended
    # zone rule to weaken an existing stricter clearance rule. Resolving
    # arbitrary native conditions here would duplicate KiCad's evaluator;
    # conservatively reject such configurations instead.
    existing = parse_string("(rules\n" + re.sub(r"(?m)^\s*#.*$", "", previous) + "\n)")
    for constraint in existing.find_all("constraint"):
        if constraint.get_string(0) != "clearance":
            continue
        minimum = constraint.find("min")
        token = minimum.get_string(0) if minimum is not None else None
        if token is None or not token.endswith("mm"):
            raise ValueError("Existing native clearance must have an explicit mm minimum")
        value = float(token[:-2])
        if not math.isfinite(value) or any(value > target for target in targets.values()):
            raise ValueError("Native zone clearance would override a stronger existing rule")
    block = _BEGIN + "".join(rules) + _END
    path.write_text(previous.rstrip() + "\n\n" + block)
