"""Escape + differential-pair composition guards (Issue #3952).

``--differential-pairs`` was a silent no-op on the escape / auto-layers-
escalation dispatch paths: ``route_with_escape`` returned before any diff-pair
branch ran, so the CoupledPathfinder pre-pass (Phase A) was never invoked on
boards whose fine-pitch packages force the escape dispatch (e.g. board 03's
USB-C).  Issue #3952 fixes this by:

1. Extracting ``route_with_escape``'s escape pre-phase (sub-grid pre-pass +
   dense detection + escape generation + pad-channel budgets) into a private
   ``Autorouter._run_escape_prephase`` helper.
2. Adding ``Autorouter.route_with_escape_and_diffpairs`` which delegates to the
   existing ``route_all_with_diffpairs`` (already flips
   ``paired_escape_coupling=True`` and refreshes ``diff_pair_map`` before escape
   generation) with the escape main-pass as its ``non_diffpair_strategy`` and
   ``coupled_only=True`` (budget-exit pairs fall through, no net unrouted).
3. Gating the four ``route_cmd.py`` escape-dispatch sites on
   ``args.differential_pairs`` so no-pair boards take the byte-identical old
   path.

These are FAST unit/structural guards -- they do not run a full board route.
The end-to-end xfail flip on board 03 is pinned by
``tests/router/test_board03_routing_baseline.py::TestBoard03RoutingBaseline::
test_coupled_pathfinder_phase_a_invoked``.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

REPO_ROOT = Path(__file__).resolve().parents[2]
ROUTE_CMD = REPO_ROOT / "src" / "kicad_tools" / "cli" / "route_cmd.py"


def _tssop20_pads_with_diffpair(net_offset: int = 1) -> list[Pad]:
    """TSSOP-20 dense fixture whose last two right-row pads are a diff pair.

    Mirrors the STM32G031F6P6 geometry used in
    ``tests/test_router_dense_escape_softstart.py`` so the dense-package
    detector fires, but assigns ``DP_P`` / ``DP_N`` (with
    ``diffpair_partner`` metadata handled by the caller) to two adjacent
    same-column pads so a coupled pair exists on the package.
    """
    pads: list[Pad] = []
    pin = 1
    for i in range(10):
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


def _build_router_with_tssop20() -> Autorouter:
    """Construct an Autorouter seeded with the TSSOP-20 dense fixture."""
    rules = DesignRules(
        grid_resolution=0.075,
        trace_width=0.3,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
    )
    router = Autorouter(width=20.0, height=20.0, rules=rules)
    for pad in _tssop20_pads_with_diffpair():
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
    return router


class TestEscapePrephaseExtraction:
    """The ``_run_escape_prephase`` extraction is behavior-preserving."""

    def test_run_escape_prephase_exists_and_returns_tuple(self) -> None:
        """``_run_escape_prephase`` returns ``(subgrid_escapes, dense_packages)``."""
        router = _build_router_with_tssop20()
        result = router._run_escape_prephase()
        assert isinstance(result, tuple) and len(result) == 2, (
            "Issue #3952: _run_escape_prephase must return a 2-tuple of "
            "(subgrid_escapes, dense_packages)."
        )
        subgrid_escapes, dense_packages = result
        assert isinstance(subgrid_escapes, list)
        assert isinstance(dense_packages, list)
        refs = {pkg.ref for pkg in dense_packages}
        assert "U1" in refs, (
            "The extracted pre-phase must still detect the TSSOP-20 dense "
            "package (regression firewall for the escape pre-phase extraction)."
        )

    def test_route_with_escape_delegates_to_prephase(self) -> None:
        """``route_with_escape`` still runs the pre-phase then the main pass.

        Refactor-safety guard: ``route_with_escape``'s source must call
        ``_run_escape_prephase`` (the extraction) rather than inline the
        Phase-1 body, so the escape method and the diff-pair composition share
        exactly one copy of the escape pre-phase logic.
        """
        source = inspect.getsource(Autorouter.route_with_escape)
        assert "_run_escape_prephase()" in source, (
            "route_with_escape must delegate its Phase-1 body to "
            "_run_escape_prephase (Issue #3952 extraction)."
        )

    def test_escape_generation_idempotency_guard_present(self) -> None:
        """The one-shot ``_escapes_generated_this_run`` guard exists.

        This is the documented fallback for the escape-generation idempotency
        risk the architect flagged: the diff-pair pre-pass and the closure's
        ``_run_escape_prephase`` must not double-generate escapes for the same
        dense package.  The guard lets ``_run_escape_prephase`` skip
        regeneration when the diff-pair pre-pass already escaped a package.
        """
        router = _build_router_with_tssop20()
        assert hasattr(router, "_escapes_generated_this_run")
        assert router._escapes_generated_this_run is False

        # First call generates escapes and flips the guard.
        router._run_escape_prephase()
        assert router._escapes_generated_this_run is True

        prephase_src = inspect.getsource(Autorouter._run_escape_prephase)
        assert "_escapes_generated_this_run" in prephase_src, (
            "_run_escape_prephase must consult the one-shot guard so a second "
            "call in the same run does not double-generate escapes (Issue #3952)."
        )


class TestRouteWithEscapeAndDiffpairs:
    """The composition orchestrator wires Phase A into the escape path."""

    def test_orchestrator_exists_with_expected_signature(self) -> None:
        """``route_with_escape_and_diffpairs`` exists and takes a diffpair_config."""
        assert hasattr(Autorouter, "route_with_escape_and_diffpairs")
        sig = inspect.signature(Autorouter.route_with_escape_and_diffpairs)
        params = list(sig.parameters)
        assert params[1] == "diffpair_config", (
            "route_with_escape_and_diffpairs's first non-self parameter must be diffpair_config."
        )
        for expected in ("use_negotiated", "timeout", "per_net_timeout"):
            assert expected in params, (
                f"route_with_escape_and_diffpairs must accept '{expected}' so the "
                "CLI dispatch sites can forward their per-attempt budgets."
            )

    def test_orchestrator_delegates_to_route_all_with_diffpairs(self) -> None:
        """The orchestrator reuses ``route_all_with_diffpairs`` with the escape
        main-pass as the ``non_diffpair_strategy`` and ``coupled_only=True``.

        This is the crux of the architect's design: rather than re-implement
        the paired-escape-coupling ordering, delegate to the one entry point
        that already owns it.
        """
        source = inspect.getsource(Autorouter.route_with_escape_and_diffpairs)
        assert "route_all_with_diffpairs" in source, (
            "route_with_escape_and_diffpairs must delegate to "
            "route_all_with_diffpairs (Issue #3952 design)."
        )
        assert "non_diffpair_strategy" in source
        assert "coupled_only=True" in source, (
            "coupled_only=True ensures budget-exit pairs fall through to the "
            "escape main pass so no net goes unrouted (Issue #3952)."
        )
        assert "_run_escape_prephase" in source, (
            "The non_diffpair_strategy closure must run the escape pre-phase "
            "before the main pass so escape geometry is preserved."
        )


class TestCliDispatchGating:
    """All four escape-dispatch sites are gated on ``--differential-pairs``.

    Issue #3952: the three escalation dispatch sites (layer / rule / combined)
    plus ``do_routing()``'s escape branch used to call ``route_with_escape``
    unconditionally.  They must now call ``route_with_escape_and_diffpairs``
    when diff pairs are requested and the unchanged ``route_with_escape``
    otherwise (the no-pair regression firewall).
    """

    def test_all_dispatch_sites_call_the_composed_orchestrator(self) -> None:
        """``route_with_escape_and_diffpairs`` is called at four+ sites."""
        source = ROUTE_CMD.read_text()
        n_composed = source.count("route_with_escape_and_diffpairs(")
        # 1 method def in core is elsewhere; here we count CLI call sites.
        # Four dispatch sites: layer / rule / combined escalation + do_routing.
        assert n_composed >= 4, (
            "Expected the composed orchestrator to be called at all four "
            f"escape-dispatch sites; found {n_composed} call(s).  A missing "
            "call means one dispatch path still bypasses Phase A (Issue #3952)."
        )

    def test_each_composed_call_is_gated(self) -> None:
        """Each composed-orchestrator call is guarded by a diff-pair check.

        Parses ``route_cmd.py`` and asserts that every
        ``route_with_escape_and_diffpairs`` call has a diff-pair gate (either
        ``args.differential_pairs`` or a ``_build_diffpair_config`` truthiness
        check) in an enclosing ``if`` so no-pair boards keep the old path.
        """
        source = ROUTE_CMD.read_text()
        tree = ast.parse(source)

        composed_calls: list[ast.Call] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "route_with_escape_and_diffpairs"
            ):
                composed_calls.append(node)

        assert len(composed_calls) >= 4, (
            f"Expected >= 4 composed calls, found {len(composed_calls)}."
        )

        # Build a parent map so we can walk up from each call to its enclosing
        # ``if`` and check the test guards on diff pairs.
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        def _enclosing_if_tests(node: ast.AST) -> str:
            texts: list[str] = []
            cur: ast.AST | None = node
            while cur is not None:
                if isinstance(cur, ast.If):
                    texts.append(ast.unparse(cur.test))
                cur = parents.get(cur)
            return " ".join(texts)

        for call in composed_calls:
            guard_text = _enclosing_if_tests(call)
            assert "differential_pairs" in guard_text or "_dp_cfg" in guard_text, (
                "Every route_with_escape_and_diffpairs call must sit under a "
                "diff-pair gate (args.differential_pairs / _dp_cfg) so no-pair "
                "boards take the byte-identical route_with_escape path "
                f"(Issue #3952).  Guard chain seen: {guard_text!r}"
            )

    def test_build_diffpair_config_returns_none_without_flag(self) -> None:
        """``_build_diffpair_config`` returns None when the flag is off.

        This is the regression firewall: the escalation sites gate on a
        non-None result, so a board routed WITHOUT ``--differential-pairs``
        takes the unchanged ``route_with_escape`` path.
        """
        from kicad_tools.cli.route_cmd import _build_diffpair_config

        class _Args:
            differential_pairs = False

        assert _build_diffpair_config(_Args()) is None

        class _ArgsOn:
            differential_pairs = True
            diffpair_spacing = 0.2
            diffpair_max_delta = 0.5
            diffpair_per_pair_timeout = None

        cfg = _build_diffpair_config(_ArgsOn())
        assert cfg is not None and cfg.enabled is True
        assert cfg.spacing == pytest.approx(0.2)
