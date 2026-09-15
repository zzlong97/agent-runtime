import {
  DislikeFilled,
  DislikeOutlined,
  LikeFilled,
  LikeOutlined,
  ReloadOutlined,
  RobotOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { Bubble } from '@ant-design/x';
import { Avatar, Button, Tag } from 'antd';

import { MarkdownMessage } from './MarkdownMessage.jsx';

const STATUS_LABELS = {
  completed: '已完成',
  unsupported: '暂不支持',
  incomplete: '未完成',
  stopped: '已停止',
  failed: '失败',
  running: '生成中',
};

export function MessageList({
  messages,
  running,
  latestRegeneratableId,
  onRegenerate,
  onFeedback,
}) {
  const items = messages.map((message) => {
    const isAssistant = message.role === 'assistant';
    const canFeedback =
      isAssistant &&
      message.runtime_status === 'completed' &&
      !message.client_run_id;
    const footer = isAssistant ? (
      <div className="message-footer">
        <div className="message-metadata">
          {message.runtime_status ? (
            <Tag>{STATUS_LABELS[message.runtime_status] ?? message.runtime_status}</Tag>
          ) : null}
          {message.capability_id ? <Tag color="blue">{message.capability_id}</Tag> : null}
        </div>
        {canFeedback ? (
          <div className="message-actions">
            <div aria-label="回答反馈">
              <Button
                type="text"
                size="small"
                icon={message.feedback === 'like' ? <LikeFilled /> : <LikeOutlined />}
                aria-label="喜欢此回答"
                aria-pressed={message.feedback === 'like'}
                title="喜欢"
                onClick={() =>
                  onFeedback(message, message.feedback === 'like' ? 'default' : 'like')
                }
              />
              <Button
                type="text"
                size="small"
                icon={
                  message.feedback === 'dislike' ? <DislikeFilled /> : <DislikeOutlined />
                }
                aria-label="不喜欢此回答"
                aria-pressed={message.feedback === 'dislike'}
                title="不喜欢"
                onClick={() =>
                  onFeedback(
                    message,
                    message.feedback === 'dislike' ? 'default' : 'dislike',
                  )
                }
              />
            </div>
            {message.message_id === latestRegeneratableId ? (
              <Button
                type="text"
                size="small"
                icon={<ReloadOutlined />}
                aria-label="重新生成最新回答"
                title="重新生成"
                disabled={running}
                onClick={() => onRegenerate(message)}
              />
            ) : null}
          </div>
        ) : null}
      </div>
    ) : null;

    return {
      key: message.message_id,
      role: message.role,
      placement: isAssistant ? 'start' : 'end',
      avatar: (
        <Avatar
          className={isAssistant ? 'assistant-avatar' : 'user-avatar'}
          icon={isAssistant ? <RobotOutlined /> : <UserOutlined />}
        />
      ),
      variant: isAssistant ? 'borderless' : 'filled',
      content: <MarkdownMessage content={message.content || (running ? '正在思考…' : '')} />,
      footer,
      loading: isAssistant && running && !message.content,
    };
  });

  return (
    <div className="message-list" aria-live="polite">
      <Bubble.List items={items} />
      {messages.length === 0 ? (
        <div className="welcome-panel">
          <div className="welcome-icon"><LikeOutlined /></div>
          <h2>开始一段新对话</h2>
          <p>可以进行普通聊天，也可以让我把英文忠实翻译成中文。</p>
        </div>
      ) : null}
    </div>
  );
}
