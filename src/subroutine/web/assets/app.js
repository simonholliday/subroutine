/*
	The browser app — item `#597`. Read-only: see what there is, and read one in full.

	**No build step.** Preact and htm are served as written from `/app/`, and an import map in
	`index.html` resolves the one bare specifier between them. What that buys is not
	convenience: it is that the source a reader is served **is** the source in the repository,
	so the published-source promise means something for the half that runs in a browser — and
	there is no npm closure for `scripts/check_licences.py` to be structurally unable to see.

	The first of those was the AGPL's network-use clause until 2026-08-08 and is now a product
	commitment (§2.2). It is the weaker of the two arguments and it was never the deciding one.

	**It talks only to the public API** (`#351`). No private endpoints, nothing a token could
	not do — so anything this page can show, a script can too, and the UI cannot quietly become
	the only way to do something.
*/

import { h, render, Component } from "preact";
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import htm from "htm";

/* A relative specifier, so it resolves by itself and needs no entry in the import map — which
   is the thing that was missing when this page first shipped blank. */
import * as markdown from "./markdown.js";

const html = htm.bind(h);

/*
	How often to ask what has changed. `GET /v1/changes?since=` was built for exactly this and
	is what SSE's own reconnection protocol reduces to — `Last-Event-ID` *is* `?since=`. So the
	catch-up path is needed either way, and polling first means it runs on every tick rather
	than only after a network blip.

	Measured against the instance's own limit: `rate_limit_per_minute` is 600, and a ten-second
	poll spends 6 of them.
*/
const POLL_MS = 10000;

/* How many rows to ask for. The listing says when it had to stop, so this is a page rather
   than a ceiling on what exists. */
const PAGE = 100;

/*
	**What a listing asks for, which is what a row shows and nothing else** (§14.10, `#645`).

	Measured on the served instance: a whole page of tasks is 287 KB and a whole page of
	documents is **1.3 MB**, because a document's body comes down in full and the bodies here
	are the specification. Shaped, the pair is 38 KB — forty-two times smaller, and the reader
	sees exactly the same list.

	§14.10 calls response size a first-order cost for the agent client. It is a first-order cost
	for a browser on a train too, and this is the surface that felt it first.

	**These are a second copy of what `Row` renders**, which is the shape this codebase gets
	wrong most — so `tests/test_web.py` derives the requirement from `Row`, `marks`, `when` and
	`overdue` rather than trusting the two to be kept in step. A field left out does not error:
	it arrives as null, and a null reads as *not set* rather than as *not asked for*, which
	would quietly invert §12.2c.
*/
const TASK_FIELDS = [
	"ref", "title", "due_at", "planned_for", "blocked", "project_key", "assignee",
	"status", "status_is_default", "status_category",
	/* Who is holding a lease, and until when (`#726`). All three, because the mark says the
	   holder's name, the id is what says anybody holds it at all, and the expiry is what says
	   whether that still means anything — `claimed_by` alone would be null on an instance older
	   than the field while the item was genuinely claimed. */
	"claimed_by_id", "claimed_by", "claim_expires_at",
	/* Rendered by `when` on anything finished, and the field the *done* view is ordered on
	   (`#706`). §22 has no rule about showing the sort key and `#661` is the item that wants
	   one; a page whose whole claim is *most recently finished first* had better say when each
	   row finished, or the order is something a reader has to take on trust. */
	"completed_at",
	/* Not rendered — it is what the two collections are merged on (`#660`). The API sorts
	   both by `-created_at` and pages on it, so ordering them together by anything else
	   would put the client and the cursor into disagreement. */
	"created_at",
].join(",");

/* A document has no dates and no assignee — `_when` returns nothing for one — so it asks for
   less. Not the same list with the extras arriving null, because that is the difference
   between "has no deadline" and "cannot have one", and only one of them is true. */
const DOCUMENT_FIELDS = [
	"ref", "title", "project_key", "status", "status_is_default",
	/* Not rendered either — it is what the board groups on (`#653`). A document's categories
	   are its own vocabulary, so it gets its own columns rather than being mapped onto a
	   task's: `current` is not *in progress*, and saying so would be inventing a claim. */
	"status_category",
	/* As above: the merge key, not something a row shows (`#660`). */
	"created_at",
].join(",");

class Refused extends Error {
	/* Carries the status so the caller can tell "sign in" from "something went wrong". */
	constructor (status, detail) {
		super(detail || `The instance answered ${status}.`);
		this.status = status;
	}
}

async function api (path, { method = "GET", body = null } = {}) {
	/*
		Every request sends the cookie and nothing else. `credentials: "same-origin"` is the
		default for a same-origin fetch, and it is written out because this page working at all
		depends on it — a second port would be cross-origin, which is the arrangement `#364`
		warns about and the reason the app is served from the instance itself.

		**One request function, reads and writes together.** Every refusal then arrives the same
		way, which is what lets a failed write say what happened rather than disappearing — and
		a second one would be a second place for the credential rules to be written down.
	*/
	const sending = body !== null;

	const answer = await fetch(`/v1${path}`, {
		method,
		credentials: "same-origin",
		headers: sending
			? { accept: "application/json", "content-type": "application/json" }
			: { accept: "application/json" },
		body: sending ? JSON.stringify(body) : undefined,
	});

	if (answer.status === 204) return null;

	if (answer.ok) return answer.json();

	/* A problem document carries `detail`; anything else is reported by its status alone
	   rather than by guessing at a body we did not parse. */
	let detail = null;

	try {
		detail = (await answer.json()).detail;
	} catch (_) {
		detail = null;
	}

	throw new Refused(answer.status, detail);
}

/* ---- one list, not two end to end (`#660`) ------------------------------- */

export function newestFirst (rows) {
	/*
		Put tasks and documents into one order, which is the order they were written.

		**The list is two requests and has to be one list.** Tasks and documents are separate
		collections — §6.2 gives them one ref counter precisely so a reader can treat them as one
		thing — and concatenating them put every document below every task. On a project holding
		122 tasks against a page of 100, a document written a minute ago started at row 101 at
		best. Simon met it on `#659`: *"I did not see your new item arrive, but then I saw it is
		some way down the list."*

		**`created_at` rather than `ref`, and that is the whole of why the field is asked for.**
		A ref would be a fine proxy — one counter, allocated in order — but the *server* sorts by
		`-created_at` and its keyset cursor pages on it (`ordering.DEFAULT_TASK_ORDER` and
		`DEFAULT_DOCUMENT_ORDER`, both `("-created_at",)`). A client ordering by anything else
		would disagree with the boundary it is paging across, which is the defect keyset
		pagination exists to prevent.

		**`ref` breaks a tie**, descending, because the server's tiebreaker follows the last
		key's direction and refs are allocated from one counter in creation order — so it agrees
		with the server wherever two rows share a timestamp, without asking for `id` as well.

		**Compared as instants, and the honest reason is not the obvious one.** Measured across
		the two shapes this API emits — `…:00+00:00` and `…:00.100000+00:00` — lexicographic
		order and chronological order *agree*, because the characters that differ (`+` against
		`.`) happen to fall the right way. So string comparison would work today, and saying it
		would not would be a claim nothing supports.

		`Date.parse` is kept for what it does at the edges: it is right whatever the
		representation, including an offset other than `+00:00`, which this serialisation does
		not produce and a future one might. Its own cost is that it truncates to the millisecond
		— two rows a microsecond apart tie here — and `ref` resolves exactly that, correctly,
		because refs come from one counter in creation order.
	*/
	return [...rows].sort((one, other) => {
		const first = Date.parse(one.created_at);
		const second = Date.parse(other.created_at);

		if (first !== second) return second - first;

		return other.ref - one.ref;
	});
}

export function accumulated (held, arriving, { appending, collections }) {
	/*
		What the list becomes when a page arrives — the whole rule, in one place.

		**Appending is re-merged over everything held** (`#660`): a second page of tasks belongs
		*above* documents already on screen, so extending the array made the list alternate
		between the two collections after one *Show more*, in no order at all.

		**And the merge runs only where there is more than one collection to merge** (`#706`).
		That is the actual reason rather than a proxy for it. `newestFirst` sorts on `created_at`
		because that is the one key both collections are paged by; applied to a single collection
		the *server* has ordered, it silently overwrites that order. The done view asks for
		`-completed_at`, so merging would produce a page ordered by when work was **written**
		under a heading claiming when it was **finished** — plausible, complete and wrong.

		**It takes the count rather than the view name** so that an arrangement added later cannot
		get this wrong by being spelled differently, and it is a pure function rather than three
		lines inside `load` for the reason `#640` has now demonstrated five times: the harness
		calls components as plain functions, so a decision left inside `App` is covered by nothing.
	*/
	const all = appending ? [...held, ...arriving] : arriving;

	return collections > 1 ? newestFirst(all) : all;
}

/* ---- surviving a component that throws (`#680`) -------------------------- */

export function unrenderable (failure, what) {
	/*
		What a reader is shown in place of something that would not render.

		**Pure, and separate from the component, because the component cannot be tested.**
		`preact-render-to-string` does not run error boundaries — measured, both spellings — so
		the harness can prove what this says and cannot prove that Preact calls it. Keeping the
		decision out here is what makes the half we own checkable at all, which is the move
		`#640` arrived at four times.

		It names the thing rather than apologising, and it says the rest of the page is still
		good, because the failure a reader meets is *silence* and the question they have is
		whether anything else can be trusted.
	*/
	const message = (failure && failure.message) || String(failure || "");

	return {
		said: `${what || "This"} could not be displayed.`,
		/* The message verbatim. It is for us rather than for them — but a reader who reports a
		   problem with the words in front of them saves the round trip that asks for them. */
		detail: message,
	};
}

class Boundary extends Component {
	/*
		Show something when a component below this throws, rather than nothing at all.

		**This exists because two blank pages shipped from one week's work** — an import map
		missing `htm`, and a dependency array naming a value declared below it (`#643`). Both
		threw during the first render, both left an empty document, and both were found by a
		person rather than by the build. A boundary would have turned each into a sentence.

		**What is verified and what is not, stated rather than implied.** `unrenderable` above is
		pure and tested. That Preact calls `componentDidCatch` at all is documented framework
		behaviour this project's harness cannot exercise, because rendering an error boundary
		needs a DOM and `#640` refused jsdom at ~2 MB. So this is belt; `Prose`'s own catch is
		braces, and that one depends on nothing.
	*/

	componentDidCatch (failure) {
		this.setState({ failed: failure });
	}

	render (props, state) {
		if (!state.failed) return props.children;

		const note = unrenderable(state.failed, props.what);

		return html`
			<div class="broke">
				<strong>${note.said}</strong>
				${" "}<span class="detail">${note.detail}</span>
			</div>
		`;
	}
}

/* ---- what this app asks for (`#640`) ------------------------------------- */

/*
	**Every request this app makes is built here, and nowhere else.**

	Three of the four faults this arc shipped were a request the instance refused, or one that
	asked the wrong question: `?limit=` on a route that declares none, `&subtree=true` beside a
	project filter, and a list ordered by something the command line does not order by. Each
	reached a reader, because the decision about *what to ask for* lived inside a component no
	test can render — every one was found by Simon opening the page.

	Out here they are pure: arguments in, `{path, method, body}` out. So `tests/test_web.py` can
	call each one and check the query it builds against the parameters the real route declares —
	`api/query.py` refuses a parameter a route did not ask for, which makes that the same rule
	the instance applies, run before it ships rather than after.

	**Checking the spelling is not that check**, and this is not hypothetical: the test written
	for `subtree` asserted the string was *present*, so it passed while every filtered listing
	422'd into the failure page.
*/

function sent (request) {
	/* Make one built request. The only place a builder's answer meets the network, which is
	   what lets a test hold the two apart. */
	return api(request.path, { method: request.method, body: request.body ?? null });
}

function scoped (path, slug) {
	/* The workspace, on a path that may or may not already be asking something. */
	return `${path}${path.includes("?") ? "&" : "?"}workspace_id=${encodeURIComponent(slug)}`;
}

export function identityRequest () {
	/* Who is reading, and which workspaces they are allowed to see. */
	return { path: "/me", method: "GET" };
}

export function headRequest () {
	/* The newest event there is, so the first poll asks what happens *next* rather than
	   replaying everything that ever has. */
	return { path: "/changes?newest=true&limit=1", method: "GET" };
}

export function pollRequest (slug, since) {
	/*
		Whether anything has changed. One row is enough — the question is yes or no.

		**With nothing to resume from, ask for the newest instead** (`#656`). A freshly
		initialised instance holds no events at all, so the head is empty and there is no seq to
		carry; sending `0` for it is refused, because a seq starts at 1 and *"0 names nothing"*.
		That is `#309` a third time, and the reason it is worth a branch rather than a default is
		that the poll swallows its own failures — so the cursor would stay at `0`, every tick
		would be refused in the same way, and the page would simply never notice anything again.

		**Omitting `since` would also be accepted and would be wrong**: with `limit=1` that
		returns the *oldest* event, so the cursor would advance one seq per tick and crawl.
	*/
	const asking = since === null || since === undefined
		? "/changes?newest=true&limit=1"
		: `/changes?since=${encodeURIComponent(since)}&limit=1`;

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

	return asked.status_category === undefined ? ["task", "document"] : ["task"];
}

export function listingRequests (slug, key = null, after = null, selection = null) {
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
	const rows = Object.keys(SELECTABLE)
		.filter((name) => chose[name] !== undefined && chose[name] !== null)
		.map((name) => `&${name}=${encodeURIComponent(chose[name])}`)
		.join("");

	const asks = {
		task: { kind: "task", method: "GET", path: scoped(
			`/tasks?limit=${PAGE}&fields=${TASK_FIELDS}${narrowed}${rows}`
			+ from(after && after.tasks), slug) },
		document: { kind: "document", method: "GET", path: scoped(
			`/documents?limit=${PAGE}&fields=${DOCUMENT_FIELDS}${narrowed}`
			+ from(after && after.documents), slug) },
	};

	/* **Tagged with the kind rather than positional**, so a selection reading one collection
	   cannot have its rows labelled by whichever slot they happened to arrive in. */
	return collectionsFor(chose).map((kind) => asks[kind]);
}

export function itemRequests (kind, ref, slug) {
	/* One item in full: the thing, what it links to, and what was said about it. */
	const collection = kind === "document" ? "documents" : "tasks";

	return [
		{ path: scoped(`/${collection}/${ref}`, slug), method: "GET" },
		{ path: scoped(`/${collection}/${ref}/links`, slug), method: "GET" },
		{ path: scoped(`/${collection}/${ref}/comments?limit=${PAGE}`, slug), method: "GET" },
	];
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

export function assignRequest (row, who, slug) {
	/* Hand it over, or take it back off everybody — `null` is a value here, not an omission. */
	return {
		path: scoped(`/tasks/${row.ref}`, slug),
		method: "PATCH",
		body: { assignee: who },
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
	"estimate", "start", "planned_for", "due",
];

/*
	**Sent as numbers, not as the strings a control holds** (`#549`). `{"today": "false"}` was
	truthy in Python and the filter came on — a plausible, complete, wrong answer — because a
	published schema was never used as a schema. `Create` declares these `int | None`; a lax
	parser coercing `"4"` is a thing that happens to work rather than a thing that is promised.
*/
export const SAID_AS_NUMBERS = ["importance", "urgency"];

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
		const value = said(name);

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

	return body;
}

export function vocabularyRequest (slug) {
	/*
		What this workspace calls things — the types, the statuses and the tags a form offers.

		**`?workspace_id=` is not optional, and asking without it answers 200 with nothing.**
		Measured: `/v1/meta` on this instance returns `"statuses":{}`, `"item_types":{}` and
		`"link_types":[]` when no workspace is named, because they are per-workspace vocabulary
		and it has not been told which. A form built from that answer would offer a type dropdown
		with no types in it, and nothing would have failed — `#571` is the item for the shape.
	*/
	return { path: scoped("/meta", slug), method: "GET" };
}

export function projectsRequest (slug) {
	/*
		Where a new item can be filed, **in tree order** (`#770`).

		**Four fields rather than the whole project**, on `#645`'s measurement: a listing that
		asks for what it renders is the difference between 287 KB and 38 KB. `is_inbox` is here
		because the Inbox is where an item with no project lands, so it is the one entry a form
		can label as *what happens if you say nothing*.

		**`path` orders and `depth` indents, and only one of them is a field.** `path` is
		sortable and *not* selectable — measured, by asking for it and being told the twenty-one
		fields a project has. So the shape of the tree arrives as an order plus a number, which
		is enough: a flat list put `Web UI` beside `Websites` as though they were the same kind
		of thing, when one is inside `Subroutine` and the other is a root.
	*/
	return {
		path: scoped("/projects?fields=key,title,is_inbox,depth&order=path&limit=200", slug),
		method: "GET",
	};
}

export function people (roster) {
	/*
		Who work can be handed to, and **which of them is an agent** (`#770`).

		The roster has published `is_service_account` since M1 and this app read the username and
		threw the rest away — so on this workspace the control offered one person and four
		agents, all looking like colleagues.

		**That is worse than untidy.** `#473` made an agent answer to a person and `#474` is that
		delegation has never once been used here; handing work to `claude-nuc14` in the belief it
		is a colleague is the failure the accountability chain exists to prevent.

		**Said in a word, not in a glyph or a colour** (`#102`): nothing may be information only
		in how it looks, and an icon beside four of five names is exactly that. *(agent)* rather
		than *(bot)* because agent is the word this product uses everywhere else — the spec, the
		skill, `subroutine agent create` — and a second name for one thing is what this codebase
		keeps paying for.

		**The username is the label, not `display_name`.** A reader who picks *Claude on nuc14*
		and then reads `claude-nuc14` back off the item has been shown two names for one
		account, which is `#515`'s shape in miniature: every step works and the confirmation
		does not match the choice.
	*/
	return (roster || []).map((row) => ({
		username: row.user.username,
		label: row.user.is_service_account
			? `${row.user.username} (agent)`
			: row.user.username,
	}));
}

export function offered (vocabulary, kind) {
	/*
		The options for one dropdown, and which is chosen when nobody has chosen — `#756`.

		**Never a literal array.** A type and a status are workspace vocabulary: renameable, and
		an instance may add one. A form carrying its own list is wrong on the first workspace
		that does either, and wrong silently, because the control still looks complete.

		Reads `is_default` for the pre-selection rather than assuming a key, for the same reason:
		`open` and `task` are what `seed.py` happens to install here, not what the model promises.
	*/
	const known = (vocabulary && vocabulary[kind]) || [];

	return known.map((one) => ({
		key: one.key,
		label: one.label || one.key,
		chosen: Boolean(one.is_default),
	}));
}

export function filableFor (projects, project) {
	/*
		Where a new item can go, and which entry is chosen when nobody has chosen — `#756`.

		**The address decides**, which `#738` already settled: `/{workspace}/{project}` says
		where rows come from, so it says where a new one goes. Nothing new is parsed. With no
		project in the address — on the agenda, or on a whole workspace — it is the Inbox, which
		is where an item with no project lands anyway.

		**A project the address names and the listing does not hold is added rather than
		ignored.** Otherwise nothing would be chosen, the browser would select the first option,
		and the item would file into the Inbox under an address naming somewhere else — a wrong
		destination, silently, which is worse than any refusal. It can happen: this asks for 200
		projects and a workspace may hold more.

		The same shape as `offered` on purpose, so both fill the same control and neither grows
		its own idea of what a dropdown is.
	*/
	/*
		**Indented by depth, which is what `subroutine project list` already does** — two spaces
		per level, so the two surfaces render one tree the same way rather than each inventing a
		shape for it. Non-breaking, because an `<option>` is the one place a browser may collapse
		leading whitespace and the indent is the whole of what is being said.
	*/
	const known = (projects || []).map((one) => ({
		key: one.key,
		label: "  ".repeat(one.depth || 0)
			+ `${one.title || one.key}${one.is_inbox ? " (default)" : ""}`,
		chosen: project ? one.key === project : Boolean(one.is_inbox),
	}));

	if (!project || known.some((one) => one.chosen)) return known;

	return [{ key: project, label: project, chosen: true }].concat(known);
}

export function agendaRequest () {
	/*
		What is due, across **every** workspace this reader can see — `#652`, decision `#649`.

		**No `workspace_id`, and that is the whole point.** `GET /v1/tasks` refuses an ambiguous
		workspace (§8.2); `/v1/agenda` deliberately does not, and answers for all of them —
		measured against this instance, where naming `projects` returns 153 unscheduled and
		naming nothing returns 160 with an overdue row the narrower question cannot see. §13.7
		is the reason: `today` merges and `ls` groups, because a merged agenda with a dentist
		appointment beside a stand-up is the worked example that rule exists for.

		**No `timezone` either, and that is not an omission.** §6.5's chain is explicit → user →
		workspace → instance, and a *user* has a timezone. Sending the browser's would beat a
		setting the person deliberately made, on every request, silently — which is the opposite
		of what an explicit level is for. `Intl` knows where the machine is; it does not know
		where the reader keeps their diary.
	*/
	return { path: "/agenda", method: "GET" };
}

/* ---- addresses (`#638`) -------------------------------------------------- */

/*
	**Two forms, and only one of them is an identifier.**

	`/{workspace}/{ref}` is the durable one. A ref is allocated once per workspace and never
	reused (§6.2), and a workspace slug *cannot be renamed* — deliberately (`#295`), because it
	is the middle segment of every address anybody ever wrote down. So nothing in it can go
	stale.

	`/{workspace}/{project}/{ref}` is the same address with a word in it for a person reading
	it. A project key **can** be renamed — `sr` became `subroutine` on 2026-08-08, across 502
	items — so that segment is a rendering rather than a fact. It is generated fresh whenever a
	link is made, never stored, and never trusted on the way back in: the ref is last, so a
	stale one still resolves and the app corrects the bar to the current spelling.

	**One project segment, not the ancestor path.** A key is unique per workspace, so the leaf
	alone is exactly as unambiguous as `subroutine/ui` would be — and it has one fewer thing
	that can go stale, since renaming a parent would otherwise invalidate every descendant's
	address. Extra segments are accepted and ignored, so growing into the path form later costs
	nothing.
*/

export function addressOf (item, workspace, place = null) {
	/*
		The address to put in the bar for one item: readable when we know enough, durable
		always.

		**`place` keeps the reader where they were** (`#772`). Opening `#768` from
		`/projects/websites/simonholliday-com` used to leave `/projects/simonholliday-com/768`
		in the bar — the address still resolved, because everything before the ref is decoration
		(`#638`), but the tree the reader had navigated was gone from it.

		Only when the path they are on names *this item's own project*. From the agenda, from a
		whole workspace, or by following a mention into somewhere else, there is no route to
		preserve and the item's own form is the honest answer.

		**Derived from the address rather than from the project tree, and that is the deciding
		argument.** The tree would give a canonical ancestry for every item — nicer in principle
		— but it arrives from a fetch, so the same click would produce a different address
		depending on whether that fetch had landed. Every fault this app has shipped is that
		shape. The reader's own path is already in `window.location` and cannot be half there.

		**A row's `href` is deliberately left as the item's own form.** A link is about the item
		— it gets copied, opened in a tab, sent to somebody — where the bar is about this visit.
		Both resolve to the same item; only one of them should carry where you happened to come
		from.
	*/
	const durable = `/${encodeURIComponent(workspace)}/${item.ref}`;

	if (!item.project_key) return durable;

	const trail = place && place.workspace === workspace && place.trail
		&& place.trail[place.trail.length - 1] === item.project_key
		? place.trail
		: [item.project_key];

	return `/${encodeURIComponent(workspace)}`
		+ `/${trail.map(encodeURIComponent).join("/")}/${item.ref}`;
}

/* ---- what is showing: an arrangement and a selection (`#651`, `#649`) ----- */

/*
	**The path decides place; the query decides selection and arrangement; a view never
	selects.** Decision `#649`, rewritten on 2026-08-09 because its first version had two things
	where there are three — it put *selection* on the path's side of the line, and the first
	honest application of that rule produced a board whose *Done* column was structurally
	incapable of holding anything (`#718`).

	| | |
	| --- | --- |
	| `?view=` | the **arrangement** — how the rows that arrived are displayed |
	| `?status_category=`, `?include_completed=`, `?order=` | the **selection** — which rows arrive |

	§14.10 is the same separation one layer in, and it is untouched: `?fields=` and `?format=`
	decide how a row is *reported* while `domain/scoping.py` decides which rows exist, and
	`api/shaping.py` takes already-rendered views specifically so a display parameter cannot
	reach the `WHERE` clause. What changed is that a selection is no longer pretending to be a
	display parameter — it is a filter, in the open, in the address.

	**A control may set several parameters at once; the address states each of them.** So a
	reader can see what they are looking at, send it to somebody, and take one part away. That
	last is not theoretical: separating them is what makes `?include_completed=true` on a *list*
	and `?status_category=in_progress` on either arrangement possible at all, and neither was
	reachable while a view name carried the selection.

	**The bound that keeps this from meaning nothing** (`#649`): a selection parameter may only
	be one the caller could have sent anyway, and may never widen what a credential can see.
	`SELECTABLE` is that bound written down — a name the browser does not know is refused here
	rather than forwarded, so this can never become a passthrough to the query layer.
*/
export const VIEWS = ["list", "board"];

export const DEFAULT_VIEW = "list";

export const SELECTABLE = {
	/*
		`status_category` rather than `status`: a status *key* is per-workspace and renameable,
		so an address keyed on one breaks on the first instance that renames it. The four
		categories are fixed by the model, which is why they can be spelled here at all.
	*/
	status_category: ["todo", "in_progress", "done", "cancelled"],
	include_completed: ["true"],
	/*
		**One order, and it names its successor rather than pretending to be general.** `#661`
		is what puts ordering in a reader's hands; until then the only order any control here
		produces is the finished view's, and allowing more would be publishing an address whose
		results nothing has driven. `-completed_at` rather than `-updated_at`: the tempting
		proxy reorders the page whenever somebody edits a finished item, for a reason nobody did.
	*/
	order: ["-completed_at"],
};

/* What the controls produce. Named because two places need each — the chip that writes the
   address and the test that drives it — and a second spelling is what drifts. */
export const EVERYTHING = { include_completed: "true" };

export const ONLY_FINISHED = { status_category: "done", order: "-completed_at" };

export function viewOf (search) {
	/*
		Which arrangement an address asks for, and whether it asked for one that does not exist.

		**Refused rather than ignored, and named rather than blanked** — the two rules this app
		already follows, from opposite directions. `api/query.py` refuses a query parameter a
		route does not declare, because silently ignoring `fields` returns the whole object and
		charges the caller for it. But a person types a URL, and replacing their page with a
		failure over one wrong word is worse than showing them the list and saying so — which is
		exactly the shape `chosenWorkspace` settled for a workspace nobody can see.

		So: fall back, and hand the caller the word that was refused.
	*/
	const asked = new URLSearchParams(String(search || "")).get("view");

	if (asked === null || asked === "") return { view: DEFAULT_VIEW, refused: null };

	return VIEWS.includes(asked)
		? { view: asked, refused: null }
		: { view: DEFAULT_VIEW, refused: asked };
}

export function selectionOf (search) {
	/*
		Which rows an address asks for, and which of its words were not understood.

		**Same rule as `viewOf` and for the same reason**: a person types these. An unknown
		selection is dropped rather than forwarded — forwarding would make this a passthrough to
		`api/query.py`, which refuses a parameter it does not declare, so one typo would replace
		the reader's page with a 422 instead of their list.

		**A value outside its list is refused too, not only an unknown name.** `status_category`
		exists and `?status_category=finished` does not; admitting the name and passing the value
		is the half-check that reads as validation and is not.
	*/
	const asked = new URLSearchParams(String(search || ""));
	const selection = {};
	const refused = [];

	Object.keys(SELECTABLE).forEach((name) => {
		const value = asked.get(name);

		if (value === null || value === "") return;

		if (SELECTABLE[name].includes(value)) selection[name] = value;
		else refused.push(`${name}=${value}`);
	});

	return { selection, refused };
}

export function showingOf (search) {
	/* The arrangement and the selection an address asks for, read once so the two cannot be
	   read from different places and disagree — which is `#719`'s defect in miniature. */
	const arrangement = viewOf(search);
	const rows = selectionOf(search);

	return {
		view: arrangement.view,
		selection: rows.selection,
		refused: (arrangement.refused ? [`view=${arrangement.refused}`] : []).concat(rows.refused),
	};
}

export function withShowing (path, showing) {
	/*
		One address, carrying everything about what is on screen — `#651`'s *survives
		navigation*, widened by `#738` to carry the selection too.

		Four places wrote an address before `#651` and every one of them dropped the query, so
		`/projects?view=board` became `/projects` the moment anything was opened.

		**The arrangement is always written, including the default** (`#745`, Simon's). This
		narrows a rule `#651` recorded — *the default is the absence of the parameter, so it can
		become per-workspace and later per-user without invalidating an address anybody wrote
		down* — and the narrowing is the point: `#649` says **the address states each of them, so
		what you send somebody is what you were looking at**. An address omitting the arrangement
		hands its reader *their* default rather than the sender's page.

		A bare address still works and must: `viewOf` falls back exactly as before, so `/projects`
		typed by hand is the list. What changed is only what a *control* writes.

		**A selection is written out for the same reason**, and always was — there is no default
		to fall back to, and the absence of one *is* the ordinary selection.

		**Emitted in `SELECTABLE` order rather than the order they were set**, so the same
		screen always produces the same string — `go` compares the address it wants against the
		one in the bar and would otherwise push a duplicate entry onto the history.

		**An address naming one item takes neither, and that is enforced here rather than at the
		call sites** (`#766`). Both halves describe a set of rows — which ones arrive and how they
		are laid out — and an item address has one row that is not part of any set. Typing
		`/projects/ui/441` used to leave `/projects/ui/441?view=list` in the bar: a parameter
		saying nothing, in its default, about rows that are not there.

		The app already disagreed with itself about it. Every link *to* an item — a listing row,
		a prose mention, an entry in the Links list — is `addressOf`, which is a bare path. Only
		the rewrite that followed the click added a query, so the href a reader could copy and
		the address they landed on were two strings for one page, and the href was the honest one.

		**Keyed on the path rather than on a flag the caller passes**, because a flag is a thing
		to forget: `chips`, `backTo`, `widenTo` and `reloads` all build listing addresses and are
		untouched, and a fifth writer added later cannot get this wrong. `parseAddress` is
		already the one place that knows what an item address looks like.
	*/
	const place = parseAddress(path);

	if (place !== null && place.ref !== null) return path;

	const view = showing && showing.view;
	const selection = (showing && showing.selection) || {};

	const parts = (view ? [`view=${encodeURIComponent(view)}`] : [])
		.concat(Object.keys(SELECTABLE)
			.filter((name) => selection[name] !== undefined && selection[name] !== null)
			.map((name) => `${name}=${encodeURIComponent(selection[name])}`));

	return parts.length === 0 ? path : `${path}?${parts.join("&")}`;
}

export function reloads (before, after) {
	/*
		Whether moving from one showing to another has to ask the instance again.

		**Only a change of selection does** (`#738`). An arrangement is a rendering of rows
		already held — which is what the first version of `chooseView`'s comment said, and it was
		right about *that* half and wrong that the same went for finished work.

		**Lifted out of `App` rather than written inline**, which is `#640`'s cheapest route and
		the reason it is worth the function: four faults shipped from decisions left inside that
		component, every one found by Simon rather than by the build, and a wrong answer here is
		exactly that shape — a page showing the rows of a selection it is no longer on, with the
		address, the rule and the display all individually correct.

		Compared through `withShowing` with the arrangement blanked, so *what makes two
		selections the same* is answered in one place. Comparing the objects would make key order
		significant, and `{a, b}` and `{b, a}` are the same selection.
	*/
	const asked = (showing) => withShowing("", { view: null, selection: showing.selection });

	return asked(before) !== asked(after);
}

export function chips (behind, showing) {
	/*
		The controls beside a listing, and what each one is about to write.

		**A control, because an address is not a way to find something** (`#651`) — a reader who
		has never seen one cannot type a word they have not been told, and a filter with no
		control is a feature nobody finds. Every tracker a person has used offers these as tabs.

		**Two of them are arrangements and one is a selection, which is the whole of `#738`.**
		They look alike deliberately: the taxonomy belongs in the address, not in the furniture.

		**`done` shows a list, and it used to keep whichever arrangement was showing** — which I
		argued for and which driving refuted at once: *board* then *done* gave a board with one
		populated column and three empty ones. The principled statement, which is worth more than
		"it looked wrong": the finished selection carries `order=-completed_at`, and **an order
		means nothing on a board**, because a board groups rows into columns and discards the
		sequence they arrived in. A selection carrying an order belongs in a list.

		**Chosen is computed, never remembered.** An address no control produces —
		`?status_category=in_progress`, say — highlights nothing, which is true rather than
		tidy.
	*/
	const narrowed = showing.selection.status_category !== undefined;

	return [
		{ name: "list", showing: { view: "list", selection: {} } },
		{ name: "board", showing: { view: "board", selection: EVERYTHING } },
		{ name: "done", showing: { view: DEFAULT_VIEW, selection: ONLY_FINISHED } },
	].map((chip) => ({
		name: chip.name,
		href: withShowing(behind, chip.showing),
		showing: chip.showing,
		chosen: chip.name === "done"
			? showing.selection.status_category === "done"
			: !narrowed && showing.view === chip.name,
	}));
}

export function listingAddress (place) {
	/*
		The address of whatever listing is showing behind an open item.

		**Closing an item used to push `/` unconditionally**, which was harmless while `/` was
		the list and became a defect the moment `#652` made it the agenda: the address said the
		agenda and the page went on showing a workspace listing, so reloading or stepping back
		gave something the reader had not been looking at. Found by reading `close` while wiring
		the view through it — nothing failed, because an address and a page disagreeing is not
		something any test here can see.
	*/
	if (place.agenda) return "/";

	if (!place.workspace) return "/";

	const base = `/${encodeURIComponent(place.workspace)}`;

	return place.project ? `${base}/${encodeURIComponent(place.project)}` : base;
}

function segment (raw) {
	/*
		One path segment as a name, tolerating an escape a browser would not have written —
		`#681`.

		`decodeURIComponent` is all or nothing: one malformed escape throws `URIError` for the
		whole string. Since `#648` this app is the handler for **every** address nothing else
		claimed, so `/personal/100%` is its problem rather than the server's, and the throw
		reached the failure page — whose *Retry* re-ran the same parse — or, from `popstate`,
		nothing at all.

		**Falling back to the raw text is safe because of what it is compared against.** A
		workspace slug maps every non-alphanumeric character to `-` and a project key is
		`[a-z][a-z0-9]*(?:-[a-z0-9]+)*`, so neither can contain a percent sign. An undecodable
		segment therefore matches nothing, and the reader is told the address names nowhere,
		which is exactly what is true.

		**Per segment rather than per address**, because the good half has to keep working:
		`normalize_slug` keeps whatever `str.isalnum` accepts and that is Unicode-aware, so
		`Café` is a legal short name and arrives here as `caf%C3%A9`.
	*/
	try {
		return decodeURIComponent(raw);
	} catch (_) {
		return raw;
	}
}

export function parseAddress (pathname) {
	/*
		Read an address into the place it names, or null for one that names nowhere.

		Four shapes (`#638`, `#647`), and the ambiguity between the middle two resolves on one
		question — is the last segment a number?

		| | |
		| --- | --- |
		| `/{workspace}` | that workspace |
		| `/{workspace}/{project}` | that project within it |
		| `/{workspace}/{ref}` | one item |
		| `/{workspace}/{project}/{ref}` | one item, readable |

		**A ref is a positive integer and nothing else**, which is what keeps `#42` — how a
		person writes one in prose — out of a path, where a `#` is a fragment the server never
		sees. Everything before the ref is decoration: that is what makes a project renamed
		since somebody saved the link harmless rather than a dead end.
	*/
	const parts = String(pathname || "").split("/").filter((part) => part !== "");

	if (parts.length === 0) return null;

	const last = parts[parts.length - 1];
	const ref = Number(last);
	/* `[1-9][0-9]*`, which is `refs._TYPED` and `mentions.REF_PATTERN` — no leading zero.
	   `subroutine show 007` is refused at a terminal, so `/projects/007` is not an item
	   here either. A project key cannot begin with a digit (§5.4), so this segment is a
	   malformed ref rather than an ambiguous name, and reading it loosely would be the
	   browser disagreeing with every other surface about what a number means. */
	const names = /^[1-9][0-9]*$/.test(last) ? Number(last) : null;

	/* The project is the segment before the ref, or the last one when there is no ref. A
	   workspace on its own has neither. */
	const middle = names === null ? parts.slice(1) : parts.slice(1, -1);

	return {
		workspace: segment(parts[0]),
		project: middle.length > 0 ? segment(middle[middle.length - 1]) : null,
		/* **The whole project path, not only its last segment** (`#772`). `project` is what
		   narrows a listing and is deliberately the last one — a project key is unique in its
		   workspace, so the segments before it are decoration. `trail` is that decoration, kept
		   so that opening an item does not throw away the tree the reader navigated. */
		trail: middle.map(segment),
		ref: names,
	};
}

export function chosenWorkspace (asked, available, current) {
	/*
		Which workspace to show, given the address, the ones this reader has, and where they
		already were — `#650`.

		**A pure function on purpose.** The bug this replaces was not a wrong rule: the address
		was parsed correctly and the list rendered correctly, and nothing joined them. That
		wire lived inside `App`, which the render harness cannot execute (`#640`), so it was
		found by Simon opening `/personal` and seeing the wrong workspace. Lifting the decision
		out is what makes it checkable — the same move that makes `parseAddress` and
		`markdown.render` the best-covered code here.

		`refused` is named rather than silently dropped, because since `#648` an address nobody
		claimed is served the app: `/nonsense` reaches this function, and a reader who typed it
		deserves to be told rather than shown somebody else's backlog.
	*/
	const wanted = (asked && asked.workspace) || null;
	const known = wanted !== null && available.includes(wanted);
	const fallback = current || available[0] || null;

	return {
		slug: known ? wanted : fallback,
		refused: wanted !== null && !known ? wanted : null,
	};
}

export function mentionHref (workspace) {
	/*
		How a `#42` written in a description becomes a link.

		Durable rather than readable, because a mention is *stored prose* — the one place an
		address genuinely is permanent, and so the one place a renameable segment must not
		appear. It resolves within the workspace it was written in, which is what a ref means.
	*/
	return (ref) => `/${encodeURIComponent(workspace)}/${ref}`;
}

/* ---- shaping ------------------------------------------------------------ */

export function day (value) {
	/* A date in the reader's own locale, because this is the one surface where the machine
	   knows what that is. Time is dropped: everything shown here is a day-scale fact. */
	if (!value) return null;

	return new Date(value).toLocaleDateString(undefined, {
		day: "numeric",
		month: "short",
		year: "numeric",
	});
}

export function overdue (item) {
	if (!item.due_at || item.status_category === "done") return false;

	return new Date(item.due_at) < new Date();
}

/*
	**The two categories that mean a task is over**, which is `domain/tasks.FINISHED_CATEGORIES`
	said in JavaScript. Two copies of one rule is this codebase's signature defect, so it is worth
	saying why this one is not: the browser is handed `status_category` as data precisely so it
	may branch on it, and the alternative — asking the instance whether each row is finished — is
	a request per row to learn something already in the row.

	It is the *vocabulary* that is shared, not the rule, and the vocabulary is published: §6.4
	fixes these four categories beside a status key an installation may rename freely, and
	`COLUMNS` below already names all four for the board.
*/
const FINISHED = new Set(["done", "cancelled"]);

export function excluded (key, selection) {
	/*
		Whether a selection left this status category out — `#744`.

		**Three ways a column can be absent and they are one question**: *did this selection ask
		for this category?* Keying on any one of them is what shipped a board whose *Done* column
		reported *Not shown* while holding a hundred rows, and a *Cancelled* column that has
		always said *Nothing* on a board nobody asked for finished work on.

		| Selection | Excluded |
		| --- | --- |
		| `status_category=X` | every category but `X` |
		| no `include_completed` | `done` and `cancelled` |
		| `include_completed=true` | nothing |

		**Measured rather than read off the parameter's name**: a plain listing of this project on
		the served instance returns `{'todo': 143}` — no `done`, and **no `cancelled` either**, so
		the default excludes both finished categories rather than only the completed one.

		This is a model of what the instance did, so it can be wrong; `Board` therefore never
		lets it hide a row that actually arrived. Being wrong about an empty column costs a word,
		and being wrong about a full one costs the page.
	*/
	const chose = selection || {};

	if (chose.status_category !== undefined) return key !== chose.status_category;

	return chose.include_completed !== "true" && FINISHED.has(key);
}

export function completable (item) {
	/*
		Whether finishing this is something a reader could still do.

		**Two questions, and `Row` was asking only the first.** A document cannot be completed —
		it has no such axis — and neither can a task that is already over. `Row` checked the kind
		and shipped a **Complete** button on every card in the board's *Done* column (`#724`),
		where pressing it rewrites the record of when the work finished (`#723`).

		The rule is stated twice already, in `Row`'s own comment and in `Doing`'s: *a control that
		refuses when pressed is worse than one that is not there*. This is that sentence made into
		something both of them can call.

		**A missing category reads as unfinished**, which is the safe direction: it costs a button
		that turns out to do nothing, where the opposite would silently remove the only way to
		complete something.
	*/
	return item.kind === "task" && !FINISHED.has(item.status_category);
}

export function holding (item, now = null) {
	/*
		Who is holding this item, and whether the lease still means anything — `#726`.

		**A claim is not a status and this is the whole reason it gets its own mark.** Simon:
		*"the agent might be considering whether to start on the task, and decide not to — it
		might release the claim while never progressing it."* So *somebody is on this* and *work
		has begun* are two facts, and deriving either from the other would make the board assert
		something nobody said. `in_progress` stays a declaration.

		**Expired is a state worth showing, not one to hide.** `views.Task` reports an expired
		lease deliberately — *"who was working on this is worth knowing even once the lease has
		run out"* — and a claim that ran out unreleased is exactly *started and walked away
		from*, which is the thing a person watching agents work most wants to see and today
		cannot. So this reports it as `expired` rather than as nothing.

		**The same reading `domain.claims.held_by` applies**, deliberately: a row still carries
		the holder after expiry, and the comparison against the clock is what makes it stop
		meaning anything. Two copies of that comparison, which is justified for the reason the
		category set is — `claim_expires_at` is published so a client may answer this without a
		request per row — and is why a test asserts the two agree on the same row.

		`now` is an argument so this can be tested without waiting. `overdue` compares a server
		timestamp against the client's clock in the same way; over a lease measured in hours,
		ordinary skew cannot change the answer.
	*/
	if (!item.claimed_by_id || !item.claim_expires_at) return null;

	const moment = now === null ? Date.now() : now;
	const who = item.claimed_by || null;

	return Date.parse(item.claim_expires_at) <= moment
		? { held: false, who }
		: { held: true, who };
}

export function marks (item, showKind) {
	/*
		The small labels under a title.

		**Every one says a word.** `#102`: colour marks an exception and never carries the
		information by itself, so "overdue" is red *and* reads "overdue" — a reader who cannot
		separate the hues loses nothing at all.
	*/
	const found = [];

	if (showKind) found.push({ text: item.kind === "document" ? "Document" : "Task" });
	if (item.blocked) found.push({ text: "Blocked", tone: "blocked" });
	if (overdue(item)) found.push({ text: `Overdue ${day(item.due_at)}`, tone: "late" });

	/*
		**Who is holding it, before the project and the assignee** (`#726`), because it is the
		only mark here that is true *now* — the rest are properties of the item and this one
		expires. Simon: *"I cannot see what is being worked on."*

		**A name, or the fact without one.** `claimed_by` is the username, batch-loaded beside
		the assignee's; an instance that predates the field defaults it to null and this still
		says somebody holds the item, which is the half that matters.

		**And it says the word, not only a colour** — decision `#102`, which is why *left* is
		spelled out rather than being the same mark in a different hue.
	*/
	const lease = holding(item);

	if (lease) {
		found.push(lease.held
			? { text: lease.who ? `${lease.who} is on it` : "Claimed", tone: "claimed" }
			: { text: lease.who ? `${lease.who} left it` : "Claim expired", tone: "stale" });
	}

	if (item.project_key) found.push({ text: item.project_key });
	if (item.assignee) found.push({ text: item.assignee });
	if (item.status && !item.status_is_default) found.push({ text: item.status });

	return found;
}

export function moment (value, now = null) {
	/*
		An instant the program recorded, at the resolution somebody can read the order by —
		`#746`, Simon's, from driving `#729`.

		**A page sorted on a field must show enough of that field to check it.** The finished
		view is ordered on `completed_at` and rendered it as a day, so everything above the fold
		read *done 9 Aug 2026* and the ordering was invisible until you scrolled far enough to
		meet a different date. That is `#661`'s complaint arriving from the other side: not a
		listing failing to say how it is ordered, but one showing the ordering field too coarsely
		to read the order by. A reader who cannot check the claim takes it on trust, which is what
		`#706` chose `completed_at` over a proxy to avoid.

		**Today and yesterday are named rather than dated**, because *done yesterday 21:56* is
		read at a glance where *done 9 Aug 2026 21:56* is decoded against today's date first.

		**Compared as local midnights**, not as a span of hours: "yesterday" is a calendar
		question, and a clock change makes a day 23 or 25 hours long. Rounding the difference
		between two midnights is right across both.

		**`now` is injectable and the tests pass one.** A fixture holding a fixed instant against
		the wall clock is a test that passes in the morning and fails in the evening, which this
		repository shipped and pushed on 2026-08-09 (`#737`).
	*/
	if (!value) return null;

	const at = new Date(value);
	const today = now === null ? new Date() : new Date(now);
	const midnight = (date) =>
		new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();

	const clock = at.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
	const apart = Math.round((midnight(today) - midnight(at)) / 86400000);

	if (apart === 0) return `today ${clock}`;
	if (apart === 1) return `yesterday ${clock}`;

	return `${day(value)} ${clock}`;
}

export function when (item, now = null) {
	/*
		The one date worth a column. A deadline outranks a plan, and neither is invented.

		**Once something is finished, when it finished outranks both** (`#706`). A deadline on
		completed work is a fact about a date that stopped mattering — and `overdue` deliberately
		returns false for anything done, so before this a task finished a week late read `due
		3 Aug` with nothing to say it had been dealt with.

		It is also the field the *done* view is **ordered** on, which is `#661`'s complaint in
		miniature: *if the view does not show the values of the fields on which it is ordered, it
		is unclear*. A column of finish dates descending is a page a reader can check, where the
		same rows showing deadlines are an order they have to take on trust.

		`completed_at` rather than the category, because a row is only worth a date it actually
		has — a cancelled item carries one too, and *cancelled 3 Aug* is the honest thing to say
		about it rather than nothing.

		**Only the finished stamp carries a time** (`#746`). A deadline and a planned day stay
		days: both are dates *somebody chose*, and a time on one would be precision the writer
		never supplied. This is a stamp the program made, it is exact, and it is what the page is
		ordered on.
	*/
	if (item.completed_at) {
		return `${item.status_category === "cancelled" ? "cancelled" : "done"} `
			+ `${moment(item.completed_at, now)}`;
	}

	if (item.due_at && !overdue(item)) return `due ${day(item.due_at)}`;
	if (item.planned_for) return `→ ${day(item.planned_for)}`;

	return null;
}

/* ---- the board (`#653`) -------------------------------------------------- */

/*
	**The columns are what a task's status *category* can be**, not what this workspace calls
	its statuses. A key is per-workspace and renameable — `open`, `blocked` and `needs_input`
	are all `todo` here — so a board built on keys would show three columns that mean one thing
	and break on the first installation that renames one. `category` is the fixed field
	published beside the key precisely so a client may branch on it.
*/
const COLUMNS = [
	{ key: "todo", label: "To do" },
	{ key: "in_progress", label: "In progress" },
	{ key: "done", label: "Done" },
	{ key: "cancelled", label: "Cancelled" },
];

/*
	A document's categories are a different vocabulary for a different reason — a superseded
	specification is not "done" — so they get their own columns rather than being mapped onto
	the task ones. Mapping would be inventing a claim; `current` is not *in progress*.
*/
const DOCUMENT_COLUMNS = [
	{ key: "draft", label: "Draft" },
	{ key: "current", label: "Current" },
	{ key: "superseded", label: "Superseded" },
	{ key: "archived", label: "Archived" },
];

export function columns (items) {
	/*
		Arrange the rows a listing already fetched into the board's columns — `#653`.

		**Pure, and this is the fifth time that has been the point** (`#640`). The harness cannot
		touch `App`, so every decision left inside it is covered by nothing; four faults shipped
		from exactly that gap. `markdown.render`, `addressOf`, `parseAddress`, `chosenWorkspace`
		and `agendaBuckets` are the best-covered code here for the same reason.

		**A task column is shown even when empty; a document column is not.** They look like one
		rule and are two questions. The task categories *are* the structure — a board with no
		*In progress* reads as broken rather than as empty, and an empty column is where you
		drag something to. Four empty document columns on a page holding no documents are
		§12.2a's column that says the same thing on every row, four times over.

		**The order inside a column is the order the rows arrived in**, which is the listing's
		`-created_at`. Re-sorting by the reported `priority_score` would put a part-ranked item
		below an unranked one — §6.3a's exact defect, reintroduced one layer up, because the
		field a client reads is `importance * urgency` and the *ordering* of that name applies
		three bands. Ranking a board is `?order=` on the fetch, where the database applies the
		bands, and it is deliberately not done here.

		A category the server grows and this does not know about still gets a column, so a new
		one appears rather than taking its rows off the page.
	*/
	const held = new Map();

	for (const item of items) {
		const key = item.status_category || "";
		const bucket = held.get(key);

		if (bucket) bucket.push(item);
		else held.set(key, [item]);
	}

	const known = new Set([...COLUMNS, ...DOCUMENT_COLUMNS].map((column) => column.key));
	const extra = [...held.keys()]
		.filter((key) => key !== "" && !known.has(key))
		.map((key) => ({ key, label: key }));

	/*
		**A row with no category still gets somewhere to be** — caught by the test that asserts
		every fetched row appears exactly once, which is decision `#649`'s line: a view that
		dropped one would be the *query* deciding which rows exist, which §14.10 calls a scoping
		bug wearing a formatting hat.

		It should not happen — both views declare `status_category` as required and both field
		lists ask for it. But a field left out of `?fields=` arrives as null rather than erroring
		(the comment above `TASK_FIELDS` says so), and a client is the half that goes stale. The
		failure worth preventing is silent: a labelled column is a reader noticing something odd,
		a missing one is a task that has vanished.

		Last, and only when occupied — the same rule the document columns follow.
	*/
	const loose = [{ key: "", label: "Other" }];

	return [
		...COLUMNS.map((column) => ({ ...column, items: held.get(column.key) || [] })),
		...[...DOCUMENT_COLUMNS, ...extra, ...loose]
			.filter((column) => held.has(column.key))
			.map((column) => ({ ...column, items: held.get(column.key) })),
	];
}

/* ---- the agenda (`#652`) ------------------------------------------------- */

/*
	**The four buckets, in the order a day is read.** Deliberately the same words `subroutine
	today` prints, because §12.2 already decided what the agenda says and one product answering
	one question two ways is worse than either answer.

	`Next 7 days` rather than `Upcoming` for the same reason: the CLI says the horizon out loud
	and a reader should not have to learn that two surfaces mean the same span.
*/
const BUCKETS = [
	{ key: "overdue", label: "Overdue" },
	{ key: "today", label: "Today" },
	{ key: "upcoming", label: "Next 7 days" },
	{ key: "unscheduled", label: "Unscheduled" },
];

export function agendaBuckets (agenda, workspaces = []) {
	/*
		Turn an agenda response into the buckets a page renders, and nothing else.

		**A pure function on purpose** — `#640`, for the fourth time. The render harness calls
		components as plain functions and so cannot touch one that uses a hook, which means
		every decision left inside `App` is covered by nothing; four faults shipped from exactly
		that gap in two days. `markdown.render`, `addressOf`, `parseAddress` and
		`chosenWorkspace` are the best-covered code here for this reason, and this is the same
		move.

		**An empty bucket is dropped, and that is not the board's rule.** A day with nothing
		overdue should not show the word *Overdue* — the absence is the good news and printing
		a heading over nothing makes a reader look for what is missing. A *column* is different:
		a board with no `In progress` column reads as broken rather than as empty, because the
		columns are the structure. Same question, opposite answers, so it is worth saying which
		is which rather than reaching for consistency.

		**Each row is told which workspace it is from**, resolved here from `me.workspaces`,
		because the response carries `workspace_id` as a uuid and nothing readable. Whether a
		row *shows* it is the caller's decision and depends on the page.
	*/
	if (!agenda) return [];

	const named = new Map(workspaces.map((space) => [space.id, space.slug]));

	return BUCKETS
		.map(({ key, label }) => ({
			key,
			label,
			items: (agenda[key] || []).map((item) => ({
				...item,
				kind: "task",
				workspace: named.get(item.workspace_id) || null,
			})),
		}))
		.filter((bucket) => bucket.items.length > 0);
}

export function counted (buckets) {
	/* How many rows an agenda is showing, across its buckets. */
	return buckets.reduce((total, bucket) => total + bucket.items.length, 0);
}

export function spansWorkspaces (buckets) {
	/*
		Whether this agenda holds rows from more than one workspace.

		**The same rule as the kind column** (§12.2a): a mark that says the same thing on every
		row says nothing, and on a single-workspace instance every row would carry the one name
		there is. The CLI answers this per row instead — `World.address_of` prints `sandbox/#1`
		beside a bare `#589` — because it fans out across *connections* and a bare number beside
		an item somewhere else is an invitation to act on the wrong one. Here there is one
		instance, so the question is only ever about the page as a whole.
	*/
	const seen = new Set();

	for (const bucket of buckets) {
		for (const item of bucket.items) {
			if (item.workspace) seen.add(item.workspace);
		}
	}

	return seen.size > 1;
}

/* ---- following a link, or letting the browser do it (`#722`) ------------- */

export function opens (event) {
	/*
		Whether this app should handle a click itself, or stand back and let the browser have it.

		**Every navigation here is a real anchor**, so *open in a new tab*, *copy link address*,
		middle-click and the status bar all work — and a screen reader announces a link rather
		than a button, which is what makes "list the links on this page" return the items. What
		this function decides is the one case the app wants: a plain left click, which it handles
		without a page load because the app already has the rows.

		**Everything modified goes back to the browser**, and each modifier is a real gesture:
		ctrl or cmd opens a tab, shift opens a window, alt downloads. So is any button but the
		primary one — middle-click is *open in a tab* on every platform, and a context menu must
		reach the anchor rather than be intercepted before it.

		**An absent `button` counts as primary**, because a link activated from the keyboard
		reports no button in some browsers and refusing there would break the path this whole
		change exists to serve.

		`Prose` has held exactly this rule since `#637` and was the only thing that had it: a
		`#42` written inside a description opened in a new tab while the row for the same item
		did not. One rule applied to one side of a pair, which is this codebase's signature
		defect and is why the rule is a function now rather than a condition written twice.
	*/
	if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;

	return event.button === undefined || event.button === null || event.button === 0;
}

export function followed (event, act) {
	/*
		Handle a click the app wants, and leave every other kind alone.

		The pair is always written together — decide, then prevent, then act — and writing it
		once is what stops the third call site getting it in the wrong order. Preventing before
		deciding would swallow a middle click; acting before preventing would do both.
	*/
	if (!opens(event)) return;

	event.preventDefault();
	act();
}

/* ---- views -------------------------------------------------------------- */

export function Row ({ item, showKind, showWhere, workspace, onOpen, onComplete }) {
	const badges = marks(item, showKind);

	/*
		**The workspace goes in the ref cell, not in a badge** — the same answer `subroutine
		today` gives, where a row from elsewhere prints `sandbox/#1` and one from here prints
		`#589`. It belongs to the address rather than beside it: `#638` says an item has one
		durable address and it is `{workspace}/{ref}`, so showing the prefix is showing more of
		the address rather than adding a fact.
	*/
	const where = showWhere && item.workspace ? `${item.workspace}/` : "";

	/*
		**The row is a link, and the address is the item's own** (`#722`, `#638`).

		`item.workspace` first, because the agenda spans them and a row from `sandbox` addressed
		against the workspace the switcher happens to hold would send a reader somewhere else —
		the same precedence `App` already applies to opening and completing an agenda row.

		**No workspace means no address**, which happens only where `agendaBuckets` could not name
		one. An anchor with no `href` is not a link and cannot be tabbed to; that is worse than a
		button and it is why the control below falls back to one rather than rendering a hollow
		anchor. The click still works either way, so nothing is lost that was ever there.
	*/
	const slug = item.workspace || workspace;
	const address = slug ? addressOf(item, slug) : null;

	const open = (event) => followed(event, () => onOpen && onOpen(item));

	/*
		**The two controls are siblings, not one inside the other.** A button nested in a button
		is invalid, and a browser resolves it by dropping the inner one — so completing would
		open the item instead, silently, and only in some browsers.

		**Only where there is something left to finish** — `completable`, which asks about the
		status as well as the kind. This asked about the kind alone until `#724` and so put a
		**Complete** button on every card in the board's *Done* column, on work that was already
		over, where pressing it moves the record of when it finished (`#723`).
	*/
	const cells = html`
		<span class="ref">${where}#${item.ref}</span>
		<span class="title">${item.title}</span>
		<span class="when">${when(item)}</span>
		${badges.length > 0 && html`
			<span class="marks">
				${badges.map((mark) => html`
					<span class="mark ${mark.tone || ""}">${mark.text}</span>
				`)}
			</span>
		`}
	`;

	return html`
		<li>
			${address
				? html`<a class="row" href=${address} onClick=${open}>${cells}</a>`
				: html`<button class="row" onClick=${open}>${cells}</button>`}
			${completable(item) && onComplete && html`
				<button class="finish" onClick=${() => onComplete(item)}
					aria-label=${`Complete #${item.ref}, ${item.title}`}>Complete</button>
			`}
		</li>
	`;
}

export function Agenda ({ buckets, more, where, onAdd, onOpen, onComplete, busy, adding }) {
	/*
		What is due, in the order a day is read — `#652`, and `/` is where a browser opens.

		**Because bare `subroutine` already prints this** (§12.2). A person arriving at a
		terminal is shown their day rather than a help wall; a person arriving at the page was
		shown the newest hundred things in whichever workspace came first, which is a different
		question with a similar shape. One product, one answer.

		**A day with nothing in it says so once**, rather than four times under four headings.
		`agendaBuckets` drops the empty ones, so this only has to handle all of them being gone
		— which is the good day, and should read like one.
	*/
	const showWhere = spansWorkspaces(buckets);

	/*
		**The box has to be here, because `/` is now where a person lands.** Before `#652` the
		root was a listing and carried one; moving the agenda in without it would have made
		adding something require choosing a workspace first — §1.4's rule is that no entity may
		ever be *required* to create a task, and that is exactly what it would have become.

		**It files into the workspace the header shows**, which is the one honest answer on a
		page spanning several: the switcher is right above it and says which. Named rather than
		implied, so nobody has to guess where it went.
	*/
	const box = onAdd && html`
		<${Adding} onAdd=${onAdd} busy=${busy} ...${adding || {}}
			note=${where ? `Adds to ${where}.` : null} />
	`;

	if (buckets.length === 0) {
		return html`
			<div class="listing agenda">
				${box}
				<div class="empty">Nothing is due, and nothing is waiting. </div>
			</div>
		`;
	}

	return html`
		<div class="listing agenda">
			${box}

			${buckets.map((bucket) => html`
				<section class="bucket" key=${bucket.key}>
					<h2 class=${bucket.key}>${bucket.label}</h2>
					<ul class="rows">
						${bucket.items.map((item) => html`
							${/* `where` is the workspace the switcher holds, and it is the
							     fallback only — a row that knows its own uses that, which is
							     what keeps an agenda row's address pointing at the workspace
							     it actually came from. */ null}
							<${Row} key=${item.workspace + "/" + item.ref} item=${item}
								showKind=${false} showWhere=${showWhere} workspace=${where}
								onOpen=${onOpen} onComplete=${onComplete} />
						`)}
					</ul>
				</section>
			`)}

			${/*
				**The unscheduled bucket is capped by the endpoint and says so** — `unscheduled_total`
				is reported precisely because "an agenda that dumped a 400-item backlog would not be
				an agenda". Unlike the listing's `…and more` this is an exact count, because the
				server already did the counting; §8.4 declines a total for a *listing* and this is
				not one.
			*/ null}
			${more !== null && more !== undefined && more > 0 && html`
				<div class="cut">
					<span>${more} more unscheduled.</span>
				</div>
			`}
		</div>
	`;
}

export function Board ({
	items, onOpen, onComplete, onAdd, onMore, onWiden, busy, more, project, workspace,
	widenTo, selection, finishedTo, adding,
}) {
	/*
		The same rows the list shows, arranged by what state they are in — `#653`, `?view=board`.

		**Same rows, and that is the decision rather than an implementation detail** (`#649`):
		the path chose them and the query only says how they look. So this takes the array
		`Listing` takes and rearranges it — it fetches nothing, filters nothing, and cannot.

		**No dragging yet, deliberately.** `position` exists on the model and is written by
		nothing, so a card could be moved and would not stay where it was put. `#711` carries
		that and is blocked by `#28`; a board that reads well is worth having first, which is
		what `#445` §6 recommended and what unblocked this from a `!2/2` item.
	*/
	const arranged = columns(items);
	const showKind = new Set(items.map((item) => item.kind)).size > 1;

	/*
		**A column nothing was asked for must not report "Nothing"** (`#738`, and it is `#718`
		arriving through a second door) — **and a column holding rows must never say either**
		(`#744`, which is the first version of this getting it wrong).

		Finished work is a *selection*, so a board without it is a coherent thing to want and an
		empty *Done* column under it would be a false statement rather than an empty one. This
		project's own repeated lesson is that something which works and says something untrue
		about itself is worse than a failure (`#564`, `#568`, `#570`, `#572`), and a column is
		exactly where a reader looks to conclude nothing is left.

		**`column.items.length === 0` is the first term and that is the whole fix.** The version
		this replaces asked only whether the category had been requested, and won over the row
		branch — so `?view=board&status_category=done` rendered *Not shown* above a footer reading
		"Showing 100. There are more." Found by Simon on the first page he opened.

		`excluded` is a model of what the instance did and can be wrong. The rows are a fact.
		Where they disagree, the rows win.
	*/
	const unasked = (column) =>
		column.items.length === 0 && excluded(column.key, selection);

	/* The same test the listing makes, and it has to be the same: both render one page of two
	   collections, and a column tally that reads as a total is worse on a board than a short
	   list is, because a column is where somebody looks to see that nothing is left. */
	const truncated = more !== null && more !== undefined
		&& (more.tasks !== null || more.documents !== null);

	return html`
		<div class="listing board">
			${onAdd && html`<${Adding} onAdd=${onAdd} busy=${busy} ...${adding || {}} />`}

			${project && html`
				<div class="narrowed">
					<span>Showing <strong>${project}</strong> and anything under it.</span>
					${onWiden && (widenTo
						? html`<a class="widen" href=${widenTo}
							onClick=${(event) => followed(event, onWiden)}>Show everything</a>`
						: html`<button onClick=${onWiden}>Show everything</button>`)}
				</div>
			`}

			<div class="columns">
				${arranged.map((column) => html`
					<section class="column" key=${column.key}>
						${/*
							**A count of what is on the page, which is not a total** (`#718`).
							This comment used to say a board is not paged and that the number was
							exact. It is not: the board renders the rows `load` fetched and that
							fetch is capped at `PAGE`, which was measured biting on this
							project's own board the day it shipped. The notice below is what
							makes the number honest, and it is the listing's, for the reason
							`#646` gives — a reader shown 100 of 142 with no way to tell had an
							item they wrote minutes earlier become unfindable.
						*/ null}
						${/* **And no tally on a column nothing was asked for** — a `0` beside
						     *Done* is the same false statement as the word *Nothing* under it,
						     in the place a reader glances rather than reads. */ null}
						<h2>${column.label}${!unasked(column) && html`${" "}
							<span class="tally">${column.items.length}</span>`}</h2>

						${unasked(column)
							? html`<p class="empty">Not shown.${" "}
								${finishedTo && FINISHED.has(column.key)
									? html`<a href=${finishedTo}>Show finished work</a>`
									: null}</p>`
							: column.items.length === 0
							? html`<p class="empty">Nothing</p>`
							: html`
								<ul class="rows">
									${column.items.map((item) => html`
										<${Row} key=${item.kind + item.ref} item=${item}
											showKind=${showKind} workspace=${workspace}
											onOpen=${onOpen} onComplete=${onComplete} />
									`)}
								</ul>
							`}
					</section>
				`)}
			</div>

			${truncated && html`
				<div class="cut">
					<span>Showing ${items.length}. There are more.</span>
					${onMore && html`
						<button onClick=${onMore} disabled=${busy}>Show more</button>
					`}
				</div>
			`}
		</div>
	`;
}

/*
	The two axes of §6.3, **with which way they run said out loud** (`#770`).

	*5 = most important/urgent. Higher is more* — Appendix A settled it as ambiguity A1, because
	every `gte` filter depends on the answer. A bare 1–5 says none of that, and a reader who
	guesses the other way files their whole backlog upside down and is never told.

	**Ascending, so the number stays the thing.** `!4/3` is how a priority is written at a
	terminal, in a captured line, in `Facts` and in every listing; a control that put 5 at the
	top would be the one place the scale reads backwards, and the words already fix the
	direction without moving anything.
*/
export const PRIORITIES = [
	{ value: 1, label: "1 — Very low" },
	{ value: 2, label: "2 — Low" },
	{ value: 3, label: "3 — Medium" },
	{ value: 4, label: "4 — High" },
	{ value: 5, label: "5 — Very high" },
];

/*
	**Three date fields, kept apart on purpose, in the words `subroutine explain dates` uses.**

	`#769`: this said *Starts*, which is the one reading `start_at` explicitly is not. Appendix
	A's ambiguity A4 asked whether it means *work starts then*, *hide until then* or *earliest
	permitted start*, and settled it as a **defer** — the task is not actionable before it and
	views hide it by default (§6.5).

	So the browser was a second copy of a vocabulary that disagreed with the first, on the one
	surface with no `explain` to check against: a terminal reader can ask and a browser reader
	has only the label. `cli/topics.py` is the original and a test holds these against it.

	**Left to right is chronological** — hidden until, then planned for, then due — and it only
	read as arbitrary while the first one claimed to be a start.
*/
export const DATE_FIELDS = [
	["start", "Hidden until", "A defer. The task does not appear at all before this."],
	["planned_for", "Planned for", "The day you intend to do it. This is what 'today' shows."],
	["due", "Due", "A deadline. The date something has to be finished by."],
];

export function Adding ({
	onAdd, busy, note, expanded, onExpand, vocabulary, projects, members, project,
}) {
	/*
		**One box, and the capture grammar behind it** (§6.13). `+project`, `!4/3`, `#tag`,
		`~2h` and a date in words all work here exactly as they do at a terminal, which is why
		the placeholder shows one rather than describing the syntax: this is the only place a
		browser-only reader can learn that any of it exists.

		Plain prose is a complete answer, and that is the point — nothing here is required.

		**The form is a disclosure and the box is untouched** (`#756`, §1.4). *No entity from §14
		or §15 may ever be required to create, find or complete a task*, so the rest of the fields
		open **below** the same box rather than replacing it, and the box stays the only required
		control. One `<form>`, one submission: the line becomes `text` and the controls become
		explicit fields, which `POST /v1/tasks` takes together with explicit winning per field.

		**No state of its own, deliberately.** `expanded` is a prop rather than a `useState` here
		because a component that calls a hook cannot be rendered by this project's harness
		(`#640`) — four faults shipped out of decisions left inside `App`, every one found by
		Simon rather than by the build. The inputs are uncontrolled for the same reason and for
		the one the original box had: a form cleared on submit needs nothing remembered between
		keystrokes, and `required` hands the empty case to the browser, which says so in the
		reader's own language rather than in ours.
	*/
	const submit = (event) => {
		event.preventDefault();

		const form = event.currentTarget;

		if (form.elements.text.value.trim() === "" || busy) return;

		/* **Read off the form rather than tracked** — every named control, whatever the reader
		   touched, handed to `filed` which decides what is worth sending. */
		const values = {};

		Array.from(form.elements).forEach((one) => {
			if (one.name) values[one.name] = one.value;
		});

		onAdd(values);
		form.reset();
	};

	const types = offered(vocabulary && vocabulary.item_types, "task");
	const statuses = offered(vocabulary && vocabulary.statuses, "task");

	/*
		**Every date is a day, and the all-day flag follows from that** rather than being a
		control of its own. Measured: `due: "2026-08-14"` is stored as the end of that day with
		`due_is_all_day: true`, and `due: "2026-08-14T15:00"` is stored at 15:00 and not all-day
		— one field, both meanings, decided by what arrives. `datetime-local` would make every
		deadline carry a time somebody had to invent; a time of day is what the line is for.
	*/
	const day = ([name, label, hint]) => html`
		<label key=${name}><span>${label}</span>
			<input type="date" name=${name} disabled=${busy} />
			<small>${hint}</small></label>
	`;

	const rank = (name, label) => html`
		<label><span>${label}</span>
			<select name=${name} disabled=${busy}>
				<option value="">—</option>
				${PRIORITIES.map((one) => html`
					<option key=${one.value} value=${one.value}>${one.label}</option>
				`)}
			</select></label>
	`;

	const vocabularySelect = (name, label, options) => html`
		<label><span>${label}</span>
			<select name=${name} disabled=${busy || options.length === 0}>
				${options.map((one) => html`
					<option key=${one.key} value=${one.key} selected=${one.chosen}>
						${one.label}</option>
				`)}
			</select></label>
	`;

	return html`
		<form class="adding" onSubmit=${submit}>
			<div class="line">
				<input name="text" required disabled=${busy} aria-label="Add an item"
					placeholder="Add something — try: call the dentist tomorrow +work !4/3" />
				<button type="submit" disabled=${busy}>Add</button>
				${onExpand && html`
					<button type="button" class="more" aria-expanded=${expanded ? "true" : "false"}
						onClick=${() => onExpand(!expanded)}>${expanded ? "Less" : "More"}</button>
				`}
			</div>

			${expanded && html`
				<fieldset class="details">
					<legend>Everything else</legend>

					<label class="wide"><span>Description</span>
						<textarea name="description" rows="3" disabled=${busy}></textarea></label>

					${/* **The Inbox is named rather than left blank**, because it is where an item
					     with no project goes — a blank option here would be a control whose
					     effect the reader has to already know. Which entry is chosen is
					     `filableFor`, which is pure: *the project defaults from the address* is
					     a closing condition of `#756`, and a claim nothing could check while it
					     was an expression buried in this markup. */ null}
					${vocabularySelect("project", "Project", filableFor(projects, project))}

					${vocabularySelect("type", "Type", types)}
					${vocabularySelect("status", "Status", statuses)}

					<label><span>Assignee</span>
						<select name="assignee" disabled=${busy}>
							<option value="">Nobody</option>
							${(members || []).map((one) => html`
								<option key=${one.username} value=${one.username}
									>${one.label}</option>
							`)}
						</select></label>

					${rank("importance", "Importance")}
					${rank("urgency", "Urgency")}

					<label><span>Estimate</span>
						<input name="estimate" disabled=${busy} placeholder="2h, 90m, 1w2d" /></label>

					${DATE_FIELDS.map(day)}

					<label class="wide"><span>Tags</span>
						<input name="tags" disabled=${busy} placeholder="health, admin" /></label>
				</fieldset>
			`}

			${/* **Only where it is not obvious.** A listing is one workspace and saying so on
			     every page would be the column that says the same thing on every row (§12.2a);
			     the agenda spans them, so there the answer is worth a line (`#652`). */ null}
			${note && html`<span class="lands">${note}</span>`}
		</form>
	`;
}

export function Listing ({
	items, onOpen, onComplete, onAdd, onMore, onWiden, busy, more, project, workspace, widenTo,
	empty = "Nothing here yet.", adding,
}) {
	/*
		**A column that says the same thing on every row says nothing** (§12.2a). The kind is
		shown only on a mixed page: a blank beside "Document" would read as missing data rather
		than as "ordinary", and the word "Task" on every line of a list of tasks is noise.
	*/
	const showKind = new Set(items.map((item) => item.kind)).size > 1;

	/*
		**A listing that had to stop says so.** It said nothing until `#646`, and a reader was
		shown 100 of 142 with no way to tell — which is how an item they had written minutes
		earlier became unfindable. The count is of what is *shown* rather than of what exists,
		because §8.4 declines to compute a total and a wrong number would be worse than none.
	*/
	const truncated = more !== null && more !== undefined
		&& (more.tasks !== null || more.documents !== null);

	return html`
		<div class="listing">
			${onAdd && html`<${Adding} onAdd=${onAdd} busy=${busy} ...${adding || {}} />`}

			${project && html`
				<div class="narrowed">
					<span>Showing <strong>${project}</strong> and anything under it.</span>
					${onWiden && (widenTo
						? html`<a class="widen" href=${widenTo}
							onClick=${(event) => followed(event, onWiden)}>Show everything</a>`
						: html`<button onClick=${onWiden}>Show everything</button>`)}
				</div>
			`}

			${/*
				**An empty page has to say which question it answered** (`#706`). *Nothing here
				yet* under the finished view reads as an empty workspace, when what it means is
				that nothing has been finished — the opposite conclusion for somebody checking
				whether an agent has been working. The caller knows which view it asked for and
				this does not, so the sentence comes from there.
			*/ null}
			${items.length === 0
				? html`<div class="empty">${empty}</div>`
				: html`
					<ul class="rows">
						${items.map((item) => html`
							<${Row} key=${item.kind + item.ref} item=${item} showKind=${showKind}
								workspace=${workspace} onOpen=${onOpen}
								onComplete=${onComplete} />
						`)}
					</ul>
				`}

			${truncated && html`
				<div class="cut">
					<span>Showing ${items.length}. There are more.</span>
					${onMore && html`
						<button onClick=${onMore} disabled=${busy}>Show more</button>
					`}
				</div>
			`}
		</div>
	`;
}

export function Note ({ note, onUndo, onDismiss }) {
	/*
		What just happened, and what to do if it was not wanted.

		**It says the outcome in words** (`#102`): the tone is a colour and carries nothing by
		itself, so a reader who cannot separate the hues loses none of it. `alert` for a
		failure and `status` for a success, because a screen reader should interrupt for one
		and not the other.
	*/
	if (!note) return null;

	return html`
		<div class=${`note ${note.tone}`} role=${note.tone === "bad" ? "alert" : "status"}>
			<span class="said">${note.text}</span>
			${note.undo && html`<button class="undo" onClick=${onUndo}>Undo</button>`}
			<button class="dismiss" onClick=${onDismiss} aria-label="Dismiss this message">×</button>
		</div>
	`;
}

export function Facts ({ item }) {
	/*
		**A field nobody set is not printed** (§12.2c). That rule is what lets `subroutine show`
		answer "buy milk" with a number, a title and nothing else, and it is the same rule here:
		a screen of empty labels tells a reader this system wants things from them.
	*/
	const rows = [];
	const add = (label, value) => value && rows.push([label, value]);

	add("Status", item.status);
	add("Project", item.project_key);
	add("Type", item.type);
	add("Assignee", item.assignee);
	add(
		"Priority",
		item.importance && item.urgency ? `!${item.importance}/${item.urgency}` : null,
	);
	/* **`start_at` was settable before it was showable**, which `#756` made worse rather than
	   introduced: the form can set it, and a field a reader can write and never read back is
	   `#515`'s shape — every step reports success and they are left confirming the wrong
	   conclusion. The CLI has printed it as *from <date>* since M1. */
	add("Starts", day(item.start_at));
	add("Due", day(item.due_at));
	add("Planned", day(item.planned_for));
	add("Estimate", item.estimate_human);
	add("Tags", item.tags && item.tags.length > 0 ? item.tags.join(", ") : null);
	add("Parent", item.parent_ref ? `#${item.parent_ref} ${item.parent_title || ""}` : null);
	add("Updated", day(item.updated_at));

	if (rows.length === 0) return null;

	return html`
		<dl class="facts">
			${rows.map(([label, value]) => html`
				<dt>${label}</dt><dd>${value}</dd>
			`)}
		</dl>
	`;
}

export function Prose ({ text, className, where, onOpen }) {
	/*
		**The whole of this app's trust boundary, and the only `dangerouslySetInnerHTML` in
		it.** Everything a reader sees as formatted text arrives here, and the safety of that
		is entirely `markdown.render`'s: it escapes every run of text it emits and constructs
		every tag itself, so nothing written into a description can become markup.

		A second call site would be a second thing to be sure about. If formatted text is
		needed somewhere else, use this component rather than the property it wraps.

		`where` turns a `#42` in the prose into a link to that item, and its absence leaves one
		as text — which is what it was before `#638` and what it stays wherever the workspace is
		not known.
	*/

	/*
		Mentions are caught here rather than followed.

		The anchor is a real one, so it copies, opens in a new tab and works with JavaScript
		off — the whole reason `#638` puts an address on every item. Following it in *this* tab
		would reload the page for a link to something the app already knows how to show, which
		on a description with forty mentions in it is forty page loads. One listener on the
		container rather than one per link, because the HTML is set as a string and there is
		nothing to attach a handler to.
	*/
	const caught = (event) => {
		if (!onOpen) return;

		const anchor = event.target.closest && event.target.closest("a.mention");

		if (!anchor) return;

		const asked = parseAddress(new URL(anchor.href, window.location.origin).pathname);

		if (asked === null) return;

		/* **`opens` was written from the copy that used to be here** (`#722`), which was the only
		   place in the app that had the rule. It is shared now, so a row and a mention behave the
		   same way — which is what a reader expects and what they did not get. */
		followed(event, () => onOpen({ ref: asked.ref }));
	};

	/*
		**Rendering stored text cannot take the page with it** (`#680`).

		This is the only place arbitrary input becomes output, and the input is written by
		anybody with a credential — including on *somebody else's* item, since a comment renders
		through here too. `#679` closed the one way it was known to fail; this closes the ones
		nobody has found, and unlike the boundary above it depends on no framework behaviour, so
		the harness can prove it.

		The text is shown escaped rather than dropped, because a description a reader cannot see
		at all is worse than one that lost its formatting.
	*/
	let rendered;

	try {
		rendered = markdown.render(text, where);
	} catch (failure) {
		const note = unrenderable(failure, "This text");

		/*
			**The fallback may not repeat the step that failed, and the first version did.**
			It showed the text by way of `String(text)` — which is the first thing `render`
			does, so a value that threw *there* threw again here and the catch achieved
			nothing. Caught by the test, which is the whole reason the payload is a value that
			fails on being stringified rather than one that nests too deeply.
		*/
		let written = "";

		try {
			written = String(text);
		} catch (_) {
			written = "";
		}

		rendered = `<p class="broke"><strong>${markdown.escaped(note.said)}</strong> `
			+ `${markdown.escaped(note.detail)}</p>`
			+ (written === "" ? "" : `<pre><code>${markdown.escaped(written)}</code></pre>`);
	}

	return html`<div class=${className} onClick=${caught}
		dangerouslySetInnerHTML=${{ __html: rendered }}></div>`;
}

export function Doing ({ item, members, onComplete, onAssign, busy }) {
	/*
		The two things a reader can do to an item from here.

		**Only for a task, and only while it is open.** A document has neither, and a completed
		task offering "Complete" is a control whose only outcome is a refusal.

		**This wrote the rule out by hand and got it three-quarters right** (`#724`): it asked
		whether the category was `done`, so a **cancelled** task — equally over — was offered both
		controls. The set has two members and the copy here knew one of them. It is `completable`
		now, which `Row` also calls, so there is one place to be wrong rather than three.

		Assignment lists the workspace's members and nothing else, because `tasks.assignee_for`
		is workspace-scoped on purpose: handing work to somebody who cannot see it is not a
		fair act. "Nobody" sends null, which the API takes as *clear this* rather than as *no
		opinion* — driven and confirmed rather than assumed, since the two readings of a null
		are indistinguishable from the outside.
	*/
	if (!completable(item)) return null;

	return html`
		<div class="doing">
			<button class="finish" disabled=${busy} onClick=${() => onComplete(item)}
				aria-label=${`Complete #${item.ref}, ${item.title}`}>Complete</button>

			${members.length > 0 && html`
				<label class="assign">
					<span>Assigned to</span>
					<select disabled=${busy}
						onChange=${(event) => onAssign(item, event.target.value || null)}>
						<option value="" selected=${!item.assignee}>Nobody</option>
						${/* **The same answer the add form gets** (`#770`): four of this workspace's
					     five members are agents, and a control that says so on one surface and
					     not on the other is two vocabularies for one roster. */ null}
						${members.map((who) => html`
							<option value=${who.username} selected=${who.username === item.assignee}
								>${who.label}</option>
						`)}
					</select>
				</label>
			`}
		</div>
	`;
}

export function Detail ({
	item, links, comments, members = [], onOpen, onBack, onComplete, onAssign, busy, where,
	backTo, workspace,
}) {
	const body = item.description || item.body;

	/*
		**Both of these are addresses, so both are links** (`#722`). *All items* goes to the
		listing behind this one, and each linked item to that item — the two things a reader on
		this page would most want to open in a tab, and the two that were buttons.

		A link's address comes from `addressOf` with only a ref, which is the durable form
		`#638` guarantees: the other end of a link is reported as a ref and a type, with no
		project key, so the readable form is not available here and the durable one is exactly
		right.
	*/
	const back = (event) => followed(event, () => onBack && onBack());

	return html`
		<div class="detail">
			${backTo
				? html`<a class="back" href=${backTo} onClick=${back}>← All items</a>`
				: html`<button class="back" onClick=${back}>← All items</button>`}
			<h2>#${item.ref} ${item.title}</h2>
			<${Facts} item=${item} />

			${onComplete && html`
				<${Doing} item=${item} members=${members} onComplete=${onComplete}
					onAssign=${onAssign} busy=${busy} />
			`}

			${body && html`<${Prose} className="prose" text=${body} where=${where}
				onOpen=${onOpen} />`}

			${links.length > 0 && html`
				<h3>Links</h3>
				<ul class="linked">
					${links.map((link) => {
						const going = { ref: link.other.ref, kind: link.other.entity_type };
						const to = workspace ? addressOf(going, workspace) : null;
						const follow = (event) =>
							followed(event, () => onOpen && onOpen(going));

						return html`
							<li key=${link.id}>
								${link.label}${" "}
								${to
									? html`<a href=${to} onClick=${follow}>
										#${link.other.ref} ${link.other.title}</a>`
									: html`<button onClick=${follow}>
										#${link.other.ref} ${link.other.title}</button>`}
							</li>
						`;
					})}
				</ul>
			`}

			${comments.length > 0 && html`
				<h3>What happened</h3>
				<ul class="comments">
					${comments.map((note) => html`
						<li key=${note.id}>
							<div class="said">${day(note.created_at)}</div>
							<${Prose} className="body" text=${note.body} where=${where}
								onOpen=${onOpen} />
						</li>
					`)}
				</ul>
			`}
		</div>
	`;
}

export function Failed ({ error, onRetry }) {
	/*
		A 401 here is the ordinary case rather than a fault: nothing on this page can hand out
		a session, because a sign-in link is minted at a terminal until `#599` mails one. So it
		says what to ask for rather than offering a form that cannot work.
	*/
	if (error.status === 401) {
		return html`
			<div class="failed">
				<p>You are not signed in.</p>
				<p class="why">Whoever runs this instance can send you a link that signs you
				in — they make one with <code>subroutine login link</code>. It works once and
				lasts half an hour.</p>
			</div>
		`;
	}

	return html`
		<div class="failed">
			<p>That did not work.</p>
			<p class="why">${error.message}</p>
			<button class="back" onClick=${onRetry}>Try again</button>
		</div>
	`;
}

/* ---- the app ------------------------------------------------------------ */

export function App () {
	const [me, setMe] = useState(null);
	const [workspace, setWorkspace] = useState(null);
	const [items, setItems] = useState([]);
	const [open, setOpen] = useState(null);
	const [error, setError] = useState(null);
	const [ready, setReady] = useState(false);
	const [members, setMembers] = useState([]);
	const [note, setNote] = useState(null);
	const [busy, setBusy] = useState(false);
	const [more, setMore] = useState(null);
	/* The project the address narrows to, or null for the whole workspace (`#647`). Held
	   beside the workspace rather than derived on each render, because the poll and every
	   write reload the list and all of them have to narrow the same way. */
	const [project, setProject] = useState(null);
	/* The agenda, or null when the address names a workspace and the list is what is showing
	   (`#652`). Null rather than a separate `showing` flag, because "there is an agenda to
	   render" and "the agenda is what to render" are the same fact and two would drift. */
	const [agenda, setAgenda] = useState(null);
	const [unscheduled, setUnscheduled] = useState(0);
	/*
		The add form: whether it is open, and the two answers it needs to draw its dropdowns
		(`#756`).

		**Fetched once the page is ready rather than when the form opens.** Two requests and
		about 8 KB per workspace, against a poll that spends one every ten seconds for the life
		of the page — so the cost is nothing and what it buys is the absence of a loading state
		inside a form. Every fault this app has shipped came from something not having landed
		yet in the render that read it; a disclosure that fetches on open is one more of those,
		and this one would show a reader an empty type dropdown rather than a blank page.

		**Keyed by nothing, cleared on a workspace change.** The vocabulary is per workspace, so
		a cache carrying `personal`'s statuses into `projects` would be a control that looks
		complete and offers the wrong words.
	*/
	const [expanded, setExpanded] = useState(false);
	const [vocabulary, setVocabulary] = useState(null);
	const [filable, setFilable] = useState([]);
	/*
		What is on screen — the arrangement and the selection — read from the address rather
		than remembered (`#651`), so a reader can send somebody the thing they are looking at.

		**One state holding both, not two** (`#738`). `setView` not having landed in the render
		that reads it is `#719`'s defect, and two setters would give it two chances to happen
		with the halves disagreeing. They are one fact: what this page is showing.
	*/
	const [showing, setShowing] = useState({ view: DEFAULT_VIEW, selection: {} });
	const since = useRef(null);

	/*
		**The same fact again, where a callback can read it** — and the ref is the copy that is
		never stale, which is why `since` is one too.

		`load` used to take the selection from `window.location.search`, and `#719`'s reasoning
		for that was sound while it held: the address is written by `go` before any load that
		changes one, so it cannot lag the way `showing` lags the render that calls `load`.

		**What `#766` took away is the premise.** An item's address carries no query, so while
		one is open the bar says nothing about the listing behind it — and the poll goes on
		calling `load` every ten seconds. Reading the address there would quietly refetch the
		default selection under a reader who had chosen another, and they would not see it until
		they closed the item and found the columns they asked for empty.

		So the address is a valid source only while it *is* a listing address, and this is the
		source that is valid always.
	*/
	const shown = useRef(showing);

	const nowShowing = useCallback((next) => {
		/* **One writer for both copies**, so the two cannot disagree — the whole hazard of
		   keeping a second one. Every place that changes what is showing calls this and none
		   calls `setShowing`, which `tests/test_web.py` holds. */
		shown.current = next;
		setShowing(next);
	}, []);

	const go = useCallback((path, { replace = false, arranged = showing } = {}) => {
		/*
			**Every address this app writes goes through here**, carrying the arrangement and the
			selection.

			Four places wrote one before `#651` and all four dropped the query, so
			`/projects?view=board` became `/projects` the moment anything was opened — the view
			would have been a setting that silently expired on the first click.
		*/
		const wanted = withShowing(path, arranged);

		if (window.location.pathname + window.location.search === wanted) return;

		window.history[replace ? "replaceState" : "pushState"]({}, "", wanted);
	}, [showing]);

	const readAgenda = useCallback(async (spaces) => {
		/* What to ask for and how to group it are both pure and checked (`agendaRequest`,
		   `agendaBuckets`). What is left here is holding the answer. */
		const answered = await sent(agendaRequest());

		setAgenda(agendaBuckets(answered, spaces));
		setUnscheduled(
			Math.max(0, (answered.unscheduled_total || 0) - (answered.unscheduled || []).length),
		);
	}, []);

	const load = useCallback(async (slug, key = null, after = null) => {
		if (!slug) return;

		/*
			**The selection is read from the ref, not from state and not from an argument** —
			`#719`, and the parameter this replaces is the defect.

			It was `arranged = view`, which reads the *state*, and `setView` does not land in the
			render that calls this. So the first load after arriving at `/projects?view=board`
			asked for the list's rows; ten seconds later the poll — recreated once `view` had
			landed — asked for the board's. Right rows, one `POLL_MS` late, which is exactly how
			Simon described it.

			**The comment in `start` three lines from the call site says this precise thing about
			`slug`**, and I quoted its reasoning elsewhere in the same commit without applying it
			here. Passing the value at both sites would have worked and left the trap for the
			next call site, so the parameter is gone instead.

			It read `window.location.search` until `#766`, on the grounds that the address is the
			one source that is never stale. `nowShowing` is that source now, and for the reason
			written there: an item's address carries no query, so the bar stops answering this
			question the moment one is open — and the poll keeps asking it.
		*/
		const chose = shown.current.selection;

		/* What to ask for is `listingRequests`, which is pure and checked (`#640`). What is
		   left here is what to do with the answers. */
		const wanted = listingRequests(slug, key, after, chose);
		let answers;

		try {
			answers = await Promise.all(wanted.map(sent));
		} catch (failure) {
			/*
				**A project named in an address may not be there any more, and that is the case
				this whole design exists for.** `sr` became `subroutine` on 2026-08-08 across
				502 items; a link somebody saved before that names a project the instance will
				now refuse with a 404. Letting it through would replace the page with a failure
				— for an address that still identifies its item perfectly well.

				So the filter is dropped and the workspace is read instead, with the reason said
				out loud. Only for the filter: a 404 with no project asked for is a different
				fact and belongs to the caller.
			*/
			if (failure.status !== 404 || !key) throw failure;

			setNote({ text: `There is no project called ${key} here any more. `
				+ `Showing the whole workspace.`, tone: "bad" });
			setProject(null);

			return load(slug, null, after);
		}

		const fetched = answers.flatMap((answer, at) =>
			answer.items.map((row) => ({ ...row, kind: wanted[at].kind })));

		/*
			**What the list becomes is `accumulated`, which is pure and driven** (`#660`, `#706`).

			The cost of re-merging is that *Show more* can insert rows above where a reader is
			looking. That is inherent to two streams paged separately and is the right trade: a row
			in the wrong place is a list you cannot trust, and a row appearing above the fold is
			one you can.
		*/
		setItems((existing) => accumulated(existing, fetched, {
			appending: Boolean(after), collections: wanted.length,
		}));

		/*
			**What was left behind, so the listing can say so.** The envelope has carried
			`has_more` since M1 and this app read it nowhere — so it showed 100 of 142 and
			looked complete, which is how an item somebody had just written became unfindable
			rather than merely mis-sorted. A count is deliberately not asked for: §8.4 declines
			`include_total` because it costs a second full scan, and "there is more" is the part
			a reader acts on.

			**A collection this view did not ask for has nothing left behind**, so it reports
			null rather than being absent — `Listing` and `Board` both read both keys, and an
			undefined would make *there are more* depend on which view was showing.
		*/
		const left = { tasks: null, documents: null };

		answers.forEach((answer, at) => {
			left[`${wanted[at].kind}s`] =
				answer.page.has_more ? answer.page.next_cursor : null;
		});

		setMore(left);
	}, []);

	const words = useCallback(async (slug) => {
		/*
			What this workspace calls things, and where an item can be filed — the two answers
			the add form's dropdowns are built from (`#756`).

			**Called where `roster` is called rather than from an effect**, because it answers
			the same question at the same moment: the workspace has changed, so everything
			workspace-shaped has to be asked again. An effect would be a fourth thing watching
			`workspace` and a fourth chance for it to run against a value that has not landed.

			**Cleared first, so a failure cannot leave the previous workspace's words on screen.**
			A type dropdown offering another workspace's types is worse than one offering none:
			the second is visibly unfinished and the first is confidently wrong.

			Its failure is survivable, like the roster's: the capture line does not need any of
			this, and §1.4 says it must not.
		*/
		if (!slug) return;

		setVocabulary(null);
		setFilable([]);

		try {
			const [meta, projects] = await Promise.all([
				sent(vocabularyRequest(slug)),
				sent(projectsRequest(slug)),
			]);

			setVocabulary(meta);
			setFilable(projects.items);
		} catch (_) {
			/* Left empty and disabled, which is what the form renders when it has nothing to
			   offer. Nothing else on the page depends on it. */
		}
	}, []);

	const roster = useCallback(async (slug) => {
		/*
			Who work can be handed to. Its own request because it changes on a different clock
			from the backlog — and its failure is survivable: without it the assignment control
			is simply absent, which is honest, where a picker that cannot be filled is not.
		*/
		if (!slug) return;

		try {
			const found = await sent(rosterRequest(slug));

			setMembers(people(found.items));
		} catch (_) {
			setMembers([]);
		}
	}, []);

	useEffect(() => {
		/*
			**`project` is a dependency because the interval closes over it, not because it is
			read during the effect.** Without it the page widened itself ten seconds after a
			reader opened a project: `start` calls `setWorkspace` *before* awaiting the head of
			the feed and `setProject` after, so the two land in different commits. This effect
			re-ran on the workspace one — while the filter was still `null` — and the interval
			it created kept that `null` for the life of the page. The first poll to see an event
			then called `load(workspace, null)` and replaced a correct seven-item list with the
			whole workspace, at an address that still said the project.

			That is the shape worth remembering: nothing here is stale on the render, only in
			the callback the render leaves behind. The list widened, the address did not, and
			the reader is left with somebody else's backlog under their own project's URL.

			The cost is that navigating between projects restarts the ten-second window. That is
			the right trade — a poll that is late by one tick is invisible, and one that reloads
			the wrong list is what this is fixing.
		*/
		if (error || !workspace) return undefined;

		const tick = setInterval(async () => {
			try {
				/* **The agenda spans workspaces, so its poll must too** (`#652`) — and it has
				   to reload the thing on screen rather than the list underneath it. `agenda` is
				   in the dependency array below for the reason `project` is: the interval
				   closes over it, and an interval created while the list was showing would go
				   on reloading the list for the life of the page. */
				const onAgenda = agenda !== null;
				const seen = await sent(pollRequest(onAgenda ? null : workspace, since.current));

				if (seen.items.length === 0) return;

				since.current = seen.items[seen.items.length - 1].seq;

				await (onAgenda ? readAgenda(me ? me.workspaces : []) : load(workspace, project));
			} catch (_) {
				/* A poll that fails changes nothing on screen. The next one may work, and
				   replacing a readable page with an error because a background request
				   timed out is worse than being ten seconds stale. */
			}
		}, POLL_MS);

		return () => clearInterval(tick);
	}, [error, workspace, project, agenda, me, load, readAgenda]);

	const fetched = useCallback(async (ref, kind, slug) => {
		/*
			Read one item, working out what it is when nobody said.

			A ref names a task *or* a document — one counter per workspace serves both (§6.2) —
			so an address carries no kind and there is nothing to infer it from. Ask about a
			task, and read the 404 as "then it is a document" rather than as a failure. Only a
			refusal from the *second* is a refusal.
		*/
		const order = kind ? [kind] : ["task", "document"];

		for (const trying of order) {
			try {
				const [item, links, comments] = await Promise.all(
					itemRequests(trying, ref, slug).map(sent),
				);

				return { item: { ...item, kind: trying }, links: links.items,
					comments: comments.items };
			} catch (failure) {
				if (failure.status !== 404 || trying === order[order.length - 1]) throw failure;
			}
		}

		return null;
	}, []);

	const show = useCallback(async (row, { history = true, slug = workspace } = {}) => {
		try {
			const found = await fetched(row.ref, row.kind, slug);

			setOpen(found);

			/*
				**The address is written from what came back, not from what was clicked.** So a
				link somebody was sent with a retired project name in it corrects itself the
				moment the item is read — `replaceState` rather than `pushState` for that, since
				the stale spelling should not become a step in the reader's own history.
			*/
			/* **Under the path the reader is on** (`#772`), which `parseAddress` reads out of
			   the bar rather than out of state — `go` has not written anything yet, so this is
			   still the address they clicked from. */
			go(addressOf(found.item, slug, parseAddress(window.location.pathname)),
				{ replace: !history });

			window.scrollTo(0, 0);
		} catch (failure) {
			/*
				**A ref that is not there is a note, not the end of the page.**

				Prose mentions whatever somebody wrote, and `#999` is linked without asking
				whether it exists — checking would be a request per mention, on descriptions
				that carry forty. So the dead ones arrive here, and a reader who followed one
				should be told that and left with their list. Found by driving: the first
				version replaced the whole app with an error page for a typo in a description.
			*/
			if (failure.status === 404) {
				setNote({ text: `There is no #${row.ref} in ${slug}.`, tone: "bad" });
				setOpen(null);

				return;
			}

			setError(failure);
		}
	}, [fetched, go, workspace]);

	const close = useCallback(({ history = true } = {}) => {
		setOpen(null);

		/*
			**Back to what is actually behind it**, which used to be a hard-wired `/` — harmless
			while `/` was the list and wrong the moment `#652` made it the agenda, because the
			address then said the agenda while the page went on showing a workspace listing.
			Nothing failed: an address disagreeing with its page is not something any test here
			can see, and it was found by reading this while wiring `#651`'s view through it.
		*/
		if (history) go(listingAddress({ agenda: agenda !== null, workspace, project }));
	}, [agenda, go, project, workspace]);

	const start = useCallback(async () => {
		setError(null);

		try {
			const identity = await sent(identityRequest());
			const asked = parseAddress(window.location.pathname);
			const arrangement = showingOf(window.location.search);

			nowShowing({ view: arrangement.view, selection: arrangement.selection });
			const { slug, refused } = chosenWorkspace(
				asked, identity.workspaces.map((space) => space.slug), workspace,
			);

			setMe(identity);
			setWorkspace(slug);

			/* **Every refused word, named** — `viewOf`'s rule applied to the selection too
			   (`#738`). One note rather than one per word: a reader who mistyped two things
			   needs to know both, and `setNote` holds one. */
			if (arrangement.refused.length > 0) {
				setNote({
					text: `This address asks for ${arrangement.refused.join(" and ")}, `
						+ `which is not something to ask for. Showing the list.`,
					tone: "bad",
				});
			}

			if (refused !== null) {
				setNote({
					text: `There is no workspace called ${refused} that you can see.`
						+ (slug ? ` Showing ${slug}.` : ""),
					tone: "bad",
				});
			}

			/* The head of the feed, so the first poll asks what happens *next* rather than
			   replaying everything that ever has. **Null rather than zero when there is no
			   head** — an instance nobody has used yet has no events, and `since=0` is refused
			   (`#656`). */
			const head = await sent(headRequest());
			since.current = head.items.length > 0 ? head.items[0].seq : null;

			/*
				**The item an address names is opened here, beside the list rather than after
				it** (`#645`). Loading the list first and opening the item from an effect meant
				a deep link showed a page of somebody else's work, briefly, before showing the
				thing that was asked for — and waited for it.

				`slug` is passed rather than read from `workspace`: `setWorkspace` has not
				landed in this render, so the closure still holds the previous value, and a read
				scoped to it would be scoped to the wrong workspace or to nothing.
			*/
			setProject(asked && asked.project);

			/*
				**`/` is the agenda, and every other address is a listing** — decision `#649`,
				built by `#652`. The test is the address rather than a flag: an address naming
				no workspace is somebody who has not asked for one, and what they want is their
				day, which is what bare `subroutine` already gives them at a terminal (§12.2).

				The workspace is still resolved and the roster still read, because the switcher
				and every write need one — the agenda spans them all, but *adding* something
				has to land somewhere.
			*/
			await Promise.all([
				asked === null ? readAgenda(identity.workspaces) : load(slug, asked.project),
				roster(slug),
				words(slug),
				asked && asked.ref !== null
					? show({ ref: asked.ref }, { history: false, slug })
					: null,
			]);
		} catch (failure) {
			setError(failure);
		} finally {
			setReady(true);
		}
	}, [load, nowShowing, readAgenda, roster, show, words, workspace]);

	useEffect(() => {
		start();
	}, []);

	/*
		Every address a reader reaches with the back button.

		**The one they arrived at is `start`'s** (`#645`), so this does not read the address on
		mount — doing both fetched the same item twice. Back out of an item and an address with
		no ref is the list, which is why one handler covers both directions.

		**It sits below `show` because a dependency array is evaluated where it is written.**
		Declared above it, `show` is in its temporal dead zone and the whole app throws
		`Cannot access 'show' before initialization` — a blank page, which is exactly where it
		shipped from (`#643`). The order of the `const`s was checked and this was not, because
		it is not one of them.
	*/
	useEffect(() => {
		if (!ready || error || !workspace) return undefined;

		const arrive = () => {
			const asked = parseAddress(window.location.pathname);
			const narrowed = (asked && asked.project) ?? null;

			/* The arrangement is in the address too (`#651`), so stepping back into a board
			   restores the board rather than leaving the list under an address saying otherwise
			   — which is the disagreement `close` used to create for the agenda. */
			const back = showingOf(window.location.search);

			nowShowing({ view: back.view, selection: back.selection });

			/*
				**Stepping back to `/` is stepping back to the agenda** (`#652`), and this has
				to make the same decision `start` does or one address would mean two things
				depending on how the reader got there. `#645`'s split — the arrival address is
				`start`'s, every later one is this — is exactly what makes that a real risk.
			*/
			if (asked === null) {
				setProject(null);
				readAgenda(me ? me.workspaces : []);
			} else if (agenda !== null || narrowed !== project) {
				/* Leaving the agenda for a listing, or moving between listings. The filter is
				   part of the address too (`#647`), so stepping back out of a project restores
				   the whole workspace rather than leaving the list narrowed to something the
				   address no longer says. */
				setAgenda(null);
				setProject(narrowed);
				load(workspace, narrowed);
			}

			if (asked === null || asked.ref === null) {
				setOpen(null);
				return;
			}

			show({ ref: asked.ref }, { history: false });
		};

		window.addEventListener("popstate", arrive);

		return () => window.removeEventListener("popstate", arrive);
	}, [ready, error, workspace, project, agenda, me, load, nowShowing, readAgenda, show]);

	const reread = useCallback(async (row) => {
		/* Put the open item back the way `show` found it, so a detail on screen is not left
		   describing the state before the action. */
		if (open && open.item.ref === row.ref && open.item.kind === row.kind) await show(row);

		/* **Refresh what is showing** (`#652`). Completing from the agenda used to reload the
		   listing underneath it, so the row stayed on screen until the next poll — a write that
		   reports success and visibly does nothing. */
		await (agenda !== null
			? readAgenda(me ? me.workspaces : [])
			: load(workspace, project));
	}, [agenda, load, me, open, project, readAgenda, show, workspace]);

	const wrote = useCallback(async (row, said, run) => {
		/*
			**One path for every write, and the whole of why it exists is the failure case.**

			A refusal here is ordinary rather than exceptional — a member without the
			permission, a name that is not in this workspace, an item somebody else has just
			changed — so it becomes a message beside the work rather than replacing the page.
			Blanking a screen somebody is reading, because a button did not take, loses their
			place to report a problem they can do nothing about.

			`setError` is kept for the other kind: not signed in, or the instance unreachable,
			where there is nothing on the page worth keeping.
		*/
		setBusy(true);

		try {
			const answer = await run();

			setNote(said(answer));
			await reread(row);

			return answer;
		} catch (failure) {
			setNote({ text: `#${row.ref} was not changed. ${failure.message}`, tone: "bad" });

			return null;
		} finally {
			setBusy(false);
		}
	}, [reread]);

	/* **`where` defaults to the switcher's workspace and the agenda overrides it** — a row
	   there can be from anywhere, and completing it against the wrong workspace is a 404 for an
	   item the reader is looking at. Remembered on the undo for the same reason. */
	const complete = useCallback((row, where = workspace) => wrote(
		row,
		() => ({
			text: `Completed #${row.ref} ${row.title}.`,
			tone: "good",
			/* What it was, so undo restores rather than guesses. `restoreRequest` is what
			   carries it back; this is where it is remembered. */
			undo: { ref: row.ref, kind: row.kind, title: row.title, status: row.status, where },
		}),
		() => sent(completeRequest(row, where)),
	), [workspace, wrote]);

	const undo = useCallback(async () => {
		const going = note && note.undo;

		if (!going) return;

		setNote(null);
		await wrote(
			going,
			() => ({ text: `#${going.ref} is back to ${going.status}.`, tone: "good" }),
			/* Put it back where it came from. `complete` recorded that, because by now the
			   switcher may hold a different workspace entirely. */
			() => sent(restoreRequest(going, going.where || workspace)),
		);
	}, [note, workspace, wrote]);

	const assign = useCallback((row, who) => wrote(
		row,
		() => ({
			text: who ? `#${row.ref} is ${who}'s.` : `#${row.ref} is nobody's now.`,
			tone: "good",
		}),
		() => sent(assignRequest(row, who, workspace)),
	), [workspace, wrote]);

	const add = useCallback(async (values) => {
		/* **The reload afterwards keeps the filter the reader is looking at.** Without
		   `project` declared below, adding an item inside a project answered by replacing the
		   list with the whole workspace — the same stale closure as the poll, reached by a
		   button instead of a timer, and read as "adding a task loses my project".

		   `values` is every named control on the form, raw; `filed` decides what is worth
		   sending and is pure, which is where `#756`'s only real rule lives — an untouched
		   control gives an empty string, and this endpoint refuses those by name. */
		setBusy(true);

		try {
			const made = await sent(addRequest(values, workspace));

			setNote({ text: `Added #${made.ref} ${made.title}.`, tone: "good" });

			/* **Refresh what is on screen** (`#652`). Reloading the listing from the agenda
			   would report success over a page that does not change — and a new task with no
			   date belongs in *Unscheduled*, which is exactly where a reader would look for it
			   and not find it. */
			await (agenda !== null
				? readAgenda(me ? me.workspaces : [])
				: load(workspace, project));
		} catch (failure) {
			setNote({ text: `That was not added. ${failure.message}`, tone: "bad" });
		} finally {
			setBusy(false);
		}
	}, [agenda, load, me, project, readAgenda, workspace]);

	const showMore = useCallback(async () => {
		/* The next page of each collection that has one, appended. `load` takes the cursors
		   rather than the page number, because keyset pagination is what the API offers and
		   what makes a page boundary stable while somebody is adding things.

		   **`project` is declared even though `more` already changes on every load**, which
		   in practice rebuilt this callback often enough to hide the omission. Correctness
		   that depends on a *different* value happening to change is not correctness. */
		setBusy(true);

		try {
			await load(workspace, project, more);
		} catch (failure) {
			setNote({ text: `There was more, but it did not arrive. ${failure.message}`,
				tone: "bad" });
		} finally {
			setBusy(false);
		}
	}, [load, more, project, workspace]);

	const widen = useCallback(async () => {
		/*
			**Out of a project, back to the workspace.** A narrowed list that cannot say what
			narrowed it, or undo it, is an empty backlog with an explanation nobody can reach —
			and the filter arrived in the address rather than from a control the reader touched,
			so there is nothing for them to un-touch.
		*/
		setProject(null);
		go(`/${encodeURIComponent(workspace)}`);

		try {
			await load(workspace, null);
		} catch (failure) {
			/* A note, not the failure page: there is a readable list on screen and losing it
			   because a re-fetch did not land would cost the reader their place. The guard in
			   `tests/test_web.py` counts the places that blank the page, and it caught this
			   one being written the other way. */
			setNote({ text: `The rest did not load. ${failure.message}`, tone: "bad" });
		}
	}, [go, load, workspace]);

	const chooseWorkspace = useCallback(async (slug) => {
		/* A workspace is the whole of it: a project from the one you were in does not exist
		   here, and carrying it over would narrow to nothing and look like an empty backlog. */
		setWorkspace(slug);
		setProject(null);
		setNote(null);
		setOpen(null);
		/* **Choosing a workspace is leaving the agenda**, because the address it pushes names
		   one and `/` is the only address the agenda has (`#649`). Set here rather than left to
		   the effect: no `popstate` fires for a `pushState` we made ourselves. */
		setAgenda(null);
		go(`/${encodeURIComponent(slug)}`);

		try {
			await load(slug, null);
			await Promise.all([roster(slug), words(slug)]);
		} catch (failure) {
			setError(failure);
		}
	}, [go, load, roster, words]);

	const chooseView = useCallback(async (wanted) => {
		/*
			**Switching refetches, and the comment here used to say it must not.**

			That sentence was written the same day and was wrong within hours: it argued that a
			view is a rendering of rows `load` already has, so refetching would make the query
			decide which rows exist. The consequence, which Simon found by opening the page, is
			that the *Done* column was structurally incapable of holding anything — a listing
			excludes finished work by default, so the board never received a single done row
			(`#718`).

			**What refetches is the selection, and only when it changes** (`#738`). An
			arrangement genuinely is a rendering of rows already held — that half of the original
			sentence was right — so switching list to board with the same selection reloads
			nothing it did not need. The two are separate parameters now, which is what makes
			that distinction expressible at all.

			`wanted` is passed to `load` explicitly because `setShowing` has not landed in this
			render, so the closure still holds the previous one — the same reason `start` passes
			`slug` rather than reading `workspace`.
		*/
		const again = reloads(showing, wanted);

		nowShowing(wanted);

		/* **The address first, then the reload** — and the order is the whole of why `load`
		   needs no argument for this: it reads the arrangement from the address, which `go` has
		   already written. */
		go(
			listingAddress({ agenda: agenda !== null, workspace, project }),
			{ arranged: wanted },
		);

		if (agenda === null && again) await load(workspace, project);
	}, [agenda, go, load, nowShowing, project, showing, workspace]);

	if (!ready) return html`<div class="app"><div class="empty">Reading…</div></div>`;

	/* The address of the listing behind whatever is showing — what *All items* goes back to, and
	   what the view switcher hangs its arrangements off. One expression, because `close` and
	   `chooseView` already agree on it and a second spelling here would be the thing that drifts. */
	const behind = listingAddress({ agenda: agenda !== null, workspace, project });

	/*
		**The one question the render asks of the selection**, named once.

		Everything below that used to ask `view === "done"` is really asking this: *is this page
		showing only work that is over?* Answering it from the arrangement is what made `done` a
		view in the first place, and it is what `#738` took out.
	*/
	const finishedOnly = showing.selection.status_category === "done";

	/*
		Everything the add form needs beyond what every caller of `Adding` already passes — one
		prop rather than six threaded through `Agenda`, `Board` and `Listing`, none of which has
		any business knowing what a dropdown is made of.

		**`project` is the address's**, which `#738` already settled: `/{workspace}/{project}`
		says where rows come from, so it says where a new one goes. Nothing new is parsed, and on
		the agenda — which spans workspaces and narrows to nothing — it is null and the Inbox is
		what the form offers.
	*/
	const adding = {
		expanded, onExpand: setExpanded, vocabulary, projects: filable, members, project,
	};

	if (error) {
		return html`
			<div class="app">
				<${Failed} error=${error} onRetry=${start} />
			</div>
		`;
	}

	return html`
		<div class="app">
			<header class="top">
				<h1>Subroutine</h1>
				<div class="who">
					${me && html`<strong>${me.user.username}</strong>`}
					${me && me.workspaces.length > 1 && html`
						${" · "}
						<select onChange=${(event) => chooseWorkspace(event.target.value)}>
							${me.workspaces.map((space) => html`
								<option value=${space.slug} selected=${space.slug === workspace}>
									${space.slug}
								</option>
							`)}
						</select>
					`}
					${me && me.workspaces.length === 1 && html` · ${workspace}`}
				</div>

				${/*
					**Controls, because an address is not a way to find something** (`#651`). A
					reader who has never seen one cannot type a word they have not been told. They
					are on a listing only: the agenda is chosen by the path, and arranging it by
					status would answer a question nobody asked of it.

					**What each one writes, and which is highlighted, is `chips`** — a pure
					function, which is `#640`'s cheapest route and the reason this arc's four
					shipped faults were all in wiring rather than in rules. Two of the three are
					arrangements and one is a selection (`#738`); they look alike because the
					taxonomy belongs in the address, not in the furniture.

					**And each is a real link** (`#722`), so a reader can open the board in a tab
					beside their list rather than replacing it. `chips` builds exactly the address
					`chooseView` is about to write, which is what makes the two agree.
				*/ null}
				${!open && agenda === null && html`
					<nav class="views" aria-label="Which view">
						${chips(behind, showing).map((chip) => html`
							<a key=${chip.name} class=${chip.chosen ? "chosen" : ""}
								href=${chip.href}
								aria-current=${chip.chosen ? "true" : undefined}
								onClick=${(event) => followed(event, () => chooseView(chip.showing))}
								>${chip.name}</a>
						`)}
					</nav>
				`}
			</header>

			<${Note} note=${note} onUndo=${undo} onDismiss=${() => setNote(null)} />

			${open
				? html`<${Detail} ...${open} members=${members} onOpen=${show} busy=${busy}
					where=${mentionHref(workspace)} onBack=${() => close()}
					backTo=${withShowing(behind, showing)} workspace=${workspace}
					onComplete=${complete} onAssign=${assign} />`
				: agenda !== null
					? html`<${Agenda} buckets=${agenda} more=${unscheduled}
						onAdd=${add} busy=${busy} where=${workspace} adding=${adding}
						${/* **Each row is opened in its own workspace, not in the one the
						     switcher holds.** The agenda spans them; `show` defaults its slug
						     to `workspace`, so a row from `sandbox` would be looked up in
						     `projects` and reported missing. `#640`'s exact shape — the rule
						     right, the display right, and no wire between them — which is why
						     `agendaBuckets` resolves the slug onto every row. */ null}
						onOpen=${(row) => show(row, { slug: row.workspace || workspace })}
						onComplete=${(row) => complete(row, row.workspace || workspace)} />`
					: showing.view === "board"
						? html`<${Board} items=${items} onOpen=${show} onComplete=${complete}
							onAdd=${finishedOnly ? null : add} busy=${busy} more=${more} adding=${adding}
							onMore=${showMore}
							project=${project} workspace=${workspace} onWiden=${widen}
							selection=${showing.selection}
							${/* **Offered only where one parameter is the whole remedy**: a board
							     narrowed by `status_category` has every other column absent for a
							     reason no single link undoes, and a link per column claiming to
							     would be four ways to leave one state. */ null}
							finishedTo=${showing.selection.status_category === undefined
								? withShowing(behind, { view: "board", selection: EVERYTHING })
								: null}
							widenTo=${withShowing(listingAddress({ workspace }), showing)} />`
						/*
							**No capture box while only finished work is showing** (`#706`).
							Adding from here would report success over a page the new item cannot
							appear on — it is open, and this selection holds only what is over —
							which is `#515`'s shape: every step reports success and the reader is
							left confirming the wrong conclusion. `Row` already declines to offer
							*Complete* on finished work by way of `completable` (`#724`), so
							`onComplete` is passed and simply never applies; the add box has no
							such guard and is withheld here.
						*/
						: html`<${Listing} items=${items} onOpen=${show} onComplete=${complete}
							onAdd=${finishedOnly ? null : add} busy=${busy} more=${more} adding=${adding}
							onMore=${showMore} project=${project} workspace=${workspace}
							onWiden=${widen}
							widenTo=${withShowing(listingAddress({ workspace }), showing)}
							empty=${finishedOnly
								? "Nothing has been finished here yet."
								: "Nothing here yet."} />`}

			<footer class="foot">
				${/* **Counts what is on screen, not what was last fetched.** `items` is the
				     listing's state and is empty while the agenda is showing, so leaving this
				     alone would have put "0 items" under a full day (`#652`). */ null}
				<span>${agenda !== null ? counted(agenda) : items.length} items</span>
				<a href="/v1/docs/agent">API</a>
				<a href="https://github.com/simonholliday/subroutine">Source</a>
			</footer>
		</div>
	`;
}

/*
	**Guarded, so the module can be imported without a document.** That is not a concession to
	tests for their own sake: an htm template is a tagged template literal, so a malformed one
	parses perfectly and fails when it is *rendered* — which on this project's own record is
	the shape that ships and turns into a blank page. `tests/test_web.py` imports this module
	and renders every component above; it can only do that if importing it does not immediately
	reach for an element.
*/
if (typeof document !== "undefined") {
	/* **Wrapping `App` rather than sitting inside it**, so that a failure in `App` itself is
	   caught — which is where both of this arc's blank pages came from (`#680`). A boundary
	   inside the thing that throws catches nothing. */
	render(html`<${Boundary} what="The page"><${App} /></${Boundary}>`, document.getElementById("app"));
}
