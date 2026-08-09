/*
	The smallest document Preact will render into — item `#640`.

	**Not a browser, and the whole discipline is that it must never grow into one.** Its only
	job is to let `render()` run an effect, because everything after the first paint is what no
	test here could reach: `App` is 698 lines of `app.js`, holds every write and every decision
	about what to fetch, and its work happens inside `useEffect`.

	`preact-render-to-string` — vendored beside this — runs `useState` and `useCallback` and
	**does not run effects**, which was measured rather than assumed. So it catches `App`
	throwing on first paint and nothing after it. Five faults have shipped from that gap, every
	one found by a person reloading a page.

	**What Preact actually touches is three methods**, measured by giving it a document that
	recorded every access: `createElementNS`, `createTextNode` and `insertBefore`. The rest of
	what is here exists because the *app* asks for it — `getElementById` to mount into,
	`location` and `history` because addressing is half of what it does, and
	`requestAnimationFrame` because that is how Preact defers an effect.

	**The known risk, stated because this project has been bitten by it**: a harness that
	substitutes the mechanism under test can only ever confirm the half that was not broken.
	This is a substitute. It is defensible while it does one narrow thing — mount the app and
	record what it asks the instance for — and it stops being defensible the moment a test wants
	to click something. **That is the trigger to go to jsdom, not to grow this file.** There is
	no npm on the development machine today, which is why that was not the first answer.
*/

/* Every node is the same shape, because Preact does not care and a reader should not have to
   learn two. `nodeType` 3 is a text node and 1 is an element; those two numbers are the whole
   of the DOM's type system as far as this needs to go. */
export function node (name) {
	const self = {
		nodeName: String(name).toUpperCase(),
		nodeType: name === "#text" ? 3 : 1,
		childNodes: [],
		parentNode: null,
		style: {},
		data: "",
		firstChild: null,
		nextSibling: null,
		/* Preact reads this to decide whether it is inside an SVG. Null means "no", which is
		   true of every element this app renders. */
		ownerSVGElement: null,

		appendChild (child) {
			return self.insertBefore(child, null);
		},

		insertBefore (child, before) {
			child.parentNode = self;

			const at = before === null ? -1 : self.childNodes.indexOf(before);

			if (at < 0) self.childNodes.push(child);
			else self.childNodes.splice(at, 0, child);

			relink(self);

			return child;
		},

		removeChild (child) {
			self.childNodes = self.childNodes.filter((held) => held !== child);
			child.parentNode = null;
			relink(self);

			return child;
		},

		remove () {
			if (self.parentNode) self.parentNode.removeChild(self);
		},

		setAttribute (name_, value) { self[name_] = value; },
		removeAttribute (name_) { delete self[name_]; },

		/* Accepted and dropped. Nothing here dispatches an event, which is the line this file
		   holds: a test that wants to click needs a real DOM, not a larger pretence. */
		addEventListener () {},
		removeEventListener () {},
	};

	return self;
}

function relink (parent) {
	/* `firstChild` and `nextSibling` are how Preact walks what it has already placed, so they
	   have to agree with `childNodes` after every change rather than being set once. */
	parent.firstChild = parent.childNodes[0] || null;

	parent.childNodes.forEach((child, index) => {
		child.nextSibling = parent.childNodes[index + 1] || null;
	});
}

export function text (of) {
	/* Everything a reader would see, flattened. */
	return of.nodeType === 3 ? of.data : of.childNodes.map(text).join("");
}

export function install ({ pathname = "/", search = "" } = {}) {
	/*
		Put a document in front of the app, and return the root it mounted into.

		**The address is an argument because it is an input.** `#651` made the view part of it
		and `#638` made an item's address the thing you send somebody, so "what does the app ask
		for when it arrives *here*" is the question worth being able to pose.
	*/
	const root = node("div");
	const written = [];

	globalThis.document = {
		createElement: (name) => node(name),
		createElementNS: (_namespace, name) => node(name),
		createTextNode: (value) => {
			const made = node("#text");
			made.data = String(value);

			return made;
		},
		getElementById: () => root,
		addEventListener () {},
		removeEventListener () {},
	};

	/* Preact defers an effect to a frame and cancels the fallback timer, so it needs both
	   halves of the pair — the second was found by the first version throwing on it. */
	globalThis.requestAnimationFrame = (run) => setTimeout(run, 0);
	globalThis.cancelAnimationFrame = (timer) => clearTimeout(timer);

	globalThis.window = {
		location: { pathname, search, href: `http://instance${pathname}${search}` },
		/* Recorded rather than applied. What the app *would* have written is the thing worth
		   asserting on, and applying it would mean re-deriving `location` here — a second copy
		   of the browser's rule, which is what this file exists to avoid needing. */
		history: {
			pushState: (state, _title, to) => written.push({ how: "push", to }),
			replaceState: (state, _title, to) => written.push({ how: "replace", to }),
		},
		addEventListener () {},
		removeEventListener () {},
		scrollTo () {},
	};

	return { root, written };
}
