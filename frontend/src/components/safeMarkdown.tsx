/**
 * 极简安全 Markdown 渲染:纯文本节点构造,永不使用 dangerouslySetInnerHTML,
 * 原始 HTML 一律按字面文本展示。支持:段落、换行、`- ` 列表、`**强调**`、
 * 行内代码。证据、来源与用户文本均按不可信内容渲染。
 */
import React from 'react';

function renderInline(text: string, keyPrefix: string): React.ReactNode[] {
  // 按 `code` 与 **bold** 切分;其余保持字面
  const nodes: React.ReactNode[] = [];
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*)/g;
  const parts = text.split(pattern);
  parts.forEach((part, index) => {
    if (!part) return;
    const key = `${keyPrefix}-${index}`;
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      nodes.push(
        <code key={key} className="rounded bg-code-bg px-1 py-0.5 font-mono text-[0.85em]">
          {part.slice(1, -1)}
        </code>,
      );
    } else if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      nodes.push(<strong key={key}>{part.slice(2, -2)}</strong>);
    } else {
      nodes.push(<React.Fragment key={key}>{part}</React.Fragment>);
    }
  });
  return nodes;
}

export function SafeMarkdown({ text }: { text: string | null | undefined }): React.ReactElement {
  if (!text) return <p className="text-ink-muted">没有正文内容。</p>;
  const lines = text.split(/\r?\n/);
  const blocks: React.ReactNode[] = [];
  let listBuffer: string[] = [];

  const flushList = () => {
    if (listBuffer.length > 0) {
      blocks.push(
        <ul key={`ul-${blocks.length}`} className="my-1.5 list-disc space-y-1 pl-5">
          {listBuffer.map((item, i) => <li key={i}>{renderInline(item, `li-${blocks.length}-${i}`)}</li>)}
        </ul>,
      );
      listBuffer = [];
    }
  };

  lines.forEach((line, index) => {
    const trimmed = line.trim();
    if (trimmed.startsWith('- ') || trimmed.startsWith('· ')) {
      listBuffer.push(trimmed.slice(2));
      return;
    }
    flushList();
    if (!trimmed) return;
    blocks.push(<p key={`p-${index}`} className="my-1.5 leading-relaxed">{renderInline(trimmed, `p-${index}`)}</p>);
  });
  flushList();

  return <div className="text-[0.95rem]">{blocks}</div>;
}
