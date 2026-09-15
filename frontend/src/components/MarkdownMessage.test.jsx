import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { MarkdownMessage } from './MarkdownMessage.jsx';

describe('MarkdownMessage', () => {
  it('渲染 Markdown，但不执行原始 HTML', () => {
    const { container } = render(
      <MarkdownMessage content={'**安全文本**\n\n<script>window.hacked = true</script>'} />,
    );

    expect(screen.getByText('安全文本')).toHaveTextContent('安全文本');
    expect(container.querySelector('script')).not.toBeInTheDocument();
    expect(container).toHaveTextContent('<script>window.hacked = true</script>');
    expect(window.hacked).toBeUndefined();
  });

  it('为外部链接添加新窗口与隔离属性', () => {
    render(
      <MarkdownMessage content={'[外部站点](https://example.com/docs)'} />,
    );

    const link = screen.getByRole('link', { name: '外部站点' });
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('不会把危险协议保留为可点击地址', () => {
    render(<MarkdownMessage content={'[危险链接](javascript:alert(1))'} />);

    const link = screen.getByText('危险链接').closest('a');
    expect(link).not.toHaveAttribute('href', 'javascript:alert(1)');
  });

  it('对带语言标记的代码块执行语法高亮', () => {
    const { container } = render(
      <MarkdownMessage content={'```python\nprint("你好")\n```'} />,
    );

    expect(container.querySelector('code.language-python')).toHaveClass('hljs');
    expect(container.querySelector('code .hljs-built_in')).toHaveTextContent('print');
  });
});
