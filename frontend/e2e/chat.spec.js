import { expect, test } from '@playwright/test';
import { randomUUID } from 'node:crypto';

async function seedConversation(request, count = 12) {
  let sessionId = null;
  for (let index = 0; index < count; index += 1) {
    const response = await request.post('/api/v1/chat/completions', {
      data: {
        request_id: randomUUID(),
        session_id: sessionId,
        message: { content: '/demo normal' },
      },
    });
    expect(response.status()).toBe(202);
    sessionId = (await response.json()).session_id;
    await expect.poll(async () => {
      const active = await request.get(`/api/v1/chat/sessions/${sessionId}/active-run`);
      return active.json();
    }).toBeNull();
  }
  return sessionId;
}

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

test('空会话欢迎内容在桌面与窄屏可见', async ({ page }, testInfo) => {
  await page.goto('/chat');
  const welcome = page.getByRole('heading', { name: '开始一段新对话' });
  await expect(welcome).toBeInViewport();
  await expect(page.locator('.welcome-icon')).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath('empty-desktop.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(welcome).toBeInViewport();
  await expect(page.locator('.welcome-icon')).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath('empty-mobile.png'), fullPage: true });
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
  const slowAccepted = page.waitForResponse(
    (response) => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/v1/chat/completions'
      && response.status() === 202,
  );
  await sender.fill('/demo slow');
  await sender.press('Enter');
  const slowRun = await (await slowAccepted).json();
  await expect(page.getByText(/演示慢速输出/)).toBeVisible();
  const cancelResponse = page.waitForResponse(
    (response) => response.request().method() === 'POST'
      && new URL(response.url()).pathname === `/api/v1/chat/runs/${slowRun.run_id}/cancel`,
  );
  const cancelledHistory = page.waitForResponse(
    (response) => response.request().method() === 'GET'
      && new URL(response.url()).pathname === `/api/v1/chat/sessions/${slowRun.session_id}/messages`
      && response.ok(),
  );
  await page.getByRole('button', { name: '取消当前运行' }).click();
  const response = await cancelResponse;
  expect(response.status()).toBe(202);
  expect(await response.json()).toMatchObject({
    run_id: slowRun.run_id,
    session_id: slowRun.session_id,
    response_message_id: slowRun.response_message_id,
    status: 'cancel_requested',
  });
  await expect(runPanel).toContainText('cancelled');
  await expect(runPanel).toContainText(slowRun.run_id);
  const history = await (await cancelledHistory).json();
  expect(history.items.find((item) => item.message_id === slowRun.response_message_id))
    .toMatchObject({ role: 'assistant', runtime_status: 'stopped', content: expect.any(String) });
  expect(history.items.find((item) => item.message_id === slowRun.response_message_id).content.trim())
    .not.toBe('');
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

test('旧确认提示刷新后仍可拒绝并沿用同一Run结束', async ({ page, request }) => {
  await page.goto('/chat');
  const accepted = page.waitForResponse((response) => response.url().endsWith('/api/v1/chat/completions') && response.status() === 202);
  const sender = page.getByPlaceholder('输入消息，Enter 发送，Shift+Enter 换行');
  await sender.fill('/demo interrupt');
  await sender.press('Enter');
  const run = await (await accepted).json();
  await expect(page.getByText('是否继续完成演示运行？')).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: '拒绝并继续' }).click();
  await expect(page.getByText('演示运行已按拒绝结果结束。')).toBeVisible();
  await expect(page.getByLabel('运行状态')).toContainText('completed');
  const { items } = await (await request.get(`/api/v1/chat/sessions/${run.session_id}/messages`)).json();
  expect(items.map((item) => item.role)).toEqual(['user', 'assistant']);
  expect(items[1].message_id).toBe(run.response_message_id);
});

test('多屏会话在流式结束与权威历史回读期间保留消息列表和阅读位置', async ({ page, request }, testInfo) => {
  const sessionId = await seedConversation(request);
  await page.addInitScript((id) => {
    localStorage.setItem('agent-runtime-active-session', id);
  }, sessionId);
  await page.goto('/chat');
  await expect(page.locator('.ant-bubble')).toHaveCount(24);

  let releaseHistory;
  const historyGate = new Promise((resolve) => { releaseHistory = resolve; });
  let historyPending = false;
  await page.route(`**/sessions/${sessionId}/messages?*`, async (route) => {
    const response = await route.fetch();
    historyPending = true;
    await historyGate;
    await route.fulfill({ response });
  });
  await page.evaluate(() => {
    const body = document.querySelector('.conversation-body');
    const list = body.querySelector('.message-list');
    window.scrollProbe = { detached: false, samples: [] };
    new MutationObserver(() => {
      window.scrollProbe.detached ||= !list.isConnected;
      window.scrollProbe.samples.push({
        top: body.scrollTop,
        height: body.scrollHeight,
        viewport: body.clientHeight,
        hasList: Boolean(body.querySelector('.message-list')),
      });
    }).observe(body, { childList: true, subtree: true });
  });
  const sender = page.getByPlaceholder('输入消息，Enter 发送，Shift+Enter 换行');
  await sender.fill('/demo slow');
  await sender.press('Enter');
  await expect(page.getByText(/演示慢速输出/)).toBeVisible();
  const before = await page.evaluate(() => {
    const body = document.querySelector('.conversation-body');
    const inner = body.querySelector('.ant-bubble-list-scroll-box');
    body.scrollTop = body.scrollHeight;
    return {
      outer: { top: body.scrollTop, height: body.scrollHeight, viewport: body.clientHeight },
      inner: { top: inner.scrollTop, height: inner.scrollHeight, viewport: inner.clientHeight },
    };
  });
  await expect.poll(() => historyPending).toBe(true);
  const during = await page.evaluate(() => ({
    ...window.scrollProbe,
    listCount: document.querySelectorAll('.message-list').length,
  }));
  releaseHistory();
  await expect(page.getByText('演示慢速输出：第一段，第二段，已完成。')).toHaveCount(1);
  const after = await page.evaluate(() => {
    const body = document.querySelector('.conversation-body');
    return { top: body.scrollTop, height: body.scrollHeight, viewport: body.clientHeight };
  });
  await testInfo.attach('终态滚动量测', {
    body: JSON.stringify({ before, during, after }, null, 2),
    contentType: 'application/json',
  });
  expect(Math.max(before.outer.height, before.inner.height)).toBeGreaterThan(before.outer.viewport * 3);
  expect(during.listCount).toBe(1);
  expect(during.detached).toBe(false);
  await expect(page.getByText('演示慢速输出：第一段，第二段，已完成。')).toBeInViewport();
});

test('主动阅读历史不被终态刷新打断，重新生成后仍显示最新回答', async ({ page, request }) => {
  const sessionId = await seedConversation(request);
  await page.addInitScript((id) => {
    localStorage.setItem('agent-runtime-active-session', id);
  }, sessionId);
  await page.goto('/chat');
  await expect(page.locator('.ant-bubble')).toHaveCount(24);

  let releaseHistory;
  const historyGate = new Promise((resolve) => { releaseHistory = resolve; });
  let historyPending = false;
  await page.route(`**/sessions/${sessionId}/messages?*`, async (route) => {
    const response = await route.fetch();
    historyPending = true;
    await historyGate;
    await route.fulfill({ response });
  });
  const sender = page.getByPlaceholder('输入消息，Enter 发送，Shift+Enter 换行');
  await sender.fill('/demo slow');
  await sender.press('Enter');
  await expect(page.getByText(/演示慢速输出/)).toBeVisible();
  await page.evaluate(() => {
    const scroller = document.querySelector('.ant-bubble-list-scroll-box');
    scroller.scrollTop = -scroller.scrollHeight;
    window.readingList = document.querySelector('.message-list');
  });
  const firstBubble = page.locator('.ant-bubble').first();
  await expect(firstBubble).toBeInViewport();
  const readingTop = (await firstBubble.boundingBox()).y;
  await expect.poll(() => historyPending).toBe(true);
  await expect(sender).toBeDisabled();
  await expect(firstBubble).toBeInViewport();
  releaseHistory();
  await expect(sender).toBeEnabled();
  await expect(page.locator('.ant-bubble')).toHaveCount(26);
  await expect(firstBubble).toBeInViewport();
  expect(Math.abs((await firstBubble.boundingBox()).y - readingTop)).toBeLessThan(2);
  expect(await page.evaluate(() => window.readingList.isConnected)).toBe(true);

  await page.getByRole('button', { name: '重新生成最新回答' }).click();
  await expect(page.getByLabel('运行状态')).toContainText('running');
  await expect(sender).toBeEnabled();
  await expect(page.locator('.ant-bubble')).toHaveCount(26);
  await expect(page.getByText('演示慢速输出：第一段，第二段，已完成。')).toBeInViewport();
  expect(await page.evaluate(() => window.readingList.isConnected)).toBe(true);
});

test('加载更早消息保持阅读锚点，切换会话保留初次加载状态', async ({ page, request }) => {
  const sessionId = await seedConversation(request);
  const otherSessionId = await seedConversation(request, 1);
  const renamed = await request.patch(`/api/v1/chat/sessions/${otherSessionId}/rename`, {
    data: { title: '切换目标会话' },
  });
  expect(renamed.ok()).toBeTruthy();
  await page.addInitScript((id) => {
    localStorage.setItem('agent-runtime-active-session', id);
  }, sessionId);
  await page.route(`**/sessions/${sessionId}/messages?*`, async (route) => {
    const url = new URL(route.request().url());
    url.searchParams.set('limit', '12');
    const response = await route.fetch({ url: url.toString() });
    await route.fulfill({ response });
  });
  await page.goto('/chat');
  await expect(page.locator('.ant-bubble')).toHaveCount(12);
  const lastBubble = page.locator('.ant-bubble').last();
  await expect(lastBubble).toBeInViewport();
  const before = (await lastBubble.boundingBox()).y;
  await page.getByRole('button', { name: '加载更早消息' }).click();
  await expect(page.locator('.ant-bubble')).toHaveCount(24);
  await expect(lastBubble).toBeInViewport();
  expect(Math.abs((await lastBubble.boundingBox()).y - before)).toBeLessThan(2);

  let releaseSwitch;
  const switchGate = new Promise((resolve) => { releaseSwitch = resolve; });
  let switching = false;
  await page.route(`**/sessions/${otherSessionId}/active-run`, async (route) => {
    const response = await route.fetch();
    switching = true;
    await switchGate;
    await route.fulfill({ response });
  });
  await page.getByLabel('会话列表').getByText('切换目标会话').click();
  await expect.poll(() => switching).toBe(true);
  await expect(page.locator('.centered-state')).toBeVisible();
  await expect(page.locator('.message-list')).toHaveCount(0);
  await expect(page.getByPlaceholder('输入消息，Enter 发送，Shift+Enter 换行')).toBeDisabled();
  releaseSwitch();
  await expect(page.locator('.ant-bubble')).toHaveCount(2);
  await expect(page.locator('.centered-state')).toHaveCount(0);
  await expect(page.locator('.chat-header')).toContainText('切换目标会话');
});
