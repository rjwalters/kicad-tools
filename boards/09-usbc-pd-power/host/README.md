# INA226 telemetry host

This helper has protocol unit tests but has not been tested on assembled board09.
Use an external Linux host with 3.3 V I2C; install `smbus2` in its environment.
J3: 1 GND, 2 SCL, 3 SDA, 4 local 3.3 V observation, 5 PD_ALERT, 6 MON_ALERT.
Do not connect the host's power output to J3.4 or use 5 V I2C. Connect ground
first and keep host pins high impedance while the board is unpowered.

```sh
python monitor.py --bus 1 --samples 10
```

The helper checks device identity before writing, writes and reads back the
calibration/configuration, then emits JSON voltage/current/power telemetry.
It rejects calibration loss and arithmetic overflow and indicates conversions
that are not yet ready. Calibration assumes the selected 10 mΩ shunt, with
0.2 mA current LSB. It uses 64 averages and 1.1 ms bus/shunt conversion times.
Sequential register reads are slow telemetry, not synchronous waveform capture
or an over-current protection loop. No STUSB4500 NVM is written.

PD profile/contract readback tooling and the charger interoperability/bring-up
procedure remain pending; do not use telemetry output as authorization for a
3 A load test.
