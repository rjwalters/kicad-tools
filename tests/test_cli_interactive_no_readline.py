"""kct.cli.interactive must import and run where ``readline`` is absent (Issue #6894, Windows)."""

import importlib
import sys


def test_interactive_imports_without_readline(monkeypatch):
    monkeypatch.setitem(sys.modules, "readline", None)  # makes `import readline` raise ImportError
    monkeypatch.delitem(sys.modules, "kicad_tools.cli.interactive", raising=False)

    module = importlib.import_module("kicad_tools.cli.interactive")
    try:
        assert module.readline is None
        shell = module.InteractiveShell()
        shell._save_history()  # must be a no-op, not an AttributeError
    finally:
        # Drop the readline-less module so later tests re-import the real one.
        sys.modules.pop("kicad_tools.cli.interactive", None)
