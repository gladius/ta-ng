import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Built into dist/ and served by the FastAPI app under /app (app/web/server.py). `npm run dev` serves it with
// instant reload and passes /api on to the app running on 8100.
export default defineConfig({
  base: '/app/',
  plugins: [react()],
  server: { proxy: { '/api': 'http://127.0.0.1:8100' } },
})
