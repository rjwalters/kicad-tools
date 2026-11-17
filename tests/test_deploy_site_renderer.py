"""deploy-site.sh renderer selection and render-failure handling (Issue #7817)."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy-site.sh"


def _fake(path: Path, name: str, body: str) -> None:
    exe = path / name
    exe.write_text("#!/bin/bash\n" + body + "\n")
    exe.chmod(0o700)


def _run(tmp_path, *, docker=None, kicad_cli=False, uv_rc=0, kct="unset", no_3d=1):
    source = SCRIPT.read_text()
    helpers = source[source.index("# --- Logging helpers") : source.index("# --- Parse flags")]
    block = source[source.index("SKIP_RENDERS=0\n") : source.index("# --- Step 2")]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("bash", "mktemp", "rm", "cat", "printf", "date"):
        found = subprocess.run(["bash", "-c", f"command -v {tool}"], capture_output=True, text=True)
        if found.stdout.strip():
            (bindir / tool).symlink_to(found.stdout.strip())
    if docker is not None:
        _fake(bindir, "docker", f'[ "$1" = info ] && exit {docker}; exit 0')
    if kicad_cli:
        _fake(bindir, "kicad-cli", "exit 0")
    _fake(bindir, "uv", f'echo "uv:$KCT_KICAD_DOCKER:$*"; exit {uv_rc}')
    env = {"PATH": str(bindir), "HOME": str(tmp_path)}
    if kct != "unset":
        env["KCT_KICAD_DOCKER"] = kct
    script = f"NO_3D={no_3d}\n{helpers}\n{block}\necho REACHED_PUBLISH"
    return subprocess.run(
        [str(bindir / "bash"), "-c", script], env=env, capture_output=True, text=True
    )


def _out(r):
    return r.stdout + r.stderr


def test_explicit_docker_but_stopped_aborts(tmp_path):
    r = _run(tmp_path, docker=1, kicad_cli=True, kct="1")
    assert r.returncode == 1
    assert "REACHED_PUBLISH" not in _out(r)
    assert "not running" in _out(r)


def test_unset_docker_stopped_falls_back_to_host(tmp_path):
    r = _run(tmp_path, docker=1, kicad_cli=True)
    assert r.returncode == 0, _out(r)
    assert "Renderer: host kicad-cli" in _out(r)
    assert "installed but not running" in _out(r)
    assert "uv:" in _out(r) and "uv::" in _out(r)  # KCT_KICAD_DOCKER not exported


def test_docker_working_is_chosen(tmp_path):
    r = _run(tmp_path, docker=0)
    assert r.returncode == 0, _out(r)
    assert "Renderer: pinned Docker" in _out(r)
    assert "uv:1:" in _out(r)


def test_docker_disabled_uses_host(tmp_path):
    r = _run(tmp_path, docker=0, kicad_cli=True, kct="0")
    assert "Renderer: host kicad-cli" in _out(r)
    assert "uv:0:" in _out(r)


def test_no_renderer_skips_renders(tmp_path):
    r = _run(tmp_path)
    assert r.returncode == 0
    assert "skipping board renders" in _out(r)
    assert "uv:" not in _out(r)


def test_2d_render_failure_aborts_before_publish(tmp_path):
    r = _run(tmp_path, docker=0, uv_rc=1)
    assert r.returncode == 1
    assert "REACHED_PUBLISH" not in _out(r)
    assert "refusing to publish stale renders" in _out(r)
