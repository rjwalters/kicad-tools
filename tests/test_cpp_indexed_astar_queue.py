"""Compile queue controls independently of the optional native Python extension."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def queue_control(tmp_path_factory):
    compiler = shutil.which("c++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("C++ compiler required for standalone A* queue controls")
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path_factory.mktemp("astar-queue") / "queue-control"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            str(root / "src/kicad_tools/router/cpp/include"),
            str(root / "tests/cpp/indexed_astar_queue_test.cpp"),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return binary


@pytest.mark.parametrize("case", ["duplicates", "rounding", "cap", "paths"])
def test_indexed_queue_preserves_useful_search(queue_control, case):
    subprocess.run([str(queue_control), case], check=True, capture_output=True, text=True)
