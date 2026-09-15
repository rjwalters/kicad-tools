"""Router exports retain identity without loading unrelated routing engines."""

import subprocess
import sys

import pytest


def fresh(code):
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)


def test_rules_import_does_not_load_routing_engines():
    fresh("""
import sys
from kicad_tools.router import DesignRules
from kicad_tools.router.rules import DesignRules as Original
assert DesignRules is Original
assert 'kicad_tools.router.core' not in sys.modules
assert 'kicad_tools.analysis' not in sys.modules
""")


def test_package_import_and_dir_do_not_load_engines():
    fresh("""
import sys
import kicad_tools.router as router
assert 'Autorouter' in dir(router)
assert 'kicad_tools.router.core' not in sys.modules
assert 'numpy' not in sys.modules
""")


def test_star_exports_and_submodule_imports_preserve_identity():
    fresh("""
import importlib
import kicad_tools.router as router
scope = {}
exec('from kicad_tools.router import *', scope)
assert set(router.__all__) <= scope.keys()
for name in router.__all__:
    value = scope[name]
    module_name = getattr(value, '__module__', None)
    if module_name:
        assert getattr(importlib.import_module(module_name), getattr(value, '__name__', name)) is value
from kicad_tools.router import rules, cpp_backend
assert rules.DesignRules is router.DesignRules
assert cpp_backend.is_cpp_available is router.is_cpp_available
""")


def test_unknown_export_raises_attribute_error():
    import kicad_tools.router as router

    with pytest.raises(AttributeError, match="no attribute 'not_a_router_export'"):
        name = "not_a_router_export"
        getattr(router, name)
