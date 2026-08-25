<!--
	Everything below the rule is the export's own README, copied verbatim as provenance for
	the mark (`#1286`). The two paragraphs above it are ours.
-->

**Where this came from.** Simon designed and exported the set on 2026-08-25; it is not
third-party, so `web/vendored.py`'s licence machinery does not cover it and does not need to.
The files are here byte for byte, all of them, on his instruction — including the black,
inverted and on-white variants nothing currently references.

**The paths in *In the head* below are the exporter's and are not this instance's.** Assets are
served at `/app/<name>` — see `api/web.asset` — so the head declares `/app/favicon-on-black.ico`
and its siblings. A bare `/favicon.ico` reaches the API's 404 problem document here, which is
why the `<link>` tags are what makes the mark work rather than the well-known names.

---

# Subroutine favicon — export

Mark: four points and three edges forming a jagged S, top-right point as the AI sparkle.
Drawn on a 32-unit grid: 4 r nodes, 3 w edges, 5.8 r sparkle rotated 30 degrees.
Black and white only — no colour version yet.

## Files

| File | Use |
| --- | --- |
| `favicon.svg` | black mark, transparent — the primary asset |
| `favicon-inverted.svg` | white mark, transparent |
| `favicon-on-white.svg` / `favicon-on-black.svg` | mark on a solid square, 78% scale |
| `favicon.ico` | 16 / 32 / 48 in one file, black on transparent |
| `favicon-on-black.ico` | 16 / 32 / 48, white on a solid black tile |
| `favicon-on-white.ico` | 16 / 32 / 48, black on a solid white tile |
| `favicon-16.png`, `-32`, `-48`, `-64` | black on transparent |
| `favicon-16-inverted.png`, `-32`, `-48`, `-64` | white on transparent |
| `favicon-on-black-16.png`, `-32`, `-48`, `-64` | white on a solid black tile |
| `favicon-on-white-16.png`, `-32`, `-48`, `-64` | black on a solid white tile |
| `apple-touch-icon.png` | 180, white on black |
| `apple-touch-icon-light.png` | 180, black on white |
| `icon-512-on-black.png` / `icon-512-on-white.png` | manifest / store sizes |

The tiled rasters carry their own background, so they hold up on any tab bar; the
transparent ones need the tab colour to be known. Tile marks sit at 86% of the grid at
16 and 32, 80% at 48 and 64 — less padding than the SVG, or the mark closes up small.

## In the head

```html
<link rel="icon" href="/favicon-on-black.ico" sizes="16x16 32x32 48x48">
<link rel="icon" href="/favicon-on-black.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
```

## Following the OS theme

Chrome and Firefox honour a media query inside an SVG favicon; Safari does not. If you want
that, add two lines to `favicon.svg` by hand — the file is authored black, so the query only
has to override it:

    <style>@media (prefers-color-scheme:dark){#m{fill:#fff;stroke:#fff}}</style>

placed directly after the opening `<svg>` tag, with `id="m"` on the `<g>`.
