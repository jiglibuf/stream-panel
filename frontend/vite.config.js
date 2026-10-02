import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Пульт целиком живёт на одном адресе с сервисом: base «/», сборка кладётся в dist
// (именно её отдаёт aiohttp). В режиме разработки запросы API и WebSocket
// проксируются на сервис, чтобы не настраивать CORS.
export default defineConfig({
  base: '/',
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    host: true,
    port: 5173,
  },
})
