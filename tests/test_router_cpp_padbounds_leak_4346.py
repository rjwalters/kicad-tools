"""Regression tests for issue #4346.

``kct build-native`` dumped a ``nanobind: leaked 4 instances!`` banner of
type ``PadBounds`` at interpreter teardown right after a *successful* build.

Root cause: ``Pathfinder.route`` / ``Pathfinder.route_resumable`` declared
their two pad-bounds parameters with a materialized bound-type default arg
(``"start_pad_bounds"_a = PadBounds{}`` etc.).  When a nanobind default
argument is a *bound* (``nb::class_``) type, nanobind casts the sentinel
into a persistent Python object held for the module's lifetime; because
CPython does not deallocate extension modules at interpreter finalization,
those objects are still live when nanobind's teardown leak checker runs --
hence the "leaked 4 instances of PadBounds" report.

The fix binds ``route`` / ``route_resumable`` via a thin lambda whose two
pad-bounds params are ``std::optional<PadBounds>`` defaulting to
``nb::none()``; the omitted case substitutes an all-zero ``PadBounds{}``
inside the lambda, so there are zero tracked instances and Python-visible
behavior is unchanged.
"""

import subprocess
import sys

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available

pytestmark = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not built (run `kct build-native`)",
)


def test_import_router_cpp_reports_no_nanobind_leak() -> None:
    """Importing ``router_cpp`` in a fresh interpreter must not leak.

    The nanobind leak checker only runs at interpreter finalization, so the
    reliable signal is a subprocess whose stderr we scrape after it exits.
    Before the #4346 fix this printed ``nanobind: leaked 4 instances!`` for
    the four ``PadBounds{}`` default-argument sentinels.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import kicad_tools.router.router_cpp"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Importing router_cpp failed:\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "nanobind: leaked" not in result.stderr, (
        "nanobind reported a leak at teardown after importing router_cpp -- "
        "a bound-type default argument (or similar retained instance) has "
        f"regressed issue #4346.\nstderr={result.stderr!r}"
    )


def test_route_pad_bounds_defaults_are_none() -> None:
    """The Python-facing pad-bounds defaults must be ``None``, not a
    materialized ``PadBounds`` sentinel.

    Guards against re-introducing a bound-type default argument (the root
    cause of the #4346 instance leak).
    """
    from kicad_tools.router import router_cpp

    for method in (router_cpp.Pathfinder.route, router_cpp.Pathfinder.route_resumable):
        doc = method.__doc__ or ""
        assert "start_pad_bounds: kicad_tools.router.router_cpp.PadBounds | None = None" in doc, (
            f"{method.__name__} start_pad_bounds default is not None:\n{doc}"
        )
        assert "end_pad_bounds: kicad_tools.router.router_cpp.PadBounds | None = None" in doc, (
            f"{method.__name__} end_pad_bounds default is not None:\n{doc}"
        )


def _make_pathfinder():
    from kicad_tools.router import router_cpp

    grid = router_cpp.Grid3D(100, 100, 2, 0.5, 0.0, 0.0)
    rules = router_cpp.DesignRules()
    rules.trace_width = 0.2
    rules.trace_clearance = 0.2
    rules.via_diameter = 0.6
    rules.via_drill = 0.3
    rules.via_clearance = 0.2
    rules.grid_resolution = 0.5
    return router_cpp.Pathfinder(grid, rules, True)


def test_omitted_pad_bounds_matches_explicit_all_zero() -> None:
    """Omitting the pad-bounds args must route identically to passing an
    explicit all-zero ``PadBounds()`` -- preserving pre-#4346 behavior.
    """
    from kicad_tools.router import router_cpp

    kwargs = {
        "start_x": 5.0 * 0.5,
        "start_y": 50.0 * 0.5,
        "start_layer": 0,
        "end_x": 95.0 * 0.5,
        "end_y": 50.0 * 0.5,
        "end_layer": 0,
        "net": 1,
    }

    # Omitted (new default None -> all-zero PadBounds inside the binding).
    r_omitted = _make_pathfinder().route(**kwargs)
    # Explicit all-zero PadBounds (the historical default value).
    r_explicit = _make_pathfinder().route(
        start_pad_bounds=router_cpp.PadBounds(),
        end_pad_bounds=router_cpp.PadBounds(),
        **kwargs,
    )

    assert r_omitted.success and r_explicit.success
    assert len(r_omitted.segments) == len(r_explicit.segments)
    assert len(r_omitted.vias) == len(r_explicit.vias)
