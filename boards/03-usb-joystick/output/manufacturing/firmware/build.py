#!/usr/bin/env python3
"""Build the joystick and verify its compiled USB identity before exporting."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    pio = shutil.which("pio") or str(Path.home() / ".local/bin/pio")
    subprocess.run([pio, "run", "-d", str(ROOT)], check=True)
    build = ROOT / ".pio/build/joystick"
    toolchain = Path.home() / ".platformio/packages/toolchain-atmelavr/bin"
    binary = build / "firmware.bin"
    subprocess.run(
        [str(toolchain / "avr-objcopy"), "-O", "binary", str(build / "firmware.elf"), str(binary)],
        check=True,
    )
    symbols = subprocess.check_output(
        [str(toolchain / "avr-nm"), "-S", str(build / "firmware.elf")], text=True
    )
    address = next(
        int(line.split()[0], 16)
        for line in symbols.splitlines()
        if line.endswith(" USB_DeviceDescriptorIAD")
    )
    data = binary.read_bytes()
    descriptor = data[address : address + 18]
    config = json.loads((ROOT / "boards/kct_joystick.json").read_text())
    vid, pid = (int(x, 0) for x in config["build"]["hwids"][0])
    assert descriptor[:2] == b"\x12\x01", "Missing compiled device descriptor"
    assert int.from_bytes(descriptor[8:10], "little") == vid, "Compiled VID mismatch"
    assert int.from_bytes(descriptor[10:12], "little") == pid, "Compiled PID mismatch"
    assert descriptor[4:7] == b"\x00\x00\x00", "Unexpected composite/CDC device class"
    assert config["build"]["usb_product"].encode() + b"\x00" in data
    if (vid, pid) == (0x16C0, 0x27DC):
        assert b"kicad-tools.org:03\x00" in data, "Missing shared-ID serial prefix"
    # Issue #5000: this script is the ONLY build recipe that exports the
    # firmware shipped in the board's manufacturing package (see README.md
    # "Build"). The pid.codes 1209:0001 identity is reserved by its owner for
    # private bench testing only and must never appear in that export -- fail
    # loudly, don't just record it, if the configured identity ever regresses
    # to it (https://pid.codes/1209/0001/).
    assert (vid, pid) != (0x1209, 0x0001), (
        "Refusing to export firmware using the pid.codes 1209:0001 "
        "private-test-only identity as a manufacturing build artifact. "
        "Configure an owned/assigned VID:PID in boards/kct_joystick.json "
        "(see README.md 'Identity and validation status')."
    )
    assert len(data) <= 32768
    # Issue #5000: confirm the USB suspend/resume clock patch (see
    # patches/apply_usbcore_patch.py) actually compiled into this binary,
    # rather than silently no-op'ing (e.g. a package bump changing the core's
    # layout enough that the patch's sha256 guard now refuses to apply).
    assert "_usbClockResumePending" in symbols, (
        "USB_GEN_vect suspend/resume clock patch did not compile in -- "
        "check patches/apply_usbcore_patch.py output above"
    )
    output = ROOT / "artifacts"
    output.mkdir(exist_ok=True)
    files = {}
    for name in ("firmware.hex", "firmware.elf", "firmware.bin"):
        shutil.copy2(build / name, output / name)
        files[name] = hashlib.sha256((output / name).read_bytes()).hexdigest()
    report = {
        "mcu": "ATmega32U4-AU",
        "clock_hz": int(config["build"]["f_cpu"].rstrip("ULul")),
        "fuses": {"low": "0xFF", "high": "0xD9", "extended": "0xCA"},
        "usb_vid": f"0x{vid:04X}",
        "usb_pid": f"0x{pid:04X}",
        "test_identity": (vid, pid) == (0x1209, 0x0001),
        "manufacturing_identity_review": (
            "shared joystick identity with domain-prefixed serial"
            if (vid, pid) == (0x16C0, 0x27DC)
            else "review required"
        ),
        "flash_image_bytes": len(data),
        "device_descriptor_hex": descriptor.hex(),
        "usb_suspend_resume_clock_patch": "applied (see patches/apply_usbcore_patch.py)",
        "sha256": files,
    }
    (output / "build-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
