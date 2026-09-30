import { buildApiError, streamGet } from './sseClient.js';

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
  if (!response.ok) throw await buildApiError(response);
  if (response.status === 204) return null;
  return response.json();
}

function buildQuery(entries) {
  const query = new URLSearchParams();
  for (const [key, value] of entries) {
    if (value !== null && value !== undefined) query.set(key, String(value));
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

export function getActiveRun(sessionId, { fetchImpl } = {}) {
  return requestJson(
    `/sessions/${encodeURIComponent(sessionId)}/active-run`,
    {},
    fetchImpl,
  );
}

export function createChatRun({
  requestId,
  sessionId,
  content,
  fetchImpl,
}) {
  return requestJson(
    '/completions',
    {
      method: 'POST',
      body: JSON.stringify({
        request_id: requestId,
        session_id: sessionId,
        message: { content },
      }),
    },
    fetchImpl,
  );
}

export function createRegenerateRun({
  requestId,
  sessionId,
  messageId,
  fetchImpl,
}) {
  return requestJson(
    `/sessions/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/regenerate`,
    {
      method: 'POST',
      body: JSON.stringify({ request_id: requestId }),
    },
    fetchImpl,
  );
}

export function streamRunEvents(runId, options = {}) {
  return streamGet(
    `${CHAT_API_ROOT}/runs/${encodeURIComponent(runId)}/events`,
    options,
  );
}

export function cancelRun(runId, { fetchImpl } = {}) {
  return requestJson(
    `/runs/${encodeURIComponent(runId)}/cancel`,
    { method: 'POST' },
    fetchImpl,
  );
}

export function resumeRun(
  runId,
  { interruptId, requestId, resumePayload },
  { fetchImpl } = {},
) {
  return requestJson(
    `/runs/${encodeURIComponent(runId)}/resume`,
    {
      method: 'POST',
      body: JSON.stringify({
        interrupt_id: interruptId,
        request_id: requestId,
        resume_payload: resumePayload,
      }),
    },
    fetchImpl,
  );
}

export function renameSession(sessionId, title, { fetchImpl } = {}) {
  return requestJson(
    `/sessions/${encodeURIComponent(sessionId)}/rename`,
    { method: 'PATCH', body: JSON.stringify({ title }) },
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

export function submitFeedback(messageId, action, { fetchImpl } = {}) {
  return requestJson(
    `/messages/${encodeURIComponent(messageId)}/feedback`,
    { method: 'POST', body: JSON.stringify({ action }) },
    fetchImpl,
  );
}
