"""Pinned, digest-verified subset of KiCad's stock 3D model library (#7683).

The pinned toolchain image ships no ``${KICAD10_3DMODEL_DIR}`` models, so a
footprint that points at one is a gap on every pinned render.  Operator ruling
Q43 = 1: fetch only the model files the fleet's boards reference, from
``kicad-packages3D`` at a pinned tag, and verify each against a committed
sha256 manifest.  Nothing is re-hosted; the manifest records only paths and
digests, and ``ATTRIBUTION.md`` ships beside it (CC-BY-SA-4.0 with the KiCad
design exception).

``kct models sync`` populates the cache; :func:`pinned_model_dir` returns the
cache directory only when *every* manifest file verifies, so a missing or
partial cache degrades to "not synced" instead of a half-resolved model set.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "CACHE_ENV_VAR",
    "MANIFEST_PATH",
    "PINNED_3DMODEL_TAG",
    "Manifest",
    "SyncResult",
    "UPSTREAM_RAW_URL",
    "cache_dir",
    "default_fetcher",
    "load_manifest",
    "pinned_model_dir",
    "sync_models",
    "verify_cache",
]

#: ``kicad-packages3D`` tag the manifest digests were taken from.  Kept equal
#: to the pinned image's ``<major>.<minor>.<patch>`` by
#: ``tests/test_models_sync.py`` so the model set cannot drift from the
#: toolchain.
PINNED_3DMODEL_TAG = "10.0.6"

#: Committed manifest (paths + sha256) and its attribution notice.
DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "kicad-3dmodels"
MANIFEST_PATH = DATA_DIR / "manifest.json"
ATTRIBUTION_PATH = DATA_DIR / "ATTRIBUTION.md"

#: Raw-file URL template; ``{tag}`` and the percent-encoded ``{path}``.
UPSTREAM_RAW_URL = "https://gitlab.com/kicad/libraries/kicad-packages3D/-/raw/{tag}/{path}"

#: Overrides the cache root (default ``~/.cache/kicad-tools/kicad-3dmodels``).
CACHE_ENV_VAR = "KCT_KICAD_3DMODEL_CACHE"

Fetcher = Callable[[str], bytes]

#: Manifest paths that look like LCSC/EasyEDA content (a ``C<digits>`` body or
#: anything under an ``lcsc``/``easyeda`` directory).  The pinned subset is
#: KiCad's stock library only; LCSC bodies are per-user cache content that must
#: never be fetched, cached or served as part of it (rulings Q43/Q44).
_LCSC_LIKE = re.compile(
    r"(^|/)(lcsc|lcsc-3d|easyeda)(/|$)|(^|/)C\d+\.(step|stp|wrl)$", re.IGNORECASE
)


class ModelSyncError(RuntimeError):
    """A manifest problem or a failed/corrupt download."""


@dataclass(frozen=True)
class Manifest:
    """The committed subset: tag, upstream commit and ``path -> sha256``."""

    tag: str
    commit: str
    files: dict[str, str] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.files)


@dataclass
class SyncResult:
    """Outcome of one :func:`sync_models` run."""

    downloaded: list[str] = field(default_factory=list)
    cached: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed


def load_manifest(path: Path | None = None) -> Manifest:
    """Load and validate the committed manifest."""
    path = path or MANIFEST_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        files = {str(k): str(v) for k, v in data["files"].items()}
        manifest = Manifest(tag=str(data["tag"]), commit=str(data["commit"]), files=files)
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        raise ModelSyncError(f"cannot read 3D model manifest {path}: {exc}") from exc
    for rel, digest in files.items():
        parts = Path(rel).parts
        if Path(rel).is_absolute() or ".." in parts or len(digest) != 64:
            raise ModelSyncError(f"malformed manifest entry: {rel!r}")
        if _LCSC_LIKE.search(Path(rel).as_posix()):
            raise ModelSyncError(
                f"manifest entry {rel!r} looks like an LCSC/EasyEDA body; "
                "the pinned subset carries KiCad stock models only"
            )
    return manifest


def cache_dir(tag: str | None = None) -> Path:
    """Cache directory holding the *tag* model set."""
    root = os.environ.get(CACHE_ENV_VAR)
    base = Path(root) if root else Path.home() / ".cache" / "kicad-tools" / "kicad-3dmodels"
    return base / (tag or PINNED_3DMODEL_TAG)


def _sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def verify_cache(manifest: Manifest, root: Path) -> list[str]:
    """Manifest paths whose cached file is missing or has the wrong digest."""
    return [rel for rel, want in manifest.files.items() if _sha256(root / rel) != want]


def pinned_model_dir(manifest: Manifest | None = None, root: Path | None = None) -> Path | None:
    """The synced model directory, or ``None`` unless every file verifies."""
    try:
        manifest = manifest or load_manifest()
    except ModelSyncError:
        return None
    root = root or cache_dir(manifest.tag)
    if not manifest.files or verify_cache(manifest, root):
        return None
    return root


def default_fetcher(url: str) -> bytes:
    """Fetch *url* over HTTPS."""
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https host
        payload: bytes = response.read()
    return payload


def _url(tag: str, rel: str) -> str:
    return UPSTREAM_RAW_URL.format(tag=tag, path=urllib.parse.quote(rel))


def sync_models(
    manifest: Manifest | None = None,
    root: Path | None = None,
    fetcher: Fetcher | None = None,
) -> SyncResult:
    """Download every manifest file missing from *root* and verify its digest.

    A valid cached file costs no network call.  A digest mismatch (or a
    corrupt cached copy) deletes the file and is reported in ``failed``.
    """
    manifest = manifest or load_manifest()
    root = root or cache_dir(manifest.tag)
    fetcher = fetcher or default_fetcher
    result = SyncResult()
    for rel, want in manifest.files.items():
        target = root / rel
        if target.exists():
            if _sha256(target) == want:
                result.cached.append(rel)
                continue
            target.unlink(missing_ok=True)  # corrupt cache entry: refetch
        try:
            payload = fetcher(_url(manifest.tag, rel))
        except (OSError, urllib.error.URLError) as exc:
            result.failed[rel] = f"fetch failed: {exc}"
            continue
        got = hashlib.sha256(payload).hexdigest()
        if got != want:
            result.failed[rel] = f"sha256 mismatch (expected {want}, got {got})"
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a killed run never leaves a truncated file.
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".part-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(tmp, target)
        finally:
            Path(tmp).unlink(missing_ok=True)
        result.downloaded.append(rel)
    return result
