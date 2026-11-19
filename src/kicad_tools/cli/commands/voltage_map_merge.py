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

Machine output follows the canonical idiom (#4674): ``--format json`` emits
one JSON document on stdout carrying the merge summary and the
route-understated pairs (so machine consumers need not scrape stderr), with
``{"error": ...}`` on the failure paths and unchanged exit codes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..format_options import FORMAT_JSON, emit_json


def _fail(as_json: bool, message: str) -> int:
    """Report a merge failure as a document (JSON) or prose (text)."""
    if as_json:
        emit_json(
            {
                "command": "voltage-map-merge",
                "error": message,
                "success": False,
            }
        )
    else:
        print(f"Error: {message}", file=sys.stderr)
    return 1


def _route_understated_list(flagged: dict) -> list[dict]:
    """The understated-pair table in JSON-document form (sorted, plain names)."""
    out = []
    for pair, (census_dv, route_dv) in sorted(flagged.items(), key=lambda kv: sorted(kv[0])):
        a, b = sorted(pair)
        out.append({"nets": [a, b], "census_dv": census_dv, "route_dv": route_dv})
    return out


def _warn_route_understated(merged: dict) -> dict:
    """Stderr warning for pairs the signed-collapse route consumers understate (#9582).

    ``kct route`` / ``kct pcb`` collapse each net's range to its
    largest-magnitude endpoint (:func:`kicad_tools.router.pairwise_clearance.
    load_signed_voltage_map`, #4411), so a pair whose census |dV| rests on
    interval differencing (a sign-flipping net against its opposite bank)
    scores below the census value -- down to 0 V, dropping out of the route
    widening matrix entirely.  The merged envelope is for the interval-aware
    census/audit gate; route-time spacing is driven per scenario.

    Returns the flagged pairs (``{frozenset({a, b}): (census_dv, route_dv)}``)
    so the JSON document can carry them alongside the stderr prose.
    """
    from kicad_tools.router.pairwise_clearance import route_understated_pairs

    flagged = route_understated_pairs(merged)
    if not flagged:
        return flagged
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
    return flagged


def run_voltage_map_merge_command(args) -> int:
    """Handle ``voltage-map-merge``.  Returns the process exit code."""
    from kicad_tools.creepage.engine import merge_voltage_scenarios

    as_json = getattr(args, "format", "text") == FORMAT_JSON

    inputs = [Path(p) for p in args.inputs]
    if len(inputs) < 2:
        return _fail(
            as_json,
            f"voltage-map-merge needs at least two scenario maps (got {len(inputs)})",
        )

    maps: list[dict] = []
    for path in inputs:
        if not path.exists():
            return _fail(as_json, f"scenario map not found: {path}")
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            return _fail(as_json, f"cannot read scenario map {path}: {e}")
        if not isinstance(raw, dict):
            return _fail(
                as_json,
                f"scenario map {path} must be a JSON object, got {type(raw).__name__}",
            )
        maps.append(raw)

    try:
        merged = merge_voltage_scenarios(maps, names=[str(p) for p in inputs])
    except (TypeError, ValueError) as e:
        return _fail(as_json, str(e))

    flagged = _warn_route_understated(merged)

    out = Path(args.output)
    try:
        out.write_text(json.dumps(merged, indent=2) + "\n")
    except OSError as e:
        return _fail(as_json, f"cannot write merged map {out}: {e}")

    n_nets = sum(1 for k in merged if not k.startswith("_"))
    n_bounds = len(merged.get("_pair_bounds", []))
    if as_json:
        emit_json(
            {
                "command": "voltage-map-merge",
                "inputs": [str(p) for p in inputs],
                "output": str(out),
                "nets": n_nets,
                "pair_bounds": n_bounds,
                "route_understated_pairs": _route_understated_list(flagged),
                "success": True,
            }
        )
    else:
        print(
            f"Merged {len(maps)} scenario maps -> {out} "
            f"({n_nets} nets, {n_bounds} generated pair bound(s))"
        )
    return 0
