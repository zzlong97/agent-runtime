import { describe, expect, it, vi } from 'vitest';

import { streamGet } from './sseClient.js';

function streamingResponse(chunks, init = {}) {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(controller) {
        chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)));
        controller.close();
      },
    }),
    {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
      ...init,
    },
  );
}

function readerResponse(readResults) {
  const encoder = new TextEncoder();
  const read = vi.fn();
  for (const result of readResults) {
    if (result instanceof Error) read.mockRejectedValueOnce(result);
    else read.mockResolvedValueOnce({ value: encoder.encode(result), done: false });
  }
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({ read, cancel: vi.fn() }),
    },
  };
}

describe('streamGet', () => {
  it('跨网络分片解析公开 RuntimeEvent 的 id、类型和 payload', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      streamingResponse([
        'id: 2\nevent: message.started\ndata: {"event_type":"message.started",',
        '"seq":2,"payload":{"response_message_id":"m1","attempt":1}}\n\n',
        ': heartbeat\n\nid: 3\nevent: message.delta\ndata: {"event_type":',
        '"message.delta","seq":3,"payload":{"response_message_id":"m1",',
        '"attempt":1,"delta":"你好"}}\n\n',
        'id: 4\nevent: run.completed\ndata: {"event_type":"run.completed",',
        '"seq":4,"payload":{"status":"completed"}}\n\n',
      ]),
    );
    const events = [];

    const result = await streamGet('/api/v1/chat/runs/r1/events', {
      onEvent: (event) => events.push(event),
      fetchImpl: fetchMock,
    });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/chat/runs/r1/events?after_seq=0',
      expect.objectContaining({ method: 'GET' }),
    );
    expect(events.map((event) => [event.id, event.type])).toEqual([
      [2, 'message.started'],
      [3, 'message.delta'],
      [4, 'run.completed'],
    ]);
    expect(events[1].data.payload.delta).toBe('你好');
    expect(result).toEqual({ lastEventId: 4, reason: 'terminal' });
  });

  it('意外断线后从最后成功处理的 seq 自动重连', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        streamingResponse([
          'id: 7\nevent: message.delta\ndata: {"event_type":"message.delta",',
          '"seq":7,"payload":{"response_message_id":"m1","attempt":1,',
          '"delta":"部分"}}\n\n',
        ]),
      )
      .mockResolvedValueOnce(
        streamingResponse([
          'id: 9\nevent: run.completed\ndata: {"event_type":"run.completed",',
          '"seq":9,"payload":{"status":"completed"}}\n\n',
        ]),
      );

    const result = await streamGet('/api/v1/chat/runs/r1/events', {
      afterSeq: 5,
      onEvent: vi.fn(),
      reconnectDelayMs: 0,
      maxReconnectAttempts: 1,
      fetchImpl: fetchMock,
    });

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/chat/runs/r1/events?after_seq=5',
      '/api/v1/chat/runs/r1/events?after_seq=7',
    ]);
    expect(result.lastEventId).toBe(9);
  });

  it('reader 网络异常后保留最后成功处理的 seq 并自动重连', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        readerResponse([
          'id: 7\nevent: message.delta\ndata: {"event_type":"message.delta",'
            + '"seq":7,"payload":{"response_message_id":"m1","attempt":1,'
            + '"delta":"部分"}}\n\n',
          new TypeError('network disconnected'),
        ]),
      )
      .mockResolvedValueOnce(
        streamingResponse([
          'id: 9\nevent: run.completed\ndata: {"event_type":"run.completed",',
          '"seq":9,"payload":{"status":"completed"}}\n\n',
        ]),
      );
    const events = [];

    const result = await streamGet('/events', {
      afterSeq: 5,
      onEvent: (item) => events.push(item.id),
      reconnectDelayMs: 0,
      maxReconnectAttempts: 1,
      fetchImpl: fetchMock,
    });

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/events?after_seq=5',
      '/events?after_seq=7',
    ]);
    expect(events).toEqual([7, 9]);
    expect(result).toEqual({ lastEventId: 9, reason: 'terminal' });
  });

  it('首次 fetch 网络失败后从原游标重连', async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('network unavailable'))
      .mockResolvedValueOnce(
        streamingResponse([
          'id: 9\nevent: run.completed\ndata: {"event_type":"run.completed",',
          '"seq":9,"payload":{"status":"completed"}}\n\n',
        ]),
      );

    const result = await streamGet('/events', {
      afterSeq: 5,
      reconnectDelayMs: 0,
      maxReconnectAttempts: 1,
      fetchImpl: fetchMock,
    });

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/events?after_seq=5',
      '/events?after_seq=5',
    ]);
    expect(result).toEqual({ lastEventId: 9, reason: 'terminal' });
  });

  it('interrupt.required 关闭当前连接并交给页面展示 Resume 操作', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      streamingResponse([
        'id: 6\nevent: interrupt.required\ndata: {"event_type":',
        '"interrupt.required","seq":6,"payload":{"interrupt_id":"i1"}}\n\n',
      ]),
    );

    await expect(
      streamGet('/events', { fetchImpl: fetchMock }),
    ).resolves.toEqual({ lastEventId: 6, reason: 'interrupted' });
  });

  it('在建立 SSE 前保留后端产品错误字段', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: {
            code: 'RUN_NOT_FOUND',
            message: 'Run 不存在',
            retryable: false,
          },
        }),
        { status: 404, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    await expect(streamGet('/events', { fetchImpl: fetchMock })).rejects.toMatchObject({
      code: 'RUN_NOT_FOUND',
      message: 'Run 不存在',
      retryable: false,
      status: 404,
    });
  });

  it('拒绝缺少有效 seq 的公开事件', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      streamingResponse([
        'event: run.completed\ndata: {"event_type":"run.completed",',
        '"seq":2,"payload":{"status":"completed"}}\n\n',
      ]),
    );

    await expect(streamGet('/events', { fetchImpl: fetchMock })).rejects.toMatchObject({
      code: 'SSE_EVENT_INVALID',
      message: '服务器返回了无法解析的事件数据',
    });
  });
});
