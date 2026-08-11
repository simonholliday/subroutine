/*
	Markdown, rendered for the browser app — item `#637`.

	**This is the one place in the app where stored text becomes HTML, and everything about
	its design follows from that.** A description is written by anybody holding a credential,
	including every agent, and an agent's output is whatever it read somewhere. So the source
	is never trusted and never passed through: this renderer *escapes every run of text it
	emits* and produces only tags it constructs itself. There is no HTML parser here and no
	allow-list of tags to strip, because no HTML from the source ever reaches the output —
	which is a much smaller thing to get right than sanitising is.

	Two functions are the whole trust boundary. `escaped` is what text goes through; `target`
	is what a link's destination goes through. Nothing else may put a value from the source
	into the output, and a change that does is the change to look at.

	**Escaping is also the correct rendering here, not only the safe one.** Measured across
	this instance: 103 things that look like HTML tags appear in the prose — `<ref>`, `<path>`,
	`<workspace>`, `<session-id>` — and every single one is a placeholder somebody wrote for a
	reader. A renderer that passed HTML through would hand them to the browser as unknown
	elements and the reader would see nothing at all where the placeholder was.

	**The subset is measured rather than chosen.** Over 291 descriptions and documents, 1.6
	million characters: inline code in 94%, bold 88%, italic 73%, bullet lists 55%, headings
	48%, ordered lists 25%, horizontal rules 23%, tables 20%, fenced code 14%, blockquotes 14%.
	Against that — images 0, task lists 0, footnotes 0, HTML entities 0, hard line breaks 0,
	and links just 6 occurrences in 3 pieces. What is not here is not an oversight; it is
	something nobody writes, and adding it later is cheaper than getting it wrong now.
*/

/*
	Emphasis is asterisks only, and that is a measurement rather than a shortcut.

	`_italic_` appears zero times outside code in this instance, and `__bold__` nine times —
	all of them Python dunders inside backticks. What does appear is 205 intraword underscores
	in ordinary prose: `assignee_id`, `project_scope`, `next_ref_number`, written without
	backticks. CommonMark gets those right through flanking rules that are the fiddliest part
	of the whole specification. Not implementing underscore emphasis at all reaches the same
	answer for this corpus, with nothing to get wrong.
*/
const ESCAPES = {
	"&": "&amp;",
	"<": "&lt;",
	">": "&gt;",
	'"': "&quot;",
	"'": "&#39;",
};

/* What a link may point at. A scheme not on this list is not rendered as a link at all —
   see `target`. `subroutine:` is deliberately absent: it is a real scheme in the
   specification (§6.15) and nothing in this app can act on one yet, so a link that looked
   live and did nothing would be worse than showing what was written. */
const SCHEMES = new Set(["http", "https", "mailto"]);

/* How far a heading in stored prose is pushed down. The page's own title is an `h2`, so a
   description's `#` becomes an `h3` and sits under it — user text can never produce an `h1`
   and the document outline stays true whatever somebody writes. */
const HEADING_OFFSET = 2;

/*
	How deeply a nested structure is followed before the rest is shown as it was written
	(`#679`).

	**A block structure recurses once per level and the stack is finite**, which is a defect
	rather than a theoretical limit: measured by binary search, **3,360 nested blockquotes — a
	3,363-character line — throws `RangeError: Maximum call stack size exceeded`**, and so does a
	list nested 2,000 deep, and so does a list of blockquotes alternating. `render` is called
	inside a component during a Preact render, so what a reader saw was a blank page.

	**It was reachable by anybody who can write a comment**, on an item belonging to somebody
	else, which is what made it worth a cap rather than a note.

	**32 is a hundredfold margin below the failure and eight times deeper than anything real.**
	Prose in this instance nests one or two levels; a document 32 deep is not a document. The
	inline path — bold, italic, struck through — recurses too and was measured *not* to fail at
	any size tried, so it is deliberately uncapped: a limit nothing can reach is a control nobody
	can check.
*/
const MAX_DEPTH = 32;

const LIST_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])[ \t]+(.*)$/;
const TABLE_SEPARATOR = /^\s{0,3}\|(?:\s*:?-+:?\s*\|)+\s*$/;
const FENCE = /^\s{0,3}(`{3,}|~{3,})\s*(\S*)\s*$/;
const HEADING = /^\s{0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$/;
const RULE = /^\s{0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$/;
const INDENTED = /^(?: {4}|\t)/;

/*
	Every inline construct in one ordered alternation, so that precedence is the order it is
	written in rather than a sequence of passes over each other's output — which is how a
	renderer comes to interpret the HTML it just produced.

	Code spans are first, so nothing inside backticks is ever read as anything else. Bold
	before italic, because at a run of asterisks the engine takes the first alternative that
	matches. Emphasis refuses a space against its own markers, so `a * b * c` stays as typed.
*/
const INLINE = new RegExp(
	[
		"(`+)([\\s\\S]*?)\\1",                        /* 1, 2 — code span */
		"!?\\[([^\\]\\n]*)\\]\\(([^)\\s]*)\\)",       /* 3, 4 — link */
		"\\*\\*(?!\\s)([\\s\\S]+?)(?<!\\s)\\*\\*",    /* 5    — bold */
		"~~(?!\\s)([\\s\\S]+?)(?<!\\s)~~",            /* 6    — struck through */
		"\\*(?!\\s)([^*\\n]+?)(?<!\\s)\\*",           /* 7    — italic */
		"(https?://[^\\s<>\"'`)\\]]+)",               /* 8    — a bare address */
		"(?<![\\w#])#([1-9][0-9]*)(?!\\w)",           /* 9    — a mention of an item */
	].join("|"),
	"g"
);

/*
	**The mention rule is `mentions.REF_PATTERN`, character for character.**

	Lookarounds rather than `\b`, and no leading zero: that is what keeps `#42FF00`, `##1`,
	`issue#1` and `#007` out of the index server-side, and a browser that disagreed would
	underline things the backlink graph does not know about. A second copy of this rule is
	this codebase's signature defect, so the copy is deliberate, marked, and identical —
	`tests/test_web.py` compares them.

	It sits last in the alternation, so a `#42` inside backticks or inside a link's text is
	claimed by those first.
*/


export function escaped (text) {
	/*
		Turn a run of source text into something that can only ever be read as text.

		Half of this module's safety is this function being called on everything, so it is
		deliberately blunt: five characters, no exceptions, no attempt to be clever about
		where the text is going. `'` is escaped as well as `"` because an attribute quoted
		either way is then safe, which removes a thing a future edit could get wrong.
	*/

	return String(text).replace(/[&<>"']/g, (character) => ESCAPES[character]);
}


export function target (raw) {
	/*
		Return a link destination that is safe to put in an `href`, or null if there is none.

		Null means the link is not rendered as a link — the source is shown instead — because
		a link that silently loses its destination tells the reader something false about
		what was written.
	*/

	const trimmed = String(raw).trim();

	if (trimmed === "") return null;

	/*
		Control characters come off before anything is decided, and **every test below reads the
		stripped form** (`#682`, review `#677` finding 5). A browser ignores tabs, newlines and
		NULs inside a destination: `java&#9;script:` is a working `javascript:` URL that no
		prefix test sees, and a leading NUL before `//evil.example/x` still resolves to another
		host.

		This used to strip only for the scheme and test `#`, `?` and `//` against the *trimmed*
		form — so one leading control character skipped the protocol-relative refusal while
		still being scheme-parsed. Two tests reading two values is the inconsistency, and it is
		what becomes a real defect the first time somebody adds a branch depending on which.

		What is *returned* is still `trimmed`: stripping decides, it does not rewrite what the
		author wrote.
	*/
	const stripped = trimmed.replace(/[\u0000-\u0020]/g, "");

	/*
		A fragment or a query is this page; a single leading slash is this instance. `//` is
		refused deliberately: it is protocol-relative and points at another host entirely,
		which reads like a path and is not one.
	*/
	if (stripped.startsWith("#") || stripped.startsWith("?")) return trimmed;
	if (stripped.startsWith("/")) return stripped.startsWith("//") ? null : trimmed;

	/*
		The scheme is read from that same value, never matched as a prefix — schemes are
		case-insensitive too, so `JavaScript:` reduces to the same name before it is looked up.
	*/
	const scheme = /^([a-zA-Z][a-zA-Z0-9+.\-]*):/.exec(stripped);

	if (scheme === null) return trimmed;

	return SCHEMES.has(scheme[1].toLowerCase()) ? trimmed : null;
}


function anchor (href, label) {
	/*
		Render one anchor.

		`noopener` is what stops the opened page reaching back through `window.opener`, and
		`noreferrer` stops it being told which item somebody was reading. New tab because
		losing your place in a backlog to an external link is a small betrayal.
	*/

	return `<a href="${escaped(href)}" rel="noopener noreferrer" target="_blank">${label}</a>`;
}


function linked (text, destination, where) {
	/* Render one written link, or the source that was written if we will not go there. */

	const href = target(destination);

	if (href === null) return escaped(`[${text}](${destination})`);

	return anchor(href, inline(text, where));
}


function autolinked (found) {
	/*
		Render an address somebody wrote bare.

		**Its label is escaped rather than parsed, and that is not a shortcut.** Passing it
		back through `inline` recurses without end, because what it is parsing is an address
		and the address matches the same rule that produced it. Caught by running the thing —
		a stack overflow on the first bare URL, on prose that says nothing about links.
	*/

	const href = target(found);

	return href === null ? escaped(found) : anchor(href, escaped(found));
}


function mentioned (ref, where) {
	/*
		Render `#42` as a link to that item, or as the text it is.

		**`where` is asked rather than assumed**, so this module knows nothing about how
		Subroutine addresses anything — it recognises the shape and lets the caller say what it
		points at. Without one, a mention is prose, which is what it was before `#638` and what
		it stays anywhere the workspace is not known.
	*/
	const written = `#${ref}`;

	if (!where) return escaped(written);

	const href = target(where(ref));

	return href === null ? escaped(written)
		: `<a href="${escaped(href)}" class="mention">${escaped(written)}</a>`;
}


export function inline (text, where) {
	/*
		Render one run of prose: emphasis, code, links, mentions, and nothing else.

		The scan walks left to right and everything *between* matches goes through `escaped`,
		so there is no path from the source to the output that skips it.
	*/

	let out = "";
	let last = 0;

	for (const found of String(text).matchAll(INLINE)) {
		out += escaped(text.slice(last, found.index));
		last = found.index + found[0].length;

		if (found[1] !== undefined) out += `<code>${escaped(found[2])}</code>`;
		else if (found[3] !== undefined) out += linked(found[3], found[4], where);
		else if (found[5] !== undefined) out += `<strong>${inline(found[5], where)}</strong>`;
		else if (found[6] !== undefined) out += `<del>${inline(found[6], where)}</del>`;
		else if (found[7] !== undefined) out += `<em>${inline(found[7], where)}</em>`;
		else if (found[8] !== undefined) out += autolinked(found[8]);
		else out += mentioned(found[9], where);
	}

	return out + escaped(text.slice(last));
}


function leading (line) {
	/* How far a line is indented, counting a tab as four. */

	const found = /^[ \t]*/.exec(line)[0];

	return found.replace(/\t/g, "    ").length;
}


function dedented (line, by) {
	/* Remove up to `by` columns of indentation, so a nested block can be parsed on its own. */

	let text = line.replace(/^\t/, "    ");
	let removed = 0;

	while (removed < by && text.startsWith(" ")) {
		text = text.slice(1);
		removed += 1;
	}

	return text;
}


function cells (row) {
	/* Split one table row on its pipes, dropping the empty edges the leading pipe makes. */

	const parts = row.trim().split("|");

	if (parts[0].trim() === "") parts.shift();
	if (parts.length > 0 && parts[parts.length - 1].trim() === "") parts.pop();

	return parts.map((cell) => cell.trim());
}


function tableAt (lines, start, where) {
	/* Render the table beginning at `start`, and say which line comes after it. */

	const header = cells(lines[start]);
	const alignments = cells(lines[start + 1]).map((rule) => {
		if (rule.startsWith(":") && rule.endsWith(":")) return "center";
		if (rule.endsWith(":")) return "right";

		return rule.startsWith(":") ? "left" : null;
	});

	const styled = (column) => {
		const how = alignments[column];

		return how === null || how === undefined ? "" : ` style="text-align:${how}"`;
	};

	let out = "<table><thead><tr>";
	header.forEach((cell, column) => {
		out += `<th${styled(column)}>${inline(cell, where)}</th>`;
	});
	out += "</tr></thead><tbody>";

	let i = start + 2;

	while (i < lines.length && lines[i].trim().startsWith("|")) {
		out += "<tr>";
		cells(lines[i]).forEach((cell, column) => {
			out += `<td${styled(column)}>${inline(cell, where)}</td>`;
		});
		out += "</tr>";
		i += 1;
	}

	return [`${out}</tbody></table>`, i];
}


function itemBody (content, where, depth) {
	/*
		Render what one list item holds.

		A single run of prose is rendered inline, so an ordinary flat list — which is what
		2,180 of this instance's 2,219 list items are — does not gain a paragraph inside every
		bullet. Anything with a blank line or a block inside it is parsed properly instead.
	*/

	const structured = content.some(
		(line, index) =>
			(index > 0 && line.trim() === "") || (index > 0 && LIST_ITEM.test(line)) ||
			FENCE.test(line) || (index > 0 && line.trim().startsWith(">"))
	);

	return structured ? blocks(content, where, depth) : inline(content.join("\n"), where);
}


function listAt (lines, start, where, depth) {
	/* Render the list beginning at `start`, and say which line comes after it. */

	const first = LIST_ITEM.exec(lines[start]);
	const indent = leading(lines[start]);
	const ordered = /\d/.test(first[2]);

	const items = [];
	let i = start;

	while (i < lines.length) {
		const marker = LIST_ITEM.exec(lines[i]);

		if (marker === null || leading(lines[i]) !== indent) break;

		const content = [marker[3]];
		i += 1;

		/* Everything indented past the marker belongs to this item, and so does a blank line
		   with more of the item after it. A blank line with anything else after it ends the
		   list. */
		while (i < lines.length) {
			if (lines[i].trim() === "") {
				let ahead = i;

				while (ahead < lines.length && lines[ahead].trim() === "") ahead += 1;

				if (ahead >= lines.length || leading(lines[ahead]) <= indent) break;

				for (let blank = i; blank < ahead; blank += 1) content.push("");
				i = ahead;
				continue;
			}

			if (leading(lines[i]) <= indent) break;

			content.push(dedented(lines[i], indent + 2));
			i += 1;
		}

		items.push(`<li>${itemBody(content, where, depth + 1)}</li>`);
	}

	const tag = ordered ? "ol" : "ul";

	return [`<${tag}>${items.join("")}</${tag}>`, i];
}


function blocks (lines, where, depth = 0) {
	/*
		Render a sequence of lines as block-level HTML.

		**Past `MAX_DEPTH` the rest is shown as it was written** (`#679`) rather than followed
		any further. Preformatted, because that is this file's existing way of saying *this is
		your text and nothing has been done to it* — and because something nested that deeply is
		read more usefully as its own shape than as forty empty bullets.
	*/

	if (depth >= MAX_DEPTH) return `<pre><code>${escaped(lines.join("\n"))}</code></pre>`;

	let out = "";
	let i = 0;

	while (i < lines.length) {
		const line = lines[i];

		if (line.trim() === "") {
			i += 1;
			continue;
		}

		const fence = FENCE.exec(line);

		if (fence !== null) {
			const held = [];
			i += 1;

			while (i < lines.length && !lines[i].trim().startsWith(fence[1])) {
				held.push(lines[i]);
				i += 1;
			}

			/* An unclosed fence runs to the end rather than falling back to prose: somebody
			   who opened one meant the rest to be code, and reinterpreting it would apply
			   emphasis and links to what they were quoting. */
			if (i < lines.length) i += 1;

			out += `<pre><code>${escaped(held.join("\n"))}</code></pre>`;
			continue;
		}

		/* Before the list check, because `* * *` is a rule and reads as a bullet. */
		if (RULE.test(line)) {
			out += "<hr>";
			i += 1;
			continue;
		}

		const heading = HEADING.exec(line);

		if (heading !== null) {
			const level = Math.min(heading[1].length + HEADING_OFFSET, 6);

			out += `<h${level}>${inline(heading[2], where)}</h${level}>`;
			i += 1;
			continue;
		}

		if (/^\s{0,3}>/.test(line)) {
			const held = [];

			while (i < lines.length && /^\s{0,3}>/.test(lines[i])) {
				held.push(lines[i].replace(/^\s{0,3}>[ \t]?/, ""));
				i += 1;
			}

			out += `<blockquote>${blocks(held, where, depth + 1)}</blockquote>`;
			continue;
		}

		/*
			An indented code block, which is reached only at a block boundary — a paragraph
			swallows its own indented continuation lines below, so this cannot fire in the
			middle of one. 48 blocks on this instance are written this way rather than fenced,
			so dropping the form would turn each of them into a run-on paragraph.
		*/
		if (INDENTED.test(line)) {
			const held = [];

			while (i < lines.length && (INDENTED.test(lines[i]) || lines[i].trim() === "")) {
				held.push(dedented(lines[i], 4));
				i += 1;
			}

			while (held.length > 0 && held[held.length - 1].trim() === "") held.pop();

			out += `<pre><code>${escaped(held.join("\n"))}</code></pre>`;
			continue;
		}

		if (
			line.trim().startsWith("|") && i + 1 < lines.length &&
			TABLE_SEPARATOR.test(lines[i + 1])
		) {
			const [rendered, next] = tableAt(lines, i, where);

			out += rendered;
			i = next;
			continue;
		}

		if (LIST_ITEM.test(line)) {
			const [rendered, next] = listAt(lines, i, where, depth);

			out += rendered;
			i = next;
			continue;
		}

		const paragraph = [];

		while (i < lines.length && lines[i].trim() !== "") {
			const next = lines[i];

			/* A paragraph ends where another block begins, so that a list or a fence written
			   straight after a line of prose is still one. */
			if (
				paragraph.length > 0 &&
				(FENCE.test(next) || RULE.test(next) || HEADING.test(next) ||
					LIST_ITEM.test(next) || /^\s{0,3}>/.test(next))
			) break;

			paragraph.push(next);
			i += 1;
		}

		out += `<p>${inline(paragraph.join("\n"), where)}</p>`;
	}

	return out;
}


export function render (source, where) {
	/*
		Render stored Markdown as HTML.

		The only entry point. What comes back is safe to hand to `dangerouslySetInnerHTML` and
		is not safe to concatenate anything else into.
	*/

	if (source === null || source === undefined || String(source).trim() === "") return "";

	return blocks(String(source).replace(/\r\n?/g, "\n").split("\n"), where);
}
