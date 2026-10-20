"""Poured nets with a declared current are rated, never assumed (Issues #7210, #7206).

``AmpacityRule`` used to measure track segments only, so a net carried by a
copper pour read ``unknown (zone-only, not measured)`` from the precheck.
The rule now rates the pour itself (``validate/pour_ampacity.py``), so the
precheck no longer pre-judges poured nets: the coverage state follows from
the run (``AmpacityRule.unmeasured_pour_nets``).  These tests pin the
precheck side of that hand-off.
"""

from __future__ import annotations

from types import SimpleNamespace

from kicad_tools.validate import coverage as cov


def _checker(*, declared: dict[str, float], segment_nets=(), zone_nets=(), keepout_nets=()):
    net_class_map = {name: SimpleNamespace(target_ampacity=amps) for name, amps in declared.items()}
    pcb = SimpleNamespace(
        segments=[SimpleNamespace(net_name=n) for n in segment_nets],
        zones=[SimpleNamespace(net_name=n, keepout=None) for n in zone_nets]
        + [SimpleNamespace(net_name=n, keepout=object()) for n in keepout_nets],
    )
    return SimpleNamespace(net_class_map=net_class_map, pcb=pcb)


def test_pour_only_net_is_left_to_the_rule() -> None:
    checker = _checker(declared={"+5V": 2.0, "+3.3V": 1.0}, zone_nets=["+5V", "+3.3V"])
    assert cov.precheck("ampacity", checker) is None


def test_net_with_tracks_and_pour_is_left_to_the_rule() -> None:
    checker = _checker(declared={"+5V": 2.0}, segment_nets=["+5V"], zone_nets=["+5V"])
    assert cov.precheck("ampacity", checker) is None


def test_unmeasured_pour_is_unknown_and_blocking() -> None:
    result = cov.ampacity_pour_unknown({"+5V": "zone fill absent", "+3.3V": "1 copper pad(s)"})
    assert result.state == cov.UNKNOWN
    assert result.blocking
    assert result.reason is not None
    assert "+3.3V: 1 copper pad(s); +5V: zone fill absent" in result.reason


def test_no_declared_targets_still_skipped() -> None:
    checker = _checker(declared={}, zone_nets=["+5V"])
    result = cov.precheck("ampacity", checker)
    assert result is not None and result.state == cov.SKIPPED
