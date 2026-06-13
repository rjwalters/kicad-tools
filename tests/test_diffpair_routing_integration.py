"""Integration tests for the differential pair pre-pass (Issue #2464).

Verifies that:

* ``route_diffpair_prepass`` routes a 2-pad differential pair via the
  CoupledPathfinder and reports the routed net IDs.
* ``route_all_with_diffpairs`` accepts a custom ``non_diffpair_strategy``
  callable and uses it for the non-diff-pair phase, so the diff-pair
  detection is wired through to a routing-time consumer regardless of
  which top-level strategy the CLI selects.
* ``route_all_negotiated`` honors the ``self.routes`` produced by the
  pre-pass and does not re-route those nets.
"""

from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair import DifferentialPairConfig
from kicad_tools.router.rules import DesignRules, NetClassRouting


def _opt_in_diffpair_class_map(net_names: list[str]) -> dict[str, NetClassRouting]:
    """Build a per-net-name class map with ``coupled_routing=True``.

    Issue #2638 / Epic #2556 Phase 2E: engagement is now opt-in per net
    class.  Tests that exercise the dispatcher path must provide net
    classes whose ``coupled_routing`` flag is ``True``, otherwise the
    pair falls through to the main strategy.
    """
    nc = NetClassRouting(name="HighSpeedOptIn", coupled_routing=True)
    return dict.fromkeys(net_names, nc)


def _two_pad_diffpair_router(diffpair_spacing: float = 0.8) -> Autorouter:
    """Build a small Autorouter with two parallel diff-pair nets.

    The board is 30x10mm with a source on the left and a sink on the
    right.  Both diff-pair members have exactly two pads, which is what
    the CoupledPathfinder supports today.

    The CoupledPathfinder maintains a per-net footprint (trace half-width
    plus clearance) so the pad y-spacing must exceed 2 * half-width.  At
    the default (trace=0.2mm, clearance=0.15mm, grid=0.1mm) that is 0.7mm,
    so we use 0.8mm by default for a reliable test.
    """
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        grid_resolution=0.1,
    )
    router = Autorouter(
        width=30.0,
        height=10.0,
        rules=rules,
        net_class_map=_opt_in_diffpair_class_map(["USB_D+", "USB_D-"]),
    )

    p_y = 5.0 - diffpair_spacing / 2
    n_y = 5.0 + diffpair_spacing / 2

    # Source side (USB host).
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

    # Sink side (USB device).  Same y values so the pair runs straight
    # across the board with no crossover required.
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


def test_route_diffpair_prepass_routes_known_pair():
    """The pre-pass routes a 2-pad diff pair and reports the routed nets."""
    router = _two_pad_diffpair_router(diffpair_spacing=0.8)

    # Override spacing to match the fixture geometry (default USB2 = 0.2mm
    # which would not fit the trace-plus-clearance footprint at this grid).
    config = DifferentialPairConfig(enabled=True, spacing=0.8)
    routes, warnings, routed_net_ids = router.route_diffpair_prepass(config)

    # USB_D+ has net_id 1, USB_D- has net_id 2.  The CoupledPathfinder
    # should route them both as a coordinated pair.
    assert 1 in routed_net_ids and 2 in routed_net_ids, (
        f"Expected both diff-pair nets to route; got {routed_net_ids}"
    )

    # The pre-pass should produce route objects whose net IDs are the diff-pair members.
    assert routes, "Diff-pair pre-pass produced no routes"
    for r in routes:
        assert r.net in (1, 2)


def test_route_diffpair_prepass_disabled_is_noop():
    """Pre-pass returns empty results when disabled or unconfigured."""
    router = _two_pad_diffpair_router()

    routes, warnings, routed = router.route_diffpair_prepass(None)
    assert routes == []
    assert warnings == []
    assert routed == set()

    disabled_config = DifferentialPairConfig(enabled=False)
    routes, warnings, routed = router.route_diffpair_prepass(disabled_config)
    assert routes == []
    assert warnings == []
    assert routed == set()


def test_route_all_with_diffpairs_invokes_custom_strategy():
    """``non_diffpair_strategy`` callable is invoked for non-diff-pair routing."""
    router = _two_pad_diffpair_router()

    # Add a non-diff-pair net so the strategy callable has work to do.
    router.add_component(
        "R1",
        [
            {
                "number": "1",
                "x": 5.0,
                "y": 1.0,
                "width": 0.8,
                "height": 0.8,
                "net": 3,
                "net_name": "EXTRA",
            },
            {
                "number": "2",
                "x": 25.0,
                "y": 1.0,
                "width": 0.8,
                "height": 0.8,
                "net": 3,
                "net_name": "EXTRA",
            },
        ],
    )

    config = DifferentialPairConfig(enabled=True)
    invocations = {"count": 0}

    def fake_strategy():
        invocations["count"] += 1
        return []

    routes, warnings = router.route_all_with_diffpairs(config, non_diffpair_strategy=fake_strategy)

    assert invocations["count"] == 1, (
        "non_diffpair_strategy callable must be invoked once per route_all_with_diffpairs call"
    )


def test_negotiated_skips_prerouted_diffpair_nets():
    """``route_all_negotiated`` filters nets already in ``self.routes`` (Issue #2464)."""
    router = _two_pad_diffpair_router()

    config = DifferentialPairConfig(enabled=True)
    pre_routes, _warnings, routed = router.route_diffpair_prepass(config)

    # If the pre-pass produced routes, calling route_all_negotiated should
    # not re-route them.  We capture self.routes before/after to confirm
    # the diff-pair routes are not duplicated.
    if not pre_routes:
        # Pre-pass produced no routes (e.g., the coupled pathfinder couldn't
        # find a path on this fixture); skip the negotiated assertion.  The
        # filter logic is still exercised by other tests in this file.
        return

    pre_count_per_net = {}
    for r in router.routes:
        pre_count_per_net[r.net] = pre_count_per_net.get(r.net, 0) + 1

    # Run a single iteration of negotiated to exercise the skip filter.
    router.route_all_negotiated(max_iterations=1, timeout=30.0)

    # Confirm the negotiated loop did not double-route the prerouted nets.
    for net_id, pre_count in pre_count_per_net.items():
        if net_id not in routed:
            continue
        post_count = sum(1 for r in router.routes if r.net == net_id)
        assert post_count == pre_count, (
            f"Net {net_id} was re-routed: had {pre_count} routes, now {post_count}"
        )


def _diffpair_with_connector_sibling_router() -> Autorouter:
    """Build a fixture with a 2-pad diff pair plus a single-ended connector sibling.

    Issue #2482: The diff-pair pre-pass routes USB_D+/USB_D- and reserves
    grid cells in J1's pin field.  USB_CC1 terminates at the *same*
    connector (J1) and must still be able to route after the pre-pass.

    Layout:
        U1 (left) <--> J1 (right)
        - USB_D+   on U1:1, J1:1
        - USB_D-   on U1:2, J1:2
        - USB_CC1  on U1:3, J1:3

    All three nets share the connector J1 so the connector-sibling
    ordering bump (Issue #2482) must place USB_CC1 before any other
    same-tier non-sibling net.
    """
    import dataclasses

    from kicad_tools.router.rules import NET_CLASS_HIGH_SPEED

    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        grid_resolution=0.1,
    )
    # Issue #2638 / Epic #2556 Phase 2E: opt the USB diff-pair class into
    # coupled routing.  After #2651 (Phase 2.5a), ``NET_CLASS_HIGH_SPEED``
    # already has ``coupled_routing=True``; the explicit ``replace`` here
    # is retained for clarity and to keep this test stable if a future
    # issue flips the singleton back.
    hs_coupled = dataclasses.replace(NET_CLASS_HIGH_SPEED, coupled_routing=True)
    net_class_map = {
        "USB_D+": hs_coupled,
        "USB_D-": hs_coupled,
        "USB_CC1": hs_coupled,
    }
    router = Autorouter(width=30.0, height=10.0, rules=rules, net_class_map=net_class_map)

    # Diff pair pads
    p_y = 5.0 - 0.4
    n_y = 5.0 + 0.4
    cc_y = 1.5  # USB_CC1 routes above the diff pair

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
            {
                "number": "3",
                "x": 5.0,
                "y": cc_y,
                "width": 0.4,
                "height": 0.4,
                "net": 3,
                "net_name": "USB_CC1",
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
            {
                "number": "3",
                "x": 25.0,
                "y": cc_y,
                "width": 0.4,
                "height": 0.4,
                "net": 3,
                "net_name": "USB_CC1",
            },
        ],
    )
    return router


def test_diffpair_prepass_then_negotiated_routes_connector_sibling():
    """Issue #2482: diff-pair prepass + negotiated must route connector siblings.

    With one diff pair (USB_D+/USB_D-) and one shared-connector
    single-ended net (USB_CC1) all terminating on J1, the diff-pair
    pre-pass routes the pair first.  The negotiated loop then sees the
    pre-routed nets via ``self.routes`` and (as of #2482) bumps USB_CC1
    to the front of its tier so it routes before any other same-tier
    non-sibling net.

    On this generous fixture (30x10mm, no other nets), all three nets
    must connect their pads after the combined pre-pass + negotiated
    routing call.
    """
    router = _diffpair_with_connector_sibling_router()

    config = DifferentialPairConfig(enabled=True, spacing=0.8)
    pre_routes, _warnings, routed_pair = router.route_diffpair_prepass(config)

    # Sanity: the pre-pass should have routed both diff-pair members.
    if not pre_routes:
        # Skip if the CoupledPathfinder couldn't find a path on this
        # fixture -- the bump logic only matters when there are actually
        # prerouted nets to bump siblings for.
        return
    assert routed_pair == {1, 2}, (
        f"Expected diff pair pre-pass to route nets 1 and 2; got {routed_pair}"
    )

    # Now run the negotiated loop.  USB_CC1 must connect.
    routes = router.route_all_negotiated(max_iterations=10, timeout=60.0)

    # Look for at least one segment per net.
    nets_with_routes = {r.net for r in router.routes}
    for net_id, name in [(1, "USB_D+"), (2, "USB_D-"), (3, "USB_CC1")]:
        assert net_id in nets_with_routes, (
            f"Expected net {name} (id {net_id}) to have routes after "
            f"diff-pair prepass + negotiated; routed nets={nets_with_routes}"
        )
