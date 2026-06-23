"""Tests for the chorus strict-count log parser (issue #3873).

``scripts/ci/parse_chorus_result.py`` extracts the headline connectivity
numbers (strict / partial / unrouted / non-connectivity DRC) from the
"Final chorus report" block printed by ``scripts/route_chorus.py``.  The
chorus M2/M3 measurement workflow runs route_chorus under four flag
variants and feeds each leg's captured stdout to this parser so the
summary job can print a baseline-vs-m2-vs-m3-vs-m2m3 comparison table.

The routing itself is only exercisable on CI-parity hardware with the
private chorus fixture staged; this parser is the locally-testable piece,
validated here against a realistic captured log sample.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "parse_chorus_result.py"


def _load_helper_module():
    """Import ``scripts/ci/parse_chorus_result.py`` as a module."""
    spec = importlib.util.spec_from_file_location("parse_chorus_result", HELPER_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["parse_chorus_result"] = module
    spec.loader.exec_module(module)
    return module


parse_chorus_result = _load_helper_module()


# A realistic capture of the tail of a route_chorus.py run.  The leading
# noise (main-pass + completion-pass chatter) is intentionally present so
# the test proves the parser tolerates arbitrary preceding output and
# pins on the "Final chorus report" lines.  Numbers chosen so that
# strict = 51 - 20 - 0 = 31 (the documented chorus baseline).
SAMPLE_LOG_BASELINE = """\
Main pass: /usr/bin/python -m kicad_tools.cli route ...
[route] detailed routing reached 51/51 nets
Completion pass 1/3: 20 unfinished nets ...
Completion pass 2/3: 20 unfinished nets ...

Pruned 12 stranded copper block(s) from 20 unfinished net(s) (issue #3470 stub hygiene)

============================================================
Final chorus report
============================================================
  Partially-routed signal nets: 20
    - SPI_SCK
    - SPI_MOSI
    - I2C_SDA
  Unrouted signal nets: 0
  Non-connectivity DRC errors: 7

  Routed board: /tmp/chorus_routed_r2.kicad_pcb
  Manufacturable bar: unfinished=20, blocking DRC=7 (target 0/0)
"""

# A variant where some nets are fully unrouted as well as partial.
# strict = 51 - 7 - 4 = 40.
SAMPLE_LOG_M2M3 = """\
Joint region re-solve ENABLED (KCT_JOINT_REGION_RESOLVE=1)
Main pass: ...

Placement nudge ENABLED (issue #3865, M3)
nudged 3 part(s); accepted (+5 strict)

============================================================
Final chorus report
============================================================
  Partially-routed signal nets: 7
    - CLK_A
  Unrouted signal nets: 4
    - AUX_1
    - AUX_2
  Non-connectivity DRC errors: 2

  Routed board: /tmp/chorus_routed_r2.kicad_pcb
  Manufacturable bar: unfinished=11, blocking DRC=2 (target 0/0)
"""


def test_parses_baseline_counts() -> None:
    result = parse_chorus_result.parse_chorus_log(SAMPLE_LOG_BASELINE, variant="baseline")
    assert result.variant == "baseline"
    assert result.total == 51
    assert result.partial == 20
    assert result.unrouted == 0
    assert result.drc_errors == 7
    # strict is the complement: 51 - 20 - 0.
    assert result.strict == 31


def test_parses_m2m3_counts_with_unrouted() -> None:
    result = parse_chorus_result.parse_chorus_log(SAMPLE_LOG_M2M3, variant="m2m3")
    assert result.variant == "m2m3"
    assert result.partial == 7
    assert result.unrouted == 4
    assert result.drc_errors == 2
    assert result.strict == 51 - 7 - 4  # 40


def test_takes_the_last_report_block() -> None:
    """When the report is printed more than once, the final block wins."""
    doubled = SAMPLE_LOG_BASELINE + "\n" + SAMPLE_LOG_M2M3
    result = parse_chorus_result.parse_chorus_log(doubled)
    # Should reflect the M2M3 (last) block, not the baseline one.
    assert result.partial == 7
    assert result.unrouted == 4
    assert result.drc_errors == 2
    assert result.strict == 40


def test_custom_total_changes_strict() -> None:
    result = parse_chorus_result.parse_chorus_log(SAMPLE_LOG_BASELINE, variant="baseline", total=48)
    assert result.total == 48
    assert result.strict == 48 - 20 - 0  # 28


def test_missing_report_raises() -> None:
    with pytest.raises(ValueError, match="Partially-routed signal nets"):
        parse_chorus_result.parse_chorus_log("no report here\njust noise\n")


def test_partial_report_missing_drc_raises() -> None:
    """A run that crashed after the partial line but before DRC must fail loudly."""
    truncated = (
        "Final chorus report\n  Partially-routed signal nets: 20\n  Unrouted signal nets: 0\n"
        # no DRC line
    )
    with pytest.raises(ValueError, match="Non-connectivity DRC errors"):
        parse_chorus_result.parse_chorus_log(truncated)


def test_as_dict_is_json_serializable() -> None:
    result = parse_chorus_result.parse_chorus_log(SAMPLE_LOG_BASELINE, variant="m2")
    payload = json.dumps(result.as_dict())
    restored = json.loads(payload)
    assert restored == {
        "variant": "m2",
        "total": 51,
        "partial": 20,
        "unrouted": 0,
        "strict": 31,
        "drc_errors": 7,
    }


def test_cli_writes_json(tmp_path: Path) -> None:
    log_path = tmp_path / "route.log"
    log_path.write_text(SAMPLE_LOG_BASELINE, encoding="utf-8")
    out_path = tmp_path / "result.json"
    rc = parse_chorus_result.main(
        ["--variant", "baseline", "--log", str(log_path), "--output", str(out_path)]
    )
    assert rc == 0
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["variant"] == "baseline"
    assert data["strict"] == 31
    assert data["drc_errors"] == 7
