# Offset stripline calculation — issue #5016

The former centered IPC-style formula used total plane separation and a small
empirical offset correction. Moving the remote plane away made impedance grow
without bound until an arbitrary clamp, despite the finite single-plane limit.
For a 0.16 mm trace, 0.0152 mm copper and 0.13/1.078 mm dielectric gaps at Er 4.5,
it gave about 71.7 ohm. Independent numerical methods agree on 50.252 ohm.

`physics/stripline.py` now solves the homogeneous dielectric cross-section with
constant-charge boundary elements. The two-plane Dirichlet Green function is
given in [Khélifa and Chorfi (2019), section 2.3](https://doi.org/10.1186/s13661-019-1245-6).
Cosine-graded panels resolve corners; an analytic logarithmic self integral
replaces the singular quadrature contribution. Actual copper thickness is
modeled geometrically. The vacuum capacitance scales exactly by Er in this
homogeneous model; no fitted offset or width correction is applied.

## Independent verification

`finite_volume_reference.py` uses a separate sparse finite-volume Laplace solve
on the whole cross-section, including a rectangular conductor. It does not
import the production boundary-element solver. Run it with:

```sh
uv run --with scipy python docs/investigations/stripline-5016/finite_volume_reference.py
```

All dimensions below are millimeters; Er=4.5. The archived JSONL contains mesh,
node count, capacitance, elapsed time and domain size for every run.

| Width / thickness / near gap / far gap | 0.005 mesh | 0.0025 mesh | 0.00125 mesh | Extrapolated | Boundary elements |
|---|---:|---:|---:|---:|---:|
| .16 / .0152 / .13 / 1.078 | 50.04562 | 50.16866 | 50.21818 | 50.25155 | 50.25153 |
| .16 / .0152 / .13 / .13 | 38.75155 | 38.86696 | 38.91349 | 38.94492 | 38.94484 |
| .20 / .035 / .20 / .20 | 41.98730 | 42.05646 | 42.08420 | 42.10279 | 42.10287 |

Increasing the lateral half-domain from 2.5 to 4 mm changes the first case by
0.000036 ohm at 0.0025 mm mesh. The first case's boundary-panel sequence
(12/24/48/96 panels per edge) also converges, with the final two values differing
by less than 0.001 ohm. Production uses 48 graded panels per edge and caches
geometries. Tests also compare five centered, zero-thickness geometries against
the independent exact elliptic-integral solution, and check scale invariance,
reflection, permittivity scaling and the distant-plane limit.

The model assumes infinite reference planes, homogeneous dielectric and an
isolated trace. It does not resolve neighboring traces, layered permittivity,
copper roughness or fabrication tolerances. Board07's separately calculated
layered dielectric result is approximately 50.30 ohm for the .16 mm case;
this agreement checks its use of the homogeneous approximation but does not
constitute a factory impedance measurement.
