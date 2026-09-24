"""Rule-name matching for native ``kicad-cli pcb drc`` JSON reports.

KiCad's JSON DRC report carries **no structural rule-name field**: the custom
rule that produced a violation is only ever embedded in that violation's
free-text ``description``.  *How* it is embedded is a presentation detail of the
per-violation-type message template, and it is **not stable**: it differs across
KiCad patch releases *and* between violation types within one release.  Measured
on the ``jlcpcb`` profile fixtures in ``tests/test_factory_object_clearance.py``
(issue #5703), same board, same verdict, three different renderings::

    10.0.1  silk_over_copper  Silkscreen clipped by solder mask (Silk to Pad - jlcpcb clearance 0.1500 mm; actual 0.0850 mm)
    10.0.6  silk_over_copper  Silkscreen clipped by solder mask (rule 'Silk to Pad - jlcpcb' clearance 0.1500 mm; actual 0.0850 mm)
    10.0.1  hole_clearance    Hole clearance violation (rule 'PTH Hole to Track - jlcpcb' clearance 0.2800 mm; actual 0.2700 mm)

Note the third row: 10.0.1 omits the ``rule '`` wrapper from its
``silk_over_copper`` template while still emitting it from ``hole_clearance``, so
"does this KiCad emit the wrapper?" is not even a per-version question.

An assertion that matches on the literal ``rule '`` prefix therefore couples a
DRC *verdict* to a presentation detail, and it fails in the worst possible
direction:

* a **positive** assertion reports a correctly detected violation as missing
  (the ``silk`` cases of ``test_native_object_specific_clearance`` on 10.0.1);
* a **negative** assertion passes **vacuously** -- the filtered list is empty on
  any build whose template omits the wrapper, so the control proves nothing.

``findings_for_rules`` matches on the **bare rule name** instead, which every
observed template renders verbatim.  It deliberately omits the
manufacturer-profile suffix the generator appends (``- jlcpcb``): that suffix is
derived from the ``manufacturer_id`` and reintroduces a second string coupling
for no extra discrimination, since the bare names the callers pass are already
unique within the emitted rule set.

Callers must keep the names they pass distinctive.  The rule names emitted for
the ``jlcpcb`` profile are ``Trace Width``, ``Clearance``, ``Silk to Pad``, ``SMD
Pad Clearance``, ``PTH Hole to Track``, ``Inner PTH Hole to Copper``, ``Via
Drill``, ``Via Diameter``, ``Annular Ring``, ``PTH Annular Ring``, ``Copper to
Edge``, ``Hole to Edge``, ``Silkscreen Width`` and ``Silkscreen Height``.  Every
name used as a matcher today was checked against the full emitted set of the
``jlcpcb``, ``jlcpcb-tier1``, ``pcbway`` and ``oshpark`` profiles at 2/4/6 layers
and is a substring of no other rule name -- ``Clearance`` and ``Annular Ring``
would be, and neither is matched bare.
"""

from collections.abc import Iterable


def findings_for_rules(violations: Iterable[dict], *rule_names: str) -> list[dict]:
    """Violations whose ``description`` names one of ``rule_names``.

    Args:
        violations: ``violations`` array of a ``kicad-cli pcb drc --format json``
            report.
        rule_names: Bare custom-rule names, without the ``rule '`` wrapper and
            without the ``- <manufacturer_id>`` suffix the generator appends.

    Returns:
        The matching violations, in report order.
    """
    if not rule_names:
        raise ValueError("findings_for_rules() requires at least one rule name")
    return [v for v in violations if any(name in v["description"] for name in rule_names)]
