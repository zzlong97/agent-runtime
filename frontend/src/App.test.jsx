import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import RuntimeApp from './App.jsx';
import * as chatApi from './services/chatApi.js';

vi.mock('./services/chatApi.js', () => ({
  cancelRun: vi.fn(),
  createChatRun: vi.fn(),
  createRegenerateRun: vi.fn(),
  deleteSession: vi.fn(),
  getActiveRun: vi.fn(),
  listMessages: vi.fn(),
  listSessions: vi.fn(),
  renameSession: vi.fn(),
  resumeRun: vi.fn(),
  streamRunEvents: vi.fn(),
  submitFeedback: vi.fn(),
}));

const SESSION_ID = '00000000-0000-0000-0000-000000000101';
const RUN_ID = '00000000-0000-0000-0000-000000000102';
const MESSAGE_ID = '00000000-0000-0000-0000-000000000103';
const INTERRUPT_ID = '00000000-0000-0000-0000-000000000104';
const SESSION = {
  session_id: SESSION_ID,
  title: 'Runtime 会话',
  created_at: '2026-09-30T08:00:00Z',
  updated_at: '2026-09-30T09:00:00Z',
};
const RUN = {
  run_id: RUN_ID,
  session_id: SESSION_ID,
  response_message_id: MESSAGE_ID,
  status: 'queued',
  recovery_attempts: 0,
};

function event(id, type, payload) {
  return {
    id,
    type,
    data: { seq: id, event_type: type, payload },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  chatApi.listSessions.mockResolvedValue({ items: [], next_cursor: null });
  chatApi.listMessages.mockResolvedValue({ items: [], next_before: null });
  chatApi.getActiveRun.mockResolvedValue(null);
  chatApi.createChatRun.mockResolvedValue(RUN);
  chatApi.createRegenerateRun.mockResolvedValue(RUN);
  chatApi.cancelRun.mockResolvedValue({ ...RUN, status: 'cancel_requested' });
  chatApi.resumeRun.mockResolvedValue({ ...RUN, status: 'running' });
  chatApi.streamRunEvents.mockResolvedValue({ lastEventId: 0, reason: 'terminal' });
  chatApi.renameSession.mockResolvedValue(SESSION);
  chatApi.deleteSession.mockResolvedValue(null);
  chatApi.submitFeedback.mockResolvedValue({ message_id: MESSAGE_ID, feedback: 'like' });
});

describe('RuntimeApp Stage 2.5', () => {
  it('HTTP 202 返回前原子锁定提交入口并拒绝快速重复提交', async () => {
    const user = userEvent.setup();
    let resolveCreateRun;
    chatApi.createChatRun.mockImplementation(() => new Promise((resolve) => {
      resolveCreateRun = resolve;
    }));

    render(<RuntimeApp />);
    const sender = screen.getByPlaceholderText('输入消息，Enter 发送，Shift+Enter 换行');
    await user.type(sender, '/demo normal{enter}');
    await waitFor(() => expect(chatApi.createChatRun).toHaveBeenCalledTimes(1));

    await user.type(sender, '/demo normal{enter}');

    expect(chatApi.createChatRun).toHaveBeenCalledTimes(1);
    expect(screen.getAllByText('/demo normal')).toHaveLength(1);
    expect(sender).toBeDisabled();

    await act(async () => resolveCreateRun(RUN));
  });

  it('Run 提交失败时清理乐观用户消息并释放提交锁', async () => {
    const user = userEvent.setup();
    chatApi.createChatRun
      .mockRejectedValueOnce(new Error('提交响应丢失'))
      .mockResolvedValueOnce(RUN);

    render(<RuntimeApp />);
    const sender = screen.getByPlaceholderText('输入消息，Enter 发送，Shift+Enter 换行');
    await user.type(sender, '/demo normal{enter}');

    await waitFor(() => {
      expect(screen.queryByText('/demo normal')).not.toBeInTheDocument();
      expect(sender).not.toBeDisabled();
    });

    await user.type(sender, '/demo normal{enter}');
    expect(chatApi.createChatRun).toHaveBeenCalledTimes(2);
  });

  it('先展示 queued，再投影独立 SSE，并在 attempt 增长时清除旧草稿', async () => {
    const user = userEvent.setup();
    let releaseStream;
    chatApi.listSessions
      .mockResolvedValueOnce({ items: [], next_cursor: null })
      .mockResolvedValue({ items: [SESSION], next_cursor: null });
    chatApi.listMessages.mockResolvedValue({
      items: [
        {
          message_id: 'user-1',
          role: 'user',
          content: '/demo normal',
          runtime_status: null,
          capability_id: null,
          feedback: null,
        },
        {
          message_id: MESSAGE_ID,
          role: 'assistant',
          content: '服务端权威完成文本',
          runtime_status: 'completed',
          capability_id: null,
          feedback: null,
        },
      ],
      next_before: null,
    });
    chatApi.streamRunEvents.mockImplementation(
      (_runId, { onEvent }) => new Promise((resolve) => {
        releaseStream = async () => {
          await onEvent(event(1, 'run.started', { status: 'running' }));
          await onEvent(event(2, 'message.started', {
            response_message_id: MESSAGE_ID,
            attempt: 1,
          }));
          await onEvent(event(3, 'message.delta', {
            response_message_id: MESSAGE_ID,
            attempt: 1,
            delta: '旧草稿',
          }));
          await onEvent(event(4, 'message.started', {
            response_message_id: MESSAGE_ID,
            attempt: 2,
          }));
          await onEvent(event(5, 'message.delta', {
            response_message_id: MESSAGE_ID,
            attempt: 2,
            delta: '新草稿',
          }));
          await onEvent(event(6, 'message.finalized', {
            response_message_id: MESSAGE_ID,
            runtime_status: 'completed',
            capability_id: null,
          }));
          await onEvent(event(7, 'run.completed', { status: 'completed' }));
          resolve({ lastEventId: 7, reason: 'terminal' });
        };
      }),
    );

    render(<RuntimeApp />);
    const sender = screen.getByPlaceholderText('输入消息，Enter 发送，Shift+Enter 换行');
    await user.type(sender, '/demo normal{enter}');

    expect(await screen.findByText('queued')).toBeInTheDocument();
    expect(chatApi.createChatRun).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: null, content: '/demo normal' }),
    );
    await act(releaseStream);

    expect(await screen.findByText('服务端权威完成文本')).toBeInTheDocument();
    expect(screen.queryByText('旧草稿')).not.toBeInTheDocument();
    expect(screen.queryByText('新草稿')).not.toBeInTheDocument();
    expect(screen.getByLabelText('运行状态')).toHaveTextContent('completed');
    expect(screen.getByLabelText('运行状态')).toHaveTextContent('attempt 2');
  });

  it('刷新选择会话后恢复 running Run、重连事件并回读 Parent 历史', async () => {
    const user = userEvent.setup();
    chatApi.listSessions.mockResolvedValue({ items: [SESSION], next_cursor: null });
    chatApi.getActiveRun.mockResolvedValue({ ...RUN, status: 'recovering', recovery_attempts: 2 });
    chatApi.listMessages
      .mockResolvedValueOnce({ items: [], next_before: null })
      .mockResolvedValueOnce({
        items: [{
          message_id: MESSAGE_ID,
          role: 'assistant',
          content: '恢复后的权威回答',
          runtime_status: 'completed',
          capability_id: 'general_chat',
          feedback: null,
        }],
        next_before: null,
      });
    chatApi.streamRunEvents.mockImplementation(async (_runId, { onEvent }) => {
      await onEvent(event(10, 'message.started', {
        response_message_id: MESSAGE_ID,
        attempt: 3,
      }));
      await onEvent(event(11, 'run.completed', { status: 'completed' }));
      return { lastEventId: 11, reason: 'terminal' };
    });

    render(<RuntimeApp />);
    await user.click(await screen.findByText('Runtime 会话'));

    expect(await screen.findByText('恢复后的权威回答')).toBeInTheDocument();
    expect(chatApi.getActiveRun).toHaveBeenCalledWith(SESSION_ID);
    expect(chatApi.streamRunEvents).toHaveBeenCalledWith(
      RUN_ID,
      expect.objectContaining({ afterSeq: 0 }),
    );
    expect(screen.getByLabelText('运行状态')).toHaveTextContent('恢复次数');
    expect(screen.getByLabelText('运行状态')).toHaveTextContent('2');
  });

  it('刷新查询先确定无活动 Run，再读取终态 Parent 历史', async () => {
    const user = userEvent.setup();
    let resolveActiveRun;
    chatApi.listSessions.mockResolvedValue({ items: [SESSION], next_cursor: null });
    chatApi.getActiveRun.mockImplementation(() => new Promise((resolve) => {
      resolveActiveRun = resolve;
    }));
    chatApi.listMessages.mockResolvedValue({
      items: [{
        message_id: MESSAGE_ID,
        role: 'assistant',
        content: '跨终态窗口后的权威回答',
        runtime_status: 'completed',
        capability_id: 'general_chat',
        feedback: null,
      }],
      next_before: null,
    });

    render(<RuntimeApp />);
    await user.click(await screen.findByText('Runtime 会话'));
    await waitFor(() => expect(chatApi.getActiveRun).toHaveBeenCalledWith(SESSION_ID));
    expect(chatApi.listMessages).not.toHaveBeenCalled();

    await act(async () => resolveActiveRun(null));

    expect(await screen.findByText('跨终态窗口后的权威回答')).toBeInTheDocument();
    expect(chatApi.listMessages).toHaveBeenCalledWith(SESSION_ID);
  });

  it('刷新后恢复 pending Interrupt，并用同一 Run 提交 Resume', async () => {
    const user = userEvent.setup();
    chatApi.listSessions.mockResolvedValue({ items: [SESSION], next_cursor: null });
    chatApi.getActiveRun.mockResolvedValue({
      ...RUN,
      status: 'interrupted',
      pending_interrupt: {
        interrupt_id: INTERRUPT_ID,
        interrupt_payload: { prompt: '是否继续完成演示运行？' },
        created_at: '2026-09-30T09:10:00Z',
      },
    });
    chatApi.streamRunEvents.mockImplementation(async (_runId, { onEvent }) => {
      await onEvent(event(8, 'interrupt.resumed', { interrupt_id: INTERRUPT_ID }));
      await onEvent(event(9, 'run.completed', { status: 'completed' }));
      return { lastEventId: 9, reason: 'terminal' };
    });

    render(<RuntimeApp />);
    await user.click(await screen.findByText('Runtime 会话'));

    expect(await screen.findByText('是否继续完成演示运行？')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '确认并继续' }));

    expect(chatApi.resumeRun).toHaveBeenCalledWith(
      RUN_ID,
      expect.objectContaining({
        interruptId: INTERRUPT_ID,
        resumePayload: { approved: true },
      }),
    );
    await waitFor(() => {
      expect(screen.getByLabelText('运行状态')).toHaveTextContent('completed');
    });
  });

  it('显式 Cancel 展示 cancel_requested，并等待 SSE 取消终态', async () => {
    const user = userEvent.setup();
    let publishCancelled;
    chatApi.streamRunEvents.mockImplementation(
      (_runId, { onEvent }) => new Promise((resolve) => {
        publishCancelled = async () => {
          await onEvent(event(4, 'run.cancelled', { status: 'cancelled' }));
          resolve({ lastEventId: 4, reason: 'terminal' });
        };
      }),
    );
    chatApi.listMessages.mockResolvedValue({
      items: [{
        message_id: MESSAGE_ID,
        role: 'assistant',
        content: '已停止本次回复。',
        runtime_status: 'stopped',
        capability_id: null,
        feedback: null,
      }],
      next_before: null,
    });

    render(<RuntimeApp />);
    const sender = screen.getByPlaceholderText('输入消息，Enter 发送，Shift+Enter 换行');
    await user.type(sender, '/demo slow{enter}');
    await user.click(await screen.findByRole('button', { name: '取消当前运行' }));

    expect(chatApi.cancelRun).toHaveBeenCalledWith(RUN_ID);
    expect(screen.getByLabelText('运行状态')).toHaveTextContent('cancel_requested');
    await act(publishCancelled);
    expect(await screen.findByText('已停止本次回复。')).toBeInTheDocument();
    expect(screen.getByLabelText('运行状态')).toHaveTextContent('cancelled');
  });

  it.each(['queued', 'interrupted'])(
    '%s Run 直接取消为终态后立即回读 Parent 历史',
    async (status) => {
      const user = userEvent.setup();
      chatApi.listSessions.mockResolvedValue({ items: [SESSION], next_cursor: null });
      chatApi.getActiveRun.mockResolvedValue({
        ...RUN,
        status,
        ...(status === 'interrupted' ? {
          pending_interrupt: {
            interrupt_id: INTERRUPT_ID,
            interrupt_payload: { prompt: '是否继续？' },
            created_at: '2026-09-30T09:10:00Z',
          },
        } : {}),
      });
      chatApi.listMessages
        .mockResolvedValueOnce({ items: [], next_before: null })
        .mockResolvedValue({
          items: [{
            message_id: MESSAGE_ID,
            role: 'assistant',
            content: '已停止本次回复。',
            runtime_status: 'stopped',
            capability_id: null,
            feedback: null,
          }],
          next_before: null,
        });
      chatApi.cancelRun.mockResolvedValue({ ...RUN, status: 'cancelled' });
      if (status === 'queued') {
        chatApi.streamRunEvents.mockImplementation(() => new Promise(() => {}));
      }

      render(<RuntimeApp />);
      await user.click(await screen.findByText('Runtime 会话'));
      await waitFor(() => {
        expect(screen.getByLabelText('运行状态')).toHaveTextContent(status);
      });
      await user.click(await screen.findByRole('button', { name: '取消当前运行' }));

      expect(await screen.findByText('已停止本次回复。')).toBeInTheDocument();
      expect(screen.getByLabelText('运行状态')).toHaveTextContent('cancelled');
      expect(chatApi.listMessages).toHaveBeenCalledTimes(2);
    },
  );
});
