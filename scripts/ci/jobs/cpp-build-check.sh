#!/usr/bin/env bash
# scripts/ci/jobs/cpp-build-check.sh -- clean C++ rebuild + version-guard gate
# (Issue #2501). The single copy of the `cpp-build-check` job's check
# commands; both .github/workflows/ci.yml's `cpp-build-check` job and
# scripts/ci/local-gate.sh run this file (2am#7561 ruling). If anyone changes
# cpp/ source without bumping ROUTER_CPP_BUILD_VERSION (or vice versa) the
# import-time guard fails with an actionable message. The apt install,
# `uv sync` and `uv pip install nanobind` environment setup stay as plain
# workflow steps (CI-only).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

step "Clean any committed .so artifacts"
bash scripts/build-cpp.sh clean

step "Build C++ extension from source"
# Belt-and-suspenders: explicitly point cmake at the venv Python in case any
# future runner image starts setting Python_ROOT_DIR (actions/setup-python's
# tool-cache Python has no nanobind and would silently win cmake's
# find_package). Harmless when no .venv exists yet (uv creates one on the
# first `uv run`).
if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
  export Python_ROOT_DIR="$PWD/.venv"
  unset Python2_ROOT_DIR Python3_ROOT_DIR
fi
bash scripts/build-cpp.sh build

step "Verify version-guard reports backend available"
uv run python - <<'PYEOF'
from kicad_tools.router.cpp_backend import (
    is_cpp_available,
    get_cpp_unavailable_reason,
    _REQUIRED_CPP_BUILD_VERSION,
    router_cpp,
)
if not is_cpp_available():
    raise SystemExit(
        f"C++ backend unavailable after clean rebuild: "
        f"{get_cpp_unavailable_reason()}"
    )
actual = getattr(router_cpp, "BUILD_VERSION", None)
if actual != _REQUIRED_CPP_BUILD_VERSION:
    raise SystemExit(
        f"BUILD_VERSION mismatch after clean rebuild: "
        f"compiled={actual!r} required={_REQUIRED_CPP_BUILD_VERSION!r}. "
        f"Did you forget to bump ROUTER_CPP_BUILD_VERSION in both "
        f"cpp/include/types.hpp and cpp_backend.py?"
    )
print(f"C++ backend OK: BUILD_VERSION={actual}")
PYEOF

step "Smoke test C++ router"
uv run pytest tests/test_router_cpp_backend.py::TestCppBuildVersionGuard -v --no-cov
