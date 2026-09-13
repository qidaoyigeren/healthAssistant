import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// 开发代理:/v1 → 本地 FastAPI 单写者服务(stage0.server:app,默认 127.0.0.1:8000)。
// 生产部署通过同源反向代理转发 /v1(SPA 深链接回退到 index.html)。
export default defineConfig(({ mode }) => ({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/v1': {
        // 进程环境变量优先于 .env 文件：验收脚本要让代理指向它自己起的那份隔离后端。
        target: process.env.STAGE0_DEV_API_TARGET
          || loadEnv(mode, '.', '').STAGE0_DEV_API_TARGET
          || 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
}));
