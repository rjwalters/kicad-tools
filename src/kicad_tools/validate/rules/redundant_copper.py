"""Redundant / avoidable copper advisory (Issue #8218).

Cosmetic-but-avoidable copper passes every sign-off gate and ships.  This
rule *reports* four patterns (always ``severity="warning"``, never an
error), each with a location:

``redundant_copper_duplicate``
    Exact-duplicate segments -- the same predicate ``PCB.dedupe_copper``
    removes (net + layer + width + unordered endpoints, ~1 micron).
    One warning per *extra* copy, so a board with 15 duplicates reports 15.

``redundant_copper_fragments``
    A run of >= ``FRAGMENT_RUN_MIN`` consecutive same-net, same-layer
    segments each shorter than ``FRAGMENT_MAX_MM`` joined end to end
    (the 0.07-0.09 mm fragment case).

``redundant_copper_detour``
    A chain of segments (joined through degree-2 nodes) whose routed
    length exceeds ``DETOUR_RATIO`` times the straight distance between
    its ends, and is longer than ``DETOUR_MIN_LENGTH_MM``.  Known
    limitation: intentional length-tuning meanders (match groups, diff
    pairs) are also reported; treat those as expected advisories.

``redundant_copper_via_offcentre``
    A same-net track end that lands ``OFFCENTRE_MIN_MM`` to
    ``OFFCENTRE_MAX_MM`` from a via centre (on a layer the via spans),
    connecting to it only by overlap.  Ends shared with another segment
    (joints, bends near the via) and layers where a track already ends on
    the via centre are not flagged.

``redundant_copper_covered``
    A segment whose both ends lie on the centreline of a strictly longer,
    no-narrower, same-net, same-layer collinear segment (~1 micron), i.e.
    it is wholly hidden under the longer one.  Exact duplicates are only
    reported as ``redundant_copper_duplicate``.  Repair with
    ``kct pcb prune`` (connectivity-guarded), not ``dedupe``.

Redundant *branches* and the repair passes are the scope of #8216; this
rule only reports.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from kicad_tools.core.layers import via_spans_layer

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB

#: Segments shorter than this (mm) count as fragments.
FRAGMENT_MAX_MM = 0.1
#: Minimum number of consecutive fragments reported as a run.
FRAGMENT_RUN_MIN = 3
#: Routed length / straight distance above which a chain is a detour.
DETOUR_RATIO = 2.0
#: Detours shorter than this routed length (mm) are ignored.
DETOUR_MIN_LENGTH_MM = 2.0
#: Off-centre window (mm) for a track end vs. a same-net via centre.
OFFCENTRE_MIN_MM = 0.1
OFFCENTRE_MAX_MM = 0.4

#: Tolerance (mm) for "lies on the centreline" in the covered check.
COVERED_TOL_MM = 0.001

_QUANT = 3


def _point_seg_dist(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    """Distance from point ``p`` to segment ``a``-``b``."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    len2 = dx * dx + dy * dy
    if len2 == 0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _q(point: tuple[float, float]) -> tuple[float, float]:
    return (round(point[0], _QUANT), round(point[1], _QUANT))


class RedundantCopperRule(DRCRule):
    """Report avoidable copper: duplicates, fragments, detours, off-centre ends."""

    rule_id = "redundant_copper"
    name = "Redundant Copper"
    description = (
        "Reports exact-duplicate segments, runs of very short segments, "
        "long detours and track ends off a via centre (advisory)."
    )

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        results = DRCResults()
        results.rules_checked = 1
        for rid in (
            "redundant_copper_duplicate",
            "redundant_copper_fragments",
            "redundant_copper_detour",
            "redundant_copper_via_offcentre",
            "redundant_copper_covered",
        ):
            results.rules_checked_by_rule[rid] = 1

        segments = [s for s in pcb.segments if s.layer and s.width > 0]
        self._check_duplicates(segments, results)

        groups: dict[tuple[int, str], list[Any]] = defaultdict(list)
        for seg in segments:
            groups[(seg.net_number, seg.layer)].append(seg)
        for (_net, _layer), segs in groups.items():
            self._check_chains(segs, results)
            self._check_covered(segs, results)

        self._check_via_offcentre(pcb, segments, results)
        return results

    # ------------------------------------------------------------------

    @staticmethod
    def _net_label(seg: Any) -> str:
        return seg.net_name or (f"net {seg.net_number}" if seg.net_number else "unassigned")

    def _check_duplicates(self, segments: list[Any], results: DRCResults) -> None:
        seen: set[tuple[Any, ...]] = set()
        for seg in segments:
            a, b = _q(seg.start), _q(seg.end)
            key = (
                seg.net_number,
                seg.layer,
                (a, b) if a <= b else (b, a),
                round(seg.width, _QUANT),
            )
            if key not in seen:
                seen.add(key)
                continue
            label = self._net_label(seg)
            results.add(
                DRCViolation(
                    rule_id="redundant_copper_duplicate",
                    severity="warning",
                    message=(
                        f"Duplicate track segment: [{label}] on {seg.layer}, "
                        f"width {seg.width:.3f} mm (removable with `kct pcb dedupe`)"
                    ),
                    location=seg.start,
                    layer=seg.layer,
                    nets=(label,),
                )
            )

    def _check_covered(self, segs: list[Any], results: DRCResults) -> None:
        """Flag segments wholly lying on a longer collinear same-net segment."""
        for seg in segs:
            length = math.dist(seg.start, seg.end)
            for other in segs:
                if other is seg or other.width < seg.width - 1e-9:
                    continue
                # Strictly longer: equal-length containers are duplicates
                # (reported elsewhere) and cannot hide a longer segment.
                if math.dist(other.start, other.end) <= length + COVERED_TOL_MM:
                    continue
                if all(
                    _point_seg_dist(p, other.start, other.end) <= COVERED_TOL_MM
                    for p in (seg.start, seg.end)
                ):
                    label = self._net_label(seg)
                    results.add(
                        DRCViolation(
                            rule_id="redundant_copper_covered",
                            severity="warning",
                            message=(
                                f"Track segment lies wholly on a longer collinear "
                                f"segment: [{label}] on {seg.layer} "
                                f"(removable with `kct pcb prune`)"
                            ),
                            location=seg.start,
                            layer=seg.layer,
                            nets=(label,),
                        )
                    )
                    break

    def _check_chains(self, segs: list[Any], results: DRCResults) -> None:
        """Fragment-run and detour detection over same-net/layer chains."""
        # Drop exact duplicates so they don't distort node degrees.
        unique: dict[tuple[Any, ...], Any] = {}
        for seg in segs:
            a, b = _q(seg.start), _q(seg.end)
            if a == b:
                continue
            unique.setdefault((a, b) if a <= b else (b, a), seg)
        edges = list(unique.items())
        if not edges:
            return
        node_edges: dict[tuple[float, float], list[int]] = defaultdict(list)
        for i, ((a, b), _s) in enumerate(edges):
            node_edges[a].append(i)
            node_edges[b].append(i)

        visited: set[int] = set()
        for start_idx in range(len(edges)):
            if start_idx in visited:
                continue
            chain = self._walk_chain(start_idx, edges, node_edges, visited)
            self._report_chain(chain, results)

    @staticmethod
    def _walk_chain(
        start_idx: int,
        edges: list[Any],
        node_edges: dict[tuple[float, float], list[int]],
        visited: set[int],
    ) -> list[tuple[Any, tuple[float, float], tuple[float, float]]]:
        """Maximal path through degree-2 nodes, as (segment, from, to) steps."""

        def extend(idx: int, frm: tuple[float, float]) -> list[Any]:
            out = []
            cur, node = idx, frm
            while True:
                (a, b), seg = edges[cur]
                nxt_node = b if node == a else a
                out.append((seg, node, nxt_node))
                others = [e for e in node_edges[nxt_node] if e != cur]
                if len(node_edges[nxt_node]) != 2 or not others or others[0] in visited:
                    return out
                visited.add(others[0])
                cur, node = others[0], nxt_node

        visited.add(start_idx)
        (a, b), seg = edges[start_idx]
        forward = extend(start_idx, a)
        # Extend backwards from a (through the start edge's other end).
        backward: list[Any] = []
        if len(node_edges[a]) == 2:
            others = [e for e in node_edges[a] if e != start_idx]
            if others and others[0] not in visited:
                visited.add(others[0])
                backward = extend(others[0], a)
        back_steps = [(s, to, frm) for (s, frm, to) in reversed(backward)]
        return back_steps + forward

    def _report_chain(self, chain: list[Any], results: DRCResults) -> None:
        if not chain:
            return
        first_seg = chain[0][0]
        label = self._net_label(first_seg)
        layer = first_seg.layer

        # Fragment runs: consecutive short segments.
        run: list[Any] = []
        for step in [*chain, None]:
            seg = step[0] if step else None
            if seg is not None and math.dist(seg.start, seg.end) < FRAGMENT_MAX_MM:
                run.append(seg)
                continue
            if len(run) >= FRAGMENT_RUN_MIN:
                results.add(
                    DRCViolation(
                        rule_id="redundant_copper_fragments",
                        severity="warning",
                        message=(
                            f"Run of {len(run)} very short track segments "
                            f"(< {FRAGMENT_MAX_MM} mm each): [{label}] on {layer}"
                        ),
                        location=run[0].start,
                        layer=layer,
                        actual_value=float(len(run)),
                        required_value=float(FRAGMENT_RUN_MIN - 1),
                        nets=(label,),
                    )
                )
            run = []

        # Detour: routed length vs straight distance between chain ends.
        routed = sum(math.dist(s.start, s.end) for s, _f, _t in chain)
        ends = (chain[0][1], chain[-1][2])
        straight = math.dist(*ends)
        if len(chain) >= 2 and routed >= DETOUR_MIN_LENGTH_MM and straight > 0:
            ratio = routed / straight
            if ratio > DETOUR_RATIO:
                results.add(
                    DRCViolation(
                        rule_id="redundant_copper_detour",
                        severity="warning",
                        message=(
                            f"Track detour: [{label}] on {layer} routes {routed:.2f} mm "
                            f"between points {straight:.2f} mm apart (ratio {ratio:.1f})"
                        ),
                        location=ends[0],
                        layer=layer,
                        actual_value=ratio,
                        required_value=DETOUR_RATIO,
                        nets=(label,),
                    )
                )

    def _check_via_offcentre(self, pcb: PCB, segments: list[Any], results: DRCResults) -> None:
        # Endpoint multiplicity per (net, layer, point): an end shared with
        # another segment is a normal joint (e.g. a bend inside the via
        # entry), not a track that connects "by overlap only".
        ends: dict[tuple[int, str, tuple[float, float]], int] = defaultdict(int)
        for seg in segments:
            ends[(seg.net_number, seg.layer, _q(seg.start))] += 1
            ends[(seg.net_number, seg.layer, _q(seg.end))] += 1

        for via in pcb.vias:
            vx, vy = via.position
            candidates = [
                s
                for s in segments
                if s.net_number == via.net_number and via_spans_layer(via.layers, s.layer)
            ]
            # Layers where a track already terminates on the via centre are
            # properly connected; skip off-centre ends on those layers.
            centred_layers = {
                s.layer
                for s in candidates
                for end in (s.start, s.end)
                if math.dist(end, (vx, vy)) < OFFCENTRE_MIN_MM / 10
            }
            for seg in candidates:
                if seg.layer in centred_layers:
                    continue
                for end in (seg.start, seg.end):
                    d = math.dist(end, (vx, vy))
                    if not OFFCENTRE_MIN_MM <= d <= OFFCENTRE_MAX_MM:
                        continue
                    if ends[(seg.net_number, seg.layer, _q(end))] > 1:
                        continue
                    label = self._net_label(seg)
                    results.add(
                        DRCViolation(
                            rule_id="redundant_copper_via_offcentre",
                            severity="warning",
                            message=(
                                f"Track end {d:.3f} mm off via centre: [{label}] "
                                f"on {seg.layer} (connects by overlap only)"
                            ),
                            location=end,
                            layer=seg.layer,
                            actual_value=d,
                            nets=(label,),
                        )
                    )
