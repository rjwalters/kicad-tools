"""Validity-aware "nogood" cache for negotiated-routing retries (Issue #6353).

Experiment-only, opt-in, default OFF.  A *nogood* is a net-routing attempt that
failed completely (no route committed, no timeout) together with a fingerprint
of the copper the attempt could see.  If the same attempt is requested again
while that fingerprint is unchanged, the search is deterministic and will fail
the same way, so the retry can be answered from the cache.

Independent derivation (no upstream code consulted or adapted):

* **Key** -- what the attempt asks: net id, endpoint coordinates/layers/nets of
  every terminal (in order), the ``reverse_search`` flag, and (by default) the
  present-cost factor.  Anything that changes the question is a different key.
* **Failure window** -- grid-cell bounding box of all terminals inflated by
  ``margin_cells`` (``None`` = the whole grid, which is the provably
  conservative setting).  Only copper inside the window is fingerprinted.
* **Validity signal** -- a BLAKE2b digest of the ``_blocked`` and ``_net``
  occupancy planes (all layers) inside the window.  Any add/remove/net-change of
  copper in the window changes the digest, so a stale entry is *invalidated*
  (dropped and counted) instead of served.  A fast path: if the grid's global
  ``occupancy_generation`` is unchanged since insertion, nothing at all changed.
* **Soundness caveat** -- a bounded window can miss copper outside it that would
  open a detour.  That is a *false hit* (loses routability) and is exactly what
  the experiment measures; ``margin_cells=None`` cannot false-hit on occupancy.
* **Bounded** -- LRU with ``max_entries``.  Eviction only ever causes a miss
  (more work), never a different answer.
* **Deterministic** -- no wall-clock, no randomness; counters depend only on the
  call sequence.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

ENV_ENABLE = "KCT_NOGOOD_CACHE"
ENV_MARGIN = "KCT_NOGOOD_CACHE_MARGIN_CELLS"
ENV_MAX = "KCT_NOGOOD_CACHE_MAX_ENTRIES"
ENV_PF = "KCT_NOGOOD_CACHE_IGNORE_PF"
ENV_STATS_FILE = "KCT_NOGOOD_CACHE_STATS_FILE"


@dataclass
class NogoodStats:
    hits: int = 0
    misses: int = 0
    insertions: int = 0
    invalidations: int = 0
    evictions: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "insertions": self.insertions,
            "invalidations": self.invalidations,
            "evictions": self.evictions,
        }


@dataclass
class _Entry:
    fingerprint: bytes
    generation: int
    payload: Any = field(default=None)


class NogoodCache:
    """Bounded LRU map ``key -> (window fingerprint, payload)``."""

    def __init__(self, max_entries: int = 256, margin_cells: int | None = None) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self.max_entries = max_entries
        self.margin_cells = margin_cells
        self.stats = NogoodStats()
        self._entries: OrderedDict[Hashable, _Entry] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    # -- key / window / fingerprint -------------------------------------
    @staticmethod
    def make_key(
        net: int,
        terminals: Sequence[tuple[float, float, str, int]],
        reverse_search: bool,
        present_cost_factor: float | None,
    ) -> tuple:
        pf = None if present_cost_factor is None else round(float(present_cost_factor), 6)
        return (
            int(net),
            tuple((round(x, 6), round(y, 6), str(layer), int(n)) for x, y, layer, n in terminals),
            bool(reverse_search),
            pf,
        )

    def window(self, grid: Any, points: Sequence[tuple[float, float]]) -> tuple[int, int, int, int]:
        """Return ``(x0, y0, x1, y1)`` inclusive-exclusive cell bounds."""
        cells = [grid.world_to_grid(x, y) for x, y in points]
        if self.margin_cells is None or not cells:
            return (0, 0, grid.cols, grid.rows)
        m = self.margin_cells
        xs = [c[0] for c in cells]
        ys = [c[1] for c in cells]
        return (
            max(0, min(xs) - m),
            max(0, min(ys) - m),
            min(grid.cols, max(xs) + m + 1),
            min(grid.rows, max(ys) + m + 1),
        )

    @staticmethod
    def fingerprint(grid: Any, win: tuple[int, int, int, int]) -> bytes:
        x0, y0, x1, y1 = win
        h = hashlib.blake2b(digest_size=16)
        h.update(repr(win).encode())
        for plane in (grid._blocked, grid._net):
            arr = np.ascontiguousarray(np.asarray(plane)[:, y0:y1, x0:x1])
            h.update(arr.tobytes())
        return h.digest()

    # -- operations ------------------------------------------------------
    def lookup(self, key: Hashable, grid: Any, win: tuple[int, int, int, int]) -> Any | None:
        """Return the stored payload on a valid hit, else ``None``."""
        entry = self._entries.get(key)
        if entry is None:
            self.stats.misses += 1
            return None
        if entry.generation != grid.occupancy_generation:
            if self.fingerprint(grid, win) != entry.fingerprint:
                del self._entries[key]
                self.stats.invalidations += 1
                self.stats.misses += 1
                return None
            entry.generation = grid.occupancy_generation  # revalidated
        self._entries.move_to_end(key)
        self.stats.hits += 1
        return entry.payload

    def insert(
        self, key: Hashable, grid: Any, win: tuple[int, int, int, int], payload: Any
    ) -> None:
        if key in self._entries:
            del self._entries[key]
        self._entries[key] = _Entry(self.fingerprint(grid, win), grid.occupancy_generation, payload)
        self.stats.insertions += 1
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
            self.stats.evictions += 1


def nogood_cache_from_env(env: dict[str, str] | None = None) -> NogoodCache | None:
    """Build a cache from ``KCT_NOGOOD_CACHE*`` env vars, or ``None`` (default)."""
    import os

    e = os.environ if env is None else env
    if e.get(ENV_ENABLE, "").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    raw = e.get(ENV_MARGIN, "").strip().lower()
    margin = None if raw in {"", "full", "none"} else int(raw)
    cache = NogoodCache(max_entries=int(e.get(ENV_MAX, "256") or 256), margin_cells=margin)
    stats_file = e.get(ENV_STATS_FILE, "").strip()
    if stats_file:
        import atexit
        import json

        def _dump() -> None:  # experiment diagnostics only
            with open(stats_file, "a") as fh:
                fh.write(json.dumps(cache.stats.as_dict()) + "\n")

        atexit.register(_dump)
    return cache
