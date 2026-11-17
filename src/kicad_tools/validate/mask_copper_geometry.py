"""Immutable full-context native object attribution for mask exposure checking."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .gerber_geometry import repair_self_touching_polygon
from .mask_export_geometry import MaskExportOptions, _inventory, inspect_exported_mask_geometry
from .mask_geometry import _validate_structure

#: KiCad versions whose native plot profile has been qualified against this
#: repository's independent material oracle. Each entry was admitted by running
#: ``tests/test_mask_copper_native.py`` end to end under that exact KiCad build
#: (native ``pcbnew`` object plotting compared against Gerbonara's reading of
#: ``kicad-cli``-plotted Gerbers, 5 nm tolerance) and confirming the whole suite
#: passes with no skips -- see ``scripts/ci/check_mask_copper_native.py``, which
#: imports this same set so the CI gate and the library check can never drift
#: apart. Do not add a version here without repeating that comparison; an
#: unqualified build must keep failing loudly rather than silently attributing
#: mask geometry from an unverified plot profile.
QUALIFIED_NATIVE_VERSIONS = ("10.0.5", "10.0.6")

#: Custom-rule constraint names this checker may reason about during native
#: object attribution. Module-level so the non-native suite can pin membership
#: directly (tests/test_mask_copper.py); admission is deliberate, argued, and
#: parity-tested, never derived from what the ``.kicad_dru`` generator happens
#: to emit. Every entry is a minimum/maximum a DRC run *measures* against
#: already plotted material; none of them is an input to the plot itself, so
#: native mask/copper object geometry is identical with and without the rule
#: present. Constraints that do feed the plot -- zone fill shaping
#: (``zone_connection``, ``thermal_relief_gap``, ``thermal_spoke_width``,
#: ``min_resolved_spokes``) above all -- must stay out, and so must any
#: constraint whose native context has not actually been exercised by a
#: parity test. Do not widen this set to quiet a failing board.
SAFE_RULE_CONSTRAINTS = frozenset(
    {
        "clearance",
        "physical_clearance",
        "edge_clearance",
        "hole_clearance",
        "hole_to_hole",
        # Measured hole-to-edge minimum over already-plotted holes and outline; no mask/copper input (#8948).
        "physical_hole_clearance",
        "track_width",
        "hole_size",
        "via_diameter",
        "annular_width",
        # Silkscreen-to-object minimum, the rule ordinary factory
        # ``.kicad_dru`` files ship (#5818). It is geometry-neutral for
        # this checker twice over: it constrains silkscreen, which is
        # neither a mask nor a copper layer, and it is a measured
        # minimum rather than a plot input. Parity is pinned by
        # tests/test_mask_copper_native.py.
        "silk_clearance",
        # Silkscreen text minima, silkscreen-scoped by their own conditions; silk_clearance's double argument applies verbatim (#8948).
        "text_thickness",
        "text_height",
        # Measured minimum intra-pair gap the router laid each coupled pair
        # at (#6443), one rule per pair. A DRC measurement over
        # already-routed track material; the gap was an input to the router,
        # not to the plot, and the rule recording it changes nothing the
        # plotter emits. Parity-pinned by tests/test_mask_copper_native.py
        # (#8973).
        "diff_pair_gap",
        # Pair-quality maxima the protocol table or the net class resolves
        # (#6443): uncoupled length and skew are measured over the lengths
        # of already-plotted track, and via_count counts vias that already
        # exist. None of the three is a plot input. ``skew`` is emitted both
        # bare and with ``(within_diff_pairs)``; both forms measure the same
        # plotted lengths. Parity-pinned by tests/test_mask_copper_native.py
        # (#8973).
        "diff_pair_uncoupled",
        "skew",
        "via_count",
        # A keepout prohibition (#6039), argued on its own terms: unlike
        # every other entry it is not a dimensional minimum at all but a ban
        # on item PRESENCE inside a referenced rule area. DRC reports items
        # of the listed kinds there; the rule never moves, resizes, removes
        # or adds any plotted object, so the plot is identical with or
        # without it. Parity-pinned by tests/test_mask_copper_native.py
        # (#8973).
        "disallow",
    }
)


@dataclass
class AttributedMaskGeometry:
    exported: Any
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)


def _finite_mm(node, index):
    """A finite number at *index* of an s-expression node, or None.

    ``.3`` (no leading zero) parses as a string atom and ``0.3`` as a float, so
    the value is read through its string form.
    """
    value = node.get_string(index) if node is not None else None
    try:
        parsed = float(value) if value is not None else None
    except ValueError:
        return None
    return parsed if parsed is not None and math.isfinite(parsed) else None


def _positive_mm(node, index):
    value = _finite_mm(node, index)
    return value if value is not None and value > 0 else None


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


#: The native per-object worker needs pcbnew only -- unlike the selective
#: zone-fill probe, it uses neither wx nor the fill API. A candidate whose
#: KiCad build is unqualified is still rejected later by the
#: ``QUALIFIED_NATIVE_VERSIONS`` binding, so the weak probe never silently
#: widens what a report can claim (#8946).
_PCBNEW_PROBE = "import pcbnew"


def _resolve_native_python_command(
    native_python_command: list[str] | None, stage: Path
) -> tuple[list[str] | None, str | None]:
    """Resolve the interpreter argv for the native per-object worker.

    Priority: an explicit ``native_python_command`` wins; then, when the
    Docker fallback is enabled and docker is available, the pinned image's
    ``python3`` (which imports pcbnew) with *stage* bind-mounted, so the
    staged worker, board, request and output are all visible to the
    container; then host discovery of an interpreter that passes
    ``_PCBNEW_PROBE``. ``/usr/bin/python3`` is therefore used only when it
    really imports pcbnew, never as a blind fallback.

    Returns ``(argv, None)`` on success, or ``(None, reason)`` naming what to
    install or configure.
    """
    if native_python_command:
        return list(native_python_command), None
    from kicad_tools.cli.kicad_toolchain import (
        docker_fallback_enabled,
        docker_kicad_cli_prefix,
        process_config_home,
    )
    from kicad_tools.zones.placement_fill import find_kicad_python

    if docker_fallback_enabled() and shutil.which("docker"):
        return docker_kicad_cli_prefix([stage], process_config_home(), entrypoint="python3"), None
    found = find_kicad_python(probe=_PCBNEW_PROBE)
    if found is not None:
        return [str(found)], None
    return None, (
        "Native per-object geometry unavailable: no Python interpreter with "
        "pcbnew was found. Install KiCad (its bundled python3 provides "
        "pcbnew), set KCT_KICAD_DOCKER=1 to run the worker in the pinned "
        "kicad/kicad image (docker required), or set an explicit "
        "native_python_command in the kct.mask-copper-request.v1 request."
    )


def unqualified_native_version_reason(native_version: str) -> str:
    """Refusal for a KiCad build outside ``QUALIFIED_NATIVE_VERSIONS``, with a way forward.

    The refusal itself is unchanged -- an unqualified plot profile must keep
    failing loudly -- but, like the no-interpreter reason (#8946), it names
    what to do instead of only what went wrong (#9009).
    """
    return (
        "Native object attribution requires a qualified KiCad plot profile "
        f"({', '.join(QUALIFIED_NATIVE_VERSIONS)}); got "
        f"{native_version or 'no version'}. Set KCT_KICAD_DOCKER=1 to run both "
        "native passes in the pinned kicad/kicad image (docker required), or "
        "install a qualified KiCad release."
    )


def inspect_attributed_mask_geometry(
    path,
    *,
    options=None,
    native_command=None,
    native_python_command=None,
    artifact_dir=None,
    scratch_dir=None,
):
    """Capture one source set and read UUID polygons without editing that board.

    KiCad's object shape APIs retain overlapping contributors independently.
    Full-layer native export parity is an additional coverage check, never an
    inference of an object's ownership from subtraction of a copper union.
    """
    from shapely.geometry import Polygon  # type: ignore[import-untyped]
    from shapely.ops import unary_union  # type: ignore[import-untyped]

    from kicad_tools.sexp import parse_string

    path = Path(path)
    captured = {
        suffix: path.with_suffix(suffix).read_bytes() if path.with_suffix(suffix).exists() else None
        for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru")
    }
    raw = captured[".kicad_pcb"]
    if raw is None:
        raise ValueError("Missing PCB source")
    _validate_structure(raw.decode())
    tree = parse_string(raw.decode())
    inventory = _inventory(tree)
    # Parent occurrences participate in source identity too. A pad UUID must
    # not alias its footprint (or any other authored UUID), even though the
    # footprint itself contributes no standalone copper polygon.
    import uuid

    seen_identities = set()
    for uuid_field in tree.find_all("uuid"):
        value = uuid_field.get_string(0)
        if not isinstance(value, str):
            raise ValueError("Missing source UUID value")
        try:
            canonical = str(uuid.UUID(value))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Malformed source UUID") from exc
        if canonical != value or canonical in seen_identities:
            raise ValueError("Noncanonical or duplicate source UUID: " + value)
        seen_identities.add(canonical)
    for feature in inventory:
        if not feature["source_uuid"]:
            raise ValueError("Missing plotted source UUID")
    for footprint in tree.find_all("footprint") + tree.find_all("module"):
        if footprint.find_child("uuid") is None:
            raise ValueError("Missing footprint occurrence UUID")
    options = options or MaskExportOptions(standalone_sources=False)
    # Separate standalone derivatives are unnecessary: attribution is native
    # original-context object geometry, including all overlapping neighbors.
    from dataclasses import replace

    options = replace(options, standalone_sources=False)
    with tempfile.TemporaryDirectory(prefix="kct-mask-objects-", dir=scratch_dir) as temp:
        stage = Path(temp)
        board = stage / path.name
        for suffix, content in captured.items():
            if content is not None:
                board.with_suffix(suffix).write_bytes(content)
        exported = inspect_exported_mask_geometry(
            board,
            options=options,
            native_command=native_command,
            artifact_dir=artifact_dir,
            scratch_dir=scratch_dir,
        )
        result = AttributedMaskGeometry(exported)
        # Native export stderr is classified once, inside
        # inspect_exported_mask_geometry, so this caller inherits its
        # geometry-neutral stderr allowlist (#5707) rather than repeating it.
        # The separate worker launch below judges returncode and output
        # presence only -- it never word-matches stderr -- so it has no
        # equivalent misclassification to fix.
        result.errors.extend(f"{e['feature']}: {e['reason']}" for e in exported.unsupported)
        # Native pcbnew LoadBoard has no exposed general DRC-expression loader.
        # Anything outside SAFE_RULE_CONSTRAINTS is an explicit context gap,
        # even if a whole-layer union happens to agree; the set's admission
        # criterion lives with the constant.
        if captured[".kicad_dru"]:
            rule_tree = parse_string("(rules " + captured[".kicad_dru"].decode() + ")")
            for constraint in rule_tree.find_all("constraint"):
                if constraint.get_string(0) not in SAFE_RULE_CONSTRAINTS:
                    result.errors.append(
                        "Native object rule context unsupported: " + str(constraint.get_string(0))
                    )
        # LoadBoard does not expose the CLI project/--define-var text resolver.
        # Fail visibly for variable-bearing plotted text rather than use a
        # substituted or context-free glyph and rely on an overlapping union.
        for feature in inventory:
            if feature["kind"] in ("property", "fp_text", "gr_text", "fp_text_box", "gr_text_box"):
                atoms = feature["node"].get_atoms()
                if any(isinstance(v, str) and "${" in v for v in atoms):
                    result.errors.append(
                        feature["source_uuid"]
                        + ": native plotted text-variable context unsupported"
                    )
        parents = {}
        for parent in tree.find_all("footprint") + tree.find_all("module"):
            field = parent.find_child("uuid")
            if field:
                parents[field.get_string(0)] = parent
        setup = tree.find_child("setup")
        board_margin = setup.find_child("pad_to_mask_clearance") if setup else None
        if options.board_plot_params:
            params = setup.find_child("pcbplotparams") if setup else None
            safe_plot_fields = {
                "disableapertmacros",
                "usegerberextensions",
                "usegerberattributes",
                "usegerberadvancedattributes",
                "creategerberjobfile",
                "outputformat",
                "outputdirectory",
                "gerberprecision",
            }
            for setting in params.children if params else []:
                if setting.name not in safe_plot_fields:
                    if setting.name in ("useauxorigin", "mirror") and setting.get_string(0) in (
                        "false",
                        "no",
                    ):
                        continue
                    result.errors.append(
                        "Native object plot-coordinate/context option unsupported: "
                        + str(setting.name)
                    )
        # Source selectors that can change shape in a way the object API does
        # not model must not be accepted just because another object hides it.
        for feature in inventory:
            stroke = feature["node"].find_child("stroke")
            style = stroke.find_child("type") if stroke else None
            if style and style.get_string(0) not in ("default", "solid"):
                result.errors.append(
                    feature["source_uuid"] + ": dashed native graphic attribution unsupported"
                )
        worker = Path(__file__).with_name("_native_mask_objects.py")
        staged_worker = stage / worker.name
        worker_raw = worker.read_bytes()
        staged_worker.write_bytes(worker_raw)
        request = stage / "request.json"
        request.write_text(json.dumps({"source_uuids": [f["source_uuid"] for f in inventory]}))
        output = stage / "objects.json"
        command, unavailable = _resolve_native_python_command(native_python_command, stage)
        if command is None:
            assert unavailable is not None
            result.errors.append(unavailable)
            return result
        process = subprocess.run(
            [*command, str(staged_worker), str(board), str(request), str(output)],
            capture_output=True,
            text=True,
            timeout=options.timeout_seconds,
        )
        if process.returncode or not output.exists():
            result.errors.append(
                "Native per-object geometry unavailable: " + process.stderr.strip()
            )
            return result
        data = _read_worker_output(output.read_text(), {f["source_uuid"] for f in inventory})
        if data.get("source_sha256") != exported.source_sha256:
            result.errors.append("Native object/source binding mismatch")
            return result
        native_version = str(data.get("native_version", "")).split()
        if not native_version or native_version[0] not in QUALIFIED_NATIVE_VERSIONS:
            result.errors.append(unqualified_native_version_reason(data.get("native_version", "")))
            return result
        if str(data["native_version"]).split()[0] != exported.native_version.split()[0]:
            result.errors.append("Native object/export versions differ")
        result.errors.extend(data["errors"])
        from kicad_tools.cli.kicad_toolchain import portable_native_command

        # Recorded form is host-independent (#9055): the docker prefix keeps
        # only the image and entrypoint, other commands only the basename.
        # The executed argv (*command*) is untouched above.
        result.provenance = {
            "worker_sha256": _digest(worker_raw),
            "native_python_command": portable_native_command(command),
            "native_version": data["native_version"],
            "objects_sha256": _digest(output.read_bytes()),
            "max_error_mm_per_construction": data["max_error_mm"],
            "attribution": "native full-board object UUID; no neighbor removal",
        }
        for feature in inventory:
            identity = feature["source_uuid"]
            item = data["objects"].get(identity)
            if item is None:
                continue
            geometries = {}
            for layer, regions in item["layers"].items():
                polygons = [Polygon(region["shell"], region["holes"]) for region in regions]
                # Zone fills come back from KiCad fractured: each hole is
                # joined to the outline by a zero-width bridge, so the ring
                # self-touches and shapely calls it invalid even though its
                # area is unambiguous. Repair those exactly as the Gerber
                # region reader does (area must survive), and still refuse
                # anything the repair cannot account for (#9009).
                polygons = [p if p.is_valid else repair_self_touching_polygon(p) for p in polygons]
                if any(p is None for p in polygons):
                    result.errors.append(f"{identity}/{layer}: invalid native object polygon")
                    continue
                geometries[layer] = unary_union(polygons)
            own_margin = feature["node"].find_child("solder_mask_margin")
            parent_node = parents.get(feature["parent_uuid"])
            parent_margin = parent_node.find_child("solder_mask_margin") if parent_node else None
            if own_margin is not None:
                margin_source = (
                    "object:" + identity + ":solder_mask_margin=" + str(own_margin.get_value(0))
                )
            elif parent_margin is not None:
                margin_source = (
                    "footprint:"
                    + feature["parent_uuid"]
                    + ":solder_mask_margin="
                    + str(parent_margin.get_value(0))
                )
            elif board_margin is not None:
                margin_source = "board:pad_to_mask_clearance=" + str(board_margin.get_value(0))
            else:
                margin_source = "native-default (no explicit object/footprint/board margin)"
            # Source identity for triage messages (issue #9038): the owning
            # footprint's Reference and the pad number, so a finding can say
            # `pad:R1/1` instead of an opaque UUID. Additive to the internal
            # object dict; the report schema does not carry it.
            pad_ref = None
            if feature["kind"].startswith("pad:"):
                number = feature["node"].get_string(0)
                reference = None
                if parent_node is not None:
                    reference = next(
                        (
                            child.get_string(1)
                            for child in parent_node.iter_children()
                            if child.name == "property" and child.get_string(0) == "Reference"
                        ),
                        None,
                    )
                if reference and number:
                    pad_ref = f"{reference}/{number}"
            # Authored via drill (issue #8858). The native via copper is a solid
            # disk, so the hole is not in its geometry; the within-own-pad
            # classification needs it to keep a via-in-pad (drill inside the
            # opening) out of that class. None when absent or unreadable.
            drill_mm = None
            if feature["kind"] == "via":
                drill_mm = _positive_mm(feature["node"].find_child("drill"), 0)
            # Authored straight-track centreline and width (issue #8858): the
            # within-own-pad class grants a track one pad-edge crossing, which
            # needs to know where the track ends and how wide it is. The native
            # copper is a capsule that does not carry either. None when absent.
            width_mm = None
            endpoints_mm = None
            if feature["kind"] == "segment":
                width_mm = _positive_mm(feature["node"].find_child("width"), 0)
                ends = []
                for tag in ("start", "end"):
                    child = feature["node"].find_child(tag)
                    x = _finite_mm(child, 0)
                    y = _finite_mm(child, 1)
                    if x is not None and y is not None:
                        ends.append((x, y))
                endpoints_mm = tuple(ends) if len(ends) == 2 else None
            result.objects[identity] = {
                **{k: v for k, v in feature.items() if k != "node"},
                "net": item["net"],
                "layers_geometry": geometries,
                "margin_mm": item["margin_mm"],
                "margin_source": margin_source,
                "pad_ref": pad_ref,
                "drill_mm": drill_mm,
                "width_mm": width_mm,
                "endpoints_mm": endpoints_mm,
            }
        # Verify source and all sidecar bytes (including their absence) after
        # both native operations. The author files themselves were never passed.
        for suffix, content in captured.items():
            sibling = board.with_suffix(suffix)
            if (sibling.read_bytes() if sibling.exists() else None) != content:
                result.errors.append("Native operation mutated captured source: " + suffix)
        for suffix, content in captured.items():
            current = path.with_suffix(suffix)
            if (current.read_bytes() if current.exists() else None) != content:
                result.errors.append("Original source set changed during inspection: " + suffix)
        if staged_worker.read_bytes() != worker_raw:
            result.errors.append("Native worker bytes changed during invocation")
        if artifact_dir:
            destination = Path(artifact_dir)
            shutil.copy2(output, destination / "native-objects.json")
            (destination / "object-attribution.json").write_text(
                json.dumps(result.provenance, indent=2)
            )
        # A few nanometres cover native and Gerber polygon chord error plus
        # coordinate quantization. This is geometric uncertainty, not a rule waiver.
        budget = 0.000004
        for layer in ("F.Cu", "B.Cu"):
            if layer not in exported.layers:
                continue
            union = unary_union(
                [
                    o["layers_geometry"][layer]
                    for o in result.objects.values()
                    if layer in o["layers_geometry"]
                ]
            )
            native = exported.layers[layer]
            if (
                not union.difference(native.buffer(budget)).is_empty
                or not native.difference(union.buffer(budget)).is_empty
            ):
                result.errors.append(layer + ": native per-object/full-layer material mismatch")
        return result


def _read_worker_output(text, identities):
    """Validate external native-worker data before constructing any geometry."""
    import math

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate native JSON key: " + key)
            result[key] = value
        return result

    data = json.loads(text, object_pairs_hook=unique)
    if data["schema"] != "kct.native-mask-objects.v1":
        raise ValueError("Unsupported native worker schema/error budget")
    error = data["max_error_mm"]
    if (
        isinstance(error, bool)
        or not isinstance(error, (int, float))
        or not math.isfinite(error)
        or error < 0
    ):
        raise ValueError("Invalid native construction error")
    if not isinstance(data["errors"], list) or any(not isinstance(e, str) for e in data["errors"]):
        raise ValueError("Invalid native coverage errors")
    missing = identities - set(data["objects"])
    if set(data["objects"]) - identities:
        raise ValueError("Unrequested native object identity")
    if missing:
        # The worker already names an identity it could not resolve uniquely;
        # report each missing object once, not twice (#9009).
        reported = {e for e in data["errors"] if e.endswith(": missing/ambiguous native object")}
        data["errors"].extend(
            "Missing native object: " + identity
            for identity in sorted(missing)
            if f"{identity}: missing/ambiguous native object" not in reported
        )
    for identity, item in data["objects"].items():
        if not isinstance(item["net"], str) or not isinstance(item["kind"], str):
            raise ValueError("Invalid native object metadata")
        if set(item["layers"]) - {"F.Cu", "B.Cu", "F.Mask", "B.Mask"}:
            raise ValueError("Unknown native material layer")
        if set(item["margin_mm"]) - {"F.Mask", "B.Mask"}:
            raise ValueError("Unknown native mask margin side")
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            for v in item["margin_mm"].values()
        ):
            raise ValueError("Invalid native mask expansion")
        for regions in item["layers"].values():
            for region in regions:
                for ring in [region["shell"], *region["holes"]]:
                    if len(ring) < 3:
                        raise ValueError("Degenerate native polygon")
                    for point in ring:
                        if len(point) != 2 or any(
                            isinstance(v, bool)
                            or not isinstance(v, (int, float))
                            or not math.isfinite(v)
                            for v in point
                        ):
                            raise ValueError("Invalid native polygon coordinate")
    return data
