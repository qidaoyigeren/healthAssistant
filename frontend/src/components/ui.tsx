/**
 * 基础 UI:状态徽标(文字+图标,不靠颜色单独表义)、异步状态(加载/空/失败)、
 * 时间显示、抽屉、确认弹窗、aria-live。
 */
import React, { useEffect, useRef } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import {
  AlertTriangle, CheckCircle2, CircleSlash, Clock, HelpCircle,
  Loader2, RefreshCw, ShieldAlert, X,
} from 'lucide-react';
import { formatTime, usePreferences } from '../hooks/usePreferences';

// ---- 状态徽标 ---------------------------------------------------------------

export const SEVERITY_LABELS: Record<string, string> = {
  contraindicated: '禁忌级提示',
  major: '严重',
  moderate: '需关注',
  minor: '轻微',
  unknown: '未知',
};

export function SeverityBadge({ severity }: { severity: string | null | undefined }): React.ReactElement {
  if (severity == null) {
    return (
      <Badge tone="neutral" icon={<HelpCircle size={13} aria-hidden />}>
        严重度未记录
      </Badge>
    );
  }
  const known = severity in SEVERITY_LABELS;
  const label = known ? SEVERITY_LABELS[severity] : undefined;
  if (!known) {
    // 未识别的新枚举:显示原值 +「未识别状态」,不自动归为低风险
    return (
      <Badge tone="caution" icon={<HelpCircle size={13} aria-hidden />}>
        {severity} · 未识别状态
      </Badge>
    );
  }
  if (severity === 'contraindicated' || severity === 'major') {
    return <Badge tone="danger" icon={<ShieldAlert size={13} aria-hidden />}>{label}</Badge>;
  }
  if (severity === 'moderate') {
    return <Badge tone="caution" icon={<AlertTriangle size={13} aria-hidden />}>{label}</Badge>;
  }
  return <Badge tone="neutral" icon={<CircleSlash size={13} aria-hidden />}>{label}</Badge>;
}

export function ConfidenceBadge({ confidence }: { confidence: string | null | undefined }): React.ReactElement {
  if (confidence == null) {
    return <Badge tone="neutral">置信度未记录</Badge>;
  }
  const labels: Record<string, string> = { high: '置信度:高', medium: '置信度:中', low: '置信度:低', unknown: '置信度:未知' };
  const label = labels[confidence] ?? `置信度:${confidence}`;
  const tone = confidence === 'high' ? 'neutral' : 'caution';
  return <Badge tone={tone} icon={<HelpCircle size={13} aria-hidden />}>{label}</Badge>;
}

type Tone = 'neutral' | 'caution' | 'danger' | 'primary';

export function Badge({ children, tone = 'neutral', icon }: {
  children: React.ReactNode; tone?: Tone; icon?: React.ReactNode;
}): React.ReactElement {
  const tones: Record<Tone, string> = {
    neutral: 'bg-surface-alt text-ink-secondary border-border',
    caution: 'bg-caution-soft text-caution border-caution/30',
    danger: 'bg-danger-soft text-danger border-danger/30',
    primary: 'bg-primary-soft text-primary-strong border-primary/30',
  };
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${tones[tone]}`}>
      {icon}
      {children}
    </span>
  );
}

export function FactStatusBadge({ fact }: { fact: { status: string; verification_status: string } }): React.ReactElement {
  const parts: React.ReactNode[] = [];
  if (fact.verification_status === 'verified') {
    parts.push(<Badge key="v" tone="primary" icon={<CheckCircle2 size={13} aria-hidden />}>记录已核实</Badge>);
  } else if (fact.verification_status === 'disputed') {
    parts.push(<Badge key="d" tone="caution" icon={<AlertTriangle size={13} aria-hidden />}>存在待核实信息</Badge>);
  } else {
    parts.push(<Badge key="r" tone="neutral">按报告记录</Badge>);
  }
  if (fact.status === 'retracted') {
    parts.push(<Badge key="x" tone="neutral" icon={<CircleSlash size={13} aria-hidden />}>已撤回</Badge>);
  } else if (fact.status === 'superseded') {
    parts.push(<Badge key="s" tone="neutral">已被新版本替代</Badge>);
  }
  return <span className="inline-flex flex-wrap items-center gap-1.5">{parts}</span>;
}

// ---- 时间 -------------------------------------------------------------------

export function TimeText({ iso, prefix }: { iso: string | null | undefined; prefix?: string }): React.ReactElement {
  const prefs = usePreferences();
  return (
    <span className="whitespace-nowrap font-mono text-[0.8em] text-ink-secondary">
      {prefix}{formatTime(iso, prefs)}
    </span>
  );
}

// ---- 异步状态 ---------------------------------------------------------------

export function LoadingBlock({ label = '加载中…' }: { label?: string }): React.ReactElement {
  return (
    <div role="status" className="flex items-center gap-2 p-6 text-ink-secondary">
      <Loader2 size={16} className="animate-spin" aria-hidden />
      {label}
    </div>
  );
}

export function SkeletonList({ rows = 3 }: { rows?: number }): React.ReactElement {
  return (
    <div role="status" aria-label="内容加载中" className="space-y-2 p-4">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="h-12 animate-pulse rounded-lg bg-surface-alt" />
      ))}
    </div>
  );
}

export function EmptyState({ title, hint, children }: {
  title: string; hint?: string; children?: React.ReactNode;
}): React.ReactElement {
  return (
    <div className="rounded-card border border-dashed border-border-strong bg-surface p-8 text-center">
      <p className="font-medium">{title}</p>
      {hint && <p className="mx-auto mt-1 max-w-md text-sm text-ink-secondary">{hint}</p>}
      {children && <div className="mt-4 flex justify-center gap-2">{children}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry, title = '读取失败' }: {
  error: unknown; onRetry?: () => void; title?: string;
}): React.ReactElement {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div role="alert" className="rounded-card border border-danger/30 bg-danger-soft/40 p-5">
      <p className="font-medium text-danger">{title}</p>
      <p className="mt-1 text-sm text-ink-secondary">{message}</p>
      {onRetry && (
        <button type="button" onClick={onRetry}
          className="mt-3 inline-flex items-center gap-1.5 rounded-lg border border-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-alt">
          <RefreshCw size={14} aria-hidden /> 重试
        </button>
      )}
    </div>
  );
}

// ---- aria-live(异步状态适度提示)---------------------------------------------

export function LiveAnnouncement({ message }: { message: string | null }): React.ReactElement {
  return (
    <div aria-live="polite" role="status" className="sr-only">
      {message ?? ''}
    </div>
  );
}

// ---- 抽屉 / 弹窗 ---------------------------------------------------------------

export function DetailDrawer({ open, onOpenChange, title, children }: {
  open: boolean; onOpenChange: (open: boolean) => void;
  title: React.ReactNode; children: React.ReactNode;
}): React.ReactElement {
  const previousFocus = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (open) {
      previousFocus.current = document.activeElement as HTMLElement | null;
      return () => {
        // 关闭后焦点返回触发元素
        previousFocus.current?.focus();
      };
    }
    return undefined;
  }, [open]);
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-ink/40" />
        <Dialog.Content
          // 桌面右侧 400px 抽屉;移动端全屏详情
          className="fixed inset-y-0 right-0 z-50 flex w-full flex-col border-l border-border bg-surface shadow-xl md:w-[400px]"
        >
          <div className="flex items-center justify-between border-b border-border px-4 py-3">
            <Dialog.Title className="text-base font-medium">{title}</Dialog.Title>
            <Dialog.Close asChild>
              <button type="button" aria-label="关闭详情"
                className="rounded-lg p-1.5 hover:bg-surface-alt">
                <X size={18} aria-hidden />
              </button>
            </Dialog.Close>
          </div>
          <div className="flex-1 overflow-y-auto p-4">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function ConfirmDialog({ open, onOpenChange, title, description, confirmLabel, onConfirm, busy, danger, children }: {
  open: boolean; onOpenChange: (open: boolean) => void;
  title: string; description: React.ReactNode; confirmLabel: string;
  onConfirm: () => void; busy?: boolean; danger?: boolean;
  children?: React.ReactNode;
}): React.ReactElement {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-ink/40" />
        <Dialog.Content aria-label={title}
          className="fixed left-1/2 top-1/2 z-50 w-[min(92vw,420px)] -translate-x-1/2 -translate-y-1/2 rounded-card border border-border bg-surface p-5 shadow-xl">
          <Dialog.Title className="text-base font-medium">{title}</Dialog.Title>
          <Dialog.Description className="mt-2 text-sm text-ink-secondary">
            {description}
          </Dialog.Description>
          {children && <div className="mt-3">{children}</div>}
          <div className="mt-4 flex justify-end gap-2">
            <Dialog.Close asChild>
              <button type="button" className="rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-surface-alt">
                取消
              </button>
            </Dialog.Close>
            <button
              type="button"
              disabled={busy}
              onClick={() => { onConfirm(); }}
              className={`rounded-lg px-3 py-1.5 text-sm text-white disabled:opacity-50 ${
                danger ? 'bg-danger hover:opacity-90' : 'bg-primary hover:bg-primary-strong'
              }`}
            >
              {busy ? '处理中…' : confirmLabel}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

// ---- 通用卡片 / 按钮 -----------------------------------------------------------

export function Card({ children, className = '' }: {
  children: React.ReactNode; className?: string;
}): React.ReactElement {
  return (
    <section className={`rounded-card border border-border bg-surface ${className}`}>
      {children}
    </section>
  );
}

export function SectionTitle({ children, actions }: {
  children: React.ReactNode; actions?: React.ReactNode;
}): React.ReactElement {
  return (
    <div className="flex items-center justify-between gap-2 px-4 pt-4">
      <h2 className="text-base font-medium">{children}</h2>
      {actions}
    </div>
  );
}

export function PendingRow({ label, hint }: { label: string; hint?: string }): React.ReactElement {
  return (
    <div className="flex items-center gap-2 border-b border-border px-4 py-3 text-sm last:border-b-0">
      <Clock size={14} className="text-caution" aria-hidden />
      <span>{label}</span>
      {hint && <span className="text-xs text-ink-muted">{hint}</span>}
    </div>
  );
}
