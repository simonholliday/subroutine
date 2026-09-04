/*
	One item in full, and the things done to it there — completing, linking, searching and
	saying something.

	**Split out of `app.js` by `#1849`**, which moved 10,286 lines into thirteen modules and
	changed nothing about what any of them do. Read `app.js` for what the whole app is; this
	file is one layer of it.
*/

import { render } from "preact";
import { html } from "./html.js";
import { addressOf } from "./address.js";
import { accumulated, refusal } from "./answers.js";
import { Facts, Prose } from "./chrome.js";
import { completable } from "./dates.js";
import { Editing } from "./forms.js";
import { Held, blockersDone, followed, opens, partsDone, withinAllowance } from "./grouping.js";
import { Icon, marks, moment, when } from "./marks.js";
import { notOffered, offered } from "./places.js";
import { authorOf, linkChoices, written } from "./requests.js";
import { Marks, Stamp } from "./rows.js";
import { LINKS_SECTION, MAX_PARTS } from "./settings.js";

export function Doing ({
	item, members, onComplete, onAssign, onStatus, busy, statuses, projects = null,
}) {
	/*
		The two things a reader can do to an item from here.

		**Complete and Assign are for a task and only while it is open**, because a completed
		task offering "Complete" is a control whose only outcome is a refusal, and §6.14 gives a
		document no `completed_at` and no assignee to hand it to.

		**The status control is not one of those and this paragraph said it was** (`#1419`). It
		moved outside `completable`'s gate with `#758` — see the note above the return — and a
		document has had a status all along, so it is drawn on one deliberately. The sentence
		here went on describing the arrangement before that change, which is how `statusRequest`
		came to be the one builder in this file that never asked what it was writing to.

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
	/*
		**Narrowed to what the item's own project offers** (`#1029`), found by `project_id`
		rather than by the address a form would use — a row has the id and never the tree.

		`item.status` is passed as what to keep, which is the rule that stops this control ever
		misreporting: a `<select>` whose value matches no option renders blank or falls back to
		its first entry, so a task in a status its project has since stopped offering would read
		as *Open* — and pressing anything else here would write that back.
	*/
	const where = offered(
		statuses, item.kind === "document" ? "document" : "task",
		notOffered(projects, item.project_id), item.status
	);

	/*
		**The status control is outside the completable gate, and the rest are inside it**
		(`#758`). *Complete* on something already over is a control whose only outcome is a
		refusal — but moving a **cancelled** item back to *open* is exactly the kind of thing a
		person needs and had no way to do here at all, so gating the whole block on it made the
		quick path unreachable precisely where it was most wanted.

		**A status is not a claim and neither is derived from the other** (`#726`, Simon's
		ruling). Setting one here touches nothing else: a claimed item does not become *in
		progress*, and moving an item to *in progress* claims nothing.
	*/
	if (!completable(item) && where.length === 0) return null;

	return html`
		<div class="doing">
			${completable(item) && html`
				<button class="finish action" disabled=${busy} onClick=${() => onComplete(item)}
					aria-label=${`Complete #${item.ref}, ${item.title}`}
					><${Icon} name="check" />Complete</button>
			`}

			${onStatus && where.length > 0 && html`
				${/* **The vocabulary comes from the workspace**, never a literal list: a status
				     is renameable and an installation may add one, so a control carrying its
				     own three words is wrong on the first instance that does either — and
				     wrong silently, because it still looks complete. */ null}
				${/* **`value` rather than the options' `selected` alone, so a refused write cannot
				     be left on screen** (`#1419`). `wrote` re-reads only when the instance
				     accepted; after a refusal the note says *was not changed* while the control
				     still shows what the reader picked. A `<select>` keeps whatever the browser
				     put in it, and `selected` on an option only decides the *first* paint — so
				     the value has to be stated on the element that holds it, which the failure
				     path's re-render then restores. `offered` guarantees `item.status` is one
				     of these, which is what makes this safe to assert. */ null}
				<label class="assign">
					<span>Status</span>
					<select disabled=${busy} value=${item.status}
						onChange=${(event) => onStatus(item, event.target.value)}>
						${where.map((one) => html`
							<option key=${one.key} value=${one.key}
								selected=${one.key === item.status}>${one.label}</option>
						`)}
					</select>
				</label>
			`}

			${completable(item) && members.length > 0 && html`
				${/* **The sibling of the status control above, fixed with it** (`#1419`). It is
				     the same gesture through the same `wrote`, so a refused hand-over left the
				     new name on screen for the same reason. Fixing one and leaving the other is
				     how two controls beside each other come to behave differently. */ null}
				<label class="assign">
					<span>Assigned to</span>
					<select disabled=${busy} value=${item.assignee || ""}
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
	item, links, comments, governing = [], checked = [], members = [], onOpen, onBack,
	/* What this item is made of, with the envelope kept — `#1218`. Defaulted, because a
	   document is read without asking for parts at all and `has_more` has to be readable
	   without a guard at every use. */
	parts = { items: [], has_more: false },
	onComplete, onAssign, busy, where,
	backTo, workspace, editing, onEdit, onSave, conflict, vocabulary, projects,
	onStatus, statuses, onComment, onLink, onUnlink, reading, onReading,
	/* Which prose box is being previewed, and how to change it — `#776`. */
	previewing = null, onPreviewing = null,
	/* Which projects are prioritised, addressed — for the fact sheet's project row (`#986`). */
	prioritised = [],
	/* **What the address already said**, so a linked item's project chip strips it exactly as
	   a row's does — decision `#957` §4, and `#970` is where the links list joined that rule.
	   `onGo` is what makes the chip a control rather than an ornament (`#251`). */
	project = null, onGo = null,
	/* Which truncated sections this reader has opened, and how to change it — `#1820`.
	   Defaulted, because the render harness builds this component directly and a section that
	   nobody has answered for is simply the one that takes the allowance. */
	revealed = {}, onReveal = null,
}) {
	const body = item.description || item.body;

	/*
		**The links section is truncated rather than the whole list drawn** — `#1820`, and
		`#1149` is why it is above the description in the first place. Eighteen links above a
		961-character body put the body below the fold, which is that decision costing what it
		was taken to buy.

		Computed here rather than inside the section so the count and the rows come from one
		call: two calls would be two places the allowance is applied and one of them would
		eventually be given a different one.
	*/
	const linksShown = withinAllowance(links, revealed[LINKS_SECTION]);

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
				: html`<button class="back quiet" onClick=${back}>← All items</button>`}
			${/* **Editing replaces the item's own display rather than sitting beside it**
			     (`#757`). Two copies of a title on one screen, one of them stale, is the shape
			     this project keeps paying for — and a reader has to be able to see what they
			     are changing without a second version of it arguing. */ null}
			${editing
				? html`<${Editing} item=${item} busy=${busy} onSave=${onSave}
					onCancel=${() => onEdit(false)} conflict=${conflict}
					vocabulary=${vocabulary} projects=${projects} members=${members}
					where=${where} previewing=${previewing} onPreviewing=${onPreviewing}
					reading=${reading} onReading=${onReading} />`
				: html`
					${/* **The same strip a card wears** — `#2026`. This was
					     `<h2>#42 Title</h2>`, a ref inline ahead of a title, which is the
					     arrangement `#1148` moved off the board card and did not come back
					     for; a reader landing here from a card met the two facts in a
					     different shape from the one they had just clicked. */ null}
					<${Stamp} item=${item} />
					<h2>${item.title}</h2>
					${/*
						**The same marks a row draws, directly under the title** — `#1019`,
						Simon: *"I think yes, we should be consistent."*

						This was the one surface with no at-a-glance summary, and it is the one
						a reader lands on from a card — so the type, the state and the place had
						to be re-read out of a fact sheet after being visible on the row they
						clicked. Four `Facts` rows came out with it, which is the half that
						makes this a change rather than an addition.

						**No ordering value and no status suppression**: a page is not a list,
						so it is ordered by nothing, and it has no column that could already be
						saying the status. `place` is null because an item page is not narrowed
						to anything, so the project label says its whole address.
					*/ null}
					<${Marks} badges=${marks(item, null, null, !!onGo, { hideType: true })}
						onGo=${onGo} />
					<${Facts} item=${item} prioritised=${prioritised} />

					${onEdit && html`
						<button class="edit action" disabled=${busy}
							onClick=${() => onEdit(true)}>Edit</button>
					`}

					${onComplete && html`
						<${Doing} item=${item} members=${members} onComplete=${onComplete}
							onAssign=${onAssign} onStatus=${onStatus} statuses=${statuses}
							projects=${projects} busy=${busy} />
					`}

				`}

			${governing.length > 0 && html`
				${/* **What binds whoever picks this up** (`#1119`) — the workspace-wide *what
				     is in force here*, narrowed to one item. Above the links because it is the
				     section somebody has to read before doing anything, where the links are
				     what they read afterwards.

				     **Typed links only** (`#1124` Q2, Simon's). Filed nearby and mentioned in
				     passing mean *near this*, which is a different claim — and answering it
				     under this heading is how a reader learns not to trust the heading.

				     Titles and refs, never bodies: a document's title states its conclusion,
				     so this is readable without opening anything. */ null}
				<h3>Read first</h3>
				<ul class="linked">
					${governing.map((binds) => {
						const going = { ref: binds.document.ref, kind: "document" };
						const to = workspace ? addressOf(going, workspace) : null;
						const follow = (event) =>
							followed(event, () => onOpen && onOpen(going));

						/* **The type is the label and nothing else is drawn.** Every row here
						   is a document, in force, of a governing type — so a status chip
						   would say `active` on every line, which is §12.2a's column that says
						   the same thing on every row and therefore says nothing. What differs
						   between these rows is which *kind* of obligation each is, and that
						   is the word `subroutine://conventions` groups by. */
						return html`
							<li key=${binds.document.ref}>
								<span class="label">${binds.document.type}</span>${" "}
								${to
									? html`<a href=${to} onClick=${follow}>
										#${binds.document.ref} ${binds.document.title}</a>`
									: html`<button class="inline" onClick=${follow}>
										#${binds.document.ref} ${binds.document.title}</button>`}
							</li>`;
					})}
				</ul>`}

			${parts.items.length > 0 && html`
				${/* **What this item is made of, above what it is joined to** — `#1218`, Simon's
				     placement and the terminal's. A milestone's parts are the thing somebody
				     opened it to read; its links are context around that.

				     **The rollup is `#84`'s and is computed, never stored.** A parent never
				     auto-completes, so `4 of 4` beside an open parent is a question being put
				     to a person rather than a state nobody updated. */ null}
				<h3>Sub-tasks${partsDone(parts)}</h3>
				${/* **`linked` for the styling and `parts` to be addressable.** The two lists are
				     drawn identically on purpose — Simon asked for *similar format* — which
				     leaves a test no way to say *this row is a part* rather than *this row is on
				     the page*, and a strikethrough assertion that cannot tell them apart is one
				     that passes on the wrong list. */ null}
				<ul class="linked parts">
					${parts.items.map((part) => {
						const going = { ref: part.ref, kind: "task" };
						const to = workspace ? addressOf(going, workspace) : null;
						const follow = (event) =>
							followed(event, () => onOpen && onOpen(going));

						/*
							**The same marks a link end wears** (`#970`), so a part's status,
							readiness and project chip are one rendering rather than two that
							agree. `place` is what the address already said (decision `#957` §4),
							so a part in the project you are looking at carries no chip and one
							that crosses out of it does.
						*/
						const badges = marks(
							{ ...part, kind: "task" }, null, { workspace, project }, !!onGo,
						);

						return html`
							<li key=${part.id || part.ref}>
								${/* **Struck through when it is closed**, which is Simon's and is
								     *better here* than what the terminal does. The terminal dims,
								     and argues that the rollup above already carries the count —
								     but dimming is contrast alone, and `#102` says no information
								     may exist only in a colour. A strikethrough is a second
								     channel, and `marks` draws the `Done` or `Cancelled` chip
								     beside it so the line reads correctly with every style
								     switched off.

								     **Not a divergence between surfaces** (`#989`): the fact —
								     *this part is finished* — is the same on both, and only its
								     rendering differs. */ null}
								${to
									? html`<a class=${part.is_complete ? "over" : null}
										href=${to} onClick=${follow}>
										#${part.ref} ${part.title}</a>`
									: html`<button class=${`inline${part.is_complete ? " over" : ""}`}
										onClick=${follow}>
										#${part.ref} ${part.title}</button>`}
								<${Marks} badges=${badges} onGo=${onGo} />
							</li>
						`;
					})}
				</ul>
				${/* **A cap that says it is one** (`#888`, and `#1175` is the open item about a
				     listing claiming a completeness it cannot have). Fifty parts and fifty-one
				     look identical on the page; this is the only thing that tells them apart,
				     and it names the surface that can show the rest rather than merely
				     apologising. */ null}
				${parts.has_more && html`
					<p class="note">Showing the first ${MAX_PARTS}. There are more —
						<code>subroutine show #${item.ref}</code> lists them all.</p>`}
			`}

			${(links.length > 0 || onLink) && html`
				${/* **The count `#84` specified, on the surface Simon reads** (`#970`). A
				     milestone is an item whose blockers are its contents, and `subroutine show`
				     has answered *how many are left* since `#210` while this page made a reader
				     open each one. Its rule is copied deliberately: incoming `blocks` only,
				     because a *relates to* has nothing to be N of. */ null}
				<h3>Links${blockersDone(links)}</h3>
				${/* **`links` beside `linked`, so the two lists on this page are separable.**
				     `Parts` is drawn in the same format on purpose (`#1218`), which leaves
				     `.linked li` meaning *a row in either list* — and an assertion about one of
				     them that cannot say which list it is on is one that passes on the wrong
				     one. */ null}
				<ul class="linked links" id=${`section-${LINKS_SECTION}`}>
					${linksShown.map((link) => {
						const going = { ref: link.other.ref, kind: link.other.entity_type };
						const to = workspace ? addressOf(going, workspace) : null;
						const follow = (event) =>
							followed(event, () => onOpen && onOpen(going));

						/*
							**The far end, rendered as a row is** (`#970`). `kind` is what this
							app calls `entity_type` everywhere else — `Listing` adds it to every
							row it opens — and `marks` reads it, so an end arriving under the
							wire's name would be silent about the one thing no other field says.

							**No sort-value mark, because the reader is not choosing this
							order.** These arrive in `domain.links.reading_order` —
							outstanding before settled, then what the relation binds, then
							prerequisites before dependents, then like with like, then by
							number — which is the same sequence `show` and the tools draw.
							So a relation can appear in two runs, live rows and struck ones,
							and that is the point rather than a fault (`#1538`). A mark says *this
							is the column you sorted by*, and nothing here offered a choice.

							**This said "nothing sorted these" until `#1535`, and something
							always had**: `_touching` has ordered by `created_at` since it was
							written. The sentence was the reason nobody looked at an order no
							surface renders the key of.

							**`place` is what the address already said**, exactly as a row's is,
							which is decision `#957` §4 — so a link inside the project you are
							looking at carries no chip and one that crosses out of it does. That
							is the reader's answer to *which project is this blocker in*: the
							exception is what is drawn, which is `#102`'s argument applied to an
							axis that is not colour.
						*/
						const end = { ...link.other, kind: link.other.entity_type };
						/*
							**The trash chip is added here rather than inside `marks`**
							(`#1403`). `marks` is shared by the list, the board and the agenda,
							and `test_a_listing_asks_for_every_field_its_rows_render` requires
							every field it reads to be asked for by every listing — so a read
							there would put `deleted_at` on every row of every page to serve a
							chip no listing can ever draw, since none of them shows a deleted
							item at all. §13's context economy, met by a guard doing its job.

							**A word, because `#102` forbids saying it in styling alone.** The
							row is dimmed *and* reads `Deleted`, and it stays on the page
							because delete here is reversible and an absence somebody has to
							infer is worse than a mark.
						*/
						const badges = [
							...(link.other.deleted_at
								? [{ text: "Deleted", family: "state", tone: "blocked" }]
								: []),
							...marks(end, null, { workspace, project }, !!onGo),
						];

						return html`
							<li key=${link.id}>
								<span class="label">${link.label}</span>${" "}
								${/* **Struck through when it is closed** — Simon, 2026-08-17.
								     `#102`'s rule is that nothing may be said in styling alone,
								     and nothing here is: a closed item's status is not its
								     default, so `marks` draws a `Done` or a `Cancelled` chip
								     beside this and the line reads correctly with every style
								     switched off.

								     **Which retires the bare `done` span, and a defect with
								     it.** `is_complete` is `completed_at is not None`, which
								     invariant 5 makes true for done *and* cancelled — so a
								     cancelled blocker said `done` on this page, about an item
								     nobody finished. The status chip is what tells them
								     apart. */ null}
								${/* **The one link end with no address is a button standing in for an
								     anchor**, so it wears `inline` — the role for a control that must
								     read as the link it replaces rather than draw itself a box
								     (design `#1045`). */ null}
								${to
									? html`<a class=${link.other.is_complete ? "over" : null}
										href=${to} onClick=${follow}>
										#${link.other.ref} ${link.other.title}</a>`
									: html`<button class=${`inline${link.other.is_complete ? " over" : ""}`}
										onClick=${follow}>
										#${link.other.ref} ${link.other.title}</button>`}
								<${Marks} badges=${badges} onGo=${onGo} />
								${onUnlink && html`
									<button class="unlink action" disabled=${busy}
										aria-label=${`Remove the link to #${link.other.ref}`}
										onClick=${() => onUnlink(link)}>Remove</button>
								`}
							</li>
						`;
					})}
				</ul>

				${onReveal && html`<${Held} name=${LINKS_SECTION} total=${links.length}
					shown=${linksShown.length} revealed=${!!revealed[LINKS_SECTION]}
					onReveal=${onReveal} />`}

				${onLink && html`<${Linking} busy=${busy} onLink=${onLink}
					types=${linkChoices(vocabulary)} />`}
			`}

			${/* **The prose sits below what the item is joined to** (`#1149`, Simon: *"we don't
			     see them without scrolling when the description is long"*). The rule that
			     decides the whole order, and it is his: **what you need before reading the item
			     goes above; what accumulated about it stays below.**

			     So *Read first* and *Links* are above — they say what binds this and what it is
			     joined to, which is how a reader decides whether to read the description at all
			     — and *Recorded checks* and *Comments* are below, because both are the record of
			     what happened and are looked up deliberately rather than scanned.

			     **`#1119`'s argument survives rather than being inverted.** It put *Read first*
			     above *Links* because it is what somebody has to read before doing anything;
			     that reason applies harder against a long description than the links' does.

			     **Outside the editing branch it used to live in**, so that `#757` still holds:
			     editing replaces the item's own display rather than sitting beside it, and two
			     copies of a description on one screen with one of them stale is the shape this
			     project keeps paying for. */ null}
			${!editing && body && html`<${Prose} className="prose" text=${body} where=${where}
				onOpen=${onOpen} />`}

			${checked.length > 0 && html`
				${/* **What was checked, and it is a record rather than a proof** (`#1121`).
				     Somebody can post an exit code of zero without having run anything, so the
				     heading says *recorded* and never *verified* — the value is that it is
				     kept, attributed and able to go out of date.

				     **§14.1 is why this is here at all**: nothing an agent stores may be
				     invisible to the person, and a verification the browser could not show
				     would be an agent-only surface, which §14.15 forbids by name.

				     Said in words rather than in colour alone (`#102`): *passed* and *failed*
				     are the words, and the tree is printed short beside them because what a
				     reader wants to know is whether it is the one they are on. */ null}
				<h3>Recorded checks</h3>
				<ul class="linked">
					${checked.map((record) => html`
						<li key=${record.id}>
							<span class="label">${record.passed ? "passed" : "failed"}</span>${" "}
							${record.summary || "(no summary)"}${" "}
							<span class="muted">${record.tree_hash
								? `tree ${record.tree_hash.slice(0, 7)}`
								: "no tree — this cannot go out of date"}</span>
						</li>`)}
				</ul>`}


			${/* **The heading shows even with nothing under it, once there is a box** (`#759`).
			     An empty thread with no way to start one is a section that reads as absent
			     rather than as empty.

			     **It says what the section holds, not what to put in it** (`#865`, Simon's).
			     This was *What happened*, which is §5.10's rule — "a comment is what happened;
			     a document is what you concluded" — and that rule is right and stays where it
			     is *teaching*: in the specification, in the agent guide, in the skill and in
			     `subroutine_comment`'s description, all of which are read by somebody choosing
			     between the two.

			     Over the thread and on the box it stopped being a distinction and became an
			     instruction, at the moment somebody is writing. *"I have asked the supplier"*
			     and *"do we still need this?"* are neither wrong nor what happened. */ null}
			${(comments.length > 0 || onComment) && html`
				<h3>Comments</h3>
				<ul class="comments">
					${comments.map((note) => html`
						<li key=${note.id}>
							<div class="said">
								${/* **Who, then when.** A comment carries `author_id` and no
								     name, so this is resolved against the roster the page
								     already holds — and left out entirely when it cannot be,
								     rather than guessed at. */ null}
								${authorOf(note, members) && html`
									<strong>${authorOf(note, members)}</strong>${" "}
								`}
								${/* **The moment is its own element so it can stay quiet**
								     (`SR#1819`). *Who* is what a reader scans a thread for and
								     *when* is what they check once they have found it, so the
								     two are not the same weight — and a bare text node could
								     not be told apart from the name beside it. */ null}
								<span class="when">${moment(note.created_at)}</span>
							</div>
							<${Prose} className="body" text=${note.body} where=${where}
								onOpen=${onOpen} />
						</li>
					`)}
				</ul>

				${onComment && html`<${Saying} busy=${busy} onComment=${onComment}
					where=${where} previewing=${previewing} onPreviewing=${onPreviewing} />`}
			`}
		</div>
	`;
}

export function Linking ({ onLink, types, busy }) {
	/*
		Joining this item to another — `#760`.

		**A ref and a type, and nothing about which table the other end is in.** One counter
		serves tasks and documents (§6.2), so asking a reader to say *task or document* would be
		making them hold a fact the system has; `linkableTypes` resolves it by trying each in
		turn, which is what `fetched` already does to open an item by ref.

		**The types come from the workspace**, never a literal list — `link_type` is
		per-workspace vocabulary and an installation may add one. The label is the type's own
		`title`, so *Blocks* reads as this instance writes it.

		**`inputMode="numeric"` rather than `type="number"`.** A number input brings a spinner
		and a scroll-to-change gesture for a value that is an identifier, not a quantity — and
		on a phone the keypad is the whole of what is wanted.
	*/
	const submit = (event) => {
		event.preventDefault();

		const form = event.currentTarget;
		const ref = form.elements.target.value.trim().replace(/^#/, "");

		if (ref === "" || busy) return;

		/* Cleared once the write has landed, for the capture box's reason: a ref refused —
		   because it names nothing, or names something in a project this reader cannot see —
		   used to take the typed number with it. */
		Promise.resolve(onLink(ref, form.elements.link_type.value)).then((landed) => {
			if (landed) form.reset();
		});
	};

	if (types.length === 0) return null;

	return html`
		<form class="linking" onSubmit=${submit}>
			<select name="link_type" disabled=${busy} aria-label="How they are related">
				${types.map((one) => html`
					<option key=${one.value} value=${one.value}>${one.label}</option>
				`)}
			</select>
			<input name="target" required disabled=${busy} inputMode="numeric"
				aria-label="Which item" placeholder="#42" />
			<button type="submit" class="primary" disabled=${busy}>Link</button>
		</form>
	`;
}

export function Seeking ({ onSearch, asked, busy }) {
	/*
		Finding something — `#775`.

		**Submitted rather than searched on every keystroke.** `q` is `ILIKE '%term%'` on both
		sides of the term, which §10.4 lists as one of exactly two predicates that **cannot use
		an index** — so a request per character is a scan per character, and `#90` is the item
		that will measure when that stops being free. A form also gives the browser's own
		*search* keyboard behaviour for nothing.

		**`key` is the asked-for text**, so stepping back to a different search rebuilds the
		input rather than leaving the previous words in it. An uncontrolled input keeps what the
		DOM holds, which is right while somebody types and wrong when the address changes
		underneath them.

		**Clearing it is submitting nothing**, which `chooseSearch` reads as *take the search
		off* — one control, both directions, and no second button to explain.
	*/
	const submit = (event) => {
		event.preventDefault();

		onSearch(event.currentTarget.elements.q.value);
	};

	return html`
		<form class="seeking" onSubmit=${submit} role="search">
			<input key=${asked} name="q" type="search" disabled=${busy}
				defaultValue=${asked} aria-label="Search"
				placeholder="Search anything" />
			${/* **An action, not a primary** — Simon, 2026-08-20. It is this form's submit, and
			     the rule is *commits the thing this form exists to make*: a search makes
			     nothing, and three accent fills on one screen is the noise being removed. */ null}
			<button type="submit" class="action" disabled=${busy}>Search</button>
		</form>
	`;
}

export function Written ({
	name, label, rows, value = "", busy = false, required = false, placeholder = null,
	where = null, previewing = null, onPreviewing = null,
}) {
	/*
		A box for prose, and a way to see it as it will read — `#776`, Simon's suggestion of
		2026-08-10.

		**Cheap, because the renderer is already ours and already pure.** `markdown.render` is a
		function from text to a string — which is what let 25 hostile payloads be fed through
		its own entry point in `#637` — so a preview is that call plus somewhere to put the
		answer. No library, no build step, nothing new served.

		**The textarea stays mounted and is hidden**, which is the whole of why this works.
		Swapping it out for the preview would drop an *uncontrolled* field, so coming back would
		show `defaultValue` — the stored text rather than what somebody has been typing. `#757`
		chose `defaultValue` over `value` precisely so a re-render cannot reach in and reset
		what is being written, and throwing it away on a toggle would be that loss arriving
		from a friendlier direction.

		**A toggle rather than side by side**, which is a trade rather than a shortcut. Live
		text beside the box needs a mirror of every keystroke, and a mirror is the controlled
		field `#757` refused. This reads the value once, when the button is pressed.

		**No state of its own**, like every other form component here (`Adding`'s own comment
		says why): the answer lives in `App` and arrives as a prop, so `tests/dom.js` can render
		this and `#640`'s gap does not widen.

		The preview goes through `Prose`, which catches — `#679`/`#680` are why that matters
		here more than anywhere else: this is the one place the renderer is asked to read
		*incomplete* Markdown, and every half-written state is on the way to a finished one.
	*/
	const showing = Boolean(previewing) && previewing.name === name;

	const toggle = (wanted) => (event) => {
		if (!onPreviewing) return;

		/* **Read once, from the form this button is in.** The box is uncontrolled, so its
		   current text lives in the DOM and nowhere else — which is `#757`'s decision and the
		   reason a live preview would need a mirror of every keystroke. */
		const form = event.currentTarget.form;
		const box = form && form.elements[name];

		onPreviewing(wanted ? { name, text: box ? box.value : "" } : null);
	};

	return html`
		<label class=${`wide written${showing ? " previewing" : ""}`}>
			<span>${label}</span>

			${onPreviewing && html`
				${/*
					**A segmented control, and both words are always there** — Simon 2026-08-20,
					design `#1045`. It was one button whose label was the *other* state, which
					is ambiguous twice over: a button reading *Preview* cannot say whether that
					is what you are looking at or what pressing it gives you. Two segments
					cannot be.

					**And it wore the accent fill of a control that writes**, three inches above
					a Save. The accent is spent on committing; this commits nothing.

					**Marked with a fill *and* the word** (`#102`), so nothing here is carried
					by the mark alone — both labels stay readable in either state.
				*/ null}
				<div class="toggle preview" role="group" aria-label=${`How to see ${label}`}>
					<button type="button" class="segment" disabled=${busy}
						onClick=${toggle(false)}
						aria-pressed=${showing ? "false" : "true"}>Write</button>
					<button type="button" class="segment" disabled=${busy}
						onClick=${toggle(true)}
						aria-pressed=${showing ? "true" : "false"}>Preview</button>
				</div>
			`}

			${/* **Hidden rather than unmounted**, so an uncontrolled field keeps what is in
			     it — see above. `hidden` and not a class, because a hidden form control is
			     still submitted and still readable, which is exactly what is wanted. */ null}
			<textarea name=${name} rows=${rows} disabled=${busy} required=${required}
				hidden=${showing} placeholder=${placeholder} aria-label=${label}
				defaultValue=${value}></textarea>

			${showing && html`
				<${Prose} className="rendered" text=${previewing.text} where=${where} />
			`}
		</label>
	`;
}


export function Saying ({ onComment, busy, where = null, previewing = null, onPreviewing = null }) {
	/*
		Writing down what happened — `#759`.

		**A comment is prose and nothing else**, so this is one textarea: no title, no type, no
		project. §5.10's distinction is the one this product is least willing to blur, and a
		form that offered those fields would be quietly proposing a document.

		**Uncontrolled, and cleared on submit**, like every other form here — `#757`'s reason
		holds: nothing needs remembering between keystrokes, and a re-render cannot then reach
		in and take what somebody is typing.
	*/
	const submit = (event) => {
		event.preventDefault();

		const form = event.currentTarget;
		const written = form.elements.body.value.trim();

		if (written === "" || busy) return;

		/* Cleared once the write has landed, and this is the box where it costs most: a
		   comment is the longest thing anybody types here, and a refusal used to empty it. */
		Promise.resolve(onComment(written)).then((landed) => {
			if (landed) form.reset();
		});
	};

	return html`
		<form class="saying" onSubmit=${submit}>
			<${Written} name="body" label="Add a comment" rows="3" required busy=${busy}
				placeholder="Markdown works, and #42 links." where=${where}
				previewing=${previewing} onPreviewing=${onPreviewing} />
			<button type="submit" class="primary" disabled=${busy}>Add a comment</button>
		</form>
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
			<button class="back action" onClick=${onRetry}>Try again</button>
		</div>
	`;
}

/* ---- the app ------------------------------------------------------------ */
