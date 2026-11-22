import { defineConfig } from 'vite';

export default defineConfig({
  server: {
    host: 'localhost',
    port: 3000,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
});
