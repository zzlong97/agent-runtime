import { expect, test } from '@playwright/test';

test.afterEach(async ({ request }) => {
  while (true) {
    const response = await request.get('/api/v1/chat/sessions?limit=100');
    if (!response.ok()) {
      throw new Error(`浏览器验收清理 Session 失败：HTTP ${response.status()}`);
    }
    const payload = await response.json();
    if (payload.items.length === 0) return;
    for (const session of payload.items) {
      const deleted = await request.delete(
        `/api/v1/chat/sessions/${session.session_id}`,
      );
      if (!deleted.ok()) {
        throw new Error(`浏览器验收删除 Session 失败：HTTP ${deleted.status()}`);
      }
    }
  }
});

test('真实浏览器完成 S2.5 Run、刷新恢复、Resume、失败和 Cancel', async ({ page }) => {
  await page.goto('/chat');
  const sender = page.getByPlaceholder('输入消息，Enter 发送，Shift+Enter 换行');
  const runPanel = page.getByLabel('运行状态');

  const accepted = page.waitForResponse(
    (response) => response.url().endsWith('/api/v1/chat/completions') && response.status() === 202,
  );
  await sender.fill('/demo normal');
  await sender.press('Enter');
  await accepted;
  await expect(page.getByText('演示运行已正常完成。')).toBeVisible();
  await expect(runPanel).toContainText('completed');
  await expect(runPanel).toContainText('Run ID');

  await sender.fill('/demo interrupt');
  await sender.press('Enter');
  await expect(runPanel).toContainText('interrupted');
  await expect(page.getByText('是否继续完成演示运行？')).toBeVisible();

  await page.reload();
  await expect(page.getByText('是否继续完成演示运行？')).toBeVisible();
  await expect(runPanel).toContainText('interrupted');
  await page.getByRole('button', { name: '确认并继续' }).click();
  await expect(page.getByText('演示运行已在确认后继续完成。')).toBeVisible();
  await expect(runPanel).toContainText('completed');

  await sender.fill('/demo fail');
  await sender.press('Enter');
  await expect(runPanel).toContainText('failed');
  await expect(page.getByText('抱歉，本次回复未能完成。')).toBeVisible();

  await page.getByRole('button', { name: /新建会话/ }).click();
  await sender.fill('/demo slow');
  await sender.press('Enter');
  await expect(page.getByText(/演示慢速输出/)).toBeVisible();
  await page.getByRole('button', { name: '取消当前运行' }).click();
  await expect(runPanel).toContainText('cancel_requested');
  await expect(runPanel).toContainText('cancelled');
  await expect(page.getByText(/已停止/)).toBeVisible();
});

test('HTTP 202 延迟期间重复提交只创建一个 Session 和 Run', async ({ page, request }) => {
  let completionRequests = 0;
  await page.route('**/api/v1/chat/completions', async (route) => {
    completionRequests += 1;
    const response = await route.fetch();
    await new Promise((resolve) => setTimeout(resolve, 800));
    await route.fulfill({ response });
  });

  await page.goto('/chat');
  const sender = page.getByPlaceholder('输入消息，Enter 发送，Shift+Enter 换行');
  await sender.fill('/demo normal');
  await sender.press('Enter');
  await expect.poll(() => completionRequests).toBe(1);
  const submittingSender = page.getByPlaceholder('正在提交运行…');
  await expect(submittingSender).toBeDisabled();

  await page.keyboard.type('/demo normal');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(100);
  expect(completionRequests).toBe(1);

  await expect(page.getByText('演示运行已正常完成。')).toBeVisible();
  const sessionsResponse = await request.get('/api/v1/chat/sessions?limit=100');
  expect(sessionsResponse.ok()).toBeTruthy();
  const sessions = await sessionsResponse.json();
  expect(sessions.items).toHaveLength(1);

  const messagesResponse = await request.get(
    `/api/v1/chat/sessions/${sessions.items[0].session_id}/messages?limit=100`,
  );
  expect(messagesResponse.ok()).toBeTruthy();
  const messages = await messagesResponse.json();
  expect(messages.items.filter((item) => item.role === 'user')).toHaveLength(1);
  expect(completionRequests).toBe(1);
});
