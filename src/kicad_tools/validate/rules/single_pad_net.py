"""Single-pad-net design defect rule.

Detects nets that are declared with a non-empty name but only have a
single pad assigned to them.  Such nets are physically unroutable and
almost always indicate one of the following design defects:

- A footprint is missing from the PCB (the net was declared on the
  schematic side but only one component instantiates it on the PCB).
- The schematic was edited after the PCB was generated and the netlist
  was not re-synced (schematic/PCB drift).
- A part was deleted from the PCB but its associated nets remained.

Power and ground nets that legitimately have a single pad (e.g., a
single test point or a pour-only net) are silently allowed: this rule
fires only on signal nets.

The router currently classifies these as "structurally unroutable" and
silently skips them, which lets agents iterating on a design mistake
"13/13 routed, DRC clean" for a successful build when 4 SWD signals are
floating.  This rule surfaces them as DRC errors so the agent loop sees
the problem before iterating.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule
from .netlist import _absolute_pad_position

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Footprint, Pad


class SinglePadNetRule(DRCRule):
    """Flag signal nets that have only one pad attached.

    A net that has been declared in the board header but only ever
    appears on a single footprint pad is structurally unroutable and
    almost always a design defect (missing footprint, schematic drift,
    or stale netlist).

    Power and ground nets that are classified as ``is_pour_net=True``
    by :func:`kicad_tools.router.net_class.classify_and_apply_rules`
    are silently allowed because a single test point or pour-only net
    is a legitimate design pattern.  Only signal-class single-pad nets
    fire.
    """

    rule_id = "single_pad_net"
    name = "Single-Pad Net"
    description = (
        "Detects signal nets attached to only one pad (missing footprint or schematic/PCB drift)"
    )

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check the PCB for signal nets with only one pad attached.

        The detector walks ``pcb.footprints`` -> ``fp.pads`` (not
        ``pcb.nets``, which only enumerates the declared net headers)
        because the single-pad condition is defined by the count of
        pad assignments, not the count of header entries.

        Args:
            pcb: The PCB to check.
            design_rules: Design rules (unused by this rule but required
                by the base-class interface).

        Returns:
            DRCResults containing one error per single-pad signal net.
        """
        results = DRCResults()
        results.rules_checked = 1

        # Build a map of (net_number, net_name) -> [(footprint, pad)]
        # using the actual pad assignments (not the header).
        net_pads: dict[
            tuple[int, str],
            list[tuple[Footprint, Pad]],
        ] = defaultdict(list)

        for fp in pcb.footprints:
            for pad in fp.pads:
                # Skip pads with no net assignment (unconnected) and
                # the conventional (net 0, "") unconnected net.
                if not pad.net_name:
                    continue
                if pad.net_number == 0 and pad.net_name == "":
                    continue
                net_pads[(pad.net_number, pad.net_name)].append((fp, pad))

        # Identify single-pad nets.
        single_pad: list[tuple[tuple[int, str], tuple[Footprint, Pad]]] = [
            (key, pads_list[0]) for key, pads_list in net_pads.items() if len(pads_list) == 1
        ]

        if not single_pad:
            return results

        # Build pour-net suppression set.  Use the net-name classifier
        # so a single-pad GND testpoint or a single-pad +3V3 marker
        # doesn't error -- only signal-class single-pad nets fire.
        pour_nets: set[str] = set()
        try:
            from kicad_tools.router.net_class import classify_and_apply_rules

            net_id_by_name = {net_num: net_name for (net_num, net_name), _ in single_pad}
            if net_id_by_name:
                rules = classify_and_apply_rules(net_id_by_name)
                pour_nets = {name for name, cfg in rules.items() if cfg and cfg.is_pour_net}
        except Exception:
            # Conservative: if the classifier blows up, don't suppress
            # anything -- the worst case is a pour net erroneously
            # reported as a single-pad defect, which is recoverable.
            pour_nets = set()

        # Emit one error per non-pour single-pad signal net.
        for (_net_number, net_name), (fp, pad) in single_pad:
            if net_name in pour_nets:
                continue

            ref = fp.reference or fp.name
            location = _absolute_pad_position(fp, pad)

            results.add(
                DRCViolation(
                    rule_id=self.rule_id,
                    severity="error",
                    message=(
                        f"Net '{net_name}' has only 1 pad on {ref}-{pad.number} "
                        f"-- likely missing footprint or schematic/PCB drift"
                    ),
                    location=location,
                    items=(f"{ref}-{pad.number}",),
                    nets=(net_name,),
                )
            )

        return results
