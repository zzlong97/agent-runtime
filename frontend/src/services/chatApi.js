import { buildApiError, streamPost } from './sseClient.js';

const CHAT_API_ROOT = '/api/v1/chat';

async function requestJson(path, options = {}, fetchImpl = globalThis.fetch) {
  const requestOptions = { ...options };
  if (requestOptions.body !== undefined) {
    requestOptions.headers = {
      'Content-Type': 'application/json',
      ...requestOptions.headers,
    };
  }
  const response = await fetchImpl(`${CHAT_API_ROOT}${path}`, requestOptions);
  if (!response.ok) {
    throw await buildApiError(response);
  }
  if (response.status === 204) {
    return null;
  }
  return response.json();
}

function buildQuery(entries) {
  const query = new URLSearchParams();
  for (const [key, value] of entries) {
    if (value !== null && value !== undefined) {
      query.set(key, String(value));
    }
  }
  return query.toString();
}

export function listSessions({ cursor = null, limit = 20, fetchImpl } = {}) {
  const query = buildQuery([
    ['limit', limit],
    ['cursor', cursor],
  ]);
  return requestJson(`/sessions?${query}`, {}, fetchImpl);
}

export function listMessages(
  sessionId,
  { before = null, limit = 50, fetchImpl } = {},
) {
  const query = buildQuery([
    ['limit', limit],
    ['before', before],
  ]);
  return requestJson(
    `/sessions/${encodeURIComponent(sessionId)}/messages?${query}`,
    {},
    fetchImpl,
  );
}

export function renameSession(sessionId, title, { fetchImpl } = {}) {
  return requestJson(
    `/sessions/${encodeURIComponent(sessionId)}/rename`,
    {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    },
    fetchImpl,
  );
}

export function deleteSession(sessionId, { fetchImpl } = {}) {
  return requestJson(
    `/sessions/${encodeURIComponent(sessionId)}`,
    { method: 'DELETE' },
    fetchImpl,
  );
}

export function stopSession(sessionId, { fetchImpl } = {}) {
  return requestJson(
    `/sessions/${encodeURIComponent(sessionId)}/stop`,
    { method: 'POST' },
    fetchImpl,
  );
}

export function submitFeedback(messageId, action, { fetchImpl } = {}) {
  return requestJson(
    `/messages/${encodeURIComponent(messageId)}/feedback`,
    {
      method: 'POST',
      body: JSON.stringify({ action }),
    },
    fetchImpl,
  );
}

export function streamCompletion({
  sessionId,
  content,
  signal,
  onEvent,
  fetchImpl,
}) {
  return streamPost(`${CHAT_API_ROOT}/completions`, {
    body: {
      session_id: sessionId,
      message: { content },
    },
    signal,
    onEvent,
    fetchImpl,
  });
}

export function regenerateMessage({
  sessionId,
  messageId,
  signal,
  onEvent,
  fetchImpl,
}) {
  return streamPost(
    `${CHAT_API_ROOT}/sessions/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/regenerate`,
    { signal, onEvent, fetchImpl },
  );
}
