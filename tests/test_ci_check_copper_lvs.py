"""Tests for the independent copper-LVS re-check asserter (issue #3840).

``scripts/ci/check_copper_lvs.py`` is the parser/asserter side of the
out-of-process copper-LVS gate added to the board-03/06/07 ``--lvs-only``
CI jobs.  It reads the JSON emitted by
``python -m kicad_tools.lvs.copper_lvs <sch> <routed_pcb>`` and asserts the
result is clean, mirroring the exit-code + ``::error::`` annotation
convention of the sibling asserters (``check_board_00_e2e.py``,
``check_routed_drc.py``).

These tests exercise the asserter in isolation against synthetic JSON
payloads matching the shape produced by
:func:`kicad_tools.lvs.copper_lvs.result_to_json` -- no KiCad data or
subprocess needed.

Exit-code contract under test:
    0 -- clean result (clean: true).
    1 -- usage/parse error (missing file, bad JSON, missing 'clean' key).
    2 -- dirty result (clean: false) -- the gate caught a regression.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "check_copper_lvs.py"


def _load_helper_module():
    """Import ``scripts/ci/check_copper_lvs.py`` as a module."""
    spec = importlib.util.spec_from_file_location("check_copper_lvs", HELPER_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_copper_lvs"] = module
    spec.loader.exec_module(module)
    return module


check_copper_lvs = _load_helper_module()


def _write_json(tmp_path: Path, payload: object) -> Path:
    p = tmp_path / "copper.json"
    p.write_text(json.dumps(payload))
    return p


# --- clean result -> exit 0 ------------------------------------------------


def test_clean_result_exits_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = _write_json(tmp_path, {"clean": True, "mismatches": []})
    rc = check_copper_lvs.main([str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[ok] copper-LVS clean" in out


def test_clean_result_no_mismatches_key_exits_0(tmp_path: Path) -> None:
    # ``mismatches`` is optional when clean.
    p = _write_json(tmp_path, {"clean": True})
    assert check_copper_lvs.main([str(p)]) == 0


# --- dirty result -> exit 2 ------------------------------------------------


def test_dirty_short_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = _write_json(
        tmp_path,
        {
            "clean": False,
            "mismatches": [
                {
                    "kind": "short",
                    "net_a": "GND",
                    "net_b": "LED_ANODE",
                    "pad_a": "D1.1",
                    "pad_b": "R1.2",
                }
            ],
        },
    )
    rc = check_copper_lvs.main([str(p)])
    assert rc == 2
    out = capsys.readouterr().out
    assert "::error" in out
    assert "clean=false" in out
    assert "1 short" in out


def test_dirty_open_exits_2(tmp_path: Path) -> None:
    p = _write_json(
        tmp_path,
        {
            "clean": False,
            "mismatches": [
                {
                    "kind": "open",
                    "net_a": "VCC",
                    "net_b": "VCC",
                    "pad_a": "U1.7",
                    "pad_b": "C1.1",
                }
            ],
        },
    )
    assert check_copper_lvs.main([str(p)]) == 2


def test_dirty_empty_mismatches_still_exits_2(tmp_path: Path) -> None:
    # ``clean: false`` is authoritative even if mismatches list is empty
    # (the gate must not pass a self-contradictory dirty payload).
    p = _write_json(tmp_path, {"clean": False, "mismatches": []})
    assert check_copper_lvs.main([str(p)]) == 2


# --- malformed / usage -> exit 1 -------------------------------------------


def test_missing_clean_key_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = _write_json(tmp_path, {"mismatches": []})
    rc = check_copper_lvs.main([str(p)])
    assert rc == 1
    assert "missing required 'clean' key" in capsys.readouterr().out


def test_malformed_json_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "copper.json"
    p.write_text("{not valid json")
    rc = check_copper_lvs.main([str(p)])
    assert rc == 1
    assert "could not parse" in capsys.readouterr().out


def test_missing_file_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = check_copper_lvs.main([str(tmp_path / "does-not-exist.json")])
    assert rc == 1
    assert "does not exist" in capsys.readouterr().out


def test_non_object_json_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = _write_json(tmp_path, ["clean", True])
    rc = check_copper_lvs.main([str(p)])
    assert rc == 1
    assert "not a JSON object" in capsys.readouterr().out


# --- stdin support ---------------------------------------------------------


def test_reads_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"clean": True, "mismatches": []})))
    assert check_copper_lvs.main(["-"]) == 0


def test_reads_dirty_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "clean": False,
                    "mismatches": [
                        {
                            "kind": "short",
                            "net_a": "A",
                            "net_b": "B",
                            "pad_a": "U1.1",
                            "pad_b": "U1.2",
                        }
                    ],
                }
            )
        ),
    )
    assert check_copper_lvs.main(["/dev/stdin"]) == 2
