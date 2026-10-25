"""An item's address in another workspace is not a mention of this one's item - `SR#5146`.

**A bullet of L-7 of the cold review of 2026-10-17**: the terminal prints an item elsewhere as
``metacortex/#42``, or ``work/metacortex/#42`` on another connection, and pasted into a
description either was indexed as a mention of this workspace's ``#42``, and linked to it by the
browser. Neither long form is read as one now, in either copy of the rule.
"""

import json
import pathlib

import sqlalchemy.orm

import subroutine.domain.mentions
import subroutine.domain.tasks
import test_services
import test_web

#: The two long forms the terminal prints (`World.address_of`), around one number.
LONG_FORMS = ("metacortex/#42", "work/metacortex/#42")


def test_neither_long_form_is_a_candidate () -> None:
	"""The index reads no number out of either long form, and still reads a bare one."""

	for written in LONG_FORMS:
		assert subroutine.domain.mentions.candidates(f"Copied from {written}.") == [], written

	assert subroutine.domain.mentions.candidates("See #42, not metacortex/#7.") == [42]


def test_another_workspaces_address_makes_no_backlink (session: sqlalchemy.orm.Session) -> None:
	"""A description pasting another workspace's address leaves this workspace's item alone."""

	workspace = test_services._workspace(session)
	project = test_services._project(session, workspace, key="web")
	target = subroutine.domain.tasks.create(
		session, project=project, title="Pick up the dry cleaning"
	)

	subroutine.domain.tasks.create(
		session,
		project=project,
		title="Elsewhere",
		description=f"Copied from metacortex/#{target.ref} and work/metacortex/#{target.ref}.",
	)

	backlinks = subroutine.domain.mentions.backlinks(
		session,
		principal=test_services._reader(session, workspace),
		workspace_id=workspace.id,
		target_type="task",
		target_id=target.id,
	)

	assert backlinks == []


def test_the_browser_links_neither_long_form (tmp_path: pathlib.Path) -> None:
	"""``markdown.js`` carries the same rule, so it links neither form and still links ``#42``."""

	module = test_web._staged(tmp_path)
	sources = [f"Copied from {written}." for written in LONG_FORMS] + ["See #42."]
	rendered = test_web._ran(tmp_path, f"""
		import * as markdown from "{(module.parent / "markdown.js").as_uri()}";
		import * as app from "{module.as_uri()}";

		const where = app.mentionHref("projects");
		const sources = {json.dumps(sources)};

		process.stdout.write(JSON.stringify(sources.map((s) => markdown.render(s, where))));
	""")

	for index, written in enumerate(LONG_FORMS):
		assert "<a " not in rendered[index], (written, rendered[index])

	assert '<a href="/projects/42" class="mention">#42</a>' in rendered[-1], rendered[-1]
