"""Tests for KiCad IPC socket discovery."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from kicad_tools.ipc.discovery import (
    KiCadInstance,
    _get_search_dirs,
    _is_socket,
    discover_instances,
    discover_socket,
)


class TestKiCadInstance:
    """Tests for KiCadInstance dataclass."""

    def test_basic_instance(self):
        inst = KiCadInstance(socket_path=Path("/tmp/kicad/kicad.sock"))
        assert inst.socket_path == Path("/tmp/kicad/kicad.sock")
        assert inst.pid is None
        assert inst.version is None
        assert "KiCad@" in str(inst)

    def test_instance_with_metadata(self):
        inst = KiCadInstance(
            socket_path=Path("/tmp/kicad/kicad.sock"),
            pid=12345,
            version="9.0.1",
        )
        assert "pid=12345" in str(inst)
        assert "v9.0.1" in str(inst)


class TestGetSearchDirs:
    """Tests for platform-specific search directory logic."""

    def test_linux_with_xdg(self):
        with (
            patch("sys.platform", "linux"),
            patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}, clear=False),
        ):
            dirs = _get_search_dirs()
            assert Path("/run/user/1000/kicad") in dirs
            assert Path("/tmp/kicad") in dirs

    def test_linux_without_xdg(self):
        env = dict(os.environ)
        env.pop("XDG_RUNTIME_DIR", None)
        with patch("sys.platform", "linux"), patch.dict(os.environ, env, clear=True):
            dirs = _get_search_dirs()
            assert Path("/tmp/kicad") in dirs

    def test_macos(self):
        with (
            patch("sys.platform", "darwin"),
            patch.dict(os.environ, {"TMPDIR": "/var/folders/xx/T/"}, clear=False),
        ):
            dirs = _get_search_dirs()
            assert Path("/var/folders/xx/T/kicad") in dirs
            assert Path("/tmp/kicad") in dirs

    def test_windows(self):
        with (
            patch("sys.platform", "win32"),
            patch.dict(
                os.environ,
                {"LOCALAPPDATA": "C:\\Users\\test\\AppData\\Local", "TEMP": "C:\\Temp"},
                clear=False,
            ),
        ):
            dirs = _get_search_dirs()
            assert any("kicad" in str(d) for d in dirs)


class TestDiscoverSocket:
    """Tests for socket discovery."""

    def test_explicit_path_exists(self, tmp_path):
        sock = tmp_path / "test.sock"
        sock.touch()
        result = discover_socket(explicit_path=str(sock))
        assert result == sock

    def test_explicit_path_not_exists(self, tmp_path):
        result = discover_socket(explicit_path=str(tmp_path / "nonexistent.sock"))
        assert result is None

    def test_env_variable(self, tmp_path):
        sock = tmp_path / "kicad.sock"
        sock.touch()
        with patch.dict(os.environ, {"KICAD_IPC_SOCKET": str(sock)}):
            result = discover_socket()
        assert result == sock

    def test_env_variable_missing_file(self):
        with patch.dict(os.environ, {"KICAD_IPC_SOCKET": "/nonexistent/socket"}):
            # Should fall through to directory search
            with patch("kicad_tools.ipc.discovery._get_search_dirs", return_value=[]):
                result = discover_socket()
        assert result is None

    def test_directory_search_finds_sock(self, tmp_path):
        kicad_dir = tmp_path / "kicad"
        kicad_dir.mkdir()
        sock = kicad_dir / "kicad.sock"
        sock.touch()

        env = dict(os.environ)
        env.pop("KICAD_IPC_SOCKET", None)
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "kicad_tools.ipc.discovery._get_search_dirs",
                return_value=[kicad_dir],
            ),
        ):
            result = discover_socket()
        assert result == sock

    def test_no_sockets_found(self):
        env = dict(os.environ)
        env.pop("KICAD_IPC_SOCKET", None)
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "kicad_tools.ipc.discovery._get_search_dirs",
                return_value=[],
            ),
        ):
            result = discover_socket()
        assert result is None


class TestDiscoverInstances:
    """Tests for discovering multiple instances."""

    def test_no_instances(self):
        env = dict(os.environ)
        env.pop("KICAD_IPC_SOCKET", None)
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "kicad_tools.ipc.discovery._get_search_dirs",
                return_value=[],
            ),
        ):
            instances = discover_instances()
        assert instances == []

    def test_env_instance(self, tmp_path):
        sock = tmp_path / "kicad.sock"
        sock.touch()
        with (
            patch.dict(os.environ, {"KICAD_IPC_SOCKET": str(sock)}),
            patch(
                "kicad_tools.ipc.discovery._get_search_dirs",
                return_value=[],
            ),
        ):
            instances = discover_instances()
        assert len(instances) == 1
        assert instances[0].socket_path == sock

    def test_multiple_instances(self, tmp_path):
        kicad_dir = tmp_path / "kicad"
        kicad_dir.mkdir()
        sock1 = kicad_dir / "instance1.sock"
        sock2 = kicad_dir / "instance2.sock"
        sock1.touch()
        sock2.touch()

        env = dict(os.environ)
        env.pop("KICAD_IPC_SOCKET", None)
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "kicad_tools.ipc.discovery._get_search_dirs",
                return_value=[kicad_dir],
            ),
        ):
            instances = discover_instances()
        assert len(instances) == 2

    def test_deduplication(self, tmp_path):
        sock = tmp_path / "kicad.sock"
        sock.touch()
        with (
            patch.dict(os.environ, {"KICAD_IPC_SOCKET": str(sock)}),
            patch(
                "kicad_tools.ipc.discovery._get_search_dirs",
                return_value=[tmp_path],
            ),
        ):
            instances = discover_instances()
        # Same socket found via env and dir search should be deduplicated
        assert len(instances) == 1


class TestIsSocket:
    """Tests for socket file detection."""

    def test_regular_file(self, tmp_path):
        f = tmp_path / "regular.txt"
        f.touch()
        assert _is_socket(f) is False

    def test_nonexistent(self, tmp_path):
        assert _is_socket(tmp_path / "nope") is False

    def test_directory(self, tmp_path):
        assert _is_socket(tmp_path) is False
