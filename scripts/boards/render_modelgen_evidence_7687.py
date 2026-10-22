"""Render `kct models generate` bodies for board 03 J2 and board 09 RSH1/L1 (Issue #7687).

For each target the script cuts the footprint out of its board into a scratch
one-footprint PCB (with an Edge.Cuts rectangle 1 mm outside the courtyard),
generates its STEP with the CadQuery families and ``--attach``es it, then runs
``kicad-cli pcb render --side top`` on the pinned evidence toolchain.  The
committed boards are not touched (they keep their #7685 VRML bodies).

Needs the ``[models]`` extra and the pinned toolchain::

    KCT_KICAD_DOCKER=1 uv run --with cadquery \\
        python scripts/boards/render_modelgen_evidence_7687.py [OUT_DIR]

Writes ``<OUT_DIR>/<ref>-top.png`` (default ``docs/guides/images/parametric-3d-models``)
and keeps the scratch boards, bodies and provenance in a temporary directory.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from kicad_tools.cli.kicad_toolchain import evidence_toolchain
from kicad_tools.modelgen import generate_model, load_footprint_from_pcb
from kicad_tools.pcb.models3d import iter_footprint_blocks

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "docs/guides/images/parametric-3d-models"

#: (board PCB, reference, family override, parameters)
TARGETS = [
    ("boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb", "J2", None, {}),
    ("boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb", "RSH1", None, {"H": 0.9}),
    ("boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb", "L1", "box", {"H": 5.0}),
]

_REF = re.compile(r'\((?:property\s+"Reference"|fp_text\s+reference)\s+"([^"]+)"')
_AT = re.compile(r'^\(footprint\s+"[^"]+"[\s\S]*?\n\s*\(at ([-\d.]+) ([-\d.]+)')


def _one_footprint_board(src: Path, ref: str, dest_dir: Path) -> Path:
    text = src.read_text()
    blocks = iter_footprint_blocks(text)
    block = next(
        b for b in blocks if (m := _REF.search(text[b.start : b.end])) and m.group(1) == ref
    )
    fp = text[block.start : block.end + 1]
    at = _AT.search(fp)
    assert at is not None, f"{ref}: footprint has no (at ...)"
    ox, oy = float(at.group(1)), float(at.group(2))
    crt = load_footprint_from_pcb(src, reference=ref).courtyard
    assert crt is not None, f"{ref}: footprint has no courtyard"
    edge = (
        f"\t(gr_rect (start {ox + crt.x0 - 1:.4f} {oy + crt.y0 - 1:.4f}) "
        f"(end {ox + crt.x1 + 1:.4f} {oy + crt.y1 + 1:.4f}) "
        '(stroke (width 0.05) (type default)) (fill no) (layer "Edge.Cuts"))\n'
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    pcb = dest_dir / f"{ref}.kicad_pcb"
    pcb.write_text(text[: blocks[0].start] + fp + "\n" + edge + ")\n")
    for table in src.parent.glob("*-lib-table"):
        shutil.copy(table, dest_dir / table.name)
    for lib in src.parent.glob("*.pretty"):
        shutil.copytree(lib, dest_dir / lib.name, dirs_exist_ok=True)
    return pcb


def main() -> int:
    out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kct-modelgen-7687-") as tmp:
        scratch = Path(tmp)
        boards = []
        for rel, ref, family, params in TARGETS:
            pcb = _one_footprint_board(REPO / rel, ref, scratch / ref)
            lib_id = load_footprint_from_pcb(pcb, reference=ref).lib_id
            result = generate_model(
                lib_id, pcb=pcb, reference=ref, family=family, params=params, attach=True
            )
            print(
                f"{ref}: {result.path.name} family={result.provenance['family']} "
                f"params={result.provenance['params']} "
                f"inside_courtyard={result.provenance['inside_courtyard']}"
            )
            boards.append((ref, pcb))
        with evidence_toolchain([scratch, out]) as toolchain:
            print(f"kicad-cli {toolchain.version} ({toolchain.docker_image or 'host'})")
            for ref, pcb in boards:
                png = out / f"{ref}-top.png"
                cmd = [
                    toolchain.kicad_cli, "pcb", "render", "--output", str(png),
                    "--side", "top", "--quality", "high", "--width", "900",
                    "--height", "700", str(pcb),
                ]  # fmt: skip
                proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
                if proc.returncode != 0:
                    print(proc.stderr or proc.stdout, file=sys.stderr)
                    return proc.returncode
                print(f"{ref}: wrote {png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
