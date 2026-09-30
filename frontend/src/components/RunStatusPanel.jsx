import { Button, Collapse, Descriptions, Space, Tag, Typography } from 'antd';

const STATUS_COLORS = {
  queued: 'default',
  running: 'processing',
  recovering: 'warning',
  interrupted: 'warning',
  cancel_requested: 'warning',
  completed: 'success',
  failed: 'error',
  cancelled: 'default',
  idle: 'default',
};

export function RunStatusPanel({ run, onResume }) {
  const prompt = run.pendingInterrupt?.interrupt_payload?.prompt;
  const items = [
    {
      key: 'runtime-status',
      label: (
        <span className="run-panel-label">
          运行状态
          <Tag color={STATUS_COLORS[run.status]}>{run.status}</Tag>
          {run.attempt > 0 ? <Tag>attempt {run.attempt}</Tag> : null}
        </span>
      ),
      children: (
        <>
          <Descriptions
            className="run-descriptions"
            size="small"
            column={{ xs: 1, sm: 2, lg: 5 }}
            items={[
              { key: 'run_id', label: 'Run ID', children: <code>{run.runId ?? '—'}</code> },
              { key: 'session_id', label: 'Session ID', children: <code>{run.sessionId ?? '—'}</code> },
              { key: 'message_id', label: '消息 ID', children: <code>{run.messageId ?? '—'}</code> },
              { key: 'capability_id', label: 'Capability', children: <code>{run.capabilityId ?? '—'}</code> },
              { key: 'recovery_attempts', label: '恢复次数', children: <code>{run.recoveryAttempts}</code> },
            ]}
          />
          {run.status === 'interrupted' && run.pendingInterrupt ? (
            <div className="interrupt-action" aria-label="中断操作">
              <Typography.Text>{typeof prompt === 'string' ? prompt : '运行等待人工确认。'}</Typography.Text>
              <Space>
                <Button type="primary" onClick={() => onResume(true)}>确认并继续</Button>
                <Button onClick={() => onResume(false)}>拒绝并继续</Button>
              </Space>
            </div>
          ) : null}
        </>
      ),
    },
  ];

  return (
    <section className="run-status-panel" aria-label="运行状态">
      <Collapse ghost size="small" defaultActiveKey={['runtime-status']} items={items} />
    </section>
  );
}
