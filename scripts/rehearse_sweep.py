"""Ask every read route of an installation, as every credential it holds - item ``#5063``.

The reading half of ``scripts/rehearse_upgrade.py``, which runs this under the build it is
rehearsing. **It is a program of its own because it imports that build**, and the rehearsal does
not: what it rehearses can be another checkout, and a package imported once cannot be swapped.

It builds the application the way ``serve`` does, from the settings in its environment, and calls
it in process - no port and no socket - so a server error here is the application's own answer.
Every route that answers ``GET`` is asked as every credential the installation holds and as
nobody, once plainly and once for each sample value of each query parameter it declares, and
the listings are paged through, because reading a page back is where a cursor fails. What each
was answered is written out for the rehearsal to judge; nothing here decides what is wrong.

    python scripts/rehearse_sweep.py <kept.json> <answers.json>

``kept.json`` is what the rehearsal kept while the release filled the installation: the
credentials it printed, the calendar feeds and the numbers of the items it made.
"""

import asyncio
import json
import pathlib
import sys
import typing

import httpx

import subroutine.api.app

#: The workspaces the rehearsal's installation holds, the last of them deleted by the release.
WORKSPACES = ("metacortex", "zion", "construct")

#: The listings read back page by page, two rows at a time, and the orders a task listing is
#: paged in - a computed sort is where a cursor once answered 500 past the first page.
PAGED = (
	"/v1/tasks", "/v1/documents", "/v1/changes", "/v1/journal", "/v1/users",
	"/v1/export/tasks", "/v1/export/events", "/v1/export/users",
)
ORDERS = ("priority_score", "-priority_score", "due_at", "-due_at")

#: The query parameters a read route declares that :func:`samples` gives no value to, and why.
#: ``tests/test_rehearse_upgrade.py`` holds the two together both ways, so a parameter a route
#: gains is asked about from the day it exists, rather than read past by a sweep that says nothing.
NOT_SAMPLED = {
	"cursor": "every listing is paged through by its own next cursor",
	"workspace_id": "every address is asked in each workspace in turn",
}

#: What one answer is recorded as: who asked, where, the status, and for anything worth reading
#: later - a server error, a conflict, a refusal of the request's shape - the start of its body.
Answer = dict[str, typing.Any]


def samples (pricing: str) -> dict[str, tuple[str, ...]]:
	"""Return one or more values for each query parameter a read route can declare, by name.

	A route is asked once plainly and once for each of these it declares, so nothing is sent
	that it would refuse unread. The values name what the rehearsal's installation holds:
	``pricing`` is the number of the item other work is filed under.
	"""

	return {
		"actor": ("keanu",), "answers_to": ("keanu", "marcus"), "assignee": ("me", "hugo"),
		"before": ("5",), "claimed_by": ("me",), "date": ("2026-10-20",), "deferred": ("true",),
		"deleted": ("true",), "depth": ("2",), "due_after": ("2026-10-01",),
		"due_before": ("2026-12-01",), "entity_type": ("task",), "events": ("true",),
		"fields": ("title",), "format": ("compact",), "group_by": ("status_category", "assignee"),
		"group_limit": ("2",), "horizon_days": ("14",), "include": ("backlinks",),
		"include_archived": ("true",), "include_completed": ("true",), "include_revoked": ("true",),
		"include_total": ("true",), "leaving": ("marcus", "keanu"), "limit": ("1",),
		"newest": ("true",), "oldest": ("true",), "open": ("true", "false"),
		"order": ("-created_at", "priority_score", "-priority_score", "due_at", "title"),
		"parent": (pricing, "web"), "project": ("web", "vault"),
		"q": ("deploy", "type:bug urgency>3 deploy"), "ready": ("true",), "since": ("1",),
		"status": ("blocked",), "status_category": ("done", "current"), "subtree": (pricing,),
		"tag": ("errand",), "timezone": ("America/New_York",), "to_act_on": ("true",),
		"type": ("bug", "decision"), "unscheduled_limit": ("3",), "until": ("2026-12-31",),
		"visibility": ("private",), "link": ("not-a-link",),
	}


def rows_of (body: typing.Any) -> list[typing.Any]:
	"""Return the rows of a listing, wherever its envelope keeps them, or none."""

	if isinstance(body, list):
		return body

	if isinstance(body, dict):
		for key in ("data", "items", "results"):
			if isinstance(body.get(key), list):
				return typing.cast(list[typing.Any], body[key])

		for value in body.values():
			if isinstance(value, list):
				return value

	return []


class Sweep:
	"""Every read of one installation, and what each was answered."""

	#: The client every read goes through, open for the length of :meth:`run`.
	client: httpx.AsyncClient

	def __init__ (self, kept: dict[str, typing.Any]) -> None:
		"""Build the application from this environment's settings, and sort what was kept."""

		self.application = subroutine.api.app.create_app()
		self.credentials = {
			name: value
			for name, value in kept.items()
			if isinstance(value, str) and value.startswith("sr_") and not value.startswith("sr_cal_")
		}
		self.feeds = [
			value for value in kept.values() if isinstance(value, str) and value.startswith("sr_cal_")
		]
		self.refs: dict[str, int] = kept.get("refs", {})
		self.samples = samples(str(self.refs.get("pricing", 1)))
		self.declared = {
			path: sorted(one["name"] for one in methods.get("get", {}).get("parameters", []) if one["in"] == "query")
			for path, methods in self.application.openapi()["paths"].items()
		}
		self.answers: list[Answer] = []

	async def run (self) -> dict[str, typing.Any]:
		"""Ask everything, and return the answers with what could not be asked."""

		transport = httpx.ASGITransport(app=self.application, raise_app_exceptions=False)

		async with httpx.AsyncClient(transport=transport, base_url="http://rehearsal", timeout=60) as client:
			self.client = client
			found = await self._found()
			usernames = await self._usernames()
			templates = sorted(
				{
					prefix + str(getattr(route, "path", ""))
					for prefix, router in subroutine.api.app.ROUTERS
					for route in router.routes
					if "GET" in (getattr(route, "methods", None) or ())
				}
			)
			asked: set[str] = set()

			for who in (*self.credentials, "nobody"):
				for workspace in WORKSPACES:
					for template in templates:
						for path in self._addresses(template, found[workspace], workspace, usernames):
							asked.add(template)
							await self._get(who, path)

				for workspace in WORKSPACES[:2]:
					for listing in PAGED:
						scope = f"?workspace_id={workspace}" if "workspace_id" in self.declared.get(listing, []) else ""
						await self._page_through(who, listing + scope)

					for order in ORDERS:
						await self._page_through(who, f"/v1/tasks?workspace_id={workspace}&order={order}")

		return {
			"answers": self.answers,
			"never_asked": [template for template in templates if template not in asked],
		}

	async def _get (self, who: str, path: str) -> httpx.Response | None:
		"""Make one read as one credential, record how it was answered, and return the answer."""

		headers = {"authorization": f"Bearer {self.credentials[who]}"} if who in self.credentials else {}

		try:
			answered = await self.client.get(path, headers=headers)

		# **A read that raised is recorded as a server error**, since nothing a caller sends should
		# make the application raise past its own handlers.
		except Exception as failure:
			self.answers.append({"who": who, "path": path, "status": 599, "detail": repr(failure)[:600]})

			return None

		answer: Answer = {"who": who, "path": path, "status": answered.status_code}

		if answered.status_code >= 500 or answered.status_code in (409, 410, 422):
			answer["detail"] = answered.text[:600]

		self.answers.append(answer)

		return answered

	async def _found (self) -> dict[str, dict[str, list[str]]]:
		"""Return what each workspace holds, asked as the founder, who owns every one of them."""

		found: dict[str, dict[str, list[str]]] = {}

		for workspace in WORKSPACES:
			held: dict[str, list[str]] = {"projects": [], "tasks": [], "documents": [], "views": []}

			for kind, path, field in (
				("projects", f"/v1/projects?workspace_id={workspace}&limit=200", "key"),
				("tasks", f"/v1/tasks?workspace_id={workspace}&limit=200&include_completed=true", "ref"),
				("documents", f"/v1/documents?workspace_id={workspace}&limit=200", "ref"),
				("views", f"/v1/views?workspace_id={workspace}", "key"),
			):
				answered = await self._get("keanu", path)

				if answered is not None and answered.status_code == 200:
					held[kind] = [str(row[field]) for row in rows_of(answered.json()) if row.get(field) is not None]

			found[workspace] = held

		# **What the release made is asked for by number too**, so an item a listing no longer
		# shows - which is what a read failing in the window looks like - is still read on its own.
		for name, number in self.refs.items():
			if name == "zion_task":
				continue

			bucket = "documents" if name in ("decision", "note", "draft") else "tasks"

			if str(number) not in found["metacortex"][bucket]:
				found["metacortex"][bucket].append(str(number))

		return found

	async def _usernames (self) -> list[str]:
		"""Return every account the founder can see, and one that does not exist."""

		answered = await self._get("keanu", "/v1/users?limit=200")
		names = (
			[str(row.get("username")) for row in rows_of(answered.json())]
			if answered is not None and answered.status_code == 200
			else ["keanu"]
		)

		return [*names, "nobody-at-all"]

	def _addresses (
		self, template: str, held: dict[str, list[str]], workspace: str, usernames: list[str]
	) -> list[str]:
		"""Return every address one route is asked at in one workspace, with its query variants."""

		paths = [template]

		if "{id_or_ref}" in template:
			kind = "documents" if template.startswith("/v1/documents") else "tasks"
			paths = [template.replace("{id_or_ref}", ref) for ref in held[kind]]

		elif "{id_or_key:path}" in template:
			paths = [template.replace("{id_or_key:path}", key) for key in held["projects"]]

		elif "{id_or_slug}" in template:
			paths = [template.replace("{id_or_slug}", workspace)]

		elif "{username}" in template:
			paths = [template.replace("{username}", name) for name in usernames]

		elif "{key}" in template:
			paths = [template.replace("{key}", key) for key in held["views"]]

		elif "{name}" in template:
			paths = [template.replace("{name}", name) for name in ("app.js", "styles.css", "nothing.js")]

		elif "{prefix}/{secret}" in template:
			paths = []

			for feed in self.feeds:
				_scheme, _kind, prefix, secret = feed.split("_", 3)
				paths.append(template.replace("{prefix}", prefix).replace("{secret}", secret))

		declared = self.declared.get(template.replace(":path", ""), [])
		scope = f"workspace_id={workspace}" if "workspace_id" in declared else ""
		variants = ["", *(f"{name}={value}" for name in declared for value in self.samples.get(name, ()))]

		return [
			path + (f"?{query}" if query else "")
			for path in paths
			for query in ("&".join(part for part in (variant, scope) if part) for variant in variants)
		]

	async def _page_through (self, who: str, path: str) -> None:
		"""Follow a listing's cursor for a few pages, two rows at a time."""

		joiner = "&" if "?" in path else "?"
		cursor: str | None = None

		for _page in range(4):
			answered = await self._get(who, path + f"{joiner}limit=2" + (f"&cursor={cursor}" if cursor else ""))

			if answered is None or answered.status_code != 200:
				return

			try:
				body = answered.json()

			except ValueError:
				return

			if not isinstance(body, dict):
				return

			cursor = body.get("next_cursor") or (body.get("page") or {}).get("next_cursor")

			if not cursor:
				return


def main () -> None:
	"""Read the kept file, ask everything, and write the answers."""

	kept_path, out_path = (pathlib.Path(one) for one in sys.argv[1:3])
	sweep = Sweep(json.loads(kept_path.read_text(encoding="utf-8")))
	out_path.write_text(json.dumps(asyncio.run(sweep.run())), encoding="utf-8")


if __name__ == "__main__":
	main()
