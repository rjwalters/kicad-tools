# ATmega32U4 joystick firmware

This firmware reports X/Y axes (0–1023) and five buttons over USB HID.
It uses the real revision-B MCU pins directly: PF0/ADC0 (U1.41) is X,
PF1/ADC1 (U1.40) is Y, and PD0–PD4 (U1.18/19/20/21/25) are BTN1–4
and the joystick pushbutton. Each button has an external 10 kΩ pull-up.
Inputs are sampled every 2 ms with a five-step debounce integrator. ADC
conversions use AVCC as the reference and discard the first sample after a
channel change. The USB configuration requests 100 mA and exposes HID only.

## Identity and validation status

The compiled image uses the documented shared joystick identity **16C0:27DC**.
The standard OS HID class driver is used; no custom host driver or VID/PID-only
application lookup is required. The serial descriptor starts with the project's
domain (`kicad-tools.org:03`), and the manufacturer/product strings identify this
demo. Keep control of that domain while these devices remain in use. This is a
shared identity, not a uniquely allocated or USB-certified product ID. The
[owner's published sharing terms](https://github.com/obdev/v-usb/blob/master/usbdrv/USB-IDs-for-free.txt)
permit this joystick use. A future dedicated project allocation can replace it.
The earlier private-test-only 1209:0001 image is no longer the exported build.

Compilation and the binary device descriptor are verified by `build.py`,
which also refuses (hard assertion failure, not just a report field) to
export a build configured with the pid.codes 1209:0001 private-test-only
identity — see "USB suspend/resume clock handling" below for the corresponding
build-time regression guard on the suspend-clock patch.
The current build uses 7,158 bytes of flash and 261 bytes of RAM. No physical
USB enumeration or joystick-motion test has been performed. The application
suppresses reports while suspended and does not request remote wakeup; this
is not USB compliance certification.

## USB suspend/resume clock handling

`framework-arduino-avr` 5.4.0's `USB_GEN_vect` ISR (`cores/arduino/USBCore.cpp`)
ships with the `USB_ClockDisable()` / `USB_ClockEnable()` calls left as
commented-out `//TODO` lines, so the MCU's USB PLL is never actually frozen
while the host suspends the device. As of 2026-09-10 this is not a stale
package choice: the same `//TODO` / commented calls are still present,
byte-for-byte, on the `master` branch of upstream
[ArduinoCore-avr](https://github.com/arduino/ArduinoCore-avr) — there is no
newer released core version that already fixes this.

`patches/usbcore-suspend-resume.patch` is applied automatically to the
installed core before every build (`platformio.ini`'s
`extra_scripts = pre:patches/apply_usbcore_patch.py`):

- On suspend (`SUSPI`), the ISR now calls `USB_ClockDisable()` directly. That
  function only writes `USBCON`/`PLLCSR` — no blocking wait, no `delay()` —
  so it is safe there.
- On resume (`WAKEUPI`), the ISR does **not** call `USB_ClockEnable()`
  directly, unlike the naive fix. `USB_ClockEnable()` busy-waits on the
  `PLOCK` bit and then calls `delay(1)`, and `delay()` spins on `millis()`,
  which only advances via Timer0's own ISR — but global interrupts are
  cleared for the duration of *this* ISR (no `ISR_NOBLOCK` in scope), so
  Timer0 could never fire and `delay(1)` would hang forever. Instead the ISR
  sets a flag that `USBDevice_::poll()` — now called every `loop()` iteration
  from `src/main.cpp` — services safely from main-loop context, where
  interrupts are enabled again.

`apply_usbcore_patch.py` refuses to patch blindly: it checks the installed
`USBCore.cpp` against the exact sha256 this patch was written against before
applying, and aborts the build with an explicit error if a platform/package
bump has changed that file (rather than silently mis-applying a line-anchored
patch, or silently doing nothing). `build.py` separately asserts that the
patched symbol (`_usbClockResumePending`) is present in the compiled ELF, so
a version bump that causes the patch to no-op is caught as a hard build
failure rather than a silent regression to the unpatched, TODO'd core.

This is a code-level fix for a documented upstream defect, reviewed against
the ATmega32U4 datasheet's USB clock/PLL description — it is **not** a
substitute for measuring the result on real silicon. Bench suspend current
and host resume reliability (does the device actually re-enumerate/resume
input reporting after a host-initiated suspend?) remain open hardware
qualification steps; see "Program and bring up" below.

## Build

Install PlatformIO Core, then run from the repository root:

```sh
python3 boards/03-usb-joystick/firmware/build.py
```

The script compiles, checks the actual device descriptor against the configured
VID/PID and product string, and exports HEX/ELF/BIN plus SHA256 evidence into
`artifacts/`. Dependencies are pinned: PlatformIO AVR 5.3.0, Arduino AVR package
5.4.0, AVR GCC package 1.70300.191015, and Joystick commit
`12cf2bbdb8910619d32ba3bf4b7d669c8e813b99` (v2.1.1), vendored with one
serial-descriptor change described in `lib/Joystick/KCT-CHANGES.md`. The build
also applies `patches/usbcore-suspend-resume.patch` to the installed Arduino
AVR core; see "USB suspend/resume clock handling" above.

## Program and bring up

Use a 5 V-compatible USBasp on J3. Its standard six-pin mapping is
1 MISO, 2 VCC, 3 SCK, 4 MOSI, 5 RESET, 6 GND. With USB disconnected,
power the board from one regulated 5 V source at J3; configure the programmer
accordingly. Do not connect a powered programmer and USB VBUS simultaneously.

The application starts at address zero and needs no bootloader. The settings
are low fuse FF (8 MHz crystal, no divide-by-eight), high fuse D9
(ISP enabled, application reset vector), and extended fuse CA (HWB disabled,
nominal 3.4 V brownout). The soldered crystal must be present before setting
these fuses. From this directory:

```sh
pio run -t fuses
pio run -t upload
```

These commands program physical hardware; the repository build does not run
them. The configured slow ISP clock also supports an initially factory-clocked
MCU. Read back the fuses and verify flash using the programmer before unplugging.

Disconnect the programmer, connect USB, and inspect the host's game-controller
panel. Confirm two axes move through their range, center near 512, and each of
the five buttons changes only its corresponding HID button. Test unplug/replug,
reset, and suspend/resume before accepting a physical assembly.

## Sources and licenses

The [Microchip datasheet](https://ww1.microchip.com/downloads/en/DeviceDoc/Atmel-7766-8-bit-AVR-ATmega16U4-32U4_Datasheet.pdf)
defines the silicon pin mapping, ADC operation and fuse values. The build uses
[Arduino AVR](https://github.com/arduino/ArduinoCore-avr) and
[ArduinoJoystickLibrary](https://github.com/MHeironimus/ArduinoJoystickLibrary/tree/12cf2bbdb8910619d32ba3bf4b7d669c8e813b99).
The original dependency source and license notices are in `dependency-sources.zip`;
the modified Joystick source is included directly in `lib/Joystick/`.
`platformio.ini` supplies the exact rebuild recipe for relinking a modified
library. Application code follows this repository's license.

The 8 MHz clock supports the lower end of USB bus voltage after cable and fuse
loss. The USB PLL still generates 48 MHz. Brown-out detection is set to 3.4 V
(3.2–3.6 V threshold range); ADC prescaling by 64 retains a 125 kHz ADC clock.
