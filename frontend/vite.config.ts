import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// dev 时把 /api 代理到本地 popper serve（默认 8765）
export default defineConfig({
  plugins: [react()],
  // Compression complements feature pruning; warnings remain enabled.
  build: { minify: 'terser', terserOptions: { compress: { passes: 2 } } },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
        configure(proxy) {
          proxy.on('proxyReq', (outgoing, incoming) => {
            // Only translate this dev server's own Origin. Foreign origins must
            // remain unchanged so the backend's CSRF check still rejects them.
            const origin = incoming.headers.origin
            if (origin === 'http://127.0.0.1:5173' || origin === 'http://localhost:5173') {
              outgoing.setHeader('Origin', 'http://127.0.0.1:8765')
            }
          })
        }
      }
    }
  }
})
