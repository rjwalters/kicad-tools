"""Single-pad-net design defect rule.

Detects nets that are declared with a non-empty name but only have a
single pad assigned to them.  Such nets fall into three distinct
categories that this rule reports at different severities so the agent
loop sees a meaningful signal-to-noise ratio (see issue #2613):

1. **Genuine NC** (``severity="info"``).  Nets named with the KiCad-emitted
   ``unconnected-(REF-PIN-PadN)`` convention.  KiCad generates these
   names when the schematic explicitly marks a symbol pin as no-connect
   via the ``no_connect line`` attribute.  These are by-design and need
   no action; surfacing them at info level lets agents acknowledge them
   without treating them as defects.

2. **Connector NC** (``severity="info"``).  Nets named with the
   KiCad-default ``Net-(REFN-PadN)`` convention where ``REF`` matches a
   connector prefix (``J`` or ``P``).  These are typically intentional
   GPIO no-connects on header-style connectors (e.g., RPi 2x20 header
   pins reserved for an M.2 E-key hat footprint).  They show up as
   single-pad nets because no other footprint asserts a connection on
   that header pin, but the design intent is correct.

3. **Defect** (``severity="error"``).  Everything else -- single-pad
   signal nets that look like real named signals (``UART_TX``,
   ``I2S_BCLK``, etc.) or default-named nets on non-connector
   footprints.  These are real design defects: the schematic asserts a
   connection that exists on only one pad, which is structurally
   unroutable.  Common root causes are missing footprints, schematic
   edits not re-synced to the PCB, off-by-one wire stubs, and floating
   labels.

Power and ground nets that legitimately have a single pad (e.g., a
single test point or a pour-only net) are silently allowed: this rule
fires only on signal nets.

The router currently classifies these as "structurally unroutable" and
silently skips them, which lets agents iterating on a design mistake
"13/13 routed, DRC clean" for a successful build when 4 SWD signals are
floating.  This rule surfaces them as DRC errors (or infos for the
benign categories) so the agent loop sees the problem before iterating.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule
from .netlist import _absolute_pad_position

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Footprint, Pad


# Net-name pattern for KiCad's explicit-NC emission.  KiCad writes nets
# of this form when a symbol pin is marked ``no_connect line`` in its
# library definition (e.g., AP2112K-3.3 pin 4 = NC).  The pin name
# component may include any KiCad-legal pin name characters, including
# the literal "NC" string used in many libraries.
#
# Examples that should match:
#   unconnected-(U1-NC-Pad4)
#   unconnected-(U7-NC-Pad7)
#   unconnected-(U2-Vbat-Pad6)
_KICAD_NC_PATTERN = re.compile(r"^unconnected-\(.+-Pad\d+\)$")

# Net-name pattern for KiCad's default-named single-pad-on-connector
# emission.  When a connector pin has no other footprint asserting a
# connection on its net, KiCad emits the name in the form
# ``Net-(REFN-PadN)`` where REF is the footprint reference.  We
# auto-downgrade to info only when the prefix is a connector designator
# (``J`` or ``P``) -- the same letter used by the corresponding
# footprint on the PCB.
#
# Examples that should match (connector-pin convention):
#   Net-(J2-Pad11)
#   Net-(J2-3)         <- some KiCad versions omit "Pad"
#   Net-(P1-Pad5)
#
# Examples that should NOT match (these are real defects):
#   Net-(U3-1)         <- IC pin
#   Net-(U5-21)        <- IC pin
#   Net-(Q1-Pad2)      <- transistor pin
_CONNECTOR_NET_PATTERN = re.compile(r"^Net-\(([JP])\d+-(?:Pad)?\d+\)$")


def _classify_net(net_name: str, footprint_ref: str) -> str:
    """Classify a single-pad signal net by name and footprint context.

    Args:
        net_name: The net name (must be non-empty).
        footprint_ref: The reference designator (e.g., ``"U1"``,
            ``"J2"``) of the lone footprint hosting the net.

    Returns:
        One of:
        - ``"genuine_nc"`` -- KiCad-emitted explicit-NC net.
        - ``"connector_nc"`` -- ``Net-(REFN-PadN)`` on a connector
          footprint (J/P prefix), typically intentional GPIO NC.
        - ``"defect"`` -- everything else; real design defect.
    """
    if _KICAD_NC_PATTERN.match(net_name):
        return "genuine_nc"

    connector_match = _CONNECTOR_NET_PATTERN.match(net_name)
    if connector_match is not None:
        # Validate that the prefix in the net name matches the
        # footprint's reference prefix.  This prevents a stray
        # ``Net-(J5-1)`` from being downgraded if the matching pad is
        # actually on a non-connector footprint (which would be a real
        # data inconsistency).
        net_prefix = connector_match.group(1)
        ref_prefix = "".join(c for c in footprint_ref if c.isalpha())
        if ref_prefix == net_prefix:
            return "connector_nc"
        # Falls through to defect if prefixes disagree.

    return "defect"


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

    Each surviving single-pad net is then categorized into one of three
    severities (see module docstring): info for KiCad-emitted explicit
    NCs (``unconnected-(REF-PIN-PadN)``), info for connector-pin
    convention nets (``Net-(JN-NN)``), and error for everything else.
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
            DRCResults containing one entry per single-pad signal net,
            categorized into info/error severities (see module
            docstring).
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

        # Emit categorized output per non-pour single-pad signal net.
        for (_net_number, net_name), (fp, pad) in single_pad:
            if net_name in pour_nets:
                continue

            ref = fp.reference or fp.name
            location = _absolute_pad_position(fp, pad)
            category = _classify_net(net_name, ref)

            if category == "genuine_nc":
                severity = "info"
                message = (
                    f"Net '{net_name}' has only 1 pad on {ref}-{pad.number} "
                    f"-- KiCad-emitted explicit no-connect (symbol pin marked NC); "
                    f"no action required"
                )
            elif category == "connector_nc":
                severity = "info"
                message = (
                    f"Net '{net_name}' has only 1 pad on {ref}-{pad.number} "
                    f"-- connector pin with no asserted connection "
                    f"(typically intentional GPIO no-connect); "
                    f"add explicit no_connect flag in schematic to silence"
                )
            else:
                severity = "error"
                message = (
                    f"Net '{net_name}' has only 1 pad on {ref}-{pad.number} "
                    f"-- likely missing footprint or schematic/PCB drift"
                )

            results.add(
                DRCViolation(
                    rule_id=self.rule_id,
                    severity=severity,
                    message=message,
                    location=location,
                    items=(f"{ref}-{pad.number}",),
                    nets=(net_name,),
                )
            )

        return results
