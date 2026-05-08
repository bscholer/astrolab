// Dynamic per-job route: no static project. The SPA fallback in the static
// adapter (configured in svelte.config.js) serves index.html for any unknown
// path, and the client-side router takes over.
export const prerender = false;
export const ssr = false;
