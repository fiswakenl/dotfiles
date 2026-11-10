import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

// /api — к server.py рядом
export default defineConfig({ plugins: [svelte()], server: { port: 5191, proxy: { '/api': 'http://127.0.0.1:8791' } } })
