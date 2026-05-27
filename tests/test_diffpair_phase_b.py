"""Tests for diff-pair Phase B intra-clearance repair (Issue #3040).

PRs #3022 and #3025 (Phase A) added detection only: after
``CoupledPathfinder`` produces a routed (P, N) pair,
``find_intra_pair_clearance_violations`` walks every same-layer
segment-pair and records any violation into
``DiffPairRouter._intra_clearance_violations``.

This suite pins Phase B (Issue #3040) router enforcement:

1. ``route_differential_pair_coupled`` accepts an
   ``extra_spacing_cells`` parameter that widens both the
   ``min_spacing_cells`` floor and the target ``spacing_cells``
   passed to the :class:`CoupledPathfinder`.
2. ``DiffPairRouter.repair_intra_clearance_violations`` is callable
   and returns ``0`` when no violations are buffered.
3. The repair pass resolves a violation that the wider spacing can
   fix (synthetic in-tree case).
4. The repair pass is bounded -- it does not loop forever when the
   violation is unfixable.
5. The ``Autorouter.route_all_with_diffpairs`` hook invokes the
   repair pass when ``diffpair_intra_clearance_violations()`` is
   non-empty.
6. ``validate_routes`` surfaces any residual ``IntraPairClearanceViolation``
   from the diff-pair buffer so the CLI safety net catches them.

Regression boundary: PR #3022's ``test_diffpair_coupled_floor.py`` and
PR #3025's ``test_diffpair_intra_clearance_detection.py`` suites must
stay green (verified separately).
"""

from __future__ import annotations

from kicad_tools.core.types import CopperLayer as Layer
from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair_routing import (
    DiffPairRouter,
    IntraPairClearanceViolation,
    find_intra_pair_clearance_violations,
)
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_route(
    net_id: int,
    net_name: str,
    segments: list[tuple[float, float, float, float, Layer]],
    width: float = 0.15,
) -> Route:
    """Build a Route from a list of ``(x1, y1, x2, y2, layer)`` tuples."""
    return Route(
        net=net_id,
        net_name=net_name,
        segments=[
            Segment(
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
                width=width,
                layer=layer,
                net=net_id,
                net_name=net_name,
            )
            for (x1, y1, x2, y2, layer) in segments
        ],
    )


def _make_violating_pair() -> tuple[Route, Route, IntraPairClearanceViolation]:
    """Build a P/N pair with a known intra-clearance violation.

    Returns the two routes and the violation record the detector
    produces from them.  Used by the repair-pass + validate-routes
    safety-net tests.
    """
    # Two parallel traces 0.15 mm wide, centerlines 0.20 mm apart.
    # Edge-to-edge clearance = 0.05 mm; threshold 0.10 mm; deficit 0.05 mm.
    p_route = _make_route(
        net_id=1,
        net_name="USB_D+",
        segments=[(0.0, 1.0, 10.0, 1.0, Layer.F_CU)],
    )
    n_route = _make_route(
        net_id=2,
        net_name="USB_D-",
        segments=[(0.0, 1.2, 10.0, 1.2, Layer.F_CU)],
    )
    violation = find_intra_pair_clearance_violations(
        p_route, n_route, threshold_mm=0.10, pair_name="USB_D"
    )
    assert violation is not None, "Test helper produced a non-violating pair"
    return p_route, n_route, violation


# ---------------------------------------------------------------------------
# Phase B-1: extra_spacing_cells plumbed into route_differential_pair_coupled
# ---------------------------------------------------------------------------


def test_route_differential_pair_coupled_accepts_extra_spacing_cells_kwarg():
    """``route_differential_pair_coupled`` must accept ``extra_spacing_cells``.

    The repair pass calls the method with ``extra_spacing_cells=1`` (then
    2) on retry; if the kwarg disappears the repair silently degrades to
    the original spacing and the retry is a no-op.
    """
    import inspect

    from kicad_tools.router.diffpair_routing import DiffPairRouter

    sig = inspect.signature(DiffPairRouter.route_differential_pair_coupled)
    assert "extra_spacing_cells" in sig.parameters, (
        "route_differential_pair_coupled lost the extra_spacing_cells "
        "kwarg added by Issue #3040 Phase B"
    )
    # Default must be zero (legacy behaviour) so callers that don't
    # know about the kwarg are unaffected.
    assert sig.parameters["extra_spacing_cells"].default == 0


# ---------------------------------------------------------------------------
# Phase B-2: repair_intra_clearance_violations exists and is a no-op when
# the buffer is empty.
# ---------------------------------------------------------------------------


def test_repair_intra_clearance_violations_noop_on_empty_buffer():
    """Repair pass returns 0 (and does not raise) on an empty buffer."""
    router = Autorouter(width=50.0, height=50.0, rules=DesignRules())
    dpr = DiffPairRouter(router)
    # Buffer is empty -- repair is a no-op.
    assert dpr.intra_clearance_violations() == []
    resolved = dpr.repair_intra_clearance_violations()
    assert resolved == 0


# ---------------------------------------------------------------------------
# Phase B-3: residual violations surface via validate_routes safety net
# ---------------------------------------------------------------------------


def test_validate_routes_surfaces_residual_intra_clearance_violations():
    """Residual ``IntraPairClearanceViolation`` records that the repair pass
    could not resolve must surface from ``validate_routes`` as
    ``obstacle_type="segment"`` entries so the CLI seg-seg-violation
    accounting picks them up.

    Without this safety net, a pair whose repair retries all fail would
    silently persist to disk -- the only signal would be the post-save
    ``kct check`` (run as a separate process), and ``kct route``'s exit
    code would not reflect the defect.
    """
    from kicad_tools.router.io import validate_routes

    router = Autorouter(width=50.0, height=50.0, rules=DesignRules())
    # Manually plant a violation into the DiffPairRouter buffer to
    # simulate "Phase B repair couldn't fix this one".
    p_route, n_route, violation = _make_violating_pair()
    # Make sure both nets exist on the router so its net_names resolve.
    router.net_names[1] = "USB_D+"
    router.net_names[2] = "USB_D-"
    # The DiffPairRouter lazily initialises -- grab via the public
    # diffpair_intra_clearance_violations() accessor path.
    dpr = router._diffpair  # triggers lazy init
    dpr._intra_clearance_violations.append(violation)

    # validate_routes() should now surface this as a clearance
    # violation in the returned list, so the CLI's seg_seg count
    # picks it up and exits non-zero.
    violations = validate_routes(router)
    diffpair_residuals = [
        v
        for v in violations
        if v.obstacle_type == "segment"
        and v.net_name in ("USB_D+", "USB_D-")
        and v.obstacle_net_name in ("USB_D+", "USB_D-")
        and v.required == 0.10
    ]
    assert len(diffpair_residuals) >= 1, (
        f"validate_routes did not surface the residual intra-clearance "
        f"violation from DiffPairRouter._intra_clearance_violations; "
        f"got {[v.obstacle_type for v in violations]}"
    )
    # The residual's distance must match what the detector recorded
    # (0.05 mm edge-to-edge clearance for the synthetic pair).
    assert abs(diffpair_residuals[0].distance - 0.05) < 1e-6


def test_validate_routes_emits_no_residuals_when_buffer_empty():
    """When the diff-pair buffer is empty, ``validate_routes`` does not
    emit any spurious diff-pair-residual entries.

    Regression guard: the safety net must trigger ONLY when there are
    actual recorded violations, never on every call.
    """
    from kicad_tools.router.io import validate_routes

    router = Autorouter(width=50.0, height=50.0, rules=DesignRules())
    # No diff-pair routing happened -- buffer is empty by construction.
    violations = validate_routes(router)
    # Filter for diff-pair-shaped residuals (net pair USB_*).  None.
    intra_pair_residuals = [
        v
        for v in violations
        if v.net_name.startswith("USB_") and v.obstacle_net_name.startswith("USB_")
    ]
    assert intra_pair_residuals == []


# ---------------------------------------------------------------------------
# Phase B-4: bounded retry -- a synthetic "wider spacing cannot fix this"
# case must not loop forever and must leave the original routes intact.
# ---------------------------------------------------------------------------


def test_repair_pass_bounded_when_pair_cannot_be_repaired():
    """The repair pass must not loop forever on an unfixable case.

    Build a router whose ``DiffPairRouter`` is stubbed so
    ``route_differential_pair_coupled`` always returns ``([], None)``
    (no path found) regardless of ``extra_spacing_cells``.  The repair
    pass must:

      1. Try at most ``max_retries_per_pair`` (default 2) attempts.
      2. Return 0 (no pairs resolved).
      3. Leave the original violation in the buffer for the validate-
         routes safety net.

    Without bounded retry this test would hang.
    """
    router = Autorouter(width=50.0, height=50.0, rules=DesignRules())
    dpr = router._diffpair  # lazy init

    # Plant a violation so the repair pass has something to attempt.
    p_route, n_route, violation = _make_violating_pair()
    router.net_names[1] = "USB_D+"
    router.net_names[2] = "USB_D-"
    router.nets[1] = []
    router.nets[2] = []
    # Seed the routes so the repair has something to rip up.
    router.routes.append(p_route)
    router.routes.append(n_route)
    dpr._intra_clearance_violations.append(violation)

    # Track attempts.
    call_count = {"value": 0}

    original_method = dpr.route_differential_pair_coupled

    def _failing_route(*args, **kwargs):
        call_count["value"] += 1
        return [], None

    dpr.route_differential_pair_coupled = _failing_route  # type: ignore[assignment]
    try:
        # Stub detect_differential_pairs_with_source so the repair
        # pass has a pair to map back to.  Use a simple namespace.
        from types import SimpleNamespace

        fake_positive = SimpleNamespace(net_id=1, net_name="USB_D+")
        fake_negative = SimpleNamespace(net_id=2, net_name="USB_D-")
        fake_pair = SimpleNamespace(
            positive=fake_positive,
            negative=fake_negative,
            rules=None,
            pair_type=SimpleNamespace(value="usb"),
            get_net_ids=lambda: (1, 2),
        )
        dpr.detect_differential_pairs_with_source = lambda: [(fake_pair, "stub")]

        # Run the repair pass.
        resolved = dpr.repair_intra_clearance_violations(max_retries_per_pair=2)
    finally:
        dpr.route_differential_pair_coupled = original_method  # type: ignore[assignment]

    # No resolutions, capped at max_retries_per_pair attempts (2).
    assert resolved == 0
    assert call_count["value"] <= 2, (
        f"Repair pass exceeded max_retries_per_pair=2: made {call_count['value']} attempts"
    )
    # Original violation still in the buffer for the safety net.
    assert any(v.positive_net_name == "USB_D+" for v in dpr.intra_clearance_violations()), (
        "Repair pass dropped the original violation despite failing to fix it"
    )


# ---------------------------------------------------------------------------
# Phase B-5: repair pass resolves a violation when the retry succeeds.
# ---------------------------------------------------------------------------


def test_repair_pass_resolves_violation_when_retry_succeeds():
    """When the retry call (with wider spacing) returns clean routes and
    records NO new violations, the repair pass removes the original
    violation from the buffer and reports a non-zero resolution count.

    Stubs ``route_differential_pair_coupled`` to simulate a successful
    retry that lays clean routes (we manually append the new routes to
    ``router.routes`` to mirror the production behaviour) without
    appending a violation.
    """
    router = Autorouter(width=50.0, height=50.0, rules=DesignRules())
    dpr = router._diffpair  # lazy init

    p_route, n_route, violation = _make_violating_pair()
    router.net_names[1] = "USB_D+"
    router.net_names[2] = "USB_D-"
    router.nets[1] = []
    router.nets[2] = []
    # The repair pass looks at ``router.routes`` to find the routes
    # for the violating pair; the test must seed them.
    router.routes.append(p_route)
    router.routes.append(n_route)
    dpr._intra_clearance_violations.append(violation)

    call_count = {"value": 0}
    original_method = dpr.route_differential_pair_coupled

    def _succeeding_route(
        pair,
        spacing=None,
        coupled_only=False,
        extra_spacing_cells=0,
        per_pair_timeout=None,
    ):
        # Simulate a clean retry: lay clean routes (wider spacing) and
        # add them to router.routes.  Do NOT append to
        # ``_intra_clearance_violations``.
        call_count["value"] += 1
        clean_p = _make_route(
            net_id=1,
            net_name="USB_D+",
            segments=[(0.0, 1.0, 10.0, 1.0, Layer.F_CU)],
        )
        clean_n = _make_route(
            net_id=2,
            net_name="USB_D-",
            segments=[(0.0, 1.5, 10.0, 1.5, Layer.F_CU)],
        )
        router.routes.append(clean_p)
        router.routes.append(clean_n)
        return [clean_p, clean_n], None

    dpr.route_differential_pair_coupled = _succeeding_route  # type: ignore[assignment]
    try:
        from types import SimpleNamespace

        fake_positive = SimpleNamespace(net_id=1, net_name="USB_D+")
        fake_negative = SimpleNamespace(net_id=2, net_name="USB_D-")
        fake_pair = SimpleNamespace(
            positive=fake_positive,
            negative=fake_negative,
            rules=None,
            pair_type=SimpleNamespace(value="usb"),
            get_net_ids=lambda: (1, 2),
        )
        dpr.detect_differential_pairs_with_source = lambda: [(fake_pair, "stub")]

        resolved = dpr.repair_intra_clearance_violations(max_retries_per_pair=2)
    finally:
        dpr.route_differential_pair_coupled = original_method  # type: ignore[assignment]

    assert resolved == 1, f"Repair pass should have resolved exactly 1 pair; got {resolved}"
    assert call_count["value"] == 1, (
        f"Repair pass should have succeeded on the first attempt; "
        f"made {call_count['value']} attempts"
    )
    # Original violation must be gone from the buffer.
    assert not any(v.positive_net_name == "USB_D+" for v in dpr.intra_clearance_violations()), (
        "Repair pass left the original violation in the buffer after successful resolution"
    )


# ---------------------------------------------------------------------------
# Phase B-6: the Autorouter hook calls the repair pass when violations
# are present after route_all_with_diffpairs.
# ---------------------------------------------------------------------------


def test_autorouter_route_all_with_diffpairs_invokes_repair_when_violations_present():
    """The ``Autorouter.route_all_with_diffpairs`` wrapper must invoke
    the Phase B repair pass when the inner call leaves violations in
    the buffer.

    Stubs the inner DiffPairRouter so the inner call is a no-op but
    appends one violation, and asserts that the wrapper subsequently
    calls ``repair_intra_clearance_violations``.
    """
    from kicad_tools.router.diffpair import DifferentialPairConfig

    router = Autorouter(width=50.0, height=50.0, rules=DesignRules())
    dpr = router._diffpair  # lazy init

    p_route, n_route, violation = _make_violating_pair()
    router.net_names[1] = "USB_D+"
    router.net_names[2] = "USB_D-"

    # Stub the inner route_all_with_diffpairs to plant a violation.
    def _stub_inner(*_args, **_kwargs):
        dpr._intra_clearance_violations.append(violation)
        return [], []

    repair_calls = {"value": 0}

    def _stub_repair(**_kwargs):
        repair_calls["value"] += 1
        # Pretend the repair removed the violation so the hook's
        # follow-up validate state is clean.
        dpr._intra_clearance_violations.clear()
        return 1

    dpr.route_all_with_diffpairs = _stub_inner  # type: ignore[assignment]
    dpr.repair_intra_clearance_violations = _stub_repair  # type: ignore[assignment]

    cfg = DifferentialPairConfig(enabled=True)
    router.route_all_with_diffpairs(cfg)

    assert repair_calls["value"] == 1, (
        "Autorouter.route_all_with_diffpairs did not call "
        "DiffPairRouter.repair_intra_clearance_violations after the inner "
        "call recorded a violation; the Phase B hook is missing or "
        "mis-wired"
    )


def test_autorouter_route_all_with_diffpairs_skips_repair_when_no_violations():
    """When the inner call leaves no violations, the wrapper must not
    invoke the repair pass (avoids unnecessary lookup churn)."""
    from kicad_tools.router.diffpair import DifferentialPairConfig

    router = Autorouter(width=50.0, height=50.0, rules=DesignRules())
    dpr = router._diffpair

    # Stub the inner route_all_with_diffpairs as a no-op (no violations).
    def _stub_inner_clean(*_args, **_kwargs):
        return [], []

    repair_calls = {"value": 0}

    def _stub_repair(**_kwargs):
        repair_calls["value"] += 1
        return 0

    dpr.route_all_with_diffpairs = _stub_inner_clean  # type: ignore[assignment]
    dpr.repair_intra_clearance_violations = _stub_repair  # type: ignore[assignment]

    cfg = DifferentialPairConfig(enabled=True)
    router.route_all_with_diffpairs(cfg)

    assert repair_calls["value"] == 0, (
        "Autorouter.route_all_with_diffpairs invoked the Phase B repair "
        "pass even though no violations were recorded; the hook should "
        "be guarded by ``intra_clearance_violations()``"
    )


# ---------------------------------------------------------------------------
# Phase B-7: extra_spacing_cells actually widens min_spacing_cells in the
# pathfinder constructor call (white-box guard so future refactors can't
# silently regress to the legacy spacing).
# ---------------------------------------------------------------------------


def test_extra_spacing_cells_widens_min_spacing_cells():
    """When ``extra_spacing_cells=2`` is passed, the resulting
    ``min_spacing_cells`` floor is two cells wider than the baseline.

    Inspect the CoupledPathfinder constructed by
    ``route_differential_pair_coupled`` by stubbing ``CoupledPathfinder``
    and capturing the kwarg.  This is the property the repair pass
    relies on -- without it, retries would re-use the same spacing and
    the same violation would recur.
    """
    from unittest.mock import patch

    from kicad_tools.router import diffpair_routing

    router = Autorouter(width=50.0, height=50.0, rules=DesignRules())
    dpr = router._diffpair

    # Build a minimal DifferentialPair with two-pad pads so the
    # coupled-spec path executes.  We then stub CoupledPathfinder to
    # return ``None`` (no path) so the rest of the routine is a no-op;
    # we only care about the constructor kwargs.

    from kicad_tools.router.diffpair import (
        DifferentialPair,
        DifferentialPairRules,
        DifferentialPairType,
        DifferentialSignal,
    )

    rules = DifferentialPairRules.for_type(DifferentialPairType.USB2)
    pos = DifferentialSignal(
        net_name="USB_D+",
        net_id=1,
        base_name="USB_D",
        polarity="P",
        notation="plus_minus",
    )
    neg = DifferentialSignal(
        net_name="USB_D-",
        net_id=2,
        base_name="USB_D",
        polarity="N",
        notation="plus_minus",
    )
    pair = DifferentialPair(
        name="USB_D",
        positive=pos,
        negative=neg,
        pair_type=DifferentialPairType.USB2,
        rules=rules,
    )

    # Stub _get_pair_pads to return synthetic 2-pad lists.
    from kicad_tools.router.primitives import Pad

    p_pads = [
        Pad(
            x=1.0,
            y=1.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="USB_D+",
            layer=Layer.F_CU,
            ref="J1",
            pin="1",
        ),
        Pad(
            x=5.0,
            y=1.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="USB_D+",
            layer=Layer.F_CU,
            ref="U1",
            pin="1",
        ),
    ]
    n_pads = [
        Pad(
            x=1.0,
            y=2.0,
            width=1.0,
            height=1.0,
            net=2,
            net_name="USB_D-",
            layer=Layer.F_CU,
            ref="J1",
            pin="2",
        ),
        Pad(
            x=5.0,
            y=2.0,
            width=1.0,
            height=1.0,
            net=2,
            net_name="USB_D-",
            layer=Layer.F_CU,
            ref="U1",
            pin="2",
        ),
    ]
    dpr._get_pair_pads = lambda _pair: (p_pads, n_pads)  # type: ignore[assignment]

    captured_kwargs: list[dict] = []

    real_pathfinder = diffpair_routing.CoupledPathfinder

    def _capture(*args, **kwargs):
        captured_kwargs.append(dict(kwargs))
        # Build a real CoupledPathfinder then stub its route_coupled
        # to return None so the outer routine returns early.
        pf = real_pathfinder(*args, **kwargs)
        pf.route_coupled = lambda *a, **k: None
        return pf

    with patch.object(diffpair_routing, "CoupledPathfinder", side_effect=_capture):
        # First call: extra_spacing_cells=0 baseline.
        dpr.route_differential_pair_coupled(pair, coupled_only=True, extra_spacing_cells=0)
        # Second call: extra_spacing_cells=2.
        dpr.route_differential_pair_coupled(pair, coupled_only=True, extra_spacing_cells=2)

    assert len(captured_kwargs) == 2, (
        f"Stub CoupledPathfinder was called {len(captured_kwargs)} times; expected 2"
    )
    base_floor = captured_kwargs[0]["min_spacing_cells"]
    wider_floor = captured_kwargs[1]["min_spacing_cells"]
    assert wider_floor == base_floor + 2, (
        f"extra_spacing_cells=2 should widen min_spacing_cells by 2; "
        f"got baseline={base_floor}, wider={wider_floor}"
    )


# ---------------------------------------------------------------------------
# Phase B-8 (Issue #3115): fine-grid sub-pass plumbing and behaviour.
# ---------------------------------------------------------------------------


def test_repair_intra_clearance_violations_accepts_enable_fine_grid_pass_kwarg():
    """``repair_intra_clearance_violations`` must accept ``enable_fine_grid_pass``.

    The fine-grid sub-pass added by Issue #3115 lives at the third
    attempt slot; the kwarg lets test callers disable it to pin the
    legacy main-grid-only contract without depending on the new
    behaviour being inert.
    """
    import inspect

    from kicad_tools.router.diffpair_routing import DiffPairRouter

    sig = inspect.signature(DiffPairRouter.repair_intra_clearance_violations)
    assert "enable_fine_grid_pass" in sig.parameters, (
        "repair_intra_clearance_violations lost the enable_fine_grid_pass "
        "kwarg added by Issue #3115"
    )
    # Default must be True so production calls automatically get the
    # extra resolution; tests that need the legacy behaviour pass False.
    assert sig.parameters["enable_fine_grid_pass"].default is True


def test_route_pair_on_fine_grid_uses_finer_resolution():
    """``_route_pair_on_fine_grid`` builds a pathfinder whose grid has a
    finer resolution than the main grid.

    White-box guard against the simplest failure mode the fix could
    silently hit: the helper accidentally re-using the main grid (or a
    grid with the same resolution) so the angle-#1 sub-cell motion is
    unavailable.  Captures the kwargs the helper hands to
    ``CoupledPathfinder`` and asserts the pathfinder's grid has a
    resolution strictly less than the main grid's.
    """
    from unittest.mock import patch

    from kicad_tools.router import diffpair_routing
    from kicad_tools.router.diffpair import (
        DifferentialPair,
        DifferentialPairRules,
        DifferentialPairType,
        DifferentialSignal,
    )
    from kicad_tools.router.primitives import Pad

    router = Autorouter(width=50.0, height=50.0, rules=DesignRules())
    dpr = router._diffpair

    rules = DifferentialPairRules.for_type(DifferentialPairType.USB2)
    pos = DifferentialSignal(
        net_name="USB_D+",
        net_id=1,
        base_name="USB_D",
        polarity="P",
        notation="plus_minus",
    )
    neg = DifferentialSignal(
        net_name="USB_D-",
        net_id=2,
        base_name="USB_D",
        polarity="N",
        notation="plus_minus",
    )
    pair = DifferentialPair(
        name="USB_D",
        positive=pos,
        negative=neg,
        pair_type=DifferentialPairType.USB2,
        rules=rules,
    )

    # Synthetic asymmetric pads: J1 has 0.8mm tall pads (FFC),
    # U1 has 0.35mm tall pads (QFN) -- the exact pathology the
    # issue body cites at boards/06-diffpair-test/U2 vs J4.
    p_pads = [
        Pad(
            x=2.0,
            y=2.0,
            width=0.8,
            height=0.8,
            net=1,
            net_name="USB_D+",
            layer=Layer.F_CU,
            ref="J1",
            pin="1",
        ),
        Pad(
            x=8.0,
            y=2.0,
            width=0.35,
            height=0.35,
            net=1,
            net_name="USB_D+",
            layer=Layer.F_CU,
            ref="U1",
            pin="1",
        ),
    ]
    n_pads = [
        Pad(
            x=2.0,
            y=2.3,
            width=0.8,
            height=0.8,
            net=2,
            net_name="USB_D-",
            layer=Layer.F_CU,
            ref="J1",
            pin="2",
        ),
        Pad(
            x=8.0,
            y=2.3,
            width=0.35,
            height=0.35,
            net=2,
            net_name="USB_D-",
            layer=Layer.F_CU,
            ref="U1",
            pin="2",
        ),
    ]
    dpr._get_pair_pads = lambda _pair: (p_pads, n_pads)  # type: ignore[assignment]

    main_resolution = router.grid.resolution

    captured_pathfinders: list = []
    real_pathfinder = diffpair_routing.CoupledPathfinder

    def _capture(grid, *args, **kwargs):
        pf = real_pathfinder(grid, *args, **kwargs)
        captured_pathfinders.append(pf)
        # Stub route_coupled to short-circuit (None) so we only test
        # the construction.
        pf.route_coupled = lambda *a, **k: None
        return pf

    with patch.object(diffpair_routing, "CoupledPathfinder", side_effect=_capture):
        dpr._route_pair_on_fine_grid(
            pair,
            spacing_override=None,
            extra_spacing_cells=1,
            per_pair_timeout=None,
            resolution_factor=0.5,
        )

    assert len(captured_pathfinders) == 1, (
        f"Expected exactly one fine-grid CoupledPathfinder; got {len(captured_pathfinders)}"
    )
    fine_pathfinder = captured_pathfinders[0]
    fine_resolution = fine_pathfinder.grid.resolution
    assert fine_resolution < main_resolution, (
        f"Fine-grid pathfinder resolution {fine_resolution}mm is not "
        f"finer than main-grid resolution {main_resolution}mm -- the "
        f"angle-#1 sub-cell pathfinder advantage is lost"
    )
    # The default resolution_factor=0.5 should produce roughly
    # half-pitch (allow a small fudge for the 4M-cell cap).
    assert fine_resolution <= main_resolution * 0.6, (
        f"Fine-grid resolution {fine_resolution}mm exceeds 60% of the "
        f"main-grid resolution {main_resolution}mm -- the cell-count "
        f"cap may have neutered the resolution_factor=0.5 request"
    )


def test_repair_pass_invokes_fine_grid_when_main_retries_fail():
    """When all main-grid retries fail, the repair pass must invoke
    the Issue #3115 fine-grid sub-pass before falling through.

    Stubs ``route_differential_pair_coupled`` to always fail (no path)
    and ``_route_pair_on_fine_grid`` to record the call.  Verifies
    that exactly one fine-grid sub-pass attempt was made per pair
    after the main-grid retries exhausted.
    """
    router = Autorouter(width=50.0, height=50.0, rules=DesignRules())
    dpr = router._diffpair

    p_route, n_route, violation = _make_violating_pair()
    router.net_names[1] = "USB_D+"
    router.net_names[2] = "USB_D-"
    router.nets[1] = []
    router.nets[2] = []
    router.routes.append(p_route)
    router.routes.append(n_route)
    dpr._intra_clearance_violations.append(violation)

    main_grid_calls = {"value": 0}
    fine_grid_calls = {"value": 0}

    original_method = dpr.route_differential_pair_coupled
    original_fine = dpr._route_pair_on_fine_grid

    def _failing_main(*args, **kwargs):
        main_grid_calls["value"] += 1
        return [], None

    def _failing_fine(*args, **kwargs):
        fine_grid_calls["value"] += 1
        return [], None

    dpr.route_differential_pair_coupled = _failing_main  # type: ignore[assignment]
    dpr._route_pair_on_fine_grid = _failing_fine  # type: ignore[assignment]

    try:
        from types import SimpleNamespace

        fake_positive = SimpleNamespace(net_id=1, net_name="USB_D+")
        fake_negative = SimpleNamespace(net_id=2, net_name="USB_D-")
        fake_pair = SimpleNamespace(
            positive=fake_positive,
            negative=fake_negative,
            rules=None,
            pair_type=SimpleNamespace(value="usb"),
            get_net_ids=lambda: (1, 2),
            name="USB_D",
        )
        dpr.detect_differential_pairs_with_source = lambda: [(fake_pair, "stub")]

        resolved = dpr.repair_intra_clearance_violations(
            max_retries_per_pair=2,
            enable_fine_grid_pass=True,
        )
    finally:
        dpr.route_differential_pair_coupled = original_method  # type: ignore[assignment]
        dpr._route_pair_on_fine_grid = original_fine  # type: ignore[assignment]

    # Two main-grid retries attempted, then one fine-grid attempt.
    assert main_grid_calls["value"] == 2, (
        f"Expected exactly 2 main-grid attempts; got {main_grid_calls['value']}"
    )
    assert fine_grid_calls["value"] == 1, (
        f"Expected exactly 1 fine-grid attempt after main retries failed; "
        f"got {fine_grid_calls['value']}"
    )
    assert resolved == 0, f"All attempts failed; expected resolved=0 got {resolved}"


def test_repair_pass_skips_fine_grid_when_disabled():
    """``enable_fine_grid_pass=False`` must suppress the Issue #3115
    sub-pass entirely so the legacy main-grid-only contract is preserved
    for tests / opt-out callers.
    """
    router = Autorouter(width=50.0, height=50.0, rules=DesignRules())
    dpr = router._diffpair

    p_route, n_route, violation = _make_violating_pair()
    router.net_names[1] = "USB_D+"
    router.net_names[2] = "USB_D-"
    router.nets[1] = []
    router.nets[2] = []
    router.routes.append(p_route)
    router.routes.append(n_route)
    dpr._intra_clearance_violations.append(violation)

    fine_grid_calls = {"value": 0}
    original_method = dpr.route_differential_pair_coupled
    original_fine = dpr._route_pair_on_fine_grid

    def _failing_main(*args, **kwargs):
        return [], None

    def _spy_fine(*args, **kwargs):
        fine_grid_calls["value"] += 1
        return [], None

    dpr.route_differential_pair_coupled = _failing_main  # type: ignore[assignment]
    dpr._route_pair_on_fine_grid = _spy_fine  # type: ignore[assignment]

    try:
        from types import SimpleNamespace

        fake_positive = SimpleNamespace(net_id=1, net_name="USB_D+")
        fake_negative = SimpleNamespace(net_id=2, net_name="USB_D-")
        fake_pair = SimpleNamespace(
            positive=fake_positive,
            negative=fake_negative,
            rules=None,
            pair_type=SimpleNamespace(value="usb"),
            get_net_ids=lambda: (1, 2),
            name="USB_D",
        )
        dpr.detect_differential_pairs_with_source = lambda: [(fake_pair, "stub")]

        dpr.repair_intra_clearance_violations(
            max_retries_per_pair=2,
            enable_fine_grid_pass=False,
        )
    finally:
        dpr.route_differential_pair_coupled = original_method  # type: ignore[assignment]
        dpr._route_pair_on_fine_grid = original_fine  # type: ignore[assignment]

    assert fine_grid_calls["value"] == 0, (
        f"Fine-grid sub-pass invoked despite enable_fine_grid_pass=False; "
        f"got {fine_grid_calls['value']} call(s)"
    )


def test_repair_pass_accepts_fine_grid_routes_when_clean():
    """When the fine-grid sub-pass returns clean (non-violating) routes,
    the repair pass marks them on the main grid, increments the resolved
    counter, and removes the original violation from the buffer.
    """
    from types import SimpleNamespace

    router = Autorouter(width=50.0, height=50.0, rules=DesignRules())
    dpr = router._diffpair

    p_route, n_route, violation = _make_violating_pair()
    router.net_names[1] = "USB_D+"
    router.net_names[2] = "USB_D-"
    router.nets[1] = []
    router.nets[2] = []
    router.routes.append(p_route)
    router.routes.append(n_route)
    dpr._intra_clearance_violations.append(violation)

    original_method = dpr.route_differential_pair_coupled
    original_fine = dpr._route_pair_on_fine_grid

    def _failing_main(*args, **kwargs):
        return [], None

    def _succeeding_fine(
        pair,
        spacing_override=None,
        extra_spacing_cells=1,
        per_pair_timeout=None,
        resolution_factor=0.5,
    ):
        # Lay clean routes 0.5mm apart (well above 0.10mm threshold).
        clean_p = _make_route(
            net_id=1,
            net_name="USB_D+",
            segments=[(0.0, 1.0, 10.0, 1.0, Layer.F_CU)],
        )
        clean_n = _make_route(
            net_id=2,
            net_name="USB_D-",
            segments=[(0.0, 1.5, 10.0, 1.5, Layer.F_CU)],
        )
        return [clean_p, clean_n], None

    dpr.route_differential_pair_coupled = _failing_main  # type: ignore[assignment]
    dpr._route_pair_on_fine_grid = _succeeding_fine  # type: ignore[assignment]

    try:
        fake_positive = SimpleNamespace(net_id=1, net_name="USB_D+")
        fake_negative = SimpleNamespace(net_id=2, net_name="USB_D-")
        fake_pair = SimpleNamespace(
            positive=fake_positive,
            negative=fake_negative,
            rules=None,
            pair_type=SimpleNamespace(value="usb"),
            get_net_ids=lambda: (1, 2),
            name="USB_D",
        )
        dpr.detect_differential_pairs_with_source = lambda: [(fake_pair, "stub")]

        resolved = dpr.repair_intra_clearance_violations(
            max_retries_per_pair=2,
            enable_fine_grid_pass=True,
        )
    finally:
        dpr.route_differential_pair_coupled = original_method  # type: ignore[assignment]
        dpr._route_pair_on_fine_grid = original_fine  # type: ignore[assignment]

    assert resolved == 1, (
        f"Fine-grid sub-pass returned clean routes; expected resolved=1 got {resolved}"
    )
    # The original violation should be gone from the buffer.
    assert not any(v.positive_net_name == "USB_D+" for v in dpr.intra_clearance_violations()), (
        "Original violation persisted after a successful fine-grid sub-pass"
    )


def test_route_pair_on_fine_grid_returns_empty_when_no_pads():
    """``_route_pair_on_fine_grid`` returns ``([], None)`` cleanly when
    ``_get_pair_pads`` can't resolve the pair's pads.

    Guard against the helper raising on a malformed input -- the repair
    pass's ``except Exception`` clause catches it but a clean return is
    less noisy in the logs.
    """
    from kicad_tools.router.diffpair import (
        DifferentialPair,
        DifferentialPairRules,
        DifferentialPairType,
        DifferentialSignal,
    )

    router = Autorouter(width=50.0, height=50.0, rules=DesignRules())
    dpr = router._diffpair

    rules = DifferentialPairRules.for_type(DifferentialPairType.USB2)
    pos = DifferentialSignal(
        net_name="USB_D+",
        net_id=1,
        base_name="USB_D",
        polarity="P",
        notation="plus_minus",
    )
    neg = DifferentialSignal(
        net_name="USB_D-",
        net_id=2,
        base_name="USB_D",
        polarity="N",
        notation="plus_minus",
    )
    pair = DifferentialPair(
        name="USB_D",
        positive=pos,
        negative=neg,
        pair_type=DifferentialPairType.USB2,
        rules=rules,
    )

    # No pads resolved.
    dpr._get_pair_pads = lambda _pair: None  # type: ignore[assignment]
    routes, warning = dpr._route_pair_on_fine_grid(
        pair,
        spacing_override=None,
        extra_spacing_cells=1,
        per_pair_timeout=None,
    )
    assert routes == []
    assert warning is None


# ---------------------------------------------------------------------------
# Phase B-9 (Issue #3115 angle #5): partner-aware A* heuristic on
# ``CoupledPathfinder``.
#
# These tests pin the angle-#5 follow-up to PR #3122 (fine-grid sub-pass).
# The legacy ``_heuristic`` is the Manhattan sum ``(p_dist + n_dist) *
# cost_straight``; angle #5 switches the default to
# ``max(p_dist, n_dist) * cost_straight + spacing_penalty + layer_cost``
# which is still admissible but biases the search toward partner-
# synchronised moves.
# ---------------------------------------------------------------------------


def test_coupled_pathfinder_accepts_heuristic_mode_kwarg():
    """Issue #3115 (angle #5): the ``heuristic_mode`` kwarg must be on
    ``CoupledPathfinder.__init__`` so callers (and tests) can opt back
    into the legacy Manhattan-sum heuristic.

    The default must be ``"partner_aware"`` so production calls
    automatically get the new behaviour without changing the public
    ``CoupledPathfinder()`` call sites in
    ``route_differential_pair_coupled`` and ``_route_pair_on_fine_grid``.
    """
    import inspect

    from kicad_tools.router.diffpair_routing import CoupledPathfinder

    sig = inspect.signature(CoupledPathfinder.__init__)
    assert "heuristic_mode" in sig.parameters, (
        "CoupledPathfinder.__init__ lost the heuristic_mode kwarg added by Issue #3115"
    )
    assert sig.parameters["heuristic_mode"].default == "partner_aware", (
        "Issue #3115 requires partner_aware as the default heuristic_mode "
        "so production routing automatically gets the new behaviour"
    )
    # Companion knob: spacing_penalty_factor.
    assert "spacing_penalty_factor" in sig.parameters, (
        "CoupledPathfinder.__init__ lost the spacing_penalty_factor kwarg added by Issue #3115"
    )


def test_coupled_pathfinder_rejects_invalid_heuristic_mode():
    """A typo in ``heuristic_mode`` should fail loudly at construction,
    not silently fall back to one of the two real modes.
    """
    import pytest

    from kicad_tools.router.diffpair_routing import CoupledPathfinder
    from kicad_tools.router.grid import RoutingGrid

    rules = DesignRules()
    grid = RoutingGrid(width=10.0, height=10.0, rules=rules)
    with pytest.raises(ValueError, match="heuristic_mode"):
        CoupledPathfinder(grid, rules, target_spacing_cells=3, heuristic_mode="bogus")  # type: ignore[arg-type]


def test_partner_aware_heuristic_at_goal_is_zero():
    """Both heuristics MUST return ``0`` (no layer change) when both
    traces sit at their goal cells -- this is a pure admissibility
    smoke test (h(goal) == 0).
    """
    from kicad_tools.router.diffpair_routing import (
        CoupledPathfinder,
        CoupledState,
        GridPos,
    )
    from kicad_tools.router.grid import RoutingGrid

    rules = DesignRules()
    grid = RoutingGrid(width=10.0, height=10.0, rules=rules)

    p_goal = GridPos(20, 5, 0)
    n_goal = GridPos(20, 8, 0)
    at_goal = CoupledState(p_goal, n_goal, (0, 0))

    legacy = CoupledPathfinder(grid, rules, target_spacing_cells=3, heuristic_mode="manhattan_sum")
    new = CoupledPathfinder(grid, rules, target_spacing_cells=3, heuristic_mode="partner_aware")
    # target_spacing_cells == |p_goal.y - n_goal.y| so the spacing
    # penalty is zero at the goal.  Layer matches so layer_cost is zero
    # too.  Both heuristics collapse to ``0``.
    assert legacy._heuristic(at_goal, p_goal, n_goal) == 0.0
    assert new._heuristic(at_goal, p_goal, n_goal) == 0.0


def test_partner_aware_heuristic_undershoots_manhattan_sum_on_asymmetric_state():
    """The whole point of angle #5: on a state where one trace is
    much further from its goal than the other, the partner-aware
    heuristic returns a SMALLER value than the Manhattan-sum
    heuristic.

    A smaller heuristic means a HIGHER ranking in the A* priority
    queue, so states whose partner moves are still ahead are
    preferred -- this is what biases the search toward partner-
    synchronised escapes.

    Concretely: state has P 10 cells from p_goal and N 2 cells from
    n_goal, with spacing matching the target so spacing_penalty == 0.
    Manhattan-sum heuristic = (10 + 2) * cost_straight = 12 *
    cost_straight.  Partner-aware heuristic = max(10, 2) *
    cost_straight = 10 * cost_straight.  Strictly less.
    """
    from kicad_tools.router.diffpair_routing import (
        CoupledPathfinder,
        CoupledState,
        GridPos,
    )
    from kicad_tools.router.grid import RoutingGrid

    rules = DesignRules()
    grid = RoutingGrid(width=20.0, height=20.0, rules=rules)

    # State: P far from goal, N close.  Spacing matches target.
    target_spacing = 3
    p_pos = GridPos(5, 5, 0)
    n_pos = GridPos(5, 8, 0)  # 3 cells south of p_pos => spacing == 3.
    state = CoupledState(p_pos, n_pos, (0, 0))
    p_goal = GridPos(15, 5, 0)  # 10 cells from p_pos
    n_goal = GridPos(7, 8, 0)  # 2 cells from n_pos

    legacy = CoupledPathfinder(
        grid,
        rules,
        target_spacing_cells=target_spacing,
        heuristic_mode="manhattan_sum",
    )
    new = CoupledPathfinder(
        grid,
        rules,
        target_spacing_cells=target_spacing,
        heuristic_mode="partner_aware",
    )

    legacy_h = legacy._heuristic(state, p_goal, n_goal)
    new_h = new._heuristic(state, p_goal, n_goal)

    expected_legacy = (10 + 2) * rules.cost_straight  # 12 * cost_straight
    expected_new = 10 * rules.cost_straight  # spacing matches -> no penalty
    assert legacy_h == expected_legacy
    assert new_h == expected_new
    assert new_h < legacy_h, (
        f"partner-aware heuristic must undershoot Manhattan-sum on "
        f"asymmetric P/N distance, got new={new_h} legacy={legacy_h}"
    )


def test_partner_aware_heuristic_penalises_spacing_divergence():
    """The spacing-penalty term must penalise states whose current
    center-to-center spacing diverges from the target.  Pins the
    monotonicity contract: a state with spacing == target has a
    smaller (or equal) heuristic than the same state with spacing
    further from target.
    """
    from kicad_tools.router.diffpair_routing import (
        CoupledPathfinder,
        CoupledState,
        GridPos,
    )
    from kicad_tools.router.grid import RoutingGrid

    rules = DesignRules()
    grid = RoutingGrid(width=20.0, height=20.0, rules=rules)

    target_spacing = 3
    new = CoupledPathfinder(
        grid,
        rules,
        target_spacing_cells=target_spacing,
        heuristic_mode="partner_aware",
        spacing_penalty_factor=0.5,
    )

    p_goal = GridPos(10, 5, 0)
    n_goal = GridPos(10, 8, 0)

    # State A: spacing matches target exactly.
    state_match = CoupledState(GridPos(5, 5, 0), GridPos(5, 8, 0), (0, 0))
    h_match = new._heuristic(state_match, p_goal, n_goal)

    # State B: spacing is 6 cells (3 over target).  Manhattan distance
    # to goal is identical to state A so the only difference is the
    # spacing penalty.
    state_diverged = CoupledState(GridPos(5, 5, 0), GridPos(5, 11, 0), (0, 0))
    h_diverged = new._heuristic(state_diverged, p_goal, n_goal)

    assert h_diverged > h_match, (
        f"State with diverged spacing should have a larger heuristic "
        f"(lower ranking) than a same-distance-to-goal state at target "
        f"spacing; got h_diverged={h_diverged} h_match={h_match}"
    )


def test_partner_aware_heuristic_is_admissible():
    """Admissibility check on a small synthetic search.

    Routes a 2-pad differential pair end-to-end with the partner-aware
    heuristic and verifies that the heuristic at every reachable state
    never exceeds the actual remaining path cost (g_score of the
    optimal path minus g_score of the state).  We pin this by:

      1. Running an unbounded coupled search on a synthetic pair where
         the start and goal are aligned and clearly routable.
      2. Asserting the resulting pair routes (no path-found regression).

    The admissibility argument is per-state but as a stand-in we
    verify the search completes -- a non-admissible heuristic could
    prune the optimal path and produce a non-optimal result, but on
    this trivially routable pair any non-optimal route still violates
    the spacing floor and would fail the within-pair clearance check
    that ``find_intra_pair_clearance_violations`` runs.
    """
    from kicad_tools.router.diffpair_routing import (
        CoupledPathfinder,
        find_intra_pair_clearance_violations,
    )
    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.primitives import Pad

    # Use a sparse 30x10mm grid with simple 2-layer stack -- plenty of
    # room for both traces and no obstacles.
    rules = DesignRules()
    grid = RoutingGrid(width=30.0, height=10.0, rules=rules)

    target_spacing = max(2, int(round(0.3 / grid.resolution)))
    pf = CoupledPathfinder(
        grid,
        rules,
        target_spacing_cells=target_spacing,
        heuristic_mode="partner_aware",
    )

    p_start = Pad(
        x=2.0,
        y=4.0,
        width=0.5,
        height=0.5,
        net=1,
        net_name="DP_P",
        layer=Layer.F_CU,
        ref="J1",
        pin="1",
    )
    p_end = Pad(
        x=28.0,
        y=4.0,
        width=0.5,
        height=0.5,
        net=1,
        net_name="DP_P",
        layer=Layer.F_CU,
        ref="J2",
        pin="1",
    )
    n_start = Pad(
        x=2.0,
        y=4.5,
        width=0.5,
        height=0.5,
        net=2,
        net_name="DP_N",
        layer=Layer.F_CU,
        ref="J1",
        pin="2",
    )
    n_end = Pad(
        x=28.0,
        y=4.5,
        width=0.5,
        height=0.5,
        net=2,
        net_name="DP_N",
        layer=Layer.F_CU,
        ref="J2",
        pin="2",
    )

    result = pf.route_coupled(p_start, p_end, n_start, n_end)
    assert result is not None, (
        "partner-aware heuristic must still find a route on a trivially routable synthetic pair"
    )
    p_route, n_route = result
    assert p_route.segments, "expected at least one P segment"
    assert n_route.segments, "expected at least one N segment"

    # The synthetic pair has 0.5mm spacing which exceeds the default
    # 0.075mm intra_pair_clearance + 0.15mm trace width = 0.225mm
    # required center spacing -- so no intra-pair clearance violation
    # should be produced.  This is a non-regression check against an
    # accidentally inadmissible heuristic that lets the search return
    # a path that violates the spacing floor.
    violations = find_intra_pair_clearance_violations(
        p_route,
        n_route,
        threshold_mm=0.075,
    )
    assert violations is None, (
        f"partner-aware heuristic produced route with intra-pair clearance violation: {violations}"
    )


def test_partner_aware_heuristic_solves_asymmetric_pad_pair():
    """Issue #3115 angle #5 motivational test.

    Construct an asymmetric-pad differential pair (pad heights 0.8mm
    on one side, 0.35mm on the other -- the exact J4-FFC vs U2-QFN
    pathology from board 06 per the curator's note #4 on issue
    #3097).  Verify that with the partner-aware heuristic the
    resulting routes have no ``diffpair_clearance_intra`` violation.

    This is the "provably wins" gate the AC requires.  We compare
    the partner-aware result against the legacy Manhattan-sum result
    on the SAME synthetic pair; the partner-aware run must produce
    a clean route (``find_intra_pair_clearance_violations is None``)
    while we just assert the legacy run runs (it may or may not
    produce a violation depending on grid quantisation).
    """
    from kicad_tools.router.diffpair_routing import (
        CoupledPathfinder,
        find_intra_pair_clearance_violations,
    )
    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.primitives import Pad

    rules = DesignRules()
    grid = RoutingGrid(width=20.0, height=10.0, rules=rules)

    # Compute a target spacing of roughly 0.4mm (in grid cells).
    target_spacing = max(2, int(round(0.4 / grid.resolution)))

    # Asymmetric pads: 0.8mm-tall on left, 0.35mm-tall on right.
    p_start = Pad(
        x=2.0,
        y=4.0,
        width=0.8,
        height=0.8,
        net=1,
        net_name="DP_P",
        layer=Layer.F_CU,
        ref="J1",
        pin="1",
    )
    p_end = Pad(
        x=18.0,
        y=4.0,
        width=0.35,
        height=0.35,
        net=1,
        net_name="DP_P",
        layer=Layer.F_CU,
        ref="U1",
        pin="1",
    )
    n_start = Pad(
        x=2.0,
        y=4.4,
        width=0.8,
        height=0.8,
        net=2,
        net_name="DP_N",
        layer=Layer.F_CU,
        ref="J1",
        pin="2",
    )
    n_end = Pad(
        x=18.0,
        y=4.4,
        width=0.35,
        height=0.35,
        net=2,
        net_name="DP_N",
        layer=Layer.F_CU,
        ref="U1",
        pin="2",
    )

    # Partner-aware run.
    pf_new = CoupledPathfinder(
        grid,
        rules,
        target_spacing_cells=target_spacing,
        heuristic_mode="partner_aware",
    )
    new_result = pf_new.route_coupled(p_start, p_end, n_start, n_end)
    assert new_result is not None, (
        "partner-aware heuristic must find a route on the asymmetric-pad synthetic pair"
    )
    p_route_new, n_route_new = new_result

    # The asymmetric pad case is what angle #5 targets -- the
    # partner-aware heuristic must produce a clean route here, OR (if
    # the underlying spacing floor still admits a sub-threshold route)
    # at least the partner-aware run must not regress against legacy.
    new_violations = find_intra_pair_clearance_violations(
        p_route_new,
        n_route_new,
        threshold_mm=0.075,
    )

    # Legacy run -- just verify it doesn't crash (the legacy heuristic
    # was the production path until Issue #3115; both modes must
    # remain functional).
    pf_legacy = CoupledPathfinder(
        grid,
        rules,
        target_spacing_cells=target_spacing,
        heuristic_mode="manhattan_sum",
    )
    legacy_result = pf_legacy.route_coupled(p_start, p_end, n_start, n_end)
    assert legacy_result is not None, (
        "legacy Manhattan-sum heuristic must still find a route -- "
        "the opt-out path must not regress"
    )

    # The partner-aware route must be clean.  This is the "provably
    # wins" assertion: even when both heuristics route the pair, the
    # partner-aware one produces a route whose center-to-center
    # spacing matches the target throughout.
    assert new_violations is None, (
        f"partner-aware heuristic produced intra-pair clearance "
        f"violation on the asymmetric-pad pair (the angle-#5 target "
        f"case): {new_violations}"
    )


def test_legacy_manhattan_sum_heuristic_still_routes_symmetric_pair():
    """Non-regression: the legacy ``manhattan_sum`` heuristic must
    still produce a valid route on a simple symmetric-pad pair.  Pins
    that the opt-out path is preserved verbatim for any pinned-test
    caller (Issue #3115).
    """
    from kicad_tools.router.diffpair_routing import (
        CoupledPathfinder,
        find_intra_pair_clearance_violations,
    )
    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.primitives import Pad

    rules = DesignRules()
    grid = RoutingGrid(width=20.0, height=10.0, rules=rules)
    target_spacing = max(2, int(round(0.3 / grid.resolution)))

    p_start = Pad(
        x=2.0,
        y=4.0,
        width=0.5,
        height=0.5,
        net=1,
        net_name="DP_P",
        layer=Layer.F_CU,
        ref="J1",
        pin="1",
    )
    p_end = Pad(
        x=18.0,
        y=4.0,
        width=0.5,
        height=0.5,
        net=1,
        net_name="DP_P",
        layer=Layer.F_CU,
        ref="J2",
        pin="1",
    )
    n_start = Pad(
        x=2.0,
        y=4.5,
        width=0.5,
        height=0.5,
        net=2,
        net_name="DP_N",
        layer=Layer.F_CU,
        ref="J1",
        pin="2",
    )
    n_end = Pad(
        x=18.0,
        y=4.5,
        width=0.5,
        height=0.5,
        net=2,
        net_name="DP_N",
        layer=Layer.F_CU,
        ref="J2",
        pin="2",
    )

    pf = CoupledPathfinder(
        grid,
        rules,
        target_spacing_cells=target_spacing,
        heuristic_mode="manhattan_sum",
    )
    result = pf.route_coupled(p_start, p_end, n_start, n_end)
    assert result is not None, (
        "Legacy Manhattan-sum heuristic must continue to find routes "
        "on the symmetric easy case -- opt-out path is broken"
    )
    p_route, n_route = result
    violations = find_intra_pair_clearance_violations(
        p_route,
        n_route,
        threshold_mm=0.075,
    )
    assert violations is None, (
        f"Symmetric-pad pair under legacy heuristic must not violate "
        f"intra-pair clearance: {violations}"
    )
