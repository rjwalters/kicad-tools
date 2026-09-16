"""Bounded reservations for two widened, tapered pads on one footprint."""

from __future__ import annotations

import copy
import heapq
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .geometry import Pt
from .obstacles import CommittedCopper
from .quadtree import NodeKey

if TYPE_CHECKING:
    from ..primitives import Pad
    from .pathfinder import Connection, LatticePathfinder


@dataclass(frozen=True)
class EscapeChoice:
    key: NodeKey
    layer: int
    points: tuple[Pt, ...]
    length: float
    point: Pt
    exit_layer: int
    net: int
    half: float
    neck_half: float
    clearance: float

    def reserve(self, copper: CommittedCopper) -> None:
        copper.add_run(self.layer, list(self.points), self.net, self.neck_half, self.clearance)
        # add_run deliberately drops zero-length segments; this disk is the
        # full-width landing, so insert it directly into the spatial index.
        copper.copper[self.exit_layer].add(
            self.point, self.point, self.net, self.half, self.clearance
        )
        if self.exit_layer != self.layer:
            copper.add_via(self.point, self.net, self.clearance)

    def clears(self, copper: CommittedCopper) -> bool:
        return (
            all(
                copper.seg_clear(a, b, self.layer, self.net, self.neck_half, self.clearance)
                for a, b in zip(self.points, self.points[1:], strict=False)
            )
            and copper.node_clear(self.point, self.exit_layer, self.net, self.half, self.clearance)
            and (
                self.exit_layer == self.layer
                or copper.via_clear(self.point, self.net, self.clearance)
            )
        )


def foreign_reservations(
    committed: CommittedCopper, plan: dict[int, EscapeChoice], net: int
) -> CommittedCopper:
    foreign = [choice for choice in plan.values() if choice.net != net]
    if not foreign:
        return committed
    # Only copper indices/lists are mutable reservation state. Share the
    # fixed-fill, pairwise and Kelvin snapshots; prepared Shapely guards
    # cannot be deep-copied and must retain their original branch exclusions.
    result = copy.copy(committed)
    result.copper = copy.deepcopy(committed.copper)
    result.vias = list(committed.vias)
    result.via_copper = list(committed.via_copper)
    for choice in foreign:
        choice.reserve(result)
    return result


def chosen_stub(choice: EscapeChoice, committed: CommittedCopper) -> tuple[list, float]:
    # Static predicates were checked when planning. Recheck accumulated copper
    # on the actual neck, without pretending the body's landing is on its layer.
    valid = committed.node_clear(
        choice.point, choice.layer, choice.net, choice.neck_half, choice.clearance
    ) and all(
        committed.seg_clear(a, b, choice.layer, choice.net, choice.neck_half, choice.clearance)
        for a, b in zip(choice.points, choice.points[1:], strict=False)
    )
    stubs = [(choice.key, choice.layer, list(choice.points), choice.length)] if valid else []
    return stubs, 2 * choice.neck_half


def plan_tapered_escapes(
    pf: LatticePathfinder, connections: list[Connection], deadline: float | None
) -> dict[int, EscapeChoice]:
    """Reserve compatible local escapes after an unsuccessful ordinary pass.

    Only two widened pads of different nets on one footprint are coordinated.
    Other groups retain ordinary negotiation. Candidate and pair-probe caps
    bound the extra work; all time is charged to the original deadline.
    """
    if deadline is not None and time.monotonic() >= deadline:
        return {}
    groups: dict[str, dict[int, tuple[Pad, object | None]]] = defaultdict(dict)
    ambiguous: set[str] = set()
    for _key, start, end, net_class in connections:
        if 2 * pf._conn_geometry(net_class)[0] <= pf.rules.trace_width + 1e-9:
            continue
        for pad in (start, end):
            if not pad.ref:
                continue
            previous = groups[pad.ref].get(id(pad))
            if previous is not None and previous[1] != net_class:
                ambiguous.add(pad.ref)
            groups[pad.ref][id(pad)] = (pad, net_class)
    fixed = pf._fresh_committed()
    lattice, obstacles = pf.build(), pf.obstacles
    pw, ko = pf._pairwise, pf._keepouts
    plan: dict[int, EscapeChoice] = {}

    def expired() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    def options(pad: Pad, net_class: object | None) -> list[EscapeChoice]:
        half, clr = pf._conn_geometry(net_class)
        hard = pf._hard_avoided_layers(net_class)
        stubs, width = pf._escape_stubs(
            pad,
            pad.net,
            fixed,
            kmax=256,
            net_class=net_class,
            extra_clearance=0.0,
            partner_net=None,
            layers=None,
            exempt_pads=None,
        )
        if width >= 2 * half - 1e-9:
            return []
        choices = []
        extra = max(0.0, half + clr - pf._agent_radius)
        for key, layer, points, length in stubs[:256]:
            if expired():
                return []
            if layer in hard or not all(
                pf._escape_boundary.segment_clear(a, b, width / 2)
                for a, b in zip(points, points[1:], strict=False)
            ):
                continue
            point = lattice.node_point(key)
            via = not hard.intersection({0, pf.num_layers - 1}) and pf._via_ok(
                key, pad.net, fixed, clearance=clr
            )
            for exit_layer in range(pf.num_layers):
                if exit_layer in hard or (exit_layer != layer and not via):
                    continue
                if (
                    not obstacles.node_blocked(key, exit_layer, pad.net)
                    and not obstacles.segment_blocked(point, point, exit_layer, pad.net, extra)
                    and (
                        pw is None
                        or not obstacles.pairwise_pad_blocked(
                            point, point, exit_layer, pad.net, half, extra, pw
                        )
                    )
                    and (
                        ko is None
                        or not ko.segment_blocked(point, point, exit_layer, pad.net, half)
                    )
                    and fixed.node_clear(point, exit_layer, pad.net, half, clr)
                    and pf._escape_boundary.segment_clear(point, point, half)
                ):
                    choices.append(
                        EscapeChoice(
                            key,
                            layer,
                            tuple(points),
                            length,
                            point,
                            exit_layer,
                            pad.net,
                            half,
                            width / 2,
                            clr,
                        )
                    )
        return sorted(choices, key=lambda c: (c.length, c.exit_layer, c.key))[:128]

    def occupancy(choice: EscapeChoice) -> CommittedCopper:
        copper = CommittedCopper(
            pf.num_layers,
            trace_half=pf._trace_half,
            clearance=pf.rules.trace_clearance,
            via_radius=pf.rules.via_diameter / 2,
            via_via_gap=pf._via_via_gap,
            same_net_via_gap=pf._same_net_via_gap,
            pairwise=pw,
        )
        choice.reserve(copper)
        return copper

    for ref, group in sorted(groups.items()):
        if expired():
            break
        if ref in ambiguous or len(group) != 2:
            continue
        (pad_a, class_a), (pad_b, class_b) = group.values()
        if pad_a.net == pad_b.net:
            continue
        aa, bb = options(pad_a, class_a), options(pad_b, class_b)
        if not aa or not bb:
            continue
        # Best-first traversal of the sorted pair-sum matrix avoids building
        # an unbounded Cartesian candidate list.
        heap = [(aa[0].length + bb[0].length, 0, 0)]
        seen = {(0, 0)}
        for _probe in range(4096):
            if not heap or expired():
                break
            _length, i, j = heapq.heappop(heap)
            a, b = aa[i], bb[j]
            if a.clears(occupancy(b)) and b.clears(occupancy(a)):
                # Reservations from earlier footprints are also hard copper.
                prior_a = foreign_reservations(fixed, plan, a.net)
                prior_b = foreign_reservations(fixed, plan, b.net)
                if a.clears(prior_a) and b.clears(prior_b):
                    plan[id(pad_a)], plan[id(pad_b)] = a, b
                    break
            for ni, nj in ((i + 1, j), (i, j + 1)):
                if ni < len(aa) and nj < len(bb) and (ni, nj) not in seen:
                    seen.add((ni, nj))
                    heapq.heappush(heap, (aa[ni].length + bb[nj].length, ni, nj))
    return plan
