"""Kelvin / current-sense topology model and constrained routing (Issue #4473).

A **Kelvin** (4-wire / current-sense) connection taps the sense voltage
*at* a shunt/sense-resistor pad -- the *Kelvin point* -- so a
high-impedance measurement (op-amp input, ADC) reads the true shunt
voltage and is not corrupted by the IR drop along the high-current path
that flows through the same net.

Why the generic RSMT is wrong for these nets
--------------------------------------------
The autorouter's Rectilinear Steiner Minimum Tree
(:func:`kicad_tools.router.algorithms.steiner.build_rsmt`) treats every
terminal of a multi-pad net as an interchangeable equipotential and
inserts Steiner branch points wherever total wirelength is minimised.
For an ordinary signal that is fine.  For a Kelvin sense net it is
*incorrect*: an arbitrary Steiner branch can merge the sense tap into the
shunt -> load high-current segment, so the op-amp reads the voltage at the
branch point rather than at the shunt pad, injecting the load-current IR
drop straight into the measurement.

On board-05 the five ISENSE nets are 4-pad Kelvin structures (shunt
R10-R12, op-amp SP/SN input, low-side FET-source tap, ADC PA0-2).  The
generic RSMT synthesises arbitrary Steiner points on them (design.py
:func:`route_pcb` docstring, the ``2888-2907`` region) and they lose the
multi-net negotiation in the congested U3 south sense band.

The topology model
------------------
This module recognises Kelvin sense nets by convention (net-name pattern
+ presence of a shunt/sense-resistor pad) and replaces the RSMT with a
**star topology rooted at the Kelvin point**: every other terminal
connects directly to the shunt pad, so

  * the sense trace physically connects **at** the Kelvin point, and
  * no sense edge shares copper with the high-current edge except at the
    Kelvin point itself (the star has exactly one shared node -- the
    root -- and no synthesised Steiner branch points).

The result is a drop-in replacement for :func:`build_rsmt`'s return shape
``(extended_pads, edges)``: ``extended_pads`` is the original pad list
unchanged (a star introduces **no** virtual Steiner pads) and ``edges``
are ``(root_index, terminal_index)`` pairs sorted shortest-first for
routing order.

Detection is deliberately conservative so ordinary analog nets are not
disturbed: a net is only treated as Kelvin when BOTH its name matches a
current-sense pattern AND a shunt/sense-resistor root pad can be
identified.  Any net that fails either gate falls back to the generic
RSMT unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .net_names import net_name_suffix

if TYPE_CHECKING:
    from .primitives import Pad


# ---------------------------------------------------------------------------
# Detection conventions
# ---------------------------------------------------------------------------

# Net-name substrings that denote a current-sense / Kelvin net.  Kept
# intentionally broad (matches ``ISENSE_A+``, ``I_SENSE_P``, ``VSNS``,
# ``CURRENT_SENSE``, ``SHUNT``, ``IMON``, ``KELVIN``) because the second
# gate -- the presence of a shunt/sense-resistor root pad -- is what
# actually protects ordinary analog nets from being reshaped.  The suffix
# after the last ``/`` is matched so hierarchical names (``/PWR/ISENSE_A``)
# resolve the same as bare ones.
_SENSE_NAME_RE = re.compile(
    r"(I_?SENSE|V_?SENSE|C_?SENSE|CURR(ENT)?_?SENSE|SENSE|SHUNT|"
    r"ISNS|VSNS|CSNS|IMON|VMON|KELVIN)",
    re.IGNORECASE,
)

# Reference-designator pattern for a discrete resistor (the shunt).  A
# current-sense Kelvin tap is taken at a shunt resistor's pad, so the pad
# whose footprint is a resistor is the Kelvin point.
_RESISTOR_REF_RE = re.compile(r"^R\d", re.IGNORECASE)

# Library-footprint substrings that identify a resistor / shunt package,
# used as a fallback when the pad's ``ref`` is unavailable (e.g. a pad
# harvested without its designator).
_RESISTOR_FP_RE = re.compile(r"(resistor|_shunt|shunt_|current[_-]?sense)", re.IGNORECASE)


@dataclass(frozen=True)
class KelvinTopology:
    """A recognised Kelvin sense net and its constrained star topology.

    Attributes:
        root_index: Index into the net's pad list of the Kelvin-point pad
            (the shunt/sense-resistor pad every sense trace must tap).
        edges: ``(root_index, terminal_index)`` pairs forming the star,
            sorted shortest-first for routing order.  Every non-root
            terminal appears exactly once, so the sense taps connect at
            the Kelvin point and never through the high-current terminal.
    """

    root_index: int
    edges: list[tuple[int, int]]


def _is_shunt_pad(pad: Pad) -> bool:
    """Return True when ``pad`` belongs to a shunt / sense resistor.

    A pad is a Kelvin-point candidate when its component reference is a
    resistor designator (``R10``, ``R3``) or, lacking a usable ref, its
    library footprint name reads as a resistor/shunt package.  Virtual
    Steiner pads (``steiner_point``) never qualify.
    """
    if getattr(pad, "steiner_point", False):
        return False
    ref = (pad.ref or "").strip()
    if ref and _RESISTOR_REF_RE.match(ref):
        return True
    fp = getattr(pad, "footprint_name", "") or ""
    if fp and _RESISTOR_FP_RE.search(fp):
        return True
    return False


def is_sense_net_name(net_name: str) -> bool:
    """Return True when ``net_name`` reads as a current-sense / Kelvin net.

    Matches the dedicated sense-name pattern (``ISENSE``, ``VSNS``,
    ``SHUNT`` ...) against the sheet-local suffix so hierarchical names
    (``/PWR/ISENSE_A``) resolve the same as bare ones.  Deliberately
    narrower than the router's ANALOG classification: ``ADC0`` classifies
    ANALOG but is not a sense tap and is excluded here, so ordinary analog
    nets keep the generic RSMT.
    """
    if not net_name:
        return False
    suffix = net_name_suffix(net_name)
    return bool(_SENSE_NAME_RE.search(suffix))


def find_kelvin_root(pad_objs: list[Pad]) -> int | None:
    """Identify the Kelvin-point pad index among a net's pads.

    The Kelvin point is the shunt/sense-resistor pad every sense trace
    must tap.  Selection:

    * Gather the pads that sit on a resistor/shunt footprint
      (:func:`_is_shunt_pad`).
    * Zero candidates -> ``None`` (not a resolvable Kelvin net; the caller
      falls back to the generic RSMT).
    * One candidate -> that pad.
    * Several candidates (e.g. both pads of the shunt are on the net, or a
      filter resistor shares it) -> the candidate minimising total
      Manhattan distance to every other pad, a deterministic "most
      central" tie-break that keeps the star's total wirelength low.

    Returns:
        Index into ``pad_objs`` of the Kelvin-point pad, or ``None``.
    """
    candidates = [i for i, p in enumerate(pad_objs) if _is_shunt_pad(p)]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    def _total_manhattan(idx: int) -> float:
        px, py = pad_objs[idx].x, pad_objs[idx].y
        return sum(
            abs(px - pad_objs[k].x) + abs(py - pad_objs[k].y)
            for k in range(len(pad_objs))
            if k != idx
        )

    # Deterministic: minimise total distance, break exact ties by lowest
    # index so repeated runs pick the same root.
    return min(candidates, key=lambda i: (_total_manhattan(i), i))


def detect_kelvin_topology(pad_objs: list[Pad]) -> KelvinTopology | None:
    """Recognise a Kelvin sense net and return its star topology.

    A net qualifies only when ALL hold:

    1. It has at least 3 terminals (a 2-pin net has no topology choice and
       is already Kelvin-correct as a single edge).
    2. Its net name reads as a current-sense / Kelvin net
       (:func:`is_sense_net_name`).
    3. A shunt/sense-resistor root pad is resolvable
       (:func:`find_kelvin_root`).

    Returns:
        A :class:`KelvinTopology` (root index + star edges) when all gates
        pass, else ``None`` so the caller falls back to the generic RSMT.
    """
    if len(pad_objs) < 3:
        return None

    # All pads of a net share one net name; read it from the first real
    # (non-Steiner) pad.
    net_name = ""
    for p in pad_objs:
        if not getattr(p, "steiner_point", False):
            net_name = p.net_name or ""
            if net_name:
                break
    if not is_sense_net_name(net_name):
        return None

    root = find_kelvin_root(pad_objs)
    if root is None:
        return None

    edges = build_kelvin_star_edges(pad_objs, root)
    return KelvinTopology(root_index=root, edges=edges)


def build_kelvin_star_edges(pad_objs: list[Pad], root_index: int) -> list[tuple[int, int]]:
    """Build the star edge list rooted at ``root_index``.

    Every non-root terminal connects directly to the root (the Kelvin
    point).  Edges are sorted shortest-first (Manhattan) so the router
    lays the tightest, most-constrained sense taps before the longer runs
    -- matching :func:`build_rsmt`'s routing-order convention.
    """
    rx, ry = pad_objs[root_index].x, pad_objs[root_index].y
    edges = [(root_index, k) for k in range(len(pad_objs)) if k != root_index]
    edges.sort(key=lambda e: abs(rx - pad_objs[e[1]].x) + abs(ry - pad_objs[e[1]].y))
    return edges


def build_kelvin_topology(
    pad_objs: list[Pad],
) -> tuple[list[Pad], list[tuple[int, int]]] | None:
    """Constrained Kelvin topology as a drop-in for :func:`build_rsmt`.

    Returns ``(extended_pads, edges)`` with the SAME shape
    :func:`build_rsmt` returns, so a caller can substitute it directly:

    * ``extended_pads`` is ``list(pad_objs)`` unchanged -- a star
      introduces **no** synthetic Steiner branch points, which is exactly
      what makes the topology Kelvin-correct (no arbitrary branch can
      merge the sense tap into the high-current segment).
    * ``edges`` are ``(root, terminal)`` index pairs into ``extended_pads``
      sorted shortest-first.

    Returns ``None`` when the net is not a recognised Kelvin sense net, so
    the caller falls through to the generic RSMT.
    """
    topo = detect_kelvin_topology(pad_objs)
    if topo is None:
        return None
    return list(pad_objs), topo.edges


def is_kelvin_net(pad_objs: list[Pad]) -> bool:
    """Return True when this net's pads form a recognised Kelvin sense net.

    Convenience predicate for callers that want to *prioritise* Kelvin
    nets (route them before ordinary equipotentials so their constrained
    star wins the corridor negotiation) without building the topology.
    """
    return detect_kelvin_topology(pad_objs) is not None
