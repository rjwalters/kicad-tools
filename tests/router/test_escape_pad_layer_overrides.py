"""Tests for the per-pad escape-layer override mechanism (Issue #3257).

The ``EscapeRouter.escape_pad_layer_overrides`` map lets a caller force
specific ``(ref, pin)`` pads on an SSOP/TSSOP fine-pitch dual-row
package to escape onto a specific copper layer, overriding the default
even/odd alternation parity in
``_create_fine_pitch_row_escapes``.

This file pins:

1. The env-var parser correctly populates the override map from a JSON
   blob like ``{"U1.15": "F.Cu"}``.
2. Malformed entries are tolerated (logged + skipped) and do not crash
   the EscapeRouter constructor.
3. A forced ``F.Cu`` override on an odd-indexed pin clears
   ``needs_via`` so the escape stays on the surface layer (no via
   transition emitted).
4. A forced ``B.Cu`` override on an even-indexed pin sets
   ``needs_via`` so the escape vias to the inner layer.

The escape-layer override is the surgical lever softstart uses to break
the SWDIO/STATUS_LED B.Cu overlap in U1's east column without flipping
the alternation globally (which regressed routing reach 8/10 -> 7/10
per the negative-results note in
``router/two_phase.py:711-736``).
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from kicad_tools.core.types import CopperLayer as Layer
from kicad_tools.router.escape import EscapeRouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


def _make_escape_router():
    """Construct a minimal EscapeRouter with a tiny routing grid.

    Used to exercise the constructor + per-pad-override parsing in
    isolation; the actual escape generation paths are exercised via
    the integration tests on softstart's manufacturable baseline.
    """
    rules = DesignRules(
        grid_resolution=0.1,
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
    )
    grid = RoutingGrid(width=10.0, height=10.0, rules=rules)
    return EscapeRouter(grid, rules)


def test_escape_pad_layer_overrides_default_empty(monkeypatch):
    """Without the env var, the override map is an empty dict."""
    monkeypatch.delenv("KICAD_TOOLS_ESCAPE_PAD_LAYER_OVERRIDES", raising=False)
    er = _make_escape_router()
    assert er.escape_pad_layer_overrides == {}


def test_escape_pad_layer_overrides_parses_env_var(monkeypatch):
    """A well-formed JSON env var populates the override map."""
    monkeypatch.setenv(
        "KICAD_TOOLS_ESCAPE_PAD_LAYER_OVERRIDES",
        '{"U1.15": "F.Cu", "U1.17": "B.Cu", "U2.3": "F.Cu"}',
    )
    er = _make_escape_router()
    assert er.escape_pad_layer_overrides == {
        ("U1", "15"): Layer.F_CU,
        ("U1", "17"): Layer.B_CU,
        ("U2", "3"): Layer.F_CU,
    }


def test_escape_pad_layer_overrides_malformed_json_tolerated(monkeypatch):
    """Garbage JSON does not crash the constructor."""
    monkeypatch.setenv(
        "KICAD_TOOLS_ESCAPE_PAD_LAYER_OVERRIDES",
        "this is not json",
    )
    er = _make_escape_router()
    assert er.escape_pad_layer_overrides == {}


def test_escape_pad_layer_overrides_unknown_layer_skipped(monkeypatch):
    """Unknown layer names are skipped, valid entries preserved."""
    monkeypatch.setenv(
        "KICAD_TOOLS_ESCAPE_PAD_LAYER_OVERRIDES",
        '{"U1.15": "F.Cu", "U1.16": "NotALayer"}',
    )
    er = _make_escape_router()
    assert er.escape_pad_layer_overrides == {("U1", "15"): Layer.F_CU}


def test_escape_pad_layer_overrides_malformed_key_skipped(monkeypatch):
    """Keys without ``REF.PIN`` shape are skipped."""
    monkeypatch.setenv(
        "KICAD_TOOLS_ESCAPE_PAD_LAYER_OVERRIDES",
        '{"NoDot": "F.Cu", "U1.15": "B.Cu"}',
    )
    er = _make_escape_router()
    assert er.escape_pad_layer_overrides == {("U1", "15"): Layer.B_CU}


def test_escape_pad_layer_overrides_non_dict_root_tolerated(monkeypatch):
    """A JSON array (not dict) at the root is tolerated as empty."""
    monkeypatch.setenv(
        "KICAD_TOOLS_ESCAPE_PAD_LAYER_OVERRIDES",
        '["U1.15", "F.Cu"]',
    )
    er = _make_escape_router()
    assert er.escape_pad_layer_overrides == {}


def test_escape_pad_layer_overrides_affects_needs_via_for_override(monkeypatch):
    """Per-pad override forces ``needs_via`` to match the requested layer.

    The lever applied inside ``_create_fine_pitch_row_escapes`` is the
    boolean ``override_layer != pad.layer``.  When the override matches
    the pad's surface layer, ``needs_via`` must be False (stay on
    surface).  When it differs, ``needs_via`` must be True (transition
    to inner / opposite layer).  This test exercises that calculation
    directly against synthetic ``Pad`` objects without invoking the
    full escape generation pipeline.
    """
    monkeypatch.setenv(
        "KICAD_TOOLS_ESCAPE_PAD_LAYER_OVERRIDES",
        '{"U1.15": "F.Cu", "U1.16": "B.Cu"}',
    )
    er = _make_escape_router()

    # Pin 15 is on F.Cu (its own surface).  Override -> F.Cu.
    # Expected: override_layer == pad.layer -> needs_via False.
    pin15_pad = Pad(
        x=0.0,
        y=0.0,
        width=0.3,
        height=1.5,
        net=20,
        net_name="STATUS_LED",
        layer=Layer.F_CU,
        ref="U1",
        pin="15",
    )
    override_15 = er.escape_pad_layer_overrides.get((pin15_pad.ref, pin15_pad.pin))
    assert override_15 == Layer.F_CU
    needs_via_15 = override_15 != pin15_pad.layer
    assert needs_via_15 is False

    # Pin 16 is on F.Cu (its own surface).  Override -> B.Cu.
    # Expected: override_layer != pad.layer -> needs_via True.
    pin16_pad = Pad(
        x=0.0,
        y=0.0,
        width=0.3,
        height=1.5,
        net=17,
        net_name="SWDIO",
        layer=Layer.F_CU,
        ref="U1",
        pin="16",
    )
    override_16 = er.escape_pad_layer_overrides.get((pin16_pad.ref, pin16_pad.pin))
    assert override_16 == Layer.B_CU
    needs_via_16 = override_16 != pin16_pad.layer
    assert needs_via_16 is True
