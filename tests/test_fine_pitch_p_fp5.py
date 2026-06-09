"""Unit tests for Phase 5 (P_FP5) of the fine-pitch escape ladder.

Issue #3371 / P_FP5 -- verifies the composition of
``--micro-via-in-pad-fallback`` with the ``--auto-layers`` escalation
ladder.  Covers:

1. **Manufacturer-gating helper** (``_mfr_supports_via_in_pad``):
   - ``jlcpcb-tier1`` -> True (via-in-pad-capable tier)
   - ``jlcpcb`` -> False (plain tier-0; via-in-pad is a surcharge)
   - ``pcbway`` -> True (via-in-pad is a standard PCBWay offering)
   - ``oshpark`` -> False (not a standard OSHPark offering)
   - ``None`` -> False (no manufacturer configured)
   - Unknown manufacturer string -> False (defensive fallthrough)

2. **Ladder-interleaving helper** (``_interleave_fine_pitch_fallback_attempts``):
   - ``enabled=False`` preserves the input length and appends
     ``via_in_pad_fallback=False`` to every triple.
   - ``enabled=True`` produces ``2 * len(input)`` triples with a
     baseline (False) immediately followed by a fallback (True) for
     each input attempt.
   - Empty input is preserved (no attempts in, no attempts out).

3. **Per-component escape clearance plumbing**
   (``EscapeRouter._create_staggered_row_escapes``):
   - With a fine-pitch region installed on the grid, the staggered
     SOP escape uses the region's tighter ``escape_clearance`` for
     the launch-step distance.
   - Without an installed region (back-compat path), the legacy
     ``self.escape_clearance`` is used.
   - The narrow-channel guard inside ``_escape_clearance_for_ref``
     still wins -- if the corridor cannot accommodate the tighter
     clearance, the helper falls back to the standard component
     clearance (covered indirectly via the rules-helper guard).

The heavyweight softstart rev B end-to-end reach test lives in
``tests/router/test_softstart_revb_fine_pitch_escape.py`` (already
present from P_FP4; P_FP5 will tighten the floor there).

Issue: https://github.com/rjwalters/kicad-tools/issues/3371
"""

from __future__ import annotations

import pytest

from kicad_tools.cli.route_cmd import (
    _interleave_fine_pitch_fallback_attempts,
    _mfr_supports_via_in_pad,
)
from kicad_tools.router.escape import EscapeDirection, EscapeRouter
from kicad_tools.router.fine_pitch_escape import FinePitchRegion
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


# ============================================================================
# Helpers (mirror P_FP4 fixtures with horizontal SOIC-8 row geometry)
# ============================================================================


def _ucc27211_row(origin_x: float = 50.0, y: float = 50.0) -> list[Pad]:
    """4 pads of a UCC27211 SOIC-8 north row (1.27mm pitch, 0.30 x 1.55 mm)."""
    pads: list[Pad] = []
    for i in range(4):
        pads.append(
            Pad(
                x=origin_x + i * 1.27,
                y=y,
                width=0.30,
                height=1.55,
                net=10 + i,
                net_name=f"NET{i + 1}",
                layer=Layer.F_CU,
                ref="U5",
                pin=str(i + 1),
            )
        )
    return pads


def _make_rules() -> DesignRules:
    """Strict tier-1 recipe (0.20mm clearance, 0.30mm trace)."""
    return DesignRules(
        trace_width=0.30,
        trace_clearance=0.20,
        grid_resolution=0.10,
        via_drill=0.30,
        via_diameter=0.60,
        manufacturer="jlcpcb-tier1",
    )


def _make_grid(rules: DesignRules) -> RoutingGrid:
    return RoutingGrid(
        width=40.0,
        height=40.0,
        rules=rules,
        origin_x=40.0,
        origin_y=40.0,
        layer_stack=LayerStack.two_layer(),
    )


def _make_router(rules: DesignRules) -> EscapeRouter:
    grid = _make_grid(rules)
    return EscapeRouter(grid, rules)


# ============================================================================
# 1. Manufacturer-gating helper
# ============================================================================


class TestMfrSupportsViaInPad:
    """Cover the via-in-pad capability look-up used by the auto-layers compose."""

    def test_jlcpcb_tier1_supports(self):
        """The JLCPCB tier-1 / Capability+ profile supports via-in-pad."""
        assert _mfr_supports_via_in_pad("jlcpcb-tier1") is True

    def test_jlcpcb_tier0_does_not_support(self):
        """The base JLCPCB tier does not include via-in-pad processing."""
        assert _mfr_supports_via_in_pad("jlcpcb") is False

    def test_pcbway_supports(self):
        """PCBWay offers via-in-pad as a standard option."""
        assert _mfr_supports_via_in_pad("pcbway") is True

    def test_oshpark_does_not_support(self):
        """OSHPark does not offer via-in-pad as a standard tier."""
        assert _mfr_supports_via_in_pad("oshpark") is False

    def test_none_does_not_support(self):
        """No manufacturer configured -> compose is a no-op."""
        assert _mfr_supports_via_in_pad(None) is False

    def test_empty_string_does_not_support(self):
        """Empty string treated as no-manufacturer (defensive)."""
        assert _mfr_supports_via_in_pad("") is False

    def test_unknown_does_not_support(self):
        """Unknown manufacturer name falls through to False."""
        assert _mfr_supports_via_in_pad("unknown-pcb-fab") is False


# ============================================================================
# 2. Ladder-interleaving helper
# ============================================================================


class TestInterleaveFallback:
    """Cover the auto-layers ladder transformation."""

    def test_disabled_returns_input_length(self):
        """With ``enabled=False`` the ladder length matches the input exactly."""
        out = _interleave_fine_pitch_fallback_attempts(
            [(2, "stack-2L"), (4, "stack-4L")],
            enabled=False,
        )
        assert len(out) == 2
        assert all(triple[2] is False for triple in out)
        assert out[0][:2] == (2, "stack-2L")
        assert out[1][:2] == (4, "stack-4L")

    def test_enabled_doubles_length(self):
        """With ``enabled=True`` each input entry becomes (baseline, fallback)."""
        out = _interleave_fine_pitch_fallback_attempts(
            [(2, "stack-2L"), (4, "stack-4L")],
            enabled=True,
        )
        assert len(out) == 4
        assert out[0] == (2, "stack-2L", False)
        assert out[1] == (2, "stack-2L", True)
        assert out[2] == (4, "stack-4L", False)
        assert out[3] == (4, "stack-4L", True)

    def test_empty_input_preserved(self):
        """Empty input -> empty output, regardless of ``enabled``."""
        assert _interleave_fine_pitch_fallback_attempts([], enabled=False) == []
        assert _interleave_fine_pitch_fallback_attempts([], enabled=True) == []

    def test_single_input_doubled(self):
        """Single attempt -> baseline + fallback when enabled."""
        out = _interleave_fine_pitch_fallback_attempts(
            [(2, "stack-2L")],
            enabled=True,
        )
        assert out == [(2, "stack-2L", False), (2, "stack-2L", True)]


# ============================================================================
# 3. Per-component escape clearance plumbing in staggered SOP path
# ============================================================================


class TestStaggeredRowUsesPerComponentClearance:
    """Cover the SOP staggered path's per-component clearance lookup.

    Issue #3371 / P_FP5: ``_create_staggered_row_escapes`` consults
    ``self._escape_clearance_for_ref(ref, pads)`` to pick a tighter
    launch-step distance when the package sits in an installed
    fine-pitch escape region.  This test pins the wiring contract:
    with a region installed, the via row is closer to the pads than
    the default ``self.escape_clearance + trace_width`` distance.
    """

    def test_in_region_shortens_launch_step(self):
        """With a fine-pitch region installed, the launch step shrinks."""
        rules = _make_rules()
        router = _make_router(rules)
        pads = _ucc27211_row()

        # Install a fine-pitch region for U5 with tighter escape clearance.
        # The baseline ``self.escape_clearance`` is
        # ``rules.trace_clearance * 2`` = 0.40mm; the region's escape
        # clearance is the manufacturer floor + safety margin = 0.140mm,
        # so the corridor effectively shrinks by 0.26mm.
        region = FinePitchRegion(
            package_ref="U5",
            package_origin=(pads[0].x + 1.5 * 1.27, pads[0].y),
            radius_mm=5.0,
            pin_pitch=1.27,
            pad_size_along_pitch=0.30,
            escape_clearance=0.14,
        )
        router.grid.set_fine_pitch_regions([region])

        baseline_escapes = router._create_staggered_row_escapes(
            pads=pads, direction=EscapeDirection.NORTH, package=_FakePackage(pads),
        )
        # The via must sit closer to the pad than the legacy
        # ``escape_clearance + trace_width`` distance would put it.
        legacy_dist = router.escape_clearance + router.rules.trace_width  # 0.40 + 0.30 = 0.70
        assert legacy_dist == pytest.approx(0.70)

        # Pin 0 is even (i=0), so no stagger offset -- via_y is at
        # pad.y + base_escape_dist (NORTH direction, so dy=+1).
        # With the region installed, base_escape_dist should be
        # 0.14 + 0.30 = 0.44mm < 0.70mm.
        first = baseline_escapes[0]
        via_x, via_y = first.via_pos  # type: ignore[misc]
        pad = first.pad
        via_dist = via_y - pad.y
        assert via_dist == pytest.approx(0.44, abs=0.01), (
            f"Expected via_dist 0.44mm in fine-pitch region; got {via_dist:.3f}mm"
        )

    def test_out_of_region_preserves_legacy_distance(self):
        """Without a region, the legacy launch-step distance is preserved."""
        rules = _make_rules()
        router = _make_router(rules)
        pads = _ucc27211_row()

        # No regions installed -- helper returns the standard
        # ``get_clearance_for_component`` value, which is
        # ``rules.trace_clearance`` (0.20mm) for non-fine-pitch refs.
        # Per the P_FP5 wiring, we only take the shrink when
        # ``per_ref_clearance < rules.trace_clearance`` (signal that a
        # region matched and the narrow-channel guard allowed the
        # shrink); the equal case falls through to the legacy
        # ``self.escape_clearance`` (0.40mm) baseline.
        router.grid.set_fine_pitch_regions([])

        escapes = router._create_staggered_row_escapes(
            pads=pads, direction=EscapeDirection.NORTH, package=_FakePackage(pads),
        )
        first = escapes[0]
        via_x, via_y = first.via_pos  # type: ignore[misc]
        pad = first.pad
        via_dist = via_y - pad.y
        legacy_dist = router.escape_clearance + router.rules.trace_width
        assert via_dist == pytest.approx(legacy_dist, abs=0.01), (
            f"Expected legacy launch dist {legacy_dist:.3f}mm with no region; "
            f"got {via_dist:.3f}mm"
        )

    def test_out_of_region_ref_preserves_legacy(self):
        """A region for ref 'U6' must not affect a package with ref 'U5'."""
        rules = _make_rules()
        router = _make_router(rules)
        pads = _ucc27211_row()  # ref="U5"

        # Region installed for a DIFFERENT ref.
        region = FinePitchRegion(
            package_ref="U6",  # not the package being routed
            package_origin=(200.0, 200.0),  # far from the U5 pads
            radius_mm=5.0,
            pin_pitch=1.27,
            pad_size_along_pitch=0.30,
            escape_clearance=0.14,
        )
        router.grid.set_fine_pitch_regions([region])

        escapes = router._create_staggered_row_escapes(
            pads=pads, direction=EscapeDirection.NORTH, package=_FakePackage(pads),
        )
        first = escapes[0]
        via_x, via_y = first.via_pos  # type: ignore[misc]
        pad = first.pad
        via_dist = via_y - pad.y
        legacy_dist = router.escape_clearance + router.rules.trace_width
        assert via_dist == pytest.approx(legacy_dist, abs=0.01), (
            f"Out-of-region ref must not pick up tighter clearance; "
            f"expected {legacy_dist:.3f}mm, got {via_dist:.3f}mm"
        )


# ============================================================================
# 4. Auto-layers ladder composition gating (regression)
# ============================================================================


class TestLadderCompositionGating:
    """Without the new compose, the ladder shape is bit-for-bit P_FP4.

    Pinning this prevents accidental regressions in the ladder shape
    when the user has NOT opted into the new behaviour.  Three vectors:

    1. **No manufacturer**: compose is disabled -> ladder is the
       single-tuple-per-layer shape with ``via_in_pad_fallback=False``.
    2. **Tier-0 manufacturer (jlcpcb)**: compose is disabled -> ladder
       shape preserved (mfr does not support via-in-pad).
    3. **Tier-1 manufacturer + explicit flag**: ladder shape preserved
       because explicit ``--micro-via-in-pad-fallback`` implies "stay
       on for every attempt", not "interleave".
    """

    def test_tier0_jlcpcb_disables_compose(self):
        """jlcpcb (no via-in-pad) -> compose is a no-op (gate is False)."""
        # Equivalent gate computation as in route_with_layer_escalation.
        user_explicit_fallback = False  # i.e. CLI flag not passed
        enabled = not user_explicit_fallback and _mfr_supports_via_in_pad("jlcpcb")
        assert enabled is False
        ladder = _interleave_fine_pitch_fallback_attempts(
            [(2, "2L"), (4, "4L"), (6, "6L")],
            enabled=enabled,
        )
        assert len(ladder) == 3  # one entry per layer, no interleave
        assert all(t[2] is False for t in ladder)

    def test_no_manufacturer_disables_compose(self):
        """Missing manufacturer -> compose is a no-op."""
        enabled = not False and _mfr_supports_via_in_pad(None)
        assert enabled is False

    def test_tier1_with_explicit_flag_disables_compose(self):
        """Explicit ``--micro-via-in-pad-fallback`` keeps original ladder."""
        user_explicit_fallback = True  # i.e. CLI flag passed
        enabled = (
            not user_explicit_fallback
            and _mfr_supports_via_in_pad("jlcpcb-tier1")
        )
        assert enabled is False
        ladder = _interleave_fine_pitch_fallback_attempts(
            [(2, "2L"), (4, "4L")],
            enabled=enabled,
        )
        # With explicit user flag, the ladder retains pre-P_FP5 shape.
        # The env var stamping (handled by the CLI dispatch elsewhere)
        # keeps the fallback ON for every attempt.
        assert len(ladder) == 2
        assert all(t[2] is False for t in ladder)

    def test_tier1_default_enables_compose(self):
        """jlcpcb-tier1 default flow -> compose interleaves the ladder."""
        user_explicit_fallback = False
        enabled = (
            not user_explicit_fallback
            and _mfr_supports_via_in_pad("jlcpcb-tier1")
        )
        assert enabled is True
        ladder = _interleave_fine_pitch_fallback_attempts(
            [(2, "2L"), (4, "4L"), (6, "6L")],
            enabled=enabled,
        )
        # 3 -> 6 entries.
        assert len(ladder) == 6
        # Baseline / fallback alternation.
        assert [t[2] for t in ladder] == [False, True, False, True, False, True]


# ============================================================================
# Fake PackageInfo wrapper (we only need ``ref`` + ``pads`` + ``center``)
# ============================================================================


class _FakePackage:
    """Tiny stand-in for ``PackageInfo`` -- only the fields the staggered
    SOP path reads.  Avoids the cost of running ``analyze_package`` on a
    synthetic 4-pad row that does not look like a real SOP to the
    package classifier.
    """

    def __init__(self, pads: list[Pad]):
        self.pads = pads
        self.ref = pads[0].ref
        xs = [p.x for p in pads]
        ys = [p.y for p in pads]
        self.center = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
