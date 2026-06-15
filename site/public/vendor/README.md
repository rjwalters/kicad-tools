# Vendored third-party assets

Files in this directory are committed source assets (not build-generated
artifacts) served verbatim at `/vendor/<file>` by Astro's `public/` handling.

## `kicanvas.js`

Interactive in-browser PCB/schematic viewer used by the board detail page
(`site/src/pages/[slug].astro`) to render each board's `.kicad_pcb` via the
`<kicanvas-embed>` / `<kicanvas-source>` web components.

- **Project**: KiCanvas — https://kicanvas.org
- **Source**: https://github.com/theacodes/kicanvas
- **License**: MIT
- **Bundle URL**: https://kicanvas.org/kicanvas/kicanvas.js
- **Retrieved**: 2026-06-14

### Why vendored (not loaded from a CDN)

The bundle is committed here so the deployed static site has **no runtime CDN
dependency** for the viewer's core code — the site works on offline / intranet
deploys. KiCanvas ships as a single self-contained ESM bundle (~466 KB) that
registers the custom elements on import; no WASM sidecar files are needed.

> Note: KiCanvas's UI chrome (toolbar icons / fonts) pulls Google's Material
> Symbols + Nunito fonts from `fonts.googleapis.com` at runtime. That affects
> only icon glyphs and labels in the viewer toolbar — PCB rendering, pan, and
> zoom work fully offline. Self-hosting those fonts is out of scope for this
> issue.

### Updating

Re-download the latest bundle and replace the file:

```bash
curl -sSL -o site/public/vendor/kicanvas.js https://kicanvas.org/kicanvas/kicanvas.js
```

Then bump the **Retrieved** date above and re-run `npm --prefix site run build`.
