/*
	What comes back, and what to do when it is a refusal — the error type, the merge rules
	that keep a page in order as pages arrive, and the boundary that catches a render.

	**Split out of `app.js` by `#1849`**, which moved 10,286 lines into thirteen modules and
	changed nothing about what any of them do. Read `app.js` for what the whole app is; this
	file is one layer of it.
*/

import { Component, render } from "preact";
import { html } from "./html.js";
import { answers } from "./address.js";
import { DEFAULT_ORDER, DEFERRED, ORDERINGS, deferred } from "./dates.js";
import { sent } from "./requests.js";

class Refused extends Error {
	/*
		Carries the status so the caller can tell "sign in" from "something went wrong", and the
		problem document so a caller can read what the refusal *carried*.

		**The body was parsed and thrown away until `#757`**, which kept `detail` and nothing
		else. §8.9's 409 attaches the current entity — `concurrency.reporting()` does it
		deliberately, so a client can say what changed rather than only that something did — and
		that was arriving and being discarded one line before anybody could read it.

		`body` is null when the answer was not JSON, which is what stops a caller mistaking a
		proxy's HTML error page for a problem document.
	*/
	constructor (status, detail, body = null) {
		super(detail || `The instance answered ${status}.`);
		this.status = status;
		this.body = body;
	}
}

export function refusal (status, problem) {
	/*
		What a refused request becomes — exported so the *composition* can be checked (`#757`).

		`conflictIn` reads `failure.body.current`, and testing it against a hand-written object
		says nothing about whether a real refusal ever carries one. It did not: `api` parsed the
		problem document, kept `detail` and threw the rest away, one line before anybody could
		read it — and a mutation putting that back passed the whole suite, because the only test
		of the reader had built its own input.

		So the two steps meet in a function, and a test can hand this a real problem document
		and ask `conflictIn` what it makes of it.
	*/
	return new Refused(status, problem && problem.detail, problem);
}

export async function api (path, { method = "GET", body = null } = {}) {
	/*
		Every request sends the cookie and nothing else. `credentials: "same-origin"` is the
		default for a same-origin fetch, and it is written out because this page working at all
		depends on it — a second port would be cross-origin, which is the arrangement `#364`
		warns about and the reason the app is served from the instance itself.

		**One request function, reads and writes together.** Every refusal then arrives the same
		way, which is what lets a failed write say what happened rather than disappearing — and
		a second one would be a second place for the credential rules to be written down.
	*/
	const sending = body !== null;

	const answer = await fetch(`/v1${path}`, {
		method,
		credentials: "same-origin",
		headers: sending
			? { accept: "application/json", "content-type": "application/json" }
			: { accept: "application/json" },
		body: sending ? JSON.stringify(body) : undefined,
	});

	if (answer.status === 204) return null;

	if (answer.ok) return answer.json();

	/* A problem document carries `detail`; anything else is reported by its status alone
	   rather than by guessing at a body we did not parse. */
	let problem = null;

	try {
		problem = await answer.json();
	} catch (_) {
		problem = null;
	}

	throw refusal(answer.status, problem);
}

/* ---- one list, not two end to end (`#660`) ------------------------------- */

export function newestFirst (rows) {
	/*
		Put tasks and documents into one order, which is the order they were written.

		**The list is two requests and has to be one list.** Tasks and documents are separate
		collections — §6.2 gives them one ref counter precisely so a reader can treat them as one
		thing — and concatenating them put every document below every task. On a project holding
		122 tasks against a page of 100, a document written a minute ago started at row 101 at
		best. Simon met it on `#659`: *"I did not see your new item arrive, but then I saw it is
		some way down the list."*

		**`created_at` rather than `ref`, and that is the whole of why the field is asked for.**
		A ref would be a fine proxy — one counter, allocated in order — but the *server* sorts by
		`-created_at` and its keyset cursor pages on it (`ordering.DEFAULT_TASK_ORDER` and
		`DEFAULT_DOCUMENT_ORDER`, both `("-created_at",)`). A client ordering by anything else
		would disagree with the boundary it is paging across, which is the defect keyset
		pagination exists to prevent.

		**`ref` breaks a tie**, descending, because the server's tiebreaker follows the last
		key's direction and refs are allocated from one counter in creation order — so it agrees
		with the server wherever two rows share a timestamp, without asking for `id` as well.

		**Compared as instants, and the honest reason is not the obvious one.** Measured across
		the two shapes this API emits — `…:00+00:00` and `…:00.100000+00:00` — lexicographic
		order and chronological order *agree*, because the characters that differ (`+` against
		`.`) happen to fall the right way. So string comparison would work today, and saying it
		would not would be a claim nothing supports.

		`Date.parse` is kept for what it does at the edges: it is right whatever the
		representation, including an offset other than `+00:00`, which this serialisation does
		not produce and a future one might. Its own cost is that it truncates to the millisecond
		— two rows a microsecond apart tie here — and `ref` resolves exactly that, correctly,
		because refs come from one counter in creation order.
	*/
	return inOrder(rows, ORDERINGS[DEFAULT_ORDER]);
}

export function inOrder (rows, ordering) {
	/*
		Put two collections into the one order the server put each of them in — `#782`.

		**The key follows the ordering, and it has to.** `newestFirst` merged on `created_at`
		because that is what the API sorts and pages both collections by; the moment a reader
		can choose, merging on a fixed key would order the page by one thing while the cursor
		walked another. That is the disagreement keyset pagination exists to prevent, and it
		shows up as rows repeating or vanishing at a page boundary rather than as anything that
		looks like a sorting bug.

		**Only an ordering both collections answer reaches here with two of them.**
		`collectionsFor` drops documents from one they cannot, and `accumulated` does not merge
		a single collection at all — a server has already ordered it, and re-sorting would
		overwrite that with whatever this function believes (`#706`).

		**`ref` breaks a tie, always ascending**, which is oldest first and is what the server
		does: `ordering.clauses` appends the tiebreaker `.asc()` and `parse_order` builds it
		`descending: false`. Refs come from one counter in creation order (§6.2), so ascending
		by ref is ascending by the id the server actually pages on.

		**This said "following the ordering's direction" until `#879`**, and that was true until
		`eecbd93` moved the query side and false afterwards — Simon's decision of 2026-08-13 is
		that age separates rows and says nothing, so it must not inherit a direction from a key
		it has nothing to do with. Four spellings of that rule exist; two moved and two, this
		one and `cli/personal._ordering`, went on asserting in their own docstrings that they
		agreed. That is why the finding was rated above the tie order it changes: **a sentence
		claiming a rule holds is the reason nobody checks it.**

		Compared by kind rather than by field name: an instant through `Date.parse`, which is
		right whatever the representation and truncates to the millisecond — which is exactly
		what the ref tiebreak is for — and text through `localeCompare`, so `Ångström` sorts
		where a reader expects rather than where its code point falls.
	*/
	const key = ordering ? ordering.field : DEFAULT_ORDER.slice(1);
	const descending = !ordering || ordering.descending;
	const way = descending ? -1 : 1;

	const value = (row) => (ordering && ordering.compare === "instant"
		? Date.parse(row[key])
		: row[key]);

	return [...rows].sort((one, other) => {
		/*
			**Deferred work sinks, above everything else the ordering says** — `#877`. Simon's
			decision of 2026-08-14: *"deferred items appearing last. That way they are not
			invisible, but neither are they confused with non-deferred items in lists."*

			**Not multiplied by `way`, and that is the point of a leading key.** Every other
			comparison here follows the ordering's direction; this one does not, because
			*oldest first* must not mean *deferred first*. The reader's choice decides the
			arrangement inside each band and never which band comes first.

			**It has to agree with the server**, which is asked for `deferred,<order>` by
			`sunkOrder` — a client merging on a key the server did not page by is the
			disagreement keyset pagination exists to prevent (`#782`). `sinks` is on the
			ordering rather than assumed here so that the two read one table.

			A document has no `snoozed_until` and lands in the first band, which is exactly what the
			server's own answer for a document is.
		*/
		if (ordering && ordering.sinks) {
			const parked = (row) => (deferred(row) ? 1 : 0);

			if (parked(one) !== parked(other)) return parked(one) - parked(other);
		}

		const first = value(one);
		const second = value(other);

		/*
			**An absent value sorts last, in both directions** — `#794`, and it is the server's
			own rule rather than a choice made here: `ordering.clauses` appends `.nullslast()`
			to every term, ascending and descending alike, so a row with nothing to compare is
			at the end whichever way the reader asked.

			**Without this the comparator was not total, which is worse than being wrong.**
			`Date.parse(undefined)` is `NaN`, and `NaN !== NaN` is *true* — so the branch below
			was taken, both `NaN < x` and `x < NaN` are false, and `compare(a, b)` and
			`compare(b, a)` both answered "after". A sort given a comparator that contradicts
			itself may produce any arrangement at all, so the failure is not a row in the wrong
			place but a page in no order, varying with the engine and the input length.

			**Not multiplied by `way`**, for the same reason the deferred band above is not: the
			direction the reader chose arranges the rows that *have* a value, and says nothing
			about where the ones that do not belong.

			**Latent today and cheap now.** Every request asks for the fields an ordering can
			use, so nothing reaches here undefined — which is exactly the state in which a fix
			costs four lines, and the state that ends the first time a projection is narrowed.
		*/
		const absent = (what) => (
			what === null || what === undefined
			|| (typeof what === "number" && Number.isNaN(what))
		);

		if (absent(first) !== absent(second)) return absent(first) ? 1 : -1;

		if (!absent(first) && first !== second) {
			if (ordering && ordering.compare === "text") {
				return way * String(first).localeCompare(String(second));
			}

			return way * (first < second ? -1 : 1);
		}

		return one.ref - other.ref;
	});
}

export function mergeOrder (selection, rows) {
	/*
		Which order a merged page is actually in — `#875`, `#876`.

		**Three answers, and the middle one is the whole of `#875`.** A reader's explicit choice
		wins. Failing that, a *ranked search* is what the server chose for itself: it defaults a
		search to `-relevance` wherever a backend can compute one, and it says so by populating
		the field — so this reads the data rather than re-deriving the server's rule, which would
		be the same rule written down twice and free to disagree. Failing both, the default.

		**Data rather than configuration on purpose.** The alternative was asking `/v1/meta`
		whether the backend can rank and inferring what the server would have done. That is a
		copy of a decision made elsewhere; a populated field is the decision itself, arriving.
	*/
	const asked = selection && selection.order;
	const chosen = asked && ORDERINGS[asked] ? ORDERINGS[asked] : (
		rows.some((row) => row.relevance !== undefined && row.relevance !== null)
			? ORDERINGS["-relevance"]
			: ORDERINGS[DEFAULT_ORDER]
	);

	/*
		**And it sinks only where the request asked the server to** — `#882`.

		`sunkOrder` sends no `order` at all for a search nobody has given one to, so the server
		applies plain `-created_at` **without** the deferral band. This function then reached for
		`ORDERINGS["-created_at"]`, which carries `sinks: true`, and the merge sank rows the page
		had not been chosen for — the client re-sorting by a rule the server did not use, which
		is the disagreement keyset pagination exists to prevent (`#782`).

		**Two functions, each with a passing test, and nothing comparing them.** That is `#640`'s
		shape, and it shipped in the change whose own docstring says *"the server has to do the
		sinking, not the page"*. So this asks `sunkOrder` rather than deciding again: one
		selection, one answer, and the merge cannot mean something the request did not say.
	*/
	const sent = sunkOrder(selection);

	return sent && sent.startsWith(`${DEFERRED},`) ? chosen : { ...chosen, sinks: false };
}

export function sunkOrder (selection) {
	/*
		The order to ask the API for, with deferred work sinking to the bottom of it — `#877`.

		**The address carries the reader's choice and the request carries the arrangement.**
		Sinking is not something a reader picks, so it has no spelling in `SELECTABLE.order` and
		no key in `ORDERINGS`: it is a *leading* key added to whichever order is in force, which
		is what makes *most important first* mean that within each band rather than across both.

		**The server has to do it, not this app.** Sinking the rows already fetched would sink
		them within a page, and the page is chosen by the order the query ran in — so a first
		page could be all deferred work with everything startable waiting on *Show more*, which
		is a plausible, complete, wrong answer.

		**Three answers, and the middle one is the reason this is not one line.**

		- A reader who chose an order gets it sunk, unless the ordering says otherwise.
		- **A search nobody has given an order to is left alone**, because the server ranks it
		  and naming an order here would overrule that (`#875`). Null means *send no order*.
		- Everything else names the default, which this app has never done: it relied on the
		  API applying `-created_at`, and a leading key cannot be added to an order that is not
		  being sent. `orderedAs` already treats the absence as that default, so nothing a
		  reader sees changes.
	*/
	const asked = (selection || {}).order;

	if (!asked) return (selection || {}).q ? null : `${DEFERRED},${DEFAULT_ORDER}`;

	return ORDERINGS[asked] && ORDERINGS[asked].sinks ? `${DEFERRED},${asked}` : asked;
}

export function unpacked (answers, wanted) {
	/*
		What arrived, whichever shape it arrived in — `#1790`.

		A listing answers `{items, page}` and a grouped one answers `{group_by, groups}`, and
		everything downstream of here wants the same two things out of either: the rows, tagged
		with the collection they came from, and what was left behind.

		**A pure function rather than three branches inside `load`**, which is `#640`'s rule and
		the reason `accumulated`, `columns` and `agendaBuckets` are the best-covered code in this
		file: the harness calls components and helpers as plain functions, so a decision left
		inside `App` is covered by nothing. Four faults have shipped from exactly that gap.

		**The rows come out flat and in the order the groups were sent**, so `columns` goes on
		doing the arranging. It regroups rows that arrived grouped, which sounds redundant and is
		not: the board draws a column for every category whether or not the server sent rows for
		it, and `columns` is where that has always been decided.

		**One `cut` map across both collections is safe because the two vocabularies are
		disjoint** — a task is `todo`/`in_progress`/`done`/`cancelled` and a document is
		`draft`/`current`/`superseded`/`archived`. If a category name were ever added to both,
		this would silently merge two columns' accounts of themselves, so the test says so.

		`cut` is **null** when nothing was grouped, never an empty object: *this answer was not
		split* and *this answer was split and nothing was held back* are different facts, and a
		column heading reads the second as good news.
	*/
	const rows = [];
	const cut = {};
	const more = { tasks: null, documents: null };
	let grouped = false;

	answers.forEach((answer, at) => {
		const kind = wanted[at].kind;

		if (answer.groups) {
			grouped = true;

			answer.groups.forEach((group) => {
				group.items.forEach((row) => rows.push({ ...row, kind }));

				cut[group.key] = {
					more: Boolean(group.page && group.page.has_more),
					cursor: (group.page && group.page.next_cursor) || null,
					total: group.page && group.page.total !== undefined
						? group.page.total
						: null,
				};
			});

			return;
		}

		answer.items.forEach((row) => rows.push({ ...row, kind }));

		more[`${kind}s`] = answer.page.has_more ? answer.page.next_cursor : null;
	});

	return { rows, cut: grouped ? cut : null, more };
}


export function accumulated (held, arriving, { appending, collections, ordering }) {
	/*
		What the list becomes when a page arrives — the whole rule, in one place.

		**Appending is re-merged over everything held** (`#660`): a second page of tasks belongs
		*above* documents already on screen, so extending the array made the list alternate
		between the two collections after one *Show more*, in no order at all.

		**And the merge runs only where there is more than one collection to merge** (`#706`).
		That is the actual reason rather than a proxy for it. `newestFirst` sorts on `created_at`
		because that is the one key both collections are paged by; applied to a single collection
		the *server* has ordered, it silently overwrites that order. The done view asks for
		`-completed_at`, so merging would produce a page ordered by when work was **written**
		under a heading claiming when it was **finished** — plausible, complete and wrong.

		**It takes the count rather than the view name** so that an arrangement added later cannot
		get this wrong by being spelled differently, and it is a pure function rather than three
		lines inside `load` for the reason `#640` has now demonstrated five times: the harness
		calls components as plain functions, so a decision left inside `App` is covered by nothing.
	*/
	const all = appending ? [...held, ...arriving] : arriving;

	/*
		**The ordering is a parameter now, and it was the missing wire** (`#876`). This merged
		on `created_at` whatever the reader had chosen, so *A to Z* on a mixed list produced a
		page in neither order — the server sorted each collection alphabetically and this
		re-sorted the result by when things were written.

		`inOrder` was written for exactly this in `#782` and was reachable only through
		`newestFirst`, which passes the default. A guard even assumed otherwise: the
		ordering-coverage test asserts both collections fetch the ordering's `field` *"because
		`field` is what `inOrder` merges on"*. The data was being fetched for a merge that never
		used it — `#640`'s shape again, the rule right and the display right with nothing
		joining them.
	*/
	return collections > 1 ? inOrder(all, ordering || ORDERINGS[DEFAULT_ORDER]) : all;
}

/* ---- surviving a component that throws (`#680`) -------------------------- */

export function unrenderable (failure, what) {
	/*
		What a reader is shown in place of something that would not render.

		**Pure, and separate from the component, because the component cannot be tested.**
		`preact-render-to-string` does not run error boundaries — measured, both spellings — so
		the harness can prove what this says and cannot prove that Preact calls it. Keeping the
		decision out here is what makes the half we own checkable at all, which is the move
		`#640` arrived at four times.

		It names the thing rather than apologising, and it says the rest of the page is still
		good, because the failure a reader meets is *silence* and the question they have is
		whether anything else can be trusted.
	*/
	const message = (failure && failure.message) || String(failure || "");

	return {
		said: `${what || "This"} could not be displayed.`,
		/* The message verbatim. It is for us rather than for them — but a reader who reports a
		   problem with the words in front of them saves the round trip that asks for them. */
		detail: message,
	};
}

export class Boundary extends Component {
	/*
		Show something when a component below this throws, rather than nothing at all.

		**This exists because two blank pages shipped from one week's work** — an import map
		missing `htm`, and a dependency array naming a value declared below it (`#643`). Both
		threw during the first render, both left an empty document, and both were found by a
		person rather than by the build. A boundary would have turned each into a sentence.

		**What is verified and what is not, stated rather than implied.** `unrenderable` above is
		pure and tested. That Preact calls `componentDidCatch` at all is documented framework
		behaviour this project's harness cannot exercise, because rendering an error boundary
		needs a DOM and `#640` refused jsdom at ~2 MB. So this is belt; `Prose`'s own catch is
		braces, and that one depends on nothing.
	*/

	componentDidCatch (failure) {
		this.setState({ failed: failure });
	}

	render (props, state) {
		if (!state.failed) return props.children;

		const note = unrenderable(state.failed, props.what);

		return html`
			<div class="broke">
				<strong>${note.said}</strong>
				${" "}<span class="detail">${note.detail}</span>
			</div>
		`;
	}
}

/* ---- what this app asks for (`#640`) ------------------------------------- */

/*
	**Every request this app makes is built here, and nowhere else.**

	Three of the four faults this arc shipped were a request the instance refused, or one that
	asked the wrong question: `?limit=` on a route that declares none, `&subtree=true` beside a
	project filter, and a list ordered by something the command line does not order by. Each
	reached a reader, because the decision about *what to ask for* lived inside a component no
	test can render — every one was found by Simon opening the page.

	Out here they are pure: arguments in, `{path, method, body}` out. So `tests/test_web.py` can
	call each one and check the query it builds against the parameters the real route declares —
	`api/query.py` refuses a parameter a route did not ask for, which makes that the same rule
	the instance applies, run before it ships rather than after.

	**Checking the spelling is not that check**, and this is not hypothetical: the test written
	for `subtree` asserted the string was *present*, so it passed while every filtered listing
	422'd into the failure page.
*/
