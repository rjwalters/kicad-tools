"""No test may SKIP over a missing C++ router backend (Issue #9705).

A case that needs the extension must FAIL with the build hint
(:data:`tests.conftest.ROUTER_CPP_BUILD_HINT`) via the ``requires_router_cpp``
marker or a ``require_router_cpp()`` call, so a worktree -- or a CI leg --
whose ``kct build-native`` step broke cannot report a green run with every
native parity case silently missing. Before this issue, ~116 modules carried
``skipif(not is_cpp_available())`` / ``importorskip("kicad_tools.router.router_cpp")``
guards and did exactly that.

This module unit-tests the detector on small source samples (including an
unrelated backend's identically named probe as a negative control) and then
runs it over the whole ``tests/`` tree, conftests and nested directories
included.
"""

from __future__ import annotations

from pathlib import Path

from tests.router_cpp_skip_scanner import scan_source, scan_tree

TESTS_ROOT = Path(__file__).resolve().parent

# This guard and its detector necessarily contain the patterns they forbid.
SELF_EXEMPT = {"test_router_cpp_no_skip_guards.py", "router_cpp_skip_scanner.py"}


def _lines(violations):
    return [v.line for v in violations]


def test_flags_multiline_router_skipif():
    source = (
        "from kicad_tools.router.cpp_backend import is_cpp_available\n"
        "import pytest\n"
        "\n"
        "requires_cpp = pytest.mark.skipif(\n"
        '    not is_cpp_available(), reason="C++ router backend not built"\n'
        ")\n"
    )
    assert _lines(scan_source(source)) == [4]


def test_flags_inline_importorskip_of_router_cpp():
    source = "import pytest\n\nrouter_cpp = pytest.importorskip('kicad_tools.router.router_cpp')\n"
    assert _lines(scan_source(source)) == [3]


def test_flags_inline_absence_check_with_pytest_skip():
    source = (
        "import pytest\n"
        "from kicad_tools.router.cpp_backend import is_cpp_available\n"
        "\n"
        "\n"
        "def test_native():\n"
        "    if not is_cpp_available():\n"
        '        pytest.skip("native router required")\n'
    )
    assert _lines(scan_source(source)) == [7]


def test_flags_mixed_parametrize_guard():
    source = (
        "import pytest\n"
        "from kicad_tools.router.cpp_backend import is_cpp_available\n"
        "\n"
        "@pytest.mark.parametrize('backend', ['python', 'cpp'])\n"
        "def test_mixed(backend):\n"
        "    if backend == 'cpp' and not is_cpp_available():\n"
        '        pytest.skip("cpp missing")\n'
    )
    assert _lines(scan_source(source)) == [7]


def test_flags_probe_variable_alias_guard():
    source = (
        "import pytest\n"
        "from kicad_tools.router.cpp_backend import is_cpp_available\n"
        "\n"
        "available = is_cpp_available()\n"
        "\n"
        "def test_native():\n"
        "    if not available:\n"
        '        pytest.skip("native router required")\n'
    )
    assert _lines(scan_source(source)) == [8]


def test_unrelated_backend_skip_is_not_flagged():
    source = (
        "import pytest\n"
        "from kicad_tools.drc.cpp_backend import is_cpp_available\n"
        "\n"
        "requires_drc = pytest.mark.skipif(\n"
        '    not is_cpp_available(), reason="drc extension not built"\n'
        ")\n"
        "\n"
        "def test_drc():\n"
        "    if not is_cpp_available():\n"
        '        pytest.skip("drc missing")\n'
    )
    assert scan_source(source) == []


def test_placement_probe_alias_is_not_flagged():
    source = (
        "import pytest\n"
        "from kicad_tools.placement.cpp_backend import is_cpp_available\n"
        "\n"
        "def test_placement():\n"
        "    if not is_cpp_available():\n"
        '        pytest.skip("placement extension missing")\n'
    )
    assert scan_source(source) == []


def test_unrelated_importorskip_is_not_flagged():
    source = "shapely = pytest.importorskip('shapely')\n"
    assert scan_source(source) == []


def test_compliant_forms_are_not_flagged():
    source = (
        "import pytest\n"
        "from kicad_tools.router.cpp_backend import is_cpp_available\n"
        "from tests.conftest import require_router_cpp\n"
        "\n"
        "@pytest.mark.requires_router_cpp\n"
        "def test_native():\n"
        "    from kicad_tools.router import router_cpp\n"
        "    assert router_cpp.Grid3D\n"
        "\n"
        "@pytest.mark.parametrize('backend', ['python', 'cpp'])\n"
        "def test_mixed(backend):\n"
        "    if backend == 'cpp':\n"
        "        require_router_cpp()\n"
    )
    assert scan_source(source) == []


def test_no_router_cpp_skip_guards_anywhere_in_the_test_tree():
    violations = scan_tree(str(TESTS_ROOT), skip=SELF_EXEMPT)
    assert not violations, (
        "C++-router-backend skip guards are forbidden (Issue #9705); a case "
        "that needs the extension must fail with the build hint via the "
        "`requires_router_cpp` marker or require_router_cpp():\n"
        + "\n".join(str(v) for v in violations)
    )
