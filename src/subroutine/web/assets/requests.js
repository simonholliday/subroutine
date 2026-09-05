/*
	How long to wait before asking again, or `null` for not at all — `#1850`, `#445` §3.

	**A function rather than three branches inside the effect**, which is `#640`'s own pattern
	and the reason this file has any of the others: a decision written inside `App` is reachable
	by nothing. `dom.js` is capped at 120 lines and forbidden from implementing `dispatchEvent`
	— *"dispatching an event is where a shim stops being honest"* — so the mount cannot be made
	to hide a tab, and a rule left in there would ship guarded by a source scan.

	**Hidden beats idle**, and the order matters: a tab put away while somebody was typing is
	still a tab nobody is looking at.
*/
export function cadence (hidden, idleFor) {
	if (hidden) return null;

	return idleFor < ATTENTIVE_MS ? BUSY_POLL_MS : IDLE_POLL_MS;
}

/*
	Every request this app makes, built rather than issued: each returns what to send, so a
	caller can be driven and asserted on without a server.

	**Split out of `app.js` by `#1849`**, which moved 10,286 lines into thirteen modules and
	changed nothing about what any of them do. Read `app.js` for what the whole app is; this
	file is one layer of it.
*/

import { SELECTABLE, answers } from "./address.js";
import { api, sunkOrder } from "./answers.js";
import { ORDERINGS, calendarDay, day } from "./dates.js";
import { TIMED } from "./forms.js";
import { columns } from "./grouping.js";
import {
	ATTENTIVE_MS, BUSY_POLL_MS, COLUMN, DOCUMENT_FIELDS, IDLE_POLL_MS, MAX_PARTS, PAGE, POLL_FIELDS, POLL_PAGE, TASK_FIELDS,
} from "./settings.js";

export function sent (request) {
	/* Make one built request. The only place a builder's answer meets the network, which is
	   what lets a test hold the two apart. */
	return api(request.path, { method: request.method, body: request.body ?? null });
}

export function scoped (path, slug) {
	/* The workspace, on a path that may or may not already be asking something. */
	return `${path}${path.includes("?") ? "&" : "?"}workspace_id=${encodeURIComponent(slug)}`;
}

export const RELEASE_CHECK_POLLS = 360;

export function releaseMoved (served, reported) {
	/*
		Whether the instance answering now is running something other than the one that served
		this page — `#785`.

		**Moved rather than newer**, deliberately. A rollback changes the asset exactly as a
		release does, and a page left on the version that was rolled back is the same problem
		in the other direction; comparing for *difference* needs no ordering over a version
		string, which `0.7.6.dev70+g72240d9c8` does not obviously have anyway.

		**Both halves must be known.** An older instance publishes no `instance_version`, and
		`null` against a string is not a release — it is a field that was not there. Offering a
		reload on that would fire once on every load against such an instance and never stop.
	*/
	if (!served || !reported) return false;

	return String(served) !== String(reported);
}

export function identityRequest () {
	/* Who is reading, and which workspaces they are allowed to see. */
	return { path: "/me", method: "GET" };
}

export function allowedIn (me, slug) {
	/*
		What this reader may actually do in one workspace.

		**`WorkspaceAccess.permissions` is the field to act on** — its own docstring says so, in
		those words — and nothing read it (`#927`'s M-25). So a member with a read-only role, or
		anybody holding a narrowed credential, was shown Edit, Complete, the status control, the
		assignee control, the comment box, the link box and Remove, and every one of them 403'd
		when pressed. `app.js` states the rule against that three times: a control that refuses
		when pressed is worse than one that is not there.

		An empty set where the workspace is unknown, which is the state before `/v1/me` has
		answered — controls appear when the answer says they may, rather than appearing and
		being taken away.
	*/
	const found = me && me.workspaces.find((space) => space.slug === slug);

	return new Set((found && found.permissions) || []);
}


export function signOutRequest () {
	/* End this browser's session. `#927`'s M-26: the endpoint has existed since `#248` and the
	   page offered no way to reach it, so the only way to stop being signed in on a machine
	   was to wait for the session to lapse or to clear a cookie by hand. */
	return { path: "/session", method: "DELETE" };
}

export function headRequest () {
	/* The newest event there is, so the first poll asks what happens *next* rather than
	   replaying everything that ever has. */
	return { path: "/changes?newest=true&limit=1", method: "GET" };
}

export function pollRequest (slug, since) {
	/*
		What has changed — and it has to be *what*, not *whether* (`#781`, `#657`).

		**One row was enough while the answer was yes or no, and it never was.** `?since=` is
		inclusive by decision (§5.11, "inclusive-with-dedupe"): the caller sends back the last
		seq it dealt with and is handed that event again. With `limit=1` the answer is therefore
		*always* one row and always the same row, so the caller's cursor could not advance and
		its "did anything happen" test could never be false. Measured on the live instance:
		`?since=4000&limit=1` returns event 4000, `has_more: true`.

		So the page reloaded its listing every ten seconds for ever and the feed's answer was
		read only to be thrown away. The dedupe the endpoint asks for is `App`'s to do, and this
		asks for enough rows for it to be worth doing.

		**Shaped, because only three fields are read** (§14.10) — the seq to resume from, and
		the ref and workspace that say whether the item somebody has open is among them.

		**With nothing to resume from, ask for the newest instead** (`#656`). A freshly
		initialised instance holds no events at all, so the head is empty and there is no seq to
		carry; sending `0` for it is refused, because a seq starts at 1 and *"0 names nothing"*.
		That is `#309` a third time, and the reason it is worth a branch rather than a default is
		that the poll swallows its own failures — so the cursor would stay at `0`, every tick
		would be refused in the same way, and the page would simply never notice anything again.

		**Omitting `since` would also be accepted and would be wrong**: that returns the
		*oldest* events, so a busy instance's page would be about last month.
	*/
	const asking = since === null || since === undefined
		? "/changes?newest=true&limit=1"
		: `/changes?since=${encodeURIComponent(since)}&limit=${POLL_PAGE}`
			+ `&fields=${POLL_FIELDS.join(",")}`;

	/*
		**A null slug asks across every workspace, which is what the agenda needs** (`#652`).
		`/v1/changes` accepts an unnamed workspace where `/v1/tasks` refuses one — measured, and
		the same asymmetry `/v1/agenda` has, for the same reason: both are questions about
		*everything you can see* rather than about one place.

		Scoping the agenda's poll to the workspace the switcher happens to hold would have made
		it blind to a change anywhere else — the page it is refreshing spans them all.
	*/
	return { path: slug ? scoped(asking, slug) : asking, method: "GET" };
}

export function freshly (items, since) {
	/*
		The events in a poll's answer that the caller has not already dealt with — `#781`.

		**This is the dedupe `/v1/changes` asks its callers to do**, in the one place that can
		do it: *"you will see it again and should ignore what you already have"*. Skipping it
		does not look like a bug from either side — the endpoint answers correctly and the
		caller reads a non-empty list — and the consequence is a page that reloads on a timer
		while believing it reloads on a change.

		`since` of null is a first look, so everything in it is new.
	*/
	if (since === null || since === undefined) return items;

	return items.filter((one) => one.seq > since);
}

export function touching (events, open, page = null, links = []) {
	/*
		Whether anything in this batch changed the item somebody has open — `#657`.

		**A ref is compared with its workspace beside it.** Refs are unique per workspace and
		the agenda's poll spans every one of them (§6.2, `#652`), so a bare ref match would
		refetch #42 here because #42 moved somewhere else.

		**A link event counts whichever end it names, and that is the interesting case.** The
		event a link writes names its *source* — measured on the live instance, where
		`entity_type: "link"` carries the source's `item_ref` — so linking #A to #B is invisible
		to #B, which is exactly the end that grew a backlink. Rather than reason about which end
		a reader is on, any link event re-reads the open item. They are rare, and a wasted read
		is cheaper than a link that never appears.

		**A batch that had to stop means the answer is unknown**, so it is treated as yes. The
		cost of being wrong that way is one request; the other way it is the thing this exists
		to fix.

		**And the far end of a link this item already has counts too** (`#1147`). An item's own
		ref is not the only thing a reader is looking at: a milestone's contents *are* other
		items (`#84`), so `Links (14 of 14 blockers done)`, every strikethrough and every
		readiness mark on that page is computed from rows whose events name somebody else. The
		page was correct about itself and stale about everything it was showing.

		So the set watched is this item **plus every ref its links reach**, which the page is
		already holding and which costs no request to know. Compared against the same workspace,
		because a link's two ends are resolved inside one (`domain/links.py`) — so a far end is
		never elsewhere, and the ref alone would still be ambiguous across the agenda's poll.
	*/
	if (!open) return false;

	if (page && page.has_more) return true;

	const watched = new Set([open.ref, ...links.map((one) => one.other.ref)]);

	return events.some((one) => one.entity_type === "link"
		|| (watched.has(one.item_ref) && one.workspace_id === open.workspace_id));
}

export function rosterRequest (slug) {
	/*
		Who work can be handed to.

		**No `?limit=`, because this route declares none** and `api/query.py` refuses a
		parameter a route did not ask for — so sending one is a 422, not a larger page. Its
		refusal is caught, so the only symptom was an assignment control that quietly never
		appeared.
	*/
	return { path: `/workspaces/${encodeURIComponent(slug)}/members`, method: "GET" };
}

export function peopleRequest () {
	/*
		Every account on this instance — `#1397`.

		**Not narrowed to a workspace, which is the difference from `rosterRequest`.** That one
		answers *who can be handed work here*; this answers *who is on this installation*, and an
		agent belonging to no workspace still belongs to somebody. The two are deliberately
		different questions and the page shows both: the roster call supplies the roles, this one
		supplies the population.

		**No `?fields=`, against the habit `#645` established**, and the reason is that this
		listing is unpaginated by decision — `GET /v1/users` says an instance's people are
		bounded by how many somebody hired, exactly as a task's links are. The row renders
		username, agent-or-person, who it answers to and whether it is active, which is nearly
		the whole of a small model; asking for a subset would buy nothing and would have to be
		kept in step with what the page draws.

		**Readable by anyone signed in, deliberately** — `#161` and `#174`: identifiers are
		unique and public, content is neither, and this view carries no email address and no
		content. So the page needs no permission of its own, which is why there is none to check
		before drawing it.
	*/
	return { path: "/users", method: "GET" };
}

export function collectionsFor (selection) {
	/*
		Which collections a selection reads, and the order the answers come back in.

		**A selection on `status_category` reads one**, because only tasks have that axis at all:
		`GET /v1/documents` refuses it outright — measured, 422 — and a document's categories are
		`draft`, `current`, `superseded` and `archived`, none of which means *finished*. Asking
		documents for it would not widen the page, it would be a page that does not load.

		**Keyed on the selection rather than on a view name** (`#738`). It used to ask whether
		the arrangement was `done`, which stated the consequence and hid the reason — the reason
		is the filter, and it holds for `?status_category=in_progress` just as it does for
		`done`.

		**Everything else reads both**, which is the safe direction: `list` and `board` are the
		same rows differently arranged and both hold every kind of item there is (§6.2 gives them
		one ref counter precisely so a reader can treat them as one thing).
	*/
	const asked = selection || {};

	/*
		**An order documents cannot answer reads one too** (`#782`), and it is the same rule
		rather than a second one: `GET /v1/documents` sorts by `created_at`, `ref`, `title` and
		`updated_at` and nothing else, so asking it for `-priority_score` is a 422 — a page that
		does not load rather than a page that is missing half its rows.

		Simon's decision of 2026-08-10 is that a priority ordering is **tasks only** and the page
		says so. A document has no importance and no urgency, so there is no honest place to put
		one in a ranked list; the two rejected answers and why are on `#782`.
	*/
	const ordering = ORDERINGS[asked.order];

	/*
		**Read from `ANSWERED_BY` rather than naming `status_category` here** (`#872`). It named
		that one parameter, which was every task-only parameter on the day it was written and
		stopped being so twice since. The table is now the one statement of where a selection
		goes, so this asks it rather than agreeing with it.
	*/
	const impossible = Object.keys(asked).some(
		(name) => asked[name] !== undefined && asked[name] !== null
			&& answers("document", name) === "cannot"
	);

	if (impossible) return ["task"];

	return ordering && !ordering.both ? ["task"] : ["task", "document"];
}

export function listingRequests (slug, key = null, after = null, selection = null, columns = COLUMN) {
	/*
		The list, which is tasks *and* documents — except where it cannot be.

		**Two requests because they are two collections and both belong in it.** One counter per
		workspace serves them (§6.2), so a list holding only tasks tells a reader who has learned
		that a number names an item that half the numbers do not exist.

		**No `subtree`**, twice over. `project=` already includes what is under it — `#320`
		settled that `--project subroutine` covers `subroutine/UI` and `subroutine/OPS` — and
		`subtree` is a different question entirely: it is about a *task's* children, so sending
		it beside `project` is refused, *"'subtree' says how much of a parent's tree to return,
		so it needs a parent."*

		**No `order`, because the command line sends none** (`#646`). It used to ask for
		`-priority_score`, and §6.3a sorts that in three bands with *unranked last* — so an item
		somebody had just captured, which by definition has neither axis set yet, went straight
		to the bottom. `#642` was 142 of 142 on a page of 100, and its author was told it had
		been added. §6.3a exists because saying *more* about an item pushed it down. This is the
		mirror: saying nothing about one hides it entirely.
	*/
	const narrowed = key ? `&project=${encodeURIComponent(key)}` : "";
	const from = (cursor) => (cursor ? `&cursor=${encodeURIComponent(cursor)}` : "");

	/*
		**The selection comes from the address and is sent verbatim** (`#738`, decision `#649`).

		It used to be derived from the view name here — `board` meant `include_completed=true`
		and `done` meant `status_category=done&order=-completed_at` — which is the query
		selecting under a display parameter's name. Two consequences of taking that out are worth
		stating, because they are how you can tell the design is right:

		- **This function no longer takes the arrangement at all.** An arrangement decides how
		  rows are displayed, so it has nothing to say about what is asked for. That it was a
		  parameter here was the defect in miniature.
		- **`?include_completed=true` on a list and `?status_category=in_progress` on either
		  arrangement are now reachable**, and neither was possible while a view name carried the
		  selection.

		**Only the tasks request carries it**, and the asymmetry is right rather than an
		omission. `GET /v1/documents` accepts neither — measured, 422 for both — because a
		document has no completed axis: its categories are `draft`, `current`, `superseded` and
		`archived`, and none of them means *stop showing me this*. A superseded specification is
		in the listing by default, so the documents request already receives every one there is,
		and `collectionsFor` drops it entirely when the selection is one it cannot answer.

		**`status_category=done` implies `include_completed`, and the constraint is one-sided.**
		Measured on the served instance rather than read off the code: sending `true` beside it
		answers **200** and is merely redundant; sending `false` is refused by name — *"'done' is
		finished work, so excluding finished work leaves nothing."* So no control sets them
		together, and `SELECTABLE` admits only `true`, which makes the refused combination
		unreachable from an address rather than merely undocumented.

		I wrote this comment claiming *both* were refused, which asserted a constraint that does
		not exist. Found by falsifying the request derivation with a preset that should have been
		rejected and was not — the mutation passing is what sent me to measure it.
	*/
	/* **`SELECTABLE` order rather than the object's**, for the same reason `withShowing` sorts:
	   one screen must produce one string, or a cursor taken on one page is compared against a
	   path spelled differently on the next. */
	const chose = selection || {};

	/*
		**What goes on the wire is the reader's selection with deferred work sunk** (`#877`),
		and it is a separate value from `chose` on purpose: `collectionsFor` below asks
		`ORDERINGS` which collections an order can reach, and the sunk spelling is not a key in
		that table. Deriving both from one variable would make *most important first* stop
		dropping documents — a page of tasks and documents ordered by a field only one of them
		has, which is the 422 `#782` removed.
	*/
	const asking = { ...chose, order: sunkOrder(chose) };

	/*
		**Per collection, from `ANSWERED_BY`, rather than tasks-getting-everything** (`#872`).

		This used to build one string and append it to the tasks request alone, on the reasoning
		that `GET /v1/documents` refuses `status_category` and `include_completed`. True of those
		two and false of `q`, which arrived later and inherited the exclusion in silence — so a
		search filtered the tasks and returned every document there was.
	*/
	const sending = (kind) => Object.keys(SELECTABLE)
		.filter((name) => asking[name] !== undefined && asking[name] !== null)
		.filter((name) => answers(kind, name) === "sent")
		.map((name) => `&${name}=${encodeURIComponent(asking[name])}`)
		.join("");

	const rows = sending("task");

	/*
		**The order goes to both collections, and it is the only part of the selection that
		does** (`#782`). A merged list is safe only while both halves are sorted and paged by
		one key: `accumulated` re-merges what arrives, and a client merging on a key the server
		did not sort by is the disagreement keyset pagination exists to prevent.

		Everything else in the selection is refused by `GET /v1/documents` — `status_category`
		and `include_completed` are measured 422s — and `collectionsFor` drops the collection
		rather than sending them. An order documents cannot answer takes the same route, so
		anything reaching this line is one they can.
	*/
	/*
		**No second check that documents can take this order**, because `collectionsFor` has
		already answered it: an order documents cannot sort by drops the collection entirely
		(`#782`), so nothing built here ever reaches a request. Re-checking it would be a copy of
		that rule free to disagree with it, which is the defect this whole change is about.
	*/
	const readable = sending("document");

	/*
		**A grouped request is bounded by `group_limit`, and `limit` means nothing to it**
		(`#1790`). The server branches before it reads `limit`, so sending both would put a
		parameter on the wire that is silently ignored — which is the shape this project keeps
		finding as a defect rather than as tidiness.

		**And a grouped request takes no cursor.** There is no one position in a grouped answer
		to continue from; the server refuses the pair by name. Each group reports its own
		instead, which is what a column pages with.
	*/
	const grouping = Boolean(chose.group_by);
	const allowance = grouping ? `group_limit=${columns}` : `limit=${PAGE}`;
	const carried = (cursor) => (grouping ? "" : from(cursor));

	const asks = {
		task: { kind: "task", method: "GET", path: scoped(
			`/tasks?${allowance}&fields=${TASK_FIELDS}${narrowed}${rows}`
			+ carried(after && after.tasks), slug) },
		document: { kind: "document", method: "GET", path: scoped(
			`/documents?${allowance}&fields=${DOCUMENT_FIELDS}${narrowed}${readable}`
			+ carried(after && after.documents), slug) },
	};

	/* **Tagged with the kind rather than positional**, so a selection reading one collection
	   cannot have its rows labelled by whichever slot they happened to arrive in. */
	return collectionsFor(chose).map((kind) => asks[kind]);
}

export function itemRequests (kind, ref, slug) {
	/*
		One item in full: the thing, what governs it, what it links to, and what was said.

		**`governing` is a request rather than a filter over `links`** (`#1119`). Everything it
		answers could be derived here — the link types are in the response and so is each end's
		type — and deriving it would put a second copy of *what binds this* in the browser, to
		disagree with the server's the first time either changes. The rule has one home.
	*/
	const collection = kind === "document" ? "documents" : "tasks";

	return [
		{ path: scoped(`/${collection}/${ref}`, slug), method: "GET" },
		{ path: scoped(`/${collection}/${ref}/links`, slug), method: "GET" },
		{ path: scoped(`/${collection}/${ref}/comments?limit=${PAGE}`, slug), method: "GET" },
		{ path: scoped(`/${collection}/${ref}/governing`, slug), method: "GET" },
		/* **Tasks only, and asked conditionally rather than always** (`#1121`). Only a task is
		   checked, and the route refuses a document's ref by name — 404 *"#3 is a document, not
		   a task"* — so a version that asked anyway would fail the whole read of every document
		   on the page. Written the other way first and caught by the guard that drives every
		   request this function builds against a real instance. */
		...(kind === "document"
			? []
			: [
				{ path: scoped(`/tasks/${ref}/verifications`, slug), method: "GET" },
				/*
					**What this item is made of** (`#1218`). The page could say *this is part of
					#1207* and could not say *these four are part of this* — a capability the
					terminal, MCP and HTTP have all had, missing from the one surface a person is
					most likely to be looking at. §14.1's rule is that nothing an agent can see
					may be invisible to a person.

					**`include_completed=true` is unlike every other listing here and is
					load-bearing**, which is why the terminal's own call carries the same
					argument and the same reason: a parent showing two of its four children
					because the other two are finished would misreport the thing somebody opened
					it to see. A version reusing this app's ordinary listing defaults would draw
					a silently shrinking list.

					**Ordered by ref**, which for one counter allocated in creation order (§6.2)
					is oldest first — the order the parts were decided in, and the one the
					terminal prints.

					**Tasks only.** A document has no children, and `?parent=` on
					`/v1/documents` is refused rather than ignored (`api/query.py`), so asking
					would fail the whole read of every document on the page.
				*/
				{
					path: scoped(
						`/tasks?parent=${ref}&include_completed=true&order=ref`
						+ `&limit=${MAX_PARTS}`,
						slug,
					),
					method: "GET",
				},
			]),
	];
}

export function commentRequest (item, body, slug) {
	/*
		Say what happened — `#759`.

		**Only a body**, which is the endpoint's whole request model: *a comment that needed a
		title, a type or a project would be a document*, and §5.10's distinction is the one this
		product is least willing to blur. A comment is what happened; a document is what you
		concluded.

		The collection is the item's kind, because a document is commented on exactly as a task
		is — one thread, one grammar, and `#760` will want the same of links.
	*/
	const collection = item.kind === "document" ? "documents" : "tasks";

	return {
		path: scoped(`/${collection}/${item.ref}/comments`, slug),
		method: "POST",
		body: { body },
	};
}

/*
	What a **document** is, as against a task — `#761`.

	A title, prose, a type and a status, and **no priority, no dates, no estimate and no
	assignee**. Handing it the task form would offer eight fields it cannot have, which is
	§12.2a's *a column that says the same thing on every row* wearing a different costume.

	`project` is here because it decides who may read the document (§7.3a), which makes it a
	permissions field rather than a filing one.
*/
export const DOCUMENT_SAID = ["body", "type", "status", "project"];

export function written (values, item) {
	/*
		What a document form becomes on the wire — `#761`.

		**The same split as a task's, for the same reason**: creating omits an empty control
		because the endpoint refuses an empty type by name, and revising sends what it holds
		because §8.3 says a field left out is unchanged. `title` is never null on either — a
		document must have one, and the control is `required`.

		**`expected_version` on a revision, and it matters more here than on a task** (§8.9).
		`doc edit` is a whole-body replace, so what is at stake is the entire document rather
		than one field — two people with it open, last save wins, and the other person's
		paragraphs are gone with no record that they existed.
	*/
	const raw = values || {};
	const said = (name) => {
		const value = raw[name];

		return typeof value === "string" ? value.trim() : value;
	};

	const revising = Boolean(item && item.version);
	const body = revising ? { expected_version: item.version } : {};

	if (said("title")) body.title = said("title");

	DOCUMENT_SAID.forEach((name) => {
		const value = said(name);

		if (value) body[name] = value;
		/* **Only where the form actually had one to empty** (`#1044`). An emptied box and an
		   *absent* box are both falsy here, and until now nothing could hand this the second —
		   so a form with no body control read as somebody having cleared it, and one press of
		   Save sent `body: null` and emptied the document. `readForm` reads the named controls
		   off the DOM, so the key's presence is exactly the question *was there a box*. */
		else if (revising && name === "body" && name in raw) body[name] = null;
	});

	return body;
}

export function documentRequest (values, item, slug) {
	/*
		Write a document, or revise one — `#761`.

		**One builder for both**, because they are one act with one shape: the difference is a
		method and a ref, and two builders would be two places for the field list to drift.
	*/
	if (item && item.ref) {
		return {
			path: scoped(`/documents/${item.ref}`, slug),
			method: "PATCH",
			body: written(values, item),
		};
	}

	return {
		path: "/documents",
		method: "POST",
		body: { ...written(values, null), workspace_id: slug },
	};
}

export function linkRequest (item, target, linkType, kind, slug) {
	/*
		Join two items — `#760`.

		**Both kinds, and that is not a detail.** One ref counter serves tasks and documents
		(§6.2), so `#4` may be a specification; a link surface that worked on tasks alone would
		be the half-a-rule this codebase keeps finding. The *source* is whichever the reader has
		open and the *target* is `kind`, which the caller resolves rather than asks about — see
		`linkableTypes`.
	*/
	const { key, inverted } = linkAsked(linkType);
	const mine = item.kind === "document" ? "documents" : "tasks";

	/*
		**An inverse is the same link written from the other end** (`#799`), and the request now
		says so instead of acting it out (`#816`). *#42 blocked by #43* is *#43 blocks #42*, and
		this used to post to **their** links with this item as the target — correct about the
		row and wrong about who acted, because the event names the item the link hangs off and
		that was the one the reader never opened. *What did I work on* then listed it.

		Simon's rule, settling `#815`'s question 3: **the action occurs on the item which is
		edited to add the link.** So the request is always posted to the item in front of the
		reader and `direction` says which way the link runs; the instance stores the row the way
		round it has always stored it and records the action here.

		One consequence worth knowing: the row and its event deliberately name different items
		on this path. That is not a disagreement — the row says what is true and the event says
		what somebody did.
	*/
	return {
		path: scoped(`/${mine}/${item.ref}/links`, slug),
		method: "POST",
		body: {
			target: Number(target),
			link_type: key,
			target_type: kind,
			direction: inverted ? "incoming" : "outgoing",
		},
	};
}

export function unlinkRequest (item, linkId, slug) {
	/*
		Take one apart — `#760`.

		**Removing matters as much as adding**, and there is evidence rather than a principle:
		on 2026-08-09 two items were linked to a stranger's `#731` by assuming a ref, and
		`subroutine unlink` is what undid it. A browser that can only add is one that cannot fix
		a mistake, which makes every reader careful in the way that stops them using it.
	*/
	const collection = item.kind === "document" ? "documents" : "tasks";

	return {
		path: scoped(`/${collection}/${item.ref}/links/${linkId}`, slug),
		method: "DELETE",
	};
}

export function linkChoices (vocabulary) {
	/*
		Every way a reader can say two items are related — **both ends of each** (`#799`).

		Simon, driving `#755`: *"I cannot select 'blocked by' in the list, only 'blocked' — this
		type of link has a direction, I should be able to select both."* Four of this instance's
		five have one, and the control offered the near end of each. So *this blocks that* was
		expressible and *this is blocked by that* was not — and the second is usually what
		somebody opening an item means, because they are looking at the thing that is stuck.

		**An inverse is not a second link type.** It is the same row with the ends swapped,
		which is exactly what `/v1/meta`'s `inverse_title` is for, and `is_symmetric` is what
		stops `relates_to` appearing twice saying one thing under one label. Both have been
		published since M1 and read by nothing.

		**Spelled `-key`, like an order** (`ORDERINGS`), so the value a control carries says
		which direction it means without a second field to keep in step with it.
	*/
	return ((vocabulary && vocabulary.link_types) || []).flatMap((one) => [
		{ value: one.key, label: one.title },
		...(one.is_symmetric
			? []
			: [{ value: `-${one.key}`, label: one.inverse_title || `Inverse of ${one.title}` }]),
	]);
}

export function linkAsked (value) {
	/* Which type a choice names, and which way round. The sigil is the whole of the
	   difference, so reading it back is the only place that has to know. */
	const said = String(value || "");
	const inverted = said.startsWith("-");

	return { key: inverted ? said.slice(1) : said, inverted };
}

export function linkableTypes (vocabulary) {
	/*
		The kinds a link may point at, in the order to try them — `#760`.

		**A ref does not say which table it is in**, because one counter serves both (§6.2), so
		`#4` is a document on this instance and `#42` is a task. Asking the reader to say which
		would be making them hold a fact the system has: `subroutine show 4` does not ask, and
		neither should this.

		So the target type is *resolved* — try each in turn and take the one that is not a 404,
		which is exactly what `fetched` already does to open an item by ref. **The order comes
		from `/v1/meta`'s `linkable_types`** rather than a literal pair, so an installation that
		grows a third kind is tried too.
	*/
	const known = (vocabulary && vocabulary.linkable_types) || [];

	return known.length > 0 ? known : ["task", "document"];
}

export function authorOf (comment, members) {
	/*
		Who said it — `#759`.

		**The comment carries `author_id` and no name**, so this resolves it against the roster
		the page already holds. A comment thread that cannot say who spoke is a transcript with
		the names cut out, and on this instance four of five members are agents (`#770`): *who
		wrote this* is the difference between a colleague's note and a machine's.

		**Null rather than a guess when the author is not on the roster.** Somebody who has left
		the workspace, or an account this reader cannot see, resolves to nothing — and nothing is
		what should then be shown. Inventing "Unknown" would claim the lookup happened and found
		an answer.
	*/
	const found = (members || []).find((one) => one.id === (comment || {}).author_id);

	/* **The roster first, the row second** (`#636`). The roster's label marks a service
	   account — *claude (agent)* — which the response's bare username cannot, and that
	   distinction is this function's whole reason for existing. But somebody who has left the
	   workspace is on no roster, and showing nothing then is a transcript with one name cut
	   out; `views.Comment.author` answers for exactly that case. */
	return found ? found.label : (comment || {}).author || null;
}

export function completeRequest (row, slug) {
	/* Done. A verb of its own rather than a status write, because completing is what the
	   endpoint is for and it decides the status itself. */
	return { path: scoped(`/tasks/${row.ref}/complete`, slug), method: "POST" };
}

export function restoreRequest (going, slug) {
	/*
		Undo, which puts back **the status that was there** rather than a status we chose. A
		task's statuses are workspace vocabulary and `open` is only the seeded default, so
		writing that would be a different act from reversing the one just made.
	*/
	return {
		path: scoped(`/tasks/${going.ref}`, slug),
		method: "PATCH",
		body: { status: going.status },
	};
}

export function assignRequest (row, who, slug, appliesTo = null) {
	/*
		Hand it over, or take it back off everybody — `null` is a value here, not an omission.

		**One gesture, and it still asks on a repeating item** (decision `#1249` §1). Simon
		named the assignee among the fields that ask, and he is right for the reason the whole
		decision is: *who does the stand-up this week* and *who does the stand-up from now on*
		are different sentences, and nothing but the person knows which was meant. So unlike
		`moveRequest` next door — a status, which has one answer — this one carries an answer
		when there is a question to answer.
	*/
	return {
		path: scoped(`/tasks/${row.ref}`, slug),
		method: "PATCH",
		body: appliesTo ? { assignee: who, applies_to: appliesTo } : { assignee: who },
	};
}

export function prioritiseRequest (project, slug) {
	/*
		Raise one project's work in this workspace, or clear it — `#986`, decision `#982`.

		**A write on the *workspace*, even though the control names a project.** There is one such
		fact and it belongs to the workspace, so choosing a second project unsets the first in the
		same request with no clearing logic. A route on the project would read as a per-project
		flag, which is the mental model that lets four quiet boosts accumulate until the order
		means nothing — the failure that decision exists to refuse.

		`null` is a value here rather than an omission (§8.3): it clears the priority.
	*/
	return {
		path: `/workspaces/${encodeURIComponent(slug)}`,
		method: "PATCH",
		body: { prioritised_project: project },
	};
}

export function addRequest (values, slug) {
	/*
		The workspace goes *in* the body because that is where this endpoint takes it — the only
		write here that does.

		**It took a bare string until `#756`** and the capture line is still how a title arrives:
		`text` rather than a title, so the grammar runs (§6.13) and one box can set a project, a
		priority, tags and a date.

		**`filed` is called here rather than by the caller**, so that the guard which drives every
		builder against a real instance drives the body-building too. Handing this a body somebody
		assembled elsewhere would leave the one function with a rule in it — the one that decides
		what is worth sending — checked only against a body written by hand in a test.
	*/
	return { path: "/tasks", method: "POST", body: filed(values, slug) };
}

/*
	The words a form sends, split by what has to happen to them on the way.

	**Every one of these is omitted when it is empty rather than sent blank**, and that is not
	tidiness — it is what the endpoint requires. Measured on 2026-08-10 against the served
	instance: `assignee: ""` is *"There is nobody called ''"*, `type: ""` is *"No task type with
	key ''"*, `estimate: ""` is *"A duration cannot be empty"*, and `title: ""` beside a `text`
	is *"A title is required"*. A form's untouched control gives exactly that empty string, so a
	body assembled by copying the controls would be refused by whichever field the reader left
	alone first — which is every field, on the commonest submission there is.
*/
export const SAID_AS_WRITTEN = [
	"description", "project", "type", "status", "assignee",
	"estimate", "starts", "snooze", "due",
];

/*
	**Sent as numbers, not as the strings a control holds** (`#549`). `{"today": "false"}` was
	truthy in Python and the filter came on — a plausible, complete, wrong answer — because a
	published schema was never used as a schema. `Create` declares these `int | None`; a lax
	parser coercing `"4"` is a thing that happens to work rather than a thing that is promised.
*/
export const SAID_AS_NUMBERS = ["importance", "urgency"];


/*
	The fields an **edit** never sends as null — `#757`.

	A task must have all four, and every control that carries one always holds a value, so
	`null` here would mean *clear it* to a route that cannot. Everything else the edit form
	shows is nulled when it is blank, which is the opposite of what creating does and is §8.3:
	a field left out is unchanged, and only an explicit null clears it.

	Named rather than written inline so that the guard comparing the controls the form draws
	against the names the body reads can see them — `title` is drawn by `Editing` alone, and it
	was invisible to that check until this existed.
*/
export const NEVER_CLEARED = ["title", "status", "type", "project"];

export function filed (values, slug) {
	/*
		What a form submission becomes on the wire — pure, so the rule above is checkable.

		**One line and a form are one submission, not two paths.** `POST /v1/tasks` takes `text`
		*and* structured fields, and anything explicit wins over what the line said — measured:
		`text: "… !4/3 ~1h #typed"` with `importance: 5` and `estimate: "30m"` stored importance
		5, estimate 30m, urgency 3 and the tag. That is the whole of why the capture box does not
		have to move or be duplicated to satisfy §1.4: it stays the title, in the same place,
		doing the same thing, and the form is strictly additional.

		**Tags arrive as one written field** rather than as a control per tag, and the `#` is
		optional because that is how a person writes one — the sigil is the *capture line's*, and
		typing it here should not produce a tag called `#home`.
	*/
	const raw = values || {};
	const said = (name) => {
		const value = raw[name];

		return typeof value === "string" ? value.trim() : value;
	};

	const body = { workspace_id: slug };
	const line = said("text");

	if (line) body.text = line;

	SAID_AS_WRITTEN.forEach((name) => {
		const value = TIMED.includes(name)
			? withTime(said(name), said(`${name}_time`))
			: said(name);

		if (value) body[name] = value;
	});

	SAID_AS_NUMBERS.forEach((name) => {
		const value = said(name);

		if (value) body[name] = Number(value);
	});

	const tags = String(said("tags") || "")
		.split(/[\s,]+/)
		.map((one) => one.replace(/^#/, ""))
		.filter((one) => one !== "");

	if (tags.length > 0) body.tags = tags;

	repeating(said).forEach(([name, value]) => { body[name] = value; });

	return body;
}

export function readingRequest (phrase, zone = null) {
	/*
		Ask what a written repeat means, without storing anything — `#94`, §6.7.

		**The zone travels with it**, because the dates that come back are days: *every monday*
		asked from Sydney and answered in UTC lands a day out either side of midnight, which is
		`#773` at the other end of the same wire.

		Pure, like every other request builder here (`#661`) — the guard that drives every one
		of them against a real instance derives its cases from these, so a request assembled
		inline would be the one nothing checks.
	*/
	return {
		path: "/recurrence/parse",
		method: "POST",
		body: zone ? { text: phrase, timezone: zone } : { text: phrase },
	};
}

/*
	The repeat's two controls, named rather than written inline — `#94`.

	They are read by `repeating` rather than by either loop above, because the rule joining them
	is *both or neither* and a list cannot say that. Declared anyway, and in the same shape as
	`SAID_AS_WRITTEN` and `NEVER_CLEARED`, because the guard comparing the controls the form
	draws against the names the body reads works by reading these registers — a field consumed
	only inside a function body is one it cannot see, and it would fail saying the form draws a
	control nothing reads. Which it did, immediately.
*/
export const REPEATED = ["recurrence", "recurrence_anchor"];

export function repeating (said) {
	/*
		The repeat fields, or none at all — `#94`, and the one rule the loops above cannot say.

		**`recurrence_anchor` travels with `recurrence` or not at all.** It qualifies the rule
		and means nothing without one, so the service refuses it alone by name (`#918`) — and
		the anchor control always holds a value, because a repeat is always measured from
		somewhere. Sending the pair independently would therefore refuse **every ordinary
		create**: no phrase typed, a select still reading *the schedule*, and a 422 about a
		field the reader never opened the disclosure to see.

		Returned as pairs rather than written into a body, so `filed` and `edited` can each
		apply their own rule about what an empty control means and neither has to know the
		other's.
	*/
	const [ruleName, anchorName] = REPEATED;
	const rule = said(ruleName);

	if (!rule) return [];

	const anchor = said(anchorName);

	return anchor ? [[ruleName, rule], [anchorName, anchor]] : [[ruleName, rule]];
}

export function readForm (form) {
	/* Every named control on a form, whatever the reader touched — handed to `filed` or
	   `edited`, which decide what any of it means. Read off the DOM rather than tracked,
	   which is what lets both forms hold no state. */
	const values = {};

	Array.from(form.elements).forEach((one) => {
		if (one.name) values[one.name] = one.value;
	});

	return values;
}

export function localMoment (value, zone = null) {
	/*
		An instant as ``YYYY-MM-DDTHH:MM`` in the zone that stored it — what a
		``datetime-local`` control takes (`#798`).

		**The task's zone, not the reader's**, for `#773`'s reason from the other end: an
		appointment written at 14:00 in London is 14:00 in London whoever opens it, and reading
		it back through the browser's own zone would put a different time in the box — which
		saving would then store.
	*/
	if (!value) return "";

	const parts = Object.fromEntries(
		new Intl.DateTimeFormat("en-GB", {
			timeZone: zone || "UTC", year: "numeric", month: "2-digit", day: "2-digit",
			hour: "2-digit", minute: "2-digit", hourCycle: "h23",
		}).formatToParts(new Date(String(value))).map((one) => [one.type, one.value]),
	);

	return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

export function dateFor (value, allDay, zone) {
	/* The day half of a date control. A day is what the item says whether or not it also says
	   a time, so this is the same either way. */
	return calendarDay(value, zone) || "";
}

export function timeFor (value, allDay, zone) {
	/*
		The time half, and empty unless the item has one — `#798`.

		**`all_day` is the item's own answer**, and it is a fact rather than an inference: an
		appointment at midnight and a deadline meaning *the end of that day* are the same
		instant in some zones, so reading the clock and guessing would put `00:00` in the box
		for every ordinary deadline and save it as a time somebody never chose.
	*/
	if (!value || allDay !== false) return "";

	return localMoment(value, zone).slice(-5);
}

export function withTime (day, time) {
	/*
		The two halves back into one field for the wire — `#798`.

		**A day alone stays a day**, which is what keeps `datetime-local` out of this form: the
		control that forces a time would make every deadline carry one somebody had to invent.
		`schedule.interpret` reads both shapes and sets `*_is_all_day` from which it was given,
		so one field carries both meanings and the server decides — measured rather than assumed.

		**A time with no day is nothing**, because there is no day for it to be on. Saying so
		here rather than sending `T14:00` and letting the instance refuse it keeps the refusal
		out of a place the reader cannot act on.
	*/
	const on = String(day || "").trim();
	const at = String(time || "").trim();

	if (!on) return "";

	return at ? `${on}T${at}` : on;
}

export function fromItem (item) {
	/*
		An item as the edit form's starting values — `#757`.

		**Every date goes through `calendarDay` with the item's own timezone** (`#773`). An
		`<input type="date">` wants `YYYY-MM-DD`, and an all-day deadline is stored at the last
		instant of its day in the *task's* zone — so reading it any other way puts the day after
		the deadline into the box, and saving would then move it. A display bug becomes data
		loss the moment a form is filled from the same value.

		**Numbers and tags become the strings a control holds**, because that is what the form
		compares against and what comes back out of it. `filed` and `edited` turn them back.
	*/
	const said = item || {};

	return {
		description: said.description || "",
		/* **A document's prose, so this fills both forms** (`#1044`). Every other field a
		   document has — type, status, project — is already here under the same name, so the
		   alternative was a second builder differing in one line. Absent on a task, where it
		   reads as the empty string and is offered to no control. */
		body: said.body || "",
		/* **The address, not the key** (`#977`). `project_key` is what a project is *called*
		   and stopped identifying one at `#958`; `project_path` is what it is addressed by, and
		   is documented as the string a caller sends back. Falling back to the key because that
		   field is defaulted for an instance older than it (`#345`, `#482`). */
		project: said.project_path || said.project_key || "",
		type: said.type || "",
		status: said.status || "",
		assignee: said.assignee || "",
		importance: said.importance === null || said.importance === undefined
			? "" : String(said.importance),
		urgency: said.urgency === null || said.urgency === undefined
			? "" : String(said.urgency),
		estimate: said.estimate_human || "",
		snooze: dateFor(said.snoozed_until, said.snoozed_is_all_day, said.timezone),
		snooze_time: timeFor(said.snoozed_until, said.snoozed_is_all_day, said.timezone),
		starts: dateFor(said.starts_at, said.starts_is_all_day, said.timezone),
		starts_time: timeFor(said.starts_at, said.starts_is_all_day, said.timezone),
		due: dateFor(said.due_at, said.due_is_all_day, said.timezone),
		due_time: timeFor(said.due_at, said.due_is_all_day, said.timezone),
		tags: (said.tags || []).join(", "),
		/*
			**The words somebody typed, falling back to the rule they compiled to** (`#94`).
			`recurrence_text` is null when a caller sent an `RRULE` directly, and a box that
			opened empty on a task that plainly repeats would read as *this does not repeat* —
			then save as *stop repeating*, because that is what blank means here. A rule in the
			box is ugly and is the truth, and `POST /v1/recurrence/parse` accepts it back.
		*/
		recurrence: said.recurrence_text || said.recurrence_rule || "",
		recurrence_anchor: said.recurrence_anchor || "",
		/*
			**What the server made of it, carried into the form** — `#925`. The box above holds
			the words somebody typed, and Simon's objection is exactly that: *"this does not
			tell me how it has been parsed, and whether it will behave as expected"*. The
			preview only fired on a keystroke, so reopening a repeat to check it showed the one
			thing that confirms nothing — your own input.

			**No request for it**, unlike the live preview: the item already carries the
			sentence, so a form filled from an item can say what is in force before anybody
			touches anything.
		*/
		recurrence_description: said.recurrence_description || "",
	};
}

export function repeats (item) {
	/*
		Whether an item is one of a series, from either end of it — decision `#1249`, `#1253`.

		**The browser's copy of `views.repeats`**, which the API's own clients call. A page
		holds a rendered item and never a row, so the surface deciding whether to put the
		question to somebody has nothing else to read. Getting it wrong is loud in both
		directions: too narrow and the save is refused for not saying, too wide and it is
		refused for saying.

		`is_template` first because that end is reachable — `show` names the series' number
		since `#1247`, so somebody can open it, and reaching it must not be a way round the
		question.
	*/
	return Boolean(item && (item.is_template || item.recurrence_template_ref));
}

export function edited (values, item, appliesTo = null) {
	/*
		What an edit becomes on the wire — pure, and **the opposite rule from `filed`**.

		Creating omits an empty control, because `POST /v1/tasks` refuses an empty string by
		name. Editing must send **`null`**, because §8.3 says a field left out is *unchanged* and
		only an explicit null clears it. Reusing `filed` here would make clearing a deadline
		impossible: blank the box, the field is omitted, the deadline stays, and the form reports
		success. A silent no-op is the worst of the three possible failures.

		**Everything the form shows is sent, unchanged values included.** The alternative —
		sending only what moved — needs the form to compare `2026-09-01` against
		`2026-09-01T23:59:59.999999Z`, which means re-deriving the server's own normalisation on
		this side. That is a second copy of a rule, and this is not a place to keep one.

		**`title`, `status`, `type` and `project` are never nulled.** A task must have all four,
		the controls always hold one, and `null` would mean *clear it* to a route that cannot.

		**`expected_version` is the whole point of the item** (§8.9). It is opt-in, and `None`
		means *did not ask* rather than *asked and passed* — so a form omitting it silently wins
		over whatever somebody saved while it was open. This is the first surface where an item
		sits on screen long enough for that to be likely.
	*/
	const raw = values || {};
	const said = (name) => {
		const value = raw[name];

		return typeof value === "string" ? value.trim() : value;
	};

	const body = { expected_version: (item || {}).version };

	/*
		**Which occurrences this save is for** (decision `#1249`, `#1252`). Sent only when
		answered: `null` here means *nobody was asked* rather than *clear it*, and there is
		nothing to clear — the endpoint refuses an answer about an item with one of it.

		**This form is why the browser had to ask at all.** Everything it shows is sent on
		every save, unchanged values included — see above — so on a repeating item a save
		always writes a field with two answers, and without a question there would be no way to
		save one from here at all.
	*/
	if (appliesTo) body.applies_to = appliesTo;

	NEVER_CLEARED.forEach((name) => {
		const value = said(name);

		if (value) body[name] = value;
	});

	SAID_AS_WRITTEN.forEach((name) => {
		if (NEVER_CLEARED.includes(name)) return;

		body[name] = (TIMED.includes(name)
			? withTime(said(name), said(`${name}_time`))
			: said(name)) || null;
	});

	SAID_AS_NUMBERS.forEach((name) => {
		const value = said(name);

		body[name] = value ? Number(value) : null;
	});

	body.tags = String(said("tags") || "")
		.split(/[\s,]+/)
		.map((one) => one.replace(/^#/, ""))
		.filter((one) => one !== "");

	/*
		**Blank stops the repeat, which is this form's only way to say so** (`#94`). The rest of
		this function nulls an empty control because §8.3 makes that the difference between
		*unchanged* and *cleared*, and a repeat reads it the same way — the series ends, the
		work in hand keeps its number and its record, and nothing follows it.

		The anchor rides along only when there is a rule, for `repeating`'s reason: on the way
		to a `PATCH` a lone anchor is refused just as it is on the way to a `POST`.
	*/
	const repeat = repeating(said);

	body.recurrence = repeat.length > 0 ? repeat[0][1] : null;

	if (repeat.length > 1) body.recurrence_anchor = repeat[1][1];

	return body;
}

export function statusRequest (row, where, slug) {
	/*
		Move an item to a status, and **nothing else** — `#758`.

		**A status and a claim are different facts and neither is derived from the other**
		(`#726`, Simon's ruling), so this body has one field in it. It would be easy to make
		*in progress* claim the item on the way past; that is a write nobody asked for, and it
		would make the claim's meaning depend on which surface moved the status.

		**No `expected_version`, deliberately, and this is the one place that is right.** §8.9 is
		for a form somebody has been typing into while the world moved — `#757`. This is a single
		control read and written in one gesture, and refusing it because an unrelated field
		changed would be a conflict a person cannot act on and did not cause.

		**Both kinds, because a document has a status too** (`#1419`). `db/seed._STATUSES` gives
		a document `draft`, `active`, `superseded` and `archived`, and the control above is
		deliberately outside `completable`'s gate so that a reader can move one — a status is
		the only thing on this component a document *does* have. This builder hardcoded
		`/tasks/` and every press of it on a document was refused by name, which is the whole of
		`#1419`: the vocabulary lookup asked `item.kind` and the write did not.

		**Six sites in this file already branch on `item.kind`** — both comment builders, both
		link builders, the vocabulary above and the save path — and each is correct. *N copies
		that agree hide the one that does not ask*, which is `#1281` with its halves swapped.
	*/
	const collection = row.kind === "document" ? "documents" : "tasks";

	return {
		path: scoped(`/${collection}/${row.ref}`, slug),
		method: "PATCH",
		body: { status: where },
	};
}

export function conflictIn (failure) {
	/*
		The item as it now stands, when a refused save was somebody else getting there first —
		`#757`, §8.9. Null for every other refusal, which is then an ordinary note.

		**Both halves are needed and the second is the one that would be forgotten.** A 409 says
		the version moved; `current` is what `concurrency.reporting()` attaches so a client can
		say *what* it moved to. An instance that did not attach it — an older one, or a proxy
		that rewrote the body — would otherwise put an empty conflict on screen: a warning
		naming nothing, which reads as a bug in the page rather than as news about the item.

		Pure, because the decision is the part worth checking and `save` cannot be run by this
		project's harness (`#640`).
	*/
	if (!failure || failure.status !== 409) return null;

	return (failure.body && failure.body.current) || null;
}

export function updateRequest (values, item, slug, appliesTo = null) {
	/* Save an edit. `edited` builds the body here for the reason `addRequest` calls `filed`:
	   it is the guard that drives every builder against a real instance which then drives the
	   body-building too. */
	return {
		path: scoped(`/tasks/${item.ref}`, slug),
		method: "PATCH",
		body: edited(values, item, appliesTo),
	};
}
