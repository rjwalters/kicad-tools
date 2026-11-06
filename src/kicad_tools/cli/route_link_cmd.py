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

Without ``--probe`` links are committed one at a time (Issue #8314).  After
each, kicad-cli DRC (on the pinned evidence toolchain) and the pairwise creepage
census run on the board with that link added; a link that introduces a DRC
violation or a failing census pair absent from the last accepted state is
rolled back alone (the board file restored to its pre-link bytes) and later
links route against the board as it really is.  Exit 1 if any link was rolled
back or unroutable; exit 2 (nothing written) when the pinned kicad-cli is
unavailable.

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
    "NetClearanceAction",
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
    #: ``--net-clearance NET=MM`` entries that follow this ``--link`` (Issue #8546).
    net_clearance: list[str] = field(default_factory=list)
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
    body, _, layer_text = token.partition("@")
    layer = layer_text or None
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

    def __call__(self, parser, namespace, values, option_string=None):
        try:
            spec = parse_link_spec(values)
        except LinkSpecError as exc:
            parser.error(str(exc))
        links = list(getattr(namespace, self.dest, None) or [])
        links.append(spec)
        setattr(namespace, self.dest, links)


class _ModifierAction(argparse.Action):
    """Base for options that modify the ``--link`` they follow."""

    def _last(self, parser, namespace, option_string):
        links = getattr(namespace, "links", None)
        if not links:
            parser.error(f"{option_string} must follow the --link it applies to")
        assert links  # parser.error() exits; narrows the Any | None for mypy
        return links[-1]


class ViaAtAction(_ModifierAction):
    """``--via-at x,y``: force a waypoint via on the preceding ``--link``."""

    def __call__(self, parser, namespace, values, option_string=None):
        link = self._last(parser, namespace, option_string)
        try:
            link.via_at = _parse_xy(values)
        except LinkSpecError as exc:
            parser.error(f"{option_string}: {exc}")


class LinkLayersAction(_ModifierAction):
    """``--link-layers F.Cu[,B.Cu]``: restrict the preceding ``--link``'s layers."""

    def __call__(self, parser, namespace, values, option_string=None):
        link = self._last(parser, namespace, option_string)
        layers = [v for v in values.split(",") if v]
        if not layers:
            parser.error(f"{option_string}: expected a comma-separated layer list")
        link.layers = layers


class NetClearanceAction(argparse.Action):
    """``--net-clearance NET=MM``: scoped to the preceding ``--link`` (Issue #8546).

    Before any ``--link`` it stays run-global (applies to every link, as it did
    before per-link scoping); after one it constrains only that link.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        name, eq, value = values.rpartition("=")
        if not eq or not name:
            parser.error(f"{option_string}: expected NET=MM, got {values!r}")
        try:
            float(value)
        except ValueError:
            parser.error(f"{option_string}: MM is not a number in {values!r}")
        links = getattr(namespace, "links", None)
        if links:
            links[-1].net_clearance.append(values)
        else:
            glob = list(getattr(namespace, self.dest, None) or [])
            glob.append(values)
            setattr(namespace, self.dest, glob)


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
    parser.add_argument(
        "--net-class-map",
        dest="net_class_map",
        default=None,
        help="Net-class sidecar JSON classifying the HV nets for the per-link creepage gate",
    )
    parser.add_argument(
        "--net-class",
        dest="net_class",
        default="HV",
        help="Net class naming the HV group for the creepage gate (default: HV)",
    )
    parser.add_argument(
        "--min",
        type=float,
        default=None,
        help=(
            "Required minimum creepage (mm) for the per-link creepage gate; "
            "without it the census gate is skipped"
        ),
    )
    parser.add_argument("-o", "--output", help="Write to this file instead of editing in place")
    parser.add_argument("--trace-width", type=float, default=0.2, help="Track width (mm)")
    parser.add_argument("--clearance", type=float, default=0.2, help="Clearance floor (mm)")
    parser.add_argument("--via-diameter", type=float, default=0.6, help="Via diameter (mm)")
    parser.add_argument("--via-drill", type=float, default=0.3, help="Via drill (mm)")
    parser.add_argument(
        "--net-clearance",
        action=NetClearanceAction,
        dest="net_clearance",
        default=[],
        metavar="NET=MM",
        help=(
            "Clearance the new copper must keep from NET (mm), e.g. an HV net. "
            "Repeatable; never below --clearance. Scope: after a --link it "
            "applies to that link only; before any --link it applies to every "
            "link. Combined with --voltage-map the larger floor wins."
        ),
    )
    parser.add_argument(
        "--voltage-map",
        dest="voltage_map",
        default=None,
        metavar="JSON",
        help=(
            "Per-net voltage sidecar (as for 'route'/'creepage'). Derives each "
            "link's per-neighbour clearance floor from the pairwise creepage "
            "requirement and makes the per-link creepage gate use the same "
            "requirements, waiving attach-zone pairs whose outside copper clears."
        ),
    )
    parser.add_argument(
        "--creepage-standard",
        dest="creepage_standard",
        default="iec60664",
        help="Creepage standard for --voltage-map (default: iec60664)",
    )
    parser.add_argument(
        "--pollution-degree",
        dest="pollution_degree",
        type=int,
        default=2,
        choices=[1, 2, 3],
        help="IEC pollution degree for --voltage-map (default: 2)",
    )
    parser.add_argument(
        "--material-group",
        dest="material_group",
        default="IIIa",
        choices=["I", "II", "IIIa", "IIIb"],
        help="Insulation material group for --voltage-map (default: IIIa)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )


def _parse_net_clearances(items: list[str], names: dict[str, int]) -> dict[int, float]:
    by_net: dict[int, float] = {}
    for item in items:
        name, eq, value = item.rpartition("=")
        if not eq or name not in names:
            raise LinkSpecError(f"--net-clearance {item!r}: expected KNOWN_NET=MM")
        try:
            by_net[names[name]] = float(value)
        except ValueError as exc:
            raise LinkSpecError(f"--net-clearance {item!r}: MM is not a number") from exc
    return by_net


def _rules_from_args(args: argparse.Namespace, pcb: Any) -> Any:
    """Run-wide rules; ``--net-clearance`` given before any ``--link`` is folded in."""
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
    return replace(base, clearance_by_net=_parse_net_clearances(args.net_clearance, names))


def _rules_for_link(args: argparse.Namespace, pcb: Any, base: Any, spec: LinkSpec) -> Any:
    """``base`` plus this link's voltage-map-derived and ``--net-clearance`` floors.

    Per neighbour net the larger of the run-global, voltage-map and link-scoped
    floors wins.  Returns ``base`` itself when nothing applies (no-flag path
    unchanged).
    """
    required = getattr(args, "_pairwise_required", None)
    voltages = getattr(args, "_pairwise_voltages", None)
    if voltages is None and not spec.net_clearance:
        return base
    names = {net.name: num for num, net in pcb.nets.items() if getattr(net, "name", None)}
    floors: dict[int, float] = dict(base.clearance_by_net or {})

    def raise_floor(num: int, mm: float) -> None:
        floors[num] = max(floors.get(num, 0.0), mm)

    if voltages is not None:
        from kicad_tools.creepage.standards import StandardLookupError
        from kicad_tools.placement.hv_domains import build_required_by_domain_pair
        from kicad_tools.router.pairwise_clearance import _norm_net_key

        threshold = getattr(args, "hv_threshold", 30.0)
        me = _norm_net_key(spec.net)
        for name, num in names.items():
            other = _norm_net_key(name)
            if other == me:
                continue
            mm = (required or {}).get(tuple(sorted((me, other))))
            if mm is None and (me not in voltages or other not in voltages):
                # A net absent from the map is 0 V, as in the creepage census
                # (compute_creepage_census); fail closed on the same pair the gate
                # checks instead of silently falling back to --clearance (#8546).
                dv = abs(voltages.get(me, 0.0) - voltages.get(other, 0.0))
                if dv == 0.0 or dv < threshold:
                    continue
                try:
                    mm = build_required_by_domain_pair(
                        {me: voltages.get(me, 0.0), other: voltages.get(other, 0.0)},
                        standard_id=getattr(args, "creepage_standard", "iec60664"),
                        pollution_degree=getattr(args, "pollution_degree", 2),
                        material_group=getattr(args, "material_group", "IIIa"),
                        hv_threshold=threshold,
                    ).get(tuple(sorted((me, other))))
                except StandardLookupError as exc:
                    raise LinkSpecError(
                        f"--voltage-map creepage lookup failed for {spec.net!r} vs {name!r}: {exc}"
                    ) from exc
            if mm:
                raise_floor(num, float(mm))
    for num, mm in _parse_net_clearances(spec.net_clearance, names).items():
        raise_floor(num, mm)
    return replace(base, clearance_by_net=floors)


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
    from shapely.geometry import LineString, Point  # type: ignore[import-untyped]

    from kicad_tools.router.link_router import _Item

    for x1, y1, x2, y2, layer in route.segments:
        line = LineString([(x1, y1), (x2, y2)]) if (x1, y1) != (x2, y2) else Point(x1, y1)
        model.items.append(_Item(net_number, {layer: line.buffer(route.width / 2)}))
    for vx, vy in route.vias:
        disk = Point(vx, vy).buffer(route.via_size / 2)
        model.items.append(_Item(net_number, dict.fromkeys(model.copper, disk)))
        model.drills.append((vx, vy, route.via_drill))


def _route_one(
    args: argparse.Namespace,
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

    rules = _rules_for_link(args, pcb, rules, spec)
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


def _drc_finding_counts(pcb_path: Path) -> Any:
    """Counter of ``(type, severity, items)`` DRC findings for ``pcb_path``.

    Runs kicad-cli on the pinned evidence toolchain (the caller holds
    :func:`evidence_toolchain`).  Raises ``RuntimeError`` when DRC cannot run.
    """
    import json
    import tempfile
    from collections import Counter

    from .kicad_toolchain import KiCadEnvironmentError, require_library_environment
    from .runner import run_drc

    with tempfile.TemporaryDirectory(prefix="kct-route-link-") as tmp:
        report = Path(tmp) / "drc.json"
        # Refill zones (in-memory; the board file is not saved) so a new via is
        # checked against fresh plane copper, matching the sign-off convention.
        result = run_drc(pcb_path, report, format="json", schematic_parity=False, refill_zones=True)
        if not result.success or not report.is_file():
            raise RuntimeError((result.stderr or "kicad-cli DRC failed").strip())
        data = json.loads(report.read_text())
    try:
        require_library_environment(data, source=f"native DRC of {pcb_path.name}")
    except KiCadEnvironmentError as exc:
        raise RuntimeError(str(exc)) from exc
    counts: Any = Counter()
    for v in data.get("violations", []):
        if v.get("severity") not in ("error", "warning"):
            continue
        if v.get("type") == "unconnected_items":
            # A link cannot create a connectivity error; its description moves with
            # the ratsnest anchor, which would cause false rollbacks.
            continue
        items = sorted(str(i.get("description", "")) for i in v.get("items", []))
        counts[(v.get("type"), v.get("severity"), tuple(items))] += 1
    return counts


def _map_inputs(args: argparse.Namespace) -> dict[str, Any] | None:
    """``--voltage-map`` census inputs (voltage map, standard, pair bounds), or ``None``."""
    vm = getattr(args, "voltage_map", None)
    if not vm:
        return None
    import json

    from kicad_tools.creepage.engine import pair_bounds_from_dict, voltage_map_from_dict
    from kicad_tools.creepage.standards import get_standard

    raw = json.loads(Path(vm).read_text())
    voltage_map, edge_voltage = voltage_map_from_dict(raw)
    return {
        "voltage_map": voltage_map,
        "edge_voltage": edge_voltage,
        "pair_bounds": pair_bounds_from_dict(raw) or None,
        "standard_obj": get_standard(getattr(args, "creepage_standard", "iec60664")),
    }


def _hv_nets_for(pcb: Any, pcb_path: Path, args: argparse.Namespace) -> Any:
    from kicad_tools.creepage.engine import resolve_hv_nets

    net_class_map = None
    ncm = getattr(args, "net_class_map", None)
    if ncm:
        from kicad_tools.router.rules import net_class_map_from_path

        net_class_map = net_class_map_from_path(Path(ncm), pcb_path=pcb_path)
    net_class = getattr(args, "net_class", "HV") or "HV"
    mi = _map_inputs(args)
    if mi is None:
        return resolve_hv_nets(pcb, net_class, net_class_map)
    # Same membership rule as ``kct creepage --voltage-map`` (#4401).
    return resolve_hv_nets(
        pcb,
        net_class,
        net_class_map,
        voltage_map=mi["voltage_map"],
        edge_voltage=mi["edge_voltage"],
        census_threshold=getattr(args, "hv_threshold", 30.0),
    )


def _gate_enabled(args: argparse.Namespace) -> bool:
    return getattr(args, "min", None) is not None or bool(getattr(args, "voltage_map", None))


def _census_mode(pcb: Any, pcb_path: Path, args: argparse.Namespace) -> str:
    """``skipped`` (no ``--min``/``--voltage-map``), ``vacuous`` (no HV nets) or ``checked``."""
    if not _gate_enabled(args):
        return "skipped"
    return "checked" if _hv_nets_for(pcb, pcb_path, args) else "vacuous"


def _census_failures(
    pcb_path: Path, args: argparse.Namespace
) -> dict[tuple[str, str, str, str], float] | None:
    """Failing creepage pairs, ``(net_a, net_b, kind, relationship) -> min creepage``.

    ``relationship`` is part of the key so a new board-level failure is not hidden by
    an already-failing same-footprint pair; the value lets the gate catch a pair that
    got worse.  ``None`` if no census is configured.

    With ``--voltage-map`` the requirement is derived per pair (as in
    ``kct creepage --voltage-map``) and attach-zone pairs whose copper outside
    the zone still clears are waived, so the gate agrees with the router (#8546).
    """
    if not _gate_enabled(args):
        return None
    from kicad_tools.creepage.engine import compute_creepage_census
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(pcb_path))
    net_class = getattr(args, "net_class", "HV") or "HV"
    hv_nets = _hv_nets_for(pcb, pcb_path, args)
    if not hv_nets:
        return {}
    min_mm = getattr(args, "min", None)
    mi = _map_inputs(args)
    extra: dict[str, Any] = {}
    if mi is not None:
        from kicad_tools.router.pairwise_clearance import build_attach_zones

        extra = {
            "standard": getattr(args, "creepage_standard", "iec60664"),
            "pollution_degree": getattr(args, "pollution_degree", 2),
            "material_group": getattr(args, "material_group", "IIIa"),
            "voltage_map": mi["voltage_map"],
            "standard_obj": mi["standard_obj"],
            "edge_voltage": mi["edge_voltage"],
            "attach_zones": build_attach_zones(pcb.footprints),
            "pair_bounds": mi["pair_bounds"],
        }
    report = compute_creepage_census(
        pcb,
        hv_nets,
        float(min_mm) if min_mm is not None else None,
        net_class=net_class,
        board=str(pcb_path),
        **extra,
    )
    failing: dict[tuple[str, str, str, str], float] = {}
    for p in report.pairs:
        if p.passed:
            continue
        if p.relationship == "attach_zone" and getattr(p, "residual_passed", False):
            continue  # waived: the copper outside the terminating footprint clears
        key = (p.net_a, p.net_b, p.kind, p.relationship)
        failing[key] = min(p.creepage_mm, failing.get(key, p.creepage_mm))
    return failing


def _new_census_failures(
    census: dict[tuple[str, str, str, str], float],
    base: dict[tuple[str, str, str, str], float],
) -> list[tuple[str, str, str, str]]:
    """Failing pairs absent from ``base``, or already failing and now worse."""
    return sorted(k for k, v in census.items() if k not in base or v < base[k] - 1e-6)


def _gate_state(
    pcb_path: Path, args: argparse.Namespace
) -> tuple[Any, dict[tuple[str, str, str, str], float] | None]:
    return _drc_finding_counts(pcb_path), _census_failures(pcb_path, args)


def _refuse(as_json: bool, message: str) -> int:
    """Exit 2: the gate cannot run, so nothing is (or stays) written."""
    if as_json:
        emit_json({"command": "route-link", "ok": False, "refused": True, "error": message})
    else:
        print(f"route-link: REFUSED: {message}", file=sys.stderr)
    return 2


def _commit_links(
    args: argparse.Namespace,
    links: list[LinkSpec],
    pcb: Any,
    model: Any,
    rules: Any,
    origin: tuple[float, float],
    net_ids: dict[str, int],
    pcb_path: Path,
    out_path: Path,
    entries: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """Commit ``links`` one at a time with a DRC + creepage gate per link.

    Returns ``(written path | None, refusal message | None)``.
    """
    from kicad_tools.router.link_router import append_link_routes

    created = out_path != pcb_path and not out_path.exists()
    if out_path != pcb_path:
        out_path.write_bytes(pcb_path.read_bytes())
    try:
        base_drc, base_census = _gate_state(out_path, args)
    except Exception as exc:  # the gate cannot run: refuse, leave nothing behind
        if created:
            out_path.unlink(missing_ok=True)
        return None, f"cannot establish the DRC/creepage baseline: {exc}"

    n_committed = 0
    for spec in links:
        entry, route = _route_one(args, pcb, model, rules, spec, origin, net_ids)
        entry["status"] = "unroutable"
        entry["failed_check"] = None
        entries.append(entry)
        if route is None:
            continue
        snapshot = out_path.read_bytes()
        failed: str | None = None
        reason = ""
        try:
            append_link_routes(out_path, [route], origin)
            drc, census = _gate_state(out_path, args)
            new_drc = drc - base_drc
            if new_drc:
                failed = "drc"
                reason = f"{sum(new_drc.values())} new DRC violation(s): " + ", ".join(
                    sorted({k[0] or "?" for k in new_drc})
                )
                detail = [
                    f"{k[0] or '?'}: " + "; ".join(k[2])
                    for k in sorted(new_drc, key=lambda k: (k[0] or "", k[2]))[:3]
                ]
                if detail:
                    reason += " [" + " | ".join(detail) + "]"
            elif census is not None and base_census is not None:
                new_pairs = _new_census_failures(census, base_census)
                if new_pairs:
                    failed = "creepage"
                    reason = f"{len(new_pairs)} new/worse failing creepage pair(s): " + ", ".join(
                        f"{a}/{b} ({rel})" for a, b, _k, rel in new_pairs
                    )
        except Exception as exc:
            failed = "gate-error"
            reason = f"gate could not run: {exc}"
        if failed is not None:
            out_path.write_bytes(snapshot)
            entry.update(routable=False, status="rolled-back", failed_check=failed, reason=reason)
            continue
        entry["status"] = "committed"
        base_drc, base_census = drc, census
        _commit_to_model(model, route, net_ids[spec.net])
        n_committed += 1
    if not n_committed and created:
        out_path.unlink(missing_ok=True)  # nothing committed: leave no unchanged copy
    return (str(out_path) if n_committed else None), None


def run_route_link_command(args: argparse.Namespace) -> int:
    """Entry point for ``kct route-link``.

    Exit 1 if any link is unroutable or rolled back; 2 if the gate cannot run.
    """
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
        from kicad_tools.router.link_router import _build_model
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(pcb_path))
        origin = pcb.board_origin
        from .route_cmd import _preload_voltage_map

        if _preload_voltage_map(args) != 0:
            return 1
        try:
            rules = _rules_from_args(args, pcb)
            for spec in links:  # validate link-scoped --net-clearance up front
                _rules_for_link(args, pcb, rules, spec)
        except LinkSpecError as exc:
            return fail(str(exc))
        model = _build_model(pcb)
        net_ids = {net.name: num for num, net in pcb.nets.items() if net.name}

        census_mode = _census_mode(pcb, pcb_path, args)
        entries: list[dict[str, Any]] = []
        written: str | None = None
        refused: str | None = None
        if args.probe:
            for spec in links:
                entry, route = _route_one(args, pcb, model, rules, spec, origin, net_ids)
                entry["status"] = "probed"
                entry["failed_check"] = None
                entries.append(entry)
                if route is not None:
                    # Later probes must see this copper.
                    _commit_to_model(model, route, net_ids[spec.net])
        else:
            from .kicad_toolchain import KiCadEnvironmentError, evidence_toolchain

            out_path = Path(args.output) if args.output else pcb_path
            try:
                with evidence_toolchain(mounts=[out_path.resolve().parent]):
                    written, refused = _commit_links(
                        args, links, pcb, model, rules, origin, net_ids, pcb_path, out_path, entries
                    )
            except KiCadEnvironmentError as exc:
                refused = f"KiCad environment (Issue #6962): {exc}"

    if refused is not None:
        return _refuse(as_json, refused)

    ok = all(e["routable"] and e["status"] != "rolled-back" for e in entries)
    if as_json:
        emit_json(
            {
                "command": "route-link",
                "ok": ok,
                "probe": bool(args.probe),
                "written": written,
                "census": census_mode,
                "links": entries,
            }
        )
    else:
        for e in entries:
            if e["status"] == "rolled-back":
                print(f"  ROLLED BACK: {e['link']}: failed {e['failed_check']}: {e['reason']}")
            elif e["routable"]:
                verb = "routable" if args.probe else "committed"
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
        if not args.probe:
            print(f"route-link: creepage census: {census_mode}")
        if written:
            print(
                f"route-link: wrote {sum(e['status'] == 'committed' for e in entries)} link(s) to {written}"
            )
        elif args.probe:
            print("route-link: probe only; board not written")
    return 0 if ok else 1
