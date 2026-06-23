"""Tests for the chorus flag-matrix comparison-table renderer (issue #3873).

``scripts/ci/summarize_chorus_matrix.py`` is the summary-job side of the
chorus M2/M3 measurement workflow: it loads the per-leg
``result_<variant>.json`` files written by
``scripts/ci/parse_chorus_result.py`` and renders a single Markdown
comparison table (baseline vs m2 vs m3 vs m2m3, with strict-count deltas)
to the workflow Step Summary.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "summarize_chorus_matrix.py"


def _load_helper_module():
    spec = importlib.util.spec_from_file_location("summarize_chorus_matrix", HELPER_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["summarize_chorus_matrix"] = module
    spec.loader.exec_module(module)
    return module


summarize_chorus_matrix = _load_helper_module()


def _write_result(
    results_dir: Path,
    variant: str,
    *,
    strict: int,
    partial: int,
    unrouted: int,
    drc_errors: int,
) -> None:
    payload = {
        "variant": variant,
        "total": 51,
        "partial": partial,
        "unrouted": unrouted,
        "strict": strict,
        "drc_errors": drc_errors,
    }
    (results_dir / f"result_{variant}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_renders_full_matrix_with_deltas(tmp_path: Path) -> None:
    _write_result(tmp_path, "baseline", strict=31, partial=20, unrouted=0, drc_errors=7)
    _write_result(tmp_path, "m2", strict=36, partial=15, unrouted=0, drc_errors=5)
    _write_result(tmp_path, "m3", strict=34, partial=14, unrouted=3, drc_errors=4)
    _write_result(tmp_path, "m2m3", strict=40, partial=11, unrouted=0, drc_errors=2)

    results = summarize_chorus_matrix.load_results(tmp_path)
    table = summarize_chorus_matrix.render_table(results)

    # All four variants present, in canonical order.
    assert table.index("| baseline ") < table.index("| m2 ")
    assert table.index("| m2 ") < table.index("| m3 ")
    assert table.index("| m3 ") < table.index("| m2m3 ")

    # Deltas computed vs baseline strict (31).
    assert "| +5 |" in table  # m2: 36 - 31
    assert "| +3 |" in table  # m3: 34 - 31
    assert "| +9 |" in table  # m2m3: 40 - 31
    # Baseline row shows a dash, not a delta.
    assert "| baseline " in table


def test_negative_delta_rendered(tmp_path: Path) -> None:
    _write_result(tmp_path, "baseline", strict=31, partial=20, unrouted=0, drc_errors=7)
    _write_result(tmp_path, "m2", strict=28, partial=23, unrouted=0, drc_errors=9)
    results = summarize_chorus_matrix.load_results(tmp_path)
    table = summarize_chorus_matrix.render_table(results)
    assert "| -3 |" in table


def test_empty_results_dir_renders_note(tmp_path: Path) -> None:
    results = summarize_chorus_matrix.load_results(tmp_path)
    assert results == {}
    table = summarize_chorus_matrix.render_table(results)
    assert "No leg results found" in table


def test_missing_baseline_yields_na_deltas(tmp_path: Path) -> None:
    # Only m2 present (baseline leg crashed / produced no JSON).
    _write_result(tmp_path, "m2", strict=36, partial=15, unrouted=0, drc_errors=5)
    results = summarize_chorus_matrix.load_results(tmp_path)
    table = summarize_chorus_matrix.render_table(results)
    assert "| n/a |" in table


def test_main_writes_to_stdout(tmp_path: Path, capsys) -> None:
    _write_result(tmp_path, "baseline", strict=31, partial=20, unrouted=0, drc_errors=7)
    rc = summarize_chorus_matrix.main(["--results-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Chorus M2/M3 flag-matrix results" in out
    assert "| baseline " in out
