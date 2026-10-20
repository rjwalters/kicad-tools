"""Pin board 05's committed "manufacturable, fully routed" evidence (#6930).

Epic #4410 asked for a BLDC board that regenerates fully routed with zero
shorts and a passing manufacturer check.  Revision B (``redesign/build.py``)
meets that; these guards make a regression in the committed result loud.
``build.py`` itself fails closed (raises, non-zero exit) on any DRC/ERC/LVS or
manufacturer-check finding, so ``design.py`` cannot exit 0 on a bad board.
"""

import json
from pathlib import Path

import pytest

OUTPUT = Path(__file__).resolve().parents[1] / "boards/05-bldc-motor-controller/output"


def _load(relative: str) -> dict:
    path = OUTPUT / relative
    if not path.exists():
        pytest.skip(f"{relative} not committed")
    return json.loads(path.read_text())


def test_native_refill_drc_has_no_findings_or_unconnected_items():
    drc = _load("readiness/native-drc.json")
    assert drc["violations"] == []  # includes shorting_items
    assert drc["unconnected_items"] == []


def test_manufacturer_check_and_meta_gates_pass():
    check = _load("readiness/kct-check.json")
    assert check["summary"]["errors"] == 0
    assert check["summary"]["warnings"] == 0
    for gate in ("drc", "erc", "lvs"):
        assert check["meta_checks"][gate]["status"] == "PASSED", gate


def test_copper_lvs_is_clean_and_not_vacuous():
    lvs = _load("lvs.json")
    assert lvs["clean"] is True
    assert lvs["mismatches"] == []
    assert lvs["copper_mismatches"] == []
    assert lvs["copper_vacuous"] is False
    assert lvs["copper_bound_pad_count"] > 0


def test_readiness_is_ready_with_no_blockers():
    readiness = _load("readiness.json")
    assert readiness["status"] == "ready"
    assert readiness["blockers"] == []
