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

> **This release changes the database schema**, to `243497ffc330`.
>
> Install it, then run `subroutine upgrade`. That reports both versions, takes a
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
