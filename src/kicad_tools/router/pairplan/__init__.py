"""Engine-free diff-pair planning (epic #6441, issue #6837).

The pieces of coupled diff-pair routing that do not depend on any routing
engine: which pad couples a pair's coupled runs join (:mod:`.chain`), the
generated equivalent-pin ties (:mod:`.tie`) and the fan-via terminal gateway
generator (:mod:`.gateway`).  They were written for the lattice engine and
moved here unchanged so the grid engine can consume them too (P6, #6448).

**Dependency direction.**  ``pairplan`` imports nothing from
``router.lattice``, ``router.mesh``, ``router.core`` or
``router.diffpair_routing``; the engines import ``pairplan``, never the
reverse.  Engine-specific legality reaches this package only as callbacks.
``tests/router/pairplan/test_import_boundary.py`` enforces this.
"""

from __future__ import annotations

from .chain import (
    Couple,
    PairChain,
    choose_pair_chain,
    choose_pair_endpoints,
    choose_pair_tap_chain,
)
from .gateway import (
    HEADINGS,
    Gateway,
    GatewaySite,
    enumerate_gateways,
    gateway_anchor_point,
    gateway_side,
    handedness_consistent,
    propose_gateway_sites,
    rank_gateway_combos,
)
from .tie import MAX_TIE_STUB_MM, PlannedTie, plan_equivalent_pin_tie

__all__ = [
    "HEADINGS",
    "MAX_TIE_STUB_MM",
    "Couple",
    "Gateway",
    "GatewaySite",
    "PairChain",
    "PlannedTie",
    "choose_pair_chain",
    "choose_pair_endpoints",
    "choose_pair_tap_chain",
    "enumerate_gateways",
    "gateway_anchor_point",
    "gateway_side",
    "handedness_consistent",
    "plan_equivalent_pin_tie",
    "propose_gateway_sites",
    "rank_gateway_combos",
]
