"""Synthetic too-tight calibration fixture for the area-estimate heuristic.

Issue #3406 -- spun out from PR #3404 (Issue #3403).

PR #3404 added the sum-of-clearances area heuristic with
``packing_overhead=2.5`` default.  Calibration against checked-in boards
02-07 showed ratios 1.99-3.95 -- all routable boards land safely above 1.0.
But the test fleet contains no **doomed** board (ratio < 1.0), so the
heuristic's discriminative power (does it correctly skip doomed envelopes?)
was unverified.

This file adds the missing coverage with synthetic fixtures:

  1. **Doomed fixture** (``_PCB_50x50_DOOMED``): a 50 x 50 mm envelope
     packed with 35 fat SMD parts.  The base area sum * 2.5 exceeds the
     envelope -- the pre-route check MUST skip the doomed L=2 attempt
     and escalate the envelope to the next size tier.

  2. **Inverse fixture** (``_PCB_100x100_LOOSE``): a 100 x 100 mm
     envelope with only a couple of small components.  The heuristic
     happily *passes* the pre-route check, so the inner router gets to
     run.  We then mock the inner router to report high DRC density
     (the "heuristic underestimated routability" failure mode) and
     verify the reactive DRC-density backstop kicks in -- the loop
     still escalates rather than declaring the routing complete.

  3. **Pinned ratios** (``TestPinnedRatios``): independently re-runs the
     estimator on duck-typed stand-ins for the two fixtures above and
     pins the expected ``envelope / estimate`` ratios.  Any silent
     change to ``DEFAULT_PACKING_OVERHEAD``, the routing-channel
     constant, or the halo formula will break these assertions before
     a subtle regression slips out the door.

Why a separate test file?  ``test_auto_pcb_size_area_estimate.py`` is
unit-level (no router); ``test_auto_pcb_size_integration.py`` is the
catch-all for the size-escalation control flow.  This file targets the
SPECIFIC validation gap called out in #3406 -- isolating it makes the
"what does this prove?" answerable from the file name alone.

Related: #3403, PR #3404, ``feedback_manufacturable_means_100pct.md``.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kicad_tools.router.auto_pcb_size import (
    DEFAULT_PACKING_OVERHEAD,
    DEFAULT_ROUTING_CHANNEL_PER_NET_MM2,
    envelope_meets_area_estimate,
    estimate_required_area,
)
from kicad_tools.router.mfr_limits import MFR_JLCPCB

# ---------------------------------------------------------------------------
# Synthetic PCB fixtures (KiCad s-expression).
#
# The s-exprs below are intentionally minimal: just enough geometry for
# PCB.load() + the auto-pcb-size estimator to compute a sensible answer.
# Each pad block is a 4x4 mm SMD pad; the footprints are sized so that
# their pad-array bbox dominates the per-component area term in the
# estimator (component bbox > halo > channel, in our parameter range).
#
# Footprint quad-pad layout: corners of an 8x8 mm square, with pad
# centers at (-4, -4), (4, -4), (-4, 4), (4, 4).  Each pad is 4x4 mm.
# Bbox extent: x in [-6, 6] => W=12 mm; y in [-6, 6] => H=12 mm.
# Pad-array bbox area = 144 mm^2 per footprint.
#
# We re-use this geometry across both fixtures so the only differences
# are component count and envelope dimensions.
# ---------------------------------------------------------------------------


def _quad_pad_footprint_sexp(ref: str, uuid: str, cx: float, cy: float) -> str:
    """Emit a footprint S-expression with four 4x4 mm pads in a square.

    The footprint's pad-array bbox is 12 x 12 mm = 144 mm^2.  Placed at
    (``cx``, ``cy``) in board coordinates.  The pads connect to nets
    1..4 inside the s-expression but those numbers are irrelevant for
    the area estimator (which only consults pad geometry + the top-level
    ``(net N name)`` declarations).
    """
    return textwrap.dedent(f"""\
          (footprint "{ref}"
            (layer "F.Cu")
            (uuid "{uuid}")
            (at {cx} {cy})
            (pad "1" smd rect (at -4 -4) (size 4 4) (layers "F.Cu"))
            (pad "2" smd rect (at  4 -4) (size 4 4) (layers "F.Cu"))
            (pad "3" smd rect (at -4  4) (size 4 4) (layers "F.Cu"))
            (pad "4" smd rect (at  4  4) (size 4 4) (layers "F.Cu"))
          )
    """)


def _build_doomed_pcb_sexp() -> str:
    """50 x 50 mm board with 35 fat SMD parts -- the area estimate exceeds the envelope.

    Per-component bbox = 12 x 12 mm = 144 mm^2.

    Hand math at ``packing_overhead=2.5`` and ``min_clearance=0.127``:
      footprint_area = 35 * 144 = 5040 mm^2
      halo = 35 * 2*(12+12)*0.127 = 35 * 6.096 = 213.36 mm^2
      channels = 35 * 20 = 700 mm^2
      base_sum = 5040 + 213.36 + 700 = 5953.36 mm^2
      total = 2.5 * 5953.36 = 14883.4 mm^2

    Envelope = 50 * 50 = 2500 mm^2.  Ratio envelope/total = 0.168 -- well
    below 1.0, so the pre-route check MUST refuse and escalate.  (We
    don't actually try to place 35 components inside 50x50 -- they all
    sit at the origin; the estimator only cares about the pad geometry,
    not collisions.)

    Routing to next-tier 100x150 (=15 000 mm^2) gives ratio 1.008 -- still
    barely below 1.0, so two escalations are needed before the heuristic
    is satisfied (at 150x150 = 22 500 mm^2, ratio 1.51).  This is the
    *staircase of doom* the test exercises end-to-end.
    """
    parts = []
    for i in range(1, 36):
        parts.append(_quad_pad_footprint_sexp(f"U{i}", f"uu-{i}", 25.0, 25.0))
    footprints = "\n".join(parts)
    nets = "\n".join(f'  (net {i} "SIG_{i}")' for i in range(1, 36))
    return textwrap.dedent("""\
        (kicad_pcb
          (version 20240108)
          (generator "test")
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
            (44 "Edge.Cuts" user)
          )
          (net 0 "")
        """) + nets + textwrap.dedent("""
          (gr_rect
            (start 100 100)
            (end 150 150)
            (stroke (width 0.1) (type default))
            (fill none)
            (layer "Edge.Cuts")
            (uuid "outline-doomed")
          )
        """) + footprints + ")\n"


def _build_loose_pcb_sexp() -> str:
    """100 x 100 mm board with 2 small SMD parts -- area estimate well below envelope.

    Per-component bbox = 12 x 12 mm = 144 mm^2.

    Hand math at ``packing_overhead=2.5``:
      footprint_area = 2 * 144 = 288 mm^2
      halo = 2 * 2*(12+12)*0.127 = 12.192 mm^2
      channels = 2 * 20 = 40 mm^2  (assuming both signal nets)
      base_sum = 288 + 12.192 + 40 = 340.192 mm^2
      total = 2.5 * 340.192 = 850.48 mm^2

    Envelope = 100 * 100 = 10 000 mm^2.  Ratio = 11.76 -- comfortably
    above 1.0, so the pre-route check passes and the inner router gets to
    run.  When the inner router (mocked) returns high DRC density, the
    reactive backstop fires.
    """
    parts = [
        _quad_pad_footprint_sexp("U1", "uu-loose-1", 25.0, 25.0),
        _quad_pad_footprint_sexp("U2", "uu-loose-2", 75.0, 75.0),
    ]
    return textwrap.dedent("""\
        (kicad_pcb
          (version 20240108)
          (generator "test")
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
            (44 "Edge.Cuts" user)
          )
          (net 0 "")
          (net 1 "SIG_A")
          (net 2 "SIG_B")
          (gr_rect
            (start 100 100)
            (end 200 200)
            (stroke (width 0.1) (type default))
            (fill none)
            (layer "Edge.Cuts")
            (uuid "outline-loose")
          )
        """) + "\n".join(parts) + ")\n"


_PCB_50x50_DOOMED = _build_doomed_pcb_sexp()
_PCB_100x100_LOOSE = _build_loose_pcb_sexp()


# ---------------------------------------------------------------------------
# Duck-typed stand-ins for the pinned-ratio tests.
#
# Reused from test_auto_pcb_size_area_estimate.py's pattern -- the estimator
# only consults pad.position, pad.size, footprint.pads, pcb.footprints, and
# pcb.nets, so a minimal dataclass quartet suffices.  We avoid the PCB.load
# round-trip here so the calibration ratios are exact (no rounding from
# s-expression serialisation).
# ---------------------------------------------------------------------------


@dataclass
class _StubPad:
    position: tuple[float, float]
    size: tuple[float, float]


@dataclass
class _StubFootprint:
    pads: list = field(default_factory=list)


@dataclass
class _StubNet:
    name: str


@dataclass
class _StubPCB:
    footprints: list = field(default_factory=list)
    nets: dict = field(default_factory=dict)


def _quad_pad_stub(side_mm: float) -> _StubFootprint:
    """A 4-pad footprint with bbox spanning ``[-side/2..+side/2]`` x ``[-side/2..+side/2]``.

    The pads are 4x4 mm at the four corners of a ``(side - 4) x (side - 4)``
    inner square so the outer pad extents touch the bbox boundary.  This
    matches the s-expression footprint geometry used in the live-fixture
    tests above (bbox = 12 x 12 mm when side_mm=12).
    """
    half = side_mm / 2.0
    pad_half = 2.0  # 4x4 mm pads -> half-extent 2
    inner = half - pad_half  # corner pad-center offset
    return _StubFootprint(
        pads=[
            _StubPad(position=(-inner, -inner), size=(4.0, 4.0)),
            _StubPad(position=(inner, -inner), size=(4.0, 4.0)),
            _StubPad(position=(-inner, inner), size=(4.0, 4.0)),
            _StubPad(position=(inner, inner), size=(4.0, 4.0)),
        ]
    )


def _doomed_stub_pcb() -> _StubPCB:
    """The duck-typed stand-in for ``_PCB_50x50_DOOMED``."""
    return _StubPCB(
        footprints=[_quad_pad_stub(12.0) for _ in range(35)],
        nets={i: _StubNet(name=f"SIG_{i}") for i in range(1, 36)},
    )


def _loose_stub_pcb() -> _StubPCB:
    """The duck-typed stand-in for ``_PCB_100x100_LOOSE``."""
    return _StubPCB(
        footprints=[_quad_pad_stub(12.0) for _ in range(2)],
        nets={i: _StubNet(name=f"SIG_{chr(ord('A') + i - 1)}") for i in range(1, 3)},
    )


# ---------------------------------------------------------------------------
# AC #1: pre-route check skips the doomed L=2 attempt and escalates envelope
# ---------------------------------------------------------------------------


class TestDoomedFixtureSkipAndEscalate:
    """Verify the pre-route check refuses the over-constrained envelope.

    The fixture's area estimate exceeds the 50x50 envelope by ~6x, so the
    pre-route check fires immediately -- the inner router must NOT be
    called at the starting envelope.  The loop must grow to the next
    tier (and beyond if the estimate still doesn't fit).
    """

    def _args(self, pcb_path: Path) -> SimpleNamespace:
        return SimpleNamespace(
            pcb=str(pcb_path),
            output=None,
            manufacturer="jlcpcb",
            auto_layers=True,
            auto_pcb_size=True,
            max_layers=2,  # L=2 -- the "doomed L=2 attempt" the AC names
            min_completion=0.95,
            quiet=False,
            strategy="negotiated",
            packing_overhead=None,  # default 2.5
        )

    def test_doomed_initial_envelope_skips_inner_call(self, tmp_path):
        """At the initial 50x50 envelope, the inner router is NOT called.

        We mock the inner so any call records the envelope it saw.  If the
        pre-route check works correctly, the first recorded envelope (if
        any) must be a GROWN tier, not 50x50.
        """
        from kicad_tools.cli import route_cmd
        from kicad_tools.router.io import extract_board_dimensions

        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_50x50_DOOMED)
        output_path = tmp_path / "routed.kicad_pcb"
        args = self._args(pcb_path)

        envelopes_seen: list[tuple[float, float]] = []

        def fake_inner(pcb_path, output_path, args, quiet):
            dims = extract_board_dimensions(pcb_path)
            if dims is not None:
                envelopes_seen.append(dims)
            # Mock a successful route once the envelope grows enough that
            # the inner is actually called -- this ends the loop cleanly.
            args._last_layer_result = SimpleNamespace(
                nets_routed=35,
                nets_to_route=35,
                overflow=0,
                completion=1.0,
                success=True,
                router=None,
            )
            return 0

        with patch.object(route_cmd, "route_with_layer_escalation", fake_inner):
            route_cmd.route_with_size_escalation(
                pcb_path=pcb_path,
                output_path=output_path,
                args=args,
                quiet=True,
            )

        # The inner router was never called at the doomed 50x50 envelope.
        for w, h in envelopes_seen:
            assert (w, h) != pytest.approx((50.0, 50.0)), (
                f"pre-route check should have skipped 50x50; saw inner call"
                f" with envelope ({w}x{h}). All envelopes: {envelopes_seen}"
            )

    def test_doomed_fixture_grows_envelope_to_higher_tier(self, tmp_path):
        """After pre-route skip(s), the final envelope is at least 100x150.

        The doomed fixture needs the envelope to escalate past 100x100
        (still doomed at ratio 0.67) to 100x150 (ratio ~1.01 -- borderline
        but meets) or beyond, before the heuristic is satisfied.
        """
        from kicad_tools.cli import route_cmd
        from kicad_tools.router.io import extract_board_dimensions

        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_50x50_DOOMED)
        output_path = tmp_path / "routed.kicad_pcb"
        args = self._args(pcb_path)

        def fake_inner(pcb_path, output_path, args, quiet):
            args._last_layer_result = SimpleNamespace(
                nets_routed=35,
                nets_to_route=35,
                overflow=0,
                completion=1.0,
                success=True,
                router=None,
            )
            return 0

        with patch.object(route_cmd, "route_with_layer_escalation", fake_inner):
            route_cmd.route_with_size_escalation(
                pcb_path=pcb_path,
                output_path=output_path,
                args=args,
                quiet=True,
            )

        # Final envelope MUST be strictly larger than 50x50.
        final_dims = extract_board_dimensions(pcb_path)
        assert final_dims is not None
        final_w, final_h = final_dims
        # Past 100x100 at minimum -- the 100x100 tier is still doomed for
        # this fixture (ratio 0.67), so the loop should grow further.
        assert final_w * final_h > 10000.0, (
            f"expected envelope > 10 000 mm^2 (past 100x100), got "
            f"{final_w}x{final_h} = {final_w * final_h:.0f} mm^2"
        )

    def test_doomed_fixture_inner_called_at_passing_tier(self, tmp_path):
        """Once the envelope grows to where the estimate fits, the inner
        router IS called.  This verifies the loop doesn't bail prematurely.
        """
        from kicad_tools.cli import route_cmd

        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_50x50_DOOMED)
        output_path = tmp_path / "routed.kicad_pcb"
        args = self._args(pcb_path)

        call_count = {"n": 0}

        def fake_inner(pcb_path, output_path, args, quiet):
            call_count["n"] += 1
            args._last_layer_result = SimpleNamespace(
                nets_routed=35,
                nets_to_route=35,
                overflow=0,
                completion=1.0,
                success=True,
                router=None,
            )
            return 0

        with patch.object(route_cmd, "route_with_layer_escalation", fake_inner):
            rc = route_cmd.route_with_size_escalation(
                pcb_path=pcb_path,
                output_path=output_path,
                args=args,
                quiet=True,
            )

        # Eventually the inner router runs (after escalation lifts the
        # envelope above the estimate).
        assert call_count["n"] >= 1, (
            "inner router should have been called once the envelope grew"
            " enough to pass the area estimate"
        )
        # Final exit code reflects the successful inner call.
        assert rc == 0


# ---------------------------------------------------------------------------
# AC #2: reactive DRC-density backstop fires when the heuristic underestimates
# ---------------------------------------------------------------------------


class TestHeuristicUnderestimatesBackstop:
    """Inverse case: heuristic says "fits", but real routing struggles.

    The "loose" fixture (100x100 envelope, 2 small components) easily
    passes the pre-route area estimate.  But the mocked inner router
    reports a routing failure with high DRC violation density.  The
    reactive backstop (``should_escalate`` triggered by the
    ``density_threshold_viols_per_cm2`` policy field) must then
    escalate the envelope rather than declaring victory.

    This proves the two checks compose correctly: pre-route is an
    OPTIMISATION, reactive is the CORRECTNESS gate.
    """

    def _args(self, pcb_path: Path) -> SimpleNamespace:
        return SimpleNamespace(
            pcb=str(pcb_path),
            output=None,
            manufacturer="jlcpcb",
            auto_layers=True,
            auto_pcb_size=True,
            max_layers=2,
            min_completion=0.95,
            quiet=False,
            strategy="negotiated",
            packing_overhead=None,
        )

    def test_loose_fixture_passes_pre_route_check(self, tmp_path):
        """Sanity: confirm the fixture's pre-route area estimate is < envelope.

        Without this property the test below proves nothing -- if the
        fixture itself failed the pre-route check we'd be re-testing AC #1
        by accident.
        """
        from kicad_tools.router.io import extract_board_dimensions
        from kicad_tools.schema.pcb import PCB

        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100_LOOSE)
        pcb = PCB.load(pcb_path)

        est = estimate_required_area(pcb, MFR_JLCPCB)
        dims = extract_board_dimensions(pcb_path)
        assert dims is not None
        w, h = dims
        envelope = w * h
        assert envelope_meets_area_estimate(envelope, est), (
            f"fixture {envelope:.0f} mm^2 should meet estimate "
            f"{est.total_mm2:.0f} mm^2 (ratio {envelope / est.total_mm2:.2f})"
        )

    def test_reactive_backstop_escalates_when_inner_fails(self, tmp_path):
        """High DRC density from the inner router triggers ESCALATE.

        Even though the heuristic happily passes the 100x100 envelope,
        the mocked inner router reports 70% reach + 0.88 viols/cm^2
        density (above the 0.5 default threshold).  The reactive
        backstop's ``should_escalate`` returns True, so the loop grows
        to the next tier.
        """
        from kicad_tools.cli import route_cmd
        from kicad_tools.router.io import extract_board_dimensions

        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100_LOOSE)
        output_path = tmp_path / "routed.kicad_pcb"
        args = self._args(pcb_path)

        inner_calls = {"n": 0}

        def fake_inner(pcb_path, output_path, args, quiet):
            inner_calls["n"] += 1
            # First attempt: heuristic underestimates -- envelope passes
            # pre-route, but the actual routing exposes a hot-spot.
            # Density = overflow / board_area_cm2.  Pick overflow that
            # gives density above 0.5 viols/cm^2 default threshold.
            dims = extract_board_dimensions(pcb_path) or (100.0, 100.0)
            area_cm2 = (dims[0] * dims[1]) / 100.0  # 100 cm^2 at 100x100
            if inner_calls["n"] == 1:
                args._last_layer_result = SimpleNamespace(
                    nets_routed=1,
                    nets_to_route=2,
                    overflow=int(0.88 * area_cm2),  # density 0.88 > 0.5
                    completion=0.5,
                    success=False,
                    router=None,
                )
                return 2
            # Second attempt (after grow): success.
            args._last_layer_result = SimpleNamespace(
                nets_routed=2,
                nets_to_route=2,
                overflow=0,
                completion=1.0,
                success=True,
                router=None,
            )
            return 0

        with patch.object(route_cmd, "route_with_layer_escalation", fake_inner):
            rc = route_cmd.route_with_size_escalation(
                pcb_path=pcb_path,
                output_path=output_path,
                args=args,
                quiet=True,
            )

        # Reactive backstop fired: at least two attempts and the second
        # succeeded.
        assert inner_calls["n"] >= 2, (
            f"reactive backstop should have driven a second attempt; "
            f"got {inner_calls['n']} inner call(s)"
        )
        assert rc == 0

        # Envelope grew past 100x100.
        final_dims = extract_board_dimensions(pcb_path)
        assert final_dims is not None
        final_w, final_h = final_dims
        assert final_w > 100.5 or final_h > 100.5, (
            f"envelope should have grown past 100x100; saw {final_w}x{final_h}"
        )


# ---------------------------------------------------------------------------
# AC #3: pinned ratios at packing_overhead=2.5 -- catches heuristic regressions
# ---------------------------------------------------------------------------


class TestPinnedRatios:
    """Pin expected envelope/estimate ratios at packing_overhead=2.5.

    The point is REGRESSION PROTECTION: any silent change to
    ``DEFAULT_PACKING_OVERHEAD``, ``DEFAULT_ROUTING_CHANNEL_PER_NET_MM2``,
    the halo formula, or the bbox computation will move these ratios.
    The assertions are tight enough to catch a constant flip
    (e.g. someone changes 2.5 -> 3.0) but loose enough to absorb
    rounding-level changes (we use rel=1e-3).

    Hand-computed expectations at packing_overhead=2.5 + MFR_JLCPCB
    (min_clearance=0.127):

      Doomed fixture (35 quad-pad parts, side 12 mm, 35 signal nets,
      50x50 envelope):
        footprint_area = 35 * 144 = 5040 mm^2
        halo = 35 * 2*(12+12)*0.127 = 213.36 mm^2
        channels = 35 * 20 = 700 mm^2
        base = 5953.36 mm^2
        total = 14883.4 mm^2
        envelope = 2500 mm^2
        ratio = 0.168 (DOOMED)

      Loose fixture (2 quad-pad parts, side 12 mm, 2 signal nets,
      100x100 envelope):
        footprint_area = 2 * 144 = 288 mm^2
        halo = 2 * 2*(12+12)*0.127 = 12.192 mm^2
        channels = 2 * 20 = 40 mm^2
        base = 340.192 mm^2
        total = 850.48 mm^2
        envelope = 10 000 mm^2
        ratio = 11.76 (PASS)
    """

    # Hand-computed expected totals for each fixture at packing_overhead=2.5.
    EXPECTED_DOOMED_TOTAL_MM2 = 14883.4
    EXPECTED_DOOMED_RATIO = 2500.0 / 14883.4  # 0.168

    EXPECTED_LOOSE_TOTAL_MM2 = 850.48
    EXPECTED_LOOSE_RATIO = 10000.0 / 850.48  # 11.76

    def test_default_packing_overhead_is_2_5(self):
        """First-order regression guard: the documented default is 2.5.

        Any change to the constant requires updating the pinned ratios
        below, so this assertion is the canary.  If someone bumps the
        default from 2.5 to 3.0 without thinking through the calibration
        implications, this test breaks immediately.
        """
        assert DEFAULT_PACKING_OVERHEAD == 2.5
        assert DEFAULT_ROUTING_CHANNEL_PER_NET_MM2 == 20.0

    def test_doomed_fixture_total_matches_hand_math(self):
        """Doomed fixture estimate matches the documented hand-computed value."""
        pcb = _doomed_stub_pcb()
        est = estimate_required_area(pcb, MFR_JLCPCB, packing_overhead=2.5)
        assert est.total_mm2 == pytest.approx(
            self.EXPECTED_DOOMED_TOTAL_MM2, rel=1e-3
        )
        # Component count + signal-net count sanity (catches stub regressions).
        assert est.footprint_count == 35
        assert est.signal_net_count == 35

    def test_doomed_fixture_ratio_below_one(self):
        """Doomed fixture: envelope/estimate ratio is well below 1.0.

        At the 50x50 starting envelope, the ratio is ~0.17 -- this MUST
        stay below 1.0 for the pre-route check to fire.  If a heuristic
        change moves it above 1.0, the doomed fixture stops being doomed
        and the pre-route discriminator loses coverage.
        """
        pcb = _doomed_stub_pcb()
        est = estimate_required_area(pcb, MFR_JLCPCB, packing_overhead=2.5)
        envelope_mm2 = 50.0 * 50.0
        ratio = envelope_mm2 / est.total_mm2
        assert ratio == pytest.approx(self.EXPECTED_DOOMED_RATIO, rel=1e-3)
        assert ratio < 1.0, (
            f"doomed fixture ratio {ratio:.3f} should be < 1.0 to exercise "
            f"the pre-route skip path"
        )

    def test_loose_fixture_total_matches_hand_math(self):
        """Loose fixture estimate matches the documented hand-computed value."""
        pcb = _loose_stub_pcb()
        est = estimate_required_area(pcb, MFR_JLCPCB, packing_overhead=2.5)
        assert est.total_mm2 == pytest.approx(
            self.EXPECTED_LOOSE_TOTAL_MM2, rel=1e-3
        )
        assert est.footprint_count == 2
        assert est.signal_net_count == 2

    def test_loose_fixture_ratio_well_above_one(self):
        """Loose fixture: envelope/estimate ratio is well above 1.0.

        At ratio ~11.76 the loose fixture is comfortably routable per
        the heuristic.  If a constant change moves it BELOW 1.0, the
        inverse-case test would lose coverage (the loose fixture would
        start failing pre-route, and we'd never reach the reactive
        backstop path).  The wide margin (10x above 1.0) is intentional:
        even a 2x bump in packing_overhead leaves us safely > 1.0.
        """
        pcb = _loose_stub_pcb()
        est = estimate_required_area(pcb, MFR_JLCPCB, packing_overhead=2.5)
        envelope_mm2 = 100.0 * 100.0
        ratio = envelope_mm2 / est.total_mm2
        assert ratio == pytest.approx(self.EXPECTED_LOOSE_RATIO, rel=1e-3)
        assert ratio > 10.0, (
            f"loose fixture ratio {ratio:.3f} should be >> 1.0 to exercise "
            f"the reactive backstop path"
        )

    def test_ratio_separation_distinguishes_doomed_from_routable(self):
        """The two fixtures live on opposite sides of the ratio=1.0 boundary.

        This is the headline #3406 acceptance: the heuristic's
        DISCRIMINATIVE POWER is demonstrated by the doomed fixture
        landing below 1.0 and the loose fixture landing above 1.0,
        both at the SAME packing_overhead=2.5.  No tuning trickery.
        """
        doomed = _doomed_stub_pcb()
        loose = _loose_stub_pcb()
        doomed_est = estimate_required_area(doomed, MFR_JLCPCB, packing_overhead=2.5)
        loose_est = estimate_required_area(loose, MFR_JLCPCB, packing_overhead=2.5)
        doomed_ratio = (50.0 * 50.0) / doomed_est.total_mm2
        loose_ratio = (100.0 * 100.0) / loose_est.total_mm2
        assert doomed_ratio < 1.0 < loose_ratio, (
            f"heuristic should discriminate: doomed ratio {doomed_ratio:.3f} "
            f"< 1.0 < loose ratio {loose_ratio:.3f}"
        )

    def test_packing_overhead_3_0_still_separates(self):
        """Bumping packing_overhead to 3.0 keeps the discrimination.

        The 3.0 alternative was discussed in the issue body ("Consider
        whether 2.5 is empirically right, or if a different default
        better separates doomed from tight-but-routable").  Verify that
        at the alternative value, the fixtures STILL fall on opposite
        sides of the boundary -- so the choice between 2.5 and 3.0 is
        a sensitivity question, not a structural one.
        """
        doomed = _doomed_stub_pcb()
        loose = _loose_stub_pcb()
        doomed_est = estimate_required_area(doomed, MFR_JLCPCB, packing_overhead=3.0)
        loose_est = estimate_required_area(loose, MFR_JLCPCB, packing_overhead=3.0)
        doomed_ratio = (50.0 * 50.0) / doomed_est.total_mm2
        loose_ratio = (100.0 * 100.0) / loose_est.total_mm2
        assert doomed_ratio < 1.0 < loose_ratio, (
            f"at packing_overhead=3.0: doomed ratio {doomed_ratio:.3f}, "
            f"loose ratio {loose_ratio:.3f} -- separation should hold"
        )
