"""Per-net routing-policy gate: via caps and maximum routed length (Issue #6044).

A net-class-map entry (the ``--net-class-map`` sidecar ``kct check`` already
auto-discovers, keyed by **net name**) may declare two routing limits for a
net:

* ``max_vias_per_net`` -- the most vias the net may use (integer count), and
* ``length_constraint.max_length`` -- the most routed copper the net may
  carry, in mm.

This rule holds each net to the limits its own entry declares.  Nothing fires
for a net whose entry declares neither, so a board whose map carries no such
limits cannot newly fail.

Counting and measuring
======================

* **Vias:** every via whose net is the policy net
  (``pcb.vias_in_net``): through, blind/buried and micro vias alike.  A KiCad
  via carries exactly one net, so no via is counted for two nets.
* **Length:** the net's total routed copper as
  :func:`kicad_tools.router.kicad_length.measure_pcb_net` reports it -- the
  same measure the length-skew checks and the tuners use, so they cannot
  drift.  All branches of a multi-pin net are summed: this is total length,
  not pin-to-pin length.
* **Track arcs:** the shared measure reads straight segments only, so a net
  with a length limit and any track arc gets a ``warning`` saying its length
  was **not measured**, never a pass.

Diff pairs
==========

A net of an engaged differential pair whose pair limits resolve a via cap
is already held to it by ``diffpair_uncoupled_length``
(:func:`~kicad_tools.validate.diffpair_engagement.resolve_pair_thresholds`).
The caller passes those nets as ``deferred_via_nets`` and this rule skips
their via check, so a pair net is never reported twice.

Missing policy (opt-in)
=======================

With ``require_policy=True`` (``kct check --require-routing-policy``) every
*sensitive* routed net -- its entry sets ``length_critical``,
``noise_sensitive``, ``length_match_group`` or ``diffpair_partner`` -- must
declare at least one of the two limits.  Pour nets (``is_pour_net``) are
exempt.  One ``error`` is emitted per class ``name`` listing its nets.

Malformed values (a negative, fractional or non-numeric
``max_vias_per_net``; a non-positive or non-numeric ``max_length``) are an
``error`` naming the net, never an exception.  ``LengthConstraint.net_id`` is
ignored here: the map key identifies the net.

Ideas only from circuit-skills (unlicensed); no code reused.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.router.rules import NetClassRouting
    from kicad_tools.schema.pcb import PCB

RULE_ID = "routing_policy"

#: Float slack (mm) on a length limit, as the sibling skew rules have.
LENGTH_EPSILON_MM = 1e-6

#: Phrase every "length not measured" finding carries.
UNMEASURED_ARCS_PHRASE = "length unmeasured (track arcs)"


def _valid_via_cap(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_max_length(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def declared_via_cap(nc: Any) -> object:
    """The entry's raw ``max_vias_per_net`` (``None`` when not declared)."""
    return getattr(nc, "max_vias_per_net", None)


def declared_max_length(nc: Any) -> object:
    """The entry's raw ``length_constraint.max_length`` (``None`` when absent)."""
    lc = getattr(nc, "length_constraint", None)
    return getattr(lc, "max_length", None) if lc is not None else None


def declares_routing_policy(nc: Any) -> bool:
    """True when the entry declares a via cap or a maximum length."""
    return declared_via_cap(nc) is not None or declared_max_length(nc) is not None


def is_sensitive(nc: Any) -> bool:
    """True for a class ``--require-routing-policy`` expects a policy on."""
    if getattr(nc, "is_pour_net", False):
        return False
    return bool(
        getattr(nc, "length_critical", False)
        or getattr(nc, "noise_sensitive", False)
        or getattr(nc, "length_match_group", None)
        or getattr(nc, "diffpair_partner", None)
    )


def _sensitive_reasons(nc: Any) -> list[str]:
    reasons = []
    for attr in ("length_critical", "noise_sensitive", "length_match_group", "diffpair_partner"):
        if getattr(nc, attr, None):
            reasons.append(attr)
    return reasons


class RoutingPolicyRule(DRCRule):
    """Hold each net to its declared via cap and maximum routed length."""

    rule_id = RULE_ID
    name = "Routing policy"
    description = (
        "Nets with a declared max_vias_per_net / length_constraint.max_length "
        "in the net-class map stay within them"
    )

    def __init__(
        self,
        net_class_map: Mapping[str, NetClassRouting] | None,
        *,
        require_policy: bool = False,
        deferred_via_nets: Iterable[str] = (),
        num_copper_layers: int = 2,
        emit_info: bool = False,
    ) -> None:
        self._map = dict(net_class_map or {})
        self._require_policy = require_policy
        self._deferred_via_nets = frozenset(deferred_via_nets)
        self._num_copper_layers = num_copper_layers
        self._emit_info = emit_info

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,  # noqa: ARG002 (signature mirrors peer rules)
    ) -> DRCResults:
        results = DRCResults()
        if not self._map:
            return results
        ids = {n.name: n.number for n in pcb.nets.values() if n.name}
        missing: dict[str, list[str]] = {}
        missing_reasons: dict[str, set[str]] = {}
        for net in sorted(self._map):
            nc = self._map[net]
            net_id = ids.get(net)
            if net_id is None:
                continue  # unresolved key: the CLI warns about it
            routed = self._has_copper(pcb, net_id)
            if declares_routing_policy(nc):
                self._check_net(results, pcb, net, net_id, nc, routed)
            elif self._require_policy and routed and is_sensitive(nc):
                cls = getattr(nc, "name", "") or net
                missing.setdefault(cls, []).append(net)
                missing_reasons.setdefault(cls, set()).update(_sensitive_reasons(nc))
        for cls in sorted(missing):
            nets = tuple(sorted(missing[cls]))
            reasons = ", ".join(sorted(missing_reasons[cls]))
            results.add(
                DRCViolation(
                    rule_id=self.rule_id,
                    severity="error",
                    message=(
                        f"Net class {cls} ({reasons}) declares no routing policy "
                        f"(max_vias_per_net or length_constraint.max_length) for "
                        f"{len(nets)} routed net(s): {', '.join(nets)}"
                    ),
                    items=(cls,),
                    nets=nets,
                )
            )
        return results

    # ------------------------------------------------------------------

    @staticmethod
    def _has_copper(pcb: PCB, net_id: int) -> bool:
        return (
            any(True for _ in pcb.segments_in_net(net_id))
            or any(True for _ in pcb.arcs_in_net(net_id))
            or any(True for _ in pcb.vias_in_net(net_id))
        )

    def _count(self, results: DRCResults) -> None:
        results.rules_checked += 1
        results.rules_checked_by_rule[self.rule_id] = (
            results.rules_checked_by_rule.get(self.rule_id, 0) + 1
        )

    def _check_net(
        self,
        results: DRCResults,
        pcb: PCB,
        net: str,
        net_id: int,
        nc: Any,
        routed: bool,
    ) -> None:
        cls = getattr(nc, "name", "") or net
        cap = declared_via_cap(nc)
        max_len = declared_max_length(nc)

        if cap is not None and not _valid_via_cap(cap):
            results.add(
                self._finding(
                    "error",
                    f"Net {net} (class {cls}): malformed max_vias_per_net {cap!r} "
                    "in the net-class map (expected a non-negative integer)",
                    net,
                )
            )
            cap = None
        if max_len is not None and not _valid_max_length(max_len):
            results.add(
                self._finding(
                    "error",
                    f"Net {net} (class {cls}): malformed length_constraint.max_length "
                    f"{max_len!r} in the net-class map (expected a positive number of mm)",
                    net,
                )
            )
            max_len = None

        if not routed:
            return  # no copper to hold to a limit; the CLI names it

        if cap is not None and net not in self._deferred_via_nets:
            assert isinstance(cap, int)
            self._count(results)
            count = sum(1 for _ in pcb.vias_in_net(net_id))
            if count > cap:
                results.add(
                    self._finding(
                        "error",
                        f"Net {net} (class {cls}) has {count} vias, max_vias_per_net {cap}",
                        net,
                        float(count),
                        float(cap),
                    )
                )
            elif self._emit_info:
                results.add(
                    self._finding(
                        "info",
                        f"Net {net} (class {cls}) has {count} vias, within max_vias_per_net {cap}",
                        net,
                        float(count),
                        float(cap),
                    )
                )

        if max_len is not None:
            assert isinstance(max_len, (int, float))
            limit = float(max_len)
            self._count(results)
            arcs = sum(1 for _ in pcb.arcs_in_net(net_id))
            if arcs:
                results.add(
                    self._finding(
                        "warning",
                        f"Net {net} (class {cls}): {UNMEASURED_ARCS_PHRASE}: the net "
                        f"has {arcs} track arc(s), which the routed-length measure "
                        f"does not read, so max_length {limit:.3f} mm was not checked",
                        net,
                        None,
                        limit,
                    )
                )
                return
            from kicad_tools.router.kicad_length import measure_pcb_net

            length = measure_pcb_net(pcb, net_id, num_copper_hint=self._num_copper_layers)
            if length > limit + LENGTH_EPSILON_MM:
                results.add(
                    self._finding(
                        "error",
                        f"Net {net} (class {cls}) routed length {length:.3f} mm "
                        f"exceeds max_length {limit:.3f} mm",
                        net,
                        length,
                        limit,
                    )
                )
            elif self._emit_info:
                results.add(
                    self._finding(
                        "info",
                        f"Net {net} (class {cls}) routed length {length:.3f} mm "
                        f"within max_length {limit:.3f} mm",
                        net,
                        length,
                        limit,
                    )
                )

    def _finding(
        self,
        severity: str,
        message: str,
        net: str,
        actual: float | None = None,
        required: float | None = None,
    ) -> DRCViolation:
        return DRCViolation(
            rule_id=self.rule_id,
            severity=severity,
            message=message,
            actual_value=round(actual, 4) if actual is not None else None,
            required_value=round(required, 4) if required is not None else None,
            items=(net,),
            nets=(net,),
        )
