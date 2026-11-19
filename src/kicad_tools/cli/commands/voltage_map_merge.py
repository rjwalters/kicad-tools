"""``kct voltage-map-merge``: merge per-scenario voltage maps into one envelope (#9582).

Authoring one ``--voltage-map`` per operating scenario and merging them (via
:func:`kicad_tools.creepage.engine.merge_voltage_scenarios`) replaces the
hand-written envelope map of ranges plus per-pair ``_pair_bounds`` entries.
The merged output is an ordinary voltage map, so every existing
``--voltage-map`` consumer works unchanged.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def run_voltage_map_merge_command(args) -> int:
    """Handle ``voltage-map-merge``.  Returns the process exit code."""
    from kicad_tools.creepage.engine import merge_voltage_scenarios

    inputs = [Path(p) for p in args.inputs]
    if len(inputs) < 2:
        print(
            f"Error: voltage-map-merge needs at least two scenario maps (got {len(inputs)})",
            file=sys.stderr,
        )
        return 1

    maps: list[dict] = []
    for path in inputs:
        if not path.exists():
            print(f"Error: scenario map not found: {path}", file=sys.stderr)
            return 1
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"Error: cannot read scenario map {path}: {e}", file=sys.stderr)
            return 1
        if not isinstance(raw, dict):
            print(
                f"Error: scenario map {path} must be a JSON object, got {type(raw).__name__}",
                file=sys.stderr,
            )
            return 1
        maps.append(raw)

    try:
        merged = merge_voltage_scenarios(maps, names=[str(p) for p in inputs])
    except (TypeError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    out = Path(args.output)
    try:
        out.write_text(json.dumps(merged, indent=2) + "\n")
    except OSError as e:
        print(f"Error: cannot write merged map {out}: {e}", file=sys.stderr)
        return 1

    n_nets = sum(1 for k in merged if not k.startswith("_"))
    n_bounds = len(merged.get("_pair_bounds", []))
    print(
        f"Merged {len(maps)} scenario maps -> {out} "
        f"({n_nets} nets, {n_bounds} generated pair bound(s))"
    )
    return 0
