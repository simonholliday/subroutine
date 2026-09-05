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
import { named } from "./dates.js";

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

export function Principal ({ person, held }) {
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
		</div>
	`;
}

export function People ({ people, rosters, asked = 0, reached = 0 }) {
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
				/>
			`)}
		</div>
	`;
}
