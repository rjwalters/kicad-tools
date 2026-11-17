"""Deprecated alias of :mod:`kicad_tools.router.pairplan.gateway` (issue #6837).

The fan-via terminal gateway generator (issue #6444, slice 2a) is engine-free,
so epic #6441 slice 2c-i moved it unchanged to ``router/pairplan/``.  This
module only re-exports it so existing imports keep working; new code should
import from ``kicad_tools.router.pairplan.gateway``.  The alias goes away with
P6 (#6448).
"""

from __future__ import annotations

from ..pairplan.gateway import (  # noqa: F401 -- deprecated re-exports
    BEND_WEIGHT_MM,
    FAMILIES,
    GATEWAY_SETBACK_STEP_MM,
    HEADINGS,
    MAX_GATEWAY_CANDIDATES,
    MAX_GATEWAY_COMBOS,
    MAX_GATEWAY_SETBACK_MM,
    Gateway,
    GatewaySite,
    _poly_len,
    _unit,
    enumerate_gateways,
    gateway_anchor_point,
    gateway_side,
    handedness_consistent,
    propose_gateway_sites,
    rank_gateway_combos,
)
