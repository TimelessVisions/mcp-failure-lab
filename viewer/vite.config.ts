/// <reference types="vitest/config" />
import { fileURLToPath, URL } from 'node:url';

import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

/**
 * The viewer is served from a static file server with no network.
 *
 * `base: './'` keeps every emitted URL relative, so the bundle works from any
 * subdirectory without being rebuilt for it. Nothing is fetched from a CDN and
 * nothing is code-split across a network boundary: a page that needed a second
 * request to render its evidence would be a page that fails in exactly the
 * environment this is built for.
 */
export default defineConfig({
  base: './',
  plugins: [react()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // Source maps would carry the module text into the published bundle, and
    // the privacy scan runs over the built output. Off in every mode.
    sourcemap: false,
    target: 'es2022',
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/**/*.test.ts', 'tests/**/*.test.tsx'],
    css: true,
  },
});
