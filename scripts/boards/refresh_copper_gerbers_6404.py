"""Replace only the copper-layer Gerbers of a committed bundle (Issue #6404).

A zone refill changes the saved fills, and the Gerbers of the committed
manufacturing bundles are plotted from the saved fills.  Re-plotting the whole
bundle would also change the drill-file layout and every header date, so this
re-plots the board and swaps in just the ``*_Cu.*`` members of the existing
``gerbers.zip``; every other member keeps its committed bytes, order and
timestamp.

Usage::

    uv run python scripts/boards/refresh_copper_gerbers_6404.py \\
        boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb \\
        boards/06-diffpair-test/output/manufacturing/gerbers/gerbers.zip

Run it *before* the step that re-hashes the manifest / outer archive
(``refresh_readiness.py``, ``regenerate_board_04_6404.py``).
"""

from __future__ import annotations

import re
import sys
import tempfile
import zipfile
from pathlib import Path

COPPER_MEMBER = re.compile(r"-(?:F|B|In\d+)_Cu\.[A-Za-z0-9]+$")


def refresh_copper_gerbers(
    pcb: Path, archive: Path, members: re.Pattern[str] = COPPER_MEMBER
) -> list[str]:
    """Replace the ``members`` (default: copper layers) of ``archive`` with a fresh plot.

    Issue #6269 passes a silkscreen pattern to swap in just the re-plotted
    ``F_Silkscreen`` member after a reference-text move.
    """
    from kicad_tools.export.gerber import GerberExporter

    with tempfile.TemporaryDirectory(prefix="kct-6404-gerber-") as tmp:
        fresh_zip = GerberExporter(pcb).export_for_manufacturer("jlcpcb", Path(tmp))
        with zipfile.ZipFile(fresh_zip) as fresh:
            fresh_members = {name: fresh.read(name) for name in fresh.namelist()}
    with zipfile.ZipFile(archive) as old:
        entries = [(info, old.read(info.filename)) for info in old.infolist()]
    replaced: list[str] = []
    out: list[tuple[zipfile.ZipInfo, bytes]] = []
    for info, data in entries:
        if members.search(info.filename):
            data = fresh_members[info.filename]
            replaced.append(info.filename)
        out.append((info, data))
    if not replaced:
        raise RuntimeError(f"no members matching {members.pattern!r} in {archive}")
    with zipfile.ZipFile(archive, "w") as new:
        for info, data in out:
            new.writestr(info, data, compress_type=info.compress_type)
    return replaced


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print(__doc__)
        return 2
    print(refresh_copper_gerbers(Path(args[0]), Path(args[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
