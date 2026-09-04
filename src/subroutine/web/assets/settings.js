/*
	How much is asked for and what is asked for — the numbers and field lists every
	request and every page is built from.

	**Split out of `app.js` by `#1849`**, which moved 10,286 lines into thirteen modules and
	changed nothing about what any of them do. Read `app.js` for what the whole app is; this
	file is one layer of it.
*/


/*
	How far ahead the agenda looks, in days. **The CLI's number, deliberately** — it is
	`domain.agenda.DEFAULT_HORIZON_DAYS`, and `tests/test_web.py` compares the two so this
	cannot drift from the surface it is meant to match.
*/
export const HORIZON_DAYS = 7;

/*
	**Here rather than beside `agendaRequest`, and that is not tidiness** — `#1849`. The agenda's
	bucket list interpolates this number into a heading at module load, so it is read while
	modules are still evaluating; with it in `address.js` the cycle `grouping → address →
	requests → grouping` put it in the temporal dead zone and the whole app failed to load with
	`Cannot access 'HORIZON_DAYS' before initialization`.

	This module imports nothing, so nothing can be pending when it is read. **A value read at
	evaluation time belongs where evaluation cannot be in progress**, which is the rule rather
	than the one instance.
*/

/*
	How often to ask what has changed. `GET /v1/changes?since=` was built for exactly this and
	is what SSE's own reconnection protocol reduces to — `Last-Event-ID` *is* `?since=`. So the
	catch-up path is needed either way, and polling first means it runs on every tick rather
	than only after a network blip.

	Measured against the instance's own limit: `rate_limit_per_minute` is 600, and a ten-second
	poll spends 6 of them.
*/
export const POLL_MS = 10000;

/*
	How many events one poll reads.

	**Not a ceiling on what happened**, which is why `has_more` is acted on rather than ignored:
	a batch that had to stop is a batch that may have hidden the one event this reader cares
	about, and the honest answer to *I could not see all of it* is to re-read the open item
	rather than to assume. On this instance a busy minute is a few dozen events, so a hundred is
	a tick's worth several times over.
*/
export const POLL_PAGE = 100;

/*
	The three fields a poll reads, and it reads nothing else (§14.10).

	`seq` is what the cursor resumes from. `item_ref` with `workspace_id` is what says whether
	the item somebody has open is among them — the ref alone would not, because a ref is unique
	*per workspace* (§6.2) and the agenda's poll spans all of them.
*/
export const POLL_FIELDS = ["seq", "item_ref", "workspace_id", "entity_type"];

/* How many rows to ask for. The listing says when it had to stop, so this is a page rather
   than a ceiling on what exists. */
export const PAGE = 100;

/* How many rows one column of a board carries — `#1790`. Deliberately smaller than `PAGE`,
   because a grouped request costs this times the number of groups: four task categories and
   four document ones is eight, so a column-sized allowance of 100 would fetch eight hundred
   rows to fill a screen. Twenty-five is within the range Simon asked for and is what the
   server defaults to anyway; it is written out here so the two cannot drift silently. */
export const COLUMN = 25;

/* What *Show more* does on a board — `#1790`. A board pages by widening every column rather
   than by following a cursor: the columns do not share a sequence, so there is no one page to
   continue, and a reader who presses this wants more of whichever column they were looking at.
   The last step is the server's own ceiling on a group, so pressing again cannot ask for
   something that will be refused. */
export const WIDER = [25, 50, 100];

/*
	How many of an item's parts are drawn — `#1218`, and the terminal's own `MAX_CHILDREN`.

	**Whatever it does at the cap it must not do silently** (`#888`, and `#1175` is the open item
	about a listing claiming a completeness it cannot have). The response says `has_more`, and
	`Parts` renders that as a line rather than stopping at fifty rows and looking finished.
*/
export const MAX_PARTS = 50;

/*
	How many links are drawn before the rest are held behind a control — `#1820`.

	**Measured across 100 items on this instance before the number was chosen**: *Links* has a
	median of 2, is empty on 19% and holds more than five on 12%, with a maximum of 26. So five
	leaves 88% of items untouched and catches exactly the ones where the body is below the fold.

	**Truncated rather than collapsed, which is where this departs from what was asked for.**
	Simon proposed hiding the section entirely above a threshold. But `#84`'s model is that a
	milestone is an item whose blockers are its contents, and it is precisely the items with
	many links that are milestones — so a default keyed on the count would fold away the point
	of the pages it fires on. Showing the first five keeps the body reachable, still says how
	many are held, and always leaves some links on the page.

	**Which five is already decided and is not this constant's to make.**
	`domain.links.reading_order` sorts outstanding before settled first of all — Simon's call on
	2026-08-28, argued then as *on a milestone of thirty-three the few left are the whole
	answer*, which is this truncation's case stated a fortnight before it existed. So the five
	drawn are the live ones, most-binding first, prerequisites before dependents.
*/
export const LINKS_SHOWN = 5;

/*
	Where a preference this browser holds is written — `#1008`, `#1820`.

	Named rather than spelled at the call site, because a key typed in two places is a
	preference that reads back empty in one of them and nothing says so.
*/
export const BOARD_COLUMNS_REMEMBERED = "board-columns";
export const SECTIONS_REMEMBERED = "detail-sections";

/*
	What the links section is called where a reader's choice about it is recorded — `#1820`.

	**One string rather than three that have to agree.** The name is written into the stored
	map, read back out of it, and put into the list's `id` for `aria-controls`; spelled at each
	site, a rename would leave the control writing where nothing reads and nothing would say so.
	`App` never names it at all — it hands the whole map down and takes the name back from the
	control — so `#1143` adds a section by choosing a word here and nowhere else.
*/
export const LINKS_SECTION = "link";

/*
	**What a listing asks for, which is what a row shows and nothing else** (§14.10, `#645`).

	Measured on the served instance: a whole page of tasks is 287 KB and a whole page of
	documents is **1.3 MB**, because a document's body comes down in full and the bodies here
	are the specification. Shaped, the pair is 38 KB — forty-two times smaller, and the reader
	sees exactly the same list.

	§14.10 calls response size a first-order cost for the agent client. It is a first-order cost
	for a browser on a train too, and this is the surface that felt it first.

	**These are a second copy of what `Row` renders**, which is the shape this codebase gets
	wrong most — so `tests/test_web.py` derives the requirement from `Row`, `marks`, `when` and
	`overdue` rather than trusting the two to be kept in step. A field left out does not error:
	it arrives as null, and a null reads as *not set* rather than as *not asked for*, which
	would quietly invert §12.2c.
*/
export const TASK_FIELDS = [
	/* How well a row answered a search, so a merged list can be put back into the order
	   the server ranked it in (`#875`). Null on every listing that is not a ranked search. */
	"relevance",
	/* **Whether the type is its kind's default** — `#1148`. The card's strip says *Task* or
	   *Document* on every row now, so a type chip reading `task` one line below would be the
	   same word twice, which is §12.2a. A row cannot work it out: which type is default is a
	   fact about the workspace's vocabulary rather than about this item. */
	"type_is_default",
	"ref", "title", "due_at", "due_is_all_day", "starts_at", "starts_is_all_day",
	"blocked", "sub_tasks_done", "project_key",
	"project_path",
	/* **The colour in force for this row's project** (`#1027`) — its own, the nearest
	   ancestor's, or its workspace's. Resolved on the server, so what arrives is a palette name
	   and this client holds no copy of the inheritance rule (`#925`). */
	"project_colour",
	"assignee",
	/* **What the name beside a row actually is, and who is on the hook for it** (`#1414`).
	   A name is a claim by whoever created the account: somebody may call an agent
	   `claude-super` and point a different model at the same credential. Resolved on the
	   server because walking an accountability chain is a rule, and this client holding a
	   copy of it is `#925`'s argument against exactly that. */
	"assignee_is_agent", "assignee_answers_to",
	/* The other end of a `blocks` link (`#861`). `blocked` says you cannot start this;
	   this says something else cannot start until you do, and a row can be both. */
	"blocking",
	/* **That it comes back** (`#925`). The row shows a short mark rather than this sentence,
	   so what is asked for is more than what is drawn — deliberately, because the *presence*
	   of the sentence is the fact and asking for `recurrence_rule` instead would make the
	   browser hold a copy of the grammar to read it. */
	"recurrence_description",
	"status", "status_label", "status_is_default", "status_category",
	/* **What kind of thing this is** (`#764`) — a bug, a decision, a chore. A row showed
	   `Task` or `Document`, which answers what shape it has and not what it is about, and
	   Simon's fifth requirement is that *a bug and a document are distinguishable without
	   clicking*. Both kinds carry one, so both lists ask. */
	"type",
	/* **What to draw when the key above is one this client has never seen** (`#1134`, decision
	   `#1133`). Not rendered as a word — `marks` prints the type itself — but the glyph falls
	   through it, so a workspace that invents a type gets a picture that means something rather
	   than the mark for *unknown*. Both kinds carry one, for the reason above. */
	"type_category",
	/* **Which timezone a day-scale date was stored in** (`#773`). §6.5 stores an all-day
	   deadline at the last instant of its day *in the task's own zone*, so rendering it in the
	   reader's shows the next day to anybody east of it — measured, and live: the terminal said
	   `(due Fri 14 Aug)` while the browser said 15 Aug about one item. A row without this would
	   fall back to UTC, which is the answer that happens to be right here and wrong for anybody
	   whose instance is not. */
	"timezone",
	/* **When work was put off until** (`#862`). The board showed a deferred item looking
	   exactly like one nobody had put aside — the terminal hides them, so the two surfaces
	   disagreed about work that had been deliberately parked. `snoozed_is_all_day` comes with it
	   for `#864`'s reason: an item deferred to an o'clock says the o'clock. */
	"snoozed_until", "snoozed_is_all_day",
	/* Who is holding a lease, and until when (`#726`). All three, because the mark says the
	   holder's name, the id is what says anybody holds it at all, and the expiry is what says
	   whether that still means anything — `claimed_by` alone would be null on an instance older
	   than the field while the item was genuinely claimed. */
	"claimed_by_id", "claimed_by", "claim_expires_at",
	/* **What somebody labelled it** (`#1019`). `marks` never read these, so a tag reached the
	   item page's fact sheet and no listing, board or agenda — invisible for as long as tags
	   have existed. A field rendered and not asked for arrives as absent rather than as
	   unknown, which is what the guard beside this one is for. */
	"tags",
	/* Rendered by `when` on anything finished, and the field the *done* view is ordered on
	   (`#706`). §22 has no rule about showing the sort key and `#661` is the item that wants
	   one; a page whose whole claim is *most recently finished first* had better say when each
	   row finished, or the order is something a reader has to take on trust. */
	"completed_at",
	/* The merge key (`#660`), and since `#661` the value a row shows when the page is in its
	   default order. The API sorts both collections by `-created_at` and pages on it. */
	"created_at",
	/*
		**Asked for because a reader can order by them** (`#782`), and for no other reason —
		`marks` renders each only while the page is sorted on it, so on every other page these
		three arrive and are never read.

		That is the cost of a chosen ordering and it is small: three fields against a page that
		already carries sixteen. The alternative is a request that changes shape with the
		address, which would make `fields=` a second thing to keep in step with `ORDERINGS`.
	*/
	"updated_at", "importance", "urgency",
].join(",");

/* A document has no dates and no assignee — `_when` returns nothing for one — so it asks for
   less. Not the same list with the extras arriving null, because that is the difference
   between "has no deadline" and "cannot have one", and only one of them is true. */
export const DOCUMENT_FIELDS = [
	/* The task list's counterpart — `#875`. A search spans both kinds, so a key only one
	   of them carries is no key at all. */
	"relevance",
	/* `#1148`, and both kinds ask for it for one reason: the strip says the kind on every card,
	   so a chip repeating the default type is the same word twice (§12.2a). */
	"type_is_default",
	"ref", "title", "project_key", "project_path", "status", "status_label", "status_is_default",
	/* `#1027`, and both kinds ask for it: a document lives in a project exactly as a task
	   does, so a listing of decisions is marked the same way a listing of bugs is. */
	"project_colour",
	/* `#1019`, and both kinds ask for the same reason: a tag is scoped to the *workspace*
	   rather than to a kind (`#819`), so a document carries them exactly as a task does. */
	"tags",
	/* **What kind of thing this is** (`#764`) — a bug, a decision, a chore. A row showed
	   `Task` or `Document`, which answers what shape it has and not what it is about, and
	   Simon's fifth requirement is that *a bug and a document are distinguishable without
	   clicking*. Both kinds carry one, so both lists ask. */
	"type",
	/* **What to draw when the key above is one this client has never seen** (`#1134`, decision
	   `#1133`). Not rendered as a word — `marks` prints the type itself — but the glyph falls
	   through it, so a workspace that invents a type gets a picture that means something rather
	   than the mark for *unknown*. Both kinds carry one, for the reason above. */
	"type_category",
	/* Not rendered either — it is what the board groups on (`#653`). A document's categories
	   are its own vocabulary, so it gets its own columns rather than being mapped onto a
	   task's: `current` is not *in progress*, and saying so would be inventing a claim. */
	"status_category",
	/* As above: the merge key, and the value a row shows in the default order (`#660`, `#661`). */
	"created_at",
	/* One of the four keys both collections can be ordered by, so a reader who chooses
	   *recently changed* can see it here as well as on a task (`#782`). */
	"updated_at",
].join(",");
