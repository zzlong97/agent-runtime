import { defineConfig, devices } from '@playwright/test';

// 独立Runtime由执行者管理；浏览器只观察本批既有Session，不创建额外Task。
export default defineConfig({
  testDir: './e2e-real-remote', workers: 1, fullyParallel: false, retries: 0,
  reporter: 'list', timeout: 30000,
  outputDir: '../.dev-team/tasks/RA-INT-01/evidence/browser',
  use: { baseURL: 'http://127.0.0.1:18781', trace: 'off' },
  projects: [{ name: 'edge', use: { ...devices['Desktop Chrome'], channel: 'msedge' } }],
});
