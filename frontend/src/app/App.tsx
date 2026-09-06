import React, { useEffect } from 'react';
import { QueryClient, QueryClientProvider, useQueryClient } from '@tanstack/react-query';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { Layout } from './Layout';
import { ErrorBoundary, RouteErrorBoundary } from './ErrorBoundary';
import { PreferencesProvider } from '../hooks/usePreferences';
import { OverviewPage } from '../features/overview/OverviewPage';
import { ProfilePage } from '../features/profile/ProfilePage';
import { MedicationsPage } from '../features/medications/MedicationsPage';
import { AlertsPage } from '../features/alerts/AlertsPage';
import { ConflictsPage } from '../features/conflicts/ConflictsPage';
import { ExposurePage } from '../features/conflicts/ExposurePage';
import { HistoryPage } from '../features/history/HistoryPage';
import { AssistantPage } from '../features/assistant/AssistantPage';
import { SettingsPage } from '../features/settings/SettingsPage';
import { submissions } from '../api/submissions';
import { INVALIDATE_AFTER_COMMIT } from '../api/queryKeys';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: true,
      staleTime: 5_000,
    },
  },
});

export function App(): React.ReactElement {
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <CommitInvalidation />
        <PreferencesProvider>
          <BrowserRouter>
            <Layout>
              <RouteErrorBoundary>
                <Routes>
                  <Route path="/" element={<OverviewPage />} />
                  <Route path="/profile" element={<ProfilePage />} />
                  <Route path="/medications" element={<MedicationsPage />} />
                  <Route path="/alerts" element={<AlertsPage />} />
                  <Route path="/alerts/:alertId" element={<AlertsPage />} />
                  <Route path="/conflicts" element={<ConflictsPage />} />
                  <Route path="/conflicts/:conflictId" element={<ConflictsPage />} />
                  <Route path="/exposure" element={<ExposurePage />} />
                  <Route path="/history" element={<HistoryPage />} />
                  <Route path="/history/event/:eventId" element={<HistoryPage />} />
                  <Route path="/assistant" element={<AssistantPage />} />
                  <Route path="/settings" element={<SettingsPage />} />
                  <Route path="*" element={<NotFound />} />
                </Routes>
              </RouteErrorBoundary>
            </Layout>
          </BrowserRouter>
        </PreferencesProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  );
}

/** 提交 committed 后:按查询 key 失效并重新拉取,处理旧响应覆盖新状态。 */
function CommitInvalidation(): null {
  const queryClient = useQueryClient();
  useEffect(() => {
    submissions.onCommitted = (task) => {
      // 只有真正产生业务投影的写入才需要全局刷新;查询类事件不动缓存。
      if (['register_profile', 'profile_update', 'medication_change', 'procedure_exposure'].includes(task.event.event_type)) {
        for (const key of INVALIDATE_AFTER_COMMIT) {
          void queryClient.invalidateQueries({ queryKey: key });
        }
      }
      void queryClient.invalidateQueries({ queryKey: ['sessionEvents', task.event.session_id] });
      void queryClient.invalidateQueries({ queryKey: ['sessions'] });
    };
    return () => { submissions.onCommitted = null; };
  }, [queryClient]);
  return null;
}

function NotFound(): React.ReactElement {
  return (
    <div className="rounded-card border border-border bg-surface p-8 text-center">
      <p className="text-lg font-medium">没有这个页面</p>
      <p className="mt-2 text-ink-secondary">请使用左侧导航返回照护记录。</p>
    </div>
  );
}
