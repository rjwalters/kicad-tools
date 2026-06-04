"""Analog-ground bridge topology analysis (Phase 2b, issue #3178).

This module is the **topology-aware** sibling of the Phase 2 local 2-pad
bridge scan in :func:`kicad_tools.analysis.analog_detect.check_analog_ground_bridge`.

Where Phase 2 only inspects pad-net membership, Phase 2b consults the
routed copper / zone connectivity graph to answer the canonical EMC
question:

    *Does the analog ground (GNDA / AGND) join the digital ground
    (GND / DGND / VSS) through* **exactly one** *electrical path?*

Three outcomes follow:

* ``0`` distinct join points → "no bridge" advisory (same as Phase 2).
* ``1`` distinct join point → single-point bond (correct, no advisory).
* ``≥2`` distinct join points → **ground loop** advisory.

In addition, each candidate bridge is **wired-verified**: both pads must
attach to the rest of their respective ground graph through copper /
zone, otherwise the bridge is floating and is reported as such.

The module exposes:

* :class:`BridgeInfo` — per-bridge record (component reference, kind,
  pad/net pairs, wired-into-ground flags).
* :class:`GroundTopologyResult` — overall result with ``bridge_count``,
  ``bridges``, ``advisory``.
* :func:`analyze_ground_topology` — main entry point.

The function never raises; on any internal error it returns a
"fallback" result whose ``used_fallback`` flag is true, which the caller
(`check_analog_ground_bridge`) can use to drop back to the Phase 2 local
scan.

Reuse strategy
--------------
Per-net copper / zone graph construction is **delegated** to
:class:`kicad_tools.validate.connectivity.ConnectivityValidator` (its
``_build_connectivity_graph`` and ``_get_net_pads`` methods are called
twice, once per ground net).  No refactoring of the connectivity
validator is required for Phase 2b; the per-net engine is already
graph-shaped and the cross-net union step is the only new work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "BridgeInfo",
    "GroundTopologyResult",
    "analyze_ground_topology",
    "pcb_has_copper_topology",
]


# ---------------------------------------------------------------------------
# Ground-name regexes (mirrored from analog_detect.py)
# ---------------------------------------------------------------------------

# Analog grounds: ``GNDA``, ``AGND``, ``MIC_AGND`` ...
_ANALOG_GROUND_RE = re.compile(r"^(?:GNDA|AGND)$|_AGND$", re.IGNORECASE)

# Digital grounds: the return the analog ground is expected to bridge to.
_DIGITAL_GROUND_RE = re.compile(r"^(?:GND|GNDD|DGND|VSS|GROUND)$|_DGND$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Bridge-component recognition (extended from Phase 2)
# ---------------------------------------------------------------------------

# KiCad net-tie footprints.  Phase 2 used a single substring match; Phase 2b
# keeps that permissive match because real KiCad libraries name net-ties as
# ``NetTie``, ``NetTie-2_SMD``, ``NetTie_2_THT``, ``NetTie_Wide``,
# ``NetTie_3``, ``NetTie_4`` and similar.  Reference designators on net-ties
# are typically ``NT*``.
_NETTIE_NAME_RE = re.compile(r"NetTie", re.IGNORECASE)

# Ferrite-bead value patterns (mirrors analog_detect.py).
_FERRITE_VALUE_RE = re.compile(
    r"^FB\d*$|^(?:BLM|MPZ|HZ|MMZ|BK)\w*|\d+\s*R?\s*@\s*\d+\s*MHZ",
    re.IGNORECASE,
)

# 0-ohm resistor values.  Engineers spell these many ways: bare ``0``,
# ``0R``, ``0Ω``, ``0E``, ``R0``, ``0.0``, ``0 ohm`` / ``0ohm``.  The
# reference designator is required to be ``R*`` so we do not mis-classify a
# capacitor or other passive whose value happens to be ``0`` (which would be
# nonsensical for non-resistor parts but cheap to guard against).
_ZERO_OHM_VALUE_RE = re.compile(r"^(?:0|0R|0Ω|0E|0\.0|R0|0\s*ohm)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BridgeInfo:
    """A single recognized GNDA↔GND bridge component.

    Attributes
    ----------
    reference:
        Component reference designator (e.g. ``FB1``, ``R5``, ``NT2``).
    kind:
        One of ``"ferrite"`` / ``"zero_ohm"`` / ``"nettie"`` /
        ``"unknown"``.
    analog_pads:
        Pad identifiers (``REF.PAD``) on the analog-ground side.
    digital_pads:
        Pad identifiers (``REF.PAD``) on the digital-ground side.
    analog_pad_wired:
        True if at least one analog-side pad is reachable in the
        analog-ground connectivity graph (i.e. routed to another GNDA
        pad through copper or zone fill).
    digital_pad_wired:
        True if at least one digital-side pad is reachable in the
        digital-ground connectivity graph.
    """

    reference: str
    kind: str
    analog_pads: tuple[str, ...]
    digital_pads: tuple[str, ...]
    analog_pad_wired: bool
    digital_pad_wired: bool

    @property
    def is_wired(self) -> bool:
        """True when both sides of the bridge attach to their ground graph."""
        return self.analog_pad_wired and self.digital_pad_wired

    def to_dict(self) -> dict[str, object]:
        return {
            "reference": self.reference,
            "kind": self.kind,
            "analog_pads": list(self.analog_pads),
            "digital_pads": list(self.digital_pads),
            "analog_pad_wired": self.analog_pad_wired,
            "digital_pad_wired": self.digital_pad_wired,
            "is_wired": self.is_wired,
        }


@dataclass
class GroundTopologyResult:
    """Per-(analog, digital) ground-pair topology result.

    Attributes
    ----------
    analog_ground_name:
        Name of the analog-ground net (e.g. ``GNDA``).
    digital_ground_name:
        Name of the digital-ground net the bridges land on
        (e.g. ``GND``).
    bridge_count:
        Count of **verified, wired** bridges between the two grounds.
        ``0`` = isolated, ``1`` = single-point bond (desired),
        ``≥2`` = ground loop.
    bridges:
        Every recognised candidate bridge, including unwired/floating
        ones.  Use :pyattr:`BridgeInfo.is_wired` to filter.
    advisory:
        Human-readable advisory, set only when the topology is
        non-ideal (``bridge_count == 0``, ``bridge_count ≥ 2``, or at
        least one floating bridge present).
    used_fallback:
        True when no copper / zone topology was available and the
        caller should drop back to the Phase 2 local 2-pad scan.
    """

    analog_ground_name: str
    digital_ground_name: str
    bridge_count: int = 0
    bridges: list[BridgeInfo] = field(default_factory=list)
    advisory: str | None = None
    used_fallback: bool = False

    @property
    def floating_bridges(self) -> list[BridgeInfo]:
        """Bridges whose pads are NOT wired into the ground graph."""
        return [b for b in self.bridges if not b.is_wired]

    @property
    def wired_bridges(self) -> list[BridgeInfo]:
        """Bridges with both pads electrically attached to their ground."""
        return [b for b in self.bridges if b.is_wired]

    def to_dict(self) -> dict[str, object]:
        return {
            "analog_ground_name": self.analog_ground_name,
            "digital_ground_name": self.digital_ground_name,
            "bridge_count": self.bridge_count,
            "bridges": [b.to_dict() for b in self.bridges],
            "advisory": self.advisory,
            "used_fallback": self.used_fallback,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def pcb_has_copper_topology(pcb: PCB) -> bool:
    """Return True if the PCB has any segments, vias or filled zones.

    The topology analyzer needs at least one of these to confirm bridge
    pads are wired; without them the result reduces to "candidate
    components exist" which is exactly the Phase 2 local scan, so the
    caller should fall back.

    The function never raises; on any inspection failure it returns False
    (which selects the fallback path).
    """
    try:
        if list(getattr(pcb, "segments", []) or []):
            return True
        if list(getattr(pcb, "vias", []) or []):
            return True
        for zone in getattr(pcb, "zones", []) or []:
            if getattr(zone, "filled_polygons", None):
                return True
            # Even an empty filled_polygons list is meaningful if the zone
            # has a boundary polygon — pads can fall inside it.
            if getattr(zone, "polygon", None):
                return True
    except Exception:  # noqa: BLE001 — never raise from advisory code
        return False
    return False


def analyze_ground_topology(pcb: PCB) -> list[GroundTopologyResult]:
    """Run the full Phase 2b topology analysis.

    Returns one :class:`GroundTopologyResult` per analog-ground net found
    on the board.  Empty when no analog ground is present, when no digital
    ground exists to bridge to, or when the PCB has no copper topology
    (segments / vias / filled or boundary zones) — in the last case the
    caller is expected to fall back to the Phase 2 local 2-pad scan.

    Never raises.  On any internal failure the result list will be empty
    and the caller falls back.
    """
    try:
        return _analyze_ground_topology_impl(pcb)
    except Exception:  # noqa: BLE001 — never raise from advisory code
        return []


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def _analyze_ground_topology_impl(pcb: PCB) -> list[GroundTopologyResult]:
    """Inner implementation, may raise; wrapped by :func:`analyze_ground_topology`."""
    nets = getattr(pcb, "nets", None) or {}

    analog_grounds = _find_grounds(nets, _ANALOG_GROUND_RE)
    digital_grounds = _find_grounds(nets, _DIGITAL_GROUND_RE)

    if not analog_grounds or not digital_grounds:
        return []

    # Need at least minimal copper topology to do a wired-verification step.
    if not pcb_has_copper_topology(pcb):
        return [
            GroundTopologyResult(
                analog_ground_name=a_name,
                digital_ground_name=sorted(digital_grounds.values())[0],
                used_fallback=True,
            )
            for a_name in sorted(analog_grounds.values())
        ]

    # Build per-net connectivity graphs for every involved ground net.
    # We delegate to ConnectivityValidator: it already handles segments,
    # vias, zone-polygon containment and segment-chain transitive closure.
    from kicad_tools.validate.connectivity import ConnectivityValidator

    validator = ConnectivityValidator(pcb)

    ground_graphs: dict[int, dict[str, set[str]]] = {}
    for net_number in list(analog_grounds) + list(digital_grounds):
        # Per-net zone tracking attribute that ``_build_connectivity_graph``
        # writes to.  Reset before each call so state from a previous net
        # does not leak.
        validator._last_zone_connected_pads = set()
        try:
            ground_graphs[net_number] = validator._build_connectivity_graph(net_number)
        except Exception:  # noqa: BLE001
            # If a single ground net's graph build fails, treat it as
            # empty — bridges into that ground will be flagged unwired
            # rather than crashing the whole audit.
            ground_graphs[net_number] = {}

    # Pick the canonical digital-ground name to report against, even when
    # the board has multiple digital grounds (e.g. both GND and DGND).
    digital_canonical = sorted(digital_grounds.values())[0]
    digital_net_numbers = set(digital_grounds.keys())

    results: list[GroundTopologyResult] = []
    for a_net_num, a_name in sorted(analog_grounds.items(), key=lambda kv: kv[1]):
        result = _analyze_one_analog_ground(
            pcb=pcb,
            analog_net_number=a_net_num,
            analog_name=a_name,
            digital_canonical=digital_canonical,
            digital_net_numbers=digital_net_numbers,
            ground_graphs=ground_graphs,
        )
        results.append(result)

    return results


def _find_grounds(nets: dict, pattern: re.Pattern[str]) -> dict[int, str]:
    """Return ``{net_number: name}`` for all nets whose name matches *pattern*.

    Net 0 (the unconnected pseudo-net) is excluded.
    """
    found: dict[int, str] = {}
    for number, net in nets.items():
        if number == 0:
            continue
        name = getattr(net, "name", "") or ""
        if name and pattern.search(name):
            found[number] = name
    return found


def _analyze_one_analog_ground(
    *,
    pcb: PCB,
    analog_net_number: int,
    analog_name: str,
    digital_canonical: str,
    digital_net_numbers: set[int],
    ground_graphs: dict[int, dict[str, set[str]]],
) -> GroundTopologyResult:
    """Build a :class:`GroundTopologyResult` for one analog-ground net."""
    bridges = _collect_candidate_bridges(
        pcb=pcb,
        analog_net_number=analog_net_number,
        digital_net_numbers=digital_net_numbers,
        ground_graphs=ground_graphs,
    )

    wired = [b for b in bridges if b.is_wired]
    floating = [b for b in bridges if not b.is_wired]

    advisory = _build_advisory(
        analog_name=analog_name,
        digital_canonical=digital_canonical,
        wired=wired,
        floating=floating,
    )

    return GroundTopologyResult(
        analog_ground_name=analog_name,
        digital_ground_name=digital_canonical,
        bridge_count=len(wired),
        bridges=bridges,
        advisory=advisory,
        used_fallback=False,
    )


def _collect_candidate_bridges(
    *,
    pcb: PCB,
    analog_net_number: int,
    digital_net_numbers: set[int],
    ground_graphs: dict[int, dict[str, set[str]]],
) -> list[BridgeInfo]:
    """Walk the PCB's footprints and return every recognised bridge candidate."""
    bridges: list[BridgeInfo] = []
    for fp in getattr(pcb, "footprints", []) or []:
        kind = _classify_bridge_component(fp)
        if kind is None:
            continue

        ref = getattr(fp, "reference", "") or "?"
        pads = getattr(fp, "pads", []) or []

        analog_pads: list[str] = []
        digital_pads: list[str] = []
        other_nets_present = False

        for pad in pads:
            pad_id = f"{ref}.{getattr(pad, 'number', '')}"
            pad_net = getattr(pad, "net_number", None)
            pad_net_name = getattr(pad, "net_name", "") or ""

            if pad_net == analog_net_number:
                analog_pads.append(pad_id)
            elif pad_net in digital_net_numbers:
                digital_pads.append(pad_id)
            elif pad_net is None or pad_net == 0:
                # Net 0 / unresolved KiCad-10 name-only nets: we cannot
                # confirm the pad belongs to either ground.  Treating it
                # as "not yet on either ground" is safe — the bridge will
                # be rejected later if it ends up with no pads on a
                # required ground, but a partial match (e.g. analog pad
                # known, digital pad name-only) will not falsely succeed.
                _ = pad_net_name  # intentional: name-only resolution is
                # left to the caller's higher-level netlist machinery.
            else:
                # Pad sits on a third net unrelated to either ground.
                # Per the issue: "at least one pad on each ground, no
                # third unrelated net" — so reject this candidate.
                other_nets_present = True
                break

        if other_nets_present:
            continue
        if not analog_pads or not digital_pads:
            # Must touch BOTH grounds to be a bridge.
            continue

        analog_wired = _pad_set_wired(
            analog_pads,
            ground_graphs.get(analog_net_number, {}),
        )
        digital_wired = _any_pad_wired_in_any_digital_ground(
            digital_pads,
            digital_net_numbers,
            ground_graphs,
        )

        bridges.append(
            BridgeInfo(
                reference=ref,
                kind=kind,
                analog_pads=tuple(analog_pads),
                digital_pads=tuple(digital_pads),
                analog_pad_wired=analog_wired,
                digital_pad_wired=digital_wired,
            )
        )

    return bridges


def _classify_bridge_component(fp: object) -> str | None:
    """Return the bridge ``kind`` for a footprint, or ``None`` if it is not one.

    Recognised kinds:

    * ``"nettie"`` — KiCad ``NetTie*`` footprint (any pad count).
    * ``"ferrite"`` — value matches the ferrite-bead pattern.
    * ``"zero_ohm"`` — reference ``R*`` AND value matches the 0-ohm pattern.
    """
    name = getattr(fp, "name", "") or ""
    value = getattr(fp, "value", "") or ""
    ref = getattr(fp, "reference", "") or ""

    if _NETTIE_NAME_RE.search(name) or _NETTIE_NAME_RE.search(value):
        return "nettie"
    if _FERRITE_VALUE_RE.search(value):
        return "ferrite"
    if ref.upper().startswith("R") and _ZERO_OHM_VALUE_RE.match(value.strip()):
        return "zero_ohm"
    return None


def _pad_set_wired(
    bridge_pads: list[str],
    graph: dict[str, set[str]],
) -> bool:
    """Return True when at least one bridge pad attaches to copper/zone.

    A pad is "wired" when it has at least one neighbor in the per-net
    connectivity graph — i.e. it is electrically reachable to another pad
    on the same net through routed copper or zone fill.

    A bridge whose pad has no graph neighbors is considered floating: no
    routed copper, segment chain or zone polygon containment connected it
    to any other pad on the same net.  Phase 2b reports such bridges with
    a distinct "bridge present but not wired" advisory rather than
    counting them toward the single-point bond.
    """
    if not graph:
        return False
    for pad_id in bridge_pads:
        # Pad has at least one neighbor on its own net's graph.
        neighbors = graph.get(pad_id)
        if neighbors:
            return True
    return False


def _any_pad_wired_in_any_digital_ground(
    digital_pads: list[str],
    digital_net_numbers: set[int],
    ground_graphs: dict[int, dict[str, set[str]]],
) -> bool:
    """A bridge's digital pad may sit on any of several digital grounds.

    Walk every digital ground net and consider the pad wired if any of
    them reports a neighbor.
    """
    for net_number in digital_net_numbers:
        if _pad_set_wired(digital_pads, ground_graphs.get(net_number, {})):
            return True
    return False


def _build_advisory(
    *,
    analog_name: str,
    digital_canonical: str,
    wired: list[BridgeInfo],
    floating: list[BridgeInfo],
) -> str | None:
    """Render the topology advisory.

    * 0 wired, no floating → "no bridge" (existing wording).
    * 0 wired, ≥1 floating → "bridge present but not wired".
    * 1 wired → no advisory (correct single-point bond), but still warn if
      a floating bridge sits alongside.
    * ≥2 wired → "ground loop" advisory naming the bridges.
    """
    floating_msg: str | None = None
    if floating:
        refs = ", ".join(b.reference for b in floating)
        floating_msg = (
            f"analog ground {analog_name} has a bridge present but not "
            f"wired ({refs}) -- route both bridge pads to their respective "
            "ground"
        )

    if len(wired) == 0:
        if floating_msg is not None:
            return floating_msg
        return (
            f"analog ground {analog_name} has no bridge to {digital_canonical} -- "
            "add a ferrite/net-tie single-point bridge"
        )

    if len(wired) == 1:
        # Correct single-point bond.  Still surface a floating co-bridge.
        return floating_msg

    # ≥2 wired bridges → ground loop.
    refs = ", ".join(b.reference for b in wired)
    loop_msg = (
        f"ground loop: {analog_name} joined to {digital_canonical} through "
        f"{len(wired)} bridges ({refs}) -- single-point bond required"
    )
    if floating_msg is not None:
        return f"{loop_msg}; additionally, {floating_msg}"
    return loop_msg
