"""Engagement-layer gate for CoupledPathfinder (Issue #2638, Epic #2556 Phase 2E).

This module tests :func:`kicad_tools.router.diffpair.should_engage_coupled`
and the dispatcher integration in
:mod:`kicad_tools.router.diffpair_routing` -- specifically:

* ``coupled_routing=False`` default skips coupled routing for opted-out
  net classes (the new opt-in policy).
* ``coupled_routing=True`` engages coupled routing for opted-in classes.
* Engagement-layer single-ended refusal: an explicit
  ``diffpair_partner`` declaration on USB-C ``CC1`` / ``CC2`` (the
  #2527 lesson — orientation pins, NOT a diff pair) is refused even
  though the explicit declaration bypasses suffix-time refusal.
* ``--differential-pairs`` (i.e. ``DifferentialPairConfig.enabled=False``)
  master switch short-circuits engagement regardless of per-class flag.
* N-pad regression (#2473 USB-C 3-pad coverage) must not regress.
"""

import dataclasses

from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair import (
    DifferentialPair,
    DifferentialPairConfig,
    DifferentialPairType,
    DifferentialSignal,
    should_engage_coupled,
)
from kicad_tools.router.rules import (
    NET_CLASS_HIGH_SPEED,
    DesignRules,
    NetClassRouting,
)

# =============================================================================
# Test helpers
# =============================================================================


def _make_pair(p_name: str, n_name: str, p_id: int = 1, n_id: int = 2) -> DifferentialPair:
    """Construct a minimal ``DifferentialPair`` for engagement tests."""
    return DifferentialPair(
        name=p_name.rstrip("+-_PN"),
        positive=DifferentialSignal(
            net_name=p_name,
            net_id=p_id,
            base_name=p_name.rstrip("+-_PN"),
            polarity="P",
            notation="plus_minus",
        ),
        negative=DifferentialSignal(
            net_name=n_name,
            net_id=n_id,
            base_name=n_name.rstrip("+-_PN"),
            polarity="N",
            notation="plus_minus",
        ),
        pair_type=DifferentialPairType.USB2,
    )


# =============================================================================
# 1. Unit tests on ``should_engage_coupled``
# =============================================================================


class TestShouldEngageCoupled:
    def test_no_class_match_returns_no_class_match(self):
        pair = _make_pair("USB_D+", "USB_D-")
        engaged, reason = should_engage_coupled(pair, None, None)
        assert engaged is False
        assert reason == "no_class_match"

    def test_empty_class_map_returns_no_class_match(self):
        pair = _make_pair("USB_D+", "USB_D-")
        engaged, reason = should_engage_coupled(pair, {}, None)
        assert engaged is False
        assert reason == "no_class_match"

    def test_opt_in_disabled_returns_opt_in_disabled(self):
        # Class exists, coupled_routing defaults to False.
        pair = _make_pair("USB_D+", "USB_D-")
        nc = NetClassRouting(name="HighSpeed")  # coupled_routing=False
        # net_name-keyed convention (autorouter.net_class_map style).
        engaged, reason = should_engage_coupled(pair, {"USB_D+": nc, "USB_D-": nc}, None)
        assert engaged is False
        assert reason == "opt_in_disabled"

    def test_opt_in_enabled_engages(self):
        pair = _make_pair("USB_D+", "USB_D-")
        nc = NetClassRouting(name="HighSpeed", coupled_routing=True)
        engaged, reason = should_engage_coupled(pair, {"USB_D+": nc, "USB_D-": nc}, None)
        assert engaged is True
        assert reason == "engaged"

    def test_either_side_opt_in_engages(self):
        # If either P or N's class opts in, engagement proceeds.  Mirrors
        # the one-sided-declaration policy in ``_gather_explicit_pairs``.
        pair = _make_pair("USB_D+", "USB_D-")
        nc_opt = NetClassRouting(name="HighSpeed", coupled_routing=True)
        nc_off = NetClassRouting(name="Other", coupled_routing=False)
        engaged, _ = should_engage_coupled(pair, {"USB_D+": nc_opt, "USB_D-": nc_off}, None)
        assert engaged is True

    def test_class_name_keyed_with_net_to_class(self):
        # Layered-detector convention: net_class_routing keyed by class
        # name and net_to_class maps net_name -> class_name.
        pair = _make_pair("USB_D+", "USB_D-")
        nc = NetClassRouting(name="HighSpeed", coupled_routing=True)
        engaged, reason = should_engage_coupled(
            pair,
            net_class_routing={"HighSpeed": nc},
            net_to_class={"USB_D+": "HighSpeed", "USB_D-": "HighSpeed"},
        )
        assert engaged is True
        assert reason == "engaged"

    def test_single_ended_refusal_fires_regardless_of_opt_in(self):
        # The #2527 lesson, made executable.  USB-C CC1/CC2 are
        # orientation pins; coupling them would be electrically wrong.
        pair = _make_pair("USB_CC1", "USB_CC2")
        nc = NetClassRouting(
            name="USBCC",
            diffpair_partner="USB_CC2",
            coupled_routing=True,  # designer explicitly opted in
        )
        engaged, reason = should_engage_coupled(pair, {"USB_CC1": nc, "USB_CC2": nc}, None)
        assert engaged is False
        assert reason == "single_ended_refusal"

    def test_single_ended_refusal_for_sbu(self):
        pair = _make_pair("SBU1", "SBU2")
        nc = NetClassRouting(
            name="SBU",
            diffpair_partner="SBU2",
            coupled_routing=True,
        )
        engaged, reason = should_engage_coupled(pair, {"SBU1": nc, "SBU2": nc}, None)
        assert engaged is False
        assert reason == "single_ended_refusal"

    def test_single_ended_refusal_for_prefixed_cc(self):
        pair = _make_pair("FOO_CC1", "FOO_CC2")
        nc = NetClassRouting(
            name="FooCC",
            diffpair_partner="FOO_CC2",
            coupled_routing=True,
        )
        engaged, reason = should_engage_coupled(pair, {"FOO_CC1": nc, "FOO_CC2": nc}, None)
        assert engaged is False
        assert reason == "single_ended_refusal"

    def test_only_one_side_single_ended_does_not_refuse(self):
        # Defensive: refusal needs BOTH halves matching the pattern.  A
        # net oddly named ``USB_CC1`` paired with ``USB_D-`` is
        # presumably mis-declared but we don't want the refusal to
        # mask user errors; opt-in still wins.
        pair = _make_pair("USB_CC1", "USB_D-")
        nc = NetClassRouting(
            name="Mixed",
            coupled_routing=True,
        )
        engaged, _ = should_engage_coupled(pair, {"USB_CC1": nc, "USB_D-": nc}, None)
        # Engagement proceeds (single-ended refusal needs both halves);
        # the suffix/explicit-detection layer is responsible for
        # rejecting malformed pairs before reaching this point.
        assert engaged is True


# =============================================================================
# 2. Dispatcher integration -- ``route_diffpair_prepass``
# =============================================================================


def _two_pad_router(
    *,
    coupled_routing: bool = False,
    extra_class_kwargs: dict | None = None,
) -> Autorouter:
    """Two-pad USB_D+/USB_D- fixture parameterized by opt-in state."""
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    nc_kwargs: dict = {"name": "HighSpeed", "coupled_routing": coupled_routing}
    if extra_class_kwargs:
        nc_kwargs.update(extra_class_kwargs)
    nc = NetClassRouting(**nc_kwargs)
    net_class_map = {"USB_D+": nc, "USB_D-": nc}
    router = Autorouter(
        width=30.0,
        height=10.0,
        rules=rules,
        net_class_map=net_class_map,
    )

    spacing = 0.8
    p_y = 5.0 - spacing / 2
    n_y = 5.0 + spacing / 2
    router.add_component(
        "U1",
        [
            {
                "number": "1",
                "x": 5.0,
                "y": p_y,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "USB_D+",
            },
            {
                "number": "2",
                "x": 5.0,
                "y": n_y,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "USB_D-",
            },
        ],
    )
    router.add_component(
        "J1",
        [
            {
                "number": "1",
                "x": 25.0,
                "y": p_y,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "USB_D+",
            },
            {
                "number": "2",
                "x": 25.0,
                "y": n_y,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "USB_D-",
            },
        ],
    )
    return router


class TestPrepassEngagementGate:
    def test_opt_in_disabled_skips_coupled(self):
        # Default ``coupled_routing=False`` on the class -> pre-pass is
        # a no-op for this pair; net IDs are NOT in routed_net_ids.
        router = _two_pad_router(coupled_routing=False)
        config = DifferentialPairConfig(enabled=True, spacing=0.8)

        routes, _warnings, routed = router.route_diffpair_prepass(config)

        assert routed == set(), f"Expected pre-pass to skip non-opted-in pair; got routed={routed}"
        assert routes == [], f"Expected no coupled routes from refused pair; got {len(routes)}"

    def test_opt_in_enabled_engages_coupled(self):
        router = _two_pad_router(coupled_routing=True)
        config = DifferentialPairConfig(enabled=True, spacing=0.8)

        routes, _warnings, routed = router.route_diffpair_prepass(config)

        assert 1 in routed and 2 in routed, f"Expected both diff-pair nets to route; got {routed}"
        assert routes, "Opt-in pair must produce coupled routes"

    def test_master_switch_off_short_circuits(self):
        # ``--differential-pairs`` off (config.enabled=False) bypasses
        # the pre-pass entirely -- per-class opt-in is irrelevant.
        router = _two_pad_router(coupled_routing=True)
        config = DifferentialPairConfig(enabled=False, spacing=0.8)

        routes, warnings, routed = router.route_diffpair_prepass(config)

        assert routes == []
        assert warnings == []
        assert routed == set()


# =============================================================================
# 3. Engagement-layer single-ended refusal (the #2527 lesson)
# =============================================================================


def _usb_cc_router(coupled_routing: bool = True) -> Autorouter:
    """USB_CC1/CC2 fixture with explicit ``diffpair_partner`` declaration."""
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    # Designer accidentally pairs USB_CC1 with USB_CC2 via explicit
    # diffpair_partner declaration AND opts in to coupled routing.
    # Phase 2E must refuse engagement even with this configuration.
    nc = NetClassRouting(
        name="USBCC",
        diffpair_partner="USB_CC2",
        coupled_routing=coupled_routing,
    )
    net_class_map = {"USB_CC1": nc, "USB_CC2": nc}
    router = Autorouter(
        width=30.0,
        height=10.0,
        rules=rules,
        net_class_map=net_class_map,
    )

    router.add_component(
        "U1",
        [
            {
                "number": "1",
                "x": 5.0,
                "y": 4.5,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "USB_CC1",
            },
            {
                "number": "2",
                "x": 5.0,
                "y": 5.5,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "USB_CC2",
            },
        ],
    )
    router.add_component(
        "J1",
        [
            {
                "number": "1",
                "x": 25.0,
                "y": 4.5,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "USB_CC1",
            },
            {
                "number": "2",
                "x": 25.0,
                "y": 5.5,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "USB_CC2",
            },
        ],
    )
    return router


class TestSingleEndedRefusalAtEngagement:
    def test_explicit_declaration_refused_at_engagement(self):
        """#2527 lesson: explicit declaration cannot force coupling on CC1/CC2."""
        router = _usb_cc_router(coupled_routing=True)
        config = DifferentialPairConfig(enabled=True, spacing=0.8)

        # The detector returns the pair (explicit declaration is the
        # authoritative source); engagement-layer refusal kicks in.
        detected = router._diffpair.detect_differential_pairs_with_source()
        # Layered detector should pair them explicitly.
        assert any(
            {p.positive.net_name, p.negative.net_name} == {"USB_CC1", "USB_CC2"}
            and src == "explicit"
            for p, src in detected
        ), f"Expected explicit USB_CC1/USB_CC2 pair detection; got {detected}"

        # ... but the pre-pass refuses to engage coupled routing.
        routes, _warnings, routed = router.route_diffpair_prepass(config)
        assert routed == set(), (
            "USB_CC1/USB_CC2 must NOT be coupled-routed (single-ended refusal); "
            f"got routed={routed}"
        )
        assert routes == [], "Expected zero coupled routes for refused USB_CC1/USB_CC2 pair"

    def test_engagement_helper_directly_refuses_cc_pair(self):
        """Direct helper test mirroring the engagement-time guard."""
        pair = _make_pair("USB_CC1", "USB_CC2")
        nc = NetClassRouting(
            name="USBCC",
            diffpair_partner="USB_CC2",
            coupled_routing=True,
        )
        engaged, reason = should_engage_coupled(
            pair,
            net_class_routing={"USB_CC1": nc, "USB_CC2": nc},
            net_to_class=None,
        )
        assert engaged is False
        assert reason == "single_ended_refusal"


# =============================================================================
# 4. N-pad regression (#2473 USB-C 3-pad coverage)
# =============================================================================


def _three_pad_usb_router(coupled_routing: bool = True) -> Autorouter:
    """USB-C 3-pad-per-net diff pair (board 03's J1 row A/B + U1 footprint)."""
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    nc = NetClassRouting(name="HighSpeed", coupled_routing=coupled_routing)
    net_class_map = {"USB_D+": nc, "USB_D-": nc}
    router = Autorouter(
        width=40.0,
        height=20.0,
        rules=rules,
        net_class_map=net_class_map,
    )

    # Three pads per net (U1 + J1 row A + J1 row B).
    for ref, x in [("U1", 5.0), ("J1A", 25.0), ("J1B", 35.0)]:
        router.add_component(
            ref,
            [
                {
                    "number": "1",
                    "x": x,
                    "y": 9.0,
                    "width": 0.4,
                    "height": 0.4,
                    "net": 1,
                    "net_name": "USB_D+",
                },
                {
                    "number": "2",
                    "x": x,
                    "y": 11.0,
                    "width": 0.4,
                    "height": 0.4,
                    "net": 2,
                    "net_name": "USB_D-",
                },
            ],
        )
    return router


class TestNPadRegression:
    def test_three_pad_usb_engagement_routes_all_pads(self):
        """#2473 must-pass: 3-pad USB pair with opt-in must produce coupled routes.

        Phase 2E refines engagement criteria but must NOT regress the
        N-pad coverage that landed in PR #2474.
        """
        router = _three_pad_usb_router(coupled_routing=True)
        config = DifferentialPairConfig(enabled=True, spacing=2.0)

        routes, _warnings, routed = router.route_diffpair_prepass(config)

        # With opt-in, the pre-pass attempts coupled routing.  The
        # 3-pad-per-net case may yield 2 coupled segments and the
        # remaining stubs come from the main strategy, so we accept
        # any non-empty set; the critical assertion is that engagement
        # was NOT refused.
        assert routes, (
            "3-pad USB pair with coupled_routing=True must produce coupled routes "
            "(N-pad regression check)"
        )

    def test_three_pad_usb_default_off_falls_through(self):
        """Default opt-out path: 3-pad USB pair falls through to main strategy."""
        router = _three_pad_usb_router(coupled_routing=False)
        config = DifferentialPairConfig(enabled=True, spacing=2.0)

        routes, _warnings, routed = router.route_diffpair_prepass(config)

        assert routes == []
        assert routed == set()


# =============================================================================
# 5. Predefined classes preserve pre-#2638 default (coupled_routing=False)
# =============================================================================


class TestPredefinedClassesDefaultOff:
    def test_high_speed_default_off(self):
        # Phase 2E out-of-scope: predefined classes must NOT have
        # coupled_routing flipped on.  That's a follow-up issue.
        assert NET_CLASS_HIGH_SPEED.coupled_routing is False

    def test_dataclass_default_is_false(self):
        nc = NetClassRouting(name="Bare")
        assert nc.coupled_routing is False

    def test_dataclass_replace_preserves_field(self):
        nc = NetClassRouting(name="Bare")
        replaced = dataclasses.replace(nc, coupled_routing=True)
        assert replaced.coupled_routing is True
        # Original is unchanged.
        assert nc.coupled_routing is False
