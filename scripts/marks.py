"""Draw every icon this app serves, from the one vendored mark — `#2864`.

**The mark is `web/vendor/kanban.svg`** and everything else is derived from it: four SVGs, the
rasters a tab bar and a home screen ask for, and the three `.ico` files. So the mark is defined
in exactly one place, as `assets/favicon.md` has always claimed of its own set, and changing it
is replacing that file and running this.

**Run it with the project's own Python**, from the repository root::

	python scripts/marks.py

**Rasters are drawn by the Chromium the browser tests already use**, at the size each file is
for, rather than scaled from one big PNG. It is the renderer that draws these files in a tab
anyway, and `assets/favicon.md` already recorded one file made this way.

**Nothing here is run by the suite.** `tests/test_web.py` holds what the files must be — the
mark's own path data in each SVG, and each PNG's header declaring the size its name claims —
rather than re-rendering them, because a test that regenerates its subject passes whatever the
renderer does today.
"""

import pathlib
import re
import struct
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "subroutine" / "web" / "assets"
MARK = ROOT / "src" / "subroutine" / "web" / "vendor" / "kanban.svg"

#: What the mark is drawn in, on a tile and on nothing. Black and white only, as the set has
#: always been: a favicon has no page to take a colour from (`#2340`, and the site's `#2861`).
BLACK = "#000000"
WHITE = "#ffffff"

#: How much of a tile the mark covers. The old set drew its mark at 78% of a solid square and
#: this keeps that; the transparent files carry the icon's own padding, which is a quarter of
#: its grid.
ON_A_TILE = 0.78


def _paths () -> list[str]:
	"""Return the mark's path data, in order, from the vendored file.

	Read rather than copied, so this script holds no drawing of its own — the point of the
	vendored file is that it is the one place the shape lives.
	"""

	found = re.findall(r'<path\s+d="([^"]+)"\s*/>', MARK.read_text(encoding="utf-8"))

	if not found:
		raise SystemExit(f"{MARK} holds no paths, so there is nothing to draw")

	return found


def _drawn (ink: str, tile: str | None) -> str:
	"""Return one SVG of the mark, in ``ink``, on ``tile`` or on nothing."""

	strokes = "\n".join(f'\t\t<path d="{one}" />' for one in _paths())
	inset = round(24 * (1 - ON_A_TILE) / 2, 2)
	scaled = (
		f'\t<g transform="translate({inset} {inset}) scale({ON_A_TILE})">\n{strokes}\n\t</g>'
		if tile is not None
		else f"\t<g>\n{strokes}\n\t</g>"
	)
	# **The tile takes no stroke.** Everything here inherits the drawing's own 2-unit stroke,
	# so a plain rect grew a one-unit outline in the ink colour and the renderer clipped half
	# of it - a pale border around every tile, at every size.
	square = (
		f'\t<rect width="24" height="24" fill="{tile}" stroke="none" />\n'
		if tile is not None
		else ""
	)

	return (
		'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" '
		f'fill="none" stroke="{ink}" stroke-width="2" stroke-linecap="round" '
		'stroke-linejoin="round">\n'
		f"{square}{scaled}\n"
		"</svg>\n"
	)


def _rendered (svg: str, size: int, into: pathlib.Path) -> bytes:
	"""Return one PNG of ``svg`` at ``size``, drawn by Chromium."""

	import playwright.sync_api

	page_file = into / "mark.html"
	page_file.write_text(
		"<!doctype html><meta charset=utf-8>"
		"<style>html,body{margin:0;padding:0;background:transparent}"
		f"svg{{display:block;width:{size}px;height:{size}px}}</style>{svg}",
		encoding="utf-8",
	)

	with playwright.sync_api.sync_playwright() as driver:
		browser = driver.chromium.launch()
		page = browser.new_page(viewport={"width": size, "height": size})
		page.goto(page_file.as_uri())
		drawn = page.screenshot(omit_background=True, clip={"x": 0, "y": 0, "width": size, "height": size})
		browser.close()

	return drawn


def _ico (frames: dict[int, bytes]) -> bytes:
	"""Return an ICO holding each PNG, keyed by the size it was drawn at.

	**PNG inside ICO**, which every browser in the support matrix reads and which keeps each
	frame the file this script already drew. The header is six bytes and each entry sixteen:
	a size of 256 would be written as 0, and nothing here is that big.
	"""

	entries, bodies, offset = b"", b"", 6 + 16 * len(frames)

	for size, body in sorted(frames.items()):
		entries += struct.pack(
			"<BBBBHHII", size, size, 0, 0, 1, 32, len(body), offset + len(bodies)
		)
		bodies += body

	return struct.pack("<HHH", 0, 1, len(frames)) + entries + bodies


#: Every file this writes: its name, the ink, the tile it sits on, and the size to draw it at.
#: ``None`` for a size means the SVG itself.
WANTED: tuple[tuple[str, str, str | None, int | None], ...] = (
	("favicon.svg", BLACK, None, None),
	("favicon-inverted.svg", WHITE, None, None),
	("favicon-on-black.svg", WHITE, BLACK, None),
	("favicon-on-white.svg", BLACK, WHITE, None),
	*[(f"favicon-{size}.png", BLACK, None, size) for size in (16, 32, 48, 64)],
	*[(f"favicon-{size}-inverted.png", WHITE, None, size) for size in (16, 32, 48, 64)],
	*[(f"favicon-on-black-{size}.png", WHITE, BLACK, size) for size in (16, 32, 48, 64)],
	*[(f"favicon-on-white-{size}.png", BLACK, WHITE, size) for size in (16, 32, 48, 64)],
	("apple-touch-icon.png", WHITE, BLACK, 180),
	("apple-touch-icon-light.png", BLACK, WHITE, 180),
	("icon-192-on-black.png", WHITE, BLACK, 192),
	("icon-512-on-black.png", WHITE, BLACK, 512),
	("icon-512-on-white.png", BLACK, WHITE, 512),
)

#: The three multi-size files, each gathering the PNGs above at 16, 32 and 48.
ICONS: tuple[tuple[str, str], ...] = (
	("favicon.ico", "favicon-{size}.png"),
	("favicon-on-black.ico", "favicon-on-black-{size}.png"),
	("favicon-on-white.ico", "favicon-on-white-{size}.png"),
)


def main () -> None:
	"""Write every file, and say what changed."""

	written: dict[str, bytes] = {}

	with tempfile.TemporaryDirectory() as where:
		into = pathlib.Path(where)

		for name, ink, tile, size in WANTED:
			svg = _drawn(ink, tile)
			written[name] = (
				svg.encode("utf-8") if size is None else _rendered(svg, size, into)
			)

	for name, pattern in ICONS:
		written[name] = _ico({size: written[pattern.format(size=size)] for size in (16, 32, 48)})

	for name, body in sorted(written.items()):
		path = ASSETS / name
		was = path.read_bytes() if path.is_file() else b""

		path.write_bytes(body)
		print(f"{'wrote' if was != body else 'same '} {name:<28} {len(body):>7} bytes")

	print(f"{len(written)} files from {MARK.relative_to(ROOT)}")


if __name__ == "__main__":
	main()
