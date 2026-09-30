import { DeleteOutlined, EditOutlined, StopOutlined } from '@ant-design/icons';
import { Sender } from '@ant-design/x';
import { Button, Space, Spin, Typography } from 'antd';

import { MessageList } from './MessageList.jsx';
import { RunStatusPanel } from './RunStatusPanel.jsx';

export function ChatWorkspace({
  title,
  activeSessionId,
  messages,
  messagesLoading,
  nextBefore,
  loadingOlder,
  senderValue,
  active,
  submitting,
  run,
  latestRegeneratableId,
  onRename,
  onDelete,
  onLoadOlder,
  onSenderChange,
  onSend,
  onCancel,
  onResume,
  onRegenerate,
  onFeedback,
}) {
  return (
    <main className="chat-workspace">
      <header className="chat-header">
        <div className="chat-title-block">
          <Typography.Title level={4}>{title}</Typography.Title>
          <Typography.Text type="secondary">
            {activeSessionId ? '服务端活动分支' : '首条消息发送后创建 Session'}
          </Typography.Text>
        </div>
        <Space>
          <Button
            icon={<StopOutlined />}
            aria-label="取消当前运行"
            title="取消当前运行"
            disabled={!active || !run.runId || run.status === 'cancel_requested'}
            onClick={onCancel}
          />
          <Button
            icon={<EditOutlined />}
            aria-label="重命名当前会话"
            title="重命名当前会话"
            disabled={!activeSessionId || active}
            onClick={onRename}
          />
          <Button
            danger
            icon={<DeleteOutlined />}
            aria-label="删除当前会话"
            title="删除当前会话"
            disabled={!activeSessionId || active}
            onClick={onDelete}
          />
        </Space>
      </header>

      <RunStatusPanel run={run} onResume={onResume} />

      {nextBefore ? (
        <div className="older-messages-control">
          <Button type="link" loading={loadingOlder} onClick={onLoadOlder}>
            加载更早消息
          </Button>
        </div>
      ) : null}

      <div className="conversation-body">
        {messagesLoading ? (
          <div className="centered-state"><Spin description="正在读取活动分支" /></div>
        ) : (
          <MessageList
            messages={messages}
            running={active}
            latestRegeneratableId={latestRegeneratableId}
            onRegenerate={onRegenerate}
            onFeedback={onFeedback}
          />
        )}
      </div>

      <div className="sender-shell">
        <Sender
          value={senderValue}
          onChange={onSenderChange}
          onSubmit={onSend}
          onCancel={onCancel}
          loading={active}
          disabled={messagesLoading || submitting || run.status === 'interrupted'}
          placeholder={
            submitting
              ? '正在提交运行…'
              : '输入消息，Enter 发送，Shift+Enter 换行'
          }
          submitType="enter"
          autoSize={{ minRows: 1, maxRows: 6 }}
        />
        <div className="sender-hint">内容由模型生成，请核对重要信息。</div>
      </div>
    </main>
  );
}
