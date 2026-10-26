# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: chat.spec.js >> 真实浏览器完成 S2.5 Run、刷新恢复、Resume、失败和 Cancel
- Location: e2e\chat.spec.js:55:1

# Error details

```
Error: expect(locator).toContainText(expected) failed

Locator: getByLabel('运行状态')
Expected substring: "cancel_requested"
Received string:    "运行状态cancelledattempt 1Run ID2c6f7852-736f-4454-a723-24c4aadc2778Session ID2aeb138d-88ad-4bcc-9428-a99a9e78acf0消息 IDc572d25b-97a0-4ef0-900c-b2a0949a6347Capability—恢复次数0"
Timeout: 5000ms

Call log:
  - Expect "toContainText" getByLabel('运行状态') with timeout 5000ms
  - waiting for getByLabel('运行状态')
    5 × locator resolved to <section aria-label="运行状态" class="run-status-panel">…</section>
      - unexpected value "运行状态runningattempt 1Run ID2c6f7852-736f-4454-a723-24c4aadc2778Session ID2aeb138d-88ad-4bcc-9428-a99a9e78acf0消息 IDc572d25b-97a0-4ef0-900c-b2a0949a6347Capability—恢复次数0"
    9 × locator resolved to <section aria-label="运行状态" class="run-status-panel">…</section>
      - unexpected value "运行状态cancelledattempt 1Run ID2c6f7852-736f-4454-a723-24c4aadc2778Session ID2aeb138d-88ad-4bcc-9428-a99a9e78acf0消息 IDc572d25b-97a0-4ef0-900c-b2a0949a6347Capability—恢复次数0"

```

```yaml
- region "运行状态":
  - button "运行状态 cancelled attempt 1" [expanded]
  - table:
    - rowgroup:
      - 'row "Run ID : 2c6f7852-736f-4454-a723-24c4aadc2778 Session ID : 2aeb138d-88ad-4bcc-9428-a99a9e78acf0 消息 ID : c572d25b-97a0-4ef0-900c-b2a0949a6347 Capability : — 恢复次数 : 0"':
        - 'cell "Run ID : 2c6f7852-736f-4454-a723-24c4aadc2778"':
          - text: "Run ID :"
          - code: 2c6f7852-736f-4454-a723-24c4aadc2778
        - 'cell "Session ID : 2aeb138d-88ad-4bcc-9428-a99a9e78acf0"':
          - text: "Session ID :"
          - code: 2aeb138d-88ad-4bcc-9428-a99a9e78acf0
        - 'cell "消息 ID : c572d25b-97a0-4ef0-900c-b2a0949a6347"':
          - text: "消息 ID :"
          - code: c572d25b-97a0-4ef0-900c-b2a0949a6347
        - 'cell "Capability : —"':
          - text: "Capability :"
          - code: —
        - 'cell "恢复次数 : 0"':
          - text: "恢复次数 :"
          - code: "0"
```

# Test source

```ts
  1   | import { expect, test } from '@playwright/test';
  2   | import { randomUUID } from 'node:crypto';
  3   | 
  4   | async function seedConversation(request, count = 12) {
  5   |   let sessionId = null;
  6   |   for (let index = 0; index < count; index += 1) {
  7   |     const response = await request.post('/api/v1/chat/completions', {
  8   |       data: {
  9   |         request_id: randomUUID(),
  10  |         session_id: sessionId,
  11  |         message: { content: '/demo normal' },
  12  |       },
  13  |     });
  14  |     expect(response.status()).toBe(202);
  15  |     sessionId = (await response.json()).session_id;
  16  |     await expect.poll(async () => {
  17  |       const active = await request.get(`/api/v1/chat/sessions/${sessionId}/active-run`);
  18  |       return active.json();
  19  |     }).toBeNull();
  20  |   }
  21  |   return sessionId;
  22  | }
  23  | 
  24  | test.afterEach(async ({ request }) => {
  25  |   while (true) {
  26  |     const response = await request.get('/api/v1/chat/sessions?limit=100');
  27  |     if (!response.ok()) {
  28  |       throw new Error(`浏览器验收清理 Session 失败：HTTP ${response.status()}`);
  29  |     }
  30  |     const payload = await response.json();
  31  |     if (payload.items.length === 0) return;
  32  |     for (const session of payload.items) {
  33  |       const deleted = await request.delete(
  34  |         `/api/v1/chat/sessions/${session.session_id}`,
  35  |       );
  36  |       if (!deleted.ok()) {
  37  |         throw new Error(`浏览器验收删除 Session 失败：HTTP ${deleted.status()}`);
  38  |       }
  39  |     }
  40  |   }
  41  | });
  42  | 
  43  | test('空会话欢迎内容在桌面与窄屏可见', async ({ page }, testInfo) => {
  44  |   await page.goto('/chat');
  45  |   const welcome = page.getByRole('heading', { name: '开始一段新对话' });
  46  |   await expect(welcome).toBeInViewport();
  47  |   await expect(page.locator('.welcome-icon')).toBeInViewport();
  48  |   await page.screenshot({ path: testInfo.outputPath('empty-desktop.png') });
  49  |   await page.setViewportSize({ width: 390, height: 844 });
  50  |   await expect(welcome).toBeInViewport();
  51  |   await expect(page.locator('.welcome-icon')).toBeInViewport();
  52  |   await page.screenshot({ path: testInfo.outputPath('empty-mobile.png'), fullPage: true });
  53  | });
  54  | 
  55  | test('真实浏览器完成 S2.5 Run、刷新恢复、Resume、失败和 Cancel', async ({ page }) => {
  56  |   await page.goto('/chat');
  57  |   const sender = page.getByPlaceholder('输入消息，Enter 发送，Shift+Enter 换行');
  58  |   const runPanel = page.getByLabel('运行状态');
  59  | 
  60  |   const accepted = page.waitForResponse(
  61  |     (response) => response.url().endsWith('/api/v1/chat/completions') && response.status() === 202,
  62  |   );
  63  |   await sender.fill('/demo normal');
  64  |   await sender.press('Enter');
  65  |   await accepted;
  66  |   await expect(page.getByText('演示运行已正常完成。')).toBeVisible();
  67  |   await expect(runPanel).toContainText('completed');
  68  |   await expect(runPanel).toContainText('Run ID');
  69  | 
  70  |   await sender.fill('/demo interrupt');
  71  |   await sender.press('Enter');
  72  |   await expect(runPanel).toContainText('interrupted');
  73  |   await expect(page.getByText('是否继续完成演示运行？')).toBeVisible();
  74  | 
  75  |   await page.reload();
  76  |   await expect(page.getByText('是否继续完成演示运行？')).toBeVisible();
  77  |   await expect(runPanel).toContainText('interrupted');
  78  |   await page.getByRole('button', { name: '确认并继续' }).click();
  79  |   await expect(page.getByText('演示运行已在确认后继续完成。')).toBeVisible();
  80  |   await expect(runPanel).toContainText('completed');
  81  | 
  82  |   await sender.fill('/demo fail');
  83  |   await sender.press('Enter');
  84  |   await expect(runPanel).toContainText('failed');
  85  |   await expect(page.getByText('抱歉，本次回复未能完成。')).toBeVisible();
  86  | 
  87  |   await page.getByRole('button', { name: /新建会话/ }).click();
  88  |   await sender.fill('/demo slow');
  89  |   await sender.press('Enter');
  90  |   await expect(page.getByText(/演示慢速输出/)).toBeVisible();
  91  |   await page.getByRole('button', { name: '取消当前运行' }).click();
> 92  |   await expect(runPanel).toContainText('cancel_requested');
      |                          ^ Error: expect(locator).toContainText(expected) failed
  93  |   await expect(runPanel).toContainText('cancelled');
  94  |   await expect(page.getByText(/已停止/)).toBeVisible();
  95  | });
  96  | 
  97  | test('HTTP 202 延迟期间重复提交只创建一个 Session 和 Run', async ({ page, request }) => {
  98  |   let completionRequests = 0;
  99  |   await page.route('**/api/v1/chat/completions', async (route) => {
  100 |     completionRequests += 1;
  101 |     const response = await route.fetch();
  102 |     await new Promise((resolve) => setTimeout(resolve, 800));
  103 |     await route.fulfill({ response });
  104 |   });
  105 | 
  106 |   await page.goto('/chat');
  107 |   const sender = page.getByPlaceholder('输入消息，Enter 发送，Shift+Enter 换行');
  108 |   await sender.fill('/demo normal');
  109 |   await sender.press('Enter');
  110 |   await expect.poll(() => completionRequests).toBe(1);
  111 |   const submittingSender = page.getByPlaceholder('正在提交运行…');
  112 |   await expect(submittingSender).toBeDisabled();
  113 | 
  114 |   await page.keyboard.type('/demo normal');
  115 |   await page.keyboard.press('Enter');
  116 |   await page.waitForTimeout(100);
  117 |   expect(completionRequests).toBe(1);
  118 | 
  119 |   await expect(page.getByText('演示运行已正常完成。')).toBeVisible();
  120 |   const sessionsResponse = await request.get('/api/v1/chat/sessions?limit=100');
  121 |   expect(sessionsResponse.ok()).toBeTruthy();
  122 |   const sessions = await sessionsResponse.json();
  123 |   expect(sessions.items).toHaveLength(1);
  124 | 
  125 |   const messagesResponse = await request.get(
  126 |     `/api/v1/chat/sessions/${sessions.items[0].session_id}/messages?limit=100`,
  127 |   );
  128 |   expect(messagesResponse.ok()).toBeTruthy();
  129 |   const messages = await messagesResponse.json();
  130 |   expect(messages.items.filter((item) => item.role === 'user')).toHaveLength(1);
  131 |   expect(completionRequests).toBe(1);
  132 | });
  133 | 
  134 | test('多屏会话在流式结束与权威历史回读期间保留消息列表和阅读位置', async ({ page, request }, testInfo) => {
  135 |   const sessionId = await seedConversation(request);
  136 |   await page.addInitScript((id) => {
  137 |     localStorage.setItem('agent-runtime-active-session', id);
  138 |   }, sessionId);
  139 |   await page.goto('/chat');
  140 |   await expect(page.locator('.ant-bubble')).toHaveCount(24);
  141 | 
  142 |   let releaseHistory;
  143 |   const historyGate = new Promise((resolve) => { releaseHistory = resolve; });
  144 |   let historyPending = false;
  145 |   await page.route(`**/sessions/${sessionId}/messages?*`, async (route) => {
  146 |     const response = await route.fetch();
  147 |     historyPending = true;
  148 |     await historyGate;
  149 |     await route.fulfill({ response });
  150 |   });
  151 |   await page.evaluate(() => {
  152 |     const body = document.querySelector('.conversation-body');
  153 |     const list = body.querySelector('.message-list');
  154 |     window.scrollProbe = { detached: false, samples: [] };
  155 |     new MutationObserver(() => {
  156 |       window.scrollProbe.detached ||= !list.isConnected;
  157 |       window.scrollProbe.samples.push({
  158 |         top: body.scrollTop,
  159 |         height: body.scrollHeight,
  160 |         viewport: body.clientHeight,
  161 |         hasList: Boolean(body.querySelector('.message-list')),
  162 |       });
  163 |     }).observe(body, { childList: true, subtree: true });
  164 |   });
  165 |   const sender = page.getByPlaceholder('输入消息，Enter 发送，Shift+Enter 换行');
  166 |   await sender.fill('/demo slow');
  167 |   await sender.press('Enter');
  168 |   await expect(page.getByText(/演示慢速输出/)).toBeVisible();
  169 |   const before = await page.evaluate(() => {
  170 |     const body = document.querySelector('.conversation-body');
  171 |     const inner = body.querySelector('.ant-bubble-list-scroll-box');
  172 |     body.scrollTop = body.scrollHeight;
  173 |     return {
  174 |       outer: { top: body.scrollTop, height: body.scrollHeight, viewport: body.clientHeight },
  175 |       inner: { top: inner.scrollTop, height: inner.scrollHeight, viewport: inner.clientHeight },
  176 |     };
  177 |   });
  178 |   await expect.poll(() => historyPending).toBe(true);
  179 |   const during = await page.evaluate(() => ({
  180 |     ...window.scrollProbe,
  181 |     listCount: document.querySelectorAll('.message-list').length,
  182 |   }));
  183 |   releaseHistory();
  184 |   await expect(page.getByText('演示慢速输出：第一段，第二段，已完成。')).toHaveCount(1);
  185 |   const after = await page.evaluate(() => {
  186 |     const body = document.querySelector('.conversation-body');
  187 |     return { top: body.scrollTop, height: body.scrollHeight, viewport: body.clientHeight };
  188 |   });
  189 |   await testInfo.attach('终态滚动量测', {
  190 |     body: JSON.stringify({ before, during, after }, null, 2),
  191 |     contentType: 'application/json',
  192 |   });
```