"""§8.6's endpoint table says which endpoints exist. This checks it against the application.

The 2026-07-30 review found the table presenting **47 unbuilt endpoints as live** — no status
column, no marker, and `GET /v1/tasks/next` used as §8.1's own worked example of the
route-ordering rule while not existing. §13.1 forbids `/v1/meta` publishing anything an
installation does not implement, on the grounds that a client believes it; a specification is
read by the same people and the same agents.

So the table now carries a ✓ column, and this is what stops it becoming a snapshot. It fails in
**both** directions: an endpoint that ships unmarked fails here, and a mark with no endpoint
behind it fails here. That is the difference between a document that was true once and one that
stays true.

`SPEC.md` is deliberately not in the repository (it is `.gitignore`d), so this test skips when
it is absent rather than failing a checkout that does not have it.
"""

import pathlib
import re
import typing

import pytest

import subroutine.api.app

SPEC = pathlib.Path(__file__).resolve().parent.parent / "SPEC.md"

#: Served by FastAPI itself rather than by one of our routers, so `ROUTERS` does not see them.
FRAMEWORK_ROUTES = frozenset(
	{("GET", "/v1/openapi.json"), ("GET", "/docs"), ("GET", "/redoc")}
)

#: One table row, as §8.6 writes them: a ✓ or a blank, then methods, then one or more paths.
ROW = re.compile(r"^\| (✓|partly|) \| ([A-Z/]+) \| (`[^|]+`) \|", re.MULTILINE)


def shaped (path: str) -> str:
	"""Normalise a path so the spec's ``{id}`` matches the code's ``{id_or_ref}``."""

	return re.sub(r"\{[^}]+\}", "{}", path.split("?")[0].strip().strip("`").rstrip("/") or "/")


def served () -> frozenset[tuple[str, str]]:
	"""Return every (method, shaped path) the application actually answers.

	Read from ``ROUTERS`` rather than from ``app.routes``, for the reason ``api/routing.check``
	exists: ``include_router`` leaves opaque ``_IncludedRouter`` objects with no path at all.
	"""

	found: set[tuple[str, str]] = set(FRAMEWORK_ROUTES)

	for _prefix, router in subroutine.api.app.ROUTERS:
		for route in router.routes:
			path = getattr(route, "path", "")

			for method in getattr(route, "methods", ()) or ():
				found.add((method, shaped(path)))

	return frozenset(found)


def documented () -> list[tuple[str, frozenset[tuple[str, str]], str]]:
	"""Return each §8.6 row as (mark, the pairs it claims, the line it came from)."""

	if not SPEC.is_file():
		pytest.skip("SPEC.md is not in the repository; nothing to check against.")

	section = SPEC.read_text(encoding="utf-8")
	start = section.index("### 8.6 Endpoints (v1)")
	table = section[start : section.index("### 8.7", start)]
	rows: list[tuple[str, frozenset[tuple[str, str]], str]] = []

	for mark, methods, paths in ROW.findall(table):
		pairs = frozenset(
			(method.strip(), shaped(path))
			for method in methods.split("/")
			for path in paths.split(",")
		)
		rows.append((mark, pairs, f"{methods} {paths}"))

	return rows


def test_the_table_has_rows_to_check () -> None:
	"""A regex that matches nothing would make every assertion below vacuous."""

	rows = documented()

	assert len(rows) > 40, f"only {len(rows)} rows parsed out of §8.6"
	assert any(mark == "✓" for mark, _pairs, _line in rows)
	assert any(mark == "" for mark, _pairs, _line in rows)


def test_every_row_marked_built_really_is () -> None:
	"""A ✓ against an endpoint that does not exist is the defect this file is about."""

	live = served()
	lying = [
		f"{line}  (missing: {sorted(pair for pair in pairs if pair not in live)})"
		for mark, pairs, line in documented()
		if mark == "✓" and not pairs <= live
	]

	assert lying == [], (
		"§8.6 marks these as built and they are not:\n  " + "\n  ".join(lying)
	)


def test_every_row_left_blank_really_is_unbuilt () -> None:
	"""And the other direction, which is how the table becomes a snapshot.

	An endpoint that ships without its row being marked leaves the specification understating
	what exists — less dangerous than overstating it, and still drift.
	"""

	live = served()
	understated = [
		f"{line}  (now served: {sorted(pair for pair in pairs if pair in live)})"
		for mark, pairs, line in documented()
		if mark == "" and pairs & live
	]

	assert understated == [], (
		"§8.6 leaves these unmarked and they now exist:\n  " + "\n  ".join(understated)
	)


def test_every_live_endpoint_appears_somewhere_in_the_table () -> None:
	"""The converse gap: the table had no rows at all for documents, which are live.

	Reported as a list rather than asserted empty for the paths §8.6 deliberately does not
	describe — the health checks are in the table, and nothing else should be missing.
	"""

	documented_pairs: set[tuple[str, str]] = set()

	for _mark, pairs, _line in documented():
		documented_pairs |= pairs

	absent = sorted(served() - documented_pairs)

	assert absent == [], (
		"these endpoints exist and §8.6 does not mention them:\n  "
		+ "\n  ".join(f"{method} {path}" for method, path in absent)
	)


def test_the_counts_in_the_prose_match_the_table () -> None:
	"""§8.6's header states how many rows are live. A stated number is a number that drifts."""

	rows = documented()
	built = sum(1 for mark, _pairs, _line in rows if mark == "✓")
	unbuilt = sum(1 for mark, _pairs, _line in rows if mark == "")

	section = SPEC.read_text(encoding="utf-8")
	header = section[section.index("### 8.6 Endpoints (v1)") :][:1200]
	stated: typing.Any = re.search(r"\*\*(\d+) are live and (\d+) are not\*\*", header)

	assert stated is not None, "§8.6's header should state the counts"
	assert (int(stated.group(1)), int(stated.group(2))) == (built, unbuilt)
