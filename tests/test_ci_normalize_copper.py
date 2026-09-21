"""Tests for the routed-copper normalizer used by the determinism gate (#5580).

``scripts/ci/board_route_determinism_smoke.sh`` routes a board N times and
asserts the routed COPPER is byte-identical across runs.  Until #5580 its
``normalize_copper()`` helper was line-based::

    sed -E 's/\\(uuid "[^"]*"\\)/(uuid "X")/g; ...' "$1" \\
      | grep -E '^[[:space:]]*\\((segment|via|arc)' \\
      | sort

This repo writes copper as MULTI-LINE s-expressions, so that ``grep`` kept
only the bare ``(segment`` / ``(via`` header lines and discarded every
``(start ...)`` / ``(end ...)`` / ``(width ...)`` / ``(layer ...)`` /
``(net ...)`` child.  After ``sort`` the "normalized copper" was a run of
identical ``(segment`` lines followed by identical ``(via`` lines, so the
gate's comparison degenerated to ``segment_count == segment_count &&
via_count == via_count``: two routes placing every trace on a different
path, layer or width compared EQUAL.  Measured on ``main`` before the fix,
board 03's routed PCB reduced to 1793 lines holding 3 DISTINCT values
(2026-09-19 at 0c261d41).

``scripts/ci/normalize_copper.py`` replaces it with a paren-balanced
whole-node comparison, and the smoke script delegates to it.  These tests
are the AC #2 positive control: they prove the normalizer the SHELL GATE
actually runs detects a single-coordinate / width / layer / net mutation,
and is still invariant to emission order and to ``uuid`` / ``tstamp``
churn.  Without such a control the blind spot is invisible -- which is how
it survived #3799 and #3894.

Out of scope (covered elsewhere):
    * ``tests/router/test_board_route_determinism.py`` -- the Python-side
      ``_normalize_copper`` reference and the real two-route board runs.
    * ``tests/test_routing_plan_5510.py::_copper_elements`` -- the second
      reference implementation, used by the routing-plan witness.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "normalize_copper.py"
SMOKE_SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "board_route_determinism_smoke.sh"


def _load_helper_module():
    """Import ``scripts/ci/normalize_copper.py`` as a module."""
    spec = importlib.util.spec_from_file_location("ci_normalize_copper", HELPER_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ci_normalize_copper"] = module
    spec.loader.exec_module(module)
    return module


normalize_copper_helper = _load_helper_module()


# ---------------------------------------------------------------------------
# Fixture: a minimal PCB written the way this repo actually writes copper --
# MULTI-LINE nodes.  The whole point of #5580 is that the old line-based
# normalizer saw only the first line of each of these.
# ---------------------------------------------------------------------------

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

_ZONE_WITH_FILL = """\t(zone
\t\t(net 0)
\t\t(layer "B.Cu")
\t\t(filled_polygon
\t\t\t(layer "B.Cu")
\t\t\t(pts (xy 1 1) (xy 2 1) (xy 2 2))
\t\t)
\t)"""

_NON_COPPER = '\t(gr_line (start 0 0) (end 1 1) (layer "Edge.Cuts"))'


def _pcb(*nodes: str) -> str:
    """Wrap *nodes* in a minimal ``(kicad_pcb ...)`` document."""
    body = "\n".join(nodes)
    return f'(kicad_pcb\n\t(version 20241229)\n\t(generator "kicad-tools")\n{body}\n)\n'


def _write(tmp_path: Path, text: str, name: str = "board.kicad_pcb") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def _normalize(text: str) -> list[str]:
    return normalize_copper_helper.normalize_copper(text)


# ---------------------------------------------------------------------------
# AC #1: the normalizer's output CHANGES for a single copper-field mutation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "what"),
    [
        ("(start 12 26)", "(start 12 27)", "start coordinate"),
        ("(end 14.0135 23.9865)", "(end 14.0135 23.9)", "end coordinate"),
        ("(width 0.2)", "(width 0.25)", "trace width"),
        ('(layer "F.Cu")', '(layer "B.Cu")', "layer"),
        ("(net 3)", "(net 4)", "net"),
    ],
)
def test_single_field_mutation_changes_normalized_copper(
    before: str, after: str, what: str
) -> None:
    """A one-field change anywhere in a multi-line node must be detected."""
    pcb = _pcb(_SEGMENT, _VIA)
    mutated = pcb.replace(before, after, 1)
    assert mutated != pcb, f"fixture does not contain {before!r}"
    assert _normalize(pcb) != _normalize(mutated), f"{what} mutation went undetected"


def test_old_line_based_normalizer_was_blind_to_the_same_mutation() -> None:
    """Pin the regression the fix removes: the grep collapsed to COUNTS.

    This is the discriminating-power proof.  The superseded pipeline is
    reproduced in-process (no shell) purely so the assertion can state what
    it could and could not see.
    """
    copper_open = re.compile(r"^\s*\((segment|via|arc)\b")

    def old_normalize(text: str) -> list[str]:
        return sorted(line for line in text.splitlines() if copper_open.match(line))

    pcb = _pcb(_SEGMENT, _VIA)
    moved = pcb.replace("(start 12 26)", "(start 12 27)", 1)

    # The superseded normalizer saw two DISTINCT values (one per element
    # kind) and could not tell the two routes apart.
    assert set(old_normalize(pcb)) == {"\t(segment", "\t(via"}
    assert old_normalize(pcb) == old_normalize(moved)

    # The replacement does.
    assert _normalize(pcb) != _normalize(moved)


# ---------------------------------------------------------------------------
# AC #1 (second half): invariance -- no false positives.
# ---------------------------------------------------------------------------


def test_emission_order_does_not_matter() -> None:
    """Only the SET of copper geometry is compared, not write order."""
    assert _normalize(_pcb(_SEGMENT, _VIA)) == _normalize(_pcb(_VIA, _SEGMENT))


def test_uuid_and_tstamp_churn_does_not_matter() -> None:
    """Per-element identity tokens are stripped, not compared."""
    pcb = _pcb(_SEGMENT, _VIA)
    rekeyed = pcb.replace("seg-uuid-1", "seg-uuid-9").replace("via-uuid-1", "via-uuid-9")
    assert rekeyed != pcb
    assert _normalize(pcb) == _normalize(rekeyed)

    tstamped = pcb.replace('(uuid "seg-uuid-1")', "(tstamp DEADBEEF)")
    assert _normalize(pcb) == _normalize(tstamped)


def test_identical_input_compares_equal() -> None:
    """The trivial control: same bytes in, same normalization out."""
    pcb = _pcb(_SEGMENT, _VIA)
    assert _normalize(pcb) == _normalize(pcb)


def test_multiplicity_is_preserved() -> None:
    """Duplicating an element is a real copper difference, not a no-op."""
    assert _normalize(_pcb(_SEGMENT)) != _normalize(_pcb(_SEGMENT, _SEGMENT))


# ---------------------------------------------------------------------------
# Scope: what the normalizer must keep excluding.
# ---------------------------------------------------------------------------


def test_zone_pour_fills_are_excluded() -> None:
    """#5578: pours are filled after routing and vary independently."""
    with_zone = _normalize(_pcb(_SEGMENT, _ZONE_WITH_FILL))
    without_zone = _normalize(_pcb(_SEGMENT))
    assert with_zone == without_zone
    assert not any("filled_polygon" in record for record in with_zone)


def test_non_copper_nodes_are_excluded() -> None:
    assert _normalize(_pcb(_SEGMENT, _NON_COPPER)) == _normalize(_pcb(_SEGMENT))


def test_nested_copper_is_not_a_top_level_record() -> None:
    """Copper-shaped nodes inside a footprint are not routed copper."""
    nested = _pcb(f'\t(footprint "X"\n{_SEGMENT}\n\t)')
    assert _normalize(nested) == []


def test_arc_nodes_are_captured() -> None:
    arc = (
        "\t(arc\n\t\t(start 1 2)\n\t\t(mid 2 3)\n\t\t(end 3 4)\n\t\t(width 0.2)\n"
        '\t\t(layer "F.Cu")\n\t\t(net 1)\n\t)'
    )
    records = _normalize(_pcb(arc))
    assert len(records) == 1
    assert records[0].startswith("(arc")
    assert _normalize(_pcb(arc)) != _normalize(_pcb(arc.replace("(mid 2 3)", "(mid 2 4)")))


# ---------------------------------------------------------------------------
# Output shape: the shell gate diffs the output LINE BY LINE, so every
# record must occupy exactly one line.
# ---------------------------------------------------------------------------


def test_each_record_is_a_single_line() -> None:
    records = _normalize(_pcb(_SEGMENT, _VIA))
    assert len(records) == 2
    assert all("\n" not in record for record in records)
    assert all(record.startswith("(") and record.endswith(")") for record in records)


def test_records_retain_the_child_fields() -> None:
    """The whole point: children survive normalization."""
    (segment,) = _normalize(_pcb(_SEGMENT))
    for field in ("(start 12 26)", "(end 14.0135 23.9865)", "(width 0.2)", '(layer "F.Cu")'):
        assert field in segment
    assert "uuid" not in segment


# ---------------------------------------------------------------------------
# CLI contract: 0 = normalized, 1 = usage/IO/parse error, 2 = no copper.
# ---------------------------------------------------------------------------


def _run_helper(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER_SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def test_cli_prints_one_sorted_record_per_line(tmp_path: Path) -> None:
    pcb = _write(tmp_path, _pcb(_VIA, _SEGMENT))
    result = _run_helper(str(pcb))
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines == sorted(lines)
    assert lines == _normalize(_pcb(_SEGMENT, _VIA))


def test_cli_rejects_a_missing_file(tmp_path: Path) -> None:
    result = _run_helper(str(tmp_path / "nope.kicad_pcb"))
    assert result.returncode == 1
    assert "not found" in result.stderr.lower()


def test_cli_rejects_unparseable_input(tmp_path: Path) -> None:
    pcb = _write(tmp_path, "(kicad_pcb (segment (start 1 2)")
    result = _run_helper(str(pcb))
    assert result.returncode == 1


def test_cli_fails_when_no_copper_is_found(tmp_path: Path) -> None:
    """A normalization regression that filtered EVERYTHING out must not pass.

    Two empty outputs compare equal, so the gate would report determinism it
    never measured.  Exit 2 makes that loud.
    """
    pcb = _write(tmp_path, _pcb(_NON_COPPER))
    result = _run_helper(str(pcb))
    assert result.returncode == 2
    assert "no routed copper" in result.stderr.lower()


def test_cli_allow_empty_opts_out_of_the_nonempty_guard(tmp_path: Path) -> None:
    pcb = _write(tmp_path, _pcb(_NON_COPPER))
    result = _run_helper("--allow-empty", str(pcb))
    assert result.returncode == 0
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# AC #2: the SHELL GATE's own normalizer -- not just the Python helper --
# detects the mutation.  Exercised through the smoke script's
# ``--normalize-copper`` hook, which runs the exact code path the
# determinism loop runs.
# ---------------------------------------------------------------------------


def _run_smoke_normalizer(pcb: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SMOKE_SCRIPT_PATH), "--normalize-copper", str(pcb)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


uv_required = pytest.mark.skipif(
    shutil.which("uv") is None, reason="smoke script shells out to `uv run python`"
)


@uv_required
def test_smoke_script_normalizer_detects_a_moved_trace(tmp_path: Path) -> None:
    pcb = _write(tmp_path, _pcb(_SEGMENT, _VIA), "a.kicad_pcb")
    moved = _write(
        tmp_path, _pcb(_SEGMENT, _VIA).replace("(start 12 26)", "(start 12 27)", 1), "b.kicad_pcb"
    )

    first = _run_smoke_normalizer(pcb)
    second = _run_smoke_normalizer(moved)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout != second.stdout, (
        "board_route_determinism_smoke.sh's normalizer is blind to a moved trace"
    )


@uv_required
def test_smoke_script_normalizer_ignores_emission_order(tmp_path: Path) -> None:
    pcb = _write(tmp_path, _pcb(_SEGMENT, _VIA), "a.kicad_pcb")
    reordered = _write(tmp_path, _pcb(_VIA, _SEGMENT), "b.kicad_pcb")
    first = _run_smoke_normalizer(pcb)
    second = _run_smoke_normalizer(reordered)
    assert first.returncode == 0, first.stderr
    assert first.stdout == second.stdout


@uv_required
def test_smoke_script_normalizer_matches_the_helper(tmp_path: Path) -> None:
    """The shell gate must run the helper these tests pin, not a copy."""
    pcb = _write(tmp_path, _pcb(_SEGMENT, _VIA))
    result = _run_smoke_normalizer(pcb)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == _normalize(_pcb(_SEGMENT, _VIA))


def test_smoke_script_no_longer_greps_copper_header_lines() -> None:
    """Guard the blind spot from silently returning (#5580)."""
    text = SMOKE_SCRIPT_PATH.read_text()
    offending = re.compile(r"^\s*\|\s*grep -E '\^\[\[:space:\]\]\*\\\(\(segment", re.MULTILINE)
    assert not offending.search(text)
    assert "scripts/ci/normalize_copper.py" in text
