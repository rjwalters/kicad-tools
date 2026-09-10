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
    """Canonicalize generated witnesses to their archived synthetic identity.

    Active output paths never inherit a synthetic baseline. The measured PCB
    path stays real; only the isolated regeneration shares its fixture's key.
    """
    path = pcb.resolve()
    if (
        path.parent.name in {"regression-output", "regression-fixture"}
        and (path.parent.parent / "regression-fixture").is_dir()
    ):
        path = path.parent.parent / "regression-fixture" / path.name
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)
