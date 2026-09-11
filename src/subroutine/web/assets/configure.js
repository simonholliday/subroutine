/*
	The settings area — `#1445`, design `#2110` §3 and §4.

	**One address space for four scopes, and it holds all four.** `#2110` puts every
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

import { settingsAddress, titlesByPath } from "./address.js";
import { html } from "./html.js";
import { allowedIn } from "./requests.js";


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


export function inheritsAt (setting, scope) {
	/*
		Whether a setting has a wider scope above this one to inherit from — `#1448`.

		**Read off the setting's own scopes**, which the registry lists most specific first, so
		the answer is *is there a scope after this one*. At the widest a setting has, clearing it
		leaves the default; below that, clearing it lets a wider scope show through — and *hide
		nothing here* stops being the same answer as *not stated*.
	*/
	const scopes = (setting && setting.scopes) || [];
	const at = scopes.indexOf(scope);

	return at >= 0 && at < scopes.length - 1;
}


export function hiddenValue (ticked, inherits = false) {
	/*
		What a status control sends for the statuses ticked in it — `#1448`.

		**Nothing ticked is two answers, by scope.** Where nothing is above, it clears the
		setting: *hide nothing* and *not stated* read the same there, and the second is the one
		that lets the default show. Below that they differ — an empty list stops a status hidden
		further up from being hidden here, where clearing lets it through — so an empty list is
		sent as one, and clearing is the row's own act.
	*/
	const keys = (ticked || []).map(String);

	return keys.length > 0 || inherits ? keys : null;
}


function sourcePage (from, slug) {
	/*
		The settings page of whatever a value was inherited from, or null where there is none to
		name — `#1448`.

		**A project's ancestors are in its own workspace**, because a project tree never crosses
		one (§5.4), so the page's workspace is theirs and a source need carry only its address.
	*/
	if (!from) return null;

	if (from.scope === "workspace") return { scope: "workspace", slug: from.address };

	if (from.scope === "project" && slug) return { scope: "project", slug, project: from.address };

	return null;
}


function acts (onTakeBack, busy) {
	/*
		A setting control's buttons: save, and — where a value is set here and a wider scope
		could show through — take it back.

		**Taking back is not choosing nothing**, which is why it is a button of its own rather
		than an empty form (`#1448`): below the widest scope, *nothing* is a value a reader may
		want to state, and saying so must not be the same press as un-saying everything.
	*/
	return html`
		<div class="acts">
			<button type="submit" class="primary" disabled=${busy}>Save</button>
			${onTakeBack
				? html`<button type="button" class="action" disabled=${busy}
						onClick=${onTakeBack}>Stop setting it here</button>`
				: null}
		</div>
	`;
}


function SettingRow ({
	setting, stated, scope, slug = null, statuses = [], may = false, onChoose, busy = false,
}) {
	/*
		One setting on a settings page: what it is, where its value came from, and — when this
		reader may change it and this browser knows its kind — the control for it.

		**Shown and not offered when the reader lacks the verb**, because a control that refuses
		when pressed is worse than one that is not there, and the value still answers *why is it
		like this*. The verb is the one the registry publishes **for this scope**, since `#2120`
		made the answer differ between them.

		**An inherited value leads to where it is set** (`#1448`), because that is the one place
		it can be changed for everything that inherits it; and **a value set here can be taken
		back**, which is offered only where there is something above to show through and only
		when it is set here — clearing a value that is inherited changes nothing (`#2110` §4).
	*/
	const Control = CONTROLS[setting.kind];
	const value = stated ? stated.value : setting.default;
	const verb = (setting.permission || {})[scope];
	const inherits = inheritsAt(setting, scope);
	const here = Boolean(stated && stated.set_here);
	const from = stated && !here ? sourcePage(stated.inherited_from, slug) : null;
	const takeBack = inherits && here ? () => onChoose(setting.key, null) : null;

	return html`
		<div class="setting-row">
			<h4>${setting.summary}</h4>
			<p class="hint">${inForceSaid(stated)}${from
				? html` <a href=${settingsAddress(from)}>Open its settings.</a>`
				: null}</p>
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
				: html`
					${inherits && !here
						? html`<p class="hint">Saving sets it on this ${scope}, and a change made
								above it will no longer reach it.</p>`
						: null}
					<${Control} setting=${setting} value=${value} statuses=${statuses}
						inherits=${inherits} onChoose=${onChoose} onTakeBack=${takeBack}
						busy=${busy} />`}
		</div>
	`;
}


function settingRows ({
	scope, slug, registry = [], statuses = [], inForce, may = [], onChoose, busy = false,
}) {
	/*
		Every setting the registry offers at one scope, as rows — the body a workspace's page and
		a project's page share, so the two cannot come to draw one setting differently.
	*/
	const held = new Set(may || []);
	const stated = new Map((inForce.settings || []).map((one) => [one.key, one]));
	const offered = (registry || []).filter((one) => (one.scopes || []).includes(scope));

	if (offered.length === 0) return html`<p class="empty">This ${scope} has nothing to configure.</p>`;

	return offered.map((setting) => html`
		<${SettingRow} key=${setting.key} setting=${setting} scope=${scope} slug=${slug}
			stated=${stated.get(setting.key)} statuses=${statuses}
			may=${held.has((setting.permission || {})[scope])}
			onChoose=${onChoose} busy=${busy} />
	`);
}


function ProjectPages ({ slug, projects = [], more = false }) {
	/*
		A workspace's projects, each leading to its own settings page — `#1448`.

		**On the workspace's page because that is the way inheritance runs**: what is set here is
		what each of them shows unless it says otherwise, and its page is where it says so.

		**By the whole path, rebuilt from the tree** the way `titlesByPath` rebuilds it, because
		`path` is not a field a listing can ask for (`#770`) and a key is unique only among its
		siblings (`#958`) — so `ui` under `subroutine` must lead to `subroutine/ui`, and a title,
		which may repeat, is shown with the path that cannot.
	*/
	const titled = Object.entries(titlesByPath(projects));

	return html`
		<div class="setting-row setting-projects">
			<h4>Projects</h4>
			<p class="hint">Each project can set these for itself. What one does not set, it
				inherits — from the project above it, and then from here.</p>
			<ul>
				${titled.map(([path, title]) => html`
					<li key=${path}>
						<a href=${settingsAddress({ scope: "project", slug, project: path })}>${title}</a> <span class="hint">${path}</span>
					</li>
				`)}
			</ul>
			${more ? html`<p class="hint">Only the first ${titled.length} are listed here.</p>` : null}
		</div>
	`;
}


export function WorkspaceSettings ({
	workspace = null, registry = [], statuses = [], inForce = null, may = [], onChoose,
	busy = false, projects = null, more = false,
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

		**Its projects are listed beneath it** (`#1448`) — null until they are read, which draws
		nothing rather than a list that says the workspace has none.
	*/
	if (!workspace || !inForce) return html`<div class="empty">Reading…</div>`;

	return html`
		<section class="setting-page">
			<h3>${workspace.title || workspace.slug}</h3>
			${settingRows({
				scope: "workspace", slug: workspace.slug, registry, statuses, inForce, may, onChoose,
				busy,
			})}
			${projects && projects.length > 0
				? html`<${ProjectPages} slug=${workspace.slug} projects=${projects} more=${more} />`
				: null}
		</section>
	`;
}


export function ProjectSettings ({
	workspace = null, project = null, title = null, registry = [], statuses = [],
	inForce = null, may = [], onChoose, busy = false,
}) {
	/*
		A project's settings page — `#1448`, and the workspace page's rules one scope down.

		**Most of what this page shows is not the project's own.** A project usually states
		nothing and shows its parent's or its workspace's value through, so every row says where
		its value came from and leads there, and a value set here can be taken back — two acts
		that `#2110` §4 says read differently, and the reason the settings read carries
		provenance at all.

		**`may` is the reader's verbs in this project** — `allowedIn`'s answer, which is the
		project's own where the reader holds a role there (`#2111`) and its workspace's
		everywhere else — and a project setting is gated on the verb the registry publishes for
		a project, which is `project:write`.
	*/
	if (!workspace || !project || !inForce) return html`<div class="empty">Reading…</div>`;

	const space = { scope: "workspace", slug: workspace.slug };

	return html`
		<section class="setting-page">
			<h3>${title || project}</h3>
			<p class="hint">${project}, in <a href=${settingsAddress(space)}>${workspace.title || workspace.slug}</a>.
				What this project does not set, it inherits — from the nearest project above it that
				does, and then from the workspace.</p>
			${settingRows({
				scope: "project", slug: workspace.slug, registry, statuses, inForce, may, onChoose,
				busy,
			})}
		</section>
	`;
}


export function instanceChanges (instance, name, zone) {
	/*
		What an installation's page sends for what its form holds — `#2103`: only what differs.

		**Only what differs, because the route reads what was sent** (§8.3) and neither field may
		be set to nothing, so a field the reader did not touch has to be left out rather than
		sent back as it was. **A name emptied to nothing is sent**, not dropped: the route
		refuses it by name, which is the honest answer to somebody who cleared the box, where
		leaving it out would report a success that changed nothing.
	*/
	const changes = {};
	const called = String(name || "").trim();
	const where = String(zone || "");

	if (instance && called !== instance.name) changes.name = called;

	if (instance && where && where !== instance.timezone) changes.timezone = where;

	return changes;
}


export function InstanceSettings ({
	instance = null, zones = [], may = false, onChange, busy = false,
}) {
	/*
		This installation's page — `#2103`, and `#1668`'s conclusion that an instance's name and
		timezone are columns, joined with the other scopes at the page rather than at the storage.

		**What the row holds, and nothing else changeable.** The rest of what configures an
		installation — where its database is, where its backups go, which addresses it answers
		on and trusts, its secret — is the operator's file on the server, and much of it must not
		be changed from a page that same configuration serves. So it is named rather than shown:
		the reader learns where to go, and the page publishes nothing of the file.

		**Only somebody holding `instance:admin` may change either**, and no role carries it, so
		most readers see both values and why there is no control — which still answers *why are
		my dates in this zone* for somebody who never set their own.

		**No *Not set*, unlike a person's timezone**: this is the last word in §6.5's chain, so it
		cannot be absent, and the route refuses null.
	*/
	if (!instance) return html`<div class="empty">Reading…</div>`;

	const choices = zoneChoices(zones, [instance.timezone, ...ALWAYS_OFFERED]);

	return html`
		<section class="setting-page">
			<h3>${instance.name}</h3>
			<p class="hint">This installation. Its name tells it apart wherever somebody reaches more
				than one, and its timezone is used for anybody who has not said where they are and
				whose workspace has not either.</p>
			${!may
				? html`
					<p>It is called ${instance.name}, and works in ${instance.timezone}.</p>
					<p class="hint">Changing either needs the instance:admin permission, which only
						somebody who administers the whole installation holds.</p>`
				: html`
					<form class="setting-fields" onSubmit=${(event) => {
						event.preventDefault();

						const said = new FormData(event.target);

						onChange(instanceChanges(instance, said.get("name"), said.get("timezone")));
					}}>
						<label>
							<span>Name</span>
							<input class="field" name="name" defaultValue=${instance.name}
								disabled=${busy} />
						</label>
						<label>
							<span>Timezone</span>
							<select class="field" name="timezone" disabled=${busy}>
								${choices.map(({ region, zones: here }) => html`
									<optgroup key=${region} label=${region}>
										${here.map((zone) => html`
											<option key=${zone} value=${zone}
												selected=${zone === instance.timezone}>${zone}</option>
										`)}
									</optgroup>
								`)}
							</select>
						</label>
						<div class="acts">
							<button type="submit" class="primary" disabled=${busy}>Save</button>
						</div>
					</form>`}
			<p class="hint">Everything else about this installation — where its database is, where
				its backups go, the addresses it answers on and trusts — is set in its configuration
				file on the server, by whoever runs it. <code>subroutine config show</code> there prints
				all of it, with where each value came from.</p>
		</section>
	`;
}


export function ColourChoice ({
	setting, value = null, inherits = false, onChoose, onTakeBack = null, busy = false,
}) {
	/*
		Choose a colour from the palette the instance publishes, or none.

		**The choices are the registry's** (`#2365`), so the palette is not copied here, and each
		is drawn as its own swatch — coloured by the stylesheet's rule for that name, the one a
		row's edge uses, so a choice looks like what it will do.

		**`None` is offered only where nothing is above** (`#1448`). There null is the default,
		which is no colour; below it null means *not stated*, which lets a parent's colour show
		through — so a project cannot say *no colour*, and a `None` there would do the opposite
		of what it says. Taking a colour back is `onTakeBack`, named for what it does.

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
				${inherits
					? null
					: html`
						<label class="setting-option">
							<input type="radio" name="value" value="" checked=${!value} disabled=${busy} />
							<span>None</span>
						</label>`}
				${(setting.choices || []).map((name) => html`
					<label class="setting-option" key=${name}>
						<input type="radio" name="value" value=${name} checked=${name === value}
							disabled=${busy} />
						<span class="setting-swatch" data-colour=${name}></span>
						<span>${name}</span>
					</label>
				`)}
			</fieldset>
			${acts(onTakeBack, busy)}
		</form>
	`;
}


export function StatusChoice ({
	setting, value = [], statuses = [], inherits = false, onChoose, onTakeBack = null,
	busy = false,
}) {
	/*
		Choose the statuses not offered here, from this workspace's own vocabulary.

		**A deny-list, drawn as one**: a status ticked is a status not offered. `statuses.hidden`
		is a deny-list by decision, so that a status added later is offered everywhere by
		default, and a control drawn as an allow-list would turn that round in the reader's head.

		**Nothing ticked means what `hiddenValue` says**, which differs by scope: at a workspace
		it clears the setting, and at a project it is *hide nothing here* (`#1448`) — the one
		place `[]` and null are different answers, and the reason taking a value back is a
		button of its own rather than an empty form.
	*/
	const hidden = new Set(value || []);

	return html`
		<form class="setting-choice" onSubmit=${(event) => {
			event.preventDefault();

			onChoose(setting.key, hiddenValue(new FormData(event.target).getAll("value"), inherits));
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
			${acts(onTakeBack, busy)}
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

		**A project's page marks its workspace** (`#1448`), whose page is where its projects are
		listed. A list naming every project on the installation here would be one nobody scans.

		**This installation comes last and is listed for everybody** (`#2103`): last because it is
		the widest scope, and for everybody because its page answers *why is it like this* for a
		reader who may not change it, as a workspace's does.
	*/
	const chosen = (slug = null) => Boolean(
		page && (slug === null
			? page.scope === "me"
			: (page.scope === "workspace" || page.scope === "project") && page.slug === slug),
	);

	return html`
		<nav class="setting-pages" aria-label="Settings pages">
			<a href="/settings/me" class=${chosen() ? "chosen" : ""}>Your account</a>
			${(workspaces || []).map((one) => html`
				<a key=${one.slug} href=${settingsAddress({ scope: "workspace", slug: one.slug })}
					class=${chosen(one.slug) ? "chosen" : ""}>${one.title || one.slug}</a>
			`)}
			<a href=${settingsAddress({ scope: "instance" })}
				class=${page && page.scope === "instance" ? "chosen" : ""}>This installation</a>
		</nav>
	`;
}


export function Settings ({
	page = null, me = null, zones = [], device = null, onZone, busy = false,
	configured = null, onChoose, onInstance,
}) {
	/*
		The settings area: the page its address names, or a sentence saying it names none.

		**An address naming no page here is said, never answered with the nearest page**:
		answering it with some other page would show the reader something other than what they
		were sent, which is `#745`'s rule about what an address promises.

		**`me` is null until `/v1/me` answers**, drawn as *Reading…* like the people page: the
		values and the reader's verbs are both on that answer, so nothing true can be drawn first.

		**`configured` is the answers a workspace's, a project's or the installation's page
		reads** — `/v1/meta`, and for the first two a settings read and the workspace's projects
		too — used only while it is about the page the address names, **keyed by that page's own
		address**, so a slow answer about the previous page cannot land on this one.
	*/
	if (!me) return html`<div class="settings"><div class="empty">Reading…</div></div>`;

	const spaces = me.workspaces || [];
	const entity = Boolean(page && (page.scope === "workspace" || page.scope === "project"));
	const workspace = entity ? spaces.find((one) => one.slug === page.slug) || null : null;
	const current = configured && page && configured.key === settingsAddress(page)
		? configured
		: null;
	const meta = (current && current.meta) || {};
	const called = { workspace: "workspace", project: "project", instance: "installation" };
	const shared = {
		registry: meta.settings || [],
		statuses: (meta.statuses && meta.statuses.task) || [],
		inForce: current ? current.inForce : null,
		/* **This project's own answer where it has one** (`#2111`) — `allowedIn`'s rule, so this
		   page and an open item cannot disagree about what a reader may do in one project. */
		may: entity
			? [...allowedIn(me, page.slug, page.scope === "project" ? { address: page.project } : null)]
			: [],
		onChoose: (key, value) => onChoose(page, key, value),
		busy,
	};

	return html`
		<div class="settings">
			<h2 class="area">Settings</h2>
			<${SettingsNav} page=${page} workspaces=${spaces} />
			${!page
				? html`<p class="empty">There is no settings page at this address.
						<a href="/settings/me">Your own settings are here.</a></p>`
				: entity && !workspace
				? html`<p class="empty">There is no workspace called ${page.slug} that you can
						see.</p>`
				: current && current.failed
				? html`<p class="empty">This ${called[page.scope] || "page"}'s settings could not be read.
						${current.failed}</p>`
				: page.scope === "workspace"
				? html`<${WorkspaceSettings} workspace=${workspace} ...${shared}
						projects=${current ? current.projects : null}
						more=${Boolean(current && current.more)} />`
				: page.scope === "project"
				? html`<${ProjectSettings} workspace=${workspace} project=${page.project}
						title=${current ? titlesByPath(current.projects)[page.project] : null}
						...${shared} />`
				: page.scope === "instance" && current && !meta.instance
				? html`<p class="empty">This installation's answer did not say what it is called.</p>`
				: page.scope === "instance"
				? html`<${InstanceSettings} instance=${meta.instance || null} zones=${zones}
						may=${(me.instance_permissions || []).includes("instance:admin")}
						onChange=${onInstance} busy=${busy} />`
				: html`<${Timezone} stored=${(me.user && me.user.timezone) || null}
						reading=${me.reader_timezone || null} zones=${zones} device=${device}
						onZone=${onZone} busy=${busy} />`}
		</div>
	`;
}
