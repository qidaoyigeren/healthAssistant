import React, { useState } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import * as Dialog from '@radix-ui/react-dialog';
import {
  FileClock, FileText, MoreHorizontal, Pill, ScrollText,
  Settings, ShieldAlert, ShieldCheck, User, X,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

interface NavItem {
  to: string; label: string; icon: LucideIcon; short: string;
  /** 支持入口排在主线之后,渲染时用一条分隔说明它。 */
  support?: boolean;
}

/**
 * 导航顺序就是产品顺序:用药安全事项是主线,材料核对等是支持入口。
 * 前四项出现在移动端底栏(`!support`),其余收进「更多」。
 */
const NAV: NavItem[] = [
  { to: '/', label: '用药安全', icon: ShieldCheck, short: '安全' },
  { to: '/medications', label: '用药记录', icon: Pill, short: '用药' },
  { to: '/tasks', label: '照护待办', icon: FileClock, short: '待办' },
  { to: '/alerts', label: '风险与证据', icon: ShieldAlert, short: '风险' },
  { to: '/materials', label: '材料核对', icon: FileText, short: '材料', support: true },
  { to: '/profile', label: '患者档案', icon: User, short: '档案', support: true },
  { to: '/conflicts', label: '待核实', icon: FileText, short: '待核实', support: true },
  { to: '/history', label: '照护时间线', icon: FileClock, short: '时间线', support: true },
  { to: '/assistant', label: '照护助手', icon: ScrollText, short: '助手', support: true },
  { to: '/settings', label: '设置与数据', icon: Settings, short: '更多', support: true },
];

/** 主线页 `/` 与它的详情页 `/safety/:caseId` 是同一个导航项。 */
function isNavActive(to: string, isActive: boolean, pathname: string): boolean {
  return isActive || (to === '/' && pathname.startsWith('/safety'));
}

/** 桌面 220px 侧栏 + 内容区;移动端单列 + 底部主导航(次要导航收进「更多」)。 */
export function Layout({ children }: { children: React.ReactNode }): React.ReactElement {
  const [moreOpen, setMoreOpen] = useState(false);
  const location = useLocation();

  return (
    <div className="flex min-h-screen">
      {/* 桌面侧栏 */}
      <aside className="no-print hidden w-[220px] shrink-0 flex-col border-r border-border bg-surface md:flex">
        <div className="px-5 pb-2 pt-6">
          <h1 className="font-serif text-xl font-semibold tracking-wide">用药协管员</h1>
          <p className="mt-1 text-xs text-ink-muted">家庭照护记录 · 非医疗设备</p>
        </div>
        <nav aria-label="主导航" className="mt-3 flex-1 px-3 pb-6">
          {NAV.map(({ to, label, icon: Icon, support }, index) => (
            <React.Fragment key={to}>
              {support && !NAV[index - 1]?.support && (
                <p className="mb-1 mt-4 px-3 text-xs text-ink-muted">支持入口</p>
              )}
              <NavLink
                to={to}
                end={to === '/'}
                className={({ isActive }) =>
                  `mb-1 flex items-center gap-3 rounded-lg px-3 py-2.5 text-[0.95rem] ${
                    isNavActive(to, isActive, location.pathname)
                      ? 'bg-primary-soft font-medium text-primary-strong'
                      : 'text-ink-secondary hover:bg-surface-alt'
                  }`
                }
              >
                <Icon size={18} aria-hidden />
                {label}
              </NavLink>
            </React.Fragment>
          ))}
        </nav>
        <p className="border-t border-border px-5 py-3 text-xs text-ink-muted">
          本地单照护者部署<br />记录不构成医疗建议
        </p>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        {/* 移动端顶部标题 */}
        <header className="no-print flex items-center justify-between border-b border-border bg-surface px-4 py-3 md:hidden">
          <h1 className="font-serif text-lg font-semibold">用药协管员</h1>
          <span className="text-xs text-ink-muted">非医疗设备</span>
        </header>

        <main className={`mx-auto w-full ${location.pathname === '/materials' ? 'max-w-7xl' : 'max-w-4xl'} flex-1 px-4 pb-24 pt-5 md:px-8 md:pb-10`}>
          {children}
        </main>

        {/* 移动端底部主导航:主线四项(安全/用药/待办/风险) + 更多 */}
        <nav
          aria-label="主导航"
          className="no-print fixed inset-x-0 bottom-0 z-40 flex border-t border-border bg-surface pb-[env(safe-area-inset-bottom)] md:hidden"
        >
          {NAV.filter((item) => !item.support).map(({ to, label, icon: Icon, short }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              aria-label={label}
              className={({ isActive }) =>
                `flex flex-1 flex-col items-center gap-0.5 py-2 text-xs ${
                  isNavActive(to, isActive, location.pathname)
                    ? 'font-medium text-primary-strong' : 'text-ink-muted'
                }`
              }
            >
              <Icon size={20} aria-hidden />
              {short}
            </NavLink>
          ))}
          <Dialog.Root open={moreOpen} onOpenChange={setMoreOpen}>
            <Dialog.Trigger asChild>
              <button
                type="button"
                className="flex flex-1 flex-col items-center gap-0.5 py-2 text-xs text-ink-muted"
              >
                <MoreHorizontal size={20} aria-hidden />
                更多
              </button>
            </Dialog.Trigger>
            <Dialog.Portal>
              <Dialog.Overlay className="fixed inset-0 z-40 bg-ink/40" />
              <Dialog.Content
                aria-label="更多导航"
                className="no-print fixed inset-x-0 bottom-0 z-50 rounded-t-2xl border-t border-border bg-surface p-4 pb-8"
              >
                <div className="mb-2 flex items-center justify-between">
                  <Dialog.Title className="text-base font-medium">更多</Dialog.Title>
                  <Dialog.Close asChild>
                    <button type="button" aria-label="关闭" className="rounded-lg p-2 hover:bg-surface-alt">
                      <X size={18} aria-hidden />
                    </button>
                  </Dialog.Close>
                </div>
                <div className="grid grid-cols-2 gap-2">
                  {NAV.filter((item) => item.support).map(({ to, label, icon: Icon }) => (
                    <NavLink
                      key={to}
                      to={to}
                      onClick={() => setMoreOpen(false)}
                      className={({ isActive }) =>
                        `flex items-center gap-3 rounded-lg border border-border px-3 py-3 ${
                          isActive ? 'border-primary bg-primary-soft font-medium text-primary-strong' : 'text-ink'
                        }`
                      }
                    >
                      <Icon size={18} aria-hidden />
                      {label}
                    </NavLink>
                  ))}
                </div>
              </Dialog.Content>
            </Dialog.Portal>
          </Dialog.Root>
        </nav>
      </div>
    </div>
  );
}
