import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { MessageList } from './MessageList.jsx';


describe('MessageList 动态能力标签', () => {
  it('把未知但合法的 capability_id 作为普通标签原样展示', () => {
    render(
      <MessageList
        messages={[{
          message_id: '00000000-0000-0000-0000-000000000001',
          role: 'assistant',
          content: '天气查询结果',
          runtime_status: 'completed',
          capability_id: 'weather_lookup',
          feedback: null,
        }]}
        running={false}
        latestRegeneratableId={null}
        onRegenerate={vi.fn()}
        onFeedback={vi.fn()}
      />,
    );

    expect(screen.getByText('weather_lookup')).toBeInTheDocument();
    expect(screen.getByText('天气查询结果')).toBeInTheDocument();
  });
});
