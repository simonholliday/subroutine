/*
	The tagged template every component is written with — `htm` bound to Preact's `h`.

	**One binding, imported rather than repeated.** `htm.bind` caches the trees it parses
	against the tag it returns, so a second binding is a second cache of the same templates —
	and two of them is the shape this codebase keeps finding wrong for other reasons.
*/

import { h } from "preact";
import htm from "htm";

export const html = htm.bind(h);
