"""A board's committed package and its producer stay in sync (Issue #9243).

``Release._package_extras`` refuses to publish when the committed package
ships a file the new package would not (``refusing to drop files the current
package ships: ...``).  That refusal only fires when a developer runs the
board's ``package_release.py`` on the pinned KiCad toolchain, so a textually
clean merge that desynchronises a board's ``Release`` declaration from its
committed ``output/manufacturing/`` was invisible to CI -- observed when PR
#8848 dropped board 01's ``reviewed=`` carry-forward while main (#9074) kept
``procurement-review.md`` in the package.  These tests replay the file-set
half of that gate KiCad-free: every committed package name must be generic
producer output, declared by the board's ``Release``, or added by
``_package_extras`` itself, using the same drop comparison the producer runs
(:meth:`release_package.Release.dropped_files`).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from fnmatch import fnmatch
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "boards"))

from release_package import Release  # noqa: E402

from kicad_tools.cli.readiness_cmd import ReadinessOptions  # noqa: E402
from tests.test_kct_readiness_cmd import make_board

#: Generic ``kct readiness --generate`` output: every package name the
#: producer writes without board help.  Cross-checked against
#: ``ManufacturingPackage.export`` (BOM/CPL/gerbers via ``AssemblyPackage``,
#: ``report.md``/``report.pdf``/``images/`` via ``_flatten_latest_report``,
#: ``kicad_project.zip``, ``README.txt``; ``src/kicad_tools/export/manufacturing.py``)
#: and readiness_cmd's bundle finishing (``schematic.pdf``,
#: ``assembly-front.pdf``/``assembly-back.pdf``, ``manifest.json``).
GENERIC_PACKAGE_FILES: tuple[str, ...] = (
    "bom_*.csv",
    "cpl_*.csv",
    "gerbers/gerbers.zip",
    "kicad_project.zip",
    "report.md",
    "report.pdf",
    "images/*",
    "schematic.pdf",
    "assembly-front.pdf",
    "assembly-back.pdf",
    "README.txt",
    "manifest.json",
)

#: Files ``Release._package_extras`` adds for every board -- the readiness
#: gate-evidence copies (``boards/release_package.py``).
PRODUCER_EXTRA_FILES: tuple[str, ...] = ("check-report.json", "native-drc.json")


def _producer_boards() -> list[Path]:
    """Board dirs with a script-produced package (``producer == "kct readiness"``).

    Selection is by manifest producer, not by board name, so future boards
    are picked up and recipe-finalised boards (board 02: no producer -- its
    ``Release.main`` would delete and regenerate the package) are skipped,
    not failed.  Same discovery style as ``tests/test_fleet_cpl_frame.py``.
    """
    boards = []
    for script in sorted(REPO.glob("boards/*/package_release.py")):
        manifest = script.parent / "output" / "manufacturing" / "manifest.json"
        if not manifest.is_file():
            continue
        try:
            producer = json.loads(manifest.read_text()).get("producer")
        except ValueError:
            continue
        if producer == "kct readiness":
            boards.append(script.parent)
    return boards


BOARDS = _producer_boards()


def _load_release(board: Path) -> Release:
    spec = importlib.util.spec_from_file_location(
        f"package_release_{board.name}", board / "package_release.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.RELEASE


def _committed_names(package_dir: Path) -> set[str]:
    return {
        p.relative_to(package_dir).as_posix() for p in sorted(package_dir.rglob("*")) if p.is_file()
    }


def _shipped_names(release: Release, committed: set[str]) -> set[str]:
    """Names the producer would ship: generic output plus board declarations."""
    generic = {
        name for name in committed if any(fnmatch(name, pat) for pat in GENERIC_PACKAGE_FILES)
    }
    return generic | set(PRODUCER_EXTRA_FILES) | set(release.reviewed) | set(release.generated)


def test_producer_boards_are_discovered():
    # 00-simple-led and 01-voltage-divider as of 2026-11; board 02 is
    # recipe-finalised (no producer) and must be skipped, not failed.
    assert len(BOARDS) >= 2
    for board in BOARDS:
        manifest = json.loads((board / "output/manufacturing/manifest.json").read_text())
        assert manifest["producer"] == "kct readiness"


def test_dropped_files_comparison():
    assert Release.dropped_files(["b", "a", "c"], {"a", "c", "d"}) == ["b"]
    assert Release.dropped_files([], {"a"}) == []
    assert Release.dropped_files(["gerbers/gerbers.zip"], set()) == ["gerbers/gerbers.zip"]


@pytest.mark.parametrize("board", BOARDS, ids=lambda b: b.name)
def test_committed_package_would_not_be_dropped(board):
    release = _load_release(board)
    committed = _committed_names(board / "output" / "manufacturing")

    # The producer reads reviewed files (and the files they vouch for) from
    # the committed package via Release.old(); a missing name is a KeyError.
    for name, vouched in release.reviewed.items():
        assert name in committed, f"{board.name}: reviewed {name} missing from the package"
        for vouched_name in vouched:
            assert vouched_name in committed, (
                f"{board.name}: {name} vouches for {vouched_name}, missing from the package"
            )

    dropped = Release.dropped_files(committed, _shipped_names(release, committed))
    assert dropped == [], (
        f"{board.name}: the producer would refuse to publish; these committed files "
        f"are neither generic readiness output nor declared by the Release: "
        f"{', '.join(dropped)}"
    )


def test_gate_replays_the_pr_8848_merge(tmp_path):
    """The PR #8848 x #9074 shape: the package still ships
    ``procurement-review.md`` but the recipe no longer declares it."""
    package = tmp_path / "output" / "manufacturing"
    package.mkdir(parents=True)
    (package / "bom_jlcpcb.csv").write_text("bom\n")
    (package / "cpl_jlcpcb.csv").write_text("cpl\n")
    (package / "procurement-review.md").write_text("reviewed\n")
    committed = _committed_names(package)

    merged = Release(board_dir=tmp_path)  # reviewed={} -- the clean merge
    dropped = Release.dropped_files(committed, _shipped_names(merged, committed))
    assert dropped == ["procurement-review.md"]

    restored = Release(
        board_dir=tmp_path,
        reviewed={"procurement-review.md": ("bom_jlcpcb.csv", "cpl_jlcpcb.csv")},
    )
    assert Release.dropped_files(committed, _shipped_names(restored, committed)) == []


def test_package_extras_refuses_through_the_shared_comparison(tmp_path):
    """The producer half: ``_package_extras`` itself must refuse on the same
    shape, using ``Release.dropped_files`` (so test and producer cannot
    diverge)."""
    board = make_board(tmp_path)
    package = board / "output" / "manufacturing"
    package.mkdir(parents=True)
    (package / "bom_jlcpcb.csv").write_text("bom\n")
    (package / "cpl_jlcpcb.csv").write_text("cpl\n")
    (package / "procurement-review.md").write_text("reviewed\n")

    release = Release(board_dir=board)  # reviewed={} -- the PR #8848-merged recipe
    release._snapshot()

    # The generic export rebuilds the package without the hand-curated file.
    shutil.rmtree(package)
    package.mkdir()
    (package / "bom_jlcpcb.csv").write_text("bom\n")
    (package / "cpl_jlcpcb.csv").write_text("cpl\n")
    evidence = board / "output" / "readiness"
    evidence.mkdir()
    (evidence / "kct-check.json").write_text('{"summary": {"errors": 0}}\n')
    (evidence / "native-drc.json").write_text('{"error_count": 0}\n')
    options = ReadinessOptions(
        board_dir=board,
        pcb=board / "output" / "demo_routed.kicad_pcb",
        schematic=None,
        project=None,
        manufacturer="jlcpcb",
        mode="generate",
        output_dir=package,
        evidence_dir=evidence,
    )
    with pytest.raises(RuntimeError, match="refusing to drop.*procurement-review\\.md"):
        release._package_extras(options)

    # Declaring the carry-forward again (unchanged vouched bytes) publishes.
    carried = Release(
        board_dir=board,
        reviewed={"procurement-review.md": ("bom_jlcpcb.csv", "cpl_jlcpcb.csv")},
    )
    shutil.rmtree(package)
    package.mkdir()
    (package / "bom_jlcpcb.csv").write_text("bom\n")
    (package / "cpl_jlcpcb.csv").write_text("cpl\n")
    (package / "procurement-review.md").write_text("reviewed\n")
    carried._snapshot()
    shutil.rmtree(package)
    package.mkdir()
    (package / "bom_jlcpcb.csv").write_text("bom\n")
    (package / "cpl_jlcpcb.csv").write_text("cpl\n")
    assert carried._package_extras(options)  # no refusal; README sign-off lines
    assert (package / "procurement-review.md").read_text() == "reviewed\n"
