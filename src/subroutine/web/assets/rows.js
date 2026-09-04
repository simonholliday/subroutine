/*
	One item as a line, and the two arrangements built from lines: the agenda's buckets and
	the board's columns.

	**Split out of `app.js` by `#1849`**, which moved 10,286 lines into thirteen modules and
	changed nothing about what any of them do. Read `app.js` for what the whole app is; this
	file is one layer of it.
*/

import { render } from "preact";
import { html } from "./html.js";
import { addressOf } from "./address.js";
import { FINISHED, completable, day, deferred, excluded, holding, named } from "./dates.js";
import { Adding, Narrowed, Ordered, Whose } from "./forms.js";
import { NOT_SHOWN, collapsedColumns, columns, followed } from "./grouping.js";
import {
	CATEGORY_ICONS, Icon, KIND_ICONS, MARK_ICONS, TYPE_ICONS, UNKNOWN_ICON, marks, moment,
	when,
} from "./marks.js";
import { prioritisedSentence, soleStatusIn } from "./places.js";
import { filed, written } from "./requests.js";

export function Marks ({ badges, onGo = null }) {
	/*
		What `marks` decided, drawn — `#970`.

		**Its own component because there are two callers now.** A row on a list, a board or the
		agenda has said these things since `#906`; an item's *links* said none of them, so a
		reader checking whether a milestone was ready had to open every blocker in turn. Simon:
		*"I cannot look at a task and see whether all of its blockers are complete, without
		looking at each blocker individually."*

		**Lifting it out is what makes the two the same rather than two that agree.** Copying
		twenty lines of markup into the links list would have been a second set of class names
		to keep in step with one stylesheet, which is this codebase's signature defect and is
		exactly how four renderings of a link line came to disagree (`#583`, `#674`). The rule
		lives in `marks`, the drawing lives here, and both surfaces call both.

		**Nothing is rendered for an item with nothing to say**, which is §12.2a's rule that an
		empty column says nothing, applied to a line.
	*/
	if (!badges || badges.length === 0) return null;

	/* **Two classes, and they answer different questions** (`#1019`). `family` is what kind of
	   fact this is — identity, state, address, context — and decides the shape; `tone` is
	   `#102`'s exception colour and is set on three marks only. Keeping them apart is what
	   stopped the stylesheet being a set of tones that happened to look different. */

	return html`
		<span class="marks">
			${badges.map((mark) => (mark.href && onGo
				/* A mark that is an address is an `<a>`, so it can be opened in a tab and read
				   by a screen reader as the link it is. Everything else stays a `<span>`: a
				   control that only looks like one is `#251`'s shape — and that now includes a
				   caller with no `onGo`, since `marks` only offers an `href` when it was told
				   somebody is listening. Both halves are checked because they are two
				   decisions, taken in two places. */
				? html`
					<a class="mark ${mark.family || ""} ${mark.tone || ""}" href=${mark.href}
						onClick=${(event) => followed(event, () => onGo(mark.href))}>
						<${Icon} name=${mark.icon} />${" "}${mark.text}
					</a>
				`
				: html`
					<span class="mark ${mark.family || ""} ${mark.tone || ""}">
						<${Icon} name=${mark.icon} />${" "}${mark.text}
					</span>
				`))}
		</span>
	`;
}

/*
	**The card's identity, in a fixed place: a glyph, the number, what kind of thing it is,
	and what sort of thing it is** — `#1148` for the first three, `#2026` for the fourth, and
	both orders are Simon's.

	The kind glyph is a fixed width, so the ref begins at the same x on every card whatever
	its number, and everything else follows it. **The picture is what the eye catches and the
	word beside it is what carries the meaning** — `#102` in full, which forbids saying
	anything in a shape alone as firmly as it forbids saying it in a colour.

	**The kind is drawn always, rather than only on documents.** A mark drawn on the exception
	alone leaves work identified by an *absence*, and a reader cannot be asked to read a
	blank; two glyphs that differ is two presences. It costs a line on every card and that was
	taken knowingly — `#1148` measures it.

	**It moves the ref off the title's line, and on a board that is the larger half.**
	`.board .row` is `display: block`, so the ref had been flowing inline ahead of the title:
	measured on a 327px card, a title beginning after a short ref started at x=57 and one that
	wrapped started at x=12. The title has the card to itself, from one edge.

	**A component rather than markup inside `Row`, because two surfaces draw it.** An item's
	own page had `<h2>#42 Title</h2>` — the ref inline before the title, which is exactly the
	arrangement `#1148` moved off the board card and never came back for. `#1019`'s rule is
	that a row and the page a reader lands on from it say the same things; that now includes
	this strip.
*/
export function Stamp ({ item, where = "" }) {
	const kind = KIND_ICONS[item.kind] || KIND_ICONS.task;

	/*
		**Silent about a default type, because the kind beside it already said it** — `#1148`'s
		rule, and worse here than where it was found: a plain task would read `TASK · TASK`
		with one word's gap rather than one line's.
	*/
	const typed = item.type && !item.type_is_default;

	/*
		**Key, then category, then unknown** — decision `#1133`'s chain, `#1134`'s column, and
		the same resolution the chip used before this strip took the fact over.
	*/
	const type = typed
		? TYPE_ICONS[item.type] || CATEGORY_ICONS[item.type_category] || UNKNOWN_ICON
		: null;

	return html`
		<span class="stamp">
			<${Icon} name=${kind} />
			<span class="ref">${where}#${item.ref}</span>
			<span class="stamp-kind">${item.kind === "document" ? "Document" : "Task"}</span>
			${typed && html`
				${/*
					**The glyph is what separates the two words, so it is drawn always** — `#2032`,
					Simon's, and it replaces a middot that stood for one hour.

					`#2026` shipped a `·` between them and dropped the type's glyph wherever it would
					repeat the kind's, arguing that *the type adds no picture beyond what the kind
					already said*. Two things were wrong with that. The middot took its colour from
					`--line`, the hairline-border token, and was invisible in both themes. And the
					argument held only while the glyph had one job: here it has a second — *here comes
					the type* — and **a delimiter that appears conditionally is not a delimiter.**
					Dropped on a `spec`, whose picture is `document`'s own, the strip read
					`DOCUMENT SPEC`: two uppercase words with nothing between them, parsing as one
					phrase rather than as two facts.

					**So a `spec` draws `file-text` twice and that is the design.** Photographed at
					340px board width in both themes before it was decided: it barely registers,
					because the two words differ and the second glyph reads as punctuation that
					happens to carry a fact. `#102` is untouched either way — the information was
					never in the picture, and the word beside it is what carries it.
				*/ null}
				<span class="stamp-type">
					<${Icon} name=${type} />
					<span class="stamp-kind">${item.type}</span>
				</span>
			`}
		</span>
	`;
}

export function Row ({
	item, showWhere, workspace, onOpen, onComplete, ordering = null, onDrag = null,
	/* **What the address already said**, so the project label can leave it out — decision
	   `#957` §4. Absent means the address named nothing, which is the agenda at `/`. */
	place = null,
	/* Where to go when a label is clicked. Absent renders the label as a plain span rather
	   than a link that does nothing, which is `#251`'s shape. */
	onGo = null,
	/* **What is holding this row up, where the caller asked for it** (`#1383`). Empty
	   everywhere but the agenda's *Waiting on somebody else* section — see `holding` below for
	   why this is a parameter rather than a field read. */
	waitingOn = [],
	/* **Whether the container already says the status** (`#1019`) — true only on a board, and
	   only for a column whose category holds one status. A row cannot work this out: a list and
	   an agenda have no columns, and the answer is about the workspace's vocabulary rather than
	   about this item. */
	hideStatus = false,
	/*
		**Whether the container has given who-has-this a column of its own** (`#1424`).

		**The listing decides, because the answer is about the page rather than about this
		item** — which is now the only prop of that shape. `showKind` was the other and `#1148`
		retired it: the kind is in the card's strip on every row, so nothing computes it
		per page any more.

		**A fixed track is the only thing that aligns.** Every row is its own grid, so
		`max-content` is computed per row and would put the cell at a different x on each one
		— which is the defect this item exists to fix, rebuilt in CSS.
	*/
	showAssignee = false,
}) {
	/* `ordering` is the list's, and only the list has one: the agenda's rows are in buckets and
	   the board's are in columns, so neither is *ordered by* a field a reader could check. */
	const badges = marks(item, ordering, place, !!onGo, {
		hideStatus,
		/* `showAssignee` is *this row draws the column*, so the chip is the duplicate. */
		hideAssignee: showAssignee,
		/* The strip above says it, in a fixed place — `#2026`. */
		hideType: true,
	});

	/*
		**Draggable only where something can receive it** (`#711`), which is the board. A card
		that lifts on a page with nowhere to drop it is a control whose only outcome is putting
		it back — this project's own inert-control defect, in the one place a reader would feel
		it rather than read about it.

		The ref goes in the transfer rather than the item: a drop handler reads a string, and
		the column it lands in has the row already. `text/plain` because every browser carries
		it and a private type buys nothing when the two ends are one page.
	*/
	const lift = onDrag
		? {
			draggable: true,
			onDragStart: (event) => {
				event.dataTransfer.setData("text/plain", String(item.ref));
				event.dataTransfer.effectAllowed = "move";
				onDrag(item);
			},
		}
		: {};

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
	/*
		**Two lines with fixed jobs: what this item *is*, then what is true of it** (`#911`).

		The identity line is the address and the title, and the title has the rest of the width.
		The line under it holds every property and then the actions, in that order, on every
		surface — so a reader scanning a column finds a title where the last one started and a
		control where the last one was.

		**Splitting them is geometry rather than taste.** A button cannot be nested in a button,
		so *Complete* has to be a sibling of the row — and a sibling laid out beside it takes
		width down the card's whole height, which is what wrapped four titles of four on Simon's
		board while the space beside the button stood empty. For the action to sit *under* the
		title it has to be on a different line from it, and for it to share that line with the
		chips the chips cannot be inside the anchor.

		**The cost, stated rather than discovered**: the chips and the date are no longer part of
		the link, so clicking one does not open the item. The identity line spans the card and
		wraps to as many lines as the title needs, so the target is larger than it was in the
		direction that matters. A stretched-link overlay would restore the whole card and is
		refused: `#906` requires a ref to be selectable and copyable everywhere it appears, and
		an overlay is exactly what stops text being selected.
	*/
	const date = when(item);
	const acting = completable(item) && onComplete;

	/*
		**Who has this, as a cell rather than as a chip** (`#1424`, design `#1422`).

		**The fact was already on the row and could not be scanned.** A chip sits in a flow
		after however many marks precede it, so down fifty rows it begins at fifty different
		x-positions. Nothing was missing from the data; the geometry is what failed — which is
		why the terminal, whose `_assignee_cell` is a column, never had this problem.

		**The word carries it and the glyph reinforces it** (`#102`, `#1421`). `named` has
		already put *(agent)* and who answers for it into the text, so a reader in monochrome,
		with images off, or through a screen reader loses the picture and no information.

		**On the identity line rather than among the properties**, because that is what the
		claim is: who has this is part of what the row *is*, not a small fact about it. It is
		also the only line that is already a grid.
	*/
	const holder = showAssignee && item.assignee
		? html`<span class="assignee"><${Icon} name=${item.assignee_is_agent ? MARK_ICONS.agent : MARK_ICONS.person} />${" "}${named(item.assignee, item.assignee_is_agent, item.assignee_answers_to)}</span>`
		: null;

	const stamp = html`<${Stamp} item=${item} where=${where} />`;

	const identity = html`
		${stamp}
		<span class="title">${item.title}</span>
		${holder}
	`;

	/* **Nothing is rendered for an item with nothing to say**, which keeps a plain row one line
	   — §12.2a's rule that an empty column says nothing, applied to a line instead of a column. */
	const meta = (badges.length > 0 || date || acting) && html`
		<div class="meta">
			<${Marks} badges=${badges} onGo=${onGo} />
			${date && html`<span class="when">${date}</span>`}
			${acting && html`
				<button class="finish action" onClick=${() => onComplete(item)}
					aria-label=${`Complete #${item.ref}, ${item.title}`}
					><${Icon} name="check" />Complete</button>
			`}
		</div>
	`;

	/* **The `<li>` carries the gesture, not the anchor inside it.** A draggable anchor is
	   draggable by the browser already — dragging one is *copy this link* — so putting the
	   handler there would make one gesture mean two things depending on where the pointer went
	   down. The row is the card; the card is what moves. */
	/*
		**The colour of the project this belongs to** (`#1027`), as a name the stylesheet maps to
		a hue — never a value, so the same field can be rendered by a surface that draws no
		colour at all, or by none.

		**Resolved on the server** (`#925`): a project's own, or the nearest ancestor's, or its
		workspace's, or nothing. The browser could walk `project_path` itself and would then hold
		a copy of the inheritance rule, in three surfaces.

		**Absent rather than empty when nothing up the tree has chosen one**, so the CSS matches
		on the attribute existing and a plain row keeps its full width — an edge of a transparent
		colour still takes its three pixels and would shift every uncoloured row.
	*/
	const hue = item.project_colour || null;

	/* **Every row on the page carries this, including the ones with nobody on them.** The
	   track is what aligns, so a row that dropped it would put its title where the others
	   put their titles and their holder — which is the ragged edge this replaces. */
	const shape = showAssignee ? "with-assignee" : "";

	/*
		**What is holding this row up, as a third line of the row** — `#1383`, and Simon's
		report of 2026-08-31 on the second attempt at it.

		**It was a sibling `<li>` and that was the defect.** Written outside the row to avoid
		touching a component the list and the board also use — and *avoiding the shared
		component is what produced it*: a line between two rows, carrying neither one's colour
		bar and separated from neither by anything, so there was no way to tell which item it
		belonged to. `#911` had already settled the shape and I did not read it: a row is *what
		this item is*, then *what is true of it*. This is a third line of the same kind, and
		inside the `<li>` it takes the project's bar for nothing.

		**Handed in rather than read off the item, and a guard decided that.** Reading the field
		off the row here was written first and
		`test_a_listing_asks_for_every_field_its_rows_render` refused it, correctly: a listing
		narrows with `fields=` and does not ask for this, so a row drawing nothing would be
		*nobody asked* wearing the appearance of *nothing holds this up* — which is the exact
		hazard that guard exists for. Adding it to `TASK_FIELDS` would have satisfied the scan
		and changed no answer, because only the agenda resolves the field at all.

		**The agenda is the caller that asked** — its request carries no `fields=` at all — so
		it is the one that may render it. The list and the board pass nothing and draw nothing,
		knowing none of the rule.

		**One group per blocker, and the gap between groups is wider than the gap inside one.**
		They were joined with ` · ` and a blocker with nobody on it made that ambiguous: `#1589 ·
		#1592 @jo` reads as if the first two are a pair and the name belongs to both. Proximity
		is what says which name goes with which ref, so the separator is gone and the spacing
		does it — which is `#102`'s argument in a second medium, and the same defect Simon
		reported one level up.
	*/
	const holding = waitingOn.length > 0 && html`
		<div class="holding">
			<span class="quiet">waiting on</span>
			${waitingOn.map((end) => {
				const going = { ref: end.ref, kind: "task" };
				const to = slug ? addressOf(going, slug) : null;
				const who = named(end.assignee, end.assignee_is_agent, end.assignee_answers_to);

				return html`
					<span class="held">
						<a href=${to}
							onClick=${(event) =>
								followed(event, () => onOpen && onOpen(going))}>#${end.ref}</a>
						${who && html`<span class="quiet">${who}</span>`}
					</span>
				`;
			})}
		</div>
	`;

	return html`
		<li ...${lift} data-colour=${hue}>
			${address
				? html`<a class="row ${shape}" href=${address} onClick=${open}>${identity}</a>`
				: html`<button class="row inline ${shape}" onClick=${open}>${identity}</button>`}
			${meta}
			${holding}
		</li>
	`;
}

export function Agenda ({
	buckets, more, heldUp = 0, later = 0, deferred = 0, paused = 0, gone = 0, theirs = 0,
	workspace, onAdd, onOpen,
	onComplete, busy,
	adding,
	/* Where to send a reader who clicks a project label — `#959`. */
	onGo = null,
	/* Which projects are prioritised, addressed — `prioritisedHere` (`#986`). */
	prioritised = [],
	/*
		**What the address already said** — decision `#957` §4, and the prop this drew without
		until `#1215`.

		The merged agenda at `/` names no place, so both halves are null there and every row
		carries its full address. A *scoped* agenda names one, and a row inside it must strip
		what the address already says — otherwise `/projects/subroutine` labels every row
		`projects/subroutine`, which is the exception rule inverted: the thing drawn is the part
		that is *not* news.

		Defaulted to nowhere so that a caller predating the scope renders what it always did.
	*/
	place = { workspace: null, project: null },
}) {
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
	/*
		**Every row says which workspace it is from, and that is not a question** — `#968`,
		Simon's rule: *the workspace should always be shown, if no workspace is selected.*

		This asked `spansWorkspaces`, which is §12.2a's *a mark that says the same thing on
		every row says nothing* — the terminal's rule, where a listing is computed once and read
		once. Here the page polls, so what a row says would change because a stranger filed
		something in another workspace: exactly what decision `#957` §4 rules out for this
		surface, and what `#966` had just been fixed for one column along. The neighbouring
		question was raised on that item and not joined to this one.

		**It was unconditional, because the agenda was** — `agendaRequest` sent no
		`workspace_id` and `/` was the only address this view had. `#1215` gave a place an agenda
		of its own, so that reason expired: on `/projects` the workspace is in the address, and
		naming it on every row is `#968`'s own rule read backwards. Simon met it the day it
		shipped, on a page where every row said `projects/subroutine` under an address that
		already said `projects/subroutine`.

		**Still not a drop-if-uniform rule**, which is the distinction `#966` paid for: this asks
		what the *address* says, never what the rows happen to have in common. A label that
		shortened because a stranger filed something elsewhere would be a clickable control
		changing under the cursor, and this page polls.
	*/
	const showWhere = !place.workspace;

	/*
		**Everything this day is not showing, on one line** — `#1215`, Simon's decision of
		2026-08-24 amending `#649`.

		Two of these have been reported since `#997` and `#888`; the other two were silent, which
		was harmless while the agenda had one address and became a visible unexplained gap the
		moment it sat beside `?view=list` at the same one. Measured on this project before
		deciding: 136 rows in the list against 126 the agenda accounted for.

		**One line rather than four**, which was his choice against my three alternatives, and
		the reason it is not a compromise is that what makes the accounting trustworthy is the
		arithmetic rather than the layout: `tests/test_agenda.py` adds these to the rows the
		agenda shows and compares against the listing at the same scope, so a fifth exclusion
		added later stops the sum adding up and fails the build.

		**A cause contributing nothing is left out, not printed as zero.** §12.2a: a column
		saying the same thing on every row says nothing, and *0 deferred* on the ordinary day is
		that rule one surface along. On this instance `paused` is zero on every page, because
		nothing is on hold.

		**Said in the reader's terms rather than the field's.** *deferred* is a word this product
		uses of itself; what a person did was put something off.
	*/
	const held = [
		{ count: more, said: `${more} more unscheduled` },
		/* **The second of the two capped buckets** (`#1285`). Its cap says it is one for
		   `more`'s reason, and it is next to it because they are the same kind of omission —
		   rows this page chose not to draw, where the three below are rows the day itself
		   holds back. */
		{ count: heldUp, said: `${heldUp} more waiting on somebody else` },
		{ count: deferred, said: `${deferred} put off until later` },
		{ count: paused, said: `${paused} in projects nobody is running` },
		{ count: later, said: `${later} dated further out` },
		/* **The fifth, and the only one nobody chose** — decision `#1235` §3. A list at this
		   scope still shows a passed event, because it is not *completed*, so the difference
		   between the two views is said rather than left to be found. */
		{ count: gone, said: `${gone} already past` },
		/* **The sixth, and the only one about a person rather than a date** — `#1265`,
		   decision `#1267` §1. An agenda is one person's and no other view here is, so a
		   listing at this scope still draws every one of these rows. Last, because it is the
		   one line a reader cannot act on alone. */
		{ count: theirs, said: `${theirs} assigned to somebody else` },
	].filter((one) => one.count > 0);

	/*
		**Drawn on the quiet day too, and that is the case it matters most in.** An empty agenda
		saying only *nothing is due* while twenty-four rows sit behind it is the misreading this
		exists to prevent — a reader checking whether there is work concludes there is none.
		Written as one expression because the branch below returns early and a footer built twice
		is two that can disagree.
	*/
	const accounting = held.length > 0 && html`
		<p class="cut">
			${held.length > 0 && held.reduce((sum, one) => sum + one.count, 0)} not shown here:
			${held.map((one, at) => html`${at > 0 ? " · " : ""}${one.said}`)}
		</p>
	`;

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
			note=${workspace ? `Adds to ${workspace}.` : null} />
	`;

	if (buckets.length === 0) {
		return html`
			<div class="listing agenda">
				${box}
				${/* Said on the quiet day too: *nothing is due* is an answer about this workspace's
				     focus as much as about a busy one, and a fact that disappears when the page
				     empties is one a reader will think they imagined. */ null}
				${prioritisedSentence(prioritised) && html`
					<div class="focus">${prioritisedSentence(prioritised)}</div>
				`}
				<div class="empty">Nothing is due, and nothing is waiting. </div>
				${accounting}
			</div>
		`;
	}

	return html`
		<div class="listing agenda">
			${box}

			${/*
				**About the page rather than about a row** (`#986`, decision `#982`). `Next` is the
				ranked bucket and the one a prioritised project changes; the dated ones are untouched
				by design, because a deadline is answered by *when* rather than by whose project it
				is. `#851` requires a computed rank to be able to explain itself, and 84% of rows
				being in the favoured project is why the explanation cannot live on the rows.
			*/ null}
			${prioritisedSentence(prioritised) && html`
				<div class="focus">${prioritisedSentence(prioritised)}</div>
			`}

			${buckets.map((bucket) => html`
				<section class="bucket" key=${bucket.key}>
					<h2 class=${bucket.key}>${bucket.label}</h2>
					<ul class="rows">
						${bucket.items.map((item) => html`
							${/* `workspace` is the one the switcher holds, and it is the
							     fallback only — a row that knows its own uses that, which is
							     what keeps an agenda row's address pointing at the workspace
							     it actually came from.

							     **It was called `where` until `#1936`**, which is the name ten
							     other components use for an *address builder* — a function
							     `markdown.render` calls to turn a `#42` into a link. One name,
							     two types, and the failure is silent both ways: a string where
							     a function is wanted renders an apology, and a function where
							     a string is wanted renders `function () {}` into a sentence.
							     `Row` has called this exact value `workspace` all along, so
							     the rename is the two agreeing rather than a new word. */ null}
							${/* **What the address says, and that is a fact about the address
							     rather than about the rows** (`#966`, decision `#957` §4). This
							     asked whether the rows *happened* to span workspaces — a
							     drop-if-uniform rule, which §4 rules out here for the reason
							     written into `projectLabel`: this page polls, so a label that
							     shortens because a stranger filed something elsewhere is a
							     clickable control changing under the cursor. Simon met it
							     within the hour, on rows that had not changed, and then met it
							     again in the ref beside it (`#968`).

							     **It was hardcoded to nowhere until `#1215`**, which was true
							     while the agenda lived only at `/` and became wrong the moment
							     a project had one: every row on `/projects/subroutine` was
							     labelled `projects/subroutine`. The listing and the board took
							     `place` all along; only this had the assumption baked in. */ null}
							<${Row} key=${item.workspace + "/" + item.ref} item=${item}
								showWhere=${showWhere} workspace=${workspace}
								place=${place}
								onGo=${onGo}
								${/* **The agenda is the caller that asked** (`#1383`): its request
								     carries no `fields=`, so it has the whole view and may render
								     what is holding a row up. A listing narrows and passes
								     nothing, which is what keeps a blank row honest. */ null}
								waitingOn=${item.blocked_by || []}
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
			${/*
				**And what the window left out** (`#997`). The look-ahead has an edge and every
				surface has the same one, so a deadline three weeks away is in no bucket at all —
				`unscheduled` requires both dates to be null, so dated work leaves it and there is
				nowhere else to go. Simon's decision of 2026-08-18 is that the edge stays and gets
				said: the agenda is a day view, and what was missing was any sign it had left
				something out.
			*/ null}
			${accounting}
		</div>
	`;
}

export function Board ({
	items, onOpen, onComplete, onAdd, onMore, onWiden, busy, more, project, workspace,
	/* Where to send a reader who clicks a project label — `#959`. */
	onGo = null,
	/* How the page is arranged, the value behind it and how to change it — `#1783`. Defaulted
	   because the render harness builds this component directly, and a board with no ordering
	   to describe is a real state rather than a missing argument. */
	ordering = null, order = null, onOrder = null,
	widenTo, selection, finishedTo, adding, onDrag = null, onMove = null,
	over = null, onOver = null,
	/* Which projects are prioritised, and how to change it — `Narrowed` (`#986`). */
	prioritised = [], onPrioritise = null,
	/* Whose work to show, and who there is to choose from — `#1284`. */
	members = [], whose = null, onWhose = null,
	/* What the reader has explicitly chosen about collapsed columns, and how to change it —
	   `#1008`. `App` holds the state and the storage because this component stays hook-free so
	   the harness can call it (`#640`); the *defaults* are worked out below, where the columns
	   and the selection are both to hand. */
	choices = null, onCollapse = null,
	/* **What this workspace calls its statuses** (`#1019`), so a column can tell whether its
	   own name already says everything. Only the board needs it: a list and an agenda have no
	   columns to be redundant with. */
	statuses = null,
	/* What each column held back, keyed by column — `#1790`. Null where the answer was not
	   split, which is the only state in which a column tally is a total. */
	cut = null,
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

	/*
		**A collapsed column is still a drop target, at its narrow width** (`#1008`). The
		handler is on the `<section>`, so width never enters into it, and `align-items: stretch`
		keeps a shut column full height — a tall thin target with plenty of vertical travel.

		**It deliberately does not spring open on hover.** Expanding mid-drag widens the column
		and shifts everything to its right, so a reader aiming at the next column along has the
		target moved under a committed pointer. The tally incrementing after the drop is better
		confirmation than expansion, because nothing moves.
	*/
	const shut = collapsedColumns(arranged.map((column) => column.key), choices);

	/*
		**What this column did not show, if anything** — `#1790`.

		A column's tally has always counted the rows on the page rather than the rows there
		are, and the notice saying so was one line at the *foot of the board*, after every
		column. `#718`'s own argument is that a column is where somebody glances to conclude
		nothing is left, and a glance does not reach a footer four columns wide.

		Null for a column nothing was held back from, so the three states a heading can be in
		— *not split*, *split and complete*, *split and cut* — stay three rather than two.
	*/
	const held = (column) => {
		const account = cut && cut[column.key];

		return account && account.more ? account : null;
	};

	/*
		**What a column's heading says it holds** — `#1845`, Simon 2026-09-02.

		`#1790` gave every column its own allowance and a sentence under it for when that
		allowance ran out, and the `+` went on the *collapsed* heading alone — on the argument
		that a shut column has no room for the sentence the open one carries. That reads as
		though the sentence is the instrument and the character is the fallback, and on a board
		it is the other way round: the sentence is *below* the column, and a reader scanning
		four columns to see what is left never reaches it. Which is `#718`'s own reason for
		putting a tally in the heading at all. Simon met it on a *Drafts* column reading `25`
		open and `25+` shut, where only the shut one answered the question he was asking.

		**`#102` is untouched.** The `+` reinforces a fact the notice below still states in
		words, which is the arrangement every mark in this app already uses.

		**One derivation read by both headings**, rather than the same rule written twice. The
		two spellings had already parted company once, which is how the open column came to say
		less than the shut one.
	*/
	const tally = (column) => `${column.items.length}${held(column) ? "+" : ""}`;

	const classFor = (column) =>
		`column${over === column.key ? " over" : ""}`
		+ (shut.has(column.key) ? " collapsed" : "");

	/* The same test the listing makes, and it has to be the same: both render one page of two
	   collections, and a column tally that reads as a total is worse on a board than a short
	   list is, because a column is where somebody looks to see that nothing is left. */
	const truncated = more !== null && more !== undefined
		&& (more.tasks !== null || more.documents !== null);

	return html`
		<div class="listing board">
			${/* **A board says how it is ordered, and lets a reader change it** — `#1783`.
			     `#661`'s rule reaching the one arrangement it was never applied to, and the
			     one where it matters most: a column shows what the page fetched, so an order
			     nobody can see decides what is in every column and says nothing about it.

			     `empty` is the whole page rather than a column: a board with nothing on it has
			     no order to describe, and a board with one full column and three empty ones
			     very much does. */ null}
			<${Ordered} ordering=${ordering} order=${order} onOrder=${onOrder}
				busy=${busy} empty=${items.length === 0} />

			${/* **The board takes the same control as the list, from the same component** —
			     `#1284`, and it is `#1783`'s argument one parameter along: a board fetches one
			     page and partitions it, so narrowing decides what is in *every* column. Two
			     copies of this markup was the alternative and is this codebase's signature
			     defect. */ null}
			<${Whose} members=${members} whose=${whose} onWhose=${onWhose} busy=${busy} />
			${onAdd && html`<${Adding} onAdd=${onAdd} busy=${busy} ...${adding || {}} />`}

			${/* **`selection` reaches this one now** — `SR#2070`. `#1020` gave `Narrowed` the
			     sentences that say a page was narrowed by a tag or a person, and passed it from
			     `Listing` alone. On a board with no project in the address the component then
			     decided it had nothing to say and returned null, taking *Show everything* with
			     it — so a reader who clicked a tag chip on a board had no way back out. */ null}
			<${Narrowed} project=${project} onWiden=${onWiden} widenTo=${widenTo}
				selection=${selection}
				prioritised=${prioritised} onPrioritise=${onPrioritise} busy=${busy} />

			<div class="columns">
				${arranged.map((column) => html`
					${/*
						**A column is the drop target and a card is the thing dropped** (`#711`).
						`preventDefault` on dragover is what *makes* an element a target — the
						default is to refuse — so the handler that looks like it does nothing is
						the one doing the work.

						**A drop on the column an item is already in is not a write.** It is the
						commonest way a drag ends, being what happens when somebody thinks better
						of it, and reporting *#42 is in progress* about a card nobody moved is
						the kind of true-sounding falsehood this project keeps finding.
					*/ null}
					<section class=${classFor(column)} key=${column.key}
						onDragOver=${onMove ? ((event) => {
							event.preventDefault();
							event.dataTransfer.dropEffect = "move";

							if (onOver && over !== column.key) onOver(column.key);
						}) : undefined}
						onDragLeave=${onOver ? (() => onOver(null)) : undefined}
						onDrop=${onMove ? ((event) => {
							event.preventDefault();
							onMove(column.key);
						}) : undefined}>
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
						${shut.has(column.key)
							? html`
								${/* **The whole collapsed column is the control**, not a strip of it. A
									     character-wide button is a hard thing to hit, and when a column is shut
									     there is nothing else in it to click — so the button fills it and the
									     heading turns with it. `aria-expanded` is what says so to a reader who
									     cannot see the rotation. */ null}
								<h2 class="shut">
									<button type="button" class="reveal" aria-expanded="false"
										onClick=${() => onCollapse && onCollapse(column.key, false)}>
										${column.label}
										${/* **The count is what keeps collapsed from meaning blind**, and it says
											     *not shown* where that is why this is shut — otherwise a column
											     nobody asked for would read as a column holding nothing, which is
											     the false statement `#742` exists to prevent, said sideways. */ null}
										<span class="tally">
											${unasked(column) ? NOT_SHOWN : tally(column)}
										</span>
									</button>
								</h2>`
							: html`
								<h2>${column.label}${!unasked(column) && html`${" "}
									<span class="tally">${tally(column)}</span>`}
									${onCollapse && html`<button type="button" class="shut reveal"
										aria-expanded="true" aria-label=${`Collapse ${column.label}`}
										onClick=${() => onCollapse(column.key, true)}>−</button>`}</h2>

								${unasked(column)
									? html`<p class="empty">${NOT_SHOWN}.${" "}
										${finishedTo && FINISHED.has(column.key)
											? html`<a href=${finishedTo}>Show finished work</a>`
											: null}</p>`
									: column.items.length === 0
									? html`<p class="empty">${
										/*
											**`Nothing` is a claim, and it is only true when this
											column got an allowance of its own** (`#1790`, `#1782`).

											Before grouping, a board spent one allowance across
											every column in one order — so a column could be empty
											on the page while holding work the page never reached,
											and this said there was none. Measured here: *In
											progress* read as empty against three real rows.

											`cut` is what tells the two apart. Where the answer was
											split, this column was asked its own question and the
											answer really was none. Where it was not, and the board
											is truncated, the honest word is the one a column
											nobody asked about already uses.
										*/ null
									}${cut === null && truncated ? NOT_SHOWN : "Nothing"}</p>`
									: html`
										<ul class="rows">
											${column.items.map((item) => html`
												<${Row} key=${item.kind + item.ref} item=${item}
													workspace=${workspace}
													place=${{ workspace, project }} onGo=${onGo}
													onOpen=${onOpen} onComplete=${onComplete}
													onDrag=${onDrag}
													hideStatus=${soleStatusIn(
														statuses,
														item.kind === "document" ? "document" : "task",
														column.key,
													)} />
											`)}
										</ul>
										${/*
											**At the foot of the column it is about, not the board**
											(`#1790`). Simon's own framing: a reader cannot tell a
											column that is empty from one that only looks empty, and
											the notice that answered that sat below every column at
											once.

											**In words, and the count is of what is shown.** §8.4
											declines a total by default because it costs a scan per
											group, so this says *there are more* rather than
											inventing a number — which is the same trade the board's
											own footer already makes.
										*/ null}
										${held(column) && html`
											<p class="cut">Showing ${column.items.length}.${" "}
												There are more.</p>
										`}
									`}`}
					</section>
				`)}
			</div>

			${truncated && html`
				<div class="cut">
					${/* **A grouped board counts columns, not rows** (`#1790`). *Showing 100* under a
					     board whose columns were each capped at 25 describes an allowance nobody set,
					     and the number that lets a reader judge a column is the column's own, which is
					     now printed under it. So this says how many columns are short and leaves the
					     counting to them. */ null}
					<span>${cut
						? `${Object.values(cut).filter((account) => account.more).length} of these`
							+ ` columns hold more than is shown.`
						: `Showing ${items.length}. There are more.`}</span>
					${onMore && html`
						<button class="action" onClick=${onMore} disabled=${busy}>Show more</button>
					`}
				</div>
			`}
		</div>
	`;
}
