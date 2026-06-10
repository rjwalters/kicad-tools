"""Regression guard: board-05 ``kct export --strict-preflight`` behaviour.

Issue #2901 (umbrella #2746 child 4), acceptance criterion 5 — assert
that ``kct export`` invoked against the committed board-05 routed PCB
runs through the preflight pipeline and reports its results in the
expected JSON shape.

**Important state-of-the-world note.**  Board 05 currently has a known
schematic↔PCB drift residual (issue #2773 added the preflight detection
but did not remediate the source drift on this board): BOM mentions
C18/C19 not on the PCB, PCB has C15/C16 + MH1-MH4 not in the BOM, and
the BOM lacks LCSC part numbers + several footprints.  Until that drift
is fixed at the design.py / project.kct level (out of scope per #2901),
``--strict-preflight`` exits non-zero on this board.

This test therefore guards what is achievable today AND tracks the
future "clean preflight" state:

* The *structural* preflight checks (PCB parseable, schematic
  auto-detected, board outline closed, footprints have names, board
  dimensions within manufacturer limits, drill holes present) MUST all
  report OK -- those are regressions in the preflight pipeline or in
  the routed PCB integrity, both of which #2904 and prior work claim to
  fix.  This is the strict assertion.

* The *clean preflight* assertion (exit 0, no FAIL entries) was
  historically marked ``@pytest.mark.xfail(strict=False)`` for the
  BOM↔PCB drift tracked by #2773 / #2901.  The drift has since been
  fixed and the marker removed (issue #3397), so the clean state is
  now a hard floor.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "05-bldc-motor-controller"
ROUTED_PCB = BOARD_DIR / "output" / "bldc_controller_routed.kicad_pcb"

# Preflight checks that must report OK regardless of BOM drift.  These
# are the "PCB integrity" half of the preflight pipeline -- they have no
# dependency on the schematic↔PCB sync state and a regression here
# indicates either a routed-PCB regression (the zones / outline /
# footprint pipeline broke) or a preflight implementation regression.
STRUCTURAL_CHECKS_REQUIRING_OK = {
    "pcb_file",
    "schematic_file",
    "board_outline",
    "footprints",
    "board_dimensions",
    "drill_holes",
}


@pytest.fixture(scope="module")
def routed_pcb_path() -> Path:
    """Resolve the committed routed PCB or skip if absent."""
    if not ROUTED_PCB.exists():
        pytest.skip(
            f"Board 05 routed PCB not found at {ROUTED_PCB!s}; "
            "regenerate via "
            "`uv run python boards/05-bldc-motor-controller/design.py`"
        )
    return ROUTED_PCB


@pytest.fixture(scope="module")
def export_strict_preflight_result(
    routed_pcb_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict:
    """Run ``kct export --strict-preflight --format json`` and return the JSON.

    Module-scoped so all four assertions below share one subprocess
    invocation (the export run itself is ~2-5 seconds with --skip-drc /
    --skip-erc to avoid kicad-cli; without those flags it can take
    longer).  Returns the parsed JSON dict regardless of exit code so the
    individual tests can inspect both the success/failure state and the
    per-check ``preflight`` array.
    """
    out_dir = tmp_path_factory.mktemp("export_strict_preflight")
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "export",
        str(routed_pcb_path),
        "--strict-preflight",
        # Skip DRC/ERC inside preflight to avoid the kicad-cli dependency;
        # the DRC count regression is covered separately by
        # tests/test_board_05_drc_allowlist.py.
        "--skip-drc",
        "--skip-erc",
        "--output",
        str(out_dir),
        "--format",
        "json",
        # Skip gerbers + project zip to keep this test under ~5s.  The
        # preflight pipeline runs BEFORE these export steps so skipping
        # them does not affect the preflight result we're asserting on.
        "--no-gerbers",
        "--no-project-zip",
        "--no-report",
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    # The JSON output is emitted on stdout regardless of exit code when
    # --format json is set.  Parse it; if parsing fails, surface stderr
    # so a tool-level failure (import error etc.) is diagnosable.
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(
            f"kct export --format json produced unparseable stdout "
            f"(exit {proc.returncode}): {e}\n"
            f"stdout (first 1000 chars):\n{proc.stdout[:1000]}\n"
            f"stderr (last 1000 chars):\n{proc.stderr[-1000:]}"
        )
    # Stash the subprocess exit code in the parsed dict for the test
    # that asserts on it (preserves the single-run optimization above).
    data["__exit_code"] = proc.returncode
    return data


class TestBoard05ExportPreflight:
    """Acceptance criterion 5 of issue #2901."""

    def test_export_json_has_preflight_array(
        self,
        export_strict_preflight_result: dict,
    ) -> None:
        """Sanity check: the export JSON shape includes a preflight array.

        Guards against an upstream contract change to ``kct export
        --format json``.  If this fails, the rest of the AC #5 tests
        will produce confusing errors -- this surfaces the root cause
        first.
        """
        preflight = export_strict_preflight_result.get("preflight")
        assert isinstance(preflight, list), (
            f"Expected 'preflight' array in export JSON; got "
            f"{type(preflight).__name__}.  Keys present: "
            f"{sorted(export_strict_preflight_result.keys())!r}"
        )
        assert len(preflight) > 0, (
            "Preflight array is empty.  Either --strict-preflight was "
            "not honoured, or every preflight check was skipped.  "
            "Inspect the export JSON for the full result."
        )

    def test_structural_preflight_checks_all_ok(
        self,
        export_strict_preflight_result: dict,
    ) -> None:
        """PCB-integrity preflight checks must all report OK.

        Validates the structural half of the preflight pipeline:

        * ``pcb_file``: the routed PCB parses cleanly.
        * ``schematic_file``: the schematic auto-detects.
        * ``board_outline``: Edge.Cuts forms a closed polygon.
        * ``footprints``: every footprint has a library name.
        * ``board_dimensions``: the board fits within manufacturer
          limits.
        * ``drill_holes``: at least one drill hole exists.

        A failure here means either:

        * A real PCB regression (the zone / outline pipeline broke,
          e.g., a write-path bug that dropped Edge.Cuts).
        * A preflight implementation regression (e.g., the
          ``find_schematic`` auto-detection logic stopped working).

        BOM-related checks (``bom_fields``, ``bom_pcb_match``,
        ``bom_cpl_match``) are intentionally EXCLUDED from this strict
        assertion because board 05 has known BOM↔PCB drift tracked by
        #2773 / out-of-scope per #2901.  Those checks are covered by
        :meth:`test_clean_preflight_status_aspirational` below as an
        xfail-tracked aspirational state.
        """
        preflight = export_strict_preflight_result["preflight"]
        results_by_name = {item["name"]: item for item in preflight}

        non_ok: list[str] = []
        for check_name in STRUCTURAL_CHECKS_REQUIRING_OK:
            result = results_by_name.get(check_name)
            if result is None:
                non_ok.append(f"{check_name}: missing from preflight output")
                continue
            if result["status"] != "OK":
                non_ok.append(
                    f"{check_name}: status={result['status']!r} "
                    f"message={result.get('message', '')!r}"
                )

        assert not non_ok, (
            "Board 05 structural preflight checks failed:\n  "
            + "\n  ".join(non_ok)
            + "\n\nThese are PCB-integrity invariants that do NOT depend "
            "on the BOM↔PCB drift residual; a failure here is a real "
            "regression in either the routed PCB pipeline (design.py / "
            "zones / outline) or the preflight implementation."
        )

    # NOTE: this test carried ``@pytest.mark.xfail(strict=False)`` for the
    # historical BOM↔PCB drift (#2773 / #2901).  The drift has since been
    # fixed at the design.py level and the test started xpassing (observed
    # while refreshing board-05 artifacts for issue #3397), so the marker
    # was removed per its own instructions to lock in the clean state as a
    # hard regression guard.
    def test_clean_preflight_status_aspirational(
        self,
        export_strict_preflight_result: dict,
    ) -> None:
        """Aspirational: ``kct export --strict-preflight`` exits 0.

        This is the literal acceptance criterion from issue #2901: every
        preflight check passes (no FAIL entries), and the export command
        returns exit code 0 with ``--strict-preflight``.

        Previously marked ``xfail(strict=False)`` for the historical
        BOM↔PCB drift (#2773 / #2901).  The drift is fixed; this is now
        a hard regression guard (marker removed under issue #3397).
        """
        exit_code = export_strict_preflight_result["__exit_code"]
        preflight = export_strict_preflight_result["preflight"]

        fail_entries = [item for item in preflight if item["status"] == "FAIL"]

        assert exit_code == 0, (
            f"kct export --strict-preflight exited with code {exit_code}; "
            f"expected 0.  Preflight FAIL entries:\n  "
            + "\n  ".join(f"{item['name']}: {item.get('message', '')}" for item in fail_entries)
        )
        assert not fail_entries, "Preflight reported FAIL entries:\n  " + "\n  ".join(
            f"{item['name']}: {item.get('message', '')}" for item in fail_entries
        )
