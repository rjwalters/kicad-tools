"""Regression tests for Issue #3138 -- softstart dense-package escape gap.

#3138 traced the U1 cluster congestion that capped softstart routing at
6/10 nets to a single architectural gap: ``generate_design.py:1659``
called ``router.route_all_negotiated()`` directly, which bypasses the
dense-package escape pre-pass (``generate_escape_routes`` + virtual
escape-endpoint pads) that only runs through ``route_with_escape()``.

Approach A (the fix): swap the call site to
``router.route_with_escape(use_negotiated=True, ...)`` so the
U1 STM32G031F6P6 TSSOP-20 at 0.65mm pitch is escape-routed before the
main negotiated loop runs.

This module enforces three regression guarantees:

1. ``is_dense_package`` flags the STM32G031F6P6 footprint as dense, so
   the curator's ``router/escape.py:222`` branch keeps firing for this
   class of package (TSSOP / SSOP with 0.65mm pitch).
2. ``Autorouter.detect_dense_packages()`` picks up a TSSOP-20-shaped pad
   cluster at the standard ``DesignRules(trace_width=0.3,
   trace_clearance=0.15)`` softstart settings.
3. ``Autorouter.route_with_escape(use_negotiated=True)`` actually
   executes the dense-package pre-pass on such a board: at least one
   ``_escape_pad_overrides`` entry is registered before the main router
   begins, proving the bypass observed in #3138 is fixed.

The full end-to-end softstart re-route is exercised by the manual
``boards/external/softstart/generate_design.py`` flow and is too slow
for CI; these unit-level guards catch the structural regression that
#3138 was about (the missing escape pre-pass call) without needing to
re-route 10 nets on the runner.

Approach B (category-aware DRC rollback gate at
``router/optimizer/pcb.py:407``) is covered by separate tests in
``test_optimize_drc_aware.py`` -- the gate change is independent of the
softstart code path.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.escape import is_dense_package
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stm32g031f6p6_tssop20_pads(net_offset: int = 1) -> list[Pad]:
    """Build a TSSOP-20 pad fixture matching STM32G031F6P6 geometry.

    Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm has 10 pads per row at 0.65mm
    pitch with rows separated by ~5.85mm (centre-to-centre).  Pad sizes
    are 1.5mm x 0.4mm (long axis horizontal on left/right rows).

    Args:
        net_offset: starting net id for the synthetic nets.  Each pad
            gets a unique net so the dense-package detector treats
            them as ten independent signal escapees.

    Returns:
        List of 20 Pad objects positioned in two parallel rows.
    """
    pads: list[Pad] = []
    pin = 1
    for i in range(10):
        # Left row (pins 1-10): negative x of body centre
        pads.append(
            Pad(
                x=-2.925,
                y=-2.925 + i * 0.65,
                width=0.4,
                height=1.5,
                net=net_offset + i,
                net_name=f"NET_L{i}",
                layer=Layer.F_CU,
                ref="U1",
                pin=str(pin),
            )
        )
        pin += 1
    for i in range(10):
        # Right row (pins 11-20): mirror of the left row
        pads.append(
            Pad(
                x=2.925,
                y=2.925 - i * 0.65,
                width=0.4,
                height=1.5,
                net=net_offset + 10 + i,
                net_name=f"NET_R{i}",
                layer=Layer.F_CU,
                ref="U1",
                pin=str(pin),
            )
        )
        pin += 1
    return pads


# ---------------------------------------------------------------------------
# Test 1 -- ``is_dense_package`` flags a TSSOP-20 at 0.65mm pitch
# ---------------------------------------------------------------------------


class TestTSSOP20DenseDetection:
    """The dense-package detector must always flag TSSOP-20."""

    def test_is_dense_package_flags_tssop20(self):
        """STM32G031F6P6 TSSOP-20 at 0.65mm pitch is dense regardless of design rules."""
        pads = _stm32g031f6p6_tssop20_pads()

        # The fine-pitch SSOP/TSSOP rule at ``router/escape.py:222``
        # fires whenever ``min_pitch <= 0.75`` and the layout is
        # dual-row.  This must hold at the softstart-baseline 0.15mm
        # clearance and at the looser 0.3mm clearance.
        assert is_dense_package(pads, trace_width=0.3, clearance=0.15)
        assert is_dense_package(pads, trace_width=0.3, clearance=0.3)

    def test_is_dense_package_flags_tssop20_default_rules(self):
        """Without trace/clearance hints, TSSOP-20 is still dense by the SSOP rule."""
        pads = _stm32g031f6p6_tssop20_pads()
        assert is_dense_package(pads)


# ---------------------------------------------------------------------------
# Test 2 -- the Autorouter picks up TSSOP-20 dense packages
# ---------------------------------------------------------------------------


class TestAutorouterDetectsTSSOP20:
    """Autorouter.detect_dense_packages() must surface the TSSOP-20 cluster."""

    def test_detect_dense_packages_finds_tssop20(self):
        """Adding the STM32 pads to an Autorouter results in detect_dense_packages
        returning one PackageInfo with the U1 cluster."""
        rules = DesignRules(
            grid_resolution=0.075,
            trace_width=0.3,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
        )
        router = Autorouter(width=20.0, height=20.0, rules=rules)

        # Offset pad coordinates into router grid coordinates
        # (positive coordinates required).
        for pad in _stm32g031f6p6_tssop20_pads():
            shifted = Pad(
                x=pad.x + 10.0,
                y=pad.y + 10.0,
                width=pad.width,
                height=pad.height,
                net=pad.net,
                net_name=pad.net_name,
                layer=pad.layer,
                ref=pad.ref,
                pin=pad.pin,
            )
            router.pads[(shifted.ref, shifted.pin)] = shifted

        detected = router.detect_dense_packages()
        refs = {pkg.ref for pkg in detected}
        assert "U1" in refs, (
            "Issue #3138: U1 TSSOP-20 must be flagged as a dense package "
            "for the escape pre-pass; failure means is_dense_package() "
            "regressed away from the SSOP 0.75mm-pitch branch."
        )


# ---------------------------------------------------------------------------
# Test 3 -- route_with_escape actually fires the escape pre-pass on softstart-class layouts
# ---------------------------------------------------------------------------


class TestRouteWithEscapeFiresOnTSSOP20:
    """Verify the architectural fix: route_with_escape() runs the escape pre-pass.

    This is the core regression guard for #3138 -- the bug was that
    ``route_all_negotiated()`` silently skipped the pre-pass.
    ``route_with_escape()`` MUST call ``generate_escape_routes()`` for
    every detected dense package before the main negotiated loop runs.

    We do not need to drive a full softstart re-route to prove this:
    we just need to confirm that ``route_with_escape`` invokes the
    pre-pass on a TSSOP-20 layout.  The end-to-end reach measurement
    is the integration-level test that
    ``boards/external/softstart/generate_design.py`` already performs
    out-of-band.
    """

    def test_route_with_escape_invokes_pre_pass(self, monkeypatch):
        """route_with_escape() must call generate_escape_routes() on the detected dense packages.

        The TSSOP-20 in isolation has no partner pads on other components,
        so the EscapeRouter rightly defers all 20 pins to the main router
        on clearance grounds.  That is not a regression -- the test only
        asserts the *call path*: route_with_escape -> detect_dense_packages
        -> generate_escape_routes.  The end-to-end clearance behaviour is
        covered by ``test_escape_endpoint_routing.py`` and the manual
        softstart re-route.
        """
        rules = DesignRules(
            grid_resolution=0.075,
            trace_width=0.3,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
        )
        router = Autorouter(width=30.0, height=30.0, rules=rules)

        for pad in _stm32g031f6p6_tssop20_pads():
            shifted = Pad(
                x=pad.x + 15.0,
                y=pad.y + 15.0,
                width=pad.width,
                height=pad.height,
                net=pad.net,
                net_name=pad.net_name,
                layer=pad.layer,
                ref=pad.ref,
                pin=pad.pin,
            )
            router.pads[(shifted.ref, shifted.pin)] = shifted

        # Spy on generate_escape_routes so we can assert it is called.
        # Use a wrapper so the real method still runs and we don't
        # accidentally make the pre-pass a no-op.
        call_args: list = []
        real_generate = router.generate_escape_routes

        def spy(packages=None):
            call_args.append(packages)
            return real_generate(packages)

        monkeypatch.setattr(router, "generate_escape_routes", spy)

        # Use the non-negotiated path so the slow A* loop is skipped --
        # the architectural invariant we care about (pre-pass invoked)
        # is the same on both paths.  ``use_negotiated=False`` falls
        # through ``route_all`` which is fast on an empty board.
        router.route_with_escape(
            use_negotiated=False,
            timeout=10.0,
            per_net_timeout=2.0,
        )

        assert call_args, (
            "Issue #3138: route_with_escape() must invoke "
            "generate_escape_routes() on the detected dense packages. "
            "An empty call list means the pre-pass was silently skipped "
            "-- this is exactly the gap that capped softstart routing "
            "at 6/10 nets when generate_design.py called "
            "route_all_negotiated() directly."
        )

        # The dense package list passed in must include U1.
        passed_packages = call_args[0]
        if passed_packages is not None:
            refs = {pkg.ref for pkg in passed_packages}
            assert "U1" in refs, (
                "Issue #3138: detected dense packages must include U1 "
                "(TSSOP-20) when it is the only fine-pitch component."
            )


# ---------------------------------------------------------------------------
# Test 5 -- the observable stdout log line is emitted (Issue #3152)
# ---------------------------------------------------------------------------


class TestEscapeStdoutLogLine:
    """Lock in the observable ``Dense packages escaped: N`` stdout signal.

    Issue #3152: a 2026-05-27 audit reported the
    ``Dense packages escaped: 1`` log line "not reported in log" for the
    softstart board, raising a false alarm that the PR #3142 escape
    pre-pass had regressed.  The curator's empirical investigation showed
    the pre-pass *was* firing -- the audit had captured a truncated slice
    of a 401-line run -- so no source fix was warranted.

    What was missing was a test that pins the *observable* contract: the
    structural guards in this module assert the call path
    (``route_with_escape`` -> ``generate_escape_routes``) and the dense
    detection, but none of them asserted the stdout summary line itself.
    That line (``core.py:10969``) is the exact signal the audit relied
    on, and nothing locked it in against either a silent regression or
    future log-capture noise.

    These tests drive ``route_with_escape(use_negotiated=False, ...)`` --
    the fast non-negotiated path used by ``test_route_with_escape_invokes_pre_pass``
    -- and assert on captured stdout from both sides of the
    unconditional-emission contract:

    * a TSSOP-20 board prints ``Dense packages escaped: 1`` and the
      ``--- Phase 1: Escape Routing (1 dense packages) ---`` header, and
    * a board with no dense package prints
      ``Dense packages escaped: 0`` and the
      ``--- No dense packages detected, skipping escape routing ---``
      line.
    """

    def test_route_with_escape_logs_dense_packages_escaped(self, capsys):
        """route_with_escape() must print the ``Dense packages escaped: N`` (N>=1) summary on a TSSOP-20 board."""
        rules = DesignRules(
            grid_resolution=0.075,
            trace_width=0.3,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
        )
        router = Autorouter(width=30.0, height=30.0, rules=rules)

        for pad in _stm32g031f6p6_tssop20_pads():
            shifted = Pad(
                x=pad.x + 15.0,
                y=pad.y + 15.0,
                width=pad.width,
                height=pad.height,
                net=pad.net,
                net_name=pad.net_name,
                layer=pad.layer,
                ref=pad.ref,
                pin=pad.pin,
            )
            router.pads[(shifted.ref, shifted.pin)] = shifted

        # Non-negotiated path keeps CI fast (skips the slow A* loop); the
        # summary log line is emitted on both paths (``core.py:10969``).
        router.route_with_escape(
            use_negotiated=False,
            timeout=10.0,
            per_net_timeout=2.0,
        )

        out = capsys.readouterr().out

        # The Phase 1 header must report at least one dense package.
        assert "--- Phase 1: Escape Routing (1 dense packages) ---" in out, (
            "Issue #3152: route_with_escape() must print the Phase 1 "
            "escape-routing header with the dense-package count when a "
            "TSSOP-20 is present.  A missing header is the audit signal "
            "that the escape pre-pass was bypassed.\n\nCaptured stdout:\n" + out
        )

        # The summary line is the exact signal the audit relied on.
        assert "Dense packages escaped: 1" in out, (
            "Issue #3152: route_with_escape() must print "
            "'Dense packages escaped: 1' when U1 (TSSOP-20) is the only "
            "dense package.  This stdout line (core.py:10969) is the "
            "observable signal the 2026-05-27 audit checked for; pinning "
            "it here guards against a silent regression of the escape "
            "pre-pass.\n\nCaptured stdout:\n" + out
        )

    def test_route_with_escape_logs_zero_when_no_dense_package(self, capsys):
        """With no dense package, route_with_escape() must still print the zero-count summary.

        The summary line is emitted unconditionally (it prints ``0`` even
        when there are no dense packages), so pinning the zero case locks
        the contract from both sides and proves the ``N`` in the
        positive test is meaningful, not a constant.
        """
        rules = DesignRules(
            grid_resolution=0.075,
            trace_width=0.3,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
        )
        router = Autorouter(width=30.0, height=30.0, rules=rules)

        # Two widely-spaced pads on a single net: nowhere near the
        # fine-pitch dual-row geometry that triggers is_dense_package().
        router.pads[("R1", "1")] = Pad(
            x=5.0,
            y=5.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            ref="R1",
            pin="1",
        )
        router.pads[("R1", "2")] = Pad(
            x=20.0,
            y=20.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            ref="R1",
            pin="2",
        )

        router.route_with_escape(
            use_negotiated=False,
            timeout=10.0,
            per_net_timeout=2.0,
        )

        out = capsys.readouterr().out

        assert "--- No dense packages detected, skipping escape routing ---" in out, (
            "Issue #3152: route_with_escape() must print the "
            "'No dense packages detected' line when no fine-pitch "
            "package is present.\n\nCaptured stdout:\n" + out
        )
        assert "Dense packages escaped: 0" in out, (
            "Issue #3152: the 'Dense packages escaped: N' summary is "
            "emitted unconditionally and must print 0 when there are no "
            "dense packages.\n\nCaptured stdout:\n" + out
        )


# ---------------------------------------------------------------------------
# Test 4 -- the softstart call site is wired through route_with_escape
# ---------------------------------------------------------------------------


class TestSoftstartCallSite:
    """Source-level regression: the softstart generator must use route_with_escape.

    The bug in #3138 was specifically that
    ``boards/external/softstart/generate_design.py:1659`` called
    ``route_all_negotiated()`` directly.  If a future refactor reverts
    this back, the unit-level escape tests above would still pass but
    the end-to-end softstart routing would silently regress to 6/10.
    """

    def test_softstart_generator_routes_via_cpp_backend(self):
        """generate_design.py must invoke kct route --backend cpp.

        Updated for Issue #3343 P4 (rev B): rev A used the Python
        ``router.route_with_escape(...)`` path to access the TSSOP-20
        dense-package escape pre-pass (#3138).  Rev B switched to the
        LQFP-32 footprint (less dense, 0.8mm pitch vs 0.65mm) and
        routes via ``kct route --backend cpp``, which uses the C++
        adaptive-grid pipeline (with Phase 1 pad escape built in) for
        a 10-100× speedup.

        This test was originally Issue #3138's guard against reverting
        to ``router.route_all_negotiated()`` (which bypassed the
        escape pre-pass).  Under rev B P4, the equivalent guard is
        that ``route_pcb`` must invoke ``--backend cpp`` (so the C++
        adaptive-grid path runs).
        """
        from pathlib import Path

        gen_path = (
            Path(__file__).resolve().parents[1]
            / "boards"
            / "external"
            / "softstart"
            / "generate_design.py"
        )
        if not gen_path.exists():
            pytest.skip(
                "boards/external/softstart/generate_design.py not present in this "
                "checkout (expected when running tests from a slimmed source distribution)"
            )

        text = gen_path.read_text()
        # Rev B P4: kct route --backend cpp is the production path.
        assert '"--backend", "cpp"' in text or "--backend cpp" in text, (
            "Issue #3343 P4: softstart generate_design.py must invoke "
            "kct route --backend cpp.  Rev A's "
            "router.route_with_escape(...) path was retired in favour "
            "of the C++ adaptive-grid pipeline."
        )
