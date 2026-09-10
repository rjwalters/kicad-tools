"""PlatformIO pre-build hook: patch the pinned AVR core's suspend/resume clock handling.

Issue #5000: framework-arduino-avr 5.4.0's ``USB_GEN_vect`` ISR (in
``cores/arduino/USBCore.cpp``) has the ``USB_ClockDisable()`` /
``USB_ClockEnable()`` calls left as commented-out ``//TODO`` lines, so the
MCU's USB PLL/clock is never actually frozen while the host suspends the
device -- the application-level report suppression in ``src/main.cpp`` does
not, by itself, get suspend current anywhere near USB-compliant.

This is not merely a stale package: as of 2026-09-10 the same ``//TODO`` /
commented calls are still present, byte-for-byte, in the ``master`` branch of
upstream https://github.com/arduino/ArduinoCore-avr -- there is no newer
released core version to bump to that fixes this.

``patches/usbcore-suspend-resume.patch`` applies a conservative fix, run
automatically by this script before every build:

- On ``SUSPI`` (suspend), call ``USB_ClockDisable()`` directly from the ISR.
  That function only writes ``USBCON``/``PLLCSR`` -- no blocking wait, no
  ``delay()`` -- so it is safe there.
- On ``WAKEUPI`` (resume), do **not** call ``USB_ClockEnable()`` from the ISR.
  That function busy-waits on the ``PLOCK`` bit and then calls ``delay(1)``,
  and ``delay()`` spins on ``millis()``, which only advances via Timer0's own
  ISR -- but global interrupts are cleared for the duration of *this* ISR (no
  ``ISR_NOBLOCK`` in scope), so Timer0 could never fire and ``delay(1)``
  would hang forever. Instead, the ISR sets a flag that
  ``USBDevice_::poll()`` (invoked from ``loop()`` in ``src/main.cpp``, in
  main-loop context with interrupts enabled) services safely.

This still leaves bench current and host suspend/resume behavior as unverified
hardware qualification steps (see ``README.md`` "Identity and validation
status") -- this patch is a code-level fix for the documented core defect,
not a substitute for measuring the result on real silicon.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

Import("env")  # noqa: F821  (SCons injects this name)

# sha256 of the unmodified framework-arduino-avr 5.4.0 USBCore.cpp this patch
# was written against (captured 2026-09-10). If the installed file no longer
# matches, refuse to patch blindly -- a platform/package bump may have
# changed surrounding code in a way this patch's line-anchored hunks could
# silently misapply to, or upstream may have finally shipped its own fix.
EXPECTED_BASELINE_SHA256 = "9750200cafecc523c388b7d9474f2b3b94d71bcca59ab4bc481c668b57e2c836"
MARKER = "kicad-tools issue #5000"
# SCons execs this script's text directly (no __file__ in its globals), so
# resolve the patch path from the project dir PlatformIO passes in instead.
PATCH_FILE = Path(env.subst("$PROJECT_DIR")) / "patches" / "usbcore-suspend-resume.patch"  # noqa: F821


def _core_file() -> Path:
    framework_dir = env.PioPlatform().get_package_dir("framework-arduino-avr")  # noqa: F821
    if not framework_dir:
        raise SystemExit(
            "board03 firmware: framework-arduino-avr package is not installed; "
            "run the normal PlatformIO build once to fetch it, then re-run."
        )
    return Path(framework_dir) / "cores" / "arduino" / "USBCore.cpp"


def apply_patch() -> None:
    core = _core_file()
    if not core.is_file():
        raise SystemExit(f"board03 firmware: expected AVR core file missing: {core}")
    text = core.read_text()
    if MARKER in text:
        return  # already patched (idempotent across repeated builds)
    digest = hashlib.sha256(core.read_bytes()).hexdigest()
    if digest != EXPECTED_BASELINE_SHA256:
        raise SystemExit(
            "board03 firmware: USBCore.cpp content does not match the "
            f"framework-arduino-avr 5.4.0 baseline patches/usbcore-suspend-resume.patch "
            f"(issue #5000) was written against (sha256 {digest} != "
            f"{EXPECTED_BASELINE_SHA256}). Refusing to apply the patch blindly -- "
            "review the new upstream USBCore.cpp (it may already fix the suspend/"
            "resume TODOs) and update the patch and/or this baseline hash."
        )
    result = subprocess.run(
        ["patch", "--forward", "--silent", str(core), str(PATCH_FILE)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"board03 firmware: failed to apply {PATCH_FILE.name} to {core}:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    print(f"board03 firmware: applied {PATCH_FILE.name} to {core}")


apply_patch()
