// Detail page is fully client-rendered: data loads on mount via fetch, no
// server-side prerender needed. This file just exists to keep SvelteKit
// from prerendering and complaining about a dynamic [id] route.
export const ssr = false;
export const prerender = false;
