# Changelog

Notable changes, newest first. Dates are the date of the release.

**A release that changes the database schema says so at the top of its own section**, and CI
fails if it does not. `scripts/check_release_notes.py` compares the migration head against the
most recent tag rather than trusting anybody to remember, so the notice appears in the
unreleased section on the day the migration lands — written by whoever wrote the migration, not
by whoever cuts the release in a hurry three weeks later. `--emit` prints the wording.

The point of it is that you can *plan* a database upgrade instead of meeting one halfway
through installing something. See [docs/hosting.md](docs/hosting.md#upgrading) for what the
upgrade involves.

## Unreleased

> **This release changes the database schema**, to `c9e4a1b73f52`.
>
> Install it, then run `subroutine db upgrade`. That reports both versions, takes a
> verified backup, migrates and checks the result — in that order. Stop the service
> first if you are running one; expect it to be down for the length of the migration.

### Added

- **A request that can never finish is stopped and says so, rather than hanging.** Nothing
  bounded how long one statement could run, so a row lock, a query that would never finish or a
  database that had stopped answering all reached the caller as *silence* — which from outside
  is indistinguishable from a deploy, a network fault or your proxy, and has been read as
  exactly that. The new `request_timeout_seconds` setting is a bound, defaulting to **30
  seconds**; reaching it is `503 request_timed_out`, which says the request was given up on,
  that nothing it was doing was written, and that retrying may work.

  **PostgreSQL only**, because SQLite has no statement timeout of any kind. What SQLite has is a
  five-second wait for a contended lock, which is the case that actually hangs there, and it is
  unchanged. **It does not reach a backup**: `POST /v1/admin/backups` legitimately takes minutes
  and does not run on the request's own session, so no exception had to be written for it, and
  the same is true of a restore and of `subroutine db upgrade`. Set it to `0` to wait for ever
  as before.

  `request_timed_out` is a new error code, so a client matching on codes gains one it has not
  seen. It is deliberately not `service_unavailable`, which says the instance cannot serve
  anything: this instance is serving, and it was this request that did not finish.

- **Prose being written can be seen as it will read.** A task's description, a document's
  body and a comment box each carry a **Preview** button now, which renders what you have typed
  with the same Markdown renderer the item page uses. Press it again to go back; what you were
  writing is still there.

- **An item shows what refers to it.** Every `#42` written in a title, a description, a
  document body or a comment has been indexed since the first release and read by nothing, so
  *what refers to this?* was answerable on no surface. `subroutine show` now ends with a
  **Referred to by** section, `subroutine_show` carries the same, and
  `GET /v1/tasks/{ref}/backlinks` and `/v1/documents/{ref}/backlinks` answer it directly.

  A mention from a comment names the item the comment is on and says so, because a comment has
  no number of its own. A mention from a project you cannot see is not listed at all.

- **The browser notices when the instance is redeployed under it and offers to reload.** It
  compares the version that served the page against the one answering an hour later, and shows
  a line beside the work with a *Reload* button on it. It never reloads by itself — you may be
  halfway through writing something — and it is not a dialogue that has to be dismissed before
  you can carry on.

- **A project can say which statuses it does not offer, and everything under it inherits that.**
  `statuses.hidden` on `PATCH /v1/projects` and `PATCH /v1/workspaces`, and
  `subroutine project update <key> --hide-status blocked` — repeatable, and `--hide-status ''`
  offers them all again. A workspace that shrinks its vocabulary once shrinks it for every
  project below, unless one says otherwise. `GET /v1/projects` reports `hidden_statuses`
  beside `settings`: the raw map is what that project was *told*, and the new field is what is
  **in force**, resolved up the tree so a client need not walk it.

  **It narrows what is offered and refuses nothing.** Any surface may still set any status the
  workspace has — this is a preference rather than a permission, so it cannot break a script,
  an import, or anything that read the vocabulary before you changed your mind. The browser's
  status pickers are what read it today.

  **Two things are always offered whatever you hide**: the status an item is already in, so a
  control can never claim a state the item is not in; and the status new work is created in, so
  a project cannot make an ordinary task unfileable. A status key the workspace does not have is
  refused by name, with the ones it does have listed.

- **A workspace and a project can be given a colour, and everything under it inherits one.**
  `PATCH /v1/workspaces` and `PATCH /v1/projects` accept a `settings` map, and every task and
  document now reports `project_colour` — the colour in force for it, which is its project's
  own, or the nearest ancestor's, or its workspace's, or none. Colours are chosen from a
  **named palette** rather than given as hex, so the same choice can be rendered by any surface
  and is guaranteed to be legible in both light and dark themes; a value that is not one of the
  names is refused with the whole list.
  **Every row, board card, agenda line and item page carries a bar in that colour** — so on a
  page holding work from several projects you can see at a glance which is which, without
  reading a word. A project that has not been given one inherits from whatever is above it, so
  setting a colour on a workspace marks everything in it at once.
  Set one with `subroutine workspace update projects --colour indigo` or
  `subroutine project update web --colour teal`; `--colour ''` clears it. There is no control
  for it in the browser yet.

- **A project template no longer writes a setting nothing reads.** `visible_status_keys` was
  seeded into every project's `settings` by its template and consulted by no part of the
  program. It is gone; `template` itself is unchanged and still accepted.

- **Settings are validated against a declared registry.** A key nothing declares is refused by
  name rather than stored, and a map is merged **per key** — so setting one thing leaves
  everything else alone, and sending a key as `null` clears just that one.

- **The small labels on a row now say which *kind* of fact they are.** Every one used to be the
  same rounded lozenge, so a project, a tag, a status and *Blocked* looked alike and only the
  words told them apart. There are three shapes now: what an item **is** (its type, then its
  status) is filled; what is **true of it now** (blocked, blocker, overdue, deferred, claimed,
  repeats) is outlined; and **where it lives and whose it is** carries no box at all.
  **Addresses use the sigils you already type**: `#ops` for a tag and `@si` for a person, so a
  sub-project and a tag of the same name can no longer be confused. A project keeps the bare
  word. Every label still says its word, so nothing here is carried by shape or colour alone.

- **Tags appear on a row for the first time.** They reached the item page and no listing, board
  or agenda — so a label you had applied was invisible everywhere you would have looked for it.

- **An item's page opens with the same labels its row carries**, directly under the title. It
  was the one screen with no summary at a glance, and it is the one you land on from a card. The
  four fact-sheet lines that now repeat a label — status, type, assignee and tags — have gone;
  the project line stays, because only it can say a project is prioritised.

- **A board column stops repeating its own name on every card.** *Done*, *Cancelled* and
  *Superseded* say it once, at the top. **To do does not**, deliberately: `open`, `blocked` and
  `needs_input` all live in that column, so the label there is the only thing separating them.

- **A board column can be folded away, and the ones holding work that is over start folded.**
  *Cancelled*, *Superseded* and *Archived* now begin as a narrow strip with their name turned on
  its side and a count beside it, so a board full of abandoned and replaced work gives its width
  back to the work you are doing. Click the strip to open it again, or the small control at the
  top right of any open column to fold that one — the browser remembers, per column, until you
  say otherwise.
  **Nothing is hidden that you cannot see is there**: a folded column keeps its place in the
  order, says how many items it is holding, and stays a drop target, so you can still drag a
  card onto it. *Done* is deliberately not folded, because finished work is something you have
  to ask for and it is where *Show finished work* lives. An empty column stays open too — a
  board with nothing in progress is telling you something, and it is where you drag the next
  thing.

- **One project in a workspace can be prioritised, and its work rises.** `subroutine project
  prioritise web`, and `--none` to stop. Its tasks — and everything filed underneath it — sort
  higher in ranked listings and on your agenda under *Next*, while anything genuinely urgent or
  important in another project still comes first. That is the difference between this and
  putting a project on hold: nothing is hidden, and nothing else has to be.
  **One project per workspace, and choosing another moves it**, which is the whole design rather
  than a limit: a dial per project would let four quiet boosts accumulate until the order means
  nothing again, and nobody would remember setting them. Choosing web says so and says what
  stopped being the priority, in the same line. It is worth about half a step of one priority
  axis, deliberately not a number you can set. Work with no importance or urgency on it is
  unaffected, since there is nothing to raise.
  **In the browser** it is a button on any page narrowed to a project, and the project is marked
  wherever one is named — the masthead, the add and edit form, and the item's own details. A
  ranked list and the agenda say once, above the rows, which project is raising them.
- **You can say which timezone you are in.** `subroutine user timezone Europe/London`, and
  `subroutine user timezone` on its own reports what your account holds. It was settable when
  an account was made and by nothing afterwards, so somebody added by a colleague, or anybody
  who has moved since, was stuck with whatever the server was set to. Your own account and
  nobody else's — you know where you are better than anybody else does, so there is no
  permission that lets somebody set it for you. It decides which day a deadline counts as; it
  does not change how a date is written, since a day belongs to the item that has it.
- **A task can be filed underneath another one.** `subroutine add "Write the changelog"
  --under 12` puts it under item 12, which is the first step of breaking a piece of work
  into parts and handing any of them over. The API has accepted a parent since the
  beginning and no client could pass one, so until now it took a hand-written HTTP
  request. A parent you cannot see is reported as no such item rather than quietly
  ignored.
- **The masthead takes you to any project, not just any workspace.** It lists every workspace
  with the projects of the one you are in nested underneath and indented by name, so a
  sub-project is one choice rather than an address to type. **And it is there when there is
  only one workspace**, where it used to render the name as plain text: the only thing saying
  where you were could not be used to go there, which on the agenda left `/` as the only
  address you had.
- **Both project dropdowns are in alphabetical order within each parent.** They arrived in the
  order somebody created them, because a project's stored path is built from ids rather than
  names. The add and edit form keeps the Inbox at the top whatever it is called, since that is
  where an item goes if you say nothing.
- **Searching for `#42` opens that item instead of listing it.** Any query that is nothing but
  a ref goes straight there; if no such item exists it is an ordinary search, as is a bare
  `42` — a number on its own is something you might genuinely be looking for.

- **Your work can appear in the calendar application you already use.**
  `subroutine calendar create "My work"` prints one address; paste it into Google Calendar,
  Apple Calendar, Outlook or Thunderbird and anything with a date shows up there and keeps
  itself up to date. `--project`, `--mine`, `--type` and `--expires` narrow it; `calendar
  list` says when each subscription was last fetched, `calendar reset` replaces the address
  of one that has leaked without losing the subscription, and `calendar revoke` stops it.
  An item's start and its deadline are separate events, so neither hides the other, and an
  item that repeats on a fixed schedule arrives as a repeating event rather than as several
  hundred copies. The feed keeps a week of the recent past — most calendar applications
  delete an event the moment a feed stops sending it, so dropping finished work would erase
  a meeting from your calendar's history the moment you ticked it off.

  **The address is a credential**, and unlike every other one here it travels in a URL,
  because no calendar application will send anything else. It is shown once, it shows only
  what its owner may see at the moment somebody fetches it, and it stops working when that
  person's account does. An operator who would rather not have that at all can set
  `calendars_enabled = false`, and every feed address then answers 404 exactly as an address
  naming nothing does. Over HTTP it is `POST /v1/calendars`, `GET /v1/calendars`,
  `POST /v1/calendars/{id}/reset` and `DELETE /v1/calendars/{id}`, with the feed itself at
  `GET /v1/calendars/{prefix}/{secret}.ics` — the one public route that reads work.
- **An item's links say enough about each end to judge it without opening it.** In the
  browser they now carry the same marks a row on a list or a board does — the type, the
  status, the project, whether that end is blocked or blocking, who has it, and whether it
  repeats — so a reader can tell a bug from a decision, and a finished blocker from an
  outstanding one, at a glance. A closed item is struck through, and the heading says
  **N of M blockers done**, which the terminal has printed for a while and no other surface
  did. An agent reading an item gets the same count and an `(over)` on each finished end.
  The endpoints that report a link now carry those fields on `other`.
- **A listing says where each item lives, by its whole address.** `substation/dist` rather
  than `dist`, which no longer names one project. The label leaves out whatever the request
  already said — `subroutine list --project substation` shows `dist` and `tools`, not
  `substation/dist` and `substation/tools` — and disappears entirely when what is left is the
  same on every row, so an ordinary to-do list is unchanged. `subroutine show` and the agent's
  reading of an item both say the whole address too.
- **`project_path` on a task and on a document, and `path` on a project**, in every response
  and in `subroutine list --json`. It is what goes back into `--project`, `?project=` and
  `+key`. Composed once for a whole page rather than per row.
- **In the browser, that label is a link**, and clicking it narrows the page to that project.
  It leaves out whatever the address already said — from `/projects/subroutine` a row shows
  `ui`, from `/projects` it shows `subroutine/ui`, and from the agenda at `/` it leads with
  the workspace. Unlike the terminal it is never dropped when every row agrees: a page polls,
  so a label that vanished because somebody else filed something would be a control moving
  under the cursor.


- **A project can be put on hold, finished or archived** — `subroutine project update web
  --status on_hold`, and `status` on `PATCH /v1/projects`. Work in a project that is not
  running stops being offered as something to start: it leaves `list --ready` and the
  agenda's *Next*. It stays everywhere else — an ordinary listing still holds it, asking for
  the project itself still shows it, and a search still finds it — so putting a project down
  is a pause rather than a disappearance, and bringing it back is one command.

  Dated work is deliberately **not** hidden. Something overdue or due today stays on the
  agenda even while its project is on hold, because a deadline is usually a commitment to
  somebody else and pausing your own work does not cancel it.

  Three of the four statuses every workspace is seeded with had never been reachable: a
  project was given the default when it was created and no command, client or endpoint could
  ever change it.
- **A project's title, description and visibility, and a workspace's title, description and
  timezone, can be changed after they are created** — `subroutine project update` and
  `subroutine workspace update`. All six were accepted by the API and reachable from no
  client, because the only methods that existed were named for renaming and took nothing but
  the short name. The timezone is the one that mattered: every date in a workspace is read in
  it, so one set up in the wrong zone showed every deadline at the wrong time with no way to
  correct it.

  Short names are still changed by `project rename` and `workspace rename`, which say what
  stops working before they do it.

### Changed

- **An agent asking what is on today gets the same agenda everybody else does.** It used to
  be given three of the five sections — overdue, due today and the week ahead — and the two
  it was not given are the ones most days actually have anything in. On this project's own
  instance 11 of 170 open tasks carry a date, so an agent was told *nothing on today* while
  the same instance showed a person twenty ranked items. It now gets work in progress and
  the ranked backlog as well, **and each row says which section of the day it belongs to**,
  so a suggestion is not mistaken for a commitment. `limit` narrows it, which it silently
  did not before.
- **A project key is now unique among its siblings rather than across its workspace, and a
  project is addressed by its whole path.** `substation/dist` rather than
  `substation-substation-dist`: `web-ui` and `marketing` can exist under any number of
  parents. A bare name still works wherever it names one project, and is refused with the
  candidates listed when it names several — so nothing already written stops working, and
  the refusal is what teaches the longer form. The path is accepted by `--project`,
  `?project=`, `+key` in a captured line, `use --project`, `token create --project`, and as
  the address in `/v1/projects/…`.
- **`POST /v1/tasks` answers 404 rather than 422 when a captured line names a project that
  is not there.** The two ways of naming a project on that request — `+key` inside `text`,
  and the `project` field — were refused with different statuses for the same mistake. Both
  are 404 now, which is what the field error has always said.
- **A `.subroutine` marker records a project's whole address**, and one written before this
  goes on resolving. Existing markers are rewritten by `subroutine use --here --project …`,
  which the program suggests when it notices.

### Fixed

- **The rows a page shows come from the workspace its address names.** Going into a project
  from an item opened elsewhere, or stepping forward onto such an item, left one workspace's
  backlog under an address naming another — and the switcher, the capture box and the search
  went on naming the workspace you had left. A linked item on such a page was addressed into
  the wrong workspace too, so following a blocker opened whatever wore that number there.

- **An item opened from the agenda is furnished from its own workspace.** Its status picker,
  the projects its edit form offers, who it can be handed to, and which controls are drawn at
  all were all fetched for the workspace the switcher held — so an item in another workspace
  offered statuses it cannot be moved to and omitted the ones it can, offered projects it
  cannot be filed in, offered people who are not in it, and drew Edit, Complete and the comment
  box for a reader who may only read there. Every one of those would have been refused when
  pressed.

  Nothing is offered while the item's own workspace has not answered, and nothing is offered at
  all if that reader cannot see it — an empty control rather than a confidently wrong one.

- **A change made from an open item goes to that item, not to whichever item wears the same
  number in the workspace the browser's switcher happens to hold.** Opening an item from the
  agenda at `/` — which spans workspaces — left the page correctly showing that item while
  every write it offered named the *other* workspace. Changing an item's status cancelled the
  item wearing the same number somewhere else, and the re-read afterwards followed it — so the
  reader was left in front of a different item, in a different workspace, which they had just
  altered.

  It reached seven controls: the status picker, **Complete**, the assignee, the comment box,
  linking, unlinking and **Save** — the last of which carries the title, the description, both
  dates and the status, so against the wrong item it overwrote all of them in one request. Only
  the status one had been noticed. Nothing is unrecoverable: `GET /v1/tasks/{ref}/events` says
  what changed on an item and who changed it.

  Two reads had the same fault and are fixed with it: stepping **Forward** onto an item outside
  the switcher's workspace opened the wrong one, and a `#42` written in such an item's prose
  linked to whichever item wore that number somewhere else.

  **The instance was doing as it was told throughout** — `?workspace_id=` was resolved
  correctly, and a ref being unique per workspace is the design. Nothing on any other surface
  was affected: the terminal, the API and an agent each name a workspace explicitly.

- **A search ranks a title match above a body match.** Ranking was term frequency and density
  alone, so a long description mentioning a word repeatedly outranked the item whose *title*
  was about it — searching this project for `seeded` put the item called *A search for 'seeded'
  finds 'seed'* fifth, below three body matches and a 97 KB specification. A title now counts
  for two and a half times the prose beneath it.

  **Which rows a search finds is unchanged**; only their order moves. PostgreSQL only, like the
  index itself — a SQLite instance answers `q` the way it always has, unranked.

- **The agenda says when something was dated in a different timezone from your own.** A
  deadline set for the end of somebody's UTC day falls just past the end of a London reader's,
  so the row said *due Thu 20 Aug* under a heading meaning *not today* — two correct rules
  meeting on one line with nothing to explain them. A date is still shown for the day it was
  set and still bucketed against your day; what is new is a line saying so, and only when the
  zones actually differ.

- **A comment says who wrote it.** `GET /v1/tasks/{ref}/comments` returned `author_id` and no
  name, so the one view whose whole purpose is reading what people recorded could not say who
  recorded it without a lookup per line. There is an `author` field beside the id now, resolved
  for a whole page in one query, and `subroutine show` prints it beside each date — five of
  eight accounts on a typical instance are agents, so *who wrote this* is the difference
  between a colleague's note and a machine's.

- **A scripted search says why each row matched.** `subroutine search <term> --json` now
  carries `matched` — `title`, `description`, `body`, `number` or `elsewhere` — the same cell
  the terminal has shown beside each hit since 0.7.0. Without it a script got the row and not
  the reason, and a hit whose title does not contain the term reads as a broken search. It is
  the computed cell rather than the fields it was computed from, so a listing does not grow
  every hit's whole description; `null` on any listing that was not a search.

- **Asking for finished work by its status *key* finds it.** `?status=done`,
  `subroutine list --status done` and an agent's `status="done"` all answered nothing on an
  instance full of completed work, while asking the same question by category or by
  `completed_at` answered correctly. A listing hides finished work unless asked, and naming
  the finished status was not recognised as asking — so the rows were found and then filtered
  away.

  It is decided by the status's **category**, not by the word, so an installation that has
  renamed `done` still gets the right answer. `status=done&include_completed=false` is now
  refused as the contradiction it is, naming `status` — the parameter you actually sent —
  rather than one you did not.

- **An agent is told everything that binds it here, not only the decisions.**
  `subroutine://conventions` — the one channel a connecting agent is instructed to read before
  its first write — asked for documents of type `decision`, so a specification, a design or a
  dead end that was *in force and governing* reached nobody. Measured on this project's own
  instance: six such documents, the release procedure and the accountability model among them,
  with nothing wrong with how any of them was written.

  It now asks whether a document **is in force**, and lists every kind that binds a reader —
  decisions, specifications, designs and dead ends — **grouped under headings that say what
  each obliges you to**, because *we decided this*, *the specification says this* and *this
  route is closed* are different obligations. Findings and notes stay out: they describe
  rather than bind, and the index says so and points at them.

  **Two smaller faults went with it.** It relied on the default page size, so an instance with
  more than fifty of anything would have listed a page and implied it was the whole set; each
  kind is now fetched with an explicit bound and says plainly when it comes back full. And it
  sent the status key `active` as a literal — a key an installation may rename — which did not
  merely empty the index but failed the whole resource with *there is no document status called
  'active' here*. It reads the category instead, which cannot be renamed.

- **A plain date given to `due_before` or `due_after` is answered instead of failing.**
  `?due_after=2026-08-18` returned a 500 and told the caller nothing about the parameter they
  had sent; only a full timestamp worked. The two are now read exactly as `due_at.lt` and
  `due_at.gt` are, so they take a date, a timestamp *or* the relative words `/v1/meta` publishes
  — `yesterday`, `start_of_week+3d`, `now-1y` — and a date takes in the whole day it names, in
  your own timezone. A value that cannot be read is now a 422 naming the parameter.

- **`/readyz` notices when the database underneath a running instance has been replaced.** It
  reported *ready* on a process whose database file had been swapped out from under it — the
  process keeps its handles on the old file, so its reads succeed against data nobody else can
  see and every probe answers 200. Somebody confirming a restore had worked was told it had. It
  now compares the instance identity it started on against what the database says and answers
  503 when they differ, naming both.
  **A `db restore --as-clone` will make a running service report not-ready, and that is
  correct** — a clone is deliberately a new instance, so the process is serving something that
  is no longer what your agents and configuration refer to. Restart it. A `--recover` restore
  keeps the identity and changes nothing. This is not a substitute for stopping the service
  before restoring: it can only see a replacement that changed the identity.

- **The trash now holds work you had finished before you deleted it.** `subroutine list
  --trash` and `GET /v1/tasks?deleted=true` left out anything already marked done or cancelled,
  because a listing hides finished work unless you ask — so an item you completed and then
  deleted was readable by its number and appeared in no listing at all. Measured on a real
  instance: three of twenty-six. Asking what you deleted is a question about deletion, and what
  the item's status happened to be is no part of it. `include_completed=false` still narrows the
  trash if that is what you want.

- **A filled label shows its word.** The type and status labels added above rendered as blank
  grey lozenges in both themes — the rule named a colour that does not exist, so the browser
  dropped it and the text took the colour of its own background. Fixed before anybody upgrading
  would have met it.

- **A claim now reads as the state it is.** A row said `si is on it`, and an expired claim said
  `si left it` — a past event drawn as though it were a property of the item. It says
  `claimed by @si` while somebody holds it and nothing once the lease has run out, which is what
  the terminal and the agent have always done. The agent's own wording moves from `held by` to
  `claimed by` to match, so all three surfaces use the word the `claim` command uses.

- **An item that is blocked no longer says so twice.** `blocked` is both a status somebody sets
  and a state worked out from what an item is waiting on, so a card could carry two identical
  labels meaning two different things.

- **Setting a deadline to *today* now puts it on today.** A task whose deadline you changed had
  its dates read in the timezone it was *created* in, whoever changed them and wherever they
  were — so somebody in London setting *due today* on a task filed in UTC stored the end of a
  day that had already finished where they were standing. The item then sat under *Next 7 days*
  while its own row said *due today*: one screen, two answers, and nothing to say which was
  right. Deadlines, starts and defers are now read in your own timezone, per the documented
  order — an explicit one, then yours, then the workspace's, then the instance's — and the task
  records the zone the date was actually written in, so what you are shown and where it is filed
  can no longer disagree.
  **Nothing is rewritten behind you**: a task keeps its zone until somebody changes one of its
  dates, so editing a title from another country leaves every date on it reading exactly as it
  did. Existing deadlines are untouched and will read as they always have until they are next
  changed.

- **Making a start date a whole day is now in the item's history.** Switching a start from a
  time to a whole day, where the two happen to be the same moment, changed the item and left no
  record of it — so a change feed said nothing had happened while anybody holding the older
  version was quietly refused.

- **A search at the terminal no longer drops its best matches.** `subroutine search` fetched
  more rows than it was going to show, then cut them down using the *wrong* order — by date
  rather than by how well each one answered — before ranking whatever survived. So the best
  match could be missing from a short page entirely, and only appeared once the page was big
  enough to hold every result. Measured on a real backlog: a search for *timezone* showing four
  results left out the single best one at every size below fourteen. Searches that show
  everything they found were always right, which is why this went unnoticed.

- **An agent searching now gets the same answer as the terminal and the browser.** Asking an
  agent for a page of results returned tasks first and then documents, each ranked within its own
  half — so on a short page a document that answered the question better than anything else was
  not merely listed late, it was missing. Measured on one instance at one moment: a page of four
  shared **one row** with the page a person got for the same words. Both kinds are now asked for
  in full and the merged answer is cut to the page, which is what the terminal has always done
  and what the browser does — so the best answers come first whichever kind they are.
  Ordinary listings for an agent are merged the same way, for the same reason.

- **Search says what it does rather than listing where it looks.** The browser's search box said
  *"Search titles and descriptions"* and now says *"Search anything"*; an agent was told search
  covered *"titles and bodies"*, and the terminal's help named the title and what you had written
  about an item. All three had been out of date since search started reading comments, and none
  of them mentioned that typing a number finds that item. Nothing about searching has changed —
  what changed is three sentences that had quietly stopped being true, and they are worded now so
  that reading somewhere new does not make them false again.

- **`subroutine login link` works on a fresh install, instead of asking you to configure
  something first.** The README gives new self-hosters `subroutine serve` and then
  `subroutine login link`, and the second command refused unless `public_url` was set — so the
  quick start told you to do something that did not work, and the way out was a setting nobody
  had been asked to think about yet. Where the instance listens only to this machine, the link
  now uses the address `serve` prints, and says so: it works in a browser here, and if you
  reach this instance some other way — through a proxy, or from another machine — set
  `public_url` and make a fresh link. An instance listening beyond this machine with no
  `public_url` still refuses, because `0.0.0.0` is not an address anybody opens and only you
  can say which one they should. The same link over the API is unchanged.

- **`subroutine today` is now `subroutine agenda`, and the old name is gone.** **This is a
  breaking change**: `subroutine today` no longer prints anything, it says where the command
  went and stops. Scripts and aliases naming it need one word changed. It is the one thing in
  the product that was not already called an agenda — the endpoint, the client method and the
  browser's own page all were — and one name across every surface is what makes *they should
  all say the same thing* something you can rely on rather than something you have to check.
  A bare `subroutine` still prints it, as it always has.
- **Your agenda is counted in your own timezone on every surface, not in the timezone of
  whichever machine you typed on.** `subroutine agenda` used to send its own machine's zone to
  every instance it asked, so the terminal could be about a different day from the page and
  from what an agent was told — and on two connections in different zones the value sent
  matched neither of them. Every surface now lets the instance resolve your zone the same way:
  your account first, then the workspace, then the instance.

  **If your machine's zone differs from your account's, say which one you are in** —
  `subroutine user timezone Europe/London` — or your days will be counted where the server is.
  Two connections whose accounts disagree are now told about rather than quietly resolved to a
  third answer, and `--json` says which zone each one counted in.

  This does not change how a date is *written*: a day-scale deadline still reads as the day it
  was set for, because re-rendering a day through another zone would make it a different day.
- **`subroutine agenda` can be asked about another day, and how far ahead to look.**
  `subroutine agenda tomorrow`, `agenda friday`, `agenda next tuesday`, `agenda 2026-09-01`,
  `agenda +2w` — the same words `subroutine plan` and `subroutine defer` already take, so
  there is nothing new to learn. `--days 2` moves the look-ahead, so `agenda saturday --days 2`
  is the weekend and `agenda next monday --days 5` is next week without either needing a word
  of its own. A day other than today is named at the top, because anything already late shows
  under Overdue whether or not it was late on the day you asked about.
- **An offset can be written on its own** — `+2w` means `today+2w` — wherever a day is typed
  at the command line. `+` opens a project in a captured line, so it is the command line's
  alone and `subroutine explain dates` says so.
- **The agenda says how much dated work it is not showing.** A deadline further out than the
  next seven days was in no section at all — the undated pile takes only work with no dates,
  so anything dated left the view entirely and came back a week before it was due. The agenda
  is still a day view and still ends at that edge; what is new is that it tells you how much
  is past it, and names the listing that shows you. It says nothing when there is nothing.
- **The agenda is in the same order wherever you read it.** The terminal re-sorted every
  section after merging your connections, and it re-sorted on different keys from the ones
  the instance had used: by item number where the instance separates ties by age, and not at
  all where an appointment's start date is what orders it. So work with no deadline appeared
  in what looked like an arbitrary order, and the page and `--json` could disagree. The
  ordering is declared once now and both read it.
- **Relating two items from both ends no longer records it twice.** `Relates to` means the
  same thing whichever item you start from, so linking back from the other one stored a
  second identical row — and the item then showed the same line twice, with no way to tell
  them apart. Links that already exist are left as they are.
- **A machine where nobody has run `subroutine init` is told so, instead of filling the
  log with a stack trace.** Asking an agent a question before setting anything up wrote
  around 200 lines of traceback per message, ending in a database error — so the first
  thing a new user saw suggested something was broken, when the answer is one command.
  The API now answers the same sentence the terminal has always given.
- **The add and edit form can tell two projects with the same key apart.** A key only has
  to be unique among its siblings, so a workspace holding `substation/dist` and
  `websites/dist` offered two identical choices and filing into either was refused. The
  form sends the whole address now, and an item you are editing shows the project it is
  actually in.
- **`today`, `tomorrow` and `yesterday` mean the whole day, wherever you write them.** A
  deadline of `--due today` was stored as the very first moment of today, so it was
  already overdue by the time you read it back — while the same word typed into quick
  capture was read correctly. Every surface agrees now, and a deferral of `today` still
  means the start of it, because that is what the word means there.
- **A filter bound naming a day takes in the whole of it, as `subroutine explain dates`
  has always said.** `--filter created_at.lte=yesterday` stopped at the first moment of
  yesterday and so included almost none of it; a literal date in the same place already
  behaved correctly. The two spellings now agree.
- **Giving a task a deadline no longer takes it off the agenda.** On the web, dating
  something removed it from the page until the day it fell due — so the one edit you make
  to be sure of a piece of work was the edit that hid it. The agenda now shows the same
  seven-day look-ahead the terminal does. A deadline further out than that is still only
  visible in a listing, on every surface.
- **A very large number written after a `#` no longer refuses what you wrote.** A comment
  or a description that mentioned one — an order number, an identifier from another
  system — was rejected in full, and the message sent you off to check that the database
  was reachable. A number too big to be an item's own number is left as ordinary prose,
  which is what it is.
- **The masthead's home link takes the page home, not only the address.** Clicking
  **Subroutine** from a board narrowed to a project put `/` in the address bar and left the
  page exactly as it was — and the agenda then arrived at the board's full width. It also
  carried the board and the completed filter to an agenda that has neither; `/` is now `/`.
- **The workspace control says what is showing, and goes both ways.** On the agenda it marked
  a workspace selected while the page held every workspace — and a `select` fires no change for
  the option already chosen, so the one it named was the one it could not reach. It reads
  **All workspaces** there now, which is a real choice rather than a hint: picking it from a
  workspace goes back to the agenda.
- **The agenda at `/` says which workspace every row is in**, whether or not they all happen
  to agree — in the item's number as well as in the project label beside it. It named none
  when they did, which is the one page whose address names no workspace.
- **A row's address no longer runs into the title beside it.** On the agenda at `/`, where a
  row says which workspace it is from as well as the number, the address overflowed the fixed
  column it sits in and overlapped the title by ten pixels. That column is a floor now rather
  than a width, so an address of any length has room for itself.
- **An item opened from a board is read at the reading measure.** It inherited the board's
  uncapped width, because the frame asked which view was selected rather than whether a board
  was on screen — so the same item was full-width when opened from a board and correct when
  the address was refreshed.
- **A cancelled item at the end of a link no longer reads as done.** The browser marked any
  end with a completion date as `done`, and an item is given one when it is cancelled as well
  as when it is finished — so work somebody abandoned looked like work somebody had finished.
  Each end now shows its own status.

## 0.7.5 — 2026-08-17

> **This release changes the database schema**, to `c7d419e6a2b8`.
>
> Install it, then run `subroutine db upgrade`. That reports both versions, takes a
> verified backup, migrates and checks the result — in that order. Stop the service
> first if you are running one; expect it to be down for the length of the migration.
>
> It renames two columns, moves one field into another and adds one. **Nothing is
> reclassified**: anything that was deferred stays deferred and goes on behaving exactly
> as it did.

### Security

- **Everything a backup writes is owner-only, not just the backup.** The note recording how many
  rows were copied was written at whatever umask was in force, so a directory of `-rw-------`
  copies carried a world-readable file beside each one. It is row counts rather than rows, which
  is why this is small — but a backup directory where one file in two is readable by every
  account on the machine is not what the other half of it promises.

- **A credential sent in a URL is caught however it was capitalised.** `?token=` was refused by
  name, kept out of the access log, and answered with *treat that token as compromised*.
  `?TOKEN=` was none of those: the value was written to the log verbatim and the caller was told
  they had misspelled a parameter, which reads as a typo to correct rather than a secret to
  revoke. Query parameter names are case-sensitive in HTTP, and whether the server would honour
  one is a different question from whether a credential reached a log.

- **A credential that may write in one project can now only comment there.** `comment:write` has
  always been one of the five verbs a credential's write set narrows, and it was the one the
  check never reached — so a token issued `--write acme` could add to the record of every project
  it could *read*. Adding to somebody's record is changing it. This affects only credentials
  issued with `--write`; one issued without a write set reaches wherever it can read, exactly as
  before.

- **A database password is no longer written onto a command line.** `pg_dump` and `psql` were
  handed the whole URL, so for the length of a backup every process on the machine could read
  the password out of `/proc`. It travels in the child's environment now. Nothing changes where
  PostgreSQL authenticates over a Unix socket, which is the ordinary setup.

- **`subroutine db copy` survives a sub-task, a section or a sub-project.** Rows were inserted
  in whatever order the source returned them, and five tables here point at themselves — so a
  child arriving before its parent was refused by the database, and `add`, `add`, `move --under`
  was enough to make the SQLite-to-PostgreSQL migration permanently impossible. It also refuses
  a target holding tables it does not recognise, rather than reading somebody else's database as
  empty.

- **There is a limit on how large a request body this reads**, `max_body_bytes`, ten megabytes
  by default. `docs/errors.md` has described one since the first release and there was neither a
  setting nor a check, so a caller could stream as much as it liked at a route that would then
  try to parse it. Nothing anybody sends comes near it.

- **Nothing this API answers is cached by a proxy in front of it.** Responses carried no
  `Cache-Control` at all, and the one protection HTTP gives by default is for requests carrying
  `Authorization` — which the web UI's do not, because it signs in with a cookie. The app's own
  files are still cached, as they were.

- **A repeat that names a date which does not exist is refused.** "The 31st of February" was
  accepted, stored, and described back to you as `every day, on 31 February` — and asking when
  it next came round spent nearly three seconds of the server working out that it never does.
  A rare date is not an impossible one: 29 February, and the 31st of whichever months have one,
  are unaffected.

- **A page left open stops saying an item is deferred once it is not.** *Overdue* and *deferred*
  are worked out from the clock as the page draws, which is what keeps them right as time
  passes — but nothing redrew the page unless something on the instance changed, so on a quiet
  afternoon they were as stale as if they had been fixed when the page loaded. The poll redraws
  now. It asks for nothing extra to do it.

- **The workspace switcher tells a screen reader what it is.** It was the one control in the app
  with no accessible name — announced as a combo box and nothing else, while being the thing that
  decides which backlog you are looking at.

- **The web UI stops offering controls you are not allowed to use.** A read-only member — and
  every agent holding a narrowed credential — was shown Edit, Complete, the status and assignee
  controls, the comment box, the link box and Remove, and every one of them was refused when
  pressed. `/v1/me` has always said what you may do; nothing read it.

- **You can sign out of the browser**, and a session that lapses while you are reading now says
  so. The endpoint has existed since sessions did and the page offered no way to reach it; and
  the ten-second poll swallowed the refusal, so the page went on re-rendering the same rows for
  ever while every control on it quietly failed.

- **A refused write no longer takes what you typed with it.** The capture box, the link box and
  the comment box all cleared themselves the moment you pressed enter, while the request was
  still in flight — so a permission refusal, a conflict or a dropped connection reported the
  failure over a box that was already empty. They clear when the write has landed and not
  before.

- **A Markdown table's column alignment works.** It was written as an inline style, which this
  app's own security policy blocks, so an aligned column arrived left-aligned with a complaint
  in the browser console.

- **`subroutine changes` no longer prints the database at you.** A deferred task read
  `(snoozed_is_all_day, snoozed_until)` and the rows `init` writes read `created
  workspace_member`. It says what changed in the words you would use — "when it comes back",
  "your account" — and every column an event can name is now held to that by a test.

- **`subroutine explain scripting` no longer promises a boundary it cannot hold.** It said a
  token set locally applied "the same limits as would apply over the network". Your work
  commands do obey it; the `db` commands open the database directly so they still work when the
  service will not, and nothing on this side of the file could change that. The page says so,
  and names what does hold the boundary.

- **A task's title cannot repaint your terminal.** Rich's markup was already neutralised in
  titles; ANSI escapes were not, so a title carrying `ESC[2K` cleared the line above it when
  anything printed it. Titles arrive from other people, from agents, and from instances merged
  across connections. They are printed as text now, everywhere the command line prints.

- **A connection reached over plain `http` says so** — when you add it, in `subroutine
  connections`, and in `subroutine doctor`. `serve` refuses to *be* the other end of that, in
  as many words; the client stored the token and sent it on every request without a murmur.
  Loopback is unaffected, because nothing leaves the machine.

- **The sign-in confirmation page sends no referrer.** It is the one page whose own address
  carries a live sign-in link, and the instance's ordinary `same-origin` policy meant the
  stylesheet it loads arrived carrying it. That request goes to the instance itself and the
  value is redacted from its log, so nothing leaked to a stranger — but a header that need not
  carry a secret should not.

- **A mistyped project key no longer lists projects you cannot see.** Filing with `+nosuchkey`
  answered "Projects here: …" with every project in the workspace, private ones included — so
  an ordinary member could learn the name and the existence of a project they are not in, by
  making a typo. Filing *into* one was already refused, so nothing could be written where it
  should not be; what leaked was the list.

- **The token you give the local plugin now decides what that session may do.** It was read by
  nothing. `SUBROUTINE_TOKEN`, `SUBROUTINE_TOKEN_<NAME>` and `credentials.toml` were all
  ignored on a local connection, so the same `--scope task:read` service account was
  `claudebot (agent) … Narrowed to scopes task:read` at the terminal and `si (person) …
  instance:admin` over `subroutine mcp` — where a write the command line refuses succeeded.
  The plugin advertises the field as *"if you want it to have less access than you do"*.

  **If you set no token, nothing changes**: reaching the database is still the authentication
  on your own machine, which is what a standalone install relies on.

  **If you set one, it is now in force.** A stale, revoked or mistyped credential that was
  previously ignored — leaving the session with your own authority — will be refused by name.
  That is the point of it, and it is the one thing worth checking before you upgrade an
  installation whose plugin has that field filled in.

- **An agent can no longer be handed a credential in a tool result.** `subroutine_call_api`
  would reach `POST /v1/tokens`, which answers with a secret that exists nowhere else ever,
  and `POST /v1/login-links`, which answers with a working sign-in URL and accepts a username
  so it can be minted for somebody else. A tool result is text in a model's context. Both are
  refused now, naming `subroutine token create` and `subroutine login link` instead.

- **A title cannot carry a heading into the document an agent is told binds it.** Titles held
  interior newlines, and `subroutine://conventions` renders each decision as a list item — so
  anybody able to write a document could plant prose that read as the resource's own. A title
  is stored on one line now, as it has always been described. Comments are unaffected: their
  paragraphs are prose somebody wrote deliberately.

- **A link in stored prose can no longer name another host by writing it with backslashes.**
  The browser refused `//evil.example/x`, which reads like a path and is not one — but a
  backslash is not a slash to that check and is one to a browser. `/\evil.example/x`,
  `\\evil.example/x` and `\/evil.example/x` were all rendered as ordinary links to this
  instance, and all three resolve to `https://evil.example/x` when clicked.

  Anyone who can write a description or a comment could plant one, on somebody else's item,
  and it reads to the eye as an internal path. A destination is now normalised before it is
  judged, so the two spellings cannot disagree. A single leading backslash is still a link:
  it resolves to a path on this instance and nobody else's.

- **`serve --host` now configures the instance as well as the socket.** The flag reached
  uvicorn and stopped there, so an instance bound to `0.0.0.0` was still *built* as though it
  were on loopback. Two things read that and both fail open: the limiter that bounds credential
  guessing was switched off, and `/readyz` was willing to hand an unauthenticated caller the
  raw database error — an internal hostname, a database name, a filesystem path.

  Measured: 40 wrong tokens against a wide bind produced 40 refusals and no rate limiting.
  They now produce 30 refusals and then `429`. Setting `SUBROUTINE_HOST=0.0.0.0` was always
  safe; it was the flag — the form the command's own help suggests — that was not.

  If you bind beyond loopback and have not set `rate_limit`, limiting is now on where it
  previously was not. `rate_limit = false` turns it off deliberately.

- **`X-Forwarded-For` is no longer believed from any process on the same machine.** The
  documented rule is that an empty `trusted_proxies` ignores the header entirely, and this
  application implements it correctly — but uvicorn was rewriting the client address from that
  header before the application ever saw the request. So a caller could put any address in it
  and be counted against a bucket of their own choosing.

  Measured: 40 failed authentications from one machine, each with a different forged header,
  were refused **none** of the time before and are refused as one caller now.

- **A token scoped to particular permissions is now narrowed when it reads, not only when it
  writes.** `task:read`, `project:read` and `workspace:read` were checked nowhere at all, so a
  credential issued `--scope task:delete` could read every task, document, agenda and change
  feed it could reach — while `GET /v1/me` truthfully reported the single permission it held.
  Every read-narrowed credential was wider than it was issued.

  **This can refuse a credential that worked yesterday**, which is the point of it: if you
  issued a token with `--scope` and left a read verb out, it will now be told so by name, with
  the verb it needs. Reissue it with the verb, or with no `--scope` at all, which has always
  meant *as wide as its owner*. A token issued without scopes is unaffected.

  Two verbs that could never have been enforced are recorded as such rather than left looking
  like controls: `tag:write`, `status:write` and `link_type:write` gate nothing because no
  surface can add a tag, status or link type; `workspace:delete` gates nothing because nothing
  deletes a workspace. Each entry says what would remove it, and a test fails the build if a
  permission is added and checked by nothing — or if one of those gaps is closed and the note
  is left behind.

- **Managing who belongs to a workspace needs the permission named for it.** Adding and
  removing members checked `workspace:admin` where both published descriptions of `user:admin`
  say that is its job. No role changes hands — every role that holds one holds the other — but
  a token scoped to `user:admin` could not administer membership and one scoped to
  `workspace:admin` could, which is the wrong way round.

- **Restoring a backup can no longer run commands, and a file has to prove it is a backup.**
  `subroutine db restore` handed a PostgreSQL dump straight to `psql`, which executes backslash
  commands written inside a script — so a file that reached your backup directory could run
  anything as the account doing the restore. `docs/hosting.md` recommends putting backups on a
  shared network volume, which is exactly where somebody else can write one.

  A dump is now read before it is used, and a command `pg_dump` does not write is refused by
  name and line number. The scan follows the file the way `psql` does: inside a `COPY` block a
  leading backslash is ordinary data, so real dumps — which contain hundreds of those — are
  unaffected. `psql` is also run with `--no-psqlrc`, so your own start-up file is not executed
  as a side effect of restoring somebody else's copy, and with `--single-transaction`, so a
  dump that fails part-way leaves nothing behind rather than half a schema and no data.

  Separately, **a restore checked one string**: whether `alembic_version` held a schema this
  version could read. A 12 KB file holding that one table and nothing else was accepted,
  written over the live database, and reported as a success — after which the instance could
  not be read and the error blamed your `database_url`. A backup must now contain the tables
  every Subroutine database has.

- **A backup that is the right size and unreadable is no longer reported as good.** Verification
  compared the length and re-read the schema version, and both of those survive a copy that lost
  pages in the middle. The pages are now checked too, and a copy that fails is deleted rather
  than left looking usable.

### Changed

- **`POST /mcp` refuses a protocol revision it cannot speak**, with `400` and a new error code,
  `unsupported_protocol_version`. The Streamable HTTP transport requires it and the header was
  read by nothing, so any value at all was answered as though it were understood. A missing
  header still means `2025-03-26`, as the transport says. Measured against Claude Code before
  building it: everything it sends after the handshake is the revision it agreed with us, and
  the one request this refuses — its new-era discovery probe — it already knows how to fall
  back from.

- **An appointment and a deferred task are different things, and are named differently.**
  `start_at` meant two opposite things — *this begins at* and *do not show me this until* —
  and everything that read it took the second. So `Dentist on Monday at 2pm` was filed as a
  defer: hidden from your list, hidden from `--ready`, and reported as *"1 thing put off until
  later"*, right up until two o'clock on Monday — which is the moment it stopped being useful.

  There are two fields now. **`starts_at` says when work begins** and hides nothing; it carries
  a time, so an appointment can say two o'clock. **`snoozed_until` hides the row** until it
  passes, which is what `subroutine defer` has always meant. **`planned_for` is gone, absorbed
  into `starts_at`** — *planned for Tuesday* is *starts Tuesday, all day*, the same fact with
  one less field to choose between.

  **This is a breaking change and there are no aliases.** Over HTTP you send `starts` and
  `snooze` where you sent `planned_for` and `start`, and you read back `starts_at` and
  `snoozed_until`. The old names are refused rather than quietly accepted: a caller that meant
  *hide this* and silently got *show this* would not find out until something they were relying
  on being hidden turned up on their list.

  Two smaller consequences worth knowing. A date filter no longer takes `eq` — `starts_at` is
  an instant, so *what starts today* is `starts_at.gte=today` with `starts_at.lt=tomorrow`.
  And the browser's date pickers are three of a kind now: the one that used to offer no time
  was the field that could not hold one.

  `subroutine plan`, `subroutine defer` and the capture line are unchanged.

- **A row says what kind of thing an item is, with an icon.** It used to say `Task` or
  `Document`, which is its shape rather than its subject — so a bug, a chore and a decision all
  looked alike until you opened one. Each now carries its own name and glyph.

  A type this instance's copy of the app does not recognise still gets a chip, with its name and
  a neutral glyph — item types belong to a workspace, so the pictures are this app's opinion and
  the words are yours. Blocked work and the item blocking it carry a glyph too.

- **The web UI's controls are one of three sizes, and focus the same way wherever they are.**
  Buttons, inputs and dropdowns had accumulated thirteen different paddings between them, so two
  controls side by side were rarely the same height — and the *Add* button drew a different focus
  outline from every other control on the page, which is the one piece of styling a keyboard user
  depends on.

  Controls also respond to hover and focus with a short transition now, and **the page honours a
  system request for reduced motion**, which had nothing to switch off until there was motion.

- **A row gives its whole width to the title, and puts its properties and actions in the same
  place every time.** *Complete* used to sit beside the row and take width from it down the
  card's whole height, so titles wrapped to three lines while the space next to the button stood
  empty — on a board card, over a third of the width. The title now spans the card, with every
  property and then every action on the line beneath it, the same way on the list, the board and
  the agenda.

  **One thing behaves differently**: a row's chips and its date are no longer part of the link,
  so clicking one does not open the item. The title spans the card and wraps to as many lines as
  it needs, so the area that does open it is larger than it was.

- **A listing shows a project's name rather than its key.** A row's chips read
  `Subroutine` and `Web UI` where they used to read `subroutine` and `ui` — every other chip on
  a row is something written for a person, and the project was the one address among them. The
  command line still shows the key, deliberately: there it doubles as what you would type next.

- **An item holding up other work is now marked `blocker` everywhere**, where the listings said
  *holds up* and the item you opened said *Blocks* — one relationship with two names, met within
  a click. The mark an item carries and the link between two items are still worded differently,
  and deliberately: a mark says what an item *is*, a link says what it does to which other item.

- **The web UI's text sizes and spacing come from a fixed scale**, where before they had
  accumulated: eight text sizes and nineteen spacing values, none of them named. There are five
  text steps and nine spacing steps now, and nothing outside them.

  **A few things shift slightly.** Notices and messages are a little larger — they were smaller
  than the text around them, which is the wrong way round for a sentence telling you something
  went wrong; a few controls and the item metadata are a little smaller; and some padding moves
  by a pixel or two. Row height and the space between blocks are unchanged, deliberately, since
  those decide how much of a list fits on screen.

- **Every endpoint now refuses a query parameter it does not recognise**, instead of ignoring
  it and answering as though nothing were wrong. Listings have behaved this way since 0.3.0
  and the rest were added one at a time by hand — which meant single reads, both link
  listings and the backup listing had been left out.

  The refusal names the parameter and lists what the endpoint does accept, so it costs one
  call to fix:

  ```
  422  This endpoint does not accept 'workspace'.
       workspace: 'workspace' is not a parameter of this endpoint.
                  It accepts: fields, format, workspace_id.
  ```

  **This turns some requests that used to succeed into a `422`.** If you are sending a
  parameter an endpoint does not declare, it was being discarded before and you will now be
  told which one. Health checks, the sign-in page and the browser's own pages are exempt, so
  a monitor's cache-buster or a link carrying campaign parameters still works.

- **Reading one task, document, project or workspace refuses one too**, which is the case
  worth calling out separately: single reads were left out on the reasoning that ignoring a
  parameter there costs nothing.

  It costs the same as it does anywhere else, because a single read takes `fields` and
  `format` too. Measured against a real instance: `GET /v1/documents/4?fieldz=ref,title`
  returned **99,746 bytes** where the correct spelling returns 59 — the whole document,
  answered `200`, for one wrong letter.

  `GET /v1/me` takes no query parameters at all and now says so, rather than ignoring
  whatever it was given.

### Added

- **Nothing this API publishes points at anything you cannot look up.** Route descriptions in
  `/v1/openapi.json` — which answers without a credential — cited this project's own private
  tracker 51 times, so `PATCH /v1/tasks/{id_or_ref}` told you to consult a file nobody outside
  had and `POST /v1/login-links` cited an issue number. The reasoning those pointed at is now in
  the sentence beside them. A test holds every published surface to it.

- **The specification is published, at `docs/design.md`.** It is the design this was built from
  — data model, API, permissions, agent design — and the code cites it about two thousand times
  as `§7.3a` and the like. Until now those citations named a file nobody outside the project
  had, which is a poor showing for source you are meant to be able to read. It is frozen and
  wrong in places; the code is the truth, and design taken since is recorded as decisions rather
  than as edits to it.

- **A task can repeat, and finishing one occurrence brings the next.** Send a repeat when you
  file it — `every 14 days`, `every month on the 30th`, `every month on the last thursday`,
  `every year on 19 august`, or an RFC 5545 `RRULE` directly — and the rule is kept on a
  template while exactly one occurrence at a time sits in your list.

  **Two things qualify it.** *What the next date is measured from*: the rule's own grid, so the
  30th is the 30th however late you were, or the moment you finished, so "every 14 days" means
  a fortnight after you actually watered the plants. And *what brings the next one into being*
  — for now that is finishing this one; the half that happens whether or not you act, which is
  what birthdays and standing meetings need, is refused by name and lands with the calendar.

  `POST /v1/tasks/{ref}/skip` and `subroutine skip 42` let one go by. **Cancelled rather than
  done, deliberately**: both end the occurrence and both bring the next, but a series recorded
  entirely as done cannot tell you how often you actually skip it.

  A phrase this cannot read is refused and says which part was unreadable, rather than being
  stored as a rule nobody re-reads — a misread deadline is one wrong day, a misread repeat is
  a wrong day for ever.

  **You can write it in the line or set it precisely.** `subroutine add "Pay the rent on the
  30th of every month"` reads it out of the sentence; `--repeat "every month on the 30th"`
  says it exactly. The second is what a captured line can never do, because a line is typed
  once: `subroutine update 42 --repeat "every other tuesday"` changes how something comes
  round, `--repeat ""` stops it, and `--repeat-from completion` measures the next one from
  when you finished rather than from the grid.

  **A repeat belongs to the series, so changing it changes every occurrence after this one.**
  Stopping keeps the work in hand — it holds its number and its history, and nothing follows
  it.

  Agents get the same two halves: a repeat written into `subroutine_add`'s line, and `repeat`
  on `subroutine_update` to change it or stop it. Every listing row now says how often
  something comes round, on both surfaces — *due Thursday* on something fortnightly is a
  different statement from *due Thursday* on a one-off.

  **In the browser it is a *Repeats* section on the form**, closed by default and open on an
  item that already repeats. Type how often, and the page shows you what it understood along
  with the next few dates — *every other tuesday* comes back as *every other week, on Tuesday*,
  in different words on purpose, because that is what tells you it read the phrase the way you
  meant it. *Every month on the 30th* and *every 30 days* look alike and are not, and the
  difference does not show up until February.

  Emptying the box stops the repeat, the same way emptying any other box on that form clears
  what it holds.

  **A repeating item says so wherever you meet it.** A list and a board mark it *Repeats*; the
  item itself spells out how — *every 3 days, from when it is done* — and so does the form when
  you open it, before you have typed anything. That sentence is always generated from the rule
  that was stored, never from the words you wrote, so it is a check on what will actually happen
  rather than an echo of what you asked for.

  `GET /v1/tasks/{ref}/occurrences` answers *when does this come round*, over a stretch you
  name — `?until=2026-12-31`, or `?until=%2B3+months`. It computes rather than stores, so a
  birthday stays one item for ever instead of becoming one per year, and asking costs nothing
  and changes nothing. It is what a calendar view will draw from.

- **The web UI has a light and dark theme you can choose**, in the footer. Three settings —
  *Match system*, *Light*, *Dark* — and it starts on *Match system*, which is what the page did
  before. The point of the other two is the case the system setting cannot cover: a machine set
  to dark, and a person who wants this page light anyway.

  **The choice is remembered in your browser, not on your account**, so it does not follow you
  to another machine — deliberately, because a laptop in a dark room and a desktop by a window
  can reasonably want different answers, and *Match system* already gets that right per device.
  It needs no server setting and no upgrade to anything.

  **The page also honours a system request for more contrast**, raising the text and border
  colours to the stricter of the two accessibility levels. Nothing moves and nothing appears;
  it is a stronger version of the same design.

- **The backlog can be asked what is short.** An estimate is now something you can sort by
  and filter on, so *"what is small and not blocked"* is one question:

  ```console
  $ subroutine list --ready --order estimate_minutes --filter estimate_minutes.lte=2h
  ```

  The filter takes the same grammar as `~2h` in a captured line, so `30m`, `1h30m` and a bare
  number of minutes all work, and every comparison is available — `lte`, `gte`, `eq` and the
  rest. `GET /v1/tasks?estimate_minutes.lte=2h&order=estimate_minutes` over HTTP.

  **A task nobody has estimated sorts last in both directions.** Ascending means shortest
  first and an unestimated task is not known to be short; descending means longest first and
  it is not known to be long.

  An estimate has been read off a captured line, rendered by three surfaces and published in
  `/v1/meta` since the beginning, and answered no question until now.

- **An item can be moved under another one, or back to the top level.** `subroutine move 42
  --under 7` makes #42 part of #7; `subroutine move 42 --top` takes it back out. Its own
  parts travel with it.

  ```
  POST /v1/tasks/{ref}/move       {"parent": 7}     or  {"parent": null}
  POST /v1/documents/{ref}/move   {"parent": 7}     or  {"parent": null}
  ```

  A task could be given a parent when it was created and never afterwards, so a subtask
  could not be promoted or moved. **A document was worse**: its parent was reported by every
  response and accepted by no endpoint at all, so nesting existed in the schema and could
  not be reached from outside.

  Moving something under itself, or under one of its own parts, is refused. So is a move
  that would push any part of what travels with it past the nesting limit — checked against
  the deepest thing carried, not against the item named.

  **A parent in another project is refused**, because a subtask belongs to its parent's
  project. The refusal names both projects and the command that moves it there first.

### Fixed

- **Deferring something until six in the morning now means six in the morning.** `subroutine
  defer 42 "2026-08-18 06:00"` accepted the time, said *"Hidden until Tue 18 Aug"*, and stored
  midnight — so the six hours were read, discarded, and not mentioned, and the confirmation was
  the same sentence a working command would have printed. The field carries a clock everywhere
  else: a captured line writes one, the API accepts one, and your list reads it to the minute.

  It is honoured **when you write one**, and not otherwise. `friday`, `2026-08-18` and
  `today+2w` all still mean the whole day — an expression is measured from now, so keeping its
  clock would hide something until whatever time you happened to be typing.

  `subroutine today` still shows a deferred item from the start of the day it comes back on,
  where your list waits for the hour. That is deliberate: an agenda answers what today is about,
  and something arriving at six is part of today from the moment it begins.

- **The number a repeating task gives you for its series now opens the series.** Every occurrence
  of a repeat reports `recurrence_template_ref`, and asking for that number answered *"There is
  no #1"* — while the same request over HTTP returned it perfectly well. So an address this
  program handed out was denied by the program that handed it out, and the advice attached sent
  you looking in the trash for something nobody had deleted.

  The series is where a repeat's history lives: when it was set up, and every time somebody
  changed the rule. `subroutine show`, its links, its comments and its sub-tasks all reach it
  now, on every surface. **A listing still leaves it out**, which is deliberate — a rule is not a
  piece of work, and putting it in your list would show you the same title twice.

  A series and one of its occurrences share a title, so both now say **the repeat itself** on the
  one that is the rule.

- **A failure read back over HTTP is the one a local call would have raised.** The exception was
  rebuilt from the status code, and four of ours share `409` and four share `422` — so a client
  catching `SchemaMismatch` caught it against its own database and missed the identical failure
  from a server. The `code` was right throughout, which is why every message read correctly.
  Nothing you send or receive changes; what changes is which exception this project's own client
  raises.

- **A field-level error carries only codes this API publishes.** One route sent `required`, which
  is in no registry — so it appeared on the wire under a contract that does not define it, and
  this project's client dropped it on the way back in. It is `missing_field`, which is the
  registered code that means that.

- **Two connections that turn out to be one instance no longer stop the commands that cannot
  double-count.** `add`, `done`, `update`, `claim`, `comment`, `plan`, `defer`, `use` and every
  `project`, `user` and `workspace` command refused outright — including on a machine whose own
  local list was working perfectly well. Only a read that *combines* connections can count one
  instance twice, so only those refuse now: `today`, and any listing or feed asked for as one
  merged sequence, including every `--json` one. `list` and `changes` go on showing a heading
  per connection, which is what makes the duplicate visible rather than hidden.

  It matters most exactly where it used to bite hardest: copying an instance and verifying the
  copy before cutting over, and running two identities against one server from one machine.

- **A credentials file anyone can read is now mentioned by commands you actually run.** The
  warning existed, promised "any command that reads a token from the file", and was produced by
  exactly one — which is hidden from `--help` until you have a second connection.

- **`subroutine add` resolves dates in your timezone rather than each instance's.** `subroutine
  today` has always done so, and says why: two instances configured for different zones would
  otherwise file two different Fridays into one merged list. A `default_timezone` that is not a
  real zone is now refused by both, where `add` used to ignore it.

- **`starlette` is a declared dependency now.** Nineteen modules here import it directly and
  nothing named it, so the version you got was whatever FastAPI last chose. Nothing changes for
  an existing install; it means a resolver can no longer hand you one these imports do not
  exist in.

- **`SECURITY.md` no longer tells you to run a command that was renamed**, and four pages that
  counted the ways to reach an instance no longer say five when there are six.

- **A document's owner must be somebody who is in the workspace.** Writing one with an
  `owner_id` naming an account that does not exist answered a bare 500; naming a real account
  outside the workspace answered 201 and left the document owned by somebody who cannot see it.
  Changing the owner later checked that the account existed and not that they were a member.
  All three now answer the same way, naming the field — which is what assigning a task has
  always done.

- **Restoring something whose place was taken says so instead of failing.** A project's key can
  be reused once the project is in the trash, and a document can be superseded by something
  else while it is in there — so restoring the original hit a constraint and produced a 500
  over HTTP and a traceback at the terminal, from three ordinary commands. Both now refuse with
  what took the place and what to do about it.

- **An agent can name the other end of a link the way this program prints it.** `subroutine_link`
  published that its `other` argument took `#42` as well as `42`, and then refused the first —
  with a message telling the caller to "pass the number in the listing", which is the value that
  had just failed. Every other ref on that surface had always taken both.

- **Reading one very large item no longer spends a whole context window.** `subroutine_show` had
  no ceiling, so a 200 KB document came back in full — around fifty thousand tokens from a tool
  an agent uses to *check* something. The body is trimmed now, and says where it was cut and
  where to read the rest.

- **A search containing a backslash no longer fails.** On an instance using the indexed
  search backend, `C:\Users` or `re:\d+` — a Windows path, a regular expression — came back as
  a 500. Where the text happened to survive, the search was quietly wrong: a stray `E` arrived
  as one of the words being searched for. A search carrying a NUL byte failed the same way on
  both backends and now simply finds nothing.

- **A blocker in a project you have thrown away no longer holds work up.** Deleting a project
  leaves its tasks where they are — every listing hides them by joining the project — so one of
  them could go on blocking live work from outside every listing there is. The item was marked
  blocked, `--ready` skipped it, and opening it showed **no links at all**, because a link with
  an end you cannot see is not shown. Restoring the project was the only cure, and nothing said
  so. If the project comes back, so does the block.

- **A ring of blocking links is refused, and the refusal names the ring.** Nothing stopped you
  recording that A blocks B, B blocks C and C blocks A — after which none of the three could
  ever be started, every item was correctly reported as blocked, and no surface said why.
  `docs/errors.md` has listed `cycle_detected` as covering "a chain of blocking links" since
  the first release; now something raises it. `relates_to` is unaffected: it says two items are
  connected, not which comes first.

- **"Next Friday" said at a weekend is no longer a week late.** `next <weekday>` took the
  soonest such day and added a week, so once that day had passed it skipped a week entirely —
  "next Monday" said on a Tuesday was a fortnight away. It is now the named day of the week
  after this one, which is the same answer as before wherever the old one was right.

- **Five endpoints ignored the version you asked them to check.** `If-Match` and
  `expected_version` were read by `PATCH`, `DELETE`, `complete`, `skip` and `restore`, and
  silently dropped by `claim`, `release` and all three `move` endpoints — so a caller doing
  read-modify-write properly was answered `200` for a change it had asked to have refused.
  They honour it now, and answer the same `409` and `version_conflict` as everything else.
  Sending no version is unchanged, and is still the default.

- **`Retry-After` no longer sends you back too early.** A caller refused with a `429` was told
  how long to wait, rounded *down* — so waiting the 8 seconds it asked for when it needed 8.6
  earned a second refusal. It is rounded up now, and still never less than a second.

- **A `405` names every method the path takes.** `PUT /v1/tasks` answered `Allow: POST` and
  "This path accepts POST", with no mention of the `GET` beside it — twenty paths here accept
  more than one method, and each of them named one. If you are mapping this API from its own
  refusals, they are worth re-reading.

- **`HEAD` works wherever `GET` does**, which for most people means `HEAD /healthz`: a load
  balancer using the commonest default check there is was told `405 Method Not Allowed` and
  reported a perfectly healthy instance as down. A path that accepts no `GET` still refuses,
  and says what it does accept.
  refused one that was already stale, which is the common case and still works — but it
  compared the number against the row as read *in that request*, and nothing stopped two
  requests reading the same number. Both passed the check, both wrote, and one person's edit
  was gone with nothing reported anywhere.

  **The count made it invisible.** Two writes left the row at version 2 rather than 3, so a
  caller who read version 1 and now saw 2 concluded that exactly one change had happened. The
  mechanism built to report a lost update was concealing it.

  Every change to a task, project or document is now written under the version it was read
  at, so the database itself refuses the second one. It arrives as the same `409` and the same
  `version_conflict` a stale version has always earned — read it again, apply your change to
  the current version, and send it again. Nothing else changes: a change sent without a
  version is accepted exactly as before.

- **An agent's first write after a pause no longer fails with "database is locked".** Every
  write through `POST /mcp` failed if the credential had not been used for a minute — which is
  to say, whenever an agent stopped to think. The request recorded that the credential had been
  used, held that write open for the length of the call, and then blocked against itself when
  the tool went to do the work. After five seconds it gave up and blamed the operator's
  `database_url`.

  Measured on a served SQLite instance: a write took 0.04s on a credential used seconds
  earlier, and 5.04s and failed on one idle two minutes. It now takes 0.03s after ninety
  minutes idle. SQLite only — PostgreSQL locks the row rather than the file — but SQLite is
  the default.

- **A hyphenated word no longer loses its second half to the date grammar.**
  `subroutine add "Ship the add-on tomorrow"` filed a task called `Ship the add-`: the `on`
  inside `add-on` was read as the word that introduces a date. `stand-by`, `sign-on`,
  `hands-on` and anything else ending in `on`, `by`, `due`, `from` or `before` did the same.
  The date still reads — only the word stays whole.

  The same rule was wrong at the other end, so `Ship it by tomorrow's deadline` filed
  `Ship it 's deadline`. Both are fixed, and an ordinary `due friday,` or `by 2026-08-19.`
  reads exactly as it did.

- **A time you typed is no longer reported as a repeat you got wrong.** `Email Bob re: 3pm`
  answered *"not a repeat this understands"* and listed the recurrence forms — about a string
  nobody offered as a repeat. It now says what would have made it a time, and a phrase that
  genuinely was an attempt at a repeat is still told so.

- **A repeat written in lower case reads back properly.** `freq=weekly;byday=mo` was accepted
  and stored as typed, then described back as `every ` — nothing after the word. Rules are
  stored in one form now, so the sentence you are shown to check against what you meant says
  `every Monday`.

- **`subroutine explain capture` said repeats were not read yet.** They have been since the
  previous release. The page now lists `every <phrase>` with the rest of the grammar and says
  what happens to a phrase it cannot read.

- **A change to the web UI reaches your browser on the next load.** Its files were served with
  a five-minute cache and nothing to check them against, so after updating an instance you could
  be looking at the previous version's styling with no way to tell — and once the five minutes
  were up, the browser re-downloaded every file in full rather than asking whether it needed to.

  Each file now carries a tag derived from its own contents. A browser asks once per load and is
  told *unchanged* in a couple of hundred bytes, or handed the new file. No hard refresh, and
  nothing to clear.

- **The faintest text in the web UI is now readable.** The item number on every row, the
  timestamps beside a comment, the placeholder in every box and a dozen other things were drawn
  in a grey that measured **3.31:1** against the page in the light theme, where the accepted
  minimum for text that size is 4.5:1. It is darker now, and the dark theme's equivalent — which
  missed the same line by 0.02 on one of its three backgrounds — has moved with it.

  Nothing else in either palette changes, and both themes are checked by arithmetic rather than
  by eye from now on, so a future colour cannot quietly drop below the line.

- **Naming a workspace with the wrong key no longer produces an error about something else.**
  `GET /v1/tasks/1?workspace=personal` discarded `workspace` unheard, and the request was
  then refused for naming no workspace — describing a request nobody had sent, and listing
  the workspaces by name and id, which reads as a menu of values rather than as a missing
  key. It now names the parameter: `workspace_id`, and what the endpoint accepts.

- **A refusal listing the workspaces you can reach now says that either the name or the id
  will do.** It printed `projects (019fad98-…)` and left the reader to guess what the bracket
  was for.

- **An API token sent in the URL is now always refused with the warning that it is
  compromised.** Tokens have never been *accepted* from a query string, and the refusal has
  always been meant to say so — but it only ever reached one caller in four.

  A request that also carried a valid `Authorization` header was answered `200`, with the
  secret sitting in the URL and nothing said about it. A request to any endpoint that
  refuses unknown query parameters — every listing — was told `'token' is not a parameter of
  this endpoint`, which reads as a typo, so the sensible next move is to fix the spelling
  rather than to revoke anything.

  **If you have ever sent a token as `?token=`, `?api_key=`, `?apikey=`, `?access_token=` or
  `?auth=` against an instance, treat that token as compromised and revoke it** — it will be
  in the access log of that instance and of any proxy in front of it, whatever it was
  answered at the time. `subroutine token revoke` and issue a new one.

## 0.7.1 — 2026-08-14

**0.7.0 was tagged and never published.** Its release commit renamed the `Unreleased` heading, as
every release commit does — and the guard added a day earlier read that as *nothing was written
down* and failed, on all four Python versions and in the release workflow, so nothing was built
and nothing reached PyPI. Everything 0.7.0 was going to contain is below and ships as 0.7.1.
`v0.7.0` remains a tag with a red build because that is what happened; there is no 0.7.0 to
install and no release page for one, and the same is true of `v0.6.2` further down.


> **This release changes the database schema**, to `a3f9c21d7e40`.
>
> Install it, then run `subroutine db upgrade`. That reports both versions, takes a
> verified backup, migrates and checks the result — in that order. Stop the service
> first if you are running one; expect it to be down for the length of the migration.
>
> It adds indexes and changes no data, so on any instance short of a very large one it
> is a matter of seconds. On SQLite the second of the two migrations does nothing at
> all: the indexes it creates exist on PostgreSQL only.

### Added

- **Search can be served by an index, on PostgreSQL.** Set `search_backend = "native"` in
  `config.toml`. Measured at 20,000 tasks, a search that matches nothing — which is the
  slowest kind, and the one you run to check whether something already exists — goes from
  **119 ms to 1 ms**.

  **It is off by default and nothing changes until you turn it on.** On SQLite it is not
  available at all: asking for it there is not an error, you simply get the existing
  implementation. `GET /v1/meta` reports which one is answering, as `search_backend`.

  **The two find different things, which is why this is a setting and not a quiet
  improvement.** With the index on:

  - Searching `seed` finds *seeded* and *seeding*, and `paginate` finds *pagination*. Words
    are matched by their root rather than letter for letter.
  - A partial word still works from the start: `curs` finds *cursor*.
  - **A partial word no longer works from the middle**: `ursor` will not find *cursor*. This
    is the one thing the index cannot do, and it is inherent rather than an omission.

  **A very common word may stop narrowing a search** under the index — `the`, `of`, `and` and
  the rest are dropped rather than required, so `cursor the` finds what `cursor` finds. The
  default backend requires every word you type.

  If you rely on matching the middle of a word, or on every word narrowing, keep the default.

- **Search reads comments.** A search now finds an item when the words are in a comment on it,
  as well as in its title, description or body. This changes what a search *finds*, not how
  fast it finds it, so it is worth knowing about rather than being a quiet improvement: a query
  that returned nothing yesterday may return something today.

  **This one is not opt-in and does not need PostgreSQL** — it works on both backends and on
  every installation.

  Comments are where the running record of a piece of work lives, and on a working instance
  there are usually more of them than there are items — so the previous behaviour was
  answering "nothing matches" about the largest body of prose it could have looked in. That
  landed hardest on the one question search exists to answer, *does this already exist*.

  A deleted comment does not surface its item, and a comment is readable exactly when the item
  it hangs off is, so this reaches nothing a caller could not already read.

- **A row says why it matched.** The `matched` column beside a search hit now says `comment`
  where the words were in a comment, and `number` where the row is the item whose ref you
  typed. Previously both produced a row with an empty reason, which reads as a broken search
  rather than an answer.

- **A number typed into search finds the item with that number.** Searching `862` now returns
  `#862` itself, as well as everything whose title or description mentions those digits.
  `#862` works too, and so does either form over the API, in `subroutine search` and from an
  agent's `subroutine_search`.

  A ref is what everything here is addressed by — it is in every commit message, every comment
  and every sentence anybody writes about an item — and search was the one place it did not
  resolve. Measured across ten refs beforehand, the item itself came back in none of them.

  Both readings are kept rather than one replacing the other, because `862` may be the item and
  may equally be a number somebody wrote in a description. Which of them comes *first* is not
  settled yet; it arrives with search ranking.

- **`subroutine today` has an *In progress* section.** Work you have already started is
  neither scheduled nor a candidate to pick up, and it used to be filed among everything you
  had ever captured. It sits between *Today* and the rest, and the heading is dropped when
  nothing is started, like every other section. `GET /v1/agenda` carries it as `in_progress`.

- **An agent's listing says what is already started and who is holding it.** A row from
  `subroutine_list` or `subroutine_search` now carries the status where somebody has changed
  it, and `held by @somebody` where a claim is live.

  This is the half that was missing from claiming. An agent is asked to take an item and mark
  it in progress so that nobody else picks it up — and until now no other agent could see
  either fact, so the announcement had no audience and two of them could take the same work.

  A row that has nothing to say still says nothing: an ordinary open task is unchanged, and a
  lease that has run out is not reported as held, because an expired claim does not count.

- **A listing says which work is holding something else up.** A task that blocks an unfinished
  one is marked `holds up` — the mirror of the `blocked` marker, which has been there since
  0.6.0 and only ever showed the other end.

  The case it is for: the urgent thing on your list is marked `blocked`, and the five-minute
  errand actually holding it up sits further down looking like the least important row on the
  page. Both ends of the relationship now say so.

  At the command line a row that is *both* — the middle of a chain — shows `blocked`, because
  that is the fact deciding whether you can start it and there is one cell to say it in. A card
  in the browser has room and shows both. `subroutine list --json`, the API and the agent's
  listing carry the two as separate fields, so nothing is lost to that precedence, and
  `subroutine show` names what is at the far end.

- **A task carries `rank` when you ask for it in a particular order.** `GET /v1/tasks` now
  reports where each row sat in the ordering the request asked for, so a client merging pages
  from several places can reproduce that order rather than guessing at it.

  **Compare it; do not read meaning into the number.** It is not a priority somebody assessed —
  `priority_score` is that, and is unchanged — and what goes into it may change without either
  of the two axes changing. It is null unless the listing was sorted by it, because computing it
  for callers who did not ask would spend a query per row on a number nobody reads.

- **A listing answers what was *worked on*, not only what changed.**
  `?touched_at.gte=yesterday` — and `subroutine list --filter touched_at.gte=yesterday` — finds
  what was created, edited, completed, commented on or moved through a status. Add
  `touched_by.eq=<username>` for one person's.

  **This is a different question from `updated_at` and the difference is the point.** Writing a
  comment does not touch the commented-on item at all, so an item somebody spent an afternoon
  discussing looks untouched by its own timestamps. `touched_at` reads the record of what
  happened instead.

  Claiming and releasing do not count — taking a lease on something to read it is not working
  on it. Everything else does, including deletions, which show up when you ask to see the trash.

  Work you *finished* in the period is included, since finishing something is the clearest case
  of having worked on it. Ask for `include_completed=false` beside it if what you want is what
  is still in flight.

- **A document can be tagged.** `#design`, `#security`, `#adr` — the same words and the same
  tags a task uses, because a tag belongs to a workspace rather than to a kind. Set them when
  you write one and change them afterwards:

  ```
  subroutine doc create "Why we chose Preact" --tag design --tag web
  subroutine doc edit 42 --tag settled
  ```

  Also on `POST` and `PATCH /v1/documents`, and reported by `subroutine show`, the JSON
  listing and every document the API returns. Tags **replace** rather than merge, as every
  other field on a `PATCH` does, so `--tag` with nothing after it clears them.

  The table for this has been in the database since the first release and nothing could write
  to it, so no upgrade is needed — only a version that reaches it.

  An agent can tag one too — `subroutine_document(title=…, tags=["design"])` — and
  `subroutine_show` now lists an item's tags.

- **A listing can be asked about a date.** `GET /v1/tasks?created_at.gte=yesterday` — a field,
  one of `gt`, `gte`, `lt` or `lte`, and a day, an instant or any expression the rest of the
  program already takes: `today`, `now-7d`, `start_of_week`, `start_of_month+1M`. Two of them
  make a window. It works on tasks, documents and projects, and on `created_at`, `updated_at`,
  `completed_at`, `due_at`, `start_at`, `content_updated_at` and `planned_for` as each of them
  applies.

  **It narrows alongside every other filter rather than replacing them**, so *what did I finish
  in this project last week* is one request: `?completed_at.gte=start_of_week&project=web`.

  Days are read in your timezone — yours, then the workspace's, then the instance's — and a
  bound takes in the whole day it names, so `created_at.lte=yesterday` includes all of
  yesterday rather than its first moment.

  `GET /v1/meta` lists which fields each listing accepts, so this can be discovered rather than
  remembered, and `GET /v1/docs/agent` describes it for an agent. `due_before` and `due_after`
  keep working and are unchanged.

  Equality on a timestamp is refused by name, with the range to write instead: a stored instant
  is precise to the microsecond, so `created_at.eq=yesterday` would match nothing and read as an
  empty backlog rather than as a question that was not understood.

- **`subroutine list --filter` and `subroutine search --filter` ask the same question from a
  terminal.** Repeat it for a range:

  ```
  subroutine list --filter created_at.gte=yesterday
  subroutine list --filter completed_at.gte=2026-08-02 --filter completed_at.lt=today
  subroutine search "boiler" --filter created_at.gte=start_of_week
  ```

  `subroutine explain dates` covers it beside the rest of the date vocabulary. A field a
  document has not got — a deadline, a planned day, a completion — asks about tasks, so
  documents drop out of the answer rather than arriving unfiltered.

- **An agent can ask the same question**, without dropping to a raw API call:

  ```
  subroutine_list(filter={"created_at.gte": "yesterday"})
  subroutine_list(filter={"completed_at.gte": "start_of_week"})
  ```

  The MCP tool surface grew by 401 bytes to carry it — roughly 100 tokens of every session —
  which is a deliberate act rather than a rounding error. The capability was already reachable
  through `subroutine_call_api`; what the bytes buy is that a model can *find* it, since a
  model deciding what it can do reads tool names rather than every schema in full.

- **A scripted listing reports who has the work, and whether it can be started.**
  `subroutine list --json` carries `assignee`, `blocked`, `parent_ref` and `status_category`
  beside the fields it already had. The terminal has shown all four for some time; the
  scripted path had none of them, so a script or an agent reading the same listing could not
  see that anything had been handed over, and would recommend starting an item that was
  blocked.

  `status_category` is there beside `status` because a status key is a workspace's own word
  and can be renamed; the category is the axis that cannot move.

- **An agent can revise a document it has written.** `subroutine_document` takes a `ref` and
  changes that document instead of writing a new one; fields you leave out are unchanged. It
  could only ever *add* before, so an agent that re-read its own conclusion and wanted to
  correct it wrote a second document saying something different — which is the duplication a
  single record exists to prevent.

  The tool surface grew by 53 bytes, taken from the headroom already there rather than by
  raising the cap.

- **`subroutine_show` reports the status, the deferral and the project.** An agent could set
  a status through these tools, be told *Changed*, and then find no tool in the catalogue that
  would ever mention it again — so it could not tell its own write from one it only thought it
  had made. The same reading now also shows a `from` date somebody deferred to, when the work
  was completed, and the project it was filed in.

- **The web page says which version of Subroutine served it**, in the footer beside the item
  count. If you are describing what you see on a page to somebody else — or wondering whether
  the fix you just deployed is the one you are looking at — that is the number that answers it.

- **An agent is told what a workspace has abandoned, not only what it decided.**
  `subroutine://conventions` — the document an agent is pointed at before its first write —
  listed the decisions in force and nothing else, so a route that had been tried and closed
  reached nobody. It carries both now, and a client that does not read resources is told both
  addresses.

  That half is the one a newcomer cannot work out for themselves: a decision leaves a rule
  behind, and a path not taken leaves nothing at all.

- **Finishing a task through the agent tools says what the claim on it is still doing.**
  Completing something does not release it, so an agent that claims work and finishes it left
  the claim behind with nothing to say so; and one that never claimed it at all was told
  nothing either. The reply now names whichever of those happened.

  The command line is deliberately unchanged: somebody finishing *buy milk* has not asked
  about claims.

- **A listing says which items are expensive to open.** Anything carrying more than about ten
  thousand bytes of prose is marked with its size — `12k` beside the row — at the command line
  and in the agent tools alike, and every listing reports `size_bytes` for a script that wants
  to decide for itself.

  One document on this project's own instance is 128 KB, which is roughly 32,000 tokens read
  into an agent's context, and its row looked exactly like a row for a three-word note. The
  mark appears only where it applies: a column that says the same thing on every row says
  nothing, so an ordinary list is unchanged.

- **You can ask for your own work without typing your own name.** `subroutine list --assignee
  me`, `?assignee=me` over HTTP, and `--filter touched_by.eq=me` for what you have worked on
  rather than what is yours. Useful the moment somebody else starts handing you things.

  `me` here is the account you are signed in as. It is deliberately *not* the same as
  `?actor=me` on the change feed, which means the credential you are holding — an agent with a
  shell has both, and they are genuinely different.

- **A captured line can carry a time of day.** `Solar eclipse today at 18:30` sets the day and
  the time; so do `Call the dentist tomorrow at 9am`, `Report due today at 17:00` and
  `Standup from monday 09:00`. A time behind `due`, `before` or `by` sets the deadline;
  otherwise it sets when the thing starts.

  **A time has to be signalled** — written after `at`, or straight after a date already
  recognised. That is what keeps `Email Bob re: 3pm` as a title rather than an appointment,
  and it is the same reason the date words are a closed list.

  **Anything it will not read stays in the title and is reported back**, which is new for times
  and is the more useful half: a range like `14:00-15:00` names an end, and there is nowhere
  to put an end yet, so it says so rather than quietly keeping the start.

- **Python 3.14 is tested and claimed.** It was already permitted by `requires-python`, tested
  by no job and claimed by no classifier — so an install on it worked and the package said
  nothing about whether that was meant to.

### Changed

- **Work you have put off until later now sits at the bottom of the lists that show it**,
  rather than mixed in among the work you can actually start. `subroutine list --deferred`
  and every list and board in the browser arrange it that way; the mark saying *from Fri 15
  Aug* is unchanged, so a deferred item is still visible, still readable and no longer
  mistakable for something nobody has parked.

  It is a *leading* sort key, so it holds whatever else you sort by: **Most important first**
  now means that among the work you can start, and then again among the work you have put
  off. Two things are deliberately left alone — a **search** stays in the order it was
  ranked, because an item you have deferred is still the best answer to what you typed, and
  the **finished** list ignores deferral entirely, since work that is done is not waiting for
  anything.

- **`GET /v1/tasks` and `GET /v1/documents` accept `?order=deferred`**, and no listing is
  re-ordered unless it asks for it. `/v1/meta` publishes the name for both. A document is
  never deferred, so it answers with the first band and stays in a merged list rather than
  being dropped from one.

- **`subroutine today` offers you the best work to start next, not the oldest.** The section
  of undated tasks — which on most backlogs is the bulk of the agenda — was ordered by capture
  order, so `!1/1 tidy the desk` sat above `!5/5 renew the passport`, and the twenty it showed
  you were the twenty you had written down first.

  It ranks now, by the same rule `--order -priority_score` uses, and it picks the twenty
  *before* it stops at twenty — so the most important thing on a two-hundred-item backlog is
  in the answer rather than off the end of it. The heading is **Next** rather than
  *Unscheduled*, because it now names what the section is for instead of what its rows lack.

- **Items that tie in a ranked listing now come out oldest first.** Ranked lists are
  tie-heavy — on our own backlog 52 of 172 open tasks share one score — so for about a third
  of a list the tiebreak is the only thing deciding the order. It used to follow whichever
  key came before it, which under `--order -priority_score` meant *newest* first: the thing
  you wrote down most recently won, permanently.

  **Age is a separator here and not a signal.** How long something has sat in a backlog says
  nothing about whether it matters; what it can do is stop the same rows being buried for
  ever. Expect a long list to look different even where nothing about the items changed.

- **`subroutine-remote` says that setting it up needs a terminal, because it does.** Its
  description claimed the plugin "runs and is configured wherever Claude Code runs". The first
  half is true and the second is not: the two fields it needs are filled in with `/plugin`,
  which the VS Code extension does not offer, and `claude plugin` has no subcommand that sets
  one. So it could be installed from an editor and never made to work, with every step
  reporting success.

  `docs/connecting.md` now says the same thing, and gives the settings file and the exact shape
  to write into it for somebody who has no terminal at all — with the warning that a token
  written there sits in plain text.

- **The board uses the width of the window, and says when there are columns off the edge.**
  The page is capped at a comfortable reading width, which is right for a list and wrong for a
  board — on a wide display that was hiding three columns of seven, and the only sign they
  existed was a scrollbar at the bottom of a page thousands of pixels long.

  The board now takes the room it has, columns stop widening once they are comfortable rather
  than stretching to fill a large screen, and where columns still do not fit — a phone, a
  narrow window — the edge of the strip is shaded to show there is more beside it.

  A board of tasks *and* documents has seven columns rather than four, because the two keep
  their own vocabularies: a superseded specification is not "done".

### Fixed

- **A search puts the best match first — the same order everywhere.** Searching `862` returns
  `#862` itself at the top rather than somewhere among everything that mentions it, and the
  command line, the API, an agent and the browser all agree about the order. Sort by
  `-relevance` explicitly if you want it on a search that would otherwise be arranged some
  other way.

  This only exists where `search_backend = "native"` is in force, because nothing else can
  score a match. `GET /v1/meta` lists `relevance` among a listing's sort fields exactly when
  it is available, and a search result now carries a `relevance` field so a client can put
  several collections into one order.

- **The product name in the browser is a link home.** It goes to the agenda across every
  workspace, which is what typing `subroutine` with no arguments shows you.

- **Deferred work on the board says it has been deferred**, and when it comes back. It looked
  exactly like work nobody had put aside, while the command line hid it entirely — so the two
  disagreed about items you had deliberately parked.

- **The browser keeps the order you asked for.** A list holding both tasks and documents was
  re-sorted by date after it arrived, whatever you had chosen — so *A to Z* produced a page
  that was not alphabetical and the control looked broken.

- **A finished item in a listing says so** — both at the terminal, which marks it `done`, and
  on an agent's row, which names the status. It appeared looking exactly like an open one,
  which mattered little while such a row was hard to reach and matters a lot now that
  searching for a number finds finished work on purpose.

  At the terminal the column is dropped when nothing on the page is finished, like every other
  marker there, so an ordinary list is unchanged.

- **Searching for a number finds the item even when it is finished.** Typing `815` looked the
  item up correctly and then hid it, because a listing shows unfinished work unless you ask
  otherwise — and most of a backlog is finished, so this failed far more often than it worked.

  A number names one item rather than narrowing a list, so it now reaches finished work the
  way asking about a completion date already does. Searching for *words* is unchanged and
  still hides finished work; `include_completed=false` is still honoured if you ask for it.

- **Searching in the browser no longer returns every document.** A search filtered the tasks
  and left the documents alone, so looking for something that did not exist still filled the
  page — which reads as a broken search rather than as an answer.

  Both halves of the list are searched now. Nothing was wrong with the search itself: the
  browser was asking one collection the question and the other for everything it had.

- **The add and edit forms in the browser put their fields in the same place wherever you open
  them.** The board is allowed to use the whole screen, and the capture box standing above it
  was being widened along with it — so the same form was five columns across from a list and
  ten from a board on a wide display, and a date's time sat beside it in one and below it in
  the other.

  Every field therefore moved depending on which view you had opened the box from, which is
  what stops anyone filling a form in without looking at it. The form now keeps a reading
  measure of its own; the board still gets the screen.

- **The comment box asks instead of instructing.** It was labelled *What happened*, and so was
  the thread above it — which is the right way to describe the difference between a comment and
  a document, and the wrong thing to put on a box at the moment somebody is writing in it. A
  comment saying *"I have asked the supplier"* or *"do we still need this?"* is neither wrong
  nor what happened.

  The thread is headed **Comments** and the box asks for one. Nothing changed about what a
  comment is for: the distinction still reads exactly as it did in the guide, the skill and the
  agent tools, where somebody is choosing between the two rather than typing.

- **An item that starts at a particular time says so in the browser.** Capturing *Dentist on
  Monday at 14:00* has read the time since 0.6.5, and the page showed only *Starts 17 Aug
  2026* — so the one thing you had gone to the trouble of typing was the one thing you could
  not read back.

  A deadline with a time behaves the same way. An all-day date is unchanged and does not
  acquire a `00:00` it never had, because the item says which it is rather than the page
  guessing from the clock.

- **`subroutine show` said a deleted item did not exist, while `list --trash` listed it and
  `restore` put it back.** Three commands, one item, and one of them denying it was there.

  It reads it now, says `deleted <date>` on the line beneath the title, and offers
  `subroutine restore` rather than inviting a comment nobody will read. Being shown without
  that marker was the other half of the same problem: an item in the trash rendered exactly
  like a live one, so it could be read, acted on, and never known to have been deleted.

  This affected a Subroutine installed on your own machine. Reaching the same instance over
  HTTP always worked, which is why it went unnoticed.

- **`subroutine_whoami` reported the instance's version twice and called one of them yours.**
  Asked over a served instance, it answered `Program X, instance X` — the same number supplied
  twice, because these tools run on the instance and cannot see the machine that called them —
  and said nothing at all about the plugin. The intended three-way version check was inert in
  the direction that reassures: an agent read it as "no version problem".

  It now names the instance, and says plainly that what you are running is not visible from
  there. Where it *is* visible — a local connection, answered in the process your plugin
  started — all three are reported and compared as before.

- **An agent on a machine with no instance was told to run a command that machine does not
  have.** The plugin starts through `uvx`, which runs from a cache and installs nothing, so
  `Run 'subroutine init'` answered `command not found` for exactly the people who had followed
  the plugin's promise and installed only `uv`.

  It now checks whether the command exists and says whichever remedy works — and the `uvx` form
  it gives is pinned to the same release series as the program giving it, so following the
  advice cannot create an instance newer than the program that will read it.

- **The hosting guide handed a colleague the wrong credential for the browser.** Its *Adding
  the people* section offered only `subroutine token create`, so an operator following the
  detailed document gave somebody a bearer token when what they wanted was a web page. The
  README had both halves; the page it sends you to for more had less.

  Both are there now, chosen by what the person is going to use — a link for the browser, a
  token for a terminal or an agent — and `docs/connecting.md` has a section for somebody who
  wants only the browser, which it previously had no reader for at all. `subroutine explain
  connecting` says it too.

- **An image written in a description came out as a link.** Markdown's image syntax —
  `![alt](url)`, the usual link form with a `!` in front — rendered as a clickable anchor
  labelled with the alt text,
  so a reader saw a link where somebody had written a picture, and following it went wherever
  the image had been. If the destination was one the renderer refuses, the `!` was dropped from
  the text it fell back to as well. Images are still not rendered; they are now shown exactly
  as they were typed, which is what the browser already does with everything else it will not
  render.

- **An agent was shown three of this workspace's five link types**, and the two missing were
  `derives_from` and `documents` — the pair that join a piece of work to the document
  explaining it. The tool points at the workspace's own list now rather than carrying a copy,
  which is also what makes it survive somebody renaming one.

- **The tool that writes a document told an agent to revise it with a terminal command.**
  An agent reaching an instance over the network has no terminal. It names something it can
  actually call.

- **`init` refused to set up an instance when the machine's username was a reserved word.**
  A workspace's short name is derived from its title, and falls back to the username when the
  title cannot produce a legal one — but the fallback was not itself checked, so
  `subroutine init --username app --workspace MCP` created nothing and complained about a
  field nobody had typed. A container running as `app` met this on installation.

- **`subroutine list --trash` suggested a command that refused every row it had just printed.**
  It offered `subroutine show <ref>`, which does not find a deleted item; it offers
  `subroutine restore <ref>` now. And a missing ref names the trash as the other place to look,
  phrased as a condition — the program knows the item is not here, not that you deleted it.

- **`whoami` told you what your role may do only when your credential was restricted.** So an
  agent learned *more* about its own permissions by being narrowed: a plain member was handed
  the word *Member* and nothing else. The list now appears for any role short of holding
  everything, on the command line and through `subroutine_whoami`.

- **A link in stored text could reach another host behind a control character.** `//evil.example`
  was refused; one invisible character before it was not, because that check read the raw
  destination while the scheme check read the cleaned one. Both read the cleaned one now.
  Nothing that was already allowed has changed.

- **The API's own schema said a link event names nothing, and it names its source.** Published
  in `/v1/openapi.json`, so a client reading it concluded it had to resolve link events itself.
  It carries the item the link hangs off, exactly as a comment carries the item it was written
  on — which also means a client watching one item sees links made *from* it and not *to* it.

- **Asking when something was completed said that nothing was.** `completed_at` is only ever
  set on finished work and a listing hides finished work unless asked — so
  `subroutine list --filter completed_at.gte=today`, and the same request over HTTP, answered
  with an empty list the same minute a task was completed. Asking about completion now reaches
  completed work, exactly as naming a finished `status_category` already did, and asking for
  both `completed_at` and `include_completed=false` is refused rather than quietly resolved.

### Security

- **A restricted API token could sign in as its owner and come back unrestricted.** A sign-in
  link is redeemed for a browser session, and a session carries no scopes, no project scope and
  no workspace pin — so a token narrowed to, say, `task:read` could mint a link, redeem it, and
  write freely as the person it belongs to. Every bound was lost at once, including an expiry.

  **A bounded credential is refused now**, on all four axes and on expiry, and the refusal says
  which. Two things are deliberately unchanged: an unrestricted token mints links as before, and
  so does `subroutine login link` at the instance itself — which is the way back in for a
  self-hoster whose mail relay is broken, and it must not depend on anything.

  Service accounts were never affected: an agent's credential could not sign in to a browser
  already. **If you have issued somebody a narrowed token and expect them to use the web
  interface, they now need a sign-in link handed to them** rather than minting their own.

  Found by an outside review of the whole codebase.

- **Rate limiting stopped counting new callers on a busy instance.** A bucket was created and
  then immediately discarded by the housekeeping sweep that ran on the next line, because a
  fresh allowance looks exactly like one that has finished refilling. Below a few thousand
  tracked keys nothing swept and nothing was wrong; above it, every request from every new key
  went uncounted — measured at 200 allowed against a limit of 30.

  It matters most for the limiter that counts **failed** authentication, which is keyed on
  where a request came from so that guessing cannot buy a fresh allowance per guess. What it
  protects is a 256-bit random token, so this was a safeguard not working rather than a way in.

  Found by the same review.

- **Corrected: `config.toml` was described as holding no secrets and as world-readable.** It is
  neither. The file is written `0600`, and `subroutine init` puts `secret_key` in it.

  **If you took the old advice and committed or shared that file, it has your signing key in
  it.** What that key does is bounded — it signs pagination cursors and nothing else, so it is
  not a way in and rotating it locks nobody out — but it should not be somewhere public, and
  the documentation should not have said it could be.

  What remains true is the reason the split exists: **no API token is ever written to
  `config.toml`**, and `credentials.toml` is still the file that never leaves the machine.

  Found by the same review, which named three places saying it; there were five.

- **`/readyz` no longer tells the world why your database is unreachable.** The endpoint is
  public by design, and it was putting the driver's own error into the response — which is an
  internal hostname, a database name, or a filesystem path, depending on what went wrong.

  **Nothing changes on an instance only you can reach**, which is where the message earns its
  keep: setting one up, you still get the cause. Once `public_url` is set the caller is told
  the instance is not ready and the cause goes to the log instead. The remedy — check
  `database_url` — is still in both.

  Found by the same review.

## 0.6.4 — 2026-08-11

### Added

- **The browser can file an item with everything on it.** *More* beside the add box opens a
  form: description, project, type, status, assignee, importance, urgency, estimate, a start
  day, a planned day, a deadline and tags. If you are looking at a project, that is where it
  goes; the types and statuses are your workspace's own, so renaming one renames it here too.

  **The one-line box is unchanged and still first.** It is the same box — type `call the
  dentist friday !4/3` and press Add exactly as before, whether or not the form is open. When
  both are used the line still sets the title, and anything you picked in the form wins over
  what you typed.

  Every control says what it holds: projects are indented under their parents as
  `subroutine project list` shows them, the priority scale reads *1 — Very low* to *5 — Very
  high* so it cannot be filled in backwards, an agent is listed as `claude (agent)` rather
  than as a colleague, and each of the three dates carries the sentence `subroutine explain
  dates` uses for it.

  A start day now shows on an item too. It could be set from a terminal and read back nowhere.

- **An item can be edited in the browser.** *Edit* on an open item opens the same form the add
  box discloses, filled in with what the item already says, and every field can be changed or
  emptied. Blanking a deadline clears it, rather than quietly leaving it alone.

  **If somebody else saved while you were editing, nothing is overwritten.** The save is
  refused, your typing stays exactly where it is, and you are told what the item says now —
  so you can fold your change into theirs rather than losing one of them. That applies to an
  agent working on the same item as much as to another person.

- **The browser can search.** A box in the header narrows the list to what matches, the same
  way `subroutine list --q` and the agent tools do — every word has to appear, and each may
  appear in a title or a description. The search is part of the address, so you can send
  somebody what you were looking at, and clearing the box takes it off again.

- **Documents can be written and revised in the browser.** *More* beside the add box now asks
  whether you are writing a task or a document, and a document gets the fields a document has —
  prose, a type, a status and a project, and none of a task's priorities or dates. An open
  document can be revised the same way an item is edited, and the same protection applies: if
  somebody saved while you were writing, nothing is overwritten.

- **Links can be made and removed in the browser**, on an item or a document, in whichever
  ways your workspace names — *blocks*, *relates to*, and anything you have added. Type a ref
  and pick a relationship; you do not have to say whether `#42` is a task or a document,
  because it works that out.

  **A linked item now says whether it is finished**, so *Blocks #442* tells you whether you
  are still blocked without clicking through.

- **Notes can be written on an item in the browser.** *What happened* now has a box under it,
  and Markdown and `#42` links work in it exactly as they do everywhere else. Documents take
  them too.

  The thread also says **who** wrote each note, which it did not before — and an agent is
  marked as one, so a machine's note is not read as a colleague's.

- **An item's status can be changed from the open item**, without opening the edit form —
  *to do* to *in progress* to *done* is the commonest thing anybody records. The options are
  your workspace's own, so a status you renamed or added is offered here too. It is available
  on a cancelled item as well, which previously had no way back at all.

  Changing a status changes nothing else: it does not claim the item, and claiming an item
  does not move its status.

- **`subr` is the same program under a shorter name.** Ten characters against sixteen, on
  something you type dozens of times a day: `subr today`, `subr add "…"`, `subr done 42`.
  Every command answers to both, and `--help` says so.

### Changed

- **`POST /mcp` takes an API token and says so.** It used to accept a browser session cookie as
  well — not deliberately, and nothing documented it. MCP is how an agent reaches this instance
  with a URL and a token; a page in a browser should call `/v1`, which is what the web UI does.
  A request carrying only a session is now refused with a message saying what to send instead.

  Nothing that follows the documented way in is affected: an agent sends
  `Authorization: Bearer sr_…` exactly as before.

- **Subroutine describes itself differently.** It is *agent-native task management for your
  life, your projects and your team* — one line, on the PyPI summary, the README, the
  marketplace and both plugin manifests, where there used to be two variants of a different
  one. Nothing about the product changed; the description now says what it is for rather than
  which category it is in.

- **The command line and the API describe the product the same way everything else does.**
  `subroutine --help` and the API's own `/docs` page were still opening with the old
  description for an hour after everything else had changed.

- **The README tells you and your agent apart.** Installing it for yourself and wiring it into
  Claude Code are two different jobs with two different answers, and the page used to give the
  agent's answer to both — so a person was shown `uvx subroutine …`, which costs a quarter of a
  second and four characters on every command, for ever. Install it once and type `subr`. The
  plugin still needs nothing installed first; that is what `uvx` is for.

- **Starting a server says what it started.** `subroutine serve` used to print one line
  naming the agent guide. It now names each thing the instance answers:

      Serving on http://127.0.0.1:8471
        /v1   the HTTP API — the guide written for an agent is at /v1/docs/agent
        /mcp  MCP over HTTP — an agent needs this address and a token, and nothing installed

  **The second line is not new behaviour, only a new sentence.** Every served instance has
  spoken MCP since 0.5.0, and nothing said so — so the cheapest way to give an agent access,
  handing somebody an address and a token with nothing to install at their end, could be found
  only by reading the source. The guide an agent reads at `/v1/docs/agent` now says it too.

  What is listed is read from the routes the instance actually serves, so it cannot claim a
  way in that is not there, or stay quiet about one that is.

- **Cards are dragged between columns on the board.** Pick one up, drop it on another
  column, and its status moves — the same write the status control on an open item makes, so
  there is one answer to what moving something means. Dropping a card back where it started
  changes nothing, which is how most drags end.

  **Which status a column means comes from your workspace.** If your *to do* holds *Triage*
  and *Ready*, a card dropped there gets whichever one you marked as the ordinary one. A
  column with no status behind it declines the drop and says so, rather than failing.

  Reordering *within* a column is not this and is not built yet.

- **You choose how the list is ordered, and it says so.** *Order* above the rows: newest,
  oldest, A to Z, recently changed, or most important. Whichever you pick, each row shows the
  value it is sorted on — so the order is something you can check rather than take on trust.
  The choice is in the address, so the page you send somebody is the page you were looking at.

  **Ordering by importance shows tasks only, and the page says so.** A document has no
  importance or urgency, so there is no honest place to put one in a ranked list. Every other
  ordering holds across tasks and documents alike.

- **An item open in the browser keeps up with itself.** Leave `/projects/subroutine/42` open
  and a comment somebody writes on it, a status an agent sets, or a description that gets
  revised now appears there, without closing and reopening it. Your place on the page is kept:
  nothing scrolls and nothing jumps.

  If the item is deleted while you are reading it you are told, and what is on screen stays —
  it is the last version you were shown, and losing it would lose the news along with it.

  **While you are editing, nothing is replaced underneath you.** A save that clashes with
  somebody else's is still refused and still shows you what they wrote, which is the point of
  the check and is not something a background refresh should be able to slip past.

### Fixed

- **The top of the page stays put when you open an item.** Search and the view switcher used
  to disappear, and the header redistributed what was left — so the workspace dropdown moved
  to the far right and the page looked like a different page. Both are there now, and using
  either takes you back to the list. An item's address is a link somebody sends you, so it is
  as likely to be where you arrive as the list is.

- **Both ends of a link can be chosen.** *Blocked by* is in the list beside *Blocks*, and
  *Duplicated by* beside *Duplicates* — so you can say what an item is waiting on while you are
  looking at it, rather than having to open the other one. *Relates to* still appears once,
  because it means the same thing in both directions.

- **A deadline or a defer can carry a time of day.** *Hidden until* and *Due* have a time box
  beside the date. Leave it empty and the date means the whole day, exactly as before; fill it
  in and an appointment at 14:00 is an appointment at 14:00. *Planned for* stays a day, because
  that is what it is.

- **A card can be dropped anywhere in a column.** Columns were only as tall as what was in
  them, so dragging a card into an empty column worked in its top few centimetres and nowhere
  else — which is the commonest move there is. Every column is now the full height of the
  board, and a board with nothing on it yet still has somewhere to drop the first card.

- **A server behind a proxy says the address that reaches it.** `subroutine serve` tells an
  agent it needs *the address above* — and on an instance behind a reverse proxy that was the
  loopback socket, which reaches nobody. When `public_url` is set it now prints that too.

- **An item you edited and then abandoned is up to date again.** If somebody changed it while
  your edit form was open, closing the form without saving used to leave the old version on
  screen with nothing saying so. It re-reads now. Saving was always safe — a clash is still
  refused and still shows you what they wrote — and nothing is replaced while you are typing.

- **A card that cannot be moved says which thing it looked at.** Dragging one to a column
  when the page had not yet read your workspace's statuses reported *there is no status here
  that means in progress* — a claim about how your workspace is set up, when the real answer
  was that a request had not arrived. The two now say different things, and only the one you
  can do something about offers a remedy.

- **A phone number is no longer mistaken for a project name.** Typing `Call +44 7911 123456`
  said *left as written: +44 — a project is named like '+web'*. The task was always filed
  correctly and the number always stayed in the title; only the note was wrong, and it is gone.
  An unreadable project name is still reported.

- **The browser stops reloading itself every ten seconds.** It asked what had changed, was
  answered *the thing you already knew about*, and treated that as news — so it refetched the
  whole list on a timer whether or not anything had happened, on every page, all day. It now
  reloads when something has actually changed.

- **A project name it could not read is now said out loud.** Typing `+my/project` left the
  words in the title and filed the item wherever the default was, without saying so — while
  `+myproject` naming a project that does not exist has always been refused by name. The same
  mistake got the best answer one way and the worst the other. It now says what it could not
  read and what a project name looks like. `C++` and `a+b` are untouched.

- **The browser showed some dates a day out.** A deadline set for *all day Friday* is stored at
  the last instant of Friday, so reading it in a different timezone from the one that stored it
  moved it to Saturday — and a planned day, which has no time in it at all, moved the other way.
  The command line and the browser could disagree by a day about the same item. Days are now
  read in the timezone that stored them, so they agree.

  It was correct in winter and wrong in summer for anyone in the UK, which is why it lasted.

- **An appointment later today was missing from today's agenda.** A task that starts at 2pm
  did not appear until 2pm — so a dentist appointment was invisible all morning, on the
  command line and in the browser alike, and `subroutine today` said *Nothing due today* about
  a day that had something in it. Anything written with a time is affected, because
  `Dentist, 2pm–3pm` records a start as well as a deadline.

  A defer now hides something until a **day** rather than until an o'clock: the agenda is a
  view of one day, so a task starting later that day belongs to it and one starting tomorrow
  does not. `?ready=` is unchanged — *what can I start now* is a different question, and at
  9am the honest answer for a 2pm appointment is still "not yet".

- **The browser called a defer a start.** The date that hides a task until it is relevant was
  labelled *Starts*, which is the one thing it does not mean — nothing begins on that day, the
  task simply does not appear before it. It is *Hidden until* now, in the same words
  `subroutine explain dates` has always used, and *Planned for* and *Due* say what they are
  too.

- **Opening an item no longer flattens the project tree out of the address.** Viewing
  `/personal/websites/my-site` and opening something in it left `/personal/my-site/42` in the
  bar — still the right item, but the top-level project gone from where you were. It keeps
  the path you are on when that path names the item's own project, and uses the item's own
  address when it does not.

- **The address of one item is the address of one item.** Opening `/projects/ui/441` in the
  browser used to leave `/projects/ui/441?view=list` in the bar. *List* and *board* say how a
  set of rows is laid out, and a single item is not a set of rows — so the link you copied out
  of the page and the link you ended up with were two different strings for the same thing.
  Links written before this still work and tidy themselves up when you follow one; closing the
  item still takes you back to the list, board or filter you came from.

### Security

- **A sign-in link can no longer quietly make a signed-in browser somebody else.** Opening a
  link for a different account now stops and asks: the page names who you are signed in as and
  who the link is for, and nothing happens until you say yes. Saying no leaves the link unused,
  so you can still sign in with it later.

  It matters because a sign-in link works by being *opened*, so anybody who can get you to
  click one — in a message, on a page — could put your browser into their account without
  saying so, and everything you wrote next would be filed there. Confirming has to be a button
  press on this instance's own page, which is something only you can do.

  Signing in normally is unchanged, and so is opening a second link for the account you are
  already in.

- **Credentials no longer reach the server's own access log.** A sign-in link travels in a URL
  — it has to, because a link is opened by clicking it — so `GET /signin?link=…` used to be
  written into the log in full. It now reads `link=REDACTED`, and so does an API token that
  somebody has put in `?token=`, `?api_key=` or `?access_token=` by mistake: those are refused,
  and the refusal already says to treat that token as compromised, so writing it down
  afterwards helped nobody. Everything else in a request line is left alone.

  **Your reverse proxy keeps its own log and we cannot reach that**, so
  [docs/hosting.md](docs/hosting.md#keeping-credentials-out-of-your-logs) now says how to tell
  it the same thing. A link is good for half an hour and works once, which is why this is worth
  tidying rather than worrying about.

- **The web UI tells your browser what it is allowed to do.** Every response now carries a
  content security policy, and it is a strict one: nothing may be loaded from another host,
  nothing may be framed, and a form may only post back here. The app was already built that way
  — it loads no fonts, no scripts and no styles from anywhere else — so the policy costs it
  nothing and closes the gap if a defect ever appeared in the part that renders Markdown, which
  is where text other people wrote ends up on your screen.

  Responses also say `X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin`, so an
  item's address is not handed to another site when you follow a link off this one.

## 0.6.3 — 2026-08-10

**0.6.2 was tagged and never published.** Its release commit re-formatted both plugin manifests
without changing anything in them, which the guard against an undeliverable plugin correctly
refused — so the build went red on the tag and nothing reached PyPI. Everything 0.6.2 was going
to contain is below, unchanged, and ships as 0.6.3. `v0.6.2` remains as a tag with a red build
because that is what happened; there is no 0.6.2 to install and no release page for one.

### Added

- **Work can be seen as a board, in columns.** *List*, *board* and *done* sit at the top of the
  page, and what you are looking at is part of the address — `/projects?view=board&include_completed=true`
  is a link you can send somebody, and it survives opening an item and pressing back.

  The columns are **to do, in progress, done and cancelled**, which is what a status *means*
  rather than what you have named it. Rename `open` to `next up` and the board still works;
  three of the statuses you start with are all *to do*, and they share a column. An empty
  column is still shown, because the columns are the shape of the board rather than a summary
  of what happens to be in it.

  Documents get their own columns — a superseded specification is not *done* — and anything
  whose state this page does not recognise is shown under its own name rather than dropped.
  Nothing on the list is missing from the board.

  The board shows finished work — a list hides it, because a list answers *what do I have to
  do*, and a board answers *where is everything*. Like the list, it says when there is more
  than fits on a page rather than leaving a column looking complete.

  **A column says when it was not asked for**, rather than reporting that there is nothing in
  it. Ask for a board without finished work and *Done* and *Cancelled* say so and offer to show
  it; narrow one to a single category and the others say so too. An empty column and a column
  nobody asked about are different facts, and a board is exactly where somebody looks to
  conclude nothing is left.

  **Cards cannot be dragged yet.** Moving one has nowhere to be recorded, so a card would not
  stay where you put it; that is coming separately.

- **The browser opens on your day, not on a list.** `/` is now the agenda — what is overdue,
  what is due today, what is due in the next seven days, and what is waiting — across **every**
  workspace you can see, rather than the newest hundred things in whichever one came first.
  It is the same four buckets, in the same words, that bare `subroutine` prints at a terminal.

  A row from another workspace is addressed as `sandbox/#1`, exactly as `subroutine today`
  writes it, and only when the page actually spans more than one — on a single-workspace
  instance nothing is repeated on every line. Opening or completing one acts on the workspace
  it came from rather than on whichever is selected.

  It refreshes itself as work changes, and that refresh spans workspaces too. A workspace's own
  list is still one click away, and every other address is unchanged.

- **A listing can be asked for one kind of work rather than one named status.**
  `GET /v1/tasks?status_category=` takes `todo`, `in_progress`, `done` or `cancelled`, and both
  clients pass it. A status *key* is yours to rename, so anything built on `?status=done` breaks
  the day somebody renames it; the category is the fixed field published beside the key for
  exactly this. It also gathers a group — three of the seeded statuses are `todo`, and asking by
  key means knowing all three and re-learning them when you add a fourth.

  **Asking for finished work no longer needs a second parameter.** Naming a finished category
  reaches finished tasks on its own, rather than answering with an empty page because completed
  work is hidden by default. Asking for a finished category *and* excluding finished work is
  refused and says why, instead of quietly picking one of the two.

- **Tasks can be sorted by when they were finished** — `?order=-completed_at`, which is
  *most recently done first*. Unfinished work sorts last in both directions, so one query
  answers "what has been done lately" without a second filter.

- **A third control shows what has been finished, most recently first.** *Done* sits beside
  *list* and *board*, and what it writes into the address is the filter it applies —
  `/projects?status_category=done&order=-completed_at` is a link you can send somebody.

  It is what you look at to see progress rather than to plan: everything finished or cancelled,
  newest finish at the top, narrowed to a project if the address names one. Every row says
  **when** it finished rather than when it was due — a deadline on completed work is a date that
  stopped mattering. A cancelled row says *cancelled*.

  **With the time, and today and yesterday by name** — *done yesterday 21:56*. It is the field
  the page is sorted on, so a day alone left a screenful of rows all reading the same date and
  an order you had to take on trust. A deadline stays a day, because that is a date somebody
  chose rather than a moment anything recorded.

  **It holds tasks only**, because only a task can be finished: a document's states are draft,
  current, superseded and archived, and none of them means *done*.

  There is no capture box while it is on. Anything you added there would be open, and the page
  shows what is over — so it would report success over a list that could not change.

- **What is showing and how it is arranged are two separate parts of the address.** `?view=`
  says how rows are displayed — *list* or *board*. `?status_category=`, `?include_completed=`
  and `?order=` say which rows there are. A control at the top of the page may set several at
  once, and the address states each of them, so what you send somebody is what you were
  looking at.

  What this buys is combinations that had no name before: a **list including finished work**
  (`?include_completed=true`), a **board of only what is in progress**
  (`?view=board&status_category=in_progress`), a **board of finished work**. None of them was
  reachable while the arrangement carried the filter, and none of them is new machinery — they
  are what falls out of writing the two things down separately.

  A word this page does not know is ignored and named rather than replacing your page with a
  failure, which is what already happened for an arrangement it does not have.

  **Every control writes what it chose, including the default.** The address is what you send
  somebody, so it says which arrangement you were looking at rather than leaving them their own.
  A bare address still works: `/projects` typed by hand is the list.

- **Agents are now told to claim work, say when they start it, and give it back.** Three acts
  around the job: take the task before touching anything, move it to *in progress* when you
  actually begin, and release it at the end whether or not you finished.

  **The instruction to claim was already there and had never once been followed**, because it
  began *"if anybody else works from this list"* — which an agent alone on an instance reads as
  false, and which stops being false at exactly the moment the habit needed to have existed
  already. It is unconditional now.

  **Finishing does not release a claim**, so that is a separate act rather than something to
  assume, and the skill says so. A lease still expires on its own, so nothing is stranded when
  a session ends partway.

  Both plugins carry the change, and `GET /v1/docs/agent` says the same for an agent working
  from the API with no plugin at all.

- **A list says who is holding an item.** A row now carries *`agent` is on it* while somebody
  has claimed it, so you can see what is being worked on without opening anything.

  **A claim that ran out says *`agent` left it***, which is a state nothing showed before: work
  that was started and walked away from. A lease expires by itself so that a worker whose
  context ends does not strand the task, and until now the only sign of that was the item
  quietly becoming available again.

  **It does not change the item's status, deliberately.** A claim is taken *before* the work and
  may be given back without any being done — so *somebody is on this* and *this is in progress*
  are two different statements, and only the second is one anybody made.

  `GET /v1/tasks` reports `claimed_by` beside `claimed_by_id`, loaded in the same query as the
  assignee's name, so reading a page costs no extra request.

- **An instance can have more than one administrator.** `subroutine user create <name>
  --superuser` grants it, and `POST /v1/users` takes `is_superuser`. Until now there was
  exactly one — the account `init` made — and nothing could create a second: the field was
  reported everywhere, rendered by `user list` as *instance admin*, and settable by no command
  and no endpoint.

  It matters because administering is the only way to create accounts and workspaces, and no
  role can carry it. Without a second administrator you cannot delegate that, cannot keep a
  spare against losing the first, and cannot give an agent the rights to create the things you
  are asking it to create.

  **An agent cannot grant it.** Handing out administration is a person's act — the same rule
  that already stops an agent marking somebody as having left, and what stops an administering
  agent quietly making more of itself.

### Fixed

- **Anything you can navigate to in the browser can now be opened in a new tab.** Ctrl-click or
  cmd-click an item and it opens beside your list instead of replacing it; so does middle-click,
  and the right-click menu offers *Open link in new tab* and *Copy link address*. The same is
  true of the controls at the top of the page, of a linked item, of *All items*, and of *Show
  everything*.

  They were buttons, which have no address, so there was nothing for a modified click to act on
  — and no way to keep a list in one tab and work through items in others. A mention written
  inside a description was the only thing on the page that already behaved correctly.

  It also means a screen reader announces each of them as a link, so listing the links on a page
  now finds the items on it.

- **A restore now says *who* is using the database, and no longer refuses over a connection
  that has already gone.** `subroutine db restore` will not write over a database something
  else is connected to — that guard stays, because restoring underneath a running service
  destroys the instance and reports success.

  What it said was *"1 other connection to the database"*, which is the same sentence whether a
  colleague is connected, your own service is running, or something is on its way out. It now
  names each one — what kind of connection, what it is doing, and how long it has been there.

  And it looks twice. A service holds its connection for as long as it runs, so a second look a
  moment later cannot miss one; anything gone by then was not using the database. That costs a
  quarter of a second, and only when the first look found something.

- **Every link on the PyPI page now goes somewhere.** The project description is `README.md`,
  and it linked to `docs/hosting.md`, `docs/connecting.md`, the licence and how to report a
  vulnerability as paths relative to the repository. GitHub rewrites those; PyPI does not, so
  each one resolved against the project page's own address and led nowhere.

  Nine links, and four of them were what somebody needs to read *before* installing anything.
  They point at the repository directly now, which works on both.

- **Completing something that is already complete no longer changes when it was finished.**
  Every write of a finished status stamped the moment afresh, so a retried request, or a second
  press of a button, moved a record of something that had happened hours earlier. Reopening
  still clears it, and finishing it again after that is a new completion and is stamped anew.

  It matters more than it did last week, because a list can now be ordered by when work
  finished — so the old behaviour would have jumped a row to the top of that page for a reason
  nobody intended.

- **Finished work is no longer offered a *Complete* button.** Every card in the board's *Done*
  column carried one, and pressing it was not harmless: completing something that is already
  complete moves the record of when it finished. A cancelled item had the same button on its own
  page, because the check there knew about *done* and not about *cancelled*.

- **A permission list now says that `task:write` covers documents.** There is no
  `document:read` or `document:write` — a document is a work item under the same permissions as
  the task beside it — and nothing said so anywhere a person or an agent could read it. Both
  `subroutine whoami` and `subroutine_whoami` now render it as
  `task:write (tasks and documents)`.

  It is worth a line in a changelog because of what it cost: an agent read its own grants,
  found no document permission, wrote up a substantial piece of work as a comment rather than
  as the document it should have been, and asked for its credential to be widened. It had held
  the capability the whole time. A list that is true and reads as complete is worse than a
  refusal, because there is nothing to argue with.

- **An agent asking its first question before you have run `subroutine init` is now told so.**
  It used to receive a stream of objects that were not protocol messages at all — an internal
  error document with no envelope and nothing tying it to the question asked, for every message
  including the one that opens the session. Nothing could be matched to anything, so the session
  simply never started and the tools appeared broken.

  It now answers *"No Subroutine instance has been set up on this machine yet"* and names the
  one command that fixes it, which is the same sentence the command line has given for a year.

  **This matters more now that the plugin installs nothing** — arriving before `init` has been
  run is the ordinary first contact rather than an edge case.

- **A refusal from the instance reaches an agent as a refusal.** Anything the API declined —
  a permission, a bad argument, a workspace it could not resolve — was written onto the
  protocol channel as-is rather than as an error the client could read, so the agent saw
  nothing it could act on. The instance's own words are used now, including the hint.

### Changed

- **The Claude Code plugin no longer needs Subroutine installed first.** It starts the program
  through `uvx`, which fetches and caches it on first use — so installing the plugin is the
  whole of it, rather than the third step after installing Python and a package and fixing your
  `PATH`. What you need instead is [uv](https://docs.astral.sh/uv/getting-started/installation/),
  which is one line and no Python. Measured: about five seconds the first time, then a fraction
  of a second.

  **If you already ran `uv tool install subroutine`, that copy is used** rather than a
  download, so the two arrangements do not fight and nothing you have set up stops working.

  **The plugin's "subroutine command" setting is gone.** It cannot survive the change: `uvx`
  takes the package name as its first argument and there is no way to say "skip that". If you
  were pointing it at a virtualenv or a checkout, use
  `claude mcp add subroutine -- /path/to/subroutine mcp` instead — which is better for that
  purpose anyway, because the plugin's copy is cached and lags until you refresh it.

  **The version it fetches is pinned to a release series**, so you get fixes automatically and
  never a minor version that might carry a database migration on a day you did not choose one.

## 0.6.0 — 2026-08-09

> **This release changes the database schema**, to `ce11c7d2df2f`.
>
> Install it, then run `subroutine db upgrade`. That reports both versions, takes a
> verified backup, migrates and checks the result — in that order. Stop the service
> first if you are running one; expect it to be down for the length of the migration.

### Added

- **There is a web interface.** Open the instance's address in a browser and you get a list of
  everything in a workspace — tasks and documents together — and clicking one shows it in full:
  its description, what it is linked to, and everything anybody has recorded against it.

  **You can also work in it.** Complete something from the list or from the item itself; add
  something with one box, which takes the same shorthand the command line does — so
  `call the dentist tomorrow +work !4/3` files it in the right place, at the right priority,
  planned for the right day. Hand a task to somebody, or take it back, from a list of the
  people in that workspace.

  **Completing tells you what it did, and offers to undo it.** Nothing asks you to confirm
  first: a question before every action is a tax on being right, and putting it back is one
  press. A refusal — something somebody else changed, a permission you do not have — is
  reported beside the work rather than replacing the page you were reading.

  **Every item has an address you can send somebody.** Open one and the browser's address bar
  says which it is, so a link can be pasted into a message and it opens on the item — and the
  back button closes it again, rather than leaving the page.

  An address reads `/<workspace>/<project>/<number>`, and the project in the middle is there for
  the person reading it rather than for the machine. Rename a project and links written down
  beforehand go on working: the number is what resolves, and the address quietly corrects itself
  to the new name.

  **A `#42` written in a description is now a link to that item**, using the same rule the
  instance itself uses to decide what counts as a reference — so `#42FF00` and `issue#1` are
  still just text.

  **An address filters the list.** `/<workspace>` shows that workspace and
  `/<workspace>/<project>` shows that project and everything under it, saying so above the list
  with a way back to the whole of it. A project that has since been renamed does not break the
  link: the list widens and tells you why.

  **The list is the whole list.** It arrives in the same order the command line uses, so
  something you have just added is at the top rather than sorted below everything with a
  priority set — and when there is more than fits on a page it says so, and offers the rest.

  Revising a description, and everything else, is still the command line and the API.

  It is served by the instance itself, on the same address as the API, and signing in is the
  link above. Nothing to install, nothing to build, and no external service is contacted: the
  two libraries it uses are in the package. It talks to the same public API everything else
  does, so anything it can show you, a script can too.

  A workspace picker appears when you belong to more than one.

  **Descriptions and comments are shown as the Markdown they are written in** — headings,
  tables, lists, quotations, code and emphasis, rather than the punctuation somebody typed to
  get them. Tables matter more than they sound: a fifth of the prose in a working backlog turns
  out to be laid out in one, and read as raw text it is unusable.

  Anything that looks like HTML is shown as the text it is, not treated as markup. That is what
  keeps a description written by somebody else — or by an agent, repeating something it read —
  from becoming part of the page; and it is also what makes a `<placeholder>` written in prose
  visible instead of silently disappearing. A link is only made for an address the browser can
  safely open, and one that is not is left showing where it pointed rather than quietly losing
  it.

- **Somebody can sign in to a browser, with a link rather than a password.**
  `subroutine login link` prints a single-use address that works for half an hour and hands the
  browser a session lasting a fortnight. `subroutine login revoke <name>` ends every session
  that person holds, and any link they have not used yet — which is what a lost laptop needs.

  No password to store, no reset flow, and nothing worth stealing in a breach. The credential is
  an opaque value in a cookie backed by a row, so revoking it takes effect on the next request
  rather than whenever it would have expired.

  **The link is printed at a terminal rather than emailed, deliberately.** Sending it by email
  is still to come, and until it arrives an operator whose mail relay is misconfigured is not
  locked out of their own instance — the console has to be a way in when the ordinary path is
  broken. Nothing about this is required: an instance nobody signs in to is unchanged.

  A service account cannot be given one. An agent's authority is issued with a scope and a
  reach, and a browser session carries neither.

  **A session is accepted for a change only from a page this instance serves**, which means
  where it is served from and whatever `public_url` says — and any origin in `cors_origins`, if
  you have deliberately put a front end somewhere else. Reading is unaffected, and so is an API
  token: a script or an agent sends one deliberately, where a browser attaches a cookie without
  being asked.

### Changed

- **The licence has changed, to [FSL-1.1-ALv2](LICENSE) — the Functional Source License.**

  **Almost certainly nothing changes for you.** Run it, modify it, fork it, for any purpose
  including making money. A person, a team, a five-hundred-person company self-hosting it for
  its own work, a consultancy charging to set it up for a client: all free, for ever, with
  nothing to buy and nobody to ask. There is no obligation to publish anything, and nothing is
  triggered by having users — which is a *reduction* on the AGPL, whose network clause meant a
  modified instance owed its source to the people using it.

  **The one thing you may not do is sell other people access to it as a service.** If that is
  what you have in mind, write to simon.holliday@protonmail.com — a commercial licence is
  available by agreement.

  **Every release becomes Apache-2.0 two years after it ships**, automatically. That is the
  promise underneath the restriction: if this project goes somewhere you would rather not
  follow, you can take it and go.

  This is source-available rather than OSI open source — the Open Source Definition does not
  permit a licence to rule out a field of use, even one. **Versions up to and including 0.5.0
  were published under AGPL-3.0-or-later and remain so**; nothing has been withdrawn.

- **A stopping server no longer waits indefinitely for requests already in flight.** It gives
  them 15 seconds and then exits. Uvicorn's default is to wait for ever, so one request blocked
  on a database lock meant the service could not be restarted at all — `systemctl restart` hung,
  `systemctl stop` hung, and it went away only when systemd's 90-second `TimeoutStopSec` killed
  it, which is the moment an operator is least able to wait.

  **If you run it under systemd, set `TimeoutStopSec` to something longer than 15 seconds** —
  the sample unit in [docs/hosting.md](docs/hosting.md) now uses `30s`. Shorter and systemd
  would kill a shutdown that was about to finish.

- **`subroutine db upgrade` names the version it is upgrading to.** The report opened with two
  schema revisions, which cannot tell "the new code carries no migration" from "the new code was
  never installed" — both print `Nothing to do.` It now begins `Subroutine 0.5.0 expects schema
  …`, and says so plainly when the installed copy is a development build, because that is the
  case where `pip install --upgrade` silently declines to replace it.

- **Six more short names are reserved for workspaces**: `app`, `healthz`, `mcp`, `readyz`,
  `signin` and `v1`. Now that a workspace's short name is the start of its web address, one
  named after an address the instance already answers could be created, listed, and never
  opened — `example.com/mcp` reaches the agent endpoint, not your workspace.

  **Existing workspaces are untouched**; the rule applies when one is created or renamed. If
  you have a workspace with one of these names, it goes on working and is still worth renaming.

### Fixed

- **`GET /v1/meta` refuses a query parameter it does not accept, instead of ignoring it.**
  `?workspace=projects` — the spelling every agent tool uses, where this endpoint takes
  `workspace_id` — was discarded in silence and answered with empty vocabulary maps, which is
  exactly what an instance with no custom vocabulary looks like. It now says which name it
  wants, as every listing already did.

  A caller believed the empty answer, concluded there was no way to close an item as a
  duplicate, and deleted a task rather than cancelling it.

  The bare call, with no workspace named, is unchanged: it still answers and lists the
  workspaces to choose from, because a client's first call is often that one.

- **A search for two words no longer finds nothing.** `q` matched the whole query as one
  contiguous, ordered piece of text, so `vocabulary entries` found an item and
  `vocabulary seeded` did not — although both words were in it, four words apart — and
  `entries vocabulary` did not either, because it was reversed. Every word is now looked for
  separately, in any order, and may appear in the title or the description.

  **It failed in the direction that costs you something.** An empty result reads as "this does
  not exist", so the natural next step is to file the thing you were looking for — on the one
  path that exists to stop duplicates being filed. It was found by an agent searching this
  project's own backlog before adding to it, which nearly filed two.

  A search of nothing but spaces now narrows nothing, instead of matching every item
  containing a space. A search asking for more than sixteen words is refused and says so.


- **A `.subroutine` marker written before project keys became lower case is now reported.** The
  check that tells you a marker has gone stale compared the two keys case-insensitively, which
  is how they *resolve* — so a file saying `WEB` against a project stored as `web` matched, said
  nothing, and went on stating a spelling this program no longer writes anywhere. You will see
  one line under the next capture in such a checkout, saying what the key is stored as and
  offering `subroutine use --here --project …`. Nothing is broken and the marker keeps working;
  markers this version wrote already agree and stay silent.

- **A raw `subroutine_call_api` whose answer is too long to report no longer reads as a failed
  call.** The size limit is applied after the request has been made, so on a write the change had
  already happened while the agent was told it had not — and the obvious next move is to send it
  again. The message now says the instance answered, with the status, and that repeating a write
  would change things twice rather than report them once.

- **The day you planned something for is confirmed when you add it.** `subroutine add "Fix the
  sink on monday by friday"` read Monday, filed it, and told you only about Friday — so the one
  line you read to check what was understood was silent about half of it. The two are now
  reported together, `(for Mon 10 Aug, due Fri 14 Aug)`, the same way a deferred task already
  showed both its dates.

  **An agent was told nothing at all**, because the line it gets never mentioned a planned day
  under any circumstances. That is the surface where it matters most: the guidance an agent
  follows names that line as the way to check, so one doing as it was told could not tell a day
  set correctly from a day set wrongly — and a wrong day is not discovered until it has passed.

## 0.5.0 — 2026-08-07

### Added

- **An instance serves MCP itself, at `POST /mcp`.** An agent can now reach it with a URL and a
  token and nothing installed — no Python, no package, no connection file. That is what a Claude
  Code plugin needs to point at a Subroutine instance somebody else runs, which is the case this
  was built for: you are told a URL and given a token, and you get to work.

  Authenticate with the token you already issue — `Authorization: Bearer sr_…`, the same
  credential the HTTP API takes, narrowed by the same scopes and the same workspace pin. Add
  `?workspace=` when the instance has more than one and you want a default.

  The tools, the resources and the refusals are the ones `subroutine mcp` has always served;
  what is new is that they no longer require the caller to be running the program. `GET` on the
  endpoint answers `405`: this server has nothing to push, so there is no event stream to open.

- **A second plugin, `subroutine-remote`, for when the work is on somebody else's server.** Two
  fields — the address and the token — and nothing to install:

  ```console
  $ claude plugin install subroutine-remote@subroutine
  ```

  Install `subroutine` if your work lives on this machine and `subroutine-remote` if it lives on
  a server; each listing says so and points at the other. Until an address is filled in the
  remote one sits idle rather than reporting a fault, so it can be installed before anybody has
  told you where to point it.

  They carry the same skill, and its guidance on missing tools now establishes *which* plugin is
  installed before offering a remedy — every remedy for one is wasted effort on the other.

- **Reaching a server from your own machine is a command now**, rather than two files somebody
  hand-edits:

  ```console
  $ subroutine connections add work --url https://tasks.example.com
  Token for work:
  Reached hpz2g4 as si, in acme.
  ```

  It asks for the token, reaches the instance with it, and writes nothing until that works — so
  a mistyped address, a revoked credential or a proxy answering instead of the server is refused
  where you can fix it, rather than becoming one line of failure among tomorrow's results. It
  tells you the name that instance knows you by, which is the only thing that confirms you
  pasted the token you meant to.

  On a machine with no list of its own — a second laptop, a workstation whose work is all on the
  server — it also makes that connection where new work goes, and says so. `--default` asks for
  that anywhere. `--read-only`, `--token-env` and `--token-command` are the rest; there is no
  `--token`, because a credential given as an argument lands in shell history and in the process
  list. Pipe it in instead.

  A second name for a server this machine already reaches is refused, by name. Two of them make
  every merged listing count that instance's work twice, and finding out at the listing means
  being told nothing at all until a file is edited.

- **[docs/connecting.md](docs/connecting.md), organised by which of five situations you are
  in** rather than by how the software is built. Your work is on this machine or on a server;
  you are at a terminal or you are an agent — those two questions decide everything, and the
  answers were spread across a hosting guide, a README and two plugins' settings fields.

  It opens with the three things to ask for if somebody else runs the instance: the address, a
  token, and — the one people forget — **which workspace, if the instance holds more than one**.
  Without that a session's first read is refused, and an agent has no way to guess.

  Claude on the web is in it too, saying plainly that it is not built and why. A page that
  covers four situations and goes quiet on the fifth reads as an oversight.

- **`subroutine explain connecting`**, so the same question has an answer without leaving the
  terminal.

- **`subroutine list --connection work`** shows only what is on that connection, and
  `subroutine search` and `subroutine ls` take it too. `-c` before the command moves where a
  *write* goes and deliberately does not narrow what you can see — forgetting which context you
  are in should never cost you a missed item — so asking for one instance on purpose had no
  spelling at all, and meant turning the others off in `config.toml` and putting them back.

  It is a filter rather than a context: nothing durable changes, and addresses still name their
  connection, so a row you copy out of a narrowed listing still reaches the right instance once
  the flag is gone.

- **`GET /v1/meta` reports `instance_version`** — the release the instance is running, the same
  value `/v1/me` has carried since 0.3.0. It is the first thing every client fetches, which is
  what makes it the right place to find out what you are talking to.

### Fixed

- **The MCP endpoint no longer deadlocks against itself.** A request to `POST /mcp`
  authenticates its caller twice — once for the request, once for the client that acts as
  them — and both wrote the same `last_used_at` timestamp on the same credential. The first
  write held a row lock until the request finished; the second waited for it; the request
  could not finish. One request was enough, with nothing else running.

  It looked intermittent because that timestamp is only written once a minute: inside that
  window neither write happens, so the endpoint worked, then stopped, then worked. Once it
  stopped, every later call queued behind it and the service could not even be shut down
  cleanly.

  A request now counts its credential's use once, which is what it should always have done.

- **A checkout still knows where it belongs when the connection is called something else on this
  machine.** A `.subroutine` file records a connection name, and that name is each machine's own
  nickname for a server — so two machines sharing a filesystem read one file and have to agree.
  When they did not, the whole marker was ignored, not just the connection:

  > .subroutine here names connection 'their-name-for-it', which is not configured. Using 'local' instead.
  > local has several workspaces, so there is no way to tell which one this is about.

  — asked which workspace, with the answer sitting in the file. The marker also records the
  workspace's permanent identifier, so it is now looked for on the connections this machine does
  have:

  > .subroutine here names connection 'their-name-for-it', which is not configured — its workspace
  > is on 'work', so that is where this goes.

  Matched by identifier only, never by name: a project called `sr` here is not the `sr` on
  somebody else's server, and that distinction is what keeps work from being filed in the wrong
  place. If two connections turn out to hold it, nothing is guessed and the old behaviour stands.
  This also covers renaming a connection in `config.toml`, which needs no shared filesystem at
  all.

- **An instance running a different release says so, instead of being reported as not being a
  Subroutine instance.** When a server was a release behind, the response it sent could be
  missing a field this program expects — and what you saw was:

  > work answered, but not as a Subroutine instance: Me could not be read from its response
  > (user.is_active: Field required).
  >
  > Check what is serving https://… — a proxy, a captive portal or an instance on a different
  > API version will answer like this.

  About an instance you had been talking to all week, sending you to look at proxies. Running
  different versions on different machines is the ordinary arrangement, not a fault. Now:

  > work is running 0.4.0 and this program is 0.5.0, so they disagree about what a response
  > contains: Me could not be read from its response (user.is_active: Field required).
  >
  > Update whichever is older. Until then this connection works for anything the two versions
  > still agree about.

  An instance too old to say which release it is still gets the original message, because then
  a proxy or a typo'd address really are the likelier explanations.

- **A raw call with a method the instance does not answer to is refused the same way over both
  transports.** `subroutine_call_api` passed the method straight through, and what came back
  depended on how you were connected. In process, anything unrecognised reached the router and
  got a `405`. Over a network, `BREW` was a `400` from the server, and a method containing a
  newline or a stray space failed inside the HTTP library and was reported as:

  > work could not be reached at https://…: Illegal method characters
  >
  > Check that the instance is running and that you are on a network that can reach it.

  Which blames the network for something the caller typed. The method is now checked where it is
  read, so both sides answer identically and name the argument:

  > `'BREW'` is not a method this instance answers to.
  >
  > Methods you can use: DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT.

  Nothing was ever smuggled onto the wire — the HTTP library refused those methods before
  sending. This is about two transports giving one input two answers, and about a refusal saying
  what is actually wrong.

- **Two connections to one instance no longer stop you reading anything at all.** When two
  connections turn out to be the same server, a *merged* read would count everything twice, so
  it is refused. That check ran on every command that opened a connection — including
  `subroutine whoami`, which prints a line per connection and combines nothing, and
  `subroutine list`, which groups by connection and would have shown the collision plainly.

  So the one situation where two names for one instance is deliberate — copying between them,
  and checking the copy while the original is still there — was also the situation where
  nothing worked. `list`, `show` and `whoami` now answer; `today` merges into one set of buckets
  by design and still refuses, which is the case the check was written for.

- **A machine whose work is on a server is no longer told to set up a second instance.** The
  `db` commands and `serve` act on a local database, and where there is none they said:

  > Nothing has configured `database_url`, so this is the default. Run `subroutine init` to set
  > an instance up here…

  On a machine that reaches your instance over a connection — a laptop, a second workstation,
  the arrangement [docs/connecting.md](docs/connecting.md) describes — following that advice
  builds an empty second instance beside a connection that already works, and nothing tells you
  there are now two. It now says which instance the machine does reach, that the command wants a
  local database, and that `init` would give you a second one:

  > This machine has no instance of its own; it reaches work. This command acts on a local
  > database, so run it where that instance lives — `subroutine init` would set up a second,
  > empty one here.

  `init` is still the advice when nothing is configured at all, which is the situation it is
  right for.

- **An MCP tool argument is checked against the type its schema declares.** The schemas were
  published and never used as schemas, and one half of that failed silently: a `true`/`false`
  argument given the *string* `"false"` is truthy in Python, so `subroutine_list` with
  `{"today": "false"}` turned the filter **on**. The agent asked for it off, got a plausible
  answer, and had nothing to notice.

  The other half was loud and useless — a whole-number argument given text reached a comparison
  and the Python message came back: `'<' not supported between instances of 'str' and 'int'`.
  `subroutine_changes` was the likeliest to hit it, because `since` takes a *seq* and a date is
  the obvious guess.

  Both are refused by name now, saying what the argument takes. Refused rather than coerced,
  both kinds: `"5"` for a number could be read, and accepting it would teach an agent that
  strings are sometimes fine — when the case where they are not is the case that fails quietly.

- **An item can be named the way this program prints it, and the schema now says so.** Seven
  arguments were declared as whole numbers and have always accepted `"#42"` too, since that is
  the form every listing returns. A client obeying the published contract could not send it.

- **A refusal an agent reads now names a tool rather than a shell command.** `subroutine_update`
  on a task that is not there said *"Run 'subroutine list' to see what there is"* — advice a
  remote agent cannot follow, since it has no terminal and no copy of the program. That is the
  whole premise of reaching an instance with a URL and a token.

  The commands the layers below name are translated to the tools that do the same thing. Two
  are deliberately left alone: an instance that has not been created and a database that cannot
  be opened are fixed by whoever runs the machine, and naming the command is what says so.

- **A refusal an agent reads now names a field that agent can actually pass.** On an instance
  holding more than one workspace, a session that had not been told which one had its first read
  refused with *"Pass `workspace_id`"* — which is right in a problem document and is not an
  argument any tool declares. Following it produced a second refusal.

  The tools' own vocabulary is used now. The refusal below the transport carries the field
  rather than spelling it in prose, so the HTTP API still says `workspace_id` and an agent is
  told `workspace`.

### Security

- **The plugin no longer tells you your token is in your system keychain.** Both plugins' token
  fields, the README and the connecting page all said *"Stored in your system keychain, never
  in a settings file"*. On Windows it is in a plaintext file under your home directory —
  measured, not inferred.

  The client decides where the credential goes, and we described that decision without
  checking it. It now says that your editor stores it rather than Subroutine, that on Windows
  that means a file, and that it should be treated like any password on that machine. macOS
  and Linux are not described, because they were not measured.

  If you read the old sentence and concluded the file needed no protecting, it does.

  Plugin manifests at 0.4.4 and 0.4.5.

- **A raw API call can only be pointed at the API.** `subroutine_call_api` accepted any path on
  the instance, including `POST /mcp` — the endpoint that hosts `subroutine_call_api`. One
  authenticated request could nest a call inside a call inside a call, and each level occupied a
  worker thread while it waited for the next.

  Measured on a served instance: five levels answered in five seconds, twenty did not answer in
  thirty, and while a thirty-level request was in flight `/readyz` — public, and touching nothing
  — timed out twice. **A read-only credential was enough**, because the nesting sits below every
  scope and permission check.

  Paths must now begin with `/v1/`. Nothing about what a credential may *do* has changed: this
  says where the escape hatch may be aimed, which nothing said before.

  The three routes this excludes are `/healthz`, `/readyz` and `/mcp`. The first two are public
  and tell a credentialed caller nothing it cannot already ask for; the third is a transport
  rather than a route.

### Changed

- **Setting the plugin up is now written down, including the step that looks like a broken
  install.** `docs/connecting.md` said to "open its settings" and never said where they are:
  in a terminal, `claude`, then `/plugin`. There is no `claude plugin configure` subcommand,
  and `/plugin` is not available in the VS Code extension, so a terminal is currently the only
  place the address and token can be entered — once, after which every session reads them.

  It also now says to reload the window afterwards, and how to check it worked. A session that
  was already open when you configured the plugin keeps the tool list it started with, so
  `claude mcp list` reports the server connected while the session has no tools at all.

  The skill's troubleshooting ladder gained the same rung, at the top: every rung it had
  tested the configuration, and this is the one case where the configuration is correct.

  Plugin manifests at 0.4.3 and 0.4.4.

- **The `subroutine-remote` listing no longer suggests adding the instance as a connector.**
  It said *"to reach an instance from claude.ai, add it there as a connector instead"* — and
  doing that reaches a dialog offering a URL and OAuth client credentials, with nowhere to put
  the token you were given. It now says why the connector route is not available yet, so
  nobody spends an afternoon finding out.

  Plugin manifest at 0.4.3.

- **`subroutine mcp` is a transport adapter now: the tools come from the instance.** It reads a
  message, has it answered by the instance the connection names, and writes the answer back. It
  builds no tools of its own.

  Before this, an agent's tools came from whichever version of the package happened to be
  installed on the *calling* machine, while the same instance served a different set over HTTP
  to anybody using the remote plugin. Two implementations of what a tool call does, and which
  one answered depended on something nobody was tracking.

  **Nothing about using it changes.** The same command, the same options, the same tools. What
  changes is where they come from: point it at a server and you get that server's, so upgrading
  the instance upgrades every agent reaching it.

  A connection with no server — a plain SQLite install — drives this application in process,
  so there is still nothing to run and nothing to configure. It costs about **0.3 seconds** at
  session start on this machine, once, for the web stack that path now loads.

  **An instance older than this program has no such endpoint**, and says so plainly rather than
  looking like MCP is broken: *"work does not serve MCP. That instance is older than this
  program."*

- **`subroutine` the plugin now says it is the one that runs a program on your machine**, in its
  own description and in the marketplace listing, because there is now a choice to get wrong.

## 0.4.0 — 2026-08-06

> **This release changes the database schema**, to `c858f2942244`.
>
> Install it, then run `subroutine db upgrade`. That reports both versions, takes a
> verified backup, migrates and checks the result — in that order. Stop the service
> first if you are running one; expect it to be down for the length of the migration.

### Added

- **An agent working over MCP can read the guide written for it.** The agent guide and its
  worked examples are offered as MCP *resources*, so a client with no shell and no HTTP of its
  own can open them.

  They were reachable only over HTTP, which an agent using the tools may have no way to call —
  so §13.3's guide, written specifically for a caller arriving with a base URL and a token, was
  the one thing that reader could not get. It says what a ref is, what to read first, and what
  is not built yet.

  **Resources rather than another tool**, because a tool's schema is context every session
  carries whether it is used or not, and the surface is already at its budget. A resource costs
  a line in a list and its content only when something asks for it — nothing is fetched until it
  is opened.

- **An agent can be handed to somebody else.** `subroutine user transfer deploy-bot --to jo`.

  Agents stop when the person answerable for them leaves, so this is how one is kept when
  somebody goes — which makes it part of the leaver story rather than a separate feature. Only a
  person can take an agent on: being accountable is something somebody agrees to, and an agent
  cannot agree on anybody's behalf.

  Handing an agent to something that already answers to it is refused, because that is a circle
  where every record resolves and nobody answers for anything.

- **Somebody can be marked as having left, and the agents answerable to them stop.**
  `subroutine user deactivate thomas`, and `reactivate` to bring them back.

  Their account stays and so does everything they wrote, still attributed to them. What stops is
  their credentials and every agent that answers to them — because somebody gave those agents
  permission to work, and that permission was this person's to give.

  **It names what it will stop before doing it**, including agents created by other agents,
  because a deactivation that silently kills a shared worker is how people learn to stop
  deactivating leavers at all.

  The last person who can administer the instance is refused: one nobody can administer cannot
  be repaired from inside, and under the accountability model it would stop every agent at once.

  `is_active` had been enforced in four places and settable in none, so *this person has left*
  was a state the product could not reach while several code paths were written as though it
  could.

- **A task records who assigned it.** `assigned_by_id` answers the plain question a person asks
  of their own list — *who put this in my queue* — and it is what a hand-back reads when an agent
  cannot finish something and needs to send it to whoever asked for it.

  Taken from whoever made the change rather than accepted from the caller, because an assigner a
  client could set would be a claim about an act rather than a record of one. It moves only when
  the assignee actually changes, so a `PATCH` that happens to carry the same assignee alongside
  an unrelated edit cannot quietly replace somebody else's name; and it clears with the assignee,
  because an assigner with no assignee names nobody.

  **It is not a history.** The event log already carries every assignment change with its actor
  and its order, and that stays the record.

- **An agent records the person answerable for it.** Somebody gave an agent permission to work,
  and that somebody answers for the result — so a service account now names a responsible
  account, and following that chain from any agent reaches a person.

  Two rules make it worth having. The chain must **terminate at a person**, so one that loops or
  never reaches one is refused rather than stored. And it is **inherited, never chosen**: an
  agent that creates a sub-agent becomes the link that sub-agent answers to, and letting the
  creator name somebody else instead would let an agent make work traceable to a person who
  authorised none of it.

  The chain records the delegation *path* rather than collapsing it, so deactivating an agent
  partway along stops everything it created, and walking to the end still says who is
  ultimately accountable.

  Existing service accounts are adopted by the migration where the answer is unambiguous — the
  sole active superuser. Where an installation has several administrators there is no unambiguous
  answer, the migration says so by name rather than guessing, and those agents need one set
  before they next authenticate.

- **`subroutine add --description`, and `description` on the agent's `subroutine_add`.** A task
  filed with its reasoning, in one call, on the surface where you have the most context about it
  — the moment you decided it was worth filing.

  It had been reachable only afterwards, by updating an item that already existed, and only from
  an agent's tools at that. `POST /v1/tasks` has accepted a description beside a captured line
  since the first release; neither client passed one, so nothing that read a line could supply
  it and no test of the endpoint could have noticed.

  **This matters more than one missing flag, because the guidance depended on it.** The agent
  skill argues for titles that say what will be true when the work is done rather than what is
  wrong today — on the grounds that the reasoning is not lost, since it "belongs in the
  description, which is one field away". It was not one field away, so following that advice
  meant filing a title with the reasoning nowhere. Reported by a coding agent on a fresh install
  that had done exactly that, six times, and explained why its own titles had become unreadable.

- **A listing says which work is blocked.** `subroutine list` and the agent's
  `subroutine_list` now mark an item something unfinished is in the way of, and `blocked` is on
  the task in the API.

  `--ready` has filtered blocked work out since the first release, but the listing you get by
  typing nothing is the one you actually read — and it would show a blocked item above the very
  task blocking it, with no way to tell. Reported by an agent that read its own backlog as
  "start with #2".

  **The ordering is unchanged**, which is what was asked for and is also right: newest-first is
  what the default listing promises, and re-sorting it by readiness would answer a question
  nobody asked. A row that says why it is not the one to start stays true under every order.

  The column disappears entirely when nothing on the page is blocked, so a to-do list that has
  never linked two items looks exactly as it did.

- **A listing can be narrowed by the things it already accepted.** `GET /v1/tasks` has taken
  an assignee, a status, a type, a subtree and a deadline range since M1, and no client passed
  any of them — so from the CLI, from an agent's tools, or from a script, the only way to ask
  *"what is assigned to Simon"* was to fetch everything and sift it yourself.

  **The assignee filter takes a username**, not an id: `?assignee=simon`. It was `assignee_id`
  and took a UUID only, which made the question one you had to already know part of the answer
  to. Renamed rather than widened, because a parameter called `_id` that takes a name is a
  third thing to learn — and nothing could pass the old spelling anyway.

  Documents gained `status` and `type`, which is what makes §6.14's lifecycle usable: a
  document is *draft*, then *active*, then *superseded*, and asking a workspace for its active
  decisions is how you find the rules you are working under. Projects gained `parent`,
  `visibility` and `include_archived`.

- **A project key is lower case, and may contain hyphens.** `web-sales` rather than
  `WEBSALES`. The Inbox every installation starts with is now `inbox`.

  A workspace short name has always been lower case, so an address like `work/acme/#42`
  had its two halves shouted differently for no reason anybody could infer. Nothing depended
  on the case, and what you type is still case-insensitive — `+SR` and `--project SR` go on
  working, they are just stored and shown as `sr`.

  **Your existing keys are rewritten by the migration**, because every lookup compares what
  you typed against what is stored, and an instance left half-converted would report that
  none of your projects exist.

  A key may now be up to 32 characters, up from 16, which is what makes a hyphenated
  compound like `service-marketing` possible at all.

- **Work can be handed over after it is filed.** `subroutine update 42 --assignee jo`, and
  `assignee` on the agent's `subroutine_update`. A deadline, tags and a timezone can be
  changed too — all four were accepted by the API since the first release and passed by no
  client.

  A task could be assigned *when it was filed* — `@jo` in a captured line has always worked —
  and never afterwards, so work could not be passed between two people or two agents once it
  was under way.

  **By username, not by id**, on `POST` and `PATCH` alike: a caller holding a UUID for a
  person has already made a request they should not have had to. Somebody who is not a member
  of the workspace is refused by name, with the members listed, because they could not see the
  work you were handing them. `--assignee ''` hands it back to nobody.

- **An agent is told what this workspace has decided, before its first write.** A new
  `subroutine://conventions` resource lists the decisions in force here — how work is filed,
  what a title has to say, what needs an item first — and the server instructions every
  session receives now name it.

  The problem it closes, measured on this project's own instance: 57 governing documents open,
  and the one file a session is guaranteed to read named 24 of them. Ten decisions were
  reachable only by searching, and nothing prompted a search.

  **Titles and refs, never bodies**, so it costs about two kilobytes and only when something
  opens it.

- **A decision, a finding and a dead end are in force the moment you write them.** They now
  start as `active` rather than `draft`; a specification or a design still starts as `draft`,
  because that is a document's life and not a conclusion's.

  A decision that has been taken is in force, and calling it a draft is wrong the second the
  conversation ends. One lifecycle had been applied to all six document types, and the result
  was that nothing had ever been marked active — the status vocabulary was specified, seeded,
  published and used by nothing.

  `subroutine doc create --status draft` for a decision you are still thinking about, which
  is now reachable from a client for the first time.

  **`subroutine list --assignee`, `--status` and `--type`**, on `ls` as well. A status and a
  type belong to one kind of item and one workspace, so `--type bug` returns no documents
  rather than all of them, and `--status active` — which no task has — returns the documents
  in force rather than a refusal. A key *neither* kind has is still refused by name, because a
  typo that reads as an empty list is indistinguishable from having nothing to do.

### Fixed

- **Reading something you deleted no longer fails in the words of a command you did not run.**
  `subroutine show` on a deleted item answered *"There is no task here to comment on"* — the
  comment command's refusal, on a read, naming neither the item nor anything to do next.

  Underneath it, reading an item and reading its record disagreed about the trash: the item
  resolved and its comments did not. An item's record is still its record once the item is
  deleted, so it can be read; what is refused is *adding* to it, and that now says the item is
  in the trash and that restoring it is what you want.

- **An agent can plan or defer a task from a machine outside UTC.** `subroutine_update` with
  `plan` or `defer` failed on any machine whose timezone abbreviation is not also a zone name —
  `'BST' is not a timezone`, and the same for `PDT`, `CEST` and `AEST`. It read the machine's
  zone off a timestamp, which gives the abbreviation rather than the name; it now reads it the
  way `init` does.

  It worked in UTC, and in London in winter, which is why it survived: `UTC`, `GMT`, `EST` and
  `MST` happen to be valid zone names. Found on a fresh install on somebody else's laptop, in
  August.

- **The agent skill teaches the project key rule this version actually has.** It said keys were
  uppercase, letters and digits only, and at most sixteen characters — all three untrue since
  the previous release, and contradicted by the example printed directly beneath it. An agent
  asked for a project called "Claude Test" produced `claudetest`, which is what it had been
  told to do. Keys are lower case, take hyphens between words, and run to 32 characters.

### Security

- **The three routes an agent's raw API call cannot reach are now genuinely unreachable.**
  Creating a workspace, renaming one and moving a project are kept off `subroutine_call_api`
  because they are consequential, cannot be undone, and the command line asks first. The check
  matched the spelling of a path rather than the route it named, so `POST /v1/workspaces?x=1`,
  `/v1/../v1/workspaces` and `/v1/%77orkspaces` all went through and created a workspace.

  It now matches the same path templates the application registers, using the same matcher the
  router check uses, against every form the request could arrive in. A denied entry that names
  no real route fails the build, so renaming a route cannot quietly disarm it.

  **This was never a way to exceed a credential**: the permission was still required, and
  anybody who can run the CLI could always do all three. What was defeated is the promise that
  a person is asked first. Found by review, not reported in the wild.

- **A raw API call refuses anything that is not a path on the instance it is aimed at.** Given
  a whole URL, the underlying HTTP library treats it as a replacement rather than a path — and
  the connection's token travels with it. Nothing could reach that today, because the agent
  tools already required a leading `/`; the check now lives with the credential it protects
  rather than a layer above it.

### Changed

- **`subroutine upgrade` is now `subroutine db upgrade`, and the old spelling is gone.** If you
  have it in a script or a runbook, change it — there is no alias, deliberately.

  The reason there is no alias is that `db upgrade` used to mean something else: the raw
  migrator, with no backup, no confirmation and no version report. That is now
  `subroutine db migrate`. Keeping `subroutine upgrade` working would have left one spelling
  meaning two different things depending on when you learned it, and the two differ by whether
  your database is backed up first.

  Everything to do with the database is now under `db`, with no exception to explain — and a
  top-level `upgrade` stops reading as *upgrade the software*, which is the one thing it has
  never done.

  Typing the old spelling tells you where it went rather than running anything.

- **The package says it runs on Linux, where it used to say "OS Independent".** Nothing had
  ever been run anywhere else — every CI job on both workflows is Ubuntu — so the claim was a
  hint to an index rather than anything anybody had checked. It is not a statement that
  Subroutine fails elsewhere; it is a statement about what has been demonstrated. macOS and
  Windows are tracked separately and neither has been tried.

### Removed

- **Three settings that did nothing.** `trash_retention_days`, `events_retention_days` and
  `require_verification_to_complete` were declared, printed by `config show` and described in
  the specification — and read by nothing anywhere. Setting one produced no error, no
  behaviour and no way to find out.

  Nothing changes for you: they never did anything. An old `config.toml` that names one is
  still fine, since unknown keys are ignored. They come back with what enforces them —
  §6.9's purge, §5.11's retention floor, §6.12's evidence gate.

### Fixed

- **Every setting `config show` prints is now documented.** `docs/hosting.md` has a table of
  all of them and what each is for, which is what `config show` has always sent you looking
  for. Fourteen were described nowhere. A test compares the two, so a setting that exists and
  is not described cannot ship.

- **`subroutine db backups` says what each backup holds.** A backup of an empty instance has a
  correct size and a correct schema head, so a listing showing those gave you nothing to be
  suspicious of — and hollow copies sort to the top, because they are newest. Restoring "the
  latest backup" could get you an empty database.

  Each backup now records what the source held at the moment it was taken, beside the copy,
  and the listing reads it back. Backups taken before this say **holdings not recorded**
  rather than claiming to hold nothing — those are different facts and only one of them is
  known.

- **The MCP documentation resources say which workspace they are describing.** On an
  installation with more than one workspace and no workspace configured — the state an agent
  arrives in — `subroutine://meta` published `statuses: {}` and `item_types: {}`, from the one
  document whose job is publishing them, and `subroutine://conventions` refused with advice to
  pass `workspace_id`. A resource takes no arguments, so neither reader could act.

  The vocabulary sections are now left out rather than reported as empty, with a line naming
  the workspaces and how to ask for one; the rest of that document is the same in every
  workspace and is still there. The conventions index says the same thing in prose. Set the
  plugin's `workspace` and both answer in full as before.

- **A project listing can be sorted from a client, not only over raw HTTP.**
  `GET /v1/projects` had accepted `?order=` since it was written, and no client could pass it,
  because the sort vocabulary was declared inside the HTTP layer where one transport could see
  it. It now lives beside the task and document vocabularies, so `projects(order="-key")`
  works on both. Asking for nothing still gives you the tree, parents before children.

- **An item now says who it is with.** Work could be handed over on every surface and no
  surface reported the result: `subroutine update 1 --assignee jo` answered *"Changed"*, and
  `subroutine show 1` then printed the priority, the deadline and the tags and never mentioned
  jo. The view carried an `assignee_id`, which is a UUID — not what anybody types and not what
  a line has room for.

  A username is now loaded beside the statuses and the tags, in one query for the page rather
  than one per row, so `show`, the listing and `?format=compact`'s `@assignee` all follow. A
  list nobody delegates on is unchanged: the column is dropped when no item has one.

- **The plugin says which clients it can run in.** It starts Subroutine as a program on your
  own machine, so it works in Claude Code and the desktop apps and not on the web. That was
  always true and was written down nowhere.

  Installed in a browser it reported success, opened a settings page with all four fields
  present, and produced no tools — and an absence of tools is what a broken product looks
  like. Neither of the two things you would try next, checking your `PATH` and installing the
  program, could have made any difference. The limit is now in the marketplace listing, which
  you read before installing, and in the manifest, which you read after.

- **A backup written to a network volume is no longer reported as having failed.** If your
  `backup_directory` is on a mount whose files the Subroutine account cannot own — CIFS with
  `forceuid`, NFS with `root_squash`, and most other shares — every backup was written
  perfectly and then announced as an error: `Operation not permitted`, or a `503` from
  `POST /v1/admin/backups`. The file was there, complete and restorable, all along.

  It was the copy step, which used to bring the file's timestamps and permissions with it.
  Those are the parts a share like that refuses, and they are refused *after* the data has
  arrived. A backup's name already carries the moment it was taken and the schema it holds, so
  nothing was gained by copying them.

  **Worth checking your backup directory if you have seen this**, because the natural response
  was to run it again: each attempt left another complete copy.

- **A copy that fails part way no longer leaves a short file behind.** Verification already
  deleted a backup that arrived truncated, but a copy that raised on the way never reached it.

- **The line `add` echoes back cannot be mistaken for part of the title.** It confirms which
  parts of what you typed were understood as shorthand rather than left as words — the only way
  to tell that `+WEB` filed something rather than becoming part of its name — and a double space
  was all that separated the two. Worse for an agent, whose listing already shows the priority,
  so `!4/3` could appear twice on one line with two different meanings and nothing to tell them
  apart. It now reads `(read +WEB !4/3)`.

  Reported by the same agent, which called the echo genuinely useful and genuinely ambiguous.
  Both are fair.

## 0.3.0 — 2026-08-04

> **This release changes the database schema**, to `d5d0458f5ad5`.
>
> Install it, then run `subroutine upgrade`. That reports both versions, takes a
> verified backup, migrates and checks the result — in that order. Stop the service
> first if you are running one; expect it to be down for the length of the migration.

### Added

- **Claims — two workers cannot both take the same task.** `subroutine claim 42`, `release`,
  and `subroutine_claim` for an agent. Until now `--ready --order -priority_score` answered the
  same for everybody, so two workers asking the obvious question collided by construction — and
  the cost is not a merge conflict, which git handles, but both of them doing the same work and
  one finding out at the end.

  **A lease, not a lock.** It expires, and an expired one is ignored rather than needing anybody
  to clear it: workers die mid-task, and a claim that outlived its holder would strand the work
  permanently. Say it again while you are still going. Nothing has to run for a task to come
  back.

  Work somebody else holds disappears from a ready listing until their claim runs out, and your
  own never disappears from yours. Claiming something held is refused by name and by time —
  who, and until when — because those are the two facts that decide what you do next. Anybody
  who can change a task can release it, not only the holder, since the case it exists for is a
  worker that died holding one.

  `claim_lease_minutes` is read for the first time; it has been a documented setting that
  nothing consulted since the first release.

- **`subroutine agent create` — one command sets an AI agent up as a principal of its own.**
  The account, its membership and its credential in one act, because they are one decision: an
  account with no membership authenticates and can do nothing, which reads as a broken token
  rather than as a missing role.

  It then prints the environment line to set, and **that line is half the work rather than a
  convenience at the end**. An agent that can run shell commands reaches an instance two ways —
  through the tools its editor wired up, and by running `subroutine` itself — and those resolve
  credentials separately. Give the token only to the editor and the agent is itself over the
  tools and *you* in its shell, which is worse than plainly acting as you: half its work is
  correctly attributed, so a spot check finds its name and concludes the setup worked.

  The credential is checked by being presented rather than described, so a scope naming a
  permission the role does not carry shows up here instead of on the agent's first call. And
  the closing line says who the agent's shell still acts as until the variable is set, because
  a setup that looks finished and is not is this project's most expensive shape.

- **Credentials can be administered from a machine that holds no database.** `token create`,
  `token list` and `token revoke` opened a local database directly, because the commands that
  administer credentials have to work when the service will not start. Where the work lives on
  a served instance there is no local database to open, so the three commands you need in order
  to set an agent up were the three that refused — on the machine you were setting the agent up
  on.

  They now go through whichever connection your next write would go to, and open a database
  directly only when that connection is local. Both properties survive: administering a server
  from a laptop works, and so does administering a server whose service is down, because
  reaching a local database never involved the service.

  `token create` also gained the whole of setting an agent up in one call — the account, its
  membership and the credential, in one transaction rather than three requests with a
  half-finished agent if the second failed.

- **An agent session can be told which workspace it works in.** On an instance with more than
  one, every read from an agent was refused as ambiguous — correctly, and with both names in
  the message, but there was no way to answer it: the command line carries a workspace and a
  session did not. Latent for as long as every instance had one workspace, and immediate once
  any instance had two.

  A `workspace` setting in the plugin, beside the connection, and `subroutine mcp --workspace`
  underneath it. It is a default rather than a limit: a call that names a workspace still goes
  there, so an agent can read a decision filed next door, and a token pinned to a workspace is
  what narrows access for real. The session's instructions say where work will land.

  It is deliberately not read from the current context — that is working state somebody moves
  between tasks, and a session that bound to it would land wherever it happened to point when
  the process started.

- **`subroutine_whoami` — an agent can ask which principal it is.** An agent commonly reaches an
  instance two ways at once: through these tools, and by running `subroutine` in a shell. They
  resolve credentials separately, so they can be two different accounts — and until now the only
  way to find out which one the tools were using was to write to a real item and read the author
  back. An identity check whose method is a write to production is one nobody performs before
  their first write, which is when it is worth anything.

  It reports the account, the credential by its title, what that credential is limited to, and
  the workspaces it reaches. The twelfth tool, and the surface is a budget: the argument for it
  is written into the test that holds the ceiling.

- **`subroutine token create --project KEY` — a credential that reaches one project and
  nothing else.** `POST /v1/tokens` has taken a project restriction since the first release and
  the service has enforced it; the command line, which is where somebody actually sets an agent
  up, could only narrow to a whole workspace. So the narrowest credential the surface people
  use could issue was wider than the narrowest the product supports, at exactly the moment
  somebody is deciding how far to trust an agent.

  Name the project by its key; the restriction is stored as an id, so renaming the project does
  not quietly move the credential onto whatever takes the old name. It reaches everything filed
  underneath, which the command says out loud because it is not visible in what you typed.

  A key that names a project in two workspaces is refused rather than picked, and a key that
  names nothing is refused before anything is minted — a credential scoped to a project that
  does not exist is turned down everywhere it is presented, for a reason nobody can see.

- **`subroutine whoami` — which account this machine is acting as, and what it may do.** One
  machine can hold more than one credential: yours in `credentials.toml`, an agent's in the
  environment. Nothing could ask which of them a command was about to act under, so an agent
  set up carefully could be writing as its operator and neither of them would see it.

  Prints the account, the credential by its title, what the credential is narrowed to, and the
  workspaces it reaches with the role held in each. Where a credential narrows what can be done
  in a workspace, that row says exactly what is left. `--json` carries the whole answer,
  including the permission list an agent acts on.

  `GET /v1/me` has answered this since the first release and no client could reach it — the
  guard that measures which endpoints the clients can call had two entries transposed, so it
  reported the capability as present. Both halves are fixed.

- **`POST /v1/projects/{key}/restore` — take a project back out of the trash.** Deleting one has
  always said its tasks "come back with it", and nothing brought them back: the restore added
  for tasks and documents was never given to the container both of them hang off. So deleting a
  project removed everything filed in it, permanently, by a route that read as reversible.

  Nothing touches the contents in either direction — every listing joins the project, so
  undeleting the project is the whole of undeleting what is in it. Restoring a project whose
  parent is also in the trash is refused by name, because it would clear one flag and change
  nothing anybody could see.

- **`subroutine project move` — reparent a project and everything under it.** The endpoint has
  existed since the project tree did; nothing but HTTP could reach it, so reorganising meant
  leaving the command line on a tool whose main surface is one. It counts what will move —
  projects and the items travelling with them — and asks before doing it, because this is the
  one project operation with no undo.

  `--under KEY` or `--root`, and one of them has to be said: an omitted destination once meant
  "move to root", which flattened subtrees by accident.

  Deliberately not an agent tool. Rare, consequential and irreversible is the case where being
  harder to reach is the feature.

- **Rate limiting, which was specified and did nothing.** An instance reachable over a network
  now slows a credential making requests faster than it serves them, and — separately and much
  harder — slows repeated authentication failures from one place. Both return `429` with
  `Retry-After`.

  **On unless nothing outside this machine can reach the instance**, so a to-do list on your
  own laptop is unaffected and nothing about it changes. A loopback bind alone is not enough
  to be sure of that — see the security note below — so setting `public_url` turns it on
  whatever the bind. Set `rate_limit` to say otherwise either way; `rate_limit_per_minute` and
  `rate_limit_failures_per_minute` are the two allowances, and `trusted_proxies` decides
  whether a caller behind a proxy is counted or the proxy is.

  The counters live in the serving process's memory, so an instance run under something that
  forks workers would enforce a share of the limit per worker. `subroutine serve` runs one.

- **`subroutine workspace create` — make a second workspace.** `init` named the first one and
  nothing made another, so an instance could not grow past the shape it was installed with.
  `POST /v1/workspaces` had existed since the first release with no client able to reach it.

  Numbers start again at `#1` in a new workspace, so two do not share a sequence.

- **`subroutine workspace rename` — a workspace's short name can be changed.** It could not
  be, on the grounds that the name lives in other people's notes, in shell history and in
  `config.toml` on other machines. The last of those was never true: nothing in a
  configuration file names a workspace. What remains is the same exposure a project key has,
  which has been renameable since 0.1.

  Nothing inside moves — every item keeps its number, and everything stays joined to what it
  was joined to. What stops working is anything that wrote the old name down: an address like
  `acme/42`, your current context, a `.subroutine` file in a checkout. The command counts the
  items and the people affected and says all of that before asking.

  There is deliberately no alias for the old name. A name you retired should be retired.

- **The agent guide says how to resume.** `GET /v1/docs/agent` is what an agent is told to read
  first, and it did not mention the change feed at all — so the feature built for that reader
  was invisible to anybody arriving over HTTP rather than through the plugin. It now opens a
  session with `GET /v1/changes` and explains `?since=` and `?actor=me`.

- **The skill names every tool an agent has.** `subroutine_search` and `subroutine_show` were in
  the catalogue and in nothing an agent reads — and `q` had moved *off* `subroutine_list` in the
  same change that added `subroutine_search`, so the practice described a surface in which
  searching was impossible. Every tool costs context in every session whether it is called or
  not; one nobody has been told about is pure cost.

- **The agent skill says where a document belongs.** It taught agents to write documents and
  never which project to file one under, so conclusions accumulated in the Inbox — which is
  where things go when nobody decided. It now also draws the line between what belongs in an
  instance and what stays a file on disk, and says to ask rather than guess when a conclusion
  is sensitive, because a private project is what limits who can read it and publishing cannot
  be undone.

- **A document can be filed under a different project.** `project` was accepted when a
  document was created and by nothing afterwards, so a conclusion written before anybody had
  decided where it belonged stayed in the Inbox for good.

  That matters more than tidiness: a document's project is what decides who can read it, so
  "file this where the client cannot see it" was unachievable after the fact. `subroutine doc
  edit 42 --project WEB`, or `project` on `PATCH /v1/documents`.

  Sections travel with the document they are part of. Moving one to a project in another
  workspace is still refused, by name — that rewrites the ref's tenancy and would leave the
  document pointing at another workspace's statuses.

- **`subroutine doc edit` — revise a document you have already written.** `PATCH
  /v1/documents` had existed since the first release and nothing but HTTP could reach it, so
  a conclusion recorded in an instance could never be corrected there.

  Takes `--title`, `--body`, `--type`, `--status` and `--project`. Say nothing else and it
  reads the body from a pipe, or opens the document in `$VISUAL` or `$EDITOR` when there is a
  terminal — which is what makes it usable for a document of any length.

  Naming a field changes that field and leaves the text alone, so `doc edit 42 --title "…"`
  does what it looks like.

- **`subroutine show` says how many comments there are, and prints the most recent five.**
  Every comment in full is right for the three or four an item usually has and wrong for the
  hundred it might accumulate, where a reader asking "what is this" gets a transcript. The
  count is always shown — `What happened (8, showing 5)` — so a bounded section is never a
  silently truncated one. An item with five or fewer reads exactly as it did.

- **`subroutine search` marks the word it matched.** The `matched` column already said which
  field the hit was in; on a long title the reader still had to find the word. A highlight
  rather than an encoding, so a pipe or `NO_COLOR` loses the colour and keeps the answer, and
  every occurrence is marked rather than the first.

- **`subroutine_search` — an agent can search by name.** The capability existed as a `q`
  argument on `subroutine_list`, which meant a model reading tool *names* to decide what it
  could do had no reason to think searching was possible. `q` moves off `list` onto the new
  tool, so there is one name for one thing.

- **`whoami` says which versions you are actually talking to.** A session reaches Subroutine
  through as many as three installations that upgrade separately — the editor's cached copy of
  the plugin, the program on your machine, and the instance on the far end — and nothing
  reported any of them. `subroutine whoami` and `subroutine_whoami` now end with a line naming
  each, plus the migration the database is at, and a further line when one of them is a problem.

  **Three numbers that differ is not itself a problem, and nothing says it is.** The plugin's
  version moves whenever the plugin's own contents change, so it runs ahead of the package
  between releases by design. What gets a line is the program and the instance disagreeing —
  a field one of them does not have — or the plugin being *older* than the program, which
  means its skill and its settings describe an earlier version of the tools. Where the versions
  cannot be ordered, which includes every development build, it says nothing rather than
  guessing.

  **The cost of not having it is an hour, every time.** A tool that ignores an argument, a
  field that is missing, a capability you have read about that does not appear: from inside a
  session all three look identical to a feature that was never built, and the only way to tell
  them apart was to test every call by hand. `GET /v1/me` carries the same two facts as
  `instance_version` and `schema_revision` for anything talking to the API directly.

  Both fields are optional, so a client reading an instance that predates them keeps working —
  and that case is reported as *"instance too old to say"* rather than left blank, because it
  is usually the answer somebody has come looking for.

- **`--profile` says what a credential is for.** `worker` owns one project; `collaborator`
  reads a related tree and writes one part of it; `observer` reports and changes nothing;
  `colleague` is a second person in one workspace. On `subroutine token create` and
  `subroutine agent create` alike.

  **The refusals are the feature, not the shorthand.** `--profile observer --write WEB` is not
  a narrower observer, it is two intentions in one command — so it is turned down by name,
  pointing at the profile that does mean that, rather than resolved in favour of one of them.
  A credential that quietly does something other than what you just described is one nobody
  checks a second time.

  A profile expands into flags you could have typed yourself, and can express nothing they
  cannot. Leaving `--profile` off behaves exactly as before.

- **`subroutine doctor` says whether a machine's installation is coherent.** What is running
  and where it came from, which configuration it is reading, what each connection answers, and
  when a backup was last taken — in one command, exiting non-zero if anything needs attention
  so it can end an update script.

  **The configuration lines are the point.** Nearly every confusing failure in a self-hosted
  setup is the same one: a command run without the environment the service uses, acting on a
  different database and looking exactly like success. Printing which config, data and state
  directories are in force is what makes every other line mean something.

  It survives what it is examining — an unreachable instance, a credential that is refused, a
  configuration that will not parse all become lines, and the rest of the report still prints.
  It talks only to the instances you have configured.

- **`subroutine upgrade --check` says whether a newer release exists, and whether it changes
  the database schema.** The second half is the point: a version number is something `pip
  index` prints, and what you cannot get anywhere else is whether upgrading will ask you to
  stop the service.

  **Nothing checks on its own, and there is no setting that makes it.** Asking is a thing you
  do. An instance can run for years without an outbound request.

  It reports what is *running* rather than what was installed — an editable install carries
  the version it was made at — and it knows which way a schema difference points, so a build
  ahead of the newest release is told so rather than being sent to install something older.

- **A comment can be taken back out.** `subroutine uncomment 42 "some of its words"`, and
  `remove=true` on the agent's `subroutine_comment`. Named by what it says, because a comment
  has no number of its own and its id appears in nothing anybody reads — the same reason
  `unlink` names two items rather than a link.

  Matching more than one is refused rather than guessed at, and the several are deliberately
  not listed back: choosing from a printed list means choosing by position, which is the one
  way of naming things this program does not have.

  **Deleting rather than editing, and that is the decision.** A comment is attributed prose,
  so rewriting somebody's words under their name is not a permission anyone should hold.
  Withdrawal is soft, and the mentions in a withdrawn comment stop pointing at anything — a
  backlink to a sentence nobody can read is worse than none.

- **An agent can narrow a listing to one project.** `project` on `subroutine_list` and
  `subroutine_search`. `subroutine list --project` has existed since the project tree did and
  no tool could ask, so an agent that wanted to spend its context on one part of a backlog had
  no way to say so.

  It is an argument and deliberately not a default: a `.subroutine` marker decides where work
  is *filed* and never what a listing shows, because an agent silently blind to work filed next
  door finds out by not finding something.

- **An agent can write a description.** `description` on `subroutine_update`. The skill argues
  for titles that say the outcome rather than the problem, on the grounds that your reasoning
  is not lost because it belongs in the description — *"which is one field away"*. From the
  tools it was not one field away, it was unreachable, so the advice asked an agent to leave
  its reasoning out and pointed at a shelf it could not put anything on. Reported by an agent
  that met it and put the context in comments instead, which is the wrong shelf, and said so.

### Changed

- **The plugin's MCP server is called `tools`, not `subroutine`.** The plugin and the server
  shared a name, so a call rendered as *"Plugin Subroutine Subroutine"*.

  **This changes the fully-qualified tool identifiers**, from `mcp__plugin_subroutine_subroutine__*`
  to `mcp__plugin_subroutine_tools__*`. If you have written the old names down anywhere your
  editor reads them — a permission rule, a hook, an allowed-tools list — they will stop matching
  silently, and the fix is to update the name. Everything else is unaffected; the tools
  themselves are unchanged.

  Done now rather than later on the grounds that it only gets more expensive: every install
  between here and whenever it would otherwise happen is one more that has to be re-approved.

- **An agent tool refuses an argument it does not recognise, instead of ignoring it.** Passing
  a name a tool does not declare used to be silently dropped, so a call that narrowed nothing
  came back looking exactly like one that had — *"a plausible, complete, wrong answer"*, in the
  words of the agent that reported it.

  **You are most likely to meet this while upgrading**, because all it takes is a plugin newer
  than the program it launches: the tool offers an argument the program has never heard of. The
  refusal names what was passed and what the tool accepts, so the answer is in the message
  rather than in a version comparison. `subroutine whoami` ends with the versions of all three
  installations if you need to check which half is behind.

  Query parameters on the HTTP API have refused unknown names for some time; this surface was
  the odd one out.

- **The server's instructions point an agent at the skill.** They are in context every session
  and they *teach* — refs, and the comment-versus-document rule — and an agent's report was
  that "a paragraph of correct guidance in context makes the skill feel redundant". It then
  listed, searched and recommended what to file without ever opening the skill. The pointer is
  conditional, because `subroutine mcp` started by hand has no skill to read.

### Fixed

- **A `.subroutine` marker naming a connection that is gone no longer stops every command.**
  Rename a connection in `config.toml`, or switch one off with `enabled = false`, and any
  checkout marked for it refused outright — including `subroutine list`, which is meant to
  span everything you can reach whatever your context says.

  It now falls to the next thing that can answer and says so. A connection named with `-c`,
  or one you set with `subroutine use`, still refuses loudly: those are somebody speaking,
  and a marker is a file that arrived with a checkout.

  **What the marker names does not come along with it.** A marker describes one instance, so
  its workspace and its project are true only there — and its project is matched by key where
  the id does not resolve, which is what keeps markers written before project ids working. Put
  together, a checkout marked for one instance would have filed work into a *different*
  instance's project of the same name, which `SR`, `WEB`, `API` and `DOCS` are exactly the kind
  of key for. It says which of the two things it ignored, and why.

- **A `.subroutine` marker that has gone stale no longer breaks the command.** It named a
  workspace the connection had never heard of, and instead of being ignored it *erased* the
  context `subroutine use` had stored — so on an instance with more than one workspace, a
  command refused on the line after the one that set it up. The warning said "Ignoring it",
  which is the one thing it did not do.

  It now falls to the next thing that can answer, and says which: *"…which is not on local.
  Using 'projects' instead."* A stored workspace that is also gone is checked before it is
  offered, so the failure cannot simply move one step along.

- **A credential says where it may write, not only what it can see.** The reach was reported
  everywhere and the write set nowhere, so an agent bounded to read a tree and write one
  project read back exactly like one that could write all of it — the whole point of the
  credential, invisible on the three surfaces that describe it, including the line printed
  immediately after minting one.

  A credential narrowed *only* that way was worse: it printed `Narrowed to .` — a sentence
  asserting a boundary and naming none.

- **A client can read an instance that is a release behind it.** `GET /v1/me` grew two new
  fields in this cycle, and both went in as *required* — so a CLI updated before the server it
  talks to refused the server outright, reporting that it "answered, but not as a Subroutine
  instance". A machine on one release and a server on another is the ordinary state of anything
  with more than one machine in it, not an edge case.

  Fields added to a response are now defaulted, and there is a test holding the exact body an
  older instance sends, captured from one rather than written by hand. Nothing else in the
  suite could have caught it: every test builds both halves from the same source, which is
  precisely the arrangement that cannot produce a version mismatch.

- **`subroutine list --project` works on an instance with more than one workspace.** The
  listing asks every workspace and passed the project key to each; a project belongs to one, so
  the others refused, the fan-out read that as the *connection* failing, and the rows the right
  workspace had already returned were discarded with them. Every key failed except one that
  happened to exist in both.

  Latent since projects and workspaces both existed and invisible until an instance had two.
  A key that exists nowhere is still refused by name, so a typo does not quietly become an
  empty list.

- **A project listing includes the sub-projects underneath it.** `subroutine project list`
  drew a tree and `subroutine list --project PARENT` returned only what was filed directly in
  the parent, so a hierarchy answered for none of its own contents — which is most of what a
  hierarchy is for. `--project` and `?project=` now mean that project *and everything under
  it*, on tasks and on documents, over both transports.

  Naming a child still means the child alone. The same rule already governed a token's
  `project_scope`, one function away and in the opposite direction.

  `subroutine project move` counted its subtree by asking per project and adding up, which
  became a double count once a parent answered for its children; it now asks once, which also
  frees it from the page cap below.

- **`subroutine project rename` counts the whole project rather than one page.** It asked for
  a listing and reported its length, so `default_page_size` capped it: renaming a project of
  249 items promised that 50 would keep their numbers. Wrong in the direction that makes an
  irreversible operation look smaller, in the one sentence somebody reads while deciding to do
  it. It also said "1 item keep their numbers" — the noun pluralised and the verb left behind.
  Both rename commands now share one sentence, and `subroutine workspace rename` loses the
  "at least" hedge it was using to stay honest.

- **`subroutine token list` names the projects a credential is scoped to.** The line above
  resolved the workspace pin to its slug; the project scope on the next line printed raw
  UUIDs. Resolved through the same narrowing every other listing uses, so a key is never shown
  to somebody who cannot see that project — and an id that does not resolve is left as it was
  rather than dropped, because a listing of what a credential can reach must not report less
  than the truth.

- **`subroutine user list` says `instance admin`, not `admin`.** `admin` is also the key of a
  workspace role, and `user list --workspace acme` prints its answer in the same column
  position — so the same person read as `admin` in one command and `owner` in the other, where
  the first named a role she does not hold.

- **`subroutine db copy` no longer migrates a database it is about to refuse.** It brought the
  target up to schema *first* and checked whether it was empty second — so naming an existing
  instance with `--to` moved that database forward through every intervening revision and then
  reported that nothing had happened. There is no downgrade, so the build serving it would not
  start again.

  **If you have run `db copy` against a non-empty database, check its schema** with
  `subroutine db current` before starting the service that owns it. The order is now the other
  way round: an occupied target is refused before anything is written to it.

- **The change feed reports a project's deletion, and no longer erases what was inside it.**
  Two failures with one cause. A project's own deletion never appeared, so nothing polling the
  feed was told it had gone. Worse, because an item is reached through a join to its project,
  deleting one retroactively removed every event about every task and document filed in it — a
  client that polled afterwards was told those items had never existed. A feed that rewrites
  its own past cannot be resumed from, which is the one thing it is for.

- **`?since=0` is refused the same way over HTTP and locally.** `since` is a `seq` and the
  first one is 1, so zero names nothing — the endpoint said so and the local client did not,
  falling through to the "your cursor expired" refusal instead. That told a caller its events
  had been pruned on an instance that has never pruned anything. Both now answer `422`, from
  one place. A client whose cursor starts at zero should send no `since` at all.

- **A `.subroutine` marker survives a workspace rename.** It recorded the project's permanent
  id beside its key, so a project rename was already safe, and recorded the workspace by name
  alone — so renaming a workspace left every marked checkout printing `names workspace 'x',
  which is not on local` on every command, for ever. Work still went to the right project
  throughout; the warning was about nothing. New markers carry `workspace_id`; existing ones go
  on resolving by name.

- **The change feed reports links.** A link event carried no record of *what* it was a link
  on, so nothing could work out who was entitled to see it and the feed left them out
  entirely — an agent resuming from `?since=` never learned that anything had been joined to
  anything.

  They are scoped through the item the link hangs off, exactly as a comment is, so a link on
  a task in a private project stays invisible to anyone who cannot see that task.

  Links created before this release have no such record and remain absent from the feed.
  They are still on the items themselves.

- **A workspace created anywhere but `init` had no Inbox**, so filing a task in it without
  naming a project — the ordinary way to file one — failed with a `500`. That is every
  workspace ever made through `POST /v1/workspaces`, which has been able to create them since
  the first release.

  The error said setup had been "interrupted" and told you to run `init` again, which is
  wrong twice: nothing was interrupted, and re-running `init` on a live instance is its own
  hazard. An Inbox is now part of creating a workspace rather than a step each caller has to
  remember.

  **Existing workspaces are not repaired by upgrading.** If you made one over HTTP and it
  refuses tasks, create a project in it and file into that, or make the workspace again.

- **A crash is a sentence now, not forty lines of Python.** Anything the program did not
  anticipate printed a boxed traceback with a caret, which is a developer's view of a
  developer's problem. It now says that something went wrong, where the details were written,
  and where to report it.

  The stack is kept rather than thrown away — one file per crash under `crashes/` in the
  state directory, named by the instant — so the report is already on disk when somebody asks
  for it, instead of you having to reproduce the failure with a debug flag set.

  Passwords and tokens in the command line are masked in that file, because it is a file you
  are being asked to send. Failures the program *does* understand are unaffected and keep the
  specific message they already had.

- **`subroutine list` points at `subroutine search` instead of refusing.** `list -q words`,
  `list --search words` and a bare `list words` gave three different errors naming neither
  the search command nor each other — and the one that offered a suggestion offered
  `--strict`. All three now answer `Try: subroutine search "words"`.

- **`subroutine add` now says where the new item landed**, when there is more than one place
  it could have gone. Previously it confirmed with the title alone, so a capture routed to
  another instance — by a `use` context, by a `.subroutine` file, or by `-c` — looked exactly
  like one that went where you meant. `Added: work/acme/#42  Renew the certificate`.

  Nothing changes for a single instance with a single workspace: there is nowhere else for it
  to go, so there is nothing to disambiguate and the confirmation stays as it was.

  Reported by a Claude Code agent whose own bug report went to the wrong instance and who had
  to search both to find out where.

- **Advice printed under `-c` or `-w` now survives the flag.** A tip suggesting a command was
  addressed to the invocation that printed it rather than the one you type next, so
  `subroutine -c work list` could end `Tip: subroutine done 1` — and typing that, without the
  flag, completed a different item on a different instance.

  Suggested commands now carry a full address whenever the current context came from
  something that will not still be true next time. Nothing changes when it came from
  `subroutine use`, a `.subroutine` file or your only connection, which is the ordinary case.

- **A listing under `-c` or `-w` says so.** `A bare number means work/acme.` was true of the
  rows above it and false of the next command; it now reads `A bare number means work/acme
  (from the command line).` when the flags are what decided it.

- **`subroutine connections` marks the connection being written to**, not only the one that
  would be written to if nothing had chosen. Those are different questions — the second is
  the fallback, and `subroutine use`, a `.subroutine` file, `-c` or `SUBROUTINE_CONNECTION`
  all override it — and only the second was ever answered, under the word `default`.

  The row now reads `in use` where your next command goes, `default` for the fallback, and
  both together in the ordinary case where they agree. When they differ it also says why:
  `Writing to work/acme (from 'subroutine use').`

- **An agent session no longer binds to whichever instance `subroutine use` last pointed at.**
  The MCP server reads its connection once, at startup, and holds it for the whole session —
  so with no connection configured it inherited a setting people move between tasks, and two
  sessions started on one machine on one day could reach different instances with nothing to
  say so.

  It now falls back to your `default_connection`, which is set in `config.toml` and can be
  read back with `subroutine connections`. Name a connection in the plugin's settings to
  choose a different one.

- **An agent is told which instances it cannot reach.** The server's instructions named the
  connection it was bound to, which read as though that were the only one — so an agent had
  no reason to ask. Where more than one is configured they now say so; where only one is,
  they are unchanged.

### Security

- **A credential can no longer outlive the one that issued it.** A token may already not be
  given wider permissions, more projects, or a different workspace than the credential asking
  for it. Its *expiry* was not checked — so an agent holding a credential that stopped working
  tomorrow could issue itself one that never stops, with the same permissions and under the
  same account, and nothing refused it.

  That matters most where the expiry is the whole point. `--expires now+30d` is how a month's
  work on somebody else's instance is bounded, and the credential being bounded could undo it
  on the first day.

  **If you have issued a credential with an expiry, check what it has issued** —
  `subroutine token list` shows every one, when it stops working, and when it was last used.
  Anything you did not intend can be revoked by its prefix. Issuing a *shorter*-lived
  credential is unaffected, which is the ordinary case: the rule only refuses one that outlives
  its issuer or never expires at all.

- **An instance served through a reverse proxy is now rate limited by default.** The default
  was "on unless the bind is loopback", and the deployment these documents recommend is a
  TLS-terminating proxy in front of an application listening on `127.0.0.1` — so the socket
  was loopback, the service was on the public internet, and the limiter was off.

  Setting `public_url` now turns it on whatever the bind, because that setting is you saying
  a proxy serves this to other people. Nothing changes for a laptop with no `public_url`, and
  `rate_limit` still overrides both ways.

  **If you run an instance behind a proxy on a loopback bind, it has not been rate limiting.**
  Setting `public_url` — which `serve` already requires for a non-loopback bind without
  `--insecure` — is enough to fix it.

- **`trusted_proxies` — count the caller behind a reverse proxy, not the proxy.** Failed
  authentications are limited per address, and through a proxy every request arrives from the
  same one, so they shared a single allowance. Name the address your proxy connects from and
  the real caller is counted:

  ```toml
  trusted_proxies = ["127.0.0.1"]
  ```

  **Name only proxies you control.** `X-Forwarded-For` is written by whoever sends the
  request, so this is you vouching for a specific peer; pointed at anything else it would let
  a caller choose which bucket it lands in. Left empty the header is ignored entirely, which
  is the correct behaviour with nothing in front and remains the default.

## 0.2.0 — 2026-08-02

### Added

- **`GET /v1/changes` — what changed while you were away**, across everything you can see, in
  one call and without naming an item. This is the question an agent could not previously ask:
  it could write durably here and could not resume incrementally, so every session either
  re-read the backlog defensively or carried on believing something that had since moved.

  Resume with `?since=<seq>`, using the `seq` of the last event you dealt with. It is
  inclusive, so you will see that one again — send it back rather than the one after, and
  ignore what you already hold.

  `?actor=me` narrows it to what *this credential* did, which is not the same as what its
  owner did: an agent with its own service-account token gets its own work back, not yours.

  **Events under a second old are withheld deliberately.** A sequence number is allocated when
  a change is written and becomes visible when it commits, and those are not the same moment —
  without the delay a fast transaction can commit past a slower one and leave a change behind
  a cursor that has already moved on. Poll more often than that and you will simply see
  nothing new.

  `subroutine changes` reads it from the command line, and an agent has it as a tool. With no
  `--since` you get the most recent page rather than the instance's first afternoon; with one,
  you carry on from where you were. Each event names the item it is about, so a feed reads as
  `#42 Fix the parser` rather than as a list of identifiers.

- New error code `cursor_expired` (410), for a `?since=` older than the events an instance
  still holds — so a client resyncs rather than being handed a page that silently omits
  whatever fell off the end. It cannot occur yet: nothing prunes events, so nothing falls off.

### Fixed

- **`subroutine init` now says what to do when it cannot write where it was told to.** It
  checked the directory the database goes in and never the one the configuration goes in — so
  on PostgreSQL, where there is no database directory to check, nothing was checked at all, and
  a permission problem arrived as a Python traceback. It now names the outermost directory that
  is missing, which is the one you can actually create, rather than the innermost, which you
  cannot.

- **The hosting guide's first run works from a clean machine.** Setting up a service account
  meant creating `/var/lib/subroutine` by hand first, and the guide never said so — systemd
  makes that directory, but not until the service first starts, and the service cannot start
  until `subroutine init` has run. One command, now in the guide where it is needed.

- **A connection you add by hand is no longer reported as having no effect.** `config.toml`
  warns about keys it does not recognise, so that a misspelled setting cannot silently do
  nothing — and it did not know about `[connections.…]`, which is the one thing in that file
  people write by hand. It said the connection was being ignored, immediately before using it.

- **A checkout now records which instance it belongs to, always.** `subroutine use --here`
  wrote the connection into `.subroutine` only if a second one already existed — so every
  marker written before you added a second instance quietly stopped identifying anything, and
  work started in that directory went wherever the machine-wide context happened to point.
  That lands hardest on an agent, which is the one caller that cannot be asked. Existing
  markers are not repaired automatically; `subroutine use --here` rewrites one.

- **A listing across more than one instance says which one a bare number means.** It never
  did — the only clue was which rows printed without a prefix, which is the addressing rule
  read backwards. Nothing was ever ambiguous to the program, but somebody reading fifty rows
  and typing a number off them had no statement of where it would land. Silent when there is
  only one place to be in.

- **A connection with nothing in it keeps its heading in `subroutine list`.** An instance you
  had just set up and not yet used vanished from the listing entirely — no heading, no line,
  and no failure either, since a failure line only appears for a connection that errored. There
  was nothing anywhere in the output separating "reachable and empty" from "not working", and
  the natural reading of a missing group is a missing connection. A single connection still
  prints no heading at all.

- **`subroutine use <connection>` says so, instead of looking for a workspace of that name.**
  It takes `workspace` or `connection/workspace`, and given just the connection it searched the
  wrong instance and reported about the wrong thing — while the connection was sitting in the
  roster it had already loaded. It now names the completion, with the workspace filled in.

- **The connection section names `subroutine connections`**, which is how you check that a
  connection you added is being read — and which stays out of `--help` until a second
  connection exists, so it is invisible for exactly as long as the answer is "it did not work".

- **The hosting guide says how to reach a served instance from your own machine.** Adding a
  connection is two short files and the guide never showed them, while the README sells one
  list across every machine. There is a section for it now — including that the credential you
  need is a token you issue on the server rather than the `secret_key` sitting in your own
  configuration file, which is the only thing there that looks like one.

- **The hosting guide shows how to serve beyond loopback with a proxy on another machine** —
  a router, a NAS, Nginx Proxy Manager. It is two settings and no flag, and the guide had only
  ever demonstrated a public bind as a command-line option, on a page whose whole subject is a
  unit file. `--insecure` is now described as what it is: the case where there is no proxy at
  all.

- **A missing database now says which one it looked for, and why it looked there.** "There is
  no database here yet. Run 'subroutine init' first" was the answer given to a service whose
  PostgreSQL database was populated and running — because the configuration was pointing at a
  SQLite path nobody had chosen, and the message never said so. It names the database now, and
  distinguishes "nothing is set up" from "nothing configured `database_url`". The same
  refusal existed in eight places, six of them identical; there is one now.

- **`init` says when the database it just used is recorded nowhere.** Setting up on PostgreSQL
  means naming the database in the environment for that one run, because `config.toml` does not
  exist yet — and `init` writes only the signing key, so the value goes no further. It cannot
  write the URL for you: a PostgreSQL URL routinely carries a password and that file is the one
  that holds no secrets. It can tell you, and now does.

- **`init` warns before building a second instance when the first one is elsewhere.** Running
  it in a configuration directory that already holds a signing key, while the database it is
  configured for is absent, means the earlier instance is somewhere this configuration does not
  name. It used to carry on in silence — and then `db current` reported a healthy schema and
  the list came back empty, which to the person it happens to looks like their data has gone.

- **The hosting guide has a route for starting on PostgreSQL**, rather than only for switching
  to it later. Four steps, of which writing `database_url` into `config.toml` is the one whose
  absence leaves a service restarting every five seconds.

- **The hosting guide says how to get PostgreSQL**, rather than assuming you already have one.
  Installing the server, and creating the role and database with names and ownership that
  actually let the first migration run — `--owner` in particular, without which it fails on
  `permission denied for schema public` long after the step that caused it.

## 0.1.4 — 2026-08-02

### Fixed

- **If you have neither uv nor pipx, the install instructions now say how to get one.** 0.1.3
  replaced `pip install subroutine` with `uv tool install` and `pipx install`, which is right —
  and left nowhere to go for somebody who has neither, since no distribution ships either tool.
  On a fresh Ubuntu that was three failures in a row. The Install section now names
  `sudo apt install pipx`, `brew install pipx`, and uv's own installer.

### Changed

- A release now creates its GitHub release automatically, with the changelog section as its
  notes and the wheel and sdist attached — so what changed is on the Releases page rather than
  only in a file you have to open.

## 0.1.3 — 2026-08-02

### Fixed

- **The first install line no longer fails on the machine most people have.** `pip install
  subroutine` outside a virtualenv is refused by Debian, Ubuntu and Fedora — PEP 668's
  `externally-managed-environment` — and that was the first command in this README, the first
  on the PyPI page, and the first thing anybody read. It is now `uv tool install subroutine`
  or `pipx install subroutine`: they work on those systems, they put `subroutine` on your
  `PATH` where an editor or an agent can launch it, and pipx is what Debian's own error
  message tells you to use.

  **This corrects the note at the end of 0.1.2 below**, which said `pip install subroutine`
  remained right if you were only going to type commands yourself. That holds inside a
  virtualenv you have activated, and nowhere else — which is not what it implied.

## 0.1.2 — 2026-08-02

No migration notice: the schema head has not moved since 0.1.0. Nothing in the package itself
changed — this release is the plugin and the documentation around installing it.

### Fixed

- Installing the Claude Code plugin now gives you working tools without your having to supply
  a path. Your editor launches `subroutine` itself, so it has to be on your `PATH` — and
  `pip install` into a virtualenv satisfies "installed" while leaving every tool missing, with
  no error at the point of the mistake. The plugin route now tells you to install it as a
  tool, with `uv tool install subroutine` or `pipx install subroutine`, which is what puts the
  command where something other than your shell can find it.
- The plugin's skill sent that failure the wrong way. It could not tell "not installed" from
  "installed where the editor cannot see it" — both look the same from inside a session — so it
  told people who had already installed it to install it again. It now names `claude mcp list`,
  which is the one command that separates them.

`pip install subroutine` remains exactly right if you are only going to type commands
yourself.

## 0.1.1 — 2026-08-02

No migration notice: the schema head has not moved since 0.1.0.

### Fixed

- An agent working in a checkout marked for a different instance could not file anything.
  Committing a `.subroutine` file is how a team says which project a repository belongs to, so
  a colleague who cloned the repository and ran `subroutine init` had a marker naming a project
  that was not on their own instance — and `subroutine_add` refused every call rather than
  ignoring it, while the CLI beside it carried on. Reads were never affected. A renamed project
  is now also followed by its id over the Model Context Protocol, as it already was on the
  command line.

### Changed

- The version is derived from the git tag rather than written into `pyproject.toml`, so what
  the package reports and what was released cannot drift apart.

## 0.1.0 — 2026-08-01

The first public release. Everything here is new, so this section says what exists rather than
what changed.

No migration notice: there is no previous release to upgrade from, and a fresh install builds
its schema outright with `subroutine init`.

### A personal to-do list

- `subroutine init`, `add`, `today`, `done`, `plan`, `defer`, `list`, `search`, `show`,
  `comment`, `project create`, `project list` and `doc create`. Three commands from install to
  a working list, and a fourth to tick something off.
- Quick capture: `subroutine add "Fix the deploy script by friday !4/2 ~2h #ops"` sets the
  deadline, both priority axes, the estimate and a tag from the line you already type.
- Items are addressed by a number allocated once and never reused, so it goes on meaning that
  item after you have finished a dozen others.
- `subroutine start` and `subroutine stop`, so the list can say what you are in the middle of.
- `subroutine link 42 blocks 43` and `subroutine unlink`. `--ready` reads those links, so
  this is how that filter learns anything.
- `subroutine delete` and `subroutine restore`, with `list --trash` to see what is in there.
  Deleting is soft, so the wrong number costs nothing.
- `subroutine list --ready` shows only work that can actually be started — nothing unfinished
  blocks it and it is not deferred. It is the question a backlog cannot answer.
- `subroutine help` and `subroutine --help` do the same thing: they list the commands.
  `subroutine explain` covers the ideas behind them — dates, refs, the capture shorthand.

### An HTTP API, and agents as first-class users

- The HTTP API under `/v1`, with the same data model and the same permission checks the
  CLI uses. `GET /v1/docs/agent` is the guide an agent should read first.
- Scoped bearer tokens: an agent's credential can be narrower than the person who issued it,
  and may never be wider. Per-workspace pins and per-permission scopes.
- `subroutine token create --username ana` issues for a person, `--service-account claude` for
  a machine identity, creating it as it goes. Two flags because they are two decisions, and
  neither will issue a credential for an account that could not use it.
- `subroutine mcp` serves the same instance over the Model Context Protocol, in nine tools —
  including `link` and `project`, so an agent can say what blocks what and file its own work.
- **A Claude Code plugin**, which wires those tools up and carries a skill describing the
  practice — including how to adopt Subroutine in a project that does not use it yet.
- Attribution on everything, a comment thread per item, and a history of every change.

### Running it

- `subroutine serve` listens on loopback and refuses a wider bind without TLS in front of it.
- SQLite by default with no configuration; PostgreSQL with the `postgres` extra.
- `subroutine upgrade` — reports both schema versions, takes a verified backup, migrates, then
  reads the result back. It does not install software, deliberately.
- Backups to a directory of your choosing, verified where they land, and a restore that makes
  you say whether it is a recovery or a clone.
- Separate instances on one machine with `--profile`, isolated across all three XDG roots.

### Known limits

Session handoffs, verification evidence and claims are specified and not built — as are
attachments, calendar feeds, recurring tasks, a `GET /v1/changes` feed, and manual reordering.
There is no web UI.

Two of those are *refused out loud* rather than merely absent, which is worth telling apart:
the capture grammar recognises `every monday` well enough to leave it in your title and say it
did nothing with it, and a calendar credential presented to the API is turned down by name.
The rest are simply not there yet.
