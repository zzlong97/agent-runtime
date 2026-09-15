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
        throw new Error(
          `浏览器验收删除 Session 失败：HTTP ${deleted.status()}`,
        );
      }
    }
  }
});

test('真实浏览器完成 Stage 2 核心聊天产品流程', async ({ page }) => {
  await page.goto('/chat');

  await expect(
    page.getByRole('heading', { name: '开始一段新对话' }),
  ).toBeVisible();

  const sender = page.getByPlaceholder(
    '输入消息，Enter 发送，Shift+Enter 换行',
  );
  const runPanel = page.getByLabel('运行状态');

  await sender.fill('FastAPI 是什么？');
  await sender.press('Enter');
  await expect(
    page.getByText('FastAPI 是一个现代 Python Web 框架。'),
  ).toBeVisible();
  await expect(runPanel).toContainText('general_chat');
  await expect(runPanel).toContainText('completed');

  const likeButton = page.getByRole('button', {
    name: '喜欢此回答',
    exact: true,
  });
  await likeButton.click();
  await expect(likeButton).toHaveAttribute('aria-pressed', 'true');

  await page.getByRole('button', { name: '重新生成最新回答' }).click();
  await expect(runPanel).toContainText('completed');
  await expect(
    page.getByRole('button', { name: '重新生成最新回答' }),
  ).toBeEnabled();

  await sender.fill('Translate "Good morning" into Chinese');
  await sender.press('Enter');
  await expect(page.getByText('早上好。')).toBeVisible();
  await expect(runPanel).toContainText('en_to_zh');
  await expect(runPanel).toContainText('completed');

  await page.getByRole('button', { name: '重命名当前会话' }).click();
  const titleInput = page.getByRole('textbox', { name: '新会话标题' });
  await titleInput.fill('浏览器验收会话');
  await page.getByRole('button', { name: /保\s*存/ }).click();
  await expect(
    page.getByRole('heading', { name: '浏览器验收会话' }),
  ).toBeVisible();

  await page.getByRole('button', { name: /新建会话/ }).click();
  await expect(
    page.getByRole('heading', { name: '开始一段新对话' }),
  ).toBeVisible();
  await page.getByText('浏览器验收会话', { exact: true }).click();
  await expect(
    page.getByText('FastAPI 是一个现代 Python Web 框架。'),
  ).toBeVisible();

  await sender.fill('请翻译成英文：你好');
  await sender.press('Enter');
  await expect(runPanel).toContainText('unsupported');
  await expect(
    page.getByText('抱歉，当前能力无法处理这个请求。'),
  ).toBeVisible();

  await page.getByRole('button', { name: '删除当前会话' }).click();
  const dialog = page.getByRole('dialog', { name: '确认删除会话' });
  await dialog.getByRole('button', { name: /确\s*认\s*删\s*除/ }).click();
  await expect(
    page.getByRole('heading', { name: '开始一段新对话' }),
  ).toBeVisible();
  await expect(page.getByText('还没有历史会话')).toBeVisible();

  await sender.fill('执行长任务');
  await sender.press('Enter');
  await expect(page.getByText('部分输出')).toBeVisible();
  await page.getByRole('button', { name: '停止当前回答' }).click();
  await expect(runPanel).toContainText('stopped');
  await expect(page.getByText('已停止')).toBeVisible();

  await page.getByRole('button', { name: '删除当前会话' }).click();
  const stoppedDialog = page.getByRole('dialog', { name: '确认删除会话' });
  await stoppedDialog
    .getByRole('button', { name: /确\s*认\s*删\s*除/ })
    .click();
  await expect(page.getByText('还没有历史会话')).toBeVisible();
});
