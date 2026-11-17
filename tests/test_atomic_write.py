"""Tests for the shared atomic-write primitive (issue #4898).

Issue #4898 (Part of #4880's Konnect-ideas audit, item 3) hardened the
low-level ``.kicad_pcb``/``.kicad_sch``/``.kicad_pro``/``.kicad_mod``/
``.kicad_dru`` writers -- previously a plain, non-atomic ``write_text`` --
to go through a shared ``atomic_write_text`` helper
(``kicad_tools.core.atomic_write``) that writes to a sibling ``.tmp`` file,
``fsync``s it, then ``os.replace``s it onto the destination. This mirrors
the pattern ``route_cmd.py``'s ``_write_routed_pcb`` already used for the
routed-PCB output path (issue #2808).

Covers:

1. ``atomic_write_text`` itself -- normal write, atomic-failure semantics
   (a failure during ``os.replace`` must leave the pre-existing destination
   file unchanged, never torn/truncated), and the ``.tmp`` sibling naming.
2. Each of the five hardened writers (``save_pcb``, ``save_schematic``,
   ``save_project``, ``save_footprint``, ``save_design_rules``) actually
   goes through the shared helper (verified by patching ``os.replace`` and
   observing the same atomic-failure behavior end-to-end).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.core.atomic_write import atomic_write_text
from kicad_tools.core.project_file import save_project
from kicad_tools.core.sexp_file import (
    save_design_rules,
    save_footprint,
    save_pcb,
    save_schematic,
)
from kicad_tools.sexp import parse_string

# ---------------------------------------------------------------------------
# atomic_write_text unit tests
# ---------------------------------------------------------------------------


class TestAtomicWriteText:
    def test_writes_content_to_destination(self, tmp_path: Path):
        dest = tmp_path / "out.txt"
        atomic_write_text(dest, "hello world")
        assert dest.read_text() == "hello world"

    def test_no_tmp_sibling_left_behind_on_success(self, tmp_path: Path):
        dest = tmp_path / "out.txt"
        atomic_write_text(dest, "hello world")
        assert not dest.with_suffix(dest.suffix + ".tmp").exists()

    def test_replace_failure_leaves_existing_file_unchanged(self, tmp_path: Path):
        """A failure during ``os.replace`` must not clobber pre-existing
        content at the destination -- the write is atomic: either the new
        content lands, or the old content survives untouched. Never torn."""
        dest = tmp_path / "out.txt"
        SENTINEL = "sentinel-content-must-not-be-clobbered"
        dest.write_text(SENTINEL)

        with patch("kicad_tools.core.atomic_write.os.replace", side_effect=OSError("simulated")):
            with pytest.raises(OSError):
                atomic_write_text(dest, "new content")

        assert dest.read_text() == SENTINEL

    def test_tmp_file_uses_dot_tmp_suffix(self, tmp_path: Path):
        dest = tmp_path / "out.txt"
        with patch("kicad_tools.core.atomic_write.os.replace", side_effect=OSError("simulated")):
            with pytest.raises(OSError):
                atomic_write_text(dest, "new content")

        expected_tmp = dest.with_suffix(dest.suffix + ".tmp")
        assert expected_tmp.exists()
        assert expected_tmp.read_text() == "new content"

    def test_encoding_none_falls_back_to_write_text_default(self, tmp_path: Path):
        """``encoding=None`` preserves the historical unparameterized
        ``Path.write_text(content)`` call shape some callers relied on."""
        dest = tmp_path / "out.txt"
        atomic_write_text(dest, "plain", encoding=None)
        assert dest.read_text() == "plain"


# ---------------------------------------------------------------------------
# The five hardened writers route through atomic_write_text
# ---------------------------------------------------------------------------

_MINIMAL_PCB = '(kicad_pcb\n  (version 20240108)\n  (generator "test")\n)\n'
_MINIMAL_SCH = '(kicad_sch\n  (version 20240108)\n  (generator "test")\n)\n'
_MINIMAL_FOOTPRINT = '(footprint "test:fp"\n  (version 20240108)\n)\n'
_MINIMAL_DRU = '(version 1)\n(rule "test" (constraint clearance (min 0.2mm)))\n'


class TestHardenedWritersAreAtomic:
    """Each writer must leave a pre-existing destination file unchanged if
    the underlying ``os.replace`` fails -- proof that it goes through the
    shared atomic helper rather than a plain ``write_text``."""

    def test_save_pcb_is_atomic(self, tmp_path: Path):
        sexp = parse_string(_MINIMAL_PCB)
        dest = tmp_path / "board.kicad_pcb"
        SENTINEL = "(kicad_pcb (sentinel))"
        dest.write_text(SENTINEL)

        with patch("kicad_tools.core.atomic_write.os.replace", side_effect=OSError("simulated")):
            with pytest.raises(OSError):
                save_pcb(sexp, dest)

        assert dest.read_text() == SENTINEL

    def test_save_pcb_normal_write_roundtrips(self, tmp_path: Path):
        sexp = parse_string(_MINIMAL_PCB)
        dest = tmp_path / "board.kicad_pcb"
        save_pcb(sexp, dest)
        assert dest.exists()
        assert "kicad_pcb" in dest.read_text()
        assert not dest.with_suffix(dest.suffix + ".tmp").exists()

    def test_save_schematic_is_atomic(self, tmp_path: Path):
        sexp = parse_string(_MINIMAL_SCH)
        dest = tmp_path / "board.kicad_sch"
        SENTINEL = "(kicad_sch (sentinel))"
        dest.write_text(SENTINEL)

        with patch("kicad_tools.core.atomic_write.os.replace", side_effect=OSError("simulated")):
            with pytest.raises(OSError):
                save_schematic(sexp, dest)

        assert dest.read_text() == SENTINEL

    def test_save_footprint_is_atomic(self, tmp_path: Path):
        sexp = parse_string(_MINIMAL_FOOTPRINT)
        dest = tmp_path / "fp.kicad_mod"
        SENTINEL = "(footprint (sentinel))"
        dest.write_text(SENTINEL)

        with patch("kicad_tools.core.atomic_write.os.replace", side_effect=OSError("simulated")):
            with pytest.raises(OSError):
                save_footprint(sexp, dest)

        assert dest.read_text() == SENTINEL

    def test_save_design_rules_is_atomic(self, tmp_path: Path):
        from kicad_tools.core.sexp_file import load_design_rules

        # Write once normally so we have a real design_rules SExp to re-save.
        tmp_seed = tmp_path / "seed.kicad_dru"
        tmp_seed.write_text(_MINIMAL_DRU)
        sexp = load_design_rules(tmp_seed)

        dest = tmp_path / "rules.kicad_dru"
        SENTINEL = "(version 999)"
        dest.write_text(SENTINEL)

        with patch("kicad_tools.core.atomic_write.os.replace", side_effect=OSError("simulated")):
            with pytest.raises(OSError):
                save_design_rules(sexp, dest)

        assert dest.read_text() == SENTINEL

    def test_save_project_is_atomic(self, tmp_path: Path):
        dest = tmp_path / "proj.kicad_pro"
        SENTINEL = '{"sentinel": true}'
        dest.write_text(SENTINEL)

        with patch("kicad_tools.core.atomic_write.os.replace", side_effect=OSError("simulated")):
            with pytest.raises(OSError):
                save_project({"meta": {"version": 1}}, dest)

        assert dest.read_text() == SENTINEL

    def test_save_project_normal_write_roundtrips(self, tmp_path: Path):
        dest = tmp_path / "proj.kicad_pro"
        data = {"meta": {"version": 1, "filename": "proj.kicad_pro"}}
        save_project(data, dest)
        assert json.loads(dest.read_text()) == data
        assert not dest.with_suffix(dest.suffix + ".tmp").exists()


# ---------------------------------------------------------------------------
# Issue #7478: fsync must use a handle opened for writing
# ---------------------------------------------------------------------------
#
# On Windows, CPython's ``os.fsync`` calls the CRT ``_commit()``, which calls
# ``FlushFileBuffers``; that needs write access, so fsyncing a handle opened
# ``"rb"`` raises ``OSError(EBADF)``. Every ``PCB.save`` failed there, leaving
# ``<file>.tmp`` behind. POSIX ``fsync`` accepts a read-only fd, so these tests
# emulate the Windows behaviour by wrapping ``os.fsync`` to reject an fd whose
# access mode is ``O_RDONLY``. They fail on the pre-fix code on any platform
# with ``fcntl``; the Windows CI smoke job covers the real platform.

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "kicad_tools"


def _windows_like_fsync(monkeypatch: pytest.MonkeyPatch, module_os_path: str) -> list[int]:
    """Patch ``<module>.os.fsync`` to raise EBADF on a read-only fd.

    Returns a list that records every fd that was fsynced successfully, so a
    test can assert the fsync actually happened (not just that it was skipped).
    """
    fcntl = pytest.importorskip("fcntl")
    import errno
    import os as _os

    real_fsync = _os.fsync
    synced: list[int] = []

    def fsync(fd: int) -> None:
        if fcntl.fcntl(fd, fcntl.F_GETFL) & _os.O_ACCMODE == _os.O_RDONLY:
            raise OSError(errno.EBADF, "Bad file descriptor (emulated Windows _commit)")
        real_fsync(fd)
        synced.append(fd)

    monkeypatch.setattr(f"{module_os_path}.os.fsync", fsync)
    return synced


class TestFsyncUsesWritableHandle:
    def test_atomic_write_text_under_windows_fsync(self, tmp_path: Path, monkeypatch):
        synced = _windows_like_fsync(monkeypatch, "kicad_tools.core.atomic_write")
        dest = tmp_path / "x.kicad_pcb"
        atomic_write_text(dest, "x")
        assert dest.read_text() == "x"
        assert not (tmp_path / "x.kicad_pcb.tmp").exists()
        assert synced, "atomic_write_text must still fsync the tmp file"

    def test_atomic_write_text_preserves_bytes(self, tmp_path: Path, monkeypatch):
        """Opening ``r+b`` for the fsync must not truncate or alter the
        bytes ``write_text`` produced (newline/encoding branches)."""
        _windows_like_fsync(monkeypatch, "kicad_tools.core.atomic_write")
        dest = tmp_path / "x.kicad_pcb"
        atomic_write_text(dest, "a\nbµ\n", newline="")
        assert dest.read_bytes() == "a\nbµ\n".encode()

    def test_pcb_load_save_roundtrip_under_windows_fsync(self, tmp_path: Path, monkeypatch):
        from kicad_tools.schema.pcb import PCB

        _windows_like_fsync(monkeypatch, "kicad_tools.core.atomic_write")
        src = tmp_path / "board.kicad_pcb"
        src.write_text(_MINIMAL_PCB)
        pcb = PCB.load(src)
        pcb.save(src)
        assert "kicad_pcb" in src.read_text()
        assert not src.with_suffix(src.suffix + ".tmp").exists()
        PCB.load(src)

    def test_transaction_restore_under_windows_fsync(self, tmp_path: Path, monkeypatch):
        from kicad_tools.transaction import board_transaction

        synced = _windows_like_fsync(monkeypatch, "kicad_tools.transaction")
        board = tmp_path / "board.kicad_pcb"
        board.write_bytes(b"original")
        with board_transaction(board, keep_failed=False) as txn:
            board.write_bytes(b"mutated")
            txn.rollback()
        assert board.read_bytes() == b"original"
        assert not list(tmp_path.glob("*.tmp"))
        assert synced

    def test_write_routed_pcb_under_windows_fsync(self, tmp_path: Path, monkeypatch):
        from kicad_tools.cli import route_cmd

        synced = _windows_like_fsync(monkeypatch, "kicad_tools.cli.route_cmd")
        monkeypatch.setenv("KCT_KICAD_LOCK_POLICY", "ignore")
        source = tmp_path / "source.kicad_pcb"
        source.write_text("(kicad_pcb (version 20240108))")
        output = tmp_path / "routed.kicad_pcb"
        assert route_cmd._write_routed_pcb(source, output, "") == output
        assert "kicad_pcb" in output.read_text()
        assert not output.with_suffix(output.suffix + ".tmp").exists()
        assert synced

    def test_save_partial_results_under_windows_fsync(self, tmp_path: Path, monkeypatch):
        from types import SimpleNamespace

        from kicad_tools.cli import route_cmd

        synced = _windows_like_fsync(monkeypatch, "kicad_tools.cli.route_cmd")
        monkeypatch.setenv("KCT_KICAD_LOCK_POLICY", "ignore")
        source = tmp_path / "source.kicad_pcb"
        source.write_text("(kicad_pcb (version 20240108))")
        output = tmp_path / "routed.kicad_pcb"
        router = SimpleNamespace(routes=[1], to_sexp=lambda **kwargs: "(segment (width 0.2))")
        monkeypatch.setattr(
            route_cmd,
            "_interrupt_state",
            {"router": router, "output_path": output, "pcb_path": source, "quiet": True},
        )
        assert route_cmd._save_partial_results() is True
        partial = output.with_stem("routed_partial")
        assert "segment" in partial.read_text()
        assert not partial.with_suffix(partial.suffix + ".tmp").exists()
        assert synced


def _is_fsync_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "fsync"
    )


def _open_mode(call: ast.Call) -> str | None:
    """Literal mode of an ``open(...)`` call, ``"r"`` if omitted, else None."""
    if not (isinstance(call.func, ast.Name) and call.func.id == "open"):
        return None
    mode_node: ast.expr | None = call.args[1] if len(call.args) > 1 else None
    for kw in call.keywords:
        if kw.arg == "mode":
            mode_node = kw.value
    if mode_node is None:
        return "r"
    if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        return mode_node.value
    return None


def test_no_read_only_open_feeds_fsync_in_src():
    """Structural guard (issue #7478): no ``with open(..., "rb")`` (or any
    read-only mode) block in ``src/`` contains an ``fsync`` call."""
    offenders: list[str] = []
    for py in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.With, ast.AsyncWith)):
                continue
            if not any(_is_fsync_call(n) for stmt in node.body for n in ast.walk(stmt)):
                continue
            for item in node.items:
                expr = item.context_expr
                if not isinstance(expr, ast.Call):
                    continue
                mode = _open_mode(expr)
                if mode is not None and not any(c in mode for c in "wax+"):
                    rel = py.relative_to(_SRC_ROOT.parent.parent)
                    offenders.append(f"{rel}:{node.lineno} open(..., {mode!r})")
    assert not offenders, "fsync on a read-only handle fails on Windows: " + ", ".join(offenders)
