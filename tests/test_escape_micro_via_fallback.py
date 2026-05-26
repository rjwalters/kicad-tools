"""Tests for the ``--micro-via-in-pad-fallback`` opt-in (Issue #3118).

This test pins the four behaviours of the in-pad micro-via fallback
introduced inside ``EscapeRouter._try_in_pad_escape``:

1. **Default off**: the helper preserves the legacy "commit-the-via"
   behaviour bit-for-bit -- a 0.6 mm standard via is placed dead-centre
   on the primary pad even though it clips the foreign-net neighbour.
   The strict-mode fallback path (``skip_on_clearance_violation=True``)
   continues to return ``None`` exactly as #3033 expects.

2. **Flag on**: the helper retries with the 0.3 mm OD / 0.15 mm drill
   micro-via, the retry passes the clearance gate
   (pitch - micro_radius - neighbour_short / 2 = 0.50 - 0.15 - 0.15 =
   0.20 mm >= 0.15 mm clearance), and returns an EscapeRoute whose
   ``via.drill == 0.15`` and ``via.diameter == 0.3``.

3. **Tagged for the dimensions DRC exemption**: the emitted Via has
   ``is_micro=True`` (matching the schema convention from
   #3124/#3126) so ``validate/rules/dimensions.py`` recognises the
   exemption and does not flag the 0.3 / 0.15 dimensions against
   jlcpcb-tier1's 0.3 / 0.6 floor.  The same flag also makes the
   primitive serialise as ``(via micro ...)`` so the tag survives
   the optimize -> drc-nudge -> finalize round-trip.

4. **Legacy callsite (package=None)**: with the same flag enabled but
   no package context, the helper falls through to the existing
   dead-centre placement with standard via dimensions (no behaviour
   change for callers that do not provide neighbour context).

The fixture (``tests/fixtures/strict_in_pad_min.py``) is pinned to the
exact OSC_OUT short-axis geometry: 0.3 x 1.5 mm pads at 0.5 mm pitch,
0.15 mm clearance, dead-centre 0.6 mm via clips by 0.10 mm.  See the
fixture's docstring for the geometric derivation.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.escape import EscapeDirection, EscapeRouter
from tests.fixtures.strict_in_pad_min import (
    CLEARANCE,
    make_grid,
    make_package,
    make_rules,
    make_violating_pair,
)


def _build_router(
    micro_via_in_pad_fallback: bool = False,
    micro_via_diameter: float = 0.3,
    micro_via_drill: float = 0.15,
    manufacturer: str | None = "jlcpcb-tier1",
) -> EscapeRouter:
    """Construct an EscapeRouter on the violating-pair geometry.

    The ``micro_via_*`` parameters are stamped onto the constructed
    router after the fact -- they live as constructor-set attributes
    on the router (the CLI knob path uses env vars to stamp them at
    ``__init__`` time, but tests can flip them directly to keep the
    arrange step concise).
    """
    rules = make_rules(manufacturer=manufacturer)
    grid = make_grid(rules)
    router = EscapeRouter(grid, rules)
    router.micro_via_in_pad_fallback = micro_via_in_pad_fallback
    router.micro_via_diameter = micro_via_diameter
    router.micro_via_drill = micro_via_drill
    return router


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Clear env vars before each test for deterministic defaults."""
    monkeypatch.delenv("KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE", raising=False)
    monkeypatch.delenv("KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK", raising=False)
    monkeypatch.delenv("KICAD_TOOLS_MICRO_VIA_SIZE", raising=False)
    monkeypatch.delenv("KICAD_TOOLS_MICRO_VIA_DRILL", raising=False)
    yield


# ----------------------------------------------------------------------------
# Default off: legacy behaviour preserved
# ----------------------------------------------------------------------------


class TestMicroViaFallbackDefaultOff:
    """With the flag off, the helper preserves pre-#3118 behaviour."""

    def test_default_attribute_is_false(self):
        """Fresh EscapeRouter has micro_via_in_pad_fallback=False so the
        default routing pipeline is unaffected by this work.
        """
        router = _build_router()
        assert router.micro_via_in_pad_fallback is False
        # Defaults match the stitch --micro-via triple.
        assert router.micro_via_diameter == pytest.approx(0.3)
        assert router.micro_via_drill == pytest.approx(0.15)

    def test_strict_path_still_returns_none(self):
        """In strict mode without the flag, the helper still defers --
        the strict-mode behaviour from #3033 is preserved bit-for-bit.
        """
        router = _build_router(micro_via_in_pad_fallback=False)
        router.strict_in_pad_clearance = True
        package = make_package()
        primary = package.pads[0]

        route = router._try_in_pad_escape(
            pad=primary,
            direction=EscapeDirection.SOUTH,
            effective_clearance=CLEARANCE,
            escape_width=0.2,
            package=package,
            skip_on_clearance_violation=True,
        )
        assert route is None, (
            "Default-off strict path must continue to return None per "
            "Issue #3033 -- the micro-via fallback only activates when "
            "explicitly opted in."
        )


# ----------------------------------------------------------------------------
# Flag on: micro-via rescue path
# ----------------------------------------------------------------------------


class TestMicroViaFallbackOn:
    """With the flag on, the helper retries with micro-via dimensions."""

    def test_returns_route_with_micro_via_dimensions(self):
        """The retry passes the clearance gate and the emitted Via has
        the 0.3 / 0.15 dimensions.

        Geometric reasoning:
            pitch - micro_radius - neighbour_short / 2
          = 0.50  - 0.15          - 0.15
          = 0.20  mm >= 0.15 mm clearance -- passes.
        """
        router = _build_router(micro_via_in_pad_fallback=True)
        package = make_package()
        primary = package.pads[0]

        route = router._try_in_pad_escape(
            pad=primary,
            direction=EscapeDirection.SOUTH,
            effective_clearance=CLEARANCE,
            escape_width=0.2,
            package=package,
        )
        assert route is not None, (
            "Micro-via fallback must rescue this geometry -- standard "
            "0.6 mm via clips foreign neighbour by 0.10 mm, micro-via "
            "0.3 mm has 0.05 mm slack inside the 0.15 mm clearance."
        )
        assert route.via is not None
        assert route.via.diameter == pytest.approx(0.3)
        assert route.via.drill == pytest.approx(0.15)

    def test_strict_path_rescued_by_micro_via(self):
        """The fallback works under strict mode too: the strict-mode
        ``return None`` only fires when the micro-via cannot rescue.
        Here the micro-via clears the neighbour so strict mode should
        return a Route, not None.
        """
        router = _build_router(micro_via_in_pad_fallback=True)
        router.strict_in_pad_clearance = True
        package = make_package()
        primary = package.pads[0]

        route = router._try_in_pad_escape(
            pad=primary,
            direction=EscapeDirection.SOUTH,
            effective_clearance=CLEARANCE,
            escape_width=0.2,
            package=package,
            skip_on_clearance_violation=True,
        )
        assert route is not None, (
            "Strict mode + micro-via fallback must rescue this geometry "
            "instead of deferring -- micro-via passes the clearance gate."
        )
        assert route.via.diameter == pytest.approx(0.3)
        assert route.via.drill == pytest.approx(0.15)

    def test_emitted_via_is_tagged_micro(self):
        """The emitted via has ``is_micro=True`` so the dimensions
        DRC rule recognises the exemption and does not flag the 0.3 /
        0.15 dimensions against jlcpcb-tier1's 0.3 / 0.6 floor.  The
        ``is_micro`` flag also drives the ``(via micro ...)``
        s-expression serialisation that survives the finalize
        round-trip after #3126.
        """
        router = _build_router(micro_via_in_pad_fallback=True)
        package = make_package()
        primary = package.pads[0]

        route = router._try_in_pad_escape(
            pad=primary,
            direction=EscapeDirection.SOUTH,
            effective_clearance=CLEARANCE,
            escape_width=0.2,
            package=package,
        )
        assert route is not None
        assert route.via is not None
        assert route.via.is_micro is True, (
            "Micro-via fallback must tag the emitted Via so the "
            "dimensions DRC exemption applies and the via serialises "
            "as ``(via micro ...)`` (Issue #3118 / #3124)."
        )
        # The standard in-pad flag stays True too -- the fallback is a
        # retry of the same in-pad rescue, not a different code path.
        assert route.via.in_pad is True
        # And the emitted s-expression includes the ``micro`` token.
        sexp = route.via.to_sexp()
        assert "(via micro" in sexp, (
            "is_micro=True must drive the ``(via micro ...)`` "
            "serialisation so the tag survives the optimize -> "
            "drc-nudge -> finalize pipeline (#3126)."
        )


# ----------------------------------------------------------------------------
# Legacy callsite (package=None): no behaviour change
# ----------------------------------------------------------------------------


class TestMicroViaFallbackLegacyCallsite:
    """With package=None the helper falls through to the existing
    dead-centre placement with standard via dimensions, regardless of
    the flag's state.  No regression on legacy callers.
    """

    def test_package_none_uses_standard_dimensions(self):
        """No package context => no neighbour check => standard via
        dimensions even with the flag enabled.
        """
        router = _build_router(micro_via_in_pad_fallback=True)
        primary, _neighbour = make_violating_pair()

        route = router._try_in_pad_escape(
            pad=primary,
            direction=EscapeDirection.SOUTH,
            effective_clearance=CLEARANCE,
            escape_width=0.2,
            package=None,
        )
        assert route is not None, (
            "Legacy callsite (package=None) must succeed with the "
            "standard manufacturer-floor via -- no neighbour check"
        )
        assert route.via is not None
        # Manufacturer floor for jlcpcb-tier1: 0.6 mm OD / 0.3 mm drill.
        assert route.via.diameter == pytest.approx(0.6)
        assert route.via.drill == pytest.approx(0.3)
        assert route.via.is_micro is False, (
            "Standard via on the legacy callsite must NOT be tagged as "
            "a micro-via (the dimensions DRC must still flag undersize "
            "vias on this path)."
        )
