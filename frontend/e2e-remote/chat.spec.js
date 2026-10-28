import { expect, test } from '@playwright/test';

async function start(page, content) {
  const accepted = page.waitForResponse((response) => response.url().endsWith('/api/v1/chat/completions') && response.status() === 202);
  const sender = page.getByPlaceholder('输入消息，Enter 发送，Shift+Enter 换行');
  await sender.fill(content);
  await sender.press('Enter');
  return (await accepted).json();
}

test.afterEach(async ({ request }) => {
  const { items } = await (await request.get('/api/v1/chat/sessions?limit=100')).json();
  for (const session of items) {
    expect((await request.delete(`/api/v1/chat/sessions/${session.session_id}`)).ok()).toBeTruthy();
  }
});

test('正式远程Gateway完成普通文本并只保留唯一公共回答', async ({ page, request }) => {
  await page.goto('/chat');
  const run = await start(page, '/remote normal');
  await expect(page.getByText('第一段第二段')).toBeVisible();
  await expect(page.getByLabel('运行状态')).toContainText('completed');
  const { items } = await (await request.get(`/api/v1/chat/sessions/${run.session_id}/messages`)).json();
  expect(items.map((item) => item.role)).toEqual(['user', 'assistant']);
  expect(items[1].message_id).toBe(run.response_message_id);
});

test('刷新恢复两轮文本补参、空白阻止、双击单提交和同请求失败重试', async ({ page, request }, testInfo) => {
  await page.goto('/chat');
  const run = await start(page, '/remote text');
  await expect(page.getByText('请补充日期')).toBeVisible();
  await page.reload();
  await expect(page.getByText('请补充日期')).toBeVisible();
  const resumes = [];
  let failFirst = true;
  await page.route('**/runs/*/resume', async (route) => {
    resumes.push(route.request().postDataJSON());
    if (failFirst) {
      failFirst = false;
      await route.abort('failed');
    } else {
      await route.continue();
    }
  });
  const answer = page.getByRole('textbox', { name: '补充文本' });
  const submit = page.getByRole('button', { name: '提交补充并继续' });
  await answer.fill('   ');
  await expect(submit).toBeDisabled();
  await answer.fill('2026-10-01 至 2026-10-20');
  await submit.click();
  await expect(page.getByText('补充提交失败，输入已保留，请重试。')).toBeVisible();
  await expect(submit).toBeEnabled();
  await expect(answer).toHaveValue('2026-10-01 至 2026-10-20');
  await submit.dblclick();
  await expect(page.getByText('请补充范围')).toBeVisible();
  expect(resumes).toHaveLength(2);
  expect(resumes[1]).toEqual(resumes[0]);
  await expect(answer).toHaveValue('');
  await page.screenshot({ path: testInfo.outputPath('second-text-interrupt.png'), fullPage: true });
  await page.reload();
  await expect(page.getByText('请补充范围')).toBeVisible();
  await answer.fill('上海区域');
  await submit.click();
  await expect(page.getByText('参数已采用：2026-10-01 至 2026-10-20；上海区域')).toBeVisible();
  await expect(page.getByLabel('运行状态')).toContainText('completed');
  const { items } = await (await request.get(`/api/v1/chat/sessions/${run.session_id}/messages`)).json();
  expect(items.map((item) => item.role)).toEqual(['user', 'assistant']);
  expect(items[1].message_id).toBe(run.response_message_id);
  expect(items[1].content).toBe('参数已采用：2026-10-01 至 2026-10-20；上海区域');
  const events = await (await request.get(`/api/v1/chat/runs/${run.run_id}/events`)).text();
  expect(events.match(/event: run.completed/g)).toHaveLength(1);
  expect(events.match(/event: interrupt.required/g)).toHaveLength(2);
  expect(events).not.toContain('remote_context_id');
  expect(events).not.toContain('answer');
});

test('待补参时取消沿用原Run，拒绝随后Resume且只有取消终态', async ({ page, request }) => {
  await page.goto('/chat');
  const run = await start(page, '/remote cancel');
  await expect(page.getByText('请补充日期')).toBeVisible();
  const pending = (await (await request.get(`/api/v1/chat/sessions/${run.session_id}/active-run`)).json()).pending_interrupt;
  let releaseResume;
  let notifyStarted;
  const started = new Promise((resolve) => { notifyStarted = resolve; });
  await page.route('**/runs/*/resume', async (route) => {
    notifyStarted();
    await new Promise((resolve) => { releaseResume = resolve; });
    await route.continue();
  });
  await page.getByRole('textbox', { name: '补充文本' }).fill('并行取消答案');
  await page.getByRole('button', { name: '提交补充并继续' }).click();
  await started;
  await page.getByRole('button', { name: '取消当前运行' }).click();
  await expect(page.getByLabel('运行状态')).toContainText('cancelled');
  await expect(page.getByText('已停止本次回复。')).toBeVisible();
  releaseResume();
  const invalid = await request.post(`/api/v1/chat/runs/${run.run_id}/resume`, { data: {
    interrupt_id: pending.interrupt_id, request_id: crypto.randomUUID(), resume_payload: { answer: '取消后答案' },
  } });
  expect(invalid.status()).toBe(409);
  const { items } = await (await request.get(`/api/v1/chat/sessions/${run.session_id}/messages`)).json();
  expect(items.map((item) => item.role)).toEqual(['user', 'assistant']);
  expect(items[1].runtime_status).toBe('stopped');
  const events = await (await request.get(`/api/v1/chat/runs/${run.run_id}/events`)).text();
  expect(events.match(/event: run.cancelled/g)).toHaveLength(1);
  expect(events).not.toContain('event: run.completed');
});

test('Resume先被接受后页面Cancel仍停止原Run并只保留取消消息', async ({ page, request }) => {
  await page.goto('/chat');
  const run = await start(page, '/remote resume-cancel');
  await expect(page.getByText('请补充日期')).toBeVisible();
  const active = await (await request.get(`/api/v1/chat/sessions/${run.session_id}/active-run`)).json();
  const accepted = await request.post(`/api/v1/chat/runs/${run.run_id}/resume`, { data: {
    interrupt_id: active.pending_interrupt.interrupt_id,
    request_id: crypto.randomUUID(), resume_payload: { answer: '已受理参数' },
  } });
  expect(accepted.status()).toBe(202);
  expect((await accepted.json()).run_id).toBe(run.run_id);
  await page.getByRole('button', { name: '取消当前运行' }).click();
  await expect(page.getByLabel('运行状态')).toContainText('cancelled');
  await expect(page.getByText('已停止本次回复。')).toBeVisible();
  const { items } = await (await request.get(`/api/v1/chat/sessions/${run.session_id}/messages`)).json();
  expect(items.map((item) => item.role)).toEqual(['user', 'assistant']);
  expect(items[1].message_id).toBe(run.response_message_id);
  expect(items[1].runtime_status).toBe('stopped');
});
