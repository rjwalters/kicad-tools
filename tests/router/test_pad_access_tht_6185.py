"""Issue #6185: a through-hole terminal's access set spans every copper layer.

The access set (Epic #5508) enumerated exit stubs on ``pad.layer`` only. A
through-hole pad is copper on every layer, and the pathfinder starts a search
on a THT pad on every routable layer. So a THT terminal looked stranded as soon
as its last ``pad.layer`` exit closed, however open its other layers were. The
commit-time invariant then refused the commit.

On board 03 this refused USB_CC1's round-2 relief probe for "stranding" J1.A6
(USB_D+), a through-hole pin of the USB-C row whose B.Cu exits were untouched,
and the board finished 23/24.

The geometry is Phase 1a's Kelvin cluster (``test_pad_access_5508``): U3.1's
only F.Cu exit is a north corridor, closed by COMP's F.Cu track, with SMD
flankers U9 sealing the other F.Cu directions.
"""

from __future__ import annotations

from kicad_tools.router.access_witness import PASS_INITIAL
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.pad_access import compute_access_set, has_access
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules

KELVIN_POSITIONS = {"R10": (5, 3), "Q1": (7, 11), "U2": (9, 14), "U3": (5, 15)}
U9_PADS = {"1": (3.6, 15.0), "2": (6.4, 15.0), "3": (5.0, 16.4)}
COMP_Y = 14.2
COMP_X1, COMP_X2 = 1.0, 6.5
THT = {"through_hole": True, "drill": 0.4}


def _router(*, u3_tht: bool, u9_tht: bool = False) -> Autorouter:
    router = Autorouter(
        20,
        20,
        rules=DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.2),
        force_python=True,
        physics_enabled=False,
    )

    def add(ref, pin, x, y, net, net_name, extra):
        router.add_component(
            ref,
            [
                {
                    "number": pin,
                    "x": x,
                    "y": y,
                    "width": 1,
                    "height": 1,
                    "net": net,
                    "net_name": net_name,
                    "layer": Layer.F_CU,
                    **extra,
                }
            ],
        )

    for ref in ("R10", "U3", "Q1", "U2"):
        x, y = KELVIN_POSITIONS[ref]
        add(ref, "1", x, y, 1, "ISENSE_A+", THT if (ref == "U3" and u3_tht) else {})
    for pin, (x, y) in U9_PADS.items():
        add("U9", pin, x, y, 3, "FOREIGN", THT if u9_tht else {})
    router._commit_journal.set_context(PASS_INITIAL, 0)
    return router


def _comp_route(*layers: Layer) -> Route:
    return Route(
        net=2,
        net_name="COMP",
        segments=[
            Segment(
                x1=COMP_X1,
                y1=COMP_Y,
                x2=COMP_X2,
                y2=COMP_Y,
                width=0.2,
                layer=layer,
                net=2,
                net_name="COMP",
            )
            for layer in (layers or (Layer.F_CU,))
        ],
    )


def _u3(router):
    return router.pads[("U3", "1")]


def test_smd_terminal_is_still_stranded_by_its_last_own_layer_exit():
    """The SMD baseline is unchanged: B.Cu is not the pad's copper."""
    router = _router(u3_tht=False)
    router._mark_route(_comp_route())
    access = compute_access_set(_u3(router), router.grid, router.rules)
    assert access.is_empty()
    assert not has_access(_u3(router), router.grid, router.rules)


def test_tht_terminal_keeps_its_other_layer_exits():
    router = _router(u3_tht=True)
    before = compute_access_set(_u3(router), router.grid, router.rules)
    # Every B.Cu direction is open, since the SMD flankers are F.Cu only.
    assert {s.layer for s in before.stubs} == {Layer.F_CU, Layer.B_CU}

    router._mark_route(_comp_route())
    after = compute_access_set(_u3(router), router.grid, router.rules)
    assert not after.is_empty()
    assert has_access(_u3(router), router.grid, router.rules)
    # The F.Cu exits are all closed; the access that remains is B.Cu's.
    assert {s.layer for s in after.stubs} == {Layer.B_CU}


def test_tht_stub_end_via_sites_transition_from_the_stub_layer():
    router = _router(u3_tht=True)
    access = compute_access_set(_u3(router), router.grid, router.rules)
    from_layers = {site.layers[0] for site in access.via_sites}
    assert from_layers == {Layer.F_CU, Layer.B_CU}
    for site in access.via_sites:
        assert site.layers[0] == access.stubs[site.from_stub].layer


def test_invariant_admits_a_commit_that_closes_only_one_layer_of_a_tht_pin():
    """The board-03 shape: the commit is no longer refused."""
    router = _router(u3_tht=True)
    assert router._mark_route(_comp_route(), enforce_pad_access=True) is True
    assert router.pad_access_vetoes == []


def test_invariant_still_refuses_when_every_layer_of_a_tht_pin_closes():
    """Not a blanket THT exemption: sealing every layer still vetoes."""
    router = _router(u3_tht=True, u9_tht=True)
    committed = router._mark_route(_comp_route(Layer.F_CU, Layer.B_CU), enforce_pad_access=True)
    assert committed is False
    (veto,) = router.pad_access_vetoes
    assert veto.label == "U3.1"
    # The witness now counts the exits on both layers.
    assert veto.stubs_before == 2


def test_has_access_agrees_with_the_full_access_set_for_tht():
    for u9_tht in (False, True):
        for layers in ((Layer.F_CU,), (Layer.B_CU,), (Layer.F_CU, Layer.B_CU)):
            router = _router(u3_tht=True, u9_tht=u9_tht)
            router._mark_route(_comp_route(*layers))
            pad = _u3(router)
            full = compute_access_set(pad, router.grid, router.rules)
            assert has_access(pad, router.grid, router.rules) == (not full.is_empty())
