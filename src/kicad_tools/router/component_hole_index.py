"""Indexed physical component drills, independent of net and copper layers."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .primitives import Pad

Hole = tuple[float, float, float, float, float]


class ComponentHoleIndex:
    def __init__(self) -> None:
        self.sources: list[Pad] = []
        self.known = True
        self._seen: set[Hole] = set()
        self._physical: dict[int, Pad] = {}
        self._ambiguous: set[tuple[str, str]] = set()
        self._anchors: dict[tuple[str, str], tuple[Pad, float, float, float]] = {}
        self.holes: list[tuple[float, float, float, float, float]] = []
        self.bins: dict[tuple[int, int], list[int]] = defaultdict(list)

    @staticmethod
    def buckets(x1: float, y1: float, x2: float, y2: float) -> Iterator[tuple[int, int]]:
        for x in range(math.floor(x1 / 2), math.floor(x2 / 2) + 1):
            for y in range(math.floor(y1 / 2), math.floor(y2 / 2) + 1):
                yield x, y

    @staticmethod
    def key(pad: Pad) -> tuple[str, str] | None:
        identity = getattr(pad, "component_key", None) or pad.ref
        return (str(identity), pad.pin) if identity and pad.pin else None

    def add(self, pad: Pad, *, physical: bool = True) -> Hole | None:
        self.sources.append(pad)
        key = self.key(pad)
        if physical:
            if key is not None and any(
                self.key(other) == key and source_id != id(pad)
                for source_id, other in self._physical.items()
            ):
                self._ambiguous.add(key)
            self._physical[id(pad)] = replace(pad)
        elif key is not None:
            if key in self._anchors and self._anchors[key][0] is not pad:
                self._ambiguous.add(key)
            self._anchors.setdefault(key, (pad, pad.x, pad.y, pad.rotation))
            if key not in self._ambiguous and any(
                self.key(p) == key for p in self._physical.values()
            ):
                return None
        return self._index(pad)

    def refreshed(self) -> ComponentHoleIndex:
        """Follow live copper-pad poses while retaining authored drill geometry.

        Physical census records can be separate objects from routing pads.
        Bind numbered holes to the live pad by reference/pin; unnumbered NPTH
        holes remain independent physical geometry, never routing targets.
        Ambiguous identities retain independent census and live-pad holes;
        one footprint must never drag another footprint's drill away.
        """
        result = ComponentHoleIndex()
        result.sources = self.sources.copy()
        result.known = self.known
        result._physical = self._physical.copy()
        result._anchors = self._anchors.copy()
        result._ambiguous = self._ambiguous.copy()
        physical_keys = {self.key(p) for p in self._physical.values()} - {None} - self._ambiguous
        for source in self.sources:
            key = self.key(source)
            if id(source) not in self._physical and key in physical_keys:
                continue
            pad = source
            if (
                id(source) in self._physical
                and key is not None
                and key not in self._ambiguous
                and key in self._anchors
            ):
                original = self._physical[id(source)]
                live, x, y, rotation = self._anchors[key]
                angle = live.rotation - rotation
                c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
                dx, dy = original.x - x, original.y - y
                pad = replace(
                    original,
                    x=live.x + c * dx + s * dy,
                    y=live.y - s * dx + c * dy,
                    drill_rotation=original.drill_rotation + angle,
                )
            result._index(pad)
        return result

    def _index(self, pad: Pad) -> Hole | None:
        if not pad.through_hole:
            return None
        width, height = pad.drill_size or (pad.drill, pad.drill)
        if min(width, height) <= 0:
            return None
        angle = math.radians(pad.drill_rotation)
        half = abs(width - height) / 2
        dx, dy = (half, 0) if width >= height else (0, half)
        # KiCad board-space rotation is clockwise.
        ax = dx * math.cos(angle) + dy * math.sin(angle)
        ay = -dx * math.sin(angle) + dy * math.cos(angle)
        hole = (pad.x - ax, pad.y - ay, pad.x + ax, pad.y + ay, min(width, height) / 2)
        if hole in self._seen:
            return None
        self._seen.add(hole)
        x1, y1, x2, y2, radius = hole
        index = len(self.holes)
        self.holes.append(hole)
        for bucket in self.buckets(
            min(x1, x2) - radius, min(y1, y2) - radius, max(x1, x2) + radius, max(y1, y2) + radius
        ):
            self.bins[bucket].append(index)
        return hole

    def clear(self, x: float, y: float, drill: float, clearance: float) -> bool:
        if not self.known:
            return False
        reach = drill / 2 + clearance
        seen = set()
        for bucket in self.buckets(x - reach, y - reach, x + reach, y + reach):
            for index in self.bins.get(bucket, ()):
                if index in seen:
                    continue
                seen.add(index)
                x1, y1, x2, y2, radius = self.holes[index]
                dx, dy = x2 - x1, y2 - y1
                length2 = dx * dx + dy * dy
                t = max(0, min(1, ((x - x1) * dx + (y - y1) * dy) / length2)) if length2 else 0
                if (
                    math.hypot(x - x1 - t * dx, y - y1 - t * dy) - radius - drill / 2
                    < clearance - 1e-4
                ):
                    return False
        return True
