/*
	The browser app — item `#597`. Read-only: see what there is, and read one in full.

	**No build step.** Preact and htm are served as written from `/app/`, and an import map in
	`index.html` resolves the one bare specifier between them. What that buys is not
	convenience: it is that the source a reader is served is the source in the repository,
	which is what the AGPL's network-use clause is about (§2.2), and there is no npm closure
	for `scripts/check_licences.py` to be structurally unable to see.

	**It talks only to the public API** (`#351`). No private endpoints, nothing a token could
	not do — so anything this page can show, a script can too, and the UI cannot quietly become
	the only way to do something.
*/

import { h, render } from "preact";
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
].join(",");

/* A document has no dates and no assignee — `_when` returns nothing for one — so it asks for
   less. Not the same list with the extras arriving null, because that is the difference
   between "has no deadline" and "cannot have one", and only one of them is true. */
const DOCUMENT_FIELDS = ["ref", "title", "project_key", "status", "status_is_default"].join(",");

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

	return { path: scoped(asking, slug), method: "GET" };
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

export function listingRequests (slug, key = null, after = null) {
	/*
		The list, which is tasks *and* documents.

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

	return [
		{ path: scoped(`/tasks?limit=${PAGE}&fields=${TASK_FIELDS}${narrowed}`
			+ from(after && after.tasks), slug), method: "GET" },
		{ path: scoped(`/documents?limit=${PAGE}&fields=${DOCUMENT_FIELDS}${narrowed}`
			+ from(after && after.documents), slug), method: "GET" },
	];
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

export function addRequest (text, slug) {
	/*
		`text` rather than a title, so the capture grammar runs (§6.13) and one box can set a
		project, a priority, tags and a date. The workspace goes in the body because that is
		where this endpoint takes it — the only write here that does.
	*/
	return { path: "/tasks", method: "POST", body: { text, workspace_id: slug } };
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

export function addressOf (item, workspace) {
	/* The address to put in the bar for one item: readable when we know enough, durable
	   always. */
	const durable = `/${encodeURIComponent(workspace)}/${item.ref}`;

	if (!item.project_key) return durable;

	return `/${encodeURIComponent(workspace)}/${encodeURIComponent(item.project_key)}`
		+ `/${item.ref}`;
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
		workspace: decodeURIComponent(parts[0]),
		project: middle.length > 0 ? decodeURIComponent(middle[middle.length - 1]) : null,
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
	if (item.project_key) found.push({ text: item.project_key });
	if (item.assignee) found.push({ text: item.assignee });
	if (item.status && !item.status_is_default) found.push({ text: item.status });

	return found;
}

export function when (item) {
	/* The one date worth a column. A deadline outranks a plan, and neither is invented. */
	if (item.due_at && !overdue(item)) return `due ${day(item.due_at)}`;
	if (item.planned_for) return `→ ${day(item.planned_for)}`;

	return null;
}

/* ---- views -------------------------------------------------------------- */

export function Row ({ item, showKind, onOpen, onComplete }) {
	const badges = marks(item, showKind);

	/*
		**The two controls are siblings, not one inside the other.** A button nested in a button
		is invalid, and a browser resolves it by dropping the inner one — so completing would
		open the item instead, silently, and only in some browsers.

		Only a task has one. A document cannot be completed, and a control that refuses when
		pressed is worse than one that is not there.
	*/
	return html`
		<li>
			<button class="row" onClick=${() => onOpen(item)}>
				<span class="ref">#${item.ref}</span>
				<span class="title">${item.title}</span>
				<span class="when">${when(item)}</span>
				${badges.length > 0 && html`
					<span class="marks">
						${badges.map((mark) => html`
							<span class="mark ${mark.tone || ""}">${mark.text}</span>
						`)}
					</span>
				`}
			</button>
			${item.kind === "task" && onComplete && html`
				<button class="finish" onClick=${() => onComplete(item)}
					aria-label=${`Complete #${item.ref}, ${item.title}`}>Complete</button>
			`}
		</li>
	`;
}

export function Adding ({ onAdd, busy }) {
	/*
		**One box, and the capture grammar behind it** (§6.13). `+project`, `!4/3`, `#tag`,
		`~2h` and a date in words all work here exactly as they do at a terminal, which is why
		the placeholder shows one rather than describing the syntax: this is the only place a
		browser-only reader can learn that any of it exists.

		Plain prose is a complete answer, and that is the point — nothing here is required.
	*/
	const submit = (event) => {
		event.preventDefault();

		const form = event.currentTarget;
		const written = form.elements.text.value.trim();

		if (written === "" || busy) return;

		onAdd(written);
		form.reset();
	};

	/*
		**An uncontrolled input, holding no state of its own.** A box that is cleared on submit
		needs nothing remembered between keystrokes, so mirroring every one into a state
		variable would be work with no reader — and `required` hands the empty case to the
		browser, which says so in the reader's own language rather than in ours.
	*/
	return html`
		<form class="adding" onSubmit=${submit}>
			<input name="text" required disabled=${busy} aria-label="Add an item"
				placeholder="Add something — try: call the dentist tomorrow +work !4/3" />
			<button type="submit" disabled=${busy}>Add</button>
		</form>
	`;
}

export function Listing ({
	items, onOpen, onComplete, onAdd, onMore, onWiden, busy, more, project,
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
			${onAdd && html`<${Adding} onAdd=${onAdd} busy=${busy} />`}

			${project && html`
				<div class="narrowed">
					<span>Showing <strong>${project}</strong> and anything under it.</span>
					${onWiden && html`<button onClick=${onWiden}>Show everything</button>`}
				</div>
			`}

			${items.length === 0
				? html`<div class="empty">Nothing here yet.</div>`
				: html`
					<ul class="rows">
						${items.map((item) => html`
							<${Row} key=${item.kind + item.ref} item=${item} showKind=${showKind}
								onOpen=${onOpen} onComplete=${onComplete} />
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

		if (!anchor || event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) {
			return;
		}

		const asked = parseAddress(new URL(anchor.href, window.location.origin).pathname);

		if (asked === null) return;

		event.preventDefault();
		onOpen({ ref: asked.ref });
	};

	return html`<div class=${className} onClick=${caught}
		dangerouslySetInnerHTML=${{ __html: markdown.render(text, where) }}></div>`;
}

export function Doing ({ item, members, onComplete, onAssign, busy }) {
	/*
		The two things a reader can do to an item from here.

		**Only for a task, and only while it is open.** A document has neither, and a completed
		task offering "Complete" is a control whose only outcome is a refusal.

		Assignment lists the workspace's members and nothing else, because `tasks.assignee_for`
		is workspace-scoped on purpose: handing work to somebody who cannot see it is not a
		fair act. "Nobody" sends null, which the API takes as *clear this* rather than as *no
		opinion* — driven and confirmed rather than assumed, since the two readings of a null
		are indistinguishable from the outside.
	*/
	if (item.kind === "document" || item.status_category === "done") return null;

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
						${members.map((who) => html`
							<option value=${who} selected=${who === item.assignee}>${who}</option>
						`)}
					</select>
				</label>
			`}
		</div>
	`;
}

export function Detail ({
	item, links, comments, members = [], onOpen, onBack, onComplete, onAssign, busy, where,
}) {
	const body = item.description || item.body;

	return html`
		<div class="detail">
			<button class="back" onClick=${onBack}>← All items</button>
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
					${links.map((link) => html`
						<li key=${link.id}>
							${link.label}${" "}
							<button onClick=${() => onOpen({ ref: link.other.ref,
								kind: link.other.entity_type })}>
								#${link.other.ref} ${link.other.title}
							</button>
						</li>
					`)}
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
	const since = useRef(null);

	const load = useCallback(async (slug, key = null, after = null) => {
		if (!slug) return;

		/* What to ask for is `listingRequests`, which is pure and checked (`#640`). What is
		   left here is what to do with the answers. */
		let tasks;
		let documents;

		try {
			[tasks, documents] = await Promise.all(listingRequests(slug, key, after).map(sent));
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

		const fetched = [
			...tasks.items.map((row) => ({ ...row, kind: "task" })),
			...documents.items.map((row) => ({ ...row, kind: "document" })),
		];

		setItems((held) => (after ? [...held, ...fetched] : fetched));

		/*
			**What was left behind, so the listing can say so.** The envelope has carried
			`has_more` since M1 and this app read it nowhere — so it showed 100 of 142 and
			looked complete, which is how an item somebody had just written became unfindable
			rather than merely mis-sorted. A count is deliberately not asked for: §8.4 declines
			`include_total` because it costs a second full scan, and "there is more" is the part
			a reader acts on.
		*/
		setMore({
			tasks: tasks.page.has_more ? tasks.page.next_cursor : null,
			documents: documents.page.has_more ? documents.page.next_cursor : null,
		});
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

			setMembers(found.items.map((row) => row.user.username));
		} catch (_) {
			setMembers([]);
		}
	}, []);

	useEffect(() => {
		if (error || !workspace) return undefined;

		const tick = setInterval(async () => {
			try {
				const seen = await sent(pollRequest(workspace, since.current));

				if (seen.items.length === 0) return;

				since.current = seen.items[seen.items.length - 1].seq;
				await load(workspace, project);
			} catch (_) {
				/* A poll that fails changes nothing on screen. The next one may work, and
				   replacing a readable page with an error because a background request
				   timed out is worse than being ten seconds stale. */
			}
		}, POLL_MS);

		return () => clearInterval(tick);
	}, [error, workspace, load]);

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
			const address = addressOf(found.item, slug);

			if (window.location.pathname !== address) {
				window.history[history ? "pushState" : "replaceState"](
					{ ref: found.item.ref }, "", address,
				);
			}

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
	}, [fetched, workspace]);

	const close = useCallback(({ history = true } = {}) => {
		setOpen(null);

		if (history && window.location.pathname !== "/") {
			window.history.pushState({}, "", "/");
		}
	}, []);

	const start = useCallback(async () => {
		setError(null);

		try {
			const identity = await sent(identityRequest());
			const asked = parseAddress(window.location.pathname);
			const { slug, refused } = chosenWorkspace(
				asked, identity.workspaces.map((space) => space.slug), workspace,
			);

			setMe(identity);
			setWorkspace(slug);

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

			await Promise.all([
				load(slug, asked && asked.project),
				roster(slug),
				asked && asked.ref !== null
					? show({ ref: asked.ref }, { history: false, slug })
					: null,
			]);
		} catch (failure) {
			setError(failure);
		} finally {
			setReady(true);
		}
	}, [load, roster, show, workspace]);

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

			/* The filter is part of the address too (`#647`), so stepping back out of a
			   project restores the whole workspace rather than leaving the list narrowed to
			   something the address no longer says. */
			if (narrowed !== project) {
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
	}, [ready, error, workspace, project, load, show]);

	const reread = useCallback(async (row) => {
		/* Put the open item back the way `show` found it, so a detail on screen is not left
		   describing the state before the action. */
		if (open && open.item.ref === row.ref && open.item.kind === row.kind) await show(row);

		await load(workspace, project);
	}, [load, open, project, show, workspace]);

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

	const complete = useCallback((row) => wrote(
		row,
		() => ({
			text: `Completed #${row.ref} ${row.title}.`,
			tone: "good",
			/* What it was, so undo restores rather than guesses. `restoreRequest` is what
			   carries it back; this is where it is remembered. */
			undo: { ref: row.ref, kind: row.kind, title: row.title, status: row.status },
		}),
		() => sent(completeRequest(row, workspace)),
	), [workspace, wrote]);

	const undo = useCallback(async () => {
		const going = note && note.undo;

		if (!going) return;

		setNote(null);
		await wrote(
			going,
			() => ({ text: `#${going.ref} is back to ${going.status}.`, tone: "good" }),
			() => sent(restoreRequest(going, workspace)),
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

	const add = useCallback(async (text) => {
		setBusy(true);

		try {
			const made = await sent(addRequest(text, workspace));

			setNote({ text: `Added #${made.ref} ${made.title}.`, tone: "good" });
			await load(workspace, project);
		} catch (failure) {
			setNote({ text: `That was not added. ${failure.message}`, tone: "bad" });
		} finally {
			setBusy(false);
		}
	}, [load, workspace]);

	const showMore = useCallback(async () => {
		/* The next page of each collection that has one, appended. `load` takes the cursors
		   rather than the page number, because keyset pagination is what the API offers and
		   what makes a page boundary stable while somebody is adding things. */
		setBusy(true);

		try {
			await load(workspace, project, more);
		} catch (failure) {
			setNote({ text: `There was more, but it did not arrive. ${failure.message}`,
				tone: "bad" });
		} finally {
			setBusy(false);
		}
	}, [load, more, workspace]);

	const widen = useCallback(async () => {
		/*
			**Out of a project, back to the workspace.** A narrowed list that cannot say what
			narrowed it, or undo it, is an empty backlog with an explanation nobody can reach —
			and the filter arrived in the address rather than from a control the reader touched,
			so there is nothing for them to un-touch.
		*/
		setProject(null);
		window.history.pushState({}, "", `/${encodeURIComponent(workspace)}`);

		try {
			await load(workspace, null);
		} catch (failure) {
			/* A note, not the failure page: there is a readable list on screen and losing it
			   because a re-fetch did not land would cost the reader their place. The guard in
			   `tests/test_web.py` counts the places that blank the page, and it caught this
			   one being written the other way. */
			setNote({ text: `The rest did not load. ${failure.message}`, tone: "bad" });
		}
	}, [load, workspace]);

	const chooseWorkspace = useCallback(async (slug) => {
		/* A workspace is the whole of it: a project from the one you were in does not exist
		   here, and carrying it over would narrow to nothing and look like an empty backlog. */
		setWorkspace(slug);
		setProject(null);
		setNote(null);
		setOpen(null);
		window.history.pushState({}, "", `/${encodeURIComponent(slug)}`);

		try {
			await load(slug, null);
			await roster(slug);
		} catch (failure) {
			setError(failure);
		}
	}, [load, roster]);

	if (!ready) return html`<div class="app"><div class="empty">Reading…</div></div>`;

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
			</header>

			<${Note} note=${note} onUndo=${undo} onDismiss=${() => setNote(null)} />

			${open
				? html`<${Detail} ...${open} members=${members} onOpen=${show} busy=${busy}
					where=${mentionHref(workspace)} onBack=${() => close()}
					onComplete=${complete} onAssign=${assign} />`
				: html`<${Listing} items=${items} onOpen=${show} onComplete=${complete}
					onAdd=${add} busy=${busy} more=${more} onMore=${showMore}
					project=${project} onWiden=${widen} />`}

			<footer class="foot">
				<span>${items.length} items</span>
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
	render(html`<${App} />`, document.getElementById("app"));
}
