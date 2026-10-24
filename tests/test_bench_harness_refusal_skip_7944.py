"""Unit tests for the bench-harness refusal-skip helper (#7944)."""

from __future__ import annotations

import subprocess

import pytest

from tests._bench_harness import skip_or_assert_harness_ok

REFUSAL = "REFUSED (KiCad environment, Issue #6962/#7901): kicad-cli 10.0.7 is not pinned\n"


def _proc(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(["x"], returncode, stdout, stderr)


def test_refusal_skips_with_reason():
    with pytest.raises(pytest.skip.Exception, match="kicad-cli 10.0.7 is not pinned"):
        skip_or_assert_harness_ok(_proc(2, stderr="noise\n" + REFUSAL))


def test_exit_two_without_refusal_text_fails():
    with pytest.raises(AssertionError):
        skip_or_assert_harness_ok(_proc(2, stderr="Traceback ..."))


def test_other_exit_code_fails_even_with_refusal_text():
    with pytest.raises(AssertionError):
        skip_or_assert_harness_ok(_proc(1, stderr=REFUSAL))


def test_success_returns_process():
    proc = _proc(0)
    assert skip_or_assert_harness_ok(proc) is proc
