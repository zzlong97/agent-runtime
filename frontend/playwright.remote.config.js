import { defineConfig } from '@playwright/test';
import base from './playwright.config.js';

export default defineConfig({
  ...base,
  testDir: './e2e-remote',
  outputDir: '../.dev-team/tasks/RA-DEV-03/evidence/browser',
  webServer: {
    ...base.webServer,
    command: 'uv run python -m tests.e2e.remote_server',
  },
});
