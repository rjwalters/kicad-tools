# SDRAM exerciser firmware

Build from the repository root with the Arm GNU bare-metal toolchain installed:

```sh
uv run python boards/07-matchgroup-test/real_design/firmware/build.py /tmp/board07-firmware
```

The script fetches CMSIS headers and their licenses at pinned ST and Arm commits,
records SHA-256 hashes, and builds `sdram_demo.elf`, `.bin`, and a linker map.
The application and startup code run from internal flash/SRAM. No application
state depends on the SDRAM before it is initialized. Compilation with warnings
treated as errors is verified; the image has not yet run on a fabricated board.

Connect an ST-Link SWD probe to J2: 1 target-voltage sense (3.3 V), 2 SWDIO,
3 GND, 4 SWCLK, 5 NRST, 6 SWO. Pin 1 senses the externally powered board;
do not enable a second supply from the probe. Apply regulated 5 V to J1 pin 1,
with ground on pin 2. After checking the assembled supply rails, program with:

```sh
openocd -f interface/stlink.cfg -f target/stm32f4x.cfg \
  -c "program /tmp/board07-firmware/sdram_demo.elf verify reset exit"
```

J3 pins 1/2/3 are GND / board TX / board RX at 3.3 V logic. Use a 115200 baud,
8-N-1 UART adapter, crossing TX to RX. Do not connect RS-232 voltage levels.
The factory-trimmed HSI feeds a PLL for nominal HCLK 96 MHz and SDRAM 48 MHz;
UART accuracy therefore follows HSI drift. The refresh counter is conservative
for a 4% slow source. Timing values are derived from the ISSI datasheet and ST's
Discovery SDRAM initialization, with CAS 3 and 4096 refresh rows per 64 ms.
Write recovery is three clocks: this also satisfies the controller's
`TWR >= TRAS - TRCD` and `TWR >= TRC - TRCD - TRP` dependencies from
[RM0090, FMC_SDTR](https://www.st.com/resource/zh/reference_manual/DM00031020.pdf).
Compile-time assertions keep those dependencies checked when timings change.

Each cycle verifies byte-mask isolation, then writes and checks all 8 MiB with
alternating-bit and address-dependent patterns and their complements. Each
full-memory write is followed by 100 ms of automatic-refresh retention. A
successful cycle lights the green LED and emits `PASS`. A mismatch lights red,
prints the failing address and expected/actual data, and stops. A bus fault or
clock-start failure stops without claiming success. No `PASS` is claimed until
all memory locations and patterns have been checked.
