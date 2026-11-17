#!/usr/bin/env python3
"""Fetch pinned primary CMSIS headers and compile the board07 memory-test image."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import urllib.request
from pathlib import Path

ST_REV = "a833f4af71410f25b01468f976560d7ff63a2fc9"
ARM_REV = "55b19837f5703e418ca37894d5745b1dc05e4c91"
ROOT = Path(__file__).resolve().parent


def build(out: Path):
    include = out / "include"
    include.mkdir(parents=True, exist_ok=True)
    st = f"https://raw.githubusercontent.com/STMicroelectronics/cmsis-device-f4/{ST_REV}"
    arm = f"https://raw.githubusercontent.com/ARM-software/CMSIS_5/{ARM_REV}"
    sources = {name: f"{st}/Include/{name}" for name in ("stm32f429xx.h", "system_stm32f4xx.h")}
    sources.update(
        {
            name: f"{arm}/CMSIS/Core/Include/{name}"
            for name in (
                "core_cm4.h",
                "cmsis_version.h",
                "cmsis_compiler.h",
                "cmsis_gcc.h",
                "mpu_armv7.h",
            )
        }
    )
    sources.update({"ST-LICENSE.md": f"{st}/LICENSE.md", "ARM-LICENSE.txt": f"{arm}/LICENSE.txt"})
    manifest = {}
    for name, url in sources.items():
        target = include / name
        if not target.exists():
            with urllib.request.urlopen(url, timeout=30) as response:
                target.write_bytes(response.read())
        manifest[name] = {"url": url, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    (out / "vendor-manifest.json").write_text(json.dumps(manifest, indent=2))
    subprocess.run(
        [
            "arm-none-eabi-gcc",
            "-mcpu=cortex-m4",
            "-mthumb",
            "-mfloat-abi=soft",
            "-std=c11",
            "-Os",
            "-g3",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-ffreestanding",
            "-fno-builtin",
            "-ffunction-sections",
            "-fdata-sections",
            "-nostdlib",
            f"-I{include}",
            f"-T{ROOT / 'linker.ld'}",
            "-Wl,--gc-sections",
            f"-Wl,-Map={out / 'sdram_demo.map'}",
            str(ROOT / "main.c"),
            "-lgcc",
            "-o",
            str(out / "sdram_demo.elf"),
        ],
        check=True,
    )
    subprocess.run(
        [
            "arm-none-eabi-objcopy",
            "-O",
            "binary",
            str(out / "sdram_demo.elf"),
            str(out / "sdram_demo.bin"),
        ],
        check=True,
    )
    subprocess.run(["arm-none-eabi-size", str(out / "sdram_demo.elf")], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    build(parser.parse_args().output.resolve())
