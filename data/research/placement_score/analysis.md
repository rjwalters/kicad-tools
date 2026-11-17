n_boards=7 n_placements=85 n_pairs=370 (completion-decided: 229)

| Signal | Pairwise acc (all pairs) | Pairwise acc (completion-decided) | Pairwise acc (same-sigma, n=63) | Spearman vs completion | Spearman vs DRC |
|---|---|---|---|---|---|
| `hpwl_signal` | 0.708 | 0.734 | 0.683 | -0.43 (n=6) | 0.44 (n=6) |
| `hpwl_all` | 0.749 | 0.795 | 0.698 | -0.50 (n=6) | 0.51 (n=6) |
| `steiner_signal_length` | 0.678 | 0.703 | 0.667 | -0.41 (n=6) | 0.41 (n=6) |
| `rudy_peak` | 0.551 | 0.559 | 0.524 | -0.01 (n=6) | -0.03 (n=6) |
| `rudy_overflow` | 0.500 | 0.500 | 0.500 | n/a (n=0) | n/a (n=0) |
| `rudy_top10_mean` | 0.541 | 0.515 | 0.460 | -0.07 (n=6) | 0.24 (n=6) |
| `crossing_count` | 0.545 | 0.574 | 0.587 | -0.20 (n=6) | 0.19 (n=6) |
| `placement_cost_total` | 0.659 | 0.672 | 0.643 | -0.36 (n=6) | 0.46 (n=6) |
| `placement_wirelength` | 0.716 | 0.782 | 0.698 | -0.48 (n=6) | 0.48 (n=6) |
| `placement_overlap` | 0.614 | 0.570 | 0.627 | -0.29 (n=4) | 0.56 (n=4) |
| `placement_drc` | 0.591 | 0.535 | 0.516 | -0.21 (n=4) | 0.66 (n=4) |
| `placement_boundary` | 0.600 | 0.659 | 0.579 | -0.73 (n=3) | 0.47 (n=3) |
| `sigma (reference, not a signal)` | 0.739 | 0.806 | 0.500 | -0.55 (n=6) | 0.54 (n=6) |

Fitted pairwise logistic, leave-one-board-out: micro 0.743, macro 0.743 over 370 held-out pairs; same-sigma pairs 0.603.
Best single signal chosen on training boards (same folds): micro 0.646, macro 0.702.

| Held-out board | pairs | fitted | best-single (chosen) |
|---|---|---|---|
| charlieplex_3x3 | 124 | 0.774 | 0.605 (`steiner_signal_length`) |
| diffpair_test | 66 | 0.667 | 0.712 (`placement_wirelength`) |
| simple_led | 30 | 0.933 | 1.000 (`placement_wirelength`) |
| stm32_devboard | 74 | 0.797 | 0.541 (`hpwl_signal`) |
| usb_joystick | 21 | 0.667 | 0.810 (`placement_wirelength`) |
| voltage_divider | 55 | 0.618 | 0.545 (`placement_wirelength`) |

| Board (per-board pairwise acc) | `hpwl_all` | `placement_wirelength` | `placement_cost_total` | `rudy_peak` |
|---|---|---|---|---|
| charlieplex_3x3 | 0.758 | 0.726 | 0.565 | 0.669 |
| diffpair_test | 0.742 | 0.712 | 0.727 | 0.379 |
| matchgroup_test | n/a | n/a | n/a | n/a |
| simple_led | 1.000 | 1.000 | 1.000 | 0.500 |
| stm32_devboard | 0.716 | 0.689 | 0.730 | 0.446 |
| usb_joystick | 0.810 | 0.810 | 0.810 | 0.476 |
| voltage_divider | 0.618 | 0.545 | 0.455 | 0.691 |

| Gate threshold P(cand better) >= t | accepted | precision | recall |
|---|---|---|---|
| 0.5 | 370 | 0.743 | 0.743 |
| 0.6 | 310 | 0.777 | 0.651 |
| 0.7 | 242 | 0.831 | 0.543 |
| 0.8 | 182 | 0.868 | 0.427 |
| 0.9 | 112 | 0.911 | 0.276 |

| Board | placements | pairs | completion range | 100% routed | DRC range | original, router seeds (completion / DRC) | median route s | budget s | checkpoint labels |
|---|---|---|---|---|---|---|---|---|---|
| charlieplex_3x3 | 17 | 124 | 33-100% | 13 | 0-13 | 100/0, 100/0, 100/0 | 76 | 120 | 3 |
| diffpair_test | 12 | 66 | 38-100% | 4 | 0-58 | 100/0, 100/0, 100/0 | 125 | 120 | 10 |
| matchgroup_test | 2 | 0 | 0-0% | 0 | 52-52 |  | 5 | 120 | 0 |
| simple_led | 17 | 30 | 0-100% | 15 | 0-1 | 100/0, 100/0, 100/0 | 14 | 45 | 0 |
| stm32_devboard | 13 | 74 | 11-100% | 8 | 0-31 | 100/0, 100/0, 100/0 | 48 | 120 | 2 |
| usb_joystick | 7 | 21 | 12-100% | 1 | 2-97 | 96/2, 96/2, 96/2 | 123 | 120 | 7 |
| voltage_divider | 17 | 55 | 33-100% | 16 | 0-1 | 100/0, 100/0, 100/0 | 16 | 45 | 2 |

Excluded 35 rows:
  usb_joystick__p005: route timeout_no_checkpoint
  usb_joystick__p006: route timeout_no_checkpoint
  usb_joystick__p007: route timeout_no_checkpoint
  usb_joystick__p009: route timeout_no_checkpoint
  usb_joystick__p010: route timeout_no_checkpoint
  usb_joystick__p011: route timeout_no_checkpoint
  bldc_controller__orig__r0: route timeout_no_checkpoint
  bldc_controller__orig__r1: route timeout_no_checkpoint
  bldc_controller__orig__r2: route timeout_no_checkpoint
  bldc_controller__p000: route timeout_no_checkpoint
  bldc_controller__p001: route timeout_no_checkpoint
  bldc_controller__p002: route timeout_no_checkpoint
  bldc_controller__p003: route timeout_no_checkpoint
  bldc_controller__p004: route timeout_no_checkpoint
  bldc_controller__p005: route timeout_no_checkpoint
  bldc_controller__p006: route timeout_no_checkpoint
  bldc_controller__p007: route timeout_no_checkpoint
  bldc_controller__p008: route timeout_no_checkpoint
  bldc_controller__p009: route timeout_no_checkpoint
  bldc_controller__p010: route timeout_no_checkpoint
  bldc_controller__p011: route timeout_no_checkpoint
  diffpair_test__p003: route timeout_no_checkpoint
  matchgroup_test__orig__r0: route timeout_no_checkpoint
  matchgroup_test__orig__r1: route timeout_no_checkpoint
  matchgroup_test__orig__r2: route timeout_no_checkpoint
  matchgroup_test__p000: route timeout_no_checkpoint
  matchgroup_test__p001: route timeout_no_checkpoint
  matchgroup_test__p002: route timeout_no_checkpoint
  matchgroup_test__p003: route timeout_no_checkpoint
  matchgroup_test__p005: route timeout_no_checkpoint
  matchgroup_test__p006: route timeout_no_checkpoint
  matchgroup_test__p007: route timeout_no_checkpoint
  matchgroup_test__p009: route timeout_no_checkpoint
  matchgroup_test__p010: route timeout_no_checkpoint
  matchgroup_test__p011: route timeout_no_checkpoint
