"""Creepage / clearance census engine (Issue #4327, phase 1 MVP).

Clearance vs creepage
---------------------

* **Clearance** is the shortest straight-line, through-air gap between two
  conductors -- exactly what ``kct check``'s clearance rule already measures.
* **Creepage** is the shortest path *along the board surface* between two
  conductors.  A milled slot/cutout in ``Edge.Cuts`` lying between the two
  conductors **lengthens** that path (the surface route must detour around
  the slot), so ``creepage >= clearance``.  IEC 60664-1 / 62368-1 govern
  creepage for HV, so the two values are reported distinctly.

This module reuses the existing shapely copper primitives rather than
reinventing them:

* trace segments -> :func:`kicad_tools.geometry.copper.segment_copper_polygon`
* pads           -> :func:`kicad_tools.validate.rules.clearance._pad_polygon`
  (true roundrect/oval outline in board coordinates)
* vias           -> a circular disc of the via's copper radius
* zone fills     -> :meth:`ConnectivityValidator._fill_solid_region`

and derives slot/cutout obstacles from the ``Edge.Cuts`` outline
(:meth:`PCB.get_board_outline_segments` + :meth:`PCB._edge_cuts_poly_chains_sexp`).

The MVP surface-path model is an honest approximation: if the straight
nearest-points segment between two conductors does NOT cross an interior
Edge.Cuts cutout, ``creepage == clearance``; if it DOES, a visibility graph
over the intervening slot polygons' vertices yields the shortest detour
around them.
"""

from __future__ import annotations

import heapq
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kicad_tools._shapely import has_shapely, require_shapely

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.creepage.standards import CreepageStandard
    from kicad_tools.router.rules import NetClassRouting
    from kicad_tools.schema.pcb import PCB


# Sentinel used as the "net B" name for HV-vs-board-edge pairs.
BOARD_EDGE_LABEL = "<board edge>"

# Geometry epsilons (mm).  Well below any manufacturing precision but above
# IEEE-754 noise for the coordinate space we operate in.
_EPS = 1e-9
_INTERIOR_SHRINK = 1e-6  # shrink a slot before the "crosses interior" test
# A pair is a PASS when creepage >= min within this tolerance (mm).  Mirrors
# the spirit of DRC_TOLERANCE -- a sub-micron shortfall is not a real defect.
_PASS_TOLERANCE = 1e-4

# Per-net voltage model (#4371): two conductors whose mapped potentials differ
# by less than this (volts) are treated as the SAME node -> required creepage
# 0.0 (trivial PASS), short-circuiting the standard-table lookup.  The IEC
# creepage tables start at 50 V and ``_step_up_index`` raises for ``V <= 0``, so
# a same-potential pair (``dv == 0``) must never reach the lookup.
_ZERO_DV_EPS = 1e-6


def _norm_net_key(name: str) -> str:
    """Normalise a net name for voltage-map lookup (drop one leading ``/``).

    KiCad emits hierarchical net names with a leading ``/`` (``/AC_LINE``);
    hand-authored voltage maps may or may not include it.  Stripping a single
    leading slash on both sides makes the lookup robust to either convention.
    """
    return name[1:] if name.startswith("/") else name


# Reserved voltage-map key (#4371): the board-edge / earth reference potential.
_EDGE_VOLTAGE_KEY = "_edge_voltage"


def voltage_map_from_dict(data: Any) -> tuple[dict[str, float], float]:
    """Parse a per-net voltage map sidecar (#4371).

    ``data`` maps net names to their **RMS working potential relative to a
    common reference** (volts), e.g.
    ``{"/AC_LINE": 150, "/AC_NEUTRAL": 0, "/SCAP_POS": 90}``.  Keys starting
    with ``_`` are reserved for in-band metadata and are NOT treated as nets
    (mirrors :func:`kicad_tools.router.rules.net_class_map_from_dict`).  The one
    recognised reserved key is ``_edge_voltage`` -- the board-edge / earth
    reference potential (default ``0.0`` V).

    Returns ``(voltages, edge_voltage)``.

    Raises:
        TypeError: if ``data`` is not a dict.
        ValueError: if any net voltage (or ``_edge_voltage``) is not a finite
            real number.
    """
    if not isinstance(data, dict):
        raise TypeError(f"voltage_map_from_dict expects a dict, got {type(data).__name__}")
    voltages: dict[str, float] = {}
    edge_voltage = 0.0
    for key, value in data.items():
        if isinstance(key, str) and key.startswith("_"):
            if key == _EDGE_VOLTAGE_KEY:
                edge_voltage = _coerce_voltage(key, value)
            continue  # other _-prefixed keys are documentation (_comment, ...)
        voltages[str(key)] = _coerce_voltage(key, value)
    return voltages, edge_voltage


def _coerce_voltage(key: Any, value: Any) -> float:
    """Coerce a voltage-map value to a finite float or raise ``ValueError``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"voltage-map entry for {key!r} must be a number (volts), got {type(value).__name__}"
        )
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"voltage-map entry for {key!r} must be finite, got {value!r}")
    return v


# IEC 60664-1 SELV boundary: below ~50 V RMS a design is not a mains/HV
# safety concern.  A working-voltage argument at or above this threshold is a
# strong signal that the operator IS analysing a mains/HV insulation path, so
# a census that resolves ZERO HV nets at such a voltage is a vacuity red flag
# (issue #4354) rather than an inert "nothing to audit" -- the same contract as
# the LVS zero-bound-pad guard (#4011).
SELV_WORKING_VOLTAGE_V = 50.0

# Strong mains/HV net-name signals (case-insensitive, whole-token boundaries so
# substrings like ONLINE / REMAINS / GND do NOT trip).  Used both to broaden the
# HV name-pattern fallback (the ``NetClass`` enum deliberately has no HV member,
# so :func:`classify_from_name` can never return ``"HV"``) and to power the
# vacuity guard (issue #4354).  Net names frequently carry a leading ``/``
# (hierarchical sheet path), so ``/`` is an accepted token boundary.
MAINS_NAME_RE = re.compile(
    r"(?:^|[_/])"
    r"(?:"
    r"AC[_-]?LINE|AC[_-]?NEUT(?:RAL)?|L[_-]?LINE|N[_-]?LINE|"
    r"LIVE|NEUTRAL|MAINS|FUSED(?:_[A-Z0-9]+)?|HV[_A-Z0-9]*"
    r")"
    r"(?:$|[_/])",
    re.IGNORECASE,
)


def is_mains_suspect_name(name: str | None) -> bool:
    """True when ``name`` carries a strong mains/HV signal (see MAINS_NAME_RE)."""
    return bool(name) and MAINS_NAME_RE.search(name) is not None  # type: ignore[arg-type]


def mains_suspect_nets(pcb: PCB) -> list[str]:
    """Sorted board net names that strongly imply a mains/HV conductor.

    Powers the issue #4354 vacuity guard: when HV-net *resolution* returns
    empty but the board clearly carries mains-named copper, the creepage census
    must NOT silently pass -- the operator most likely just needs a
    ``--net-class-map`` (or ``--net-class``) that actually names the HV group.
    """
    return sorted(
        n.name
        for n in pcb.nets.values()
        if n.number != 0 and n.name and MAINS_NAME_RE.search(n.name)
    )


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class CreepagePair:
    """One evaluated (HV-net, other-conductor | board-edge) census row.

    ``clearance_mm`` is the straight-line copper gap; ``creepage_mm`` is the
    slot-aware surface path (``>= clearance_mm``).

    Threshold sources (phase 2, #4332):

    * ``min_mm`` -- the operator's manual override (``--min``), or ``None``.
    * ``required_creepage_mm`` -- creepage derived from an IEC standard table,
      or ``None`` when no ``--standard`` was supplied.
    * ``required_clearance_mm`` -- clearance derived from the standard, or
      ``None`` (phase-1 mode never thresholds clearance).

    When both a manual ``min_mm`` and a derived ``required_creepage_mm`` are
    present, the **stricter (larger)** governs (see :attr:`governing_creepage_mm`
    and :attr:`governing_bound`).  ``provenance`` carries the structured
    standard citation for the derived requirements (empty in phase-1 mode).
    """

    net_a: str
    net_b: str
    kind: str  # "conductor" | "edge"
    layer: str  # copper layer of the binding measurement, or "*" for edges
    clearance_mm: float
    creepage_mm: float
    min_mm: float | None = None
    required_creepage_mm: float | None = None
    required_clearance_mm: float | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def governing_creepage_mm(self) -> float:
        """The effective required creepage: the stricter of manual / derived.

        At least one of ``min_mm`` / ``required_creepage_mm`` is always set
        (validated by the CLI before the census is built).
        """
        candidates = [v for v in (self.min_mm, self.required_creepage_mm) if v is not None]
        if not candidates:
            # Defensive: no threshold supplied -> nothing to clear.
            return 0.0
        return max(candidates)

    @property
    def governing_bound(self) -> str:
        """Which threshold governs the creepage pass/fail decision."""
        has_min = self.min_mm is not None
        has_derived = self.required_creepage_mm is not None
        if has_min and has_derived:
            # Tie or derived-larger -> derived governs (conservative default).
            if self.required_creepage_mm >= self.min_mm:  # type: ignore[operator]
                return "derived"
            return "manual (--min)"
        if has_derived:
            return "derived"
        return "manual (--min)"

    @property
    def margin_mm(self) -> float:
        """Creepage headroom over the governing requirement (negative == fail)."""
        return self.creepage_mm - self.governing_creepage_mm

    @property
    def clearance_margin_mm(self) -> float | None:
        """Clearance headroom over the derived requirement, or ``None``."""
        if self.required_clearance_mm is None:
            return None
        return self.clearance_mm - self.required_clearance_mm

    @property
    def creepage_passed(self) -> bool:
        """``True`` when the surface path clears the governing creepage bound."""
        return self.creepage_mm >= self.governing_creepage_mm - _PASS_TOLERANCE

    @property
    def clearance_passed(self) -> bool:
        """``True`` when through-air clearance clears its derived requirement.

        Vacuously ``True`` in phase-1 mode (no clearance requirement).
        """
        if self.required_clearance_mm is None:
            return True
        return self.clearance_mm >= self.required_clearance_mm - _PASS_TOLERANCE

    @property
    def passed(self) -> bool:
        """``True`` only when BOTH creepage and clearance clear their bounds."""
        return self.creepage_passed and self.clearance_passed

    def to_dict(self) -> dict[str, Any]:
        # Phase-1 backward compatibility: with no derived requirement (manual
        # --min only) the JSON schema is byte-for-byte identical to phase 1.
        base = {
            "net_a": self.net_a,
            "net_b": self.net_b,
            "kind": self.kind,
            "layer": self.layer,
            "clearance_mm": round(self.clearance_mm, 4),
            "creepage_mm": round(self.creepage_mm, 4),
            "margin_mm": round(self.margin_mm, 4),
            "pass": self.passed,
        }
        if self.required_creepage_mm is None:
            return base
        # Phase-2 (standard) mode: attach the derived requirements + provenance.
        base["required_creepage_mm"] = round(self.required_creepage_mm, 4)
        base["governing_bound"] = self.governing_bound
        if self.min_mm is not None:
            base["min_mm"] = self.min_mm
        if self.required_clearance_mm is not None:
            base["required_clearance_mm"] = round(self.required_clearance_mm, 4)
            cm = self.clearance_margin_mm
            base["clearance_margin_mm"] = round(cm, 4) if cm is not None else None
            base["clearance_pass"] = self.clearance_passed
        base["provenance"] = self.provenance
        return base


@dataclass
class CreepageReport:
    """Full census of HV creepage/clearance pairs for a board.

    In phase-1 mode (manual ``--min`` only) ``standard`` is ``None`` and the
    serialized schema is byte-for-byte identical to phase 1.  In phase-2 mode a
    ``standard`` context (id/edition/PD/material group + derived-requirement
    provenance) is attached.
    """

    net_class: str
    min_mm: float | None
    hv_nets: list[str] = field(default_factory=list)
    pairs: list[CreepagePair] = field(default_factory=list)
    board: str = ""
    # Phase-2 (#4332) standard context -- None in phase-1 (manual --min) mode.
    standard: str | None = None
    standard_edition: str | None = None
    working_voltage: float | None = None
    pollution_degree: int | None = None
    material_group: str | None = None
    required_creepage_mm: float | None = None
    required_clearance_mm: float | None = None
    creepage_provenance: dict[str, Any] = field(default_factory=dict)
    clearance_provenance: dict[str, Any] = field(default_factory=dict)
    # Per-net voltage model (#4371).  None in single-voltage / phase-1 modes;
    # a ``{net_name: volts}`` map when the requirement is derived per pair from
    # ``|ΔV|`` instead of one global working voltage.  When set, the report-level
    # ``required_creepage_mm`` / ``working_voltage`` are ``None`` (per-pair).
    voltage_map: dict[str, float] | None = None
    edge_voltage: float = 0.0

    @property
    def passed(self) -> bool:
        """``True`` when every pair clears its bounds (vacuously true if empty)."""
        return all(p.passed for p in self.pairs)

    @property
    def has_hv_nets(self) -> bool:
        return bool(self.hv_nets)

    @property
    def uses_standard(self) -> bool:
        return self.standard is not None

    @property
    def uses_voltage_map(self) -> bool:
        """``True`` when the requirement is derived per pair from ``|ΔV|``."""
        return self.voltage_map is not None

    def to_dict(self) -> dict[str, Any]:
        # Phase-1 backward compatibility: no standard -> exact phase-1 schema.
        if self.standard is None:
            return {
                "board": self.board,
                "net_class": self.net_class,
                "min_mm": self.min_mm,
                "hv_nets": list(self.hv_nets),
                "pair_count": len(self.pairs),
                "pairs": [p.to_dict() for p in self.pairs],
                "passed": self.passed,
            }
        d: dict[str, Any] = {
            "board": self.board,
            "net_class": self.net_class,
            "min_mm": self.min_mm,
            "standard": self.standard,
            "standard_edition": self.standard_edition,
            "working_voltage_v": self.working_voltage,
            "pollution_degree": self.pollution_degree,
            "material_group": self.material_group,
            "required_creepage_mm": (
                round(self.required_creepage_mm, 4)
                if self.required_creepage_mm is not None
                else None
            ),
            "required_clearance_mm": (
                round(self.required_clearance_mm, 4)
                if self.required_clearance_mm is not None
                else None
            ),
            "creepage_provenance": self.creepage_provenance,
            "clearance_provenance": self.clearance_provenance,
            "hv_nets": list(self.hv_nets),
            "pair_count": len(self.pairs),
            "pairs": [p.to_dict() for p in self.pairs],
            "passed": self.passed,
        }
        # Per-net voltage mode (#4371): the requirement varies per pair, so the
        # report-level scalar requirement / working voltage are null (already set
        # to None by the caller) and we echo the voltage source instead.  These
        # keys are added ONLY in map mode, so single-voltage output is unchanged.
        if self.voltage_map is not None:
            d["voltage_source"] = "per-pair |dV| (voltage-map)"
            d["voltage_map"] = dict(self.voltage_map)
            d["edge_voltage_v"] = self.edge_voltage
        return d


# ---------------------------------------------------------------------------
# HV net selection (reuses existing net-class plumbing -- no new classifier)
# ---------------------------------------------------------------------------


def resolve_hv_nets(
    pcb: PCB,
    net_class: str,
    net_class_map: dict[str, NetClassRouting] | None = None,
    *,
    voltage_map: dict[str, float] | None = None,
    edge_voltage: float = 0.0,
    census_threshold: float | None = None,
) -> dict[int, str]:
    """Return ``{net_number: net_name}`` for nets belonging to ``net_class``.

    Selection order (no new classification mechanism is introduced):

    1. **Explicit map** -- when ``net_class_map`` is supplied (parsed by
       :func:`kicad_tools.router.rules.net_class_map_from_dict`), a net whose
       name maps to a :class:`NetClassRouting` whose ``name`` matches
       ``net_class`` (case-insensitive) is selected.
    2. **Name-pattern fallback** -- for any net NOT resolved by the map, the
       existing :func:`kicad_tools.router.net_class.classify_from_name` is
       consulted and its :class:`NetClass` value compared to ``net_class``
       (case-insensitive).  This lets ``--net-class power`` work without a map.
    3. **Mains/HV name fallback** -- when ``net_class`` is ``HV`` (the default),
       the :class:`NetClass` enum has no HV member, so ``classify_from_name``
       can never return ``"HV"`` and step 2 is unreachable for the HV group
       (issue #4354).  Any unmapped net whose name carries a strong mains/HV
       signal (:data:`MAINS_NAME_RE` -- ``AC_LINE``, ``AC_NEUTRAL``,
       ``FUSED_LINE``, ``*MAINS*``, ``HV*``, ``LIVE``,
       ``NEUTRAL`` ...) is therefore selected here.  An explicit map entry
       always wins (step 1), so operator-supplied classification is never
       overridden by this fallback.
    4. **Voltage-derived union** (issue #4401) -- when both ``voltage_map`` and
       ``census_threshold`` are supplied, every net whose mapped potential
       differs from ``edge_voltage`` by at least ``census_threshold`` volts is
       added, **in union** with the class/name selection above.  This closes the
       false-pass where a high-|V| net carrying a non-HV routing class (e.g. a
       ``±150 V`` gate-drive net classed ``Digital``) was silently excluded from
       the census.  Keys are normalised with :func:`_norm_net_key`, matching the
       census's own leading-``/`` convention.  The union never *removes* a
       class-selected net, so a class-``HV`` net at low/unmapped voltage is
       still audited.

    ``voltage_map``/``edge_voltage``/``census_threshold`` all default to the
    no-op path: with no map (or ``census_threshold=None``) the output is
    byte-identical to the class/name selection alone.
    """
    from kicad_tools.router.net_class import classify_from_name

    target = net_class.strip().lower()
    net_class_map = net_class_map or {}

    selected: dict[int, str] = {}
    for net in pcb.nets.values():
        if net.number == 0 or not net.name:
            continue
        routing = net_class_map.get(net.name)
        if routing is not None:
            if (routing.name or "").strip().lower() == target:
                selected[net.number] = net.name
            continue
        # Name-pattern fallback for nets not covered by the map.
        classification = classify_from_name(net.name)
        if classification is not None and classification.value.strip().lower() == target:
            selected[net.number] = net.name
            continue
        # Broadened mains/HV fallback for the HV group (issue #4354): there is
        # no NetClass.HV, so classify_from_name never yields "hv" above.
        if target == "hv" and MAINS_NAME_RE.search(net.name):
            selected[net.number] = net.name

    # Voltage-derived union (issue #4401): pull in any mapped net whose
    # potential differs from the board-edge reference by >= the threshold,
    # regardless of its routing class.  Union, not replace.
    if voltage_map is not None and census_threshold is not None:
        norm_vmap = {_norm_net_key(k): float(v) for k, v in voltage_map.items()}
        for net in pcb.nets.values():
            if net.number == 0 or not net.name:
                continue
            v = norm_vmap.get(_norm_net_key(net.name))
            if v is not None and abs(v - edge_voltage) >= census_threshold:
                selected[net.number] = net.name

    return selected


# ---------------------------------------------------------------------------
# Copper geometry (reuses the existing shapely primitives)
# ---------------------------------------------------------------------------


def _net_geoms_on_layer(pcb: PCB, layer_name: str) -> dict[int, list[Any]]:
    """Collect per-net copper shapely geometries on a single copper layer.

    Reuses ``segment_copper_polygon`` (traces), ``_pad_polygon`` (pads, true
    outline), a buffered point (vias), and ``_fill_solid_region`` (zone fills).
    """
    from shapely.geometry import Point  # type: ignore[import-untyped]

    from kicad_tools.core.layers import via_spans_layer
    from kicad_tools.geometry.copper import segment_copper_polygon
    from kicad_tools.validate.connectivity import ConnectivityValidator
    from kicad_tools.validate.rules.clearance import _pad_polygon

    geoms: dict[int, list[Any]] = {}

    def _add(net_number: int, geom: Any | None) -> None:
        if geom is None or getattr(geom, "is_empty", False):
            return
        geoms.setdefault(net_number, []).append(geom)

    # Trace segments
    for seg in pcb.segments_on_layer(layer_name):
        _add(seg.net_number, segment_copper_polygon(seg.start, seg.end, seg.width))

    # Pads (true roundrect/oval outline)
    for fp in pcb.footprints:
        for pad in fp.pads:
            if layer_name in pad.layers or "*.Cu" in pad.layers:
                _add(pad.net_number, _pad_polygon(pad, fp))

    # Vias (circular copper barrel on every spanned layer)
    for via in pcb.vias:
        if via_spans_layer(via.layers, layer_name):
            radius = max(getattr(via, "size", 0.0) or 0.0, 0.0) / 2.0
            if radius > 0:
                _add(via.net_number, Point(via.position).buffer(radius))

    # Zone fills (resolved to their net; hole-aware solid region)
    name_to_number = {net.name: net.number for net in pcb.nets.values() if net.name}
    for zone in pcb.zones:
        net_number = zone.net_number
        if net_number == 0 and zone.net_name:
            net_number = name_to_number.get(zone.net_name, 0)
        if net_number == 0:
            continue
        for i, pts in enumerate(zone.filled_polygons):
            if zone.filled_polygon_layer(i) != layer_name:
                continue
            _add(net_number, ConnectivityValidator._fill_solid_region(pts))

    return geoms


def _net_union_on_layer(pcb: PCB, layer_name: str) -> dict[int, Any]:
    """Union each net's copper geometries on ``layer_name`` into one shape."""
    from shapely.ops import unary_union  # type: ignore[import-untyped]

    return {
        net_number: unary_union(parts)
        for net_number, parts in _net_geoms_on_layer(pcb, layer_name).items()
        if parts
    }


# ---------------------------------------------------------------------------
# Edge.Cuts slot / board-edge geometry
# ---------------------------------------------------------------------------


def _edge_line_segments(pcb: PCB) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """All Edge.Cuts segments (outer boundary + interior slots/cutouts).

    Combines the parsed ``gr_line``/``gr_arc``/``gr_rect`` segments
    (:meth:`PCB.get_board_outline_segments`) with any ``gr_poly``/``gr_curve``
    vertex chains (:meth:`PCB._edge_cuts_poly_chains_sexp`, closed into
    segments and shifted into the board frame).
    """
    segments = list(pcb.get_board_outline_segments())

    chains = pcb._edge_cuts_poly_chains_sexp()
    if chains:
        ox, oy = pcb._board_origin
        for chain in chains:
            if len(chain) < 2:
                continue
            pts = [(x - ox, y - oy) for x, y in chain]
            # Close the ring so a polygon can be recovered from it.
            if pts[0] != pts[-1]:
                pts.append(pts[0])
            for a, b in zip(pts, pts[1:], strict=False):
                segments.append((a, b))
    return segments


def board_slot_obstacles(pcb: PCB) -> list[Any]:
    """Return shapely polygons for interior Edge.Cuts slots / cutouts.

    The Edge.Cuts linework is polygonized; the largest-area face is the board
    body, and every interior ring (hole) of that face is a milled void that
    can lengthen a surface path.  Standalone interior faces (a slot drawn as
    its own closed loop) are also returned.  Returns ``[]`` when shapely is
    unavailable or no interior geometry exists.
    """
    if not has_shapely():
        return []
    from shapely.geometry import LineString, Polygon
    from shapely.ops import polygonize, unary_union

    raw_segments = _edge_line_segments(pcb)
    lines = [LineString([a, b]) for a, b in raw_segments if math.dist(a, b) > _EPS]
    if not lines:
        return []

    faces = list(polygonize(unary_union(lines)))
    if not faces:
        return []

    # Largest face is the board body; its interior rings are the cutouts.
    board = max(faces, key=lambda f: f.area)
    obstacles: list[Any] = []
    for ring in board.interiors:
        poly = Polygon(ring)
        if poly.area > _EPS:
            obstacles.append(poly)

    # A slot drawn as an independent closed loop polygonizes to its own small
    # face that is spatially inside the board body -- include those too.
    for face in faces:
        if face is board:
            continue
        if board.contains(face.representative_point()):
            obstacles.append(face)

    return obstacles


def board_edge_geometry(pcb: PCB) -> Any | None:
    """A shapely geometry of all Edge.Cuts linework, for edge-distance."""
    if not has_shapely():
        return None
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    lines = [LineString([a, b]) for a, b in _edge_line_segments(pcb) if math.dist(a, b) > _EPS]
    if not lines:
        return None
    return unary_union(lines)


# ---------------------------------------------------------------------------
# Core surface-path (creepage) computation
# ---------------------------------------------------------------------------


def _crosses_any_obstacle(line: Any, obstacles: list[Any]) -> bool:
    """True when ``line`` passes through the interior of any obstacle."""
    for obs in obstacles:
        interior = obs.buffer(-_INTERIOR_SHRINK)
        if interior.is_empty:
            continue
        crossing = line.intersection(interior)
        if not crossing.is_empty and getattr(crossing, "length", 0.0) > _EPS:
            return True
    return False


def _shortest_detour(
    pa: tuple[float, float], pb: tuple[float, float], obstacles: list[Any]
) -> float:
    """Shortest visibility-graph path from ``pa`` to ``pb`` around obstacles.

    Nodes are the two endpoints plus every obstacle-polygon exterior vertex.
    An edge between two nodes is admissible when the connecting segment does
    not pass through the interior of any obstacle (segments that merely run
    along a slot boundary are allowed -- that is the surface path hugging the
    milled edge).  Returns the Euclidean length of the shortest admissible
    path, or the straight-line distance if no path is found (defensive).
    """
    from shapely.geometry import LineString

    nodes: list[tuple[float, float]] = [pa, pb]
    for obs in obstacles:
        for x, y in list(obs.exterior.coords)[:-1]:
            nodes.append((x, y))

    n = len(nodes)

    def _admissible(i: int, j: int) -> bool:
        seg = LineString([nodes[i], nodes[j]])
        return not _crosses_any_obstacle(seg, obstacles)

    # Dense O(n^2) adjacency -- n is tiny (a handful of slot corners).
    adj: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if _admissible(i, j):
                w = math.dist(nodes[i], nodes[j])
                adj[i].append((j, w))
                adj[j].append((i, w))

    # Dijkstra from node 0 (pa) to node 1 (pb).
    dist = [math.inf] * n
    dist[0] = 0.0
    pq: list[tuple[float, int]] = [(0.0, 0)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist[u]:
            continue
        if u == 1:
            return d
        for v, w in adj[u]:
            nd = d + w
            if nd < dist[v]:
                dist[v] = nd
                heapq.heappush(pq, (nd, v))

    if dist[1] != math.inf:
        return dist[1]
    return math.dist(pa, pb)


def surface_path_length(
    geom_a: Any,
    geom_b: Any,
    obstacles: list[Any] | None = None,
) -> tuple[float, float]:
    """Return ``(clearance, creepage)`` between two shapely geometries.

    ``clearance`` is the straight-line ``geom_a.distance(geom_b)``.
    ``creepage`` equals ``clearance`` when the straight nearest-points segment
    does not cross an interior Edge.Cuts cutout; otherwise it is the shortest
    path routing around the intervening slot polygon(s) (``> clearance``).

    Overlapping / touching geometries (``clearance <= 0``) have no meaningful
    surface detour, so ``creepage == clearance`` there too.
    """
    require_shapely("creepage surface-path geometry")
    from shapely.geometry import LineString
    from shapely.ops import nearest_points

    obstacles = obstacles or []
    clearance = geom_a.distance(geom_b)
    if clearance <= 0.0 or not obstacles:
        return clearance, clearance

    pa_pt, pb_pt = nearest_points(geom_a, geom_b)
    pa = (pa_pt.x, pa_pt.y)
    pb = (pb_pt.x, pb_pt.y)
    straight = LineString([pa, pb])
    if not _crosses_any_obstacle(straight, obstacles):
        return clearance, clearance

    creepage = _shortest_detour(pa, pb, obstacles)
    # The detour can never be shorter than the straight clearance.
    return clearance, max(creepage, clearance)


# ---------------------------------------------------------------------------
# Census assembly
# ---------------------------------------------------------------------------


def compute_creepage_census(
    pcb: PCB,
    hv_nets: dict[int, str],
    min_mm: float | None = None,
    net_class: str = "HV",
    board: str = "",
    *,
    required_creepage_mm: float | None = None,
    required_clearance_mm: float | None = None,
    standard: str | None = None,
    standard_edition: str | None = None,
    working_voltage: float | None = None,
    pollution_degree: int | None = None,
    material_group: str | None = None,
    creepage_provenance: dict[str, Any] | None = None,
    clearance_provenance: dict[str, Any] | None = None,
    voltage_map: dict[str, float] | None = None,
    standard_obj: CreepageStandard | None = None,
    edge_voltage: float = 0.0,
) -> CreepageReport:
    """Build the full HV creepage/clearance census for a board.

    For every HV net the census records one row per non-HV conductor (the
    binding, smallest-creepage layer) and one row for the board edge.

    The pass/fail threshold comes from either the operator's ``min_mm``
    (phase 1) or a standard-derived ``required_creepage_mm`` /
    ``required_clearance_mm`` (phase 2, #4332), or both -- in which case the
    stricter creepage bound governs per pair.

    Per-net voltage model (#4371)
    -----------------------------
    When ``voltage_map`` (``{net_name: volts}``) **and** ``standard_obj`` are
    supplied, the requirement is derived **per pair** from the pairwise voltage
    difference ``dv = |V(a) - V(b)|`` (unmapped nets default to ``0 V``; the
    board edge uses ``edge_voltage``, default ``0 V`` == earth), instead of one
    global working voltage.  Same-potential pairs (``dv <= _ZERO_DV_EPS``) get a
    required creepage/clearance of ``0.0`` and are a trivial PASS -- this
    short-circuits the standard-table lookup (which starts at 50 V and raises
    for ``V <= 0``).  In map mode the HV-vs-HV pairing skip is relaxed so that
    same-class nets at different potentials (bank-vs-bank, phase-vs-phase) are
    also evaluated.  Voltages are treated as worst-case DC-equivalent magnitudes
    about a common reference; AC phase relationships are NOT modelled, so
    ``|dv|`` is conservative for in-phase nets.

    In single-voltage mode (no ``voltage_map``) the derived requirement is
    identical for every pair (it depends only on the standard + voltage + PD +
    material group, not on geometry), so it is stamped onto each row -- byte-for
    -byte unchanged from phase 2.
    """
    require_shapely("creepage census")

    # Per-net voltage mode (#4371) requires BOTH a map and a resolved standard
    # table (the table is the only source of a derived requirement).
    map_mode = voltage_map is not None and standard_obj is not None

    report = CreepageReport(
        net_class=net_class,
        min_mm=min_mm,
        hv_nets=[hv_nets[num] for num in sorted(hv_nets)],
        board=board,
        standard=standard,
        standard_edition=standard_edition,
        working_voltage=working_voltage,
        pollution_degree=pollution_degree,
        material_group=material_group,
        required_creepage_mm=required_creepage_mm,
        required_clearance_mm=required_clearance_mm,
        creepage_provenance=creepage_provenance or {},
        clearance_provenance=clearance_provenance or {},
        voltage_map=dict(voltage_map) if (map_mode and voltage_map is not None) else None,
        edge_voltage=edge_voltage,
    )

    # Merged per-pair provenance (creepage + clearance citations together).
    pair_provenance: dict[str, Any] = {}
    if creepage_provenance:
        pair_provenance["creepage"] = creepage_provenance
    if clearance_provenance:
        pair_provenance["clearance"] = clearance_provenance

    def _make_pair(
        *,
        req_creep: float | None = required_creepage_mm,
        req_clear: float | None = required_clearance_mm,
        prov: dict[str, Any] | None = None,
        **kw: Any,
    ) -> CreepagePair:
        return CreepagePair(
            min_mm=min_mm,
            required_creepage_mm=req_creep,
            required_clearance_mm=req_clear,
            provenance=pair_provenance if prov is None else prov,
            **kw,
        )

    # --- Per-pair |dV| requirement machinery (map mode only, #4371) ---------
    norm_vmap: dict[str, float] = {}
    if map_mode:
        assert voltage_map is not None
        norm_vmap = {_norm_net_key(k): float(v) for k, v in voltage_map.items()}
    _req_cache: dict[float, tuple[float, float, dict[str, Any], dict[str, Any]]] = {}

    def _voltage(name: str) -> float:
        return norm_vmap.get(_norm_net_key(name), 0.0)

    def _required_for_dv(
        dv: float,
    ) -> tuple[float, float, dict[str, Any], dict[str, Any]]:
        """Derive ``(req_creepage, req_clearance, creep_prov, clear_prov)`` at ``dv``.

        Memoised by ``dv`` (rounded) so the ~1500-pair census performs at most
        one table lookup per distinct voltage difference.  ``dv <= _ZERO_DV_EPS``
        short-circuits to a trivial ``0.0`` requirement (no lookup).  An
        out-of-range ``dv`` propagates ``StandardLookupError`` (fail loud).
        """
        from kicad_tools.creepage.standards import RMS_TO_PEAK

        key = round(dv, 6)
        cached = _req_cache.get(key)
        if cached is not None:
            return cached
        if dv <= _ZERO_DV_EPS:
            res: tuple[float, float, dict[str, Any], dict[str, Any]] = (0.0, 0.0, {}, {})
        else:
            assert standard_obj is not None and pollution_degree is not None
            creep, cprov = standard_obj.required_creepage(
                dv, int(pollution_degree), material_group or "IIIa"
            )
            clr, clprov = standard_obj.required_clearance(dv * RMS_TO_PEAK, int(pollution_degree))
            res = (creep, clr, cprov, clprov)
        _req_cache[key] = res
        return res

    def _pair_requirement(
        name_a: str, name_b: str | None, *, edge: bool = False
    ) -> tuple[float, float, dict[str, Any]]:
        """Per-pair ``(req_creepage, req_clearance, provenance)`` from ``|dV|``."""
        va = _voltage(name_a)
        vb = edge_voltage if edge else _voltage(name_b or "")
        dv = abs(va - vb)
        req_creep, req_clear, cprov, clprov = _required_for_dv(dv)
        prov: dict[str, Any] = {
            "voltage": {
                "net_a_v": va,
                "net_b_v": vb,
                "delta_v_v": round(dv, 4),
                "same_potential": dv <= _ZERO_DV_EPS,
            }
        }
        if cprov:
            prov["creepage"] = cprov
        if clprov:
            prov["clearance"] = clprov
        return req_creep, req_clear, prov

    if not hv_nets:
        return report

    number_to_name = {net.number: net.name for net in pcb.nets.values()}
    obstacles = board_slot_obstacles(pcb)

    # Per-layer per-net copper unions.
    layer_unions: dict[str, dict[int, Any]] = {}
    for layer in pcb.copper_layers:
        layer_unions[layer.name] = _net_union_on_layer(pcb, layer.name)

    # --- HV-vs-other-conductor pairs (binding layer = smallest creepage) ---
    # (hv_number, other_number) -> (clearance, creepage, layer)
    #
    # In single-voltage mode HV-vs-HV pairs are skipped (an HV net has no
    # meaningful requirement against another net in its own group).  In per-net
    # voltage mode (#4371) that skip is relaxed so same-class nets at different
    # potentials (bank-vs-bank, phase-vs-phase) ARE evaluated; such pairs are
    # deduplicated to a single canonical direction (smaller net number first).
    best: dict[tuple[int, int], tuple[float, float, str]] = {}
    for layer_name, unions in layer_unions.items():
        for hv_num in hv_nets:
            hv_geom = unions.get(hv_num)
            if hv_geom is None:
                continue
            for other_num, other_geom in unions.items():
                if other_num == 0 or other_num == hv_num:
                    continue
                if other_num in hv_nets:
                    if not map_mode:
                        continue
                    # Canonical dedup: evaluate each HV-HV pair once.
                    if other_num < hv_num:
                        continue
                clearance, creepage = surface_path_length(hv_geom, other_geom, obstacles)
                key = (hv_num, other_num)
                prev = best.get(key)
                if prev is None or creepage < prev[1]:
                    best[key] = (clearance, creepage, layer_name)

    for (hv_num, other_num), (clearance, creepage, layer_name) in best.items():
        net_a_name = hv_nets[hv_num]
        net_b_name = number_to_name.get(other_num, f"net{other_num}")
        if map_mode:
            req_creep, req_clear, prov = _pair_requirement(net_a_name, net_b_name)
            report.pairs.append(
                _make_pair(
                    req_creep=req_creep,
                    req_clear=req_clear,
                    prov=prov,
                    net_a=net_a_name,
                    net_b=net_b_name,
                    kind="conductor",
                    layer=layer_name,
                    clearance_mm=clearance,
                    creepage_mm=creepage,
                )
            )
        else:
            report.pairs.append(
                _make_pair(
                    net_a=net_a_name,
                    net_b=net_b_name,
                    kind="conductor",
                    layer=layer_name,
                    clearance_mm=clearance,
                    creepage_mm=creepage,
                )
            )

    # --- HV-vs-board-edge pairs (copper union across all layers) ---
    edge_geom = board_edge_geometry(pcb)
    if edge_geom is not None and not edge_geom.is_empty:
        from shapely.ops import unary_union

        for hv_num in hv_nets:
            parts = [unions[hv_num] for unions in layer_unions.values() if hv_num in unions]
            if not parts:
                continue
            hv_all = unary_union(parts)
            clearance, creepage = surface_path_length(hv_all, edge_geom, obstacles)
            if map_mode:
                req_creep, req_clear, prov = _pair_requirement(hv_nets[hv_num], None, edge=True)
                report.pairs.append(
                    _make_pair(
                        req_creep=req_creep,
                        req_clear=req_clear,
                        prov=prov,
                        net_a=hv_nets[hv_num],
                        net_b=BOARD_EDGE_LABEL,
                        kind="edge",
                        layer="*",
                        clearance_mm=clearance,
                        creepage_mm=creepage,
                    )
                )
            else:
                report.pairs.append(
                    _make_pair(
                        net_a=hv_nets[hv_num],
                        net_b=BOARD_EDGE_LABEL,
                        kind="edge",
                        layer="*",
                        clearance_mm=clearance,
                        creepage_mm=creepage,
                    )
                )

    # Deterministic ordering: by net A, then edge last, then net B.
    report.pairs.sort(key=lambda p: (p.net_a, p.kind == "edge", p.net_b))
    return report
