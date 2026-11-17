"""Tests for the plane-net / floating-pad completion CI gate (issue #4531).

``scripts/ci/check_net_status.py`` loads a freshly-routed PCB and asserts its
RAW ``NetStatusResult.total_unconnected_pads`` is within ``--max-unconnected``
(default 0), tolerating an explicit ``--known-open-nets`` list (board 07's 5
seed-invariant #3438 opens).

The critical property under test is the ``is_advisory_incomplete``
NAME-PATTERN pitfall documented in the script: a net literally named ``VCC``
(or ``GND``/``+3V3``/...) with a floating pad is classified
``is_advisory_incomplete=True`` and DROPPED from
``blocking_incomplete_count`` -- the exact silent-pass the #4531 repro hit.
``test_gate_fails_on_floating_power_named_pad`` builds a real
``NetStatusResult`` for such a net and proves this gate (which uses the raw
count) fails on it even while ``blocking_incomplete_count`` is 0, so this bug
class cannot silently regress through the new gate itself.

Threshold/exit-code logic is exercised against a synthetic ``per_net``
(``analyze_unconnected`` monkeypatched) so the comparison / exit-code
behaviour is verified without needing a real broken route.

The script is loaded via importlib (it lives under ``scripts/ci/`` outside the
installed package), mirroring ``tests/test_check_board_05_blocking.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "check_net_status.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("check_net_status", HELPER_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_net_status"] = module
    spec.loader.exec_module(module)
    return module


def _write_pcb(tmp_path: Path) -> Path:
    pcb = tmp_path / "board_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    return pcb


# ---------------------------------------------------------------------------
# Threshold / exit-code logic (analyze_unconnected monkeypatched)
# ---------------------------------------------------------------------------


def test_default_threshold_is_zero() -> None:
    helper = _load_helper()
    assert helper.DEFAULT_MAX_UNCONNECTED == 0


def test_passes_on_fully_connected_board(tmp_path, monkeypatch) -> None:
    helper = _load_helper()
    pcb = _write_pcb(tmp_path)
    monkeypatch.setattr(helper, "analyze_unconnected", lambda _p: (0, []))
    exit_code, message = helper.check_pcb(pcb, max_unconnected=0)
    assert exit_code == 0
    assert "0 unconnected pad(s)" in message


def test_fails_on_single_floating_pad(tmp_path, monkeypatch) -> None:
    helper = _load_helper()
    pcb = _write_pcb(tmp_path)
    monkeypatch.setattr(
        helper,
        "analyze_unconnected",
        lambda _p: (1, [("VCC", ["J2.1 @ (2.50, 35.00)"])]),
    )
    exit_code, message = helper.check_pcb(pcb, max_unconnected=0)
    assert exit_code == 2
    assert "regression" in message.lower()
    assert "VCC" in message


def test_max_unconnected_tolerance_allows_up_to_bound(tmp_path, monkeypatch) -> None:
    helper = _load_helper()
    pcb = _write_pcb(tmp_path)
    monkeypatch.setattr(
        helper,
        "analyze_unconnected",
        lambda _p: (2, [("NETA", ["U1.1 @ (0.00, 0.00)", "U2.1 @ (1.00, 1.00)"])]),
    )
    # exactly at the bound -> pass
    assert helper.check_pcb(pcb, max_unconnected=2)[0] == 0
    # one below the bound -> fail
    assert helper.check_pcb(pcb, max_unconnected=1)[0] == 2


def test_missing_file_is_tool_error(tmp_path) -> None:
    helper = _load_helper()
    missing = tmp_path / "does_not_exist.kicad_pcb"
    exit_code, message = helper.check_pcb(missing, max_unconnected=0)
    assert exit_code == 1
    assert "not found" in message.lower()


# ---------------------------------------------------------------------------
# --known-open-nets (board 07)
# ---------------------------------------------------------------------------


def test_known_open_nets_are_tolerated(tmp_path, monkeypatch) -> None:
    helper = _load_helper()
    pcb = _write_pcb(tmp_path)
    monkeypatch.setattr(
        helper,
        "analyze_unconnected",
        lambda _p: (
            2,
            [
                ("DQ3", ["U1.5 @ (0.00, 0.00)"]),
                ("TMDS_D0_N", ["U2.9 @ (1.00, 1.00)"]),
            ],
        ),
    )
    exit_code, message = helper.check_pcb(
        pcb,
        max_unconnected=0,
        known_open_nets={"DQ3", "DQ4", "MIPI_DAT0_N", "TMDS_D0_N", "TMDS_D1_N"},
    )
    assert exit_code == 0
    assert "0 unconnected pad(s) on gated nets" in message


def test_new_open_on_different_net_still_fails_with_known_opens(tmp_path, monkeypatch) -> None:
    """A *different* net going open is caught even when known-opens are set."""
    helper = _load_helper()
    pcb = _write_pcb(tmp_path)
    monkeypatch.setattr(
        helper,
        "analyze_unconnected",
        lambda _p: (
            2,
            [
                ("DQ3", ["U1.5 @ (0.00, 0.00)"]),  # tolerated
                ("SOME_SIGNAL", ["U3.2 @ (5.00, 5.00)"]),  # NEW open -> must fail
            ],
        ),
    )
    exit_code, message = helper.check_pcb(
        pcb,
        max_unconnected=0,
        known_open_nets={"DQ3", "DQ4", "MIPI_DAT0_N", "TMDS_D0_N", "TMDS_D1_N"},
    )
    assert exit_code == 2
    assert "SOME_SIGNAL" in message
    assert "DQ3" not in message.split("exceeds")[0]  # tolerated net not blamed


def test_known_open_no_longer_open_passes_with_note(tmp_path, monkeypatch, capsys) -> None:
    """A known-open net that routed clean is an improvement -> pass with a note."""
    helper = _load_helper()
    pcb = _write_pcb(tmp_path)
    monkeypatch.setattr(helper, "analyze_unconnected", lambda _p: (0, []))
    exit_code, message = helper.check_pcb(pcb, max_unconnected=0, known_open_nets={"DQ3"})
    assert exit_code == 0
    assert "no longer" in message.lower()


# ---------------------------------------------------------------------------
# Regression: the is_advisory_incomplete name-pattern pitfall (#4531 core)
# ---------------------------------------------------------------------------


def _install_fake_analyzer(monkeypatch, result) -> None:
    """Replace ``NetStatusAnalyzer`` so ``analyze()`` returns ``result``.

    The gate imports the class lazily from ``kicad_tools.analysis.net_status``
    inside ``analyze_unconnected``, so patching the module attribute reaches
    the real ``analyze_unconnected`` + ``check_pcb`` code path over a
    synthetic ``NetStatusResult``.
    """
    from kicad_tools.analysis import net_status as ns

    class _FakeAnalyzer:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def analyze(self):
            return result

    monkeypatch.setattr(ns, "NetStatusAnalyzer", _FakeAnalyzer)


def _floating_power_net_result():
    """Build a ``NetStatusResult`` with a ``VCC`` net that has a floating pad.

    Mirrors the #4531 repro: J2.1 (VCC) unconnected, one other VCC pad
    connected -> status 'incomplete', net_type 'power', hence
    ``is_advisory_incomplete=True`` and dropped from
    ``blocking_incomplete_count``.
    """
    from kicad_tools.analysis.net_status import NetStatus, NetStatusResult, PadInfo

    vcc = NetStatus(
        net_number=1,
        net_name="VCC",
        total_pads=2,
        connected_pads=[PadInfo("U1", "1", (10.0, 10.0), is_connected=True, layers=["F.Cu"])],
        unconnected_pads=[PadInfo("J2", "1", (2.5, 35.0), is_connected=False, layers=["F.Cu"])],
    )
    result = NetStatusResult(nets=[vcc], total_nets=1)
    return result


def test_advisory_classifier_confirms_the_pitfall() -> None:
    """Sanity-check the pitfall exists: VCC floating pad is advisory-filtered."""
    result = _floating_power_net_result()
    vcc = result.nets[0]
    assert vcc.status == "incomplete"
    assert vcc.net_type == "power"
    assert vcc.is_advisory_incomplete is True
    # The trap: the "blocking" count the naive gate would use is ZERO ...
    assert result.blocking_incomplete_count == 0
    # ... while the RAW pad-centric count this gate uses is 1.
    assert result.total_unconnected_pads == 1


def test_gate_fails_on_floating_power_named_pad(tmp_path, monkeypatch) -> None:
    """The core #4531 regression: a floating VCC pad MUST fail this gate.

    Uses the real ``analyze_unconnected`` + ``NetStatusResult`` (only the
    analyzer's PCB load is faked), so this proves the gate reads the raw
    ``total_unconnected_pads`` and does NOT fall into the
    ``blocking_incomplete_count`` name-exclusion trap.
    """
    helper = _load_helper()
    pcb = _write_pcb(tmp_path)
    _install_fake_analyzer(monkeypatch, _floating_power_net_result())

    exit_code, message = helper.check_pcb(pcb, max_unconnected=0)
    assert exit_code == 2
    assert "VCC" in message


# ---------------------------------------------------------------------------
# CLI / main() wiring
# ---------------------------------------------------------------------------


def test_main_prints_measured_count_on_pass(tmp_path, monkeypatch, capsys) -> None:
    helper = _load_helper()
    pcb = _write_pcb(tmp_path)
    monkeypatch.setattr(helper, "analyze_unconnected", lambda _p: (0, []))
    assert helper.main([str(pcb)]) == 0
    out = capsys.readouterr().out
    assert "MEASURED total_unconnected_pads = 0" in out


def test_main_prints_measured_count_on_regression(tmp_path, monkeypatch, capsys) -> None:
    helper = _load_helper()
    pcb = _write_pcb(tmp_path)
    monkeypatch.setattr(
        helper,
        "analyze_unconnected",
        lambda _p: (1, [("VCC", ["J2.1 @ (2.50, 35.00)"])]),
    )
    assert helper.main([str(pcb)]) == 2
    out = capsys.readouterr().out
    assert "MEASURED total_unconnected_pads = 1" in out
    assert "J2.1" in out


def test_main_parses_known_open_nets_arg(tmp_path, monkeypatch) -> None:
    helper = _load_helper()
    pcb = _write_pcb(tmp_path)
    monkeypatch.setattr(
        helper,
        "analyze_unconnected",
        lambda _p: (1, [("DQ3", ["U1.5 @ (0.00, 0.00)"])]),
    )
    # without known-opens -> the DQ3 open fails
    assert helper.main([str(pcb)]) == 2
    # with DQ3 tolerated -> passes
    assert helper.main([str(pcb), "--known-open-nets", "DQ3,DQ4"]) == 0


def test_main_rejects_negative_threshold(tmp_path) -> None:
    helper = _load_helper()
    pcb = _write_pcb(tmp_path)
    assert helper.main([str(pcb), "--max-unconnected", "-1"]) == 1


def test_main_rejects_empty_known_open_nets(tmp_path, monkeypatch) -> None:
    helper = _load_helper()
    pcb = _write_pcb(tmp_path)
    monkeypatch.setattr(helper, "analyze_unconnected", lambda _p: (0, []))
    assert helper.main([str(pcb), "--known-open-nets", " , "]) == 1


def test_help_exits_zero() -> None:
    helper = _load_helper()
    with pytest.raises(SystemExit) as excinfo:
        helper.main(["--help"])
    assert excinfo.value.code == 0
