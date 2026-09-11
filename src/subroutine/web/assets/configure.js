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


export function Settings ({
	page = null, me = null, zones = [], device = null, onZone, busy = false,
}) {
	/*
		The settings area: the page its address names, or a sentence saying it names none.

		**An address naming no page here is said, never answered with the nearest page.** A
		workspace's page arrives with `#1447`; until it does, answering a link to one with the
		reader's own settings would show them something other than what they were sent, which is
		`#745`'s rule about what an address promises.

		**`me` is null until `/v1/me` answers**, drawn as *Reading…* like the people page: the
		zone in force is on that answer, so there is nothing true to draw before it.
	*/
	if (!me) return html`<div class="settings"><div class="empty">Reading…</div></div>`;

	return html`
		<div class="settings">
			<h2 class="area">Settings</h2>
			${page
				? html`<${Timezone} stored=${(me.user && me.user.timezone) || null}
						reading=${me.reader_timezone || null} zones=${zones} device=${device}
						onZone=${onZone} busy=${busy} />`
				: html`<p class="empty">There is no settings page at this address.
						<a href="/settings/me">Your own settings are here.</a></p>`}
		</div>
	`;
}
