"""``kct route-link``: route individual links between chosen endpoints (Issue #8310).

``kct route --complete`` can leave a few links open on a congested board, and
``route-auto`` only takes a net or a region.  This command targets one link at
a time -- a pad or an ``x,y`` point at each end, an optional forced via, an
optional layer allow-list -- and routes it with
:func:`kicad_tools.router.link_router.route_link`, the router the oracle
completion loop already uses, so the pairwise-clearance and edge rules live in
exactly one place.

``--probe`` is the read-only form: it reports a path (length, via count) or the
blocking item per link and never writes the board.

Per-link kicad-cli DRC / creepage gating and single-link rollback are Issue
#8314, not this command.

Coordinates on the command line are the board file's (absolute) millimetres,
as KiCad shows them.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .format_options import FORMAT_JSON, emit_json, stdout_to_stderr_when

__all__ = [
    "EndSpec",
    "LinkSpec",
    "LinkSpecError",
    "LinkAction",
    "ViaAtAction",
    "LinkLayersAction",
    "add_route_link_arguments",
    "parse_link_spec",
    "run_route_link_command",
]


class LinkSpecError(ValueError):
    """A ``--link`` / ``--via-at`` / ``--link-layers`` value could not be parsed."""


@dataclass
class EndSpec:
    """One link end: a footprint pad or a point on same-net copper."""

    ref: str | None = None
    pad: str | None = None
    xy: tuple[float, float] | None = None
    layer: str | None = None

    def describe(self) -> str:
        where = f"{self.ref}.{self.pad}" if self.xy is None else f"{self.xy[0]:g},{self.xy[1]:g}"
        return f"{where}@{self.layer}" if self.layer else where


@dataclass
class LinkSpec:
    """One requested link, with the options that follow it on the command line."""

    net: str
    a: EndSpec
    b: EndSpec
    via_at: tuple[float, float] | None = None
    layers: list[str] | None = None
    text: str = field(default="", compare=False)


def _parse_xy(text: str) -> tuple[float, float]:
    parts = text.split(",")
    if len(parts) != 2:
        raise LinkSpecError(f"expected x,y, got {text!r}")
    try:
        return (float(parts[0]), float(parts[1]))
    except ValueError as exc:
        raise LinkSpecError(f"expected numeric x,y, got {text!r}") from exc


def _parse_end(token: str) -> EndSpec:
    body, _, layer = token.partition("@")
    layer = layer or None
    if "@" in token and not layer:
        raise LinkSpecError(f"empty layer after '@' in {token!r}")
    if "," in body:
        return EndSpec(xy=_parse_xy(body), layer=layer)
    ref, dot, pad = body.rpartition(".")
    if not dot or not ref or not pad:
        raise LinkSpecError(f"endpoint {token!r} is neither REF.PAD nor x,y")
    return EndSpec(ref=ref, pad=pad, layer=layer)


def parse_link_spec(text: str) -> LinkSpec:
    """Parse ``"NET REF.PAD|x,y[@layer] REF.PAD|x,y[@layer]"``."""
    tokens = text.split()
    if len(tokens) != 3:
        raise LinkSpecError(
            f"--link needs 'NET END END' (three space-separated fields), got {text!r}"
        )
    return LinkSpec(net=tokens[0], a=_parse_end(tokens[1]), b=_parse_end(tokens[2]), text=text)


class LinkAction(argparse.Action):
    """``--link`` (repeatable): appends to the ordered ``links`` list."""

    def __call__(self, parser, namespace, values, option_string=None):  # type: ignore[no-untyped-def]
        try:
            spec = parse_link_spec(values)
        except LinkSpecError as exc:
            parser.error(str(exc))
        links = list(getattr(namespace, self.dest, None) or [])
        links.append(spec)
        setattr(namespace, self.dest, links)


class _ModifierAction(argparse.Action):
    """Base for options that modify the ``--link`` they follow."""

    def _last(self, parser, namespace, option_string):  # type: ignore[no-untyped-def]
        links = getattr(namespace, "links", None)
        if not links:
            parser.error(f"{option_string} must follow the --link it applies to")
        return links[-1]


class ViaAtAction(_ModifierAction):
    """``--via-at x,y``: force a waypoint via on the preceding ``--link``."""

    def __call__(self, parser, namespace, values, option_string=None):  # type: ignore[no-untyped-def]
        link = self._last(parser, namespace, option_string)
        try:
            link.via_at = _parse_xy(values)
        except LinkSpecError as exc:
            parser.error(f"{option_string}: {exc}")


class LinkLayersAction(_ModifierAction):
    """``--link-layers F.Cu[,B.Cu]``: restrict the preceding ``--link``'s layers."""

    def __call__(self, parser, namespace, values, option_string=None):  # type: ignore[no-untyped-def]
        link = self._last(parser, namespace, option_string)
        layers = [v for v in values.split(",") if v]
        if not layers:
            parser.error(f"{option_string}: expected a comma-separated layer list")
        link.layers = layers


def add_route_link_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the ``route-link`` arguments to ``parser``."""
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "--link",
        dest="links",
        action=LinkAction,
        metavar="'NET END END'",
        help=(
            "A link to route: 'NET REF.PAD|x,y[@layer] REF.PAD|x,y[@layer]'. "
            "Repeatable; links are routed in the order given. An x,y end must "
            "lie on same-net copper (a via or track). --via-at and "
            "--link-layers apply to the --link before them."
        ),
    )
    parser.add_argument(
        "--via-at",
        action=ViaAtAction,
        metavar="X,Y",
        default=None,
        help="Force a waypoint via at X,Y on the preceding --link.",
    )
    parser.add_argument(
        "--link-layers",
        action=LinkLayersAction,
        metavar="LAYERS",
        default=None,
        help="Comma-separated copper layers the preceding --link may use (e.g. F.Cu).",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Report per link a path (length, vias) or the blocking item; write nothing.",
    )
    parser.add_argument("-o", "--output", help="Write to this file instead of editing in place")
    parser.add_argument("--trace-width", type=float, default=0.2, help="Track width (mm)")
    parser.add_argument("--clearance", type=float, default=0.2, help="Clearance floor (mm)")
    parser.add_argument("--via-diameter", type=float, default=0.6, help="Via diameter (mm)")
    parser.add_argument("--via-drill", type=float, default=0.3, help="Via drill (mm)")
    parser.add_argument(
        "--net-clearance",
        action="append",
        default=[],
        metavar="NET=MM",
        help=(
            "Clearance the new copper must keep from NET (mm), e.g. an HV net. "
            "Repeatable; never below --clearance."
        ),
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )


def _rules_from_args(args: argparse.Namespace, pcb: Any) -> Any:
    from kicad_tools.router.link_router import LinkRouteRules

    base = LinkRouteRules(
        trace_width=args.trace_width,
        clearance=args.clearance,
        via_size=args.via_diameter,
        via_drill=args.via_drill,
    )
    if not args.net_clearance:
        return base
    names = {net.name: num for num, net in pcb.nets.items() if getattr(net, "name", None)}
    by_net: dict[int, float] = {}
    for item in args.net_clearance:
        name, eq, value = item.rpartition("=")
        if not eq or name not in names:
            raise LinkSpecError(f"--net-clearance {item!r}: expected KNOWN_NET=MM")
        try:
            by_net[names[name]] = float(value)
        except ValueError as exc:
            raise LinkSpecError(f"--net-clearance {item!r}: MM is not a number") from exc
    return replace(base, clearance_by_net=by_net)


def _resolve_end(
    pcb: Any, end: EndSpec, net_number: int, net: str, origin: tuple[float, float]
) -> tuple[Any, str | None]:
    """The :class:`LinkTerminal` for ``end``, or ``(None, why)``."""
    from kicad_tools.router.link_router import LinkTerminal, terminal_at_copper, terminal_for_pad

    if end.xy is None:
        assert end.ref is not None and end.pad is not None
        term = terminal_for_pad(pcb, end.ref, end.pad)
        if term is None:
            return None, f"pad {end.ref}.{end.pad} not found (or has no copper layer)"
        fp = pcb.get_footprint(end.ref)
        pad = next((p for p in fp.pads if str(p.number) == str(end.pad)), None)
        if pad is not None and pad.net_number != net_number:
            return None, f"pad {end.ref}.{end.pad} is on net {pad.net_name!r}, not {net!r}"
    else:
        point = (end.xy[0] - origin[0], end.xy[1] - origin[1])
        term = terminal_at_copper(pcb, net_number, point, end.layer, "via")
        if term is None:
            term = terminal_at_copper(pcb, net_number, point, end.layer, "track")
        if term is None:
            where = f" on {end.layer}" if end.layer else ""
            return None, f"no {net!r} via or track at {end.xy[0]:g},{end.xy[1]:g}{where}"
    if end.layer is not None:
        if end.layer not in term.layers:
            return None, f"{end.describe()} has no copper on layer {end.layer}"
        term = LinkTerminal(term.label, term.point, {end.layer: term.layers[end.layer]})
    return term, None


def _route_length(route: Any) -> float:
    return sum(math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2, _ in route.segments)


def _commit_to_model(model: Any, route: Any, net_number: int) -> None:
    """Make later links see ``route``'s copper (mirrors the oracle's commit)."""
    from shapely.geometry import LineString, Point

    from kicad_tools.router.link_router import _Item

    for x1, y1, x2, y2, layer in route.segments:
        line = LineString([(x1, y1), (x2, y2)]) if (x1, y1) != (x2, y2) else Point(x1, y1)
        model.items.append(_Item(net_number, {layer: line.buffer(route.width / 2)}))
    for vx, vy in route.vias:
        disk = Point(vx, vy).buffer(route.via_size / 2)
        model.items.append(_Item(net_number, dict.fromkeys(model.copper, disk)))
        model.drills.append((vx, vy, route.via_drill))


def _route_one(
    pcb: Any,
    model: Any,
    rules: Any,
    spec: LinkSpec,
    origin: tuple[float, float],
    net_ids: dict[str, int],
) -> tuple[dict[str, Any], Any]:
    """Probe/route one link; returns ``(report entry, LinkRoute | None)``."""
    from kicad_tools.router.link_router import (
        _via_site_blocker,
        explain_link_failure,
        route_link,
        route_link_via,
    )

    entry: dict[str, Any] = {
        "link": f"{spec.net} {spec.a.describe()} {spec.b.describe()}",
        "net": spec.net,
        "routable": False,
    }
    net_number = net_ids.get(spec.net)
    if net_number is None:
        entry["reason"] = f"net {spec.net!r} not on the board"
        return entry, None
    allowed = None
    if spec.layers is not None:
        known = set(model.copper)
        bad = [layer for layer in spec.layers if layer not in known]
        if bad:
            entry["reason"] = f"unknown copper layer(s) {', '.join(bad)}"
            return entry, None
        allowed = spec.layers
    ta, why = _resolve_end(pcb, spec.a, net_number, spec.net, origin)
    tb, why_b = _resolve_end(pcb, spec.b, net_number, spec.net, origin)
    if ta is None or tb is None:
        entry["reason"] = why or why_b
        return entry, None
    if allowed is not None:
        for label, term in (("A", ta), ("B", tb)):
            if not any(layer in allowed for layer in term.layers):
                entry["reason"] = (
                    f"endpoint {label} has no copper on the allowed layers {','.join(allowed)}"
                )
                return entry, None
    if spec.via_at is not None:
        site = (spec.via_at[0] - origin[0], spec.via_at[1] - origin[1])
        site_why = _via_site_blocker(model, net_number, site, rules)
        if site_why is not None:
            entry["reason"] = site_why
            return entry, None
        route = route_link_via(
            pcb, net_number, spec.net, ta, tb, site, rules, model=model, allowed_layers=allowed
        )
    else:
        route = route_link(
            pcb, net_number, spec.net, ta, tb, rules, model=model, allowed_layers=allowed
        )
    if route is None:
        entry.update(
            explain_link_failure(
                pcb, net_number, ta, tb, rules, model=model, allowed_layers=allowed
            )
        )
        return entry, None
    entry["routable"] = True
    entry["length_mm"] = round(_route_length(route), 3)
    entry["vias"] = len(route.vias)
    entry["layers"] = sorted({layer for *_xy, layer in route.segments})
    return entry, route


def run_route_link_command(args: argparse.Namespace) -> int:
    """Entry point for ``kct route-link``. Exit 1 if any link is unroutable."""
    as_json = args.format == FORMAT_JSON
    links: list[LinkSpec] = list(getattr(args, "links", None) or [])

    def fail(message: str) -> int:
        if as_json:
            emit_json({"command": "route-link", "ok": False, "error": message})
        else:
            print(f"route-link: {message}", file=sys.stderr)
        return 1

    if not links:
        return fail("at least one --link is required")
    pcb_path = Path(args.pcb)
    if not pcb_path.is_file():
        return fail(f"board not found: {pcb_path}")

    with stdout_to_stderr_when(as_json):
        from kicad_tools.router.link_router import _build_model, append_link_routes
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(pcb_path))
        origin = pcb.board_origin
        try:
            rules = _rules_from_args(args, pcb)
        except LinkSpecError as exc:
            return fail(str(exc))
        model = _build_model(pcb)
        net_ids = {net.name: num for num, net in pcb.nets.items() if net.name}

        entries: list[dict[str, Any]] = []
        committed: list[Any] = []
        for spec in links:
            entry, route = _route_one(pcb, model, rules, spec, origin, net_ids)
            entries.append(entry)
            if route is not None:
                # Later links (probe or commit) must see this copper.
                _commit_to_model(model, route, net_ids[spec.net])
                committed.append(route)

        written: str | None = None
        if not args.probe and committed:
            out_path = Path(args.output) if args.output else pcb_path
            if out_path != pcb_path:
                out_path.write_bytes(pcb_path.read_bytes())
            append_link_routes(out_path, committed, origin)
            written = str(out_path)

    ok = all(e["routable"] for e in entries)
    if as_json:
        emit_json(
            {
                "command": "route-link",
                "ok": ok,
                "probe": bool(args.probe),
                "written": written,
                "links": entries,
            }
        )
    else:
        for e in entries:
            if e["routable"]:
                verb = "routable" if args.probe else "routed"
                print(
                    f"  {verb}: {e['link']}  {e['length_mm']:.3f} mm, {e['vias']} via(s), "
                    f"layers {','.join(e['layers'])}"
                )
            else:
                print(f"  UNROUTABLE: {e['link']}: {e['reason']}")
                b = e.get("blocker")
                if b:
                    print(
                        f"    nearest foreign item: {b['kind']} on net {b['net']} ({b['layer']}), "
                        f"{b['distance_mm']:.3f} mm from the straight line; needs "
                        f">= {b['required_mm']:.3f} mm"
                    )
        if written:
            print(f"route-link: wrote {sum(e['routable'] for e in entries)} link(s) to {written}")
        elif args.probe:
            print("route-link: probe only; board not written")
    return 0 if ok else 1
