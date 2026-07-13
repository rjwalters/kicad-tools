"""Reproducible reach measurement for the DDR-byte monotone certificate.

Issue #4084 (Phase 1).  Isolates the board-07 DDR data byte (11 nets on a
facing QFN-48 pin pair) on an EMPTY 4-layer board — the "11-net bundle
alone" scenario from #3438's isolation matrix — and measures escape reach
with the monotonic-certificate ordering flag OFF (identity baseline) and
ON.  Marked ``slow`` because the negotiated route takes a few seconds even
with the C++ backend (and minutes on the pure-Python CI fallback).

Empirical result recorded here as a regression guard:

    * The DDR byte is CO-ORIENTED along the row axis (both facing columns
      declare the byte in the same net order), so the monotonic certificate
      reports it FEASIBLE (inversions=0, mirror=False) — the constructive
      order equals the row order.
    * On the isolated empty board the bundle routes 11/11 both with the
      flag OFF and ON — the certificate ordering is no worse than the
      identity baseline (the Phase-1 acceptance bar).  The #3438 2/11
      failure is a full-board-congestion effect, not intrinsic to the
      11-net bundle alone.

See ``boards/07-matchgroup-test/ddr_bundle_isolation_repro.py`` for the
standalone (non-pytest) version of this measurement.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import NetClassRouting

# Board-07 DDR byte row order on both facing columns (U1 pins 25-35 == U2
# pins 1-11), matching boards/07-matchgroup-test/generate_pcb.py.
_ROW_NETS = [
    "DQ0",
    "DQ1",
    "DQ2",
    "DQ3",
    "DM0",
    "DQS_P",
    "DQS_N",
    "DQ4",
    "DQ5",
    "DQ6",
    "DQ7",
]
_PITCH = 0.8


def _build_isolated_ddr_router(*, enable_certificate: bool) -> tuple[Autorouter, list[int]]:
    cls = NetClassRouting(
        name="DDR_DATA_BYTE_0",
        priority=1,
        trace_width=0.15,
        clearance=0.10,
        length_critical=True,
        length_match_group="DDR_DATA_BYTE_0",
        length_match_reference=None,
        length_match_tolerance_mm=0.1,
    )
    net_class_map: dict[str, NetClassRouting] = {}
    router = Autorouter(
        width=80.0,
        height=40.0,
        net_class_map=net_class_map,
        layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
    )
    router.enable_monotone_certificate_order = enable_certificate

    u1_x, u2_x = 20.0, 50.0
    base_y = 20.0 - (len(_ROW_NETS) - 1) * _PITCH / 2.0
    net_ids: list[int] = []
    for i, name in enumerate(_ROW_NETS):
        net_id = i + 1
        net_ids.append(net_id)
        y = base_y + i * _PITCH
        router.add_component(
            "U1",
            [{"number": str(25 + i), "x": u1_x, "y": y, "net": net_id, "net_name": name}],
        )
        router.add_component(
            "U2",
            [{"number": str(1 + i), "x": u2_x, "y": y, "net": net_id, "net_name": name}],
        )
        net_class_map[name] = cls
    router.net_class_map = net_class_map
    return router, net_ids


def _reach(routes: list, net_ids: list[int]) -> int:
    routed = {
        r.net
        for r in routes
        if getattr(r, "net", None) is not None and not getattr(r, "is_escape", False)
    }
    return len(routed & set(net_ids))


class TestDDRByteCertificateClassification:
    """Fast (no routing) classification check of the real board-07 bundle."""

    def test_ddr_byte_is_monotonically_feasible_as_pinned(self) -> None:
        router, net_ids = _build_isolated_ddr_router(enable_certificate=True)
        router._apply_byte_lane_inner_priority(list(net_ids))
        assert "DDR_DATA_BYTE_0" in router._last_monotone_certificates
        cert = router._last_monotone_certificates["DDR_DATA_BYTE_0"]
        # Co-oriented facing columns -> feasible, no forced crossings.
        assert cert.feasible is True
        assert cert.inversion_count == 0
        assert cert.witness == []


@pytest.mark.slow
class TestDDRByteReach:
    """Measured reach on the isolated 11-net bundle (flag OFF vs ON)."""

    def test_certificate_reach_no_worse_than_identity_baseline(self) -> None:
        router_off, ids_off = _build_isolated_ddr_router(enable_certificate=False)
        baseline = _reach(router_off.route_all_negotiated(seed=42), ids_off)

        router_on, ids_on = _build_isolated_ddr_router(enable_certificate=True)
        with_cert = _reach(router_on.route_all_negotiated(seed=42), ids_on)

        # Phase-1 acceptance: certificate ordering is NO WORSE than the
        # identity baseline on the isolated bundle.
        assert with_cert >= baseline
        # Both reach full on the empty board (the #3438 2/11 loss is a
        # full-board congestion effect, not intrinsic to the bundle).
        assert baseline == len(ids_off)
        assert with_cert == len(ids_on)
