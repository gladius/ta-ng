import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Built into dist/ and served under PATH_PREFIX + /app (no prefix by default): by nginx in the image, by the local FastAPI app otherwise. `npm run dev` serves it with
// instant reload and passes /api on to the app running on 8100.
export default defineConfig({
  base: (process.env.PATH_PREFIX ?? '') + '/app/',
  plugins: [react()],
  server: { proxy: { '/api': 'http://127.0.0.1:8100' } },
})
