# Subroutine's mark, and every file drawn from it

**The mark is Lucide's `waypoints`**, which Simon chose on 2026-09-24 in place of `kanban`
(`#3571`): *it better represents connections and less the conventional, traditional board
approach.* It lives in exactly one place - `src/subroutine/web/vendor/waypoints.svg`, vendored
with its ISC licence and recorded in `web/vendored.py` like everything else here we did not
write.

**Everything in this directory whose name begins `favicon`, `apple-touch-icon` or `icon-` is drawn
from that file** by `scripts/marks.py`, with the Chromium the browser tests already use:

```
python scripts/marks.py
```

So changing the mark is replacing one file and running one command, and no drawing is written out
twice. `tests/test_web.py` holds what the result must be — the vendored file's own path data in
each SVG, and each raster's header declaring the size its name claims — rather than re-rendering
them, because a test that regenerates its subject passes whatever the renderer does that day.

## What it replaced

**`kanban` served from 2026-09-17 to 2026-09-24**, chosen among the marks Simon picked for each
product (`#2861` in the site's project, `#2864` here). It was three bars, all paths, which is why
`scripts/marks.py` read paths alone until `waypoints` - four circles and three links - needed
every element.

**Simon designed and exported the previous set on 2026-08-25** — a jagged S with the top-right
point drawn as an AI sparkle — and it served until this one. On adopting the branding he said:
*"Replace everything. The old mark was temporary, and is now superseded."* Nothing of it survives
in the tree; the history has it.

## The files

| File | Use |
| --- | --- |
| `favicon.svg` | the mark in black on transparent — what `app.css` paints the wordmark with |
| `favicon-inverted.svg` | the same in white |
| `favicon-on-black.svg` / `favicon-on-white.svg` | the mark at 78% of a solid tile |
| `favicon.ico` | 16 / 32 / 48 in one file, black on transparent |
| `favicon-on-black.ico` | 16 / 32 / 48, white on a black tile — the one the page declares |
| `favicon-on-white.ico` | 16 / 32 / 48, black on a white tile |
| `favicon-16.png`, `-32`, `-48`, `-64` | black on transparent |
| `favicon-16-inverted.png`, `-32`, `-48`, `-64` | white on transparent |
| `favicon-on-black-16.png`, `-32`, `-48`, `-64` | white on a black tile |
| `favicon-on-white-16.png`, `-32`, `-48`, `-64` | black on a white tile |
| `apple-touch-icon.png` | 180, white on black |
| `apple-touch-icon-light.png` | 180, black on white |
| `icon-192-on-black.png`, `icon-512-on-black.png`, `icon-512-on-white.png` | what the manifest names (`#1681`) |

**Each raster is drawn at its own size** rather than scaled down from one large one, which is what
keeps a 16px mark legible: at 16 a two-unit stroke is a pixel and a third wide, and resampling
a 512 loses it.

**A tile carries its own background** and the transparent files do not, so the tiled ones hold up
on any tab bar while the plain ones need the surface's colour to be known.

**The `.ico` files hold PNGs**, which is a format every browser in the support matrix reads. The
container is a six-byte header and a sixteen-byte entry per frame; `scripts/marks.py` writes it.

## In the head

`api/web.ICON_LINKS` is the one place the head block is authored (`#1286`) and `index.html`
carries the same three lines, so the two cannot drift:

```html
<link rel="icon" href="/app/favicon-on-black.ico" sizes="16x16 32x32 48x48">
<link rel="icon" href="/app/favicon-on-black.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/app/apple-touch-icon.png">
```

**Assets are served at `/app/<name>`** — see `api/web.asset` — so a bare `/favicon.ico` reaches the
API's 404 problem document rather than a mark. The `<link>` tags are what make it work, rather than
the well-known names.

## Following the reader's theme

**The wordmark takes no theme rule at all.** `app.css` paints `favicon.svg` through `mask` with
`background: currentColor`, so the mark is whatever colour the heading resolved to — right in all
three of `#908`'s states, including the two pinned ones that a `prefers-color-scheme` rule gets
wrong.

**A tab is the other way round**: a favicon has no page to take a colour from, so the declared
files have their ink written in. The black-tiled pair is declared because it holds up on a light
and a dark tab bar alike; the inverted and on-white files are here for a surface that needs the
other ink.
