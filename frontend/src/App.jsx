import { useEffect, useMemo, useRef, useState } from 'react';
import { Input, Modal, message as toast } from 'antd';

import { ChatWorkspace } from './components/ChatWorkspace.jsx';
import { SessionSidebar } from './components/SessionSidebar.jsx';
import {
  deleteSession,
  listMessages,
  listSessions,
  regenerateMessage,
  renameSession,
  stopSession,
  streamCompletion,
  submitFeedback,
} from './services/chatApi.js';

const IDLE_RUN = {
  sessionId: null,
  messageId: null,
  capabilityId: null,
  status: 'idle',
};

function productErrorMessage(error) {
  return error?.message || '操作失败，请稍后重试';
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
  const [running, setRunning] = useState(false);
  const [run, setRun] = useState(IDLE_RUN);
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameValue, setRenameValue] = useState('');
  const [renameSaving, setRenameSaving] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteSaving, setDeleteSaving] = useState(false);
  const mountedRef = useRef(true);
  const runControllerRef = useRef(null);
  const messageRequestRef = useRef(0);
  const sessionRequestRef = useRef({ generation: 0, sequence: 0 });

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      runControllerRef.current?.abort();
    };
  }, []);

  async function refreshSessions({ cursor = null, append = false } = {}) {
    const requestState = sessionRequestRef.current;
    const generation = append
      ? requestState.generation
      : requestState.generation + 1;
    if (!append) requestState.generation = generation;
    const requestId = ++requestState.sequence;
    setSessionsLoading(true);
    try {
      const page = await listSessions({ cursor });
      if (
        !mountedRef.current ||
        generation !== sessionRequestRef.current.generation ||
        requestId !== sessionRequestRef.current.sequence
      ) {
        return;
      }
      setSessions((current) => (append ? [...current, ...page.items] : page.items));
      setSessionCursor(page.next_cursor);
    } catch (error) {
      if (
        generation === sessionRequestRef.current.generation &&
        requestId === sessionRequestRef.current.sequence
      ) {
        toast.error(productErrorMessage(error));
      }
    } finally {
      if (
        mountedRef.current &&
        generation === sessionRequestRef.current.generation &&
        requestId === sessionRequestRef.current.sequence
      ) {
        setSessionsLoading(false);
      }
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
      if (requestId === messageRequestRef.current) {
        toast.error(productErrorMessage(error));
      }
    } finally {
      if (mountedRef.current && requestId === messageRequestRef.current) {
        setMessagesLoading(false);
      }
    }
  }

  function handleCreateSession() {
    if (running) {
      toast.warning('请先停止当前回答，再新建会话');
      return;
    }
    setActiveSessionId(null);
    messageRequestRef.current += 1;
    setMessagesLoading(false);
    setLoadingOlder(false);
    setMessages([]);
    setNextBefore(null);
    setSenderValue('');
    setRun(IDLE_RUN);
  }

  async function handleSelectSession(sessionId) {
    if (running) {
      toast.warning('请先停止当前回答，再切换会话');
      return;
    }
    setActiveSessionId(sessionId);
    setLoadingOlder(false);
    setMessages([]);
    setNextBefore(null);
    setRun({ ...IDLE_RUN, sessionId });
    await refreshMessages(sessionId);
  }

  async function handleLoadOlder() {
    if (!activeSessionId || !nextBefore) return;
    const requestId = messageRequestRef.current;
    const sessionId = activeSessionId;
    setLoadingOlder(true);
    try {
      const page = await listMessages(sessionId, { before: nextBefore });
      if (!mountedRef.current || requestId !== messageRequestRef.current) return;
      setMessages((current) => [...page.items, ...current]);
      setNextBefore(page.next_before);
    } catch (error) {
      if (requestId === messageRequestRef.current) {
        toast.error(productErrorMessage(error));
      }
    } finally {
      if (mountedRef.current && requestId === messageRequestRef.current) {
        setLoadingOlder(false);
      }
    }
  }

  function updateStreamingMessage(clientRunId, event) {
    if (event.type === 'error') {
      setRun((current) => ({ ...current, status: 'failed' }));
      toast.error(event.data.message);
      return;
    }
    const eventRun = {
      sessionId: event.data.session_id,
      messageId: event.data.message_id,
      capabilityId: event.data.capability_id,
      status: event.type === 'done' ? event.data.status : 'running',
    };
    setRun(eventRun);
    setActiveSessionId(event.data.session_id);
    setMessages((current) =>
      current.map((item) =>
        item.client_run_id === clientRunId
          ? {
              ...item,
              message_id: event.data.message_id,
              content:
                event.type === 'message'
                  ? `${item.content}${event.data.delta}`
                  : item.content,
              runtime_status:
                event.type === 'done' ? event.data.status : 'running',
              capability_id: event.data.capability_id,
            }
          : item,
      ),
    );
  }

  async function runSse({ sessionId, content = null, regenerateId = null }) {
    const clientRunId = crypto.randomUUID();
    const controller = new AbortController();
    runControllerRef.current = controller;
    let resolvedSessionId = sessionId;
    setRunning(true);
    setRun({
      sessionId,
      messageId: null,
      capabilityId: null,
      status: 'running',
    });
    setMessages((current) => [
      ...current,
      {
        message_id: `pending-assistant-${clientRunId}`,
        role: 'assistant',
        content: '',
        runtime_status: 'running',
        capability_id: null,
        feedback: null,
        client_run_id: clientRunId,
      },
    ]);

    const onEvent = async (event) => {
      if (event.data.session_id) resolvedSessionId = event.data.session_id;
      updateStreamingMessage(clientRunId, event);
    };

    try {
      if (regenerateId) {
        await regenerateMessage({
          sessionId,
          messageId: regenerateId,
          signal: controller.signal,
          onEvent,
        });
      } else {
        await streamCompletion({
          sessionId,
          content,
          signal: controller.signal,
          onEvent,
        });
      }
    } catch (error) {
      if (error?.name !== 'AbortError') {
        setRun((current) => ({ ...current, status: 'failed' }));
        toast.error(productErrorMessage(error));
      }
    } finally {
      if (resolvedSessionId && mountedRef.current) {
        await Promise.all([
          refreshMessages(resolvedSessionId),
          refreshSessions(),
        ]);
      }
      if (mountedRef.current) setRunning(false);
      if (runControllerRef.current === controller) runControllerRef.current = null;
    }
  }

  async function handleSend(value) {
    const content = value.trim();
    if (!content || running) return;
    setSenderValue('');
    setMessages((current) => [
      ...current,
      {
        message_id: `pending-user-${crypto.randomUUID()}`,
        role: 'user',
        content,
        runtime_status: null,
        capability_id: null,
        feedback: null,
      },
    ]);
    await runSse({ sessionId: activeSessionId, content });
  }

  async function handleStop() {
    const sessionId = run.sessionId ?? activeSessionId;
    if (!running || !sessionId) return;
    setRun((current) => ({ ...current, status: 'stopping' }));
    try {
      await stopSession(sessionId);
    } catch (error) {
      toast.error(productErrorMessage(error));
    }
  }

  async function handleRegenerate(message) {
    if (!activeSessionId || running) return;
    await runSse({
      sessionId: activeSessionId,
      regenerateId: message.message_id,
    });
  }

  async function handleFeedback(targetMessage, value) {
    const action = value === 'default' ? 'cancel' : value;
    try {
      const result = await submitFeedback(targetMessage.message_id, action);
      setMessages((current) =>
        current.map((item) =>
          item.message_id === targetMessage.message_id
            ? { ...item, feedback: result.feedback }
            : item,
        ),
      );
    } catch (error) {
      toast.error(productErrorMessage(error));
    }
  }

  function openRename() {
    const current = sessions.find((session) => session.session_id === activeSessionId);
    setRenameValue(current?.title ?? '');
    setRenameOpen(true);
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
    messageRequestRef.current += 1;
    setMessagesLoading(false);
    setLoadingOlder(false);
    setDeleteSaving(true);
    try {
      await deleteSession(activeSessionId);
      setDeleteOpen(false);
      setActiveSessionId(null);
      setMessages([]);
      setNextBefore(null);
      setRun(IDLE_RUN);
      await refreshSessions();
    } catch (error) {
      toast.error(productErrorMessage(error));
    } finally {
      if (mountedRef.current) setDeleteSaving(false);
    }
  }

  const activeSession = sessions.find(
    (session) => session.session_id === activeSessionId,
  );
  const latestRegeneratableId = useMemo(() => {
    const lastMessage = messages.at(-1);
    return lastMessage?.role === 'assistant' &&
      lastMessage.runtime_status === 'completed'
      ? lastMessage.message_id
      : null;
  }, [messages]);

  return (
    <div className="runtime-shell">
      <SessionSidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        loading={sessionsLoading}
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
        running={running}
        run={run}
        latestRegeneratableId={latestRegeneratableId}
        onRename={openRename}
        onDelete={() => setDeleteOpen(true)}
        onLoadOlder={handleLoadOlder}
        onSenderChange={setSenderValue}
        onSend={handleSend}
        onStop={handleStop}
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
