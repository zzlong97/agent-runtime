const PUBLIC_EVENT_NAMES = new Set([
  'run.started',
  'message.started',
  'message.delta',
  'message.finalized',
  'interrupt.required',
  'interrupt.resumed',
  'run.completed',
  'run.cancelled',
  'run.failed',
]);
const TERMINAL_EVENT_NAMES = new Set([
  'run.completed',
  'run.cancelled',
  'run.failed',
]);

export class ProductApiError extends Error {
  constructor({ code, message, retryable = false, status = 0 }) {
    super(message);
    this.name = 'ProductApiError';
    this.code = code;
    this.retryable = retryable;
    this.status = status;
  }
}

class ReconnectableSseError extends Error {
  constructor(cause) {
    super('SSE 网络连接已中断', { cause });
    this.name = 'ReconnectableSseError';
  }
}

function isAbortError(error, signal) {
  return signal?.aborted || error?.name === 'AbortError';
}

export async function buildApiError(response) {
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  const detail = payload?.detail;
  if (detail && typeof detail === 'object') {
    return new ProductApiError({
      code: detail.code ?? 'HTTP_REQUEST_FAILED',
      message: detail.message ?? '请求失败，请稍后重试',
      retryable: detail.retryable ?? false,
      status: response.status,
    });
  }
  return new ProductApiError({
    code: 'HTTP_REQUEST_FAILED',
    message: `请求失败（HTTP ${response.status}）`,
    retryable: response.status >= 500,
    status: response.status,
  });
}

function invalidEventError() {
  return new ProductApiError({
    code: 'SSE_EVENT_INVALID',
    message: '服务器返回了无法解析的事件数据',
    retryable: true,
  });
}

function parseFrame(frame) {
  let eventName = 'message';
  let eventId = null;
  const dataLines = [];
  for (const line of frame.replaceAll('\r\n', '\n').split('\n')) {
    if (line.startsWith(':')) continue;
    if (line.startsWith('id:')) {
      const rawId = line.slice('id:'.length).trim();
      eventId = Number(rawId);
      if (!/^\d+$/.test(rawId) || eventId < 1) throw invalidEventError();
    } else if (line.startsWith('event:')) {
      eventName = line.slice('event:'.length).trim();
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).trimStart());
    }
  }
  if (dataLines.length === 0) return null;
  if (!PUBLIC_EVENT_NAMES.has(eventName) || eventId === null) {
    throw invalidEventError();
  }
  try {
    const data = JSON.parse(dataLines.join('\n'));
    if (data.event_type !== eventName || data.seq !== eventId) {
      throw invalidEventError();
    }
    return { id: eventId, type: eventName, data };
  } catch (error) {
    if (error instanceof ProductApiError) throw error;
    throw invalidEventError();
  }
}

function withCursor(url, cursor) {
  const separator = url.includes('?') ? '&' : '?';
  return `${url}${separator}after_seq=${cursor}`;
}

function waitForReconnect(delayMs, signal) {
  if (delayMs <= 0) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(resolve, delayMs);
    signal?.addEventListener(
      'abort',
      () => {
        clearTimeout(timeout);
        reject(signal.reason ?? new DOMException('已中止', 'AbortError'));
      },
      { once: true },
    );
  });
}

async function readConnection(response, onEvent, onCursor, signal) {
  if (!response.body) {
    throw new ProductApiError({
      code: 'SSE_STREAM_UNAVAILABLE',
      message: '服务器未返回可读取的事件流',
      retryable: true,
      status: response.status,
    });
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let cursor = null;
  let reason = null;
  let interruptPending = false;

  const dispatch = async (event) => {
    if (!event) return;
    await onEvent(event);
    cursor = event.id;
    onCursor(cursor);
    if (TERMINAL_EVENT_NAMES.has(event.type)) reason = 'terminal';
    if (event.type === 'interrupt.required') interruptPending = true;
    if (event.type === 'interrupt.resumed') interruptPending = false;
  };

  while (reason === null) {
    let result;
    try {
      result = await reader.read();
    } catch (error) {
      if (isAbortError(error, signal)) throw error;
      throw new ReconnectableSseError(error);
    }
    const { value, done } = result;
    buffer += decoder.decode(value, { stream: !done });
    buffer = buffer.replaceAll('\r\n', '\n');
    let boundary = buffer.indexOf('\n\n');
    while (boundary >= 0 && reason === null) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      await dispatch(parseFrame(frame));
      boundary = buffer.indexOf('\n\n');
    }
    if (done) break;
  }
  if (reason === null && buffer.trim()) await dispatch(parseFrame(buffer.trim()));
  if (reason === null && interruptPending) reason = 'interrupted';
  if (reason !== null) {
    try {
      await reader.cancel();
    } catch {
      // 终态或中断已成功处理，关闭底层流失败不改变页面结果。
    }
  }
  return { cursor, reason };
}

export async function streamGet(
  url,
  {
    afterSeq = 0,
    signal,
    onEvent = () => {},
    reconnectDelayMs = 250,
    maxReconnectAttempts = Number.POSITIVE_INFINITY,
    fetchImpl = globalThis.fetch,
  } = {},
) {
  let cursor = afterSeq;
  let reconnectAttempts = 0;
  while (true) {
    let connection = null;
    try {
      let response;
      try {
        response = await fetchImpl(withCursor(url, cursor), {
          method: 'GET',
          headers: { Accept: 'text/event-stream' },
          signal,
        });
      } catch (error) {
        if (isAbortError(error, signal)) throw error;
        throw new ReconnectableSseError(error);
      }
      if (!response.ok) throw await buildApiError(response);
      connection = await readConnection(
        response,
        onEvent,
        (lastProcessedSeq) => { cursor = lastProcessedSeq; },
        signal,
      );
      if (connection.cursor !== null) cursor = connection.cursor;
    } catch (error) {
      if (!(error instanceof ReconnectableSseError)) throw error;
    }
    if (connection && connection.reason !== null) {
      return { lastEventId: cursor, reason: connection.reason };
    }
    if (reconnectAttempts >= maxReconnectAttempts) {
      throw new ProductApiError({
        code: 'SSE_RECONNECT_EXHAUSTED',
        message: '事件连接多次中断，请稍后重试',
        retryable: true,
      });
    }
    reconnectAttempts += 1;
    await waitForReconnect(reconnectDelayMs, signal);
  }
}
