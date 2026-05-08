import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [sveltekit()],
  server: {
    port: 5173,
    // Allow the externally-resolvable hostname through Vite's host check.
    // Vite blocks unknown Host headers by default as a DNS-rebinding guard;
    // this list opts in the names we actually serve to.
    allowedHosts: ['astrolab.benscholer.com'],
    // Talk to the FastAPI control plane during dev. In prod the static UI
    // gets mounted under the same origin and the proxy is not needed.
    // ws: true forwards WebSocket upgrades (needed for /api/jobs/{id}/events).
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        ws: true
      }
    }
  }
});
