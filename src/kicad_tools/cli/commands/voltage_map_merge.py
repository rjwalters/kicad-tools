"""``kct voltage-map-merge``: merge per-scenario voltage maps into one envelope (#9582).

Authoring one ``--voltage-map`` per operating scenario and merging them (via
:func:`kicad_tools.creepage.engine.merge_voltage_scenarios`) replaces the
hand-written envelope map of ranges plus per-pair ``_pair_bounds`` entries.
The merged output is an ordinary voltage map for the interval-aware consumers
(``kct creepage``, ``kct audit``, ``kct repair-creepage``, ``kct route-link``).
``kct route`` / ``kct pcb`` collapse each net's range to its largest-magnitude
endpoint (#4411), so they can understate a sign-flipping pair -- the command
warns on stderr when its output contains such pairs: drive route-time spacing
with a per-scenario map and use the envelope for the census/audit gate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _warn_route_understated(merged: dict) -> None:
    """Stderr warning for pairs the signed-collapse route consumers understate (#9582).

    ``kct route`` / ``kct pcb`` collapse each net's range to its
    largest-magnitude endpoint (:func:`kicad_tools.router.pairwise_clearance.
    load_signed_voltage_map`, #4411), so a pair whose census |dV| rests on
    interval differencing (a sign-flipping net against its opposite bank)
    scores below the census value -- down to 0 V, dropping out of the route
    widening matrix entirely.  The merged envelope is for the interval-aware
    census/audit gate; route-time spacing is driven per scenario.
    """
    from kicad_tools.router.pairwise_clearance import route_understated_pairs

    flagged = route_understated_pairs(merged)
    if not flagged:
        return
    print(
        f"Warning: {len(flagged)} pair(s) in this merged envelope are understated by "
        "the route-time consumers (`kct route`, `kct pcb`): they collapse each net's "
        "range to its largest-magnitude endpoint (issue #4411), scoring below the "
        "census value:",
        file=sys.stderr,
    )
    for pair, (census_dv, route_dv) in sorted(flagged.items(), key=lambda kv: sorted(kv[0])):
        a, b = sorted(pair)
        print(
            f"  {a} <-> {b}: census {census_dv:g} V, route-time {route_dv:g} V",
            file=sys.stderr,
        )
    print(
        "  Use the merged map with the interval-aware gate (`kct creepage`, `kct audit`); "
        "drive `kct route`/`kct pcb` spacing with a per-scenario map.",
        file=sys.stderr,
    )


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

    _warn_route_understated(merged)

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
