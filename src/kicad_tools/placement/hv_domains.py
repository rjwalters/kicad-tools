"""HV-domain derivation for voltage-aware placement (issue #4373).

Turns a voltage/domain *input contract* into the two structures the
placement objective needs for its creepage-keepout term
(:func:`kicad_tools.placement.cost.compute_creepage_violation`):

* ``ref_domains``  -- map from a component reference to its HV *domain id*.
* ``required_mm_by_domain_pair`` -- map from an order-independent
  ``(domain_a, domain_b)`` tuple to the required creepage in mm, derived from
  the governing standard at the cross-domain ``|ΔV|``.

Two input sources are supported (see ``docs/placement-scoring.md``):

1. **Voltage map** (``--voltage-map v.json``) -- a flat ``{net_name: volts}``
   object, reusing the format from the per-net ΔV model (#4371). Each
   component's domain is the *name of its highest-magnitude-voltage net*
   (a footprint touching a 150 V mains net lands in that mains domain), and
   the domain's representative voltage is that magnitude. Cross-domain
   required creepage is looked up at ``|V_a - V_b|``.

2. **HV-domains declaration** (``--hv-domains d.json``) -- the manual fallback
   so the feature works standalone (before #4371 lands). Schema::

       {
         "mains":  {"refs": ["J1", "R1", "R2"], "voltage": 150},
         "signal": {"refs": ["U3", "R10"],      "voltage": 3.3}
       }

   Keys are domain ids; ``refs`` is a list of ``fnmatch`` ref globs and
   ``voltage`` is the domain's representative RMS working voltage. When a ref
   matches more than one domain it resolves to the higher-voltage domain.

The required-distance lookup uses
``kicad_tools.creepage.standards.get_standard(id).required_creepage(...)`` --
no hand-rolled table -- and preserves that module's *fail-loud* contract: a
``|ΔV|`` above the highest tabulated row raises
:class:`~kicad_tools.creepage.standards.StandardLookupError` rather than
silently extrapolating.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Mapping, Sequence

from kicad_tools.creepage.standards import get_standard


def load_voltage_map(path: str | Path) -> dict[str, float]:
    """Load a ``{net_name: volts}`` voltage map from a JSON file.

    Raises:
        ValueError: If the file is not a JSON object of ``str -> number``.
    """
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"voltage map must be a JSON object, got {type(raw).__name__}")
    out: dict[str, float] = {}
    for net, volts in raw.items():
        if not isinstance(volts, (int, float)) or isinstance(volts, bool):
            raise ValueError(f"voltage for net {net!r} must be a number, got {volts!r}")
        out[str(net)] = float(volts)
    return out


def load_hv_domains(path: str | Path) -> dict[str, dict]:
    """Load an ``--hv-domains`` declaration from a JSON file.

    Raises:
        ValueError: If the file is not a JSON object of the documented shape.
    """
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"hv-domains declaration must be a JSON object, got {type(raw).__name__}")
    out: dict[str, dict] = {}
    for domain_id, spec in raw.items():
        if not isinstance(spec, dict):
            raise ValueError(
                f"hv-domains entry {domain_id!r} must be an object with 'refs'/'voltage', "
                f"got {spec!r}"
            )
        refs = spec.get("refs", [])
        if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
            raise ValueError(f"hv-domains entry {domain_id!r} 'refs' must be a list of ref globs")
        out[str(domain_id)] = spec
    return out


def derive_ref_domains_from_voltage_map(
    nets: Sequence,
    voltage_map: Mapping[str, float],
) -> tuple[dict[str, str], dict[str, float]]:
    """Derive per-ref domains from a net voltage map.

    A component's domain is the name of the highest-|voltage| net it touches
    (ties resolve to the first-seen net). The returned ``domain_voltages`` maps
    each such domain id to that net's voltage magnitude.

    Args:
        nets: Sequence of objects exposing ``name`` and ``pins`` (an iterable of
            ``(ref, pad)`` tuples) -- e.g. :class:`kicad_tools.placement.cost.Net`.
        voltage_map: ``{net_name: volts}``.

    Returns:
        ``(ref_domains, domain_voltages)``.
    """
    ref_best: dict[str, tuple[float, str]] = {}
    for net in nets:
        volts = voltage_map.get(net.name)
        if volts is None:
            continue
        mag = abs(float(volts))
        for ref, _pad in net.pins:
            cur = ref_best.get(ref)
            if cur is None or mag > cur[0]:
                ref_best[ref] = (mag, net.name)

    ref_domains = {ref: name for ref, (_mag, name) in ref_best.items()}
    domain_voltages = {name: mag for _ref, (mag, name) in ref_best.items()}
    return ref_domains, domain_voltages


def derive_ref_domains_from_declaration(
    refs: Sequence[str],
    declaration: Mapping[str, dict],
) -> tuple[dict[str, str], dict[str, float]]:
    """Derive per-ref domains from an ``--hv-domains`` declaration.

    A ref matching several domains resolves to the higher-voltage domain
    (matching the voltage-map tie-break). Domains without a ``voltage`` still
    group refs (for reporting) but contribute no creepage requirement.

    Args:
        refs: All component references on the board.
        declaration: ``{domain_id: {"refs": [globs], "voltage": v}}``.

    Returns:
        ``(ref_domains, domain_voltages)``.
    """
    domain_voltages: dict[str, float] = {}
    for domain_id, spec in declaration.items():
        volts = spec.get("voltage")
        if volts is not None:
            domain_voltages[domain_id] = abs(float(volts))

    ref_domains: dict[str, str] = {}
    for ref in refs:
        best: tuple[float, str] | None = None
        for domain_id, spec in declaration.items():
            globs = spec.get("refs", [])
            if any(fnmatch.fnmatchcase(ref, g) for g in globs):
                mag = domain_voltages.get(domain_id, 0.0)
                if best is None or mag > best[0]:
                    best = (mag, domain_id)
        if best is not None:
            ref_domains[ref] = best[1]

    return ref_domains, domain_voltages


def build_required_by_domain_pair(
    domain_voltages: Mapping[str, float],
    *,
    standard_id: str = "iec60664",
    pollution_degree: int = 2,
    material_group: str = "IIIa",
    hv_threshold: float = 30.0,
) -> dict[tuple[str, str], float]:
    """Build the per-domain-pair required-creepage table.

    For every unordered pair of domains whose voltage difference is at least
    *hv_threshold*, the required creepage is looked up in the governing
    standard at ``|ΔV|``. Pairs below the threshold are omitted so that
    low-voltage/low-voltage domain pairs are not over-segregated (normal DRC
    clearance still applies to them).

    Args:
        domain_voltages: ``{domain_id: voltage_magnitude}``.
        standard_id: Creepage standard id (``iec60664`` / ``iec62368``).
        pollution_degree: IEC pollution degree (1, 2 or 3).
        material_group: Insulation material group (``I``/``II``/``IIIa``/``IIIb``).
        hv_threshold: Minimum ``|ΔV|`` (volts) for a pair to receive a keepout.

    Returns:
        ``{(domain_a, domain_b): required_mm}`` with order-independent keys.

    Raises:
        StandardLookupError: If a cross-domain ``|ΔV|`` exceeds the highest
            tabulated row (no silent extrapolation).
    """
    std = get_standard(standard_id)
    ids = sorted(domain_voltages)
    out: dict[tuple[str, str], float] = {}
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            dv = abs(domain_voltages[a] - domain_voltages[b])
            if dv < hv_threshold:
                continue
            required, _prov = std.required_creepage(dv, pollution_degree, material_group)
            out[(a, b)] = required
    return out


def detect_derived_tap_exempt_pairs(
    nets: Sequence,
    ref_domains: Mapping[str, str],
    voltage_map: Mapping[str, float],
    *,
    hv_threshold: float = 30.0,
) -> tuple[set[frozenset[str]], list[str]]:
    """Detect derived sense taps and build their creepage auto-exemption set.

    This is issue #4373 Phase 3 (auto). A *derived tap* is a low-voltage net
    that is electrically bound to an HV net through a bridging divider/limiter
    component (e.g. ``V_AC_SENSE_RAW`` off ``AC_LINE`` through the sense
    resistor). Such a tap **cannot** be pushed far from its parent HV node, so
    penalizing it for sitting close would fight its own spring. Instead it is
    *exempted* from the cross-domain creepage keepout against its parent -- and
    only its parent -- while staying constrained against every other HV domain
    (route it with a guard trace/ring, which is out of scope here, cross-ref
    #4372).

    Detection heuristic (reuses ``hv_threshold`` -- no new required knob):

    1. Each net in *voltage_map* is HV when ``|V| >= hv_threshold`` else LV.
    2. An LV net ``t`` is a **derived tap of** HV net ``h`` when they share at
       least one common component ref (the bridging divider/limiter touches
       both nets) and ``|V_t| < |V_h|``.
    3. For each derived ``(t, h)`` and every ref ``a`` on ``t`` and ``b`` on
       ``h`` whose **domains differ** (``ref_domains[a] != ref_domains[b]``),
       ``frozenset({a, b})`` is added to the exemption set. This exempts exactly
       the intentionally-close tap-side footprints from their parent HV domain
       and nothing else -- the bridging ref itself (which resolves to the HV
       domain) never self-exempts, and the tap keeps its keepout against
       *unrelated* HV domains.

    Args:
        nets: Sequence of objects exposing ``name`` and ``pins`` (an iterable of
            ``(ref, pad)`` tuples) -- e.g. :class:`kicad_tools.placement.cost.Net`.
        ref_domains: Map from component reference to its HV domain id (as
            produced by :func:`derive_ref_domains_from_voltage_map`).
        voltage_map: ``{net_name: volts}`` -- per-net voltages are required, so
            this path is voltage-map only (the ``--hv-domains`` declaration has
            no per-net data and yields no exemptions).
        hv_threshold: Minimum ``|V|`` (volts) at which a net counts as HV.

    Returns:
        ``(exempt_pairs, advisories)`` where *exempt_pairs* is a set of
        ``frozenset({ref_a, ref_b})`` cross-domain pairs to skip in the keepout,
        and *advisories* is a list of human-readable guard notes (one per
        detected tap-parent relation), deterministically ordered.
    """
    if not voltage_map:
        return set(), []

    # Member refs per net, restricted to nets that carry a voltage.
    refs_by_net: dict[str, set[str]] = {}
    for net in nets:
        name = net.name
        if name not in voltage_map:
            continue
        members = refs_by_net.setdefault(name, set())
        for ref, _pad in net.pins:
            members.add(ref)

    hv_nets = sorted(n for n, v in voltage_map.items() if abs(float(v)) >= hv_threshold)
    lv_nets = sorted(n for n, v in voltage_map.items() if abs(float(v)) < hv_threshold)

    exempt_pairs: set[frozenset[str]] = set()
    advisories: list[str] = []

    for t in lv_nets:
        t_refs = refs_by_net.get(t)
        if not t_refs:
            continue
        for h in hv_nets:
            h_refs = refs_by_net.get(h)
            if not h_refs:
                continue
            # A derived tap shares a bridging ref with its HV parent and sits at
            # a strictly lower voltage.
            if t_refs.isdisjoint(h_refs):
                continue
            if abs(float(voltage_map[t])) >= abs(float(voltage_map[h])):
                continue

            added = False
            for a in t_refs:
                da = ref_domains.get(a)
                if da is None:
                    continue
                for b in h_refs:
                    db = ref_domains.get(b)
                    if db is None or da == db:
                        continue
                    exempt_pairs.add(frozenset((a, b)))
                    added = True

            if added:
                advisories.append(
                    f"guarded tap: {t} derived from {h} - route with a guard trace/ring"
                )

    return exempt_pairs, advisories
