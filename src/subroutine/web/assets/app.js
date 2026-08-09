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

export function collectionsFor (arranged) {
	/*
		Which collections a view reads, and the order the answers come back in.

		**Only the *done* view reads one**, because only tasks have a completed axis at all:
		`GET /v1/documents` refuses `status_category` outright — measured, 422 — and a document's
		categories are `draft`, `current`, `superseded` and `archived`, none of which means
		*finished*. A done view asking for documents would not be widened, it would be a page
		that does not load.

		**Anything unrecognised reads both**, which is the safe direction and the one a new
		arrangement almost certainly wants: `list` and `board` are the same rows differently
		arranged and both hold every kind of item there is (§6.2 gives them one ref counter
		precisely so a reader can treat them as one thing).
	*/
	return arranged === "done" ? ["task"] : ["task", "document"];
}

export function listingRequests (slug, key = null, after = null, arranged = null) {
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
		**Finished work is fetched for the board and not for the list** — `#718`, and it is a
		qualification of decision `#649` rather than an oversight, recorded there.

		`#649` says the path decides which rows there are and the query decides how they look.
		This is the query changing a listing *default*, which the decision did not anticipate and
		which its two worked concerns do not touch: an item's address is unchanged and the path
		grammar is unchanged. The precedent is `cli/personal._listed`, which hides deferred work
		for a human reader and not for `--json` — the same rows question answered differently by
		presentation context, reasoned out and accepted.

		The argument for it is simply that **without finished work a board is not a board**: the
		*Done* column was structurally incapable of holding anything, measured on the served
		instance the day it shipped.

		**Tasks only, and the asymmetry is right rather than an omission.** `GET /v1/documents`
		does not accept `include_completed` at all — measured, it answers 422 — because a
		document has no completed axis: its categories are `draft`, `current`, `superseded` and
		`archived`, and none of them means *stop showing me this*. A superseded specification is
		still in the listing by default, so the board already receives every document there is.

		I wrote this comment claiming both collections were asked the same, which asserted a
		symmetry that does not exist. `test_every_request_the_browser_makes_is_one_the_instance_accepts`
		refused the request before it shipped — the guard `#640` exists for, doing exactly its
		job for the second time on this arc.
	*/
	const finished = arranged === "board" ? "&include_completed=true" : "";

	/*
		**The *done* view narrows to finished work and orders on when it finished** (`#706`).

		`status_category` rather than `?status=done`: a status *key* is per-workspace and
		renameable, so a view keyed on one breaks on the first instance that renames it.
		`-completed_at` rather than `-updated_at`: the tempting proxy reorders the page whenever
		somebody edits a finished item, for a reason nobody did.

		**No `include_completed` beside it, and that is not an omission.** Asking for a finished
		category implies it — `tasks.completion_wanted` returns true — and passing `false` as well
		is refused by name rather than silently resolved. Sending `true` would be a second way of
		saying the same thing, which is how two spellings of one rule start disagreeing.

		**Both handles were built by `#710` and both were measured on the live instance**, not
		read off the code: `?status_category=done&order=-completed_at` answers 200, newest finish
		first, and pages normally.

		**This is a bigger step past decision `#649` than the board took and it is flagged rather
		than taken quietly.** The board changed a listing *default*; this changes which rows there
		are, which `#649` gives to the path. What survives is the decision's own reasoning: its two
		worked concerns were *is `/personal` legal* and *does an item gain one address per view*,
		and neither is touched — the path grammar is unchanged and an item still has exactly one
		address. The sharper statement, recorded on `#649` for Simon to accept or reject: **the
		path decides where rows come from; the query decides the arrangement, and an arrangement
		chooses from within that place.** The alternative was a path segment, which `#649`
		rejected on its own merits and which would reserve a word in the position every address
		starts with.
	*/
	const only = arranged === "done" ? "&status_category=done&order=-completed_at" : "";

	const asks = {
		task: { kind: "task", method: "GET", path: scoped(
			`/tasks?limit=${PAGE}&fields=${TASK_FIELDS}${narrowed}${finished}${only}`
			+ from(after && after.tasks), slug) },
		document: { kind: "document", method: "GET", path: scoped(
			`/documents?limit=${PAGE}&fields=${DOCUMENT_FIELDS}${narrowed}`
			+ from(after && after.documents), slug) },
	};

	/* **Tagged with the kind rather than positional**, so a view reading one collection cannot
	   have its rows labelled by whichever slot they happened to arrive in. */
	return collectionsFor(arranged).map((kind) => asks[kind]);
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

export function addressOf (item, workspace) {
	/* The address to put in the bar for one item: readable when we know enough, durable
	   always. */
	const durable = `/${encodeURIComponent(workspace)}/${item.ref}`;

	if (!item.project_key) return durable;

	return `/${encodeURIComponent(workspace)}/${encodeURIComponent(item.project_key)}`
		+ `/${item.ref}`;
}

/* ---- views (`#651`, decision `#649`) ------------------------------------- */

/*
	**The path says which rows there are; the query says how they look.** That is decision
	`#649` and it is §14.10 applied to a second surface: `?fields=` and `?format=` already decide
	how a row is *reported* while `domain/scoping.py` decides which rows exist, and
	`api/shaping.py` takes already-rendered views specifically so a display parameter can never
	reach the `WHERE` clause. A view is the same promise one layer out.

	**The default is the absence of the parameter**, not `?view=list`. That is what lets it
	become per-workspace and later per-user without invalidating an address anybody wrote down —
	an address that spells out today's default would freeze it.
*/
/*
	**`done` is a third kind of thing from the first two, and it earns its place beside them.**
	`list` and `board` are the same rows arranged differently; `done` is a narrower set of rows.
	Putting it in a second control would be more honest to the taxonomy and worse for the reader —
	`#651`'s reason for having a control at all is that *a reader who has never seen one cannot
	type a word they have not been told*, and a filter with no control is a feature nobody finds.
	Every tracker a person has used offers this as a tab beside the others.
*/
export const VIEWS = ["list", "board", "done"];

export const DEFAULT_VIEW = "list";

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

export function withView (path, view) {
	/*
		One address, carrying the arrangement — `#651`'s *survives navigation*.

		Four places wrote an address before this and every one of them dropped the query, so
		`/projects?view=board` became `/projects` the moment anything was opened. The view is not
		state the app remembers; it is part of the address, which is what makes it something a
		reader can send somebody.

		**The default is written as an absence**, so an ordinary address stays ordinary and
		nothing has to be stripped back out later.
	*/
	return !view || view === DEFAULT_VIEW ? path : `${path}?view=${encodeURIComponent(view)}`;
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
	*/
	if (item.completed_at) {
		return `${item.status_category === "cancelled" ? "cancelled" : "done"} `
			+ `${day(item.completed_at)}`;
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

export function Agenda ({ buckets, more, where, onAdd, onOpen, onComplete, busy }) {
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
	const adding = onAdd && html`
		<${Adding} onAdd=${onAdd} busy=${busy}
			note=${where ? `Adds to ${where}.` : null} />
	`;

	if (buckets.length === 0) {
		return html`
			<div class="listing agenda">
				${adding}
				<div class="empty">Nothing is due, and nothing is waiting. </div>
			</div>
		`;
	}

	return html`
		<div class="listing agenda">
			${adding}

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
	widenTo,
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

	/* The same test the listing makes, and it has to be the same: both render one page of two
	   collections, and a column tally that reads as a total is worse on a board than a short
	   list is, because a column is where somebody looks to see that nothing is left. */
	const truncated = more !== null && more !== undefined
		&& (more.tasks !== null || more.documents !== null);

	return html`
		<div class="listing board">
			${onAdd && html`<${Adding} onAdd=${onAdd} busy=${busy} />`}

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
						<h2>${column.label}${" "}<span class="tally">${column.items.length}</span></h2>

						${column.items.length === 0
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

export function Adding ({ onAdd, busy, note }) {
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
			${/* **Only where it is not obvious.** A listing is one workspace and saying so on
			     every page would be the column that says the same thing on every row (§12.2a);
			     the agenda spans them, so there the answer is worth a line (`#652`). */ null}
			${note && html`<span class="lands">${note}</span>`}
		</form>
	`;
}

export function Listing ({
	items, onOpen, onComplete, onAdd, onMore, onWiden, busy, more, project, workspace, widenTo,
	empty = "Nothing here yet.",
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
	/* How the rows are arranged, read from the address rather than remembered (`#651`). It is
	   part of the address so that a reader can send somebody the thing they are looking at. */
	const [view, setView] = useState(DEFAULT_VIEW);
	const since = useRef(null);

	const go = useCallback((path, { replace = false, arranged = view } = {}) => {
		/*
			**Every address this app writes goes through here**, carrying the arrangement.

			Four places wrote one before `#651` and all four dropped the query, so
			`/projects?view=board` became `/projects` the moment anything was opened — the view
			would have been a setting that silently expired on the first click.
		*/
		const wanted = withView(path, arranged);

		if (window.location.pathname + window.location.search === wanted) return;

		window.history[replace ? "replaceState" : "pushState"]({}, "", wanted);
	}, [view]);

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
			**The arrangement is read from the address, not from state and not from an argument**
			— `#719`, and the parameter this replaces is the defect.

			It was `arranged = view`, which reads the *state*, and `setView` does not land in the
			render that calls this. So the first load after arriving at `/projects?view=board`
			asked for the list's rows; ten seconds later the poll — recreated once `view` had
			landed — asked for the board's. Right rows, one `POLL_MS` late, which is exactly how
			Simon described it.

			**The comment in `start` three lines from the call site says this precise thing about
			`slug`**, and I quoted its reasoning elsewhere in the same commit without applying it
			here. Passing the value at both sites would have worked and left the trap for the
			next call site, so the parameter is gone instead.

			The address is the one source that is never stale: `#651` made the view part of it,
			`go()` writes it before any load that changes one, and `popstate` fires only after it
			has already changed. Nothing to capture, nothing to forget.
		*/
		const arranged = viewOf(window.location.search).view;

		/* What to ask for is `listingRequests`, which is pure and checked (`#640`). What is
		   left here is what to do with the answers. */
		const wanted = listingRequests(slug, key, after, arranged);
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
				const showing = agenda !== null;
				const seen = await sent(pollRequest(showing ? null : workspace, since.current));

				if (seen.items.length === 0) return;

				since.current = seen.items[seen.items.length - 1].seq;

				await (showing ? readAgenda(me ? me.workspaces : []) : load(workspace, project));
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
			go(addressOf(found.item, slug), { replace: !history });

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
			const arrangement = viewOf(window.location.search);

			setView(arrangement.view);
			const { slug, refused } = chosenWorkspace(
				asked, identity.workspaces.map((space) => space.slug), workspace,
			);

			setMe(identity);
			setWorkspace(slug);

			if (arrangement.refused !== null) {
				setNote({
					text: `There is no ${arrangement.refused} view. Showing the list.`,
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
				asked && asked.ref !== null
					? show({ ref: asked.ref }, { history: false, slug })
					: null,
			]);
		} catch (failure) {
			setError(failure);
		} finally {
			setReady(true);
		}
	}, [load, readAgenda, roster, show, workspace]);

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
			setView(viewOf(window.location.search).view);

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
	}, [ready, error, workspace, project, agenda, me, load, readAgenda, show]);

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

	const add = useCallback(async (text) => {
		/* **The reload afterwards keeps the filter the reader is looking at.** Without
		   `project` declared below, adding an item inside a project answered by replacing the
		   list with the whole workspace — the same stale closure as the poll, reached by a
		   button instead of a timer, and read as "adding a task loses my project". */
		setBusy(true);

		try {
			const made = await sent(addRequest(text, workspace));

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
			await roster(slug);
		} catch (failure) {
			setError(failure);
		}
	}, [go, load, roster]);

	const chooseView = useCallback(async (wanted) => {
		/*
			**Switching does refetch, and the comment here used to say it must not.**

			That sentence was written the same day and was wrong within hours: it argued that a
			view is a rendering of rows `load` already has, so refetching would make the query
			decide which rows exist. The consequence, which Simon found by opening the page, is
			that the *Done* column was structurally incapable of holding anything — a listing
			excludes finished work by default, so the board never received a single done row
			(`#718`).

			What is fetched is a listing **default**, not a scope. `#649`'s rule stands for what
			it was decided about — the path grammar, and an item having one address — and this
			is recorded on it as a qualification rather than left as a contradiction.

			`wanted` is passed to `load` explicitly because `setView` has not landed in this
			render, so the closure still holds the previous arrangement — the same reason `start`
			passes `slug` rather than reading `workspace`.
		*/
		setView(wanted);

		/* **The address first, then the reload** — and the order is the whole of why `load`
		   needs no argument for this: it reads the arrangement from the address, which `go` has
		   already written. */
		go(
			listingAddress({ agenda: agenda !== null, workspace, project }),
			{ arranged: wanted },
		);

		if (agenda === null) await load(workspace, project);
	}, [agenda, go, load, project, workspace]);

	if (!ready) return html`<div class="app"><div class="empty">Reading…</div></div>`;

	/* The address of the listing behind whatever is showing — what *All items* goes back to, and
	   what the view switcher hangs its arrangements off. One expression, because `close` and
	   `chooseView` already agree on it and a second spelling here would be the thing that drifts. */
	const behind = listingAddress({ agenda: agenda !== null, workspace, project });

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
					**A control, because an address is not a way to find something** (`#651`).
					`?view=board` is what the arrangement *is*, and a reader who has never seen
					one cannot type a word they have not been told. It is on a listing only: the
					agenda is chosen by the path and arranging it by status would answer a
					question nobody asked of it.
				*/ null}
				${/*
					**"Which view" rather than "how to arrange this"** (`#706`). The label was
					written when both entries were arrangements of one set of rows; `done` is a
					different set, so the old wording described two of the three and quietly
					mis-announced the third to the readers who depend on it most.
				*/ null}
				${/*
					**And each one is a link** (`#722`), so a reader can open the board in a tab
					beside their list rather than replacing it. `withView` builds exactly the
					address `chooseView` is about to write, which is what makes the two agree —
					a hand-built `href` here would be a second copy of the rule `#651` centralised.
				*/ null}
				${!open && agenda === null && html`
					<nav class="views" aria-label="Which view">
						${VIEWS.map((name) => html`
							<a key=${name} class=${name === view ? "chosen" : ""}
								href=${withView(behind, name)}
								aria-current=${name === view ? "true" : undefined}
								onClick=${(event) => followed(event, () => chooseView(name))}
								>${name}</a>
						`)}
					</nav>
				`}
			</header>

			<${Note} note=${note} onUndo=${undo} onDismiss=${() => setNote(null)} />

			${open
				? html`<${Detail} ...${open} members=${members} onOpen=${show} busy=${busy}
					where=${mentionHref(workspace)} onBack=${() => close()}
					backTo=${withView(behind, view)} workspace=${workspace}
					onComplete=${complete} onAssign=${assign} />`
				: agenda !== null
					? html`<${Agenda} buckets=${agenda} more=${unscheduled}
						onAdd=${add} busy=${busy} where=${workspace}
						${/* **Each row is opened in its own workspace, not in the one the
						     switcher holds.** The agenda spans them; `show` defaults its slug
						     to `workspace`, so a row from `sandbox` would be looked up in
						     `projects` and reported missing. `#640`'s exact shape — the rule
						     right, the display right, and no wire between them — which is why
						     `agendaBuckets` resolves the slug onto every row. */ null}
						onOpen=${(row) => show(row, { slug: row.workspace || workspace })}
						onComplete=${(row) => complete(row, row.workspace || workspace)} />`
					: view === "board"
						? html`<${Board} items=${items} onOpen=${show} onComplete=${complete}
							onAdd=${add} busy=${busy} more=${more} onMore=${showMore}
							project=${project} workspace=${workspace} onWiden=${widen}
							widenTo=${withView(listingAddress({ workspace }), view)} />`
						/*
							**No capture box on the finished view** (`#706`). Adding from here
							would report success over a page the new item cannot appear on — it is
							open, and this view holds only what is over — which is `#515`'s shape:
							every step reports success and the reader is left confirming the wrong
							conclusion. `Row` already declines to offer *Complete* on finished work
							by way of `completable` (`#724`), so `onComplete` is passed and simply
							never applies; the add box has no such guard and is withheld here.
						*/
						: html`<${Listing} items=${items} onOpen=${show} onComplete=${complete}
							onAdd=${view === "done" ? null : add} busy=${busy} more=${more}
							onMore=${showMore} project=${project} workspace=${workspace}
							onWiden=${widen} widenTo=${withView(listingAddress({ workspace }), view)}
							empty=${view === "done"
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
