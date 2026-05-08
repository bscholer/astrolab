// Static adapter: prerender all routes that don't depend on a runtime API.
// The Library page fetches /api/* in the browser, so we keep SSR off for the
// production bundle.
export const prerender = true;
export const ssr = false;
