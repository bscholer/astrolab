import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [sveltekit()],
  server: {
    port: 5173,
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
