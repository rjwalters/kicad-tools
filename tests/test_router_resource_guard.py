"""Resource exhaustion must abort a route, never change one (Issue #4724).

The router's broad ``except Exception`` handlers exist so a malformed board,
a fixture grid without an occupancy API, or an optional diagnostic cannot
abort a route.  Under host memory pressure the SAME handlers convert a
``MemoryError`` into a routing DECISION -- "this Steiner cell is free", "this
component has no pitch", "this rip-up did not fire" -- and the run silently
continues on a different trajectory.  That is the #4724 signature: board-06's
stagnation recovery reported ``restored 5 / rerouted 6`` on a host at
load-average ~90 where five same-code runs on a quiet host all reported
``restored 4 / rerouted 7``.

These tests pin both halves of the contract at each guarded handler:

* an exhaustion failure PROPAGATES (with its type intact), and
* an ordinary exception keeps its historical fallback byte-for-byte.
"""

from __future__ import annotations

import errno

import pytest

from kicad_tools.router.algorithms.steiner import make_blocked_cell_predicate
from kicad_tools.router.resource_guard import (
    is_resource_exhaustion,
    reraise_if_resource_exhaustion,
)


class TestIsResourceExhaustion:
    def test_memory_error_is_exhaustion(self):
        assert is_resource_exhaustion(MemoryError("out of memory"))

    def test_recursion_error_is_exhaustion(self):
        assert is_resource_exhaustion(RecursionError())

    def test_enomem_oserror_is_exhaustion(self):
        assert is_resource_exhaustion(OSError(errno.ENOMEM, "Cannot allocate memory"))

    def test_other_oserror_is_not_exhaustion(self):
        assert not is_resource_exhaustion(OSError(errno.ENOENT, "No such file"))

    def test_native_bad_alloc_rethrown_as_runtime_error_is_exhaustion(self):
        """A C++ layer that catches and rethrows loses the MemoryError type."""
        assert is_resource_exhaustion(RuntimeError("std::bad_alloc while building grid"))

    def test_ordinary_runtime_error_is_not_exhaustion(self):
        assert not is_resource_exhaustion(RuntimeError("no path found"))

    @pytest.mark.parametrize("exc", [ValueError("x"), AttributeError("y"), KeyError("z")])
    def test_board_and_fixture_failures_are_not_exhaustion(self, exc):
        """The failures the broad handlers were written for stay absorbed."""
        assert not is_resource_exhaustion(exc)

    def test_no_active_exception_is_not_exhaustion(self):
        assert not is_resource_exhaustion(None)


class TestReraiseHelper:
    def test_reraises_the_original_exception_object(self):
        sentinel = MemoryError("mirror allocation failed")
        with pytest.raises(MemoryError) as caught:
            try:
                raise sentinel
            except Exception:
                reraise_if_resource_exhaustion("unit: probe")
        assert caught.value is sentinel

    def test_reports_the_type_and_context_on_stderr(self, capsys):
        with pytest.raises(MemoryError):
            try:
                raise MemoryError("boom")
            except Exception:
                reraise_if_resource_exhaustion("unit: probe")
        err = capsys.readouterr().err
        assert "[resource-exhaustion]" in err
        assert "unit: probe" in err
        assert "MemoryError" in err

    def test_returns_silently_for_an_ordinary_exception(self, capsys):
        fell_through = False
        try:
            raise ValueError("bad board data")
        except Exception:
            reraise_if_resource_exhaustion("unit: probe")
            fell_through = True
        assert fell_through
        assert capsys.readouterr().err == ""

    def test_is_a_no_op_outside_an_except_block(self):
        """Never manufactures a ``No active exception to re-raise``."""
        reraise_if_resource_exhaustion("unit: probe")


class _Grid:
    """Grid stub whose occupancy API raises a caller-chosen exception."""

    resolution = 0.1

    def __init__(self, exc: BaseException | None = None, indices=(0, 1)) -> None:
        self._exc = exc
        self._indices = list(indices)

    def get_routable_indices(self):
        return self._indices

    def is_blocked_for_net(self, gx, gy, layer_idx, net):  # noqa: ARG002
        if self._exc is not None:
            raise self._exc
        return False


class _Rules:
    trace_width = 0.2
    trace_clearance = 0.2


class TestSteinerBlockedCellPredicate:
    """``_point_blocked``'s fallback decides where a branch point lands."""

    def test_ordinary_failure_still_reports_the_cell_free(self):
        predicate = make_blocked_cell_predicate(_Grid(TypeError("mock grid")), _Rules(), net=3)
        assert predicate is not None
        assert predicate(5, 5) is False

    def test_memory_error_propagates_instead_of_reporting_free(self):
        predicate = make_blocked_cell_predicate(_Grid(MemoryError("grid scan")), _Rules(), net=3)
        assert predicate is not None
        with pytest.raises(MemoryError):
            predicate(5, 5)

    def test_ordinary_routable_index_failure_still_disables_relocation(self):
        class _NoIndices(_Grid):
            def get_routable_indices(self):
                raise AttributeError("fixture grid")

        assert make_blocked_cell_predicate(_NoIndices(), _Rules(), net=3) is None

    def test_memory_error_from_routable_index_probe_propagates(self):
        class _OomIndices(_Grid):
            def get_routable_indices(self):
                raise MemoryError("layer table")

        with pytest.raises(MemoryError):
            make_blocked_cell_predicate(_OomIndices(), _Rules(), net=3)

    def test_ordinary_margin_failure_keeps_the_one_cell_margin(self):
        class _BadRules:
            @property
            def trace_width(self):
                raise AttributeError("mock rules")

        predicate = make_blocked_cell_predicate(_Grid(), _BadRules(), net=3)
        assert predicate is not None
        assert predicate(5, 5) is False

    def test_memory_error_from_margin_math_propagates(self):
        class _OomRules:
            @property
            def trace_width(self):
                raise MemoryError("rules table")

        with pytest.raises(MemoryError):
            make_blocked_cell_predicate(_Grid(), _OomRules(), net=3)


class _PitchPathfinder:
    """Minimal stand-in exposing only what ``_get_component_pitches`` reads."""

    from kicad_tools.router.cpp_backend import CppPathfinder as _Real

    _get_component_pitches = _Real._get_component_pitches

    def __init__(self, exc: BaseException) -> None:
        self._component_pitches_cache = None

        class _PyGrid:
            def compute_component_pitches(self):
                raise exc

        class _Grid3D:
            _py_grid = _PyGrid()

        self._grid = _Grid3D()


class TestComponentPitchCache:
    """An empty pitch map is CACHED and drives neck-down + clearance."""

    def test_ordinary_failure_still_degrades_to_an_empty_map(self):
        assert _PitchPathfinder(ValueError("bad footprint"))._get_component_pitches() == {}

    def test_memory_error_propagates_instead_of_caching_an_empty_map(self):
        pathfinder = _PitchPathfinder(MemoryError("pitch table"))
        with pytest.raises(MemoryError):
            pathfinder._get_component_pitches()
        # Nothing was cached, so a later call on a recovered host still works.
        assert pathfinder._component_pitches_cache is None
