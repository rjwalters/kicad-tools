# 05 — Protocol Recipes

Copy-pasteable `NetClassRouting` blocks for common HSDI protocols. Each
recipe sets only the protocol-essential fields; literal values are
defaults rather than physical optima — adjust to your stackup.

Prefer declaring `pair_protocol` over a literal skew: the limit then comes
from the sourced table in [07-protocol-defaults.md](07-protocol-defaults.md).

## USB 2.0 — 90 Ω diff; 2.5 mm skew (full speed), 1.27 mm (high speed)

This recipe used to set `skew_tolerance_mm=3.0`, which had no source.

```python
from kicad_tools.router.rules import NetClassRouting

usb2_fs = NetClassRouting(
    name="USB2_FS",
    trace_width=0.2,
    clearance=0.15,
    intra_pair_clearance=0.125,
    target_diff_impedance=90.0,
    pair_protocol="USB2_FS",  # 12 Mbit/s; "USB2_HS" for 480 Mbit/s
    coupled_routing=True,
    diffpair_partner="USB_DM",
)
assert usb2_fs.effective_intra_pair_clearance() == 0.125
assert usb2_fs.effective_skew_tolerance() == 2.5
usb2_hs = NetClassRouting(name="USB2_HS", pair_protocol="USB2_HS")
assert usb2_hs.effective_skew_tolerance() == 1.27
```

## USB 3.0 SuperSpeed (5 Gbps per lane) — 90 Ω diff, tight skew

```python
from kicad_tools.router.rules import NetClassRouting

usb3 = NetClassRouting(
    name="USB3_SS",
    trace_width=0.2,
    clearance=0.15,
    intra_pair_clearance=0.075,
    target_diff_impedance=90.0,
    impedance_tolerance_percent=10.0,
    skew_tolerance_mm=0.1,
    coupled_routing=True,
    diffpair_partner="USB3_RX_N",
)
assert usb3.effective_intra_pair_clearance() == 0.075
assert usb3.effective_skew_tolerance() == 0.1
```

## PCIe Gen 1 (2.5 GT/s) — 85 Ω diff, ±5 mil (≈0.127 mm) skew

```python
from kicad_tools.router.rules import NetClassRouting

pcie = NetClassRouting(
    name="PCIe_Gen1",
    trace_width=0.2,
    clearance=0.15,
    intra_pair_clearance=0.075,
    target_diff_impedance=85.0,
    skew_tolerance_mm=0.127,
    coupled_routing=True,
    diffpair_partner="PCIE_TX_N",
)
assert pcie.effective_skew_tolerance() == 0.127
```

## MIPI D-PHY (HS Data / Clock Lane) — 100 Ω diff, ±1.5 mil (≈0.038 mm) skew

```python
from kicad_tools.router.rules import NetClassRouting

mipi = NetClassRouting(
    name="MIPI_DPHY",
    trace_width=0.2,
    clearance=0.15,
    intra_pair_clearance=0.075,
    target_diff_impedance=100.0,
    skew_tolerance_mm=0.038,
    coupled_routing=True,
    diffpair_partner="MIPI_DATA0_N",
)
assert mipi.effective_intra_pair_clearance() == 0.075
assert mipi.effective_skew_tolerance() == 0.038
```

## Notes

- The pre-configured `NET_CLASS_HIGH_SPEED` (in `router/rules.py`) is a
  reasonable default for USB 3.0 / PCIe / MIPI when you opt nets in via
  `Autorouter(..., high_speed_nets=[...])` — only fork your own
  `NetClassRouting` when a per-protocol field above differs.
- The `effective_*` accessors return finite floats — see guides
  [02](02-clearance-and-classes.md) and [04](04-length-matching.md).
