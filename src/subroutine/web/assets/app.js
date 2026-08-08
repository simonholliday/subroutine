/*
	The browser app — item `#597`. Read-only: see what there is, and read one in full.

	**No build step.** Preact and htm are served as written from `/app/`, and an import map in
	`index.html` resolves the one bare specifier between them. What that buys is not
	convenience: it is that the source a reader is served is the source in the repository,
	which is what the AGPL's network-use clause is about (§2.2), and there is no npm closure
	for `scripts/check_licences.py` to be structurally unable to see.

	**It talks only to the public API** (`#351`). No private endpoints, nothing a token could
	not do — so anything this page can show, a script can too, and the UI cannot quietly become
	the only way to do something.
*/

import { h, render } from "preact";
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import htm from "htm";

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

class Refused extends Error {
	/* Carries the status so the caller can tell "sign in" from "something went wrong". */
	constructor (status, detail) {
		super(detail || `The instance answered ${status}.`);
		this.status = status;
	}
}

async function api (path) {
	/*
		Every request sends the cookie and nothing else. `credentials: "same-origin"` is the
		default for a same-origin fetch, and it is written out because this page working at all
		depends on it — a second port would be cross-origin, which is the arrangement `#364`
		warns about and the reason the app is served from the instance itself.
	*/
	const answer = await fetch(`/v1${path}`, {
		credentials: "same-origin",
		headers: { accept: "application/json" },
	});

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
	/* The one date worth a column. A deadline outranks a plan, and neither is invented. */
	if (item.due_at && !overdue(item)) return `due ${day(item.due_at)}`;
	if (item.planned_for) return `→ ${day(item.planned_for)}`;

	return null;
}

/* ---- views -------------------------------------------------------------- */

export function Row ({ item, showKind, onOpen }) {
	const badges = marks(item, showKind);

	return html`
		<li>
			<button class="row" onClick=${() => onOpen(item)}>
				<span class="ref">#${item.ref}</span>
				<span class="title">${item.title}</span>
				<span class="when">${when(item)}</span>
				${badges.length > 0 && html`
					<span class="marks">
						${badges.map((mark) => html`
							<span class="mark ${mark.tone || ""}">${mark.text}</span>
						`)}
					</span>
				`}
			</button>
		</li>
	`;
}

export function Listing ({ items, onOpen }) {
	if (items.length === 0) {
		return html`<div class="empty">Nothing here yet.</div>`;
	}

	/*
		**A column that says the same thing on every row says nothing** (§12.2a). The kind is
		shown only on a mixed page: a blank beside "Document" would read as missing data rather
		than as "ordinary", and the word "Task" on every line of a list of tasks is noise.
	*/
	const showKind = new Set(items.map((item) => item.kind)).size > 1;

	return html`
		<ul class="rows">
			${items.map((item) => html`
				<${Row} key=${item.kind + item.ref} item=${item} showKind=${showKind}
					onOpen=${onOpen} />
			`)}
		</ul>
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

export function Detail ({ item, links, comments, onOpen, onBack }) {
	const body = item.description || item.body;

	return html`
		<div class="detail">
			<button class="back" onClick=${onBack}>← All items</button>
			<h2>#${item.ref} ${item.title}</h2>
			<${Facts} item=${item} />

			${body && html`<div class="prose">${body}</div>`}

			${links.length > 0 && html`
				<h3>Links</h3>
				<ul class="linked">
					${links.map((link) => html`
						<li key=${link.id}>
							${link.label}${" "}
							<button onClick=${() => onOpen({ ref: link.other.ref,
								kind: link.other.entity_type })}>
								#${link.other.ref} ${link.other.title}
							</button>
						</li>
					`)}
				</ul>
			`}

			${comments.length > 0 && html`
				<h3>What happened</h3>
				<ul class="comments">
					${comments.map((note) => html`
						<li key=${note.id}>
							<div class="said">${day(note.created_at)}</div>
							<div class="body">${note.body}</div>
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
	const since = useRef(null);

	const load = useCallback(async (slug) => {
		if (!slug) return;

		const scope = `workspace_id=${encodeURIComponent(slug)}`;

		/*
			Two requests because tasks and documents are two collections, and both belong here:
			one counter per workspace serves them (§6.2), so a list holding only tasks tells a
			reader who has learned that a number names an item that half the numbers do not
			exist. That was a real complaint about the CLI listing before it spanned both.
		*/
		const [tasks, documents] = await Promise.all([
			api(`/tasks?${scope}&order=-priority_score&limit=${PAGE}`),
			api(`/documents?${scope}&order=-updated_at&limit=${PAGE}`),
		]);

		setItems([
			...tasks.items.map((row) => ({ ...row, kind: "task" })),
			...documents.items.map((row) => ({ ...row, kind: "document" })),
		]);
	}, []);

	const start = useCallback(async () => {
		setError(null);

		try {
			const identity = await api("/me");
			const first = identity.workspaces[0];
			const slug = (workspace || (first && first.slug)) ?? null;

			setMe(identity);
			setWorkspace(slug);

			/* The head of the feed, so the first poll asks about what happens *next* rather
			   than replaying everything that ever has. */
			const head = await api(`/changes?newest=true&limit=1`);
			since.current = head.items.length > 0 ? head.items[0].seq : 0;

			await load(slug);
		} catch (failure) {
			setError(failure);
		} finally {
			setReady(true);
		}
	}, [load, workspace]);

	useEffect(() => {
		start();
	}, []);

	useEffect(() => {
		if (error || !workspace) return undefined;

		const tick = setInterval(async () => {
			try {
				const seen = await api(
					`/changes?since=${since.current}&limit=1&workspace_id=` +
						encodeURIComponent(workspace),
				);

				if (seen.items.length === 0) return;

				since.current = seen.items[seen.items.length - 1].seq;
				await load(workspace);
			} catch (_) {
				/* A poll that fails changes nothing on screen. The next one may work, and
				   replacing a readable page with an error because a background request
				   timed out is worse than being ten seconds stale. */
			}
		}, POLL_MS);

		return () => clearInterval(tick);
	}, [error, workspace, load]);

	const show = useCallback(async (row) => {
		const kind = row.kind === "document" ? "documents" : "tasks";
		const scope = `workspace_id=${encodeURIComponent(workspace)}`;

		try {
			const [item, links, comments] = await Promise.all([
				api(`/${kind}/${row.ref}?${scope}`),
				api(`/${kind}/${row.ref}/links?${scope}`),
				api(`/${kind}/${row.ref}/comments?${scope}&limit=${PAGE}`),
			]);

			setOpen({ item: { ...item, kind: row.kind }, links: links.items,
				comments: comments.items });
			window.scrollTo(0, 0);
		} catch (failure) {
			setError(failure);
		}
	}, [workspace]);

	const chooseWorkspace = useCallback(async (slug) => {
		setWorkspace(slug);
		setOpen(null);

		try {
			await load(slug);
		} catch (failure) {
			setError(failure);
		}
	}, [load]);

	if (!ready) return html`<div class="app"><div class="empty">Reading…</div></div>`;

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
			</header>

			${open
				? html`<${Detail} ...${open} onOpen=${show} onBack=${() => setOpen(null)} />`
				: html`<${Listing} items=${items} onOpen=${show} />`}

			<footer class="foot">
				<span>${items.length} items</span>
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
	render(html`<${App} />`, document.getElementById("app"));
}
