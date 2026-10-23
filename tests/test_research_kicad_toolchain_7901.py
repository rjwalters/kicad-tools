"""Research harnesses reach kicad-cli only through the pinned evidence toolchain (Issue #7901).

``scripts/research/diffpair_bench.py`` used to call ``find_kicad_cli()``
directly for its per-pair kicad-cli verdicts, which ignores
``KCT_KICAD_DOCKER=1`` and the pinned release (#6962): on a Mac with Homebrew
KiCad 10.0.7 its "kicad-cli" columns came from the host build.  Pinned here:

* no research harness that grades with kicad-cli looks one up on its own;
* a kicad-cli verdict outside ``ere.kicad_toolchain_session`` refuses instead
  of falling back to a host lookup;
* the session is ``evidence_toolchain`` (pinned release required unless
  ``--allow-unpinned-kicad``), and ``KCT_KICAD_DOCKER=1`` sends the bench's
  kicad-cli through the pinned image;
* a refused toolchain exits 2 and writes no result file;
* every bench row records the kicad-cli version and image it used.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from kicad_tools.cli import kicad_toolchain as toolchain
from kicad_tools.cli import runner

REPO_ROOT = Path(__file__).resolve().parent.parent
RESEARCH = REPO_ROOT / "scripts" / "research"
BENCH = RESEARCH / "diffpair_bench.py"

#: Research scripts whose kicad-cli numbers are recorded as results.
KICAD_CLI_HARNESSES = ("diffpair_bench.py", "expand_route_experiment.py", "shrink_experiment.py")

#: Ways of reaching a kicad-cli that bypass ``evidence_toolchain``.
_BYPASS_NAMES = {"find_kicad_cli", "locate_kicad_cli", "_locate_kicad_cli"}


def _load():
    sys.path.insert(0, str(RESEARCH))
    spec = importlib.util.spec_from_file_location("diffpair_bench_7901", BENCH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


bench = _load()
ere = bench.ere

FAKE_SESSION = {
    "kicad_cli_version": toolchain.PINNED_KICAD_VERSION,
    "kicad_cli_image": toolchain.PINNED_KICAD_IMAGE,
    "kicad_cli_source": "docker",
    "kicad_cli_pinned": True,
    "kicad_cli_binary": "/fake/kicad-cli",
}


# ---------------------------------------------------------------------------
# Static: no harness looks a kicad-cli up on its own
# ---------------------------------------------------------------------------


#: DRC wrappers that fall back to ``find_kicad_cli()`` (the host build) unless
#: handed the session's binary.  This is the path the original ``grade`` bug
#: took: ``run_kicad_cli_drc`` -> ``run_geometric_drc`` -> ``find_kicad_cli``.
_DRC_WRAPPERS = {"run_kicad_cli_drc", "run_geometric_drc"}

#: Callables that run a command line given as one shell string.
_SHELL_RUNNERS = {"system", "popen", "getoutput", "getstatusoutput"}
_SUBPROCESS_RUNNERS = {"run", "call", "check_call", "check_output", "Popen"}


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_kicad_cli_exe(node: ast.AST) -> bool:
    """A string literal naming the bare executable: ``"kicad-cli"`` or ``".../kicad-cli"``."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and (node.value == "kicad-cli" or node.value.endswith("/kicad-cli"))
    )


def _is_kicad_cli_command_line(node: ast.AST) -> bool:
    """A string or f-string literal that starts a ``kicad-cli ...`` command line."""
    if isinstance(node, ast.JoinedStr):
        node = node.values[0] if node.values else node
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.split(" ", 1)[0] in ("kicad-cli", "kicad-cli.exe")
        and " " in node.value
    )


def _bypass_findings(source: str) -> list[str]:
    """Every way *source* reaches a kicad-cli without the session's binary.

    AST-based, so comments, docstrings and table headers that merely mention
    kicad-cli are not flagged; only code that would run one is.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.ImportFrom):
            found += [f"import {a.name}" for a in node.names if a.name in _BYPASS_NAMES]
        if name in _BYPASS_NAMES:
            found.append(f"line {node.lineno}: {name}")

        # A bare executable literal in an argv list/tuple, e.g.
        # ``subprocess.run(["kicad-cli", "pcb", "drc", b])``.
        if isinstance(node, (ast.List, ast.Tuple)) and any(map(_is_kicad_cli_exe, node.elts)):
            found.append(f"line {node.lineno}: 'kicad-cli' argv literal")

        if not isinstance(node, ast.Call):
            continue
        fname = _call_name(node.func)
        args = list(node.args) + [k.value for k in node.keywords]

        # ``shutil.which("kicad-cli")``, ``Popen(executable="kicad-cli")``,
        # ``os.execvp("kicad-cli", ...)``: the executable passed straight in.
        if any(map(_is_kicad_cli_exe, args)):
            found.append(f"line {node.lineno}: {fname}('kicad-cli')")

        # ``os.system("kicad-cli pcb drc ...")`` / ``subprocess.run(f"kicad-cli ...", shell=True)``.
        if fname in _SHELL_RUNNERS | _SUBPROCESS_RUNNERS and any(
            map(_is_kicad_cli_command_line, args)
        ):
            found.append(f"line {node.lineno}: {fname}('kicad-cli ...') command line")

        # A DRC wrapper without the session's binary looks the host one up.
        if fname in _DRC_WRAPPERS:
            kw = {k.arg: k.value for k in node.keywords}
            given = kw.get("kicad_cli")
            splatted = any(k.arg is None for k in node.keywords)
            if not splatted and (
                given is None or (isinstance(given, ast.Constant) and given.value is None)
            ):
                found.append(f"line {node.lineno}: {fname}() without kicad_cli=")
    return found


def _bypasses(path: Path) -> list[str]:
    return _bypass_findings(path.read_text())


@pytest.mark.parametrize("name", KICAD_CLI_HARNESSES)
def test_harness_never_looks_up_kicad_cli_itself(name):
    assert _bypasses(RESEARCH / name) == []


def test_bypass_detector_catches_the_old_bench_code(tmp_path):
    old = tmp_path / "old.py"
    old.write_text(
        "def f():\n"
        "    from kicad_tools.cli.runner import find_kicad_cli\n"
        "    exe = find_kicad_cli()\n"
        "    import shutil\n"
        "    return shutil.which('kicad-cli')\n"
    )
    assert len(_bypasses(old)) == 3


#: One snippet per bypass form the scanner must flag (Judge review of #7904).
BYPASS_SNIPPETS = {
    "subprocess_argv_list": "import subprocess\nsubprocess.run(['kicad-cli', 'pcb', 'drc', str(b)])\n",
    "subprocess_argv_tuple": "import subprocess\nsubprocess.check_output(('kicad-cli', 'version'))\n",
    "argv_built_first": "argv = ['kicad-cli', 'pcb', 'drc']\nrun(argv + [b])\n",
    "absolute_path_argv": "subprocess.run(['/usr/bin/kicad-cli', 'version'])\n",
    "popen_executable_kw": "subprocess.Popen(['x'], executable='kicad-cli')\n",
    "execvp": "import os\nos.execvp('kicad-cli', ['kicad-cli', 'version'])\n",
    "os_system_string": "import os\nos.system('kicad-cli pcb drc board.kicad_pcb')\n",
    "shell_fstring": "subprocess.run(f'kicad-cli pcb drc {b}', shell=True)\n",
    "shutil_which": "import shutil\nshutil.which('kicad-cli')\n",
    "find_kicad_cli": "from kicad_tools.cli.runner import find_kicad_cli\nfind_kicad_cli()\n",
    "run_kicad_cli_drc_no_kw": (
        "from kicad_tools.benchmark.external.metrics import run_kicad_cli_drc\n"
        "run_kicad_cli_drc(b, timeout=600, all_track_errors=True)\n"
    ),
    "run_kicad_cli_drc_kw_none": "metrics.run_kicad_cli_drc(b, kicad_cli=None)\n",
    "run_geometric_drc_no_kw": (
        "from kicad_tools.drc.geometric import run_geometric_drc\nrun_geometric_drc(b)\n"
    ),
    "run_geometric_drc_attr": "geometric.run_geometric_drc(b, timeout=60)\n",
}


@pytest.mark.parametrize("snippet", BYPASS_SNIPPETS.values(), ids=BYPASS_SNIPPETS.keys())
def test_bypass_detector_flags_every_bypass_form(snippet):
    assert _bypass_findings(snippet), "scanner missed a kicad-cli bypass"


def test_bypass_detector_passes_session_routed_code():
    clean = (
        '"""Mentions kicad-cli in a docstring; runs it only via the session."""\n'
        "import subprocess\n"
        "from pathlib import Path\n"
        "from kicad_tools.benchmark.external.metrics import run_kicad_cli_drc\n"
        "from kicad_tools.drc.geometric import run_geometric_drc\n"
        "# kicad-cli is the session's (a comment that mentions kicad-cli)\n"
        "def grade(b, session, **kw):\n"
        "    exe = Path(session['kicad_cli_binary'])\n"
        "    run_kicad_cli_drc(b, timeout=600, kicad_cli=exe)\n"
        "    run_geometric_drc(b, kicad_cli=exe)\n"
        "    run_geometric_drc(b, **kw)  # caller supplies kicad_cli\n"
        "    subprocess.run([str(exe), 'pcb', 'drc', str(b)])\n"
        "    head = '| Board | kicad-cli |\\n'\n"
        "    print('kicad-cli verdicts must run inside a session')\n"
        "    return head\n"
    )
    assert _bypass_findings(clean) == []


# ---------------------------------------------------------------------------
# Outside a session: refuse, never fall back to a host lookup
# ---------------------------------------------------------------------------


@pytest.fixture
def no_host_lookup(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("kicad-cli looked up outside the evidence toolchain")

    monkeypatch.setattr(runner, "find_kicad_cli", forbidden)
    monkeypatch.setattr(runner, "locate_kicad_cli", forbidden)
    monkeypatch.setattr(ere, "_KICAD_SESSION", None)


def test_pair_verdicts_outside_a_session_refuse(tmp_path, no_host_lookup):
    pairs = [{"pair": "A", "nets": ["A_P", "A_N"]}]
    with pytest.raises(ere.KiCadToolchainRequired, match="#7901"):
        bench.kicad_pair_verdicts(tmp_path / "b.kicad_pcb", pairs)
    assert "kicad_cli_verdict" not in pairs[0]


def test_grade_outside_a_session_refuses_before_running(tmp_path, monkeypatch, no_host_lookup):
    from kicad_tools.benchmark.external import metrics

    def forbidden(*_a, **_k):
        raise AssertionError("grade ran something outside the evidence toolchain")

    for name in ("measure_completion", "run_kct_check", "run_kicad_cli_drc"):
        monkeypatch.setattr(metrics, name, forbidden)
    with pytest.raises(ere.KiCadToolchainRequired):
        ere.grade(tmp_path / "b.kicad_pcb", ere.BOARDS["03"])


# ---------------------------------------------------------------------------
# The session is evidence_toolchain
# ---------------------------------------------------------------------------


def _fake_evidence_toolchain(seen: dict, version: str, image: str | None):
    @contextlib.contextmanager
    def fake(mounts=(), *, required_version=toolchain.PINNED_KICAD_VERSION):
        seen["mounts"] = list(mounts)
        seen["required_version"] = required_version
        yield toolchain.EvidenceToolchain("/x/kicad-cli", version, Path("/cfg"), image)

    return fake


def test_session_requires_the_pinned_release_by_default(monkeypatch, tmp_path):
    seen: dict = {}
    fake = _fake_evidence_toolchain(seen, toolchain.PINNED_KICAD_VERSION, "img@sha256:abc")
    monkeypatch.setattr(toolchain, "evidence_toolchain", fake)
    with ere.kicad_toolchain_session([tmp_path]) as record:
        assert ere.kicad_cli_session()["kicad_cli_binary"] == "/x/kicad-cli"
    assert seen["required_version"] == toolchain.PINNED_KICAD_VERSION
    assert ere.REPO in seen["mounts"] and tmp_path in seen["mounts"]
    assert record["kicad_cli_image"] == "img@sha256:abc"
    assert record["kicad_cli_source"] == "docker" and record["kicad_cli_pinned"] is True
    assert ere._KICAD_SESSION is None  # cleared on exit


def test_allow_unpinned_labels_the_host_build(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        toolchain, "evidence_toolchain", _fake_evidence_toolchain(seen, "10.0.7", None)
    )
    with ere.kicad_toolchain_session(allow_unpinned=True) as record:
        pass
    assert seen["required_version"] is None
    assert record["kicad_cli_pinned"] is False
    assert record["kicad_cli_image"] is None and record["kicad_cli_source"] == "host"
    assert bench.kicad_cli_cell(record) == "UNPINNED 10.0.7 host"
    assert bench.kicad_cli_cell({}) == "unrecorded"
    assert bench.kicad_cli_cell(FAKE_SESSION) == f"{toolchain.PINNED_KICAD_VERSION} image"


# ---------------------------------------------------------------------------
# KCT_KICAD_DOCKER=1 end to end: the verdict DRC runs in the pinned image
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell fake")
def test_docker_route_runs_the_bench_verdict_in_the_pinned_image(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "docker.log"
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "violations": [
                    {
                        "type": "skew_out_of_range",
                        "severity": "error",
                        "items": [{"description": "Track [A_P] on F.Cu"}],
                    }
                ]
            }
        )
    )
    docker = bindir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" >> {calls}\n'
        'out=""; prev=""\n'
        'for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
        f'if [ -n "$out" ]; then cat {report} > "$out"; exit 5; fi\n'
        f"echo {toolchain.PINNED_KICAD_VERSION}\n"
    )
    docker.chmod(0o755)
    host = bindir / "kicad-cli"  # a host 10.0.7 that must NOT be used
    host.write_text("#!/bin/sh\necho 10.0.7\n")
    host.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bindir), "/usr/bin", "/bin"]))
    monkeypatch.setenv(toolchain.KICAD_DOCKER_ENV, "1")
    monkeypatch.delenv(toolchain.KICAD_DOCKER_IMAGE_ENV, raising=False)
    monkeypatch.setattr(runner, "_kicad_cli_candidates", lambda: [Path(shutil.which("kicad-cli"))])
    runner.find_kicad_cli.cache_clear()

    routed = tmp_path / "work" / "b.kicad_pcb"
    routed.parent.mkdir()
    routed.write_text("(kicad_pcb)")
    routed.with_suffix(".kicad_dru").write_text(
        '(rule "kct diff pair skew - A_P/A_N")\n(rule "kct diff pair vias - A_P/A_N")\n'
    )
    pairs = [{"pair": "A", "nets": ["A_P", "A_N"]}]
    try:
        with ere.kicad_toolchain_session([routed.parent]) as record:
            bench.kicad_pair_verdicts(routed, pairs)
    finally:
        runner.find_kicad_cli.cache_clear()

    assert record["kicad_cli_image"] == toolchain.pinned_kicad_image()
    assert record["kicad_cli_version"] == toolchain.PINNED_KICAD_VERSION
    assert record["kicad_cli_pinned"] is True
    argv = calls.read_text().split("\n")
    assert "drc" in argv and argv[argv.index("--entrypoint") + 2] == toolchain.pinned_kicad_image()
    assert pairs[0]["kicad_cli_verdict"] == {
        "skew": "fail",
        "uncoupled": None,
        "vias": "pass",
        "gap": None,
    }


# ---------------------------------------------------------------------------
# The bench command: refuse, or record the toolchain in every row
# ---------------------------------------------------------------------------


def _run_argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "run",
        "--boards",
        "03,06rf",
        "--engines",
        "grid",
        "--seeds",
        "42",
        "--label",
        "t",
        "--work-dir",
        str(tmp_path / "work"),
        "--out-dir",
        str(tmp_path / "out"),
        *extra,
    ]


def test_refused_toolchain_exits_2_and_writes_nothing(tmp_path, monkeypatch, capsys):
    @contextlib.contextmanager
    def refuse(mounts=(), *, required_version=None):
        raise toolchain.KiCadEnvironmentError("kicad-cli 10.0.7 is not the qualified toolchain")
        yield  # pragma: no cover

    monkeypatch.setattr(toolchain, "evidence_toolchain", refuse)
    monkeypatch.setattr(bench, "run_cell", lambda *a, **k: pytest.fail("routed while refused"))
    assert bench.main(_run_argv(tmp_path)) == ere.REFUSED_EXIT
    assert "REFUSED" in capsys.readouterr().err
    assert not list((tmp_path / "out").glob("*.json"))
    # Not even the empty directories: they are created only after the check.
    assert not (tmp_path / "out").exists() and not (tmp_path / "work").exists()


def test_every_bench_row_records_the_kicad_cli_it_used(tmp_path, monkeypatch):
    seen: dict = {}
    fake = _fake_evidence_toolchain(seen, toolchain.PINNED_KICAD_VERSION, "kicad/kicad@sha256:ab")
    monkeypatch.setattr(toolchain, "evidence_toolchain", fake)

    def fake_cell(board, engine, seed, *_a, **_k):
        # A refused-before-grading row carries no kicad-cli columns of its own.
        return {"board": board.key, "engine": engine, "seed": seed, "valid": False}

    monkeypatch.setattr(bench, "run_cell", fake_cell)
    bench.main(_run_argv(tmp_path))
    (out,) = (tmp_path / "out").glob("t-*.json")
    doc = json.loads(out.read_text())
    assert doc["kicad_cli"]["kicad_cli_image"] == "kicad/kicad@sha256:ab"
    assert len(doc["rows"]) == 2
    for row in doc["rows"]:
        assert row["kicad_cli_version"] == toolchain.PINNED_KICAD_VERSION
        assert row["kicad_cli_image"] == "kicad/kicad@sha256:ab"
        assert row["kicad_cli_pinned"] is True
        assert "kicad_cli_binary" not in row  # a scratch path, not provenance
    assert (tmp_path / "work").resolve() in seen["mounts"]
    assert seen["required_version"] == toolchain.PINNED_KICAD_VERSION


def test_allow_unpinned_flag_reaches_the_session(tmp_path, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        toolchain, "evidence_toolchain", _fake_evidence_toolchain(seen, "10.0.7", None)
    )
    monkeypatch.setattr(
        bench, "run_cell", lambda b, e, s, *a, **k: {"board": b.key, "engine": e, "seed": s}
    )
    bench.main(_run_argv(tmp_path, "--allow-unpinned-kicad"))
    assert seen["required_version"] is None
    (out,) = (tmp_path / "out").glob("t-*.json")
    assert all(r["kicad_cli_pinned"] is False for r in json.loads(out.read_text())["rows"])
