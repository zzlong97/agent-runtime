const PRODUCT_EVENT_NAMES = new Set(['message', 'error', 'done']);

export class ProductApiError extends Error {
  constructor({ code, message, retryable = false, status = 0 }) {
    super(message);
    this.name = 'ProductApiError';
    this.code = code;
    this.retryable = retryable;
    this.status = status;
  }
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

function parseFrame(frame) {
  let eventName = 'message';
  const dataLines = [];
  for (const line of frame.replaceAll('\r\n', '\n').split('\n')) {
    if (line.startsWith('event:')) {
      eventName = line.slice('event:'.length).trim();
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).trimStart());
    }
  }
  if (!PRODUCT_EVENT_NAMES.has(eventName) || dataLines.length === 0) {
    return null;
  }
  try {
    return {
      type: eventName,
      data: JSON.parse(dataLines.join('\n')),
    };
  } catch {
    throw new ProductApiError({
      code: 'SSE_EVENT_INVALID',
      message: '服务器返回了无法解析的事件数据',
      retryable: true,
    });
  }
}

export async function streamPost(
  url,
  {
    body,
    signal,
    onEvent = () => {},
    fetchImpl = globalThis.fetch,
  } = {},
) {
  const requestInit = {
    method: 'POST',
    signal,
  };
  if (body !== undefined) {
    requestInit.headers = { 'Content-Type': 'application/json' };
    requestInit.body = JSON.stringify(body);
  }

  const response = await fetchImpl(url, requestInit);
  if (!response.ok) {
    throw await buildApiError(response);
  }
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
  let terminalEventSeen = false;
  const dispatch = async (event) => {
    if (event.type === 'done') {
      terminalEventSeen = true;
    }
    await onEvent(event);
  };
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    buffer = buffer.replaceAll('\r\n', '\n');

    let boundary = buffer.indexOf('\n\n');
    while (boundary >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const event = parseFrame(frame);
      if (event) {
        await dispatch(event);
      }
      boundary = buffer.indexOf('\n\n');
    }
    if (done) {
      break;
    }
  }

  const finalEvent = parseFrame(buffer.trim());
  if (finalEvent) {
    await dispatch(finalEvent);
  }
  if (!terminalEventSeen) {
    throw new ProductApiError({
      code: 'SSE_STREAM_INCOMPLETE',
      message: '事件流在完成前意外中断，请重试',
      retryable: true,
      status: response.status,
    });
  }
}
