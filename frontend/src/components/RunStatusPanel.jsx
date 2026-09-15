import { Collapse, Descriptions, Tag } from 'antd';

const STATUS_COLORS = {
  running: 'processing',
  stopping: 'warning',
  completed: 'success',
  unsupported: 'default',
  stopped: 'warning',
  failed: 'error',
  idle: 'default',
};

export function RunStatusPanel({ run }) {
  const items = [
    {
      key: 'runtime-status',
      label: (
        <span className="run-panel-label">
          运行状态
          <Tag color={STATUS_COLORS[run.status]}>{run.status}</Tag>
        </span>
      ),
      children: (
        <Descriptions
          className="run-descriptions"
          size="small"
          column={{ xs: 1, sm: 2, lg: 4 }}
          items={[
            {
              key: 'session_id',
              label: 'session_id',
              children: <code>{run.sessionId ?? '—'}</code>,
            },
            {
              key: 'message_id',
              label: 'message_id',
              children: <code>{run.messageId ?? '—'}</code>,
            },
            {
              key: 'capability_id',
              label: 'capability_id',
              children: <code>{run.capabilityId ?? '—'}</code>,
            },
            {
              key: 'status',
              label: 'status',
              children: <code>{run.status}</code>,
            },
          ]}
        />
      ),
    },
  ];

  return (
    <section className="run-status-panel" aria-label="运行状态">
      <Collapse ghost size="small" defaultActiveKey={['runtime-status']} items={items} />
    </section>
  );
}
