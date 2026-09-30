import { CommentOutlined, PlusOutlined } from '@ant-design/icons';
import { Conversations } from '@ant-design/x';
import { Button, Skeleton, Typography } from 'antd';

export function SessionSidebar({
  sessions,
  activeSessionId,
  loading,
  disabled,
  nextCursor,
  onCreate,
  onSelect,
  onLoadMore,
}) {
  return (
    <aside className="session-sidebar" aria-label="会话列表">
      <div className="brand-block">
        <div className="brand-mark">AR</div>
        <div>
          <Typography.Title level={4}>AgentRuntime</Typography.Title>
          <Typography.Text type="secondary">Stage 2.5 Runtime</Typography.Text>
        </div>
      </div>

      <Button
        className="new-session-button"
        type="primary"
        size="large"
        icon={<PlusOutlined />}
        onClick={onCreate}
        disabled={disabled}
        block
      >
        新建会话
      </Button>

      <div className="session-list-scroll">
        {loading && sessions.length === 0 ? (
          <Skeleton active paragraph={{ rows: 5 }} title={false} />
        ) : sessions.length === 0 ? (
          <div className="empty-sessions">
            <CommentOutlined />
            <span>还没有历史会话</span>
          </div>
        ) : (
          <Conversations
            activeKey={activeSessionId ?? undefined}
            items={sessions.map((session) => ({
              key: session.session_id,
              label: session.title,
              disabled,
            }))}
            onActiveChange={onSelect}
          />
        )}
      </div>

      {nextCursor ? (
        <Button
          type="text"
          loading={loading}
          disabled={disabled}
          onClick={onLoadMore}
          aria-label="加载更多会话"
          block
        >
          加载更多
        </Button>
      ) : null}
    </aside>
  );
}
