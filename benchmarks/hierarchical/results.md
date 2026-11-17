# Bottom-up baseline vs. spacing-proxy GA

Issue #2721 -- Stelios's hypothesis: bottom-up hierarchical placement gets "80% of the way there" on non-Analog boards without a cascaded GA.

| Board | Algorithm | Components | Nets | Wirelength (mm) | Overlap pairs | Overlap area (mm^2) | Wall-clock (s) | Generations |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 01-voltage-divider | input (as-shipped) | 4 | 3 | 58.00 | 0 | 0.00 | 0.000 |  |
| 01-voltage-divider | bottom-up | 4 | 3 | 611.27 | 0 | 0.00 | 0.000 |  |
| 01-voltage-divider | ga (gen=20, pop=20) | 4 | 3 | 5.59 | 0 | 0.00 | 0.264 | 20 |
| 01-voltage-divider | ga (gen=50, pop=50) | 4 | 3 | 4.95 | 0 | 0.00 | 0.322 | 20 |
| 02-charlieplex-led | input (as-shipped) | 14 | 10 | 262.00 | 0 | 0.00 | 0.000 |  |
| 02-charlieplex-led | bottom-up | 14 | 10 | 252.58 | 0 | 0.00 | 0.001 |  |
| 02-charlieplex-led | ga (gen=20, pop=20) | 14 | 10 | 365.25 | 0 | 0.00 | 0.302 | 20 |
| 02-charlieplex-led | ga (gen=50, pop=50) | 14 | 10 | 381.38 | 0 | 0.00 | 0.631 | 50 |
| 03-usb-joystick | input (as-shipped) | 12 | 16 | 523.00 | 0 | 0.00 | 0.000 |  |
| 03-usb-joystick | bottom-up | 12 | 16 | 2259.02 | 0 | 0.00 | 0.001 |  |
| 03-usb-joystick | ga (gen=20, pop=20) | 12 | 16 | 355.73 | 0 | 0.00 | 0.427 | 20 |
| 03-usb-joystick | ga (gen=50, pop=50) | 12 | 16 | 294.77 | 0 | 0.00 | 0.766 | 46 |

## Reading the table

- **Wirelength** is the half-perimeter sum across all multi-pin nets; lower is better.
- **Overlap pairs** counts pairs of components whose bounding-box rectangles intersect. Zero is required for a routable placement.
- **Overlap area** is total intersected area; zero is required.
- **Wall-clock** is single-thread Python wall time on the dev machine; not normalized.
- **Generations** is the number of GA generations actually run (may be < requested if convergence triggered).
