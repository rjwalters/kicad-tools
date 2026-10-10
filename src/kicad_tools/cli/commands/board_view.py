"""``kct board-view``: question-specific board images for agents (issue #6316).

    kct board-view overview board.kicad_pcb
    kct board-view net board.kicad_pcb PHASE_B
    kct board-view layers board.kicad_pcb PHASE_B
    kct board-view crop board.kicad_pcb 20 30 50 50 --net PWM_A
    kct board-view list board.kicad_pcb

Why a command of its own rather than views on ``kct screenshot``: that command
takes one positional and dispatches on its suffix (``.kicad_pcb`` or
``.kicad_sch``), so view subcommands would break ``kct screenshot
board.kicad_pcb``; it needs ``kicad-cli`` and ``cairosvg`` where this needs
only matplotlib; and its JSON document has a different shape.

The parser is declared once, here, and the handler takes the parsed namespace
directly: there is no second standalone parser to drift from this one.

Machine output (``--format json``): one document, described in
``docs/reference/machine-output.md``.  ``list`` prints JSON by default.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..format_options import FORMAT_JSON, FORMAT_TEXT, add_format_flag, emit_json

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace

__all__ = ["add_board_view_parser", "run_board_view_command"]

COMMAND = "board-view"

_DESCRIPTION = (
    "Draw a routed or partly routed board to answer a routing question: mm axes, "
    "reference designators, pad net labels, one net highlighted, and the connections "
    "still missing as ratsnest lines. Needs matplotlib (pip install "
    "'kicad-tools[visualization]'), not kicad-cli. Axes are board-relative by default, "
    "the frame of `kct net-status` and `kct pcb move-footprint --to`; `kct check` "
    "locations are sheet-absolute (use --absolute). Missing links come from the same "
    "connectivity model as `kct check`. Zone pours are not drawn. Treat what you read "
    "off an image as a hypothesis and confirm it with `kct net-status` or `kct check`."
)


def _add_common(parser: ArgumentParser, *, image: bool, default_format: str) -> None:
    parser.add_argument("board_view_pcb", metavar="pcb", help="Path to .kicad_pcb file")
    if image:
        parser.add_argument(
            "-o",
            "--output",
            dest="board_view_output",
            default=None,
            help="Output PNG path (default: <pcb>-<view>[-<net>].png beside the board)",
        )
        parser.add_argument(
            "--max-size",
            dest="board_view_max_size",
            type=int,
            default=1568,
            help="Longest image side in pixels (default: 1568)",
        )
    parser.add_argument(
        "--absolute",
        dest="board_view_absolute",
        action="store_true",
        help=(
            "Use sheet-absolute coordinates (what `kct check` reports) instead of "
            "board-relative ones"
        ),
    )
    add_format_flag(parser, default=default_format, dest="board_view_format")


def _add_margin(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--margin",
        dest="board_view_margin",
        type=float,
        default=4.0,
        help="Margin round the net, in mm (default: 4)",
    )


def add_board_view_parser(subparsers: Any) -> None:
    """Register ``board-view`` and its five views on the ``kct`` parser."""
    parser = subparsers.add_parser(
        COMMAND,
        help="Board images for a routing question: net highlight, ratsnest, mm axes",
        description=_DESCRIPTION,
    )
    views = parser.add_subparsers(dest="board_view_command", help="View")

    overview = views.add_parser(
        "overview", help="Whole board, all copper layers, every missing link"
    )
    _add_common(overview, image=True, default_format=FORMAT_TEXT)

    net = views.add_parser(
        "net",
        help="Crop round one net's missing links (or its pads if complete), net highlighted",
    )
    _add_common(net, image=True, default_format=FORMAT_TEXT)
    net.add_argument("board_view_net", metavar="net", help="Net name to highlight")
    _add_margin(net)

    layers = views.add_parser("layers", help="The 'net' crop as one panel per copper layer")
    _add_common(layers, image=True, default_format=FORMAT_TEXT)
    layers.add_argument("board_view_net", metavar="net", help="Net name to highlight")
    _add_margin(layers)

    crop = views.add_parser("crop", help="An explicit window: x0 y0 x1 y1 in mm")
    _add_common(crop, image=True, default_format=FORMAT_TEXT)
    for name in ("x0", "y0", "x1", "y1"):
        crop.add_argument(
            f"board_view_{name}", metavar=name, type=float, help=f"Window {name} in mm"
        )
    crop.add_argument(
        "--net", dest="board_view_net", default=None, help="Net name to highlight (optional)"
    )

    listing = views.add_parser(
        "list", help="Unfinished nets and their missing pad pairs as JSON, no image"
    )
    _add_common(listing, image=False, default_format=FORMAT_JSON)


def _fail(as_json: bool, view: str, source: str, message: str) -> int:
    if as_json:
        emit_json(
            {"command": COMMAND, "view": view, "input": source, "error": message, "success": False}
        )
    else:
        print(f"Error: {message}", file=sys.stderr)
    return 1


def _default_output(pcb: str, view: str, net: str | None) -> str:
    path = Path(pcb)
    suffix = f"-{view}"
    if net:
        suffix += "-" + re.sub(r"[^A-Za-z0-9_.+-]+", "_", net).strip("_")
    return str(path.with_name(f"{path.stem}{suffix}.png"))


def _document(view: str, source: str, result: dict[str, Any]) -> dict[str, Any]:
    dropped = {"image_base64", "pcb_path", "output_path", "error_message"}
    document = {key: value for key, value in result.items() if key not in dropped}
    document["command"] = COMMAND
    document["input"] = source
    if view != "list":
        document["output"] = result.get("output_path")
    return document


def _print_links(result: dict[str, Any]) -> None:
    links = result["missing_links"]
    print(
        f"Unfinished nets: {result['unfinished_net_count']} "
        f"({result['missing_link_count']} missing links)"
    )
    for net, pairs in links.items():
        for pair in pairs:
            print(f"  {net}: {pair['from']} -> {pair['to']}  {pair['length_mm']:.2f} mm")
    for entry in result["pour_advisory"]:
        pads = ", ".join(entry["stranded_pads"])
        print(f"  {entry['net']}: pour does not reach {pads} (advisory in kct check, no link)")


def run_board_view_command(args: Namespace) -> int:
    """Handle ``kct board-view`` and its views."""
    view = getattr(args, "board_view_command", None)
    if not view:
        print("Usage: kct board-view <view> <pcb> [options]")
        print("Views: overview, net, layers, crop, list")
        return 1

    from kicad_tools.boardview import board_view

    as_json = getattr(args, "board_view_format", FORMAT_TEXT) == FORMAT_JSON
    source = args.board_view_pcb
    net = getattr(args, "board_view_net", None)
    window = None
    if view == "crop":
        window = (args.board_view_x0, args.board_view_y0, args.board_view_x1, args.board_view_y1)
    output = None
    if view != "list":
        output = getattr(args, "board_view_output", None) or _default_output(source, view, net)

    result = board_view(
        source,
        view,
        net=net,
        window=window,
        absolute=getattr(args, "board_view_absolute", False),
        margin_mm=getattr(args, "board_view_margin", 4.0),
        max_size_px=getattr(args, "board_view_max_size", 1568),
        output_path=output,
    )
    if not result["success"]:
        return _fail(as_json, view, source, str(result["error_message"]))

    if as_json:
        emit_json(_document(view, source, result))
        return 0

    origin = result["board_origin_mm"]
    if view != "list":
        x0, y0, x1, y1 = result["window_mm"]
        print(f"Board view saved to {result['output_path']}")
        print(
            f"  Size: {result['width_px']}x{result['height_px']} px ({result['px_per_mm']} px/mm)"
        )
        print(f"  Window: x {x0:g}..{x1:g} mm, y {y0:g}..{y1:g} mm")
    print(f"Frame: {result['frame']}, board origin on the sheet ({origin[0]:g}, {origin[1]:g}) mm")
    _print_links(result)
    if view != "list":
        print(f"Note: {result['caveat']}")
    return 0
