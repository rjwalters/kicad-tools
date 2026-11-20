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
   (a footprint touching a 150 V mains net lands in that mains domain).  The
   magnitude only *selects* the domain; cross-domain required creepage is
   looked up at the signed, end-to-end ``|ΔV|`` of the selected nets'
   ``[lo, hi]`` intervals (:func:`interval_pair_dv`, issue #9710), so a
   ``+150 V`` / ``-150 V`` pair is a 300 V pair, as in the creepage census.

2. **HV-domains declaration** (``--hv-domains d.json``) -- the manual fallback
   so the feature works standalone (before #4371 lands). Schema::

       {
         "mains":  {"refs": ["J1", "R1", "R2"], "voltage": 150},
         "signal": {"refs": ["U3", "R10"],      "voltage": 3.3}
       }

   Keys are domain ids; ``refs`` is a list of ``fnmatch`` ref globs and
   ``voltage`` is the domain's representative **signed** working voltage. When
   a ref matches more than one domain it resolves to the higher-|voltage|
   domain; pairs are differenced signed (``+150`` vs ``-150`` is 300 V).

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
from typing import Iterable, Mapping, Sequence

from kicad_tools.creepage.engine import voltage_map_from_dict
from kicad_tools.creepage.standards import get_standard


def load_voltage_intervals(path: str | Path) -> dict[str, tuple[float, float]]:
    """Load a voltage-map sidecar as **signed** closed ``(lo, hi)`` intervals.

    Same parse contract as :func:`load_voltage_map` (shared
    :func:`kicad_tools.creepage.engine.voltage_map_from_dict`; reserved
    ``_``-prefixed keys skipped, ``_edge_voltage`` discarded) but without the
    magnitude collapse: a scalar ``v`` becomes the degenerate ``(v, v)`` and a
    range keeps both signed endpoints.  This is the view placement's pair-stress
    lookup needs (issue #9710): a ``+150 V`` / ``-150 V`` pair differences to
    300 V here exactly as the creepage census reads it, where the magnitude
    view would report 0 V.

    Raises:
        ValueError: If the file is not a JSON object, or if any net voltage is
            not a finite real number.
    """
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"voltage map must be a JSON object, got {type(raw).__name__}")
    intervals = voltage_map_from_dict(raw)[0]
    return {name: (float(iv.lo), float(iv.hi)) for name, iv in intervals.items()}


def load_voltage_map(path: str | Path) -> dict[str, float]:
    """Load a ``{net_name: volts}`` voltage map from a JSON file.

    Delegates parsing to :func:`kicad_tools.creepage.engine.voltage_map_from_dict`
    so the placement ``--voltage-map`` loader honors the exact same parse
    contract as ``kct creepage --voltage-map`` (the two consumers of the shared
    sidecar, #4404). That contract skips ``_``-prefixed reserved keys
    (``_comment`` and friends are documentation) and interprets ``_edge_voltage``
    as the board-edge reference potential. Placement derives per-ref domains from
    net magnitudes and has no concept of an edge/earth reference, so the
    ``edge_voltage`` half of the returned pair is discarded here.

    Range support (#4411): the shared parser models each net as a closed
    ``[lo, hi]`` interval.  This loader returns the **magnitude view**
    ``max(|lo|, |hi|)`` per net -- what domain *selection* and
    ``detect_derived_tap_exempt_pairs`` rank by.  It must **not** be used for
    pair stress: a ``+150 V`` and a ``-150 V`` net both read ``150`` here.  The
    ``optimize-placement`` keepout table is built from
    :func:`load_voltage_intervals` instead (issue #9710), so signed and
    swinging pairs are differenced end-to-end.

    Raises:
        ValueError: If the file is not a JSON object, or if any net voltage is
            not a finite real number.
    """
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        # ``voltage_map_from_dict`` raises ``TypeError`` for a non-dict; keep the
        # historical ``ValueError`` contract for the placement loader's callers.
        raise ValueError(f"voltage map must be a JSON object, got {type(raw).__name__}")
    intervals = voltage_map_from_dict(raw)[0]
    return {name: max(abs(iv.lo), abs(iv.hi)) for name, iv in intervals.items()}


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


def selection_magnitude(value: DomainVoltage) -> float:
    """Worst-case magnitude ``max(|lo|, |hi|)`` of a scalar or interval voltage.

    Used only to *rank* candidate domains (which net/declared domain a ref
    belongs to).  Pair stress is never computed from it -- see
    :func:`interval_pair_dv` (issue #9710).
    """
    lo, hi = as_domain_interval(value)
    return max(abs(lo), abs(hi))


def derive_ref_domains_from_voltage_map(
    nets: Sequence,
    voltage_map: Mapping[str, DomainVoltage],
) -> tuple[dict[str, str], dict[str, float]]:
    """Derive per-ref domains from a net voltage map.

    A component's domain is the name of the highest-magnitude net it touches
    (ties resolve to the first-seen net), where an interval's magnitude is
    ``max(|lo|, |hi|)``. The returned ``domain_voltages`` maps each such domain
    id to that **selection magnitude**.

    The magnitude is for selection only: it is *not* the domain's pair-stress
    voltage (a ``+150 V`` and a ``-150 V`` domain both read ``150``).  Callers
    building a keepout table must pass the selected nets' signed intervals to
    :func:`build_required_by_domain_pair` via ``domain_intervals`` (issue #9710;
    see :func:`domain_intervals_for`).

    Args:
        nets: Sequence of objects exposing ``name`` and ``pins`` (an iterable of
            ``(ref, pad)`` tuples) -- e.g. :class:`kicad_tools.placement.cost.Net`.
        voltage_map: ``{net_name: volts}`` where each value is a signed scalar
            or a ``(lo, hi)`` interval.

    Returns:
        ``(ref_domains, domain_voltages)``.
    """
    ref_best: dict[str, tuple[float, str]] = {}
    for net in nets:
        volts = voltage_map.get(net.name)
        if volts is None:
            continue
        mag = selection_magnitude(volts)
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

    A ref matching several domains resolves to the higher-|voltage| domain
    (matching the voltage-map tie-break; ties keep the first-declared domain).
    Domains without a ``voltage`` still group refs (for reporting) but
    contribute no creepage requirement.

    The returned ``domain_voltages`` are the declared **signed** voltages
    (issue #9710): a ``-150 V`` domain stays ``-150.0`` so
    :func:`build_required_by_domain_pair` differences it against a ``+150 V``
    domain at 300 V.  Ranking uses ``abs(...)`` only.

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
            domain_voltages[domain_id] = float(volts)

    ref_domains: dict[str, str] = {}
    for ref in refs:
        best: tuple[float, str] | None = None
        for domain_id, spec in declaration.items():
            globs = spec.get("refs", [])
            if any(fnmatch.fnmatchcase(ref, g) for g in globs):
                mag = abs(domain_voltages.get(domain_id, 0.0))
                if best is None or mag > best[0]:
                    best = (mag, domain_id)
        if best is not None:
            ref_domains[ref] = best[1]

    return ref_domains, domain_voltages


# ---------------------------------------------------------------------------
# Interval pair differencing (#9659)
# ---------------------------------------------------------------------------

# A domain voltage is either a signed scalar (a degenerate closed interval) or
# a closed ``(lo, hi)`` interval pair (any 2-sequence -- the creepage engine's
# ``VoltageInterval`` named tuple included).
DomainVoltage = float | tuple[float, float]


def as_domain_interval(value: DomainVoltage) -> tuple[float, float]:
    """Coerce a domain voltage to a closed ``(lo, hi)`` interval.

    A scalar ``v`` is the degenerate interval ``(v, v)`` -- **signed**, so a
    bipolar pair such as ``{A: +150, B: -150}`` differences to 300 V exactly as
    the signed route loader (#4867) and the creepage census (#4411) read it.  A
    pair-like value is normalised to a plain float tuple.
    """
    if isinstance(value, (int, float)):
        v = float(value)
        return (v, v)
    try:
        lo, hi = value
    except (TypeError, ValueError) as e:
        raise TypeError(f"domain voltage must be a scalar or a (lo, hi) pair, got {value!r}") from e
    return (float(lo), float(hi))


def interval_pair_dv(a: DomainVoltage, b: DomainVoltage) -> float:
    """Worst-case ``|dV|`` between two closed interval domain voltages.

    ``dv = max(|a.hi - b.lo|, |b.hi - a.lo|)`` -- the same end-to-end
    differencing the creepage census uses per net pair (#4411): a swinging
    node's excursion cannot hide behind its dominant static value.  Two
    degenerate (scalar) intervals collapse to ``|v - w|``, so scalar inputs
    reproduce the pre-interval behaviour exactly.  Shared by the export-rules
    domain matrix (#9659); route-time tables are expected to converge on it.
    """
    a_lo, a_hi = as_domain_interval(a)
    b_lo, b_hi = as_domain_interval(b)
    return max(abs(a_hi - b_lo), abs(b_hi - a_lo))


def domain_intervals_for(
    domain_ids: Iterable[str],
    net_intervals: Mapping[str, DomainVoltage],
) -> dict[str, tuple[float, float]]:
    """Signed ``(lo, hi)`` intervals of the selected voltage-map domains.

    Placement's voltage-map domains are net names (see
    :func:`derive_ref_domains_from_voltage_map`), so each selected domain's
    pair-stress interval is just that net's signed interval.  Feed the result
    to :func:`build_required_by_domain_pair` as ``domain_intervals`` (issue
    #9710).
    """
    return {d: as_domain_interval(net_intervals[d]) for d in domain_ids if d in net_intervals}


def build_required_by_domain_pair(
    domain_voltages: Mapping[str, DomainVoltage],
    *,
    standard_id: str = "iec60664",
    pollution_degree: int = 2,
    material_group: str = "IIIa",
    hv_threshold: float = 30.0,
    pair_bounds: Mapping[frozenset[str], tuple[float, str]] | None = None,
    include_intra_domain: bool = False,
    domain_intervals: Mapping[str, tuple[float, float]] | None = None,
) -> dict[tuple[str, str], float]:
    """Build the per-domain-pair required-creepage table.

    For every unordered pair of domains whose voltage difference is at least
    *hv_threshold*, the required creepage is looked up in the governing
    standard at ``|ΔV|``. Pairs below the threshold are omitted so that
    low-voltage/low-voltage domain pairs are not over-segregated (normal DRC
    clearance still applies to them). Equal-potential pairs are always omitted,
    including when the threshold is zero.

    Args:
        domain_voltages: ``{domain_id: voltage}`` where a voltage is either a
            **signed** scalar (degenerate interval -- signs are load-bearing,
            #4867) or a closed ``(lo, hi)`` interval pair.  Interval values are
            differenced end-to-end via :func:`interval_pair_dv` (#9659), so a
            swinging domain can never collapse against its peers.
        standard_id: Creepage standard id (``iec60664`` / ``iec62368``).
        pollution_degree: IEC pollution degree (1, 2 or 3).
        material_group: Insulation material group (``I``/``II``/``IIIa``/``IIIb``).
        hv_threshold: Minimum ``|ΔV|`` (volts) for a pair to receive a keepout.
        pair_bounds: Optional device-bounded caps (#8043) keyed by the unordered
            ``frozenset({domain_a, domain_b})`` -> ``(max_dv, why)``. The pair's
            ``|ΔV|`` becomes ``min(|ΔV|, max_dv)``: a bound only lowers.
            ``None`` (default) leaves behaviour unchanged.
        include_intra_domain: When ``True``, also emit ``(domain, domain)``
            self-pairs for domains whose own interval span reaches
            *hv_threshold* (a swinging ``[-150, +150]`` domain holds 300 V
            across its own members, #9659).  Scalar (degenerate) domains span
            ``0 V`` and never produce a self-pair, so the default ``False``
            contract is unchanged for every existing caller.
        domain_intervals: Optional per-domain closed ``[lo, hi]`` working-voltage
            intervals (#4411, consumed end-to-end for issue #9602). When a
            domain of a pair carries an entry here it **overrides** the
            interval derived from its *domain_voltages* value, and the pair
            stress is the census' worst-case endpoint combination
            ``dv = max(|a.hi - b.lo|, |b.hi - a.lo|)`` (via
            :func:`interval_pair_dv`, #9659); a domain absent from the mapping
            keeps its *domain_voltages* value (scalar or interval), so an
            all-scalar (or ``None``) input is unchanged by construction.

    Returns:
        ``{(domain_a, domain_b): required_mm}`` with order-independent keys.

    Raises:
        StandardLookupError: If a cross-domain ``|ΔV|`` exceeds the highest
            tabulated row (no silent extrapolation).
    """
    std = get_standard(standard_id)
    ivals = {d: as_domain_interval(v) for d, v in domain_voltages.items()}
    if domain_intervals:
        # #9602 override: an explicit per-domain interval wins over the value
        # carried in domain_voltages (a domain absent there was never iterated
        # on main either, so unknown keys stay ignored).
        for d, iv in domain_intervals.items():
            if d in ivals:
                ivals[d] = as_domain_interval(iv)
    ids = sorted(ivals)
    out: dict[tuple[str, str], float] = {}
    pairs: list[tuple[str, str]] = [
        (ids[i], ids[j]) for i in range(len(ids)) for j in range(i + 1, len(ids))
    ]
    if include_intra_domain:
        pairs.extend((d, d) for d in ids)
    for a, b in pairs:
        dv = interval_pair_dv(ivals[a], ivals[b])
        if pair_bounds and a != b:
            bound = pair_bounds.get(frozenset((a, b)))
            if bound is not None:
                dv = min(dv, bound[0])
        if dv == 0.0 or dv < hv_threshold:
            continue
        # Explicitly basic (also the #7082 default): placement never
        # takes the reinforced opt-in used by kct creepage / kct audit.
        required, _prov = std.required_creepage(
            dv, pollution_degree, material_group, insulation="basic"
        )
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
