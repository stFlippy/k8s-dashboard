import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';
import { authMiddleware } from './src/auth.mjs';

const webRoot = fileURLToPath(new URL('./web/', import.meta.url));

export default defineConfig({
  root: webRoot,
  server: {
    host: '127.0.0.1',
    port: 4175,
    strictPort: true,
    fs: { strict: true, allow: [webRoot] }
  },
  plugins: [{
    name: 'quaber-auth-mock',
    configureServer(server) { server.middlewares.use(authMiddleware); }
  }]
});
