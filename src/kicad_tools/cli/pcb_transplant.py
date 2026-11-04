"""``kct pcb transplant SRC DST --nets A,B -o OUT`` (issue #8311).

Copies the tracks and vias of the named nets from SRC onto DST (same
placement, different routing) and writes ``OUT``.  DST is never modified.
Logic lives in :mod:`kicad_tools.pcb.transplant`; this is the glue: spec
resolution, ``--dry-run`` (rendered as a ``pcb diff``) and the optional DRC.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from kicad_tools.cli.check_diff import DiffSideError, resolve_side

__all__ = ["run_pcb_transplant"]


def _split_nets(nets: Sequence[str]) -> list[str]:
    """Flatten ``--nets A,B`` (and repeats) to names; commas split names."""
    out: list[str] = []
    for chunk in nets:
        out.extend(n.strip() for n in chunk.split(",") if n.strip())
    return list(dict.fromkeys(out))


def _run_drc(out: Path) -> None:
    from kicad_tools.cli.runner import find_kicad_cli, run_drc

    if find_kicad_cli() is None:
        print("[DRC] skipped: kicad-cli not found", file=sys.stderr)
        return
    with tempfile.TemporaryDirectory(prefix="kct-transplant-drc-") as tmp:
        report = Path(tmp) / "drc.json"
        res = run_drc(out, output_path=report, schematic_parity=False)
        if not res.success or not report.exists():
            print(f"[DRC] kicad-cli failed: {getattr(res, 'stderr', '')}", file=sys.stderr)
            return
        try:
            data = json.loads(report.read_text())
        except ValueError:
            print("[DRC] could not parse kicad-cli report", file=sys.stderr)
            return
        n = len(data.get("violations", []))
        u = len(data.get("unconnected_items", []))
        print(f"[DRC] {n} violation(s), {u} unconnected item(s) in {out}")


def run_pcb_transplant(
    src_spec: str,
    dst_spec: str,
    *,
    nets: Sequence[str],
    output: str | None,
    dry_run: bool = False,
    drc: bool = False,
) -> int:
    """Transplant; returns 0 done, 2 dry-run differs, 1 error."""
    from kicad_tools.pcb.transplant import TransplantError, transplant_nets
    from kicad_tools.schema.pcb import PCB

    names = _split_nets(nets)
    if not names:
        print("Error: --nets requires at least one net name", file=sys.stderr)
        return 1
    if not dry_run and not output:
        print("Error: -o/--output is required (DST is never modified in place)", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="kct-pcb-transplant-") as tmp:
        scratch = Path(tmp)
        paths: dict[str, Path] = {}
        boards: dict[str, PCB] = {}
        for label, spec in (("src", src_spec), ("dst", dst_spec)):
            try:
                paths[label] = resolve_side(spec, scratch, label)
                if paths[label].is_dir():
                    raise DiffSideError(f"{label}: {spec!r} is a directory, not a .kicad_pcb")
                boards[label] = PCB.load(paths[label])
            except DiffSideError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            except Exception as e:  # noqa: BLE001 - any parse failure is a load error
                print(f"Error: {label}: cannot load {spec}: {e}", file=sys.stderr)
                return 1

        if output and Path(output).resolve() == paths["dst"].resolve():
            print("Error: -o must differ from DST (never modified in place)", file=sys.stderr)
            return 1

        try:
            result = transplant_nets(boards["src"], boards["dst"], names)
        except TransplantError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

        for name, c in sorted(result.nets.items()):
            note = (
                " (SRC has no copper on this net; DST's was removed)"
                if (c.added_tracks + c.added_vias == 0)
                else ""
            )
            print(
                f"{name}: removed {c.removed_tracks} track(s)/{c.removed_vias} via(s), "
                f"added {c.added_tracks} track(s)/{c.added_vias} via(s){note}"
            )

        if dry_run:
            from kicad_tools.cli.pcb_diff import run_pcb_diff

            preview = scratch / "would-be.kicad_pcb"
            boards["dst"].save(preview)
            print("[dry-run] nothing written; resulting change vs DST:")
            return run_pcb_diff(str(paths["dst"]), str(preview))

        assert output is not None
        out = Path(output)
        try:
            boards["dst"].save(out)
        except OSError as e:
            print(f"Error: cannot write {output}: {e}", file=sys.stderr)
            return 1
        print(f"Wrote {out}")

    if drc:
        _run_drc(out)
    return 0
