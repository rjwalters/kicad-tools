"""LCSC/EasyEDA fetch-on-demand 3D model resolver (fourth ``add-3d-models`` tier).

The JLCPCB-assembly fleet identifies its assembly parts by **LCSC C-numbers**,
and nearly every LCSC part carries a 3D STEP body in the EasyEDA parts
database.  This module resolves a footprint whose only usable identity is a
C-number to a cached ``.step`` file, fetching it on demand from EasyEDA when a
committed per-board sidecar maps its ``lib_id`` to a C-number.

**License posture.**  EasyEDA/LCSC STEP bodies are design-use-oriented and are
**not** explicitly redistributable, so the fetched models are cached locally
and never committed to the repo.  Committed ``.kicad_pcb`` files carry only a
portable path-variable ``(model "${KCT_LCSC_3D_DIR}/C#####.step" ...)`` ref
into that cache, resolved at render time (mirroring the
``${KICADn_3DMODEL_DIR}`` precedent).

**Offset policy.**  An EasyEDA STEP is a bare ``.step`` with no ``.kicad_mod``,
so there is no *source* footprint pad centroid to register against.  The bodies
are treated as **origin-authored**: the resolver returns
``source_anchor=(0.0, 0.0)`` (an explicit origin, *not* ``None``), so the
shared ``add_model_refs_to_text`` offset math computes ``dx, dy =
target_anchor`` -- the body's origin lands on the target footprint's pad
centroid.  This is an approximation (origin-centered placement, not
pin-1-registered).

**Orientation policy.**  There is likewise no source footprint to *derive* a
rotation from, and EasyEDA bodies are authored in whatever orientation the
part vendor chose, so the tier's default identity rotation is a guess that is
wrong for many parts.  Authored per-part corrections therefore come from two
places, resolved in ``models3d._resolve_lcsc`` (first non-``None`` wins,
merged per field): a per-board sidecar entry in **object form**
(:class:`LcscModelEntry`), then the packaged, C-number-keyed table in
:mod:`kicad_tools.pcb.lcsc_model_transforms`, then identity.  The one
exception to the per-field merge: a sidecar ``rotate`` may not silently
inherit a packaged ``offset`` calibrated under a different rotation — that
raises ``ValueError`` (#4636).  See ``docs/guides/lcsc-3d-models.md`` for the
frame semantics and the calibration recipe.

**Offline / CI safety.**  The fetch is opt-in.  A model resolves only when the
cache already holds the STEP, or when fetching is explicitly enabled (via the
``fetch`` flag / ``KCT_LCSC_FETCH`` env var).  Fetch and parse failures never
raise -- they degrade to ``None`` (reported as unresolved) so a patch or render
never fails for want of a body, and CI never needs network.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_CACHE_ENV_VAR",
    "LCSC_MODEL_PATH_VAR",
    "LcscModelEntry",
    "fetch_enabled",
    "load_lcsc_mapping",
    "lcsc_cache_dir",
    "resolve_lcsc_step",
    "synthesize_model_block",
]

# A model-frame (x, y, z) triple, as written into ``(offset ...)`` /
# ``(rotate ...)``.
Triple = tuple[float, float, float]

# Env var naming the on-disk LCSC STEP cache directory.  It doubles as the
# ``(model ...)`` path variable emitted into committed ``.kicad_pcb`` files
# (KiCad resolves ``${KCT_LCSC_3D_DIR}`` from the process environment at render
# time), mirroring the ``${KICADn_3DMODEL_DIR}`` mechanism.
DEFAULT_CACHE_ENV_VAR = "KCT_LCSC_3D_DIR"
LCSC_MODEL_PATH_VAR = "${KCT_LCSC_3D_DIR}"

# Env var that opts fetch-on-cache-miss in.  Absent/false => cache-only.
FETCH_ENV_VAR = "KCT_LCSC_FETCH"

# EasyEDA API surface (two plain HTTP GETs).  Endpoint URLs mirror
# ``easyeda2kicad``'s ``easyeda/easyeda_api.py`` on ``master``.  EasyEDA
# publishes no public API spec; if a fetch fails, re-check that upstream file
# for endpoint drift before debugging this client.
_API_COMPONENT_INFO = "https://easyeda.com/api/products/{lcsc_id}/components"
_API_STEP_MODEL = "https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/{uuid}"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT = 30

# --------------------------------------------------------------------------
# Untrusted-input validation
# --------------------------------------------------------------------------
# The C-number originates from a committed sidecar (``lcsc_models.json``) that
# must be treated as untrusted: it flows into a cache *filename*
# (``{lcsc_id}.step``), into a request *URL* (``.format(lcsc_id=...)``), and
# into a committed ``.kicad_pcb`` *model ref*.  An unvalidated value enables
# path traversal (``../outside/pwned``), URL injection (``C1/../../evil?x=``),
# and ref injection.  EasyEDA C-numbers are ``C`` followed by digits, so we pin
# the value to that shape at every trust boundary.
_LCSC_ID_RE = re.compile(r"^C\d+$")

# Bounds on a fetched STEP body written to the cache.  A compromised/rogue
# endpoint can return an arbitrarily large or non-STEP payload; cap the size
# and require the ISO-10303-21 header before ever writing to disk.  Real LCSC
# bodies are typically a few MB; 50 MB is generous but bounded.
_MAX_STEP_BYTES = 50 * 1024 * 1024
_STEP_MAGIC = b"ISO-10303-21"


def _is_valid_lcsc_id(lcsc_id: str) -> bool:
    """True when *lcsc_id* is a well-formed EasyEDA C-number (``C`` + digits)."""
    return bool(_LCSC_ID_RE.match(lcsc_id))


def lcsc_cache_dir() -> Path:
    """Return the LCSC STEP cache directory (honoring ``KCT_LCSC_3D_DIR``).

    Default: ``~/.cache/kicad-tools/lcsc-3d/``.  The directory is *not* created
    here; callers create it lazily on first write.
    """
    override = os.environ.get(DEFAULT_CACHE_ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / ".cache" / "kicad-tools" / "lcsc-3d"


def fetch_enabled(flag: bool = False) -> bool:
    """True when fetch-on-cache-miss is permitted.

    Enabled by an explicit *flag* (e.g. ``--fetch-lcsc``) or a truthy
    ``KCT_LCSC_FETCH`` env var (``1``/``true``/``yes``/``on``).  Default is
    cache-only (no network).
    """
    if flag:
        return True
    val = os.environ.get(FETCH_ENV_VAR, "").strip().lower()
    return val in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LcscModelEntry:
    """One resolved sidecar entry: a C-number plus optional per-part transforms.

    *rotate* and *offset* are model-frame triples (X as the footprint 2D X, Y
    negated versus footprint 2D Y, Z up from the board).  ``None`` means "this
    sidecar says nothing about that field", which lets the packaged
    :mod:`kicad_tools.pcb.lcsc_model_transforms` table supply it — the merge is
    per field, so a sidecar that overrides only ``offset`` does not suppress a
    packaged ``rotate``.

    The merge is per field in the other direction too, with one guard: because
    ``offset`` is applied *after* ``rotate``, a packaged ``offset`` is only
    meaningful in the frame of its sibling ``rotate``.  So a sidecar that
    overrides ``rotate`` to a *different* value while inheriting a packaged
    ``offset`` with non-zero invalidated components raises ``ValueError``
    rather than emitting a plausible-but-wrong body (#4636).  Restating
    ``offset`` here — ``[0, 0, 0]`` counts — always resolves it.
    """

    lcsc: str
    rotate: Triple | None = None
    offset: Triple | None = None


# Keys accepted in the sidecar's object form.  Anything else is a build error:
# a typo'd ``"rotation"`` must not degrade to a silent, green no-op -- that is
# precisely the failure mode the override mechanism exists to prevent.
_ENTRY_KEYS = frozenset({"lcsc", "rotate", "offset"})


def _parse_triple(path: Path, lib_id: str, field_name: str, value: object) -> Triple:
    """Validate an object-form ``rotate``/``offset`` array, or raise ``ValueError``."""
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(
            f"LCSC sidecar {path}: {field_name!r} for {lib_id!r} must be an array of "
            f"exactly 3 numbers, got {value!r}"
        )
    out: list[float] = []
    for component in value:
        # ``bool`` is an ``int`` subclass; a JSON ``true`` is not a coordinate.
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise ValueError(
                f"LCSC sidecar {path}: {field_name!r} for {lib_id!r} must contain only "
                f"numbers, got {component!r}"
            )
        as_float = float(component)
        if not math.isfinite(as_float):
            raise ValueError(
                f"LCSC sidecar {path}: {field_name!r} for {lib_id!r} must contain only "
                f"finite numbers, got {component!r}"
            )
        out.append(as_float)
    return (out[0], out[1], out[2])


def _parse_entry(path: Path, lib_id: str, value: object) -> LcscModelEntry:
    """Parse one sidecar value (bare C-number string, or the object form)."""
    if isinstance(value, str):
        c_number = value
        rotate: Triple | None = None
        offset: Triple | None = None
    elif isinstance(value, dict):
        unknown = sorted(set(value) - _ENTRY_KEYS)
        if unknown:
            raise ValueError(
                f"LCSC sidecar {path}: unknown key(s) {unknown} for {lib_id!r} "
                f"(expected any of {sorted(_ENTRY_KEYS)})"
            )
        raw_lcsc = value.get("lcsc")
        if not isinstance(raw_lcsc, str):
            raise ValueError(
                f"LCSC sidecar {path}: object form for {lib_id!r} must contain a "
                "string 'lcsc' C-number"
            )
        c_number = raw_lcsc
        rotate = (
            _parse_triple(path, lib_id, "rotate", value["rotate"]) if "rotate" in value else None
        )
        offset = (
            _parse_triple(path, lib_id, "offset", value["offset"]) if "offset" in value else None
        )
    else:
        raise ValueError(
            f"LCSC sidecar {path}: entry for {lib_id!r} must be a string C-number or an "
            "object with an 'lcsc' key"
        )
    # The C-number is untrusted and flows into a cache filename, a request
    # URL, and a committed board ref; a malformed value (path traversal,
    # URL injection, non-C string) is a build error, matching the
    # malformed-sidecar posture above.  Validated identically on both forms.
    if not _is_valid_lcsc_id(c_number):
        raise ValueError(
            f"LCSC sidecar {path}: invalid C-number {c_number!r} for {lib_id!r} "
            "(expected 'C' followed by digits, e.g. 'C50950')"
        )
    return LcscModelEntry(lcsc=c_number, rotate=rotate, offset=offset)


def load_lcsc_mapping(sidecar_path: Path | str) -> dict[str, LcscModelEntry]:
    """Load a ``lib_id -> C-number`` sidecar (``lcsc_models.json``).

    Two per-entry forms are accepted:

    * **bare string** -- ``{"Module:Joystick_Analog": "C50950"}``, meaning "use
      this C-number with whatever transform the packaged table supplies (or
      identity)";
    * **object** -- ``{"Connector:Part": {"lcsc": "C444929", "rotate": [0, 0,
      90], "offset": [0, 0, 0]}}``, a board-local override of the packaged
      :mod:`kicad_tools.pcb.lcsc_model_transforms` table.  ``rotate`` and
      ``offset`` are independently optional.

    Raises ``ValueError`` on a malformed file (not silently ignored -- a broken
    committed sidecar is a build error, distinct from a runtime network
    failure).  Unknown object keys are rejected rather than ignored, so a
    typo'd ``"rotation"`` fails loudly instead of silently doing nothing.
    """
    path = Path(sidecar_path)
    try:
        raw = json.loads(path.read_text())
    except OSError as e:
        raise ValueError(f"cannot read LCSC sidecar {path}: {e}") from e
    except json.JSONDecodeError as e:
        raise ValueError(f"malformed LCSC sidecar {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"LCSC sidecar {path} must be a JSON object of lib_id -> C-number")
    mapping: dict[str, LcscModelEntry] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValueError(f"LCSC sidecar {path}: entry keys must be string lib_ids")
        mapping[key] = _parse_entry(path, key, value)
    return mapping


# --------------------------------------------------------------------------
# Minimal in-repo EasyEDA fetch client (stdlib only; no ``easyeda2kicad`` dep)
# --------------------------------------------------------------------------


def _http_get(url: str) -> bytes | None:
    """GET *url* with a browser User-Agent; return body bytes or ``None``.

    Never raises -- any network/HTTP error degrades to ``None``.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310
            body: bytes = resp.read()
            return body
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


def _parse_3d_uuid(component_info: bytes) -> str | None:
    """Extract the 3D-model uuid from an EasyEDA component-info JSON body.

    The uuid lives in ``result.packageDetail.dataStr.shape`` as an
    ``SVGNODE~{json}`` line whose parsed JSON has ``attrs.uuid`` (falling back
    to a top-level ``uuid``).  Returns ``None`` when the shape carries no
    3D-model node.
    """
    try:
        doc = json.loads(component_info)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    result = doc.get("result")
    if not isinstance(result, dict):
        return None
    package_detail = result.get("packageDetail")
    if not isinstance(package_detail, dict):
        return None
    data_str = package_detail.get("dataStr")
    if not isinstance(data_str, dict):
        return None
    shape = data_str.get("shape")
    if not isinstance(shape, list):
        return None
    for entry in shape:
        if not isinstance(entry, str) or not entry.startswith("SVGNODE"):
            continue
        # Format: "SVGNODE~{json}" (tilde-delimited).
        _, _, payload = entry.partition("~")
        if not payload:
            continue
        try:
            node = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(node, dict):
            continue
        attrs = node.get("attrs")
        if isinstance(attrs, dict):
            uuid = attrs.get("uuid")
            if isinstance(uuid, str) and uuid:
                return uuid
        uuid = node.get("uuid")
        if isinstance(uuid, str) and uuid:
            return uuid
    return None


def _fetch_lcsc_step(lcsc_id: str) -> bytes | None:
    """Fetch raw STEP bytes for *lcsc_id* from EasyEDA, or ``None`` on failure.

    Two GETs: component-info (to extract the 3D-model uuid) then the STEP body.
    Never raises -- degrades to ``None`` on any network/parse failure.
    """
    info = _http_get(_API_COMPONENT_INFO.format(lcsc_id=lcsc_id))
    if info is None:
        return None
    uuid = _parse_3d_uuid(info)
    if not uuid:
        return None
    step = _http_get(_API_STEP_MODEL.format(uuid=uuid))
    if not step:
        return None
    # Bound the third-party payload before it can reach the disk: reject an
    # oversize body or anything that is not a real ISO-10303-21 STEP file.
    if len(step) > _MAX_STEP_BYTES:
        return None
    if not step.lstrip().startswith(_STEP_MAGIC):
        return None
    return step


# --------------------------------------------------------------------------
# Cache-aware resolution
# --------------------------------------------------------------------------


def resolve_lcsc_step(
    lcsc_id: str,
    *,
    cache_dir: Path | None = None,
    fetch: bool = False,
    warn: object = None,
) -> Path | None:
    """Return the cached STEP path for *lcsc_id*, fetching on demand if enabled.

    Cache hit -> returns the path with no network call.  Cache miss with
    fetching enabled -> fetches, writes ``{lcsc_id}.step`` into *cache_dir*, and
    returns it; a fetch failure warns (via *warn*, a ``callable(str)`` such as a
    logger) and returns ``None``.  Cache miss with fetching disabled -> returns
    ``None`` (no network).

    Args:
        lcsc_id: LCSC part number (e.g. ``"C50950"``).
        cache_dir: Cache directory (default: :func:`lcsc_cache_dir`).
        fetch: When True, fetch on a cache miss; when False, cache-only.
        warn: Optional ``callable(str)`` invoked on a fetch failure.
    """
    # Defense in depth: even though ``load_lcsc_mapping`` rejects a malformed
    # C-number at the sidecar boundary, never let an unvalidated value reach the
    # filesystem or network here.  The resolver path stays never-raise, so an
    # invalid id warns and returns None rather than raising.
    if not _is_valid_lcsc_id(lcsc_id):
        if callable(warn):
            warn(f"LCSC 3D model skipped: invalid C-number {lcsc_id!r} (no model inserted)")
        return None
    cache = cache_dir if cache_dir is not None else lcsc_cache_dir()
    step_path = cache / f"{lcsc_id}.step"
    if step_path.is_file():
        return step_path
    if not fetch:
        return None
    data = _fetch_lcsc_step(lcsc_id)
    if data is None:
        if callable(warn):
            warn(f"LCSC 3D model fetch failed for {lcsc_id} (no model inserted)")
        return None
    try:
        cache.mkdir(parents=True, exist_ok=True)
        step_path.write_bytes(data)
    except OSError as e:
        if callable(warn):
            warn(f"LCSC 3D model cache write failed for {lcsc_id}: {e}")
        return None
    return step_path


def _fmt_num(value: float) -> str:
    """Format a float the way KiCad writes model offsets (no trailing zeros).

    Deliberately identical to ``models3d._fmt_num`` so a synthesized block and
    a block the shared offset machinery has rewritten are byte-comparable.
    ``tests/test_lcsc_models.py`` asserts the two stay in agreement.
    """
    if value == 0.0:
        value = 0.0  # normalize -0.0 -> 0.0
    rounded = round(value, 6)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:g}"


def _fmt_xyz(triple: Triple | None, default: Triple) -> str:
    """Render ``(xyz x y z)`` for *triple*, falling back to *default*."""
    x, y, z = default if triple is None else triple
    return f"(xyz {_fmt_num(x)} {_fmt_num(y)} {_fmt_num(z)})"


def synthesize_model_block(
    lcsc_id: str,
    *,
    rotate: Triple | None = None,
    offset: Triple | None = None,
) -> str:
    """Build a dedented ``(model ...)`` block referencing the LCSC cache.

    The path uses the portable ``${KCT_LCSC_3D_DIR}`` variable.  With no
    override the block is the historical identity node -- ``(offset (xyz 0 0
    0))`` (so the shared offset machinery injects the full target pad-centroid
    delta as the model's final offset), ``(scale (xyz 1 1 1))``, ``(rotate
    (xyz 0 0 0))``.

    Args:
        lcsc_id: LCSC part number (e.g. ``"C444929"``).
        rotate: Optional model-frame rotation triple, written **verbatim**.
            The LCSC tier's derived ``theta`` is always ``0`` (there is no
            source footprint to derive one from), so nothing composes with
            this value.
        offset: Optional model-frame offset triple.  It seeds the node's
            ``(offset ...)``, and the shared pad-centroid delta ``(dx, -dy)``
            is then *added* into its X/Y by ``models3d._apply_offset_delta``;
            Z passes through untouched.  Because KiCad applies scale ->
            rotate -> offset, this is a post-rotation translation.
    """
    return (
        f'(model "{LCSC_MODEL_PATH_VAR}/{lcsc_id}.step"\n'
        "\t(offset\n"
        f"\t\t{_fmt_xyz(offset, (0.0, 0.0, 0.0))}\n"
        "\t)\n"
        "\t(scale\n"
        "\t\t(xyz 1 1 1)\n"
        "\t)\n"
        "\t(rotate\n"
        f"\t\t{_fmt_xyz(rotate, (0.0, 0.0, 0.0))}\n"
        "\t)\n"
        ")"
    )
