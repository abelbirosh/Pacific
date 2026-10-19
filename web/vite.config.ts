import react from '@vitejs/plugin-react'
import { defineConfig, type Plugin } from 'vite'

// Link previewers only follow absolute image URLs, so on Vercel the share image gets the production origin.
function absoluteShareImage(): Plugin {
  const host = process.env.VERCEL_PROJECT_PRODUCTION_URL
  return {
    name: 'absolute-share-image',
    transformIndexHtml: (html) => (host ? html.replaceAll('content="/assets/', `content="https://${host}/assets/`) : html),
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), absoluteShareImage()],
})
