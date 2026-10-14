"""PCBWorld D3 corpus support for ``kct bench external --suite pcbworld`` (issue #6331).

Phase 1 of #5942. Everything third-party stays external:

* PCBWorld (BSD-3-Clause, GPL-3.0 engine submodule) and PCBench (mixed board
  licenses) are fetched at their pinned SHAs (``pcbworld.toml``) into the
  gitignored benchmark cache at runtime -- nothing is vendored.
* PCBWorld's own preparation steps (``convert_v9``, ``drc_fix_v9``,
  ``make_guide``, ``sort_prefix``) and its scorer (``eval.pipeline``) are only
  ever run as subprocesses under ``PCBWORLD_PYTHON`` with the PCBWorld
  checkout as cwd. They are NEVER imported in-process (PCBWorld ships a
  ``kicad_tools.py`` that would collide with this package).
* PCBWorld's Clean Pass is read back from its ``per_rollout.csv`` and
  normalized into :class:`kicad_tools.benchmark.external.PCBWorldScore`. It is
  never recomputed locally and never merged with the local gates.

External tools are configured explicitly through environment variables:

``PCBWORLD_PYTHON``  interpreter of PCBWorld's own environment
``KICAD_CLI``        KiCad 9 ``kicad-cli``
``PCBNEW_PYTHON``    interpreter providing KiCad 9's ``pcbnew`` module

All of them are required, and ``KICAD_CLI`` must report KiCad 9.x: a Clean
Pass computed with another KiCad major is not comparable and is refused.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found]

import fetch_boards  # sibling module (benchmarks/external/fetch_boards.py)

DEFAULT_CORPUS_PATH = Path(__file__).resolve().parent / "pcbworld.toml"

ENV_PCBWORLD_PYTHON = "PCBWORLD_PYTHON"
ENV_KICAD_CLI = "KICAD_CLI"
ENV_PCBNEW_PYTHON = "PCBNEW_PYTHON"

# Scoring "cell" name: the directory PCBWorld's eval.pipeline is pointed at.
SCORING_CELL = "kct"
PREP_WORKERS = 2

_PREFIX_RE = re.compile(r"^\d{4}_")
_TRACK_RE = re.compile(r"^\s*\((segment|via|arc)\b", re.MULTILINE)


class PCBWorldError(RuntimeError):
    """Actionable fetch / preparation / tooling / scoring failure."""


# --------------------------------------------------------------------------
# Corpus manifest
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SplitSpec:
    alias: str
    splits_key: str
    difficulty: str
    partition: str


@dataclass(frozen=True)
class CorpusSpec:
    pcbworld_repo_url: str
    pcbworld_commit: str
    pcbworld_license: str
    pcbench_repo_url: str
    pcbench_commit: str
    pcbench_license: str
    pcbench_license_note: str
    kicad_required_major: int
    guide_board_filename: str
    splits_file: str
    splits: Mapping[str, SplitSpec] = field(default_factory=dict)


def load_corpus(path: Path = DEFAULT_CORPUS_PATH) -> CorpusSpec:
    """Parse ``pcbworld.toml``; reject unpinned (non-40-hex) SHAs."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    try:
        pw, pb, kicad, prep = data["pcbworld"], data["pcbench"], data["kicad"], data["prep"]
        splits = {
            alias: SplitSpec(
                alias=alias,
                splits_key=entry["splits_key"],
                difficulty=entry["difficulty"],
                partition=entry["partition"],
            )
            for alias, entry in data["splits"].items()
        }
        spec = CorpusSpec(
            pcbworld_repo_url=pw["repo_url"],
            pcbworld_commit=pw["commit"],
            pcbworld_license=pw["license"],
            pcbench_repo_url=pb["repo_url"],
            pcbench_commit=pb["commit"],
            pcbench_license=pb["license"],
            pcbench_license_note=pb.get("license_note", ""),
            kicad_required_major=int(kicad["required_major"]),
            guide_board_filename=prep["guide_board_filename"],
            splits_file=prep["splits_file"],
            splits=splits,
        )
    except KeyError as exc:
        raise PCBWorldError(f"{path}: missing required key {exc}") from exc
    for label, sha in (("pcbworld", spec.pcbworld_commit), ("pcbench", spec.pcbench_commit)):
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise PCBWorldError(f"{path}: [{label}] commit must be a full 40-hex SHA, got {sha!r}")
    return spec


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------


def _safe_extract(
    data: bytes,
    dest: Path,
    *,
    sha: str,
    label: str,
    include: Callable[[str], bool] | None = None,
) -> None:
    """Extract a codeload tarball into ``dest`` (top-level dir stripped).

    Verifies the top-level directory references the pinned SHA, refuses path
    traversal and skips links / special files.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            members = tar.getmembers()
            if not members:
                raise PCBWorldError(f"{label}: fetched archive is empty")
            top = members[0].name.split("/", 1)[0]
            if sha not in top:
                raise PCBWorldError(
                    f"{label}: archive top-level directory {top!r} does not reference the "
                    f"pinned commit {sha} -- refusing a possibly-stale archive"
                )
            root = dest.resolve()
            for member in members:
                rel = member.name.split("/", 1)[1] if "/" in member.name else ""
                if not rel or not (member.isfile() or member.isdir()):
                    continue
                if include is not None and not include(rel):
                    continue
                target = (dest / rel).resolve()
                if root not in target.parents and target != root:
                    raise PCBWorldError(f"{label}: unsafe path in archive: {member.name!r}")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                fobj = tar.extractfile(member)
                if fobj is not None:
                    target.write_bytes(fobj.read())
    except tarfile.TarError as exc:
        raise PCBWorldError(f"{label}: fetched archive is not a valid tar.gz: {exc}") from exc


def _fetch_repo(
    repo_url: str,
    commit: str,
    label: str,
    dest: Path,
    opener: Callable[[str], Any],
    include: Callable[[str], bool] | None = None,
) -> Path:
    marker = dest / ".kct-fetched"
    if marker.read_text(encoding="utf-8").strip() == commit if marker.exists() else False:
        return dest  # already materialized at the pin
    spec = fetch_boards.BoardSpec(
        slug=label, name=label, repo_url=repo_url, vcs="github", commit=commit,
        board_path="", license="",
    )  # fmt: skip
    url = fetch_boards._archive_url(spec)
    try:
        with opener(url) as response:
            data = response.read()
    except OSError as exc:
        raise PCBWorldError(
            f"{label}: failed to download {url}: {exc}. Check network access, or run on a host "
            "with the cache pre-populated and pass --skip-fetch."
        ) from exc
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    _safe_extract(data, dest, sha=commit, label=label, include=include)
    marker.write_text(commit + "\n", encoding="utf-8")
    return dest


def source_dirs(corpus: CorpusSpec, cache_dir: Path) -> tuple[Path, Path]:
    root = fetch_boards.resolve_cache_dir(cache_dir) / "pcbworld" / "src"
    return (
        root / f"pcbworld-{corpus.pcbworld_commit[:12]}",
        root / f"pcbench-{corpus.pcbench_commit[:12]}",
    )


def _check_cached(label: str, path: Path) -> Path:
    if not path.is_dir():
        raise PCBWorldError(
            f"--skip-fetch given but the {label} cache {path} does not exist -- "
            "drop --skip-fetch to download it"
        )
    return path


def fetch_pcbworld(
    corpus: CorpusSpec,
    cache_dir: Path | None = None,
    *,
    skip_fetch: bool = False,
    opener: Callable[[str], Any] = urllib.request.urlopen,
) -> Path:
    """Fetch PCBWorld at its pin (needed first: its split files name the boards)."""
    pw_dir, _ = source_dirs(corpus, Path(fetch_boards.resolve_cache_dir(cache_dir)))
    if skip_fetch:
        return _check_cached("PCBWorld", pw_dir)
    return _fetch_repo(corpus.pcbworld_repo_url, corpus.pcbworld_commit, "pcbworld", pw_dir, opener)


def fetch_pcbench(
    corpus: CorpusSpec,
    cache_dir: Path | None = None,
    *,
    names: Sequence[str],
    skip_fetch: bool = False,
    opener: Callable[[str], Any] = urllib.request.urlopen,
) -> Path:
    """Fetch only the selected boards of PCBench (``PCBs/<name>/``) at its pin.

    The cached tree is a subset; it is re-fetched if a later selection needs
    boards that are not present.
    """
    _, pb_dir = source_dirs(corpus, Path(fetch_boards.resolve_cache_dir(cache_dir)))
    if skip_fetch:
        return _check_cached("PCBench", pb_dir)
    wanted = {strip_prefix(n) for n in names}
    if all((pb_dir / "PCBs" / n).is_dir() for n in wanted):
        return pb_dir
    marker = pb_dir / ".kct-fetched"
    if marker.exists():
        marker.unlink()  # a different selection must not short-circuit on the old marker

    def include(rel: str) -> bool:
        parts = rel.split("/")
        return len(parts) >= 2 and parts[0] == "PCBs" and parts[1] in wanted

    return _fetch_repo(
        corpus.pcbench_repo_url, corpus.pcbench_commit, "pcbench", pb_dir, opener, include
    )


# --------------------------------------------------------------------------
# External tools
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolConfig:
    pcbworld_python: str
    kicad_cli: str
    pcbnew_python: str
    kicad_version: str
    kicad_major: int


def parse_kicad_major(version_text: str) -> int | None:
    m = re.match(r"\s*(\d+)\.\d+", version_text or "")
    return int(m.group(1)) if m else None


def resolve_tools(
    corpus: CorpusSpec,
    env: Mapping[str, str] | None = None,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> ToolConfig:
    """Resolve the explicit tool env vars and verify KiCad's major version.

    Raises:
        PCBWorldError: a variable is unset / not executable, ``kicad-cli``
            cannot report a version, or the major version is not the one
            PCBWorld requires (never downgraded to a "close enough" score).
    """
    env = os.environ if env is None else env
    values: dict[str, str] = {}
    hints = {
        ENV_PCBWORLD_PYTHON: "the Python interpreter of PCBWorld's own environment",
        ENV_KICAD_CLI: "the path to a KiCad 9 kicad-cli",
        ENV_PCBNEW_PYTHON: "a Python interpreter that can `import pcbnew` from KiCad 9",
    }
    for var, hint in hints.items():
        val = env.get(var)
        if not val:
            raise PCBWorldError(
                f"{var} is not set. Export {var}=<{hint}> -- PCBWorld preparation and scoring "
                "need an explicit KiCad 9 toolchain (see benchmarks/external/README.md, "
                "'PCBWorld suite')."
            )
        values[var] = val
    cli = values[ENV_KICAD_CLI]
    if shutil.which(cli) is None:
        raise PCBWorldError(f"{ENV_KICAD_CLI}={cli!r} was not found or is not executable")
    try:
        proc = runner([cli, "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PCBWorldError(f"could not run `{cli} --version`: {exc}") from exc
    version = (proc.stdout or "").strip()
    major = parse_kicad_major(version)
    if proc.returncode != 0 or major is None:
        raise PCBWorldError(f"`{cli} --version` gave unusable output: {version!r}")
    if major != corpus.kicad_required_major:
        raise PCBWorldError(
            f"{ENV_KICAD_CLI} reports KiCad {version!r}, but PCBWorld D3 requires KiCad "
            f"{corpus.kicad_required_major}.x. A Clean Pass from another major version is "
            "not comparable to published numbers; point KICAD_CLI at a KiCad "
            f"{corpus.kicad_required_major} install."
        )
    return ToolConfig(
        pcbworld_python=values[ENV_PCBWORLD_PYTHON],
        kicad_cli=cli,
        pcbnew_python=values[ENV_PCBNEW_PYTHON],
        kicad_version=version,
        kicad_major=major,
    )


# --------------------------------------------------------------------------
# Split enumeration + preparation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PreparedBoard:
    """A prepared D3 ``guide`` board with stable identity and provenance."""

    board_id: str  # pcbworld/<split>/<NNNN_name> -- stable, from PCBWorld's own split file
    split: str
    name: str  # PCBWorld split-file entry (prefixed), e.g. 0003_kitspace_grove_adaptor
    source_name: str  # PCBench folder name (prefix stripped)
    guide_pcb: Path
    guide_pro: Path | None
    input_sha256: str
    pro_sha256: str | None

    @property
    def slug(self) -> str:
        """Filesystem-safe identifier (reports / routed outputs)."""
        return re.sub(r"[^A-Za-z0-9._-]+", "_", self.board_id.replace("/", "_"))


def select_boards(corpus: CorpusSpec, split: str, pcbworld_dir: Path, limit: int) -> list[str]:
    """Deterministic smoke subset: the first ``limit`` names of the split's test list.

    The board list is read from PCBWorld's own files at the pin -- no list is
    stored in this repository.
    """
    if split not in corpus.splits:
        raise PCBWorldError(f"unknown split {split!r}; available: {sorted(corpus.splits)}")
    if limit < 1:
        raise PCBWorldError(f"--limit must be >= 1, got {limit}")
    spec = corpus.splits[split]
    try:
        splits = json.loads((pcbworld_dir / corpus.splits_file).read_text(encoding="utf-8"))
        entry = splits[spec.splits_key]
        boards_json = json.loads((pcbworld_dir / entry["boards_json"]).read_text(encoding="utf-8"))
        names = list(boards_json[entry["difficulty"]][entry["boards_split"]])
    except (OSError, KeyError, ValueError) as exc:
        raise PCBWorldError(
            f"could not enumerate split {split!r} from PCBWorld at the pinned commit "
            f"({corpus.pcbworld_commit[:12]}): {type(exc).__name__}: {exc}. "
            "The upstream layout may have changed -- re-verify pcbworld.toml."
        ) from exc
    if (entry["difficulty"], entry["boards_split"]) != (spec.difficulty, spec.partition):
        raise PCBWorldError(
            f"split {split!r}: pcbworld.toml says {spec.difficulty}/{spec.partition} but "
            f"PCBWorld's {corpus.splits_file} says {entry['difficulty']}/{entry['boards_split']}"
        )
    overrides = (boards_json.get("_test_overrides") or {}).get(entry["difficulty"], {})
    names = [overrides.get(n, n) for n in names]
    return names[:limit]


def strip_prefix(name: str) -> str:
    return _PREFIX_RE.sub("", name)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def count_copper_items(pcb_path: Path) -> int:
    """Number of top-level ``segment`` / ``via`` / ``arc`` items in a board."""
    return len(_TRACK_RE.findall(pcb_path.read_text(encoding="utf-8", errors="replace")))


def assert_unrouted(pcb_path: Path) -> None:
    """Guide boards must carry no tracks / vias (we never run normalize on them)."""
    n = count_copper_items(pcb_path)
    if n:
        raise PCBWorldError(
            f"{pcb_path}: prepared guide board already contains {n} track/via item(s); the "
            "pcbworld suite does not rip up copper (normalize is skipped on guide boards). "
            "The upstream `guide` variant contract changed or the wrong variant was selected."
        )


def _run_step(
    runner: Callable[..., Any],
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    step: str,
) -> None:
    try:
        proc = runner(argv, cwd=str(cwd), env=dict(env), capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PCBWorldError(f"PCBWorld prep step {step!r} could not start: {exc}") from exc
    if proc.returncode != 0:
        tail = "\n".join(((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()[-15:])
        raise PCBWorldError(
            f"PCBWorld prep step {step!r} failed (exit {proc.returncode}). Last output:\n{tail}"
        )


def prepare_boards(
    corpus: CorpusSpec,
    tools: ToolConfig,
    *,
    split: str,
    names: Sequence[str],
    pcbworld_dir: Path,
    pcbench_dir: Path,
    work_dir: Path,
    runner: Callable[..., Any] = subprocess.run,
) -> list[PreparedBoard]:
    """Run PCBWorld's four prep steps on ``names`` and return the guide boards.

    Only the selected PCBench boards are staged (as symlinks) into the prep
    input root, so the steps process the smoke subset rather than the corpus.
    ``sort_prefix`` renumbers within the prepared subset, so boards are
    matched by prefix-stripped name; the reported ``board_id`` keeps the
    numbering from PCBWorld's own split file.
    """
    pcbs_root = work_dir / "PCBs"
    v9 = work_dir / "v9"
    newdrc = work_dir / "newdrc"
    sorted_out = work_dir / "exacad_sorted"

    def locate(name: str) -> Path | None:
        if not sorted_out.is_dir():
            return None
        for cand in sorted_out.iterdir():
            if cand.is_dir() and strip_prefix(cand.name) == strip_prefix(name):
                guide = cand / corpus.guide_board_filename
                if guide.is_file():
                    return cand
        return None

    if not all(locate(n) for n in names):
        for p in (pcbs_root, v9, newdrc, sorted_out):
            if p.exists():
                shutil.rmtree(p)
        pcbs_root.mkdir(parents=True)
        for name in names:
            src = pcbench_dir / "PCBs" / strip_prefix(name)
            if not src.is_dir():
                raise PCBWorldError(
                    f"PCBench source folder for {name!r} not found at {src}. PCBWorld's split "
                    f"entry should match a PCBench `PCBs/<name>` folder at "
                    f"{corpus.pcbench_commit[:12]}; re-check the pin."
                )
            (pcbs_root / src.name).symlink_to(src.resolve(), target_is_directory=True)

        env = dict(os.environ)
        env.update(
            {
                "PCBENCH_PCBS_ROOT": str(pcbs_root),
                "PCBENCH_V9_ROOT": str(v9),
                "PCBENCH_NEWDRC_OUT": str(newdrc),
                "PCBENCH_SORTED_OUT": str(sorted_out),
                "KICAD_CLI": tools.kicad_cli,
                "PCBNEW_PYTHON": tools.pcbnew_python,
            }
        )
        prep = "tools/datagen/pcbench_prep"
        py = tools.pcbworld_python
        w = str(PREP_WORKERS)
        steps = [
            ("convert_v9", [py, f"{prep}/convert_v9.py", "--workers", w]),
            ("drc_fix_v9", [py, f"{prep}/drc_fix_v9.py", "--workers", w]),
            (
                "make_guide",
                [
                    py,
                    f"{prep}/make_guide.py",
                    "--base-dir",
                    str(newdrc),
                    "--stem",
                    "processed_v9",
                    "--suffix",
                    "_guide_v3",
                    "--workers",
                    w,
                ],
            ),  # fmt: skip
            ("sort_prefix", [py, f"{prep}/sort_prefix.py"]),
        ]
        for step, argv in steps:
            _run_step(runner, argv, cwd=pcbworld_dir, env=env, step=step)

    prepared: list[PreparedBoard] = []
    for name in names:
        folder = locate(name)
        if folder is None:
            raise PCBWorldError(
                f"PCBWorld preparation finished but produced no "
                f"{corpus.guide_board_filename} for {name!r} under {sorted_out} -- the board "
                "may have failed conversion; inspect the prep logs."
            )
        guide = folder / corpus.guide_board_filename
        assert_unrouted(guide)
        pro = guide.with_suffix(".kicad_pro")
        prepared.append(
            PreparedBoard(
                board_id=f"pcbworld/{split}/{name}",
                split=split,
                name=name,
                source_name=strip_prefix(name),
                guide_pcb=guide,
                guide_pro=pro if pro.is_file() else None,
                input_sha256=file_sha256(guide),
                pro_sha256=file_sha256(pro) if pro.is_file() else None,
            )
        )
    return prepared


# --------------------------------------------------------------------------
# Scoring (PCBWorld's own eval.pipeline, normalized into our schema)
# --------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _bool(value: Any) -> bool | None:
    s = str(value).strip().lower()
    if s in ("true", "1", "1.0"):
        return True
    if s in ("false", "0", "0.0"):
        return False
    return None


def _unavailable(
    corpus: CorpusSpec, tools: ToolConfig | None, board: PreparedBoard, reason: str
) -> dict[str, Any]:
    return {
        "available": False,
        "unavailable_reason": reason,
        "comparable": False,
        "comparable_reason": reason,
        **_provenance(corpus, tools, board),
    }


def _provenance(
    corpus: CorpusSpec, tools: ToolConfig | None, board: PreparedBoard
) -> dict[str, Any]:
    return {
        "pcbworld_commit": corpus.pcbworld_commit,
        "pcbench_commit": corpus.pcbench_commit,
        "kicad_version": tools.kicad_version if tools else None,
        "pcbnew_python": tools.pcbnew_python if tools else None,
        "input_sha256": board.input_sha256,
        "pcbworld_board_id": board.board_id,
    }


def score_boards(
    corpus: CorpusSpec,
    tools: ToolConfig,
    boards: Mapping[str, tuple[PreparedBoard, Path | None]],
    *,
    pcbworld_dir: Path,
    scoring_dir: Path,
    seed: int,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, dict[str, Any]]:
    """Score routed boards with PCBWorld's ``eval.pipeline`` (eval + aggregate).

    Args:
        boards: ``{board_id: (prepared, routed_pcb_or_None)}``. A missing
            routed output is an *unavailable* score, never CP=0.

    Returns:
        ``{board_id: payload}`` where ``payload`` is a kwargs dict for
        :class:`kicad_tools.benchmark.external.PCBWorldScore`. One scorer
        invocation covers all staged boards; failures mark every staged board
        unavailable with the actionable reason.
    """
    results: dict[str, dict[str, Any]] = {}
    cell_dir = scoring_dir / SCORING_CELL
    boards_dir = cell_dir / "boards"
    if cell_dir.exists():
        shutil.rmtree(cell_dir)
    boards_dir.mkdir(parents=True)

    staged: dict[str, PreparedBoard] = {}  # PCBWorld board_id (cell grammar) -> prepared
    for board_id, (prepared, routed) in boards.items():
        if routed is None or not Path(routed).is_file():
            results[board_id] = _unavailable(
                corpus, tools, prepared, "router produced no output board; nothing to score"
            )
            continue
        stem = f"{prepared.name}_{SCORING_CELL}_s{seed:02d}_r00"
        shutil.copyfile(routed, boards_dir / f"{stem}.kicad_pcb")
        if prepared.guide_pro is not None:
            shutil.copyfile(prepared.guide_pro, boards_dir / f"{stem}.kicad_pro")
        staged[prepared.name] = prepared

    if not staged:
        return results

    def fail_all(reason: str) -> dict[str, dict[str, Any]]:
        for prepared in staged.values():
            results[prepared.board_id] = _unavailable(corpus, tools, prepared, reason)
        return results

    env = dict(os.environ)
    env.update({"KICAD_CLI": tools.kicad_cli, "PCBNEW_PYTHON": tools.pcbnew_python})
    argv = [
        tools.pcbworld_python, "-m", "eval.pipeline", "--skip-rollout",
        "--output-dir", str(cell_dir), "--stages", "eval,aggregate",
    ]  # fmt: skip
    try:
        proc = runner(argv, cwd=str(pcbworld_dir), env=env, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return fail_all(f"PCBWorld scorer could not start: {exc}")
    if proc.returncode != 0:
        tail = "\n".join(((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()[-10:])
        return fail_all(f"PCBWorld eval.pipeline failed (exit {proc.returncode}): {tail}")

    csv_path = cell_dir / "per_rollout.csv"
    if not csv_path.is_file():
        return fail_all(f"PCBWorld scorer wrote no per_rollout.csv under {cell_dir}")
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    by_board: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_board.setdefault(row.get("board_id", ""), []).append(row)

    for name, prepared in staged.items():
        board_rows = by_board.get(name)
        if not board_rows:
            results[prepared.board_id] = _unavailable(
                corpus, tools, prepared, "PCBWorld per_rollout.csv has no row for this board"
            )
            continue
        rollouts = []
        for row in board_rows:
            ok = (row.get("eval_status") or "").strip().lower() == "ok"
            rollouts.append(
                {
                    "rollout_idx": int(_num(row.get("rollout_idx")) or 0),
                    "seed": int(_num(row.get("seed")) or seed),
                    "eval_ok": ok,
                    "eval_error": (row.get("eval_error") or None),
                    "clean_pass": _bool(row.get("clean_pass")) if ok else None,
                    "routability": _num(row.get("routability")) if ok else None,
                    "drv_errors_only_count": _num(row.get("drv_errors_only_count")) if ok else None,
                    "potential_gain": _num(row.get("potential_gain")),
                    "final_potential": _num(row.get("final_potential")),
                }
            )
        chosen = rollouts[0]  # kct runs one rollout per board in Phase 1
        if not chosen["eval_ok"] or chosen["clean_pass"] is None:
            results[prepared.board_id] = _unavailable(
                corpus,
                tools,
                prepared,
                f"PCBWorld evaluation failed for this board: {chosen['eval_error'] or 'no verdict'}",
            )
            continue
        results[prepared.board_id] = {
            "available": True,
            "comparable": True,
            "clean_pass": chosen["clean_pass"],
            "routability": chosen["routability"],
            "drv_errors_only_count": (
                int(chosen["drv_errors_only_count"])
                if chosen["drv_errors_only_count"] is not None
                else None
            ),
            "potential_gain": chosen["potential_gain"],
            "rollouts": rollouts,
            **_provenance(corpus, tools, prepared),
        }
    return results
