"""Tests for the ``--strict-in-pad-clearance`` opt-in (Issues #3033, #3062).

This test pins three behaviours on ``EscapeRouter._try_in_pad_escape``:

1. **Default (strict=False)**: when the dead-centre in-pad via clips a
   foreign-net neighbour pad AND the long-axis nudge cannot rescue
   (short-axis violation), the helper logs a warning and commits the
   violating via -- preserving the historical "proceed anyway, defer
   DRC to the user" branch introduced by PR #2945.

2. **Constructor-level strict=True**: same input geometry, but the
   helper returns ``None`` and emits an INFO log line describing the
   deferral.  This is the path the QFP-alternating dispatcher,
   SSOP/TSSOP dispatcher, and even-pin branch all consume via
   ``self.strict_in_pad_clearance`` (re-introduced from the dropped
   commit on PR #3038's branch).

3. **Env-var KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE=1**: the CLI knob.
   When the env var is set, ``EscapeRouter.__init__`` flips
   ``self.strict_in_pad_clearance`` to True without any constructor
   argument changes -- this is how the ``kct route
   --strict-in-pad-clearance`` flag threads the opt-in through to
   the lazily-constructed EscapeRouter without touching every call
   site between ``route_cmd`` and ``Autorouter._escape``.

Out-of-scope (covered by sub-B #3063):
- Enabling the flag on any in-tree board.
- The lateral-recovery main-router work that would let board 04 use
  the flag without dropping completion from 11/12 to 3/12 nets.
"""

from __future__ import annotations

import logging
import os

import pytest

from kicad_tools.router.escape import EscapeDirection, EscapeRouter
from tests.fixtures.strict_in_pad_min import (
    CLEARANCE,
    make_grid,
    make_package,
    make_rules,
    make_violating_pair,
)

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _build_router(manufacturer: str | None = "jlcpcb-tier1") -> EscapeRouter:
    """Build an EscapeRouter pinned to the violating-pair geometry."""
    rules = make_rules(manufacturer=manufacturer)
    grid = make_grid(rules)
    return EscapeRouter(grid, rules)


@pytest.fixture(autouse=True)
def _clear_env_strict(monkeypatch):
    """Clear the env var before each test so the default state is
    deterministic; tests that need it set use monkeypatch to set it
    explicitly.
    """
    monkeypatch.delenv("KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE", raising=False)
    yield


# ----------------------------------------------------------------------------
# Default behaviour (strict=False): legacy "proceed anyway"
# ----------------------------------------------------------------------------


class TestStrictDefaultProceedsAnyway:
    """Pre-#3033 behaviour: violating via gets committed with a warning."""

    def test_default_constructor_attribute_is_false(self):
        """When no env var is set and no opt-in flag is passed, the
        EscapeRouter's ``strict_in_pad_clearance`` attribute is False
        and the legacy "proceed anyway" branch is selected.
        """
        router = _build_router()
        assert router.strict_in_pad_clearance is False, (
            "Default EscapeRouter must have strict_in_pad_clearance=False "
            "to preserve legacy bit-for-bit behaviour"
        )

    def test_default_commits_violating_via_with_warning(self, caplog):
        """With strict=False (default), ``_try_in_pad_escape`` returns
        an EscapeRoute with the violating via and logs a warning.
        """
        router = _build_router()
        # Verify the manufacturer supports via-in-pad (precondition for
        # the helper to do anything other than early-return None).
        assert router.via_in_pad_supported, (
            "Test fixture must use a manufacturer with via_in_pad_supported"
        )

        package = make_package()
        primary = package.pads[0]

        # Invoke the helper with the same defaults the QFP dispatcher
        # uses; explicitly NOT passing skip_on_clearance_violation so
        # we exercise the default (False) path.
        with caplog.at_level(logging.WARNING, logger="kicad_tools.router.escape"):
            route = router._try_in_pad_escape(
                pad=primary,
                direction=EscapeDirection.SOUTH,
                effective_clearance=CLEARANCE,
                escape_width=0.2,
                package=package,
            )

        assert route is not None, (
            "Default (strict=False) path must commit the violating via, "
            "not defer; got None which implies the strict branch fired"
        )
        assert route.via is not None
        # Via lands dead-centre on the primary pad.
        assert abs(route.via.x - primary.x) < 1e-3
        assert abs(route.via.y - primary.y) < 1e-3
        # The structured warning describing the clearance violation
        # must appear in the log.
        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and "violates clearance to a neighboring foreign-net pad" in r.message
        ]
        assert warning_records, (
            "Expected the in-pad clearance-violation WARNING from _try_in_pad_escape; saw none"
        )


# ----------------------------------------------------------------------------
# Strict=True via constructor-attribute opt-in
# ----------------------------------------------------------------------------


class TestStrictTrueDefers:
    """With ``strict_in_pad_clearance=True`` set after construction,
    the helper returns None and logs an INFO line instead of
    committing the violation.
    """

    def test_strict_true_returns_none_and_logs_info(self, caplog):
        router = _build_router()
        # Flip the constructor-set attribute to True to simulate the
        # env-var path that the CLI flag stamps (the equivalent
        # behaviour without going through the env var).
        router.strict_in_pad_clearance = True
        assert router.via_in_pad_supported

        package = make_package()
        primary = package.pads[0]

        with caplog.at_level(logging.INFO, logger="kicad_tools.router.escape"):
            route = router._try_in_pad_escape(
                pad=primary,
                direction=EscapeDirection.SOUTH,
                effective_clearance=CLEARANCE,
                escape_width=0.2,
                package=package,
                # Pass through the dispatcher contract: dispatcher reads
                # self.strict_in_pad_clearance and forwards it as kwarg.
                skip_on_clearance_violation=router.strict_in_pad_clearance,
            )

        assert route is None, (
            "strict=True path must defer (return None) instead of committing the violating via"
        )
        info_records = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and "In-pad rescue DEFERRED" in r.message
        ]
        assert info_records, (
            "Expected the 'In-pad rescue DEFERRED' INFO log line from the "
            "strict branch; saw none.  All records:\n  "
            + "\n  ".join(f"{r.levelname}: {r.message}" for r in caplog.records)
        )

        # Also verify no warning about committed-anyway clearance violation
        # was emitted in this branch (the strict branch returns BEFORE the
        # legacy warning).
        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and "violates clearance to a neighboring foreign-net pad" in r.message
        ]
        assert not warning_records, (
            "strict=True must NOT emit the legacy proceed-anyway warning; "
            "got " + str([r.message for r in warning_records])
        )

    def test_strict_true_kwarg_overrides_default(self, caplog):
        """A False-default EscapeRouter still honours the kwarg when a
        caller passes ``skip_on_clearance_violation=True`` directly --
        this is the contract the dispatcher relies on.
        """
        router = _build_router()
        assert router.strict_in_pad_clearance is False  # default

        package = make_package()
        primary = package.pads[0]

        with caplog.at_level(logging.INFO, logger="kicad_tools.router.escape"):
            route = router._try_in_pad_escape(
                pad=primary,
                direction=EscapeDirection.SOUTH,
                effective_clearance=CLEARANCE,
                escape_width=0.2,
                package=package,
                skip_on_clearance_violation=True,  # explicit override
            )

        assert route is None
        assert any("In-pad rescue DEFERRED" in r.message for r in caplog.records)


# ----------------------------------------------------------------------------
# Env var KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE=1
# ----------------------------------------------------------------------------


class TestStrictEnvVar:
    """The env var flips the default-state attribute at construction
    time -- this is the CLI flag's transport layer.
    """

    def test_env_var_flips_constructor_attribute(self, monkeypatch):
        """With KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE=1 in the env,
        a fresh EscapeRouter has ``strict_in_pad_clearance=True``.
        """
        monkeypatch.setenv("KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE", "1")
        router = _build_router()
        assert router.strict_in_pad_clearance is True, (
            "Env var KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE=1 must flip the "
            "constructor-set strict_in_pad_clearance to True"
        )

    def test_env_var_off_keeps_attribute_false(self, monkeypatch):
        """With KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE=0 (the explicit-off
        spelling), the attribute remains False (default behaviour).
        """
        monkeypatch.setenv("KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE", "0")
        router = _build_router()
        assert router.strict_in_pad_clearance is False

    def test_env_var_unset_keeps_attribute_false(self):
        """With the env var unset (handled by the autouse fixture),
        the attribute is False.  Pin the absence-of-env-var path
        explicitly to guard against a regression in the os.environ.get
        default-value argument.
        """
        assert "KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE" not in os.environ
        router = _build_router()
        assert router.strict_in_pad_clearance is False

    def test_env_var_end_to_end_deferral(self, caplog, monkeypatch):
        """End-to-end with env var set: constructor reads the env,
        attribute becomes True, dispatcher reads the attribute and
        forwards it as kwarg, helper defers with INFO log.
        """
        monkeypatch.setenv("KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE", "1")
        router = _build_router()
        assert router.strict_in_pad_clearance is True

        package = make_package()
        primary = package.pads[0]

        with caplog.at_level(logging.INFO, logger="kicad_tools.router.escape"):
            route = router._try_in_pad_escape(
                pad=primary,
                direction=EscapeDirection.SOUTH,
                effective_clearance=CLEARANCE,
                escape_width=0.2,
                package=package,
                # Dispatcher reads self.strict_in_pad_clearance and
                # forwards it; this models the call shape from
                # _escape_qfp_alternating.
                skip_on_clearance_violation=router.strict_in_pad_clearance,
            )

        assert route is None
        assert any("In-pad rescue DEFERRED" in r.message for r in caplog.records), (
            "End-to-end env-var path must reach the DEFERRED INFO log; "
            "saw " + str([r.message for r in caplog.records])
        )


# ----------------------------------------------------------------------------
# Sanity: the violating-pair fixture is actually a violation
# ----------------------------------------------------------------------------


class TestFixtureSanity:
    """Pin the fixture's geometric pre-condition so a future refactor
    that accidentally weakens the violation (e.g. widens the pitch)
    fails loudly instead of silently masking the strict-mode tests.
    """

    def test_violating_pair_actually_violates(self):
        """The dead-centre via on primary clips neighbour by 0.10 mm."""
        primary, neighbour = make_violating_pair()
        # The via is 0.60 mm in diameter dead-centre on primary;
        # neighbour's nearest edge is at primary.y + PITCH - neighbour_short/2.
        via_radius = 0.30
        primary_to_neighbour_edge = (neighbour.y - primary.y) - (neighbour.height / 2)
        gap = primary_to_neighbour_edge - via_radius
        assert gap < CLEARANCE, (
            f"Fixture must violate the {CLEARANCE} mm clearance; got "
            f"gap={gap:.3f} mm (deficit {CLEARANCE - gap:.3f} mm)"
        )
