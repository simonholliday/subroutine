/*
	Who is on this instance, what they may do, and which agents answer to whom — `#1397`.

	**The first page in the browser's administrative area**, which design `#2110` §3 settled as
	an *area* rather than a fourth view: `VIEWS` is three arrangements of work, and a directory
	of principals is not one. Its own address space is also what keeps §1.4 true structurally —
	somebody who never opens this never meets a workspace, a role or a credential.

	**Everything here is a rendering of calls that already existed.** `GET /v1/users` has
	published the roster since M1 and `GET /v1/workspaces/{slug}/members` has carried roles for
	as long; `#1382` §4.5's finding is not that the data is missing but that *"there is no
	surface anywhere that shows a principal, its scopes, or which agents answer to whom"*. So
	this file has no cleverness in it, and that is the point.
*/

import { html } from "./html.js";
import { Icon, MARK_ICONS } from "./marks.js";
import { day, named } from "./dates.js";

/*
	**What a person is told when they hold no role anywhere.**

	An em dash rather than an empty cell, because a blank reads as *not loaded* on a page that
	fetches its roles a workspace at a time — and this is the one row where the answer genuinely
	is *nothing*, which somebody looking at a fleet needs to be able to see rather than infer.
*/
const NO_ROLES = "—";

export function rolesByUsername (rosters) {
	/*
		Fold each workspace's membership into one answer per person.

		`rosters` is `[{ slug, title, members }]`, one entry per workspace the reader can see —
		which is what `/v1/me` already lists, so the page asks for no more than the reader was
		already told they could reach.

		**Keyed by username rather than by id**, because that is what the roster and the users
		listing agree on: `GET /v1/users` publishes `username` and the member rows carry
		`user.username`. Both also carry `id`, and using it here would work — the reason not to
		is that a reader comparing this page against `subroutine user list` compares names.

		**A workspace nobody could read contributes nothing rather than an empty group.** A
		failed roster arrives as an absent entry, so a partial answer under-reports and never
		invents; `#1305`'s rule about a total that cannot say what it left out is why the page
		says how many workspaces it asked about.
	*/
	const found = new Map();

	for (const roster of rosters || []) {
		for (const row of (roster && roster.members) || []) {
			const who = row && row.user && row.user.username;

			if (!who) continue;

			if (!found.has(who)) found.set(who, []);

			found.get(who).push({ slug: roster.slug, title: roster.title, role: row.role });
		}
	}

	return found;
}

export function Roles ({ held }) {
	/*
		What one principal may do, as *role* in *workspace*, workspace by workspace.

		**The role is the word the workspace uses**, never a key — `#1717`'s rule, met here
		because `views.Member.role` is already the role's `title` rather than its id.

		**Ordered as the reader's own workspaces are**, which is `/v1/me`'s order, so two rows
		list the same workspace in the same place and the column can be scanned. That is
		`#1424`'s finding one page along: the fault it fixed was an assignee rendered as a chip
		in a flow, unscannable *because it was not aligned*.
	*/
	if (!held || held.length === 0) return html`<span class="norole">${NO_ROLES}</span>`;

	return html`
		<span class="roles">
			${held.map((one, at) => html`
				${at > 0 ? html`<span class="between"> · </span>` : null}
				<span class="role">${one.role}<span class="in"> in </span>${one.title}</span>
			`)}
		</span>
	`;
}

export function Principal ({ person, held, count = null, chosen = false, onChoose = null }) {
	/*
		One account: what it is called, whether it is an agent, who answers for it, and what it
		may do.

		**`named` rather than a wording of its own** (`#1420`). The label is
		`@claude-super (agent, @morpheus)` — one cell that already says the three things this
		page exists to say, and it is the same cell the assignee control and every row render,
		so a reader meets one vocabulary. The accountable person is resolved on the server and
		arrives as `answers_to`; **the browser holds no copy of the chain rule** (`#925`).

		**A glyph beside the word and never instead of it** (`#102`): nothing may be said in a
		colour alone *and nothing in a shape alone either*, so the picture is reinforcement and
		`named`'s *(agent)* is what carries it.

		**Somebody who has left is shown, not hidden.** `users.listed` returns them — it filters
		deleted accounts and not inactive ones — and a directory that omitted them could not
		answer *who used to hold this*, which is the question an audit starts from.
	*/
	const agent = Boolean(person.is_service_account);

	return html`
		<div class=${person.is_active === false ? "principal gone" : "principal"}>
			<span class="account">
				<${Icon} name=${agent ? MARK_ICONS.agent : MARK_ICONS.person} />${" "}
				${named(person.username, agent, person.answers_to)}
			</span>
			${person.is_active === false
				? html`<span class="left">has left</span>`
				: null}
			<${Roles} held=${held} />
			${/*
				**How many credentials, said only where the reader may act on any** (`#1820`'s
				shape). `count` is null where the page never fetched them — which is every
				render by the sample harness and every reader whose call was refused — and a
				zero is worth saying where a number is: *nobody has issued this agent anything*
				is the answer somebody auditing a fleet is looking for, and an absent cell reads
				as *not loaded* rather than as none.
			*/ null}
			${count === null
				? null
				: html`<button type="button" class="reveal holdscount"
						aria-expanded=${chosen ? "true" : "false"}
						onClick=${() => onChoose && onChoose(chosen ? null : person.username)}>
						${count} ${count === 1 ? "credential" : "credentials"}
						${/* **The caret as well as the word** (`#1046` rule 4). `aria-expanded`
						     alone says nothing to a reader who can see, and the caret alone says
						     nothing to one who cannot; the rule wants both and the stylesheet
						     turns this one when the state is true. */ null}
						<${Icon} name="caret-down" />
					</button>`}
		</div>
	`;
}

export function People ({
	people, rosters, asked = 0, reached = 0,
	credentials = null, chosen = null, onChoose = null,
	issuing = null, onIssuing = null, confirming = null, onConfirming = null,
	issued = null, onIssued = null, onIssue = null, onRevoke = null,
	offers = [], workspaces = [], mayCreate = false, busy = false,
}) {
	/*
		The page: every account on this instance, oldest first.

		**Oldest first is the server's order and is kept** — `GET /v1/users`' own docstring says
		why: the first account is the one `init` made, so a reader is usually looking for the
		ones that came after it. Re-sorting here would be the browser disagreeing with
		`subroutine user list` about what the list *is*.

		**It says how many workspaces it could ask about**, because the roles column is assembled
		from one call per workspace and a reader cannot otherwise tell *holds no role* from *we
		could not look*. `#1305`'s rule: a total that cannot say what it left out is worse than
		no total, and this is that rule for a column rather than for a count.

		**No `#63` box drawing, and no tree.** The accountability chain is genuinely a tree and
		indentation would be legitimate for one — `project list` does exactly that. It is not
		done here because this listing's order is the server's, not the tree's, and indenting a
		list ordered by something else states a relationship the arrangement does not carry.
	*/
	if (!people) return html`<div class="empty">Reading…</div>`;

	if (people.length === 0) {
		return html`<div class="empty">Nobody has an account on this instance yet.</div>`;
	}

	const held = rolesByUsername(rosters);
	/*
		**Grouped by username, because that is what the two calls agree on.** `GET /v1/tokens`
		publishes `username` beside `user_id` and the roster publishes the same, so keying on it
		here keeps this page comparable with `subroutine token list` — which is the surface
		somebody checks it against.

		Null where the credentials were never fetched, so `Principal` can tell *nobody has one*
		from *nothing asked*. Those are different answers and only one of them is worth drawing.
	*/
	const holdings = credentials === null ? null : new Map();

	for (const one of credentials || []) {
		if (!holdings.has(one.username)) holdings.set(one.username, []);

		holdings.get(one.username).push(one);
	}

	return html`
		<div class="people">
			<h2 class="area">People</h2>
			${reached < asked
				? html`<p class="partial">
						Roles are shown for ${reached} of ${asked} workspaces; the rest could not
						be read.
					</p>`
				: null}
			${people.map((person) => html`
				<${Principal}
					key=${person.username}
					person=${person}
					held=${held.get(person.username)}
					count=${holdings === null ? null : (holdings.get(person.username) || []).length}
					chosen=${chosen === person.username}
					onChoose=${onChoose}
				/>
			`)}

			${/*
				**One panel below the list rather than one per row.** A directory is read by
				running an eye down it, and forty rows each carrying a form is not a directory
				any more — so the acts belong to whichever principal was asked about, in one
				place, where the reader's attention already is.
			*/ null}
			${chosen
				? html`<${Holdings}
						username=${chosen}
						credentials=${(holdings && holdings.get(chosen)) || []}
						onRevoke=${onConfirming} onIssue=${() => onIssuing({ username: chosen })}
						busy=${busy} />`
				: null}

			${mayCreate && !issuing && !issued
				? html`<button type="button" class="action adding"
						onClick=${() => onIssuing({ creating: true })}>Add an agent</button>`
				: null}

			${confirming
				? html`<${Stopping} credential=${confirming} busy=${busy}
						onConfirm=${onRevoke} onCancel=${() => onConfirming(null)} />`
				: null}

			${issuing
				? html`<${Issuing} subject=${issuing} offers=${offers} workspaces=${workspaces}
						onIssue=${onIssue} onCancel=${() => onIssuing(null)} busy=${busy} />`
				: null}

			${issued ? html`<${Secret} issued=${issued} onDone=${onIssued} />` : null}
		</div>
	`;
}


export function Holdings ({ username, credentials, onRevoke, onIssue, busy = false }) {
	/*
		Every credential one principal holds, and the two acts available on them.

		**An empty set is a sentence rather than a blank.** *Nobody has issued this agent
		anything* is a real answer and the one an operator auditing a fleet is looking for; an
		empty region under a chosen name reads as a page that failed to load.
	*/
	return html`
		<div class="holdings">
			<h3>What @${username} holds</h3>
			${credentials.length === 0
				? html`<p class="empty">No credential has been issued for this account.</p>`
				: credentials.map((one) => html`
					<${Credential} key=${one.id} credential=${one} onRevoke=${onRevoke}
						busy=${busy} />
				`)}
			<button type="button" class="action" onClick=${onIssue}>Issue a credential</button>
		</div>
	`;
}


/*
	**What a credential may do, in the words the terminal already uses.**

	`views.Token.columns` renders *everything its owner can do* for an unnarrowed credential, and
	this says it identically. Two surfaces describing one fact in two vocabularies is `#1266`'s
	subject, and `tests/test_web.py` holds the two strings to each other so neither can be
	reworded alone.

	**Read from `narrows` rather than from `scopes.length`**, which is the field's whole reason
	for existing: `scopes: []` means *no narrowing* and reading it as *no permissions* is the
	fastest way to a wrong conclusion about what a leaked credential could do. Its own docstring
	says so — *"spelled out so that reading `scopes: []` the wrong way round is not the only
	thing between a caller and a wrong conclusion"*.
*/
export const UNNARROWED = "everything its owner can do";

export function reachOf (credential) {
	/* What one credential may do and where, as a sentence a reader can act on. */
	const may = credential.narrows && credential.scopes.length > 0
		? credential.scopes.join(", ")
		: UNNARROWED;
	const within = credential.project_scope_keys && credential.project_scope_keys.length > 0
		? `, within ${credential.project_scope_keys.join(", ")}`
		: "";

	return `${may}${within}`;
}

export function Credential ({ credential, onRevoke, busy = false }) {
	/*
		One credential, and the one act available on it.

		**Anything drawn here can be revoked, and that is a property of the route rather than a
		check made here.** `GET /v1/tokens` is *narrowed the same way revoking is*, so the page
		cannot be in a state where it offers a control that would be refused — which is the rule
		`app.js` states three times and the reason `#927`'s M-25 mattered.

		**A revoked or expired one is still listed.** `usable` is the question *would this be
		accepted right now*, and an operator auditing an instance needs the ones that would not
		be — a credential that stopped working last week is how you find out what an agent lost
		access to, and hiding it makes the list agree with itself and disagree with the record.
	*/
	return html`
		<div class=${credential.usable ? "credential" : "credential spent"}>
			<span class="what">
				<strong>${credential.title}</strong>
				<span class="prefix">${credential.prefix}</span>
			</span>
			<span class="reach">${reachOf(credential)}</span>
			${credential.usable
				? html`
					<button type="button" class="action" disabled=${busy}
						onClick=${() => onRevoke && onRevoke(credential)}>Revoke</button>
				`
				: html`<span class="spentword">
						${credential.revoked_at ? "revoked" : "expired"}
					</span>`}
		</div>
	`;
}

export function Stopping ({ credential, onConfirm, onCancel, busy = false }) {
	/*
		What revoking this stops, asked before it happens.

		**`project rename`'s precedent**: it counts the items and names the three things that
		stop working *before* doing any of it. The same shape, because the same thing is true —
		the act is immediate, nothing recovers it, and a replacement is a different credential
		with a different secret.

		**The cheap answer is *not now*** — `#1382` §4.4. That section's warning is that a screen
		offering *Approve?* manufactures rubber-stamping, and *"a rubber stamp is worse than no
		control because it produces a record saying a human checked"*. So this asks a question
		answerable from what is on screen, and the answer that costs nothing is the one that
		changes nothing: **Cancel comes first and is what Escape and a stray Return reach.**

		**When it was last used is the fact that decides it**, and this is the first thing
		anywhere to read that column — `#1395` measured `last_used_at` as written and read by
		nothing. An operator staring at a credential they do not recognise is asking exactly one
		question, and *nothing has presented this in three months* and *this was used a minute
		ago* are opposite answers.
	*/
	return html`
		<div class="stopping">
			<p>
				Revoking <strong>${credential.title}</strong> (${credential.prefix}) stops
				anything using it, immediately. Nothing recovers it, and a replacement is a
				different credential.
			</p>
			<p class="lastused">
				${credential.last_used_at
					? html`It was last used on ${day(credential.last_used_at)}.`
					: "It has never been used."}
			</p>
			<div class="acts">
				<button type="button" class="quiet" onClick=${onCancel}>Cancel</button>
				<button type="button" class="primary" disabled=${busy}
					onClick=${() => onConfirm(credential)}>Revoke it</button>
			</div>
		</div>
	`;
}

export function Secret ({ issued, onDone }) {
	/*
		The credential, at the one moment it can be read.

		**Shown once, and the form says so before minting rather than after.** Telling somebody a
		secret is unrecoverable *after* they have closed the box is an apology; telling them
		before is a warning. `agent create` prints the same sentence at a terminal, where the
		scrollback is the reader's own; a page is not scrollback, so this is the one control here
		that must be dismissed deliberately.

		**Nothing stores it and nothing offers to.** No copy-to-clipboard, because that is a
		promise about a permission this page does not hold and cannot check; no download, which
		the artifact sandbox would refuse anyway. Select and copy is what every terminal does.
	*/
	return html`
		<div class="secret">
			${issued.account_created
				? html`<p>Created the machine identity <strong>${issued.username}</strong>.</p>`
				: null}
			<p><strong>This is the only time this credential is shown.</strong> Nothing
				recovers it afterwards, including this instance.</p>
			<code class="credentialvalue">${issued.token}</code>
			<div class="acts">
				<button type="button" class="primary" onClick=${onDone}>I have copied it</button>
			</div>
		</div>
	`;
}


export function offeredScopes (me) {
	/*
		Which permissions this operator may put on a credential — `#1396`.

		**Their own, because that is exactly the envelope.** `_refuse_amplification` refuses a
		credential wider than the one asking for it on four axes, and scopes are one of them —
		so the set a form may honestly offer *is* what the operator holds. Offering more and
		letting the server refuse is the shape `app.js` states the rule against three times: a
		control that refuses when pressed is worse than one that is not there.

		**No vocabulary endpoint is needed and none should be added.** `/v1/meta` publishes
		statuses, types and link types and deliberately not permissions; `/v1/me` already
		publishes what this caller may do, per workspace and over the installation. A hardcoded
		list in this file would be a second copy of the permission vocabulary, which is this
		codebase's signature defect.

		**The union across workspaces, and a scope is a ceiling rather than a grant.** Somebody
		who administers one workspace and reads another may name either permission; `authorize`
		still asks per workspace when the credential is used, so a scope naming something they
		cannot do somewhere is simply never exercisable there. Narrowing never widens — §7.3 —
		and the form says so, because *choosing permissions* reads like handing them out.
	*/
	const found = new Set(me && me.instance_permissions ? me.instance_permissions : []);

	for (const space of (me && me.workspaces) || []) {
		for (const may of space.permissions || []) found.add(may);
	}

	return [...found].sort();
}

export function Issuing ({ subject, offers, workspaces, onIssue, onCancel, busy = false }) {
	/*
		Mint a credential, and create a machine identity to hold it where that is the act.

		**`subject.creating` is the difference between the two acts, and they are two acts.**
		Naming a machine identity creates one if there is none — that is what `agent create` is,
		one call — while naming a person never creates, and conflating them is `#207`'s subject:
		a person named where a machine was expected would have their credential quietly issued
		under an argument whose stated subject is machines.

		**Nothing here is an *approve* button.** `#1382` §4.4: a screen that shows a
		recommendation with an accept button manufactures rubber-stamping, and *"a rubber stamp
		is worse than no control because it produces a record saying a human checked"*. So this
		asks what the credential may do and offers no default answer to it — an unchosen scope
		set means *not narrowed*, which is stated on the control rather than assumed.

		**The warning about the secret is before the act, not after it.** Told afterwards it is
		an apology; told here it is a warning, and it is the only chance a reader gets.
	*/
	return html`
		<form class="issuing" onSubmit=${(event) => {
			event.preventDefault();

			const fields = new FormData(event.target);
			const scopes = fields.getAll("scopes").map(String);

			onIssue({
				title: String(fields.get("title") || "").trim(),
				serviceAccount: subject.creating
					? String(fields.get("who") || "").trim()
					: null,
				username: subject.creating ? null : subject.username,
				workspace: String(fields.get("workspace") || "") || null,
				expires: String(fields.get("expires") || "").trim() || null,
				scopes,
			});
		}}>
			<h3>${subject.creating ? "Add an agent" : `Issue a credential for @${subject.username}`}</h3>

			${subject.creating
				? html`
					<label class="field">
						<span>What to call it</span>
						<input name="who" required placeholder="claude-nuc14" />
					</label>
				`
				: null}

			<label class="field">
				<span>What this credential is for</span>
				<input name="title" required placeholder="Nightly triage" />
			</label>

			<label class="field">
				<span>Only in this workspace</span>
				<select name="workspace">
					<option value="">Every workspace its owner belongs to</option>
					${(workspaces || []).map((space) => html`
						<option value=${space.slug}>${space.title}</option>
					`)}
				</select>
			</label>

			<label class="field">
				<span>Stops working</span>
				<input name="expires" placeholder="now+30d, or a date" />
			</label>

			${/*
				**Checkboxes rather than a multiple-select**, because the default matters and a
				multiple-select cannot state one. Nothing ticked is *not narrowed*, which is the
				opposite of what an empty list looks like it means — so the sentence beneath says
				it in words rather than leaving a reader to infer it from an empty control.
			*/ null}
			<fieldset class="scopes">
				<legend>What it may do</legend>
				<p class="hint">Tick nothing and it may do everything its owner can. Ticking
					narrows it; it can never grant more than you hold.</p>
				${(offers || []).map((may) => html`
					<label class="scope">
						<input type="checkbox" name="scopes" value=${may} />
						<span>${may}</span>
					</label>
				`)}
			</fieldset>

			<p class="warn">The credential is shown once, when it is made. Nothing recovers it
				afterwards.</p>

			<div class="acts">
				<button type="button" class="quiet" onClick=${onCancel}>Cancel</button>
				<button type="submit" class="primary" disabled=${busy}>
					${subject.creating ? "Add the agent" : "Issue it"}
				</button>
			</div>
		</form>
	`;
}
