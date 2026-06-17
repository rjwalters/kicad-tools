#!/usr/bin/env python3
"""Board 00 end-to-end CI gate (issue #3751).

Asserts post-recipe artifact correctness for ``boards/00-simple-led/`` after
the CI workflow has:

1. Run ``boards/00-simple-led/generate_design.py <output_dir>`` against a
   clean output directory.
2. Run ``kct check <routed.kicad_pcb>`` and verified ``Overall: PASSED``.
3. Re-emitted ``board.json`` via ``kct board-metrics <board_dir>`` against
   that output directory in its expected ``<board_dir>/output/`` shape.

This script is the *parser/asserter* side of the gate.  Given the staging
board directory it:

- asserts every expected artifact exists (sch, pcb, routed pcb, lvs.json,
  manufacturing/manifest.json);
- asserts ``output/lvs.json`` has ``clean: true``;
- asserts ``output/board.json`` reports ``status: ok``, ``drc_violations: 0``,
  ``lvs_clean: true``;

and exits non-zero with a ``::error::``-annotated message on any mismatch
so the failure surfaces inline on the GitHub PR Files-changed view.

Sibling of ``scripts/ci/check_routed_drc.py`` -- same exit-code convention,
same annotation pattern, deliberately small so the gate's contract stays
auditable.

Why this lives in a script (vs. inline ``run:`` blocks in ci.yml):

- Each assertion gets a stable, attributable ``::error::`` annotation
  including the file path and the failing field.  Inline shell+python
  heredocs in ci.yml lose that affordance (no file: target) and are harder
  to unit-test.
- The script is invokable locally (``uv run python scripts/ci/check_board_00_e2e.py
  /tmp/board00-staging/00-simple-led``) so contributors can reproduce the CI
  gate exactly without pushing.

Exit codes:
    0 -- All assertions passed.
    1 -- Tool/usage error (missing arg, board_dir doesn't exist, JSON parse
         failure, etc.) -- distinct from "the gate caught a real regression".
    2 -- One or more assertions failed (the gate caught a regression).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Artifacts required to exist under ``<board_dir>/output/``.  Paths are
# slash-joined relative to the output directory.
REQUIRED_ARTIFACTS: tuple[str, ...] = (
    "simple_led.kicad_sch",
    "simple_led.kicad_pcb",
    "simple_led_routed.kicad_pcb",
    "lvs.json",
    "manufacturing/manifest.json",
    "board.json",
)

# board.json fields that must match these exact values for the gate to
# pass.  The values are deliberately hardcoded (not loaded from the file's
# own contents) so a regression that silently switches ``status`` from
# ``"ok"`` to ``"partial"`` is caught here.
REQUIRED_BOARD_JSON_FIELDS: dict[str, Any] = {
    "status": "ok",
    "drc_violations": 0,
    "lvs_clean": True,
}


def _err(msg: str, file: str | Path | None = None) -> None:
    """Emit a GitHub Actions ``::error::`` annotation to stdout.

    The ``file=`` parameter surfaces the failure inline on the PR
    Files-changed view when the path is tracked by git; for tmp paths
    (the staging directory under ``/tmp/``) the annotation still shows
    in the job log with the field name surfaced for triage.
    """
    if file is not None:
        print(f"::error file={file}::{msg}")
    else:
        print(f"::error::{msg}")


def assert_artifacts_exist(output_dir: Path) -> list[str]:
    """Assert every member of REQUIRED_ARTIFACTS exists under ``output_dir``.

    Returns the list of *missing* relative paths (empty list = all present).
    """
    missing: list[str] = []
    for rel in REQUIRED_ARTIFACTS:
        if not (output_dir / rel).is_file():
            missing.append(rel)
    return missing


def assert_lvs_clean(lvs_path: Path) -> str | None:
    """Assert ``lvs.json`` reports ``clean: true``.

    Returns ``None`` on pass, or a human-readable error message on fail.
    """
    try:
        data = json.loads(lvs_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return f"could not read/parse {lvs_path}: {e}"
    if not data.get("clean"):
        mismatches = data.get("mismatches", [])
        return (
            f"lvs.json reports clean=False with {len(mismatches)} mismatch(es); "
            f"first: {mismatches[0] if mismatches else '<none>'}"
        )
    return None


def assert_board_json_fields(board_json_path: Path) -> list[str]:
    """Assert ``board.json`` has the required field values.

    Returns the list of error messages (empty list = all OK).
    """
    errors: list[str] = []
    try:
        data = json.loads(board_json_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return [f"could not read/parse {board_json_path}: {e}"]
    for key, want in REQUIRED_BOARD_JSON_FIELDS.items():
        got = data.get(key)
        if got != want:
            errors.append(f"board.json[{key!r}]={got!r}, expected {want!r}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_board_00_e2e.py",
        description=(
            "Assert post-recipe artifact correctness for board 00. "
            "Pass the board-layout directory (the one containing output/)."
        ),
    )
    parser.add_argument(
        "board_dir",
        type=Path,
        help=(
            "Board-layout directory (must contain output/). "
            "Example: /tmp/board00-staging/00-simple-led"
        ),
    )
    args = parser.parse_args(argv)

    board_dir: Path = args.board_dir
    if not board_dir.is_dir():
        _err(f"board_dir does not exist or is not a directory: {board_dir}")
        return 1

    output_dir = board_dir / "output"
    if not output_dir.is_dir():
        _err(f"output dir does not exist: {output_dir}")
        return 1

    failed = False

    # 1. Artifact presence ----------------------------------------------------
    missing = assert_artifacts_exist(output_dir)
    if missing:
        for rel in missing:
            _err(f"missing required artifact: {rel}", file=output_dir / rel)
        failed = True
    else:
        print(f"[ok] all {len(REQUIRED_ARTIFACTS)} required artifacts present")

    # 2. LVS clean ------------------------------------------------------------
    lvs_path = output_dir / "lvs.json"
    if lvs_path.is_file():
        lvs_err = assert_lvs_clean(lvs_path)
        if lvs_err is not None:
            _err(lvs_err, file=lvs_path)
            failed = True
        else:
            print("[ok] lvs.json clean")
    # (missing lvs.json was already reported above)

    # 3. board.json fields ----------------------------------------------------
    board_json_path = output_dir / "board.json"
    if board_json_path.is_file():
        field_errors = assert_board_json_fields(board_json_path)
        if field_errors:
            for msg in field_errors:
                _err(msg, file=board_json_path)
            failed = True
        else:
            print(
                "[ok] board.json fields: "
                + ", ".join(f"{k}={v!r}" for k, v in REQUIRED_BOARD_JSON_FIELDS.items())
            )

    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
