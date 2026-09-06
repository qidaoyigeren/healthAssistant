import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// 开发代理:/v1 → 本地 FastAPI 单写者服务(stage0.server:app,默认 127.0.0.1:8000)。
// 生产部署通过同源反向代理转发 /v1(SPA 深链接回退到 index.html)。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/v1': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
});
