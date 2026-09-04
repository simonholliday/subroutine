/*
	The browser app — item `#597`. Read-only: see what there is, and read one in full.

	**No build step.** Preact and htm are served as written from `/app/`, and an import map in
	`index.html` resolves the one bare specifier between them. What that buys is not
	convenience: it is that the source a reader is served **is** the source in the repository,
	so the published-source promise means something for the half that runs in a browser — and
	there is no npm closure for `scripts/check_licences.py` to be structurally unable to see.

	The first of those was the AGPL's network-use clause until 2026-08-08 and is now a product
	commitment (§2.2). It is the weaker of the two arguments and it was never the deciding one.

	**It talks only to the public API** (`#351`). No private endpoints, nothing a token could
	not do — so anything this page can show, a script can too, and the UI cannot quietly become
	the only way to do something.
*/

import { render } from "preact";
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { html } from "./html.js";
import {
	AGENDA_VIEW, ANSWERED_BY, BOARD, DEFAULT_VIEW, EVERYTHING, MAX_REF, ONLY_FINISHED,
	PATH_SEPARATOR, PRODUCT, SELECTABLE, VIEWS, addressOf, agendaRequest, answers, chips,
	chosenWorkspace, encodedPath, frame, listingAddress, mentionHref, pageTitle,
	parseAddress, permits, projectLabel, refAsked, reloads, selectionOf, shortVersion,
	showingOf, titlesByPath, viewOf, withShowing, widened,
} from "./address.js";
import {
	Boundary, accumulated, inOrder, mergeOrder, newestFirst, refusal, sunkOrder, unpacked,
	unrenderable,
} from "./answers.js";
import {
	Facts, Foot, Note, Prose, THEMES, Theme, Wordmark, applyTheme, themeChoice,
} from "./chrome.js";
import {
	DEFAULT_ORDER, DEFERRED, ORDERINGS, calendarDay, completable, day, deferred, excluded,
	holding, named, offeredOrders, orderedAs, orderingValue, overdue,
} from "./dates.js";
import { Detail, Doing, Failed, Linking, Saying, Seeking, Written } from "./detail.js";
import {
	ANCHORS, Adding, Asking, CAPTURE_HINT, Conflict, DATE_FIELDS, DOCUMENT_HINT,
	DocumentFields, Editing, Fields, Listing, Narrowed, PRIORITIES, Reading, Repeats, TIMED,
} from "./forms.js";
import {
	CLOSED_BY_DEFAULT, NOT_SHOWN, agendaBuckets, blockersDone, choicesIn, collapsedColumns,
	columns, counted, followed, opens, partsDone, rememberChoices, withinAllowance,
} from "./grouping.js";
import {
	CATEGORY_ICONS, Icon, KIND_ICONS, MARK_ICONS, TYPE_ICONS, UNKNOWN_ICON, WAITING_STATUS,
	marks, moment, when,
} from "./marks.js";
import {
	addressedProjects, filableFor, notOffered, offered, people, placesToGo, prioritisedHere,
	prioritisedSentence, projectName, projectsRequest, rankedByPriority, soleStatusIn,
	statusFor, treeOrdered, unmovable, vocabularyRequest,
} from "./places.js";
import {
	DOCUMENT_SAID, NEVER_CLEARED, RELEASE_CHECK_POLLS, REPEATED, SAID_AS_NUMBERS,
	SAID_AS_WRITTEN, addRequest, allowedIn, assignRequest, authorOf, collectionsFor,
	commentRequest, completeRequest, conflictIn, dateFor, documentRequest, edited, filed,
	freshly, fromItem, headRequest, identityRequest, itemRequests, linkAsked, linkChoices,
	linkRequest, linkableTypes, listingRequests, localMoment, pollRequest,
	prioritiseRequest, readForm, readingRequest, releaseMoved, repeating, repeats,
	restoreRequest, rosterRequest, scoped, sent, signOutRequest, statusRequest, timeFor,
	touching, unlinkRequest, updateRequest, withTime, written,
} from "./requests.js";
import { Agenda, Board, Marks, Row, Stamp } from "./rows.js";
import {
	BOARD_COLUMNS_REMEMBERED, COLUMN, HORIZON_DAYS, POLL_MS, SECTIONS_REMEMBERED, WIDER,
} from "./settings.js";

export function App () {
	const [me, setMe] = useState(null);
	const [workspace, setWorkspace] = useState(null);
	const [items, setItems] = useState([]);
	const [open, setOpen] = useState(null);
	const [error, setError] = useState(null);
	const [ready, setReady] = useState(false);
	const [members, setMembers] = useState([]);
	const [note, setNote] = useState(null);

	/* **Which prose box is being previewed, and what it held when the button was pressed**
	   (`#776`). One answer for the page rather than one per box: two previews at once is a
	   state nobody asked for, and this is where every other form's state already lives —
	   `Adding`'s own comment says why none of them keeps its own. */
	const [previewing, setPreviewing] = useState(null);

	/* **Whether the instance has been redeployed under this page** — `#785`. Its own state
	   rather than a `note`, because a note is what just happened and is replaced by the next
	   write: a release notice cleared by somebody saving a title is one nobody sees. */
	const [released, setReleased] = useState(false);
	const [busy, setBusy] = useState(false);
	/*
		**A counter nobody reads, bumped so the clock-dependent marks are recomputed** (`#950`,
		cold review `#927`'s L-7).

		`overdue` and `deferred` are worked out from `new Date()` at render, and `deferred`'s own
		comment says why they are computed rather than published: *a published boolean would be
		stale — computed when the page was fetched, and going on saying "deferred" after the
		moment passed, on a page a reader leaves open.* It was computed and stale anyway, because
		the only thing that re-renders is the poll, and `#781` correctly made the poll do nothing
		when the feed reports nothing new.

		**The value is deliberately unread.** The marks read the clock directly, so all that is
		needed is a render; carrying an instant down through every component to arrive at the
		same answer would be more code for the same page. Setting state is the whole mechanism.

		**On the existing tick rather than a timer of its own** (Simon's decision of 2026-08-17).
		It costs no request — `#781`'s finding was a poll *fetching* when nothing had changed,
		which this does not do — and a second interval is one more thing `_driven` has to hold,
		in a harness whose whole design is holding the one.
	*/
	const [, retick] = useState(0);
	const [more, setMore] = useState(null);
	/* What each of a board's columns held back — `#1790`. Null unless the answer was grouped. */
	const [cut, setCut] = useState(null);
	/*
		How much each column of a board is currently allowed — `#1790`.

		**Named for what it holds, not `allowed`**, which this component already binds to the
		reader's permission set eighty lines below — `#1409`'s collision, caught here by the
		parser rather than by a reader.

		**A ref rather than state, for the reason `shown` is one**: `load` is called from a
		dozen places, including the poll, and a callback closes over the render that made it. As
		a defaulted argument this reset to 25 on the next poll, so *Show more* widened the board
		for ten seconds and then silently undid itself.

		It outlives leaving the board, which is deliberate and costs nothing: a widened
		allowance means nothing to an arrangement that does not group, so the only reader it
		reaches is one who comes back to a board they had already widened.
	*/
	const columnSize = useRef(COLUMN);
	/* **Read once, from the same storage `index.html` read before the first paint** (`#908`).
	   Held here only so the control shows the right option: the attribute is already on the
	   document by the time this runs, so re-applying it on mount would be a second copy of a
	   decision the page has made. */
	const [theme, setTheme] = useState(() => themeChoice(globalThis.localStorage));
	/* **What this browser remembers about collapsed columns** (`#1008`), read once for the
	   same reason the theme is: the value belongs to the browser rather than to a request, and
	   re-reading it on every render would make storage a dependency of the poll.

	   Only what somebody explicitly chose lives here — the *defaults* are decided per render by
	   `collapsedColumns`, because they depend on the selection in the address and that changes
	   under this component without storage having anything to say about it. */
	const [columnChoices, setColumnChoices] = useState(
		() => choicesIn(globalThis.localStorage, BOARD_COLUMNS_REMEMBERED)
	);
	/* **What this browser remembers about revealing a truncated section** (`#1820`), keyed by
	   the section's name rather than by the item — so it is bounded by how many sections this
	   app has, where a key per item would grow for ever and be stale the moment an item's link
	   count changed. Read once, for `columnChoices`' reason. */
	const [sectionChoices, setSectionChoices] = useState(
		() => choicesIn(globalThis.localStorage, SECTIONS_REMEMBERED)
	);
	/* The project the address narrows to, or null for the whole workspace (`#647`). Held
	   beside the workspace rather than derived on each render, because the poll and every
	   write reload the list and all of them have to narrow the same way. */
	const [project, setProject] = useState(null);
	/* The agenda, or null when a listing is what is showing (`#652`). Null rather than a
	   separate `showing` flag, because "there is an agenda to render" and "the agenda is what to
	   render" are the same fact and two would drift. */
	const [agenda, setAgenda] = useState(null);
	/*
		Whether the address named no place at all — `#1215`.

		**Not the same fact as "the agenda is showing", and conflating them was the defect
		waiting to happen.** Until the agenda became a view those two were the same thing, so
		`listingAddress` took `agenda: agenda !== null` and returned `/` from it. Now a project
		can be showing an agenda, and an address written from that flag would send a reader from
		`/projects/subroutine` back to the merged root every time they closed an item.

		The merged agenda is the one thing `/` means, and what makes it merged is that nobody
		named a workspace.
	*/
	const [everywhere, setEverywhere] = useState(true);
	const [unscheduled, setUnscheduled] = useState(0);
	/* How much more somebody else is sitting on than this page drew (`#1285`). */
	const [heldUp, setHeldUp] = useState(0);
	const [later, setLater] = useState(0);
	/* What the day is holding back that somebody chose to hold back — `#1215`. */
	const [deferred, setDeferred] = useState(0);
	const [paused, setPaused] = useState(0);
	const [gone, setGone] = useState(0);
	/* Work of somebody else's, which this page is narrowed away from and a list is not
	   (`#1265`). */
	const [theirs, setTheirs] = useState(0);
	/*
		The add form: whether it is open, and the two answers it needs to draw its dropdowns
		(`#756`).

		**Fetched once the page is ready rather than when the form opens.** Two requests and
		about 8 KB per workspace, against a poll that spends one every ten seconds for the life
		of the page — so the cost is nothing and what it buys is the absence of a loading state
		inside a form. Every fault this app has shipped came from something not having landed
		yet in the render that read it; a disclosure that fetches on open is one more of those,
		and this one would show a reader an empty type dropdown rather than a blank page.

		**Keyed by nothing, cleared on a workspace change.** The vocabulary is per workspace, so
		a cache carrying `personal`'s statuses into `projects` would be a control that looks
		complete and offers the wrong words.
	*/
	const [expanded, setExpanded] = useState(false);
	/* Whether the disclosed form is writing a document rather than a task (`#761`). Beside
	   `expanded` rather than inside it, because closing the form and reopening it should not
	   silently change what it will write. */
	const [writing, setWriting] = useState(false);
	/* Whether the open item is being edited, and the version somebody else saved underneath it
	   (`#757`). `conflict` holds *their* item rather than a flag, because the only useful thing
	   to say about a 409 is what the item says now. */
	const [editing, setEditing] = useState(false);
	const [conflict, setConflict] = useState(null);
	/* A write held back until somebody says which occurrences it is for (decision `#1249`,
	   `#1253`). It holds *the write* rather than a flag, because two gestures reach it — a save
	   and handing an item to somebody — and the answer has to resume whichever one was
	   interrupted. Null whenever nothing is being asked, which is nearly always. */
	const [asking, setAsking] = useState(null);
	/* What the server made of the repeat somebody is typing (`#94`, §6.7). Null until they
	   type something, so the disclosure opens saying nothing rather than complaining about an
	   empty box. Shared by both forms because only one of them is ever on screen. */
	const [reading, setReading] = useState(null);
	/* The newest phrase asked about, so an answer overtaken in flight can be dropped. A ref
	   rather than state: it is read by the callback that wrote it and never rendered, so
	   putting it in state would re-render the page on every keystroke to no effect. */
	const latestRepeat = useRef("");
	const [vocabulary, setVocabulary] = useState(null);
	const [filable, setFilable] = useState([]);

	/* **What this reader may do here, so a control they cannot use is not drawn** (`#927`'s
	   M-25). Every handler below is passed conditionally on this: the components already
	   render nothing when a handler is absent, which is the mechanism `finishedOnly` has used
	   for `onAdd` all along. */
	const allowed = allowedIn(me, workspace);
	const mayWrite = allowed.has("task:write");
	/* **There is no `mayComment` beside this, and `#684`'s dead-code guard is what said so.**
	   The comment box is offered on an open item and nowhere else, so the only permission check
	   commenting ever needed is the *item's* — `mayCommentThere` below. This one had no reader
	   left the moment that landed. */
	/*
		What is on screen — the arrangement and the selection — read from the address rather
		than remembered (`#651`), so a reader can send somebody the thing they are looking at.

		**One state holding both, not two** (`#738`). `setView` not having landed in the render
		that reads it is `#719`'s defect, and two setters would give it two chances to happen
		with the halves disagreeing. They are one fact: what this page is showing.
	*/
	const [showing, setShowing] = useState({ view: DEFAULT_VIEW, selection: {} });
	const since = useRef(null);

	/* **What served this page, captured once and never again** — `#785`. `me` is refetched
	   after a prioritise, so comparing against the live value would quietly move the baseline
	   and the release would never be noticed. */
	const served = useRef(null);
	const polled = useRef(0);

	/*
		**The same fact again, where a callback can read it** — and the ref is the copy that is
		never stale, which is why `since` is one too.

		`load` used to take the selection from `window.location.search`, and `#719`'s reasoning
		for that was sound while it held: the address is written by `go` before any load that
		changes one, so it cannot lag the way `showing` lags the render that calls `load`.

		**What `#766` took away is the premise.** An item's address carries no query, so while
		one is open the bar says nothing about the listing behind it — and the poll goes on
		calling `load` every ten seconds. Reading the address there would quietly refetch the
		default selection under a reader who had chosen another, and they would not see it until
		they closed the item and found the columns they asked for empty.

		So the address is a valid source only while it *is* a listing address, and this is the
		source that is valid always.
	*/
	const shown = useRef(showing);

	const nowShowing = useCallback((next) => {
		/* **One writer for both copies**, so the two cannot disagree — the whole hazard of
		   keeping a second one. Every place that changes what is showing calls this and none
		   calls `setShowing`, which `tests/test_web.py` holds. */
		shown.current = next;
		setShowing(next);
	}, []);

	/*
		**The open item, where a callback can read it** — `#657`, and the same arrangement
		`shown` has for the same reason.

		The poll runs from an interval built once per workspace, so it holds whatever `open` was
		when that interval was made — which is almost always nothing, because a reader opens an
		item long after the page has settled. Putting `open` in the effect's dependency array
		instead would restart the ten-second window every time anybody opened or closed
		anything, which is the trade that was already refused for `project`.

		It carries the workspace it was read from as well as the item, because a refetch has to
		ask the same place the first read asked. Deriving it from state would be reading a
		second fact from a third copy.
	*/
	const held = useRef(null);

	const nowOpen = useCallback((next) => {
		/* **One writer for both copies**, as above and held by the same kind of test. */
		held.current = next;
		setOpen(next);
	}, []);

	const go = useCallback((path, { replace = false, arranged = showing } = {}) => {
		/*
			**Every address this app writes goes through here**, carrying the arrangement and the
			selection.

			Four places wrote one before `#651` and all four dropped the query, so
			`/projects?view=board` became `/projects` the moment anything was opened — the view
			would have been a setting that silently expired on the first click.
		*/
		const wanted = withShowing(path, arranged);

		if (window.location.pathname + window.location.search === wanted) return;

		window.history[replace ? "replaceState" : "pushState"]({}, "", wanted);
	}, [showing]);

	const readAgenda = useCallback(async (spaces, slug = null, key = null) => {
		/* What to ask for and how to group it are both pure and checked (`agendaRequest`,
		   `agendaBuckets`). What is left here is holding the answer.

		   **The scope is passed rather than read from state** (`#1215`), for the reason `start`
		   gives about `slug` three call sites away: `setWorkspace` and `setProject` have not
		   landed in the render that calls this, so a read of either would ask about the place
		   the reader just left. */
		const answered = await sent(agendaRequest(slug, key));

		setAgenda(agendaBuckets(answered, spaces));
		setUnscheduled(
			Math.max(0, (answered.unscheduled_total || 0) - (answered.unscheduled || []).length),
		);
		setHeldUp(
			Math.max(
				0,
				(answered.blocked_by_others_total || 0)
					- (answered.blocked_by_others || []).length,
			),
		);
		setLater(answered.later_total || 0);
		/* **Both defaulted on the wire** (`#345`, `#482`), so a page served by an instance that
		   predates them reads zero and draws one line fewer rather than refusing. */
		setDeferred(answered.deferred_total || 0);
		setPaused(answered.paused_total || 0);
		setGone(answered.passed_total || 0);
		setTheirs(answered.assigned_elsewhere_total || 0);
	}, []);

	const load = useCallback(async (slug, key = null, after = null, columns = null) => {
		if (!slug) return;

		/*
			**The selection is read from the ref, not from state and not from an argument** —
			`#719`, and the parameter this replaces is the defect.

			It was `arranged = view`, which reads the *state*, and `setView` does not land in the
			render that calls this. So the first load after arriving at `/projects?view=board`
			asked for the list's rows; ten seconds later the poll — recreated once `view` had
			landed — asked for the board's. Right rows, one `POLL_MS` late, which is exactly how
			Simon described it.

			**The comment in `start` three lines from the call site says this precise thing about
			`slug`**, and I quoted its reasoning elsewhere in the same commit without applying it
			here. Passing the value at both sites would have worked and left the trap for the
			next call site, so the parameter is gone instead.

			It read `window.location.search` until `#766`, on the grounds that the address is the
			one source that is never stale. `nowShowing` is that source now, and for the reason
			written there: an item's address carries no query, so the bar stops answering this
			question the moment one is open — and the poll keeps asking it.
		*/
		const chose = shown.current.selection;

		/* What to ask for is `listingRequests`, which is pure and checked (`#640`). What is
		   left here is what to do with the answers. */
		const wanted = listingRequests(slug, key, after, chose, columns ?? columnSize.current);
		let answers;

		try {
			answers = await Promise.all(wanted.map(sent));
		} catch (failure) {
			/*
				**A project named in an address may not be there any more, and that is the case
				this whole design exists for.** `sr` became `subroutine` on 2026-08-08 across
				502 items; a link somebody saved before that names a project the instance will
				now refuse with a 404. Letting it through would replace the page with a failure
				— for an address that still identifies its item perfectly well.

				So the filter is dropped and the workspace is read instead, with the reason said
				out loud. Only for the filter: a 404 with no project asked for is a different
				fact and belongs to the caller.
			*/
			if (failure.status !== 404 || !key) throw failure;

			setNote({ text: `There is no project called ${key} here any more. `
				+ `Showing the whole workspace.`, tone: "bad" });
			setProject(null);

			return load(slug, null, after);
		}

		/* **Whichever shape arrived** (`#1790`) — `unpacked` is pure and driven, so the rule
		   for reading a grouped answer is not a branch buried in this callback. */
		const { rows: fetched, cut, more: left } = unpacked(answers, wanted);

		/*
			**What the list becomes is `accumulated`, which is pure and driven** (`#660`, `#706`).

			The cost of re-merging is that *Show more* can insert rows above where a reader is
			looking. That is inherent to two streams paged separately and is the right trade: a row
			in the wrong place is a list you cannot trust, and a row appearing above the fold is
			one you can.
		*/
		setItems((existing) => accumulated(existing, fetched, {
			appending: Boolean(after),
			collections: wanted.length,
			// **From the rows as well as the selection** (`#875`): a search the server ranked
			// says so by populating `relevance`, and re-deriving that rule here would be a
			// second copy of it. Computed over what is about to be held, not over `fetched`
			// alone, so an appended page cannot merge on a different key from the one below it.
			//
			// **`chose`, which is `shown.current.selection`, never `showing`.** A callback
			// closes over the render that made it, so `showing` lags — which is the whole
			// reason `shown` exists, and `load` already reads the selection that way a few
			// lines above. Reading it the other way here would merge a page by the arrangement
			// the reader had *before* the one they are looking at.
			ordering: mergeOrder(chose, after ? [...existing, ...fetched] : fetched),
		}));

		/*
			**What was left behind, so the listing can say so.** The envelope has carried
			`has_more` since M1 and this app read it nowhere — so it showed 100 of 142 and
			looked complete, which is how an item somebody had just written became unfindable
			rather than merely mis-sorted. A count is deliberately not asked for: §8.4 declines
			`include_total` because it costs a second full scan, and "there is more" is the part
			a reader acts on.

			**A collection this view did not ask for has nothing left behind**, so it reports
			null rather than being absent — `Listing` and `Board` both read both keys, and an
			undefined would make *there are more* depend on which view was showing.
		*/
		setMore(left);

		/* **What each column held back, so a heading can stop reading as a total** (`#1790`).
		   Null where nothing was grouped, which is every arrangement but the board — and the
		   distinction matters, because *not split* and *split, nothing held back* are two
		   different things to say under a column. */
		setCut(cut);
	}, []);

	const words = useCallback(async (slug) => {
		/*
			What this workspace calls things, and where an item can be filed — the two answers
			the add form's dropdowns are built from (`#756`).

			**Called where `roster` is called rather than from an effect**, because it answers
			the same question at the same moment: the workspace has changed, so everything
			workspace-shaped has to be asked again. An effect would be a fourth thing watching
			`workspace` and a fourth chance for it to run against a value that has not landed.

			**Cleared first, so a failure cannot leave the previous workspace's words on screen.**
			A type dropdown offering another workspace's types is worse than one offering none:
			the second is visibly unfinished and the first is confidently wrong.

			Its failure is survivable, like the roster's: the capture line does not need any of
			this, and §1.4 says it must not.
		*/
		if (!slug) return;

		setVocabulary(null);
		setFilable([]);

		try {
			const [meta, projects] = await Promise.all([
				sent(vocabularyRequest(slug)),
				sent(projectsRequest(slug)),
			]);

			setVocabulary(meta);
			setFilable(projects.items);
		} catch (_) {
			/* Left empty and disabled, which is what the form renders when it has nothing to
			   offer. Nothing else on the page depends on it. */
		}
	}, []);

	/*
		**The same three answers, for the workspace the *open item* is in** — `#1041`.

		`words` and `roster` above answer them for the switcher's workspace, which is right for
		the listing, the board and the capture line and wrong for an item opened from the agenda:
		a status picker offering another workspace's statuses cannot write any of them, and the
		one this item is *in* is not on the list. `words`'s own comment states the rule — *"a
		type dropdown offering another workspace's types is worse than one offering none: the
		second is visibly unfinished and the first is confidently wrong"* — about switching
		workspaces, and this is the same sentence one surface along.

		**Kept as one extra answer rather than a cache by slug.** Two workspaces are in play at
		most: the one the listing is showing and the one the open item is in. A map keyed by slug
		would be a third thing to invalidate for a case that cannot arise.

		`learned` is a ref rather than state because nothing renders it: it stops a second open
		of the same foreign workspace refetching, and is cleared on failure so a retry is not
		locked out.
	*/
	const [elsewhere, setElsewhere] = useState(null);
	const learned = useRef(null);

	const learnAbout = useCallback(async (slug) => {
		/* Ask one workspace what it calls things, where work can be filed there, and who is in
		   it — the three answers an open item has to be furnished from. */

		if (!slug || learned.current === slug) return;

		learned.current = slug;

		try {
			const [meta, projects, joined] = await Promise.all([
				sent(vocabularyRequest(slug)),
				sent(projectsRequest(slug)),
				sent(rosterRequest(slug)),
			]);

			setElsewhere({
				slug,
				vocabulary: meta,
				projects: projects.items,
				members: people(joined.items),
			});
		} catch (_) {
			/* **Nothing is stored, and that is the whole of the failure path.** A reader whose
			   credential cannot reach this workspace is shown an empty picker rather than
			   another workspace's words — which is what the last branch of `furnished` already
			   says, so writing an empty answer here as well would be a second statement of one
			   rule. Clearing `learned` is what lets the next open try again. */
			learned.current = null;
		}
	}, []);

	const roster = useCallback(async (slug) => {
		/*
			Who work can be handed to. Its own request because it changes on a different clock
			from the backlog — and its failure is survivable: without it the assignment control
			is simply absent, which is honest, where a picker that cannot be filled is not.
		*/
		if (!slug) return;

		try {
			const found = await sent(rosterRequest(slug));

			setMembers(people(found.items));
		} catch (_) {
			setMembers([]);
		}
	}, []);


	/*
		**This page is about that workspace now** — `#1042`, and it is one step in three places.

		Three things follow a workspace and none of them is derived from the others: the state
		the listing, the capture box and the permissions are drawn from, who is in it, and what
		it calls things. `chooseWorkspace` did all three and was the only caller — so the two
		*other* ways a reader reaches another workspace, an address stepped back onto and a
		project chip on an item from elsewhere, moved the address and left every one of them
		pointing at the workspace before.

		**A no-op where it is already that workspace**, which is what makes it safe to call
		without asking: choosing the one you are in should not refetch its vocabulary, and both
		of the other callers reach it far more often with the workspace unchanged than changed.
	*/
	const enter = useCallback((slug) => {
		if (!slug || slug === workspace) return;

		setWorkspace(slug);

		/* Not awaited, and neither is a failure lost: both swallow their own and leave the
		   control they fill simply absent, which is what `words` argues for in its own words. */
		roster(slug);
		words(slug);
	}, [roster, words, workspace]);
	const fetched = useCallback(async (ref, kind, slug) => {
		/*
			Read one item, working out what it is when nobody said.

			A ref names a task *or* a document — one counter per workspace serves both (§6.2) —
			so an address carries no kind and there is nothing to infer it from. Ask about a
			task, and read the 404 as "then it is a document" rather than as a failure. Only a
			refusal from the *second* is a refusal.
		*/
		const order = kind ? [kind] : ["task", "document"];

		for (const trying of order) {
			try {
				const [item, links, comments, governing, checked, parts] = await Promise.all(
					itemRequests(trying, ref, slug).map(sent),
				);

				return { item: { ...item, kind: trying }, links: links.items,
					comments: comments.items, governing: governing.items,
					/* Absent for a document, which asks for no such thing — so this is the
					   empty list rather than a read of `undefined`. */
					checked: checked ? checked.items : [],
					/* **The envelope is kept, not flattened** (`#1218`). `has_more` is the only
					   thing that can tell fifty parts from fifty-one, and a bare array would
					   lose it — which is the shape `#1175` is open about elsewhere. */
					parts: parts
						? {
							items: parts.items,
							/* **`page.has_more`, not `has_more`.** The envelope nests it
							   (§8.4) and reading the top level would have answered *no more*
							   for every parent there is — the cap saying nothing, silently,
							   which is the exact failure the line under the list exists to
							   prevent. */
							has_more: !!(parts.page && parts.page.has_more),
						}
						: { items: [], has_more: false } };
			} catch (failure) {
				if (failure.status !== 404 || trying === order[order.length - 1]) throw failure;
			}
		}

		return null;
	}, []);

	const show = useCallback(async (
		row, { history = true, slug = workspace, quiet = false } = {},
	) => {
		/*
			**Reports whether it opened anything, and `quiet` suppresses the note when a
			caller has its own answer to a ref that is not there** — `#976`. A search for
			`#916` tries this first and falls back to searching for the text, so *there is no
			#916 here* would be a refusal contradicted a moment later by the results.
		*/
		try {
			const found = await fetched(row.ref, row.kind, slug);

			/* **With the workspace it was read from**, so a background re-read asks the same
			   place (`#657`). `slug` defaults to the current workspace and is overridden when a
			   row from somewhere else is opened, so it is the only copy that is always right. */
			nowOpen({ ...found, slug });

			/* **And what that workspace calls things** (`#1041`). Only when it is not the one
			   already asked: the ordinary open costs nothing, and an item from the agenda costs
			   three requests once. */
			if (slug !== workspace) learnAbout(slug);

			/*
				**The address is written from what came back, not from what was clicked.** So a
				link somebody was sent with a retired project name in it corrects itself the
				moment the item is read — `replaceState` rather than `pushState` for that, since
				the stale spelling should not become a step in the reader's own history.
			*/
			/* **Under the path the reader is on** (`#772`), which `parseAddress` reads out of
			   the bar rather than out of state — `go` has not written anything yet, so this is
			   still the address they clicked from. */
			go(addressOf(found.item, slug, parseAddress(window.location.pathname)),
				{ replace: !history });

			window.scrollTo(0, 0);

			return true;
		} catch (failure) {
			/*
				**A ref that is not there is a note, not the end of the page.**

				Prose mentions whatever somebody wrote, and `#999` is linked without asking
				whether it exists — checking would be a request per mention, on descriptions
				that carry forty. So the dead ones arrive here, and a reader who followed one
				should be told that and left with their list. Found by driving: the first
				version replaced the whole app with an error page for a typo in a description.
			*/
			if (failure.status === 404) {
				if (!quiet) setNote({ text: `There is no #${row.ref} in ${slug}.`, tone: "bad" });

				nowOpen(null);

				return false;
			}

			setError(failure);
		}

		return false;
	}, [fetched, go, learnAbout, nowOpen, workspace]);

	const close = useCallback(({ history = true } = {}) => {
		nowOpen(null);

		/*
			**Back to what is actually behind it**, which used to be a hard-wired `/` — harmless
			while `/` was the list and wrong the moment `#652` made it the agenda, because the
			address then said the agenda while the page went on showing a workspace listing.
			Nothing failed: an address disagreeing with its page is not something any test here
			can see, and it was found by reading this while wiring `#651`'s view through it.
		*/
		if (history) go(listingAddress({ agenda: everywhere, workspace, project }));
	}, [agenda, go, nowOpen, project, workspace]);

	const refresh = useCallback(async () => {
		/*
			Read the open item again, in the background — `#657`.

			**Not `show`, and the difference is the whole point.** `show` is somebody arriving:
			it writes the address and scrolls to the top. Doing either underneath a reader who
			has scrolled halfway down a description is worse than leaving them ten minutes
			stale, which is the judgement `#597` already made about a failed poll.

			**Never while the item is being edited**, and that is correctness rather than
			courtesy. The form carries `expected_version` (§8.9), so swapping the item under it
			would swap the version too — and the save that should have been refused with
			*somebody changed this* would instead go through and overwrite them silently. The
			409 is the design; this must not defeat it.

			**Gone is a note, not the end of the page.** A 404 here says the item was deleted
			while somebody was reading it. What is on screen is still the last thing they were
			shown and is worth keeping — `#597` settled that a page is blanked only when nothing
			on it is worth keeping, and this is not one of those.
		*/
		const reading = held.current;

		if (!reading) return;

		/*
			**Standing off leaves a mark** (`#792`). The poll advances its cursor before asking
			whether to re-read, so an event that touched this item while the form was open is
			*consumed*: nothing would ever look at it again. Saving hides that, because `wrote`
			re-reads and §8.9 catches the clash — **cancelling does not**, and the pane goes back
			to showing an item that moved, with nothing saying so. That is the state `#657` exists
			to remove, surviving in the one path it did not cover.

			A mark rather than a re-read on every close: most edits are abandoned with nothing
			having happened, and three requests each time would be the cost of a case that is
			rare.
		*/
		if (editing) {
			missed.current = true;

			return;
		}

		try {
			const found = await fetched(reading.item.ref, reading.item.kind, reading.slug);

			if (found) nowOpen({ ...found, slug: reading.slug });
		} catch (failure) {
			if (failure.status === 404) {
				setNote({
					text: `#${reading.item.ref} is no longer there. What is on screen is the `
						+ "last version you were shown.",
					tone: "bad",
				});

				return;
			}

			/* Anything else is a background request that did not work, which is what the poll
			   around it already swallows. The item on screen stays exactly as it was. */
		}
	}, [editing, fetched, nowOpen]);

	useEffect(() => {
		/*
			**`project` is a dependency because the interval closes over it, not because it is
			read during the effect.** Without it the page widened itself ten seconds after a
			reader opened a project: `start` calls `setWorkspace` *before* awaiting the head of
			the feed and `setProject` after, so the two land in different commits. This effect
			re-ran on the workspace one — while the filter was still `null` — and the interval
			it created kept that `null` for the life of the page. The first poll to see an event
			then called `load(workspace, null)` and replaced a correct seven-item list with the
			whole workspace, at an address that still said the project.

			That is the shape worth remembering: nothing here is stale on the render, only in
			the callback the render leaves behind. The list widened, the address did not, and
			the reader is left with somebody else's backlog under their own project's URL.

			The cost is that navigating between projects restarts the ten-second window. That is
			the right trade — a poll that is late by one tick is invisible, and one that reloads
			the wrong list is what this is fixing.
		*/
		if (error || !workspace) return undefined;

		const tick = setInterval(async () => {
			/* **Before the request, not after it.** A poll that fails changes nothing on
			   screen by design, and the clock has still moved — so a mark that should have
			   gone must go whether or not the instance answered. */
			retick((count) => count + 1);

			/* **An hour, riding the poll rather than keeping its own timer** (`#785`). A
			   release is not something that happens between two glances at a page, and one
			   extra request an hour against a 600-a-minute allowance costs nothing. `/v1/me`
			   rather than `/v1/meta`, which this item proposed: it is the smaller response by
			   a long way, it is already the first request this page makes, and it has carried
			   `instance_version` since `#381`. */
			polled.current += 1;

			if (polled.current >= RELEASE_CHECK_POLLS) {
				polled.current = 0;

				try {
					const running = await sent(identityRequest());

					if (releaseMoved(served.current, running.instance_version)) {
						setReleased(true);
					}
				} catch (unreachable) {
					/* The next check asks again. An instance that cannot be reached says
					   nothing about which version it is running. */
				}
			}

			try {
				/* **The agenda spans workspaces, so its poll must too** (`#652`) — and it has
				   to reload the thing on screen rather than the list underneath it. `agenda` is
				   in the dependency array below for the reason `project` is: the interval
				   closes over it, and an interval created while the list was showing would go
				   on reloading the list for the life of the page. */
				const onAgenda = agenda !== null;
				const seen = await sent(pollRequest(onAgenda ? null : workspace, since.current));

				/* **The dedupe `?since=` asks its callers to do** (`#781`). It is inclusive, so
				   the resumed event comes back every time and `seen.items` is never empty —
				   which made the test below always true, left the cursor exactly where `start`
				   put it, and reloaded the listing every ten seconds whether or not anything
				   had happened. The feed was being called and its answer discarded. */
				const fresh = freshly(seen.items, since.current);

				if (fresh.length === 0) return;

				since.current = fresh[fresh.length - 1].seq;

				/* **The open item first, because it is what the reader is looking at** (`#657`).
				   `held` rather than `open` for the reason `since` is a ref: this callback is
				   left behind by a render that almost certainly had nothing open. */
				if (touching(fresh, held.current && held.current.item, seen.page,
					held.current ? held.current.links : [])) await refresh();

				await (onAgenda
					? readAgenda(me ? me.workspaces : [], everywhere ? null : workspace, project)
					: load(workspace, project));
			} catch (failure) {
				/* A poll that fails changes nothing on screen. The next one may work, and
				   replacing a readable page with an error because a background request
				   timed out is worse than being ten seconds stale.

				   **Except when the answer is that this reader is no longer signed in**
				   (`#927`'s M-26). A session lapses after a fixed span and can be ended from
				   another window, and swallowing the 401 left the page re-rendering the same
				   stale rows every ten seconds for ever — every control on it refusing, with
				   nothing saying why. That is the one failure the *next* poll cannot fix, so
				   it is the one that has to reach the reader. */
				if (failure.status === 401) setError(failure);
			}
		}, POLL_MS);

		return () => clearInterval(tick);
	}, [error, workspace, project, agenda, everywhere, me, load, readAgenda, refresh]);

	const signOut = useCallback(async () => {
		/* **The answer is asked for and then acted on**, rather than the page being blanked
		   optimistically: a refusal here means the reader is still signed in, and a page that
		   said otherwise would be lying about the one thing they just asked about.

		   The 401 that follows is not a failure — it is the state they asked for — so it goes
		   through the same `Failed` panel that says how to get a new link. */
		try {
			await sent(signOutRequest());

		} catch (failure) {
			setNote({ text: `That did not sign you out. ${failure.message}`, tone: "bad" });

			return;
		}

		setError({ status: 401, message: "You are not signed in." });
	}, []);

	useEffect(() => {
		/*
			**What the browser tab says** — `#1214`, Simon: *"I have multiple tabs open and they
			all just say 'Subroutine'."*

			**An effect rather than a render, because `document` is not this app's to draw.**
			Everything else here returns markup and lets Preact decide when it lands; the title
			is a property of the document, so it is written after the render that decided it —
			which is also what makes it survive a navigation that changes nothing else on screen.

			**Deciding what it says is `pageTitle` and is pure** (`#640`), so what is left here
			is the assignment. That split is the one this arc keeps being rescued by: four of the
			faults it shipped were wiring, and none was a rule.

			**`filable` is the project tree in pre-order**, which is the shape `titlesByPath`
			needs — it is `GET /v1/projects?order=path` verbatim, never `filableFor`'s reordering,
			which moves the Inbox to the front and would put a depth-walk out by one subtree.
		*/
		document.title = pageTitle({
			item: open && open.item,
			place: { workspace: everywhere ? null : workspace, project },
			showing,
			workspaces: me ? me.workspaces : [],
			projects: filable,
		});
	}, [everywhere, filable, me, open, project, showing, workspace]);

	const start = useCallback(async () => {
		setError(null);

		try {
			const identity = await sent(identityRequest());
			const asked = parseAddress(window.location.pathname);
			const arrangement = showingOf(window.location.search);

			nowShowing({ view: arrangement.view, selection: arrangement.selection });
			const { slug, refused } = chosenWorkspace(
				asked, identity.workspaces.map((space) => space.slug), workspace,
			);

			if (served.current === null) served.current = identity.instance_version || "";

			setMe(identity);
			setWorkspace(slug);

			/* **Every refused word, named** — `viewOf`'s rule applied to the selection too
			   (`#738`). One note rather than one per word: a reader who mistyped two things
			   needs to know both, and `setNote` holds one. */
			if (arrangement.refused.length > 0) {
				setNote({
					text: `This address asks for ${arrangement.refused.join(" and ")}, `
						+ `which is not something to ask for. Showing the list.`,
					tone: "bad",
				});
			}

			if (refused !== null) {
				setNote({
					text: `There is no workspace called ${refused} that you can see.`
						+ (slug ? ` Showing ${slug}.` : ""),
					tone: "bad",
				});
			}

			/* The head of the feed, so the first poll asks what happens *next* rather than
			   replaying everything that ever has. **Null rather than zero when there is no
			   head** — an instance nobody has used yet has no events, and `since=0` is refused
			   (`#656`). */
			const head = await sent(headRequest());
			since.current = head.items.length > 0 ? head.items[0].seq : null;

			/*
				**The item an address names is opened here, beside the list rather than after
				it** (`#645`). Loading the list first and opening the item from an effect meant
				a deep link showed a page of somebody else's work, briefly, before showing the
				thing that was asked for — and waited for it.

				`slug` is passed rather than read from `workspace`: `setWorkspace` has not
				landed in this render, so the closure still holds the previous value, and a read
				scoped to it would be scoped to the wrong workspace or to nothing.
			*/
			setProject(asked && asked.project);
			setEverywhere(asked === null);

			/*
				**`/` is the *merged* agenda, and every other address is a place** — decision
				`#649`, built by `#652`, amended 2026-08-24. The test is the address: naming no
				workspace is somebody who has not asked for one, and what they want is their
				day across all of them, which is what bare `subroutine` gives them at a terminal
				(§12.2).

				**What that no longer decides is the *arrangement*.** A place gets an agenda too
				now, of its own work; `#649`'s grammar always said so and nothing had built it.

				The workspace is still resolved and the roster still read, because the switcher
				and every write need one — the merged agenda spans them all, but *adding*
				something has to land somewhere.
			*/
			await Promise.all([
				/*
					**The arrangement decides which reader now, not the address** (`#1215`,
					amending `#649`). It was `asked === null`, so the agenda was the thing at
					the root and `/?view=list` — an address `#649` itself specifies — rendered
					the agenda and dropped the parameter.

					`DEFAULT_VIEW` is the agenda, so a bare address still gets one; what
					changed is that a scoped address gets one too, and that `?view=list` is
					finally obeyed at every address rather than only below the root.
				*/
				arrangement.view === AGENDA_VIEW
					? readAgenda(
						identity.workspaces, asked === null ? null : slug, asked && asked.project,
					)
					: load(slug, asked && asked.project),
				roster(slug),
				words(slug),
				asked && asked.ref !== null
					? show({ ref: asked.ref }, { history: false, slug })
					: null,
			]);
		} catch (failure) {
			setError(failure);
		} finally {
			setReady(true);
		}
	}, [load, nowShowing, readAgenda, roster, show, words, workspace]);

	useEffect(() => {
		start();
	}, []);

	useEffect(() => {
		/*
			**Read what was missed, once the form is out of the way** — `#792`.

			The stand-off itself is correctness rather than courtesy and stays: the form carries
			`expected_version` (§8.9), so replacing the item under it would replace the version
			too, and a save that should have been refused with *somebody changed this* would go
			through and overwrite them.

			So the news waits rather than being dropped. Silent, like every other background
			read (`#657`): nothing scrolls, nothing moves, and the reader is looking at what the
			instance holds rather than at what it held when they started typing.
		*/
		if (editing || !missed.current) return;

		missed.current = false;
		refresh();
	}, [editing, refresh]);

	/*
		Every address a reader reaches with the back button.

		**The one they arrived at is `start`'s** (`#645`), so this does not read the address on
		mount — doing both fetched the same item twice. Back out of an item and an address with
		no ref is the list, which is why one handler covers both directions.

		**It sits below `show` because a dependency array is evaluated where it is written.**
		Declared above it, `show` is in its temporal dead zone and the whole app throws
		`Cannot access 'show' before initialization` — a blank page, which is exactly where it
		shipped from (`#643`). The order of the `const`s was checked and this was not, because
		it is not one of them.
	*/
	useEffect(() => {
		if (!ready || error || !workspace) return undefined;

		const arrive = () => {
			const asked = parseAddress(window.location.pathname);
			const narrowed = (asked && asked.project) ?? null;

			/* The arrangement is in the address too (`#651`), so stepping back into a board
			   restores the board rather than leaving the list under an address saying otherwise
			   — which is the disagreement `close` used to create for the agenda. */
			const back = showingOf(window.location.search);

			/*
				**Asked before `nowShowing` overwrites the answer** — `#767`. `shown.current` is
				the live copy of what is on screen (the state lags in a callback, which is
				`#657`), so this is the last moment the question *did the selection change* can
				be put at all.
			*/
			const changed = reloads(shown.current, back);

			nowShowing({ view: back.view, selection: back.selection });

			/*
				**The workspace the address names** — `#1042`, and `#1040` fixed the *item* half
				of this and left the listing. Stepping forward onto an item outside the switcher's
				workspace loaded that switcher's rows underneath it, so closing the item showed
				one workspace's backlog under an address naming another.

				`chosenWorkspace` rather than `asked.workspace` directly, because an address is
				anybody's to type: a slug this reader cannot see falls back exactly as it does on
				arrival, rather than loading a listing that can only refuse.
			*/
			const { slug } = chosenWorkspace(
				asked, (me ? me.workspaces : []).map((space) => space.slug), workspace,
			);

			enter(slug);

			/*
				**Stepping back to `/` is stepping back to the agenda** (`#652`), and this has
				to make the same decision `start` does or one address would mean two things
				depending on how the reader got there. `#645`'s split — the arrival address is
				`start`'s, every later one is this — is exactly what makes that a real risk.
			*/
			setEverywhere(asked === null);

			if (back.view === AGENDA_VIEW) {
				/* **Stepping into an agenda, at whatever place the address names** (`#1215`).
				   It was `asked === null`, which was the same question while the agenda lived
				   only at the root; a scoped agenda makes the arrangement the thing to ask
				   about, and `start` above asks it the same way so one address cannot mean two
				   things depending on how the reader arrived. */
				setProject(narrowed);
				readAgenda(
					me ? me.workspaces : [], asked === null ? null : slug, narrowed,
				);
			} else if (agenda !== null || narrowed !== project || changed) {
				/* Leaving the agenda for a listing, or moving between listings. The filter is
				   part of the address too (`#647`), so stepping back out of a project restores
				   the whole workspace rather than leaving the list narrowed to something the
				   address no longer says.

				   **And a changed selection, which is `#767`.** This branch predates the
				   selection being in the address at all: when only the project could change
				   which rows there are, `narrowed !== project` was the whole question. `#738`
				   made the selection a second thing that changes it and this was not revisited
				   — so stepping back out of the finished view left the reader looking at
				   finished rows under an address saying the ordinary list, with an empty-state
				   message that would have read *Nothing here yet.*

				   `reloads` is what answers it, here and in `chooseView`, so the two cannot
				   drift about what makes a selection different. */
				setAgenda(null);
				setProject(narrowed);
				load(slug, narrowed);
			}

			if (asked === null || asked.ref === null) {
				nowOpen(null);
				return;
			}

			/* **In the workspace the address names** — `#1040`. This let the slug default to
			   the switcher's, so stepping *forward* onto an item outside it opened whichever
			   item wore that number inside it, under an address saying otherwise. */
			show({ ref: asked.ref }, { slug, history: false });
		};

		window.addEventListener("popstate", arrive);

		return () => window.removeEventListener("popstate", arrive);
	}, [ready, error, workspace, project, agenda, everywhere, me, enter, load, nowOpen, nowShowing,
		readAgenda, show]);

	/*
		**Which workspace an action about the *open item* names** — `#1040`, Simon 2026-08-20.

		`App` holds two answers and they part company the moment an item is opened from
		somewhere that spans workspaces. `open.slug` is the one the item was actually read from;
		`workspace` is the switcher's, set on mount and by the switcher alone. Open a row from
		the agenda at `/` and the first moves while the second does not.

		**Every write read the second.** A status change on a `sandbox` item sent
		`PATCH /v1/tasks/20?workspace_id=projects`, and since a ref is unique *per workspace*
		the instance did as it was told and cancelled a different task — then the re-read
		afterwards followed it, so the reader was left in front of somebody else's item, which
		they had just changed. Six other controls had the same defect and nobody had met them.

		**Named once rather than spelled at each site**, because the sites are what went wrong:
		`show`'s own comment already called `open.slug` *"the only copy that is always right"*,
		and exactly one of the eight things that need it read it.
	*/
	const openIn = open ? open.slug || workspace : workspace;

	/*
		**What the open item is furnished with, which follows `openIn` and not the switcher** —
		`#1041`, the read half of the same defect.

		Its statuses, the projects it can be filed in, who it can be handed to and what its
		reader may do there are all properties of *its* workspace. They were all the switcher's,
		so an item opened from the agenda offered statuses it could not be moved to, projects it
		could not be filed in, and controls that would have been refused when pressed — which
		`allowedIn`'s own comment calls worse than not drawing them at all.

		**Nothing rather than the wrong thing while the answer is in flight.** `words` already
		chooses that for the switcher's workspace and says why; the same rule holds here, which
		is why a mismatched `elsewhere` reads as empty rather than falling back.
	*/
	const furnished = openIn === workspace
		? { vocabulary, projects: filable, members }
		: elsewhere && elsewhere.slug === openIn
			? elsewhere
			: { vocabulary: null, projects: [], members: [] };

	/* **The reader's standing in the *item's* workspace**, which is not the one the capture box
	   below is drawn from. A member who may write here and only read there was offered Edit,
	   Complete, the status control and the comment box on a foreign item, and every one of them
	   would have been refused when pressed — `#927`'s M-25 one surface along. */
	const allowedThere = allowedIn(me, openIn);
	const mayWriteThere = allowedThere.has("task:write");
	const mayCommentThere = allowedThere.has("comment:write");

	const reread = useCallback(async (row) => {
		/* Put the open item back the way `show` found it, so a detail on screen is not left
		   describing the state before the action.

		   **Read back where it was written** (`#1040`). This defaulted to the switcher's
		   workspace, so a write to an item outside it was followed by a read of whatever wore
		   that number *inside* it — which is what rewrote the address and put the reader in
		   front of the wrong item. The write and this are the same defect twice, not a cause
		   and a consequence: fixing one alone leaves the page still walking away. */
		if (open && open.item.ref === row.ref && open.item.kind === row.kind) {
			await show(row, { slug: openIn });
		}

		/* **Refresh what is showing** (`#652`). Completing from the agenda used to reload the
		   listing underneath it, so the row stayed on screen until the next poll — a write that
		   reports success and visibly does nothing. */
		await (agenda !== null
			? readAgenda(me ? me.workspaces : [], everywhere ? null : workspace, project)
			: load(workspace, project));
	}, [agenda, everywhere, load, me, open, openIn, project, readAgenda, show, workspace]);

	const wrote = useCallback(async (row, said, run) => {
		/*
			**One path for every write, and the whole of why it exists is the failure case.**

			A refusal here is ordinary rather than exceptional — a member without the
			permission, a name that is not in this workspace, an item somebody else has just
			changed — so it becomes a message beside the work rather than replacing the page.
			Blanking a screen somebody is reading, because a button did not take, loses their
			place to report a problem they can do nothing about.

			`setError` is kept for the other kind: not signed in, or the instance unreachable,
			where there is nothing on the page worth keeping.
		*/
		setBusy(true);

		try {
			const answer = await run();

			setNote(said(answer));
			await reread(row);

			return answer;
		} catch (failure) {
			setNote({ text: `#${row.ref} was not changed. ${failure.message}`, tone: "bad" });

			return null;
		} finally {
			setBusy(false);
		}
	}, [reread]);

	/* **`where` defaults to the switcher's workspace and the agenda overrides it** — a row
	   there can be from anywhere, and completing it against the wrong workspace is a 404 for an
	   item the reader is looking at. Remembered on the undo for the same reason. */
	const complete = useCallback((row, where = workspace) => wrote(
		row,
		() => ({
			text: `Completed #${row.ref} ${row.title}.`,
			tone: "good",
			/* What it was, so undo restores rather than guesses. `restoreRequest` is what
			   carries it back; this is where it is remembered. */
			undo: { ref: row.ref, kind: row.kind, title: row.title, status: row.status, where },
		}),
		() => sent(completeRequest(row, where)),
	), [workspace, wrote]);

	const undo = useCallback(async () => {
		const going = note && note.undo;

		if (!going) return;

		setNote(null);
		await wrote(
			going,
			() => ({ text: `#${going.ref} is back to ${going.status}.`, tone: "good" }),
			/* Put it back where it came from. `complete` recorded that, because by now the
			   switcher may hold a different workspace entirely. */
			() => sent(restoreRequest(going, going.where || workspace)),
		);
	}, [note, workspace, wrote]);

	/* **`inside` for `status`'s reason** (`#1040`): assigning somebody else's item to somebody
	   is a write nobody can see they made. */
	const assigning = useCallback((row, who, inside, appliesTo) => wrote(
		row,
		() => ({
			text: who ? `#${row.ref} is ${who}'s.` : `#${row.ref} is nobody's now.`,
			tone: "good",
		}),
		() => sent(assignRequest(row, who, inside, appliesTo)),
	), [wrote]);

	/*
		**One gesture, and it still asks when the item repeats** (decision `#1249` §1). Simon
		named the assignee among the fields with two answers, and *who does the stand-up this
		week* is a different sentence from *who does it from now on*. So unlike `status` next
		door — which has one answer and writes straight through — this one stops and asks.

		The question is put before the request rather than after a refusal, which matters:
		`#1259`'s rule is that a remedy has to belong to the surface it arrives on, and *send
		applies_to* is not something anybody can do from a select.
	*/
	const assign = useCallback((row, who, inside = workspace) => {
		if (!repeats(row)) return assigning(row, who, inside, null);

		setAsking({
			what: who ? `giving it to ${who}` : "taking it off everybody",
			run: (appliesTo) => assigning(row, who, inside, appliesTo),
		});

		return null;
	}, [assigning, workspace]);

	const add = useCallback(async (values, asDocument) => {
		/* **The reload afterwards keeps the filter the reader is looking at.** Without
		   `project` declared below, adding an item inside a project answered by replacing the
		   list with the whole workspace — the same stale closure as the poll, reached by a
		   button instead of a timer, and read as "adding a task loses my project".

		   `values` is every named control on the form, raw; `filed` decides what is worth
		   sending and is pure, which is where `#756`'s only real rule lives — an untouched
		   control gives an empty string, and this endpoint refuses those by name. */
		setBusy(true);

		try {
			/* **A document's title is the same box's text**, so it moves across rather than
			   the form growing a second naming control (`#761`). `written` takes `title`,
			   which is what that box is called on the other side. */
			const made = await sent(asDocument
				? documentRequest({ ...values, title: values.text }, null, workspace)
				: addRequest(values, workspace));

			setNote({ text: `Added #${made.ref} ${made.title}.`, tone: "good" });

			/* **Refresh what is on screen** (`#652`). Reloading the listing from the agenda
			   would report success over a page that does not change — and a new task with no
			   date belongs in *Unscheduled*, which is exactly where a reader would look for it
			   and not find it. */
			await (agenda !== null
				? readAgenda(me ? me.workspaces : [], everywhere ? null : workspace, project)
				: load(workspace, project));

			/* **Whether it landed, so the form knows whether to clear itself.** `wrote` has
			   answered this way all along — the answer or null — and this one swallowed the
			   refusal and returned nothing, which is what let the capture box empty on a
			   failure. */
			return true;
		} catch (failure) {
			setNote({ text: `That was not added. ${failure.message}`, tone: "bad" });

			return false;
		} finally {
			setBusy(false);
		}
	}, [agenda, everywhere, load, me, project, readAgenda, workspace]);

	const saving = useCallback(async (values, appliesTo) => {
		/*
			**A 409 is an ordinary answer here, not a failure** (§8.9, `#757`). Somebody else
			saved while this form was open; nothing was written, and the reader's typing is
			still in the DOM where they left it.

			So it is caught apart from every other refusal: `wrote` would report it as *"was not
			changed"* beside a closed form, which is true and useless. What a person needs is
			that the item moved, what it says now, and their own words still in front of them.
		*/
		if (!open) return;

		setBusy(true);
		setConflict(null);

		try {
			/* **The item's own workspace** — `#1040`. This is the widest of the seven: a save
			   carries the title, the description, the dates and the status, so against the
			   wrong item it overwrites all of them at once. */
			const saved = await sent(open.item.kind === "document"
				? documentRequest(values, open.item, openIn)
				: updateRequest(values, open.item, openIn, appliesTo));

			setNote({ text: `#${saved.ref} saved.`, tone: "good" });
			setEditing(false);
			await show(saved, { slug: openIn, history: false });
		} catch (failure) {
			/* **The current item travels on the 409**, attached by `concurrency.reporting()`
			   precisely so a client can say what changed rather than only that something did. */
			const theirs = conflictIn(failure);

			if (theirs) {
				setConflict(theirs);

				return;
			}

			setNote({ text: `#${open.item.ref} was not saved. ${failure.message}`, tone: "bad" });
		} finally {
			setBusy(false);
		}
	}, [open, openIn, show]);

	/*
		**The question goes in front of the save, and only for a repeating item** (decision
		`#1249`, `#1253`). It is not a confirmation: `edited` sends every control this form
		shows on every save — including the ones nobody touched — so on a series a save always
		writes a field with two answers, and there would otherwise be no way to save one here
		at all.

		A document never reaches it, because a document does not repeat and `repeats` reads the
		two fields only a task carries.
	*/
	const save = useCallback((values) => {
		if (!open || !repeats(open.item)) return saving(values, null);

		setAsking({ what: "this change", run: (appliesTo) => saving(values, appliesTo) });

		return null;
	}, [open, saving]);

	/*
		**The card in the air** (`#711`). A ref rather than the row, because the only thing a
		drop needs is which item it was — and a ref is what the transfer already carries, so the
		two halves cannot disagree about the subject.

		A ref rather than state, for the reason `since` and `held` are refs: `onDrop` runs from a
		listener the browser holds, and a render between the lift and the drop would leave the
		handler reading whichever card was in the air when it was created.
	*/
	/* **Something moved while the form was open** (`#792`). A ref rather than state because
	   nothing renders it: it is a note the poll leaves for the moment the editor closes. */
	const missed = useRef(false);

	const lifted = useRef(null);
	/* **Which column the pointer is over**, and this one is state rather than a ref because it
	   is *rendered*. The pair is the split this app makes everywhere: a ref for what a callback
	   reads, state for what a reader sees. */
	const [over, setOver] = useState(null);

	const dragged = useCallback((item) => {
		lifted.current = item;
	}, []);

	/*
		Remember that a column was opened or shut, and put it into force — `#1008`.

		**Both directions are recorded, and `false` is the load-bearing one.** A reader who opens
		a column the defaults would close has to have that survive the next render, or the
		default reasserts itself and the control appears to do nothing —
		:func:`collapsedColumns` only takes a default where the key is absent.

		Written to storage on the way through rather than in an effect, because the value being
		stored is the value being set: an effect watching the state would be a second place the
		same fact is decided, and it would fire on mount and write back what it had just read.
	*/
	const collapse = useCallback((key, shut) => {
		setColumnChoices((held) => rememberChoices(
			{ ...held, [key]: shut }, globalThis.localStorage, BOARD_COLUMNS_REMEMBERED
		));
	}, []);

	/*
		Reveal a truncated section, or fold it back — `#1820`.

		**One preference for the whole app rather than one per item**, which is the decision a
		later reader would reverse. A reader who reveals a milestone's links has said something
		about how they read this page, not about `#1387`; and on the 88% of items with five
		links or fewer the choice is inert, because there is nothing to hold back. Keying it by
		item would put an entry in storage for every milestone anybody ever opened, prune none
		of them, and go on claiming an answer for an item whose links have since been cut to
		three.
	*/
	const reveal = useCallback((name, open) => {
		setSectionChoices((held) => rememberChoices(
			{ ...held, [name]: open }, globalThis.localStorage, SECTIONS_REMEMBERED
		));
	}, []);

	/* **`inside` defaults to the switcher's workspace and an open item overrides it** —
	   `#1040`, and `complete` above carries the same argument for the same reason. A board card
	   is in the listing's workspace by construction; an open item need not be. */
	const status = useCallback((row, where, inside = workspace) => wrote(
		row,
		() => ({ text: `#${row.ref} is ${where.replace(/_/g, " ")}.`, tone: "good" }),
		() => sent(statusRequest(row, where, inside)),
	), [workspace, wrote]);

	const moved = useCallback((category) => {
		/*
			A card dropped on a column — `#711`.

			**A column is a category and the API takes a status**, so something has to choose
			which one: `statusFor` does, from the workspace's own vocabulary. Null means this
			workspace has no status in that category, and the drop is declined rather than sent —
			a 422 in answer to a gesture is worse than the gesture doing nothing.

			**A drop on the column it came from is not a write.** That is how a drag ends when
			somebody thinks better of it, and reporting *#42 is in progress* about a card nobody
			moved is a true-sounding falsehood, which is the shape this project keeps finding.

			**It goes through `status`, so it is the same write the select on an open item
			makes** (`#758`) — one path, one refusal, one re-read. A second one would be two
			answers to *what does moving an item mean*, and `#726`'s ruling — a status is not a
			claim — would then have to hold in two places.
		*/
		const item = lifted.current;

		lifted.current = null;
		setOver(null);

		if (!item || item.status_category === category) return;

		const chosen = statusFor(
			vocabulary && vocabulary.statuses,
			item.kind === "document" ? "document" : "task",
			category,
		);

		if (chosen.key === null) {
			setNote({ text: unmovable(chosen.because, category), tone: "bad" });

			return;
		}

		status(item, chosen.key);
	}, [status, vocabulary]);

	const readRepeat = useCallback(async (phrase) => {
		/*
			**The same refusal a save would give, arriving while there is still time to change
			it** (`#94`, §6.7). It is the same function on the server, so a phrase this accepts
			and the create refuses cannot exist — which is the whole reason to check first.

			**Not routed through `wrote`**, unlike every other call from this component: nothing
			is stored, nothing is re-read, and a *Repeat read* toast on every keystroke would be
			the noise that makes somebody stop reading toasts. A refusal is rendered inside the
			disclosure it belongs to, beside the box it is about.

			**The last phrase asked wins, and an answer overtaken while in flight is dropped.**
			Typing *every monda* and then *every monday* sends two, and the shorter one can land
			second — so an answer is shown only while the phrase it was for is still the newest
			one asked, which is cheaper and steadier than cancelling requests.

			**A ref rather than the state**, and getting that wrong is what the browser test
			caught: comparing against the answer already *held* asks whether this differs from
			the last one shown, which is true of every new phrase — so the first answer stuck
			and nothing after it was ever displayed. The question is *is this still what they
			are typing*, and only something written at ask-time can answer it.
		*/
		const asked = String(phrase || "").trim();

		latestRepeat.current = asked;

		if (asked === "") {
			setReading(null);

			return;
		}

		const zone = (workspace && workspace.timezone) || null;
		const current = () => latestRepeat.current === asked;

		try {
			const answer = await sent(readingRequest(asked, zone));

			if (current()) setReading({ ...answer, asked });
		} catch (why) {
			if (current()) {
				setReading({
					asked,
					problem: (why.body && why.body.detail)
						|| why.message
						|| "That is not a repeat this understands.",
				});
			}
		}
	}, [workspace]);

	const comment = useCallback(async (body) => {
		/* **The item is re-read afterwards rather than the comment appended locally**, because
		   what comes back is what the instance stored — a `#42` in it becomes a mention, and a
		   thread assembled on this side would drift from the one everybody else sees. `wrote`
		   does the re-read, which is why this goes through it like every other write. */
		if (!open) return false;

		return Boolean(await wrote(
			open.item,
			() => ({ text: `Noted on #${open.item.ref}.`, tone: "good" }),
			/* **The item's own workspace, not the switcher's** — `#1040`. Prose written onto
			   the wrong item is the least recoverable of the seven: nothing about it looks
			   like an accident afterwards. */
			() => sent(commentRequest(open.item, body, openIn)),
		));
	}, [open, openIn, wrote]);

	const link = useCallback(async (target, linkType) => {
		/*
			**Which kind the ref names is resolved rather than asked** (`#760`). One counter
			serves tasks and documents (§6.2), so `#4` is a document here and `#42` is a task —
			and `subroutine show 4` does not ask which, so neither does this. Each kind is tried
			in turn and a 404 moves on, which is exactly what `fetched` does to open one.

			**Only a 404 moves on.** A refusal for any other reason — no permission, a link that
			would make a cycle — is the answer, and swallowing it to try the next kind would
			report *there is no #42* about an item that is right there.
		*/
		if (!open) return false;

		setBusy(true);

		try {
			/* The item's own workspace decides what may be linked to what — `#1041`. */
			const kinds = linkableTypes(furnished.vocabulary);
			let made = null;

			for (const kind of kinds) {
				try {
					made = await sent(
						/* The item's own workspace — `#1040`. Both ends of a link are resolved
						   in it, so the switcher's would name a different pair entirely. */
						linkRequest(open.item, target, linkType, kind, openIn),
					);
					break;
				} catch (failure) {
					if (failure.status !== 404 || kind === kinds[kinds.length - 1]) throw failure;
				}
			}

			setNote({ text: `#${open.item.ref} ${made.label.toLowerCase()} `
				+ `#${made.other.ref}.`, tone: "good" });
			await show(open.item, { slug: openIn, history: false });

			return true;
		} catch (failure) {
			setNote({ text: `That link was not made. ${failure.message}`, tone: "bad" });

			return false;
		} finally {
			setBusy(false);
		}
	}, [furnished, open, openIn, show]);

	const unlink = useCallback((going) => wrote(
		open ? open.item : { ref: 0 },
		() => ({ text: `#${open.item.ref} no longer ${going.label.toLowerCase()} `
			+ `#${going.other.ref}.`, tone: "good" }),
		() => sent(unlinkRequest(open.item, going.id, openIn)),
	), [open, openIn, wrote]);

	const showMore = useCallback(async () => {
		/* The next page of each collection that has one, appended. `load` takes the cursors
		   rather than the page number, because keyset pagination is what the API offers and
		   what makes a page boundary stable while somebody is adding things.

		   **`project` is declared even though `more` already changes on every load**, which
		   in practice rebuilt this callback often enough to hide the omission. Correctness
		   that depends on a *different* value happening to change is not correctness. */
		setBusy(true);

		try {
			/*
				**A grouped answer widens rather than appends** (`#1790`). Its columns do not
				share a sequence, so there is no cursor that means *the next page of this board*
				— and the server refuses one sent beside a grouping for that reason. Asking again
				with a larger allowance is what *more of this* means here, and it is a fresh
				answer rather than an appended one, so `accumulated` replaces instead of merging.
			*/
			if (cut) {
				const wider = WIDER.find((size) => size > columnSize.current) || null;

				if (wider === null) return;

				columnSize.current = wider;

				await load(workspace, project, null, wider);

				return;
			}

			await load(workspace, project, more);
		} catch (failure) {
			setNote({ text: `There was more, but it did not arrive. ${failure.message}`,
				tone: "bad" });
		} finally {
			setBusy(false);
		}
	}, [cut, load, more, project, workspace]);

	const widen = useCallback(async () => {
		/*
			**Out of a project, back to the workspace.** A narrowed list that cannot say what
			narrowed it, or undo it, is an empty backlog with an explanation nobody can reach —
			and the filter arrived in the address rather than from a control the reader touched,
			so there is nothing for them to un-touch.
		*/
		setProject(null);
		go(`/${encodeURIComponent(workspace)}`);

		try {
			await load(workspace, null);
		} catch (failure) {
			/* A note, not the failure page: there is a readable list on screen and losing it
			   because a re-fetch did not land would cost the reader their place. The guard in
			   `tests/test_web.py` counts the places that blank the page, and it caught this
			   one being written the other way. */
			setNote({ text: `The rest did not load. ${failure.message}`, tone: "bad" });
		}
	}, [go, load, workspace]);

	const prioritise = useCallback(async (chosen) => {
		/*
			**Raise one project's work here, or stop** — `#986`, decision `#982`.

			**Written on the workspace and read back from the identity**, which is why this
			refetches it: `me.workspaces[].prioritised_project` is what every surface on this
			page reads — the header sentence, the masthead's mark, the form's dropdown — so a
			write that did not refresh it would change the ordering and leave three labels saying
			the old thing. That is `#640`'s shape and it has shipped from here four times.

			**And the listing is reloaded**, because the whole point is that the rows move.
			Neither step is optional: doing the first alone reorders nothing a reader can see,
			and doing the second alone reorders the rows under labels that disagree with them.

			A refusal is a note beside the work rather than the failure page, which is `wrote`'s
			argument — this is a preference, and losing a readable list over one is a poor trade.
		*/
		setBusy(true);

		try {
			await sent(prioritiseRequest(chosen, workspace));

			const identity = await sent(identityRequest());

			setMe(identity);
			await load(workspace, project);
			setNote({
				text: chosen
					? `${chosen} is prioritised here.`
					: "Nothing is prioritised here now.",
				tone: "good",
			});
		} catch (failure) {
			setNote({ text: `That did not change. ${failure.message}`, tone: "bad" });
		} finally {
			setBusy(false);
		}
	}, [load, project, workspace]);

	const home = useCallback(async () => {
		/*
			**The masthead goes home, and the page goes with it** — `#962`, Simon 2026-08-17.

			`go` writes the address bar and nothing else, so this was `go("/")` alone: the
			address said `/` and the reader went on looking at whatever board they were on,
			narrowed to whatever project they were in. No `popstate` fires for a `pushState` we
			made ourselves, which is the fact all three of these callbacks exist for — `widen`,
			`narrow` and this — and the one an inline `go` keeps being written without.

			**The same three steps `popstate` takes for `/`**, deliberately spelled the same way:
			clear the project and read the agenda. One address must not mean two things depending
			on whether the reader arrived by clicking or by stepping back, which is what `#652`
			settled and `#645`'s split makes a real risk.

			**And the arrangement is dropped rather than carried.** `go` carries it (`#651`) and
			is right nearly everywhere; `/` is the exception, because an agenda has no board and
			no completed filter — so `/?view=board&include_completed=true` is a selection that
			means nothing where it lands. `addressOf` already knows this and returns `"/"` for
			the agenda; `withShowing` does not, because `parseAddress("/")` is null.
		*/
		/*
			**One expression for what is written and what is held** (`#967`). `go` decides the
			address and `nowShowing` decides the state, which are two jobs — and passing a blank
			arrangement to the first alone reads exactly like doing both. It shipped that way:
			the address said `/` with no view and `showing.view` was still `"board"`, so
			`#963`'s frame gave the agenda the board's width until the page was refreshed.

			`nowShowing`'s own docstring is the rule this broke — *one writer, so the two cannot
			disagree* — met from the side where one of them was simply not called.
		*/
		const plainly = { view: null, selection: {} };

		nowOpen(null);
		setProject(null);
		setNote(null);
		/* **Back to the merged agenda, which is what `/` means** (`#1215`). Widening is leaving
		   every place named, so the flag `listingAddress` reads has to move with it — otherwise
		   the next address this page writes would still carry the workspace the reader just
		   stepped out of. */
		setEverywhere(true);
		nowShowing(plainly);
		go("/", { arranged: plainly });

		await readAgenda(me ? me.workspaces : []);
	}, [go, me, nowOpen, nowShowing, readAgenda]);

	const narrow = useCallback(async (address) => {
		/*
			**Into a project, from a label on a row** — `#959`, decision `#957` §4.

			`widen` above is this in the other direction and was the whole of it: a narrowed
			list could be left and never entered, so the only way into a project was to type its
			address. A label that says where a row lives is the obvious control for going there,
			and it is the same three steps.

			**Three steps, and pushing the address is only one of them.** `go` writes the bar
			and nothing else — no `popstate` fires for a `pushState` we made ourselves — so a
			handler that stopped there would move the address and leave the page exactly as it
			was. That is what this shipped as while it was being written, and it looks like a
			link that does nothing.

			**Read back through `parseAddress` rather than passed as a project**, because the
			control is an anchor and its `href` is the fact. Deriving the project from it here
			means the thing the reader can copy and the thing this loads are one string.
		*/
		const place = parseAddress(address);
		const wanted = (place && place.project) || null;

		/* **The workspace the address names, which need not be the one the switcher holds** —
		   `#1042`. A project chip on an item opened from the agenda points into *that* item's
		   workspace, and this loaded the switcher's rows underneath it: one workspace's backlog
		   under an address naming another. The masthead reaches here too, since `goTo` sends
		   anything naming a project to this. */
		const where = (place && place.workspace) || workspace;

		setProject(wanted);
		setEverywhere(false);
		go(address);

		try {
			enter(where);

			/*
				**Whatever arrangement the reader is in follows them into the project**
				(`#1215`, `#745`). This forced a listing, which was right while the agenda
				existed only at the root and is wrong now: somebody reading their agenda and
				clicking a project chip is asking *what is on for that project*, and answering
				with a backlog changes the question rather than the scope.

				`showing.view` rather than the address, because `go` above writes a bare
				project address — the arrangement is carried in state here and written by the
				next control that touches it.
			*/
			if (showing.view === AGENDA_VIEW) {
				await readAgenda(me ? me.workspaces : [], where, wanted);

				return;
			}

			setAgenda(null);
			await load(where, wanted);
		} catch (failure) {
			/* A note rather than the failure page, for `widen`'s reason: there is a readable
			   list on screen and losing it because a re-fetch did not land costs the reader
			   their place. */
			setNote({ text: `The rest did not load. ${failure.message}`, tone: "bad" });
		}
	}, [enter, go, load, me, readAgenda, showing, workspace]);


	const chooseWorkspace = useCallback(async (slug) => {
		/* A workspace is the whole of it: a project from the one you were in does not exist
		   here, and carrying it over would narrow to nothing and look like an empty backlog. */
		enter(slug);
		setProject(null);
		setNote(null);
		nowOpen(null);
		/* **Choosing a workspace is naming a place**, which is what stops the next address this
		   page writes from being the merged root. Set here rather than left to the effect: no
		   `popstate` fires for a `pushState` we made ourselves. */
		setEverywhere(false);
		go(`/${encodeURIComponent(slug)}`);

		try {
			/*
				**The arrangement follows the reader here too** (`#1215`), for `narrow`'s reason
				one level up: somebody reading their agenda who picks a workspace is asking what
				is on there, and this used to answer with a backlog.

				**It used to *have* to.** The comment this replaces said choosing a workspace is
				leaving the agenda "because `/` is the only address the agenda has" — true when
				it was written, and the sentence `#649`'s amendment retires.
			*/
			if (showing.view === AGENDA_VIEW) {
				await readAgenda(me ? me.workspaces : [], slug, null);

				return;
			}

			setAgenda(null);
			await load(slug, null);
		} catch (failure) {
			setError(failure);
		}
	}, [enter, go, load, me, nowOpen, readAgenda, showing]);

	const goTo = useCallback(async (address) => {
		/*
			**Where the masthead sends you** — `#975`. One control, two destinations, and which one
			is a property of the address rather than of the option.

			`placesToGo` emits an address per entry precisely so that this has nothing to decide:
			`parseAddress` says whether a project was named, `narrow` goes into one and
			`chooseWorkspace` changes the whole workspace. Both already do all three steps that
			moving in this app takes — `go` writes the address bar and nothing else (`#962`), so a
			handler that stopped there would leave the page as it was.

			**A workspace is not narrowed into**, which is why this is a branch rather than one
			call: `narrow` keeps the workspace and reloads a listing, and arriving from another
			workspace needs the vocabulary, the roster and the project list asked again.
		*/
		const place = parseAddress(address);

		if (place === null) return home();

		return place.project
			? narrow(address)
			: chooseWorkspace(place.workspace);
	}, [chooseWorkspace, home, narrow]);

	const chooseSearch = useCallback(async (text) => {
		/*
			**A search is a selection, so it goes in the address** — decision `#649`, and it is
			what makes a search something a reader can send to somebody.

			**Its refusal is a note, not the failure page.** `domain/search` caps a query at
			`MAX_TERMS` words and says so by name, with a hint explaining that every word has
			to appear so a longer search finds *less*. That arrives here as a 422 from `load`,
			which every other caller lets through to `setError` — and blanking somebody's screen
			because they typed too many words is the failure `viewOf` already declined for a
			mistyped arrangement, arriving by a third door.
		*/
		const asked = text.trim();

		/*
			**A query that is nothing but a ref opens that item** — `#976`, Simon's.

			`#867` made a ref *findable* and `#823` put the exact hit first, so `#916` already
			returns the item — beside every row whose prose holds those digits, measured at four to
			sixty of them per ref when `#867` was built, because `7` appears inside `17` and inside
			every `2026-08-07`. This is the last step: a results page with one useful row on it is a
			page the reader still has to read.

			**Tried before the address is written**, so a ref that resolves never puts a search in
			the bar, and one that does not is an ordinary search with nothing to undo. `show` does
			all three steps that moving in this app takes — `go` writes the address bar and nothing
			else (`#962`), three times over — and it reports whether it opened anything, which is
			what makes the fall-through possible rather than guessed at.

			**Quiet, because *there is no #916 here* would be contradicted a moment later** by the
			search results this then runs.

			The workspace is this one and there is nothing to decide: refs are allocated per
			workspace (§6.2), and this control is rendered only off the agenda, so a search is
			always inside exactly one.
		*/
		const jumping = refAsked(asked);

		if (jumping !== null && await show({ ref: jumping, kind: null }, { quiet: true })) return;

		/*
			**A search is a list of results, so searching leaves the agenda** (`#1215`).

			The agenda is a day, and a day is not a set of rows to narrow — which is the reason
			this control used to be hidden there at all. Now that a place opens on an agenda by
			default, hiding it would mean a reader at `/projects` has no way to search from the
			page they land on; the honest answer is that the control stays and the arrangement
			moves, because *results* are a list.

			**Written here rather than left to `showingOf` to work out.** That function falls a
			selection back to the list, which is what saves a hand-typed address — but a control
			must write what it chose (`#745`), not produce an address that something downstream
			quietly corrects.
		*/
		const wanted = {
			view: asked === "" ? showing.view : "list",
			selection: asked === ""
				? { ...showing.selection, q: undefined }
				: { ...showing.selection, q: asked },
		};

		if (!reloads(showing, wanted) && wanted.view === showing.view) return;

		nowShowing(wanted);
		/* **Searching leaves whatever item was open** (`#786`). The address this writes is the
		   listing's, so keeping the item on screen would be the page and the bar disagreeing —
		   which is what `close` was fixed for, arriving from a third door now that the control
		   is reachable over an item at all. */
		nowOpen(null);
		go(listingAddress({ agenda: everywhere, workspace, project }), { arranged: wanted });

		/* **Leaving the agenda if that is where the search started.** It returned here instead,
		   which was right while the agenda had no search box; now the box is on every page that
		   names a place, and a search that wrote an address and left the buckets on screen would
		   be the page and the bar disagreeing. */
		if (wanted.view === AGENDA_VIEW) return;

		setAgenda(null);

		try {
			await load(workspace, project);
		} catch (failure) {
			setNote({ text: `That search was refused. ${failure.message}`, tone: "bad" });
		}
	}, [agenda, go, load, nowOpen, nowShowing, project, show, showing, workspace]);

	const chooseOrder = useCallback(async (asked) => {
		/*
			**An order is how the rows look, so it goes in the address** — decision `#649`, and
			the same path a search and a filter already take.

			**Written out even when it is the default**, which is `#745`'s narrowing: what you
			send somebody has to be what you were looking at, and an address omitting the order
			hands its reader *their* default rather than the sender's page.

			`reloads` is what stops a re-render when the reader picks what is already chosen —
			the same guard `chooseSearch` uses, and the reason `withShowing` emits in
			`SELECTABLE` order: one screen must produce one string, or a cursor taken on one
			page is compared against a path spelled differently on the next.
		*/
		const wanted = {
			view: showing.view,
			selection: { ...showing.selection, order: asked },
		};

		if (!reloads(showing, wanted)) return;

		nowShowing(wanted);
		go(listingAddress({ agenda: everywhere, workspace, project }), { arranged: wanted });

		try {
			await load(workspace, project);
		} catch (failure) {
			setNote({ text: `That order was refused. ${failure.message}`, tone: "bad" });
		}
	}, [agenda, go, load, nowShowing, project, showing, workspace]);

	const chooseView = useCallback(async (wanted) => {
		/*
			**Switching refetches, and the comment here used to say it must not.**

			That sentence was written the same day and was wrong within hours: it argued that a
			view is a rendering of rows `load` already has, so refetching would make the query
			decide which rows exist. The consequence, which Simon found by opening the page, is
			that the *Done* column was structurally incapable of holding anything — a listing
			excludes finished work by default, so the board never received a single done row
			(`#718`).

			**What refetches is the selection, and only when it changes** (`#738`). An
			arrangement genuinely is a rendering of rows already held — that half of the original
			sentence was right — so switching list to board with the same selection reloads
			nothing it did not need. The two are separate parameters now, which is what makes
			that distinction expressible at all.

			`wanted` is passed to `load` explicitly because `setShowing` has not landed in this
			render, so the closure still holds the previous one — the same reason `start` passes
			`slug` rather than reading `workspace`.
		*/
		const again = reloads(showing, wanted);

		nowShowing(wanted);

		/* As `chooseSearch`: this writes a listing address, so it leaves the item (`#786`). */
		nowOpen(null);

		/* **The address first, then the reload** — and the order is the whole of why `load`
		   needs no argument for this: it reads the arrangement from the address, which `go` has
		   already written. */
		go(
			listingAddress({ agenda: everywhere, workspace, project }),
			{ arranged: wanted },
		);

		/*
			**Entering and leaving the agenda is this control's job now** (`#1215`).

			It was neither: the agenda was the thing at the root, so switching arrangements
			could only ever move between list and board and `agenda === null` was a fact about
			*where you were* rather than about what you had asked for. Now it is a third
			arrangement of the same place, and picking it has to fetch from the other endpoint
			— which is exactly the amendment `#649` took: an arrangement may draw its rows from
			a different endpoint.

			**The scope is what the address already says**, so switching arrangement never
			changes which place is showing. That is `#649`'s untouched half: the path decides
			place, and this only decides how it is drawn.
		*/
		if (wanted.view === AGENDA_VIEW) {
			await readAgenda(
				me ? me.workspaces : [], everywhere ? null : workspace, project,
			);

			return;
		}

		/* **Leaving the agenda always reloads, whatever `reloads` says about the selection.**
		   The two arrangements read different endpoints, so there are no rows in hand to
		   rearrange — a version that trusted `again` here would leave the agenda's buckets on
		   screen under an address saying `?view=list`. */
		const leaving = agenda !== null;

		if (leaving) setAgenda(null);

		if (leaving || again) await load(workspace, project);
	}, [agenda, everywhere, go, load, me, nowOpen, nowShowing, project, readAgenda, showing,
		workspace]);

	if (!ready) return html`<div class="app"><div class="empty">Reading…</div></div>`;

	/* The address of the listing behind whatever is showing — what *All items* goes back to, and
	   what the view switcher hangs its arrangements off. One expression, because `close` and
	   `chooseView` already agree on it and a second spelling here would be the thing that drifts. */
	const behind = listingAddress({ agenda: everywhere, workspace, project });

	/*
		**The one question the render asks of the selection**, named once.

		Everything below that used to ask `view === "done"` is really asking this: *is this page
		showing only work that is over?* Answering it from the arrangement is what made `done` a
		view in the first place, and it is what `#738` took out.
	*/
	const finishedOnly = showing.selection.status_category === "done";

	/*
		Everything the add form needs beyond what every caller of `Adding` already passes — one
		prop rather than six threaded through `Agenda`, `Board` and `Listing`, none of which has
		any business knowing what a dropdown is made of.

		**`project` is the address's**, which `#738` already settled: `/{workspace}/{project}`
		says where rows come from, so it says where a new one goes. Nothing new is parsed, and on
		the agenda — which spans workspaces and narrows to nothing — it is null and the Inbox is
		what the form offers.
	*/
	const adding = {
		expanded, onExpand: setExpanded, vocabulary, projects: filable, members, project,
		writing, onWriting: setWriting,
		/* **In the bundle for the reason beside it** (`#912`): a form's project dropdown marks
		   the prioritised project (`#986`) and neither `Agenda` nor `Listing` has any business
		   knowing that. One address, from the workspace the page is in. */
		prioritised: prioritisedHere(me ? me.workspaces : [], workspace)[0] || null,
		/* **In the bundle rather than threaded** (`#912`'s argument, one item along): `Agenda`,
		   `Board` and `Listing` each pass this through and none of them has any business
		   knowing what a repeat preview is. */
		reading, onReading: readRepeat,
		/* **In the bundle for the same reason** (`#776`): the capture form has two prose boxes
		   and `Agenda`, `Board` and `Listing` each pass this through knowing nothing about
		   what a preview is. One answer for the page, so opening a second box closes the
		   first — two previews at once is a state nobody asked for. */
		previewing, onPreviewing: setPreviewing, where: mentionHref(workspace),
	};

	if (error) {
		return html`
			<div class="app">
				<${Failed} error=${error} onRetry=${start} />
			</div>
		`;
	}

	return html`
		${/*
			**The board is the one view that wants the screen** (`#846`). A list wants a
			readable line length, which is what the 1100px cap is for; a board wants as many
			columns visible as will fit, and on a wide display the cap was hiding three of
			seven behind a scrollbar at the bottom of a three-thousand-pixel page.

			One class rather than two containers, because everything else about the frame —
			the header, the capture box, the footer — is the same in both and duplicating it
			is how two layouts come to disagree.
		*/ null}
		<div class=${frame(showing, open)}>
			<header class="top">
				${/*
					**The masthead goes home** (`#868`), which is the convention every reader
					already has — and `/` is the right destination by decision `#649` rather
					than by convention alone: it is the agenda across every workspace, because
					a bare `subroutine` prints the agenda and one product answers one question
					the same way on both surfaces.

					**A real anchor through `followed`**, never a click handler. That is what
					makes *open in a new tab*, *copy link address* and middle-click work, and
					what makes a screen reader announce a link — the rule `opens` states and
					every other navigation here already obeys.
				*/ null}
				<${Wordmark} version=${me ? me.instance_version : null}
					onHome=${(event) => followed(event, home)} />
				<div class="who">
					${me && html`<strong>${me.user.username}</strong>`}
					${me && html`
						${" · "}
						${/* **Named, because there is nowhere to put a visible label** (`#927`'s
						     L-7). Every other select in this app sits inside a `<label>` carrying
						     a `<span>`; this one is a chip in the masthead between a username and
						     a sign-out link, and a word in front of it would cost more than it
						     explains. `aria-label` is what the link-type select already does for
						     the same reason. Without it a screen reader announces a combo box
						     with no name at all — the control that decides which backlog you are
						     looking at. */ null}
						${/* **It says what is showing, and every choice on it is reachable**
						     (`#969`, Simon's). It marked a workspace selected while the agenda
						     was showing *every* workspace — untrue, and a dead end with it: a
						     `<select>` fires no `change` for the option already selected, so the
						     one workspace the control claimed was the one it could not reach.

						     **`All workspaces` is a real option rather than a hint**, and that
						     is the part worth keeping. A disabled placeholder fixes the claim
						     and leaves the control one-way — having narrowed, the way back to
						     everything is a link elsewhere on the page — which is the same
						     inert shape one step along. This is descriptive rather than an
						     instruction: it says what you are looking at, which is what `/` is,
						     and choosing it goes there.

						     **Shown however many workspaces there are** (`#975`, Simon's). One
						     workspace used to render the name as inert text, so the only thing
						     naming it could not be used to reach it — and on the agenda, `/` is
						     the only address a reader has. Both options stay meaningful with one
						     workspace, because `/` and `/{workspace}` are different pages: the
						     agenda buckets by date and a listing does not.

						     **What it offers is `placesToGo`**, which is pure and Node-tested —
						     `#640`'s cheapest route, and the reason this markup holds no rule. */ null}
						<select aria-label="Where to look"
							onChange=${(event) => (event.target.value
								? goTo(event.target.value)
								: home())}>
							${placesToGo(me.workspaces, filable,
								{ workspace, project, agenda: everywhere }).map((one) => html`
								<option key=${one.value} value=${one.value} selected=${one.chosen}>
									${"\u00a0\u00a0".repeat(one.depth) + one.label}
								</option>
							`)}
						</select>
					`}
					${me && html`
						${" · "}
						<button class="link inline" onClick=${signOut}>Sign out</button>
					`}
				</div>

				${/*
					**A search is a control for the same reason the chips are** (`#651`): an
					address is not a way to find something, and a reader who has never seen one
					cannot type a word they have not been told.

					**On a listing, and over an item opened from one** (`#786`). The condition
					used to carry `!open` as well, and `.top` is `justify-content: space-between`
					— so opening an item took two of the header's four children away and the box
					pushed the workspace switcher hard right. Simon found it by comparing two
					addresses. Nothing moved; two things vanished.

					**The agenda half stays and has a reason the other half never had**: a day is
					not a set of rows to narrow, and arranging it by status would answer a
					question nobody asked of it.
				*/ null}
				${/* **On every page that names a place, including an agenda** (`#1215`). The
				     reason below for hiding it — a day is not a set of rows to narrow — is still
				     true of the agenda itself and is no longer a reason to withhold the control:
				     since a place opens on an agenda by default, hiding it here would mean a
				     reader has no way to search from the page they land on. `chooseSearch`
				     answers it by moving the arrangement, because results are a list.

				     **Still nothing at `/`.** The merged agenda spans every workspace and
				     `GET /v1/tasks` refuses an ambiguous one (§8.2), so there is nothing for a
				     search to be a search *of*. */ null}
				${!everywhere && html`
					<${Seeking} busy=${busy} onSearch=${chooseSearch}
						asked=${showing.selection.q || ""} />
				`}

				${/*
					**Controls, because an address is not a way to find something** (`#651`). A
					reader who has never seen one cannot type a word they have not been told. They
					are on a listing only: the agenda is chosen by the path, and arranging it by
					status would answer a question nobody asked of it.

					**What each one writes, and which is highlighted, is `chips`** — a pure
					function, which is `#640`'s cheapest route and the reason this arc's four
					shipped faults were all in wiring rather than in rules. Two of the three are
					arrangements and one is a selection (`#738`); they look alike because the
					taxonomy belongs in the address, not in the furniture.

					**And each is a real link** (`#722`), so a reader can open the board in a tab
					beside their list rather than replacing it. `chips` builds exactly the address
					`chooseView` is about to write, which is what makes the two agree.

					**Shown over an open item too** (`#786`), and `behind` is already the address
					of the listing underneath — so a chip on an item page is the way back to that
					listing, arranged as the reader asked.
				*/ null}
				${/* **Shown wherever a place is named, which since `#1215` includes an agenda**
				     (`#649`'s amendment). The test was `agenda === null`, and it was right while
				     the agenda existed only at the root: there was nothing to arrange and no
				     listing to switch to. Now a project's agenda is the *default*, so that test
				     would have hidden the switcher on the page most readers land on and left
				     them no way to reach the list or the board at all.

				     **Still nothing at `/`.** The merged agenda spans every workspace and
				     `GET /v1/tasks` refuses an ambiguous one (§8.2), so there is no listing
				     behind it to offer — `#649` reserves `/?view=list` for a backlog nothing
				     implements. A control that led somewhere the app cannot go is worse than
				     no control. */ null}
				${!everywhere && html`
					<nav class="views" aria-label="Which view">
						${chips(behind, showing).map((chip) => html`
							<a key=${chip.name} class=${chip.chosen ? "chosen" : ""}
								href=${chip.href}
								aria-current=${chip.chosen ? "true" : undefined}
								onClick=${(event) => followed(event, () => chooseView(chip.showing))}
								>${chip.name}</a>
						`)}
					</nav>
				`}
			</header>

			${released && html`
				${/* **Never reloads by itself** (`#785`). Somebody may be halfway through an
				     edit form, and `#757` went to some trouble to make sure their typing
				     survives a conflict; throwing it away for a version bump is the same loss
				     from a friendlier direction. */ null}
				<${Note}
					note=${{
						text: "A new version of this page is available.",
						tone: "good",
						act: { label: "Reload", go: () => window.location.reload() },
					}}
					onDismiss=${() => setReleased(false)} />
			`}

			<${Note} note=${note} onUndo=${undo} onDismiss=${() => setNote(null)} />

			${asking && html`
				<${Asking} what=${asking.what} busy=${busy}
					onAnswer=${(appliesTo) => {
						const run = asking.run;

						setAsking(null);
						run(appliesTo);
					}}
					onCancel=${() => setAsking(null)} />
			`}

			${open
				? html`<${Detail} ...${open} members=${furnished.members} onOpen=${show} busy=${busy}
					editing=${editing} conflict=${conflict} onSave=${mayWriteThere ? save : null}
					reading=${reading} onReading=${readRepeat}
					previewing=${previewing} onPreviewing=${setPreviewing}
					${/* **Bound to the item's own workspace, the way the agenda binds its rows**
					     (`#1040`). These three take a row and default to the switcher's, which is
					     right for a listing — one listing is one workspace — and wrong here,
					     because an item opened from the agenda may be in another one. `Detail`
					     cannot pass it: the component is handed an item and knows nothing about
					     where it was read from, and `openIn` is the only place that does. */ null}
					onStatus=${mayWriteThere ? (row, where) => status(row, where, openIn) : null}
					statuses=${furnished.vocabulary && furnished.vocabulary.statuses}
					onComment=${mayCommentThere ? comment : null}
					onLink=${mayWriteThere ? link : null} onUnlink=${mayWriteThere ? unlink : null}
					vocabulary=${furnished.vocabulary} projects=${furnished.projects}
					onEdit=${mayWriteThere ? (wanted) => { setEditing(wanted); setConflict(null); } : null}
					${/* **The item's own workspace** — `#1040`. `mentionHref`'s own docstring says
					     a mention "resolves within the workspace it was written in, which is what
					     a ref means", and it was handed the switcher's — so a `#42` in the prose
					     of an item opened from the agenda linked to whatever wore that number
					     somewhere else. A read rather than a write, so it costs a wrong page
					     rather than a wrong change. */ null}
					where=${mentionHref(openIn)} onBack=${() => close()}
					backTo=${withShowing(behind, showing)} workspace=${openIn}
					${/* **The item's own workspace here too, and the listing's narrowing only
					     where they are the same place** (`#1042`). `Detail` uses these to address
					     the *linked* items and to decide what their labels may leave out — and
					     both ends of a link are in the item's workspace, never the switcher's.
					     So a blocker on an item opened from the agenda was addressed
					     `/projects/…`, and following it opened whatever wore that number there. */ null}
					project=${openIn === workspace ? project : null} onGo=${narrow}
					${/* **`open.slug`, which is the item's own workspace rather than the
					     switcher's** — an item opened from the agenda may be in another one, and
					     marking its project from the wrong workspace's focus would be a
					     confident wrong answer. Its own comment at `nowOpen` says it is the only
					     copy that is always right; `open.item.workspace` is not a field, and
					     reading it would have fallen back to the switcher in silence. */ null}
					prioritised=${prioritisedHere(me ? me.workspaces : [], open.slug || workspace)}
					onComplete=${mayWriteThere ? (row) => complete(row, openIn) : null}
					${/* **A reader who cannot write may still reveal a section** (`#1820`). This
					     is a fact about how the page is read rather than about the item, so it is
					     passed unconditionally — gating it on `mayWriteThere` would leave a
					     viewer looking at five of twenty-six links with no way to see the rest,
					     which is `#1781`'s complaint with the halves swapped. */ null}
					revealed=${sectionChoices} onReveal=${reveal}
					onAssign=${mayWriteThere ? (row, who) => assign(row, who, openIn) : null} />`
				: agenda !== null
					? html`<${Agenda} buckets=${agenda} more=${unscheduled} heldUp=${heldUp}
						later=${later}
						deferred=${deferred} paused=${paused} gone=${gone} theirs=${theirs}
						onAdd=${mayWrite ? add : null} busy=${busy} workspace=${workspace} adding=${adding}
						onGo=${narrow}
						${/* **What the address already said** (`#957` §4, `#1215`). The merged
						     agenda at `/` names no place, so its rows carry their whole address;
						     a scoped one strips what the reader can already see above the list.
						     Hardcoded to nowhere until a place had an agenda, which put
						     `projects/subroutine` on every row of `/projects/subroutine`. */ null}
						place=${{ workspace: everywhere ? null : workspace, project }}
						${/* **Every workspace's when nothing is named, and one workspace's when
						     something is** — each workspace may prioritise a project of its own
						     (§13.7), so a scoped agenda must not announce another's. */ null}
						prioritised=${prioritisedHere(
							me ? me.workspaces : [], everywhere ? undefined : workspace,
						)}
						${/* **Each row is opened in its own workspace, not in the one the
						     switcher holds.** The agenda spans them; `show` defaults its slug
						     to `workspace`, so a row from `sandbox` would be looked up in
						     `projects` and reported missing. `#640`'s exact shape — the rule
						     right, the display right, and no wire between them — which is why
						     `agendaBuckets` resolves the slug onto every row. */ null}
						onOpen=${(row) => show(row, { slug: row.workspace || workspace })}
						onComplete=${mayWrite
							? (row) => complete(row, row.workspace || workspace)
							: null} />`
					: showing.view === "board"
						? html`<${Board} items=${items} onOpen=${show} onComplete=${mayWrite ? complete : null}
							cut=${cut}
							onAdd=${finishedOnly || !mayWrite ? null : add} busy=${busy} more=${more} adding=${adding}
							onMore=${showMore} onGo=${narrow}
							project=${project} workspace=${workspace} onWiden=${widen}
							${/* The board's narrowed bar is the listing's, so it carries the same
							     control — one component, one answer (`#986`). */ null}
							prioritised=${prioritisedHere(me ? me.workspaces : [], workspace)}
							onPrioritise=${mayWrite ? prioritise : null}
							selection=${showing.selection}
							${/*
							     **Gated like every other control on this call** — `#1781`, and
							     it is `#927`'s M-25 one control along. The board's drop is
							     `statusRequest`'s PATCH, the same write the Complete button
							     makes, and that button has been gated since M-25 shipped; this
							     was not, because the sweep went through the *visible* controls
							     and a drag has no button to leave undrawn.

							     **Both halves, for different reasons.** Without `onMove` a card
							     still lifts and the drop is silently inert, which is a gesture
							     that reads as working and does nothing. Without `onDrag` the
							     columns still light as targets for a card nobody can pick up.
							     `Row` draws `draggable` only when it has `onDrag`, and `Board`
							     attaches its drop handlers only when it has `onMove`, so
							     withholding both is what removes the affordance rather than
							     hiding it.

							     `over` stays as it is: it is state rather than a handler, and
							     with `onOver` withheld nothing ever sets it. */ null}
							onDrag=${mayWrite ? dragged : null}
							onMove=${mayWrite ? moved : null}
							over=${over} onOver=${mayWrite ? setOver : null}
							${/* **Storage holds the reader's explicit choices and nothing else**
							     (`#1008`); `CLOSED_BY_DEFAULT` answers for every key nobody has
							     touched. `Board` works the set out against the columns it has
							     already arranged — computing it here would mean calling
							     `columns` twice on one render, which is two answers that can
							     disagree. */ null}
							choices=${columnChoices} onCollapse=${collapse}
							${/* **What this workspace calls its statuses** (`#1019`), so a column
							     whose category holds exactly one can stop repeating it on every
							     card. Already fetched for the item page's status control, so
							     this costs no request. */ null}
							statuses=${vocabulary && vocabulary.statuses}
							${/* **Offered only where one parameter is the whole remedy**: a board
							     narrowed by `status_category` has every other column absent for a
							     reason no single link undoes, and a link per column claiming to
							     would be four ways to leave one state. */ null}
							${/* **`BOARD`, not `EVERYTHING`** (`#1790`). This is a link *into* a board,
							     so it has to write the same selection the board chip writes —
							     otherwise following it lands on an ungrouped board, which is the
							     one arrangement this item exists to stop anybody seeing. */ null}
							finishedTo=${showing.selection.status_category === undefined
								? withShowing(behind, { view: "board", selection: BOARD })
								: null}
							widenTo=${withShowing(listingAddress({ workspace }), widened(showing))} />`
						/*
							**No capture box while only finished work is showing** (`#706`).
							Adding from here would report success over a page the new item cannot
							appear on — it is open, and this selection holds only what is over —
							which is `#515`'s shape: every step reports success and the reader is
							left confirming the wrong conclusion. `Row` already declines to offer
							*Complete* on finished work by way of `completable` (`#724`), so
							`onComplete` is passed and simply never applies; the add box has no
							such guard and is withheld here.
						*/
						: html`<${Listing} items=${items} onOpen=${show}
							onComplete=${mayWrite ? complete : null}
							onAdd=${finishedOnly || !mayWrite ? null : add} busy=${busy}
							more=${more} adding=${adding}
							onMore=${showMore} project=${project} workspace=${workspace}
							onGo=${narrow}
							${/* **This workspace's alone**, because a listing is narrowed to one —
							     unlike the agenda above, which spans them and names each. */ null}
							prioritised=${prioritisedHere(me ? me.workspaces : [], workspace)}
							onPrioritise=${mayWrite ? prioritise : null}
							ordering=${orderedAs(showing.selection)}
							order=${showing.selection.order || null}
							${/* **No control on the finished view** (`#782`). Its order is part of what
							     that chip asked for, and changing it there would leave a page
							     narrowed to finished work ordered by when it was written — an
							     ordering that contradicts the selection it sits inside. */ null}
							onOrder=${finishedOnly ? null : chooseOrder}
							onWiden=${widen}
							widenTo=${withShowing(listingAddress({ workspace }), widened(showing))}
							selection=${showing.selection}
							empty=${finishedOnly
								? "Nothing has been finished here yet."
								: "Nothing here yet."} />`}

			${/* `items` is the listing's state and is empty while the agenda is showing, so
			     counting it unconditionally put "0 items" under a full day (`#652`). */ null}
			<${Foot} count=${agenda !== null ? counted(agenda) : items.length}
				theme=${theme}
				onTheme=${(chosen) => setTheme(
					applyTheme(chosen, globalThis.localStorage, document.documentElement)
				)} />
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
	/* **Wrapping `App` rather than sitting inside it**, so that a failure in `App` itself is
	   caught — which is where both of this arc's blank pages came from (`#680`). A boundary
	   inside the thing that throws catches nothing. */
	render(html`<${Boundary} what="The page"><${App} /></${Boundary}>`, document.getElementById("app"));
}

/*
	**Registering the service worker, which is what lets this page be installed** (`#1665`).

	The worker itself caches nothing — `sw.js` says why at length — so this buys installability
	and changes nothing about how the app behaves in an ordinary tab.

	**`scope: "/"` with the file at `/app/sw.js`.** A worker's default scope is the directory it
	came from, and the page it has to control is at the root; `api.web.worker` sends the
	`Service-Worker-Allowed` header that permits the wider claim. If that header ever goes, this
	rejects with a `SecurityError` naming the scope rather than failing quietly.

	**Guarded on `navigator` rather than on `document`**, because the two are separate questions:
	the render harness supplies a document so that components can be rendered, and it has no
	reason to supply a navigator. Nothing here may stop this module being imported.

	**Nothing is done with the promise but swallow its rejection.** A browser that refuses to
	register — a private window, an insecure origin, a user's own setting — is one where the app
	works exactly as it always has, so there is nothing to tell anybody and an unhandled
	rejection in the console would say there was.
*/
if (typeof navigator !== "undefined" && "serviceWorker" in navigator) {
	navigator.serviceWorker.register("/app/sw.js", { scope: "/" }).catch(() => {
		/* Not installable here, which costs this reader nothing they had. */
	});
}

/*
	**The app's public surface, unchanged** — `#1849`. Every name below was exported from this
	file before it was split, and is re-exported by hand rather than with `export *` so that a
	helper which had to become visible to a sibling module does not silently become part of what
	this app publishes. A test holds the two lists to each other.
*/
export {
	AGENDA_VIEW,
	ANSWERED_BY,
	BOARD,
	DEFAULT_VIEW,
	EVERYTHING,
	MAX_REF,
	ONLY_FINISHED,
	PATH_SEPARATOR,
	PRODUCT,
	SELECTABLE,
	VIEWS,
	addressOf,
	agendaRequest,
	answers,
	chips,
	chosenWorkspace,
	encodedPath,
	frame,
	listingAddress,
	mentionHref,
	pageTitle,
	parseAddress,
	permits,
	projectLabel,
	refAsked,
	reloads,
	selectionOf,
	shortVersion,
	showingOf,
	titlesByPath,
	viewOf,
	withShowing,
} from "./address.js";

export {
	HORIZON_DAYS,
} from "./settings.js";
export {
	accumulated,
	inOrder,
	mergeOrder,
	newestFirst,
	refusal,
	sunkOrder,
	unpacked,
	unrenderable,
} from "./answers.js";
export {
	Facts,
	Foot,
	Note,
	Prose,
	THEMES,
	Theme,
	Wordmark,
	applyTheme,
	themeChoice,
} from "./chrome.js";
export {
	DEFAULT_ORDER,
	DEFERRED,
	ORDERINGS,
	calendarDay,
	completable,
	day,
	deferred,
	excluded,
	holding,
	named,
	offeredOrders,
	orderedAs,
	orderingValue,
	overdue,
} from "./dates.js";
export {
	Detail,
	Doing,
	Failed,
	Linking,
	Saying,
	Seeking,
	Written,
} from "./detail.js";
export {
	ANCHORS,
	Adding,
	Asking,
	CAPTURE_HINT,
	Conflict,
	DATE_FIELDS,
	DOCUMENT_HINT,
	DocumentFields,
	Editing,
	Fields,
	Listing,
	Narrowed,
	PRIORITIES,
	Reading,
	Repeats,
	TIMED,
} from "./forms.js";
export {
	CLOSED_BY_DEFAULT,
	NOT_SHOWN,
	agendaBuckets,
	blockersDone,
	choicesIn,
	collapsedColumns,
	columns,
	counted,
	followed,
	opens,
	partsDone,
	rememberChoices,
	withinAllowance,
} from "./grouping.js";
export {
	CATEGORY_ICONS,
	Icon,
	KIND_ICONS,
	MARK_ICONS,
	TYPE_ICONS,
	UNKNOWN_ICON,
	WAITING_STATUS,
	marks,
	moment,
	when,
} from "./marks.js";
export {
	addressedProjects,
	filableFor,
	notOffered,
	offered,
	people,
	placesToGo,
	prioritisedHere,
	prioritisedSentence,
	projectName,
	projectsRequest,
	rankedByPriority,
	soleStatusIn,
	statusFor,
	treeOrdered,
	unmovable,
	vocabularyRequest,
} from "./places.js";
export {
	DOCUMENT_SAID,
	NEVER_CLEARED,
	RELEASE_CHECK_POLLS,
	REPEATED,
	SAID_AS_NUMBERS,
	SAID_AS_WRITTEN,
	addRequest,
	allowedIn,
	assignRequest,
	authorOf,
	collectionsFor,
	commentRequest,
	completeRequest,
	conflictIn,
	dateFor,
	documentRequest,
	edited,
	filed,
	freshly,
	fromItem,
	headRequest,
	identityRequest,
	itemRequests,
	linkAsked,
	linkChoices,
	linkRequest,
	linkableTypes,
	listingRequests,
	localMoment,
	pollRequest,
	prioritiseRequest,
	readForm,
	readingRequest,
	releaseMoved,
	repeating,
	repeats,
	restoreRequest,
	rosterRequest,
	signOutRequest,
	statusRequest,
	timeFor,
	touching,
	unlinkRequest,
	updateRequest,
	withTime,
	written,
} from "./requests.js";
export {
	Agenda,
	Board,
	Marks,
	Row,
	Stamp,
} from "./rows.js";
