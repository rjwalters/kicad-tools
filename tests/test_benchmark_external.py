"""Tests for benchmarks/external/ (Epic #4932 / issue #4933).

``benchmarks/`` is not on ``pythonpath`` (pyproject.toml only adds
``src``), so ``fetch_boards.py`` and ``normalize.py`` are loaded via
``importlib`` from their file paths, mirroring
``tests/test_check_net_status.py``'s pattern for scripts that live outside
the installed package.

No live network access is used: ``fetch_board()`` is exercised against an
in-memory tarball built with the stdlib ``tarfile`` module and a stubbed
``opener`` callable, per the issue's test plan.
"""

from __future__ import annotations

import importlib.util
import io
import json
import stat
import sys
import tarfile
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTERNAL_DIR = REPO_ROOT / "benchmarks" / "external"
MANIFEST_PATH = EXTERNAL_DIR / "boards.toml"
FIXTURE_PCB = REPO_ROOT / "tests" / "fixtures" / "projects" / "multilayer_zones.kicad_pcb"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fetch_boards():
    return _load_module("kct_bench_fetch_boards", EXTERNAL_DIR / "fetch_boards.py")


@pytest.fixture(scope="module")
def normalize(fetch_boards):
    # normalize.py imports its sibling fetch_boards.py via a sys.path
    # insert of its own directory -- load it after fetch_boards so that
    # module (already cached under a distinct sys.modules key above) does
    # not get double-imported under yet another name.
    return _load_module("kct_bench_normalize", EXTERNAL_DIR / "normalize.py")


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


class TestManifestParsing:
    def test_manifest_file_exists(self):
        assert MANIFEST_PATH.exists()

    def test_parses_expected_slugs(self, fetch_boards):
        boards = fetch_boards.load_manifest(MANIFEST_PATH)
        # #4934's JSON report schema references these slugs -- keep them
        # stable (curator enhancement on #4933).
        assert set(boards.keys()) == {"strf", "pocketbeagle", "beagleconnect_freedom"}

    def test_strf_fields(self, fetch_boards):
        boards = fetch_boards.load_manifest(MANIFEST_PATH)
        strf = boards["strf"]
        assert strf.vcs == "github"
        assert strf.repo_url == "https://github.com/pms67/STRF-Kicad"
        assert strf.board_path == "STRF.kicad_pcb"
        assert strf.license == "MIT"
        # Full 40-char SHA, not a branch/tag -- reproducibility requirement.
        assert len(strf.commit) == 40
        assert strf.deep_pcb_reference["airwires"] == 98
        assert strf.deep_pcb_reference["completion_pct"] == 100
        assert strf.deep_pcb_reference["vias"] == 68

    def test_pocketbeagle_fields(self, fetch_boards):
        boards = fetch_boards.load_manifest(MANIFEST_PATH)
        pb = boards["pocketbeagle"]
        assert pb.vcs == "github"
        assert pb.board_path == "KiCAD/PocketBeagle.kicad_pcb"
        assert pb.license == "CC-BY-4.0"
        assert len(pb.commit) == 40
        assert pb.deep_pcb_reference["airwires"] == 290

    def test_beagleconnect_freedom_fields(self, fetch_boards):
        boards = fetch_boards.load_manifest(MANIFEST_PATH)
        bcf = boards["beagleconnect_freedom"]
        assert bcf.vcs == "gitlab"
        assert bcf.gitlab_project_id == 190
        assert "KICAD" in bcf.board_path
        assert bcf.license == "CC-BY-4.0"
        assert len(bcf.commit) == 40
        assert bcf.deep_pcb_reference["airwires"] == 414

    def test_all_boards_have_full_sha_commits(self, fetch_boards):
        boards = fetch_boards.load_manifest(MANIFEST_PATH)
        for slug, spec in boards.items():
            assert len(spec.commit) == 40, f"{slug}: commit does not look like a full SHA"
            int(spec.commit, 16)  # raises ValueError if not hex


# ---------------------------------------------------------------------------
# Cache dir resolution
# ---------------------------------------------------------------------------


class TestCacheDirResolution:
    def test_default_cache_dir(self, fetch_boards, monkeypatch):
        monkeypatch.delenv(fetch_boards.CACHE_DIR_ENV_VAR, raising=False)
        assert fetch_boards.resolve_cache_dir() == fetch_boards.DEFAULT_CACHE_DIR

    def test_explicit_arg_wins(self, fetch_boards, tmp_path):
        assert fetch_boards.resolve_cache_dir(tmp_path) == tmp_path

    def test_env_override(self, fetch_boards, monkeypatch, tmp_path):
        monkeypatch.setenv(fetch_boards.CACHE_DIR_ENV_VAR, str(tmp_path))
        assert fetch_boards.resolve_cache_dir() == tmp_path

    def test_distinct_from_hardware_fixture_env_var(self, fetch_boards):
        # Naming-collision guard from the curator enhancement: must not
        # reuse tests/conftest.py's KICAD_TOOLS_EXTERNAL_BOARDS_DIR.
        assert fetch_boards.CACHE_DIR_ENV_VAR != "KICAD_TOOLS_EXTERNAL_BOARDS_DIR"


# ---------------------------------------------------------------------------
# Fetch (mocked HTTP layer -- no live network access)
# ---------------------------------------------------------------------------


def _make_tarball(top_dir: str, member_path: str, content: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=f"{top_dir}/")
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
        info = tarfile.TarInfo(name=f"{top_dir}/{member_path}")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class TestFetchBoard:
    def _spec(self, fetch_boards, **overrides):
        defaults = {
            "slug": "fakeboard",
            "name": "Fake Board",
            "repo_url": "https://github.com/fake/fakeboard",
            "vcs": "github",
            "commit": "a" * 40,
            "board_path": "sub/dir/fake.kicad_pcb",
            "license": "MIT",
        }
        defaults.update(overrides)
        return fetch_boards.BoardSpec(**defaults)

    def test_fetch_extracts_pinned_board(self, fetch_boards, tmp_path):
        spec = self._spec(fetch_boards)
        top_dir = f"fakeboard-{spec.commit}"
        content = b"(kicad_pcb (version 20240108))"
        tar_bytes = _make_tarball(top_dir, spec.board_path, content)

        opener = lambda url: _FakeResponse(tar_bytes)  # noqa: E731

        dest = fetch_boards.fetch_board(spec, tmp_path, opener=opener)

        assert dest.exists()
        assert dest.read_bytes() == content
        assert dest.name == "fake.kicad_pcb"
        assert dest.parent == tmp_path / spec.slug

    def test_fetch_rejects_commit_mismatch(self, fetch_boards, tmp_path):
        spec = self._spec(fetch_boards)
        # Top-level dir does NOT reference the pinned commit -- simulates a
        # moved tag / archive API silently serving a different ref.
        top_dir = "fakeboard-deadbeef"
        tar_bytes = _make_tarball(top_dir, spec.board_path, b"data")
        opener = lambda url: _FakeResponse(tar_bytes)  # noqa: E731

        with pytest.raises(fetch_boards.FetchError, match="does not reference the pinned commit"):
            fetch_boards.fetch_board(spec, tmp_path, opener=opener)

    def test_fetch_rejects_missing_board_path(self, fetch_boards, tmp_path):
        spec = self._spec(fetch_boards)
        top_dir = f"fakeboard-{spec.commit}"
        tar_bytes = _make_tarball(top_dir, "wrong/path.kicad_pcb", b"data")
        opener = lambda url: _FakeResponse(tar_bytes)  # noqa: E731

        with pytest.raises(fetch_boards.FetchError, match="not found in the fetched archive"):
            fetch_boards.fetch_board(spec, tmp_path, opener=opener)

    def test_fetch_rejects_empty_archive(self, fetch_boards, tmp_path):
        spec = self._spec(fetch_boards)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz"):
            pass
        opener = lambda url: _FakeResponse(buf.getvalue())  # noqa: E731

        with pytest.raises(fetch_boards.FetchError, match="empty"):
            fetch_boards.fetch_board(spec, tmp_path, opener=opener)

    def test_github_archive_url(self, fetch_boards):
        spec = self._spec(fetch_boards)
        url = fetch_boards._archive_url(spec)
        assert url == f"https://codeload.github.com/fake/fakeboard/tar.gz/{spec.commit}"

    def test_gitlab_archive_url(self, fetch_boards):
        spec = self._spec(
            fetch_boards,
            vcs="gitlab",
            repo_url="https://git.example.org/group/proj",
            gitlab_project_id=42,
        )
        url = fetch_boards._archive_url(spec)
        assert url == (
            f"https://git.example.org/api/v4/projects/42/repository/archive.tar.gz?sha={spec.commit}"
        )

    def test_gitlab_missing_project_id_raises(self, fetch_boards):
        spec = self._spec(fetch_boards, vcs="gitlab", gitlab_project_id=None)
        with pytest.raises(fetch_boards.FetchError, match="gitlab_project_id"):
            fetch_boards._archive_url(spec)

    def test_unsupported_vcs_raises(self, fetch_boards):
        spec = self._spec(fetch_boards, vcs="svn")
        with pytest.raises(fetch_boards.FetchError, match="unsupported vcs"):
            fetch_boards._archive_url(spec)

    def test_fetch_all_unknown_slug_raises(self, fetch_boards):
        with pytest.raises(fetch_boards.FetchError, match="unknown board slug"):
            fetch_boards.fetch_all(MANIFEST_PATH, slugs=["not-a-real-board"])

    def test_fetch_all_restricts_to_requested_slugs(self, fetch_boards, tmp_path, monkeypatch):
        calls = []

        def fake_fetch_board(spec, cache_dir, *, opener=None):
            calls.append(spec.slug)
            return tmp_path / f"{spec.slug}.kicad_pcb"

        monkeypatch.setattr(fetch_boards, "fetch_board", fake_fetch_board)

        result = fetch_boards.fetch_all(MANIFEST_PATH, tmp_path, slugs=["strf"])

        assert calls == ["strf"]
        assert set(result.keys()) == {"strf"}


# ---------------------------------------------------------------------------
# Normalization (rip-up + baseline capture) against a small tracked fixture
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_normalize_rips_up_copper(self, normalize, tmp_path):
        output_path = tmp_path / "normalized.kicad_pcb"

        baseline = normalize.normalize_board(FIXTURE_PCB, output_path)

        assert output_path.exists()
        result = PCB.load(output_path)
        assert result.segment_count == 0
        assert result.via_count == 0
        # Baseline reflects the ORIGINAL board's copper, not the ripped-up
        # result.
        assert baseline.segments == 5
        assert baseline.vias == 2

    def test_normalize_preserves_placement_and_nets(self, normalize, tmp_path):
        output_path = tmp_path / "normalized.kicad_pcb"
        original = PCB.load(FIXTURE_PCB)

        normalize.normalize_board(FIXTURE_PCB, output_path)
        result = PCB.load(output_path)

        assert len(result.footprints) == len(original.footprints)
        original_refs = {fp.reference for fp in original.footprints}
        result_refs = {fp.reference for fp in result.footprints}
        assert result_refs == original_refs

        # Footprint positions untouched by rip-up.
        original_positions = {fp.reference: fp.position for fp in original.footprints}
        result_positions = {fp.reference: fp.position for fp in result.footprints}
        assert result_positions == original_positions

        assert result.nets == original.nets
        assert result.net_classes == original.net_classes

    def test_normalize_preserves_zones_and_outline(self, normalize, tmp_path):
        output_path = tmp_path / "normalized.kicad_pcb"
        original = PCB.load(FIXTURE_PCB)

        normalize.normalize_board(FIXTURE_PCB, output_path)
        result = PCB.load(output_path)

        assert len(result.zones) == len(original.zones)
        original_zone_names = sorted(z.name for z in original.zones)
        result_zone_names = sorted(z.name for z in result.zones)
        assert result_zone_names == original_zone_names

        # Board outline (Edge.Cuts gr_rect) preserved verbatim.
        outline_before = [
            c for c in original._sexp.children if not c.is_atom and c.name == "gr_rect"
        ]
        outline_after = [c for c in result._sexp.children if not c.is_atom and c.name == "gr_rect"]
        assert len(outline_after) == len(outline_before) == 1

    def test_normalize_writes_baseline_sidecar(self, normalize, tmp_path):
        output_path = tmp_path / "normalized.kicad_pcb"

        baseline = normalize.normalize_board(FIXTURE_PCB, output_path)

        sidecar_path = output_path.with_name("normalized.baseline.json")
        assert sidecar_path.exists()
        sidecar = json.loads(sidecar_path.read_text())
        assert sidecar == baseline.to_dict()
        assert sidecar["segments"] == 5
        assert sidecar["vias"] == 2

    def test_rip_up_returns_removed_counts(self, normalize):
        pcb = PCB.load(FIXTURE_PCB)

        removed = normalize.rip_up(pcb)

        assert removed == {"segments": 5, "vias": 2}
        assert pcb.segment_count == 0
        assert pcb.via_count == 0

    def test_capture_baseline_matches_routing_status(self, normalize):
        pcb = PCB.load(FIXTURE_PCB)
        status = pcb.routing_status()

        baseline = normalize.capture_baseline(pcb)

        assert baseline.segments == status["segments"]
        assert baseline.vias == status["vias"]
        assert baseline.trace_length_mm == pytest.approx(status["trace_length_mm"])
        assert baseline.nets_with_traces == len(status["nets_with_traces"])
        assert baseline.unrouted_pads == len(status["unrouted_pads"])


# ---------------------------------------------------------------------------
# Old-format fallback (kicad-cli upgrade path) -- no live KiCad required
# ---------------------------------------------------------------------------


class TestLoadWithUpgrade:
    def _bad_pcb(self, tmp_path: Path) -> Path:
        bad = tmp_path / "unparseable.kicad_pcb"
        bad.write_text("(kicad_pcb (this is not : valid <<< s-expr")
        return bad

    def test_no_kicad_cli_found_reports_clearly(self, normalize, tmp_path, monkeypatch):
        bad = self._bad_pcb(tmp_path)
        monkeypatch.setattr("kicad_tools.cli.runner.find_kicad_cli", lambda: None)

        with pytest.raises(normalize.NormalizeError, match="kicad-cli was not found"):
            normalize.load_with_upgrade(bad)

    def test_upgrade_subprocess_fails_to_start(self, normalize, tmp_path):
        bad = self._bad_pcb(tmp_path)
        nonexistent_cli = tmp_path / "does-not-exist" / "kicad-cli"

        with pytest.raises(normalize.NormalizeError, match="failed to start"):
            normalize.load_with_upgrade(bad, kicad_cli=nonexistent_cli)

    def test_upgrade_subcommand_reports_nonzero_exit(self, normalize, tmp_path):
        bad = self._bad_pcb(tmp_path)
        fake_cli = tmp_path / "fake-kicad-cli"
        fake_cli.write_text("#!/bin/sh\necho 'boom: unsupported board' 1>&2\nexit 1\n")
        fake_cli.chmod(fake_cli.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        with pytest.raises(normalize.NormalizeError, match="also failed"):
            normalize.load_with_upgrade(bad, kicad_cli=fake_cli)

    def test_upgrade_reports_still_unparseable(self, normalize, tmp_path):
        bad = self._bad_pcb(tmp_path)
        # "Succeeds" but does not actually fix the file's content -- the
        # retried PCB.load() must still fail, and that failure must be
        # reported clearly rather than as a second raw traceback.
        fake_cli = tmp_path / "fake-kicad-cli"
        fake_cli.write_text("#!/bin/sh\nexit 0\n")
        fake_cli.chmod(fake_cli.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        with pytest.raises(normalize.NormalizeError, match="still failed to parse"):
            normalize.load_with_upgrade(bad, kicad_cli=fake_cli)

    def test_upgrade_success_path_retries_and_succeeds(self, normalize, tmp_path):
        bad = self._bad_pcb(tmp_path)
        fake_cli = tmp_path / "fake-kicad-cli"
        # Rewrites the target file with valid content, simulating a real
        # `kicad-cli pcb upgrade` doing its job.
        fake_cli.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            'sys.argv[-1] and open(sys.argv[-1], "w").write("(kicad_pcb (version 20240108))")\n'
        )
        fake_cli.chmod(fake_cli.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        result = normalize.load_with_upgrade(bad, kicad_cli=fake_cli)

        assert isinstance(result, PCB)
        assert bad.read_text() == "(kicad_pcb (version 20240108))"

    def test_already_valid_board_skips_upgrade_entirely(self, normalize):
        # PCB.load() succeeds on the first try -- kicad_cli is never
        # consulted (would raise NormalizeError if it were, since None is
        # not a valid path to run).
        result = normalize.load_with_upgrade(FIXTURE_PCB, kicad_cli=Path("/should/not/be/used"))
        assert isinstance(result, PCB)
