import { describe, expect, it, vi } from 'vitest';

import {
  cancelRun,
  createChatRun,
  createRegenerateRun,
  deleteSession,
  getActiveRun,
  listMessages,
  listSessions,
  renameSession,
  resumeRun,
  streamRunEvents,
  submitFeedback,
} from './chatApi.js';

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const RUN = {
  run_id: 'run-1',
  session_id: 'session-1',
  response_message_id: 'message-1',
  status: 'queued',
  recovery_attempts: 0,
};

describe('chatApi', () => {
  it('使用后端不透明游标读取 Session 和消息分页', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ items: [], next_cursor: 'opaque-cursor' }))
      .mockResolvedValueOnce(jsonResponse({ items: [], next_before: 'message-0' }));

    await listSessions({ cursor: 'opaque token/+', limit: 7, fetchImpl: fetchMock });
    await listMessages('session/1', {
      before: 'message/2',
      limit: 15,
      fetchImpl: fetchMock,
    });

    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/v1/chat/sessions?limit=7&cursor=opaque+token%2F%2B',
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      '/api/v1/chat/sessions/session%2F1/messages?limit=15&before=message%2F2',
    );
  });

  it('普通消息和 Regenerate 先提交 request_id 并接收 202 Run 摘要', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(RUN, 202))
      .mockResolvedValueOnce(jsonResponse(RUN, 202));

    await createChatRun({
      requestId: 'request-1',
      sessionId: null,
      content: '你好',
      fetchImpl: fetchMock,
    });
    await createRegenerateRun({
      requestId: 'request-2',
      sessionId: 'session-1',
      messageId: 'message-0',
      fetchImpl: fetchMock,
    });

    expect(fetchMock.mock.calls).toEqual([
      [
        '/api/v1/chat/completions',
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({
            request_id: 'request-1',
            session_id: null,
            message: { content: '你好' },
          }),
        }),
      ],
      [
        '/api/v1/chat/sessions/session-1/messages/message-0/regenerate',
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({ request_id: 'request-2' }),
        }),
      ],
    ]);
  });

  it('接入活动 Run、Cancel 和同 Run Resume 产品接口', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ ...RUN, status: 'interrupted' }))
      .mockResolvedValueOnce(jsonResponse({ ...RUN, status: 'cancel_requested' }, 202))
      .mockResolvedValueOnce(jsonResponse({ ...RUN, status: 'running' }, 202));

    await getActiveRun('session-1', { fetchImpl: fetchMock });
    await cancelRun('run-1', { fetchImpl: fetchMock });
    await resumeRun(
      'run-1',
      {
        interruptId: 'interrupt-1',
        requestId: 'resume-request-1',
        resumePayload: { approved: true },
      },
      { fetchImpl: fetchMock },
    );

    expect(fetchMock.mock.calls).toEqual([
      ['/api/v1/chat/sessions/session-1/active-run', {}],
      ['/api/v1/chat/runs/run-1/cancel', { method: 'POST' }],
      [
        '/api/v1/chat/runs/run-1/resume',
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({
            interrupt_id: 'interrupt-1',
            request_id: 'resume-request-1',
            resume_payload: { approved: true },
          }),
        }),
      ],
    ]);
  });

  it('独立 GET SSE 使用 Run 资源和 after_seq', async () => {
    const encoder = new TextEncoder();
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(
              encoder.encode(
                'id: 8\nevent: run.completed\ndata: {"event_type":' +
                  '"run.completed","seq":8,"payload":{"status":"completed"}}\n\n',
              ),
            );
            controller.close();
          },
        }),
        { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
      ),
    );

    await streamRunEvents('run/1', { afterSeq: 5, fetchImpl: fetchMock });

    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/v1/chat/runs/run%2F1/events?after_seq=5',
    );
    expect(fetchMock.mock.calls[0][1]).toEqual(
      expect.objectContaining({ method: 'GET' }),
    );
  });

  it('保留会话产品操作并转换稳定 HTTP 错误', async () => {
    const successFetch = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ title: '新标题' }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ feedback: 'like' }));
    await renameSession('session-1', '新标题', { fetchImpl: successFetch });
    await deleteSession('session-1', { fetchImpl: successFetch });
    await submitFeedback('message-1', 'like', { fetchImpl: successFetch });

    const failureFetch = vi.fn().mockResolvedValue(
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
      submitFeedback('message-1', 'like', { fetchImpl: failureFetch }),
    ).rejects.toMatchObject({
      code: 'MESSAGE_FEEDBACK_NOT_ALLOWED',
      status: 409,
    });
  });
});
