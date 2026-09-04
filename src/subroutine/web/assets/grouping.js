/*
	Rows into columns and buckets, and what a reader chose to collapse — including what is
	remembered in storage between visits.

	**Split out of `app.js` by `#1849`**, which moved 10,286 lines into thirteen modules and
	changed nothing about what any of them do. Read `app.js` for what the whole app is; this
	file is one layer of it.
*/


import { html } from "./html.js";
import { named } from "./dates.js";
import { HORIZON_DAYS, LINKS_SHOWN } from "./settings.js";

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

/* **The categories that mean the work is over**, shut unless the reader says otherwise —
   `#1008`, Simon 2026-08-18: *"they were cancelled or superseded for a reason, and need not
   take up screen real-estate all of the time."*

   **A constant rather than anything derived, and that is what makes it safe.** A default read
   off the rows would reshuffle the board under the reader every `POLL_MS` as items move —
   `#966` and decision `#957` §4's shape, which has bitten here three times. A default read off
   the *selection* is stable, and was the first version of this; it was wrong for a plainer
   reason, below.

   **`done` is deliberately absent.** Finished work is an achievement and is already a
   selection nobody has by default — so a reader looking at a *Done* column has asked for it,
   and answering that by hiding it is `#515`'s shape. It is also where *Show finished work*
   lives (`FINISHED`), which is the board's only route to that selection.

   **`superseded` and `archived` are here because nothing can ask them not to be.** `GET
   /v1/documents` refuses `include_completed` and `status_category` alike — measured, 422 on
   each — so every superseded document is in the response whether anybody wanted it or not.
   `#713` is the filter that would let somebody genuinely not ask, on every surface rather than
   only here; this should shrink when that lands rather than becoming a second way to say it. */
export const CLOSED_BY_DEFAULT = new Set(["cancelled", "superseded", "archived"]);

/* **What a column says when the selection left it out**, in one place because it is said
   twice — as a sentence in an open column and beside the heading of a shut one (`#1008`).
   Two spellings of one fact is this codebase's signature defect at its smallest scale, and
   `#742` is where the board established that this fact matters at all. The heading is
   `text-transform: uppercase`, so the case here is the source's rather than the reader's. */
export const NOT_SHOWN = "Not shown";

export function collapsedColumns (keys, chosen) {
	/*
		Decide which columns start collapsed — `#1008`.

		**An explicit choice always wins, in both directions.** `false` is a reader who opened
		something this would have closed, and it has to survive — otherwise the default
		reasserts itself on the next render and the control appears to do nothing. Only a key
		nobody has answered for takes a default.

		**An earlier version defaulted on *what the selection left out* and it was wrong twice.**
		It did not answer the complaint — Simon was looking at a board with `include_completed`
		on, so his *Cancelled* column held rows rather than a placeholder, and a rule about
		empty columns left it exactly as it was. And collapsing a *Not shown* column buries
		*Show finished work*, which is the only way to ask for finished work from a board;
		`test_a_board_column_nobody_asked_for_does_not_report_that_it_is_empty` caught that
		within the hour, having been written for `#738`.

		So an empty column is simply left open. It is cheap — a heading and one word — and it is
		information: `Nothing` under *To do* is the answer somebody wanted, and
		:func:`columns` already argues that an empty *In progress* reads as broken rather than
		as absent and is where you drag something to.

		Pure and given the keys rather than the columns, so the harness can drive it (`#640`).
	*/
	const wanted = chosen || {};

	return new Set(keys.filter((key) => (
		wanted[key] === undefined ? CLOSED_BY_DEFAULT.has(key) : wanted[key] === true
	)));
}

export function choicesIn (storage, key) {
	/*
		What this browser remembers under `key`, as `{ name: boolean }`.

		`localStorage` per `#908`'s theme precedent: browser-local, no API change, no migration,
		and no column on a `User` row that is more often an agent than a person (`#473`). A
		second data point for `#904`.

		**Anything unrecognised reads as nothing remembered.** A value written by an older
		version or by somebody poking at storage must not put the page into a state no control
		can get it out of — the reasoning is `themeChoice`'s and the failure would be worse here,
		because a wrongly collapsed column hides work.

		Takes the storage rather than reaching for it, because it throws in some privacy modes
		and the render harness runs in Node, where it may not exist at all.

		**Takes the key too, since `#1820`.** This was written for the board and a second reader
		wanted the same defensive parse for a different preference. Two copies of *anything
		unrecognised reads as nothing remembered* is this codebase's signature defect, and the
		second copy is the one that would be written without the reasoning above it — so the
		storage key is a parameter and there is no default, because a default is what lets a
		caller write to somebody else's preference by forgetting.
	*/
	try {
		const held = storage && storage.getItem(key);
		const read = held ? JSON.parse(held) : null;

		if (!read || typeof read !== "object" || Array.isArray(read)) return {};

		return Object.fromEntries(
			Object.entries(read).filter(([, value]) => typeof value === "boolean")
		);
	} catch (unreadable) {
		return {};
	}
}

export function rememberChoices (chosen, storage, key) {
	/*
		Write the reader's choices back under `key`, and return what was stored.

		Written whole rather than a name at a time, because the caller holds the whole map and a
		partial write is a second copy of it that can disagree. A storage failure is swallowed
		for `applyTheme`'s reason: not remembering is worse than not honouring, and only for the
		next load.
	*/
	try {
		if (storage) storage.setItem(key, JSON.stringify(chosen));
	} catch (unavailable) {
		/* A private window can refuse to remember. The choice still applies to this page. */
	}

	return chosen;
}


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
	**The sections, in the order a day is read.** Deliberately the same words `subroutine agenda`
	prints, because §12.2 already decided what the agenda says and one product answering one
	question two ways is worse than either answer.

	`Next 7 days` rather than `Upcoming` for the same reason: the CLI says the horizon out loud
	and a reader should not have to learn that two surfaces mean the same span.

	**That sentence was untrue in two places until `#927`'s H-15, and nothing compared them.**
	`in_progress` was missing outright, so an item somebody had started and given no date to
	appeared in `subroutine agenda`, in the agent's agenda, and on **no** browser surface — nor
	in its count. And the last section was still called `Unscheduled`, which the CLI renamed to
	`Next` when it started ranking rather than listing by capture order.

	`cli/personal.AGENDA_SECTIONS` is the list this must equal, and `tests/test_web.py` reads
	both. Add a section there and this fails until it is here.
*/
const BUCKETS = [
	/* **First, and it is Simon's decision of 2026-09-01** (`#1775`): work happening today is
	   not pushed down the page by what is parked on an answer. It takes dated rows from every
	   section it passed, because one list decides order and membership alike — but not from
	   `occasions` or `overdue`, each refused by a clause of its own in `domain/agenda.py`. */
	{ key: "today", label: "Today" },
	/* **Second, and it is Simon's decision of 2026-09-02** (`#1846`). It was fifth, under four
	   sections that between them are most of a screen: measured on the served instance, the
	   heading rendered at y=1157 on a 900px window and y=1942 on a phone, while the same row
	   on a workspace-scoped agenda was the first thing on the page. He reported the merged
	   agenda as missing it — it was below the fold, which is the same thing to a reader.

	   `domain/agenda.BUCKETS` carries the reasoning and the membership consequence; this list
	   only has to equal it, and `tests/test_web.py` compares the two. */
	{ key: "overdue", label: "Overdue" },
	/* **Was first, on Simon's decision of 2026-08-25** (`#1243`): *"I would naturally
	   complete a task before starting another."* Everything below this is a candidate to
	   begin; this is the only section that is already in hand. Since `#1846` a started task
	   whose deadline has passed is reported above, as late. */
	{ key: "in_progress", label: "In progress" },
	/* **Somebody is waiting on an answer**, so nothing under it can move until this is dealt
	   with — the only section that is not work the reader could pick up (`#1116`). It sat
	   above `overdue` until `#1846`. */
	{ key: "waiting", label: "Waiting on you" },
	/* **The pair with the one above it** (`#1285`, decision `#1267` §3). *Waiting on you* is a
	   question somebody parked for you; this is the reader's own work held up by an item
	   somebody else is assigned to. */
	{ key: "blocked_by_others", label: "Waiting on somebody else" },
	/* **What is happening to the reader rather than being done by them** — decision `#1235`
	   §4. A birthday, a booked fortnight, a code freeze: none of it is work anybody can pick
	   up, which is why it is a section of its own rather than part of *Today*. It sat above
	   *Today* until `#1775`; what keeps its rows now is that bucket's own `not is_occasion`
	   clause, which decision `#1235` §4 had already required for its own reason.

	   **"Happening" rather than "Happening today"**, matching the terminal: it is true of a
	   fortnight that began last week, where "today" would deny it. */
	{ key: "occasions", label: "Happening" },
	{ key: "upcoming", label: `Next ${HORIZON_DAYS} days` },
	{ key: "unscheduled", label: "Next" },
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

export function blockersDone (links) {
	/*
		How much of a milestone is left, as `#84` specified and `#210` built at the terminal.

		**Counted over incoming `blocks` alone**, which is `cli/personal`'s own rule and the
		reason is on it: a *relates to* has nothing to be N of, and counting every link printed
		`48 of 48` about an item with forty-eight outstanding blockers.

		**Nothing at all when there are no blockers**, rather than `0 of 0` — a rollup on an
		item that is not a milestone is a number a reader has to work out is meaningless.

		Returns the string because that is what the heading wants; there is no second caller,
		and inventing one would be `#303`'s shape.
	*/
	const held = (links || []).filter(
		/* **The category, never the key** (`#1157`). This compared `link_type` to the literal
		   `blocks`, which kept working while the behaviour behind it broke: a workspace that
		   renames the key keeps every label on this page and loses the count beside them
		   (`#1156`). `gating` is what the server calls a relation that holds work up. */
		(link) =>
			link.link_category === "gating"
			&& link.direction === "incoming"
			/* **A blocker in the trash is not one** (`#1403`). It held a milestone at `0 of 6`
			   with one of the six deleted, so the milestone could never reach 6 of 6 and
			   nothing on the page said why. `readiness.unblocked` has excluded a deleted
			   blocker since it was written; this is the count catching up with the rule, on
			   the third of the three surfaces that keep one. */
			&& !(link.other || {}).deleted_at
	);

	if (held.length === 0) return "";

	const done = held.filter((link) => link.other && link.other.is_complete).length;

	return `  (${done} of ${held.length} blockers done)`;
}

export function withinAllowance (rows, revealed, allowance = LINKS_SHOWN) {
	/*
		The rows of a section that are drawn, which is the front of the list or all of it.

		Pure and given the rows rather than the item, so the harness can drive it (`#640`) and so
		`#1143` can hand it backlinks — median 3, over five on 25% of items, maximum 36, which is
		twice this section's incidence and a longer tail. Whatever is built here has to be what
		that section uses; a shape hand-fitted to one heading would be a second copy of the rule
		by the time it landed.

		**The rows, and never also a count of the ones held back.** An earlier version returned
		both, and the count was read by nothing but its own test while :func:`Held` derived the
		same number a second way — two places one allowance is applied, which is what makes a
		control and its own subject disagree. `Held` is given what this returned instead.

		**Revealed shows everything, and there is no second allowance.** A reader who asked for
		the rest asked for all of it: a *Show 13 more* that reveals five is a control whose label
		is a lie the second time it is pressed.
	*/
	const all = rows || [];

	if (revealed || all.length <= allowance) return all;

	return all.slice(0, allowance);
}

export function Held ({ name, total, shown, revealed, onReveal, plural }) {
	/*
		The control that reveals a truncated section, or folds it back — `#1820`.

		**Nothing at all when nothing is held and nothing was revealed**, which is the state 88%
		of items are in. A control that says *Show 0 more* is §12.2a's column that says the same
		thing on every row, wearing a button.

		**The count is what keeps truncated from meaning blind**, which is Simon's own *"x linked
		items"* and `#1008`'s settled reasoning. Unlike the board's cap this one knows the number
		exactly — every row arrived and was counted here rather than left behind by a query — so
		it says *5 of 18* where the board can only say *there are more*.

		**Both numbers come from the rows themselves**, so the control cannot describe an
		allowance the list did not apply. `shown` is the length of what
		:func:`withinAllowance` returned rather than the allowance it was given — the two agree
		today and would part company the moment a section were handed a different one.

		**`shown === total` is not on its own the state that draws nothing**, which is the
		off-by-one waiting in this component: a revealed section shows all of its rows too, and
		the two are told apart by whether anybody asked.

		**`aria-expanded` on the button and `aria-controls` naming the list**, so a reader who
		cannot see the rows appear is told what changed. The id is the section's name rather than
		the item's, because two sections on one page must not claim one id and the same section
		never appears twice.

		**Both directions are remembered and `false` is the load-bearing one.** A reader who
		folds a section back has to have that survive the next poll, or the default reasserts
		itself and the control appears to do nothing.
	*/
	if (shown === total && !revealed) return null;

	const what = plural || `${name}s`;

	return html`
		<div class="cut">
			<span>${revealed
				? `Showing all ${total} ${what}.`
				: `Showing ${shown} of ${total} ${what}.`}</span>
			<button type="button" class="action"
				aria-expanded=${revealed ? "true" : "false"}
				aria-controls=${`section-${name}`}
				onClick=${() => onReveal(name, !revealed)}>
				${revealed ? "Show fewer" : `Show all ${total}`}</button>
		</div>
	`;
}

export function partsDone (parts) {
	/*
		How much of a parent is finished — `#1218`, and `#84`'s rule at the terminal.

		**Computed from the children rather than stored**, because a parent never
		auto-completes: that is a write nobody made, it credits the closer of the last child
		with a decision they did not take, and it cannot reverse when a child is added later.
		So `4 of 4` beside an open parent is the question being put to a person and must read
		as exactly that rather than as an error.

		**Counted over what arrived, and the cap is why that needs saying.** A parent with more
		parts than `MAX_PARTS` reports a count of what is drawn; the heading is not where that
		is disclosed, the line under the list is.

		**Nothing at all when there are no parts**, matching `blockersDone` — a rollup on an
		item that is not a parent is a number a reader has to work out is meaningless.
	*/
	const rows = (parts && parts.items) || [];

	if (rows.length === 0) return "";

	const done = rows.filter((row) => row.is_complete).length;

	return `  (${done} of ${rows.length} done)`;
}
