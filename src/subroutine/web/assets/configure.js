/*
	The settings area — `#1445`, design `#2110` §3 and §4.

	**One address space for four scopes, and it holds one of them so far.** `#2110` puts every
	settings page under `/settings` — the reader's own, then a workspace's, a project's and the
	installation's — and draws each the same way. The reader's own came first because it needed
	nothing built: `user.timezone` is a column rather than a registry entry (`#1024` §4's rule,
	since §6.5's chain computes with it on every date), `PATCH /v1/users/{username}` already
	accepts it, and `/v1/me` already reports it.

	**An area rather than a fourth view**, which is `people.js`'s argument unchanged: somebody
	who never opens this never meets a workspace, a role or a credential, so §1.4 is kept by
	where the page lives rather than by care.

	**Not `settings.js`**, which is the module of numbers and field lists every request is built
	from, and was named long before this area existed.
*/

import { html } from "./html.js";


export function listedZones () {
	/*
		Every zone this browser can render a date in, or none where it cannot say.

		**The browser's list rather than one of ours**, because a page drawing dates in a zone
		can only honestly offer zones it can draw them in — and a list written here would be a
		second copy of the IANA database that nothing would ever bring up to date.
	*/
	try {
		return typeof Intl.supportedValuesOf === "function"
			? Intl.supportedValuesOf("timeZone")
			: [];
	} catch (_) {
		return [];
	}
}


export function deviceZone () {
	/* The zone this device says it is in, or null where it will not say. */
	try {
		return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
	} catch (_) {
		return null;
	}
}


/*
	What a timezone control offers whether or not the browser lists it.

	**Measured on Node 18: `UTC` is not among the 418 zones `Intl.supportedValuesOf` names**, and
	it is exactly what a server set to UTC reports. So a reader whose zone is UTC — the commonest
	value on a machine nobody configured — would have found the control reading *Not set*.
*/
export const ALWAYS_OFFERED = ["UTC"];

/* Where a zone whose name has no region goes, which is last: the one a reader looks for least. */
const OTHER = "Other";


export function zoneChoices (listed = [], also = []) {
	/*
		The zones a control offers, grouped by the region each one names.

		**`also` is offered whether or not the browser lists it** — the zone in force and the
		device's own, above all. A control that cannot show the value somebody chose reads *Not
		set* to them, which is a false answer about their own account on the one page that
		exists to tell them.

		**Grouped on the first segment**, because four hundred names in one list is a list
		nobody reads: `Europe/London` is found under *Europe*.
	*/
	const every = new Set([...(listed || []), ...(also || []).filter(Boolean)]);
	const regions = new Map();

	for (const zone of [...every].sort()) {
		const cut = zone.indexOf("/");
		const region = cut > 0 ? zone.slice(0, cut) : OTHER;

		if (!regions.has(region)) regions.set(region, []);

		regions.get(region).push(zone);
	}

	const named = [...regions.keys()].filter((region) => region !== OTHER).sort();
	const ordered = regions.has(OTHER) ? [...named, OTHER] : named;

	return ordered.map((region) => ({ region, zones: regions.get(region) }));
}


export function zoneSaid (stored, reading) {
	/*
		Where the zone in force came from, in words — `#2110` §4's provenance, for the one setting
		this page holds.

		**Chosen and not chosen are one zone and two facts**, and they read differently: *you
		chose London* is something a reader can undo, where *London, because nobody said
		otherwise* is a wider scope showing through, and clearing it changes nothing.

		**It describes §6.5's order**, which is `schedule.zone_for`: a person's zone comes before a
		workspace's, so one chosen here holds in every workspace; unchosen, each workspace's own
		holds inside it and the installation's everywhere else. `reading` is `/v1/me`'s
		`reader_timezone` — that chain with the workspace step left out — so while nothing is
		chosen it *is* the installation's zone, and naming it costs nothing.
	*/
	if (stored) return `Every date is worked out in ${stored}, in every workspace.`;

	const installation = reading ? ` — ${reading} —` : "";

	return "Not set, so each workspace's own timezone is used inside it, and this installation's"
		+ `${installation} everywhere else.`;
}


export function Timezone ({
	stored = null, reading = null, zones = [], device = null, onZone, busy = false,
}) {
	/*
		The one control `/settings/me` holds — `#1446`, and `#1297`'s option 2.

		**The only way somebody with no terminal can say where they are.** `user timezone` acts
		on whoever is signed in and takes no `--username`, by design — *you know which zone you
		are in better than anybody else does* — so nobody could say it for them either.

		**Clearing is choosing *Not set*, not a second button.** Null is a value here (§8.3): it
		puts the reader back on each workspace's zone, and `timezoneRequest` sends it rather than
		leaving the field out, which the route would read as *leave it alone*.

		**This device's zone is one press away when it differs from the zone in force**, because
		finding one name among four hundred is the whole cost of this control and the browser
		already knows the likeliest answer. Offered *beside* the list and never selected in it:
		a value sitting in a control that nobody chose is read as one somebody did.

		**Uncontrolled, like every form here** (`#757`). The option in force is marked
		`selected` and nothing else is held, so a re-render cannot reach in and undo a choice
		somebody is halfway through making.
	*/
	const choices = zoneChoices(zones, [stored, device, ...ALWAYS_OFFERED]);
	const offer = device && device !== stored ? device : null;

	return html`
		<section class="timezone">
			<h3>Your timezone</h3>
			<p>Where you keep your diary: <em>today</em>, <em>tomorrow</em> and every date you
				read are worked out in it.</p>
			<form onSubmit=${(event) => {
				event.preventDefault();

				onZone(String(new FormData(event.target).get("timezone") || "") || null);
			}}>
				<label>
					<span>Timezone</span>
					<select class="field" name="timezone" disabled=${busy}>
						<option value="" selected=${!stored}>Not set</option>
						${choices.map(({ region, zones: here }) => html`
							<optgroup key=${region} label=${region}>
								${here.map((zone) => html`
									<option key=${zone} value=${zone} selected=${zone === stored}>${zone}</option>
								`)}
							</optgroup>
						`)}
					</select>
				</label>
				<p class="hint">${zoneSaid(stored, reading)}</p>
				${offer ? html`<p class="hint">This device is set to ${offer}.</p>` : null}
				<div class="acts">
					${offer
						? html`<button type="button" class="action" disabled=${busy}
								onClick=${() => onZone(offer)}>Use ${offer}</button>`
						: null}
					<button type="submit" class="primary" disabled=${busy}>Save</button>
				</div>
			</form>
		</section>
	`;
}


/*
	Which control draws a setting, by the name of its kind — `#1447`, design `#2110` §5.

	**A switch, not a form engine**, which is §5's own decision: an engine for two settings and
	four scopes is a primitive whose only customer is a guess. One entry per kind the registry
	publishes, and nothing else.

	**`CONTROLLED_KINDS` is derived from it rather than listed beside it**, so a guard can hold
	this map against the registry's kinds and a kind added without a control fails the build.
	A kind this browser has never heard of — an instance a release ahead may publish one — is
	said on the page, never left out (`#1539`).
*/
const CONTROLS = {
	colour: ColourChoice,
	status_keys: StatusChoice,
};

export const CONTROLLED_KINDS = Object.keys(CONTROLS);


export function inForceSaid (stated) {
	/*
		Where a value in force came from, in words — `#2110` §4's provenance, for any setting.

		**Three answers, and a page reads differently for each.** A value set here can be cleared
		here; one inherited can only be overridden here or changed where it was set; and one
		stated nowhere is the default, which nobody chose.
	*/
	if (!stated) return "";

	if (stated.set_here) return "Set here.";

	const from = stated.inherited_from;

	if (from && from.scope === "workspace") {
		return `Inherited from the workspace, ${from.title || from.address}.`;
	}

	if (from) return `Inherited from ${from.title || from.address} (${from.address}).`;

	return "Not set anywhere, so the default applies.";
}


export function valueSaid (setting, value, statuses = []) {
	/*
		A value in force, in words — for a reader who may not change it, so a row they can only
		read still says what is true rather than only where it came from.
	*/
	if (setting.kind === "colour") return value || "None";

	if (setting.kind === "status_keys") {
		const labels = new Map((statuses || []).map((one) => [one.key, one.label || one.key]));
		const named = (value || []).map((key) => labels.get(key) || key);

		return named.length > 0 ? `Not offered: ${named.join(", ")}.` : "Every status is offered.";
	}

	return value === null || value === undefined ? "Not set." : String(value);
}


function SettingRow ({ setting, stated, statuses = [], may = false, onChoose, busy = false }) {
	/*
		One setting on a settings page: what it is, where its value came from, and — when this
		reader may change it and this browser knows its kind — the control for it.

		**Shown and not offered when the reader lacks the verb**, because a control that refuses
		when pressed is worse than one that is not there, and the value still answers *why is it
		like this*.
	*/
	const Control = CONTROLS[setting.kind];
	const value = stated ? stated.value : setting.default;
	const verb = (setting.permission || {}).workspace;

	return html`
		<div class="setting-row">
			<h4>${setting.summary}</h4>
			<p class="hint">${inForceSaid(stated)}</p>
			${!Control
				? html`
					<p>${valueSaid(setting, value, statuses)}</p>
					<p class="hint">This page cannot change this setting yet: it is a kind this
						version of the browser does not know how to draw.</p>`
				: !may
				? html`
					<p>${valueSaid(setting, value, statuses)}</p>
					<p class="hint">Changing this needs the ${verb} permission, which you do not
						hold here.</p>`
				: html`<${Control} setting=${setting} value=${value} statuses=${statuses}
						onChoose=${onChoose} busy=${busy} />`}
		</div>
	`;
}


export function WorkspaceSettings ({
	workspace = null, registry = [], statuses = [], inForce = null, may = [], onChoose,
	busy = false,
}) {
	/*
		A workspace's settings page — `#1447`, design `#2110` §3 to §5.

		**Built from what the instance publishes, never from a list of ours.** Which settings
		exist, what each accepts and which verb changes it come from the registry in `/v1/meta`
		(`#2365`); what is in force and where it came from, from the settings read (`#2450`). So
		a setting added to the registry appears here with no change to this file, which is
		`#1024`'s promise that *adding a setting is an entry and a default*.

		**`may` is the reader's verbs in this workspace**, `allowedIn`'s answer, and each row is
		gated on the verb the registry publishes for it at this scope.
	*/
	if (!workspace || !inForce) return html`<div class="empty">Reading…</div>`;

	const held = new Set(may || []);
	const stated = new Map((inForce.settings || []).map((one) => [one.key, one]));
	const here = (registry || []).filter((one) => (one.scopes || []).includes("workspace"));

	return html`
		<section class="setting-page">
			<h3>${workspace.title || workspace.slug}</h3>
			${here.length === 0
				? html`<p class="empty">This workspace has nothing to configure.</p>`
				: here.map((setting) => html`
					<${SettingRow} key=${setting.key} setting=${setting}
						stated=${stated.get(setting.key)} statuses=${statuses}
						may=${held.has((setting.permission || {}).workspace)}
						onChoose=${onChoose} busy=${busy} />
				`)}
		</section>
	`;
}


export function ColourChoice ({ setting, value = null, onChoose, busy = false }) {
	/*
		Choose a colour from the palette the instance publishes, or none.

		**The choices are the registry's** (`#2365`), so the palette is not copied here, and each
		is drawn as its own swatch — coloured by the stylesheet's rule for that name, the one a
		row's edge uses, so a choice looks like what it will do.

		**Uncontrolled, like every form here** (`#757`): the value in force is `checked`, so a
		re-render cannot undo a choice somebody is halfway through making.
	*/
	return html`
		<form class="setting-choice" onSubmit=${(event) => {
			event.preventDefault();

			onChoose(setting.key, String(new FormData(event.target).get("value") || "") || null);
		}}>
			<fieldset>
				<legend>Colour</legend>
				<label class="setting-option">
					<input type="radio" name="value" value="" checked=${!value} disabled=${busy} />
					<span>None</span>
				</label>
				${(setting.choices || []).map((name) => html`
					<label class="setting-option" key=${name}>
						<input type="radio" name="value" value=${name} checked=${name === value}
							disabled=${busy} />
						<span class="setting-swatch" data-colour=${name}></span>
						<span>${name}</span>
					</label>
				`)}
			</fieldset>
			<div class="acts">
				<button type="submit" class="primary" disabled=${busy}>Save</button>
			</div>
		</form>
	`;
}


export function StatusChoice ({ setting, value = [], statuses = [], onChoose, busy = false }) {
	/*
		Choose the statuses not offered here, from this workspace's own vocabulary.

		**A deny-list, drawn as one**: a status ticked is a status not offered. `statuses.hidden`
		is a deny-list by decision, so that a status added later is offered everywhere by
		default, and a control drawn as an allow-list would turn that round in the reader's head.

		**Nothing ticked clears the setting** rather than storing an empty list, because at a
		workspace — the top of the chain — the two answer the same, and *not stated* is the one
		that lets the default show. At a project they differ, and a project's page will have to
		say so.
	*/
	const hidden = new Set(value || []);

	return html`
		<form class="setting-choice" onSubmit=${(event) => {
			event.preventDefault();

			const ticked = new FormData(event.target).getAll("value").map(String);

			onChoose(setting.key, ticked.length > 0 ? ticked : null);
		}}>
			<fieldset>
				<legend>Not offered</legend>
				${(statuses || []).map((one) => html`
					<label class="setting-option" key=${one.key}>
						<input type="checkbox" name="value" value=${one.key}
							checked=${hidden.has(one.key)} disabled=${busy} />
						<span>${one.label || one.key}</span>
					</label>
				`)}
			</fieldset>
			<div class="acts">
				<button type="submit" class="primary" disabled=${busy}>Save</button>
			</div>
		</form>
	`;
}


function SettingsNav ({ page = null, workspaces = [] }) {
	/*
		The pages this area holds, for this reader — `#1447`.

		**Plain anchors, like the footer's**: a settings page is opened deliberately and rarely, a
		full load between two of them costs nothing, and the address is the page (`#745`).

		**Every workspace the reader can reach is listed**, not only those they administer: a page
		they cannot change still answers *why is it like this*, and a list that left the others
		out would say those workspaces have no settings.
	*/
	const chosen = (scope, slug = null) => Boolean(
		page && page.scope === scope && (slug === null || page.slug === slug),
	);

	return html`
		<nav class="setting-pages" aria-label="Settings pages">
			<a href="/settings/me" class=${chosen("me") ? "chosen" : ""}>Your account</a>
			${(workspaces || []).map((one) => html`
				<a key=${one.slug} href=${`/settings/workspace/${encodeURIComponent(one.slug)}`}
					class=${chosen("workspace", one.slug) ? "chosen" : ""}>${one.title || one.slug}</a>
			`)}
		</nav>
	`;
}


export function Settings ({
	page = null, me = null, zones = [], device = null, onZone, busy = false,
	configured = null, onChoose,
}) {
	/*
		The settings area: the page its address names, or a sentence saying it names none.

		**An address naming no page here is said, never answered with the nearest page.** A
		project's page and the installation's arrive with `#1448` and `#2103`; until they do,
		answering a link to one with some other page would show the reader something other than
		what they were sent, which is `#745`'s rule about what an address promises.

		**`me` is null until `/v1/me` answers**, drawn as *Reading…* like the people page: the
		values and the reader's verbs are both on that answer, so nothing true can be drawn first.

		**`configured` is the workspace page's two answers** — `/v1/meta` for that workspace and
		its settings read — and is used only while it is about the workspace the address names, so
		a slow answer about the previous page cannot land on this one.
	*/
	if (!me) return html`<div class="settings"><div class="empty">Reading…</div></div>`;

	const spaces = me.workspaces || [];
	const workspace = page && page.scope === "workspace"
		? spaces.find((one) => one.slug === page.slug) || null
		: null;
	const current = configured && page && configured.slug === page.slug ? configured : null;
	const meta = (current && current.meta) || {};

	return html`
		<div class="settings">
			<h2 class="area">Settings</h2>
			<${SettingsNav} page=${page} workspaces=${spaces} />
			${!page
				? html`<p class="empty">There is no settings page at this address.
						<a href="/settings/me">Your own settings are here.</a></p>`
				: page.scope === "workspace" && !workspace
				? html`<p class="empty">There is no workspace called ${page.slug} that you can
						see.</p>`
				: page.scope === "workspace" && current && current.failed
				? html`<p class="empty">This workspace's settings could not be read.
						${current.failed}</p>`
				: page.scope === "workspace"
				? html`<${WorkspaceSettings} workspace=${workspace} registry=${meta.settings || []}
						statuses=${(meta.statuses && meta.statuses.task) || []}
						inForce=${current ? current.inForce : null}
						may=${workspace.permissions || []}
						onChoose=${(key, value) => onChoose(page.slug, key, value)} busy=${busy} />`
				: html`<${Timezone} stored=${(me.user && me.user.timezone) || null}
						reading=${me.reader_timezone || null} zones=${zones} device=${device}
						onZone=${onZone} busy=${busy} />`}
		</div>
	`;
}
