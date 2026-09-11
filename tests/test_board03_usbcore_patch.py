"""Host-side integrity checks for the Board03 SCons patch hook."""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "boards/03-usb-joystick"
BASELINE = b"void suspend() {\n    // TODO\n}\n"
PATCHED = b"// kicad-tools issue #5000\nvoid suspend() {\n    USB_ClockDisable();\n}\n"
PATCH = """--- USBCore.cpp
+++ USBCore.cpp
@@ -1,3 +1,4 @@
+// kicad-tools issue #5000
 void suspend() {
-    // TODO
+    USB_ClockDisable();
 }
"""


@pytest.fixture(params=["firmware", "output/manufacturing/firmware"])
def hook(request, monkeypatch):
    path = ROOT / request.param / "patches/apply_usbcore_patch.py"
    spec = importlib.util.spec_from_file_location("usbcore_patch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "EXPECTED_BASELINE_SHA256", hashlib.sha256(BASELINE).hexdigest())
    monkeypatch.setattr(module, "EXPECTED_PATCHED_SHA256", hashlib.sha256(PATCHED).hexdigest())
    return module


@pytest.fixture
def files(tmp_path):
    core = tmp_path / "USBCore.cpp"
    core.write_bytes(BASELINE)
    patch = tmp_path / "core.patch"
    patch.write_text(PATCH)
    return core, patch


def test_exact_baseline_and_idempotent_result(hook, files):
    core, patch = files
    hook.apply_patch(core, patch)
    assert core.read_bytes() == PATCHED
    patch.unlink()  # A verified result needs no second patch application.
    hook.apply_patch(core, patch)
    assert core.read_bytes() == PATCHED


@pytest.mark.parametrize(
    "content",
    [
        b"unknown upstream core\n",
        b"// kicad-tools issue #5000\n",
        PATCHED.replace(b"USB_ClockDisable();", b"// USB_ClockDisable();"),
        PATCHED + b"// stale extra modification\n",
    ],
)
def test_rejects_unverified_content_even_with_marker(hook, files, content):
    core, patch = files
    core.write_bytes(content)
    with pytest.raises(SystemExit, match="Refusing modified, stale or partial"):
        hook.apply_patch(core, patch)
    assert core.read_bytes() == content


@pytest.mark.parametrize("returncode", [0, 1])
def test_partial_patch_never_changes_installed_core(hook, files, monkeypatch, returncode):
    core, patch = files

    def partial_patch(command, **kwargs):
        Path(command[-2]).write_bytes(b"// kicad-tools issue #5000\npartial output\n")
        return subprocess.CompletedProcess(command, returncode, "", "injected partial patch")

    monkeypatch.setattr(hook.subprocess, "run", partial_patch)
    with pytest.raises(SystemExit, match="failed to apply|does not match reviewed content"):
        hook.apply_patch(core, patch)
    assert core.read_bytes() == BASELINE
