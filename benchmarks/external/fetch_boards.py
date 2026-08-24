"""Fetch benchmark boards pinned in ``boards.toml`` (Epic #4932 / issue #4933).

Materializes each board's ``.kicad_pcb`` at its pinned commit into a
gitignored cache directory -- WITHOUT vendoring third-party board files
into this repo (license + repo-size hygiene). GitHub-hosted boards are
fetched via the ``codeload.github.com`` tarball endpoint; the GitLab-hosted
BeagleConnect Freedom board is fetched via the GitLab Repository Archive
API (unauthenticated -- see
https://docs.gitlab.com/ee/api/repositories.html#get-file-archive).

This cache directory is intentionally distinct from
``tests/conftest.py``'s ``KICAD_TOOLS_EXTERNAL_BOARDS_DIR`` /
``boards/external/`` convention, which resolves locally-symlinked sibling
hardware-fixture repos -- a different mechanism from this module's
fetched-at-runtime DeepPCB comparison boards. See ``README.md`` in this
directory.

Usage:
    uv run python benchmarks/external/fetch_boards.py
    uv run python benchmarks/external/fetch_boards.py --board strf
    uv run python benchmarks/external/fetch_boards.py --cache-dir /tmp/boards
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import tarfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python < 3.11 (tomli is a project dependency there)
    import tomli as tomllib  # type: ignore[import-not-found]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_MANIFEST_PATH = Path(__file__).resolve().parent / "boards.toml"

# Deliberately distinct from tests/conftest.py's EXTERNAL_BOARDS_ENV_VAR
# ("KICAD_TOOLS_EXTERNAL_BOARDS_DIR") -- that resolves a different,
# locally-symlinked mechanism (see module docstring above).
CACHE_DIR_ENV_VAR = "KCT_BENCHMARK_EXTERNAL_CACHE_DIR"
DEFAULT_CACHE_DIR = REPO_ROOT / ".cache" / "kct-benchmarks" / "external"


class FetchError(RuntimeError):
    """Raised when a board cannot be fetched or its pinned commit verified."""


@dataclass(frozen=True)
class BoardSpec:
    """One ``boards.toml`` entry, resolved into a typed record."""

    slug: str
    name: str
    repo_url: str
    vcs: str
    commit: str
    board_path: str
    license: str
    gitlab_project_id: int | None = None
    deep_pcb_reference: dict[str, Any] = field(default_factory=dict)


def load_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> dict[str, BoardSpec]:
    """Parse ``boards.toml`` into ``{slug: BoardSpec}``."""
    with open(path, "rb") as f:
        data = tomllib.load(f)

    boards: dict[str, BoardSpec] = {}
    for slug, entry in data.items():
        if not isinstance(entry, dict):
            continue
        boards[slug] = BoardSpec(
            slug=slug,
            name=entry["name"],
            repo_url=entry["repo_url"],
            vcs=entry["vcs"],
            commit=entry["commit"],
            board_path=entry["board_path"],
            license=entry["license"],
            gitlab_project_id=entry.get("gitlab_project_id"),
            deep_pcb_reference=dict(entry.get("deep_pcb_reference", {})),
        )
    return boards


def resolve_cache_dir(cache_dir: Path | None = None) -> Path:
    """Resolve the fetch cache dir: explicit arg > env override > default."""
    if cache_dir is not None:
        return Path(cache_dir)
    if override := os.environ.get(CACHE_DIR_ENV_VAR):
        return Path(override)
    return DEFAULT_CACHE_DIR


def _github_owner_repo(repo_url: str) -> str:
    if "github.com/" not in repo_url:
        raise FetchError(f"not a github.com URL: {repo_url!r}")
    return repo_url.rstrip("/").split("github.com/", 1)[1]


def _github_archive_url(spec: BoardSpec) -> str:
    owner_repo = _github_owner_repo(spec.repo_url)
    return f"https://codeload.github.com/{owner_repo}/tar.gz/{spec.commit}"


def _gitlab_archive_url(spec: BoardSpec) -> str:
    if spec.gitlab_project_id is None:
        raise FetchError(f"{spec.slug}: gitlab board is missing gitlab_project_id in the manifest")
    if "://" not in spec.repo_url:
        raise FetchError(f"{spec.slug}: repo_url is missing a scheme: {spec.repo_url!r}")
    host = spec.repo_url.split("://", 1)[1].split("/", 1)[0]
    return (
        f"https://{host}/api/v4/projects/{spec.gitlab_project_id}"
        f"/repository/archive.tar.gz?sha={spec.commit}"
    )


def _archive_url(spec: BoardSpec) -> str:
    if spec.vcs == "github":
        return _github_archive_url(spec)
    if spec.vcs == "gitlab":
        return _gitlab_archive_url(spec)
    raise FetchError(f"{spec.slug}: unsupported vcs {spec.vcs!r} (expected 'github' or 'gitlab')")


def fetch_board(
    spec: BoardSpec,
    cache_dir: Path | None = None,
    *,
    opener: Callable[[str], Any] = urllib.request.urlopen,
) -> Path:
    """Download ``spec``'s pinned commit and extract its board into ``cache_dir``.

    Verifies the fetched archive's top-level directory name references the
    pinned commit SHA before extracting anything -- this defends against a
    moved tag or an archive API silently serving a different ref on a
    resolution failure. This is a best-effort string check against the
    archive naming convention both GitHub's codeload and GitLab's
    Repository Archive API use (``<repo>-<...>-<sha>/...``), not a
    cryptographic verification.

    Returns:
        Path to the extracted ``.kicad_pcb`` file inside ``cache_dir``.

    Raises:
        FetchError: on a network/archive-format problem, a commit mismatch,
            or a missing ``board_path`` within the fetched tree.
    """
    cache_dir = resolve_cache_dir(cache_dir)
    board_dir = cache_dir / spec.slug
    board_dir.mkdir(parents=True, exist_ok=True)

    url = _archive_url(spec)
    try:
        with opener(url) as response:
            data = response.read()
    except OSError as exc:
        raise FetchError(f"{spec.slug}: failed to download {url}: {exc}") from exc

    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            names = tar.getnames()
            if not names:
                raise FetchError(f"{spec.slug}: fetched archive is empty ({url})")

            top_dir = names[0].split("/", 1)[0]
            if spec.commit not in top_dir:
                raise FetchError(
                    f"{spec.slug}: fetched archive's top-level directory "
                    f"{top_dir!r} does not reference the pinned commit "
                    f"{spec.commit} -- refusing a possibly-stale archive ({url})"
                )

            member_path = f"{top_dir}/{spec.board_path}"
            try:
                member = tar.getmember(member_path)
            except KeyError as exc:
                raise FetchError(
                    f"{spec.slug}: board_path {spec.board_path!r} not found in "
                    f"the fetched archive (looked for {member_path!r})"
                ) from exc

            extracted = tar.extractfile(member)
            if extracted is None:
                raise FetchError(f"{spec.slug}: {member_path!r} is not a regular file")
            board_bytes = extracted.read()
    except tarfile.TarError as exc:
        raise FetchError(
            f"{spec.slug}: fetched archive is not a valid tar.gz ({url}): {exc}"
        ) from exc

    dest = board_dir / Path(spec.board_path).name
    dest.write_bytes(board_bytes)
    return dest


def fetch_all(
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    cache_dir: Path | None = None,
    *,
    slugs: list[str] | None = None,
    opener: Callable[[str], Any] = urllib.request.urlopen,
) -> dict[str, Path]:
    """Fetch every board in the manifest (or just ``slugs``) into ``cache_dir``."""
    boards = load_manifest(manifest_path)
    if slugs:
        missing = set(slugs) - boards.keys()
        if missing:
            raise FetchError(f"unknown board slug(s): {sorted(missing)}")
        boards = {slug: boards[slug] for slug in slugs}

    return {slug: fetch_board(spec, cache_dir, opener=opener) for slug, spec in boards.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--board",
        action="append",
        dest="boards",
        metavar="SLUG",
        help="Fetch only this board slug (repeatable); default: all boards in the manifest",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--cache-dir", type=Path, default=None, help="Override the fetch cache dir")
    args = parser.parse_args(argv)

    try:
        fetched = fetch_all(args.manifest, args.cache_dir, slugs=args.boards)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for slug, path in fetched.items():
        print(f"{slug}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
