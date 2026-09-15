import { describe, expect, it, vi } from 'vitest';

import { streamPost } from './sseClient.js';

function streamingResponse(chunks, init = {}) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
    ...init,
  });
}

describe('streamPost', () => {
  it('可以跨网络分片解析 message、error 和 done 事件', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      streamingResponse([
        'event: message\ndata: {"session_id":"s1","message_id":"m1",',
        '"capability_id":"general_chat","delta":"你',
        '好"}\n\nevent: done\ndata: {"session_id":"s1",',
        '"message_id":"m1","capability_id":"general_chat",',
        '"status":"completed"}\n\n',
      ]),
    );
    const events = [];

    await streamPost('/api/v1/chat/completions', {
      body: { session_id: null, message: { content: '你好' } },
      onEvent: (event) => events.push(event),
      fetchImpl: fetchMock,
    });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/chat/completions',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: null,
          message: { content: '你好' },
        }),
      }),
    );
    expect(events).toEqual([
      {
        type: 'message',
        data: {
          session_id: 's1',
          message_id: 'm1',
          capability_id: 'general_chat',
          delta: '你好',
        },
      },
      {
        type: 'done',
        data: {
          session_id: 's1',
          message_id: 'm1',
          capability_id: 'general_chat',
          status: 'completed',
        },
      },
    ]);
  });

  it('在建立 SSE 前保留后端产品错误字段', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: {
            code: 'SESSION_NOT_FOUND',
            message: 'Session 不存在',
            retryable: false,
          },
        }),
        {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        },
      ),
    );

    await expect(
      streamPost('/api/v1/chat/completions', { fetchImpl: fetchMock }),
    ).rejects.toMatchObject({
      code: 'SESSION_NOT_FOUND',
      message: 'Session 不存在',
      retryable: false,
      status: 404,
    });
  });

  it('事件流未发布 done 就断开时返回中文可重试错误', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      streamingResponse([
        'event: message\ndata: {"session_id":"s1","message_id":"m1",' +
          '"capability_id":"general_chat","delta":"部分内容"}\n\n',
      ]),
    );

    await expect(
      streamPost('/api/v1/chat/completions', { fetchImpl: fetchMock }),
    ).rejects.toMatchObject({
      code: 'SSE_STREAM_INCOMPLETE',
      message: '事件流在完成前意外中断，请重试',
      retryable: true,
    });
  });

  it('无法解析产品事件数据时不暴露底层 JSON 异常', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      streamingResponse(['event: message\ndata: {invalid-json}\n\n']),
    );

    await expect(
      streamPost('/api/v1/chat/completions', { fetchImpl: fetchMock }),
    ).rejects.toMatchObject({
      code: 'SSE_EVENT_INVALID',
      message: '服务器返回了无法解析的事件数据',
      retryable: true,
    });
  });
});
