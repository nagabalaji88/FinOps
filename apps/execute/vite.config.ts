import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

const shared = path.resolve(__dirname, '../../packages/shared')

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@finops/shared/styles.css': path.resolve(shared, 'src/styles/index.css'),
      '@finops/shared': path.resolve(shared, 'src/index.ts'),
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5174,
    proxy: {
      '/api': { target: process.env.VITE_API_TARGET ?? 'http://localhost:8000', changeOrigin: true },
      '/health': { target: process.env.VITE_API_TARGET ?? 'http://localhost:8000', changeOrigin: true },
    },
  },
  preview: { port: 4174 },
  build: {
    outDir: 'dist',
    sourcemap: true,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          charts: ['recharts'],
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: path.resolve(shared, 'src/test-setup.ts'),
  },
})
