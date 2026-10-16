"""``rss_trace`` must import and no-op without the Unix-only ``resource`` module.

Issue #6739: on Windows ``kct route`` crashed with
``ModuleNotFoundError: No module named 'resource'`` because ``rss_trace``
imported ``resource`` at module level and ``route_cmd`` imports the tracer
unconditionally.  These tests simulate a platform without ``resource`` by
loading a *fresh, separately named* copy of the module while
``sys.modules['resource']`` is ``None`` (which makes ``import resource``
raise ``ImportError``).  The real ``kicad_tools.router.rss_trace`` module is
never reloaded, so other tests are unaffected.
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import textwrap
from types import ModuleType

import pytest

import kicad_tools.router.rss_trace as real_rss_trace


def _load_without_resource(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setitem(sys.modules, "resource", None)
    spec = importlib.util.spec_from_file_location("_rss_trace_no_resource", real_rss_trace.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_import_succeeds_without_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_without_resource(monkeypatch)
    assert mod.resource is None


def test_rss_readings_are_zero_without_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_without_resource(monkeypatch)
    assert mod.peak_rss_bytes() == 0
    assert mod.child_peak_rss_bytes() == 0


def test_tracer_disabled_without_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KCT_RSS_TRACE", raising=False)
    mod = _load_without_resource(monkeypatch)
    stream = io.StringIO()
    tracer = mod.RSSTracer(stream=stream)
    assert tracer.enabled is False
    assert tracer.mark("start") == 0
    assert tracer.marks == []
    assert stream.getvalue() == ""


def test_tracer_enabled_without_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KCT_RSS_TRACE", "1")
    mod = _load_without_resource(monkeypatch)
    stream = io.StringIO()
    tracer = mod.RSSTracer(stream=stream)
    assert tracer.enabled is True
    assert tracer.mark("start") == 0
    assert tracer.mark("end") == 0
    assert [label for label, _, _ in tracer.marks] == ["start", "end"]
    out = stream.getvalue()
    assert "start" in out and "end" in out


def test_real_module_still_has_resource() -> None:
    """The monkeypatched loads above must not leak into the real module."""
    assert real_rss_trace.resource is not None
    assert real_rss_trace.peak_rss_bytes() > 0


def test_cli_imports_with_resource_blocked_in_subprocess() -> None:
    """Mirror the Windows failure: ``resource`` cannot be imported at all."""
    script = textwrap.dedent(
        """
        import sys

        class _BlockResource:
            def find_spec(self, name, path=None, target=None):
                if name == "resource":
                    raise ModuleNotFoundError("No module named 'resource'", name=name)
                return None

        sys.modules.pop("resource", None)
        sys.meta_path.insert(0, _BlockResource())

        import kicad_tools.cli.route_cmd  # noqa: F401
        import kicad_tools.router.rss_trace as rss_trace
        from kicad_tools.router.rss_trace import RSSTracer

        assert rss_trace.resource is None
        RSSTracer().mark("route-start")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=300
    )
    assert result.returncode == 0, result.stderr
