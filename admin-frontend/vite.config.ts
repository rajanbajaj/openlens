import { defineConfig } from 'vite';
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';

// https://vite.dev/config/
export default defineConfig({
  plugins: [tailwindcss(), react()],
  server: {
    // Port 5174 so the admin app runs alongside the user frontend
    // (5173) against the same backend. Generated API client uses
    // relative URLs; proxy /api to the backend in dev so it behaves
    // like the same-origin prod build.
    port: 5174,
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
  },
});
