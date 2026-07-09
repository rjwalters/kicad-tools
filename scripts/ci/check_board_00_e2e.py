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

``--lvs-only`` mode (issue #3779) restricts the gate to artifact presence
+ ``lvs.json clean: true`` and **skips** the ``board.json``
``status``/``drc_violations`` assertions.  This is for boards with
**allowlisted DRC residuals** in ``.github/routed-drc-tolerance.yml``
that route PARTIAL by design, so their ``board.json`` is
``status: "partial"`` / ``drc_violations > 0`` and the full
``status: ok`` / ``drc_violations: 0`` assertion would (incorrectly)
fail.  ``board.json[lvs_clean]`` is still asserted when ``board.json`` is
present, since copper-LVS clean is the whole point of those jobs.

``--lvs-not-run`` mode (#4006, board 06): for PCB-first fixture boards
whose schematic is deliberately unwired, LVS is *unavailable* — the
copper comparator binds zero pins and any verdict would be zero-evidence
(the vacuity hole found in PR #4005's review).  Board 06's recipe skips
the LVS step entirely, so this mode asserts the honest state: all
artifacts EXCEPT ``lvs.json`` present, ``lvs.json`` ABSENT, and
``board.json`` carrying NO ``lvs_clean`` key (the site then renders
"LVS not run" instead of a zero-evidence "Ready" badge).

``--lvs-vacuous`` mode (#4006, board 07): same unwired-fixture situation,
but for a recipe that still *emits* ``lvs.json`` (its CI job asserts the
file exists).  Asserts ``lvs.json`` is present and carries the
vacuity-guard verdict (``clean: false`` + ``copper_vacuous: true``), and
that ``board.json`` does NOT claim ``lvs_clean`` (board-metrics treats a
vacuous report as LVS-not-run).  A ``clean: true`` here means the guard
regressed; a non-vacuous dirty result means the schematic gained wired
nets and the job should graduate to ``--lvs-only``.

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

# Artifacts always required under ``<board_dir>/output/`` regardless of the
# board's name.  The board-specific schematic/PCB artifacts are derived from
# ``--stem`` (defaulting to board 00's ``simple_led``) so this asserter works
# for any board, not just board 00 (issue #3762).
BASE_REQUIRED_ARTIFACTS: tuple[str, ...] = (
    "lvs.json",
    "manufacturing/manifest.json",
    "board.json",
)

# Default board stem (board 00) so the historical board-00 invocation that
# passes no ``--stem`` keeps asserting the same artifact set.
DEFAULT_STEM = "simple_led"


def required_artifacts(stem: str) -> tuple[str, ...]:
    """Return the full required-artifact list for a board with this stem.

    ``stem`` is the board's file prefix (e.g. ``simple_led`` for board 00,
    ``voltage_divider`` for board 01).  The schematic, unrouted PCB and
    routed PCB names are derived from it; the base artifacts (lvs.json,
    manifest, board.json) are board-independent.
    """
    return (
        f"{stem}.kicad_sch",
        f"{stem}.kicad_pcb",
        f"{stem}_routed.kicad_pcb",
        *BASE_REQUIRED_ARTIFACTS,
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

# In ``--lvs-only`` mode (boards 06/07) we still assert the board is
# copper-LVS clean, but NOT ``status``/``drc_violations`` (those boards
# carry allowlisted DRC residuals and route PARTIAL by design).
LVS_ONLY_BOARD_JSON_FIELDS: dict[str, Any] = {
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


def assert_artifacts_exist(output_dir: Path, artifacts: tuple[str, ...]) -> list[str]:
    """Assert every member of ``artifacts`` exists under ``output_dir``.

    Returns the list of *missing* relative paths (empty list = all present).
    """
    missing: list[str] = []
    for rel in artifacts:
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
    if data.get("copper_vacuous") is True or data.get("copper_bound_pad_count") == 0:
        # Belt-and-braces (#4006): a clean=true report claiming zero bound
        # pins is exactly the zero-evidence artifact the vacuity guard
        # forbids; never accept it as clean.
        return (
            "lvs.json reports clean=true but is VACUOUS "
            "(copper_vacuous/copper_bound_pad_count=0, #4006) -- zero-evidence "
            "clean is not clean"
        )
    return None


def assert_lvs_vacuous(lvs_path: Path) -> str | None:
    """Assert ``lvs.json`` carries the vacuity-guard verdict (#4006).

    Expected shape for an unwired fixture schematic:
    ``clean: false`` AND ``copper_vacuous: true``.

    Returns ``None`` on pass, or a human-readable error message on fail.
    """
    try:
        data = json.loads(lvs_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return f"could not read/parse {lvs_path}: {e}"
    if data.get("clean"):
        return (
            "expected a VACUOUS lvs.json (clean=false, copper_vacuous=true, "
            "#4006) but it reports clean=true -- either the vacuity guard "
            "regressed or the schematic is now wired (graduate this job to "
            "--lvs-only)"
        )
    if data.get("copper_vacuous") is not True:
        return (
            "expected a VACUOUS lvs.json (copper_vacuous=true, #4006) but got "
            "a non-vacuous dirty report -- the schematic appears to bind pins "
            "now; graduate this job to --lvs-only and fix the real mismatches"
        )
    return None


def assert_board_json_fields(
    board_json_path: Path,
    required_fields: dict[str, Any] = REQUIRED_BOARD_JSON_FIELDS,
) -> list[str]:
    """Assert ``board.json`` has the required field values.

    ``required_fields`` defaults to the full set (``status``,
    ``drc_violations``, ``lvs_clean``); ``--lvs-only`` mode passes
    :data:`LVS_ONLY_BOARD_JSON_FIELDS` (just ``lvs_clean``) so boards with
    allowlisted DRC residuals are not failed on ``status``/``drc_violations``.

    Returns the list of error messages (empty list = all OK).
    """
    errors: list[str] = []
    try:
        data = json.loads(board_json_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return [f"could not read/parse {board_json_path}: {e}"]
    for key, want in required_fields.items():
        got = data.get(key)
        if got != want:
            errors.append(f"board.json[{key!r}]={got!r}, expected {want!r}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_board_00_e2e.py",
        description=(
            "Assert post-recipe artifact correctness for a demo board. "
            "Pass the board-layout directory (the one containing output/). "
            "Defaults to board 00's artifact names; override with --stem."
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
    parser.add_argument(
        "--stem",
        default=DEFAULT_STEM,
        help=(
            "Board file stem used to derive the schematic/PCB artifact names "
            f"(default: {DEFAULT_STEM!r} for board 00; e.g. 'voltage_divider' "
            "for board 01)."
        ),
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--lvs-only",
        action="store_true",
        help=(
            "Restrict the gate to artifact presence + lvs.json clean=true "
            "(plus board.json lvs_clean=true when present), and SKIP the "
            "board.json status=ok / drc_violations=0 assertions.  Use for "
            "boards with genuinely wired schematics that carry allowlisted "
            "DRC residuals (status='partial')."
        ),
    )
    mode_group.add_argument(
        "--lvs-not-run",
        action="store_true",
        help=(
            "Honest-state gate for an unwired fixture schematic whose recipe "
            "SKIPS the LVS step (#4006, board 06): assert lvs.json is ABSENT "
            "and board.json carries NO lvs_clean key ('LVS not run'), and "
            "SKIP the status/drc_violations assertions."
        ),
    )
    mode_group.add_argument(
        "--lvs-vacuous",
        action="store_true",
        help=(
            "Honest-state gate for an unwired fixture schematic whose recipe "
            "still EMITS lvs.json (#4006, board 07): assert lvs.json is "
            "present with the vacuity-guard verdict (clean=false, "
            "copper_vacuous=true) and board.json carries NO lvs_clean key, "
            "and SKIP the status/drc_violations assertions."
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

    artifacts = required_artifacts(args.stem)
    if args.lvs_not_run:
        # The recipe deliberately emits NO lvs.json (#4006); its absence is
        # asserted below instead of its presence here.
        artifacts = tuple(a for a in artifacts if a != "lvs.json")
    failed = False

    # 1. Artifact presence ----------------------------------------------------
    missing = assert_artifacts_exist(output_dir, artifacts)
    if missing:
        for rel in missing:
            _err(f"missing required artifact: {rel}", file=output_dir / rel)
        failed = True
    else:
        print(f"[ok] all {len(artifacts)} required artifacts present")

    # 2. LVS verdict -----------------------------------------------------------
    lvs_path = output_dir / "lvs.json"
    if args.lvs_not_run:
        if lvs_path.is_file():
            _err(
                "lvs.json must be ABSENT in --lvs-not-run mode (#4006): this "
                "board's schematic is unwired, so any emitted LVS verdict is "
                "zero-evidence.  If the schematic is now wired, graduate this "
                "job to --lvs-only.",
                file=lvs_path,
            )
            failed = True
        else:
            print("[ok] lvs.json absent (LVS not run -- unwired fixture schematic)")
    elif args.lvs_vacuous:
        if lvs_path.is_file():
            lvs_err = assert_lvs_vacuous(lvs_path)
            if lvs_err is not None:
                _err(lvs_err, file=lvs_path)
                failed = True
            else:
                print("[ok] lvs.json carries the vacuity-guard verdict (#4006)")
        # (missing lvs.json was already reported above)
    elif lvs_path.is_file():
        lvs_err = assert_lvs_clean(lvs_path)
        if lvs_err is not None:
            _err(lvs_err, file=lvs_path)
            failed = True
        else:
            print("[ok] lvs.json clean")
    # (missing lvs.json was already reported above)

    # 3. board.json fields ----------------------------------------------------
    # In --lvs-only mode assert only lvs_clean (allowlisted-DRC boards are
    # status='partial' / drc_violations>0, which the full field set would
    # incorrectly reject).  In the unwired-fixture modes (#4006) assert
    # lvs_clean is ABSENT: board-metrics omits it when LVS was not run / the
    # report is vacuous, and its presence would mean a zero-evidence badge.
    board_json_path = output_dir / "board.json"
    if args.lvs_not_run or args.lvs_vacuous:
        if board_json_path.is_file():
            try:
                board_data = json.loads(board_json_path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                _err(f"could not read/parse {board_json_path}: {e}")
                failed = True
            else:
                if "lvs_clean" in board_data:
                    _err(
                        "board.json must NOT carry lvs_clean for an unwired "
                        f"fixture board (#4006); got lvs_clean="
                        f"{board_data['lvs_clean']!r}",
                        file=board_json_path,
                    )
                    failed = True
                else:
                    print("[ok] board.json omits lvs_clean ('LVS not run')")
    else:
        required_fields = (
            LVS_ONLY_BOARD_JSON_FIELDS if args.lvs_only else REQUIRED_BOARD_JSON_FIELDS
        )
        if board_json_path.is_file():
            field_errors = assert_board_json_fields(board_json_path, required_fields)
            if field_errors:
                for msg in field_errors:
                    _err(msg, file=board_json_path)
                failed = True
            else:
                print(
                    "[ok] board.json fields: "
                    + ", ".join(f"{k}={v!r}" for k, v in required_fields.items())
                )

    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
