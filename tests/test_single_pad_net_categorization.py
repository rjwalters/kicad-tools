"""Unit tests for SinglePadNetRule's three-category classification.

Per issue #2613 the rule classifies single-pad signal nets into:

- ``genuine_nc`` (severity=info) -- KiCad-emitted
  ``unconnected-(REF-PIN-PadN)`` from explicit symbol NC pin
  attributes.
- ``connector_nc`` (severity=info) -- ``Net-(JN-NN)`` connector-pin
  convention, typically intentional GPIO no-connects.
- ``defect`` (severity=error) -- everything else; real schematic
  defect that the agent loop must surface.

The tests construct a minimal synthetic :class:`PCB` from raw
:class:`Footprint` / :class:`Pad` instances (no S-expression parsing)
to exercise the classification logic directly.
"""

from __future__ import annotations

import pytest

from kicad_tools.schema.pcb import PCB, Footprint, Pad
from kicad_tools.validate.rules.single_pad_net import (
    SinglePadNetRule,
    _classify_net,
)

# ---------------------------------------------------------------------------
# Pure unit tests for the classifier helper
# ---------------------------------------------------------------------------


class TestClassifyNet:
    """The ``_classify_net`` helper covers the three categories."""

    @pytest.mark.parametrize(
        "net_name,ref,expected",
        [
            # KiCad-emitted explicit-NC nets (genuine_nc).
            ("unconnected-(U1-NC-Pad4)", "U1", "genuine_nc"),
            ("unconnected-(U7-NC-Pad7)", "U7", "genuine_nc"),
            ("unconnected-(U2-Vbat-Pad6)", "U2", "genuine_nc"),
            # Connector-pin convention (connector_nc).
            ("Net-(J2-Pad11)", "J2", "connector_nc"),
            ("Net-(J2-3)", "J2", "connector_nc"),
            ("Net-(P1-Pad5)", "P1", "connector_nc"),
            # Real defects: IC pins.
            ("Net-(U3-1)", "U3", "defect"),
            ("Net-(U5-21)", "U5", "defect"),
            ("Net-(Q1-Pad2)", "Q1", "defect"),
            # Real defects: named signals.
            ("UART_TX", "U8", "defect"),
            ("I2S_BCLK", "J2", "defect"),
            ("DBG_LED2", "U8", "defect"),
            # Cross-prefix mismatch: net name says J but pad is on U
            # (data inconsistency -- fall through to defect).
            ("Net-(J5-1)", "U8", "defect"),
        ],
    )
    def test_classification(self, net_name: str, ref: str, expected: str) -> None:
        assert _classify_net(net_name, ref) == expected


# ---------------------------------------------------------------------------
# Synthetic-PCB integration tests for the full rule
# ---------------------------------------------------------------------------


def _pad(number: str, net_number: int, net_name: str) -> Pad:
    """Build a minimal Pad on F.Cu."""
    return Pad(
        number=number,
        type="smd",
        shape="rect",
        position=(0.0, 0.0),
        size=(1.0, 1.0),
        layers=["F.Cu"],
        net_number=net_number,
        net_name=net_name,
    )


def _footprint(reference: str, pads: list[Pad]) -> Footprint:
    """Build a minimal Footprint at the origin."""
    return Footprint(
        name="Test:TEST",
        layer="F.Cu",
        position=(0.0, 0.0),
        rotation=0.0,
        reference=reference,
        value=reference,
        pads=pads,
    )


def _make_synthetic_pcb(footprints: list[Footprint]) -> PCB:
    """Build a :class:`PCB` instance directly from in-memory footprints.

    This avoids the round-trip through S-expression parsing -- the
    rule reads ``pcb.footprints`` directly (which proxies to
    ``_footprints``).
    """
    pcb = PCB.__new__(PCB)
    # ``pcb.footprints`` is a property backed by ``_footprints``.
    pcb._footprints = list(footprints)
    return pcb


class TestRuleSeverityCategorization:
    """SinglePadNetRule emits info/error per category."""

    def test_genuine_nc_emits_info(self) -> None:
        """A KiCad-emitted ``unconnected-...`` net is reported at info level."""
        fp = _footprint(
            "U1",
            [_pad("4", net_number=42, net_name="unconnected-(U1-NC-Pad4)")],
        )
        pcb = _make_synthetic_pcb([fp])

        rule = SinglePadNetRule()
        results = rule.check(pcb, design_rules=None)

        assert len(results.violations) == 1
        v = results.violations[0]
        assert v.is_info, f"expected info severity, got {v.severity!r}"
        assert "explicit no-connect" in v.message.lower()
        assert v.nets == ("unconnected-(U1-NC-Pad4)",)

    def test_connector_nc_emits_info(self) -> None:
        """``Net-(J2-Pad11)`` on a J-prefixed footprint is info."""
        fp = _footprint(
            "J2",
            [_pad("11", net_number=43, net_name="Net-(J2-Pad11)")],
        )
        pcb = _make_synthetic_pcb([fp])

        rule = SinglePadNetRule()
        results = rule.check(pcb, design_rules=None)

        assert len(results.violations) == 1
        v = results.violations[0]
        assert v.is_info, f"expected info severity, got {v.severity!r}"
        assert "connector pin" in v.message.lower()

    def test_connector_nc_with_p_prefix_emits_info(self) -> None:
        """``Net-(P1-Pad5)`` on a P-prefixed footprint is info."""
        fp = _footprint(
            "P1",
            [_pad("5", net_number=44, net_name="Net-(P1-Pad5)")],
        )
        pcb = _make_synthetic_pcb([fp])

        rule = SinglePadNetRule()
        results = rule.check(pcb, design_rules=None)

        assert len(results.violations) == 1
        assert results.violations[0].is_info

    def test_named_signal_emits_error(self) -> None:
        """A named-signal singleton (e.g., UART_TX) is a real defect."""
        fp = _footprint(
            "U8",
            [_pad("10", net_number=45, net_name="UART_TX")],
        )
        pcb = _make_synthetic_pcb([fp])

        rule = SinglePadNetRule()
        results = rule.check(pcb, design_rules=None)

        assert len(results.violations) == 1
        v = results.violations[0]
        assert v.is_error, f"expected error severity, got {v.severity!r}"
        assert "missing footprint or schematic/PCB drift" in v.message

    def test_ic_pin_default_name_emits_error(self) -> None:
        """``Net-(U3-1)`` on an IC footprint is a real defect."""
        fp = _footprint(
            "U3",
            [_pad("1", net_number=46, net_name="Net-(U3-1)")],
        )
        pcb = _make_synthetic_pcb([fp])

        rule = SinglePadNetRule()
        results = rule.check(pcb, design_rules=None)

        assert len(results.violations) == 1
        assert results.violations[0].is_error

    def test_mixed_design_categorizes_each_net(self) -> None:
        """All three categories surface in a mixed-defect PCB.

        This is the smoke-test for the chorus-test-revA scenario: 1
        explicit NC, 1 connector NC, 2 real defects -- the rule must
        emit 2 infos and 2 errors.
        """
        u1 = _footprint(
            "U1",
            [
                _pad("1", net_number=1, net_name="VCC"),
                _pad("2", net_number=2, net_name="VCC"),
                _pad("4", net_number=3, net_name="unconnected-(U1-NC-Pad4)"),
            ],
        )
        j2 = _footprint(
            "J2",
            [
                _pad("11", net_number=4, net_name="Net-(J2-Pad11)"),  # connector_nc
            ],
        )
        u8 = _footprint(
            "U8",
            [
                # Multi-pad net so we have an "ok" reference net.
                _pad("1", net_number=1, net_name="VCC"),
                _pad("10", net_number=5, net_name="UART_TX"),  # defect
            ],
        )
        u3 = _footprint(
            "U3",
            [
                _pad("1", net_number=6, net_name="Net-(U3-1)"),  # defect (IC pin)
            ],
        )
        pcb = _make_synthetic_pcb([u1, j2, u8, u3])

        rule = SinglePadNetRule()
        results = rule.check(pcb, design_rules=None)

        # Expect 4 single-pad violations: 2 info + 2 error.
        # The multi-pad VCC net should not fire.
        assert len(results.violations) == 4
        infos = [v for v in results.violations if v.is_info]
        errors = [v for v in results.violations if v.is_error]
        assert len(infos) == 2, [v.message for v in infos]
        assert len(errors) == 2, [v.message for v in errors]

        info_nets = {v.nets[0] for v in infos}
        assert info_nets == {"unconnected-(U1-NC-Pad4)", "Net-(J2-Pad11)"}

        error_nets = {v.nets[0] for v in errors}
        assert error_nets == {"UART_TX", "Net-(U3-1)"}


class TestRuleExistingBehaviorPreserved:
    """The pour-net suppression and zero-violation cases still work."""

    def test_no_single_pad_returns_empty(self) -> None:
        """When every net is multi-pad, the rule emits nothing."""
        u1 = _footprint(
            "U1",
            [_pad("1", net_number=1, net_name="SIG"), _pad("2", net_number=1, net_name="SIG")],
        )
        pcb = _make_synthetic_pcb([u1])
        rule = SinglePadNetRule()
        results = rule.check(pcb, design_rules=None)
        assert results.violations == []
        assert results.rules_checked == 1
