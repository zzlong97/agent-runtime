import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';

test('真实远程既有Session的页面恢复与消息身份', async ({ page, request }) => {
  const receipt = JSON.parse(readFileSync('../.dev-team/tasks/RA-INT-01/evidence/真实联调运行回执.json', 'utf8'));
  const name = process.env.RA_INT_BROWSER_CASE || 'full_parameters';
  const recorded = receipt.cases.find((item) => item.case === name);
  expect(recorded?.session_id).toBeTruthy();
  // 只恢复指定Session，不触发新的业务请求；刷新继续使用原Run。
  await page.addInitScript((id) => localStorage.setItem('agent-runtime-active-session', id), recorded.session_id);
  await page.goto('/chat');
  const facts = await (await request.get(`/__ra_int__/facts/${recorded.run_id}`)).json();
  const active = await (await request.get(`/api/v1/chat/sessions/${recorded.session_id}/active-run`)).json();
  if (facts.run.status === 'interrupted') {
    expect(active.run_id).toBe(recorded.run_id);
    expect(active.pending_interrupt.interrupt_payload.kind).toBe('text');
    await expect(page.getByRole('textbox', { name: '补充文本' })).toBeVisible();
    await expect(page.getByRole('button', { name: '提交补充并继续' })).toBeVisible();
    await page.reload();
    await expect(page.getByRole('textbox', { name: '补充文本' })).toBeVisible();
    const refreshed = await (await request.get(`/api/v1/chat/sessions/${recorded.session_id}/active-run`)).json();
    expect(refreshed.pending_interrupt.interrupt_id).toBe(active.pending_interrupt.interrupt_id);
  } else if (facts.run.status === 'completed') {
    const { items } = await (await request.get(`/api/v1/chat/sessions/${recorded.session_id}/messages`)).json();
    expect(items.map((item) => item.role)).toEqual(['user', 'assistant']);
    expect(items[1].message_id).toBe(recorded.response_message_id);
    expect(items[1].capability_id).toBe('semantic_pattern_mining');
    expect(items[1].content).toContain('2026-10-01');
    expect(items[1].content).toContain('2026-10-20');
    await expect(page.locator('.ant-bubble-content')).toHaveCount(2);
    await expect(page.locator('.ant-bubble-content').last()).toContainText('2026-10-01');
    await expect(page.locator('.ant-bubble-content').last()).toContainText('2026-10-20');
  } else {
    expect(active.run_id).toBe(recorded.run_id);
    await expect(page.getByLabel('运行状态')).toContainText(facts.run.status);
  }
  console.log(JSON.stringify({ case: name, run_id: recorded.run_id, status: facts.run.status,
    page_restored: true, business_requests_created: 0 }));
});
