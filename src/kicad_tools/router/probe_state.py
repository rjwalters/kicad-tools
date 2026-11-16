"""Snapshot/restore of router state around a read-only probe (issue #9003).

``--report-detour-ratio`` runs the real per-net search against the empty
board before the real route starts, then has to leave NO trace on the live
run.  The first version of that guard was a hand-maintained list of
accumulators to save, and it missed ``Autorouter.routing_failures`` -- plus
the failure-analysis cache and budget, the Python-fallback memo and stage
budget, lazily built grid snapshots, and more (PR #9014 review).  A list
like that goes stale as soon as ``route_net`` grows a new side effect.

:class:`AttributeStateSnapshot` replaces the list with a structural rule:
for each object it covers, every attribute binding is recorded, and every
plain container attribute (``list`` / ``dict`` / ``set`` / ``deque``) gets a
shallow copy of its content.  :meth:`AttributeStateSnapshot.restore` then

- deletes attributes the probe created (lazy caches, budgets, memos);
- rebinds every attribute to its pre-probe object (a list the probe
  *replaced*, e.g. ``routing_failures = [f for f ...]``, is put back);
- puts each container's pre-probe content back in place, keeping its
  identity, since other objects may hold a reference to it;
- rewinds :class:`random.Random` and :class:`numpy.random.Generator`
  attributes to their pre-probe state;
- optionally restores ``numpy.ndarray`` content (``copy_arrays=True``, for
  small helper objects).  Large grid planes are restored by the caller
  from an explicit list, since copying every grid array would double the
  grid's memory.

Content copies are shallow, one level deep.  A mutable object held inside
an attribute and mutated in place (not rebound) is not rewound unless that
object is snapshotted too.  Callers therefore snapshot each helper object
the probe can mutate (pathfinder, grid, halo registry, ...).
"""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Iterable
from typing import Any

_CONTAINER_TYPES = (list, dict, set, deque)


def _copy_content(value: Any) -> Any:
    if isinstance(value, dict):
        return list(value.items())
    return list(value)


def _restore_content(value: Any, saved: Any) -> None:
    if isinstance(value, list):
        value[:] = saved
    elif isinstance(value, dict):
        # ``dict.update`` explicitly: ``Counter.update`` would ADD counts.
        dict.clear(value)
        dict.update(value, saved)
    elif isinstance(value, deque):
        value.clear()
        value.extend(saved)
    else:  # set
        value.clear()
        value.update(saved)


class AttributeStateSnapshot:
    """Shallow attribute-level snapshot of one object (see module docstring).

    ``skip`` names attributes left entirely alone: not rebound, not deleted,
    content not restored (state that must move forward, such as the grid's
    #4794 occupancy generation counter).
    """

    def __init__(
        self,
        obj: Any,
        *,
        skip: Iterable[str] = (),
        copy_arrays: bool = False,
    ) -> None:
        self._obj = obj
        self._skip = frozenset(skip)
        try:
            attrs = vars(obj)
        except TypeError:  # __slots__ / builtin: nothing to snapshot
            self._bindings: dict[str, Any] | None = None
            return
        self._bindings = dict(attrs)
        self._contents: dict[str, Any] = {}
        self._rng_states: dict[str, Any] = {}
        self._arrays: dict[str, Any] = {}
        np = _numpy()
        for name, value in attrs.items():
            if name in self._skip:
                continue
            if isinstance(value, _CONTAINER_TYPES):
                self._contents[name] = _copy_content(value)
            elif isinstance(value, random.Random):
                self._rng_states[name] = value.getstate()
            elif np is not None and isinstance(value, np.random.Generator):
                self._rng_states[name] = value.bit_generator.state
            elif copy_arrays and np is not None and isinstance(value, np.ndarray):
                self._arrays[name] = value.copy()

    def restore(self) -> None:
        if self._bindings is None:
            return
        attrs = vars(self._obj)
        for name in [n for n in attrs if n not in self._bindings and n not in self._skip]:
            del attrs[name]
        for name, value in self._bindings.items():
            if name in self._skip:
                continue
            attrs[name] = value
            if name in self._contents:
                _restore_content(value, self._contents[name])
            elif name in self._rng_states:
                state = self._rng_states[name]
                if isinstance(value, random.Random):
                    value.setstate(state)
                else:
                    value.bit_generator.state = state
            elif name in self._arrays:
                value[...] = self._arrays[name]


class GlobalRngSnapshot:
    """Module-level ``random`` and legacy ``numpy.random`` state."""

    def __init__(self) -> None:
        self._py = random.getstate()
        np = _numpy()
        self._np = np.random.get_state() if np is not None else None

    def restore(self) -> None:
        random.setstate(self._py)
        if self._np is not None:
            _numpy().random.set_state(self._np)


def _numpy() -> Any:
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - numpy is a hard dependency
        return None
    return np
