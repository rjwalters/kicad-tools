"""Test that ``kct route`` emits a top-of-output warning for single-pad nets.

This is a unit-level test for ``_emit_single_pad_net_warning`` -- the
helper that prints a banner before routing starts when the loaded PCB
has any single-pad signal nets.  Earlier versions of this test relied on
board 04 (which used to ship without its MCU and therefore had four
ghost SWD nets); issue #2531 fixed board 04, so we now exercise the
banner directly with a synthetic ``Autorouter`` shim.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from types import SimpleNamespace


def test_warning_banner_lists_signal_nets() -> None:
    """The banner prints exactly the named signal nets, suppressing pours."""
    from kicad_tools.cli.route_cmd import _emit_single_pad_net_warning

    # Synthetic router: only ``net_names`` is read by the helper.
    router = SimpleNamespace(
        net_names={
            10: "SWDIO",
            11: "SWCLK",
            12: "SWO",
            13: "NRST",
            14: "+3.3V",  # pour-net classified -- should be suppressed
            15: "GND",  # pour-net classified -- should be suppressed
        }
    )
    single_pad_nets = [10, 11, 12, 13, 14, 15]

    buf = io.StringIO()
    with redirect_stdout(buf):
        _emit_single_pad_net_warning(router, single_pad_nets)
    out = buf.getvalue()

    # The header mentions the count and "single-pad signal" wording.
    assert "single-pad signal net" in out, out
    # All four signal nets show up in the per-net list.
    for net in ("SWDIO", "SWCLK", "SWO", "NRST"):
        assert net in out, f"Expected '{net}' in output:\n{out}"
    # Pour nets are suppressed (legitimate single-pad pour-only nets).
    assert "+3.3V" not in out, out
    assert "GND" not in out, out
    # The banner points users at the check command for the full report.
    assert "kct check" in out and "single_pad_net" in out


def test_warning_banner_silent_on_empty_input() -> None:
    """No banner printed when there are no single-pad nets."""
    from kicad_tools.cli.route_cmd import _emit_single_pad_net_warning

    router = SimpleNamespace(net_names={})
    buf = io.StringIO()
    with redirect_stdout(buf):
        _emit_single_pad_net_warning(router, [])
    assert buf.getvalue() == ""


def test_warning_banner_silent_when_only_pour_nets() -> None:
    """No banner printed if every single-pad net is a pour net."""
    from kicad_tools.cli.route_cmd import _emit_single_pad_net_warning

    router = SimpleNamespace(net_names={1: "GND", 2: "+3.3V", 3: "+5V"})
    buf = io.StringIO()
    with redirect_stdout(buf):
        _emit_single_pad_net_warning(router, [1, 2, 3])
    # The banner is suppressed because every flagged net is pour-classified.
    assert "single-pad signal net" not in buf.getvalue()
