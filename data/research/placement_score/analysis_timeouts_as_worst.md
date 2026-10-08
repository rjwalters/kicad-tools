n_boards=8 n_placements=116 n_pairs=446 (completion-decided: 305)

| Signal | Pairwise acc (all pairs) | Pairwise acc (completion-decided) | Pairwise acc (same-sigma, n=73) | Spearman vs completion | Spearman vs DRC |
|---|---|---|---|---|---|
| `hpwl_signal` | 0.673 | 0.675 | 0.616 | -0.25 (n=7) | 0.31 (n=7) |
| `hpwl_all` | 0.709 | 0.725 | 0.630 | -0.32 (n=7) | 0.36 (n=7) |
| `steiner_signal_length` | 0.646 | 0.649 | 0.589 | -0.22 (n=7) | 0.28 (n=7) |
| `rudy_peak` | 0.540 | 0.541 | 0.507 | -0.05 (n=7) | 0.04 (n=7) |
| `rudy_overflow` | 0.500 | 0.500 | 0.500 | n/a (n=0) | n/a (n=0) |
| `rudy_top10_mean` | 0.531 | 0.508 | 0.438 | -0.05 (n=7) | 0.14 (n=7) |
| `crossing_count` | 0.518 | 0.528 | 0.562 | -0.00 (n=7) | 0.17 (n=7) |
| `placement_cost_total` | 0.652 | 0.659 | 0.610 | -0.26 (n=7) | 0.27 (n=7) |
| `placement_wirelength` | 0.713 | 0.761 | 0.658 | -0.42 (n=7) | 0.22 (n=7) |
| `placement_overlap` | 0.614 | 0.582 | 0.596 | -0.17 (n=5) | 0.27 (n=5) |
| `placement_drc` | 0.608 | 0.574 | 0.507 | -0.19 (n=5) | 0.25 (n=5) |
| `placement_boundary` | 0.583 | 0.620 | 0.568 | -0.73 (n=3) | 0.47 (n=3) |
| `sigma (reference, not a signal)` | 0.754 | 0.811 | 0.500 | -0.54 (n=7) | 0.20 (n=7) |

Fitted pairwise logistic, leave-one-board-out: micro 0.666, macro 0.607 over 446 held-out pairs; same-sigma pairs 0.548.
Best single signal chosen on training boards (same folds): micro 0.713, macro 0.730.

| Held-out board | pairs | fitted | best-single (chosen) |
|---|---|---|---|
| charlieplex_3x3 | 124 | 0.806 | 0.726 (`placement_wirelength`) |
| diffpair_test | 78 | 0.551 | 0.756 (`placement_wirelength`) |
| matchgroup_test | 22 | 0.000 | 0.727 (`placement_wirelength`) |
| simple_led | 30 | 0.967 | 1.000 (`placement_wirelength`) |
| stm32_devboard | 74 | 0.757 | 0.689 (`placement_wirelength`) |
| usb_joystick | 63 | 0.619 | 0.667 (`placement_wirelength`) |
| voltage_divider | 55 | 0.545 | 0.545 (`placement_wirelength`) |

| Board (per-board pairwise acc) | `hpwl_all` | `placement_wirelength` | `placement_cost_total` | `rudy_peak` |
|---|---|---|---|---|
| bldc_controller | n/a | n/a | n/a | n/a |
| charlieplex_3x3 | 0.758 | 0.726 | 0.565 | 0.669 |
| diffpair_test | 0.769 | 0.756 | 0.769 | 0.321 |
| matchgroup_test | 0.091 | 0.727 | 0.318 | 0.500 |
| simple_led | 1.000 | 1.000 | 1.000 | 0.500 |
| stm32_devboard | 0.716 | 0.689 | 0.730 | 0.446 |
| usb_joystick | 0.683 | 0.667 | 0.714 | 0.571 |
| voltage_divider | 0.618 | 0.545 | 0.455 | 0.691 |

| Gate threshold P(cand better) >= t | accepted | precision | recall |
|---|---|---|---|
| 0.5 | 446 | 0.666 | 0.666 |
| 0.6 | 350 | 0.720 | 0.565 |
| 0.7 | 256 | 0.742 | 0.426 |
| 0.8 | 179 | 0.838 | 0.336 |
| 0.9 | 90 | 0.900 | 0.182 |

| Board | placements | pairs | completion range | 100% routed | DRC range | original, router seeds (completion / DRC) | median route s | budget s | checkpoint labels |
|---|---|---|---|---|---|---|---|---|---|
| bldc_controller | 13 | 0 | -1--1% | 0 | 0-0 | -1/0, -1/0, -1/0 | 127 | 120 | 0 |
| charlieplex_3x3 | 17 | 124 | 33-100% | 13 | 0-13 | 100/0, 100/0, 100/0 | 76 | 120 | 3 |
| diffpair_test | 13 | 78 | -1-100% | 4 | 0-58 | 100/0, 100/0, 100/0 | 125 | 120 | 10 |
| matchgroup_test | 13 | 22 | -1-0% | 0 | 0-52 | -1/0, -1/0, -1/0 | 125 | 120 | 0 |
| simple_led | 17 | 30 | 0-100% | 15 | 0-1 | 100/0, 100/0, 100/0 | 14 | 45 | 0 |
| stm32_devboard | 13 | 74 | 11-100% | 8 | 0-31 | 100/0, 100/0, 100/0 | 48 | 120 | 2 |
| usb_joystick | 13 | 63 | -1-100% | 1 | 0-97 | 96/2, 96/2, 96/2 | 125 | 120 | 7 |
| voltage_divider | 17 | 55 | 33-100% | 16 | 0-1 | 100/0, 100/0, 100/0 | 16 | 45 | 2 |
