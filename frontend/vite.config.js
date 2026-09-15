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
  },
  test: {
    include: ['src/**/*.test.{js,jsx}'],
    environment: 'jsdom',
    setupFiles: './src/test/setup.js',
    css: true,
  },
}));
