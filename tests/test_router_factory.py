"""Tests for the placement-to-router bridge (Issue #2720, KiCad-2).

Verifies that :class:`PlacementRouterFactory`:

* Caches per-pad local offsets relative to component reference positions.
* Applies the candidate placement transform on every call without mutating
  the base router.
* Falls back to base positions for components missing from the candidate dict.
* Rotates pad offsets by ``(new_rotation - base_rotation)`` correctly.
* Is callable with the ``RouterFactory`` signature consumed by
  :class:`~kicad_tools.router.evaluators.CppAstarRoutingEvaluator`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pytest

from kicad_tools.optim.router_factory import (
    PlacementRouterFactory,
    _build_factory_from_router,
    build_pcb_router_factory,
)


# ---------------------------------------------------------------------------
# Lightweight fakes — just enough surface for the factory to deep-copy and
# mutate.  We deliberately avoid pulling in the real Autorouter (which would
# require loading a PCB file).
# ---------------------------------------------------------------------------


@dataclass
class _FakePad:
    x: float
    y: float
    width: float = 0.5
    height: float = 0.5
    net: int = 0
    net_name: str = ""
    ref: str = ""
    pin: str = ""


@dataclass
class _FakeRouter:
    pads: dict = field(default_factory=dict)
    nets: dict = field(default_factory=dict)


def _two_pad_router() -> _FakeRouter:
    """Two pads on net 1: U1.1 at (0, 0), U2.1 at (10, 0)."""
    pads = {
        ("U1", "1"): _FakePad(x=0.0, y=0.0, net=1, ref="U1", pin="1"),
        ("U2", "1"): _FakePad(x=10.0, y=0.0, net=1, ref="U2", pin="1"),
    }
    return _FakeRouter(pads=pads, nets={1: [("U1", "1"), ("U2", "1")]})


def _two_pin_component_router() -> _FakeRouter:
    """One component (U1) with two pads at (0,0) and (2,0)."""
    pads = {
        ("U1", "1"): _FakePad(x=0.0, y=0.0, net=1, ref="U1", pin="1"),
        ("U1", "2"): _FakePad(x=2.0, y=0.0, net=2, ref="U1", pin="2"),
    }
    return _FakeRouter(
        pads=pads,
        nets={1: [("U1", "1")], 2: [("U1", "2")]},
    )


# ---------------------------------------------------------------------------
# _build_factory_from_router unit tests
# ---------------------------------------------------------------------------


class TestBuildFactoryFromRouter:
    def test_derives_centroid_when_no_explicit_position(self):
        """Without component_positions, base_x/y == centroid of pads."""
        router = _two_pin_component_router()
        factory = _build_factory_from_router(router, component_positions=None)
        # Centroid of (0,0) and (2,0) = (1, 0)
        assert factory.component_refs["U1"].base_x == pytest.approx(1.0)
        assert factory.component_refs["U1"].base_y == pytest.approx(0.0)
        assert factory.component_refs["U1"].base_rotation == 0.0

    def test_uses_explicit_position(self):
        """When component_positions provided, that wins over centroid."""
        router = _two_pin_component_router()
        factory = _build_factory_from_router(
            router, component_positions={"U1": (50.0, 60.0, 90.0)}
        )
        assert factory.component_refs["U1"].base_x == pytest.approx(50.0)
        assert factory.component_refs["U1"].base_y == pytest.approx(60.0)
        assert factory.component_refs["U1"].base_rotation == pytest.approx(90.0)

    def test_pad_offsets_are_local_to_centroid(self):
        """Each pad's local offset = pad_pos - component_centroid."""
        router = _two_pin_component_router()
        factory = _build_factory_from_router(router, component_positions=None)
        offs = {(o.ref, o.pin): (o.local_dx, o.local_dy) for o in factory.pad_offsets}
        # Centroid is (1, 0), so pin 1 at (0, 0) -> (-1, 0); pin 2 at (2, 0) -> (1, 0)
        assert offs[("U1", "1")] == pytest.approx((-1.0, 0.0))
        assert offs[("U1", "2")] == pytest.approx((1.0, 0.0))


# ---------------------------------------------------------------------------
# PlacementRouterFactory __call__ tests
# ---------------------------------------------------------------------------


class TestFactoryCall:
    def test_returns_shared_base_router(self):
        """The factory returns the SAME base router (mutated in place).

        The shared-base + in-place-mutate strategy is required because the
        real Autorouter holds C++-extension state that cannot be deep-copied.
        Concurrency safety is documented as the caller's responsibility (the
        evaluator default ``num_workers=1`` enforces serial calls).
        """
        router = _two_pad_router()
        factory = _build_factory_from_router(router, component_positions=None)

        new_router = factory({"U1": (100.0, 0.0)}, {"U1": 0.0})

        # Pad position is mutated.
        assert new_router.pads[("U1", "1")].x == pytest.approx(100.0)
        # And the returned router IS the base.
        assert new_router is router

    def test_translates_pad_to_new_position(self):
        """Moving U1 to (50, 50) should put pad U1.1 at (50, 50)."""
        router = _two_pad_router()
        factory = _build_factory_from_router(router, component_positions=None)
        new_router = factory({"U1": (50.0, 50.0)}, {"U1": 0.0})
        assert new_router.pads[("U1", "1")].x == pytest.approx(50.0)
        assert new_router.pads[("U1", "1")].y == pytest.approx(50.0)

    def test_missing_component_uses_base_position(self):
        """When candidate omits a ref, it stays at its base position."""
        router = _two_pad_router()
        factory = _build_factory_from_router(router, component_positions=None)
        new_router = factory({"U1": (100.0, 100.0)}, {"U1": 0.0})
        # U2 was not in positions dict -> should stay at base (10, 0).
        assert new_router.pads[("U2", "1")].x == pytest.approx(10.0)
        assert new_router.pads[("U2", "1")].y == pytest.approx(0.0)

    def test_rotation_rotates_pad_offset(self):
        """A 90° rotation should rotate pad offsets accordingly."""
        # Two-pin component with pin 2 at +x offset.
        router = _two_pin_component_router()
        factory = _build_factory_from_router(router, component_positions=None)
        # Place U1 at origin and rotate 90°: pin 2 (offset +1, 0) becomes (0, +1).
        new_router = factory({"U1": (0.0, 0.0)}, {"U1": 90.0})
        assert new_router.pads[("U1", "2")].x == pytest.approx(0.0, abs=1e-9)
        assert new_router.pads[("U1", "2")].y == pytest.approx(1.0)
        # Pin 1 (offset -1, 0) becomes (0, -1).
        assert new_router.pads[("U1", "1")].x == pytest.approx(0.0, abs=1e-9)
        assert new_router.pads[("U1", "1")].y == pytest.approx(-1.0)

    def test_translation_after_rotation(self):
        """Combined: move centroid to (10, 20) and rotate 180°."""
        router = _two_pin_component_router()
        factory = _build_factory_from_router(router, component_positions=None)
        new_router = factory({"U1": (10.0, 20.0)}, {"U1": 180.0})
        # Pin 2 offset (1, 0) under 180° rotation = (-1, 0); + centroid (10, 20)
        # = (9, 20).
        assert new_router.pads[("U1", "2")].x == pytest.approx(9.0)
        assert new_router.pads[("U1", "2")].y == pytest.approx(20.0)

    def test_multiple_calls_apply_latest_placement(self):
        """Each call overwrites the previous placement in-place.

        Because we mutate the base router, the *latest* call's positions
        win. ``r1`` and ``r2`` are the same object — both reflect the most
        recent placement.
        """
        router = _two_pad_router()
        factory = _build_factory_from_router(router, component_positions=None)
        r1 = factory({"U1": (10.0, 0.0)}, {"U1": 0.0})
        r2 = factory({"U1": (20.0, 0.0)}, {"U1": 0.0})
        # Both references point at the same router; latest-write wins.
        assert r1 is r2
        assert r2.pads[("U1", "1")].x == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# build_pcb_router_factory entry point — light smoke test (pulls in the
# real load_pcb_for_routing path; uses a small in-tree fixture).
# ---------------------------------------------------------------------------


class TestBuildPcbRouterFactory:
    def test_loads_real_pcb(self):
        """Smoke test: construct a factory from the simplest in-tree board."""
        from pathlib import Path

        pcb_dir = (
            Path(__file__).parent.parent
            / "boards"
            / "01-voltage-divider"
            / "output"
        )
        kicad_pcbs = list(pcb_dir.glob("*.kicad_pcb")) if pcb_dir.exists() else []
        if not kicad_pcbs:
            pytest.skip("No in-tree .kicad_pcb fixture available")

        factory = build_pcb_router_factory(
            kicad_pcbs[0],
            validate_drc=False,
            auto_adjust_grid=True,
        )

        assert isinstance(factory, PlacementRouterFactory)
        assert len(factory.component_refs) > 0
        assert len(factory.pad_offsets) > 0

        # Now exercise the call: pass the discovered base positions back.
        # Calling with the base positions should reproduce the original pad
        # coordinates (within FP tolerance) on the (in-place mutated) router.
        positions = {
            ref: (cref.base_x, cref.base_y)
            for ref, cref in factory.component_refs.items()
        }
        rotations = {ref: cref.base_rotation for ref, cref in factory.component_refs.items()}
        returned_router = factory(positions, rotations)
        # The factory returns the base router by design (in-place mutation).
        assert returned_router is factory.base_router
        # Pad positions match what we asked for.
        for off in factory.pad_offsets:
            cref = factory.component_refs[off.ref]
            expected_x = cref.base_x + off.local_dx
            expected_y = cref.base_y + off.local_dy
            actual = returned_router.pads.get((off.ref, off.pin))
            assert actual is not None
            assert actual.x == pytest.approx(expected_x, abs=1e-6)
            assert actual.y == pytest.approx(expected_y, abs=1e-6)
