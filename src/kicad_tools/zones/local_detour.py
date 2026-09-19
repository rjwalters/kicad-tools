"""Bounded, non-mutating power escapes that replace a local signal path."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from shapely.geometry import LineString, Point, box  # type: ignore[import-untyped]
from shapely.ops import unary_union  # type: ignore[import-untyped]

from .pour_escape import Escape, EscapeRules, find_escape

Point2D = tuple[float, float]


@dataclass(frozen=True)
class Track:
    """One identified track, in the same coordinate frame as the obstacles."""

    uuid: str
    net: str
    layer: str
    start: Point2D
    end: Point2D
    width: float

    @property
    def copper(self) -> Any:
        return LineString((self.start, self.end)).buffer(self.width / 2)


@dataclass(frozen=True)
class LocalDetour:
    """A candidate only: the caller must tune, validate and commit atomically."""

    signal_net: str
    removed_tracks: tuple[str, ...]
    power: Escape
    # Each escape ends in a new via iff escape.via is true.
    signal_paths: tuple[tuple[str, Escape], ...]


def _path_ends(cut: Sequence[Track]) -> tuple[Point2D, Point2D] | None:
    """Reject branches, cycles, and a path accompanied by a disjoint cycle."""
    degree: Counter[Point2D] = Counter()
    neighbors: dict[Point2D, set[Point2D]] = defaultdict(set)
    for track in cut:
        a, b = track.start, track.end
        if a == b:
            return None
        degree.update((a, b))
        neighbors[a].add(b)
        neighbors[b].add(a)
    ends = sorted(point for point, count in degree.items() if count == 1)
    if len(ends) != 2 or any(count > 2 for count in degree.values()):
        return None
    seen = {ends[0]}
    pending = [ends[0]]
    while pending:
        for neighbor in neighbors[pending.pop()] - seen:
            seen.add(neighbor)
            pending.append(neighbor)
    return (ends[0], ends[1]) if len(seen) == len(degree) else None


def _anchored_ends(cut, retained, pads, vias, ends, net, layer) -> bool:
    """Keep all external contacts at the two anchored cut terminals."""
    copper = unary_union([track.copper for track in cut])
    terminal_area = unary_union(
        [Point(point).buffer(2 * max(t.width for t in cut) + 0.001) for point in ends]
    )
    anchors = [track.copper for track in retained if track.net == net and track.layer == layer]
    anchors += [entry[0] for entry in pads if entry[1] == net and layer in entry[2]]
    # Existing via layer spans are not modeled by this planner's obstacle
    # interface. Decline cuts through them rather than sever an unseen leg.
    if any(
        name == net and copper.intersects(point.buffer(radius)) for point, name, radius, _ in vias
    ):
        return False
    if not all(any(geom.distance(Point(end)) < 1e-7 for geom in anchors) for end in ends):
        return False
    return all(
        not copper.intersects(anchor)
        or copper.intersection(anchor).difference(terminal_area).is_empty
        for anchor in anchors
    )


def plan_local_detour(
    *,
    start: Point2D,
    net: str,
    layer: str,
    pads: Sequence[tuple],
    tracks: Sequence[Track],
    vias: Sequence[tuple],
    primary: Sequence[tuple],
    bounds: tuple[float, float, float, float],
    rules: EscapeRules,
    signal_rules: Mapping[str, EscapeRules],
    alternate_layer: str = "B.Cu",
    radius: float = 2.0,
    node_budget: int = 200_000,
    max_candidates: int = 3,
) -> LocalDetour | None:
    """Plan an escape by replacing one simple, anchored signal fragment.

    Only nets explicitly supplied in signal_rules are eligible. Track UUIDs
    identify removals and obstacles; no bounding-box identity matching is used.
    There are at most five searches per candidate, sharing its node budget
    equally. No input copper is changed and failed searches return no plan.
    """
    if not math.isfinite(radius) or radius <= 0 or node_budget < 5 or max_candidates < 1:
        raise ValueError("Local recovery requires positive radius and bounded search budgets")
    if alternate_layer == layer:
        raise ValueError("The alternate layer must differ from the original layer")
    ids = [track.uuid for track in tracks]
    if len(ids) != len(set(ids)) or any(not uid for uid in ids):
        raise ValueError("Tracks must have unique nonempty identifiers")
    nearby = [
        track
        for track in tracks
        if track.net != net
        and track.net in signal_rules
        and track.layer == layer
        and track.copper.distance(Point(start)) <= radius
    ]
    candidates = sorted(
        {track.net for track in nearby},
        key=lambda name: (
            min(t.copper.distance(Point(start)) for t in nearby if t.net == name),
            name,
        ),
    )[:max_candidates]
    per_search = node_budget // 5
    for name in candidates:
        cut = [track for track in nearby if track.net == name]
        ends = _path_ends(cut)
        widths = {track.width for track in cut}
        if ends is None or len(widths) != 1:
            continue
        width = next(iter(widths))
        if width < signal_rules[name].width:
            continue  # Do not silently widen authored impedance geometry.
        removed = {track.uuid for track in cut}
        retained = [track for track in tracks if track.uuid not in removed]
        if not _anchored_ends(cut, retained, pads, vias, ends, name, layer):
            continue
        segments = [(track.copper, track.net, track.layer) for track in retained]
        candidate_vias = list(vias)
        common = {"pads": pads, "segments": segments, "vias": candidate_vias, "bounds": bounds}
        power = find_escape(
            start, net, layer, primary=primary, rules=rules, node_budget=per_search, **common
        )
        if power is None:
            continue
        segments.extend(
            (LineString((a, b)).buffer(rules.width / 2), net, layer)
            for a, b in zip(power.points, power.points[1:], strict=False)
        )
        if power.via:
            candidate_vias.append((Point(power.points[-1]), net, rules.diameter / 2, rules.drill))
        signal = replace(signal_rules[name], width=width)
        first, last = sorted(ends, key=lambda point: math.dist(point, start))
        direct = find_escape(
            first,
            name,
            layer,
            primary=[(Point(last).buffer(width / 2), {layer}, "terminal")],
            rules=signal,
            node_budget=per_search,
            **common,
        )
        if direct is not None:
            return LocalDetour(name, tuple(sorted(removed)), power, ((layer, direct),))
        escapes = []
        for origin in (first, last):
            # Synthetic plane discovers a legal via location, not a completed
            # connection. A separate search below must join both new vias.
            escape = find_escape(
                origin,
                name,
                layer,
                primary=[(box(*bounds), {alternate_layer}, "via search")],
                rules=signal,
                node_budget=per_search,
                **common,
            )
            if escape is None:
                break
            escapes.append(escape)
            segments.extend(
                (LineString((a, b)).buffer(width / 2), name, layer)
                for a, b in zip(escape.points, escape.points[1:], strict=False)
            )
            candidate_vias.append(
                (Point(escape.points[-1]), name, signal.diameter / 2, signal.drill)
            )
        if len(escapes) != 2:
            continue
        back = find_escape(
            escapes[0].points[-1],
            name,
            alternate_layer,
            primary=[
                (
                    Point(escapes[1].points[-1]).buffer(signal.diameter / 2),
                    {alternate_layer},
                    "target via",
                )
            ],
            rules=signal,
            node_budget=per_search,
            **common,
        )
        if back is not None:
            return LocalDetour(
                name,
                tuple(sorted(removed)),
                power,
                ((layer, escapes[0]), (layer, escapes[1]), (alternate_layer, back)),
            )
    return None


@dataclass(frozen=True)
class TuningAdjustment:
    net: str
    removed_track: str
    layer: str
    replacement: Escape


@dataclass(frozen=True)
class PairMatch:
    skew_mm: float
    adjustment: TuningAdjustment | None = None


def match_detour_pair(
    plan: LocalDetour,
    *,
    power_net: str,
    power_layer: str,
    partner_net: str,
    tracks: Sequence[Track],
    pads: Sequence[tuple],
    vias: Sequence[tuple],
    initial_lengths_mm: Mapping[str, float],
    new_via_length_mm: float,
    tolerance_mm: float,
    bounds: tuple[float, float, float, float],
    rules: EscapeRules,
) -> PairMatch | None:
    """Match the changed pair using at most 384 checked meander candidates.

    The caller supplies complete measured lengths, including existing vias,
    and the actual drilled length of each new ordinary through via. Unchanged
    copper is retained; only a straight segment's middle receives a meander.
    No feasible insertion returns None, so the whole detour can be rejected.
    """
    values = (*initial_lengths_mm.values(), new_via_length_mm, tolerance_mm)
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("Pair lengths and tolerance must be finite and nonnegative")
    if partner_net == plan.signal_net:
        raise ValueError("A pair requires two different nets")
    removed = set(plan.removed_tracks)
    original = [track for track in tracks if track.uuid in removed]
    if len(original) != len(removed) or any(t.net != plan.signal_net for t in original):
        raise ValueError("The planned cut no longer matches the input tracks")
    old = sum(math.dist(t.start, t.end) for t in original)
    added = sum(
        sum(math.dist(a, b) for a, b in zip(path.points, path.points[1:], strict=False))
        + (new_via_length_mm if path.via else 0)
        for _, path in plan.signal_paths
    )
    lengths = {
        plan.signal_net: initial_lengths_mm[plan.signal_net] - old + added,
        partner_net: initial_lengths_mm[partner_net],
    }
    delta = abs(lengths[plan.signal_net] - lengths[partner_net])
    if delta <= tolerance_mm:
        return PairMatch(delta)
    shorter = min(lengths, key=lambda net: lengths[net])
    retained = [t for t in tracks if t.uuid not in removed]
    planned = [(power_layer, power_net, plan.power)] + [
        (layer, plan.signal_net, path) for layer, path in plan.signal_paths
    ]
    extra_segments = [
        (LineString((a, b)).buffer(path.rules.width / 2), layer)
        for layer, _, path in planned
        for a, b in zip(path.points, path.points[1:], strict=False)
    ]
    all_vias = list(vias) + [
        (Point(path.points[-1]), net, path.rules.diameter / 2, path.rules.drill)
        for _, net, path in planned
        if path.via
    ]
    choices = sorted(
        (
            t
            for t in retained
            if t.net == shorter
            and t.width >= rules.width
            and (t.start[0] == t.end[0] or t.start[1] == t.end[1])
        ),
        key=lambda t: (-math.dist(t.start, t.end), t.uuid),
    )[:8]
    board_area = box(*bounds)
    for track in choices:
        distance = math.dist(track.start, track.end)
        if distance <= 4.5:
            continue
        blockers = [
            t.copper.buffer(rules.clearance + 0.001)
            for t in retained
            if t.uuid != track.uuid and t.layer == track.layer
        ]
        blockers += [
            g.buffer(rules.clearance + 0.001) for g, layer in extra_segments if layer == track.layer
        ]
        blockers += [
            entry[0].buffer(rules.clearance + 0.001) for entry in pads if track.layer in entry[2]
        ]
        blockers += [
            point.buffer(max(radius + rules.clearance, drill / 2 + rules.hole_copper) + 0.001)
            for point, _, radius, drill in all_vias
        ]
        blocked = unary_union(blockers)
        ux = (track.end[0] - track.start[0]) / distance
        uy = (track.end[1] - track.start[1]) / distance
        for sign in (1, -1):
            amplitude = round(delta / 2, 6) * sign
            for index in range(24):
                offset = 2 + index * 0.5
                if offset + 2 >= distance - 0.5:
                    break
                a = (track.start[0] + ux * offset, track.start[1] + uy * offset)
                b = (a[0] - uy * amplitude, a[1] + ux * amplitude)
                c = (b[0] + ux * 2, b[1] + uy * 2)
                d = (a[0] + ux * 2, a[1] + uy * 2)
                points = (a, b, c, d)
                copper = LineString(points).buffer(track.width / 2)
                if not board_area.covers(copper) or copper.intersects(blocked):
                    continue
                residual = abs(delta - 2 * abs(amplitude))
                if residual > tolerance_mm:
                    continue
                replacement = Escape(
                    (track.start, *points, track.end), False, replace(rules, width=track.width)
                )
                return PairMatch(
                    residual, TuningAdjustment(shorter, track.uuid, track.layer, replacement)
                )
    return None
