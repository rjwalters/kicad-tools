"""Keep synthetic recipe witnesses separate from assembled gallery releases."""

import shutil
from pathlib import Path


def recipe_output_dir(board_dir: Path, *, prepare: bool = False) -> Path:
    fixture = board_dir / "regression-fixture"
    if not fixture.is_dir():
        return board_dir / "output"
    output = board_dir / "regression-output"
    if prepare:
        output.mkdir(parents=True, exist_ok=True)
        for source in fixture.iterdir():
            if source.is_file() and source.suffix in {
                ".kicad_pcb",
                ".kicad_pro",
                ".kicad_dru",
                ".kicad_sch",
                ".kicad_sym",
                ".json",
            }:
                shutil.copy2(source, output / source.name)
    return output if output.is_dir() else fixture


def recipe_baseline_key(pcb: Path) -> str:
    """Retain existing synthetic tolerances without applying them to a new board.

    Coverage gates use these legacy baseline identifiers only after selecting
    the isolated synthetic recipe artifact. The measured PCB path stays real.
    """
    path = pcb.resolve()
    if (
        path.parent.name in {"regression-output", "regression-fixture"}
        and (path.parent.parent / "regression-fixture").is_dir()
    ):
        path = path.parent.parent / "output" / path.name
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)
