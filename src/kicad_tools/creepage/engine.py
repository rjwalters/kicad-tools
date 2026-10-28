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
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NamedTuple

from kicad_tools._shapely import has_shapely, require_shapely
from kicad_tools.creepage.standards import (
    DEFAULT_DIMENSION_X_MM,
    DEFAULT_INSULATION,
    dimension_x_mm,
)

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
_BRIDGE_QUAD_SEGS = 16  # arc resolution of the dimension-X opening (round joins)
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

# Range-object keys for a swinging-node interval entry (#4411).
_RANGE_MIN_KEY = "min"
_RANGE_MAX_KEY = "max"


class VoltageInterval(NamedTuple):
    """A net's mapped potential as a closed interval ``[lo, hi]`` (volts) (#4411).

    A single static value ``v`` cannot represent a switching node that *swings*
    over a range (a common-source net that rides ``-170..+90 V`` every mains
    cycle in normal operation).  Each mapped net therefore becomes a closed
    interval:

    * a scalar ``v`` -> the degenerate interval ``(v, v)`` (byte-identical to the
      pre-#4411 scalar model);
    * a ``{"min": v0, "max": v1}`` object -> ``(min(v0, v1), max(v0, v1))``.

    Endpoints are worst-case DC-equivalent magnitudes about a common reference
    (see the census docstring); AC phase relationships are NOT modelled.
    """

    lo: float
    hi: float

    @property
    def is_degenerate(self) -> bool:
        """``True`` when ``lo == hi`` (equivalent to a scalar voltage)."""
        return self.lo == self.hi


def _as_interval(value: Any) -> VoltageInterval:
    """Normalise a voltage-map value to a :class:`VoltageInterval`.

    Accepts an already-parsed interval (pass-through) or a bare scalar (a
    degenerate ``(v, v)`` interval).  This lets the census / HV-union consumers
    be called with either the interval-typed map from
    :func:`voltage_map_from_dict` or a plain ``{net: volts}`` scalar map without
    a separate parse step -- a scalar is exactly its degenerate interval.
    """
    if isinstance(value, VoltageInterval):
        return value
    return VoltageInterval(float(value), float(value))


def voltage_map_from_dict(data: Any) -> tuple[dict[str, VoltageInterval], float]:
    """Parse a per-net voltage map sidecar (#4371, ranges added #4411).

    ``data`` maps net names to their **working potential relative to a common
    reference** (volts).  Each entry is EITHER a scalar (a single static
    potential) OR a ``{"min": v0, "max": v1}`` object describing a swinging
    node's excursion, e.g.
    ``{"/AC_LINE": 150, "/AC_NEUTRAL": 0, "/SRC_NEG": {"min": -170, "max": 90}}``.
    Every net becomes a closed :class:`VoltageInterval`; a scalar is the
    degenerate interval ``(v, v)`` and a range is normalised to
    ``(min, max)`` (author order is irrelevant).

    Keys starting with ``_`` are reserved for in-band metadata and are NOT
    treated as nets (mirrors
    :func:`kicad_tools.router.rules.net_class_map_from_dict`).  The one
    recognised reserved key is ``_edge_voltage`` -- the board-edge / earth
    reference potential (default ``0.0`` V).  ``_edge_voltage`` stays a *scalar*
    (earth is a fixed reference, not a swing); a range object there is rejected.

    Returns ``(voltages, edge_voltage)``.

    Raises:
        TypeError: if ``data`` is not a dict.
        ValueError: if any net voltage endpoint (or ``_edge_voltage``) is not a
            finite real number, or a range object is malformed.
    """
    if not isinstance(data, dict):
        raise TypeError(f"voltage_map_from_dict expects a dict, got {type(data).__name__}")
    voltages: dict[str, VoltageInterval] = {}
    edge_voltage = 0.0
    for key, value in data.items():
        if isinstance(key, str) and key.startswith("_"):
            if key == _EDGE_VOLTAGE_KEY:
                edge_voltage = _coerce_voltage(key, value)  # scalar-only reference
            continue  # other _-prefixed keys are documentation (_comment, ...)
        voltages[str(key)] = _coerce_interval(key, value)
    return voltages, edge_voltage


def _coerce_interval(key: Any, value: Any) -> VoltageInterval:
    """Coerce a voltage-map value to a :class:`VoltageInterval` or raise.

    A scalar becomes the degenerate interval ``(v, v)``; a
    ``{"min": v0, "max": v1}`` object is normalised to ``(min, max)``.  Both
    endpoints go through the existing :func:`_coerce_voltage` finite/real/no-bool
    checks.  A malformed range object (missing a key, carrying extra keys, or a
    non-numeric endpoint) raises ``ValueError`` in the same style as the scalar
    message.
    """
    if isinstance(value, dict):
        if set(value) != {_RANGE_MIN_KEY, _RANGE_MAX_KEY}:
            raise ValueError(
                f"voltage-map range entry for {key!r} must be an object with exactly "
                f"'min' and 'max' keys, got keys {sorted(value)!r}"
            )
        lo = _coerce_voltage(f"{key}.min", value[_RANGE_MIN_KEY])
        hi = _coerce_voltage(f"{key}.max", value[_RANGE_MAX_KEY])
        return VoltageInterval(min(lo, hi), max(lo, hi))
    v = _coerce_voltage(key, value)
    return VoltageInterval(v, v)


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
    # Footprint relationship of the binding measurement (#4403).  ``"board"``
    # is a layout-fixable net-to-net (or net-to-edge) approach; ``"same_footprint"``
    # means the binding gap is between two pads of a SINGLE footprint (component
    # pin pitch), which the board layout cannot change -- per IEC 60664-1 that is
    # functional insulation governed by the component's own rating, not the
    # board's creepage tables.
    relationship: str = "board"  # "board" | "same_footprint" | "attach_zone"
    # Set True only when the operator passes ``--waive-same-footprint`` AND this
    # pair is ``relationship == "same_footprint"``.  A waived pair is still listed
    # (annotated WAIVED) but is excluded from :attr:`CreepageReport.gate_passed`.
    # It NEVER affects the raw :attr:`CreepageReport.passed` (the safety gate).
    waived: bool = False
    # Attach-zone residual (#8028).  Only set when ``relationship ==
    # "attach_zone"``: the pair's minimum measured with the copper inside the
    # router's #4506 attach zone(s) clipped away, across every layer the zone
    # covers (a waived witness must never mask a failing one elsewhere on the
    # same pair, softstart#88).  ``residual_creepage_mm is None`` means no
    # copper of one net remains outside the zone, i.e. nothing left to fail.
    residual_clearance_mm: float | None = None
    residual_creepage_mm: float | None = None
    residual_layer: str | None = None
    residual_witness_mm: tuple[float, float] | None = None

    @property
    def residual_passed(self) -> bool:
        """``True`` when the copper OUTSIDE the attach zone still clears its bounds.

        Vacuously ``True`` when no copper remains outside the zone.  Only
        meaningful for ``relationship == "attach_zone"`` rows.
        """
        if self.residual_creepage_mm is None:
            return True
        if self.residual_creepage_mm < self.governing_creepage_mm - _PASS_TOLERANCE:
            return False
        if self.required_clearance_mm is not None and self.residual_clearance_mm is not None:
            return self.residual_clearance_mm >= self.required_clearance_mm - _PASS_TOLERANCE
        return True

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
        # --min only) the JSON schema matched phase 1 byte-for-byte.  The
        # additive ``relationship`` key (#4403) intentionally changes that
        # schema in BOTH phase-1 and phase-2 rows -- the serialization
        # drift-guard tests were updated to include it.  ``waived`` is emitted
        # only on the rows the operator actually waived.
        base = {
            "net_a": self.net_a,
            "net_b": self.net_b,
            "kind": self.kind,
            "layer": self.layer,
            "clearance_mm": round(self.clearance_mm, 4),
            "creepage_mm": round(self.creepage_mm, 4),
            "margin_mm": round(self.margin_mm, 4),
            "relationship": self.relationship,
            "pass": self.passed,
        }
        if self.waived:
            base["waived"] = True
        if self.relationship == "attach_zone":
            base["residual_pass"] = self.residual_passed
            if self.residual_creepage_mm is not None:
                base["residual_creepage_mm"] = round(self.residual_creepage_mm, 4)
            if self.residual_clearance_mm is not None:
                base["residual_clearance_mm"] = round(self.residual_clearance_mm, 4)
            if self.residual_layer is not None:
                base["residual_layer"] = self.residual_layer
            if self.residual_witness_mm is not None:
                base["residual_witness_mm"] = [round(v, 4) for v in self.residual_witness_mm]
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
    serialized schema is the phase-1 schema (plus the additive report-level
    verdict keys ``gate_passed`` / ``waived_count``, #4687).  In phase-2 mode a
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
    # Dimension X (mm) used to bridge narrow cutouts (#7058).
    dimension_x_mm: float | None = None
    required_creepage_mm: float | None = None
    required_clearance_mm: float | None = None
    creepage_provenance: dict[str, Any] = field(default_factory=dict)
    clearance_provenance: dict[str, Any] = field(default_factory=dict)
    # Per-net voltage model (#4371).  None in single-voltage / phase-1 modes;
    # a ``{net_name: volts}`` map when the requirement is derived per pair from
    # ``|ΔV|`` instead of one global working voltage.  When set, the report-level
    # ``required_creepage_mm`` / ``working_voltage`` are ``None`` (per-pair).
    voltage_map: dict[str, VoltageInterval] | None = None
    edge_voltage: float = 0.0
    # Insulation type the derived requirement assumed (issue #7082): "basic",
    # "supplementary" or "reinforced".  None in phase-1 (manual --min) mode.
    insulation: str | None = None

    @property
    def passed(self) -> bool:
        """``True`` when every pair clears its bounds (vacuously true if empty).

        This is the RAW, un-waived result.  The manufacturing-readiness gate
        (``kct audit`` -> :class:`IsolationStatus`) keys off THIS property, so a
        same-footprint waiver must NEVER flow into it -- an unqualified
        component-internal shortfall still blocks manufacturing by default
        (#4403 safety guard).  Only :attr:`gate_passed` honors waivers, and only
        the standalone ``kct creepage`` CLI opts into it.
        """
        return all(p.passed for p in self.pairs)

    @property
    def gate_passed(self) -> bool:
        """``True`` when every NON-waived pair clears its bounds (#4403).

        Identical to :attr:`passed` unless the operator waived same-footprint
        pairs via ``kct creepage --waive-same-footprint``.  Waived pairs remain
        listed (annotated WAIVED) but drop out of this exit-code gate so the
        actionable board-level defects are no longer buried under
        component-rating residuals.
        """
        return all(p.passed for p in self.pairs if not p.waived)

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
        # Verdict semantics (#4687): ``passed`` is the RAW, waiver-blind result
        # (what ``kct audit`` keys off); ``gate_passed`` is the waiver-aware
        # verdict the standalone CLI exit code follows.  Both are always
        # serialized so JSON consumers can gate unconditionally on
        # ``gate_passed`` -- without waivers the two are identical.
        waived_count = sum(1 for p in self.pairs if p.waived)
        # Phase-1 backward compatibility: no standard -> phase-1 schema (plus
        # the additive verdict keys above, #4687).
        if self.standard is None:
            return {
                "board": self.board,
                "net_class": self.net_class,
                "min_mm": self.min_mm,
                "hv_nets": list(self.hv_nets),
                "pair_count": len(self.pairs),
                "pairs": [p.to_dict() for p in self.pairs],
                "passed": self.passed,
                "gate_passed": self.gate_passed,
                "waived_count": waived_count,
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
            "dimension_x_mm": self.dimension_x_mm,
            "insulation": self.insulation,
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
            "gate_passed": self.gate_passed,
            "waived_count": waived_count,
        }
        # Per-net voltage mode (#4371): the requirement varies per pair, so the
        # report-level scalar requirement / working voltage are null (already set
        # to None by the caller) and we echo the voltage source instead.  These
        # keys are added ONLY in map mode, so single-voltage output is unchanged.
        if self.voltage_map is not None:
            d["voltage_source"] = "per-pair |dV| (voltage-map)"
            # Backward-compat (#4411): echo a degenerate interval (lo == hi) as a
            # bare scalar so an all-scalar map serialises byte-identically to the
            # pre-range schema; a genuine swing surfaces as ``{"min", "max"}``.
            d["voltage_map"] = {
                name: (iv.lo if iv.is_degenerate else {"min": iv.lo, "max": iv.hi})
                for name, iv in self.voltage_map.items()
            }
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
    voltage_map: dict[str, VoltageInterval] | None = None,
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
       ``census_threshold`` are supplied, every net whose worst-case magnitude
       relative to ``edge_voltage`` -- ``max(|lo - edge|, |hi - edge|)`` over its
       mapped interval (#4411) -- is at least ``census_threshold`` volts is
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
        norm_vmap = {_norm_net_key(k): _as_interval(v) for k, v in voltage_map.items()}
        for net in pcb.nets.values():
            if net.number == 0 or not net.name:
                continue
            iv = norm_vmap.get(_norm_net_key(net.name))
            if iv is not None:
                # Worst-case magnitude relative to the edge (#4411): a net that
                # swings across the threshold in EITHER extreme is pulled in.
                worst = max(abs(iv.lo - edge_voltage), abs(iv.hi - edge_voltage))
                if worst >= census_threshold:
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
# Footprint-membership index for same-footprint classification (#4403)
# ---------------------------------------------------------------------------


class _FootprintPadIndex:
    """Per-layer footprint -> net -> pad-polygon index (#4403).

    The census unions each net's copper per layer (:func:`_net_union_on_layer`)
    before pairing, which erases which *pad* -- and therefore which *footprint*
    -- produced a binding measurement.  This index recovers that identity so a
    component-internal pad gap (two pads of one package) can be told apart from
    a board-fixable net-to-net approach.

    It attributes the *binding* measurement: a binding pair ``(net_a, net_b)``
    with binding straight-line ``clearance`` on ``layer`` is ``same_footprint``
    iff a **single footprint** holds pads on BOTH nets whose minimum
    pad-to-pad distance on that layer equals the binding ``clearance`` within a
    geometric epsilon.  The equality check matters: two nets can share a
    footprint *and* also approach at a board-level site elsewhere that is the
    true binding minimum -- when the intra-footprint gap is larger than the
    binding clearance, something else (a trace, another package) bound it, so
    the pair is correctly ``board``.

    Reuses the same pad geometry (:func:`_pad_polygon`) and layer-membership
    test as :func:`_net_geoms_on_layer`, and ``fp.reference`` as the footprint
    identity -- the same refdes source the hole-to-hole same-footprint downgrade
    uses (``validate/rules/dimensions.py``).
    """

    def __init__(self, pcb: PCB) -> None:
        from kicad_tools.validate.rules.clearance import _pad_polygon

        # layer -> net_number -> set(fp_ref) that has a pad on that net/layer.
        self._by_net: dict[str, dict[int, set[str]]] = {}
        # layer -> (fp_ref, net_number) -> [pad polygons].
        self._pads: dict[str, dict[tuple[str, int], list[Any]]] = {}

        copper_layer_names = [layer.name for layer in pcb.copper_layers]
        for fp in pcb.footprints:
            ref = fp.reference
            if not ref:
                continue
            for pad in fp.pads:
                poly = _pad_polygon(pad, fp)
                if poly is None or getattr(poly, "is_empty", False):
                    continue
                for layer_name in copper_layer_names:
                    if layer_name in pad.layers or "*.Cu" in pad.layers:
                        self._by_net.setdefault(layer_name, {}).setdefault(
                            pad.net_number, set()
                        ).add(ref)
                        self._pads.setdefault(layer_name, {}).setdefault(
                            (ref, pad.net_number), []
                        ).append(poly)

    def relationship(self, layer_name: str, net_a: int, net_b: int, clearance_mm: float) -> str:
        """Classify a binding pair as ``"same_footprint"`` or ``"board"``."""
        by_net = self._by_net.get(layer_name)
        pads = self._pads.get(layer_name)
        if not by_net or pads is None:
            return "board"
        shared = by_net.get(net_a, set()) & by_net.get(net_b, set())
        for ref in shared:
            a_polys = pads.get((ref, net_a), [])
            b_polys = pads.get((ref, net_b), [])
            if not a_polys or not b_polys:
                continue
            min_dist = min(pa.distance(pb) for pa in a_polys for pb in b_polys)
            if abs(min_dist - clearance_mm) <= _PASS_TOLERANCE:
                return "same_footprint"
        return "board"


def _attach_zone_residual(
    layer_unions: dict[str, dict[int, Any]],
    zones: Sequence[Any],
    net_a: int,
    net_b: int,
    name_a: str,
    name_b: str,
    obstacles: list[Any],
) -> tuple[float | None, float | None, str | None, tuple[float, float] | None]:
    """Residual ``(clearance, creepage, layer, witness)`` outside the attach zones (#8028).

    On every layer the zones cover for BOTH nets, the zone bbox is clipped out
    of each net's copper and the pair is re-measured; the minimum over layers
    is returned.  ``creepage``/``clearance`` are ``None`` when no copper of one
    net remains outside the zone on any layer.
    """
    from shapely.geometry import box
    from shapely.ops import nearest_points, unary_union

    best: tuple[float, float, str, tuple[float, float]] | None = None
    min_clearance: float | None = None
    for layer_name, unions in layer_unions.items():
        geom_a = unions.get(net_a)
        geom_b = unions.get(net_b)
        if geom_a is None or geom_b is None:
            continue
        boxes = [
            box(z.min_x, z.min_y, z.max_x, z.max_y)
            for z in zones
            if z.covers_layer(name_a, layer_name)
            and z.covers_layer(name_b, layer_name)
            and _norm_net_key(name_a) in z.net_names
            and _norm_net_key(name_b) in z.net_names
        ]
        if boxes:
            clip = unary_union(boxes)
            geom_a = geom_a.difference(clip)
            geom_b = geom_b.difference(clip)
        if geom_a.is_empty or geom_b.is_empty:
            continue
        clearance, creepage = surface_path_length(geom_a, geom_b, obstacles, _bridged=True)
        if min_clearance is None or clearance < min_clearance:
            min_clearance = clearance
        if best is None or creepage < best[1]:
            pa, pb = nearest_points(geom_a, geom_b)
            best = (
                clearance,
                creepage,
                layer_name,
                ((pa.x + pb.x) / 2.0, (pa.y + pb.y) / 2.0),
            )
    if best is None:
        return None, None, None, None
    return min_clearance, best[1], best[2], best[3]


def _witness_in_attach_zone(
    geom_a: Any, geom_b: Any, zones: Sequence[Any], name_a: str, name_b: str, layer_name: str
) -> bool:
    """True when BOTH nearest points of the binding gap lie in one exempting zone."""
    from shapely.ops import nearest_points

    pa, pb = nearest_points(geom_a, geom_b)
    return any(
        z.exempts(pa.x, pa.y, name_a, name_b, layer_name)
        and z.exempts(pb.x, pb.y, name_a, name_b, layer_name)
        for z in zones
    )


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


def _flatten_polygons(geoms: list[Any]) -> list[Any]:
    """Explode multi-part geometries into their non-empty ``Polygon`` parts."""
    out: list[Any] = []
    for g in geoms:
        if g is None or g.is_empty:
            continue
        if hasattr(g, "geoms"):
            out.extend(_flatten_polygons(list(g.geoms)))
        elif g.geom_type == "Polygon":
            out.append(g)
    return out


def _bridge_narrow_cutouts(obstacles: list[Any], dimension_x_mm: float) -> list[Any]:
    """Drop cutout parts narrower than ``dimension_x_mm`` (IEC 60664-1 dim. X).

    A groove narrower than X is not followed by the creepage path; it is
    measured straight across.  The morphological opening by a disc of diameter
    X (round-joined erosion then dilation by X/2) keeps only the material in
    which an X-diameter disc fits, so every narrow part -- stubs, tapers,
    wedges, necks -- is dropped and the creepage path crosses it.  The small
    ``eps`` keeps a slot exactly X wide counted as a groove (not bridged).

    Round joins are deliberate: a mitre join would regrow sharp corners from
    the eroded vertices and keep material where no X-disc fits, overstating
    creepage.  The cost is that slot corners sharper than radius X/2 are
    rounded, which can only shorten the measured path (understating creepage
    is the conservative direction).  Clipping back to the source obstacle keeps
    the opening anti-extensive, so it can only remove parts, never grow a slot.
    """
    if dimension_x_mm <= 0.0 or not obstacles:
        return list(obstacles)

    r = dimension_x_mm / 2.0 - 1e-4
    opened = [
        obs.buffer(-r, quad_segs=_BRIDGE_QUAD_SEGS)
        .buffer(r, quad_segs=_BRIDGE_QUAD_SEGS)
        .intersection(obs)
        for obs in obstacles
    ]
    return _flatten_polygons(opened)


def _boundary_segments(geom: Any) -> Any:
    """Every boundary segment of ``geom`` as an ``(S, 2, 2)`` float array.

    Walks multi-part geometries and collections; a polygon contributes its
    exterior and hole rings, a line its own segments, and a point one
    zero-length segment (so it still acts as a candidate endpoint).
    """
    import numpy as np

    rings: list[Any] = []

    def _walk(g: Any) -> None:
        if g is None or g.is_empty:
            return
        gt = g.geom_type
        if gt == "Polygon":
            rings.append(np.asarray(g.exterior.coords, dtype=float)[:, :2])
            for hole in g.interiors:
                rings.append(np.asarray(hole.coords, dtype=float)[:, :2])
        elif gt in ("LineString", "LinearRing"):
            rings.append(np.asarray(g.coords, dtype=float)[:, :2])
        elif gt == "Point":
            pt = np.asarray(g.coords, dtype=float)[:, :2]
            rings.append(np.vstack([pt, pt]))
        elif hasattr(g, "geoms"):
            for part in g.geoms:
                _walk(part)

    _walk(geom)
    segs = [np.stack([r[:-1], r[1:]], axis=1) for r in rings if len(r) >= 2]
    if not segs:
        return np.zeros((0, 2, 2), dtype=float)
    return np.concatenate(segs, axis=0)


def _feet_on_segments(points: Any, segs: Any) -> Any:
    """Closest point on each segment for each point: ``(P, S, 2)`` array."""
    import numpy as np

    a = segs[None, :, 0, :]
    ab = segs[None, :, 1, :] - a
    ap = points[:, None, :] - a
    len2 = (ab * ab).sum(-1)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(len2 > 0.0, (ap * ab).sum(-1) / np.where(len2 > 0.0, len2, 1.0), 0.0)
    t = np.clip(t, 0.0, 1.0)
    return a + t[..., None] * ab


def _obstacle_interiors(obstacles: list[Any]) -> list[Any]:
    """Slightly shrunk obstacle interiors used by the admissibility test."""
    out = []
    for obs in obstacles:
        interior = obs.buffer(-_INTERIOR_SHRINK)
        if not interior.is_empty:
            out.append(interior)
    return out


def _first_admissible(
    lengths: Any, starts: Any, ends: Any, interiors: list[Any], bound: float
) -> float:
    """Shortest candidate segment (``< bound``) not crossing any obstacle interior.

    Candidates are tested in ascending length order, in vectorised batches, so
    the typical case (the shortest candidate is visible) costs one batch.
    Returns ``inf`` when no candidate under ``bound`` is admissible.
    """
    import numpy as np
    import shapely  # type: ignore[import-untyped]

    keep = np.nonzero(lengths < bound)[0]
    if keep.size == 0:
        return math.inf
    order = keep[np.argsort(lengths[keep], kind="stable")]
    if not interiors:
        return float(lengths[order[0]])
    for interior in interiors:
        shapely.prepare(interior)  # in-place and idempotent; speeds the intersects
    # Small first batch keeps the common "shortest candidate is visible" case
    # cheap; the batch then grows so a fully occluded pair (slot past both
    # copper ends) stays linear in the candidate count.  The interiors are
    # already shrunk by _INTERIOR_SHRINK, so a line running along a slot
    # boundary does not intersect them and a plain predicate is enough.
    lo, batch = 0, 64
    while lo < order.size:
        idx = order[lo : lo + batch]
        lo += batch
        batch = min(batch * 8, 65536)
        lines = shapely.linestrings(np.stack([starts[idx], ends[idx]], axis=1))
        ok = np.ones(idx.size, dtype=bool)
        for interior in interiors:
            ok &= ~shapely.intersects(lines, interior)
        if ok.any():
            return float(lengths[idx[int(np.argmax(ok))]])
    return math.inf


def _seed_length(
    geom: Any,
    segs: Any,
    node: tuple[float, float],
    interiors: list[Any],
) -> float:
    """Shortest admissible straight segment from ``geom`` to ``node`` (inf if none).

    Exact for polygonal shapes: the candidates are the perpendicular foot of
    ``node`` on *every* boundary segment of every part of ``geom`` plus every
    vertex.  The minimum over the visible part of a segment is attained at the
    foot, a segment end, or where the sight line grazes an obstacle vertex --
    and that last case is the path through that obstacle-vertex graph node, so
    the search loses nothing by skipping it (#7058 review).
    """
    import numpy as np
    from shapely.geometry import Point

    if geom.distance(Point(node)) <= 0.0:
        return 0.0
    p = np.asarray([node], dtype=float)
    if segs.shape[0] == 0:
        return math.inf
    feet = _feet_on_segments(p, segs)[0]
    starts = np.concatenate([feet, segs[:, 0, :], segs[:, 1, :]], axis=0)
    lengths = np.hypot(starts[:, 0] - node[0], starts[:, 1] - node[1])
    ends = np.broadcast_to(p, starts.shape)
    return _first_admissible(lengths, starts, ends, interiors, math.inf)


def _best_direct_hop(
    segs_a: Any, segs_b: Any, interiors: list[Any], bound: float = math.inf
) -> float:
    """Shortest admissible straight segment from shape A to shape B (``< bound``).

    Candidates are every vertex of A dropped onto every segment of B (its
    perpendicular foot, clamped to the segment) and vice versa; segment-to-
    segment distance is always attained at such a vertex/foot pair.
    """
    import numpy as np

    best = bound
    for src, dst in ((segs_a, segs_b), (segs_b, segs_a)):
        if src.shape[0] == 0 or dst.shape[0] == 0:
            continue
        verts = np.unique(src.reshape(-1, 2), axis=0)
        chunk = max(1, 200_000 // dst.shape[0])
        for lo in range(0, verts.shape[0], chunk):
            vs = verts[lo : lo + chunk]
            feet = _feet_on_segments(vs, dst)  # (P, S, 2)
            starts = np.broadcast_to(vs[:, None, :], feet.shape).reshape(-1, 2)
            ends = feet.reshape(-1, 2)
            lengths = np.hypot(ends[:, 0] - starts[:, 0], ends[:, 1] - starts[:, 1])
            d = _first_admissible(lengths, starts, ends, interiors, best)
            if d < best:
                best = d
    return best


def _shortest_detour(geom_a: Any, geom_b: Any, obstacles: list[Any]) -> float:
    """Shortest visibility-graph path from ``geom_a`` to ``geom_b`` around obstacles.

    Nodes are a super-source (``geom_a``), a super-sink (``geom_b``) and every
    obstacle-polygon exterior vertex.  Source/sink edges use the shortest
    admissible segment from the whole shape -- any point of any part, not one
    fixed nearest-point pair or only its vertices -- so the path may leave and
    enter each shape wherever is shortest.  The direct source -> sink edge
    likewise considers every vertex-to-edge foot of both shapes.  An edge
    between two nodes is admissible when the connecting segment does not pass
    through the interior of any obstacle (segments that merely run along a
    slot boundary are allowed -- that is the surface path hugging the milled
    edge).  Returns ``inf`` when no path is found.
    """
    from shapely.geometry import LineString

    obstacles = _flatten_polygons(obstacles)
    interiors = _obstacle_interiors(obstacles)
    verts: list[tuple[float, float]] = []
    for obs in obstacles:
        for x, y in list(obs.exterior.coords)[:-1]:
            verts.append((x, y))

    segs_a = _boundary_segments(geom_a)
    segs_b = _boundary_segments(geom_b)

    n = len(verts) + 2  # node 0 = source, node 1 = sink
    adj: list[list[tuple[int, float]]] = [[] for _ in range(n)]

    def _add(i: int, j: int, w: float) -> None:
        adj[i].append((j, w))
        adj[j].append((i, w))

    # Dense O(n^2) adjacency -- n is tiny (a handful of slot corners).
    for i in range(len(verts)):
        for j in range(i + 1, len(verts)):
            seg = LineString([verts[i], verts[j]])
            if not _crosses_any_obstacle(seg, obstacles):
                _add(i + 2, j + 2, math.dist(verts[i], verts[j]))
    for i, vert in enumerate(verts):
        wa = _seed_length(geom_a, segs_a, vert, interiors)
        if wa != math.inf:
            _add(0, i + 2, wa)
        wb = _seed_length(geom_b, segs_b, vert, interiors)
        if wb != math.inf:
            _add(1, i + 2, wb)

    # Detour through the slot vertices first; the direct A -> B hop is the
    # expensive search (it is quadratic when a slot occludes every straight
    # line), so it only has to beat this bound.
    dist = [math.inf] * n
    dist[0] = 0.0
    pq: list[tuple[float, int]] = [(0.0, 0)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist[u]:
            continue
        if u == 1:
            break
        for nbr, w in adj[u]:
            nd = d + w
            if nd < dist[nbr]:
                dist[nbr] = nd
                heapq.heappush(pq, (nd, nbr))
    return min(dist[1], _best_direct_hop(segs_a, segs_b, interiors, dist[1]))


def surface_path_length(
    geom_a: Any,
    geom_b: Any,
    obstacles: list[Any] | None = None,
    *,
    dimension_x_mm: float = DEFAULT_DIMENSION_X_MM,
    _bridged: bool = False,
) -> tuple[float, float]:
    """Return ``(clearance, creepage)`` between two shapely geometries.

    ``clearance`` is the straight-line ``geom_a.distance(geom_b)``.
    ``creepage`` equals ``clearance`` when the straight nearest-points segment
    does not cross an interior Edge.Cuts cutout; otherwise it is the shortest
    path routing around the intervening slot polygon(s) (``> clearance``).

    Cutout parts narrower than ``dimension_x_mm`` (IEC 60664-1 dimension X)
    are bridged -- measured straight across -- before the search; pass ``0``
    to disable.  The path may leave/enter each shape at any point, not just
    the straight-line nearest pair (#7058).

    Overlapping / touching geometries (``clearance <= 0``) have no meaningful
    surface detour, so ``creepage == clearance`` there too.
    """
    require_shapely("creepage surface-path geometry")
    from shapely.geometry import LineString
    from shapely.ops import nearest_points

    obstacles = obstacles or []
    if not _bridged:
        obstacles = _bridge_narrow_cutouts(obstacles, dimension_x_mm)
    clearance = geom_a.distance(geom_b)
    if clearance <= 0.0 or not obstacles:
        return clearance, clearance

    pa_pt, pb_pt = nearest_points(geom_a, geom_b)
    straight = LineString([(pa_pt.x, pa_pt.y), (pb_pt.x, pb_pt.y)])
    if not _crosses_any_obstacle(straight, obstacles):
        return clearance, clearance

    creepage = _shortest_detour(geom_a, geom_b, obstacles)
    if creepage == math.inf:
        creepage = clearance  # defensive: no admissible path
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
    voltage_map: dict[str, VoltageInterval] | None = None,
    standard_obj: CreepageStandard | None = None,
    edge_voltage: float = 0.0,
    overvoltage_category: str = "II",
    insulation: str = DEFAULT_INSULATION,
    attach_zones: Sequence[Any] | None = None,
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
    When ``voltage_map`` (``{net_name: VoltageInterval}``) **and** ``standard_obj``
    are supplied, the requirement is derived **per pair** from the worst-case
    pairwise voltage difference ``dv = max(|a.hi - b.lo|, |b.hi - a.lo|)`` over
    the two nets' closed intervals (#4411) -- a swinging node's excursion can no
    longer hide behind its dominant static value (unmapped nets default to the
    degenerate ``0 V`` interval; the board edge uses the degenerate
    ``edge_voltage`` interval, default ``0 V`` == earth), instead of one global
    working voltage.  Same-potential pairs (``dv <= _ZERO_DV_EPS``) get a
    required creepage/clearance of ``0.0`` and are a trivial PASS -- this
    short-circuits the standard-table lookup (which starts at 50 V and raises
    for ``V <= 0``).  In map mode the HV-vs-HV pairing skip is relaxed so that
    same-class nets at different potentials (bank-vs-bank, phase-vs-phase) are
    also evaluated.  Voltages are treated as worst-case DC-equivalent magnitudes
    about a common reference; AC phase relationships are NOT modelled, so
    ``|dv|`` is conservative for in-phase nets.  ``overvoltage_category``
    (default ``"II"``) selects the Table F.1 rated impulse voltage for the
    per-pair clearance derivation (issue #7057); ``insulation`` (default
    ``"reinforced"``) selects the basic / supplementary / reinforced rule for
    both the per-pair creepage and clearance (issue #7082).

    Attach zones (#8028)
    --------------------
    When ``attach_zones`` (the router's own
    :func:`~kicad_tools.router.pairwise_clearance.build_attach_zones` output) is
    supplied, a ``board`` conductor pair whose binding gap lies inside a zone
    that terminates both nets on that layer is classified ``attach_zone`` and
    carries a *residual*: the pair re-measured with the zone's copper clipped
    away on every layer it covers.  The CLI waives such a row only when the
    residual passes, so a waived witness never masks a failing one elsewhere on
    the same pair (softstart#88).  ``None`` (default) leaves the census unchanged.

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
        voltage_map=(
            {k: _as_interval(v) for k, v in voltage_map.items()}
            if (map_mode and voltage_map is not None)
            else None
        ),
        edge_voltage=edge_voltage,
        insulation=insulation if standard is not None else None,
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
    norm_vmap: dict[str, VoltageInterval] = {}
    if map_mode:
        assert voltage_map is not None
        norm_vmap = {_norm_net_key(k): _as_interval(v) for k, v in voltage_map.items()}
    _req_cache: dict[float, tuple[float, float, dict[str, Any], dict[str, Any]]] = {}

    def _voltage(name: str) -> VoltageInterval:
        # Unmapped nets default to the degenerate 0 V interval.
        return norm_vmap.get(_norm_net_key(name), VoltageInterval(0.0, 0.0))

    def _required_for_dv(
        dv: float,
    ) -> tuple[float, float, dict[str, Any], dict[str, Any]]:
        """Derive ``(req_creepage, req_clearance, creep_prov, clear_prov)`` at ``dv``.

        Memoised by ``dv`` (rounded) so the ~1500-pair census performs at most
        one table lookup per distinct voltage difference.  ``dv <= _ZERO_DV_EPS``
        short-circuits to a trivial ``0.0`` requirement (no lookup).  An
        out-of-range ``dv`` propagates ``StandardLookupError`` (fail loud).
        """
        key = round(dv, 6)
        cached = _req_cache.get(key)
        if cached is not None:
            return cached
        if dv <= _ZERO_DV_EPS:
            res: tuple[float, float, dict[str, Any], dict[str, Any]] = (0.0, 0.0, {}, {})
        else:
            assert standard_obj is not None and pollution_degree is not None
            creep, cprov = standard_obj.required_creepage(
                dv, int(pollution_degree), material_group or "IIIa", insulation
            )
            # ``dv`` (RMS) plays the role of the rated voltage for the Table
            # F.1 impulse lookup -- the same approximation the creepage lookup
            # makes (issue #7057); the steady-state peak is derived inside.
            clr, clprov = standard_obj.required_clearance(
                dv, int(pollution_degree), overvoltage_category, insulation
            )
            res = (creep, clr, cprov, clprov)
        _req_cache[key] = res
        return res

    def _pair_requirement(
        name_a: str, name_b: str | None, *, edge: bool = False
    ) -> tuple[float, float, dict[str, Any]]:
        """Per-pair ``(req_creepage, req_clearance, provenance)`` from worst-case ``|dV|``.

        Each net is a closed interval (#4411).  The binding stress is the
        worst-case endpoint combination
        ``dv = max(|a.hi - b.lo|, |b.hi - a.lo|)`` -- a swinging node's excursion
        can no longer hide behind its dominant static value.  For two degenerate
        intervals ``(v, v)``/``(w, w)`` this collapses to ``|v - w|``, so scalar
        maps reproduce the pre-range behaviour byte-for-byte.  The board edge is
        the degenerate interval ``(edge_voltage, edge_voltage)``.
        """
        a = _voltage(name_a)
        b = VoltageInterval(edge_voltage, edge_voltage) if edge else _voltage(name_b or "")
        # Worst-case ΔV over interval endpoints, tracking which endpoints governed.
        dv_hi_lo = abs(a.hi - b.lo)
        dv_lo_hi = abs(b.hi - a.lo)
        if dv_hi_lo >= dv_lo_hi:
            dv, a_ep, b_ep, va, vb = dv_hi_lo, "hi", "lo", a.hi, b.lo
        else:
            dv, a_ep, b_ep, va, vb = dv_lo_hi, "lo", "hi", a.lo, b.hi
        req_creep, req_clear, cprov, clprov = _required_for_dv(dv)
        voltage_prov: dict[str, Any] = {
            "net_a_v": va,
            "net_b_v": vb,
            "delta_v_v": round(dv, 4),
            "same_potential": dv <= _ZERO_DV_EPS,
        }
        # Record the governing endpoints only when a genuine swing was involved,
        # so an all-scalar (degenerate) map serialises byte-identically to the
        # pre-#4411 provenance (the endpoint choice is arbitrary when lo == hi).
        if not (a.is_degenerate and b.is_degenerate):
            voltage_prov["net_a_endpoint"] = a_ep
            voltage_prov["net_b_endpoint"] = b_ep
        prov: dict[str, Any] = {"voltage": voltage_prov}
        if cprov:
            prov["creepage"] = cprov
        if clprov:
            prov["clearance"] = clprov
        return req_creep, req_clear, prov

    if not hv_nets:
        return report

    number_to_name = {net.number: net.name for net in pcb.nets.values()}
    # Bridge sub-dimension-X cutouts once per census (#7058).
    x_mm = dimension_x_mm(pollution_degree)
    report.dimension_x_mm = x_mm
    obstacles = _bridge_narrow_cutouts(board_slot_obstacles(pcb), x_mm)

    # Per-layer per-net copper unions.
    layer_unions: dict[str, dict[int, Any]] = {}
    for layer in pcb.copper_layers:
        layer_unions[layer.name] = _net_union_on_layer(pcb, layer.name)

    # Footprint-membership index (#4403): recovers the pad->footprint identity
    # the per-net union erases, so each binding pair can be classified as a
    # board-fixable approach or a component-internal (same-footprint) gap.
    fp_index = _FootprintPadIndex(pcb)

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
                clearance, creepage = surface_path_length(
                    hv_geom, other_geom, obstacles, _bridged=True
                )
                key = (hv_num, other_num)
                prev = best.get(key)
                if prev is None or creepage < prev[1]:
                    best[key] = (clearance, creepage, layer_name)

    for (hv_num, other_num), (clearance, creepage, layer_name) in best.items():
        net_a_name = hv_nets[hv_num]
        net_b_name = number_to_name.get(other_num, f"net{other_num}")
        relationship = fp_index.relationship(layer_name, hv_num, other_num, clearance)
        residual: dict[str, Any] = {}
        if attach_zones and relationship == "board":
            lu = layer_unions[layer_name]
            if _witness_in_attach_zone(
                lu[hv_num], lu[other_num], attach_zones, net_a_name, net_b_name, layer_name
            ):
                r_clr, r_creep, r_layer, r_wit = _attach_zone_residual(
                    layer_unions,
                    attach_zones,
                    hv_num,
                    other_num,
                    net_a_name,
                    net_b_name,
                    obstacles,
                )
                relationship = "attach_zone"
                residual = {
                    "residual_clearance_mm": r_clr,
                    "residual_creepage_mm": r_creep,
                    "residual_layer": r_layer,
                    "residual_witness_mm": r_wit,
                }
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
                    relationship=relationship,
                    **residual,
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
                    relationship=relationship,
                    **residual,
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
            clearance, creepage = surface_path_length(hv_all, edge_geom, obstacles, _bridged=True)
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
