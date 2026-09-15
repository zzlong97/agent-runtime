import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  base: '/',
  resolve: {
    alias:
      mode === 'test'
        ? [
            {
              find: /^@ant-design\/x$/,
              replacement: fileURLToPath(
                new URL('./src/test/antDesignX.js', import.meta.url),
              ),
            },
          ]
        : [],
  },
  build: {
    outDir: '../src/agent_runtime/static',
    emptyOutDir: true,
    assetsDir: 'assets',
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            {
              name: 'vendor',
              test: /node_modules[\\/]/,
              minSize: 100_000,
              maxSize: 400_000,
              priority: 10,
            },
          ],
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.js',
    css: true,
  },
}));
