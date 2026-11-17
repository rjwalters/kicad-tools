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


def test_resume_register_state_machine(tmp_path):
    """Compile the actual added core helpers against instrumented registers."""
    import shutil

    patch = (ROOT / "firmware/patches/usbcore-suspend-resume.patch").read_text()
    added = "\n".join(
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    helpers = added.split("// BEGIN kicad-tools USB clock state machine", 1)[1].split("\n", 1)[1]
    helpers = helpers.split("// END kicad-tools USB clock state machine", 1)[0]
    harness = (
        r"""
#include <cassert>
#include <cstdint>
#define OTGPADE 4
#define FRZCLK 5
#define PLLE 1
#define PLOCK 0
#define WAKEUPI 4
#define SUSPI 0
#define EORSTI 3
#define WAKEUPE 4
#define SUSPE 0
uint8_t USBCON = 0, PLLCSR = 0, UDIEN = 0, SREG = 0x80;
uint8_t _usbSuspendState = 0, _usbConfiguration = 1;
bool _usbClockResumePending = false;
struct InterruptFlags {
    uint8_t value = 0;
    operator uint8_t() const { return value; }
    void operator=(int mask) { // UDINT: zero clears, one preserves latched flags
        assert(!(mask & 0x82)); // ATmega32U4 reserved bits stay zero
        if ((value & (1<<WAKEUPI)) && !(mask & (1<<WAKEUPI)))
            assert(!(USBCON & (1<<FRZCLK))); // clock BEFORE acknowledgement
        value &= mask;
    }
} UDINT;
void cli() { SREG &= ~0x80; }
void USB_ClockDisable() { USBCON |= 1<<FRZCLK; PLLCSR &= ~(1<<PLLE); }
"""
        + helpers
        + r"""
void suspend_then_wake() {
    USBCON = 0; PLLCSR = 1<<PLLE; _usbConfiguration = 1;
    UDINT.value = 1<<SUSPI;
    USB_SuspendClock();
    assert(USBCON & (1<<FRZCLK));
    assert(!(PLLCSR & (1<<PLLE)));
    UDINT.value = 1<<WAKEUPI;
    USB_RequestClockResume();
    assert(PLLCSR & (1<<PLLE));
    assert(_usbClockResumePending && (_usbSuspendState & (1<<SUSPI)));
    assert(UDINT.value & (1<<WAKEUPI));
    USB_PollClockResume(); // PLL not locked: no acknowledgement or sendable state
    assert(USBCON & (1<<FRZCLK));
    assert(_usbClockResumePending && (UDINT.value & (1<<WAKEUPI)));
    assert(SREG == 0x80);
}
int main() {
    suspend_then_wake();
    PLLCSR |= 1<<PLOCK;
    USB_PollClockResume();
    assert(!(USBCON & (1<<FRZCLK)) && !_usbClockResumePending);
    assert(!(UDINT.value & (1<<WAKEUPI)) && !(_usbSuspendState & (1<<SUSPI)));
    assert(UDIEN & (1<<SUSPE));
    USB_PollClockResume(); // idempotent
    suspend_then_wake();
    UDINT.value |= 1<<SUSPI; // later event, while its IRQ is masked
    PLLCSR |= 1<<PLOCK;
    USB_PollClockResume();
    assert(UDINT.value & (1<<SUSPI));
    assert(_usbSuspendState & (1<<SUSPI));
    USB_SuspendClock(); // ISR services preserved event after clocks restart
    assert(USBCON & (1<<FRZCLK));
    suspend_then_wake();
    UDINT.value |= 1<<EORSTI;
    PLLCSR |= 1<<PLOCK;
    USB_PollClockResume();
    assert(UDINT.value & (1<<EORSTI));
    assert(_usbConfiguration == 0 && (_usbSuspendState & (1<<SUSPI)));
    USB_CancelClockResume(); // reset ISR cancels stale pending work
    assert(!_usbClockResumePending && (UDIEN & (1<<SUSPE)));
    USB_PollClockResume();
    assert(_usbConfiguration == 0);
    SREG = 0; USB_PollClockResume(); assert(SREG == 0);
}
"""
    )
    source = tmp_path / "state.cpp"
    source.write_text(harness)
    compiler = shutil.which("c++") or shutil.which("g++")
    assert compiler, "C++ compiler required for firmware state-machine regression"
    binary = tmp_path / "state"
    subprocess.run([compiler, "-std=c++11", str(source), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)


def test_application_polls_before_cadence_or_sends():
    source = (ROOT / "firmware/src/main.cpp").read_text()
    loop = source.split("void loop() {", 1)[1]
    assert (
        loop.index("USBDevice.poll()") < loop.index("millis()") < loop.index("joystick.sendState()")
    )
    assert "!USBDevice.isSuspended()" in loop
