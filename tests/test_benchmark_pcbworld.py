"""Tests for the PCBWorld D3 suite support module (issue #6331).

Everything third-party is mocked: archives are in-memory tarballs, and the
PCBWorld prep / scorer / ``kicad-cli`` subprocesses are replaced with a fake
``runner``. No network, no KiCad, no PCBWorld checkout is needed.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import re
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTERNAL_DIR = REPO_ROOT / "benchmarks" / "external"

GUIDE = "processed_v9_guide_v3.kicad_pcb"
UNROUTED_PCB = '(kicad_pcb (version 20240108)\n  (net 0 "")\n  (gr_line (start 0 0) (end 1 1)))\n'
NAMES = ["0003_kitspace_grove_adaptor", "0004_S1G-Mod_JST_Adapter", "0008_CapPCB_CapPcb"]


@pytest.fixture(scope="module")
def pcbworld():
    if str(EXTERNAL_DIR) not in sys.path:
        sys.path.insert(0, str(EXTERNAL_DIR))
    spec = importlib.util.spec_from_file_location("pcbworld", EXTERNAL_DIR / "pcbworld.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["pcbworld"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def corpus(pcbworld):
    return pcbworld.load_corpus()


def _tar(top: str, files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _pcbworld_tar(corpus, names=NAMES) -> bytes:
    return _tar(
        f"PCBWorld-{corpus.pcbworld_commit}",
        {
            corpus.splits_file: json.dumps(
                {
                    "d3a": {
                        "boards_json": "configs/datasets/d3.json",
                        "difficulty": "easy",
                        "boards_split": "test",
                    },
                    "d3b": {
                        "boards_json": "configs/datasets/d3.json",
                        "difficulty": "medium",
                        "boards_split": "test",
                    },
                }
            ),  # fmt: skip
            "configs/datasets/d3.json": json.dumps(
                {
                    "_test_overrides": {"medium": {"0373_old": "0376_new"}},
                    "easy": {"test": list(names), "train": ["0001_train"]},
                    "medium": {"test": ["0373_old", "0400_other"]},
                }
            ),
        },
    )


def _pcbench_tar(corpus, names=NAMES) -> bytes:
    files = {}
    for n in names:
        files[f"PCBs/{n[5:]}/board.kicad_pcb"] = "x"
    files["PCBs/other_board/board.kicad_pcb"] = "x"
    return _tar(f"PCBench-{corpus.pcbench_commit}", files)


class FakeOpener:
    def __init__(self, corpus):
        self.corpus = corpus
        self.urls: list[str] = []

    def __call__(self, url):
        self.urls.append(url)
        if self.corpus.pcbworld_commit in url:
            data = _pcbworld_tar(self.corpus)
        elif self.corpus.pcbench_commit in url:
            data = _pcbench_tar(self.corpus)
        else:  # pragma: no cover
            raise OSError("unexpected url")
        return _Resp(data)


class _Resp:
    def __init__(self, data):
        self._d = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._d


def _fake_cli(tmp_path: Path) -> str:
    p = tmp_path / "kicad-cli"
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return str(p)


def make_runner(*, kicad_version="9.0.3", copper_in_guide=False, eval_rows=None, calls=None):
    """Fake subprocess.run covering kicad-cli, the 4 prep steps and eval.pipeline."""
    calls = calls if calls is not None else []

    def runner(argv, **kw):
        calls.append((list(argv), kw))
        ok = lambda out="": SimpleNamespace(returncode=0, stdout=out, stderr="")  # noqa: E731
        if argv[-1] == "--version":
            return ok(kicad_version + "\n")
        env = kw.get("env", {})
        if argv[1].endswith("make_guide.py"):
            base = Path(env["PCBENCH_NEWDRC_OUT"])
            for d in Path(env["PCBENCH_PCBS_ROOT"]).iterdir():
                out = base / d.name
                out.mkdir(parents=True, exist_ok=True)
                text = UNROUTED_PCB + (
                    "(segment (start 0 0) (end 1 1))\n" if copper_in_guide else ""
                )
                (out / GUIDE).write_text(text)
                (out / GUIDE).with_suffix(".kicad_pro").write_text("{}")
        if argv[1].endswith("sort_prefix.py"):
            src, dst = Path(env["PCBENCH_NEWDRC_OUT"]), Path(env["PCBENCH_SORTED_OUT"])
            for i, d in enumerate(sorted(src.iterdir()), 1):
                (dst / f"{i:04d}_{d.name}").mkdir(parents=True, exist_ok=True)
                for f in d.iterdir():
                    (dst / f"{i:04d}_{d.name}" / f.name).write_text(f.read_text())
        if argv[1:3] == ["-m", "eval.pipeline"]:
            cell = Path(argv[argv.index("--output-dir") + 1])
            boards = sorted((cell / "boards").glob("*.kicad_pcb"))
            rows = eval_rows(boards) if eval_rows else []
            with open(cell / "per_rollout.csv", "w", newline="") as f:
                w = csv.DictWriter(
                    f,
                    ["board_id", "rollout_idx", "seed", "eval_status", "eval_error", "clean_pass",
                     "routability", "drv_errors_only_count", "potential_gain", "final_potential"],
                )  # fmt: skip
                w.writeheader()
                w.writerows(rows)
        return ok()

    return runner


@pytest.fixture
def tools(pcbworld, corpus, tmp_path):
    env = {
        "PCBWORLD_PYTHON": "/opt/pcbworld/bin/python",
        "KICAD_CLI": _fake_cli(tmp_path),
        "PCBNEW_PYTHON": "/usr/bin/python3-kicad",
    }
    return pcbworld.resolve_tools(corpus, env, runner=make_runner())


class TestManifest:
    def test_pins_are_full_shas_and_provenance_recorded(self, corpus):
        assert corpus.pcbworld_commit == "b3d62f5c37e7528670d112e03d9a90029f23f4f3"
        assert re.fullmatch(r"[0-9a-f]{40}", corpus.pcbench_commit)
        assert corpus.pcbench_commit.startswith("dec3be75")
        assert corpus.pcbworld_license == "BSD-3-Clause"
        assert "mixed" in corpus.pcbench_license_note.lower()
        assert corpus.kicad_required_major == 9
        assert corpus.splits["d3a"].difficulty == "easy"
        assert corpus.splits["d3b"].difficulty == "medium"

    def test_short_sha_is_rejected(self, pcbworld, tmp_path):
        text = (
            (EXTERNAL_DIR / "pcbworld.toml")
            .read_text()
            .replace("dec3be75cbdef74787625f9043c7391cd473bb64", "dec3be75")
        )
        bad = tmp_path / "bad.toml"
        bad.write_text(text)
        with pytest.raises(pcbworld.PCBWorldError, match="40-hex"):
            pcbworld.load_corpus(bad)

    def test_no_board_list_is_vendored(self):
        text = (EXTERNAL_DIR / "pcbworld.toml").read_text()
        assert "kitspace" not in text and "exacad" not in text.lower()


class TestTools:
    def _env(self, tmp_path, **drop):
        env = {
            "PCBWORLD_PYTHON": "py",
            "KICAD_CLI": _fake_cli(tmp_path),
            "PCBNEW_PYTHON": "pn",
        }
        for k in drop:
            env.pop(k)
        return env

    @pytest.mark.parametrize("var", ["PCBWORLD_PYTHON", "KICAD_CLI", "PCBNEW_PYTHON"])
    def test_missing_variable_is_actionable(self, pcbworld, corpus, tmp_path, var):
        env = self._env(tmp_path)
        env.pop(var)
        with pytest.raises(pcbworld.PCBWorldError, match=var):
            pcbworld.resolve_tools(corpus, env, runner=make_runner())

    def test_kicad_10_is_refused(self, pcbworld, corpus, tmp_path):
        with pytest.raises(pcbworld.PCBWorldError, match="requires KiCad 9"):
            pcbworld.resolve_tools(
                corpus, self._env(tmp_path), runner=make_runner(kicad_version="10.0.1")
            )

    def test_kicad_9_accepted_and_version_recorded(self, pcbworld, corpus, tmp_path):
        t = pcbworld.resolve_tools(
            corpus, self._env(tmp_path), runner=make_runner(kicad_version="9.0.3")
        )
        assert t.kicad_major == 9 and t.kicad_version == "9.0.3"

    def test_missing_cli_binary(self, pcbworld, corpus, tmp_path):
        env = self._env(tmp_path)
        env["KICAD_CLI"] = str(tmp_path / "nope")
        with pytest.raises(pcbworld.PCBWorldError, match="not found"):
            pcbworld.resolve_tools(corpus, env, runner=make_runner())


class TestFetchAndSelection:
    def test_select_is_deterministic_first_k_in_split_order(self, pcbworld, corpus, tmp_path):
        opener = FakeOpener(corpus)
        pw = pcbworld.fetch_pcbworld(corpus, tmp_path, opener=opener)
        assert pcbworld.select_boards(corpus, "d3a", pw, 2) == NAMES[:2]
        assert pcbworld.select_boards(corpus, "d3a", pw, 2) == NAMES[:2]

    def test_test_overrides_applied_for_d3b(self, pcbworld, corpus, tmp_path):
        pw = pcbworld.fetch_pcbworld(corpus, tmp_path, opener=FakeOpener(corpus))
        assert pcbworld.select_boards(corpus, "d3b", pw, 5) == ["0376_new", "0400_other"]

    def test_bad_split_and_limit(self, pcbworld, corpus, tmp_path):
        pw = pcbworld.fetch_pcbworld(corpus, tmp_path, opener=FakeOpener(corpus))
        with pytest.raises(pcbworld.PCBWorldError, match="unknown split"):
            pcbworld.select_boards(corpus, "d9z", pw, 1)
        with pytest.raises(pcbworld.PCBWorldError, match=">= 1"):
            pcbworld.select_boards(corpus, "d3a", pw, 0)

    def test_fetch_pins_urls_and_extracts_only_selected_pcbench_boards(
        self, pcbworld, corpus, tmp_path
    ):
        opener = FakeOpener(corpus)
        pcbworld.fetch_pcbworld(corpus, tmp_path, opener=opener)
        pb = pcbworld.fetch_pcbench(corpus, tmp_path, names=NAMES[:1], opener=opener)
        assert any(corpus.pcbworld_commit in u for u in opener.urls)
        assert any(corpus.pcbench_commit in u for u in opener.urls)
        assert (pb / "PCBs" / NAMES[0][5:]).is_dir()
        assert not (pb / "PCBs" / "other_board").exists()

    def test_second_fetch_is_cached(self, pcbworld, corpus, tmp_path):
        opener = FakeOpener(corpus)
        pcbworld.fetch_pcbworld(corpus, tmp_path, opener=opener)
        pcbworld.fetch_pcbworld(corpus, tmp_path, opener=opener)
        assert len(opener.urls) == 1

    def test_offline_failure_is_actionable(self, pcbworld, corpus, tmp_path):
        def boom(url):
            raise OSError("network unreachable")

        with pytest.raises(pcbworld.PCBWorldError, match="--skip-fetch"):
            pcbworld.fetch_pcbworld(corpus, tmp_path, opener=boom)

    def test_skip_fetch_without_cache(self, pcbworld, corpus, tmp_path):
        with pytest.raises(pcbworld.PCBWorldError, match="--skip-fetch"):
            pcbworld.fetch_pcbworld(corpus, tmp_path, skip_fetch=True)

    def test_stale_archive_rejected(self, pcbworld, corpus, tmp_path):
        wrong = _tar("PCBWorld-" + "0" * 40, {"a": "b"})
        with pytest.raises(pcbworld.PCBWorldError, match="does not reference the pinned"):
            pcbworld.fetch_pcbworld(corpus, tmp_path, opener=lambda url: _Resp(wrong))


class TestPrepare:
    def _prep(self, pcbworld, corpus, tools, tmp_path, runner, names=NAMES[:2]):
        opener = FakeOpener(corpus)
        pw = pcbworld.fetch_pcbworld(corpus, tmp_path, opener=opener)
        pb = pcbworld.fetch_pcbench(corpus, tmp_path, names=names, opener=opener)
        return pcbworld.prepare_boards(
            corpus, tools, split="d3a", names=names, pcbworld_dir=pw, pcbench_dir=pb,
            work_dir=tmp_path / "work", runner=runner,
        )  # fmt: skip

    def test_guide_boards_stable_ids_hashes_and_step_order(self, pcbworld, corpus, tools, tmp_path):
        calls: list = []
        prepared = self._prep(pcbworld, corpus, tools, tmp_path, make_runner(calls=calls))
        assert [b.board_id for b in prepared] == [f"pcbworld/d3a/{n}" for n in NAMES[:2]]
        assert all(b.guide_pcb.name == corpus.guide_board_filename for b in prepared)
        assert all(len(b.input_sha256) == 64 for b in prepared)
        steps = [Path(c[0][1]).stem for c in calls if c[0][-1] != "--version"]
        assert steps == ["convert_v9", "drc_fix_v9", "make_guide", "sort_prefix"]
        # PCBWorld only ever runs as a subprocess under its own interpreter + cwd.
        for argv, kw in calls:
            assert argv[0] == tools.pcbworld_python
            assert kw["cwd"].endswith(corpus.pcbworld_commit[:12])
            assert kw["env"]["KICAD_CLI"] == tools.kicad_cli
        assert "make_guide.py" in " ".join(calls[2][0]) and "_guide_v3" in calls[2][0]

    def test_only_selected_boards_are_staged_for_prep(self, pcbworld, corpus, tools, tmp_path):
        self._prep(pcbworld, corpus, tools, tmp_path, make_runner())
        staged = sorted(p.name for p in (tmp_path / "work" / "PCBs").iterdir())
        assert staged == sorted(n[5:] for n in NAMES[:2])

    def test_rerun_reuses_prepared_boards(self, pcbworld, corpus, tools, tmp_path):
        calls: list = []
        runner = make_runner(calls=calls)
        self._prep(pcbworld, corpus, tools, tmp_path, runner)
        n = len(calls)
        self._prep(pcbworld, corpus, tools, tmp_path, runner)
        assert len(calls) == n

    def test_guide_with_copper_is_rejected(self, pcbworld, corpus, tools, tmp_path):
        with pytest.raises(pcbworld.PCBWorldError, match="track/via"):
            self._prep(pcbworld, corpus, tools, tmp_path, make_runner(copper_in_guide=True))

    def test_failed_step_is_actionable(self, pcbworld, corpus, tools, tmp_path):
        inner = make_runner()

        def runner(argv, **kw):
            if argv[1].endswith("drc_fix_v9.py"):
                return SimpleNamespace(returncode=2, stdout="", stderr="pcbnew import failed")
            return inner(argv, **kw)

        with pytest.raises(pcbworld.PCBWorldError, match=r"(?s)drc_fix_v9.*pcbnew import failed"):
            self._prep(pcbworld, corpus, tools, tmp_path, runner)

    def test_missing_pcbench_folder(self, pcbworld, corpus, tools, tmp_path):
        pw = pcbworld.fetch_pcbworld(corpus, tmp_path, opener=FakeOpener(corpus))
        (tmp_path / "pb" / "PCBs").mkdir(parents=True)
        with pytest.raises(pcbworld.PCBWorldError, match="PCBench source folder"):
            pcbworld.prepare_boards(
                corpus, tools, split="d3a", names=NAMES[:1], pcbworld_dir=pw,
                pcbench_dir=tmp_path / "pb", work_dir=tmp_path / "w2", runner=make_runner(),
            )  # fmt: skip


class TestScoring:
    def _prepared(self, pcbworld, corpus, tools, tmp_path):
        opener = FakeOpener(corpus)
        pw = pcbworld.fetch_pcbworld(corpus, tmp_path, opener=opener)
        pb = pcbworld.fetch_pcbench(corpus, tmp_path, names=NAMES, opener=opener)
        prepared = pcbworld.prepare_boards(
            corpus, tools, split="d3a", names=NAMES, pcbworld_dir=pw, pcbench_dir=pb,
            work_dir=tmp_path / "work", runner=make_runner(),
        )  # fmt: skip
        return pw, prepared

    def test_cp_and_potential_gain_kept_per_rollout_and_missing_output_is_null(
        self, pcbworld, corpus, tools, tmp_path
    ):
        pw, prepared = self._prepared(pcbworld, corpus, tools, tmp_path)
        routed = tmp_path / "r.kicad_pcb"
        routed.write_text(UNROUTED_PCB)
        calls: list = []

        def rows(boards):
            out = []
            for b in boards:
                bid = b.stem.rsplit("_kct_s", 1)[0]
                good = bid == NAMES[0]
                out.append({
                    "board_id": bid, "rollout_idx": 0, "seed": 42, "eval_status": "ok",
                    "eval_error": "", "clean_pass": "True" if good else "False",
                    "routability": 1.0 if good else 0.5, "drv_errors_only_count": 0 if good else 4,
                    "potential_gain": "nan" if good else "2.5", "final_potential": "3.0",
                })  # fmt: skip
            return out

        runner = make_runner(eval_rows=rows, calls=calls)
        inputs = {
            prepared[0].board_id: (prepared[0], routed),
            prepared[1].board_id: (prepared[1], routed),
            prepared[2].board_id: (prepared[2], None),  # router produced nothing
        }
        res = pcbworld.score_boards(
            corpus, tools, inputs, pcbworld_dir=pw, scoring_dir=tmp_path / "score",
            seed=42, runner=runner,
        )  # fmt: skip
        first, second, missing = (res[p.board_id] for p in prepared)
        assert first["available"] and first["clean_pass"] is True
        assert first["potential_gain"] is None  # NaN -> null, not 0
        assert second["clean_pass"] is False and second["potential_gain"] == 2.5
        assert second["rollouts"][0]["potential_gain"] == 2.5
        assert first["comparable"] and first["kicad_version"] == "9.0.3"
        assert first["pcbworld_commit"] == corpus.pcbworld_commit
        assert first["input_sha256"] == prepared[0].input_sha256
        # No output -> unavailable, never CP=0 / pass.
        assert missing["available"] is False
        assert "clean_pass" not in missing and "no output" in missing["unavailable_reason"]
        # Scorer invoked once, as a subprocess with the documented stages.
        argv, kw = calls[0]
        assert argv[1:3] == ["-m", "eval.pipeline"]
        assert "--skip-rollout" in argv and argv[argv.index("--stages") + 1] == "eval,aggregate"
        assert kw["cwd"] == str(pw)
        staged = sorted(p.name for p in (tmp_path / "score" / "kct" / "boards").glob("*.kicad_pcb"))
        assert staged == [f"{n}_kct_s42_r00.kicad_pcb" for n in NAMES[:2]]
        assert (
            (tmp_path / "score" / "kct" / "boards" / staged[0]).with_suffix(".kicad_pro").exists()
        )

    def test_scorer_failure_marks_unavailable(self, pcbworld, corpus, tools, tmp_path):
        pw, prepared = self._prepared(pcbworld, corpus, tools, tmp_path)
        routed = tmp_path / "r.kicad_pcb"
        routed.write_text(UNROUTED_PCB)

        def runner(argv, **kw):
            return SimpleNamespace(returncode=1, stdout="", stderr="engine build missing")

        res = pcbworld.score_boards(
            corpus, tools, {prepared[0].board_id: (prepared[0], routed)}, pcbworld_dir=pw,
            scoring_dir=tmp_path / "s", seed=1, runner=runner,
        )  # fmt: skip
        r = res[prepared[0].board_id]
        assert r["available"] is False and "engine build missing" in r["unavailable_reason"]
        assert r["comparable"] is False

    def test_eval_error_row_is_unavailable(self, pcbworld, corpus, tools, tmp_path):
        pw, prepared = self._prepared(pcbworld, corpus, tools, tmp_path)
        routed = tmp_path / "r.kicad_pcb"
        routed.write_text(UNROUTED_PCB)

        def rows(boards):
            return [{"board_id": NAMES[0], "rollout_idx": 0, "seed": 1, "eval_status": "error",
                     "eval_error": "load failed", "clean_pass": "", }]  # fmt: skip

        res = pcbworld.score_boards(
            corpus, tools, {prepared[0].board_id: (prepared[0], routed)}, pcbworld_dir=pw,
            scoring_dir=tmp_path / "s", seed=1, runner=make_runner(eval_rows=rows),
        )  # fmt: skip
        assert res[prepared[0].board_id]["available"] is False
        assert "load failed" in res[prepared[0].board_id]["unavailable_reason"]


def test_assert_unrouted_counts_segments_vias_arcs(pcbworld, tmp_path):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(UNROUTED_PCB)
    pcbworld.assert_unrouted(p)
    p.write_text(UNROUTED_PCB + "  (via (at 1 1) (size 0.6))\n")
    with pytest.raises(pcbworld.PCBWorldError):
        pcbworld.assert_unrouted(p)
