/*
	A day, an instant and whether either has passed — the readiness predicates a row is
	marked by, and the orderings a listing offers.

	**Split out of `app.js` by `#1849`**, which moved 10,286 lines into thirteen modules and
	changed nothing about what any of them do. Read `app.js` for what the whole app is; this
	file is one layer of it.
*/

import { render } from "preact";
import { moment } from "./marks.js";
import { timeFor, written } from "./requests.js";

//: A date with no time in it, which is a calendar day rather than an instant.
const CALENDAR_DAY = /^\d{4}-\d{2}-\d{2}$/;

export function calendarDay (value, zone = null) {
	/*
		The calendar day a value falls on, as ``YYYY-MM-DD`` — `#773`.

		**A day-scale fact must be read in the timezone that stored it, not in the reader's.**
		§6.5 stores an all-day deadline at the *last* instant of its day in the task's own
		timezone, so `2026-08-14T23:59:59.999999Z` is *Friday the 14th* to the task and *Saturday
		the 15th* to a browser in London. Measured against `#589` on the served instance: the
		terminal said `(due Fri 14 Aug)` and the browser said 15 Aug, about one item, at one
		moment.

		It is wrong in both directions and for different fields, which is why the fix is one
		function rather than one adjustment: an all-day *deadline* is stored at the end of the
		day, so a reader **east** of the task sees the next one; an all-day *start* is stored at
		the beginning, so a reader **west** sees the previous one.

		**A bare `YYYY-MM-DD` is returned untouched, and that is not an optimisation.**
		`starts_at` is a calendar date with no instant behind it at all, and `new Date(
		"2026-08-13")` parses it as UTC midnight — so rendering it anywhere west of UTC moves it
		to the 12th. Not parsing it is the only way to be exactly right.

		**Seasonal, which is why nothing noticed.** London is UTC in winter and UTC+1 in summer,
		so this is correct half the year — `#532`'s shape, where CI in UTC cannot see it.
	*/
	if (!value) return null;

	const written = String(value);

	if (CALENDAR_DAY.test(written)) return written;

	const parts = Object.fromEntries(
		new Intl.DateTimeFormat("en-US", {
			timeZone: zone || "UTC", year: "numeric", month: "2-digit", day: "2-digit",
		}).formatToParts(new Date(written)).map((one) => [one.type, one.value]),
	);

	return `${parts.year}-${parts.month}-${parts.day}`;
}

export function day (value, zone = null, allDay = true) {
	/*
		A date in the reader's own locale, because this is the one surface where the machine
		knows what that is.

		**`zone` is the timezone that stored the value**, and passing it is what makes the day
		right — see `calendarDay`. Omitting it is correct for a genuine instant like
		`updated_at`, where the question really is *when was this, where I am*.

		**`allDay` is the item's own answer and `false` is what adds the time** — `#864`. This
		said "time is dropped: everything shown here is a day-scale fact", which stopped being
		true when `#797` taught the capture grammar to read `at 14:00`: Simon captured *Dentist
		on Monday at 14:00*, the grammar stored it, and this rendered *Starts 17 Aug 2026*. A
		field a person can write and cannot read back is `#515`'s shape.

		**Read rather than inferred**, which is `timeFor`'s argument and it holds here for the
		same reason: an appointment at midnight and a deadline meaning *the end of that day* are
		the same instant in some zones, so looking at the clock and guessing would print `00:00`
		against every ordinary deadline. The default is `true` so that a caller which has no
		such flag — `updated_at`, `completed_at`, `starts_at`, which is a date and has no time
		to show — is unchanged and cannot accidentally acquire one.
	*/
	if (!value) return null;

	const [year, month, date] = calendarDay(value, zone).split("-").map(Number);

	/*
		**Formatted from the parts rather than from the original value**, so the day cannot move
		a second time on the way out. A `Date` built this way is local midnight of exactly the
		day `calendarDay` decided on, and `toLocaleDateString` with no `timeZone` then renders
		that day whatever the reader's offset is.
	*/
	const shown = new Date(year, month - 1, date).toLocaleDateString(undefined, {
		day: "numeric",
		month: "short",
		year: "numeric",
	});
	/* `timeFor` is the one place that decides whether a stored value carries a time, and it is
	   already what fills the form's time box. Reusing it is what stops the fact sheet and the
	   form disagreeing about whether an item has an o'clock. */
	const at = timeFor(value, allDay, zone);

	return at ? `${shown}, ${at}` : shown;
}

export function overdue (item, now = null) {
	/* **`now` for the reason `holding`, `moment` and `when` already take one** (`#950`): a mark
	   read off the clock is only as fresh as the last render, so a test that cannot move the
	   clock can only assert what the mark says *at this instant* — which is the one thing that
	   was never in doubt. Null is the real clock, so every caller is unchanged. */
	if (!item.due_at || item.status_category === "done") return false;

	return new Date(item.due_at) < (now === null ? new Date() : new Date(now));
}

export function deferred (item, now = null) {
	/*
		Whether this item has been put off until a moment that has not arrived — `#862`.

		**A board showed deferred work looking exactly like work nobody had parked**, while
		`subroutine list` hid it — so the two surfaces disagreed about items somebody had
		deliberately set aside, and the board's reader had no way to tell. Simon's decision of
		2026-08-14 is that they are **marked rather than hidden**: *"that way they are not
		invisible, but neither are they confused with non-deferred items"*.

		**Computed here rather than published as a field, unlike `blocked`**, and the difference
		is what each needs. `blocked` reads the link graph, which the browser does not have, so
		it can only arrive as data. This needs `snoozed_until` and a clock — and the row carries
		`snoozed_until` anyway, because the mark says *when*. A published boolean would also be
		**stale**: it would be computed when the page was fetched and would go on saying
		"deferred" after the moment passed, on a page a reader leaves open.

		`overdue` is the same shape reading the other end of the same clock, which is why this
		sits beside it.

		**`readiness.undeferred` is the server's spelling** and the two agree by construction:
		null is not deferred, and an instant that has passed is not deferred.
	*/
	if (!item.snoozed_until) return false;

	return new Date(item.snoozed_until) > (now === null ? new Date() : new Date(now));
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
export const FINISHED = new Set(["done", "cancelled"]);

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

export function named (username, isAgent = false, answersTo = null, sigil = "@") {
	/*
		How one principal is written wherever this page names one — `#1414`.

		**The twin of `views.principal_named`, and the duplication is unavoidable**: this is the
		one renderer that is not in Python, so the rule cannot be shared the way `views.py`
		shares it between the two clients. `tests/test_assignee_surfaces.py` is what holds the
		two to the same answer, which is `#1266`'s arrangement extended rather than a new one.

		    @morpheus                         a person
		    @claude-super (agent, @morpheus)  an agent, and who answers for it
		    @claude-super (agent)             an agent whose chain does not reach a person

		**The word, not a glyph** — `#102`, and the roster below already made this call in
		writing. An icon may sit beside this and never instead of it.

		**`sigil` is the caller's** (`#1420`). On a row `@morpheus` sits beside `#ops` and a project
		path, and the sigil is what makes three addresses tell themselves apart (`#1019`). In a
		control whose every option is an account it distinguishes nothing, and a marker on every
		row of a list is §12.2a's column that says the same thing everywhere. **The accountable
		person keeps its `@` either way**, because that one is a reference to a *different*
		account inside the text rather than the label's own opening.
	*/
	if (!username) return "";

	const who = `${sigil}${username}`;

	if (!isAgent) return who;

	return answersTo ? `${who} (agent, @${answersTo})` : `${who} (agent)`;
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

export const DEFAULT_ORDER = "-created_at";

/* What `?order=` calls the deferral band — `domain.ordering.DEFERRED`. Ascending, always, so
   it is spelled without a sign: work that can be started first, and work somebody has put off
   last. Not an entry in `ORDERINGS` and not a value `SELECTABLE.order` admits, because it is
   never a reader's choice — `sunkOrder` puts it in front of whatever they did choose. */
export const DEFERRED = "deferred";

/*
	How each order this app can be in reads, and whether the row already shows what it is
	sorted on — `#661`.

	**Simon, 2026-08-08**: *"if the view does not show me how it is ordered, or show the values
	of the fields on which it is ordered, it is unclear."* Two faults, and this is both answers
	in one place so they cannot drift: the sentence a listing prints, and whether a row needs
	the value adding.

	**`already` is what stops the value being said twice.** The finished view is ordered on
	`completed_at` and `when` has printed it since `#706` — with a time since `#746`, for exactly
	this reason — so a second copy under the title would be noise. The default order has no such
	cell, so it gets a mark.

	**Keyed by what the address carries**, which is `SELECTABLE.order`'s vocabulary, so an order
	that becomes reachable without a sentence here is a listing that cannot say how it is
	ordered — which `tests/test_web.py` refuses.
*/
export const ORDERINGS = {
	"-relevance": {
		/*
			**The server's choice, not a reader's** — `#823`, `#875`. A search defaults to its
			own ranking wherever a backend can compute one, so this exists for `inOrder` to
			merge on and is deliberately **absent from `SELECTABLE.order`**: it is not
			addressable, not offered, and means nothing without a search. `offer: null` is the
			same answer `-completed_at` already gives.

			`render: "none"` because a score is not something to print. A reader wants the best
			match at the top, not a number telling them it is.
		*/
		sentence: "Best match first", offer: null, field: "relevance",
		shows: "relevance", render: "none", label: "",
		compare: "number", descending: true, both: true,
		/* **A search does not sink deferred work, and `#867` is why** (`#877`). A ranking
		   answers *how well does this row match*, and an item somebody has put off is still
		   the best answer to it — typing a number finds that item, and sinking would put it
		   below every row that merely mentions the digits. */
		sinks: false,
	},
	"-created_at": {
		sentence: "Newest first", offer: "Newest first", field: "created_at",
		shows: "created_at", render: "moment", label: "written",
		compare: "instant", descending: true, both: true, sinks: true,
	},
	"created_at": {
		sentence: "Oldest first", offer: "Oldest first", field: "created_at",
		shows: "created_at", render: "moment", label: "written",
		compare: "instant", descending: false, both: true, sinks: true,
	},
	"title": {
		/* **The row shows the title already**, so `render` is nothing at all. An ordering whose
		   field is the row's own headline is the one case where saying the value would be
		   printing the same string twice on one line. */
		sentence: "A to Z", offer: "A to Z", field: "title",
		shows: "title", render: "none", label: "",
		compare: "text", descending: false, both: true, sinks: true,
	},
	"-updated_at": {
		sentence: "Recently changed first", offer: "Recently changed", field: "updated_at",
		shows: "updated_at", render: "moment", label: "changed",
		compare: "instant", descending: true, both: true, sinks: true,
	},
	"-priority_score": {
		/*
			**Tasks only, and that is Simon's decision of 2026-08-10** (`#782`). A document has
			no importance and no urgency to be ordered by, so a merged list cannot be put in one
			priority order at all. Of the three answers — drop them, sink them, or do not offer
			priority — dropping is the one that needs no new machinery: `collectionsFor` already
			drops documents from a selection they cannot answer, which is how the finished view
			is tasks-only today.

			Sinking them was defensible from §6.3a's own three-band rule and was refused because
			it renders as the defect `#660` fixed — *"I saw it is some way down the list"* — and
			a rule a reader must learn to tell it from a bug reads as a bug.

			**`!i/u` rather than the score**, because that is what this product calls it
			everywhere else; `priority_score` is `importance * urgency` and a bare 20 says
			nothing a reader can act on.
		*/
		sentence: "Most important first, and documents have no importance",
		offer: "Most important", field: "priority_score",
		shows: "importance,urgency", render: "priority", label: "",
		compare: "number", descending: true, both: false, sinks: true,
	},
	"-completed_at": {
		/* Not offered as a choice: it is the *finished* view's order, reached by the chip that
		   also narrows to finished work. Offering it beside the rest would be an ordering that
		   silently changes which rows there are, which decision `#649` forbids. */
		sentence: "Most recently finished first", offer: null, field: "completed_at",
		shows: "completed_at", render: "none", label: "finished",
		compare: "instant", descending: true, both: false,
		/* **Finished work is not waiting for anything.** A defer says *not yet*, and a task
		   that is done has answered that question — so sinking one here would arrange a page
		   by a decision that has already been overtaken. */
		sinks: false,
	},
};

export function orderingValue (ordering, item) {
	/*
		What a row shows of the field the page is sorted on, or nothing.

		**`render` is a name rather than a function** so that `ORDERINGS` stays a table a guard
		can read. A function here would make the whole thing opaque to
		`tests/test_web.py`, which derives from it what the request must ask for — and a table
		that cannot be read is a table nothing checks.

		`none` is for an ordering the row already carries: `title` is the headline and
		`completed_at` is printed by `when` (`#706`, `#746`).
	*/
	if (!ordering) return null;

	if (ordering.render === "priority") {
		return item.importance && item.urgency ? `!${item.importance}/${item.urgency}` : null;
	}

	if (ordering.render === "moment") {
		return item[ordering.field] ? `${ordering.label} ${moment(item[ordering.field])}` : null;
	}

	/* **`none` lands here, and so does anything unrecognised** — deliberately, because saying
	   nothing is the safe half. What stops an entry quietly showing nothing through a typo is
	   `tests/test_web.py`, which fails a `render` this function does not handle. A runtime
	   fallback cannot tell *the row already shows it* from *somebody misspelled it*. */
	return null;
}

export function offeredOrders () {
	/*
		The orders a reader may choose, in the order they are offered — `#782`.

		**Derived from `ORDERINGS` rather than listed beside it**, because a second list is what
		this codebase gets wrong most: two copies agree until one of them is edited. `offer` is
		null for an order that exists and is not a choice — the finished view's, which is
		reached by the chip that also narrows to finished work, and offering it as an ordering
		would be an ordering that silently changes which rows there are (decision `#649`).
	*/
	return Object.entries(ORDERINGS).filter(([, one]) => one.offer);
}

export function orderedAs (selection) {
	/*
		How the list a selection asks for is ordered.

		**The absence of `order` is the default rather than nothing** — `listingRequests` sends
		none and the API applies `-created_at` (`ordering.DEFAULT_TASK_ORDER`), so a page with no
		`order` in its address is not unordered, it is ordered by something the reader was never
		told. That is the whole of `#661`.

		Null for an order nobody wrote a sentence for, so a caller says nothing rather than
		inventing a claim about how the rows are arranged.
	*/
	const asked = (selection || {}).order || DEFAULT_ORDER;

	return ORDERINGS[asked] || null;
}
