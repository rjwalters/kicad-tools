"""Tests for ``kct bench external`` (Epic #4932 Phase 2, issue #4941).

Exercises the fetch -> normalize -> route -> measure pipeline WITHOUT live
network access or a real (slow) router pass:

* Fetch is either bypassed via ``--skip-fetch`` (a pre-populated cache dir)
  or stubbed with an in-memory tarball ``opener`` -- mirroring
  ``tests/test_benchmark_external.py``'s pattern -- so no live HTTP request
  is ever made.
* The router itself is replaced with an injectable stub: both
  ``_run_one_board`` and (via ``monkeypatch`` on
  ``kicad_tools.cli.route_cmd.main``) the full CLI path never invoke the
  real negotiated router, which would take minutes on anything but a
  trivial fixture.

Per the issue's note on sandboxes without live network access, no test
here depends on ``benchmarks/external/boards.toml``'s real pinned boards
or on actually reaching GitHub/GitLab.
"""

from __future__ import annotations

import io
import json
import shutil
import tarfile
from pathlib import Path
from typing import Any

import pytest

from kicad_tools.benchmark.external import BackendInfo
from kicad_tools.cli.commands import bench as bench_cmd
from kicad_tools.cli.parser import create_parser
from kicad_tools.schema.pcb import PCB

# ---------------------------------------------------------------------------
# Fixture boards
# ---------------------------------------------------------------------------

# One net (SIG1) across 3 pads on 2 footprints -- 2 required connections
# (pads - 1). ONE pre-existing segment joins R1.2 <-> R2.1, leaving R1.1
# stranded, so normalize.rip_up() has something real to strip and the
# baseline sidecar captures a non-trivial "before" snapshot.
SOURCE_FIXTURE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "SIG1")

  (gr_line (start 0 0) (end 20 0) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 20 0) (end 20 20) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 20 20) (end 0 20) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 0 20) (end 0 0) (layer "Edge.Cuts") (width 0.05))

  (footprint "R_0402"
    (layer "F.Cu")
    (at 5 10)
    (property "Reference" "R1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG1"))
  )

  (footprint "R_0402"
    (layer "F.Cu")
    (at 15 10)
    (property "Reference" "R2")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG1"))
  )

  (segment (start 5.5 10) (end 14.5 10) (width 0.25) (layer "F.Cu") (net 1))
)
"""

# What a successful zero-touch route of the RIPPED (normalized) board would
# look like: both required connections satisfied (R1.1<->R1.2, R1.2<->R2.1),
# 0 vias, 10mm of copper (1mm + 9mm).
ROUTED_OUTPUT_FIXTURE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "SIG1")

  (gr_line (start 0 0) (end 20 0) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 20 0) (end 20 20) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 20 20) (end 0 20) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 0 20) (end 0 0) (layer "Edge.Cuts") (width 0.05))

  (footprint "R_0402"
    (layer "F.Cu")
    (at 5 10)
    (property "Reference" "R1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG1"))
  )

  (footprint "R_0402"
    (layer "F.Cu")
    (at 15 10)
    (property "Reference" "R2")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG1"))
  )

  (segment (start 4.5 10) (end 5.5 10) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 5.5 10) (end 14.5 10) (width 0.25) (layer "F.Cu") (net 1))
)
"""


def _make_stub_route(output_text: str | None = None, *, write_output: bool = True):
    """Build a fake ``route_cmd.main``-shaped callable for injection.

    Never invokes the real router -- writes ``output_text`` (or copies the
    input unchanged when ``output_text`` is ``None``) to the ``-o`` path.
    ``write_output=False`` simulates a fatal router failure that leaves no
    output file at all (route_cmd.py's documented exit-code-1 behavior).
    """

    def _route(argv: list[str]) -> int:
        input_path = Path(argv[0])
        output_path = Path(argv[argv.index("-o") + 1])
        if write_output:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_text is not None:
                output_path.write_text(output_text, encoding="utf-8")
            else:
                shutil.copyfile(input_path, output_path)
            return 0
        return 1

    return _route


def _make_capturing_stub_route(output_text: str | None, captured_argv: list[list[str]]):
    """Like ``_make_stub_route``, but records each invocation's argv.

    Used to assert the tuned protocol actually appends ``--net-class-map``
    / ``--differential-pairs`` to the router invocation (issue #4943)
    without depending on the real router.
    """

    def _route(argv: list[str]) -> int:
        captured_argv.append(list(argv))
        input_path = Path(argv[0])
        output_path = Path(argv[argv.index("-o") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_text is not None:
            output_path.write_text(output_text, encoding="utf-8")
        else:
            shutil.copyfile(input_path, output_path)
        return 0

    return _route


def _cpp_backend() -> BackendInfo:
    return BackendInfo(backend="cpp", available=True, version="1.0.0", build_version=21)


def _python_backend() -> BackendInfo:
    return BackendInfo(
        backend="python",
        available=False,
        unavailable_reason="C++ router extension not built",
    )


def _spec(fetch_boards_mod: Any, **overrides: Any):
    defaults = {
        "slug": "fixture",
        "name": "Fixture Board",
        "repo_url": "https://example.invalid/fixture",
        "vcs": "github",
        "commit": "a" * 40,
        "board_path": "fixture.kicad_pcb",
        "license": "MIT",
    }
    defaults.update(overrides)
    return fetch_boards_mod.BoardSpec(**defaults)


# ---------------------------------------------------------------------------
# Repo-root discovery / dynamic import of benchmarks/external/
# ---------------------------------------------------------------------------


class TestExternalModuleLoading:
    def test_find_repo_root_locates_benchmarks_external(self):
        root = bench_cmd._find_repo_root()
        assert root is not None
        assert (root / "benchmarks" / "external" / "fetch_boards.py").exists()

    def test_load_external_modules_returns_expected_attrs(self):
        fetch_boards, normalize = bench_cmd._load_external_modules()
        assert hasattr(fetch_boards, "fetch_board")
        assert hasattr(fetch_boards, "load_manifest")
        assert hasattr(fetch_boards, "BoardSpec")
        assert hasattr(normalize, "normalize_board")
        assert hasattr(normalize, "NormalizeError")

    def test_load_tuned_rules_returns_expected_attrs(self):
        tuned_rules = bench_cmd._load_tuned_rules()
        assert hasattr(tuned_rules, "build_tuned_net_class_map")
        assert hasattr(tuned_rules, "diff_pairs_for")
        assert hasattr(tuned_rules, "TUNED_DIFF_PAIRS")


# ---------------------------------------------------------------------------
# tuned_rules.py -- the declared STRF netclass/diff-pair config (issue #4943)
# ---------------------------------------------------------------------------


class TestTunedRules:
    """DeepPCB's STRF case-study values, reproduced from
    https://deeppcb.ai/benchmark/mixed-signal-rf-board-routing/ (verified
    2026-08-25): USB diff pairs at 0.20mm track / 0.15mm gap, SPI bus on
    its own compact-via netclass. See ``benchmarks/external/tuned_rules.py``
    for the full sourcing and schema-mapping notes.
    """

    def test_strf_usb_pairs_match_declared_geometry(self):
        tuned_rules = bench_cmd._load_tuned_rules()
        ncm = tuned_rules.build_tuned_net_class_map("strf")
        assert ncm is not None

        for net_p, net_n in (("USB_D+", "USB_D-"), ("USB_CONN_D+", "USB_CONN_D-")):
            for net, partner in ((net_p, net_n), (net_n, net_p)):
                entry = ncm[net]
                assert entry["trace_width"] == pytest.approx(0.20)
                assert entry["intra_pair_clearance"] == pytest.approx(0.15)
                assert entry["coupled_routing"] is True
                assert entry["diffpair_partner"] == partner

    def test_strf_spi_bus_gets_compact_via_netclass(self):
        tuned_rules = bench_cmd._load_tuned_rules()
        ncm = tuned_rules.build_tuned_net_class_map("strf")
        assert ncm is not None

        for net in ("SPI3_SCK", "SPI3_MOSI", "SPI3_MISO", "SPI3_!CS"):
            entry = ncm[net]
            assert entry["via_size"] == pytest.approx(0.4)
            assert entry["name"] == "SPI"

    def test_strf_net_class_map_is_net_class_routing_loadable(self):
        """Every entry round-trips through the real ``NetClassRouting.from_dict``
        used by ``--net-class-map`` (issue #2996's canonical loader) -- not
        just a dict with the right-looking keys.
        """
        from kicad_tools.router.rules import NetClassRouting

        tuned_rules = bench_cmd._load_tuned_rules()
        ncm = tuned_rules.build_tuned_net_class_map("strf")
        assert ncm is not None
        for net, entry in ncm.items():
            parsed = NetClassRouting.from_dict(entry)
            assert parsed.name == entry["name"], net

    def test_unsupported_board_returns_none(self):
        tuned_rules = bench_cmd._load_tuned_rules()
        assert tuned_rules.build_tuned_net_class_map("pocketbeagle") is None
        assert tuned_rules.build_tuned_net_class_map("beagleconnect_freedom") is None
        assert tuned_rules.build_tuned_net_class_map("does-not-exist") is None

    def test_diff_pairs_for_strf(self):
        tuned_rules = bench_cmd._load_tuned_rules()
        pairs = tuned_rules.diff_pairs_for("strf")
        assert pairs is not None
        assert ("USB_D+", "USB_D-") in pairs
        assert ("USB_CONN_D+", "USB_CONN_D-") in pairs

    def test_diff_pairs_for_unsupported_board_returns_none(self):
        tuned_rules = bench_cmd._load_tuned_rules()
        assert tuned_rules.diff_pairs_for("pocketbeagle") is None


# ---------------------------------------------------------------------------
# _run_one_board -- the per-board pipeline, in isolation
# ---------------------------------------------------------------------------


class TestRunOneBoard:
    def test_full_pipeline_with_stub_router(self, tmp_path):
        fetch_boards, normalize = bench_cmd._load_external_modules()
        cache_dir = tmp_path / "cache"
        (cache_dir / "fixture").mkdir(parents=True)
        (cache_dir / "fixture" / "fixture.kicad_pcb").write_text(SOURCE_FIXTURE, encoding="utf-8")
        output_dir = tmp_path / "out"
        spec = _spec(fetch_boards)

        report = bench_cmd._run_one_board(
            spec,
            fetch_boards,
            normalize,
            cache_dir=cache_dir,
            output_dir=output_dir,
            seed=7,
            manufacturer="jlcpcb",
            layers=2,
            skip_fetch=True,
            run_kicad_cli=False,
            kicad_cli_timeout=60,
            backend=_cpp_backend(),
            verbose=False,
            route_fn=_make_stub_route(ROUTED_OUTPUT_FIXTURE),
        )

        assert report.board_id == "fixture"
        assert report.protocol == "zero-touch"
        assert report.board_commit == "a" * 40
        assert report.board_source == spec.repo_url
        assert report.completion.connections_total == 2
        assert report.completion.connections_routed == 2
        assert report.completion.completion_pct == 100.0
        assert report.copper.via_count == 0
        assert report.copper.wirelength_mm == pytest.approx(10.0)
        assert report.kicad_cli_drc.ran is False  # run_kicad_cli=False

        # Timing recorded because the (stubbed) C++ backend was reported live.
        assert report.timing.valid is True
        assert report.timing.wall_clock_s is not None
        assert report.timing.wall_clock_s >= 0.0

        assert any(n == "seed=7" for n in report.notes)
        assert any("human baseline" in n for n in report.notes)
        assert any("router exit code: 0" in n for n in report.notes)

        # The normalized (ripped-up) board was written and stripped of copper.
        normalized_path = output_dir / "normalized" / "fixture.kicad_pcb"
        assert normalized_path.exists()
        normalized_pcb = PCB.load(str(normalized_path))
        assert len(normalized_pcb.segments) == 0
        assert len(normalized_pcb.vias) == 0

        # The routed output was written by the (stub) router.
        assert (output_dir / "routed" / "fixture.kicad_pcb").exists()

        # The baseline sidecar captured the pre-rip-up human routing.
        baseline_path = normalized_path.with_name("fixture.baseline.json")
        assert baseline_path.exists()
        baseline = json.loads(baseline_path.read_text())
        assert baseline["segments"] == 1
        assert baseline["vias"] == 0

    def test_skip_fetch_missing_file_raises(self, tmp_path):
        fetch_boards, normalize = bench_cmd._load_external_modules()
        spec = _spec(fetch_boards)

        with pytest.raises(bench_cmd.BenchExternalError, match="--skip-fetch"):
            bench_cmd._run_one_board(
                spec,
                fetch_boards,
                normalize,
                cache_dir=tmp_path / "cache",
                output_dir=tmp_path / "out",
                seed=1,
                manufacturer="jlcpcb",
                layers=2,
                skip_fetch=True,
                run_kicad_cli=False,
                kicad_cli_timeout=60,
                backend=_cpp_backend(),
                verbose=False,
                route_fn=_make_stub_route(ROUTED_OUTPUT_FIXTURE),
            )

    def test_timing_refused_when_backend_unavailable(self, tmp_path):
        """The stopwatch is never even started without the C++ backend.

        This is the CLI-level enforcement of Epic #4932's stated risk:
        gate BEFORE recording, not merely discard afterward.
        """
        fetch_boards, normalize = bench_cmd._load_external_modules()
        cache_dir = tmp_path / "cache"
        (cache_dir / "fixture").mkdir(parents=True)
        (cache_dir / "fixture" / "fixture.kicad_pcb").write_text(SOURCE_FIXTURE, encoding="utf-8")

        report = bench_cmd._run_one_board(
            spec := _spec(fetch_boards),
            fetch_boards,
            normalize,
            cache_dir=cache_dir,
            output_dir=tmp_path / "out",
            seed=1,
            manufacturer="jlcpcb",
            layers=2,
            skip_fetch=True,
            run_kicad_cli=False,
            kicad_cli_timeout=60,
            backend=_python_backend(),
            verbose=False,
            route_fn=_make_stub_route(ROUTED_OUTPUT_FIXTURE),
        )
        del spec

        assert report.timing.valid is False
        assert report.timing.wall_clock_s is None
        assert report.timing.refusal_reason is not None
        # Routing still happened (metrics reflect the stub's fully-routed
        # output) -- only the timing number was refused, not the run itself.
        assert report.completion.completion_pct == 100.0

    def test_router_produces_no_output_falls_back_to_ripped_board(self, tmp_path):
        fetch_boards, normalize = bench_cmd._load_external_modules()
        cache_dir = tmp_path / "cache"
        (cache_dir / "fixture").mkdir(parents=True)
        (cache_dir / "fixture" / "fixture.kicad_pcb").write_text(SOURCE_FIXTURE, encoding="utf-8")

        report = bench_cmd._run_one_board(
            _spec(fetch_boards),
            fetch_boards,
            normalize,
            cache_dir=cache_dir,
            output_dir=tmp_path / "out",
            seed=1,
            manufacturer="jlcpcb",
            layers=2,
            skip_fetch=True,
            run_kicad_cli=False,
            kicad_cli_timeout=60,
            backend=_cpp_backend(),
            verbose=False,
            route_fn=_make_stub_route(write_output=False),
        )

        assert report.completion.completion_pct == 0.0
        assert any("no output file" in n for n in report.notes)
        assert any("router exit code: 1" in n for n in report.notes)

    def test_fetch_via_stubbed_opener(self, tmp_path):
        """``skip_fetch=False`` path with an in-memory tarball opener.

        No live network call -- mirrors
        ``tests/test_benchmark_external.py::TestFetchBoard``'s pattern.
        """
        fetch_boards, normalize = bench_cmd._load_external_modules()
        spec = _spec(
            fetch_boards,
            repo_url="https://github.com/example/fixture",
            board_path="sub/fixture.kicad_pcb",
        )
        top_dir = f"fixture-{spec.commit}"

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name=f"{top_dir}/")
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
            content = SOURCE_FIXTURE.encode("utf-8")
            info = tarfile.TarInfo(name=f"{top_dir}/{spec.board_path}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        tar_bytes = buf.getvalue()

        class _FakeResponse:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        opener = lambda url: _FakeResponse(tar_bytes)  # noqa: E731

        report = bench_cmd._run_one_board(
            spec,
            fetch_boards,
            normalize,
            cache_dir=tmp_path / "cache",
            output_dir=tmp_path / "out",
            seed=1,
            manufacturer="jlcpcb",
            layers=2,
            skip_fetch=False,
            run_kicad_cli=False,
            kicad_cli_timeout=60,
            backend=_cpp_backend(),
            verbose=False,
            route_fn=_make_stub_route(ROUTED_OUTPUT_FIXTURE),
            opener=opener,
        )

        assert report.board_id == "fixture"
        assert (tmp_path / "cache" / "fixture" / "fixture.kicad_pcb").exists()

    def test_tuned_protocol_appends_net_class_map_and_diffpair_flags(self, tmp_path):
        """Issue #4943: a supplied ``net_class_map_path`` engages
        ``--net-class-map`` + ``--differential-pairs`` on the router
        invocation and tags the report ``protocol="tuned"``.
        """
        fetch_boards, normalize = bench_cmd._load_external_modules()
        cache_dir = tmp_path / "cache"
        (cache_dir / "fixture").mkdir(parents=True)
        (cache_dir / "fixture" / "fixture.kicad_pcb").write_text(SOURCE_FIXTURE, encoding="utf-8")
        spec = _spec(fetch_boards)

        ncm_path = tmp_path / "tuned" / "fixture.net_class_map.json"
        ncm_path.parent.mkdir(parents=True)
        ncm_path.write_text("{}", encoding="utf-8")

        captured: list[list[str]] = []
        report = bench_cmd._run_one_board(
            spec,
            fetch_boards,
            normalize,
            cache_dir=cache_dir,
            output_dir=tmp_path / "out",
            seed=1,
            manufacturer="jlcpcb",
            layers=2,
            skip_fetch=True,
            run_kicad_cli=False,
            kicad_cli_timeout=60,
            backend=_cpp_backend(),
            verbose=False,
            route_fn=_make_capturing_stub_route(ROUTED_OUTPUT_FIXTURE, captured),
            protocol="tuned",
            net_class_map_path=ncm_path,
            diff_pairs=[("SIG1", "SIG1")],
        )

        assert report.protocol == "tuned"
        assert len(captured) == 1
        assert "--net-class-map" in captured[0]
        assert str(ncm_path) in captured[0]
        assert "--differential-pairs" in captured[0]
        assert any("tuned protocol: applied declared net-class-map" in n for n in report.notes)

    def test_zero_touch_protocol_omits_net_class_map_flags(self, tmp_path):
        """The default (no ``net_class_map_path``) path must NOT add the
        tuned-only flags -- zero-touch stays rules-as-shipped.
        """
        fetch_boards, normalize = bench_cmd._load_external_modules()
        cache_dir = tmp_path / "cache"
        (cache_dir / "fixture").mkdir(parents=True)
        (cache_dir / "fixture" / "fixture.kicad_pcb").write_text(SOURCE_FIXTURE, encoding="utf-8")
        spec = _spec(fetch_boards)

        captured: list[list[str]] = []
        report = bench_cmd._run_one_board(
            spec,
            fetch_boards,
            normalize,
            cache_dir=cache_dir,
            output_dir=tmp_path / "out",
            seed=1,
            manufacturer="jlcpcb",
            layers=2,
            skip_fetch=True,
            run_kicad_cli=False,
            kicad_cli_timeout=60,
            backend=_cpp_backend(),
            verbose=False,
            route_fn=_make_capturing_stub_route(ROUTED_OUTPUT_FIXTURE, captured),
        )

        assert report.protocol == "zero-touch"
        assert "--net-class-map" not in captured[0]
        assert "--differential-pairs" not in captured[0]
        assert not any("tuned protocol" in n for n in report.notes)


# ---------------------------------------------------------------------------
# Full CLI dispatch: run_bench_command
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, cache_dir: Path) -> Path:
    manifest_path = tmp_path / "boards.toml"
    manifest_path.write_text(
        f"""
[fixture]
name = "Fixture Board"
repo_url = "https://example.invalid/fixture"
vcs = "github"
commit = "{"a" * 40}"
board_path = "fixture.kicad_pcb"
license = "MIT"
""",
        encoding="utf-8",
    )
    (cache_dir / "fixture").mkdir(parents=True)
    (cache_dir / "fixture" / "fixture.kicad_pcb").write_text(SOURCE_FIXTURE, encoding="utf-8")
    return manifest_path


class TestRunBenchExternalCli:
    def test_end_to_end_via_cli(self, tmp_path, monkeypatch):
        from kicad_tools.cli import route_cmd

        monkeypatch.setattr(route_cmd, "main", _make_stub_route(ROUTED_OUTPUT_FIXTURE))

        cache_dir = tmp_path / "cache"
        manifest_path = _write_manifest(tmp_path, cache_dir)
        output_dir = tmp_path / "out"

        args = create_parser().parse_args(
            [
                "bench",
                "external",
                "--board",
                "fixture",
                "--manifest",
                str(manifest_path),
                "--cache-dir",
                str(cache_dir),
                "--output-dir",
                str(output_dir),
                "--skip-fetch",
                "--skip-kicad-cli-drc",
                "--seed",
                "7",
            ]
        )
        rc = bench_cmd.run_bench_command(args)
        assert rc == 0

        json_path = output_dir / "fixture.zero-touch.json"
        assert json_path.exists()
        payload = json.loads(json_path.read_text())
        assert payload["board_id"] == "fixture"
        assert payload["board_commit"] == "a" * 40
        assert payload["completion"]["completion_pct"] == 100.0
        assert payload["kicad_cli_drc"]["ran"] is False

        # Markdown report is named per protocol (issue #4943) so a later
        # --tuned run never overwrites -- or gets conflated with -- this
        # zero-touch report.
        assert (output_dir / "report.zero-touch.md").exists()
        assert not (output_dir / "report.md").exists()
        markdown = (output_dir / "report.zero-touch.md").read_text()
        assert "fixture" in markdown
        assert "zero-touch" in markdown

    def test_unknown_board_slug_errors(self, tmp_path, capsys):
        cache_dir = tmp_path / "cache"
        manifest_path = _write_manifest(tmp_path, cache_dir)
        args = create_parser().parse_args(
            [
                "bench",
                "external",
                "--board",
                "does-not-exist",
                "--manifest",
                str(manifest_path),
                "--cache-dir",
                str(cache_dir),
                "--skip-fetch",
            ]
        )
        assert bench_cmd.run_bench_command(args) == 1
        assert "unknown board slug" in capsys.readouterr().err

    def test_skip_fetch_without_cached_file_produces_no_reports(self, tmp_path, capsys):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        manifest_path = tmp_path / "boards.toml"
        manifest_path.write_text(
            f"""
[fixture]
name = "Fixture Board"
repo_url = "https://example.invalid/fixture"
vcs = "github"
commit = "{"a" * 40}"
board_path = "fixture.kicad_pcb"
license = "MIT"
""",
            encoding="utf-8",
        )
        args = create_parser().parse_args(
            [
                "bench",
                "external",
                "--manifest",
                str(manifest_path),
                "--cache-dir",
                str(cache_dir),
                "--skip-fetch",
            ]
        )
        assert bench_cmd.run_bench_command(args) == 1
        assert "no benchmark reports were produced" in capsys.readouterr().err

    def test_no_subcommand_prints_usage(self, capsys):
        args = create_parser().parse_args(["bench"])
        assert bench_cmd.run_bench_command(args) == 1
        out = capsys.readouterr().out
        assert "Usage: kct bench" in out
        assert "external" in out

    def test_json_format_emits_single_document(self, tmp_path, monkeypatch, capsys):
        from kicad_tools.cli import route_cmd

        monkeypatch.setattr(route_cmd, "main", _make_stub_route(ROUTED_OUTPUT_FIXTURE))

        cache_dir = tmp_path / "cache"
        manifest_path = _write_manifest(tmp_path, cache_dir)
        output_dir = tmp_path / "out"

        args = create_parser().parse_args(
            [
                "bench",
                "external",
                "--board",
                "fixture",
                "--manifest",
                str(manifest_path),
                "--cache-dir",
                str(cache_dir),
                "--output-dir",
                str(output_dir),
                "--skip-fetch",
                "--skip-kicad-cli-drc",
                "--format",
                "json",
            ]
        )
        rc = bench_cmd.run_bench_command(args)
        assert rc == 0

        out = capsys.readouterr().out
        payload = json.loads(out)  # single JSON document -- would raise otherwise
        assert payload["success"] is True
        assert len(payload["reports"]) == 1
        assert payload["reports"][0]["board_id"] == "fixture"
        assert payload["errors"] == {}

    def test_default_boards_run_every_manifest_entry(self, tmp_path, monkeypatch):
        from kicad_tools.cli import route_cmd

        monkeypatch.setattr(route_cmd, "main", _make_stub_route(ROUTED_OUTPUT_FIXTURE))

        cache_dir = tmp_path / "cache"
        manifest_path = tmp_path / "boards.toml"
        manifest_path.write_text(
            f"""
[fixture_a]
name = "Fixture A"
repo_url = "https://example.invalid/a"
vcs = "github"
commit = "{"a" * 40}"
board_path = "a.kicad_pcb"
license = "MIT"

[fixture_b]
name = "Fixture B"
repo_url = "https://example.invalid/b"
vcs = "github"
commit = "{"b" * 40}"
board_path = "b.kicad_pcb"
license = "MIT"
""",
            encoding="utf-8",
        )
        for slug, filename in (("fixture_a", "a.kicad_pcb"), ("fixture_b", "b.kicad_pcb")):
            board_dir = cache_dir / slug
            board_dir.mkdir(parents=True)
            (board_dir / filename).write_text(SOURCE_FIXTURE, encoding="utf-8")

        output_dir = tmp_path / "out"
        args = create_parser().parse_args(
            [
                "bench",
                "external",
                "--manifest",
                str(manifest_path),
                "--cache-dir",
                str(cache_dir),
                "--output-dir",
                str(output_dir),
                "--skip-fetch",
                "--skip-kicad-cli-drc",
            ]
        )
        rc = bench_cmd.run_bench_command(args)
        assert rc == 0
        assert (output_dir / "fixture_a.zero-touch.json").exists()
        assert (output_dir / "fixture_b.zero-touch.json").exists()


# ---------------------------------------------------------------------------
# --tuned protocol, end-to-end (issue #4943)
# ---------------------------------------------------------------------------


class TestTunedProtocolCli:
    def test_tuned_unsupported_board_records_per_board_error(self, tmp_path, monkeypatch, capsys):
        """``--tuned`` against a board with no declared config (real
        ``tuned_rules.py`` only covers STRF) must fail that board with a
        clear message -- never silently fall back to zero-touch rules.
        """
        from kicad_tools.cli import route_cmd

        monkeypatch.setattr(route_cmd, "main", _make_stub_route(ROUTED_OUTPUT_FIXTURE))

        cache_dir = tmp_path / "cache"
        manifest_path = _write_manifest(tmp_path, cache_dir)
        output_dir = tmp_path / "out"

        args = create_parser().parse_args(
            [
                "bench",
                "external",
                "--board",
                "fixture",
                "--manifest",
                str(manifest_path),
                "--cache-dir",
                str(cache_dir),
                "--output-dir",
                str(output_dir),
                "--skip-fetch",
                "--skip-kicad-cli-drc",
                "--tuned",
            ]
        )
        rc = bench_cmd.run_bench_command(args)
        assert rc == 1
        err = capsys.readouterr().err
        assert "fixture" in err
        assert "STRF only" in err

        # No report was produced for the unsupported board.
        assert not (output_dir / "fixture.tuned.json").exists()
        assert not (output_dir / "report.tuned.md").exists()

    def test_tuned_end_to_end_via_cli(self, tmp_path, monkeypatch):
        """A board WITH a declared tuned config (monkeypatched here, since
        production ``tuned_rules.py`` only defines STRF and this fixture
        board isn't STRF) runs the full tuned pipeline: sidecar written,
        ``--net-class-map``/``--differential-pairs`` passed to the router,
        report tagged ``protocol="tuned"``, output files kept separate
        from the zero-touch names.
        """
        from kicad_tools.cli import route_cmd

        captured: list[list[str]] = []
        monkeypatch.setattr(
            route_cmd, "main", _make_capturing_stub_route(ROUTED_OUTPUT_FIXTURE, captured)
        )

        tuned_rules = bench_cmd._load_tuned_rules()
        fake_ncm = {"SIG1": {"name": "Tuned", "trace_width": 0.3}}
        monkeypatch.setattr(
            tuned_rules,
            "build_tuned_net_class_map",
            lambda slug: fake_ncm if slug == "fixture" else None,
        )
        monkeypatch.setattr(
            tuned_rules,
            "diff_pairs_for",
            lambda slug: None,
        )

        cache_dir = tmp_path / "cache"
        manifest_path = _write_manifest(tmp_path, cache_dir)
        output_dir = tmp_path / "out"

        args = create_parser().parse_args(
            [
                "bench",
                "external",
                "--board",
                "fixture",
                "--manifest",
                str(manifest_path),
                "--cache-dir",
                str(cache_dir),
                "--output-dir",
                str(output_dir),
                "--skip-fetch",
                "--skip-kicad-cli-drc",
                "--seed",
                "7",
                "--tuned",
            ]
        )
        rc = bench_cmd.run_bench_command(args)
        assert rc == 0

        # Sidecar written under output_dir/tuned/, matching the monkeypatched config.
        ncm_path = output_dir / "tuned" / "fixture.net_class_map.json"
        assert ncm_path.exists()
        assert json.loads(ncm_path.read_text()) == fake_ncm

        # Router was invoked with the tuned-protocol flags.
        assert len(captured) == 1
        assert "--net-class-map" in captured[0]
        assert str(ncm_path) in captured[0]
        assert "--differential-pairs" in captured[0]

        # Report is tagged "tuned" and kept in tuned-named files, distinct
        # from the zero-touch names.
        json_path = output_dir / "fixture.tuned.json"
        assert json_path.exists()
        payload = json.loads(json_path.read_text())
        assert payload["protocol"] == "tuned"
        assert not (output_dir / "fixture.zero-touch.json").exists()

        assert (output_dir / "report.tuned.md").exists()
        assert not (output_dir / "report.zero-touch.md").exists()
        markdown = (output_dir / "report.tuned.md").read_text()
        assert "tuned" in markdown

    def test_tuned_flag_defaults_false(self):
        """``--tuned`` is opt-in; the bare ``bench external`` subcommand
        stays zero-touch (backward compatible with issue #4941's CLI).
        """
        args = create_parser().parse_args(["bench", "external"])
        assert getattr(args, "tuned", False) is False
