import { useState } from 'react';
import { Button, Collapse, Descriptions, Input, Space, Tag, Typography } from 'antd';

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

export function RunStatusPanel({ run, onResume, resuming = false }) {
  const [answer, setAnswer] = useState('');
  const prompt = run.pendingInterrupt?.interrupt_payload?.prompt;
  const textRequired = run.pendingInterrupt?.interrupt_payload?.kind === 'text';
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
              <Typography.Text>{typeof prompt === 'string' ? prompt : '运行等待人工输入。'}</Typography.Text>
              {textRequired ? (
                <Space orientation="vertical" className="interrupt-text">
                  <Input.TextArea aria-label="补充文本" value={answer} disabled={resuming}
                    onChange={(event) => setAnswer(event.target.value)} autoSize={{ minRows: 2, maxRows: 6 }} />
                  <Button type="primary" loading={resuming} disabled={resuming || !answer.trim()}
                    onClick={() => onResume({ answer })}>提交补充并继续</Button>
                </Space>
              ) : (
                <Space>
                  <Button type="primary" disabled={resuming} loading={resuming} onClick={() => onResume({ approved: true })}>确认并继续</Button>
                  <Button disabled={resuming} onClick={() => onResume({ approved: false })}>拒绝并继续</Button>
                </Space>
              )}
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
