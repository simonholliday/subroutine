/*
	The address is the state (`#738`): which view, which selection, which page — read out of
	a URL and written back into one, so a link carries everything a screen is showing.

	**Split out of `app.js` by `#1849`**, which moved 10,286 lines into thirteen modules and
	changed nothing about what any of them do. Read `app.js` for what the whole app is; this
	file is one layer of it.
*/

import { named } from "./dates.js";
import { filed } from "./requests.js";
import { HORIZON_DAYS } from "./settings.js";

export function agendaRequest (slug = null, project = null) {
	/*
		What is due — `#652`, decision `#649`, and scoped since `#1215`.

		**Unscoped it spans every workspace this reader can see, and that is what `/` asks for.**
		A workspace or a project in the address narrows it, because the agenda is an arrangement
		of a place now rather than the one thing at the root.

		**No `workspace_id` when nothing named one, and that is the whole point.** `GET /v1/tasks` refuses an ambiguous
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
	/*
		**The look-ahead is asked for, and a bucket nobody requests is a bucket nobody gets**
		(`#985`). This sent no query at all, and `GET /v1/agenda` omits `upcoming` unless
		asked — so *any* future deadline took a task out of every bucket and off this page
		entirely, until the day it fell due. Three correct decisions with no wire between
		them: the domain, the endpoint and `BUCKETS` were each right on their own, and this
		page rendered a `Next 7 days` heading it could never be given data for.
	*/
	/* **The scope, when the address named one** (`#1215`). `/` sends neither and gets the
	   merged agenda across every workspace, which is what §13.7 says a person's day is; a
	   workspace or a project in the address narrows it, exactly as it narrows a listing.

	   **`project` needs `workspace_id` beside it and the endpoint refuses it without one**, so
	   both are written from one place rather than one being forgotten at a call site — a
	   project key is per workspace, and a request naming a project and no workspace is a
	   question with more than one answer. */
	const scope = (slug ? `&workspace_id=${encodeURIComponent(slug)}` : "")
		/* **`encodeURIComponent`, not `encodedPath`.** A project is a whole path since `#958`,
		   and here it is a query *value* rather than address structure — so its separators are
		   part of the value and must be escaped, exactly as the listing escapes them. Written
		   the other way first: `websites/handouts` went on the wire with a literal slash, which
		   the route reads as a different project. `listingRequests` is the copy to match. */
		+ (slug && project ? `&project=${encodeURIComponent(project)}` : "");

	return { path: `/agenda?horizon_days=${HORIZON_DAYS}${scope}`, method: "GET" };
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

	/* **The item's own address, since `#512` publishes one.** This used to rebuild a path
	   out of the one the reader navigated, keeping it only when its last segment matched the
	   item's key — which was the best available while a row carried a key and nothing else,
	   and is now second-hand information about a fact the row states. */
	const path = item.project_path || item.project_key;

	return `/${encodeURIComponent(workspace)}/${encodedPath(path)}/${item.ref}`;
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
export const VIEWS = ["agenda", "list", "board"];

/*
	**The agenda, everywhere and by default** — Simon, 2026-08-24, amending `#649`.

	This is that decision's own unbuilt row rather than a departure from it. Its grammar already
	applies `?view=` *"on any of the above"* and its own §*Why `/` is the agenda* spells the
	pairing out — *"`/?view=list` is the backlog"* — and nothing built it: the root showed an
	agenda because the **path** named no workspace, so `/?view=list` rendered the agenda and
	dropped the parameter.

	**It does not break *a view never selects*.** That clause was written against `#718` and
	`#706`, where a view *name* silently appended `include_completed=true` or
	`status_category=done` to the tasks listing and made those filters unreachable on their own.
	The agenda is not that: it is a different endpoint answering a different question, which
	`#649` says itself. The bound it actually sets — a view may never reach a filter the caller
	could not also have sent, and may never narrow what a credential can see — is untouched.

	**What the amendment adds is the sentence this default needs**: an arrangement may draw its
	rows from a different endpoint, and when it does it must say what it left behind. At `/`
	there was nothing to compare the agenda against; beside `?view=list` on the same address
	there is. `Agenda`'s footer is that, and `later_total`/`unscheduled_total` were already half
	of it.
*/
export const DEFAULT_VIEW = "agenda";

//: The arrangement drawn from `/v1/agenda` rather than from a listing, named once so the
//: places that switch readers cannot disagree about the spelling.
export const AGENDA_VIEW = "agenda";

export const SELECTABLE = {
	/*
		`status_category` rather than `status`: a status *key* is per-workspace and renameable,
		so an address keyed on one breaks on the first instance that renames it. The four
		categories are fixed by the model, which is why they can be spelled here at all.
	*/
	status_category: ["todo", "in_progress", "done", "cancelled"],
	include_completed: ["true"],
	/*
		**Every order a reader can choose, and the finished view's** (`#782`, `#661`).

		It was one value until `#782` — the finished view's — because until a control existed,
		admitting more would have published addresses whose results nothing had driven.

		**`ref` is deliberately absent.** One counter allocates refs in creation order (§6.2), so
		*by number* and *oldest first* are the same page under two names, and a second name for
		one ordering is a choice a reader has to make and cannot get right.

		Each of these must have an `ORDERINGS` entry, which is what makes a listing able to say
		how it is arranged; `tests/test_web.py` fails the build on one that does not.
	*/
	order: [
		"-created_at", "created_at", "title", "-updated_at", "-priority_score", "-completed_at",
	],
	/*
		**Free text, and the only one** (`#775`). Every other entry here maps a name to the
		values it may carry, which is what stops the address becoming a passthrough to
		`api/query.py`; a search term cannot be enumerated, so `null` says *any value* and
		`permits` is where that distinction lives rather than spread across two callers.

		**The bound `#738` set still holds**, and it is worth restating because this is the
		first entry to relax half of it: a selection parameter may only be one the caller could
		have sent anyway, and may never widen what a credential can see. `q` **narrows** —
		`domain/search.matching` is an extra predicate on a query `domain/scoping` has already
		narrowed — so admitting any value here admits nothing a reader could not already read.
	*/
	q: null,
	/*
		**How the allowance is spent, which is a selection and not an arrangement** (`#1790`).

		It looks like an arrangement and is not, and the distinction is `#738`'s: an arrangement
		decides how rows are *displayed*, and this decides which rows *arrive*. One page spends
		its whole allowance in one order, so a category holding older work loses its rows to an
		unrelated category's recency — measured here, a board drew one row under *In progress*
		where three existed. Grouping gives each its own.

		**It clears `#738`'s bound**, which this file states two entries above: a selection
		parameter may only be one the caller could have sent anyway, and may never widen what a
		credential can see. This reallocates an allowance across rows `domain/scoping` has
		already narrowed, so it widens nothing at all.

		**In the address rather than derived from the view**, for the reason `#738` exists: the
		request builder must not know which arrangement is showing. The board chip carries it in
		its selection, exactly as it already carries `include_completed`.
	*/
	group_by: ["status_category"],
	/*
		**A tag and a person, free text like `q` and for the same reason** — `#1020`. Neither
		can be enumerated: a tag is whatever somebody typed and an account name is whatever the
		instance has, so `null` says *any value* and `permits` refuses only the empty one.

		**Both clear `#738`'s bound, which this file states twice above**: a selection parameter
		may only be one the caller could have sent anyway, and may never widen what a credential
		can see. `GET /v1/tasks` has filtered on `tag` since `#1319` and on `assignee` since M1 —
		read out of the endpoint's own refusal rather than assumed — and both are extra
		predicates on a query `domain/scoping` has already narrowed, so admitting them here
		admits nothing a reader could not already read.

		**Query rather than path** (`#649`), and that is the whole of the design question this
		item was filed to settle: the path says which rows there are and the query says how they
		are shown, and neither a tag nor a person is a *place*.
	*/
	tag: null,
	assignee: null,
};

/*
	Which collections can answer each selection parameter — `#872`.

	**This existed as a rule and not as a statement, which is exactly how it broke.** The two
	functions below both needed it: `collectionsFor` decides which collections to *read*, and
	`listingRequests` decides what to *send* each of them. Neither said which parameters a
	documents request can take, so the second withheld the whole selection from documents —
	right for the two parameters that existed when it was written, and wrong the moment a third
	arrived.

	`q` was that third (`#775`). It inherited a tasks-only rule nobody had written down, so a
	browser search filtered the tasks and returned **every document** — measured on the served
	instance, and the first thing Simon tried.

	So: a name here is the one place that answers *where does this go*, and
	`tests/test_web.py` fails the build on a `SELECTABLE` entry missing from it. A fourth
	parameter cannot repeat this by being forgotten; it can only repeat it by being declared
	wrongly, which is a different and much louder mistake.

	**Three answers, not two, and collapsing them is a defect of its own** — met while fixing
	this one, and caught by two existing tests. "Documents cannot be sent this" and "documents
	must not be asked at all" are different facts:

	- **`sent`** — pass it through. `order` (narrowed further by `ORDERINGS[…].both`, `#782`)
	  and **`q`**, which `GET /v1/documents` has always filtered correctly. Only the browser
	  was not asking.
	- **`already`** — omit it and keep the collection, because the answer is the same either
	  way. `include_completed` is a measured 422 on documents, *and* a document listing shows
	  every document there is — a superseded specification is in it by default. So the
	  parameter is unsendable and its absence changes nothing.
	- **`cannot`** — drop the collection. `status_category` is a 422 *and* its absence would
	  give the wrong rows: a document's categories are `draft`, `current`, `superseded` and
	  `archived`, and none of them means *finished*, so a listing of finished work has no
	  honest document half at all.
*/
export const ANSWERED_BY = {
	status_category: { task: "sent", document: "cannot" },
	include_completed: { task: "sent", document: "already" },
	order: { task: "sent", document: "sent" },
	q: { task: "sent", document: "sent" },
	/* **Both, and it is the same axis name meaning two vocabularies** — a task's categories are
	   `todo`/`in_progress`/`done`/`cancelled` and a document's are
	   `draft`/`current`/`superseded`/`archived`. Unlike `status_category` above, which is
	   `cannot` because a *value* of it has no honest document half, the axis itself is answered
	   by each collection in its own terms. */
	group_by: { task: "sent", document: "sent" },
	/* **A document carries tags and can be narrowed by one**, so this is `sent` on both sides
	   and a tagged page keeps its document half. */
	tag: { task: "sent", document: "sent" },
	/* **A document has no assignee at all** — the column does not exist on it, and
	   `GET /v1/documents` refuses the parameter. So `cannot`, which `collectionsFor` already
	   reads: a page narrowed to a person is tasks only, and it is that way because the rows do
	   not exist rather than because somebody chose to hide them. */
	assignee: { task: "sent", document: "cannot" },
};

export function answers (kind, name) {
	/* How a collection handles this selection parameter: `sent`, `already` or `cannot`. */

	const handling = ANSWERED_BY[name];

	return handling === undefined ? "cannot" : handling[kind] || "cannot";
}

export function permits (name, value) {
	/*
		Whether a selection parameter may carry a value — `#775`.

		Two rules in one place: an enumerated name takes what its list allows, and a free-text
		one takes anything that is not empty. **An empty `q` is refused rather than sent**,
		which is `domain/search.terms`' own rule said on this side: a query with no words in it
		narrows nothing, and `q=" "` used to be a real filter matching every row containing a
		space — a filter nobody asked for, answering a question nobody put.
	*/
	const allowed = SELECTABLE[name];

	if (allowed === undefined) return false;

	return allowed === null ? String(value).trim() !== "" : allowed.includes(value);
}

/* What the controls produce. Named because two places need each — the chip that writes the
   address and the test that drives it — and a second spelling is what drifts. */
export const EVERYTHING = { include_completed: "true" };

/* What a board asks for: everything, **split so that no column is starved by its neighbours**
   (`#1790`). Named beside `EVERYTHING` rather than spelled at the chip, because the test that
   drives the chip and the chip itself must not be two spellings of one answer. */
export const BOARD = { ...EVERYTHING, group_by: "status_category" };

/* **The order is now what the server would default to anyway** — `domain.tasks.default_order`,
   item `#1150` — and it is written out here rather than dropped, for a reason that is about
   this page and not about the rule.

   `chips` decides that *done* shows a **list** rather than keeping whichever arrangement was
   showing, and its argument is that this selection carries an order while an order means
   nothing on a board. Taking the order out of the address would make that sentence false about
   its own code while leaving the behaviour right, which is worse than a copy that agrees.

   So: a copy, deliberately, and this comment is what stops it becoming a silent one. If the
   default changes, change it here too — or take the address change and rewrite `chips`. */
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

		if (permits(name, value)) selection[name] = value;
		else refused.push(`${name}=${value}`);
	});

	return { selection, refused };
}

export function showingOf (search) {
	/* The arrangement and the selection an address asks for, read once so the two cannot be
	   read from different places and disagree — which is `#719`'s defect in miniature. */
	const arrangement = viewOf(search);
	const rows = selectionOf(search);
	const narrowed = Object.keys(rows.selection).length > 0;

	/*
		**An arrangement that cannot honour a selection does not get one** — `#1215`, and the
		one edge the agenda-as-default creates.

		The agenda is a different endpoint with its own question, so it takes no
		`status_category`, no `include_completed` and no `order`. Left alone, `?status_category=
		done` with no view named would have become an agenda that silently ignored the words
		beside it — an address stating something the page is not doing, which is precisely what
		decision `#649` exists to prevent.

		**Two cases and they are answered differently, because one of them is a mistake and the
		other is not.** A selection with *no* view named is somebody asking a question the list
		answers, so the list is the default for them; nothing was refused and nothing is said. A
		selection beside an explicit `view=agenda` is a reader asking for two things that cannot
		both happen, and that is named and fallen back exactly as an unknown view is — `viewOf`'s
		own rule, which is that a person types these and deserves the word back.
	*/
	const impossible = narrowed && arrangement.view === AGENDA_VIEW;
	const named = new URLSearchParams(String(search || "")).get("view") === AGENDA_VIEW;

	const showing = impossible ? "list" : arrangement.view;

	/*
		**A board groups whether or not the address says so** — `#1798`.

		`#1790` put the axis in the selection, which is right: the request builder must not know
		which arrangement is showing (`#738`). What it missed is that **an address written
		before that shipped names a board and carries no axis**, and there are a lot of those —
		every bookmark, every link anybody has sent, and the one in the reader's own history.
		Simon opened exactly that address on the served instance and got the ungrouped board
		back, with the whole defect intact and no sign that anything had changed.

		**This is a default, not a derivation, and the distinction is `#738`'s.** What that item
		forbade was the *request builder* branching on the arrangement, which made the selection
		invisible and left `?include_completed=true` on a list unreachable. Here the axis is
		still an ordinary member of the selection: it is what the chip writes, what
		`withShowing` puts back into the address, and what `listingRequests` reads. Only its
		absence is filled in.

		**There is deliberately no way to ask for an ungrouped board.** One allowance shared
		across every column is not a thing anybody wants — it draws a column empty while it
		holds work, which is what `#1782` was — so an address that could still request it would
		be a way to reach a defect on purpose.
	*/
	/*
		**The axis alone, never the rest of `BOARD`.** Spreading the whole preset would put
		`include_completed` on a board address that deliberately left it off — and `?view=board`
		without it is a coherent thing to ask for, which is `#738`: its finished column reads
		*Not shown* rather than reporting on rows nobody asked about.
	*/
	const selection = showing === "board" && rows.selection.group_by === undefined
		? { ...rows.selection, group_by: BOARD.group_by }
		: rows.selection;

	return {
		view: showing,
		selection,
		refused: (arrangement.refused ? [`view=${arrangement.refused}`] : [])
			.concat(impossible && named ? ["view=agenda beside a filter"] : [])
			.concat(rows.refused),
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

/*
	**What a narrowing is, as opposed to an arrangement** — `#1020`. `Narrowed` says these out
	loud and *Show everything* is what drops them, so both need the same list and neither should
	carry its own copy.

	`project` is not here because it is on the *path* (`#649`) — widening drops it by addressing
	the workspace instead, which is what `listingAddress` already does. `q` is not here either:
	the search box shows the term and clears it, so it has a way back of its own and a second
	one would be two controls for one state.
*/
export const NARROWINGS = ["tag", "assignee"];

export function widened (showing) {
	/* The same showing with every narrowing dropped — what *Show everything* goes to. */

	const selection = { ...((showing && showing.selection) || {}) };

	for (const name of NARROWINGS) delete selection[name];

	return { ...showing, selection };
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
		/* **First, because it is the default** (`#1215`). A reader arriving at a place is
		   looking at this one, so a switcher whose first option was something else would read
		   as though the page had chosen the second. */
		{ name: "agenda", showing: { view: AGENDA_VIEW, selection: {} } },
		{ name: "list", showing: { view: "list", selection: {} } },
		{ name: "board", showing: { view: "board", selection: BOARD } },
		/* **`list`, spelled out, not `DEFAULT_VIEW`.** It read the default until the default
		   became the agenda, at which point *done* would have asked an agenda to show finished
		   work — which it holds back by construction, so the chip would have produced an empty
		   page. The reason above is unchanged and is why this is a list at all: the finished
		   selection carries `order=-completed_at`, and an order means nothing on a board or in
		   a set of day buckets. */
		{ name: "done", showing: { view: "list", selection: ONLY_FINISHED } },
	].map((chip) => ({
		name: chip.name,
		href: withShowing(behind, chip.showing),
		showing: chip.showing,
		chosen: chip.name === "done"
			? showing.selection.status_category === "done"
			: !narrowed && showing.view === chip.name,
	}));
}

//: What the browser tab says this application is, after whatever the page is.
export const PRODUCT = "Subroutine";

export function shortVersion (version) {
	/*
		The build, as short as it can be and still name itself — `#1536`.

		**Everything after `+` is PEP 440's *local version*, and it is the sha.** Dropping it
		turns `0.8.3.dev36+g7fad4af9d` into `0.8.3.dev36` and leaves a tagged release exactly
		as it was, because a tag has no local segment at all — so one rule gives both forms
		and neither is a special case.

		**Display only.** `instance_version` itself is untouched: `releaseMoved` orders it and
		`installations.ordered` refuses to compare anything it cannot, and a version shortened
		before either of them saw it would be answering a different question. The full string
		is on the element's `title`, so the sha is one hover away rather than gone.
	*/
	if (!version) return null;

	const [release] = version.split("+");

	return release || null;
}

export function titlesByPath (projects) {
	/*
		Every project's whole path against its title — `#1214`.

		**The tree arrives flat and in pre-order with a `depth`**, which is the shape
		`placesToGo` already rebuilds an address from and the reason a project's `key` need only
		be unique among its siblings (`#958`). The same walk answers a different question here:
		whatever sits at each height is this row's ancestry, so `ancestry.length = depth` is what
		pops back out of a subtree.

		**Titles, where a row's project *chip* uses slugs**, and the two are not in tension.
		`#151`'s rule is that a chip is the thing you can type back into an address; a tab title
		is read at a glance and never typed, and a reader scanning eight tabs is looking for the
		word they call the place rather than the word the URL calls it. Simon's own examples are
		titles.
	*/
	const found = {};
	const ancestry = [];

	(projects || []).forEach((one) => {
		const depth = one.depth || 0;

		ancestry.length = depth;
		ancestry.push(one.key);

		found[ancestry.join(PATH_SEPARATOR)] = `${one.title || one.key}`;
	});

	return found;
}

export function pageTitle ({
	item = null, place = null, showing = null, workspaces = [], projects = [],
}) {
	/*
		What the browser tab says — `#1214`, Simon: *"I have multiple tabs open and they all just
		say 'Subroutine' — unhelpful."*

		**Nothing wrote one at all.** `index.html` carried a static `<title>` and `document.title`
		was assigned nowhere, so it was not that the title was wrong; it was that every tab, on
		every page, said one word.

		| page | title |
		| --- | --- |
		| an item | `#1111 The release gate finishes inside its own timeout` |
		| the root | `Agenda` |
		| a workspace | `Projects: Agenda` |
		| a project | `Projects / Subroutine: Board` |
		| a sub-project | `Projects / Subroutine / Web UI: Board` |

		**The scope reads with `/` and the view with `:`**, which is his and is right: they are
		different axes, and one separator for both would read as a four-level path.

		**A tab truncates from the right, so the front of the title is what survives.** That is
		why the ref leads on an item — `#1111` is what tells two tabs apart at fifteen characters.
		It cuts the other way for a place and only sometimes: several tabs on *different* projects
		are told apart by the scope, several on *one* project in different views by the view.
		Scope-first is right because the first case is the common one, and the trade is recorded
		here rather than rediscovered.

		**The view is whichever control is highlighted**, read from `chips` rather than from
		`showing.view`. So the tab and the switcher cannot disagree — and *done* is a selection
		rather than an arrangement (`#738`), which a title built from the view name alone would
		have called `List`.

		**The product name is on every page**, which settles the disagreement between his two
		examples: a bookmark or a history entry reading only `Agenda` says nothing about which
		application it came from, and the title is what names both.

		**Pure, so it can be driven in Node** (`#640`) — the whole family of defects this arc
		shipped were wiring rather than rules, and a rule that can be asked directly is one
		fewer.
	*/
	const suffix = ` · ${PRODUCT}`;

	/* **An item is its own page and takes no scope**, exactly as its address takes neither an
	   arrangement nor a selection (`#766`): both describe a set of rows, and one item is not
	   part of any set. */
	if (item) return `#${item.ref} ${item.title || ""}`.trim() + suffix;

	const named = (place && place.workspace) || null;
	const space = (workspaces || []).find((one) => one.slug === named);
	const titles = titlesByPath(projects);
	const filed = (place && place.project) || "";

	/* Each segment of the project path in turn, so a sub-project reads as its whole lineage.
	   A segment the tree does not describe falls back to its key rather than vanishing — the
	   listing does the same, for `#959`'s reason: a chip that disappears is worse than one
	   naming something unfamiliar. */
	const chain = filed
		? filed.split(PATH_SEPARATOR).map((_part, at, parts) => {
			const path = parts.slice(0, at + 1).join(PATH_SEPARATOR);

			return titles[path] || parts[at];
		})
		: [];

	const scope = named ? [`${(space && space.title) || named}`, ...chain] : [];
	const chosen = (chips("", showing || { view: DEFAULT_VIEW, selection: {} })
		.find((chip) => chip.chosen) || {}).name;
	const view = `${chosen || (showing && showing.view) || DEFAULT_VIEW}`;
	const shown = view.charAt(0).toUpperCase() + view.slice(1);

	return (scope.length > 0 ? `${scope.join(" / ")}: ${shown}` : shown) + suffix;
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

		**`agenda` means *the address named no place*, and since `#1215` that is no longer the
		same thing as *the agenda is showing*.** A project can be showing one now, and a caller
		passing the second would push a reader from `/projects/subroutine` back to the merged
		root every time they closed an item. The property keeps its name because callers pass it
		by name and `App` holds the honest one, `everywhere`.
	*/
	if (place.agenda) return "/";

	if (!place.workspace) return "/";

	const base = `/${encodeURIComponent(place.workspace)}`;

	/* Encoded per segment, because `place.project` is a whole path since `#958` and
	   `encodeURIComponent` would turn its separators into `%2F` — an address the router reads
	   as one project keyed with slashes in it, which is a project that cannot exist. */
	return place.project ? `${base}/${encodedPath(place.project)}` : base;
}

/* What separates one project key from the next in an address — decision `#957`, and the same
   character `domain.projects.PATH_SEPARATOR` uses. Named rather than written as a literal in
   five places, because it is the one thing this file and the server have to agree about. */
export const PATH_SEPARATOR = "/";

export function encodedPath (path) {
	/*
		A project's whole address, escaped for a URL a segment at a time.

		**Not `encodeURIComponent` over the whole thing.** That escapes the separators too, so
		`substation/dist` becomes `substation%2Fdist` — one segment, naming a project keyed with
		a slash in it, which is a project that cannot exist. The separators are structure and the
		segments are values; only the second kind is escaped.
	*/
	return String(path || "").split(PATH_SEPARATOR).map(encodeURIComponent).join(PATH_SEPARATOR);
}

export function projectLabel (item, place) {
	/*
		Where a row's item lives, as much of its address as the URL did not already say —
		decision `#957` §4.

		| the page | the label on a row in `projects/subroutine/ui` |
		| --- | --- |
		| `/` | `projects/subroutine/ui` |
		| `/projects` | `subroutine/ui` |
		| `/projects/subroutine` | `ui` |
		| `/projects/subroutine/ui` | nothing |

		**The workspace leads when the address named none**, which is the agenda at `/`: it
		spans every workspace, so a bare `subroutine/ui` there would name a project in whichever
		one the reader assumed.

		**Slug form, lower case, one label** — not `Subroutine / Web UI`. A path made of titles
		has to invent a separator that reads as hierarchy, and it stops being the thing you can
		type back (`#151`). That supersedes `#912`, which chose the title for this chip when the
		chip was one segment and the argument was register rather than identity: `Web UI` and
		`Substation / Web UI` are not the same trade.

		**Nothing is dropped when every row agrees, and the terminal does drop it.** The reason
		is not taste and belongs here, where somebody will otherwise reach for consistency:

		> **A terminal listing is a snapshot and this page is live.** It is computed once and
		> read once, so a label derived from what is on it is stable by construction. This polls
		> every ten seconds (`#657`, `#781`), so dropping-if-uniform means a label can appear or
		> vanish while somebody is looking at it — because a stranger filed something in another
		> project — and these are clickable, which makes that a control moving under the cursor.

		`drop_if_uniform=False` is the same exception `#511` took for the assignee, one surface
		along.
	*/
	const path = (item && item.project_path) || "";

	if (!path) return "";

	const asked = (place && place.project) || "";
	const workspace = place && place.workspace ? "" : `${(item && item.workspace) || ""}`;

	if (!asked) {
		return workspace ? `${workspace}${PATH_SEPARATOR}${path}` : path;
	}

	/* Only on a segment boundary, for the reason `cli/personal._project_cell` gives: stripping
	   `ui` off `ui-things/x` leaves `-things/x`, which is not an address of anything. */
	if (path === asked) return "";

	const inside = `${asked}${PATH_SEPARATOR}`;

	return path.startsWith(inside) ? path.slice(inside.length) : path;
}

export function segment (raw) {
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

export function frame (showing, open) {
	/*
		How wide the page is, which is a question about what is on it — `#963`.

		**The board is the one view that wants the screen** (`#846`): 1100px is a reading
		measure, right for a list, a document and a form, and wrong for a board, where the useful
		thing is how many columns you can see at once.

		**And an open item is a document, whatever view the reader came from.** This asked
		`showing.view === "board"` alone until `#963`, and opening an item never clears the view
		— so a board, a click, and the item page arrived at the board's uncapped width. Simon met
		it as a page that came right when refreshed, because `/projects/subroutine/871` carries
		no `?view=` and the view falls back to the list.

		**`#863` is one step behind this, and its own comment names the gap.** That item moved
		`wide` off the board and onto the frame, writing *"this is on the frame, so it widens
		everything the frame holds — which the sentence above says is wrong for a form"*, and
		`.adding` was given the measure back. Nobody asked what happens when the frame holds a
		**document**, which is the thing in this app most obviously wanting one.

		**Not stale CSS**, which is the reading this was reported under and was measured before
		anything was written: `#914`'s assets answer `cache-control: no-cache` with an ETag and
		a `304` on a match, so the browser revalidates on every load. That a refresh *fixes* it
		is evidence against caching rather than for it — a refresh hits the same cache.

		Lifted out rather than written inline, which is `#640`'s cheapest route and the reason
		it is worth a function: every fault this app has shipped is a rule that was right with
		nothing joining it to the display, and `App` is the component no render harness can call.
	*/
	return showing && showing.view === "board" && !open ? "app wide" : "app";
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
		/* **The whole path, and until `#958` this was its last segment alone.** The sentence
		   that used to be here said the earlier segments were decoration *"because a project
		   key is unique in its workspace"* — which stopped being true when a key became unique
		   among its siblings, and `substation/dist` and `websites/dist` became two projects
		   with one last segment. `project` is what narrows a listing, so it has to be the thing
		   that names one. */
		project: middle.length > 0 ? middle.map(segment).join(PATH_SEPARATOR) : null,
		/* The same path as segments, kept because `#772` reads it that way — opening an item
		   must not throw away the tree the reader navigated. */
		trail: middle.map(segment),
		ref: names,
	};
}

/*
	The largest number a ref can be — `refs.MAX_REF`, which is a 32-bit signed maximum.

	Here so that `#99999999999999` falls through to an ordinary search rather than becoming a
	lookup the database refuses. `parse_ref` bounds it for the same reason on the other side.
*/
export const MAX_REF = 2147483647;

export function refAsked (text) {
	/*
		The item a search box is really asking for, or `null` — `#976`, Simon's.

		**The sigil is required, and a bare number is left as a search.** `#` is how a person
		writes a ref (§6.15) and it is the whole signal. A bare `916` is a plausible search term
		on any instance — `8471` is the port `docs/hosting.md` names, `403` and `404` appear
		throughout — and jumping on one would make those unfindable for as long as an item
		happened to hold that number. **The failure would be invisible in the direction that
		matters**: the reader gets an item, which looks like a search that worked rather than one
		that never ran.

		**Anchored at both ends**, so this is the query and not a word in it. `#916 dentist` is a
		search, because somebody who typed a second word wants both to count.

		The grammar is `refs._TYPED`'s and `parseAddress`'s: `[1-9][0-9]*`, no leading zero, so
		`#007` is a search here exactly as `subroutine show 007` is refused at a terminal. One
		rule about what a number means, on every surface.
	*/
	const match = /^#([1-9][0-9]*)$/.exec(String(text || "").trim());

	if (match === null) return null;

	const ref = Number(match[1]);

	return ref > MAX_REF ? null : ref;
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
