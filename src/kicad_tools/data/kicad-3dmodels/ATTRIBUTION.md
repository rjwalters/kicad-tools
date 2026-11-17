# Attribution: KiCad 3D models

`manifest.json` lists a subset of the stock KiCad 3D model library
(`kicad-packages3D`) by relative path and sha256 digest. No model file is
stored or re-hosted in this repository: `kct models sync` fetches each file
from the upstream project at the pinned tag and verifies it against the
manifest.

- Upstream: https://gitlab.com/kicad/libraries/kicad-packages3D
- Pinned tag / commit: see `tag` and `commit` in `manifest.json`
- License: [CC-BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)
  with the KiCad Libraries exception, which permits using the models in a
  design without the design itself becoming subject to the share-alike term.
  See https://www.kicad.org/libraries/license/
- Copyright: the KiCad Library Team and the individual model authors.

The fetched files keep this notice with them in the cache directory
(`ATTRIBUTION.md` is copied next to the models by `kct models sync`). Do not
redistribute fetched models without it.
