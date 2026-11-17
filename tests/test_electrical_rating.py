"""Tests for the advisory electrical-rating analyzer (issue #4381).

Covers LED overcurrent, capacitor voltage derating, rail-voltage inference, the
skip/census contract (missing field, unknown rail, malformed value), and the
`kct analyze electrical-rating` CLI (text + json, exit codes).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.analysis import (
    ElectricalRatingAnalyzer,
    ElectricalRatingResult,
    infer_rail_voltage,
)
from kicad_tools.cli.analyze_cmd import main as analyze_main

FIXTURES = Path(__file__).parent / "fixtures" / "electrical_rating"
LEDS = FIXTURES / "leds.kicad_sch"
CAPS = FIXTURES / "caps.kicad_sch"


def _by_ref(results: list[ElectricalRatingResult]) -> dict[str, ElectricalRatingResult]:
    return {r.reference: r for r in results}


# ---------------------------------------------------------------------------
# Rail-voltage inference
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("+3.3V", 3.3),
        ("3V3", 3.3),
        ("+3V3", 3.3),
        ("1V8", 1.8),
        ("+5V", 5.0),
        ("+5VA", 5.0),  # analog rail
        ("+12V", 12.0),
        ("12V0", 12.0),
        ("VBUS", 5.0),  # USB convention
        ("-12V", 12.0),  # magnitude
        ("/+5V", 5.0),  # leading slash stripped
        ("VCC", None),
        ("VBAT", None),
        ("+VIN", None),
        ("GND", None),
        ("GNDA", None),
        ("Net-(R1-2)", None),  # auto-generated net name
        ("", None),
        (None, None),
    ],
)
def test_infer_rail_voltage(name, expected):
    assert infer_rail_voltage(name) == expected


# ---------------------------------------------------------------------------
# LED overcurrent
# ---------------------------------------------------------------------------
def test_led_overcurrent_fail_and_pass():
    results = _by_ref(ElectricalRatingAnalyzer().analyze(LEDS))

    # D1: +5V, Vf=2.0V, R=100 -> I=(5-2)/100=30mA > 20mA -> FAIL
    d1 = results["D1"]
    assert d1.check == "led_overcurrent"
    assert d1.status == "FAIL"
    assert d1.rail_net == "+5V"
    assert d1.rail_voltage_v == 5.0
    assert d1.r_series_ohms == 100.0
    assert d1.series_ref == "R1"
    assert d1.current_a == pytest.approx(0.03)
    assert d1.if_max_a == pytest.approx(0.02)

    # D2: R=220 -> I=13.6mA < 20mA -> PASS
    d2 = results["D2"]
    assert d2.status == "PASS"
    assert d2.current_a == pytest.approx((5.0 - 2.0) / 220.0)


def test_led_skip_missing_if_max():
    d3 = _by_ref(ElectricalRatingAnalyzer().analyze(LEDS))["D3"]
    assert d3.status == "SKIP"
    assert d3.reason == "no If_max field"
    # A skip is never a fail.
    assert d3.status != "FAIL"


def test_led_skip_unparseable_vf():
    d4 = _by_ref(ElectricalRatingAnalyzer().analyze(LEDS))["D4"]
    assert d4.status == "SKIP"
    assert "Vf" in (d4.reason or "")


def test_led_default_vf_flag_surfaces_assumption():
    # With a default Vf, D3 still skips (it has no If_max), but a part that has
    # If_max and no Vf would use the default. D4 has an unparseable Vf, so the
    # default does NOT apply (parse failure is a skip, not a missing field).
    results = _by_ref(ElectricalRatingAnalyzer(led_default_vf=2.0).analyze(LEDS))
    assert results["D3"].status == "SKIP"  # no If_max regardless of default
    assert results["D4"].status == "SKIP"  # unparseable Vf, default not used


# ---------------------------------------------------------------------------
# Capacitor voltage derating
# ---------------------------------------------------------------------------
def test_cap_derating_fail_and_pass():
    results = _by_ref(ElectricalRatingAnalyzer().analyze(CAPS))

    # C1: +12V, rated 16V, margin 0.2 -> need >=14.4V, 16>=14.4 -> PASS
    c1 = results["C1"]
    assert c1.check == "cap_derating"
    assert c1.status == "PASS"
    assert c1.rail_voltage_v == 12.0
    assert c1.rated_voltage_v == 16.0
    assert c1.required_voltage_v == pytest.approx(14.4)

    # C2: rated 10V < 14.4 -> FAIL
    c2 = results["C2"]
    assert c2.status == "FAIL"
    assert c2.rated_voltage_v == 10.0


def test_cap_skip_unknown_rail():
    c3 = _by_ref(ElectricalRatingAnalyzer().analyze(CAPS))["C3"]
    assert c3.status == "SKIP"
    assert c3.reason == "unknown rail voltage"


def test_cap_skip_unparseable_rating():
    c4 = _by_ref(ElectricalRatingAnalyzer().analyze(CAPS))["C4"]
    assert c4.status == "SKIP"
    assert "Voltage_Rating" in (c4.reason or "")


def test_cap_derate_margin_parameter():
    # A tighter margin makes the 16V cap on the 12V rail insufficient:
    # need >= 12 * (1 + 0.4) = 16.8 > 16 -> FAIL.
    results = _by_ref(ElectricalRatingAnalyzer(derate_margin=0.4).analyze(CAPS))
    assert results["C1"].status == "FAIL"
    assert results["C1"].required_voltage_v == pytest.approx(16.8)


# ---------------------------------------------------------------------------
# Census / advisory-robustness contract
# ---------------------------------------------------------------------------
def test_census_counts_skips_visibly():
    results = ElectricalRatingAnalyzer().analyze(LEDS)
    checked = [r for r in results if r.status in ("PASS", "FAIL")]
    skipped = [r for r in results if r.status == "SKIP"]
    assert len(checked) == 2  # D1, D2
    assert len(skipped) == 2  # D3, D4
    # No candidate is ever silently dropped.
    assert len(results) == 4


def test_analyzer_never_raises_on_missing_file():
    # Advisory contract: bad input degrades to an empty census, never raises.
    assert ElectricalRatingAnalyzer().analyze(FIXTURES / "does_not_exist.kicad_sch") == []


def test_analyzer_never_raises_on_garbage(tmp_path):
    junk = tmp_path / "junk.kicad_sch"
    junk.write_text("this is not a schematic")
    assert ElectricalRatingAnalyzer().analyze(junk) == []


def test_result_to_dict_serializable():
    import json

    results = ElectricalRatingAnalyzer().analyze(CAPS)
    for r in results:
        d = r.to_dict()
        json.dumps(d)  # must be JSON-serializable
        assert d["reference"] == r.reference
        assert d["status"] in ("PASS", "FAIL", "SKIP")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_text_fail_exit_code(capsys):
    rc = analyze_main(["electrical-rating", str(LEDS)])
    out = capsys.readouterr().out
    assert rc == 1  # a FAIL gates
    assert "D1" in out
    assert "FAIL" in out
    assert "skipped=2" in out


def test_cli_json_summary(capsys):
    import json

    rc = analyze_main(["electrical-rating", str(CAPS), "--format", "json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["fail"] == 1
    assert payload["summary"]["skipped"] == 2
    assert payload["summary"]["checked"] == 2
    assert payload["parameters"]["derate_margin"] == 0.2


def test_cli_missing_file(capsys):
    rc = analyze_main(["electrical-rating", str(FIXTURES / "nope.kicad_sch")])
    assert rc == 1
    assert "File not found" in capsys.readouterr().err


def test_cli_bad_suffix(capsys, tmp_path):
    bad = tmp_path / "board.kicad_pcb"
    bad.write_text("")
    rc = analyze_main(["electrical-rating", str(bad)])
    assert rc == 1
    assert "kicad_sch" in capsys.readouterr().err


def test_cli_via_kct_dispatch(capsys):
    # Exercise the real parser -> commands.analyze dispatch path.
    from kicad_tools.cli.commands.analyze import run_analyze_command
    from kicad_tools.cli.parser import create_parser

    parser = create_parser()
    args = parser.parse_args(["analyze", "electrical-rating", str(CAPS), "--format", "json"])
    rc = run_analyze_command(args)
    assert rc == 1
    assert '"cap_derating"' in capsys.readouterr().out
