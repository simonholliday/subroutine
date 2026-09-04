/*
	The small labels under a title, and the pictures beside them. `#102` is the rule the
	whole module answers to: nothing is said in a colour or a shape alone.

	**Split out of `app.js` by `#1849`**, which moved 10,286 lines into thirteen modules and
	changed nothing about what any of them do. Read `app.js` for what the whole app is; this
	file is one layer of it.
*/

import * as phosphor from "./phosphor.js";
import { html } from "./html.js";
import { encodedPath, projectLabel, withShowing } from "./address.js";
import { day, deferred, holding, named, orderingValue, overdue } from "./dates.js";
import { repeats } from "./requests.js";

/*
	**Which glyph an item type gets, and the answer for one this client has never heard of.**

	`#764`'s requirement is Simon's fifth: *conventional iconography so a bug and a document are
	distinguishable without clicking*. The constraint that shapes it is `#441`'s: **item types
	are workspace data**, so this cannot be the vocabulary — it is one client's opinion about a
	vocabulary the server owns, and it has to be wrong gracefully.

	**The fallback is the important half and is why it is first here.** `#826` measured that no
	surface can add or rename a type today, so mapping the seeded keys is correct *now* — and the
	moment `#826` goes the other way it stops being correct **silently**, with a new type
	rendering as whatever this falls back to and nobody finding out from a failure. So the
	unrecognised path is the one to get right, and `circle-dashed` is a glyph that reads as
	*something, unspecified* rather than as a mistake.

	**`is_system` is deliberately not consulted**, which reverses what `#524` and `#827`
	recommended: it says *we seeded this* and not *what this is*, so a workspace that renamed
	`bug` to `defect` would publish `is_system: true` and this would still not know which icon to
	draw. `#906` records the whole argument and `#826` inherits it — the field that would work is
	a classifier on `ItemType`, modelled on `Status.category`.

	**That field exists now**, and `CATEGORY_ICONS` below is what it buys: decision `#1133`, built
	as `#1134`. The paragraph above stands as written — this map is still one client's opinion,
	and the keys it names are still renameable — but the silent-wrongness it warns about is what
	the category answers. A type this has never heard of now draws by what *kind* of thing it is.
*/
/* The picture of a *kind*, which is a coarser question than the type below and the one a
   reader asks first — `#1148`. Two entries and there will only ever be two: `linkable_types`
   is `["task", "document"]` and a kind is the model's own division rather than a workspace's
   vocabulary, so this cannot grow the way `TYPE_ICONS` can.

   **`check-square` is `CATEGORY_ICONS.work`'s glyph and that repetition is deliberate**: the
   product already draws work that way, and a second picture for the same idea would be a
   second thing for a reader to learn.

   **The strip made that repetition visible and then made it load-bearing**, in two steps on
   one afternoon. This paragraph first argued that a `bug` card carrying two glyphs was *two
   facts in two places rather than one fact twice* — true while one sat in the strip and the
   other in a chip a line below. `#2026` put them side by side and dropped the second wherever
   it repeated the first. `#2032` put it back, because the glyph became the separator between
   the two words and a delimiter cannot be conditional.

   So a `spec` draws `file-text` twice, deliberately, and **this map owes the strip nothing**:
   an unknown type under `work` or `reference` resolving to its own kind's picture is now
   simply how the strip looks rather than a case anybody handles. */
export const KIND_ICONS = {
	task: "check-square",
	document: "file-text",
};

export const TYPE_ICONS = {
	task: "check-square",
	bug: "bug",
	feature: "sparkle",
	chore: "broom",
	spike: "flask",
	event: "calendar-dots",
	note: "note",
	spec: "file-text",
	design: "compass-tool",
	decision: "gavel",
	finding: "magnifying-glass",
	dead_end: "prohibit",
};

/*
	**What to draw for a type this client has never heard of** — decision `#1133`'s six
	categories, `#1134`'s column, and the middle step of the chain in `marks`.

	**Every one of these is an existing glyph, and that is the design rather than a shortcut.**
	A category's picture is the picture of the type that represents it: a workspace that invents
	`epic` under `work` gets `check-square`, the same mark `task` carries. It reads as *this is
	work, and I do not know more*, which is exactly true — and `#102` is what makes it safe,
	because nothing here is information only in a glyph: the word beside it says `epic`.

	Six new glyphs was the other option and is worse on every axis. It would vendor bytes for
	pictures nobody has seen, invent a visual vocabulary for categories that Simon has not
	chosen, and give an unknown `work` type a *different* mark from `task` — which says the two
	are different kinds of thing when the whole claim of the category is that they are the same
	kind.
*/
export const CATEGORY_ICONS = {
	work: "check-square",
	defect: "bug",
	question: "flask",
	/* The picture of the type that represents it, which is this map's own rule: a workspace
	   that adds `holiday` under `occasion` through `#1129` gets what `event` carries, and reads
	   as *this is something that happens, and I do not know more*. */
	occasion: "calendar-dots",
	decision: "gavel",
	reference: "file-text",
	record: "note",
};

/* The two ends of a `blocks` link (`#913`, Simon's suggestion on `#911`). A lock for work
   something else is holding shut, and a key for the item that opens it — which is `#861`'s
   stated intent, that the blocker is *the item you should pick* rather than a thing to warn
   about. A stop-hand or a warning triangle would say the opposite. */
/*
	A glyph per *kind* of mark, keyed on what the mark is rather than on what it says.

	**It was keyed on the rendered text** (`#1019`), looked up as `MARK_ICONS[mark.text]` at
	draw time — so rewording `Blocked` would have dropped its picture in silence, and
	`Deferred to Fri 21 Aug` could never have had one at all because its words vary by item.
	This change reworded two of the three keys it held, which is how that came up.

	**Still a constant rather than a string beside each push**, because
	`test_every_glyph_named_is_one_that_was_vendored` scans it: a name with no path data draws
	nothing and says nothing, which is `#925`'s recorded defect and was found with a typo.
*/
/*
	The status key that says a row is waiting for a person to answer something (`#1383`).

	**The twin of `domain.agenda.WAITING_STATUS`, and the duplication is unavoidable**: this is
	the one renderer that is not in Python. `tests/test_web.py` holds the two to one key, which
	is `BLOCKED_MARK`'s arrangement extended rather than a new one.

	**A key rather than a category, which nothing else here reads.** `#96` refused a fifth
	status category on the grounds that the distinction that matters is *who ends the wait* — a
	`blocks` link resolves itself where this needs a person — so there is no category to ask
	for. `views.waiting_on_a_person` carries the argument and what a rename costs.
*/
export const WAITING_STATUS = "needs_input";

export const MARK_ICONS = {
	blocked: "lock-simple",
	blocker: "key",
	repeats: "repeat",
	/* **Who the name on a row belongs to** (`#1421`, design `#1422`). Not a *state* — an
	   assignee is an address, and these ride the address mark rather than making it a chip:
	   `#1019`'s families are what stop a boxed `@si` reading as the same kind of thing as a
	   boxed status, which is the confusion those families were introduced to end. */
	person: "user",
	agent: "robot",
};

/* What an item type this client does not recognise is drawn as. */
export const UNKNOWN_ICON = "circle-dashed";

export function Icon ({ name, decorative = true }) {
	/*
		One glyph, drawn from the vendored path data.

		**`aria-hidden` by default, and that is `#102` rather than laziness.** No information may
		exist only in a glyph, so every icon here sits beside the word it illustrates — and an
		icon announced *as well as* its label makes a screen reader say everything twice. A caller
		with a glyph genuinely standing alone passes `decorative=false`, and nothing does yet.

		**An unknown name draws nothing rather than throwing.** A missing glyph should cost a
		reader a picture, never the page — and the mapping above is one client's opinion about a
		vocabulary somebody else owns, so being handed a name this does not have is a normal
		event and not an error.
	*/
	const path = phosphor.PATHS[name];

	if (!path) return null;

	return html`
		<svg class="icon" viewBox=${phosphor.VIEWBOX} fill="currentColor"
			aria-hidden=${decorative ? "true" : null} focusable="false">
			<path d=${path} />
		</svg>
	`;
}

export function marks (
	item, ordering = null, place = null, linkable = false,
	/*
		**Named rather than positional, and that is a defect being removed rather than a
		preference.** `c107538` took `showKind` out of this list and shifted every argument
		after it at **three** call sites — the item page, its sub-tasks and its links — each
		silently moving `ordering` into `place`. Nothing type-checks this file, so four tests
		caught it and reported symptoms that read like unrelated defects.

		A key this does not know is ignored; a key spelled wrongly is `undefined`, which is
		falsey and therefore fails the same way a missing argument would. Neither is as bad as
		a value arriving in the wrong parameter and meaning something else.

		- `hideStatus` — the caller has a column saying it already.
		- `hideAssignee` — **the list draws who has this as a column of its own** (`#1424`), so
		  a chip here would be the same fact twice on the one surface that aligns it. Absent
		  means draw it, which is the board, the agenda and an item's own links.
		- `hideType` — the caller draws a `Stamp`, which says the type in a fixed place.
	*/
	options = {}
) {
	const { hideStatus = false, hideAssignee = false, hideType = false } = options;

	/*
		The small labels under a title.

		**Every one says a word.** `#102`: colour marks an exception and never carries the
		information by itself, so "overdue" is red *and* reads "overdue" — a reader who cannot
		separate the hues loses nothing at all.

		**One of them is the value the page is sorted on** (`#661`), when the row does not
		already carry it. It is here rather than in a column of its own because it is the same
		kind of thing as the rest — a small fact about this row — and because a column would be
		empty on every page whose ordering field is already shown.
	*/
	const found = [];

	/*
		**What it is, rather than what shape it has** (`#764`). This said `Task` or `Document`,
		which is the *kind* — true, and not what Simon's fifth requirement asks: *a bug and a
		document are distinguishable without clicking*. Every seeded type has a glyph and an
		unrecognised one still gets a chip, because `#102` says nothing may be information only
		in a glyph and the word is what carries it either way.

		**The label comes from the item, not from a table here.** A workspace renames its types
		(§5.5), so the text is whatever the server said; only the picture is this client's
		opinion, and it degrades on its own.

		**Silent about the default, because the strip above already said it** (`#1148`). Every
		card now carries *Task* or *Document* in a fixed place, so a chip reading `task` one
		line below is the same word twice on the commonest card in the product — §12.2a's rule,
		and Simon's own words when he took the decision. A `bug`, a `decision`, a `finding` is
		still news and still says so.

		**This replaced a dead branch rather than adding a rule.** What was here drew *Task* or
		*Document* when a page held both kinds — behind an `else` that could not run, because
		every item has a type. `#1148` is that branch's own bug report; the strip is where the
		fact went, and `showKind` went with it.
	*/
	if (!hideType && item.type && !item.type_is_default) {
		found.push({
			text: item.type,
			family: "identity",
			/* **Key, then category, then unknown** — decision `#1133`'s chain, `#1134`'s
			   column. The first step keeps today's eleven glyphs exactly as they were; the
			   second is what a type this client has never heard of gets, and the third is
			   what a *category* it has never heard of gets, since the server may grow one. */
			icon: TYPE_ICONS[item.type]
				|| CATEGORY_ICONS[item.type_category]
				|| UNKNOWN_ICON,
		});
	}

	/*
		**Before everything else, because it is why the row is where it is.** A reader checking
		an order reads down one edge; putting the value after the project and the assignee would
		make it land in a different place on every line.

		`moment` rather than `day` for the reason `#746` gave about the finished view: a column
		of dates rendered a day at a time reads as one value for a whole screen, and an order
		nobody can check is an order taken on trust.
	*/
	const sorted = orderingValue(ordering, item);

	if (sorted) found.push({ text: sorted, family: "context" });

	/*
		**Gathered rather than pushed** (`#1019`), because the status chip below has to know
		what they say before it decides whether to speak. The seeded status `blocked` means
		*declared, often outside the system* (`#96`) and the derived state means *an
		unfinished blocker in the graph* (`#425`) — two facts, one word, and a card carrying
		both read `Blocked Blocked`.
	*/
	const states = [];

	/*
		**Somebody has to answer something before this can move** — `#1383`, Simon's instruction
		of 2026-08-27: *"If you need input from me before you can continue, you should assign to
		me, I should clearly be able to see it ASAP."*

		**A promotion rather than a new fact.** The status chip below already drew this, in the
		`identity` family, as the raw key `needs_input` — the same weight as any other status and
		reading as a column name rather than as a sentence. What was missing is that this one is
		an *exception*: `#1116` puts it above `overdue` on the agenda because you owe somebody an
		answer, which is a commitment unkept in exactly the way a passed deadline is. So it takes
		`late`'s tone, and the chip below falls silent because a state mark now carries the word.

		**First, above `Blocked`**, which is the agenda's own order (`BUCKETS` puts *Waiting on
		you* above *Waiting on somebody else*): a row can be both, and *answer this* is the more
		actionable of the two because nobody can act on the task until the question is settled.

		**By key, and `views.waiting_on_a_person` is where that is argued** — `#96` refused a
		fifth status category, so there is no category to ask for. A workspace renaming the key
		loses this mark and loses the agenda's bucket together, which is one cost rather than a
		disagreement between two surfaces.

		**No glyph, unlike `Blocked` and `Blocker`.** Nothing vendored says *a question parked
		for a person* — `flask` is taken, as the picture of the `question` type category — and
		`#102` makes a glyph reinforcement rather than information, so the word carries this on
		its own exactly as `Deferred` does.
	*/
	if (item.status === WAITING_STATUS) {
		states.push({ text: "Needs input", family: "state", tone: "late" });
	}

	if (item.blocked) {
		states.push({ text: "Blocked", family: "state", tone: "blocked", icon: MARK_ICONS.blocked });
	}
	/*
		**And the other end of it** (`#861`, which is `#569` reaching the surface it was
		reported from). `#569` began with an agent reading a *board*: the urgent item carried
		`Blocked` and the five-minute errand actually holding it up carried nothing, so the one
		thing worth starting looked like the least important row on the page. The terminal and
		the agent's row were given the mark and this was missed.

		**Both marks rather than a precedence**, unlike `subroutine list`, and the difference is
		the medium: the terminal has one cell, so `#569` had to choose, and it chose `blocked`
		because that is the fact deciding whether you can act. A card has room for two, and a
		row mid-chain genuinely is both.

		**`quiet`, not `blocked`.** §12.2's colour rule is that colour marks an exception, and a
		warning tone here would say *something is wrong with this item* about the row that is
		holding everything else up — which is the opposite of what it means. It is the item you
		should pick.
	*/
	/*
		**`Blocker`, not `Holds up` and not `Blocks`** (`#913`, Simon). A mark has no object, so
		a verb asks a question the card is built not to answer — `#569` settled that a listing
		says *that* an item blocks something and `show` says *what*. A noun naming this item's
		role is complete standing alone, and beside `Blocked` it is role-and-state rather than
		two inflections of one verb.

		It was `Holds up`, which read nothing like the `Blocks` on the item it opened. `#569`'s
		own argument against a word this close to `blocked` is on `cli/personal.BLOCKING_MARK`
		and still stands; what outweighed it is that one relationship had two names.
	*/
	/*
		**A chip like every other state, where it used to be a borderless caption** (`#1019`,
		Simon). `quiet` was chosen to keep a *warning* tone off the item you should pick, and
		that argument is untouched — the outline is what says *this is a state*, and the
		colour is still reserved for the two that are problems.
	*/
	if (item.blocking) {
		states.push({ text: "Blocker", family: "state", icon: MARK_ICONS.blocker });
	}
	/*
		**`Sub-tasks done`, and a card commonly carries it beside `Blocker`** (`#1615`). A
		parent whose sub-tasks are all finished is the row `readiness.a_container` deliberately
		leaves startable, because `#84` refuses to complete a parent on somebody's behalf — and
		nothing anywhere was putting the question that refusal creates.

		**The fact, not the consequence.** `Finishable` or `Ready to close` would answer the
		question rather than ask it, which is the decision `#84` declines to take for a person.
		What is true is that the sub-tasks are done.

		**No tone**, deliberately. `blocked` and `late` are the two reserved for problems, and
		this is the opposite — it is the row worth looking at because it is nearly over. The
		outline is what says *this is a state*, which is `#1019`'s arrangement.
	*/
	if (item.sub_tasks_done) {
		states.push({ text: "Sub-tasks done", family: "state" });
	}
	/*
		**That it comes back at all** — `#925`, Simon: *"nothing indicates that it is a repeating
		task"*. The terminal's row has said so since the day the CLI learned about repeats and
		the agent's since `#922`; this was the third rendering of one fact and the only one
		silent about it, on the surface `#755` made a person's primary one.

		**A short mark here and the whole sentence on the item page**, which is his split and is
		right for the medium: a card is narrow and is scanned, so *does this come back* is the
		question a row is being asked, where *how* is what somebody opens it to check. The full
		parsed description is one click away in `Facts`.

		**A glyph beside the word, like the two `blocks` marks** — and it is the fifteenth of
		1,512, vendored from Phosphor's own source rather than drawn from memory. `#102` is what
		decides the order of that sentence: the word carries the information and the picture is
		what makes it findable at a glance, so a reader who cannot see the glyph loses nothing.
	*/
	if (item.recurrence_description) {
		states.push({ text: "Repeats", family: "state", icon: MARK_ICONS.repeats });
	}
	/*
		**`quiet`, not `late`** (`#862`). A deferred item is not a problem — it is a decision
		somebody made, and the mark exists so the reader can see the decision rather than be
		warned about it. §12.2's rule is that colour marks an *exception*.

		**It says when**, because *deferred* without a date is a fact the reader can do nothing
		with: the question a parked item raises is *when does this come back*.
	*/
	if (deferred(item)) {
		states.push({
			text: `Deferred to ${day(item.snoozed_until, item.timezone, item.snoozed_is_all_day)}`,
			family: "state",
		});
	}
	if (overdue(item)) {
		states.push({
			/* The flag, for `when`'s reason two functions down (`#1298`): the same deadline
			   drawn by two renderers must not answer differently about whether it has a time. */
			text: `Overdue ${day(item.due_at, item.timezone, item.due_is_all_day)}`,
			family: "state",
			tone: "late",
		});
	}

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

	/*
		**A live lease only, and the expired one is gone** — `#1019`, Simon: *"we should lose
		labels which represent a past event and not a property"*.

		**This reverses `#726`'s explicit choice** rather than tidying it away, so the argument
		it loses is written here: an expired claim is *started and walked away from*, which
		`holding`'s own comment calls the thing a person watching agents work most wants to
		see. What outweighed it is that a chip reads as a property of the item, and *left it*
		is an event — the record of which is the item's history, where an event belongs.

		**It also ends a divergence.** `mcp/tools.py` reads `views.holder`, which applies the
		clock and returns nobody, so the agent has never shown an expired lease. The browser
		was the only surface that did.

		**`claimed by`, matching the `claim` and `release` verbs**, and `@` because a username
		is a person — the sigil the agent's row already uses and quick capture already reads.
	*/
	if (lease && lease.held) {
		states.push({
			text: lease.who ? `claimed by @${lease.who}` : "Claimed",
			family: "state",
			tone: "claimed",
		});
	}

	/* **Where it lives, and clicking it narrows the view to that path** — decision `#957` §4.
	   `href` rather than a handler because it *is* an address: `#649` puts the place on the
	   path, so the thing this control does is go there, and a link is what a reader can open in
	   a tab, copy or middle-click. */
	const label = projectLabel(item, place);
	const home = (item.workspace || (place && place.workspace)) || "";

	const address = [];

	if (label) {
		address.push({
			text: label,
			/* **No sigil, deliberately** (`#1019`, Simon). A project is the only mark that is
			   an address, and `#ops` and `@si` beside it carry theirs — so a bare word is
			   already the third distinguishable thing, and the collision this was written for
			   (a sub-project sharing a tag's name) is settled by the *tag's* sigil.
			   **Simon's stated reason expires**: he gave it as *the only linked item without a
			   sigil*, and `#1020` will make tags and assignees links too. The decision holds on
			   the reading above, which does not depend on what is clickable. */
			family: "address",
			/* **A link only where somebody is listening**, which is `#251`'s rule: a surface
			   that cannot navigate renders the label and no anchor, rather than an anchor whose
			   only outcome is a page that has not moved. */
			href: linkable && home
				? `/${encodeURIComponent(home)}/${encodedPath(item.project_path)}`
				: null,
		});
	}

	/*
		**Tags reach a row for the first time** (`#1019`). `marks` never read them — they went
		to the item page's fact sheet and nowhere else — so a label somebody applied was
		invisible on every listing, board and agenda since tags existed.

		**`#` is quick capture's own sigil**, so the chip reads as the thing a person types.
		Measured before deciding whether to cap them: four is the most any item here carries,
		so there is no overflow to design.
	*/
	/*
		**Narrowing to one of them is a query on the place, not a path** — `#1020`, and `#649`
		decides it with nothing left to judge: the path says which rows there are and the query
		says how they are shown, and neither a tag nor a person is a *place*. So both of these
		reach the workspace they live in, carrying the narrowing, where the project chip above
		reaches a path.

		**`withShowing` rather than a string**, because it is the one place that knows how a
		selection is written and emits it in `SELECTABLE` order — two writers of that string
		would be `#651`'s four all over again, and the second one always drops something.

		**A link only where somebody is listening**, exactly as the project chip has it: a
		surface that cannot navigate renders the word and no anchor (`#251`).
	*/
	const narrowing = (selection) => (linkable && home
		? withShowing(`/${encodeURIComponent(home)}`, { selection })
		: null);

	for (const tag of item.tags || []) {
		address.push({ text: `#${tag}`, family: "address", href: narrowing({ tag }) });
	}

	if (item.assignee && !hideAssignee) {
		address.push({
			text: named(item.assignee, item.assignee_is_agent, item.assignee_answers_to),
			family: "address",
			/* **The account, not what the reader sees** — `named` puts *(agent)* and who
			   answers for it into the text, and none of that is what the endpoint takes. */
			href: narrowing({ assignee: item.assignee }),
			/* **The glyph reinforces the word and never replaces it** — `#102` as the
			   stylesheet states it: *nothing may be said in colour alone, and nothing may be
			   said in a shape alone either*. `named` above has already put *(agent)* in the
			   text, so a reader in monochrome, with images off, or through a screen reader
			   loses the picture and no information — which is what `Icon`'s `aria-hidden`
			   default is for.

			   **Both kinds, because the scanning problem is a difference and not a presence**
			   (`#1422`). Marking only agents makes the *absence* of a mark carry the other
			   half, and an absence does not catch an eye on a page of fifty rows. */
			icon: item.assignee_is_agent ? MARK_ICONS.agent : MARK_ICONS.person,
		});
	}

	/*
		**The status, and the two things that silence it** — `#1019`, both Simon's.

		**One**: where the column a card sits in already says it. That is the caller's to know,
		because only a board has columns — and it is not computed from the page's contents.
		§12.2a's drop-if-uniform was refused for the browser by decision `#957` §4, because the
		page polls and a chip vanishing under the reader is `#966`'s shape.

		**Two**: where a state mark already carries the word. `blocked` is a seeded status *and*
		a derived state, so a card could say it twice with two meanings. The status is the
		workspace's own vocabulary and is not ours to rename (§5.5), so the state keeps the word
		— and this generalises for nothing extra, to a workspace that renames one `Deferred`.

		**Compared case-blind on the key**, which is what the view sends: the status arrives as
		`blocked` and the mark says `Blocked`.
	*/
	const said = new Set(states.map((mark) => mark.text.toLowerCase().replace(/[_ ]/g, " ")));
	const status = item.status && !item.status_is_default && !hideStatus
		&& !said.has(String(item.status).toLowerCase().replace(/[_ ]/g, " "))
		? [{ text: item.status, family: "identity" }]
		: [];

	/*
		**Assembled rather than pushed in place**, so the order is one statement: what it is,
		why it is here, what is true of it now, where it lives.

		The ordering value stays second and that is `#661`'s alignment argument — a reader
		checking an order reads down one edge, so it must land in the same place on every line.
		Putting the status before it would move it whenever a status happened to show.
	*/
	return [...found, ...status, ...states, ...address];
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

		**The finished stamp always carries a time; a deadline and a planned day carry one when
		the row says they have one.** `#746` read *"both stay days: a time on one would be
		precision the writer never supplied"*, which was true while nothing could supply it —
		and `#797` taught the capture grammar `at 14:00`, so `#864` gave the start its flag and
		left the deadline behind. The two lines below then disagreed about one question, on one
		row (`#1298`). The stamp is different in kind: the program made it, it is exact, and it
		is what the page is ordered on.
	*/
	if (item.completed_at) {
		return `${item.status_category === "cancelled" ? "cancelled" : "done"} `
			+ `${moment(item.completed_at, now)}`;
	}

	if (item.due_at && !overdue(item))
		return `due ${day(item.due_at, item.timezone, item.due_is_all_day)}`;
	if (item.starts_at)
		return `→ ${day(item.starts_at, item.timezone, item.starts_is_all_day)}`;

	return null;
}

/* ---- the board (`#653`) -------------------------------------------------- */
