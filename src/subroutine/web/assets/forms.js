/*
	Everything a reader types into — the fields, the capture line, the editor — and the
	listing that carries the controls narrowing it.

	**Split out of `app.js` by `#1849`**, which moved 10,286 lines into thirteen modules and
	changed nothing about what any of them do. Read `app.js` for what the whole app is; this
	file is one layer of it.
*/

import { render } from "preact";
import { html } from "./html.js";
import { answers, frame } from "./address.js";
import { DEFAULT_ORDER, calendarDay, day, named, offeredOrders } from "./dates.js";
import { Written } from "./detail.js";
import { followed } from "./grouping.js";
import { Icon, when } from "./marks.js";
import {
	filableFor, notOffered, offered, prioritisedSentence, rankedByPriority,
} from "./places.js";
import { edited, fromItem, readForm, sent, written } from "./requests.js";
import { Row } from "./rows.js";

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

	`#769`: this said *Starts*, which is the one reading `snoozed_until` explicitly is not. Appendix
	A's ambiguity A4 asked whether it means *work starts then*, *hide until then* or *earliest
	permitted start*, and settled it as a **defer** — the task is not actionable before it and
	views hide it by default (§6.5).

	So the browser was a second copy of a vocabulary that disagreed with the first, on the one
	surface with no `explain` to check against: a terminal reader can ask and a browser reader
	has only the label. `cli/topics.py` is the original and a test holds these against it.

	**Left to right is chronological** — starts, then hidden until, then due. The middle one
	is the odd member and is meant to look it: two of these say when the work happens and one
	says when you want to be bothered about it.
*/
/*
	The three dates, and **which of them can carry a time** — `#798`.

	Simon, driving `#755`: *"My appointment starts at 14:00 and finishes at 15:00 but I cannot
	express that via the UI."* Every control was `<input type="date">`, which is a day and
	nothing else, on a product whose agenda has an appointment bucket.

	**The instance was never the limit.** `schedule.interpret` already reads both shapes and
	derives the flag from which it was given — measured: `2026-08-17` becomes the last instant
	of that day with `all_day` true, and `2026-08-17T14:00` becomes that minute with `all_day`
	false. So this is a control, and the `*_is_all_day` columns go on being written by the
	server from what it was sent.

	**All three carry a time since `#854`.** `starts` used to be the exception, because the
	column behind it was a bare `DATE` — and Simon read the missing time picker as an
	inconsistency, which it was. The model was the limit rather than the form, and the note
	here said so; the column is an instant now, so the exception is gone and an appointment
	starting at 14:00 can say so.
*/
export const DATE_FIELDS = [
	["starts", "Starts", "When it begins. This is what 'agenda' shows.", true],
	["snooze", "Hidden until", "A defer. The task does not appear at all before this.", true],
	["due", "Due", "A deadline. The date something has to be finished by.", true],
];

/*
	The fields whose control is a day *and* a time — read off `DATE_FIELDS` so the form and the
	request body cannot disagree about which is which (`#798`).

	**Declared here rather than beside `SAID_AS_WRITTEN`**, because a `const` initialiser runs
	where it is written: above the table it reads, this is the temporal dead zone and the whole
	app throws on import. `filed` and `edited` read it inside a function body, so they are free
	to sit anywhere. That is `#643` — a blank page from a declaration order — one module later.
*/
export const TIMED = DATE_FIELDS.filter(([, , , time]) => time).map(([name]) => name);

export function Fields ({
	busy, vocabulary, projects, members, project, values, reading, onReading,
	/* The prioritised project's address, so the dropdown can mark it (`#986`). */
	prioritised = null,
	/* Which prose box is being previewed and what it held when the button was pressed — one
	   answer for the whole page, in `App`, like every other piece of state here (`#776`). */
	previewing = null, onPreviewing = null, where = null,
}) {
	/*
		Every field beyond the one that names the item — shared by adding and editing (`#757`).

		**One block, because they are one form.** The item Simon wrote asks for *the same form*,
		and the alternative was two copies of thirteen controls whose only difference is what
		they start out holding. Two copies of one rule is this codebase's signature defect, and
		a control present on one and missing from the other would be invisible from either side.

		**What differs between the two is not here**: the line above (a capture box against a
		title), and what an empty control *means* on the way out. Creating omits it, because the
		endpoint refuses an empty string by name; editing sends `null`, because §8.3 says a field
		left out is unchanged and only an explicit null clears it. `filed` and `edited` are those
		two rules, both pure, and neither belongs in markup.

		**`defaultValue` rather than `value`, measured.** It renders as the `value` *attribute*,
		which is what an uncontrolled input reads once and then leaves alone — so a re-render
		while somebody is typing cannot reach in and reset what they have written. `#657` will
		make that a real event rather than a theoretical one: the page polls, and an item open on
		screen is about to start updating itself underneath an open form.
	*/
	const held = values || {};

	const day = ([name, label, hint, timed]) => html`
		<label key=${name}><span>${label}</span>
			<span class="when">
				<input type="date" name=${name} disabled=${busy}
					defaultValue=${held[name] || ""} />
				${timed && html`
					<input type="time" name=${`${name}_time`} disabled=${busy}
						aria-label=${`${label}, time`} defaultValue=${held[`${name}_time`] || ""} />
				`}
			</span>
			<small>${hint}</small></label>
	`;

	const rank = (name, label) => html`
		<label><span>${label}</span>
			<select name=${name} disabled=${busy}>
				<option value="" selected=${!held[name]}>—</option>
				${PRIORITIES.map((one) => html`
					<option key=${one.value} value=${one.value}
						selected=${String(held[name]) === String(one.value)}>${one.label}</option>
				`)}
			</select></label>
	`;

	const vocabularySelect = (name, label, options) => html`
		<label><span>${label}</span>
			<select name=${name} disabled=${busy || options.length === 0}>
				${options.map((one) => html`
					<option key=${one.key} value=${one.key}
						selected=${held[name] ? held[name] === one.key : one.chosen}>
						${one.label}</option>
				`)}
			</select></label>
	`;

	return html`
		<fieldset class="details">
			<legend>Everything else</legend>

			<${Written} name="description" label="Description" rows="3" busy=${busy}
				value=${held.description || ""} where=${where}
				previewing=${previewing} onPreviewing=${onPreviewing} />

			${/* **The Inbox is named rather than left blank**, because it is where an item with
			     no project goes — a blank option here would be a control whose effect the
			     reader has to already know. Which entry is chosen is `filableFor`, which is
			     pure: *the project defaults from the address* is a closing condition of `#756`,
			     and a claim nothing could check while it was an expression buried in markup. */ null}
			${vocabularySelect("project", "Project",
				filableFor(projects, held.project || project, prioritised))}

			${vocabularySelect("type", "Type", offered(vocabulary && vocabulary.item_types, "task"))}
			${/* **Narrowed to what this project offers** (`#1029`). The project the form is
			     filing into is what decides, which is why the whole address goes in — and it is
			     read live from `held`, so choosing a different project re-narrows the statuses
			     in the same render. `held.status` is what an item being edited is in now, and
			     is offered whatever the project says. */ null}
			${vocabularySelect("status", "Status", offered(
				vocabulary && vocabulary.statuses, "task",
				notOffered(projects, held.project || project), held.status
			))}

			<label><span>Assignee</span>
				<select name="assignee" disabled=${busy}>
					<option value="" selected=${!held.assignee}>Nobody</option>
					${(members || []).map((one) => html`
						<option key=${one.username} value=${one.username}
							selected=${held.assignee === one.username}>${one.label}</option>
					`)}
				</select></label>

			${rank("importance", "Importance")}
			${rank("urgency", "Urgency")}

			<label><span>Estimate</span>
				<input name="estimate" disabled=${busy} placeholder="2h, 90m, 1w2d"
					defaultValue=${held.estimate || ""} /></label>

			${/*
				**Every date is a day, and a time beside it where one means something** (`#798`).

				The all-day flag follows rather than being a control of its own. Measured:
				`due: "2026-08-14"` is stored as the end of that day with `due_is_all_day: true`,
				and `due: "2026-08-14T15:00"` is stored at 15:00 and not all-day — one field,
				both meanings, decided by what arrives.

				**Two controls rather than `datetime-local`**, and the reason is the one this
				comment used to give for having no time at all: that control forces a time on
				every deadline, and a person writing *by Friday* would have to invent one. A
				time input left empty is a day, which is the ordinary case unharmed.

				**And the sentence this replaces was wrong.** It said *a time of day is what the
				capture line is for* — `#797` measured that the capture line cannot read a time
				either, so the argument rested on a channel that does not exist. Simon found the
				consequence by trying to write down a dentist appointment.
			*/ null}
			${DATE_FIELDS.map(day)}

			<label class="wide"><span>Tags</span>
				<input name="tags" disabled=${busy} placeholder="health, admin"
					defaultValue=${held.tags || ""} /></label>

			<${Repeats} busy=${busy} held=${held} reading=${reading} onReading=${onReading} />
		</fieldset>
	`;
}

//: What the anchor offers, and the words for it. **Not read from `/v1/meta`**, unlike every
//: vocabulary control beside it — `schedule` and `completion` are this application's own
//: constants rather than a workspace's renameable keys (§5.5), so publishing them would be
//: inventing a vocabulary nobody can change. `#826` is the item if that ever stops being true.
export const ANCHORS = [
	["schedule", "The schedule"],
	["completion", "When it was last done"],
];

export function Repeats ({ busy, held, reading, onReading }) {
	/*
		How often something comes round — `#94`, and Simon's direction of 2026-08-16: *recurring
		events are not completed until a user can add and edit an item's recurrence via the Web
		UI*.

		**A disclosure inside a disclosure**, which is his suggested shape and is what §1.4 makes
		a form anyway. Most items do not repeat, so three more controls unfolded on every capture
		would be the page telling every reader about a feature almost none of them is using.

		**`<details>` rather than a checkbox and a rule about what it reveals.** It is a
		disclosure natively — keyboard-reachable, announced as one, and open or closed with no
		state anywhere. That matters more here than usual: a component calling a hook cannot be
		rendered by this project's harness (`#640`), and four faults have shipped out of
		decisions left inside `App` for want of that.

		**Open when the item already repeats.** A rule folded out of sight on the one item it
		applies to is the same failure as no control at all — somebody edits the deadline, saves,
		and cannot see that the thing they did not touch is still there.

		**The preview is the whole argument for the endpoint** (§6.7). *Every month on the 30th*
		and *every 30 days* are different schedules that read alike, and the difference does not
		show until February. Reading it back **in different words from the ones typed** is what
		turns an ambiguous natural-language feature into a checkable one; echoing the input would
		confirm nothing.
	*/
	const rule = (held || {}).recurrence || "";
	const anchor = (held || {}).recurrence_anchor || "";
	/*
		**What is in force, until somebody types** — `#925`. `reading` is the live answer and
		arrives only after a keystroke, so an item opened for editing showed its own phrase back
		and nothing else. Falling back to the stored sentence means the check is there the
		moment the disclosure opens, which is when a person is *reviewing* rather than writing —
		and reviewing is the case §6.7's read-back exists for.
	*/
	const said = reading || (rule && (held || {}).recurrence_description
		? { description: (held || {}).recurrence_description }
		: null);

	return html`
		<details class="repeats wide" open=${Boolean(rule)}>
			<summary>Repeats</summary>

			${/* **A wrapper, and it is not tidiness — measured** (`#94`). `display: grid` on a
			     `<details>` lays out the summary and then puts everything after it in one
			     anonymous slot box, so the fields are not grid items at all: every one of them
			     came out 219px wide inside a 938px row, stacked down the left, with the select
			     clipped and the preview wrapped. `::details-content` is the direct fix and is
			     too new to rely on. Nothing short of a browser could have found this — the
			     computed `grid-column` reads `1 / -1` on children that are not participating,
			     which is what makes the shim's answer look right. */ null}
			<div class="fields">
				<label class="wide"><span>How often</span>
					<input name="recurrence" disabled=${busy} defaultValue=${rule}
						placeholder="every other tuesday"
						onInput=${onReading && ((event) => onReading(event.target.value))} />
					<small>Leave it empty to stop repeating.</small></label>

				${/* **Measured from**, and the reason it is a control rather than a guess:
				     *every three days* means the third of every third day to somebody paying
				     rent and three days after you last did it to somebody watering plants,
				     and there is no way to tell those apart from the words. */ null}
				<label><span>Measured from</span>
					<select name="recurrence_anchor" disabled=${busy}>
						${ANCHORS.map(([value, label]) => html`
							<option key=${value} value=${value}
								selected=${anchor ? anchor === value : value === "schedule"}
								>${label}</option>
						`)}
					</select></label>

				<${Reading} reading=${said} />
			</div>
		</details>
	`;
}

export function Reading ({ reading }) {
	/*
		What the server made of the phrase — `#94`, §6.7.

		**Three states and they are not two.** Nothing typed yet says nothing at all; a phrase
		this cannot read says so and names the shapes that work, because a reader stuck on
		wording needs an example rather than a complaint; and a phrase it can read comes back as
		a sentence *and* the next few dates, which is the part that catches a rule that parses
		and means the wrong thing.

		**`role="status"` rather than `alert`.** This updates while somebody is typing, and an
		assertive live region would interrupt a screen reader on every keystroke — which is how
		a helpful thing becomes the reason somebody turns the page off.
	*/
	if (!reading) return null;

	if (reading.problem) {
		return html`
			<p class="reading bad" role="status">${reading.problem}</p>
		`;
	}

	return html`
		<p class="reading" role="status">
			<strong>${reading.description}</strong>
			${reading.occurrences && reading.occurrences.length > 0 && html`
				<span class="next">Next: ${reading.occurrences
					.slice(0, 3)
					.map((one) => calendarDay(one))
					.join(", ")}</span>
			`}
		</p>
	`;
}

/*
	**The only place a browser-only reader learns §6.13 exists** — `#484` settled that the
	capture grammar has exactly one delivery channel per surface, and a surface without one
	silently stops using it. At a terminal that is `subroutine explain capture`; here there is
	no terminal, so it is this placeholder or nothing.

	Named rather than written into the markup because it now shares its attribute with a
	ternary (`#761`), and a guard reading the first `placeholder="` in a component would find
	whichever branch happened to be written first.
*/
//: What the capture box suggests, and it is a line a new installation can actually run.
//:
//: **`+work` named a project nothing ships** (`SR#1545`). A fresh `init` gives one workspace and
//: one project — the Inbox — so the product's own first example was refused by the product,
//: which is a poor first screen however good the refusal is. `+inbox` is the one project that
//: is always there, so this succeeds verbatim; `tests/test_personal_path.py` drives it against
//: a real fresh install so it cannot go stale the way `+work` did.
//:
//: **A single key rather than a path, deliberately.** `capture.py`'s own refusal teaches the
//: same shape — *"a project is named like `+web`"* — as do the agent tool, `/v1/meta`'s
//: examples and the README. And a `+a/b` here would teach addressing inside the one box whose
//: workspace is chosen silently, which is `SR#1544`.
export const CAPTURE_HINT = "Add something — try: Book a dentist appointment tomorrow +inbox !4/3";

//: What the same box asks for when a document is being written. A title, not a captured line:
//: the grammar is deliberately not applied to it (`#761`).
export const DOCUMENT_HINT = "What did you conclude?";

export function DocumentFields ({
	busy, vocabulary, projects, project, values, prioritised = null,
	previewing = null, onPreviewing = null, where = null,
}) {
	/*
		A document's fields — `#761`. Deliberately not `Fields`.

		A document has a title, prose, a type and a status, and **none of a task's eight**. The
		body is the point of it, so the textarea is tall: a conclusion written into three rows
		is a conclusion nobody will write.

		**Type is not decoration here.** `#506` made `decision`, `finding` and `dead_end` start
		*in force* rather than at `draft`, because `subroutine://conventions` publishes what is
		in force — so a decision written at the wrong status is invisible to the one channel
		built to deliver it. Both controls read the workspace's own vocabulary and default to
		what it says, which is what keeps that right without teaching it here.

		The resource used to ask for `status=active` by name and does not since `#1036`: a
		status key is renameable where its category is not, so it derives the keys from this
		workspace's own vocabulary. Said here because this comment named the old spelling for
		three days after it stopped being true.
	*/
	const held = values || {};

	const pick = (name, label, options) => html`
		<label><span>${label}</span>
			<select name=${name} disabled=${busy || options.length === 0}>
				${options.map((one) => html`
					<option key=${one.key} value=${one.key}
						selected=${held[name] ? held[name] === one.key : one.chosen}>
						${one.label}</option>
				`)}
			</select></label>
	`;

	return html`
		<fieldset class="details">
			<legend>The document</legend>

			<${Written} name="body" label="What it says" rows="12" busy=${busy}
				value=${held.body || ""} placeholder="Markdown works, and #42 links."
				where=${where} previewing=${previewing} onPreviewing=${onPreviewing} />

			${pick("type", "Type", offered(vocabulary && vocabulary.item_types, "document"))}
			${/* Narrowed the same way a task's is (`#1029`) — a document's statuses are a
			     different vocabulary and the same project decides which of them are offered. */ null}
			${pick("status", "Status", offered(
				vocabulary && vocabulary.statuses, "document",
				notOffered(projects, held.project || project), held.status
			))}
			${pick("project", "Project",
				filableFor(projects, held.project || project, prioritised))}
		</fieldset>
	`;
}

export function Adding ({
	onAdd, busy, note, expanded, onExpand, vocabulary, projects, members, project,
	writing, onWriting, reading, onReading, prioritised = null,
	previewing = null, onPreviewing = null, where = null,
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

		/* **Cleared once the write has landed, never before it** (`#927`'s M-24). This reset
		   ran synchronously while the request was still in flight, so a 403, a 409, a 429 or a
		   dropped connection answered *"That was not added"* over a box that had already been
		   emptied — everything typed, gone, with nothing to retry from. `Conflict`'s own
		   comment below calls exactly that the worst possible answer. */
		Promise.resolve(onAdd(readForm(form), Boolean(writing))).then((landed) => {
			if (landed) form.reset();
		});
	};

	return html`
		<form class="adding" onSubmit=${submit}>
			<div class="line">
				${/* **The same box whichever kind is being written** (§1.4). A document's title
				     is a line of prose exactly as a task's is, and the capture grammar is
				     simply not applied to it — so the control does not move, change size or
				     acquire a second spelling for what somebody types into it. */ null}
				<input name="text" required disabled=${busy}
					aria-label=${writing ? "The document's title" : "Add an item"}
					placeholder=${writing ? DOCUMENT_HINT : CAPTURE_HINT} />
				<button type="submit" class="primary" disabled=${busy}
					>${writing ? "Write" : "Add"}</button>
				${onExpand && html`
					${/* **A reveal, and it draws the state it already declared** — design `#1045`.
					     `aria-expanded` has been here since this was written and nothing showed it,
					     so *More* looked exactly like *Cancel* and *Search*. The caret turns. */ null}
					<button type="button" class="more reveal"
						aria-expanded=${expanded ? "true" : "false"}
						onClick=${() => onExpand(!expanded)}
						>${expanded ? "Less" : "More"}<${Icon} name="caret-down" /></button>
				`}
			</div>

			${/* **The kind is a control inside the disclosure, not a third button beside the
			     box** (`#761`). A document is not what a to-do list is for, so it stays behind
			     the same *More* a task's other fields are behind — and the collapsed state,
			     which is the one §1.4 protects, gains nothing at all. */ null}
			${expanded && onWriting && html`
				<div class="kind">
					<label><span>Writing</span>
						<select disabled=${busy}
							onChange=${(event) => onWriting(event.target.value === "document")}>
							<option value="task" selected=${!writing}>A task</option>
							<option value="document" selected=${Boolean(writing)}
								>A document</option>
						</select></label>
				</div>
			`}

			${expanded && (writing
				? html`<${DocumentFields} busy=${busy} vocabulary=${vocabulary}
					projects=${projects} project=${project} prioritised=${prioritised}
					where=${where} previewing=${previewing} onPreviewing=${onPreviewing} />`
				: html`<${Fields} busy=${busy} vocabulary=${vocabulary}
					projects=${projects} members=${members} project=${project}
					prioritised=${prioritised}
					where=${where} previewing=${previewing} onPreviewing=${onPreviewing}
					reading=${reading} onReading=${onReading} />`)}

			${/* **Only where it is not obvious.** A listing is one workspace and saying so on
			     every page would be the column that says the same thing on every row (§12.2a);
			     the agenda spans them, so there the answer is worth a line (`#652`). */ null}
			${note && html`<span class="lands">${note}</span>`}
		</form>
	`;
}

export function Editing ({
	item, busy, onSave, onCancel, vocabulary, projects, members, conflict, reading, onReading,
	prioritised = null, previewing = null, onPreviewing = null, where = null,
}) {
	/*
		The same form, filled from an item — `#757`.

		**The title is an input here rather than a capture line.** Editing is not capturing:
		re-running §6.13's grammar over a title somebody is correcting would eat `!4/3` out of
		it and change two fields nobody touched. `Update` takes `title` and no `text`, which is
		the API saying the same thing.

		**`conflict` is the item as it now stands**, handed back by a 409 — see `Conflict` below.
		The form keeps everything typed into it, because discarding somebody's work to tell them
		their work could not be saved is the worst possible answer.
	*/
	const submit = (event) => {
		event.preventDefault();

		const form = event.currentTarget;

		if (form.elements.title.value.trim() === "" || busy) return;

		onSave(readForm(form));
	};

	return html`
		<form class="adding editing" onSubmit=${submit}>
			<div class="line">
				<input name="title" required disabled=${busy} aria-label="Title"
					defaultValue=${item.title} />
				<button type="submit" class="primary" disabled=${busy}>Save</button>
				${/* **Quiet, because it changes nothing** — design `#1045`. It wore `More`'s
				     look, in `More`'s position, doing the opposite of revealing anything. */ null}
				<button type="button" class="quiet" onClick=${onCancel}>Cancel</button>
			</div>

			${conflict && html`<${Conflict} theirs=${conflict} />`}

			${/*
				**Whichever form the item's kind wants** — `#1044`, Simon 2026-08-20. This was
				always `Fields`, so opening **Edit** on a document offered a task's eight fields
				and none of its own: an empty Description where its Body should be, and no way
				to reach the body at all. `Adding` six lines up has made this choice since
				`#761`; only the editing half never did.

				**And the empty box was the smaller half.** `written` clears a document's body
				when the form gives it nothing — which is right for an emptied control and is
				what a form with *no such control* also looks like — so pressing Save here sent
				`body: null` and wiped the document. It is guarded on both sides now: `written`
				only clears what it was given a control for.
			*/ null}
			${item.kind === "document"
				? html`<${DocumentFields} busy=${busy} vocabulary=${vocabulary}
					projects=${projects} values=${fromItem(item)} prioritised=${prioritised}
					where=${where} previewing=${previewing} onPreviewing=${onPreviewing} />`
				: html`<${Fields} busy=${busy} vocabulary=${vocabulary} projects=${projects}
					members=${members} values=${fromItem(item)} prioritised=${prioritised}
					where=${where} previewing=${previewing} onPreviewing=${onPreviewing}
					reading=${reading} onReading=${onReading} />`}
		</form>
	`;
}

export function Asking ({ what, onAnswer, onCancel, busy = false }) {
	/*
		Decision `#1249` §6: which occurrences a change is for, asked on save.

		**Simon offered a second Save button and it was rejected on three counts.** It doubles
		the primary action on every save, so neither is primary and the reader has to read both
		even to fix a typo; a button label cannot say what you changed; and nobody else does it,
		so there is nothing already learned to lean on. A control beside each field was rejected
		too — it keeps the decision next to the thing it is about, and it is easy to miss, which
		for a change that feels irreversible is worse than being stopped.

		**Two answers, not the three every calendar offers.** Google, Apple and Outlook need
		*this and following* because they compute every occurrence from the rule, so *all* would
		rewrite last March. Nothing here re-derives an occurrence somebody has finished, so
		*every one from now on* already means this one and every one after — and the words say
		exactly that rather than *all*, which would promise something that does not happen.

		**It names the item and not the change.** Naming the change is what decision `#1249` §6
		asked for and it is not free: this form sends every control it shows on every save, and
		telling what moved would mean comparing `2026-09-01` against the instant the server
		stored — a second copy of the server's own normalisation, living here. `#1276` is that,
		with the measurement.

		Inline and not a modal, which is the house rule `Note` states: news with something to do
		about it is a panel with buttons in it.
	*/
	return html`
		<div class="conflict asking" role="alert">
			<strong>This repeats.</strong>${" "}
			Does ${what} apply to just this one, or to every one from now on?
			${/* **Both answers wear `action` and neither wears `primary`** (design `#1046`,
			     decision `#1249` §6). Neither is *the* action: the whole argument against a
			     second Save button was that two primary controls mean neither is primary, and
			     making one of these the accented one would say the same thing in a quieter
			     voice — that the other is the unusual answer, when it is simply the other
			     answer. `Cancel` is `quiet` because it changes nothing, which is what that
			     role means. */ null}
			<div class="line">
				<button type="button" class="action" disabled=${busy}
					onClick=${() => onAnswer("this_one")}>Just this one</button>
				<button type="button" class="action" disabled=${busy}
					onClick=${() => onAnswer("from_now_on")}>Every one from now on</button>
				<button type="button" class="quiet" disabled=${busy}
					onClick=${onCancel}>Cancel</button>
			</div>
		</div>
	`;
}

export function Conflict ({ theirs }) {
	/*
		What a 409 means, said to a person — `#757`, §8.9.

		**Nothing was written**, which is the first thing to say: the refusal is the whole
		write, so the item is exactly as it was and nothing has been half-applied. The problem
		document says so too and this is where a reader will look.

		**Their title, because it is the one field a reader can recognise the item by.** The
		alternative — diffing every field and reporting what moved — reads as precision and is
		mostly noise: what somebody needs is *this changed under you, look before you overwrite
		it*, and then the item itself, which is one click behind the form.
	*/
	return html`
		<div class="conflict" role="alert">
			<strong>Somebody else saved this while you were editing.</strong>${" "}
			Nothing you typed has been lost and nothing was written. It now reads
			“${theirs.title}”. Look at it, fold in your change, and save again.
		</div>
	`;
}

export function Narrowed ({
	project, onWiden, widenTo, prioritised = [], onPrioritise = null, busy = false,
}) {
	/*
		What narrowed this page, how to undo it, and — since `#986` — whether this project is the
		one whose work is raised here.

		**One component because the list and the board had it twice, byte for byte.** Two copies
		of one rule is this codebase's signature defect, and it was harmless only for as long as
		the bar held one control; a second one would have been the moment they started to drift.

		**This is the control decision `#982` asks the browser for**, and it is here rather than
		beside every project name for the reason the mark is: the page is *about* this project,
		so this is where the question "should its work be raised?" is actually asked. Elsewhere a
		project is a destination or a place to file something, and a write control there would be
		a decision offered to somebody who came to do something else.

		**It says what it will displace before it does it**, which is the whole anti-spiral
		argument made visible: choosing this project is also the other one stopping, and a reader
		who is not shown the trade is the reader who sets a fifth one.
	*/
	if (!project) return null;

	const raised = prioritised.includes(project);
	const displaces = prioritised.find((one) => one !== project) || null;

	return html`
		<div class="narrowed">
			<span>Showing <strong>${project}</strong> and anything under it.</span>
			${onPrioritise && html`
				<button type="button" class="prioritise action" disabled=${busy}
					onClick=${() => onPrioritise(raised ? null : project)}
					title=${raised
						? "Stop raising this project's work"
						: displaces
							? `Raise this project's work — ${displaces} stops being the priority`
							: "Raise this project's work above the rest"}
					>${raised ? "Stop prioritising" : "Prioritise"}</button>
			`}
			${onWiden && (widenTo
				? html`<a class="widen" href=${widenTo}
					onClick=${(event) => followed(event, onWiden)}>Show everything</a>`
				: html`<button class="action" onClick=${onWiden}>Show everything</button>`)}
		</div>
	`;
}

export function Listing ({
	items, onOpen, onComplete, onAdd, onMore, onWiden, busy, more, project, workspace, widenTo,
	/* Where to send a reader who clicks a project label — `#959`. */
	onGo = null,
	empty = "Nothing here yet.", adding, ordering = null, order = null, onOrder = null,
	/* Which projects are prioritised, and how to change it — `#986`. */
	prioritised = [], onPrioritise = null,
}) {
	/*
		**The kind used to be dropped when a page held one of them** (§12.2a), and it is in the
		card's strip now — always, on both kinds — because `#1148` needed it where a reader
		could find it rather than where it happened to be news. The rule survives one field
		along: the *type* chip is silent about the default, so a page of plain tasks says
		`Task` once in a fixed place instead of `task` in a chip on every row.

		**`showKind` computed this per page and is gone with the branch it fed.** That branch
		sat behind an `else` that could not run, because every item has a type — which is the
		defect `#1148` reported.
	*/

	/*
		**Who has the work gets a column of its own, and it is *not* dropped when uniform**
		(`#1424`, design `#1422`).

		**Any assignee at all, rather than more than one.** §12.2a's drop-if-uniform rule
		collapses two opposite facts here — *nobody has been assigned any of this* and *one
		person has been assigned all of it* are both a single distinct value, and the second
		reads as the first. `#511` exists because delegation was invisible, so a rule that
		hid it again exactly when everything is delegated would rebuild that defect at its
		worst moment. `cli/personal` says the same thing as `drop_if_uniform=False`, and
		decision `#957` §4 names this column as the precedent for it.

		**So the two surfaces agree, and this item's own description said they could not.**
		It read `#957` §4 as refusing drop-if-uniform in the browser outright. §4 refuses it
		for the *project label*, whose reason is that the label is **clickable** — a control
		moving under the cursor while the page polls. The *status* chip above is silent when a
		column holds one status and is not a control, which is why both have always been true.
		`showKind` was this paragraph's example until `#1148` retired it.

		**Empty everywhere still costs nothing**, because there is no column at all then —
		which is §1.4 falling out of a layout rule rather than being enforced by one, and is
		what keeps a shopping list looking exactly as it did before assignment existed.
	*/
	const showAssignee = items.some((item) => item.assignee);

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

			${/*
				**How the list is ordered, said rather than left to be inferred** (`#661`).

				Simon: *"I'm sure the ordering is sensible, but if the view does not show me how
				it is ordered … it is unclear."* The order was never arbitrary — `#646` chose
				`-created_at` deliberately, over `-priority_score`, because a just-captured item
				has neither axis set and §6.3a therefore sorts it to the bottom of its own page.
				**A good default nobody is told about is indistinguishable from no order at all.**

				Above the rows rather than below them, because it is the frame a reader needs
				*before* reading the first one — and not on an empty page, where there is no
				order to describe and the sentence would be a claim about nothing.
			*/ null}
			${ordering && items.length > 0 && html`
				<div class="ordered">
					${onOrder
						? html`
							<label>
								<span>Order</span>
								<select value=${order || DEFAULT_ORDER} disabled=${busy}
									onChange=${(event) => onOrder(event.currentTarget.value)}>
									${offeredOrders().map(([key, one]) => html`
										<option value=${key} selected=${key === (order || DEFAULT_ORDER)}
											>${one.offer}</option>
									`)}
								</select>
							</label>
						`
						: html`<span>${ordering.sentence}</span>`}
					${/* **The sentence stays beside the control rather than being replaced by it.**
					     A select says what you may choose; it does not say what the page is
					     doing, and `#661` is about the second. It also carries the part a
					     control cannot — that a ranked page holds no documents. */ null}
					${onOrder && html`<span class="says">${ordering.sentence}</span>`}
				</div>
			`}

			${/*
				**Said only where it changes the answer** (`#986`). A prioritised project raises work
				inside a ranked order and does nothing to a page sorted newest-first, so a sentence
				over every listing would claim an effect the page is not showing — and a reader learns
				to ignore a line that is only sometimes true.
			*/ null}
			${rankedByPriority(order) && items.length > 0
				&& prioritisedSentence(prioritised) && html`
				<div class="focus">${prioritisedSentence(prioritised)}</div>
			`}

			<${Narrowed} project=${project} onWiden=${onWiden} widenTo=${widenTo}
				prioritised=${prioritised} onPrioritise=${onPrioritise} busy=${busy} />

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
							<${Row} key=${item.kind + item.ref} item=${item}
								workspace=${workspace} onOpen=${onOpen} ordering=${ordering}
								place=${{ workspace, project }} onGo=${onGo}
								showAssignee=${showAssignee}
								onComplete=${onComplete} />
						`)}
					</ul>
				`}

			${truncated && html`
				<div class="cut">
					<span>Showing ${items.length}. There are more.</span>
					${onMore && html`
						<button class="action" onClick=${onMore} disabled=${busy}>Show more</button>
					`}
				</div>
			`}
		</div>
	`;
}
