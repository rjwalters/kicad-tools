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
                ".kct",
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


def measure_signal_reach(pcb_path: Path, pour_nets: set[str]) -> tuple[int, int, list[str]]:
    """Measure routed-signal-net reach on a routed PCB.

    Issue #3413 phase 5: the gate previously asserted only the DRC error
    count + rule-exercise, so a reach regression (e.g. 18/21 with 3
    connectivity errors) stayed green as long as the error count fit
    under the allowlist floor.  This helper counts COMPLETE signal nets
    per :class:`~kicad_tools.analysis.net_status.NetStatusAnalyzer` --
    the same per-net completeness model the connectivity DRC rule uses.

    Pour nets are excluded: their connectivity is plane/stitching-based
    and is gated separately (the board recipe's copper-union audit;
    see boards/06-diffpair-test/generate_design.py step 11).

    Shared by ``check_diffpair_coverage.py`` (board 06) and
    ``check_matchgroup_coverage.py`` (board 07, Issue #7319) so both gates
    count reach with one model.

    Args:
        pcb_path: Routed PCB to measure.
        pour_nets: Net names excluded from the signal universe (the
            board module's ``POUR_NETS``).

    Returns:
        ``(complete_count, signal_total, incomplete_names)``.
    """
    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    result = NetStatusAnalyzer(pcb_path).analyze()
    signal = [n for n in result.nets if n.net_name and n.net_name not in pour_nets]
    complete = [n for n in signal if n.status == "complete"]
    incomplete_names = sorted(n.net_name for n in signal if n.status != "complete")
    return len(complete), len(signal), incomplete_names
