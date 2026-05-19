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
        f"Repair pass exceeded max_retries_per_pair=2: "
        f"made {call_count['value']} attempts"
    )
    # Original violation still in the buffer for the safety net.
    assert any(
        v.positive_net_name == "USB_D+"
        for v in dpr.intra_clearance_violations()
    ), "Repair pass dropped the original violation despite failing to fix it"


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

    def _succeeding_route(pair, spacing=None, coupled_only=False, extra_spacing_cells=0):
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

    assert resolved == 1, (
        f"Repair pass should have resolved exactly 1 pair; got {resolved}"
    )
    assert call_count["value"] == 1, (
        f"Repair pass should have succeeded on the first attempt; "
        f"made {call_count['value']} attempts"
    )
    # Original violation must be gone from the buffer.
    assert not any(
        v.positive_net_name == "USB_D+"
        for v in dpr.intra_clearance_violations()
    ), "Repair pass left the original violation in the buffer after successful resolution"


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
    from types import SimpleNamespace

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
        Pad(x=1.0, y=1.0, width=1.0, height=1.0, net=1, net_name="USB_D+",
            layer=Layer.F_CU, ref="J1", pin="1"),
        Pad(x=5.0, y=1.0, width=1.0, height=1.0, net=1, net_name="USB_D+",
            layer=Layer.F_CU, ref="U1", pin="1"),
    ]
    n_pads = [
        Pad(x=1.0, y=2.0, width=1.0, height=1.0, net=2, net_name="USB_D-",
            layer=Layer.F_CU, ref="J1", pin="2"),
        Pad(x=5.0, y=2.0, width=1.0, height=1.0, net=2, net_name="USB_D-",
            layer=Layer.F_CU, ref="U1", pin="2"),
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
        dpr.route_differential_pair_coupled(
            pair, coupled_only=True, extra_spacing_cells=0
        )
        # Second call: extra_spacing_cells=2.
        dpr.route_differential_pair_coupled(
            pair, coupled_only=True, extra_spacing_cells=2
        )

    assert len(captured_kwargs) == 2, (
        f"Stub CoupledPathfinder was called {len(captured_kwargs)} times; "
        f"expected 2"
    )
    base_floor = captured_kwargs[0]["min_spacing_cells"]
    wider_floor = captured_kwargs[1]["min_spacing_cells"]
    assert wider_floor == base_floor + 2, (
        f"extra_spacing_cells=2 should widen min_spacing_cells by 2; "
        f"got baseline={base_floor}, wider={wider_floor}"
    )
