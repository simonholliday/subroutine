/*
	What happened, in a workspace or to one item — `#2731` and `#1428`, design `#2724`.

	**A page drawn in place of the work, as the settings and people pages are**, and for their
	reason: it shows no rows, arrangement or selection, so it is not a fourth view. It is
	reached at `/<workspace>/journal` and `/<workspace>/<ref>/journal` (`journalPageOf`).

	**Everything here is a rendering of what `/v1/journal` and an item's journal already say.**
	Who did each thing, through which door and never with which credential, what a change moved
	between, and how a comment opens. No entry carries a whole text (`#2728`), so nothing here
	renders prose as Markdown: an opening is a sentence cut at a word, and the item has the rest.

	**The wording of each kind of entry is later work** (Simon, 2026-09-16), so an entry says
	what `subroutine journal` says, in the same words.

	Hook-free, so the render harness can call every component here (`#640`).
*/

import { html } from "./html.js";
import { addressOf } from "./address.js";
import { day } from "./dates.js";
import { clock } from "./marks.js";

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

		**Sorted rather than trusted.** `/v1/journal` answers its newest page in the order things
		happened and an item's journal answers newest first (`#2772`), so the page puts the two in
		one order itself.
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

export function happened (entry) {
	/*
		What an entry says happened, in `subroutine journal`'s words: the action, and for a
		comment, that it was written on the item named beside it.
	*/
	const verb = String(entry.action || "").replaceAll("_", " ");

	return entry.entity_type === "comment" ? `${verb} a comment on` : verb;
}

export function movedBetween (entry) {
	/*
		The lines an entry adds under its headline — `subroutine journal`'s rule.

		**Only an update's changes.** Creating a task writes a change for every field it was born
		with, so drawing those would bury the page under what every new item looks like.

		**A change with neither side named is its phrase alone**, which is what a text change
		always is here (`#2728`) and what an id nothing can name becomes (`#1430`).
	*/
	if (entry.action !== "updated") return [];

	return (entry.changed || []).map((change) => (
		change.before === null && change.after === null
			? change.said
			: `${change.said}: ${change.before ?? "nothing"} to ${change.after ?? "nothing"}`
	));
}

function Entry ({ entry, workspace }) {
	/*
		One thing that happened: when, who and through which door, what, and to which item.

		**The item is a link, addressed as it is filed** (`#2727`), so it opens where a reader
		would find it rather than at a bare number.
	*/
	const filed = entry.item_project_path;
	const address = entry.item_ref
		? addressOf({ ref: entry.item_ref, project_key: filed, project_path: filed }, workspace)
		: null;
	const door = entry.actor_interface ? DOORS[entry.actor_interface] : null;
	const lines = movedBetween(entry);

	return html`
		<li class="entry">
			<span class="clock">${clock(entry.created_at)}</span>
			<div class="happened">
				<p class="headline">
					<strong>${entry.actor || "the instance"}</strong>
					${door ? html` <span class="door">${door}</span>` : null}
					${" "}${happened(entry)}${" "}
					${address
						? html`<a href=${address}>#${entry.item_ref} ${entry.item_title}</a>`
						: entry.item_title}
				</p>
				${lines.length > 0
					? html`<ul class="changes">${lines.map((line) => html`<li>${line}</li>`)}</ul>`
					: null}
				${entry.said
					? html`<p class="said">
							${entry.said}${entry.said_truncated ? "…" : ""}
							${entry.said_truncated && address
								? html` <a class="rest" href=${address}>The rest is on the item.</a>`
								: null}
						</p>`
					: null}
			</div>
		</li>
	`;
}

export function Journal ({
	page = null, journal = null, workspaces = [], address = null, onOlder = null, busy = false,
}) {
	/*
		The page: what happened, newest first, a day at a time.

		**`journal` is the answer for the page at `address`**, and an answer for another page is
		drawn as *Reading…* rather than drawn here, so a journal left behind by the last page is
		never shown under this one's heading.

		**Older is asked for, never fetched ahead**: the latest hundred are what somebody opening
		this is looking for, and each page further back is a request they chose.
	*/
	if (!page) {
		return html`
			<div class="journal">
				<h2 class="area">Journal</h2>
				<p class="empty">There is no journal at this address. A workspace has one, at
					<code>/workspace/journal</code>, and so does each item in it.</p>
			</div>
		`;
	}

	const space = (workspaces || []).find((one) => one.slug === page.workspace);

	if ((workspaces || []).length > 0 && !space) {
		return html`
			<div class="journal">
				<h2 class="area">Journal</h2>
				<p class="empty">There is no workspace called ${page.workspace} that you can see.</p>
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
					? html`Everything done in <a href=${home}>${space ? space.title : page.workspace}</a>${
						" "}that you can see, newest first.`
					: html`Everything done to <a href=${`${home}/${page.ref}`}>#${page.ref}${first
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
						<ol class="entries">
							${group.entries.map((entry) => html`
								<${Entry} key=${entry.seq} entry=${entry} workspace=${page.workspace} />
							`)}
						</ol>
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
