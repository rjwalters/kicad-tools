"""Positive control for ``board06_determinism_smoke.sh``'s content hash (#5586).

Until this fix, ``compute_content_hash()`` was line-based::

    sed -E 's/\\(uuid "[^"]*"\\)/(uuid "X")/g; ...' "${path}" \\
      | grep -E '^[[:space:]]*\\((segment|via|arc)' \\
      | sort \\
      | md5sum

This repo writes copper as MULTI-LINE s-expressions, so that ``grep`` kept
only the bare ``(segment`` / ``(via`` header lines and discarded every
``(start ...)`` / ``(end ...)`` / ``(width ...)`` / ``(layer ...)`` /
``(net ...)`` child.  After ``sort`` the hashed input was a run of
identical ``(segment`` lines followed by identical ``(via`` lines, so the
hash degenerated to a function of ``segment_count`` and ``via_count``
alone -- two routes placing every trace on a different path, layer or
width hashed EQUAL.

``compute_content_hash()`` now delegates to
``scripts/ci/normalize_copper.py`` (the same helper
``board_route_determinism_smoke.sh`` uses, #5580/#5585), so this test is
the AC positive control mirroring ``tests/test_ci_normalize_copper.py``:
it proves the hash the SHELL GATE actually runs detects a single-
coordinate mutation and stays invariant to emission order, through the
script's own ``--content-hash`` introspection hook (added by #5586) --
not a reimplementation.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE_SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "board06_determinism_smoke.sh"

_SEGMENT = """\t(segment
\t\t(start 12 26)
\t\t(end 14.0135 23.9865)
\t\t(width 0.2)
\t\t(layer "F.Cu")
\t\t(uuid "seg-uuid-1")
\t\t(net 3)
\t)"""

_VIA = """\t(via
\t\t(at 14.0135 23.9865)
\t\t(size 0.6)
\t\t(drill 0.3)
\t\t(layers "F.Cu" "B.Cu")
\t\t(uuid "via-uuid-1")
\t\t(net 3)
\t)"""


def _pcb(*nodes: str) -> str:
    body = "\n".join(nodes)
    return f'(kicad_pcb\n\t(version 20241229)\n\t(generator "kicad-tools")\n{body}\n)\n'


def _write(tmp_path: Path, text: str, name: str) -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


uv_required = pytest.mark.skipif(
    shutil.which("uv") is None, reason="smoke script shells out to `uv run python`"
)


def _run_content_hash(pcb: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SMOKE_SCRIPT_PATH), "--content-hash", str(pcb)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


@uv_required
def test_content_hash_detects_a_moved_trace(tmp_path: Path) -> None:
    pcb = _write(tmp_path, _pcb(_SEGMENT, _VIA), "a.kicad_pcb")
    moved = _write(
        tmp_path,
        _pcb(_SEGMENT, _VIA).replace("(start 12 26)", "(start 12 27)", 1),
        "b.kicad_pcb",
    )

    first = _run_content_hash(pcb)
    second = _run_content_hash(moved)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout.strip() != second.stdout.strip(), (
        "board06_determinism_smoke.sh's content hash is blind to a moved trace"
    )


@uv_required
def test_content_hash_ignores_emission_order(tmp_path: Path) -> None:
    """Board 06's diff-pair pre-pass reorders segments run-to-run harmlessly."""
    pcb = _write(tmp_path, _pcb(_SEGMENT, _VIA), "a.kicad_pcb")
    reordered = _write(tmp_path, _pcb(_VIA, _SEGMENT), "b.kicad_pcb")

    first = _run_content_hash(pcb)
    second = _run_content_hash(reordered)
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == second.stdout.strip()


@uv_required
def test_content_hash_ignores_uuid_and_tstamp_churn(tmp_path: Path) -> None:
    pcb_text = _pcb(_SEGMENT, _VIA)
    rekeyed = pcb_text.replace("seg-uuid-1", "seg-uuid-9").replace("via-uuid-1", "via-uuid-9")
    assert rekeyed != pcb_text

    pcb = _write(tmp_path, pcb_text, "a.kicad_pcb")
    rekeyed_pcb = _write(tmp_path, rekeyed, "b.kicad_pcb")

    first = _run_content_hash(pcb)
    second = _run_content_hash(rekeyed_pcb)
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == second.stdout.strip()


def test_smoke_script_no_longer_greps_copper_header_lines() -> None:
    """Guard the blind spot from silently returning (#5586).

    Matches the live (non-comment) pipeline, not the historical ``grep -E``
    invocation quoted in the surrounding docstring/comments for context.
    """
    text = SMOKE_SCRIPT_PATH.read_text()
    offending = re.compile(r"^\s*\|\s*grep -E '\^\[\[:space:\]\]\*\\\(\(segment", re.MULTILINE)
    assert not offending.search(text)
    assert "scripts/ci/normalize_copper.py" in text


def test_content_hash_usage_error_without_a_path() -> None:
    result = subprocess.run(
        ["bash", str(SMOKE_SCRIPT_PATH), "--content-hash"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 1
    assert "requires a .kicad_pcb path" in result.stderr


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__]))
