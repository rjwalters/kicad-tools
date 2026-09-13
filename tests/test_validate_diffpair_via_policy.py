"""The PCB skew checker measures ordinary vias using manufacturing policy."""

from types import SimpleNamespace

import pytest

from kicad_tools.router.rules import NetClassRouting
from kicad_tools.validate.diffpair_skew import derive_skew_data


@pytest.mark.parametrize("via_net", [1, 2], ids=["positive", "negative"])
@pytest.mark.parametrize(
    ("layers", "via_type", "policy", "expected_skew"),
    [
        (["F.Cu", "In1.Cu"], "through", None, 1.6),
        (["F.Cu", "In1.Cu"], "through", False, 1.6),
        (["F.Cu", "In1.Cu"], "through", True, 1.6 / 3),
        (["F.Cu", "B.Cu"], "through", None, 1.6),
        (["F.Cu", "B.Cu"], "through", True, 1.6),
        (["F.Cu", "In1.Cu"], "micro", None, 1.6 / 3),
        (["F.Cu", "In1.Cu"], "micro", True, 1.6 / 3),
    ],
    ids=[
        "ordinary-default",
        "ordinary-through-policy",
        "ordinary-blind-policy",
        "through-default",
        "through-blind-policy",
        "micro-default",
        "micro-blind-policy",
    ],
)
def test_checker_via_span_policy(via_net, layers, via_type, policy, expected_skew):
    # Equal planar copper makes the skew isolate the lone via's contribution.
    segment = SimpleNamespace(start=(0.0, 0.0), end=(10.0, 0.0))
    via = SimpleNamespace(layers=layers, via_type=via_type)
    pcb = SimpleNamespace(
        nets={1: SimpleNamespace(name="USB_D+"), 2: SimpleNamespace(name="USB_D-")},
        segments_in_net=lambda net_id: iter([segment]),
        vias_in_net=lambda net_id: iter([via] if net_id == via_net else []),
    )
    nc = NetClassRouting(name="USB", coupled_routing=True, skew_tolerance_mm=0.05)
    kwargs = {} if policy is None else {"blind_buried_supported": policy}

    skew, thresholds = derive_skew_data(
        pcb,
        {"USB_D+": nc, "USB_D-": nc},
        board_thickness_mm=1.6,
        num_copper_layers=4,
        **kwargs,
    )

    assert skew[("USB_D+", "USB_D-")] == pytest.approx(expected_skew)
    assert thresholds == {(1, 2): 0.05}
