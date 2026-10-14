"""Validity-aware "nogood" cache for negotiated-routing retries (Issue #6353).

Experiment-only, opt-in, default OFF.  A *nogood* is a net-routing attempt that
failed completely (no route committed) with a *definitive* search failure,
stored together with a fingerprint of the grid state the attempt could read.
If the same attempt is requested again while that fingerprint is unchanged, the
search would read the same inputs and fail the same way, so the retry can be
answered from the cache.

Independent derivation (no upstream code consulted or adapted):

* **Key** -- what the attempt asks: net id, endpoint coordinates/layers/nets of
  every terminal (in order), the ``reverse_search`` flag, the present-cost
  factor, and a ``context`` tuple the caller fills with the search-budget
  settings (iteration caps, relief mode, whether a wall-clock budget is set).
  Anything that changes the question is a different key.  Layer sets and
  design rules are *not* in the key; they are fixed for a router instance.
* **What may be cached** (:func:`search_failure_is_definitive`) -- only a
  failure whose search *drained its open set*: no wall-clock timeout, no
  iteration cap, no post-route-validation resume exhaustion, no commit refusal.
  A budget-limited failure can turn into a success under different costs within
  the same budget, so it is never inserted.
* **Failure window** -- grid-cell bounding box of all terminals inflated by
  ``margin_cells``.  ``None`` (the default, ``full``) is the whole grid.
* **Validity signal** -- a BLAKE2b digest, taken on every lookup, of **every**
  numpy plane on the grid (``_blocked``, ``_net``, ``_usage_count``,
  ``_history_cost``, ``_present_cost_ema``, ``_is_obstacle``, ``_is_zone``, pad
  copper, ...; ``_saved*`` rip-up snapshots excluded) plus the stored-route
  halo registry and an optional caller-supplied ``extra`` state (the C++
  wrapper's reservation / fixed-fill / keepout / net-map stamp).  Cost planes
  are included on purpose: they change which candidate paths the search
  validates first.  There is no ``occupancy_generation`` fast path, because
  history and usage changes do not bump it.
* **Soundness** -- with ``margin_cells=None`` a hit is served only when the key
  and the whole-grid state the search reads are unchanged and the stored
  failure was a drained search, so a re-run would fail identically.  A bounded
  window (``margin_cells=N``) slices the per-cell planes to the window and can
  miss a change outside it that opens a detour: that *false hit* is a heuristic
  the experiment measures, not a sound mode.  ``KCT_NOGOOD_CACHE_IGNORE_PF``
  drops the present-cost factor from the key and is deliberately unsound.
* **Cost** -- the whole-grid digest is recomputed on every lookup that finds an
  entry and on every insertion (no per-attempt incremental hashing).
* **Bounded** -- LRU with ``max_entries``.  Eviction only ever causes a miss
  (more work), never a different answer.
* **Deterministic** -- no wall-clock, no randomness; counters depend only on the
  call sequence.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

ENV_ENABLE = "KCT_NOGOOD_CACHE"
ENV_MARGIN = "KCT_NOGOOD_CACHE_MARGIN_CELLS"
ENV_MAX = "KCT_NOGOOD_CACHE_MAX_ENTRIES"
ENV_PF = "KCT_NOGOOD_CACHE_IGNORE_PF"
ENV_STATS_FILE = "KCT_NOGOOD_CACHE_STATS_FILE"

# ``FAILURE_*`` codes (``cpp/include/types.hpp``) that mean the C++ open set
# drained: NO_PATH, VIA_VIA_BLOCKED, PAIRWISE_BLOCKED.  ITERATION_LIMIT (2) and
# TIMEOUT (3) are budget give-ups and are never cacheable.
DRAINED_FAILURE_REASONS = frozenset({1, 5, 7})


def reset_search_exhausted(router: Any) -> None:
    """Clear the Python pathfinder's ``last_route_exhausted`` flag(s).

    Called before an attempt and after each recorded failure so that
    :func:`search_failure_is_definitive` only sees a flag set by the search
    that just failed.  Only runs when the cache is enabled.
    """
    for target in (router, getattr(router, "_py_router", None)):
        if target is not None and hasattr(target, "last_route_exhausted"):
            target.last_route_exhausted = False


def search_failure_is_definitive(router: Any) -> bool:
    """Return ``True`` only if the most recent failed search drained its open set.

    * Pure-Python ``Router``: ``last_route_exhausted`` (open set empty, no goal
      rejected) -- ``False`` after a timeout ``break`` or the iteration cap.
    * C++ ``CppPathfinder``: the C++ failure reason must be a drained one
      (:data:`DRAINED_FAILURE_REASONS`) **and** the Python fallback must have
      run and drained too.  A skipped fallback (timeout, tuned iteration cap,
      repeated resume exhaustion, #6098 memo) or a fallback that hit its own
      budget is not a proof, so the failure is not cached.
    * Anything else (a commit refused by ``mark_route``, an unknown router):
      ``False``.
    """
    if hasattr(router, "_try_python_fallback"):
        get_info = getattr(router, "get_last_failure_info", None)
        info = get_info() if get_info is not None else None
        if not info or int(info.get("failure_reason") or 0) not in DRAINED_FAILURE_REASONS:
            return False
        py = getattr(router, "_py_router", None)
        return py is not None and getattr(py, "last_route_exhausted", False) is True
    return getattr(router, "last_route_exhausted", False) is True


def search_budget_context(router: Any, per_net_timeout: float | None) -> tuple:
    """Budget settings that shape a search, for the cache key."""
    return (
        getattr(router, "_effective_search_iterations", None),
        getattr(router, "_per_net_iteration_cap_active", None),
        getattr(router, "_max_iterations_override", None),
        getattr(router, "_relief_mode", None),
        per_net_timeout is not None,
    )


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
        context: tuple = (),
    ) -> tuple:
        pf = None if present_cost_factor is None else round(float(present_cost_factor), 6)
        return (
            int(net),
            tuple((round(x, 6), round(y, 6), str(layer), int(n)) for x, y, layer, n in terminals),
            bool(reverse_search),
            pf,
            tuple(context),
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
    def fingerprint(
        grid: Any,
        win: tuple[int, int, int, int],
        extra: Callable[[], object] | None = None,
    ) -> bytes:
        """Digest every grid plane the search can read (see module docstring).

        Planes whose trailing two dims are ``(rows, cols)`` are sliced to
        ``win``; any other array is hashed whole.
        """
        x0, y0, x1, y1 = win
        rows, cols = getattr(grid, "rows", None), getattr(grid, "cols", None)
        h = hashlib.blake2b(digest_size=16)
        h.update(repr(win).encode())
        for name, value in sorted(vars(grid).items()):
            if not isinstance(value, np.ndarray) or name.startswith("_saved"):
                continue
            arr = value
            if arr.ndim >= 2 and arr.shape[-2:] == (rows, cols):
                arr = arr[..., y0:y1, x0:x1]
            h.update(name.encode())
            h.update(repr((arr.dtype.str, arr.shape)).encode())
            h.update(np.ascontiguousarray(arr).tobytes())
        marks = getattr(getattr(grid, "_route_halo", None), "marks", None)
        if marks is not None:
            h.update(repr(sorted(marks.items(), key=repr)).encode())
        if extra is not None:
            h.update(repr(extra()).encode())
        return h.digest()

    # -- operations ------------------------------------------------------
    def lookup(
        self,
        key: Hashable,
        grid: Any,
        win: tuple[int, int, int, int],
        extra: Callable[[], object] | None = None,
    ) -> Any | None:
        """Return the stored payload on a valid hit, else ``None``."""
        entry = self._entries.get(key)
        if entry is None:
            self.stats.misses += 1
            return None
        if self.fingerprint(grid, win, extra) != entry.fingerprint:
            del self._entries[key]
            self.stats.invalidations += 1
            self.stats.misses += 1
            return None
        self._entries.move_to_end(key)
        self.stats.hits += 1
        return entry.payload

    def insert(
        self,
        key: Hashable,
        grid: Any,
        win: tuple[int, int, int, int],
        payload: Any,
        extra: Callable[[], object] | None = None,
    ) -> None:
        if key in self._entries:
            del self._entries[key]
        self._entries[key] = _Entry(self.fingerprint(grid, win, extra), payload)
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
