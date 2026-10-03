import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

const frontend = fileURLToPath(new URL('./frontend', import.meta.url));

export default defineConfig({
  root: frontend,
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    fs: { strict: true, allow: [frontend] },
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
        configure(proxy) {
          proxy.on('proxyReq', (outgoing, incoming) => {
            if (['http://127.0.0.1:5173', 'http://localhost:5173'].includes(incoming.headers.origin ?? '')) {
              outgoing.setHeader('Origin', 'http://127.0.0.1:8765');
            }
          });
        },
      },
    },
  },
  build: { outDir: 'dist', sourcemap: false },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    clearMocks: true,
  },
});
