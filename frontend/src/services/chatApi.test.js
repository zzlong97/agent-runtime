import { describe, expect, it, vi } from 'vitest';

import {
  deleteSession,
  listMessages,
  listSessions,
  regenerateMessage,
  renameSession,
  stopSession,
  streamCompletion,
  submitFeedback,
} from './chatApi.js';

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('chatApi', () => {
  it('使用后端不透明游标读取 Session 和消息分页', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          items: [
            {
              session_id: 'session-1',
              title: '第一条会话',
              created_at: '2026-09-14T08:00:00Z',
              updated_at: '2026-09-14T09:00:00Z',
            },
          ],
          next_cursor: 'opaque-cursor',
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          items: [
            {
              message_id: 'message-1',
              role: 'assistant',
              content: '完整回答',
              runtime_status: 'completed',
              capability_id: 'general_chat',
              feedback: 'like',
            },
          ],
          next_before: 'message-0',
        }),
      );

    const sessions = await listSessions({
      cursor: 'opaque token/+',
      limit: 7,
      fetchImpl: fetchMock,
    });
    const messages = await listMessages('session/1', {
      before: 'message/2',
      limit: 15,
      fetchImpl: fetchMock,
    });

    expect(sessions.next_cursor).toBe('opaque-cursor');
    expect(messages.items[0].feedback).toBe('like');
    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/v1/chat/sessions?limit=7&cursor=opaque+token%2F%2B',
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      '/api/v1/chat/sessions/session%2F1/messages?limit=15&before=message%2F2',
    );
  });

  it('接入改名、删除、停止和反馈产品接口', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          session_id: 'session-1',
          title: '新标题',
          created_at: '2026-09-14T08:00:00Z',
          updated_at: '2026-09-14T10:00:00Z',
        }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(
        jsonResponse({ session_id: 'session-1', status: 'stopped' }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ message_id: 'message-1', feedback: 'dislike' }),
      );

    await renameSession('session-1', '新标题', { fetchImpl: fetchMock });
    await deleteSession('session-1', { fetchImpl: fetchMock });
    await stopSession('session-1', { fetchImpl: fetchMock });
    await submitFeedback('message-1', 'dislike', { fetchImpl: fetchMock });

    expect(fetchMock.mock.calls).toEqual([
      [
        '/api/v1/chat/sessions/session-1/rename',
        expect.objectContaining({
          method: 'PATCH',
          body: JSON.stringify({ title: '新标题' }),
        }),
      ],
      [
        '/api/v1/chat/sessions/session-1',
        expect.objectContaining({ method: 'DELETE' }),
      ],
      [
        '/api/v1/chat/sessions/session-1/stop',
        expect.objectContaining({ method: 'POST' }),
      ],
      [
        '/api/v1/chat/messages/message-1/feedback',
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({ action: 'dislike' }),
        }),
      ],
    ]);
  });

  it('聊天与重新生成使用同一个 POST SSE 客户端', async () => {
    const encoder = new TextEncoder();
    const streamResponse = () =>
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(
              encoder.encode(
                'event: done\ndata: {"session_id":"s1","message_id":"m2",' +
                  '"capability_id":"en_to_zh","status":"completed"}\n\n',
              ),
            );
            controller.close();
          },
        }),
        { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
      );
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(streamResponse())
      .mockResolvedValueOnce(streamResponse());
    const events = [];

    await streamCompletion({
      sessionId: null,
      content: 'Good morning',
      onEvent: (event) => events.push(event),
      fetchImpl: fetchMock,
    });
    await regenerateMessage({
      sessionId: 's1',
      messageId: 'm1',
      onEvent: (event) => events.push(event),
      fetchImpl: fetchMock,
    });

    expect(fetchMock.mock.calls[0]).toEqual([
      '/api/v1/chat/completions',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          session_id: null,
          message: { content: 'Good morning' },
        }),
      }),
    ]);
    expect(fetchMock.mock.calls[1]).toEqual([
      '/api/v1/chat/sessions/s1/messages/m1/regenerate',
      expect.objectContaining({ method: 'POST' }),
    ]);
    expect(events).toHaveLength(2);
  });

  it('将普通 HTTP 错误转换为稳定产品错误', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(
        {
          detail: {
            code: 'MESSAGE_FEEDBACK_NOT_ALLOWED',
            message: '仅允许反馈已完成回答',
            retryable: false,
          },
        },
        409,
      ),
    );

    await expect(
      submitFeedback('message-1', 'like', { fetchImpl: fetchMock }),
    ).rejects.toMatchObject({
      code: 'MESSAGE_FEEDBACK_NOT_ALLOWED',
      message: '仅允许反馈已完成回答',
      retryable: false,
      status: 409,
    });
  });
});
