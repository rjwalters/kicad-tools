"""Tests for Issue #3321: ``Autorouter.route_all_with_diffpairs`` honours
``--timeout`` and auto-derives a per-pair budget for the CoupledPathfinder.

Background: prior to #3321 the top-level
``Autorouter.route_all_with_diffpairs`` had no ``timeout`` parameter, so
the CLI's ``--timeout`` flag never reached the diff-pair pre-pass even
when the recipe set it.  On board 07 (matchgroup-test) this caused
``CoupledPathfinder`` to peg CPU at 99.7 % for >40 minutes when
``--differential-pairs --timeout 600`` was supplied: the per-pair
coupled A* never self-aborted because ``per_pair_timeout`` was ``None``
and there was no upstream budget.

Issue #3275 (board 07 differential-pairs re-enable) is blocked on this
infrastructure -- the wager can only be evaluated once the pre-pass is
bounded.

The contract this test suite pins:

1. ``timeout`` is accepted as a kwarg on
   ``Autorouter.route_all_with_diffpairs`` with default ``None`` so
   back-compat is preserved (the legacy CoupledPathfinder behaviour is
   unbounded when neither ``--timeout`` nor
   ``--diffpair-per-pair-timeout`` is supplied).
2. When ``timeout`` is set AND ``diffpair_config.per_pair_timeout`` is
   ``None``, the inner ``DiffPairRouter.route_all_with_diffpairs`` is
   invoked with a derived ``per_pair_timeout`` equal to
   ``max(5.0, min(timeout * 0.3, 60.0))``.
3. When ``timeout`` is set AND ``diffpair_config.per_pair_timeout`` is
   ALSO set, the explicit config wins (no override; the user's
   ``--diffpair-per-pair-timeout`` takes precedence).
4. When ``timeout`` is ``None`` (legacy CLI invocation), the inner
   router is invoked with ``per_pair_timeout=None`` -- back-compat.
5. The CLI ``route`` command actually passes ``_budgeted_timeout(args)``
   to the top-level method at every diff-pair callsite (regression
   guard against future refactors that drop the kwarg).
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair import DifferentialPairConfig


# ---------------------------------------------------------------------------
# Signature surface
# ---------------------------------------------------------------------------


def test_autorouter_route_all_with_diffpairs_accepts_timeout_kwarg():
    """The top-level entry point exposes a ``timeout`` parameter."""
    sig = inspect.signature(Autorouter.route_all_with_diffpairs)
    assert "timeout" in sig.parameters, (
        "Autorouter.route_all_with_diffpairs must accept a 'timeout' "
        "kwarg so the CLI's --timeout reaches the CoupledPathfinder "
        "(issue #3321)."
    )


def test_autorouter_route_all_with_diffpairs_timeout_default_is_none():
    """Back-compat: the default must be ``None`` so existing callers
    that do not pass ``timeout`` see unchanged behaviour."""
    sig = inspect.signature(Autorouter.route_all_with_diffpairs)
    assert sig.parameters["timeout"].default is None


# ---------------------------------------------------------------------------
# Forwarding semantics: how the timeout reaches the inner router
# ---------------------------------------------------------------------------


def _make_autorouter_stub() -> Autorouter:
    """Build a minimal ``Autorouter`` instance for unit-testing the
    diff-pair forwarding logic without running the real router.

    We bypass ``__init__`` because instantiating ``Autorouter`` requires
    a full board state; the methods under test only touch ``_diffpair``
    and ``_diffpair_router``, both of which we stub.
    """
    router = Autorouter.__new__(Autorouter)
    router._diffpair_router = None
    # ``self._diffpair`` is a @property that lazy-inits the router.
    # We stub the inner router directly so the property never fires.
    inner = MagicMock()
    inner.route_all_with_diffpairs = MagicMock(return_value=([], []))
    inner.intra_clearance_violations = MagicMock(return_value=[])
    inner.repair_intra_clearance_violations = MagicMock()
    router._diffpair_router = inner

    # ``_finalize_routing`` runs after the inner call; stub it.
    router._finalize_routing = MagicMock()
    return router


def test_timeout_derives_per_pair_budget_when_config_is_unset():
    """When ``timeout=600`` and ``per_pair_timeout`` is unset, the inner
    router must receive a derived budget of ``min(600*0.3, 60) = 60.0``."""
    router = _make_autorouter_stub()
    cfg = DifferentialPairConfig(enabled=True)
    assert cfg.per_pair_timeout is None  # precondition

    router.route_all_with_diffpairs(cfg, timeout=600.0)

    inner = router._diffpair_router
    inner.route_all_with_diffpairs.assert_called_once()
    kwargs = inner.route_all_with_diffpairs.call_args.kwargs
    assert "per_pair_timeout" in kwargs
    # 600 * 0.3 = 180, capped at 60.
    assert kwargs["per_pair_timeout"] == 60.0


def test_timeout_derived_budget_floored_at_5s_for_tiny_timeout():
    """For very small ``--timeout`` values (e.g. 10 s) the derived
    per-pair budget must not collapse below 5 s, otherwise every pair
    would abort before A* gets a chance to converge."""
    router = _make_autorouter_stub()
    cfg = DifferentialPairConfig(enabled=True)

    router.route_all_with_diffpairs(cfg, timeout=10.0)

    kwargs = router._diffpair_router.route_all_with_diffpairs.call_args.kwargs
    # 10 * 0.3 = 3, floored at 5.
    assert kwargs["per_pair_timeout"] == 5.0


def test_timeout_derived_budget_uses_30_percent_when_in_range():
    """For mid-range ``--timeout`` values the derived budget is
    ``timeout * 0.3``: 100 s -> 30 s (below the 60 s cap, above the 5 s
    floor)."""
    router = _make_autorouter_stub()
    cfg = DifferentialPairConfig(enabled=True)

    router.route_all_with_diffpairs(cfg, timeout=100.0)

    kwargs = router._diffpair_router.route_all_with_diffpairs.call_args.kwargs
    assert kwargs["per_pair_timeout"] == 30.0


def test_explicit_per_pair_timeout_takes_precedence_over_derived():
    """If the user explicitly set ``--diffpair-per-pair-timeout`` via
    ``DifferentialPairConfig.per_pair_timeout``, the auto-derivation
    must NOT fire (the user's explicit budget wins)."""
    router = _make_autorouter_stub()
    cfg = DifferentialPairConfig(enabled=True, per_pair_timeout=15.0)

    router.route_all_with_diffpairs(cfg, timeout=600.0)

    kwargs = router._diffpair_router.route_all_with_diffpairs.call_args.kwargs
    # The kwarg must be None so the inner router falls back to the
    # config value (15.0) per its own precedence rule.
    assert kwargs["per_pair_timeout"] is None


def test_timeout_none_preserves_legacy_unbounded_behaviour():
    """No ``timeout`` -> no derived budget -> back-compat preserved.

    This is the critical back-compat test: existing tests and callers
    that never passed ``timeout`` must continue to invoke the inner
    router with ``per_pair_timeout=None``.
    """
    router = _make_autorouter_stub()
    cfg = DifferentialPairConfig(enabled=True)

    router.route_all_with_diffpairs(cfg)  # no timeout

    kwargs = router._diffpair_router.route_all_with_diffpairs.call_args.kwargs
    assert kwargs["per_pair_timeout"] is None


def test_timeout_zero_or_negative_does_not_derive_budget():
    """Defensive: a ``timeout <= 0`` is treated the same as ``None``
    (no derivation, no forwarding).  This protects against odd CLI
    states where ``--timeout 0`` slipped through validation."""
    router = _make_autorouter_stub()
    cfg = DifferentialPairConfig(enabled=True)

    router.route_all_with_diffpairs(cfg, timeout=0.0)

    kwargs = router._diffpair_router.route_all_with_diffpairs.call_args.kwargs
    assert kwargs["per_pair_timeout"] is None


def test_timeout_does_not_derive_when_diffpair_disabled():
    """When ``diffpair_config.enabled`` is False, the derivation is
    skipped entirely (the inner router short-circuits to ``route_all``
    so the per-pair budget would never be consulted anyway)."""
    router = _make_autorouter_stub()
    cfg = DifferentialPairConfig(enabled=False)

    router.route_all_with_diffpairs(cfg, timeout=600.0)

    kwargs = router._diffpair_router.route_all_with_diffpairs.call_args.kwargs
    assert kwargs["per_pair_timeout"] is None


def test_timeout_does_not_derive_when_config_is_none():
    """When ``diffpair_config`` itself is ``None`` (the truly-legacy
    invocation path) the derivation is skipped."""
    router = _make_autorouter_stub()

    router.route_all_with_diffpairs(None, timeout=600.0)

    kwargs = router._diffpair_router.route_all_with_diffpairs.call_args.kwargs
    assert kwargs["per_pair_timeout"] is None


# ---------------------------------------------------------------------------
# Structured-log signal
# ---------------------------------------------------------------------------


def test_autoderive_emits_structured_log_signal(caplog):
    """When auto-derivation fires, an INFO-level
    ``DIFFPAIR_PER_PAIR_TIMEOUT_AUTODERIVED`` log line is emitted so log
    parsers / CI gates can distinguish explicit configuration from the
    safety-net fallback (similar to the AUTOFIX_SKIPPED pattern from
    PR #3247)."""
    import logging

    router = _make_autorouter_stub()
    cfg = DifferentialPairConfig(enabled=True)

    with caplog.at_level(logging.INFO, logger="kicad_tools.router.core"):
        router.route_all_with_diffpairs(cfg, timeout=100.0)

    autoderive_records = [
        r for r in caplog.records
        if "DIFFPAIR_PER_PAIR_TIMEOUT_AUTODERIVED" in r.getMessage()
    ]
    assert autoderive_records, (
        "Auto-derived per-pair timeout must emit a structured log signal "
        "so triagers can tell whether the budget came from the CLI flag "
        "or from the --timeout fallback (issue #3321)."
    )


def test_autoderive_does_not_log_when_user_set_explicit_budget(caplog):
    """When the user already set ``--diffpair-per-pair-timeout`` we
    should NOT emit the auto-derived signal (the signal is reserved for
    the fallback path)."""
    import logging

    router = _make_autorouter_stub()
    cfg = DifferentialPairConfig(enabled=True, per_pair_timeout=20.0)

    with caplog.at_level(logging.INFO, logger="kicad_tools.router.core"):
        router.route_all_with_diffpairs(cfg, timeout=100.0)

    autoderive_records = [
        r for r in caplog.records
        if "DIFFPAIR_PER_PAIR_TIMEOUT_AUTODERIVED" in r.getMessage()
    ]
    assert not autoderive_records, (
        "Explicit --diffpair-per-pair-timeout must not trigger the "
        "auto-derived log signal."
    )


# ---------------------------------------------------------------------------
# CLI plumbing: regression guard
# ---------------------------------------------------------------------------


def test_cli_route_cmd_forwards_timeout_to_diffpair_callsite():
    """The CLI's diff-pair callsite must pass ``timeout=`` so the
    pre-pass actually receives the budget.  Without this, the
    auto-derivation in ``Autorouter.route_all_with_diffpairs`` cannot
    fire because no caller ever supplies the kwarg.

    Source-level regression guard: count the diff-pair callsites in
    ``route_cmd.py`` and ensure each one has a ``timeout=`` kwarg.
    """
    import pathlib

    src = (
        pathlib.Path(__file__).parent.parent
        / "src/kicad_tools/cli/route_cmd.py"
    ).read_text()

    # Locate every ``router.route_all_with_diffpairs(`` call and inspect
    # the next few lines for a ``timeout=`` kwarg.
    lines = src.splitlines()
    callsites = []
    for idx, line in enumerate(lines):
        if "router.route_all_with_diffpairs(" in line:
            # Capture the call expression up to its closing paren.
            # Crude but sufficient: take up to the next 10 lines.
            chunk = "\n".join(lines[idx : idx + 10])
            callsites.append(chunk)

    assert callsites, (
        "Expected at least one ``router.route_all_with_diffpairs(`` "
        "callsite in route_cmd.py; refactor may have moved it."
    )

    for site in callsites:
        assert "timeout=" in site, (
            "Every CLI ``router.route_all_with_diffpairs(`` callsite "
            "must forward ``timeout=`` so the CoupledPathfinder is "
            "bounded by --timeout (issue #3321).  Found a callsite "
            "without timeout forwarding:\n"
            f"{site}"
        )
