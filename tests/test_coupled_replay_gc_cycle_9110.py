"""``replay`` must not strand a reference cycle (Issue #9110).

``replay`` used to build its value-resolution helper as a nested ``resolve``
closure that called itself for the recursion, so every call left a
``resolve -> cell -> resolve`` cycle behind.  The cycle also pinned the
``objects`` dict -- and with it every native ``Grid3D``/``CoupledPathfinder``
and route result -- until the cyclic GC happened to run: ~140 MB of native
memory ratcheted per replay with the GC disabled (measured on the MIPI_DAT0
fixture: 7 GB peak RSS over 50 replays, flat at ~190 MB after the fix),
which is what drove the xdist worker crash in #9089.

The fix this file pins makes ``_resolve`` a module-level function: no
closure, no cycle, and plain refcounting frees everything when ``replay``
returns or raises.  The fake-backend tests run everywhere, without the
native build; the last test replays a real Board07 fixture against the
native module and is skipped when it is not built.
"""

from __future__ import annotations

import gc
from pathlib import Path

import pytest

from kicad_tools.router.coupled_capture import _ObjRef, _Struct, replay

EXPECTED_DIAGNOSTICS = {
    "success": True,
    "iterations": 7,
    "best_progress": 5.0,
    "timeout_exceeded": False,
    "iteration_limited": False,
    "rejections": {"via_blocked": 2},
    "path_len": 3,
}


class _FakeRouteResult:
    def __init__(self) -> None:
        self.success = True
        self.iterations = 7
        self.best_progress = 5.0
        self.timeout_exceeded = False
        self.iteration_limited = False
        self.rejections = {"via_blocked": 2}
        self.path = [0, 1, 2]


class _FakeSearch:
    def __init__(self, seed: int = 0) -> None:
        self.seed = seed
        self.last_args: tuple | None = None

    def route(self, *args: object, **kwargs: object) -> _FakeRouteResult:
        self.last_args = (args, kwargs)
        return _FakeRouteResult()


class _FakeGeometry:
    """Stand-in for a native struct replay materialises via ``getattr``."""

    def __init__(self) -> None:
        self.cells: list | None = None
        self.meta: dict | None = None


class _FakeModule:
    Search = _FakeSearch
    Geometry = _FakeGeometry


# Exercises every ``_resolve`` branch: object back-reference, struct with a
# nested list and a nested dict holding a tuple and another back-reference.
CALLS = [
    {"op": "new", "type": "Search", "obj": 1, "args": [42], "kwargs": {}},
    {"op": "new", "type": "Search", "obj": 2, "args": [], "kwargs": {}},
    {
        "op": "call",
        "obj": 1,
        "method": "route",
        "args": [
            _ObjRef(2),
            _Struct(
                "Geometry",
                {"cells": [1, 2], "meta": {"a": _ObjRef(1), "t": (1, 2)}},
            ),
        ],
        "kwargs": {"budget": 100},
    },
]


def _drain_pending_cycles() -> None:
    """Collect cycles left by imports/fixtures so the assertion counts only
    what the code under test added."""
    gc.enable()
    gc.collect()


def test_replay_leaves_no_cyclic_garbage() -> None:
    _drain_pending_cycles()
    gc.disable()
    try:
        result = replay(CALLS, router_cpp=_FakeModule())
    finally:
        gc.enable()
    assert result.routes == [EXPECTED_DIAGNOSTICS]

    # The refactor must resolve exactly as the closure did.
    search = result.objects[1]
    peer, geometry = search.last_args[0]
    assert peer is result.objects[2]
    assert geometry.cells == [1, 2]
    assert geometry.meta == {"a": search, "t": (1, 2)}
    assert search.last_args[1] == {"budget": 100}

    del result
    assert gc.collect() == 0


def test_replay_raising_leaves_no_cyclic_garbage() -> None:
    """A replay that dies mid-way must not strand the cycle either."""

    class _Boom:
        def route(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("boom mid-replay")

    module = _FakeModule()
    module.Boom = _Boom
    calls = [
        {"op": "new", "type": "Boom", "obj": 1, "args": [], "kwargs": {}},
        {
            "op": "call",
            "obj": 1,
            "method": "route",
            "args": [_Struct("Geometry", {"cells": [1]})],
            "kwargs": {},
        },
    ]

    _drain_pending_cycles()
    gc.disable()
    try:
        with pytest.raises(RuntimeError, match="boom mid-replay"):
            replay(calls, router_cpp=module)
    finally:
        gc.enable()
    assert gc.collect() == 0


@pytest.mark.requires_router_cpp
def test_replay_board07_fixture_leaves_no_cyclic_garbage() -> None:
    """The #9110 acceptance criterion against a real Board07 capture."""
    from kicad_tools.router.coupled_capture import load_fixture

    fixture = load_fixture(Path(__file__).parent / "fixtures" / "board07_coupled_mipi_dat0")
    _drain_pending_cycles()
    gc.disable()
    try:
        result = replay(fixture.calls)
    finally:
        gc.enable()
    assert result.routes
    del result
    assert gc.collect() == 0
