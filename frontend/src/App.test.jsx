import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import RuntimeApp from './App.jsx';
import * as chatApi from './services/chatApi.js';

vi.mock('./services/chatApi.js', () => ({
  deleteSession: vi.fn(),
  listMessages: vi.fn(),
  listSessions: vi.fn(),
  regenerateMessage: vi.fn(),
  renameSession: vi.fn(),
  stopSession: vi.fn(),
  streamCompletion: vi.fn(),
  submitFeedback: vi.fn(),
}));

const SESSION_ONE = {
  session_id: '00000000-0000-0000-0000-000000000101',
  title: '第一条会话',
  created_at: '2026-09-14T08:00:00Z',
  updated_at: '2026-09-14T09:00:00Z',
};

const SESSION_TWO = {
  session_id: '00000000-0000-0000-0000-000000000102',
  title: '更早的会话',
  created_at: '2026-09-13T08:00:00Z',
  updated_at: '2026-09-13T09:00:00Z',
};

const COMPLETE_HISTORY = {
  items: [
    {
      message_id: '00000000-0000-0000-0000-000000000201',
      role: 'user',
      content: '介绍一下 FastAPI',
      runtime_status: null,
      capability_id: null,
      feedback: null,
    },
    {
      message_id: '00000000-0000-0000-0000-000000000202',
      role: 'assistant',
      content: 'FastAPI 是一个现代 Python Web 框架。',
      runtime_status: 'completed',
      capability_id: 'general_chat',
      feedback: null,
    },
  ],
  next_before: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  chatApi.listSessions.mockResolvedValue({ items: [], next_cursor: null });
  chatApi.listMessages.mockResolvedValue({ items: [], next_before: null });
  chatApi.renameSession.mockResolvedValue(SESSION_ONE);
  chatApi.deleteSession.mockResolvedValue(null);
  chatApi.stopSession.mockResolvedValue({
    session_id: SESSION_ONE.session_id,
    status: 'stopped',
  });
  chatApi.submitFeedback.mockResolvedValue({
    message_id: COMPLETE_HISTORY.items[1].message_id,
    feedback: 'like',
  });
  chatApi.streamCompletion.mockResolvedValue();
  chatApi.regenerateMessage.mockResolvedValue();
});

describe('RuntimeApp', () => {
  it('新的 Session 全量刷新不会被较早的列表响应覆盖', async () => {
    const user = userEvent.setup();
    let resolveInitialSessions;
    chatApi.listSessions
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveInitialSessions = resolve;
          }),
      )
      .mockResolvedValueOnce({ items: [SESSION_ONE], next_cursor: null });
    chatApi.streamCompletion.mockImplementation(async ({ onEvent }) => {
      await onEvent({
        type: 'done',
        data: {
          session_id: SESSION_ONE.session_id,
          message_id: '00000000-0000-0000-0000-000000000200',
          capability_id: 'general_chat',
          status: 'completed',
        },
      });
    });

    render(<RuntimeApp />);
    const sender = screen.getByPlaceholderText('输入消息，Enter 发送，Shift+Enter 换行');
    await user.type(sender, '你好{enter}');
    const sessionSidebar = screen.getByLabelText('会话列表');
    expect(await within(sessionSidebar).findByText('第一条会话')).toBeInTheDocument();

    await act(async () => {
      resolveInitialSessions({ items: [SESSION_TWO], next_cursor: null });
    });
    expect(within(sessionSidebar).getByText('第一条会话')).toBeInTheDocument();
    expect(within(sessionSidebar).queryByText('更早的会话')).not.toBeInTheDocument();
  });

  it('分页加载、选择 Session，并从后端读取活动分支历史', async () => {
    const user = userEvent.setup();
    chatApi.listSessions
      .mockResolvedValueOnce({
        items: [SESSION_ONE],
        next_cursor: 'next-session-page',
      })
      .mockResolvedValueOnce({
        items: [SESSION_TWO],
        next_cursor: null,
      });
    chatApi.listMessages.mockResolvedValue(COMPLETE_HISTORY);

    render(<RuntimeApp />);

    await screen.findByText('第一条会话');
    await user.click(screen.getByRole('button', { name: '加载更多会话' }));
    expect(await screen.findByText('更早的会话')).toBeInTheDocument();
    expect(chatApi.listSessions).toHaveBeenLastCalledWith({
      cursor: 'next-session-page',
    });

    await user.click(screen.getByText('第一条会话'));
    expect(await screen.findByText('介绍一下 FastAPI')).toBeInTheDocument();
    expect(screen.getByText('FastAPI 是一个现代 Python Web 框架。')).toBeInTheDocument();
    expect(chatApi.listMessages).toHaveBeenCalledWith(SESSION_ONE.session_id);
  });

  it('快速切换 Session 时不会让较早响应覆盖当前活动会话', async () => {
    const user = userEvent.setup();
    let resolveFirstHistory;
    chatApi.listSessions.mockResolvedValue({
      items: [SESSION_ONE, SESSION_TWO],
      next_cursor: null,
    });
    chatApi.listMessages
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirstHistory = resolve;
          }),
      )
      .mockResolvedValueOnce({
        items: [
          {
            message_id: '00000000-0000-0000-0000-000000000211',
            role: 'assistant',
            content: '第二个会话的权威历史',
            runtime_status: 'completed',
            capability_id: 'general_chat',
            feedback: null,
          },
        ],
        next_before: null,
      });

    render(<RuntimeApp />);
    await screen.findByText('第一条会话');
    await user.click(screen.getByText('第一条会话'));
    await user.click(screen.getByText('更早的会话'));
    expect(await screen.findByText('第二个会话的权威历史')).toBeInTheDocument();

    resolveFirstHistory(COMPLETE_HISTORY);
    await waitFor(() => {
      expect(screen.queryByText('介绍一下 FastAPI')).not.toBeInTheDocument();
    });
    expect(screen.getByText('第二个会话的权威历史')).toBeInTheDocument();
  });

  it('切换 Session 后丢弃旧会话尚未完成的更早消息分页', async () => {
    const user = userEvent.setup();
    let resolveOlderPage;
    chatApi.listSessions.mockResolvedValue({
      items: [SESSION_ONE, SESSION_TWO],
      next_cursor: null,
    });
    chatApi.listMessages
      .mockResolvedValueOnce({ ...COMPLETE_HISTORY, next_before: 'older-page' })
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveOlderPage = resolve;
          }),
      )
      .mockResolvedValueOnce({
        items: [
          {
            message_id: '00000000-0000-0000-0000-000000000212',
            role: 'assistant',
            content: '第二个会话的权威历史',
            runtime_status: 'completed',
            capability_id: 'general_chat',
            feedback: null,
          },
        ],
        next_before: null,
      });

    render(<RuntimeApp />);
    await user.click(await screen.findByText('第一条会话'));
    await screen.findByText('FastAPI 是一个现代 Python Web 框架。');
    await user.click(screen.getByRole('button', { name: '加载更早消息' }));
    await user.click(screen.getByText('更早的会话'));
    expect(await screen.findByText('第二个会话的权威历史')).toBeInTheDocument();

    await act(async () => {
      resolveOlderPage({
        items: [
          {
            message_id: '00000000-0000-0000-0000-000000000213',
            role: 'assistant',
            content: '不应混入新会话的旧分页消息',
            runtime_status: 'completed',
            capability_id: 'general_chat',
            feedback: null,
          },
        ],
        next_before: null,
      });
    });
    expect(screen.queryByText('不应混入新会话的旧分页消息')).not.toBeInTheDocument();
  });

  it('新建 Session 后流式展示回答并更新运行状态面板', async () => {
    const user = userEvent.setup();
    const finalHistory = {
      items: [
        {
          message_id: '00000000-0000-0000-0000-000000000301',
          role: 'user',
          content: '你好',
          runtime_status: null,
          capability_id: null,
          feedback: null,
        },
        {
          message_id: '00000000-0000-0000-0000-000000000302',
          role: 'assistant',
          content: '你好，很高兴见到你。',
          runtime_status: 'completed',
          capability_id: 'general_chat',
          feedback: null,
        },
      ],
      next_before: null,
    };
    chatApi.listMessages.mockResolvedValue(finalHistory);
    chatApi.streamCompletion.mockImplementation(async ({ onEvent }) => {
      await onEvent({
        type: 'message',
        data: {
          session_id: SESSION_ONE.session_id,
          message_id: finalHistory.items[1].message_id,
          capability_id: 'general_chat',
          delta: '你好，很高兴见到你。',
        },
      });
      await onEvent({
        type: 'done',
        data: {
          session_id: SESSION_ONE.session_id,
          message_id: finalHistory.items[1].message_id,
          capability_id: 'general_chat',
          status: 'completed',
        },
      });
    });

    render(<RuntimeApp />);
    const sender = await screen.findByPlaceholderText('输入消息，Enter 发送，Shift+Enter 换行');
    await user.type(sender, '你好{enter}');

    expect(await screen.findByText('你好，很高兴见到你。')).toBeInTheDocument();
    expect(chatApi.streamCompletion).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: null, content: '你好' }),
    );
    const runPanel = screen.getByLabelText('运行状态');
    expect(runPanel).toHaveTextContent(SESSION_ONE.session_id);
    expect(runPanel).toHaveTextContent(finalHistory.items[1].message_id);
    expect(runPanel).toHaveTextContent('general_chat');
    expect(runPanel).toHaveTextContent('completed');
  });

  it('SSE 意外断开后回读服务端已持久化的权威消息状态', async () => {
    const user = userEvent.setup();
    chatApi.listSessions
      .mockResolvedValueOnce({ items: [], next_cursor: null })
      .mockResolvedValue({ items: [SESSION_ONE], next_cursor: null });
    chatApi.listMessages.mockResolvedValue({
      items: [
        {
          message_id: '00000000-0000-0000-0000-000000000311',
          role: 'user',
          content: '生成一段内容',
          runtime_status: null,
          capability_id: null,
          feedback: null,
        },
        {
          message_id: '00000000-0000-0000-0000-000000000312',
          role: 'assistant',
          content: '服务端保存的部分内容',
          runtime_status: 'incomplete',
          capability_id: 'general_chat',
          feedback: null,
        },
      ],
      next_before: null,
    });
    chatApi.streamCompletion.mockImplementation(async ({ onEvent }) => {
      await onEvent({
        type: 'message',
        data: {
          session_id: SESSION_ONE.session_id,
          message_id: '00000000-0000-0000-0000-000000000312',
          capability_id: 'general_chat',
          delta: '部分内容',
        },
      });
      throw new Error('事件流意外中断');
    });

    render(<RuntimeApp />);
    const sender = await screen.findByPlaceholderText('输入消息，Enter 发送，Shift+Enter 换行');
    await user.type(sender, '生成一段内容{enter}');

    expect(await screen.findByText('服务端保存的部分内容')).toBeInTheDocument();
    expect(chatApi.listMessages).toHaveBeenCalledWith(SESSION_ONE.session_id);
    expect(screen.getByText('未完成')).toBeInTheDocument();
  });

  it('运行期间可调用 Stop，并等待 SSE 发布停止终态', async () => {
    const user = userEvent.setup();
    let publishDone;
    chatApi.listSessions.mockResolvedValue({
      items: [SESSION_ONE],
      next_cursor: null,
    });
    chatApi.streamCompletion.mockImplementation(
      ({ onEvent }) =>
        new Promise((resolve) => {
          publishDone = async () => {
            await onEvent({
              type: 'done',
              data: {
                session_id: SESSION_ONE.session_id,
                message_id: '00000000-0000-0000-0000-000000000401',
                capability_id: 'general_chat',
                status: 'stopped',
              },
            });
            resolve();
          };
        }),
    );

    render(<RuntimeApp />);
    await user.click(await screen.findByText('第一条会话'));
    const sender = screen.getByPlaceholderText('输入消息，Enter 发送，Shift+Enter 换行');
    await user.type(sender, '请执行长任务{enter}');
    expect(screen.getByRole('button', { name: '删除当前会话' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '停止当前回答' }));

    expect(chatApi.stopSession).toHaveBeenCalledWith(SESSION_ONE.session_id);
    await publishDone();
    await waitFor(() => {
      expect(screen.getByLabelText('运行状态')).toHaveTextContent('stopped');
    });
  });

  it('支持重命名、二次确认删除、重新生成与反馈', async () => {
    const user = userEvent.setup();
    chatApi.listSessions
      .mockResolvedValueOnce({ items: [SESSION_ONE], next_cursor: null })
      .mockResolvedValue({ items: [], next_cursor: null });
    chatApi.listMessages.mockResolvedValue(COMPLETE_HISTORY);
    chatApi.regenerateMessage.mockImplementation(async ({ onEvent }) => {
      await onEvent({
        type: 'done',
        data: {
          session_id: SESSION_ONE.session_id,
          message_id: '00000000-0000-0000-0000-000000000203',
          capability_id: 'general_chat',
          status: 'completed',
        },
      });
    });

    render(<RuntimeApp />);
    await user.click(await screen.findByText('第一条会话'));
    await screen.findByText('FastAPI 是一个现代 Python Web 框架。');

    const feedback = screen.getByLabelText('回答反馈');
    await user.click(within(feedback).getAllByRole('button')[0]);
    expect(chatApi.submitFeedback).toHaveBeenCalledWith(
      COMPLETE_HISTORY.items[1].message_id,
      'like',
    );

    await user.click(screen.getByRole('button', { name: '重新生成最新回答' }));
    expect(chatApi.regenerateMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        sessionId: SESSION_ONE.session_id,
        messageId: COMPLETE_HISTORY.items[1].message_id,
      }),
    );

    await user.click(screen.getByRole('button', { name: '重命名当前会话' }));
    const renameInput = screen.getByRole('textbox', { name: '新会话标题' });
    await user.clear(renameInput);
    await user.type(renameInput, '新的演示标题');
    await user.click(screen.getByRole('button', { name: /保\s*存/ }));
    expect(chatApi.renameSession).toHaveBeenCalledWith(
      SESSION_ONE.session_id,
      '新的演示标题',
    );
    await user.click(screen.getByRole('button', { name: '删除当前会话' }));
    const dialog = screen.getByText('确认删除会话').closest('[role="dialog"]');
    expect(dialog).not.toBeNull();
    expect(dialog).toHaveTextContent('删除后无法恢复');
    await user.click(
      within(dialog).getByRole('button', { name: /确\s*认\s*删\s*除/ }),
    );
    expect(chatApi.deleteSession).toHaveBeenCalledWith(SESSION_ONE.session_id);
  });
});
