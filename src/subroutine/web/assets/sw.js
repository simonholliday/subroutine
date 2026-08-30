/*
	The service worker — item `#1665`.

	**It caches nothing, and that is the whole design.** Chrome will not offer *Install app*
	for a page with no service worker, so one has to exist; every other thing a service worker
	is for collides head-on with `#914`, which replaced `public, max-age=300` with `no-cache`
	precisely so that a restarted server cannot serve current bytes to a page that will not ask
	for them. Its own comment names the risk in terms: *a long cache is a user looking at last
	week's app with no way to know it*. A worker that cached the shell would be that risk with a
	longer memory and no expiry at all.

	**So this passes every request straight to the network** and the app behaves, byte for byte,
	as it does without it. The cost is that an installed app is not usable offline — which is
	honest rather than unfortunate: everything on the page comes from the instance, so an
	offline shell would render an empty page and a row of failed requests.

	**Why the handler is not empty.** `self.addEventListener("fetch", () => {})` would satisfy a
	reading of *has a fetch handler* and nothing else: Chrome detects a handler that never calls
	`respondWith` and skips the worker for performance, so the installability check it was
	written to pass is the one it would fail. Answering with an ordinary `fetch` of the same
	request is the smallest thing that is really a handler.

	**`skipWaiting` and `clients.claim` are `#914`'s rule applied to this file.** Without them a
	replaced worker waits for every tab to close before it takes over, which is exactly the
	stale-version trap the no-cache policy exists to avoid — and a worker is the one asset a
	hard refresh does not fix.
*/

self.addEventListener("install", () => {
	self.skipWaiting();
});

self.addEventListener("activate", (event) => {
	event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
	event.respondWith(fetch(event.request));
});
