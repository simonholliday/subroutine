/*
	The frame around a page rather than the page: the theme, the wordmark, the footer, an
	item's fact sheet, and prose rendered from Markdown.

	**Split out of `app.js` by `#1849`**, which moved 10,286 lines into thirteen modules and
	changed nothing about what any of them do. Read `app.js` for what the whole app is; this
	file is one layer of it.
*/

import * as markdown from "./markdown.js";
import { html } from "./html.js";
import { PRODUCT, parseAddress, shortVersion } from "./address.js";
import { unrenderable } from "./answers.js";
import { day } from "./dates.js";
import { followed } from "./grouping.js";
import { written } from "./requests.js";

export function Note ({ note, onUndo, onDismiss }) {
	/*
		What just happened, and what to do if it was not wanted.

		**It says the outcome in words** (`#102`): the tone is a colour and carries nothing by
		itself, so a reader who cannot separate the hues loses none of it. `alert` for a
		failure and `status` for a success, because a screen reader should interrupt for one
		and not the other.

		**`act` is a second button and not a second component** (`#785`). News with something
		to do about it is what this already is — `undo` is exactly that shape — so a release
		notice is one more label rather than a modal, which is the house rule for news and the
		one thing this item asked not to build.
	*/
	if (!note) return null;

	return html`
		<div class=${`note ${note.tone}`} role=${note.tone === "bad" ? "alert" : "status"}>
			<span class="said">${note.text}</span>
			${note.undo && html`<button class="undo action" onClick=${onUndo}>Undo</button>`}
			${note.act && html`
				<button class="undo action" onClick=${note.act.go}>${note.act.label}</button>
			`}
			<button class="dismiss quiet" onClick=${onDismiss}
				aria-label="Dismiss this message">×</button>
		</div>
	`;
}

/* The three states a reader can be in. `system` is the default and is the absence of a
   choice — `index.html` writes no attribute for it, so `prefers-color-scheme` decides. */
export const THEMES = [
	["system", "Match system"],
	["light", "Light"],
	["dark", "Dark"],
];

export function themeChoice (storage) {
	/*
		Which theme this browser has been told to use — one of `THEMES`, never anything else.

		**Anything unrecognised reads as `system`**, which is the same answer as nothing stored.
		A value written by an older version, by a person poking at storage, or by another app on
		this origin must not put the page into a state no control can get it out of.

		Takes the storage rather than reaching for `localStorage`, because storage throws in some
		privacy modes and because the render harness runs in Node, where it may not exist at all.
	*/
	try {
		const chosen = storage && storage.getItem("theme");

		return THEMES.some(([key]) => key === chosen) ? chosen : "system";
	} catch (unavailable) {
		return "system";
	}
}

export function applyTheme (chosen, storage, root) {
	/*
		Record the reader's choice and put it into force, returning what was actually applied.

		**`system` removes the attribute rather than setting one**, because the stylesheet's
		three states are two selectors and their absence — so writing `data-theme="system"` would
		be a fourth state matching neither, and the page would be stuck on light.

		Storage is written first and the attribute second, and a storage failure still applies
		the theme: not remembering it is worse than not honouring it, but only for the next load.
	*/
	const wanted = THEMES.some(([key]) => key === chosen) ? chosen : "system";

	try {
		if (storage) {
			storage.setItem("theme", wanted);
		}
	} catch (unavailable) {
		/* A private window can refuse to remember. The choice still applies to this page. */
	}

	if (root) {
		if (wanted === "system") {
			delete root.dataset.theme;
		} else {
			root.dataset.theme = wanted;
		}
	}

	return wanted;
}

export function Theme ({ chosen, onChoose }) {
	/*
		The reader's light-or-dark control — `#908`, requirement 8 of `#441`.

		**In the footer** because §1.4 says a control nobody needs is not shown, and a theme is
		wanted by roughly everybody once and almost never again — so it belongs with the
		set-once things rather than on the masthead, which answers *what am I looking at*.

		Hook-free like everything else here, so the render harness can call it (`#640`). What it
		is given is the current choice; what it does is hand back a new one.
	*/
	return html`
		<label class="theme">
			<span>Theme</span>
			<select value=${chosen} onChange=${(event) => onChoose(event.currentTarget.value)}>
				${THEMES.map(([key, offer]) => html`
					<option value=${key} selected=${key === chosen}>${offer}</option>
				`)}
			</select>
		</label>
	`;
}

export function Wordmark ({ version, onHome }) {
	/*
		The masthead: what this is, which build it is, and the way home.

		**A component for the reason `Foot` is one** (`#640`). `App` uses hooks, so the render
		harness cannot call it and nothing checks anything written inside it — which is why the
		version had to leave `App`'s markup to be guarded at all rather than merely to be tidy.

		**The masthead goes home** (`#868`), and `/` is the right destination by decision `#649`
		rather than by convention alone: it is the agenda across every workspace, because a bare
		`subroutine` prints the agenda and one product answers one question the same way on both
		surfaces. **A real anchor**, never a click handler, which is what makes *open in a new
		tab*, *copy link address* and middle-click work and what makes a screen reader announce
		a link at all.

		**The build is inside the heading and outside the link** (`#1536`). The anchor means *go
		to the agenda*, and a build number read out as part of that names a worse destination
		than the one it goes to. As a sibling it is still inside the heading.

		**Which build served this page is what a trial user cannot otherwise answer**, and until
		now answering it meant scrolling to the footer. `#784` put it there for exactly this
		reason — its reader is on another machine and every defect arrives as prose — and this
		supersedes that placement rather than joining it, so the fact stays in one place.

		**Absent is *not answered yet*, never *no version*.** `/v1/me` is the first request this
		app makes and this renders before it lands, so an empty element here would report the
		absence of a fact as though it were one.

		The mark is drawn by the stylesheet rather than by markup, so there is nothing here to
		give a text alternative to: `#102` forbids saying anything in a shape alone, the word
		beside it carries the whole meaning, and a decorative `::before` is what says so.
	*/
	return html`
		<h1>
			<a href="/" onClick=${onHome}>${PRODUCT}</a>
			${version ? html`
				<span class="build" title=${version}>${shortVersion(version)}</span>
			` : null}
		</h1>
	`;
}

export function Foot ({ count, theme, onTheme }) {
	/*
		What is on screen, and the two ways out.

		**Which instance served the page moved to the masthead** (`#1536`), and this no longer
		reports it. `#784` put it here because Simon reads this browser from another machine
		and every defect he finds arrives as prose, so *which build* had to be answerable —
		that reason is unchanged and is better served above the fold, by a reader who should
		not have to scroll to answer it. One fact, in one place; the version is still the
		instance's rather than the page's, because §22.3 forbids the build step that would
		give a page one.

		A component rather than markup inside `App` for the reason every component here is one:
		`App` uses hooks, so the render harness cannot call it and nothing checks what it says
		(`#640`). Written without hooks, this can be checked.
	*/
	return html`
		<footer class="foot">
			${/* **Counts what is on screen, not what was last fetched** (`#652`). */ null}
			<span>${count} items</span>
			${/*
				**The way into the administrative area** (`#1397`), and it is here rather than in
				the masthead because that is what it is: a page a reader opens deliberately and
				rarely, not a fourth arrangement of their work. §1.4's whole constraint is that
				somebody keeping a to-do list never has to meet a workspace, a role or a
				credential — putting this beside the workspace switcher would make it the first
				thing they read.

				**A plain anchor, exactly like its two neighbours.** Every internal navigation in
				this app goes through `followed` so that a click updates the address without a
				reload; this one deliberately does not, because a full load of a page read once
				an hour costs nothing and threading a handler through `Foot` would give this
				component its first reason to know what the app is. The 404 fallback in
				`api/web.unmatched` serves the shell for it, which is what makes the address work
				typed, bookmarked or shared.
			*/ null}
			<a href="/people">People</a>
			<a href="/v1/docs/agent">API</a>
			<a href="https://github.com/simonholliday/subroutine">Source</a>
			<${Theme} chosen=${theme} onChoose=${onTheme} />
		</footer>
	`;
}

function revisedInWords (revisions) {
	/* The value half of the *Revised* row: how many times, by whom, and when.

	   `once` rather than `1 time`, matching `views.revised_in_words` — and the name is left
	   out where the event recorded no actor rather than replaced by a placeholder, because
	   *somebody revised this* is the whole of what is known. */
	const times = revisions.count === 1 ? "once" : `${revisions.count} times`;
	const who = revisions.last_by ? ` by @${revisions.last_by}` : "";

	return `${times}${who} on ${day(revisions.last_at)}`;
}


export function Facts ({ item, prioritised = [] }) {
	/*
		**A field nobody set is not printed** (§12.2c). That rule is what lets `subroutine show`
		answer "buy milk" with a number, a title and nothing else, and it is the same rule here:
		a screen of empty labels tells a reader this system wants things from them.
	*/
	const rows = [];
	const add = (label, value) => value && rows.push([label, value]);

	/*
		**Compared on the path, never on the key.** A key is unique only among its siblings since
		`#958`, so two projects may be keyed `dist` and comparing keys would mark the wrong one.
	*/
	const raised = prioritised.includes(item.project_path || item.project_key);

	/*
		**Status, type, assignee and tags are marks now** (`#1019`) and are not repeated here —
		a fact sheet three lines below a chip saying the same word is the duplication this
		change was about, one surface along.

		**The project stays and is not one of those**, because only this row can say
		*(prioritised)*. `#982` §4 refuses that as a *mark* — it would appear on 84% of rows
		here, and a visible magnitude invites *"can I set it to 2?"*, which is the dial that
		design declines. `#986` put it here for the opposite reason: a fact sheet is about one
		item, so the rule that drops a repeated mark does not reach it, and it answers *why is
		this ranked where it is* exactly once, beside the project it is about.
	*/
	add("Project", item.project_key && (
		raised ? `${item.project_key} (prioritised)` : item.project_key
	));
	add(
		"Priority",
		item.importance && item.urgency ? `!${item.importance}/${item.urgency}` : null,
	);
	/* **`snoozed_until` was settable before it was showable**, which `#756` made worse rather than
	   introduced: the form can set it, and a field a reader can write and never read back is
	   `#515`'s shape — every step reports success and they are left confirming the wrong
	   conclusion. The CLI has printed it as *from <date>* since M1. */
	add("Starts", day(item.starts_at, item.timezone, item.starts_is_all_day));
	add("Due", day(item.due_at, item.timezone, item.due_is_all_day));
	/* **Named for what it does rather than for the column it used to share** (`#854`). This
	   line and the one above both read `snoozed_until` and `start_at` before the rename, so
	   the item page said *Starts* about a defer while the form beneath it said *Hidden until*
	   about the same value — one column under two opposite names, three clicks apart. */
	add("Hidden until", day(item.snoozed_until, item.timezone, item.snoozed_is_all_day));
	/*
		**How it repeats, in the words the *rule* produces** — `#925`, and §6.7's whole argument.
		Simon: *"an indicator of how the task repeats, based on its parsed and translated status
		in the database, **not** the original string that I typed in (which could have been
		misinterpreted)"*.

		`recurrence_description` is generated on the server by `recurrence.describe`, so this is
		the one sentence every surface shows and no client holds a copy of the grammar. The
		phrase somebody typed is deliberately not here: reading your own input back confirms
		nothing, which is the difference between a check and a mirror.
	*/
	add("Repeats", item.recurrence_description);
	add("Estimate", item.estimate_human);
	add("Parent", item.parent_ref ? `#${item.parent_ref} ${item.parent_title || ""}` : null);
	add("Updated", day(item.updated_at));

	/* **That the body has been replaced, which nothing said until `#1768`.** `Updated` above
	   moves for any change at all — a status, an assignee, a rank — so it could never answer
	   *is what I am reading a later draft than the comment beneath it*. This counts only
	   replacements of the body, which is what decision `#1766` asks people to make instead of
	   correcting underneath.

	   **The words are `views.revised_in_words`' and the arrangement is not**, deliberately.
	   Every other row here is a label and a value in a definition list, and a sentence
	   repeating its own label would read wrongly in that column — so the count, the name and
	   the day are the same and the leading word is the `<dt>`. `views.principal_named` has
	   the same shape for the same reason: this is the one renderer that is not Python. */
	add("Revised", item.revisions ? revisedInWords(item.revisions) : null);

	if (rows.length === 0) return null;

	return html`
		<dl class="facts">
			${rows.map(([label, value]) => html`
				<dt>${label}</dt><dd>${value}</dd>
			`)}
		</dl>
	`;
}

export function Prose ({ text, className, where, onOpen }) {
	/*
		**The whole of this app's trust boundary, and the only `dangerouslySetInnerHTML` in
		it.** Everything a reader sees as formatted text arrives here, and the safety of that
		is entirely `markdown.render`'s: it escapes every run of text it emits and constructs
		every tag itself, so nothing written into a description can become markup.

		A second call site would be a second thing to be sure about. If formatted text is
		needed somewhere else, use this component rather than the property it wraps.

		`where` turns a `#42` in the prose into a link to that item, and its absence leaves one
		as text — which is what it was before `#638` and what it stays wherever the workspace is
		not known.
	*/

	/*
		Mentions are caught here rather than followed.

		The anchor is a real one, so it copies, opens in a new tab and works with JavaScript
		off — the whole reason `#638` puts an address on every item. Following it in *this* tab
		would reload the page for a link to something the app already knows how to show, which
		on a description with forty mentions in it is forty page loads. One listener on the
		container rather than one per link, because the HTML is set as a string and there is
		nothing to attach a handler to.
	*/
	const caught = (event) => {
		if (!onOpen) return;

		const anchor = event.target.closest && event.target.closest("a.mention");

		if (!anchor) return;

		const asked = parseAddress(new URL(anchor.href, window.location.origin).pathname);

		if (asked === null) return;

		/* **`opens` was written from the copy that used to be here** (`#722`), which was the only
		   place in the app that had the rule. It is shared now, so a row and a mention behave the
		   same way — which is what a reader expects and what they did not get. */
		followed(event, () => onOpen({ ref: asked.ref }));
	};

	/*
		**Rendering stored text cannot take the page with it** (`#680`).

		This is the only place arbitrary input becomes output, and the input is written by
		anybody with a credential — including on *somebody else's* item, since a comment renders
		through here too. `#679` closed the one way it was known to fail; this closes the ones
		nobody has found, and unlike the boundary above it depends on no framework behaviour, so
		the harness can prove it.

		The text is shown escaped rather than dropped, because a description a reader cannot see
		at all is worse than one that lost its formatting.
	*/
	let rendered;

	try {
		rendered = markdown.render(text, where);
	} catch (failure) {
		const note = unrenderable(failure, "This text");

		/*
			**The fallback may not repeat the step that failed, and the first version did.**
			It showed the text by way of `String(text)` — which is the first thing `render`
			does, so a value that threw *there* threw again here and the catch achieved
			nothing. Caught by the test, which is the whole reason the payload is a value that
			fails on being stringified rather than one that nests too deeply.
		*/
		let written = "";

		try {
			written = String(text);
		} catch (_) {
			written = "";
		}

		rendered = `<p class="broke"><strong>${markdown.escaped(note.said)}</strong> `
			+ `${markdown.escaped(note.detail)}</p>`
			+ (written === "" ? "" : `<pre><code>${markdown.escaped(written)}</code></pre>`);
	}

	return html`<div class=${className} onClick=${caught}
		dangerouslySetInnerHTML=${{ __html: rendered }}></div>`;
}
