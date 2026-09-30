import { useEffect, useMemo, useRef, useState } from 'react';
import { Input, Modal, message as toast } from 'antd';

import { ChatWorkspace } from './components/ChatWorkspace.jsx';
import { SessionSidebar } from './components/SessionSidebar.jsx';
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
} from './services/chatApi.js';

const ACTIVE_STATUSES = new Set([
  'queued',
  'running',
  'recovering',
  'interrupted',
  'cancel_requested',
]);
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled']);
const IDLE_RUN = {
  runId: null,
  sessionId: null,
  messageId: null,
  capabilityId: null,
  status: 'idle',
  recoveryAttempts: 0,
  attempt: 0,
  lastSeq: 0,
  pendingInterrupt: null,
};
const ACTIVE_SESSION_STORAGE_KEY = 'agent-runtime-active-session';

function rememberActiveSession(sessionId) {
  try {
    if (sessionId) localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, sessionId);
    else localStorage.removeItem(ACTIVE_SESSION_STORAGE_KEY);
  } catch {
    // 浏览器禁用存储时仍可在当前页面使用 Runtime。
  }
}

function productErrorMessage(error) {
  return error?.message || '操作失败，请稍后重试';
}

function projectSummary(summary, previous = IDLE_RUN) {
  if (!summary) return { ...IDLE_RUN, sessionId: previous.sessionId };
  return {
    runId: summary.run_id,
    sessionId: summary.session_id,
    messageId: summary.response_message_id,
    capabilityId: previous.capabilityId,
    status: summary.status,
    recoveryAttempts: summary.recovery_attempts ?? 0,
    attempt: previous.runId === summary.run_id ? previous.attempt : 0,
    lastSeq: previous.runId === summary.run_id ? previous.lastSeq : 0,
    pendingInterrupt: summary.pending_interrupt ?? (
      summary.status === 'interrupted' ? previous.pendingInterrupt : null
    ),
  };
}

function draftMessage(messageId) {
  return {
    message_id: messageId,
    role: 'assistant',
    content: '',
    runtime_status: 'running',
    capability_id: null,
    feedback: null,
    client_run_id: messageId,
  };
}

function upsertDraft(messages, messageId, update = {}) {
  const index = messages.findIndex((item) => item.message_id === messageId);
  if (index < 0) return [...messages, { ...draftMessage(messageId), ...update }];
  return messages.map((item, itemIndex) => (
    itemIndex === index ? { ...item, ...update } : item
  ));
}

export default function RuntimeApp() {
  const [sessions, setSessions] = useState([]);
  const [sessionCursor, setSessionCursor] = useState(null);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [activeSessionId, setActiveSessionId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [nextBefore, setNextBefore] = useState(null);
  const [senderValue, setSenderValue] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [run, setRun] = useState(IDLE_RUN);
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameValue, setRenameValue] = useState('');
  const [renameSaving, setRenameSaving] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteSaving, setDeleteSaving] = useState(false);
  const mountedRef = useRef(true);
  const submittingRef = useRef(false);
  const runRef = useRef(IDLE_RUN);
  const runControllerRef = useRef(null);
  const initialSessionRestoreRef = useRef(false);
  const messageRequestRef = useRef(0);
  const sessionRequestRef = useRef({ generation: 0, sequence: 0 });

  const updateRun = (next) => {
    const resolved = typeof next === 'function' ? next(runRef.current) : next;
    runRef.current = resolved;
    if (mountedRef.current) setRun(resolved);
    return resolved;
  };

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      runControllerRef.current?.abort();
    };
  }, []);

  async function refreshSessions({ cursor = null, append = false } = {}) {
    const requestState = sessionRequestRef.current;
    const generation = append ? requestState.generation : requestState.generation + 1;
    if (!append) requestState.generation = generation;
    const requestId = ++requestState.sequence;
    setSessionsLoading(true);
    try {
      const page = await listSessions({ cursor });
      if (
        !mountedRef.current ||
        generation !== sessionRequestRef.current.generation ||
        requestId !== sessionRequestRef.current.sequence
      ) return;
      setSessions((current) => (append ? [...current, ...page.items] : page.items));
      setSessionCursor(page.next_cursor);
      if (!append && !initialSessionRestoreRef.current) {
        initialSessionRestoreRef.current = true;
        let rememberedSessionId = null;
        try {
          rememberedSessionId = localStorage.getItem(ACTIVE_SESSION_STORAGE_KEY);
        } catch {
          rememberedSessionId = null;
        }
        if (page.items.some((item) => item.session_id === rememberedSessionId)) {
          void handleSelectSession(rememberedSessionId);
        }
      }
    } catch (error) {
      if (
        generation === sessionRequestRef.current.generation &&
        requestId === sessionRequestRef.current.sequence
      ) toast.error(productErrorMessage(error));
    } finally {
      if (
        mountedRef.current &&
        generation === sessionRequestRef.current.generation &&
        requestId === sessionRequestRef.current.sequence
      ) setSessionsLoading(false);
    }
  }

  useEffect(() => {
    void refreshSessions();
  }, []);

  async function refreshMessages(sessionId) {
    const requestId = ++messageRequestRef.current;
    setMessagesLoading(true);
    try {
      const page = await listMessages(sessionId);
      if (!mountedRef.current || requestId !== messageRequestRef.current) return;
      setMessages(page.items);
      setNextBefore(page.next_before);
    } catch (error) {
      if (requestId === messageRequestRef.current) toast.error(productErrorMessage(error));
    } finally {
      if (mountedRef.current && requestId === messageRequestRef.current) {
        setMessagesLoading(false);
      }
    }
  }

  function applyRuntimeEvent(expectedRunId, event) {
    const current = runRef.current;
    if (current.runId !== expectedRunId || event.id <= current.lastSeq) return;
    const payload = event.data.payload;
    let next = { ...current, lastSeq: event.id };

    if (event.type === 'run.started' || event.type === 'interrupt.resumed') {
      next = { ...next, status: 'running', pendingInterrupt: null };
    } else if (event.type === 'message.started') {
      if (payload.attempt > current.attempt) {
        next = { ...next, attempt: payload.attempt, messageId: payload.response_message_id };
        setMessages((items) => upsertDraft(items, payload.response_message_id, {
          content: '',
          runtime_status: 'running',
          capability_id: null,
          client_run_id: payload.response_message_id,
        }));
      }
    } else if (event.type === 'message.delta') {
      if (payload.attempt === current.attempt) {
        setMessages((items) => {
          const target = items.find((item) => item.message_id === payload.response_message_id);
          return upsertDraft(items, payload.response_message_id, {
            content: `${target?.content ?? ''}${payload.delta}`,
            runtime_status: 'running',
          });
        });
      }
    } else if (event.type === 'message.finalized') {
      next = {
        ...next,
        messageId: payload.response_message_id,
        capabilityId: payload.capability_id,
      };
      setMessages((items) => upsertDraft(items, payload.response_message_id, {
        runtime_status: payload.runtime_status,
        capability_id: payload.capability_id,
      }));
    } else if (event.type === 'interrupt.required') {
      next = { ...next, status: 'interrupted' };
    } else if (event.type === 'run.completed') {
      next = { ...next, status: 'completed' };
    } else if (event.type === 'run.cancelled') {
      next = { ...next, status: 'cancelled' };
    } else if (event.type === 'run.failed') {
      next = { ...next, status: 'failed' };
      toast.error(payload.message);
    }
    updateRun(next);
  }

  async function restoreInterrupt(runId, sessionId) {
    const active = await getActiveRun(sessionId);
    if (!mountedRef.current || runRef.current.runId !== runId || !active) return;
    updateRun((current) => projectSummary(active, current));
  }

  async function connectRun(summary, { afterSeq = null } = {}) {
    runControllerRef.current?.abort();
    const controller = new AbortController();
    runControllerRef.current = controller;
    const runId = summary.run_id;
    const sessionId = summary.session_id;
    setMessages((items) => upsertDraft(items, summary.response_message_id));
    try {
      const result = await streamRunEvents(runId, {
        afterSeq: afterSeq ?? runRef.current.lastSeq,
        signal: controller.signal,
        onEvent: (event) => applyRuntimeEvent(runId, event),
      });
      if (!mountedRef.current || runRef.current.runId !== runId) return;
      if (result.reason === 'interrupted') {
        await restoreInterrupt(runId, sessionId);
      } else {
        await Promise.all([refreshMessages(sessionId), refreshSessions()]);
      }
    } catch (error) {
      if (error?.name !== 'AbortError') toast.error(productErrorMessage(error));
    } finally {
      if (runControllerRef.current === controller) runControllerRef.current = null;
    }
  }

  function handleCreateSession() {
    if (submittingRef.current || ACTIVE_STATUSES.has(runRef.current.status)) {
      toast.warning('请先结束当前 Run，再新建会话');
      return;
    }
    runControllerRef.current?.abort();
    setActiveSessionId(null);
    rememberActiveSession(null);
    messageRequestRef.current += 1;
    setMessages([]);
    setNextBefore(null);
    setSenderValue('');
    updateRun(IDLE_RUN);
  }

  async function handleSelectSession(sessionId) {
    if (submittingRef.current || ACTIVE_STATUSES.has(runRef.current.status)) {
      toast.warning('请先结束当前 Run，再切换会话');
      return;
    }
    runControllerRef.current?.abort();
    const requestId = ++messageRequestRef.current;
    setActiveSessionId(sessionId);
    rememberActiveSession(sessionId);
    setMessages([]);
    setNextBefore(null);
    setMessagesLoading(true);
    updateRun({ ...IDLE_RUN, sessionId });
    try {
      const active = await getActiveRun(sessionId);
      if (!mountedRef.current || requestId !== messageRequestRef.current) return;
      // 先确认活动 Run，再读取 Parent 历史，避免终态提交窗口读到旧快照。
      const page = await listMessages(sessionId);
      if (!mountedRef.current || requestId !== messageRequestRef.current) return;
      setMessages(page.items);
      setNextBefore(page.next_before);
      if (active) {
        const restored = projectSummary(active, { ...IDLE_RUN, sessionId });
        updateRun(restored);
        if (active.status !== 'interrupted') void connectRun(active, { afterSeq: 0 });
      }
    } catch (error) {
      if (requestId === messageRequestRef.current) toast.error(productErrorMessage(error));
    } finally {
      if (mountedRef.current && requestId === messageRequestRef.current) {
        setMessagesLoading(false);
      }
    }
  }

  async function handleLoadOlder() {
    if (!activeSessionId || !nextBefore) return;
    const requestId = messageRequestRef.current;
    setLoadingOlder(true);
    try {
      const page = await listMessages(activeSessionId, { before: nextBefore });
      if (!mountedRef.current || requestId !== messageRequestRef.current) return;
      setMessages((current) => [...page.items, ...current]);
      setNextBefore(page.next_before);
    } catch (error) {
      if (requestId === messageRequestRef.current) toast.error(productErrorMessage(error));
    } finally {
      if (mountedRef.current) setLoadingOlder(false);
    }
  }

  async function submitRun({
    content = null,
    regenerateId = null,
    optimisticMessageId = null,
  }) {
    if (submittingRef.current || ACTIVE_STATUSES.has(runRef.current.status)) return;
    submittingRef.current = true;
    setSubmitting(true);
    const requestId = crypto.randomUUID();
    let summary = null;
    try {
      summary = regenerateId
        ? await createRegenerateRun({
            requestId,
            sessionId: activeSessionId,
            messageId: regenerateId,
          })
        : await createChatRun({
            requestId,
            sessionId: activeSessionId,
            content,
          });
      setActiveSessionId(summary.session_id);
      rememberActiveSession(summary.session_id);
      updateRun(projectSummary(summary));
      setMessages((items) => upsertDraft(items, summary.response_message_id));
    } catch (error) {
      if (optimisticMessageId) {
        setMessages((items) => items.filter(
          (item) => item.message_id !== optimisticMessageId,
        ));
      }
      toast.error(productErrorMessage(error));
    } finally {
      submittingRef.current = false;
      if (mountedRef.current) setSubmitting(false);
    }
    if (summary) await connectRun(summary, { afterSeq: 0 });
  }

  async function handleSend(value) {
    const content = value.trim();
    if (
      !content ||
      submittingRef.current ||
      ACTIVE_STATUSES.has(runRef.current.status)
    ) return;
    const optimisticMessageId = `pending-user-${crypto.randomUUID()}`;
    setSenderValue('');
    setMessages((current) => [
      ...current,
      {
        message_id: optimisticMessageId,
        role: 'user',
        content,
        runtime_status: null,
        capability_id: null,
        feedback: null,
      },
    ]);
    await submitRun({ content, optimisticMessageId });
  }

  async function handleCancel() {
    if (!run.runId || !ACTIVE_STATUSES.has(run.status) || run.status === 'cancel_requested') return;
    try {
      const summary = await cancelRun(run.runId);
      updateRun((current) => projectSummary(summary, current));
      if (TERMINAL_STATUSES.has(summary.status)) {
        await Promise.all([
          refreshMessages(summary.session_id),
          refreshSessions(),
        ]);
      } else if (!runControllerRef.current) {
        void connectRun(summary);
      }
    } catch (error) {
      toast.error(productErrorMessage(error));
    }
  }

  async function handleResume(approved) {
    const pending = run.pendingInterrupt;
    if (!run.runId || !pending) return;
    try {
      const summary = await resumeRun(run.runId, {
        interruptId: pending.interrupt_id,
        requestId: crypto.randomUUID(),
        resumePayload: { approved },
      });
      updateRun((current) => projectSummary(summary, current));
      await connectRun(summary);
    } catch (error) {
      toast.error(productErrorMessage(error));
    }
  }

  async function handleRegenerate(message) {
    if (
      !activeSessionId ||
      submittingRef.current ||
      ACTIVE_STATUSES.has(runRef.current.status)
    ) return;
    await submitRun({ regenerateId: message.message_id });
  }

  async function handleFeedback(targetMessage, value) {
    const action = value === 'default' ? 'cancel' : value;
    try {
      const result = await submitFeedback(targetMessage.message_id, action);
      setMessages((current) => current.map((item) => (
        item.message_id === targetMessage.message_id
          ? { ...item, feedback: result.feedback }
          : item
      )));
    } catch (error) {
      toast.error(productErrorMessage(error));
    }
  }

  async function handleRename() {
    const title = renameValue.trim();
    if (!activeSessionId || !title) return;
    setRenameSaving(true);
    try {
      await renameSession(activeSessionId, title);
      setRenameOpen(false);
      await refreshSessions();
    } catch (error) {
      toast.error(productErrorMessage(error));
    } finally {
      if (mountedRef.current) setRenameSaving(false);
    }
  }

  async function handleDelete() {
    if (!activeSessionId) return;
    setDeleteSaving(true);
    try {
      await deleteSession(activeSessionId);
      setDeleteOpen(false);
      setActiveSessionId(null);
      rememberActiveSession(null);
      setMessages([]);
      setNextBefore(null);
      updateRun(IDLE_RUN);
      await refreshSessions();
    } catch (error) {
      toast.error(productErrorMessage(error));
    } finally {
      if (mountedRef.current) setDeleteSaving(false);
    }
  }

  const active = submitting || ACTIVE_STATUSES.has(run.status);
  const activeSession = sessions.find((session) => session.session_id === activeSessionId);
  const latestRegeneratableId = useMemo(() => {
    const lastMessage = messages.at(-1);
    return lastMessage?.role === 'assistant' && lastMessage.runtime_status === 'completed'
      ? lastMessage.message_id
      : null;
  }, [messages]);

  return (
    <div className="runtime-shell">
      <SessionSidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        loading={sessionsLoading}
        disabled={active}
        nextCursor={sessionCursor}
        onCreate={handleCreateSession}
        onSelect={handleSelectSession}
        onLoadMore={() => refreshSessions({ cursor: sessionCursor, append: true })}
      />
      <ChatWorkspace
        title={activeSession?.title ?? '新会话'}
        activeSessionId={activeSessionId}
        messages={messages}
        messagesLoading={messagesLoading}
        nextBefore={nextBefore}
        loadingOlder={loadingOlder}
        senderValue={senderValue}
        active={active}
        submitting={submitting}
        run={run}
        latestRegeneratableId={latestRegeneratableId}
        onRename={() => {
          setRenameValue(activeSession?.title ?? '');
          setRenameOpen(true);
        }}
        onDelete={() => setDeleteOpen(true)}
        onLoadOlder={handleLoadOlder}
        onSenderChange={setSenderValue}
        onSend={handleSend}
        onCancel={handleCancel}
        onResume={handleResume}
        onRegenerate={handleRegenerate}
        onFeedback={handleFeedback}
      />

      <Modal
        title="重命名会话"
        open={renameOpen}
        destroyOnHidden
        okText="保存"
        cancelText="取消"
        confirmLoading={renameSaving}
        onOk={handleRename}
        onCancel={() => setRenameOpen(false)}
        okButtonProps={{ disabled: !renameValue.trim() }}
      >
        <label className="field-label" htmlFor="rename-session-title">新会话标题</label>
        <Input
          id="rename-session-title"
          aria-label="新会话标题"
          value={renameValue}
          maxLength={100}
          showCount
          onChange={(event) => setRenameValue(event.target.value)}
          onPressEnter={handleRename}
        />
      </Modal>

      <Modal
        title="确认删除会话"
        open={deleteOpen}
        destroyOnHidden
        okText="确认删除"
        cancelText="取消"
        okButtonProps={{ danger: true }}
        confirmLoading={deleteSaving}
        onOk={handleDelete}
        onCancel={() => setDeleteOpen(false)}
      >
        <p>删除后无法恢复，该会话的消息、运行状态和反馈都会被永久清理。</p>
      </Modal>
    </div>
  );
}
