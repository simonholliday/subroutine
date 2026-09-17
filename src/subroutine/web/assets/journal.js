/*
	What happened, in a workspace or to one item — `#2731` and `#1428`, design `#2724`.

	**A page drawn in place of the work, as the settings and people pages are**, and for their
	reason: it has no arrangement or selection of its own, so it is not a fourth view. It is
	reached at `/<workspace>/-/journal` and `/<workspace>/<ref>/-/journal` (`journalPageOf`).

	**Everything here is a rendering of what `/v1/journal` and an item's journal already say.**
	Who did each thing, through which door and never with which credential, what a change moved
	between, and how a comment opens. No entry carries a whole text (`#2728`), so nothing here
	renders prose as Markdown: an opening is a sentence cut at a word, and the item has the rest.

	**Drawn the way the list draws an item** (`#2826`, Simon's): each run of entries about one
	item is that item's row, with a line under it for each thing that happened, so the
	project's colour and the item's status are scanned without reading.

	Hook-free, so the render harness can call every component here (`#640`).
*/

import { html } from "./html.js";
import { addressOf, journalAddress } from "./address.js";
import { day } from "./dates.js";
import { clock } from "./marks.js";
import { Row } from "./rows.js";

/*
	**The door a change came in through, in words** — `#2727`. The instance records which door;
	it never records a credential's title here, because a title is whatever its owner chose to
	call it and says something about a colleague's own setup (Simon, 2026-09-16).

	A door this page does not know is left unsaid rather than printed raw: a new door is a word
	to add here, and a key on the page would be a reader decoding a program.
*/
export const DOORS = {
	browser: "in the browser",
	api: "through the API",
	mcp: "through agent tools",
	feed: "from a calendar feed",
	local: "on the instance's own machine",
};

export function mergedEntries (held, arriving) {
	/*
		The entries a page holds once more arrive — newest first, and each once.

		**Keyed by `seq`**, the number the instance gives every event. A page reads from the edge
		of what it holds, inclusively, so the entry at the edge comes back and is dropped here
		rather than drawn twice. Inclusive because two entries can share an instant, and a strict
		bound would lose the second at a page boundary.

		**Sorted rather than trusted.** Both journals answer newest first (`#2772`), and a newer read
		lands above what the page holds while an older one lands below it, so the order is the
		page's to keep rather than any one answer's.
	*/
	const found = new Map();

	for (const entry of [...(held || []), ...(arriving || [])]) found.set(entry.seq, entry);

	return [...found.values()].sort((one, other) => other.seq - one.seq);
}

export function journalBounds (holding, direction) {
	/*
		Where a read starts, given what the page holds — `#2731`, `#1428`.

		**Nothing on arrival.** **The newest entry held, for what is newer**, and **the oldest, for
		what is older** — each an instant, which `journalRequest` sends inclusively. **An item's
		cursor, for older**, because its route pages by cursor rather than by a period.

		Pure, so the decision is checkable without a mounted page (`#640`): a wrong edge here reads
		the same page again or skips one, and looks like nothing at all.
	*/
	const held = (holding && holding.entries) || [];

	return {
		from: direction === "newer" && held.length > 0 ? held[0].created_at : null,
		until: direction === "older" && held.length > 0 ? held[held.length - 1].created_at : null,
		cursor: direction === "older" && holding ? holding.cursor || null : null,
	};
}

export function journalAfter (holding, direction, {
	address, arriving = [], more = false, cursor = null, kind = null,
}) {
	/*
		What the page holds once a read lands — `#2731`, `#1428`.

		**On arrival, what arrived.** **Newer and older are merged into what was held**, each once
		and newest first. **Older says whether there is further back**, from the read that just
		went there; a newer read leaves that answer as it was.

		**A newer read that could not reach what the page held has a gap behind it**: more arrived
		than one read carries, and none of it meets the newest entry already drawn. The page then
		starts again from what arrived, with older to ask for, rather than drawing two runs as if
		they were one.
	*/
	const held = (holding && holding.entries) || [];
	const reaches = held.length === 0 || arriving.some((entry) => entry.seq <= held[0].seq);
	const gap = direction === "newer" && more && !reaches;
	const kept = direction === "newer" && !gap;

	return {
		address,
		kind: kind || (holding && holding.kind) || null,
		entries: mergedEntries(direction === null || gap ? [] : held, arriving),
		older: kept ? Boolean(holding && holding.older) : more,
		cursor: kept ? (holding && holding.cursor) || null : cursor,
	};
}

export function byDay (entries) {
	/*
		Entries grouped under the day each happened, in the reader's own zone and in the order
		given.

		**The reader's zone, because these are instants the program recorded**, not days somebody
		wrote: `day` with no zone is *when was this, where I am*, which is the question.
	*/
	const days = [];

	for (const entry of entries || []) {
		const heading = day(entry.created_at);
		const last = days[days.length - 1];

		if (last && last.day === heading) {
			last.entries.push(entry);
		} else {
			days.push({ day: heading, entries: [entry] });
		}
	}

	return days;
}

/*
	What a comment's entry says happened, in `views._HAPPENED`'s words — the verb alone, because
	the row above names the item it was written on.
*/
const COMMENTED = {
	created: "commented",
	updated: "edited a comment",
	deleted: "deleted a comment",
};

/*
	The one action whose line is not its verb and *it* - `#2862`, Simon: *released it* reads as a
	release of code, where this is a lease being given up. `views._AN_ACTION` carries the same
	phrase for the terminal and the agent tools, and *claimed it* is left as it was.
*/
const HELD = {
	released: "released the claim",
};

export function changeInWords (change) {
	/*
		One change as its line says it — `#2826`, Simon's example: *changed status*, then what it
		moved between.

		**`views.change_in_words` in the browser's words** (decision `#2823`): an empty side as
		*never*, *nobody* or *nothing*, a chosen name in quotes, and a date in the reader's own
		locale. **A change with neither side named is its name alone**, which is what a text
		change always is here (`#2728`) and what an id nothing can name becomes (`#1430`). A test
		drives this and the server's renderer over the same changes.
	*/
	if (change.before === null && change.after === null) return `changed ${change.said}`;

	return `changed ${change.said} from ${sideInWords(change, change.before)} to ${
		sideInWords(change, change.after)}`;
}

function sideInWords (change, value) {
	/* One side of a change as it is read: its empty word, a day, quoted, or as it is. An
	   instance older than the words sends no `empty`, and every empty side was *nothing*. */
	if (value === null || value === undefined) return change.empty || "nothing";

	if (change.dated) return dayInWords(value);

	return change.quoted ? `"${value}"` : value;
}

function dayInWords (written) {
	/* A journal's date arrives as `2026-09-18` or `2026-09-18T17:00`, **already in the item's
	   own zone**, so the day is written from its text and never converted — `day` returns a
	   bare date untouched for exactly that reason — and the time is the text after it. */
	const text = String(written);

	if (!/^\d{4}-\d{2}-\d{2}/.test(text)) return text;

	const shown = day(text.slice(0, 10));

	return text.length > 10 ? `${shown}, ${text.slice(11, 16)}` : shown;
}

export function linkOf (entry) {
	/*
		What a link entry joined, from this item's side: the other item, the kind of link, and
		whether it was made or undone — `#2826`.

		**The other end, whichever end this is.** An entry about a link names the item it was made
		from, and read through the other item it names that one. **Null for anything it cannot
		read**, so the line says what happened rather than guessing at a number.
	*/
	if (entry.entity_type !== "link") return null;

	const made = entry.action !== "deleted";
	const side = (field) => {
		const change = (entry.changed || []).find((one) => one.field === field);

		return change ? (made ? change.after : change.before) : null;
	};
	const source = Number(side("source"));
	const target = Number(side("target"));
	const other = source === entry.item_ref ? target : source;
	const type = side("link_type");

	if (!Number.isInteger(other) || other <= 0 || !type) return null;

	return { made, other, type: String(type).replaceAll("_", " ") };
}

export function linesOf (entry) {
	/*
		What one entry adds under its item's row, a line a thing that happened — `#2826`.

		**The row names the item, so a line never does**: *commented*, *changed status from…*,
		*linked it to #2803*. **A line a change** for an update, because each is its own fact.
		Creating something is *created it*, whatever columns it was born with: drawing those would
		bury the page under what every new item looks like.
	*/
	const base = {
		at: entry.created_at,
		actor: entry.actor || "the instance",
		door: entry.actor_interface || null,
	};

	if (entry.entity_type === "comment") {
		return [{
			...base,
			text: COMMENTED[entry.action] || `${verbOf(entry)} a comment`,
			said: entry.said || null,
			cut: Boolean(entry.said_truncated),
		}];
	}

	const link = linkOf(entry);

	if (link) return [{ ...base, link }];

	if (entry.action === "updated" && (entry.changed || []).length > 0) {
		return entry.changed.map((change) => ({ ...base, text: changeInWords(change) }));
	}

	return [{ ...base, text: HELD[entry.action] || `${verbOf(entry)} it` }];
}

function verbOf (entry) {
	/* An action as a word: `created`, `claimed`, `moved`. */
	return String(entry.action || "").replaceAll("_", " ");
}

export function collapsed (lines) {
	/*
		A row's lines with adjacent repeats said once — Simon's rule, 2026-09-17.

		**Identical lines** — the same person, through the same door, saying the same thing —
		**become one, with the newest time and how many times.** **Links of one kind, made or
		undone together by one person, become one line naming every item**, in the order they
		were joined: an item linked to nine others drew nine lines that looked the same. **Only
		adjacent lines**, so a claim, a change and a claim again stay three, because the order is
		what the history is read by.

		Lines arrive newest first, so the line already kept is the newer and its time stands.
	*/
	const kept = [];

	for (const line of lines || []) {
		const last = kept[kept.length - 1];
		const alongside = Boolean(last) && last.actor === line.actor && last.door === line.door;

		if (alongside && last.link && line.link
			&& last.link.made === line.link.made && last.link.type === line.link.type) {
			if (last.link.others.includes(line.link.other)) {
				last.count += 1;
			} else {
				last.link.others.unshift(line.link.other);
			}

			continue;
		}

		if (alongside && !last.link && !line.link
			&& last.text === line.text && last.said === line.said) {
			last.count += 1;

			continue;
		}

		kept.push(line.link
			? { ...line, count: 1, link: { ...line.link, others: [line.link.other] } }
			: { ...line, count: 1 });
	}

	return kept;
}

export function journalRows (entries) {
	/*
		A day's entries as rows, **consecutive entries about one item sharing its row** — `#2826`,
		Simon's. A row is the item; its lines are what happened to it, newest first.

		**Consecutive, not every entry about the item**, so the page still reads in the order
		things happened: an item touched this morning and again this afternoon, with something else
		between, is two rows. **An entry about no item** — a workspace being made — is a row of its
		own, named by what the journal calls it.
	*/
	const rows = [];

	for (const entry of entries || []) {
		const last = rows[rows.length - 1];
		const about = entry.item_ref
			? `#${entry.item_ref}`
			: `${entry.entity_type}:${entry.item_title || ""}`;

		if (last && last.about === about) {
			last.lines.push(...linesOf(entry));
		} else {
			rows.push({
				about,
				seq: entry.seq,
				ref: entry.item_ref || null,
				title: entry.item_title || null,
				project: entry.item_project_path || null,
				lines: linesOf(entry),
			});
		}
	}

	return rows.map((row) => ({ ...row, lines: collapsed(row.lines) }));
}

function Line ({ line, workspace, address = null }) {
	/*
		One thing that happened, under its item's row: when, who, what — and the door it came
		through and how many times, quiet at the end, because they are context rather than news.
	*/
	const door = line.door ? DOORS[line.door] : null;
	/* A statement of its own rather than inline in the markup: the words end in *from*, and the
	   served-module scan reads *from* and a quote after it as an import. */
	const joined = line.link && line.link.made ? "linked it to" : "unlinked it from";
	const others = line.link
		? line.link.others.map((other, index) => html`${index === 0 ? "" : ", "}<a
			href=${addressOf({ ref: other }, workspace)}>#${other}</a>`)
		: null;

	return html`
		<li>
			<span class="clock">${clock(line.at)}</span>
			<span class="what">
				<strong>${line.actor}</strong>${" "}${line.link
					? html`${joined}${" "}${others}${
						" "}(${line.link.type})`
					: line.text}${line.said
					? html`${" "}<span class="said">"${line.said}${line.cut ? "…" : ""}"</span>${
						line.cut && address ? html`${" "}<a class="rest" href=${address}>more</a>` : null}`
					: null}${line.count > 1
					? html`<span class="door"> · ${line.count} times</span>`
					: null}${door ? html`<span class="door"> · ${door}</span>` : null}
			</span>
		</li>
	`;
}

function JournalRow ({ row, item = null, workspace, onGo = null }) {
	/*
		One item's row with what happened to it underneath — `#2826`.

		**The list's own row where the item could be read**, so the project's colour, the status
		and the marks are scanned the way the list is, and a line takes the row's colour bar by
		being inside it. **No Complete button and nothing opened in place**: this is a record, and
		its links are ordinary links to the item's page.

		**A plain row where it could not** — deleted since, or about something with no row of its
		own, like a workspace — drawn from what the journal named.
	*/
	const filed = row.project;
	const address = !row.ref
		? null
		: item
		? addressOf(item, workspace)
		: addressOf({ ref: row.ref, project_key: filed, project_path: filed }, workspace);
	const lines = html`
		<ul class="happened">
			${row.lines.map((line, index) => html`
				<${Line} key=${index} line=${line} workspace=${workspace} address=${address} />
			`)}
		</ul>
	`;

	if (item) {
		return html`
			<${Row} item=${item} workspace=${workspace} place=${{ workspace, project: null }}
				onGo=${onGo}>${lines}<//>
		`;
	}

	const identity = html`
		${row.ref ? html`<span class="stamp"><span class="ref">#${row.ref}</span></span>` : null}
		<span class="title">${row.title}</span>
	`;

	return html`
		<li>
			${address
				? html`<a class="row" href=${address}>${identity}</a>`
				: html`<span class="row">${identity}</span>`}
			${lines}
		</li>
	`;
}

function ReadInstead ({ workspaces }) {
	/*
		The journals a reader can open from a page that shows none, as links — `#2773`.

		**Their real addresses, never a pattern of one.** This said `/workspace/journal`, which
		reads as a path to type, and the address it was first seen refusing was that pattern with
		a workspace put in. Nothing where the reader has no workspace to offer.
	*/
	const held = workspaces || [];

	if (held.length === 0) return null;

	return html`Open the journal for${" "}${held.map((one, index) => html`${
		index === 0 ? "" : index === held.length - 1 ? " or " : ", "
	}<a href=${journalAddress({ workspace: one.slug, ref: null })}>${one.title}</a>`)}.`;
}

export function Journal ({
	page = null, journal = null, workspaces = [], address = null, onOlder = null, busy = false,
	items = {}, onGo = null,
}) {
	/*
		The page: what happened, newest first, a day at a time.

		**`journal` is the answer for the page at `address`**, and an answer for another page is
		drawn as *Reading…* rather than drawn here, so a journal left behind by the last page is
		never shown under this one's heading.

		**Older is asked for, never fetched ahead**: the latest hundred are what somebody opening
		this is looking for, and each page further back is a request they chose.

		**`items` are the rows, by number**, read beside the journal (`journalItemsRequests`);
		a number not in it draws a plain row from what the journal named.
	*/
	if (!page) {
		return html`
			<div class="journal">
				<h2 class="area">Journal</h2>
				<p class="empty">
					There is no journal at this address. Each workspace has one, and so does each item in it.${
					" "}<${ReadInstead} workspaces=${workspaces} />
				</p>
			</div>
		`;
	}

	const space = (workspaces || []).find((one) => one.slug === page.workspace);

	if ((workspaces || []).length > 0 && !space) {
		return html`
			<div class="journal">
				<h2 class="area">Journal</h2>
				<p class="empty">
					There is no workspace called ${page.workspace} that you can see.${
					" "}<${ReadInstead} workspaces=${workspaces} />
				</p>
			</div>
		`;
	}

	const current = journal && journal.address === address ? journal : null;
	const entries = current ? current.entries || [] : [];
	const first = entries.find((entry) => entry.item_ref === page.ref);
	const home = `/${encodeURIComponent(page.workspace)}`;

	return html`
		<div class="journal">
			<h2 class="area">Journal</h2>
			<p class="about">
				${page.ref === null
					? html`What has happened in the <a href=${home}>${space ? space.title : page.workspace}</a>${
						" "}workspace, newest first.`
					: html`What has happened to <a href=${`${home}/${page.ref}`}>#${page.ref}${first
						? ` ${first.item_title}` : ""}</a>, newest first.`}
			</p>
			${!current
				? html`<p class="empty">Reading…</p>`
				: current.failed
				? html`<p class="empty">This journal could not be read. ${current.failed}</p>`
				: entries.length === 0
				? html`<p class="empty">Nothing has happened here that you can see.</p>`
				: html`
					${byDay(entries).map((group) => html`
						<h3 class="day">${group.day}</h3>
						<ul class="rows">
							${journalRows(group.entries).map((row) => html`
								<${JournalRow} key=${row.seq} row=${row} item=${row.ref ? items[row.ref] || null : null}
									workspace=${page.workspace} onGo=${onGo} />
							`)}
						</ul>
					`)}
					${current.older && onOlder
						? html`<p class="older">
								<button type="button" class="action" disabled=${busy}
									onClick=${onOlder}>Older</button>
							</p>`
						: null}
				`}
		</div>
	`;
}
