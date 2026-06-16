"""Cross-subsystem audit tests for the local->world rotation sign convention.

Regression coverage for issue #3739 (which OVERTURNED #2789/#2778/#2788/#738).

KiCad's ``pcbnew`` 10.0.1 was probed directly (the authoritative engine)
and its forward local->world pad transform uses the *negated* footprint
angle relative to standard CCW math::

    rot_rad = math.radians(-fp.rotation)
    rx = px * cos(rot_rad) - py * sin(rot_rad)
    ry = px * sin(rot_rad) + py * cos(rot_rad)

For a footprint at (100,100) with a pad at local (2,0), pcbnew places the
pad at: deg0 (102,100), deg90 (100,98), deg180 (98,100), deg270 (100,102).
The earlier standard-CCW form (PR #738 / this module's pre-#3739 oracle)
produced the *mirror-image* world positions at 90°/270°, which sent pads
to the wrong half of the board and let ``kct check`` pass copper shorts
that ``kicad-cli`` flagged.

This module's canonical oracle is therefore KiCad's negated-angle
transform (``core.geometry.rotate_pad_offset`` / ``PCB.get_pad_position``).
The negative control still asserts the two sign conventions differ at
{45°, 90°, 270°}; 0°/180° agree under both (the long-known "test trap").

Affected sites covered by this module (one test class each):

    1. ``mcp.tools.routing.route_net`` (line 274)
    2. ``mcp.tools.routing._build_pad_positions`` (line 593)
    3. ``cli.placement_cmd._estimate_routability`` (line 943)
    4. ``reasoning.state.ComponentState._parse_*`` (lines 436, 485-486)
    5. ``router.io.route_pcb`` (line 2244)
    6. ``router.adaptive.AdaptiveAutorouter._add_component_to_router`` (line 181)
    7. ``optim.place_route.PlaceRouteOptimizer._load_components_into_router`` (line 303)

The canonical oracle used by all assertions is ``PCB.get_pad_position``
or the equivalent inline forward transform (the two are identical).
"""

from __future__ import annotations

import contextlib
import math
from pathlib import Path

import pytest

# Tolerance for floating-point pad-position comparisons (mm).
EPS = 1e-9

# Fixture geometry: chosen so that canonical (CCW-positive) and buggy
# (negated) sign conventions produce noticeably different world positions
# at each of {45°, 90°, 270°}.  pad_local = (3.0, 1.0) has nonzero,
# distinct x/y components which is required to break 0°/180° symmetries
# in cos.  See the negative-control test below.
ROTATIONS = [45.0, 90.0, 270.0]
PAD_LOCAL = (3.0, 1.0)
FP_POS = (20.0, 15.0)


def _canonical_world(
    fp_pos: tuple[float, float],
    rotation_deg: float,
    pad_local: tuple[float, float],
) -> tuple[float, float]:
    """Canonical KiCad forward transform (negated footprint angle).

    This is the reference oracle, verified against pcbnew 10.0.1 (#3739).
    It is mathematically identical to ``PCB.get_pad_position`` and the
    shared helper ``core.geometry.rotate_pad_offset``.
    """
    rot_rad = math.radians(-rotation_deg)
    cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
    px, py = pad_local
    return (
        fp_pos[0] + px * cos_r - py * sin_r,
        fp_pos[1] + px * sin_r + py * cos_r,
    )


def _buggy_world(
    fp_pos: tuple[float, float],
    rotation_deg: float,
    pad_local: tuple[float, float],
) -> tuple[float, float]:
    """The (incorrect) standard-CCW transform that PR #738 used."""
    rot_rad = math.radians(rotation_deg)
    cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
    px, py = pad_local
    return (
        fp_pos[0] + px * cos_r - py * sin_r,
        fp_pos[1] + px * sin_r + py * cos_r,
    )


# ---------------------------------------------------------------------------
# Negative control: the fixture geometry actually discriminates between
# the two sign conventions at every rotation we test.
# ---------------------------------------------------------------------------


class TestFixtureDiscriminatesSignConventions:
    """Verify that the chosen fixture would actually fail under buggy code.

    This is a *meta-test* on the test fixture itself.  Without it, a
    silently-passing parametrized test against buggy code would be
    indistinguishable from a correct fix.
    """

    @pytest.mark.parametrize("rotation", ROTATIONS)
    def test_canonical_and_buggy_differ(self, rotation: float) -> None:
        canonical = _canonical_world(FP_POS, rotation, PAD_LOCAL)
        buggy = _buggy_world(FP_POS, rotation, PAD_LOCAL)
        dx = abs(canonical[0] - buggy[0])
        dy = abs(canonical[1] - buggy[1])
        # 0.5 mm is comfortably above any floating-point noise but
        # small enough that almost any meaningful pad geometry produces
        # discrimination at non-axis-aligned rotations.
        assert max(dx, dy) > 0.5, (
            f"rotation={rotation}°: fixture is symmetric — canonical="
            f"{canonical}, buggy={buggy}; tests will silently pass "
            f"against buggy code.  Choose pad_local with nonzero, "
            f"distinct x/y components."
        )

    def test_axis_aligned_rotations_are_blind_spot(self) -> None:
        """Document why the parametrization excludes 0° and 180°.

        At 0°: sin(0) == sin(-0) == 0 and cos(0) == cos(-0) == 1, so
        both conventions produce identical results.

        At 180°: cos(180) == cos(-180) == -1 and sin(180) == -sin(-180)
        but the y-component of pad_local interacts symmetrically; for
        ANY pad_local, the canonical and buggy results are identical.

        This test exists to make that blind spot explicit and to fail
        loudly if someone later "simplifies" the parametrization to
        only test 0° or 180° (which would render the entire module
        useless as bug coverage).
        """
        for rot in (0.0, 180.0):
            canonical = _canonical_world(FP_POS, rot, PAD_LOCAL)
            buggy = _buggy_world(FP_POS, rot, PAD_LOCAL)
            assert canonical == pytest.approx(buggy, abs=1e-12), (
                f"rotation={rot}°: canonical and buggy unexpectedly "
                f"differ — re-check the sign-symmetry argument."
            )


# ---------------------------------------------------------------------------
# Shared minimal PCB fixture.  A single rotated SMD footprint with one
# pad at PAD_LOCAL, an asymmetric offset chosen to discriminate the two
# sign conventions at {45°, 90°, 270°}.
# ---------------------------------------------------------------------------


def _rotated_pcb_text(rotation_deg: float) -> str:
    """KiCad PCB s-expression text with one rotated footprint, one pad.

    Net ``SIG1`` (net 1) carries pad ``U1.1`` only — but since the
    Autorouter requires >= 2 pads on a net to attempt routing, we add
    a second footprint ``U2`` with a single pad on SIG1 at a different
    fixed (rotation=0) position.  Only U1's pad is asymmetrically
    placed; U2's pad is at its footprint origin so rotation has no
    effect on its world position.
    """
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "SIG1")

  (gr_line (start 0 0) (end 80 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 80 0) (end 80 60) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 80 60) (end 0 60) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 60) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "Custom_U1"
    (layer "F.Cu")
    (at {FP_POS[0]} {FP_POS[1]} {rotation_deg})
    (attr smd)
    (property "Reference" "U1")
    (property "Value" "ASYM")
    (pad "1" smd rect (at {PAD_LOCAL[0]} {PAD_LOCAL[1]}) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG1"))
  )

  (footprint "Custom_U2"
    (layer "F.Cu")
    (at 60.0 45.0 0)
    (attr smd)
    (property "Reference" "U2")
    (property "Value" "SYM")
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG1"))
  )
)
"""


# ---------------------------------------------------------------------------
# Site 1: mcp.tools.routing.route_net (line 274)
# ---------------------------------------------------------------------------


class TestRouteNetRotation:
    """``mcp.tools.routing.route_net`` collects pads per footprint and
    forwards them to the Autorouter via ``add_component``.  Site at
    line 274.

    We patch ``Autorouter.add_component`` to capture the pad payloads.
    The rotation regression bites during pad collection — well before
    the actual route attempt — so the test does not need to run the A*
    search, only verify the world positions handed to the router.
    """

    @pytest.mark.parametrize("rotation", ROTATIONS)
    def test_pads_collected_with_canonical_rotation(
        self, tmp_path: Path, rotation: float, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("pydantic")

        pcb_file = tmp_path / "rotated.kicad_pcb"
        pcb_file.write_text(_rotated_pcb_text(rotation))

        captured: dict[str, list[dict]] = {}

        def fake_add_component(self, ref: str, pads: list[dict]) -> None:  # noqa: ANN001
            captured[ref] = list(pads)

        monkeypatch.setattr(
            "kicad_tools.router.core.Autorouter.add_component",
            fake_add_component,
        )

        # Patch route_net (the bound method on the Autorouter instance)
        # so the function returns immediately after pad collection.
        def fake_route_net(self, net_number):  # noqa: ANN001
            return []

        monkeypatch.setattr(
            "kicad_tools.router.core.Autorouter.route_net",
            fake_route_net,
        )

        from kicad_tools.mcp.tools.routing import route_net

        # The route_net result will be "no routes" (because we mocked
        # the routing call), but pad collection still ran.  Best-effort
        # invocation — assertions are on captured.
        with contextlib.suppress(Exception):
            route_net(pcb_path=str(pcb_file), net_name="SIG1")

        u1_pads = captured.get("U1")
        assert u1_pads is not None, f"rotation={rotation}°: route_net did not load U1's pad"
        assert len(u1_pads) == 1
        pad = u1_pads[0]

        expected = _canonical_world(FP_POS, rotation, PAD_LOCAL)
        assert abs(pad["x"] - expected[0]) < EPS, (
            f"rotation={rotation}°: route_net U1.1 x={pad['x']} differs "
            f"from canonical {expected[0]}"
        )
        assert abs(pad["y"] - expected[1]) < EPS, (
            f"rotation={rotation}°: route_net U1.1 y={pad['y']} differs "
            f"from canonical {expected[1]}"
        )


# ---------------------------------------------------------------------------
# Site 2: mcp.tools.routing._build_pad_positions (line 593)
# ---------------------------------------------------------------------------


class TestBuildPadPositionsRotation:
    """``_build_pad_positions`` returns a ``dict[net_number, [(x, y), ...]]``.
    Site at line 593.

    The U1 pad on SIG1 must land at the canonical world position.
    """

    @pytest.mark.parametrize("rotation", ROTATIONS)
    def test_world_position_matches_get_pad_position(self, tmp_path: Path, rotation: float) -> None:
        pytest.importorskip("pydantic")
        from kicad_tools.mcp.tools.routing import _build_pad_positions
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "rotated.kicad_pcb"
        pcb_file.write_text(_rotated_pcb_text(rotation))
        pcb = PCB.load(str(pcb_file))

        expected_u1 = pcb.get_pad_position("U1", "1")
        assert expected_u1 is not None

        positions = _build_pad_positions(pcb)
        sig1_positions = positions[1]
        # SIG1 has two pads: U1.1 (rotated) and U2.1 (at origin offset).
        # Find U1's by closeness to expected.
        u1_actual = min(
            sig1_positions,
            key=lambda p: (p[0] - expected_u1[0]) ** 2 + (p[1] - expected_u1[1]) ** 2,
        )
        assert abs(u1_actual[0] - expected_u1[0]) < EPS, (
            f"rotation={rotation}°: x mismatch — got {u1_actual[0]}, expected {expected_u1[0]}"
        )
        assert abs(u1_actual[1] - expected_u1[1]) < EPS, (
            f"rotation={rotation}°: y mismatch — got {u1_actual[1]}, expected {expected_u1[1]}"
        )


# ---------------------------------------------------------------------------
# Site 3: cli.placement_cmd._estimate_routability (line 943)
# ---------------------------------------------------------------------------


class TestEstimateRoutabilityRotation:
    """``_estimate_routability`` builds a private Autorouter and loads
    every footprint's pads into it via ``router.add_component``.  Site
    at line 943.

    We patch ``Autorouter.add_component`` to capture the pad payloads
    and assert U1's pad-1 lands at the canonical world position.
    """

    @pytest.mark.parametrize("rotation", ROTATIONS)
    def test_pad_world_position_matches_canonical(
        self, tmp_path: Path, rotation: float, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pcb_file = tmp_path / "rotated.kicad_pcb"
        pcb_file.write_text(_rotated_pcb_text(rotation))

        captured: dict[str, list[dict]] = {}

        def fake_add_component(self, ref: str, pads: list[dict]) -> None:  # noqa: ANN001
            captured[ref] = list(pads)

        monkeypatch.setattr(
            "kicad_tools.router.core.Autorouter.add_component",
            fake_add_component,
        )

        # Patch the analyzer so we don't run the (expensive) full
        # routability analysis — pad-loading completes BEFORE the
        # analyzer is constructed, so we still get full coverage of
        # the transform.
        class _FakeReport:
            estimated_success_rate = 1.0
            total_nets = 0
            problem_nets: list = []

        class _FakeAnalyzer:
            def __init__(self, router) -> None:  # noqa: ANN001
                self.router = router

            def analyze(self) -> _FakeReport:
                return _FakeReport()

        monkeypatch.setattr(
            "kicad_tools.router.analysis.RoutabilityAnalyzer",
            _FakeAnalyzer,
        )

        from kicad_tools.cli.placement_cmd import _estimate_routability

        _estimate_routability(pcb_file, quiet=True)

        # U1 was loaded with one pad at PAD_LOCAL.
        u1_pads = captured.get("U1")
        assert u1_pads is not None, "U1 was never loaded into router"
        assert len(u1_pads) == 1
        pad = u1_pads[0]

        expected = _canonical_world(FP_POS, rotation, PAD_LOCAL)
        assert abs(pad["x"] - expected[0]) < EPS, (
            f"rotation={rotation}°: U1 pad x={pad['x']} differs from canonical {expected[0]}"
        )
        assert abs(pad["y"] - expected[1]) < EPS, (
            f"rotation={rotation}°: U1 pad y={pad['y']} differs from canonical {expected[1]}"
        )


# ---------------------------------------------------------------------------
# Site 4: reasoning.state.ComponentState._parse_* (line 436)
# ---------------------------------------------------------------------------


class TestReasoningStatePadRotation:
    """``PCBState.from_pcb`` parses footprints via ``_parse_footprint``
    which precomputes ``cos_r, sin_r`` at line 436 from the footprint
    rotation and passes them to ``_parse_pad`` (lines 485-486) which
    applies the forward local->world transform.

    Verify the resulting ``PadState.x/y`` match the canonical world
    position.
    """

    @pytest.mark.parametrize("rotation", ROTATIONS)
    def test_pad_state_world_position_matches_canonical(
        self, tmp_path: Path, rotation: float
    ) -> None:
        from kicad_tools.reasoning.state import PCBState

        pcb_file = tmp_path / "rotated.kicad_pcb"
        pcb_file.write_text(_rotated_pcb_text(rotation))

        state = PCBState.from_pcb(pcb_file)
        u1 = state.get_component("U1")
        assert u1 is not None, "U1 missing from parsed state"
        assert len(u1.pads) == 1
        pad = u1.pads[0]

        expected = _canonical_world(FP_POS, rotation, PAD_LOCAL)
        assert abs(pad.x - expected[0]) < EPS, (
            f"rotation={rotation}°: U1.1 PadState.x={pad.x} differs from canonical {expected[0]}"
        )
        assert abs(pad.y - expected[1]) < EPS, (
            f"rotation={rotation}°: U1.1 PadState.y={pad.y} differs from canonical {expected[1]}"
        )


# ---------------------------------------------------------------------------
# Site 5: router.io.route_pcb (line 2244)
# ---------------------------------------------------------------------------


class TestRoutePcbRotation:
    """``router.io.route_pcb`` takes a ``components`` list of plain
    dicts (not a PCB object) and applies the forward transform at line
    2244.  Patch ``Autorouter.add_component`` to capture the result.
    """

    @pytest.mark.parametrize("rotation", ROTATIONS)
    def test_pad_world_position_matches_canonical(
        self, rotation: float, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, list[dict]] = {}

        def fake_add_component(self, ref: str, pads: list[dict]) -> None:  # noqa: ANN001
            captured[ref] = list(pads)

        monkeypatch.setattr(
            "kicad_tools.router.core.Autorouter.add_component",
            fake_add_component,
        )

        # Mock the heavy route_all() call too — we only care about the
        # pad-coordinate transform that runs BEFORE routing begins.
        def fake_route_all(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
            return []

        monkeypatch.setattr("kicad_tools.router.core.Autorouter.route_all", fake_route_all)
        monkeypatch.setattr(
            "kicad_tools.router.core.Autorouter.cleanup_artifacts",
            lambda self: None,
        )
        monkeypatch.setattr(
            "kicad_tools.router.core.Autorouter.to_sexp",
            lambda self, **kw: "",
        )
        monkeypatch.setattr(
            "kicad_tools.router.core.Autorouter.get_statistics",
            lambda self: {"routes": 0, "segments": 0, "vias": 0},
        )

        from kicad_tools.router.io import route_pcb

        components = [
            {
                "ref": "U1",
                "x": FP_POS[0],
                "y": FP_POS[1],
                "rotation": rotation,
                "pads": [
                    {
                        "number": "1",
                        "x": PAD_LOCAL[0],
                        "y": PAD_LOCAL[1],
                        "width": 0.6,
                        "height": 0.6,
                        "net": "SIG1",
                    }
                ],
            },
            {
                "ref": "U2",
                "x": 60.0,
                "y": 45.0,
                "rotation": 0.0,
                "pads": [
                    {
                        "number": "1",
                        "x": 0.0,
                        "y": 0.0,
                        "width": 0.6,
                        "height": 0.6,
                        "net": "SIG1",
                    }
                ],
            },
        ]
        net_map = {"SIG1": 1}

        # The patched route_all() returns an empty list; we only care
        # that pad collection ran first.
        with contextlib.suppress(Exception):
            route_pcb(
                board_width=80,
                board_height=60,
                components=components,
                net_map=net_map,
            )

        u1_pads = captured.get("U1")
        assert u1_pads is not None, "U1 not loaded into router"
        assert len(u1_pads) == 1
        pad = u1_pads[0]

        expected = _canonical_world(FP_POS, rotation, PAD_LOCAL)
        assert abs(pad["x"] - expected[0]) < EPS, (
            f"rotation={rotation}°: route_pcb pad x={pad['x']} differs from canonical {expected[0]}"
        )
        assert abs(pad["y"] - expected[1]) < EPS, (
            f"rotation={rotation}°: route_pcb pad y={pad['y']} differs from canonical {expected[1]}"
        )


# ---------------------------------------------------------------------------
# Site 6: router.adaptive.AdaptiveAutorouter._add_component_to_router (line 181)
# ---------------------------------------------------------------------------


class TestAdaptiveRouterRotation:
    """``AdaptiveAutorouter._add_component_to_router`` applies the
    forward transform at line 181 and forwards to ``add_component``.

    Call the method directly to avoid the full adaptive routing flow.
    """

    @pytest.mark.parametrize("rotation", ROTATIONS)
    def test_pad_world_position_matches_canonical(
        self, rotation: float, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kicad_tools.router.adaptive import AdaptiveAutorouter

        components = [
            {
                "ref": "U1",
                "x": FP_POS[0],
                "y": FP_POS[1],
                "rotation": rotation,
                "pads": [
                    {
                        "number": "1",
                        "x": PAD_LOCAL[0],
                        "y": PAD_LOCAL[1],
                        "width": 0.6,
                        "height": 0.6,
                        "net": "SIG1",
                    }
                ],
            }
        ]
        adaptive = AdaptiveAutorouter(
            width=80,
            height=60,
            components=components,
            net_map={"SIG1": 1},
            verbose=False,
        )

        # Capture add_component invocations.
        captured: dict[str, list[dict]] = {}

        class _FakeRouter:
            def add_component(self, ref: str, pads: list[dict]) -> None:
                captured[ref] = list(pads)

        adaptive._add_component_to_router(_FakeRouter(), components[0])

        u1_pads = captured.get("U1")
        assert u1_pads is not None
        assert len(u1_pads) == 1
        pad = u1_pads[0]

        expected = _canonical_world(FP_POS, rotation, PAD_LOCAL)
        assert abs(pad["x"] - expected[0]) < EPS, (
            f"rotation={rotation}°: AdaptiveRouter pad x={pad['x']} "
            f"differs from canonical {expected[0]}"
        )
        assert abs(pad["y"] - expected[1]) < EPS, (
            f"rotation={rotation}°: AdaptiveRouter pad y={pad['y']} "
            f"differs from canonical {expected[1]}"
        )


# ---------------------------------------------------------------------------
# Site 7: optim.place_route.PlaceRouteOptimizer._load_components_into_router
# (line 303)
# ---------------------------------------------------------------------------


class TestPlaceRouteOptimizerLoadRotation:
    """``PlaceRouteOptimizer._load_components_into_router`` is a static
    method that walks ``pcb.footprints`` and loads each footprint's
    pads into an Autorouter via ``add_component``.  Site at line 303.
    """

    @pytest.mark.parametrize("rotation", ROTATIONS)
    def test_pad_world_position_matches_canonical(self, tmp_path: Path, rotation: float) -> None:
        from kicad_tools.optim.place_route import PlaceRouteOptimizer
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "rotated.kicad_pcb"
        pcb_file.write_text(_rotated_pcb_text(rotation))
        pcb = PCB.load(str(pcb_file))

        captured: dict[str, list[dict]] = {}

        class _FakeRouter:
            def add_component(self, ref: str, pads: list[dict]) -> None:
                captured[ref] = list(pads)

        PlaceRouteOptimizer._load_components_into_router(_FakeRouter(), pcb)

        u1_pads = captured.get("U1")
        assert u1_pads is not None, "U1 not loaded into router"
        assert len(u1_pads) == 1
        pad = u1_pads[0]

        expected = pcb.get_pad_position("U1", "1")
        assert expected is not None
        assert abs(pad["x"] - expected[0]) < EPS, (
            f"rotation={rotation}°: PlaceRouteOptimizer pad x={pad['x']} "
            f"differs from canonical {expected[0]}"
        )
        assert abs(pad["y"] - expected[1]) < EPS, (
            f"rotation={rotation}°: PlaceRouteOptimizer pad y={pad['y']} "
            f"differs from canonical {expected[1]}"
        )
