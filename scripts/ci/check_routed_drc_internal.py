#!/usr/bin/env python3
"""kicad-tools-internal routed-DRC gate: generic gate + board04 paid-drill dispatch.

``scripts/ci/check_routed_drc.py`` is vendored into consumer repos by
``scripts/install-kct.sh`` and must stay consumer-generic (Issue #8134).  This
wrapper is NOT vendored: it adds kicad-tools' own board04 reviewed
paid-mechanical-drill dispatch (#5009) on top of the generic gate and then runs
the generic ``main()``.  Every other PCB goes through the generic
``count_errors`` unchanged.

Board04 uses a paid mechanical-drill option, not laser microvias.  Its reviewed
validator checks the actual geometry, option selection and native floors before
applying scoped rules.  Stock tier1 alone cannot express that process.  Never
turn this into an error-count allowance.

Exit codes and CLI are identical to ``check_routed_drc.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_routed_drc as _generic  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
PAID_DRILL_BOARD = REPO_ROOT / "boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb"

_generic_count_errors = _generic.count_errors


def _run_paid_drill_check(pcb_path: Path) -> tuple[int, dict[str, int]]:
    checker = REPO_ROOT / "boards/04-stm32-devboard/check_manufacturing.py"
    with tempfile.TemporaryDirectory(prefix="kct-paid-drill-check-") as tmp:
        report = Path(tmp) / "check.json"
        proc = subprocess.run(
            [sys.executable, str(checker), str(pcb_path.resolve()), str(report)],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        if proc.returncode != 0 or not report.is_file():
            detail = proc.stderr.strip() or proc.stdout.strip()
            if report.is_file():
                try:
                    failed = json.loads(report.read_text())
                    detail += "\n" + json.dumps(
                        {"summary": failed.get("summary"), "meta_checks": failed.get("meta_checks")}
                    )
                except (OSError, ValueError):
                    pass
            raise RuntimeError(
                f"Reviewed paid-drill validation failed (exit {proc.returncode}): "
                f"{detail.strip() or 'no report or diagnostic output'}"
            )
        try:
            data = json.loads(report.read_text())
            if data.get("meta_checks", {}).get("overall") != "PASSED":
                raise ValueError("reviewed process meta-checks did not pass")
            return _generic._count_blocking_errors(data)
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeError(f"Invalid reviewed paid-drill report: {exc}") from exc


def count_errors(
    pcb_path: Path, mfr: str = _generic.DEFAULT_MANUFACTURER
) -> tuple[int, dict[str, int]]:
    """Generic ``count_errors`` plus the board04 paid-drill dispatch."""
    if pcb_path.resolve() == PAID_DRILL_BOARD:
        if mfr != "jlcpcb-tier1":
            raise RuntimeError("Board04 requires its reviewed tier1 paid-drill process")
        print("Using board04 reviewed paid mechanical-drill validator (#5009).", file=sys.stderr)
        return _run_paid_drill_check(pcb_path)
    return _generic_count_errors(pcb_path, mfr)


def main(argv: list[str] | None = None) -> int:
    # The generic main() resolves count_errors from its module globals.
    _generic.count_errors = count_errors
    return _generic.main(argv)


if __name__ == "__main__":
    sys.exit(main())
