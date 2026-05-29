"""Derive single-ended impedance specs from a net-class map.

Issue #3157.

Single-ended controlled impedance is **declarative / opt-in**.  The
:class:`~kicad_tools.validate.rules.impedance.ImpedanceRule` no longer
auto-applies its name-pattern heuristics (``.*CLK$`` / ``.*MCLK$`` /
``.*ETH.*`` -> 50Ω) as DRC errors, because those assume "ends in CLK ==
high-speed 50Ω" and produced 32 false positives on low-speed audio
clock nets (``DAC_CLK``, ``BCLK``, ``MCLK``, ``I2S_LRCLK``) on a
4-layer board with an explicit stackup (chorus-test).

The declarative surface already exists:
:attr:`~kicad_tools.router.rules.NetClassRouting.target_single_impedance`
is serialized through the ``kct check --net-class-map`` sidecar (Issue
#2684).  This module is the thin producer-side shim that maps each
class's ``target_single_impedance`` to per-net
:class:`~kicad_tools.validate.rules.impedance.NetImpedanceSpec` entries,
mirroring the producer wiring used by
:mod:`kicad_tools.validate.match_group_skew` (Issue #2710) and
:mod:`kicad_tools.validate.diffpair_skew` (Issue #2675).

The resulting specs are passed to ``ImpedanceRule(specs=...)`` as an
**explicit** spec list.  Explicit specs bypass the rule's heuristic-
suppression gate and always evaluate, so a net declared 50Ω single-ended
still fires when its routed width is wrong.

Scope: single-ended only.  Diff-pair impedance
(:attr:`~kicad_tools.router.rules.NetClassRouting.target_diff_impedance`)
is consumed via the router's ``detected_pairs`` / coupled-lines path and
is intentionally NOT touched here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from kicad_tools.validate.rules.impedance import NetImpedanceSpec

if TYPE_CHECKING:
    from kicad_tools.router.rules import NetClassRouting


def derive_single_ended_impedance_specs(
    net_class_map: dict[str, NetClassRouting] | None,
) -> list[NetImpedanceSpec]:
    """Build explicit single-ended impedance specs from a net-class map.

    Walks the ``{net_name: NetClassRouting}`` map and, for every net whose
    class declares a non-``None``
    :attr:`~kicad_tools.router.rules.NetClassRouting.target_single_impedance`,
    emits a :class:`NetImpedanceSpec` matching that exact net name with the
    declared ``target_z0`` and the class's
    :attr:`~kicad_tools.router.rules.NetClassRouting.impedance_tolerance_percent`.

    The spec's ``net_pattern`` is the net name escaped and anchored
    (``^name$``) so it matches only that one net -- net names are literal
    identifiers, not patterns, in this map.

    Args:
        net_class_map: Map of ``{net_name: NetClassRouting}`` (the
            autorouter convention; deserialized from the
            ``kct check --net-class-map`` sidecar).  ``None`` or empty
            returns ``[]`` so the standalone ``kct check`` path degrades
            gracefully to "no single-ended impedance specs".

    Returns:
        A list of :class:`NetImpedanceSpec` carrying only ``target_z0``
        (single-ended).  Empty when no class declares
        ``target_single_impedance`` -- which is the common case, since no
        predefined ``NET_CLASS_*`` sets it.
    """
    if not net_class_map:
        return []

    specs: list[NetImpedanceSpec] = []
    for net_name, nc in net_class_map.items():
        target = getattr(nc, "target_single_impedance", None)
        if target is None:
            continue
        tolerance = getattr(nc, "impedance_tolerance_percent", 10.0)
        specs.append(
            NetImpedanceSpec(
                net_pattern=f"^{re.escape(net_name)}$",
                target_z0=float(target),
                tolerance_percent=float(tolerance),
            )
        )
    return specs
