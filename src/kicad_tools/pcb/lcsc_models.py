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
pin-1-registered) and scale/rotation are left at KiCad defaults (``1 1 1`` /
``0 0 0``); a fetched body whose native orientation differs from the
footprint's silkscreen may sit rotated.  These limitations are acceptable for
cosmetic render bodies and noted for future per-mapping overrides.

**Offline / CI safety.**  The fetch is opt-in.  A model resolves only when the
cache already holds the STEP, or when fetching is explicitly enabled (via the
``fetch`` flag / ``KCT_LCSC_FETCH`` env var).  Fetch and parse failures never
raise -- they degrade to ``None`` (reported as unresolved) so a patch or render
never fails for want of a body, and CI never needs network.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

__all__ = [
    "DEFAULT_CACHE_ENV_VAR",
    "LCSC_MODEL_PATH_VAR",
    "fetch_enabled",
    "load_lcsc_mapping",
    "lcsc_cache_dir",
    "resolve_lcsc_step",
    "synthesize_model_block",
]

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


def load_lcsc_mapping(sidecar_path: Path | str) -> dict[str, str]:
    """Load a ``lib_id -> C-number`` sidecar (``lcsc_models.json``).

    The sidecar is a flat JSON object, e.g.
    ``{"Module:Joystick_Analog": "C50950"}``.  Raises ``ValueError`` on a
    malformed file (not silently ignored -- a broken committed sidecar is a
    build error, distinct from a runtime network failure).
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
    mapping: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(
                f"LCSC sidecar {path}: entries must be string lib_id -> string C-number"
            )
        mapping[key] = value
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


def synthesize_model_block(lcsc_id: str) -> str:
    """Build a dedented ``(model ...)`` block referencing the LCSC cache.

    The path uses the portable ``${KCT_LCSC_3D_DIR}`` variable and a baseline
    ``(offset (xyz 0 0 0))`` so the shared offset machinery injects the full
    target pad-centroid delta as the model's final offset.
    """
    return (
        f'(model "{LCSC_MODEL_PATH_VAR}/{lcsc_id}.step"\n'
        "\t(offset\n"
        "\t\t(xyz 0 0 0)\n"
        "\t)\n"
        "\t(scale\n"
        "\t\t(xyz 1 1 1)\n"
        "\t)\n"
        "\t(rotate\n"
        "\t\t(xyz 0 0 0)\n"
        "\t)\n"
        ")"
    )
