"""Referee-enforceable KiCad rule export from a voltage map (Issue #4508).

Phase 3 of the pairwise HV-isolation clearance epic (#4431).  Phases 1-2 make
the pairwise HV<->LV clearance requirement enforceable *inside* kct's own
router/placement/census.  This module makes the SAME requirement enforceable by
the **external referee** -- ``kicad-cli pcb drc`` -- so a routed HV board is
independently confirmed against the voltage-map-derived pairwise creepage matrix
(the project's two-engine manufacturability bar: 0 errors by BOTH kct AND
kicad-cli).

Two artifacts are emitted from the same ``--voltage-map`` + creepage-standard
inputs the router/placement/census already consume:

1. **Net -> netclass assignments** -- every mapped net is grouped into a
   voltage-*domain* netclass (one class per distinct working-voltage magnitude),
   written into the project's ``net_settings`` (``.kicad_pro``).

2. **Custom ``(rule ...)`` clauses** -- one KiCad netclass-pair clause per domain
   pair whose required creepage exceeds the board's DRU clearance floor, written
   into a sentinel-delimited block in ``<project>.kicad_dru`` (which
   ``kicad-cli pcb drc`` picks up automatically next to the project).

Congruence with the router-side exemptions
------------------------------------------
The pairwise matrix reuses
:func:`kicad_tools.placement.hv_domains.build_required_by_domain_pair`
verbatim -- the same delta-V -> creepage lookup, the same ``hv_threshold`` floor
and the same fail-loud out-of-table contract the router (#4511) and census use --
so the referee and kct can never disagree about the matrix.

The #4506 rated-footprint attach-zone exemption (a domain-bridging package whose
own pins sit at pin-pitch, e.g. a sense resistor / optocoupler straddling two
domains, which the board layout cannot pull apart) is expressed **directly in the
pairwise rule's condition** as an ``insideCourtyard`` exclusion.  Inside such a
footprint's courtyard the elevated pairwise clearance is *not* applied and the
board's normal DRU clearance floor governs -- exactly the router-side relaxation
from #4506 and the same-footprint census waiver from #4403.  Baking the exclusion
into the condition (rather than a separate, later "relaxation" rule) makes the
result **independent of KiCad's rule-precedence semantics**: whichever way KiCad
resolves overlapping rules, a courtyard-internal pair is never subject to the
elevated bound, and -- crucially -- the exclusion is scoped to a specific
reference designator's courtyard, so it can never weaken the referee board-wide.

Fallback / no-op
----------------
With no voltage map supplied there is nothing to derive; :func:`build_export`
returns an empty plan and the CLI is a clean no-op (nothing written).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import combinations
from typing import Iterable, Mapping, Sequence

from kicad_tools.placement.hv_domains import build_required_by_domain_pair

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Netclass-name prefix that marks a kct-owned voltage-domain class.  Every class
# / pattern re-emit is idempotent by this prefix: a re-run first drops all
# ``kct_``-prefixed classes and patterns, then re-adds fresh -- so user-authored
# classes are preserved and kct-owned ones never duplicate or go stale.
NETCLASS_PREFIX = "kct_"

# Sentinel markers delimiting the kct-owned block inside a ``.kicad_dru`` file.
# On re-emit only the text BETWEEN these markers is replaced; any hand-written
# rules outside the block are preserved.
DRU_BLOCK_BEGIN = "# BEGIN kct creepage rules (Issue #4508) -- managed, do not edit"
DRU_BLOCK_END = "# END kct creepage rules"

# KiCad ``.kicad_dru`` files start with a version header.
DRU_VERSION_HEADER = "(version 1)"

# A required creepage within this tolerance (mm) of the DRU floor adds nothing
# over the board-wide clearance rule, so no pairwise rule is emitted for it.
_FLOOR_TOLERANCE = 1e-6


def _norm_net_key(name: str) -> str:
    """Normalise a net name for domain lookup (drop one leading ``/``).

    Mirrors the census (:func:`kicad_tools.creepage.engine._norm_net_key`) and
    the router (:func:`kicad_tools.router.pairwise_clearance._norm_net_key`) so
    all three key the same nets regardless of the leading hierarchical slash.
    """
    return name[1:] if name.startswith("/") else name


def domain_name(volts: float) -> str:
    """Deterministic netclass name for a working-voltage *magnitude*.

    ``150`` -> ``kct_150V``; ``3.3`` -> ``kct_3p3V``; ``0`` -> ``kct_0V``.  The
    decimal point is rendered ``p`` so the name is a safe bare token in a KiCad
    rule condition (``A.NetClass == 'kct_3p3V'``) and a valid netclass name.
    """
    token = f"{abs(float(volts)):g}".replace(".", "p").replace("-", "m")
    return f"{NETCLASS_PREFIX}{token}V"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleClause:
    """One KiCad custom ``(rule ...)`` clause (clearance constraint)."""

    name: str
    condition: str
    min_mm: float

    def render(self) -> str:
        """Render as ``.kicad_dru`` S-expression text."""
        return (
            f'(rule "{self.name}"\n'
            f'  (condition "{self.condition}")\n'
            f"  (constraint clearance (min {self.min_mm:g}mm)))"
        )


@dataclass
class ExportPlan:
    """The full set of derived artifacts for one board.

    ``net_domains`` maps each *actual* net name to its domain netclass;
    ``domain_voltages`` maps each domain netclass to its representative voltage
    magnitude; ``rules`` are the pairwise clearance clauses; and
    ``bridging_by_pair`` records, per domain pair, the reference designators of
    domain-bridging footprints excluded from that pair's elevated bound.
    """

    net_domains: dict[str, str] = field(default_factory=dict)
    domain_voltages: dict[str, float] = field(default_factory=dict)
    rules: list[RuleClause] = field(default_factory=list)
    bridging_by_pair: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    dru_floor_mm: float = 0.0

    @property
    def is_empty(self) -> bool:
        """``True`` when there is nothing to emit (no voltage map / no domains)."""
        return not self.net_domains and not self.rules


# ---------------------------------------------------------------------------
# Domain derivation
# ---------------------------------------------------------------------------


def build_domains(
    voltage_map: Mapping[str, float],
    net_names: Iterable[str],
) -> tuple[dict[str, str], dict[str, float]]:
    """Group nets into voltage-domain netclasses.

    A net's domain is determined by the magnitude of its mapped potential; nets
    sharing a magnitude share a domain.  ``net_names`` supplies the *authoritative*
    net names (from the PCB) so the emitted netclass patterns match exactly what
    KiCad sees, regardless of whether the voltage-map author wrote the leading
    hierarchical ``/``.

    Args:
        voltage_map: ``{net_name: volts}`` (magnitudes taken with ``abs``).
        net_names: All net names present on the board.

    Returns:
        ``(net_domains, domain_voltages)`` where *net_domains* maps each matched
        actual net name to its domain netclass and *domain_voltages* maps each
        domain netclass to its voltage magnitude.
    """
    norm_map = {_norm_net_key(k): abs(float(v)) for k, v in voltage_map.items()}

    net_domains: dict[str, str] = {}
    domain_voltages: dict[str, float] = {}
    for name in net_names:
        mag = norm_map.get(_norm_net_key(name))
        if mag is None:
            continue
        dname = domain_name(mag)
        net_domains[name] = dname
        domain_voltages[dname] = mag
    return net_domains, domain_voltages


def detect_bridging_footprints(
    footprints: Iterable,
    net_domains: Mapping[str, str],
) -> dict[tuple[str, str], list[str]]:
    """Map each domain pair to the domain-bridging footprints straddling it.

    A footprint *bridges* domains ``a`` and ``b`` when it holds pads on nets in
    both.  Such a package's own pins sit at pin-pitch (the layout cannot pull
    them to full creepage), so it is exempted from the elevated pairwise bound --
    the #4506 rated-footprint attach-zone semantics, expressed here as a
    per-footprint ``insideCourtyard`` exclusion on the pair's rule.

    Keys are order-independent ``(a, b)`` tuples (sorted); values are sorted,
    de-duplicated reference-designator lists.
    """
    out: dict[tuple[str, str], set[str]] = {}
    for fp in footprints:
        ref = getattr(fp, "reference", "") or ""
        if not ref:
            continue
        domains: set[str] = set()
        for pad in getattr(fp, "pads", []):
            net_name = getattr(pad, "net_name", "") or ""
            if not net_name:
                continue
            dom = net_domains.get(net_name) or net_domains.get(_norm_net_key(net_name))
            if dom is not None:
                domains.add(dom)
        for a, b in combinations(sorted(domains), 2):
            out.setdefault((a, b), set()).add(ref)
    return {pair: sorted(refs) for pair, refs in out.items()}


# ---------------------------------------------------------------------------
# Rule clause construction
# ---------------------------------------------------------------------------


def build_rule_clauses(
    domain_voltages: Mapping[str, float],
    bridging_by_pair: Mapping[tuple[str, str], Sequence[str]],
    *,
    standard_id: str = "iec60664",
    pollution_degree: int = 2,
    material_group: str = "IIIa",
    hv_threshold: float = 30.0,
    dru_floor_mm: float = 0.0,
) -> list[RuleClause]:
    """Build the pairwise clearance rule clauses above the DRU floor.

    Reuses :func:`kicad_tools.placement.hv_domains.build_required_by_domain_pair`
    verbatim for the ``{(a, b): required_mm}`` matrix (same table lookup, same
    ``hv_threshold`` gate, same fail-loud out-of-table contract).  Only pairs
    whose required creepage is strictly above ``dru_floor_mm`` get a rule -- at
    or below the floor the board-wide DRU clearance already covers them.

    The referee constraint is ``clearance`` (through-air).  Because creepage is
    always >= clearance, requiring ``clearance >= required_creepage`` is a
    conservative (never-under-strict) enforcement of the creepage requirement.

    Raises:
        StandardLookupError: If any cross-domain ``|dV|`` exceeds the highest
            tabulated row (propagated from ``build_required_by_domain_pair`` --
            never silently extrapolated).
    """
    required = build_required_by_domain_pair(
        domain_voltages,
        standard_id=standard_id,
        pollution_degree=pollution_degree,
        material_group=material_group,
        hv_threshold=hv_threshold,
    )

    clauses: list[RuleClause] = []
    for (a, b), req in sorted(required.items()):
        if req <= dru_floor_mm + _FLOOR_TOLERANCE:
            continue
        condition = f"A.NetClass == '{a}' && B.NetClass == '{b}'"
        for ref in bridging_by_pair.get((a, b), ()):
            # Exclude intra-footprint proximity for a domain-bridging package:
            # inside its courtyard the normal DRU clearance floor governs (#4506).
            condition += f" && !(A.insideCourtyard('{ref}') && B.insideCourtyard('{ref}'))"
        clauses.append(RuleClause(name=f"kct_creepage_{a}_vs_{b}", condition=condition, min_mm=req))
    return clauses


# ---------------------------------------------------------------------------
# Top-level plan assembly
# ---------------------------------------------------------------------------


def build_export(
    voltage_map: Mapping[str, float] | None,
    net_names: Iterable[str],
    footprints: Iterable,
    *,
    standard_id: str = "iec60664",
    pollution_degree: int = 2,
    material_group: str = "IIIa",
    hv_threshold: float = 30.0,
    dru_floor_mm: float = 0.0,
) -> ExportPlan:
    """Derive the full :class:`ExportPlan` from a voltage map.

    A ``None``/empty voltage map yields an empty plan (the no-op fallback).
    """
    if not voltage_map:
        return ExportPlan(dru_floor_mm=dru_floor_mm)

    net_names = list(net_names)
    net_domains, domain_voltages = build_domains(voltage_map, net_names)
    bridging_by_pair = detect_bridging_footprints(footprints, net_domains)
    rules = build_rule_clauses(
        domain_voltages,
        bridging_by_pair,
        standard_id=standard_id,
        pollution_degree=pollution_degree,
        material_group=material_group,
        hv_threshold=hv_threshold,
        dru_floor_mm=dru_floor_mm,
    )
    # Only report bridging footprints that actually gate an emitted rule (a pair
    # below the DRU floor has no rule, so its "exemption" is meaningless noise).
    relevant_bridging = {
        pair: list(refs)
        for pair, refs in bridging_by_pair.items()
        if _pair_has_rule(pair[0], pair[1], rules)
    }
    return ExportPlan(
        net_domains=net_domains,
        domain_voltages=domain_voltages,
        rules=rules,
        bridging_by_pair=relevant_bridging,
        dru_floor_mm=dru_floor_mm,
    )


def _pair_has_rule(a: str, b: str, rules: Sequence[RuleClause]) -> bool:
    needle = f"kct_creepage_{a}_vs_{b}"
    return any(r.name == needle for r in rules)


# ---------------------------------------------------------------------------
# .kicad_pro netclass assignment (idempotent, merge-preserving)
# ---------------------------------------------------------------------------


def apply_netclass_assignments(project_data: dict, plan: ExportPlan) -> None:
    """Write the voltage-domain netclasses + net patterns into project data.

    Idempotent and merge-preserving: all ``kct_``-prefixed classes/patterns are
    dropped first (so a stale domain assignment from a prior run never lingers),
    then the current domains re-added.  User-authored classes/patterns and the
    project's ``rules``/``defaults`` are never touched.
    """
    from kicad_tools.core.project_file import (
        add_netclass_definition,
        add_netclass_pattern,
        get_net_settings,
    )

    net_settings = get_net_settings(project_data)

    # Drop kct-owned classes/patterns (idempotent re-emit).
    net_settings["classes"] = [
        c
        for c in net_settings.get("classes", [])
        if not str(c.get("name", "")).startswith(NETCLASS_PREFIX)
    ]
    net_settings["netclass_patterns"] = [
        p
        for p in net_settings.get("netclass_patterns", [])
        if not str(p.get("netclass", "")).startswith(NETCLASS_PREFIX)
    ]

    # Re-add the current domains.  The class's own clearance is set to the DRU
    # floor -- the elevated pairwise minima live in the .kicad_dru rules, not the
    # class definition, so this never over-constrains a domain against itself.
    for dname in sorted(plan.domain_voltages):
        add_netclass_definition(project_data, dname, clearance=plan.dru_floor_mm or 0.2)

    for net_name in sorted(plan.net_domains):
        add_netclass_pattern(project_data, plan.net_domains[net_name], net_name)


# ---------------------------------------------------------------------------
# .kicad_dru rule block (idempotent sentinel-delimited merge -- NET-NEW logic)
# ---------------------------------------------------------------------------


def render_dru_block_body(plan: ExportPlan) -> str:
    """Render the block body: a provenance header comment + the rule clauses."""
    lines: list[str] = [
        "# Generated by `kct creepage-export-rules` (Issue #4508).",
        "# Pairwise HV creepage clearance, referee-enforceable by kicad-cli pcb drc.",
    ]
    if plan.domain_voltages:
        domains = ", ".join(
            f"{d}={plan.domain_voltages[d]:g}V" for d in sorted(plan.domain_voltages)
        )
        lines.append(f"# Voltage domains: {domains}")
    if plan.bridging_by_pair:
        for (a, b), refs in sorted(plan.bridging_by_pair.items()):
            lines.append(
                f"# Attach-zone exemption ({a}<->{b}): {', '.join(refs)} "
                "(domain-bridging footprint(s); courtyard-internal pairs relax to the DRU floor)"
            )
    if not plan.rules:
        lines.append("# (no domain pair exceeds the DRU clearance floor -- no rules emitted)")
    body = "\n".join(lines)
    if plan.rules:
        body += "\n" + "\n".join(rule.render() for rule in plan.rules)
    return body


def merge_dru_block(existing: str | None, block_body: str) -> str:
    """Merge the kct-owned rule block into existing ``.kicad_dru`` content.

    * No existing content -> create ``(version 1)`` + the block.
    * Existing block present -> replace only the text between the sentinels.
    * Existing content, no block -> append the block, preserving everything
      (and prepending a version header only if the file lacks one).

    Idempotent: re-merging identical inputs yields byte-identical output.
    """
    block = f"{DRU_BLOCK_BEGIN}\n{block_body}\n{DRU_BLOCK_END}"

    if existing is None or not existing.strip():
        return f"{DRU_VERSION_HEADER}\n\n{block}\n"

    pattern = re.compile(
        re.escape(DRU_BLOCK_BEGIN) + r".*?" + re.escape(DRU_BLOCK_END),
        re.DOTALL,
    )
    if pattern.search(existing):
        merged = pattern.sub(lambda _m: block, existing)
        return merged if merged.endswith("\n") else merged + "\n"

    # Append; ensure a version header exists.
    prefix = existing if existing.endswith("\n") else existing + "\n"
    if not re.search(r"^\s*\(version\b", existing, re.MULTILINE):
        prefix = f"{DRU_VERSION_HEADER}\n" + prefix
    return f"{prefix}\n{block}\n"
