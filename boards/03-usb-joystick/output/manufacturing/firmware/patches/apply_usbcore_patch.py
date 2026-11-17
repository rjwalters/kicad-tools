"""Verify and apply the pinned AVR USB clock patch (issues #5000/#5251).

Wake requests start the PLL without waiting in the ISR. Poll keeps sends gated
until PLOCK, restores USB clock inputs, then acknowledges WAKEUPI. Pending
suspend/reset events remain visible. No delay() or blocking PLL wait runs in the
resume path. The pinned core's unrelated attach helper uses delay()/micros();
physical USB behavior and current measurements still require bench qualification.
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
EXPECTED_PATCHED_SHA256 = "9d4103bae42a2b7a88b36798a74cad50a058766f239c19a2ac11dfeaaac4aa47"


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
