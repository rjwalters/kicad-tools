"""Pairwise (net-pair) clearance resolver for HV isolation routing (Issue #4431).

Phase 1 of the "scalar clearance -> pairwise clearance" epic (mirrors the
diff-pair clearance epic #2556). The router's clearance model is
*scalar-per-net* (:attr:`kicad_tools.router.rules.DesignRules.trace_clearance`
and :attr:`~kicad_tools.router.rules.NetClassRouting.clearance`), which cannot
express the HV-isolation requirement: a mains/bank net needs IEC-creepage
spacing (1.3-1.6 mm @150 V PD2) from *low-voltage* copper but only DRU-functional
spacing (0.3-0.4 mm) from its *own cluster's* nets. A single scalar forces the
false choice between "unroutable TO-220 field" (blanket-wide) and "111 board
creepage fails" (cluster-relaxed).

This module adds the shared **data carrier + resolver** and a **Python
post-route validator** primitive. It is a *consumption* problem, not a new
algorithm: the delta-V -> creepage lookup, the HV threshold gating and the
fail-loud out-of-table contract are all reused verbatim from the already-merged
placement derivation
(:func:`kicad_tools.placement.hv_domains.build_required_by_domain_pair`, itself
backed by :meth:`kicad_tools.creepage.standards.CreepageStandard.required_creepage`).
Feeding that builder a per-net ``{net_name: V}`` map of **signed** potentials
(#4867) -- each net treated as its own "domain" -- yields the order-independent
``{(net_a, net_b): required_mm}`` matrix the router resolves against, so the
router, placement and the post-route creepage census all agree by construction.

Explicitly OUT OF SCOPE here (deferred to follow-up architect phases):

* **Phase 2** -- search-time avoidance (widening the A* grid-reservation halo
  around HV copper) plus the C++ ``validate_route`` extension. That is what
  actually lets an HV board *converge* instead of thrashing the negotiator;
  Phase 1 is a diagnostic/foundation only and does NOT by itself make an HV
  board route cleanly.
* **Phase 3** -- KiCad netclass-pair custom ``(rule ...)`` export for
  ``kicad-cli pcb drc`` referee-enforceability.

Backward compatibility: absent a voltage map, :attr:`DesignRules.pairwise_clearance`
is ``None`` and every consumer falls through to the pre-existing scalar path
byte-identically.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, NamedTuple, Sequence

from kicad_tools.core.geometry import segment_to_segment_distance

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment, Via
    from kicad_tools.schema.pcb import Footprint

# A pairwise conflict is only reported when the edge-to-edge gap falls short of
# the requirement by more than this tolerance (mm).  Mirrors the census'
# ``_PASS_TOLERANCE`` -- a sub-micron shortfall is FP noise, not a real defect.
_PASS_TOLERANCE = 1e-4

# Extra necking distance beyond the footprint courtyard (or pad-bounds
# fallback).  Kept as data rather than CLI policy so the same flat regions can
# cross the Python/C++ boundary in Phase 2.
ATTACH_ZONE_MARGIN_MM = 0.5


# KiCad's "this pad exists on every copper layer" wildcard (through-hole pads).
ALL_COPPER_LAYERS = "*.Cu"


def _norm_layer_key(layer: object) -> str:
    """Normalise a layer reference to its KiCad name.

    Accepts a :class:`~kicad_tools.core.types.CopperLayer` enum member (which
    carries ``kicad_name``), a raw ``"F.Cu"``-style string, or anything whose
    ``str()`` is the layer name.
    """
    name = getattr(layer, "kicad_name", None)
    if isinstance(name, str):
        return name
    return str(layer)


@dataclass(frozen=True)
class AttachZone:
    """Rated-footprint necking region (#4506), pad-scoped and layer-scoped.

    ``net_names`` contains every connected net terminating on the footprint.
    Pairwise widening is waived only when the gap point is inside the bbox and
    *both* nets are members; the scalar DRU check has already run separately.

    Issue #4699 narrowed the region twice, because the gate's blanket
    courtyard-bbox waiver silently disagreed with ``kct creepage``'s much
    narrower ``--waive-same-footprint`` (component-internal pad pairs only) and
    turned real trace-to-trace shortfalls into *silent* passes:

    * **Pad-scoped** -- the bbox spans the footprint's CONNECTED PADS (plus
      :data:`ATTACH_ZONE_MARGIN_MM`), not its courtyard.  The physical licence
      for sub-creepage copper here is that the rated part's own pin pitch
      dictates the spacing of the copper attaching to it; that argument covers
      the pad field, not the body overhang / courtyard keep-out ring, where
      through-traffic merely happens to pass.
    * **Layer-scoped** -- ``net_layers`` records, per net, the copper layers on
      which that net actually has pad copper on this footprint.  A pair is
      waived on a given layer only when BOTH nets have pad copper there, so
      copper crossing *under* an SMD part on an inner layer is no longer
      exempted by an XY bbox it never touches.  Through-hole pads carry the
      :data:`ALL_COPPER_LAYERS` wildcard and therefore still waive on every
      layer, as the barrel geometry requires.

    ``net_layers`` is optional: an empty frozenset (hand-built zones, older
    callers) restores the pre-#4699 layer-agnostic behaviour, and a net absent
    from it is unrestricted.
    """

    min_x: float
    min_y: float
    max_x: float
    max_y: float
    net_names: frozenset[str]
    net_layers: frozenset[tuple[str, frozenset[str]]] = frozenset()

    def layers_for(self, net: str) -> frozenset[str]:
        """Copper layers on which ``net`` has pad copper here (empty = any)."""
        if not self.net_layers:
            return frozenset()
        key = _norm_net_key(net)
        for name, layers in self.net_layers:
            if name == key:
                return layers
        return frozenset()

    def covers_layer(self, net: str, layer: object) -> bool:
        """True when ``net``'s pad copper reaches ``layer`` on this footprint."""
        layers = self.layers_for(net)
        if not layers or ALL_COPPER_LAYERS in layers:
            return True
        return _norm_layer_key(layer) in layers

    def exempts(
        self,
        x: float,
        y: float,
        net_a: str,
        net_b: str,
        layer: object | None = None,
    ) -> bool:
        """True when this zone waives the pairwise widening for the pair.

        ``layer`` is the copper layer the two conflicting objects share.
        ``None`` means "no single layer applies" (e.g. a through-via against a
        through-via) and keeps the layer-agnostic verdict.
        """
        a = _norm_net_key(net_a)
        b = _norm_net_key(net_b)
        if not (self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y):
            return False
        if a not in self.net_names or b not in self.net_names:
            return False
        if layer is None:
            return True
        return self.covers_layer(a, layer) and self.covers_layer(b, layer)


def _pad_copper_layers(pad: object) -> frozenset[str]:
    """Copper layers a pad's OWN copper occupies (``*.Cu`` for through-hole).

    Shared by :func:`build_attach_zones` (per-net #4506 zone layer scoping)
    and :func:`board_pad_geometry` (issue #4507's pad-widening layer filter)
    so the two cannot read a pad's layer list two different ways.
    """
    return frozenset(
        layer for layer in (getattr(pad, "layers", None) or ()) if layer.endswith(".Cu")
    )


def build_attach_zones(
    footprints: Iterable[Footprint],
    *,
    margin: float = ATTACH_ZONE_MARGIN_MM,
) -> tuple[AttachZone, ...]:
    """Build the flat pad-bbox attach zones once for a routing session.

    The region is the bounding box of the footprint's *connected* pads plus
    ``margin`` (issue #4699 -- pre-#4699 this preferred the far larger
    courtyard polygon, which waived HV proximity across the whole body
    outline).  Each zone also records, per net, the copper layers that net's
    pads occupy, so the exemption cannot reach a layer the part has no copper
    on.  Empty/unconnected/single-net footprints cannot exempt a pair and are
    omitted.
    """
    from kicad_tools.geometry.courtyard import _fp_transform

    zones: list[AttachZone] = []
    for footprint in footprints:
        connected = [pad for pad in footprint.pads if pad.net_name]
        names = frozenset(_norm_net_key(pad.net_name) for pad in connected)
        if len(names) < 2:
            continue

        bounds: list[tuple[float, float, float, float]] = []
        layers_by_net: dict[str, set[str]] = {}
        transform = _fp_transform(footprint)
        for pad in connected:
            x, y = transform(pad.position)
            # Pad rotation is absolute in the board frame (it already
            # includes the parent footprint rotation).  Project all four
            # rectangular corners onto board axes so the bbox never
            # excludes rotated pad copper.
            angle = math.radians(-getattr(pad, "rotation", 0.0))
            cos_rot = math.cos(angle)
            sin_rot = math.sin(angle)
            half_w = abs(cos_rot) * pad.size[0] / 2.0 + abs(sin_rot) * pad.size[1] / 2.0
            half_h = abs(sin_rot) * pad.size[0] / 2.0 + abs(cos_rot) * pad.size[1] / 2.0
            bounds.append((x - half_w, y - half_h, x + half_w, y + half_h))
            key = _norm_net_key(pad.net_name)
            copper = _pad_copper_layers(pad)
            if copper:
                layers_by_net.setdefault(key, set()).update(copper)
        if not bounds:
            continue

        zones.append(
            AttachZone(
                min(b[0] for b in bounds) - margin,
                min(b[1] for b in bounds) - margin,
                max(b[2] for b in bounds) + margin,
                max(b[3] for b in bounds) + margin,
                names,
                frozenset(
                    (net, frozenset(layers)) for net, layers in layers_by_net.items() if layers
                ),
            )
        )
    return tuple(zones)


def _attach_zone_exempts(
    zones: Sequence[AttachZone],
    x: float,
    y: float,
    net_a: str,
    net_b: str,
    layer: object | None = None,
) -> bool:
    return any(zone.exempts(x, y, net_a, net_b, layer) for zone in zones)


def _norm_net_key(name: str) -> str:
    """Normalise a net name for pairwise lookup (drop one leading ``/``).

    KiCad emits hierarchical net names with a leading ``/`` (``/AC_LINE``);
    hand-authored voltage maps may or may not include it.  Stripping a single
    leading slash on both sides makes the lookup robust to either convention --
    identical to the creepage census' ``_norm_net_key`` (#4371) so the router
    and the census key the same nets.
    """
    return name[1:] if name.startswith("/") else name


@dataclass(frozen=True)
class PairwiseClearanceTable:
    """Order-independent net-pair -> required-clearance carrier (Issue #4431).

    Attributes:
        dru: The scalar manufacturer/DRU clearance floor (mm).  Every resolved
            requirement is at least this value -- a pairwise widening never
            *tightens* below the fab's functional spacing.
        net_voltages: ``{net_name: V}`` worst-case **signed** potential per net
            (relative to the map's common reference), keyed by the
            ``/``-stripped net name.  Signs are load-bearing (#4867): the pair
            requirement is looked up at ``|Va - Vb|``, so a +150 V net and a
            -150 V net are 300 V apart, not 0 V.  Retained for provenance /
            diagnostics; the resolver reads :attr:`required_by_pair`.
        required_by_pair: ``{(net_a, net_b): required_mm}`` with order-independent
            (sorted, ``/``-stripped) keys, as produced by
            :func:`kicad_tools.placement.hv_domains.build_required_by_domain_pair`
            over :attr:`net_voltages`.  Pairs below the HV threshold are absent
            (they need no widening beyond ``dru``).
    """

    dru: float
    net_voltages: Mapping[str, float]
    required_by_pair: Mapping[tuple[str, str], float]

    def required_clearance(self, net_a: str, net_b: str) -> float:
        """Return ``max(dru, creepage_lookup(|Va - Vb|))`` for a net pair.

        Same-net queries and pairs below the HV threshold (absent from
        :attr:`required_by_pair`) return :attr:`dru` -- i.e. the scalar path is
        preserved for everything that does not require HV widening.
        """
        a = _norm_net_key(net_a)
        b = _norm_net_key(net_b)
        if a == b:
            return self.dru
        key = (a, b) if a <= b else (b, a)
        return max(self.dru, self.required_by_pair.get(key, 0.0))

    def max_required_clearance(self) -> float:
        """Largest resolved requirement across every HV pair (mm).

        Issue #4511 / Epic #4431 Phase 2b: the search-time spatial index must
        return every candidate within the *widest* pairwise requirement, not
        just the scalar floor, or the domain-aware blocking kernels never see
        the foreign HV copper they must widen against.  Returns :attr:`dru`
        when no pair needs widening (the dormant / no-voltage-map case), so a
        board without HV pairs inflates its R-tree exactly as before.
        """
        if not self.required_by_pair:
            return self.dru
        return max(self.dru, max(self.required_by_pair.values()))


def load_signed_voltage_map(path: str | Path) -> dict[str, float]:
    """Load a ``--voltage-map`` sidecar preserving each net's **sign** (#4867).

    Parses through :func:`kicad_tools.creepage.engine.voltage_map_from_dict` --
    the same parse contract ``kct creepage`` uses -- so reserved ``_``-prefixed
    metadata keys (``_comment``, ``_edge_voltage``) are skipped identically.

    This is the router's counterpart to placement's
    :func:`kicad_tools.placement.hv_domains.load_voltage_map`, which collapses
    each net to ``max(|lo|, |hi|)`` because placement's cross-domain model is
    magnitude-only.  The router must NOT do that: the pairwise table differences
    potentials, and two nets at +150 V / -150 V are 300 V apart (3.20 mm of IEC
    60664-1 creepage), not 0 V.  A bipolar map about a mid-point reference is
    the normal encoding for an HV bank topology.

    Range support (#4411): each net is parsed as a closed ``[lo, hi]`` interval
    and collapsed to the endpoint of largest magnitude *with its sign* --
    exactly the authored value for a scalar entry (the degenerate interval), and
    the worst-case excursion for a swinging node.  The router's table is
    scalar-per-net, so a genuinely swinging pair can still be differenced less
    conservatively than the census' full
    ``dv = max(|a.hi - b.lo|, |b.hi - a.lo|)``; consuming intervals end-to-end
    is #4411 follow-up work, out of scope here.  For an all-non-negative map
    this returns exactly what ``load_voltage_map`` does.

    Raises:
        ValueError: If the file is not a JSON object, or any net voltage
            endpoint is not a finite real number.
    """
    from kicad_tools.creepage.engine import voltage_map_from_dict

    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        # ``voltage_map_from_dict`` raises ``TypeError`` for a non-dict; keep the
        # ``ValueError`` contract the CLI loaders already catch.
        raise ValueError(f"voltage map must be a JSON object, got {type(raw).__name__}")
    intervals = voltage_map_from_dict(raw)[0]
    return {name: (iv.hi if abs(iv.hi) >= abs(iv.lo) else iv.lo) for name, iv in intervals.items()}


def build_pairwise_clearance_table(
    net_voltages: Mapping[str, float],
    *,
    dru: float,
    standard_id: str = "iec60664",
    pollution_degree: int = 2,
    material_group: str = "IIIa",
    hv_threshold: float = 30.0,
) -> PairwiseClearanceTable:
    """Build a :class:`PairwiseClearanceTable` from a per-net voltage map.

    Reuses :func:`kicad_tools.placement.hv_domains.build_required_by_domain_pair`
    -- the SAME builder placement (#4373) and the creepage census consume --
    treating each net as its own single-net "domain".  Given identical inputs
    the router and placement therefore produce byte-identical matrices (there is
    no forked lookup).  The delta-V -> creepage step is fail-loud: an out-of-table
    ``|Delta V|`` raises
    :class:`~kicad_tools.creepage.standards.StandardLookupError` rather than
    silently extrapolating.

    Args:
        net_voltages: ``{net_name: volts}`` -- **signed** potentials about the
            map's common reference, differenced as supplied (#4867).  Signs are
            load-bearing: ``{A: +150, B: -150}`` is a 300 V pair, exactly as the
            creepage census (``kct creepage``) reads the same sidecar.  Passing
            pre-``abs()``-ed magnitudes silently collapses every bipolar pair to
            ``0 V`` and drops it out of the matrix, so use
            :func:`load_signed_voltage_map` (not placement's magnitude-only
            ``load_voltage_map``) to read a sidecar for this builder.  Reserved
            ``_``-prefixed metadata keys are stripped by that loader.
        dru: Scalar clearance floor (mm), typically ``DesignRules.trace_clearance``.
        standard_id: Creepage standard id (``iec60664`` / ``iec62368``).
        pollution_degree: IEC pollution degree (1, 2 or 3).
        material_group: Insulation material group (``I``/``II``/``IIIa``/``IIIb``).
        hv_threshold: Minimum ``|Delta V|`` (volts) for a pair to receive a
            creepage widening; pairs below it keep only the ``dru`` floor.

    Returns:
        A frozen :class:`PairwiseClearanceTable`.

    Raises:
        StandardLookupError: If a cross-pair ``|Delta V|`` exceeds the highest
            tabulated row (no silent extrapolation).
    """
    # Lazy import keeps the router<->placement dependency off module-import time
    # (placement.__init__ pulls router.rules), avoiding any import-order cycle.
    from kicad_tools.placement.hv_domains import build_required_by_domain_pair

    # Issue #4867: difference the potentials AS SUPPLIED.  Taking ``abs()`` here
    # (pre-#4867) collapsed every bipolar pair before ``build_required_by_domain_pair``
    # could difference it -- +150 V vs -150 V became |150| - |150| = 0 V, fell
    # below ``hv_threshold`` and vanished from the matrix entirely, invisible to
    # both search-time avoidance and the post-route audit.  An all-non-negative
    # map is unaffected (``abs`` was the identity on it).
    normalised = {_norm_net_key(name): float(v) for name, v in net_voltages.items()}
    required = build_required_by_domain_pair(
        normalised,
        standard_id=standard_id,
        pollution_degree=pollution_degree,
        material_group=material_group,
        hv_threshold=hv_threshold,
    )
    return PairwiseClearanceTable(
        dru=float(dru),
        net_voltages=normalised,
        required_by_pair=required,
    )


class SubthresholdCoverage(NamedTuple):
    """Pairs the ``--hv-threshold`` drops that the census still requires (#4507).

    ``build_pairwise_clearance_table`` omits every pair whose ``|Delta V|`` is
    below ``hv_threshold`` (default 30 V), so the router enforces only the
    scalar DRU floor for them.  The creepage census (``kct creepage``) has no
    such threshold: it looks the standard up at whatever ``|Delta V|`` the pair
    actually has, and IEC 60664-1's low-voltage rows are **above** a typical
    0.2 mm fab floor (0.40-0.53 mm for PD2 / material group IIIa).

    Every such pair is therefore a requirement the router will not enforce and
    the census will score -- structurally unreachable "0 board-level fails" on
    any board with low-voltage pairs, no matter how good the search is.  This
    is a *policy* gap, not a defect (the threshold exists so LV<->LV pairs are
    not over-segregated), but it must be visible rather than silent.

    Attributes:
        pair_count: Number of sub-threshold pairs whose requirement exceeds the
            DRU floor.  ``0`` when the threshold hides nothing.
        max_required_mm: Largest such requirement (mm); ``0.0`` when none.
        worst_pair: The ``(net_a, net_b)`` carrying :attr:`max_required_mm`
            (``/``-stripped, sorted), or ``None`` when none.
        worst_delta_v: That pair's ``|Delta V|`` in volts; ``0.0`` when none.
    """

    pair_count: int
    max_required_mm: float
    worst_pair: tuple[str, str] | None
    worst_delta_v: float


def subthreshold_coverage_gap(
    net_voltages: Mapping[str, float],
    *,
    dru: float,
    standard_id: str = "iec60664",
    pollution_degree: int = 2,
    material_group: str = "IIIa",
    hv_threshold: float = 30.0,
) -> SubthresholdCoverage:
    """Measure what ``hv_threshold`` hides from the pairwise matrix (#4507).

    Mirrors :func:`build_pairwise_clearance_table`'s inputs exactly -- same
    signed differencing (#4867), same standard lookup -- but reports the pairs
    that fall on the *other* side of the threshold and would still carry a
    requirement above ``dru``.  See :class:`SubthresholdCoverage`.

    Same-potential pairs (``|Delta V| == 0``) are excluded: the census gives
    them a 0 mm requirement, so they are not a coverage gap.

    The standard lookup is memoised per distinct ``|Delta V|``, so a large
    voltage map costs O(N^2) float comparisons but only a handful of table
    lookups.  An out-of-table ``|Delta V|`` is *swallowed* here (the pair is
    skipped) rather than raised: this is a diagnostic, and the fail-loud
    contract belongs to :func:`build_pairwise_clearance_table`, which sees the
    same voltages.
    """
    from kicad_tools.creepage.standards import StandardLookupError, get_standard

    std = get_standard(standard_id)
    normalised = {_norm_net_key(name): float(v) for name, v in net_voltages.items()}
    ids = sorted(normalised)
    lookup_cache: dict[float, float | None] = {}

    count = 0
    worst_required = 0.0
    worst_pair: tuple[str, str] | None = None
    worst_dv = 0.0
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            dv = abs(normalised[a] - normalised[b])
            if dv <= 0.0 or dv >= hv_threshold:
                continue
            if dv not in lookup_cache:
                try:
                    lookup_cache[dv] = std.required_creepage(dv, pollution_degree, material_group)[
                        0
                    ]
                except StandardLookupError:
                    lookup_cache[dv] = None
            required = lookup_cache[dv]
            if required is None or required <= dru:
                continue
            count += 1
            if required > worst_required:
                worst_required = required
                worst_pair = (a, b)
                worst_dv = dv
    return SubthresholdCoverage(count, worst_required, worst_pair, worst_dv)


class CppDomainMatrix(NamedTuple):
    """C++-consumable form of a :class:`PairwiseClearanceTable` (Issue #4510).

    Attributes:
        net_to_domain: Indexed by net id; entry is the net's domain index or
            ``-1`` when the net participates in no widening pair.  Net ids
            beyond the list's length are domain-less by construction.
        matrix: Dense symmetric ``matrix[dom_a][dom_b]`` of *widening* values
            (mm).  Entries are the raw creepage requirement WITHOUT the DRU
            floor, so the C++ validator's ``max(effective_scalar, matrix[a][b])``
            never raises a pair above its scalar rule unless HV widening
            genuinely applies -- matching
            :func:`segment_pair_violation`'s ``required <= floor`` skip.
    """

    net_to_domain: list[int]
    matrix: list[list[float]]


def build_cpp_domain_matrix(
    table: PairwiseClearanceTable | None,
    net_name_to_id: Mapping[str, int],
) -> CppDomainMatrix | None:
    """Project a pairwise table onto integer net ids for ``Grid3D`` (Issue #4510).

    ``Grid3D`` has no string table, so the net-name-keyed
    :attr:`PairwiseClearanceTable.required_by_pair` must be re-expressed as a
    per-net domain-id array plus a dense domain-pair matrix.  Phase 1 treats
    each net as its own domain, so the "domains" here are exactly the nets that
    participate in at least one widening pair AND resolve to a board net id.

    Returns ``None`` when nothing needs widening (no table, an empty matrix, or
    fewer than two resolvable participating nets) -- the dormant signal that
    keeps the C++ setter uncalled and ``validate_route`` byte-identical.
    """
    if table is None:
        return None
    pairs = table.required_by_pair
    if not pairs:
        return None

    participating: set[str] = set()
    for (net_a, net_b), required in pairs.items():
        if required > 0.0:
            participating.add(net_a)
            participating.add(net_b)
    if not participating:
        return None

    # A board net name may carry a leading ``/``; the table keys never do.  Two
    # distinct board nets can in principle normalise to the same key, so every
    # matching id joins the same domain.
    ids_by_key: dict[str, list[int]] = {}
    for name, net_id in net_name_to_id.items():
        key = _norm_net_key(name)
        if key in participating and int(net_id) >= 0:
            ids_by_key.setdefault(key, []).append(int(net_id))
    if len(ids_by_key) < 2:
        return None

    domain_keys = sorted(ids_by_key)  # deterministic domain indices
    domain_index = {key: i for i, key in enumerate(domain_keys)}

    max_id = max(net_id for ids in ids_by_key.values() for net_id in ids)
    net_to_domain = [-1] * (max_id + 1)
    for key, ids in ids_by_key.items():
        for net_id in ids:
            net_to_domain[net_id] = domain_index[key]

    size = len(domain_keys)
    matrix = [[0.0] * size for _ in range(size)]
    for (net_a, net_b), required in pairs.items():
        i = domain_index.get(net_a)
        j = domain_index.get(net_b)
        if i is None or j is None:
            continue
        value = float(required)
        matrix[i][j] = value
        matrix[j][i] = value

    return CppDomainMatrix(net_to_domain=net_to_domain, matrix=matrix)


def project_zone_layers(
    zone: AttachZone,
    ids_by_key: Mapping[str, Sequence[int]],
    layer_indices: Mapping[str, int] | None,
) -> dict[int, frozenset[int]] | None:
    """Project a zone's per-net pad layers into engine layer indices (#4699).

    Shared by every id-space consumer of the #4506 exemption -- the lattice
    projection (``lattice/pairwise.py``) and the C++ ``Grid3D`` projection
    (:func:`attach_zones_to_net_ids`, issue #4507) -- so no engine can drift
    into a different layer verdict than the ``AttachZone.exempts`` acceptance
    check the #4588 gate runs.

    Returns ``None`` -- the layer-agnostic verdict -- when no layer map was
    supplied or the zone records no pad layers (older / hand-built zones).  A
    net whose pads carry the :data:`ALL_COPPER_LAYERS` wildcard (through-hole)
    is omitted from the result, which every consumer reads as "unrestricted",
    so a through-hole rated part keeps waiving on every layer.
    """
    if layer_indices is None or not zone.net_layers:
        return None
    projected: dict[int, frozenset[int]] = {}
    for key, layer_names in zone.net_layers:
        if ALL_COPPER_LAYERS in layer_names:
            continue  # through-hole pad copper exists on every layer
        indices = frozenset(
            idx for name in layer_names if (idx := layer_indices.get(name)) is not None
        )
        if not indices:
            continue
        for net_id in ids_by_key.get(key, ()):
            projected[net_id] = indices
    return projected or None


def attach_zones_to_net_ids(
    zones: Sequence[AttachZone],
    net_name_to_id: Mapping[str, int],
    layer_indices: Mapping[str, int] | None = None,
) -> list[tuple[float, float, float, float, list[int], dict[int, frozenset[int]] | None]]:
    """Translate net-name-keyed attach zones to net-id tuples (Issue #4510).

    ``Grid3D`` works in integer net ids, so :attr:`AttachZone.net_names` must be
    resolved through the router's reverse map before crossing into C++.  A zone
    may legitimately span more than two nets (a 3-pad rated connector); zones
    that resolve to fewer than two ids cannot exempt any pair and are dropped.

    ``layer_indices`` (issue #4507) maps KiCad copper-layer names to the C++
    grid's layer indices, so each zone's per-net pad layers
    (:attr:`AttachZone.net_layers`) project into the integer layer space
    ``Grid3D::attach_zone_exempts`` speaks.  Omitting it -- or supplying a zone
    that records no pad layers -- yields ``None`` in the trailing slot, the
    layer-agnostic verdict the C++ side used before #4507.
    """
    ids_by_key: dict[str, list[int]] = {}
    for name, net_id in net_name_to_id.items():
        if int(net_id) >= 0:
            ids_by_key.setdefault(_norm_net_key(name), []).append(int(net_id))

    out: list[tuple[float, float, float, float, list[int], dict[int, frozenset[int]] | None]] = []
    for zone in zones:
        net_ids = sorted({nid for key in zone.net_names for nid in ids_by_key.get(key, ())})
        if len(net_ids) < 2:
            continue
        out.append(
            (
                zone.min_x,
                zone.min_y,
                zone.max_x,
                zone.max_y,
                net_ids,
                project_zone_layers(zone, ids_by_key, layer_indices),
            )
        )
    return out


class PairwiseViolation(NamedTuple):
    """A single post-route pairwise-clearance shortfall (Issue #4431).

    Attributes:
        net_a: The moving/route net name.
        net_b: The foreign net name it is too close to.
        actual_mm: Measured edge-to-edge gap (mm).
        required_mm: The derived pairwise requirement (mm).
        x: Violation x-coordinate (mm) -- midpoint of the closest-gap segment
            between the two conflicting copper elements (trace, via or --
            issue #4507 -- pad).
        y: Violation y-coordinate (mm).
    """

    net_a: str
    net_b: str
    actual_mm: float
    required_mm: float
    x: float
    y: float


def _segment_edge_gap(seg_a: Segment, seg_b: Segment) -> float:
    """Edge-to-edge gap (mm) between two trace segments' copper on one layer."""
    centre = segment_to_segment_distance(
        seg_a.x1, seg_a.y1, seg_a.x2, seg_a.y2, seg_b.x1, seg_b.y1, seg_b.x2, seg_b.y2
    )
    return centre - seg_a.width / 2.0 - seg_b.width / 2.0


def _closest_gap_midpoint(seg_a: Segment, seg_b: Segment) -> tuple[float, float]:
    """Return the midpoint between the closest points on two line segments."""
    return closest_gap_midpoint_xy(
        seg_a.x1, seg_a.y1, seg_a.x2, seg_a.y2, seg_b.x1, seg_b.y1, seg_b.x2, seg_b.y2
    )


def closest_gap_midpoint_xy(
    ax1: float,
    ay1: float,
    ax2: float,
    ay2: float,
    bx1: float,
    by1: float,
    bx2: float,
    by2: float,
) -> tuple[float, float]:
    """Coordinate-level form of :func:`_closest_gap_midpoint` (issue #4602).

    The lattice engine's search-time attach-zone exemption probes the SAME
    closest-gap midpoint the post-route validator reports, so the in-search
    verdict and the #4588 gate agree by construction.  The lattice works on
    bare point tuples, hence this :class:`Segment`-free entry point.
    """
    ux, uy = ax2 - ax1, ay2 - ay1
    vx, vy = bx2 - bx1, by2 - by1
    wx, wy = ax1 - bx1, ay1 - by1
    a = ux * ux + uy * uy
    b = ux * vx + uy * vy
    c = vx * vx + vy * vy
    d = ux * wx + uy * wy
    e = vx * wx + vy * wy

    if a <= 1e-15 and c <= 1e-15:
        return ((ax1 + bx1) / 2.0, (ay1 + by1) / 2.0)
    if a <= 1e-15:
        t = max(0.0, min(1.0, e / c))
        closest_b = (bx1 + t * vx, by1 + t * vy)
        return ((ax1 + closest_b[0]) / 2.0, (ay1 + closest_b[1]) / 2.0)
    if c <= 1e-15:
        s = max(0.0, min(1.0, -d / a))
        closest_a = (ax1 + s * ux, ay1 + s * uy)
        return ((closest_a[0] + bx1) / 2.0, (closest_a[1] + by1) / 2.0)

    denominator = a * c - b * b
    s_num, s_den = denominator, denominator
    t_num, t_den = denominator, denominator

    if denominator <= 1e-15:
        s_num, s_den = 0.0, 1.0
        t_num, t_den = e, c
    else:
        s_num = b * e - c * d
        t_num = a * e - b * d
        if s_num < 0.0:
            s_num, t_num, t_den = 0.0, e, c
        elif s_num > s_den:
            s_num, t_num, t_den = s_den, e + b, c

    if t_num < 0.0:
        t_num = 0.0
        if -d < 0.0:
            s_num, s_den = 0.0, 1.0
        elif -d > a:
            s_num, s_den = 1.0, 1.0
        else:
            s_num, s_den = -d, a
    elif t_num > t_den:
        t_num = t_den
        if -d + b < 0.0:
            s_num, s_den = 0.0, 1.0
        elif -d + b > a:
            s_num, s_den = 1.0, 1.0
        else:
            s_num, s_den = -d + b, a

    s = 0.0 if abs(s_num) <= 1e-15 else s_num / s_den
    t = 0.0 if abs(t_num) <= 1e-15 else t_num / t_den
    closest_a = (ax1 + s * ux, ay1 + s * uy)
    closest_b = (bx1 + t * vx, by1 + t * vy)
    return ((closest_a[0] + closest_b[0]) / 2.0, (closest_a[1] + closest_b[1]) / 2.0)


class PadGeometry(NamedTuple):
    """A foreign pad's true copper polygon, for the pad-widening gate (#4507).

    Eight of the seventeen T4 softstart-rev-C residual pairwise fails (PR
    #4885's audit) are routed trace/via copper against a foreign PAD -- copper
    :func:`segment_pair_violation` cannot see because it only ever compares two
    :class:`~kicad_tools.router.primitives.Segment` objects.  This carrier is
    the pad-shaped analogue of a :class:`~kicad_tools.router.primitives.Route`
    that :func:`_copper_pair_violation` can compare a trace or via against.

    Attributes:
        net_name: The pad's net (raw, not ``/``-stripped -- every consumer
            here routes lookups through :meth:`PairwiseClearanceTable.
            required_clearance`, which normalises on both sides itself).
        layers: Copper layers the pad's OWN copper occupies, from
            :func:`_pad_copper_layers`.  Empty means "unknown/unrestricted"
            (mirrors :meth:`AttachZone.covers_layer`'s convention) --
            :data:`ALL_COPPER_LAYERS` is the through-hole wildcard, present
            verbatim rather than expanded, exactly as ``AttachZone.net_layers``
            stores it.
        polygon: The pad's TRUE copper outline (roundrect/oval honoured, via
            :func:`~kicad_tools.validate.rules.clearance._pad_polygon`) in the
            SAME sheet-absolute frame every :class:`Segment`/:class:`Via` this
            module works with uses.
    """

    net_name: str
    layers: frozenset[str]
    polygon: Any


def _pad_covers_layer(layers: frozenset[str], layer: object) -> bool:
    """True when a pad's own copper reaches ``layer`` (empty/``*.Cu`` = any)."""
    if not layers or ALL_COPPER_LAYERS in layers:
        return True
    return _norm_layer_key(layer) in layers


def _pad_probe_layer(pad: PadGeometry) -> str | None:
    """The single shared layer to probe a #4506 zone at for a via-vs-``pad``
    pair (issue #4507).

    A via is modelled layer-agnostic here (its barrel is assumed to reach
    every layer it might occupy -- see :func:`_via_copper_polygon`'s
    docstring), so the "shared layer" for the exemption probe is the PAD's:
    ``None`` (layer-agnostic, mirroring the through-hole / via-vs-via
    convention) when the pad is through-hole, layer-unknown, or occupies more
    than one explicit copper layer; that one layer name otherwise.
    """
    if not pad.layers or ALL_COPPER_LAYERS in pad.layers or len(pad.layers) != 1:
        return None
    return next(iter(pad.layers))


def _segment_copper_polygon(seg: Segment) -> Any | None:
    """A trace segment's true copper outline (rectangle + round caps)."""
    from kicad_tools.geometry.copper import segment_copper_polygon

    return segment_copper_polygon(seg.start, seg.end, seg.width)


def _via_copper_polygon(via: Via) -> Any:
    """A via's copper disc, in the barrel's own (x, y) with its full diameter.

    Modelled layer-agnostic: a via's annular ring exists on every layer its
    barrel spans, and (for the common through-hole case this fixture and the
    C++ ``validate_route`` via-vs-via/via-vs-segment branches share) that is
    usually the whole stack.  A blind/buried via that does NOT reach a given
    inner layer is therefore checked one layer wider than its true geometry --
    a conservative (never-silently-missed) approximation, not a correctness
    regression, and the same simplification :func:`_pad_probe_layer` accepts
    on the pad side of a via pair.
    """
    from shapely.geometry import Point  # type: ignore[import-untyped]

    return Point(via.x, via.y).buffer(via.diameter / 2.0)


def _shapely_gap_and_midpoint(poly_a: Any, poly_b: Any) -> tuple[float, float, float]:
    """Edge-to-edge gap (mm) and closest-gap midpoint between two polygons.

    The polygon analogue of :func:`_segment_edge_gap` / :func:`_closest_gap_midpoint`
    for copper this module cannot model as two line segments (pads, vias).
    ``shapely.shortest_line`` returns the endpoints closest points on ``poly_a``
    and ``poly_b`` even when the two overlap (the fully-degenerate case a
    negative-clearance HV shortfall can produce); this mirrors
    :func:`closest_gap_midpoint_xy`'s own tolerance of that situation.
    """
    import shapely  # type: ignore[import-untyped]

    gap = float(poly_a.distance(poly_b))
    line = shapely.shortest_line(poly_a, poly_b)
    if line is None or line.is_empty:
        point = poly_a.centroid
        return gap, float(point.x), float(point.y)
    (x1, y1), (x2, y2) = line.coords[0], line.coords[-1]
    return gap, (x1 + x2) / 2.0, (y1 + y2) / 2.0


def _copper_pair_violation(
    poly_a: Any,
    net_a: str,
    poly_b: Any,
    net_b: str,
    layer: object | None,
    table: PairwiseClearanceTable,
    *,
    floor: float,
    attach_zones: Sequence[AttachZone],
    tolerance: float,
) -> PairwiseViolation | None:
    """Shapely-geometry pairwise check -- the pad/via analogue of
    :func:`segment_pair_violation` (issue #4507).

    Used for any copper PAIR that is not trace-vs-trace (which stays on the
    faster analytic path segment_pair_violation already had).  ``layer`` is
    the single shared copper layer the two pieces occupy -- exactly what
    ``segment_pair_violation`` requires its two segments to already agree on
    -- or ``None`` for a layer-agnostic pair (via-vs-via, or an ambiguous/
    through-hole pad probe), the same convention the C++ ``widen()`` helper
    (``grid.cpp``) and ``LatticePairwise.exempt_pt_pt`` use for via-vs-via.
    """
    required = table.required_clearance(net_a, net_b)
    if required <= floor + tolerance:
        return None
    gap, gap_x, gap_y = _shapely_gap_and_midpoint(poly_a, poly_b)
    if gap >= required - tolerance:
        return None
    if gap >= floor - tolerance and _attach_zone_exempts(
        attach_zones, gap_x, gap_y, net_a, net_b, layer
    ):
        return None
    return PairwiseViolation(
        net_a=net_a,
        net_b=net_b,
        actual_mm=gap,
        required_mm=required,
        x=gap_x,
        y=gap_y,
    )


def segment_pair_violation(
    seg_a: Segment,
    seg_b: Segment,
    table: PairwiseClearanceTable,
    *,
    net_a_name: str | None = None,
    net_b_name: str | None = None,
    dru: float | None = None,
    attach_zones: Sequence[AttachZone] = (),
    tolerance: float = _PASS_TOLERANCE,
) -> PairwiseViolation | None:
    """Check one segment pair against its derived pairwise requirement.

    Returns a :class:`PairwiseViolation` when the two segments share a layer and
    their edge-to-edge gap falls short of ``required_clearance(a, b)`` by more
    than ``tolerance``; otherwise ``None``.  Pairs whose requirement does not
    exceed the scalar DRU floor are skipped -- the ordinary scalar clearance
    check already governs them, so this validator only adds the *HV widening*.

    Net names default to the segments' own :attr:`Segment.net_name`; the live
    in-loop validators pass ``net_a_name``/``net_b_name`` explicitly because a
    mid-route segment may not yet carry a populated net-name string (the net id
    is authoritative there and is resolved by the caller).

    Different-layer pairs return ``None`` in Phase 1 (surface creepage is a
    same-layer phenomenon here; cross-layer/through-hole geometry is the census'
    and Phase 2's remit).
    """
    if seg_a.layer != seg_b.layer:
        return None
    a_name = seg_a.net_name if net_a_name is None else net_a_name
    b_name = seg_b.net_name if net_b_name is None else net_b_name
    floor = table.dru if dru is None else dru
    required = table.required_clearance(a_name, b_name)
    if required <= floor + tolerance:
        # No HV widening for this pair -- the scalar path already covers it.
        return None
    gap = _segment_edge_gap(seg_a, seg_b)
    if gap >= required - tolerance:
        return None
    gap_x, gap_y = _closest_gap_midpoint(seg_a, seg_b)
    if gap >= floor - tolerance and _attach_zone_exempts(
        attach_zones, gap_x, gap_y, a_name, b_name, seg_a.layer
    ):
        return None
    return PairwiseViolation(
        net_a=a_name,
        net_b=b_name,
        actual_mm=gap,
        required_mm=required,
        x=gap_x,
        y=gap_y,
    )


def route_pairwise_violation(
    route: Route,
    exclude_net: int,
    foreign_routes: Iterable[Route],
    table: PairwiseClearanceTable,
    *,
    id_to_name: Mapping[int, str] | None = None,
    dru: float | None = None,
    attach_zones: Sequence[AttachZone] = (),
    tolerance: float = _PASS_TOLERANCE,
    foreign_pads: Sequence[PadGeometry] = (),
) -> PairwiseViolation | None:
    """Find the first pairwise-clearance shortfall for a freshly-routed net.

    Walks every segment AND via of ``route`` against every segment and via of
    the already-routed ``foreign_routes`` (skipping the route's own net),
    returning the first :class:`PairwiseViolation` or ``None`` when the route
    clears all pairwise requirements.  This is the additive HV check threaded
    into the Python post-route validators (``pathfinder`` and ``cpp_backend``);
    the scalar segment/pad/via checks run first and unchanged.

    Issue #4507: via coverage comes for free from :attr:`Route.vias` (every
    caller already carries real via geometry there); ``foreign_pads`` is
    additive and opt-in (default ``()``, a strict no-op) -- only
    :func:`board_pairwise_violations` populates it today, from
    :func:`board_pad_geometry`.

    ``exclude_net`` is the route's own net id (same-net copper never conflicts).
    ``id_to_name`` resolves a net id to its board net name; when omitted the
    routes' own :attr:`Route.net_name` strings are used.  Ids are authoritative
    mid-route (segment/route name strings may be unset), so the live validators
    pass an inverted ``net_name_to_id`` map here.
    """
    if table is None:
        return None
    floor = table.dru if dru is None else dru
    # ``exclude_net`` is the route's own net id and is authoritative mid-route
    # (``route.net``/``route.net_name`` may be unset before finalisation).
    moving_name = _resolve_net_name(id_to_name, exclude_net, route.net_name)
    return _segments_pairwise_violation(
        route.segments,
        moving_name,
        exclude_net,
        foreign_routes,
        table,
        id_to_name=id_to_name,
        floor=floor,
        attach_zones=attach_zones,
        tolerance=tolerance,
        vias=route.vias,
        foreign_pads=foreign_pads,
    )


def _moving_via_vs_foreign_segment(
    via: Via,
    moving_name: str,
    seg: Segment,
    foreign_name: str,
    table: PairwiseClearanceTable,
    *,
    floor: float,
    attach_zones: Sequence[AttachZone],
    tolerance: float,
) -> PairwiseViolation | None:
    """Moving-via-vs-foreign-trace pairwise check (issue #4507).

    ``net_a`` is always the MOVING net (the via's), matching every other
    violation this module reports.
    """
    seg_poly = _segment_copper_polygon(seg)
    if seg_poly is None:
        return None
    return _copper_pair_violation(
        _via_copper_polygon(via),
        moving_name,
        seg_poly,
        foreign_name,
        seg.layer,
        table,
        floor=floor,
        attach_zones=attach_zones,
        tolerance=tolerance,
    )


def _moving_segment_vs_foreign_via(
    seg: Segment,
    moving_name: str,
    via: Via,
    foreign_name: str,
    table: PairwiseClearanceTable,
    *,
    floor: float,
    attach_zones: Sequence[AttachZone],
    tolerance: float,
) -> PairwiseViolation | None:
    """Moving-trace-vs-foreign-via pairwise check (issue #4507)."""
    seg_poly = _segment_copper_polygon(seg)
    if seg_poly is None:
        return None
    return _copper_pair_violation(
        seg_poly,
        moving_name,
        _via_copper_polygon(via),
        foreign_name,
        seg.layer,
        table,
        floor=floor,
        attach_zones=attach_zones,
        tolerance=tolerance,
    )


def _via_vs_via_violation(
    via_a: Via,
    net_a: str,
    via_b: Via,
    net_b: str,
    table: PairwiseClearanceTable,
    *,
    floor: float,
    attach_zones: Sequence[AttachZone],
    tolerance: float,
) -> PairwiseViolation | None:
    """Via-vs-via pairwise check (issue #4507); always layer-agnostic."""
    return _copper_pair_violation(
        _via_copper_polygon(via_a),
        net_a,
        _via_copper_polygon(via_b),
        net_b,
        None,
        table,
        floor=floor,
        attach_zones=attach_zones,
        tolerance=tolerance,
    )


def _copper_vs_pad_violation(
    poly: Any,
    net_name: str,
    layer: object | None,
    pad: PadGeometry,
    table: PairwiseClearanceTable,
    *,
    floor: float,
    attach_zones: Sequence[AttachZone],
    tolerance: float,
) -> PairwiseViolation | None:
    """Trace-or-via-vs-pad pairwise check (issue #4507).

    ``layer`` is the moving copper's own layer for a trace (applicability is
    enforced -- a pad on a different, non-through-hole layer cannot conflict);
    pass ``None`` for a via, whose barrel is modelled layer-agnostic (see
    :func:`_via_copper_polygon`), which also skips the applicability filter.
    """
    if layer is not None and not _pad_covers_layer(pad.layers, layer):
        return None
    probe_layer = layer if layer is not None else _pad_probe_layer(pad)
    return _copper_pair_violation(
        poly,
        net_name,
        pad.polygon,
        pad.net_name,
        probe_layer,
        table,
        floor=floor,
        attach_zones=attach_zones,
        tolerance=tolerance,
    )


def _segments_pairwise_violation(
    segments: Sequence[Segment],
    moving_name: str,
    exclude_net: int,
    foreign_routes: Iterable[Route],
    table: PairwiseClearanceTable,
    *,
    id_to_name: Mapping[int, str] | None,
    floor: float,
    attach_zones: Sequence[AttachZone],
    tolerance: float,
    vias: Sequence[Via] = (),
    foreign_pads: Sequence[PadGeometry] = (),
) -> PairwiseViolation | None:
    """Shared moving-copper-vs-foreign-copper walk (issue #4507).

    ONE implementation backs :func:`route_pairwise_violation` (the post-route
    acceptance gate), :func:`path_pairwise_violation` (the candidate-path
    predicate, trace-vs-trace only -- it has no via/route context to widen
    with) and (via :func:`find_pairwise_violations`) the board-level audit and
    replay, so a copper-moving pass that consults the predicate can never
    disagree with the gate that audits its output -- the same single-
    implementation discipline :func:`project_zone_layers` enforces for the
    #4506 layer scoping.

    Coverage past trace-vs-trace (issue #4507's widening, dormant unless a
    caller supplies ``vias``/``foreign_pads``):

    * ``segments`` (moving) vs a foreign route's :attr:`Route.vias` --
      trace-vs-via.
    * ``vias`` (moving) vs a foreign route's segments/vias -- via-vs-trace,
      via-vs-via.
    * ``segments``/``vias`` (moving) vs ``foreign_pads`` -- trace-vs-pad,
      via-vs-pad.  Checked once per pad (not per foreign route), since a pad
      is static board copper, not owned by any :class:`Route`.
    """
    for other in foreign_routes:
        if other.net == exclude_net:
            continue
        # Cheap prune: skip the whole foreign route when this net pair needs no
        # HV widening beyond the scalar floor.
        foreign_name = _resolve_net_name(id_to_name, other.net, other.net_name)
        required = table.required_clearance(moving_name, foreign_name)
        if required <= floor + tolerance:
            continue
        for seg in segments:
            for oseg in other.segments:
                violation = segment_pair_violation(
                    seg,
                    oseg,
                    table,
                    net_a_name=moving_name,
                    net_b_name=foreign_name,
                    dru=floor,
                    attach_zones=attach_zones,
                    tolerance=tolerance,
                )
                if violation is not None:
                    return violation
            for ovia in other.vias:
                violation = _moving_segment_vs_foreign_via(
                    seg,
                    moving_name,
                    ovia,
                    foreign_name,
                    table,
                    floor=floor,
                    attach_zones=attach_zones,
                    tolerance=tolerance,
                )
                if violation is not None:
                    return violation
        for via in vias:
            for oseg in other.segments:
                violation = _moving_via_vs_foreign_segment(
                    via,
                    moving_name,
                    oseg,
                    foreign_name,
                    table,
                    floor=floor,
                    attach_zones=attach_zones,
                    tolerance=tolerance,
                )
                if violation is not None:
                    return violation
            for ovia in other.vias:
                violation = _via_vs_via_violation(
                    via,
                    moving_name,
                    ovia,
                    foreign_name,
                    table,
                    floor=floor,
                    attach_zones=attach_zones,
                    tolerance=tolerance,
                )
                if violation is not None:
                    return violation

    for pad in foreign_pads:
        required = table.required_clearance(moving_name, pad.net_name)
        if required <= floor + tolerance:
            continue
        for seg in segments:
            seg_poly = _segment_copper_polygon(seg)
            if seg_poly is None:
                continue
            violation = _copper_vs_pad_violation(
                seg_poly,
                moving_name,
                seg.layer,
                pad,
                table,
                floor=floor,
                attach_zones=attach_zones,
                tolerance=tolerance,
            )
            if violation is not None:
                return violation
        for via in vias:
            violation = _copper_vs_pad_violation(
                _via_copper_polygon(via),
                moving_name,
                None,
                pad,
                table,
                floor=floor,
                attach_zones=attach_zones,
                tolerance=tolerance,
            )
            if violation is not None:
                return violation
    return None


def path_pairwise_violation(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    layer: Layer,
    width: float,
    exclude_net: int,
    foreign_routes: Iterable[Route],
    table: PairwiseClearanceTable | None,
    *,
    net_name: str | None = None,
    id_to_name: Mapping[int, str] | None = None,
    dru: float | None = None,
    attach_zones: Sequence[AttachZone] = (),
    tolerance: float = _PASS_TOLERANCE,
) -> PairwiseViolation | None:
    """Check ONE candidate path against the pairwise (HV) requirement (#4507).

    The coordinate-level form of :func:`route_pairwise_violation`: instead of
    a finished :class:`~kicad_tools.router.primitives.Route` it takes the
    ``(x1, y1) -> (x2, y2)`` path a copper-moving pass is *about to commit* --
    the exact shape the optimizer's
    :meth:`~kicad_tools.router.optimizer.collision.CollisionChecker.path_is_clear`
    hook already speaks (the first seven parameters match it positionally on
    purpose).  Issue #4766 consults this before accepting a moved segment, so
    the ``--lattice-optimize`` post-passes stop proposing copper the pairwise
    audit backstop then flags.

    Scope matches the audit (same #4506 attach-zone exemption): both delegate
    to the same :func:`_segments_pairwise_violation` walk, so predicate and
    gate agree by construction.  Issue #4507 widened that shared walk to also
    check the candidate against a foreign route's :attr:`Route.vias`
    (trace-vs-via); the candidate itself has no via of its own (it is one
    in-flight segment move), and foreign PAD copper is not threaded through
    this predicate -- callers that need pad coverage use
    :func:`route_pairwise_violation` / :func:`find_pairwise_violations` with
    an explicit ``foreign_pads``, as :func:`board_pairwise_violations` does.

    The moving net's name resolves from ``net_name`` when supplied, else from
    ``id_to_name[exclude_net]``.  An unresolvable moving net returns ``None``
    -- identical to the gate's empty ``route.net_name`` fallback, where an
    unmatchable name can participate in no widening pair.  ``table=None`` (no
    ``--voltage-map``) is likewise a strict no-op.
    """
    if table is None:
        return None
    moving_name = (
        net_name if net_name is not None else _resolve_net_name(id_to_name, exclude_net, "")
    )
    if not moving_name:
        return None
    floor = table.dru if dru is None else dru

    # Lazy import mirrors the module's TYPE_CHECKING-only primitives import
    # (this module stays importable without the router package loaded).
    from kicad_tools.router.primitives import Segment

    candidate = Segment(x1, y1, x2, y2, width, layer, net=exclude_net, net_name=moving_name)
    return _segments_pairwise_violation(
        (candidate,),
        moving_name,
        exclude_net,
        foreign_routes,
        table,
        id_to_name=id_to_name,
        floor=floor,
        attach_zones=attach_zones,
        tolerance=tolerance,
    )


@dataclass(frozen=True)
class PairwisePathChecker:
    """Bound pairwise (HV) path predicate for copper-moving passes (#4507).

    Packages everything :func:`path_pairwise_violation` needs -- the resolved
    :class:`PairwiseClearanceTable`, a **live view** of the board's committed
    foreign copper, the id->name map and the #4506 attach zones -- behind the
    optimizer's ``path_is_clear(x1, y1, x2, y2, layer, width, exclude_net)``
    calling convention (:class:`~kicad_tools.router.optimizer.collision.
    CollisionChecker`), so issue #4766 can compose it with the existing scalar
    collision checker: a moved segment is acceptable only when the scalar
    checker AND this predicate both pass.

    ``foreign_routes`` is a zero-argument callable re-evaluated on every query
    (typically ``lambda: grid.routes``): the grid-synced optimize loop unmarks
    each route before mutating it and re-marks it after, so a snapshot taken at
    construction time would go stale mid-pass.

    Instances only exist when a voltage map is active *and* the table it
    installed actually widens some pair -- :meth:`from_router` returns ``None``
    otherwise (the dormancy contract every pairwise consumer follows), so
    callers guard with a single ``is None`` check and the scalar-only path
    stays byte-identical.

    This is the one resolver for router-derived pairwise context: the #4766
    copper-moving post-passes build their route-level gates from these same
    four fields (:attr:`table`, :attr:`id_to_name`, :attr:`attach_zones` and
    :meth:`foreign_routes`), so no pass can drift into a different verdict
    from the path-level predicate or from the #4588 audit.
    """

    table: PairwiseClearanceTable
    foreign_routes: Callable[[], Iterable[Route]]
    id_to_name: Mapping[int, str] | None = None
    attach_zones: Sequence[AttachZone] = ()
    dru: float | None = None
    tolerance: float = _PASS_TOLERANCE

    @classmethod
    def from_router(cls, router: object) -> PairwisePathChecker | None:
        """Build the checker from a live ``Autorouter``; ``None`` when dormant.

        Reads the same authorities the acceptance gate reads: the table from
        ``router.rules.pairwise_clearance``, foreign copper live from
        ``router.grid.routes`` (the same foreign-copper source the pathfinder's
        ``Router._validate_route_clearance`` gate walks), names from
        ``router.net_names`` (plus the pad-derived fallback
        ``Autorouter._net_name_to_id`` adds for routers assembled without
        ``add_component``), and the #4506 zones from the CLI resolver's
        memoised ``_pairwise_attach_zones_cache`` hand-off (#4602 precedent).

        Returns ``None`` when no voltage map installed a table, **or** when the
        installed table widens nothing (``required_by_pair`` empty) -- either
        way the caller's signal to skip pairwise consultation entirely.  A
        table that asks for no widening can only ever answer "clear", so
        arming a scan for it would be pure cost (#4766).

        A router **without a grid** still yields a checker: ``foreign_routes``
        then falls back to ``router.routes``.  The copper-moving post-passes
        (#4766) are driven in unit tests by stub routers that carry routes but
        no grid, and the pathfinder-facing consumers pass a real router where
        the two are the same live list anyway.
        """
        rules = getattr(router, "rules", None)
        table = getattr(rules, "pairwise_clearance", None) if rules is not None else None
        if table is None or not getattr(table, "required_by_pair", None):
            return None

        id_to_name: dict[int, str] = {
            int(net_id): name
            for net_id, name in (getattr(router, "net_names", None) or {}).items()
            if name
        }
        name_to_id_fn = getattr(router, "_net_name_to_id", None)
        if callable(name_to_id_fn):
            # Sorted so a name collision on one id resolves deterministically
            # (#4766): an unsorted walk lets dict order pick the winner.
            for name, net_id in sorted(name_to_id_fn().items()):
                id_to_name.setdefault(int(net_id), name)

        zones = getattr(router, "_pairwise_attach_zones_cache", None) or ()
        grid = getattr(router, "grid", None)
        source: object = router if grid is None else grid
        return cls(
            table=table,
            foreign_routes=lambda: getattr(source, "routes", None) or (),
            id_to_name=id_to_name or None,
            attach_zones=tuple(zones),
        )

    def path_violation(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: Layer,
        width: float,
        exclude_net: int,
    ) -> PairwiseViolation | None:
        """First pairwise shortfall for the candidate path, or ``None``."""
        return path_pairwise_violation(
            x1,
            y1,
            x2,
            y2,
            layer,
            width,
            exclude_net,
            self.foreign_routes(),
            self.table,
            id_to_name=self.id_to_name,
            dru=self.dru,
            attach_zones=self.attach_zones,
            tolerance=self.tolerance,
        )

    def path_is_clear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: Layer,
        width: float,
        exclude_net: int,
    ) -> bool:
        """``CollisionChecker``-shaped verdict: True when no pair falls short."""
        return self.path_violation(x1, y1, x2, y2, layer, width, exclude_net) is None


def find_pairwise_violations(
    routes: Iterable[Route],
    table: PairwiseClearanceTable,
    *,
    id_to_name: Mapping[int, str] | None = None,
    dru: float | None = None,
    attach_zones: Sequence[AttachZone] = (),
    tolerance: float = _PASS_TOLERANCE,
    foreign_pads: Sequence[PadGeometry] = (),
) -> list[PairwiseViolation]:
    """Scan a whole routed board for pairwise-clearance shortfalls (board-level).

    Every unordered pair of distinct-net routes is checked trace-vs-trace,
    trace-vs-via and via-vs-via (issue #4507 widened this past the original
    trace-vs-trace-only scan -- ``Route.vias`` was already carried by every
    caller, just never walked).  When ``foreign_pads`` is supplied (issue
    #4507; only :func:`board_pairwise_violations` populates it today, from
    :func:`board_pad_geometry`), every route's trace and via copper is ALSO
    checked against every foreign pad, once per route -- a pad is static
    board copper, not owned by any :class:`Route`, so it cannot join the
    route-pair loop the way a via can.  Returns a deterministically-ordered
    list of every :class:`PairwiseViolation` found (empty when the board
    satisfies all pairwise requirements).  Intended for a post-route board
    audit / tests; the in-loop validators use :func:`route_pairwise_violation`
    for early-exit.

    ``attach_zones`` (issue #4588) carries the #4506 rated-footprint necking
    regions through to every check here, exactly as
    :func:`route_pairwise_violation` already does.  Omitting them would make
    this scan report every deliberately-exempted domain-bridging footprint
    (tap resistors, TO-220 packages, optocouplers) as a violation -- a false
    *fail* that renders every HV board unroutable by construction.
    """
    materialised = list(routes)
    floor = table.dru if dru is None else dru
    out: list[PairwiseViolation] = []
    for i in range(len(materialised)):
        ra = materialised[i]
        a_name = _resolve_net_name(id_to_name, ra.net, ra.net_name)
        for j in range(i + 1, len(materialised)):
            rb = materialised[j]
            if ra.net == rb.net:
                continue
            b_name = _resolve_net_name(id_to_name, rb.net, rb.net_name)
            required = table.required_clearance(a_name, b_name)
            if required <= floor + tolerance:
                continue
            for seg in ra.segments:
                for oseg in rb.segments:
                    violation = segment_pair_violation(
                        seg,
                        oseg,
                        table,
                        net_a_name=a_name,
                        net_b_name=b_name,
                        dru=floor,
                        attach_zones=attach_zones,
                        tolerance=tolerance,
                    )
                    if violation is not None:
                        out.append(violation)
                for ovia in rb.vias:
                    violation = _moving_segment_vs_foreign_via(
                        seg,
                        a_name,
                        ovia,
                        b_name,
                        table,
                        floor=floor,
                        attach_zones=attach_zones,
                        tolerance=tolerance,
                    )
                    if violation is not None:
                        out.append(violation)
            for via in ra.vias:
                for oseg in rb.segments:
                    violation = _moving_via_vs_foreign_segment(
                        via,
                        a_name,
                        oseg,
                        b_name,
                        table,
                        floor=floor,
                        attach_zones=attach_zones,
                        tolerance=tolerance,
                    )
                    if violation is not None:
                        out.append(violation)
                for ovia in rb.vias:
                    violation = _via_vs_via_violation(
                        via,
                        a_name,
                        ovia,
                        b_name,
                        table,
                        floor=floor,
                        attach_zones=attach_zones,
                        tolerance=tolerance,
                    )
                    if violation is not None:
                        out.append(violation)

    if foreign_pads:
        for route in materialised:
            route_name = _resolve_net_name(id_to_name, route.net, route.net_name)
            for pad in foreign_pads:
                required = table.required_clearance(route_name, pad.net_name)
                if required <= floor + tolerance:
                    continue
                for seg in route.segments:
                    seg_poly = _segment_copper_polygon(seg)
                    if seg_poly is None:
                        continue
                    violation = _copper_vs_pad_violation(
                        seg_poly,
                        route_name,
                        seg.layer,
                        pad,
                        table,
                        floor=floor,
                        attach_zones=attach_zones,
                        tolerance=tolerance,
                    )
                    if violation is not None:
                        out.append(violation)
                for via in route.vias:
                    violation = _copper_vs_pad_violation(
                        _via_copper_polygon(via),
                        route_name,
                        None,
                        pad,
                        table,
                        floor=floor,
                        attach_zones=attach_zones,
                        tolerance=tolerance,
                    )
                    if violation is not None:
                        out.append(violation)
    return out


def board_attach_zones(board_path: str | Path) -> tuple[AttachZone, ...]:
    """#4506 attach zones for a board FILE, in that file's own coordinate frame.

    ``build_attach_zones`` works on already-loaded :class:`Footprint` objects and
    therefore inherits whatever frame the caller loaded them in.  ``PCB.load``
    detects the ``Edge.Cuts`` origin and reports footprint positions
    **board-relative** (``schema/pcb.py::_detect_board_origin``), while every
    piece of copper *written into the file* -- and every
    :class:`~kicad_tools.router.primitives.Segment` the router works with --
    is **sheet-absolute**.  Zones built straight off ``PCB.load(...).footprints``
    are therefore offset by ``board_origin`` from the copper they are meant to
    waive, on every board that does not sit at sheet origin (0, 0), i.e. on
    every real board.

    Getting that shift wrong is silently bidirectional and has cost real time
    twice (#4588's gate fixture pins it for the CLI path; the #4507 T4 proof run
    mis-attributed two "genuine router leaks" to a mis-framed ad-hoc replay).
    This helper is the single answer for anyone reading copper out of a board
    file: it returns zones that line up with ``(segment ...)`` / ``(via ...)``
    coordinates as they appear in that same file.

    Args:
        board_path: Path to a ``.kicad_pcb`` file.

    Returns:
        Attach zones shifted into the board file's sheet-absolute frame.
    """
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(board_path))
    ox, oy = pcb.board_origin
    return shift_attach_zones(build_attach_zones(pcb.footprints), ox, oy)


def shift_attach_zones(
    zones: Iterable[AttachZone], offset_x: float, offset_y: float
) -> tuple[AttachZone, ...]:
    """Translate attach zones by ``(offset_x, offset_y)`` (#4506 frame plumbing).

    Board-relative -> sheet-absolute is the only shift the router needs, and it
    is applied in exactly two places (the CLI's memoised resolver and
    :func:`board_attach_zones`); both go through this one implementation so the
    two cannot drift apart.
    """
    from dataclasses import replace as dataclass_replace

    return tuple(
        dataclass_replace(
            zone,
            min_x=zone.min_x + offset_x,
            min_y=zone.min_y + offset_y,
            max_x=zone.max_x + offset_x,
            max_y=zone.max_y + offset_y,
        )
        for zone in zones
    )


def board_trace_routes(board_path: str | Path) -> list[Route]:
    """Every ``(segment ...)``/``(via ...)`` in a board file, one route per net.

    Zone fills are still excluded (pour geometry is the census' remit, #3901 --
    unchanged since this function's introduction).  Vias were originally
    excluded too, when the pairwise gate was trace-vs-trace only; issue #4507
    widened the gate to also walk via copper, and both parsers already read
    the SAME sheet-absolute frame (:func:`~kicad_tools.router.optimizer.pcb.
    parse_segments` / ``parse_vias`` work on the raw file text directly,
    unlike ``PCB.load``'s board-relative footprint view -- see
    :func:`board_attach_zones`), so folding vias in here is a frame-safe
    widening, not a new frame to get wrong.

    Net ids are re-assigned densely (1..N, in net-name order over the UNION of
    net names carrying a segment or a via) because a name-based board resolves
    unknown names to id 0, and :func:`find_pairwise_violations` skips
    equal-id route pairs -- which would silently drop every comparison on
    such a board.
    """
    from kicad_tools.router.optimizer.pcb import parse_segments, parse_vias
    from kicad_tools.router.primitives import Route

    text = Path(board_path).read_text()
    segments_by_net = parse_segments(text)
    vias_by_net = parse_vias(text)
    net_names = sorted(set(segments_by_net) | set(vias_by_net))
    return [
        Route(
            net=index,
            net_name=net_name,
            segments=list(segments_by_net.get(net_name, ())),
            vias=list(vias_by_net.get(net_name, ())),
        )
        for index, net_name in enumerate(net_names, start=1)
    ]


def board_pad_geometry(board_path: str | Path) -> tuple[PadGeometry, ...]:
    """Every connected pad's true copper polygon, sheet-absolute (issue #4507).

    ``PCB.load`` reports footprint (and therefore pad) positions
    **board-relative** (see :func:`board_attach_zones`'s docstring for the
    full frame explanation); this shifts every pad polygon by the same
    ``board_origin`` :func:`board_attach_zones` already applies, so it lines
    up with :func:`board_trace_routes`' segments/vias in the same file's own
    sheet-absolute frame.

    Unconnected (net-less) pads are skipped -- they cannot participate in a
    net-pair requirement.  A degenerate (non-positive-size) pad is skipped by
    :func:`~kicad_tools.validate.rules.clearance._pad_polygon` itself.
    """
    from shapely.affinity import translate  # type: ignore[import-untyped]

    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.clearance import _pad_polygon

    pcb = PCB.load(str(board_path))
    ox, oy = pcb.board_origin
    out: list[PadGeometry] = []
    for footprint in pcb.footprints:
        for pad in footprint.pads:
            if not pad.net_name:
                continue
            polygon = _pad_polygon(pad, footprint)
            if polygon is None:
                continue
            out.append(
                PadGeometry(
                    net_name=pad.net_name,
                    layers=_pad_copper_layers(pad),
                    polygon=translate(polygon, ox, oy),
                )
            )
    return tuple(out)


def board_pairwise_violations(
    board_path: str | Path,
    table: PairwiseClearanceTable,
    *,
    dru: float | None = None,
    attach_zones: Sequence[AttachZone] | None = None,
    tolerance: float = _PASS_TOLERANCE,
    foreign_pads: Sequence[PadGeometry] | None = None,
) -> list[PairwiseViolation]:
    """Replay the router's own pairwise gate over a finished board FILE (#4507).

    The board-level equivalent of :func:`find_pairwise_violations` for copper
    that is no longer in memory: it reads the traces, vias, pads and the #4506
    attach zones out of the *same* file, in the *same* frame, and runs the
    identical checks the in-run audit uses.  This is how a routed board is
    scored against the gate after the fact (the #4507 T4 softstart proof), and
    having one supported implementation is what keeps that scoring from being
    re-derived -- with a fresh frame bug each time -- as a throwaway script.

    Args:
        board_path: Path to a routed ``.kicad_pcb`` file.
        table: The pairwise requirement table the run was scored with.
        dru: Scalar clearance floor (mm); defaults to ``table.dru``.
        attach_zones: Rated-footprint exemption regions.  ``None`` (the default)
            resolves them from ``board_path`` via :func:`board_attach_zones`;
            pass ``()`` to score the board with the #4506 exemption disabled
            (the census' view, which has no concept of it).
        tolerance: Sub-micron pass tolerance, as elsewhere in this module.
        foreign_pads: Pad copper to widen against.  ``None`` (the default)
            resolves them from ``board_path`` via :func:`board_pad_geometry`;
            pass ``()`` to score trace/via copper only (the pre-#4507 scope).

    Returns:
        Every trace/via pairwise shortfall on the board (trace-vs-trace,
        trace-vs-via, via-vs-via, and -- unless disabled -- trace/via-vs-pad),
        deterministically ordered.
    """
    zones = board_attach_zones(board_path) if attach_zones is None else attach_zones
    pads = board_pad_geometry(board_path) if foreign_pads is None else foreign_pads
    return find_pairwise_violations(
        board_trace_routes(board_path),
        table,
        dru=dru,
        attach_zones=zones,
        tolerance=tolerance,
        foreign_pads=pads,
    )


def normalize_net_key(name: str) -> str:
    """Public form of the pairwise net-name normaliser (#4766).

    Consumers outside this module (the DRC nudge's revert gate) need to key
    routes the same way :func:`violation_pair_key` keys violations; exporting
    the normaliser keeps them from reaching across a module boundary for the
    private :func:`_norm_net_key`.
    """
    return _norm_net_key(name)


def violation_pair_key(violation: PairwiseViolation) -> tuple[str, str]:
    """Order-independent, ``/``-stripped net-pair key of a violation (#4766)."""
    a = _norm_net_key(violation.net_a)
    b = _norm_net_key(violation.net_b)
    return (a, b) if a <= b else (b, a)


def violation_pair_keys(violations: Iterable[PairwiseViolation]) -> set[tuple[str, str]]:
    """Set of :func:`violation_pair_key` values for a violation list (#4766).

    The post-pass gates compare a before/after scan by *net pair*, not by
    coordinate: a pre-existing (inherited) shortfall that merely moves must not
    be mistaken for one the pass introduced, or the pass would start
    "repairing" copper the #4588 audit is supposed to keep reporting.
    """
    return {violation_pair_key(v) for v in violations}


def _resolve_net_name(id_to_name: Mapping[int, str] | None, net_id: int, fallback: str) -> str:
    """Resolve a net id to its board net name, falling back to a known string."""
    if id_to_name is not None:
        name = id_to_name.get(net_id)
        if name:
            return name
    return fallback
