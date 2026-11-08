import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'AVALOKITA_');
  const target = env.AVALOKITA_API || process.env.AVALOKITA_API;
  return {
    plugins: [react()],
    server: { host: '127.0.0.1', proxy: target ? {
      '/api': { target, changeOrigin: true, configure(proxy) {
        proxy.on('proxyReq', req => { req.setHeader('Origin', new URL(target).origin); req.setHeader('Sec-Fetch-Site', 'same-origin'); });
      } },
    } : undefined },
    build: { target: 'es2022' },
  };
});
