"""Derive per-net ampacity targets from a net-class map.

Issue #4217 (Part 3 of #4215).

Ampacity targets are **declarative**: a net only carries a required
current when its
:class:`~kicad_tools.router.rules.NetClassRouting` explicitly sets
:attr:`~kicad_tools.router.rules.NetClassRouting.target_ampacity`.  Unlike
single-ended impedance there is no name-pattern heuristic ("ends in CLK
== 50Ω") equivalent for current — a trace's ampacity requirement is never
inferred from its net name.

This module is the thin producer-side shim, mirroring
:func:`kicad_tools.validate.impedance_specs.derive_single_ended_impedance_specs`.
It maps each class's ``target_ampacity`` to a ``{net_name: current_a}``
dict which
:class:`~kicad_tools.validate.rules.ampacity.AmpacityRule` consumes as its
``specs``.

The result is threaded through the same ``kct check --net-class-map``
sidecar path that
:meth:`kicad_tools.validate.checker.DRCChecker.check_impedance` and
:meth:`~kicad_tools.validate.checker.DRCChecker.check_match_group_length_skew`
already consume — ``target_ampacity`` is just another field read off the
same :class:`NetClassRouting` objects already flowing through that map.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.router.rules import NetClassRouting


def derive_ampacity_specs(
    net_class_map: dict[str, NetClassRouting] | None,
) -> dict[str, float]:
    """Build a ``{net_name: target_ampacity}`` map from a net-class map.

    Walks the ``{net_name: NetClassRouting}`` map and, for every net whose
    class declares a non-``None``
    :attr:`~kicad_tools.router.rules.NetClassRouting.target_ampacity`,
    records that current (in amps) against the net name.

    Args:
        net_class_map: Map of ``{net_name: NetClassRouting}`` (the
            autorouter convention; deserialized from the
            ``kct check --net-class-map`` sidecar).  ``None`` or empty
            returns ``{}`` so the standalone ``kct check`` path degrades
            gracefully to "no ampacity targets".

    Returns:
        A ``{net_name: current_a}`` map.  Empty when no class declares
        ``target_ampacity`` — which is the common case, since no
        predefined ``NET_CLASS_*`` sets it.
    """
    if not net_class_map:
        return {}

    specs: dict[str, float] = {}
    for net_name, nc in net_class_map.items():
        target = getattr(nc, "target_ampacity", None)
        if target is None:
            continue
        specs[net_name] = float(target)
    return specs
