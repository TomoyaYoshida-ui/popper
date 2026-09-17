import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// dev 时把 /api 代理到本地 popper serve（默认 8765）
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true
      }
    }
  }
})