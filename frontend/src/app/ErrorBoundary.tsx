import React from 'react';
import { useLocation } from 'react-router-dom';

interface Props {
  children: React.ReactNode;
  /** 通过 key 变化(如路由切换)自动恢复渲染。 */
  recoverOnNavigation?: boolean;
}

interface State {
  error: Error | null;
}

/** 全局错误边界:一次接口/渲染失败不白屏,可局部重试或恢复导航。 */
export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  render(): React.ReactNode {
    if (this.state.error) {
      return (
        <div role="alert" className="rounded-card border border-danger-soft bg-danger-soft/40 p-6">
          <p className="font-medium text-danger">页面显示出现问题</p>
          <p className="mt-1 text-ink-secondary">
            {this.state.error.message || '未知错误'}。您的记录没有受到影响。
          </p>
          <button
            type="button"
            onClick={() => this.setState({ error: null })}
            className="mt-3 rounded-lg bg-primary px-4 py-2 text-white hover:bg-primary-strong"
          >
            重试
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

/** 函数组件包装:路由变化时自动清除错误状态。 */
export function RouteErrorBoundary({ children }: { children: React.ReactNode }): React.ReactElement {
  const location = useLocation();
  return (
    <ErrorBoundary key={location.pathname} recoverOnNavigation>
      {children}
    </ErrorBoundary>
  );
}
