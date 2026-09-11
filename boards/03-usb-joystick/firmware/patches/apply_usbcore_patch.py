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
from tempfile import TemporaryDirectory

# sha256 of the unmodified framework-arduino-avr 5.4.0 USBCore.cpp this patch
# was written against (captured 2026-09-10). If the installed file no longer
# matches, refuse to patch blindly -- a platform/package bump may have
# changed surrounding code in a way this patch's line-anchored hunks could
# silently misapply to, or upstream may have finally shipped its own fix.
EXPECTED_BASELINE_SHA256 = "9750200cafecc523c388b7d9474f2b3b94d71bcca59ab4bc481c668b57e2c836"
EXPECTED_PATCHED_SHA256 = "254511c6d316c82b2a19559fdfaee24cb1b2011123a0c6c74d6ba0f0f933ed92"


def _core_file() -> Path:
    framework_dir = env.PioPlatform().get_package_dir("framework-arduino-avr")  # noqa: F821
    if not framework_dir:
        raise SystemExit(
            "board03 firmware: framework-arduino-avr package is not installed; "
            "run the normal PlatformIO build once to fetch it, then re-run."
        )
    return Path(framework_dir) / "cores" / "arduino" / "USBCore.cpp"


def apply_patch(core: Path, patch_file: Path) -> None:
    """Accept only the pinned baseline or complete reviewed patched content."""
    if not core.is_file():
        raise SystemExit(f"board03 firmware: expected AVR core file missing: {core}")
    digest = hashlib.sha256(core.read_bytes()).hexdigest()
    if digest == EXPECTED_PATCHED_SHA256:
        return
    if digest != EXPECTED_BASELINE_SHA256:
        raise SystemExit(
            "board03 firmware: USBCore.cpp content does not match the pinned "
            "baseline or complete patched result "
            f"(sha256 {digest}). Refusing modified, stale or partial core content."
        )
    # A failed or partially applied patch must not poison the shared package.
    # Verify the complete candidate before changing the installed core.
    with TemporaryDirectory(prefix="kct-usbcore-") as directory:
        candidate = Path(directory) / "USBCore.cpp"
        candidate.write_bytes(core.read_bytes())
        result = subprocess.run(
            ["patch", "--forward", "--silent", str(candidate), str(patch_file)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SystemExit(
                f"board03 firmware: failed to apply {patch_file.name}:\n"
                f"{result.stdout}\n{result.stderr}"
            )
        patched = candidate.read_bytes()
        if hashlib.sha256(patched).hexdigest() != EXPECTED_PATCHED_SHA256:
            raise SystemExit("board03 firmware: patch output does not match reviewed content")
        core.write_bytes(patched)
    print(f"board03 firmware: applied {patch_file.name} to {core}")


# SCons injects Import; importing this module for host-side tests has no effects.
if "Import" in globals():
    Import("env")  # noqa: F821
    # SCons execs the script without __file__, so use its project directory.
    apply_patch(
        _core_file(),
        Path(env.subst("$PROJECT_DIR")) / "patches" / "usbcore-suspend-resume.patch",  # noqa: F821
    )
