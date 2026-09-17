"""Determinism: the same seed must produce byte-identical KiCad files.

Without this the committed fixtures churn on every regeneration and a reviewer
cannot tell an intentional geometry change from UUID noise -- which would make
"the fixture drifted" indistinguishable from "the fixture was edited to hide a
disagreement".

Two sources of non-determinism had to be removed to make this possible, and
both are re-checked here rather than merely trusted:

* ``PCB.add_trace`` / ``add_via`` / ``add_footprint_from_file`` mint a fresh
  ``uuid.uuid4()`` per object -> ``board.seeded_uuids`` patches ``uuid.uuid4``
  with a stream drawn from the case.
* ``PCB.create`` stamps ``date.today()`` into the title block ->
  ``CopperCase.board_date`` pins it.

``PYTHONHASHSEED`` gets its own test: the UUID seed is derived from the case's
name, and ``hash()`` on a string is salted per process, so a careless
implementation passes every same-process comparison and still produces
different files on CI than on a laptop.

No kicad-cli needed.
"""

from __future__ import annotations

import filecmp
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conformance.board import write_case
from tests.conformance.fixtures import (
    NAMED_FIXTURES,
    build_named_fixture,
    fixture_stem,
)
from tests.conformance.generator import FIXED_BOARD_DATE, generate_case

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "conformance"


@pytest.mark.parametrize("seed", [0, 7, 23])
def test_corpus_case_is_byte_identical_across_runs(seed: int, tmp_path: Path) -> None:
    case_a = generate_case(seed)
    case_b = generate_case(seed)
    assert case_a == case_b, "the generator itself is not reproducible"

    a = write_case(case_a, tmp_path / "run-a")
    b = write_case(case_b, tmp_path / "run-b")

    assert filecmp.cmp(a.pcb_path, b.pcb_path, shallow=False), (
        f"seed {seed}: .kicad_pcb differs between two generator runs"
    )
    assert filecmp.cmp(a.project_path, b.project_path, shallow=False), (
        f"seed {seed}: .kicad_pro differs between two generator runs"
    )


@pytest.mark.parametrize("name", NAMED_FIXTURES)
def test_committed_fixture_matches_a_fresh_regeneration(name: str, tmp_path: Path) -> None:
    """Regenerating a fixture reproduces the committed bytes exactly.

    This is what makes the committed boards reviewable: a diff in a fixture is
    always a real geometry change, never UUID churn.
    """
    case = build_named_fixture(name)
    stem = fixture_stem(case)
    fresh = write_case(case, tmp_path, stem=stem)

    committed_pcb = FIXTURES_DIR / f"{stem}.kicad_pcb"
    committed_pro = FIXTURES_DIR / f"{stem}.kicad_pro"

    assert filecmp.cmp(fresh.pcb_path, committed_pcb, shallow=False), (
        f"{stem}.kicad_pcb drifted from its generator; regenerate with "
        "`uv run python -m tests.conformance.fixtures --write`"
    )
    assert filecmp.cmp(fresh.project_path, committed_pro, shallow=False), (
        f"{stem}.kicad_pro drifted from its generator; regenerate with "
        "`uv run python -m tests.conformance.fixtures --write`"
    )


def test_board_date_is_pinned_not_today() -> None:
    """A ``date.today()`` stamp would make every regeneration a diff."""
    case = generate_case(0)
    assert case.board_date == FIXED_BOARD_DATE
    pcb_text = (
        FIXTURES_DIR / f"{fixture_stem(build_named_fixture(NAMED_FIXTURES[0]))}.kicad_pcb"
    ).read_text(encoding="utf-8")
    assert f'(date "{FIXED_BOARD_DATE}")' in pcb_text


def test_determinism_survives_a_different_pythonhashseed(tmp_path: Path) -> None:
    """Byte-identity must not depend on the process's string-hash salt.

    CPython salts ``hash(str)`` per process. A UUID seed derived from
    ``hash(case.name)`` would pass every in-process comparison above and still
    emit different files on CI than on a developer's machine, which is exactly
    the failure this whole determinism story exists to prevent.
    """
    script = (
        "import sys;"
        f"sys.path[:0] = [{str(REPO_ROOT)!r}, {str(REPO_ROOT / 'src')!r}];"
        "from pathlib import Path;"
        "from tests.conformance.generator import generate_case;"
        "from tests.conformance.board import write_case;"
        "print(write_case(generate_case(3), Path(sys.argv[1])).pcb_path)"
    )

    outputs = []
    for hashseed in ("0", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=hashseed)
        target = tmp_path / f"seed{hashseed}"
        proc = subprocess.run(
            [sys.executable, "-c", script, str(target)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        outputs.append(Path(proc.stdout.strip()))

    assert filecmp.cmp(outputs[0], outputs[1], shallow=False), (
        "board bytes depend on PYTHONHASHSEED -- the UUID seed must not be "
        "derived from the salted built-in hash()"
    )
