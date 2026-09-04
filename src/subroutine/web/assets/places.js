/*
	Where something can live and who can hold it — the workspace, project and status
	vocabularies, the roster, and the addresses a reader can be sent to.

	**Split out of `app.js` by `#1849`**, which moved 10,286 lines into thirteen modules and
	changed nothing about what any of them do. Read `app.js` for what the whole app is; this
	file is one layer of it.
*/

import { PATH_SEPARATOR } from "./address.js";
import { named } from "./dates.js";
import { scoped } from "./requests.js";

export function vocabularyRequest (slug) {
	/*
		What this workspace calls things — the types, the statuses and the tags a form offers.

		**`?workspace_id=` is not optional, and asking without it answers 200 with nothing.**
		Measured: `/v1/meta` on this instance returns `"statuses":{}`, `"item_types":{}` and
		`"link_types":[]` when no workspace is named, because they are per-workspace vocabulary
		and it has not been told which. A form built from that answer would offer a type dropdown
		with no types in it, and nothing would have failed — `#571` is the item for the shape.
	*/
	return { path: scoped("/meta", slug), method: "GET" };
}

export function projectsRequest (slug) {
	/*
		Where a new item can be filed, **in tree order** (`#770`).

		**Four fields rather than the whole project**, on `#645`'s measurement: a listing that
		asks for what it renders is the difference between 287 KB and 38 KB. `is_inbox` is here
		because the Inbox is where an item with no project lands, so it is the one entry a form
		can label as *what happens if you say nothing*.

		**`id` and `hidden_statuses` are here for the status pickers** (`#1029`). A project may
		be told which statuses it does not offer, and that setting is inherited — so the server
		resolves the chain and publishes the answer per project, which is why this asks for the
		resolved list rather than for `settings`. A row finds its project's by `project_id`, and
		a *form* finds it by whichever project is selected, which is the case an item-level
		field could never answer because nothing exists yet to carry one.

		**`path` orders and `depth` indents, and only one of them is a field.** `path` is
		sortable and *not* selectable — measured, by asking for it and being told the twenty-one
		fields a project has. So the shape of the tree arrives as an order plus a number, which
		is enough: a flat list put `Web UI` beside `Websites` as though they were the same kind
		of thing, when one is inside `Subroutine` and the other is a root.
	*/
	return {
		path: scoped(
			"/projects?fields=id,key,title,is_inbox,depth,hidden_statuses"
			+ "&order=path&limit=200",
			slug
		),
		method: "GET",
	};
}

export function people (roster) {
	/*
		Who work can be handed to, and **which of them is an agent** (`#770`).

		The roster has published `is_service_account` since M1 and this app read the username and
		threw the rest away — so on this workspace the control offered one person and four
		agents, all looking like colleagues.

		**That is worse than untidy.** `#473` made an agent answer to a person and `#474` is that
		delegation has never once been used here; handing work to `claude-nuc14` in the belief it
		is a colleague is the failure the accountability chain exists to prevent.

		**Said in a word, not in a glyph or a colour** (`#102`): nothing may be information only
		in how it looks, and an icon beside four of five names is exactly that. *(agent)* rather
		than *(bot)* because agent is the word this product uses everywhere else — the spec, the
		skill, `subroutine agent create` — and a second name for one thing is what this codebase
		keeps paying for.

		**The username is the label, not `display_name`.** A reader who picks *Claude on nuc14*
		and then reads `claude-nuc14` back off the item has been shown two names for one
		account, which is `#515`'s shape in miniature: every step works and the confirmation
		does not match the choice.
	*/
	return (roster || []).map((row) => ({
		/* **The id as well as the name** (`#759`): a comment carries `author_id` and no
		   username, so the only way to say who spoke is to resolve it against this. */
		id: row.user.id,
		username: row.user.username,
		/* **`named` rather than a second wording** (`#1420`). This control said
		   `claude-nuc14 (agent)` while a row beside it said `@claude-nuc14 (agent, @si)` —
		   two vocabularies for one roster, which is the thing the paragraph above objects to
		   about *(bot)*, one field along. The accountable person is resolved on the server and
		   arrives as `answers_to`; the browser holds no copy of the chain rule (`#925`).

		   **The sigil comes with it**, which this control did not carry before. A reader picks
		   a name here and reads it back off the item, and `#515`'s shape is that every step
		   works while the confirmation does not match the choice. */
		label: named(row.user.username, row.user.is_service_account, row.user.answers_to, ""),
	}));
}

export function offered (vocabulary, kind, hidden = null, keep = null) {
	/*
		The options for one dropdown, and which is chosen when nobody has chosen — `#756`.

		**Never a literal array.** A type and a status are workspace vocabulary: renameable, and
		an instance may add one. A form carrying its own list is wrong on the first workspace
		that does either, and wrong silently, because the control still looks complete.

		Reads `is_default` for the pre-selection rather than assuming a key, for the same reason:
		`open` and `task` are what `seed.py` happens to install here, not what the model promises.

		## What `hidden` does, and the one rule that overrides it (`#1029`)

		**A project may say which statuses it does not offer**, resolved server-side up the
		project tree and published per project. Measured on the instance that asked for this:
		171 open tasks, every one of them `open` — six words offered where two are used.

		**It narrows the offer and refuses nothing** (Simon, 2026-08-20). Any surface may still
		set any status the workspace has; this is a preference, not a permission, so a script or
		an agent that learned the vocabulary last week cannot be broken by somebody's tidying.

		**A picker always offers the status the thing would have if you did nothing.** One rule,
		read two ways: for an item that exists, `keep` is what it is in now; for one that does
		not, `is_default` is what the server will give it. Both survive being hidden.

		Without the first, a `<select>` whose value matches no option renders blank or falls back
		to its first entry — so a blocked task would report as *Open*, and saving anything else
		on the form would write that back. Without the second, a project that hid its default
		could not file an ordinary task at all: the control would pick whatever came first, and
		the server would have handed out the hidden one anyway.

		Both escapes are self-healing. Move the item onto a status the project offers, or stop
		hiding the default, and the extra entry stops appearing.
	*/
	const known = (vocabulary && vocabulary[kind]) || [];
	const away = new Set(hidden || []);

	return known
		.filter((one) => !away.has(one.key) || one.key === keep || Boolean(one.is_default))
		.map((one) => ({
			key: one.key,
			label: one.label || one.key,
			chosen: Boolean(one.is_default),
		}));
}


export function notOffered (projects, chosen) {
	/*
		What one project does not offer, named the way each caller already has it — `#1029`.

		**An id or an address, because the two readers hold different things.** An item's row
		carries `project_id`; a form's project control carries the whole address (`#977`), and
		it has to, because a key stopped identifying a project at `#958`. Asking each to convert
		would put the same walk in two more places.

		**A lookup rather than a walk**, because the inheritance is already resolved: the server
		publishes `hidden_statuses` per project, having looked up the tree itself. A client
		walking `project_path` would need every ancestor's settings and a third copy of the
		precedence rule, which is `#925`'s reason for publishing a rendering rather than a rule.

		Answers with nothing for a project this page has not loaded, which is the honest reading:
		the roster is capped at 200 and scoped to one workspace, so *not here* means *not known*
		rather than *nothing hidden*. Failing towards offering everything is the safe direction —
		an extra option is a shrug and a missing one is a control that cannot say what an item is.
	*/
	if (!chosen) return [];

	const found = addressedProjects(projects || [])
		.find((one) => one.id === chosen || one.address === chosen);

	return (found && found.hidden_statuses) || [];
}

export function statusFor (vocabulary, kind, category) {
	/*
		Which status a column means — `#711`.

		**A board's columns are *categories* and the API takes a *status*.** The four categories
		are fixed by the model, which is what lets a board have columns at all (`#653`); a status
		is workspace vocabulary, renameable, and there may be several in one category — `open`,
		`blocked` and `needs_input` are all `todo` here. So dropping a card on a column is a
		question with more than one answer and something has to choose.

		**The default of that category**, and the first one otherwise. `is_default` is what a
		workspace has already said about *which of these is the ordinary one*, which is exactly
		the question, and it is the same field `offered` reads for the same reason. Choosing by
		key would be this app carrying its own vocabulary, which is wrong on the first instance
		that renames anything and wrong silently.

		**No key, and *why* there is none** (`#791`). Two different things reach here as an absent
		status and only one is about the workspace: a category genuinely holding none, and a page
		that has not read `/v1/meta` yet — `words` clears the vocabulary before it fetches and
		treats its own failure as survivable (§1.4), so null is a state this app reaches on a
		failed or in-flight request rather than only on an unusual configuration.

		Collapsing them made a drop say *"There is no status here that means in progress"* about
		a workspace that has one, which is a refusal naming a cause it has not established — the
		rule the CLI already follows, broken here.
	*/
	if (!vocabulary) return { key: null, because: "unread" };

	const known = (vocabulary[kind] || []).filter((one) => one.category === category);

	if (known.length === 0) return { key: null, because: "absent" };

	return { key: (known.find((one) => one.is_default) || known[0]).key, because: null };
}

export function soleStatusIn (vocabulary, kind, category) {
	/*
		Whether a category has exactly one status, so a column naming it says everything — `#1019`.

		**This is what lets a board drop the status chip without dropping information.** Simon:
		*"items in the done column all have the done label — these seem superfluous."* True of
		*Done*, *Cancelled* and *Superseded*, each the only status in its category — and **false
		of To do**, where `open`, `blocked` and `needs_input` all live and only the first is the
		default, so the chip is the one thing separating three states.

		**A fact about the workspace's vocabulary, not about the page's contents**, and that is
		the whole reason it is asked this way. §12.2a's drop-if-uniform would answer *Done* too,
		by looking at the rows — but decision `#957` §4 refused that for the browser because the
		page polls, so a chip would appear and vanish under the reader as a column filled. This
		answer cannot change while nobody edits the vocabulary.

		**Unknown vocabulary keeps the chip.** `words` clears it before fetching and treats its
		own failure as survivable (§1.4), so null is a state a working page reaches — and
		hiding a fact because a request is in flight is the wrong direction to fail.
	*/
	if (!vocabulary) return false;

	return (vocabulary[kind] || []).filter((one) => one.category === category).length === 1;
}

export function unmovable (because, category) {
	/*
		What to say when a card cannot be moved — `#791`.

		A sentence per reason, and each says what was actually looked at. **The unread one offers
		the remedy**, because there is one and it is *wait a moment or reload*; the absent one
		does not, because nothing the reader can do from here changes their workspace's statuses.

		Pure so both readings are driven. The wire from here to `setNote` is two lines inside
		`App` and is reachable by no harness this project has (`#640`, `#748`).
	*/
	const named = String(category || "").replace(/_/g, " ");

	if (because === "unread") {
		return "This page has not read what this workspace calls things, so it cannot tell "
			+ `which status means ${named}. Reload and try again.`;
	}

	if (because === "absent") return `There is no status here that means ${named}.`;

	return null;
}

export function projectName (key, projects) {
	/*
		What to call a project on a row — `#912`.

		**Its name, not its key.** Every other chip on a row is something a person reads: the
		kind is `Task` or `Document`, the status is its label, the assignee is a username. The
		project was the one address among them, lower case by `#508`'s rule and shaped to be
		*typed* — `--project ui` — which is a thing nobody does in a browser. Simon met it as
		`Document` beside `subroutine`, two registers in one row of chips.

		**Deliberately still the key at a terminal**, where the chip doubles as what to type
		next. This is a divergence between surfaces rather than a defect in one, which is why
		`cli/personal` is untouched.

		**Falls back to the key**, because the app asks for two hundred projects and a workspace
		may hold more — the same limit `filableFor` works around from the other end. A chip that
		vanished, or read `undefined`, would be worse than one in the wrong register.
	*/
	const named = (projects || []).find((one) => one.key === key);

	return named && named.title ? named.title : key;
}

export function treeOrdered (projects) {
	/*
		The same projects, with each parent's children in alphabetical order — `#974`.

		**What arrives is creation order wearing a tree order's clothes**, and that is the whole
		reason this exists. `projectsRequest` asks for `order=path`, and `project.path` is built
		from ancestor *ids*; ids here are uuid7, which lead with a timestamp. So the server sorts
		siblings by when somebody made them. Measured on the live instance: `Null sweep` after
		`Subroutine` at the root, and `Web UI` before `Release and hosting` beneath it.

		**No `order=` can do this instead.** A single `ORDER BY` gives alphabetical-within-tree
		only if the sortable path is composed of the *names*, and `path` is composed of ids
		deliberately — a key is renameable (`#508`, `#957`), so a materialised path of renameable
		segments would have to be rewritten on every rename.

		**`order=path` is what makes reassembling it here sound**, and it is worth stating rather
		than relying on: a parent's path is a string prefix of every descendant's, so lexicographic
		order is a genuine pre-order traversal — a parent is immediately followed by its whole
		subtree, and sibling subtrees are contiguous. With `depth` beside it that is enough to
		rebuild the tree. `path` itself is **not selectable** (`#770` measured that when it wrote
		the request), so the arriving order plus a number is all there is, and it is sufficient.

		**Sorted by what a reader sees**, which is the title where there is one, through
		`localeCompare` — `<` on strings compares code points, so `Ä` would sort after `Z`.

		**A list that is not in pre-order comes back untouched**, which is not defensiveness: it
		is the honest answer when the premise this is built on does not hold, and a wrong tree
		assembled confidently is worse than the order the server sent.
	*/
	const rows = projects || [];

	if (rows.length === 0) return rows;

	/* One node per row, and `children` filled by walking the pre-order with a stack of
	   ancestors. `depth` says how far to pop: a row of depth 2 hangs off whatever is at
	   depth 1, which is the last thing on the stack at that height. */
	const roots = [];
	const ancestry = [];

	for (const row of rows) {
		const node = { row, children: [] };
		const depth = row.depth || 0;

		if (depth > ancestry.length) return rows;

		ancestry.length = depth;

		if (depth === 0) roots.push(node);
		else ancestry[depth - 1].children.push(node);

		ancestry.push(node);
	}

	const named = (node) => `${node.row.title || node.row.key || ""}`;
	const flattened = [];

	const walk = (nodes) => {
		for (const node of nodes.sort((a, b) => named(a).localeCompare(named(b)))) {
			flattened.push(node.row);
			walk(node.children);
		}
	};

	walk(roots);

	return flattened;
}

export function placesToGo (workspaces, projects, showing) {
	/*
		Everything the masthead can take you to — `#975`, Simon's.

		**Workspaces by title, alphabetically, with the projects of the one you are in nested
		underneath.** The control listed workspace *slugs* and nothing else, so the only way into a
		project was a label on a row that happened to be in one, or typing the address.

		**Only the current workspace's projects, and that is a measurement rather than a
		preference** (Simon, 2026-08-17). Projects arrive one workspace at a time — `scoped()` pins
		`workspace_id` on every request and `words(slug)` returns early without one — so on the
		agenda at `/` the app holds none at all, and offering every workspace's would mean a
		request per workspace on load. This costs nothing and leaves any project two hops away.

		**A value is an address, not a pair**, so `narrow` can read it back through `parseAddress`
		and the thing the reader chooses is the thing that ends up in the bar. That is `#959`'s own
		argument for the row labels, and it is why nothing here has to know how to navigate.

		**The path is rebuilt from the tree rather than asked for.** A project's `key` is unique
		only among its siblings since `#958`, so `dist` may name two projects and cannot address
		either; `path` would say it exactly and is **not selectable** — measured by `#770` when it
		wrote that request. Reassembling it from the ancestry costs nothing here because the tree
		has already been walked, and it is exact: a child's address is its parent's plus its key.
	*/
	const spaces = (workspaces || [])
		.slice()
		/* **The slug breaks a tie**, because a title may repeat and the order would otherwise
		   fall to whatever `created_at` happened to be — arbitrary, and different on every
		   instance. `#851`'s lesson one list along: a tie broken by nothing is a tie broken by
		   insertion order, and nothing is guarding it. */
		.sort((a, b) => `${a.title || a.slug}`.localeCompare(`${b.title || b.slug}`)
			|| `${a.slug}`.localeCompare(`${b.slug}`));
	const where = showing || {};
	const here = where.workspace || null;
	const inside = where.project || null;
	const options = [{
		value: "",
		label: "All workspaces",
		depth: 0,
		chosen: Boolean(where.agenda),
	}];

	for (const space of spaces) {
		/* **Not on the agenda**, even though the app happens to hold this workspace's projects
		   there: `chosenWorkspace` falls back to the first workspace when the address names
		   none, so `words` has run. Offering one workspace's tree and not the others' would
		   be a request that landed showing through as a rule nobody chose. */
		const mine = !where.agenda && space.slug === here;

		/*
			**Everything in this control is named the way a person reads it** — `#980`, Simon
			2026-08-18, reversing `#979` the same day.

			`#979` labelled a workspace by its slug, on the grounds that a title is free text and
			not unique, so it cannot identify a destination. **That argument applies to the
			projects underneath at least as strongly** — two siblings may share a title exactly
			as two workspaces may — and it accepted titles for those in the same breath. A rule
			applied to one half of one control is not a rule, and what it produced was `projects`
			above `Inbox` and `Web UI`: two registers in one list, which is `#912` verbatim.

			**The failure it was written for was a data error and nothing else.** The workspace
			slugged `projects` was *titled* `Personal`, which is the only reason two entries read
			alike. `#981` is that nothing can correct such a thing after creation.

			**A collision is accepted rather than designed around.** Two workspaces genuinely
			sharing a title read alike here, and each is still a distinct destination because the
			**value is an address** — so both are reachable and nothing is lost but a glance.
			`views.WorkspaceRef` carries the slug too, if that ever stops being enough with data
			somebody meant.
		*/
		options.push({
			value: `/${encodeURIComponent(space.slug)}`,
			label: `${space.title || space.slug}`,
			depth: 0,
			chosen: !where.agenda && mine && !inside,
		});

		if (!mine) continue;

		/* Ancestor keys by depth, so a row at depth 2 addresses as `parent/child`. The list is a
		   pre-order (see `treeOrdered`), so whatever sits at each height is this row's ancestry. */
		const ancestry = [];

		for (const one of treeOrdered(projects)) {
			const depth = one.depth || 0;

			ancestry.length = depth;
			ancestry.push(one.key);

			const address = ancestry.map((part) => encodeURIComponent(part)).join("/");

			options.push({
				value: `/${encodeURIComponent(space.slug)}/${address}`,
				/* **Marked here, and never on a task row** (`#986`, decision `#982`). 84% of
				   this instance's open tasks are in the project most likely to be prioritised,
				   so a mark per row would appear on 84% of them — which §12.2a drops as saying
				   nothing. This is a control where the question is actually asked, and the
				   answer is one entry out of a handful. Only the project itself: its subtree
				   inherits the bonus, and marking children would read as four prioritised
				   projects, which is the state that design makes impossible. */
				label: `${one.title || one.key}`
					+ (space.prioritised_project === ancestry.join("/") ? " (prioritised)" : ""),
				depth: depth + 1,
				chosen: !where.agenda && inside === ancestry.join("/"),
			});
		}
	}

	return options;
}

function _inboxFirst (ordered) {
	/*
		The Inbox and everything filed under it, moved to the front — see `filableFor`.

		**Only when it is a root.** An Inbox somebody has filed *inside* something else is left
		where the tree puts it, because a row indented two levels at the top of the list reads as a
		fault rather than as a default.
	*/
	const at = ordered.findIndex((one) => one.is_inbox);

	if (at < 0 || (ordered[at].depth || 0) !== 0) return ordered;

	let after = at + 1;

	while (after < ordered.length && (ordered[after].depth || 0) > 0) after += 1;

	return ordered.slice(at, after).concat(ordered.slice(0, at), ordered.slice(after));
}

export function addressedProjects (projects) {
	/*
		The project roster with each entry's full address on it — the walk `#977` established.

		**Rebuilt from the tree rather than read off the row**, because `path` is sortable and
		*not* selectable on that listing (`#770`). What arrives is a genuine pre-order — the
		fetch asks for `order=path` and a parent's path is a string prefix of every
		descendant's — so an ancestry stack indexed by `depth` reassembles the addresses in one
		pass.

		**Extracted because three things now want it**: the form's project control (`#977`), the
		masthead's places (`#975`), and which statuses a project offers (`#1029`). Two copies of
		one walk is this codebase's signature defect, and this one is subtle enough to drift —
		`ancestry.length = depth` is what pops back out of a subtree, and it only works on a
		pre-order.
	*/
	const ancestry = [];

	return treeOrdered(projects).map((one) => {
		ancestry.length = one.depth || 0;
		ancestry.push(one.key);

		return { ...one, address: ancestry.join(PATH_SEPARATOR) };
	});
}

export function filableFor (projects, project, prioritised = null) {
	/*
		Where a new item can go, and which entry is chosen when nobody has chosen — `#756`.

		**The address decides**, which `#738` already settled: `/{workspace}/{project}` says
		where rows come from, so it says where a new one goes. Nothing new is parsed. With no
		project in the address — on the agenda, or on a whole workspace — it is the Inbox, which
		is where an item with no project lands anyway.

		**A project the address names and the listing does not hold is added rather than
		ignored.** Otherwise nothing would be chosen, the browser would select the first option,
		and the item would file into the Inbox under an address naming somewhere else — a wrong
		destination, silently, which is worse than any refusal. It can happen: this asks for 200
		projects and a workspace may hold more.

		The same shape as `offered` on purpose, so both fill the same control and neither grows
		its own idea of what a dropdown is.
	*/
	/*
		**Indented by depth, which is what `subroutine project list` already does** — two spaces
		per level, so the two surfaces render one tree the same way rather than each inventing a
		shape for it. Non-breaking, because an `<option>` is the one place a browser may collapse
		leading whitespace and the indent is the whole of what is being said.
	*/
	/*
		**Alphabetical within each parent, and the Inbox first anyway** — `#974`, and Simon's
		answer of 2026-08-17 when asked whether *within its parent* included it.

		This control's job is choosing where an item goes, and the Inbox is what happens if you say
		nothing — which is why it is the one entry labelled `(default)`. Burying the default halfway
		down an alphabetical list makes the form worse at its one job, so the exception belongs
		here, to this control's labelling, rather than to the ordering everything shares.

		**Its subtree comes with it**, which is why `_inboxFirst` moves a run rather than a row: a
		project can be re-parented (`#44`), so the Inbox may one day have children, and lifting a
		parent out of a pre-order list without them would leave those children indented under
		whatever happened to precede them.
	*/
	/*
		**The value is the whole address, not the bare key** (`#977`). A key has been unique
		only among its *siblings* since `#958`, so a workspace holding `substation/dist` beside
		`websites/dist` offered two options carrying one value and either one was refused —
		correctly, by `selection.addressed`, which lists the candidates rather than guessing.
		The refusal was `#957` working; what was broken is that this control could not offer
		anything better, because it was sending the wrong string.

		**Rebuilt from the tree rather than read off the row**, because `path` is not selectable
		on this listing (`#770`) — the same walk `placesToGo` makes, for the same reason.

		Computed *before* `_inboxFirst` reorders, so the ancestry stack reads a genuine
		pre-order. It happens to survive that move — a contiguous subtree keeps its ancestors in
		front of it — but depending on that is depending on a property of somebody else's
		function.
	*/
	const promoted = _inboxFirst(addressedProjects(projects));

	const known = promoted.map((one) => ({
		key: one.address,
		/* `(prioritised)` beside `(default)`, and both for the same reason: a control offering
		   somewhere to file work should say which entry is not an ordinary one (`#986`). The
		   address is passed in rather than read off a row, because a form knows which projects
		   there are and never which workspace it is in. */
		label: "  ".repeat(one.depth || 0)
			+ `${one.title || one.key}${one.is_inbox ? " (default)" : ""}`
			+ (prioritised && one.address === prioritised ? " (prioritised)" : ""),
		chosen: project ? one.address === project : Boolean(one.is_inbox),
	}));

	if (!project || known.some((one) => one.chosen)) return known;

	return [{ key: project, label: project, chosen: true }].concat(known);
}

export function prioritisedHere (workspaces, workspace = null) {
	/*
		Which projects are prioritised on this page, addressed the way a reader would type them —
		`#986`, decision `#982`.

		**One project per workspace, so a page spanning them can hold one each** (§13.7). A
		listing is narrowed to one workspace and names that one; the agenda spans every workspace
		and names them all, which is why `workspace` is optional rather than required.

		**Qualified by the slug only when there is more than one workspace to confuse it with**,
		which is the rule every address on this surface follows and the terminal's
		`qualifies_workspace` said the same way. Saying *dist is prioritised* on an instance
		holding two workspaces that each have a `dist` would name neither.

		**Pure, so it can be driven in Node** (`#640`). The whole family of defects this codebase
		keeps finding here is a correct rule with no wire to it, and the cheapest guard against
		that is a decision lifted out of the component that renders it.
	*/
	const spaces = (workspaces || []).filter((one) => one && one.prioritised_project);
	const wanted = workspace
		? spaces.filter((one) => one.slug === workspace)
		: spaces;
	const qualifies = (workspaces || []).length > 1 && !workspace;

	return wanted.map((one) => (qualifies
		? `${one.slug}/${one.prioritised_project}`
		: one.prioritised_project));
}

export function prioritisedSentence (found) {
	/*
		What is prioritised, with the verb, the possessive and the noun all agreeing about how
		many — `#986`.

		**The terminal's sentence, word for word**, because a person moving between the two
		surfaces should not have to work out that they are being told the same thing;
		`tests/test_web.py` compares them. Two workspaces may each prioritise a project, so a
		line reading "personal/home, projects/subroutine is prioritised, so its work rises" is
		the kind of detail that makes a reader distrust every number beside it.
	*/
	if (!found || found.length === 0) return null;

	if (found.length === 1) {
		return `${found[0]} is prioritised, so its work rises here.`;
	}

	const named = `${found.slice(0, -1).join(", ")} and ${found[found.length - 1]}`;

	return `${named} are prioritised, so their work rises here.`;
}

export function rankedByPriority (order) {
	/*
		Whether this listing is sorted by §6.3a's rank, in either direction — `#986`.

		The prioritised project raises work inside a *ranked* order and does nothing to a page
		sorted newest-first, so the sentence is said only where it is true. The terminal answers
		the same question with `_ranked_by_priority`, and a page that announced an effect it was
		not showing would be worse than one that said nothing.
	*/
	return `${order || ""}`.split(",")
		.map((part) => part.trim().replace(/^-/, ""))
		.includes("priority_score");
}
