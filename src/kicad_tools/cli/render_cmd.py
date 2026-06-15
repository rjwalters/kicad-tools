"""
Board render command for kicad-tools CLI.

Generates per-board visual artifacts for downstream consumers (e.g. the
kicad-tools.org demo gallery, Epic #3674):

- 2D layer plots (front + back: copper + silkscreen + edge cuts) via
  ``kicad-cli pcb export png``.
- 3D ray-traced renders (front + back) via ``kicad-cli pcb render``.

Outputs are written to a fixed, documented path under each board's output
directory so the site build and CI can consume them without board-specific
logic:

    boards/<id>/output/renders/pcb-front.png   # 2D front layer plot
    boards/<id>/output/renders/pcb-back.png    # 2D back layer plot
    boards/<id>/output/renders/3d-front.png    # 3D ray-traced front
    boards/<id>/output/renders/3d-back.png     # 3D ray-traced back

The routed PCB (``*_routed.kicad_pcb``) is preferred; the command falls back
to the unrouted ``*.kicad_pcb`` when no routed artifact exists.

This command only writes image files — it makes no assumptions about hosting
or any website. Generated PNGs are git-ignored build artifacts.

``kicad-cli pcb render`` requires KiCad 8.0.4 or newer and a display (use a
virtual framebuffer such as ``xvfb-run`` on headless CI).

Usage:
    kct render boards/01-voltage-divider          # render one board
    kct render boards/                             # render all boards under a root
    kct render boards/01-voltage-divider --no-3d   # skip 3D (headless CI)
    kct render boards/01-voltage-divider --format json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.cli.export_cmd import _find_pcb_for_export
from kicad_tools.cli.runner import (
    find_kicad_cli,
    get_kicad_version,
    run_pcb_export_png,
    run_pcb_render,
)

# Minimum KiCad version required for `kicad-cli pcb render`.
MIN_RENDER_VERSION = (8, 0, 4)

# Layer presets for the 2D plots (copper + silkscreen + board outline).
# KiCad 8+ accepts the long `F.Silkscreen` / `B.Silkscreen` canonical names.
FRONT_LAYERS = ["F.Cu", "F.Silkscreen", "Edge.Cuts"]
BACK_LAYERS = ["B.Cu", "B.Silkscreen", "Edge.Cuts"]

# Output file names (fixed contract for downstream consumers).
RENDER_OUTPUTS = {
    "pcb-front": "pcb-front.png",
    "pcb-back": "pcb-back.png",
    "3d-front": "3d-front.png",
    "3d-back": "3d-back.png",
}


def _parse_version(version: str | None) -> tuple[int, int, int] | None:
    """Parse a KiCad version string like '8.0.6' into a (8, 0, 6) tuple.

    Returns None if the version can't be parsed. Extra build metadata after
    the first three numeric components is ignored.
    """
    if not version:
        return None
    # The `kicad-cli version` output may include extra words; grab the first
    # token that looks like a dotted version.
    for token in version.replace(",", " ").split():
        parts = token.split(".")
        nums: list[int] = []
        for part in parts[:3]:
            # Strip any trailing non-digits (e.g. "0rc1").
            digits = ""
            for ch in part:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            if not digits:
                break
            nums.append(int(digits))
        if len(nums) >= 2:
            while len(nums) < 3:
                nums.append(0)
            return (nums[0], nums[1], nums[2])
    return None


def _discover_boards(root: Path) -> list[Path]:
    """Discover board directories under *root*.

    A "board directory" is one that contains an ``output/`` subdirectory.
    If *root* itself is a board directory it is returned as the single board.
    Otherwise each immediate child board directory (sorted) is returned,
    recursing one level into ``external/`` style grouping directories.
    """
    if (root / "output").is_dir():
        return [root]

    boards: list[Path] = []
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if (child / "output").is_dir():
            boards.append(child)
        else:
            # One level of grouping (e.g. boards/external/<board>/output).
            for grandchild in sorted(p for p in child.iterdir() if p.is_dir()):
                if (grandchild / "output").is_dir():
                    boards.append(grandchild)
    return boards


def _render_board(
    board_dir: Path,
    *,
    do_3d: bool,
    kicad_cli: Path | None,
    quiet: bool,
) -> dict:
    """Render a single board. Returns a status dict for JSON output.

    Status entries:
        board: board directory name
        pcb: resolved PCB path (or None)
        status: "ok" | "skipped" | "partial" | "error"
        outputs: {name: path} for files written
        errors: list of error strings
    """
    name = board_dir.name
    result: dict = {
        "board": name,
        "pcb": None,
        "status": "skipped",
        "outputs": {},
        "errors": [],
    }

    output_dir = board_dir / "output"
    pcb_path = _find_pcb_for_export(output_dir) if output_dir.is_dir() else None
    if pcb_path is None:
        # Non-fatal skip — board has no PCB to render yet.
        result["errors"].append("no .kicad_pcb found")
        if not quiet:
            print(f"  {name}: SKIP (no .kicad_pcb found)", file=sys.stderr)
        return result

    result["pcb"] = str(pcb_path)
    renders_dir = output_dir / "renders"
    renders_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, str] = {}
    errors: list[str] = []

    # --- 2D layer plots ---
    plots = [
        ("pcb-front", FRONT_LAYERS),
        ("pcb-back", BACK_LAYERS),
    ]
    for key, layers in plots:
        out = renders_dir / RENDER_OUTPUTS[key]
        res = run_pcb_export_png(pcb_path, out, layers, kicad_cli=kicad_cli)
        if res.success:
            written[key] = str(out)
        else:
            errors.append(f"{key}: {res.stderr.strip()}")

    # --- 3D ray-traced renders ---
    if do_3d:
        renders = [
            ("3d-front", "front"),
            ("3d-back", "back"),
        ]
        for key, side in renders:
            out = renders_dir / RENDER_OUTPUTS[key]
            res = run_pcb_render(pcb_path, out, side=side, kicad_cli=kicad_cli)
            if res.success:
                written[key] = str(out)
            else:
                errors.append(f"{key}: {res.stderr.strip()}")

    result["outputs"] = written
    result["errors"] = errors

    expected = 2 + (2 if do_3d else 0)
    if len(written) == expected:
        result["status"] = "ok"
    elif written:
        result["status"] = "partial"
    else:
        result["status"] = "error"

    if not quiet:
        for key in written:
            print(f"  {name}: wrote {written[key]}")
        for err in errors:
            print(f"  {name}: ERROR {err}", file=sys.stderr)

    return result


def run_render(args: argparse.Namespace) -> int:
    """Execute the render command. Returns a process exit code."""
    root = Path(args.path)
    if not root.exists():
        print(f"Error: path not found: {root}", file=sys.stderr)
        return 1

    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        print(
            "Error: kicad-cli not found. Install KiCad 8.0.4+ from "
            "https://www.kicad.org/download/",
            file=sys.stderr,
        )
        return 1

    do_3d = not args.no_3d

    # Version guard for 3D rendering (`pcb render` needs KiCad 8.0.4+).
    if do_3d:
        version = get_kicad_version(kicad_cli)
        parsed = _parse_version(version)
        if parsed is not None and parsed < MIN_RENDER_VERSION:
            min_str = ".".join(str(n) for n in MIN_RENDER_VERSION)
            print(
                f"Error: 3D render requires KiCad {min_str}+ "
                f"(found {version}). Re-run with --no-3d to skip 3D renders.",
                file=sys.stderr,
            )
            return 1

    boards = _discover_boards(root)
    if not boards:
        print(f"Error: no board directories found under {root}", file=sys.stderr)
        return 1

    results = [
        _render_board(b, do_3d=do_3d, kicad_cli=kicad_cli, quiet=args.format == "json")
        for b in boards
    ]

    if args.format == "json":
        print(json.dumps({"boards": results}, indent=2))

    # Exit non-zero only if a board with a PCB failed every render. Boards
    # skipped for lack of a PCB are a non-fatal condition (exit 0).
    has_error = any(r["status"] == "error" for r in results)
    return 1 if has_error else 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``kct render`` command (standalone invocation)."""
    parser = argparse.ArgumentParser(
        prog="kct render",
        description=(
            "Render per-board 2D layer plots and 3D ray-traced PNGs into "
            "boards/<id>/output/renders/."
        ),
    )
    parser.add_argument(
        "path",
        help="Board directory or a root containing board directories",
    )
    parser.add_argument(
        "--no-3d",
        action="store_true",
        help="Skip 3D ray-traced renders (for headless CI without a display)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    args = parser.parse_args(argv)
    return run_render(args)


if __name__ == "__main__":
    raise SystemExit(main())
