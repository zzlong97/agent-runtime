import { defineConfig, devices } from '@playwright/test';
import { randomUUID } from 'node:crypto';

const browserChannel = process.env.PLAYWRIGHT_BROWSER_CHANNEL;
const e2ePort = Number.parseInt(
  process.env.PLAYWRIGHT_E2E_PORT ?? String(10_000 + (process.pid % 50_000)),
  10,
);
if (!Number.isInteger(e2ePort) || e2ePort < 1024 || e2ePort > 65_535) {
  throw new Error('PLAYWRIGHT_E2E_PORT 必须是 1024 至 65535 之间的整数');
}
const e2eRunId = process.env.PLAYWRIGHT_E2E_RUN_ID ?? randomUUID();
const e2eBaseUrl = `http://127.0.0.1:${e2ePort}`;
process.env.PLAYWRIGHT_E2E_PORT = String(e2ePort);
process.env.PLAYWRIGHT_E2E_RUN_ID = e2eRunId;

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  reporter: 'list',
  use: {
    baseURL: e2eBaseUrl,
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'uv run python tests/e2e/server.py',
    cwd: '..',
    url: `${e2eBaseUrl}/health`,
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      ...process.env,
      RUN_POSTGRES_TESTS: '1',
      PLAYWRIGHT_E2E_PORT: String(e2ePort),
      PLAYWRIGHT_E2E_RUN_ID: e2eRunId,
    },
  },
  projects: [
    {
      name: 'browser',
      use: {
        ...devices['Desktop Chrome'],
        ...(browserChannel ? { channel: browserChannel } : {}),
      },
    },
  ],
});
