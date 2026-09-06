/**
 * 设置与数据:服务状态(/v1/health 的真实口径)、字号/时区偏好、
 * 真实导出与备份(服务端产物 ID + 校验)、打印当前药单。
 */
import React, { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Download, Printer, ShieldCheck } from 'lucide-react';
import { api } from '../../api/client';
import { qk } from '../../api/queryKeys';
import { usePreferences } from '../../hooks/usePreferences';
import {
  Badge, Card, ConfirmDialog, ErrorState, LoadingBlock, SectionTitle, SkeletonList,
} from '../../components/ui';
import type { ArtifactReportDto } from '../../api/types';

export function SettingsPage(): React.ReactElement {
  const { setFontSize, setTimezone } = usePreferences();
  const prefs = usePreferences();
  const healthQuery = useQuery({
    queryKey: qk.health,
    queryFn: ({ signal }) => api.health(signal),
    refetchInterval: 30_000,
  });
  const artifactsQuery = useQuery({
    queryKey: qk.artifacts,
    queryFn: ({ signal }) => api.artifacts(signal),
  });

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-xl font-semibold">设置与数据</h1>
        <p className="mt-1 text-sm text-ink-secondary">界面偏好保存在本浏览器;所有记录保存在服务端数据库。</p>
      </header>

      <Card>
        <SectionTitle>显示偏好</SectionTitle>
        <div className="grid grid-cols-1 gap-4 p-4 md:grid-cols-2">
          <div>
            <p className="mb-1.5 text-sm font-medium">字号(大字模式真实生效)</p>
            <div className="flex gap-2">
              <button type="button"
                onClick={() => setFontSize('normal')}
                aria-pressed={prefs.fontSize === 'normal'}
                className={`rounded-lg border px-3.5 py-1.5 text-sm ${prefs.fontSize === 'normal' ? 'border-primary bg-primary-soft font-medium text-primary-strong' : 'border-border'}`}>
                标准
              </button>
              <button type="button"
                onClick={() => setFontSize('large')}
                aria-pressed={prefs.fontSize === 'large'}
                className={`rounded-lg border px-3.5 py-1.5 text-sm ${prefs.fontSize === 'large' ? 'border-primary bg-primary-soft font-medium text-primary-strong' : 'border-border'}`}>
                大字
              </button>
            </div>
          </div>
          <div>
            <p className="mb-1.5 text-sm font-medium">时间显示时区</p>
            <div className="flex gap-2">
              <button type="button"
                onClick={() => setTimezone('local')}
                aria-pressed={prefs.timezone === 'local'}
                className={`rounded-lg border px-3.5 py-1.5 text-sm ${prefs.timezone === 'local' ? 'border-primary bg-primary-soft font-medium text-primary-strong' : 'border-border'}`}>
                浏览器时区
              </button>
              <button type="button"
                onClick={() => setTimezone('asia-shanghai')}
                aria-pressed={prefs.timezone === 'asia-shanghai'}
                className={`rounded-lg border px-3.5 py-1.5 text-sm ${prefs.timezone === 'asia-shanghai' ? 'border-primary bg-primary-soft font-medium text-primary-strong' : 'border-border'}`}>
                北京时间(Asia/Shanghai)
              </button>
            </div>
          </div>
        </div>
      </Card>

      <Card>
        <SectionTitle>服务状态</SectionTitle>
        {healthQuery.isPending && <LoadingBlock />}
        {healthQuery.isError && (
          <div className="p-4">
            <ErrorState error={healthQuery.error}
              title="服务不可达" />
            <p className="mt-2 text-xs text-ink-secondary">
              页面仍可浏览已加载内容;所有写操作需要服务恢复后重试(幂等键保证不重复记录)。
            </p>
          </div>
        )}
        {healthQuery.data && (
          <div className="space-y-2 p-4 text-sm">
            <p className="flex flex-wrap items-center gap-2">
              <Badge tone="primary" icon={<ShieldCheck size={13} aria-hidden />}>
                服务可达({healthQuery.data.status})
              </Badge>
              <span className="text-xs text-ink-muted">
                注意:这只是健康端点的状态,不代表外部模型或全部证据源已验证可用。
              </span>
            </p>
            <dl className="grid grid-cols-1 gap-x-6 gap-y-1.5 md:grid-cols-2">
              <Item label="数据库 schema 版本">{healthQuery.data.schema_version ?? '未记录'}</Item>
              <Item label="后台 worker">{healthQuery.data.worker_thread ? '已开启' : '未开启(事件不会自动处理)'}</Item>
              <Item label="待处理事件任务">{healthQuery.data.pending_outbox_tasks}</Item>
              <Item label="待复查结论">{healthQuery.data.pending_rechecks}</Item>
              <Item label="LLM 规划模式">
                {healthQuery.data.llm_planner_enabled ? '已启用(服务端配置)' : '未启用(确定性处理,仍是真实业务执行)'}
              </Item>
              <Item label="图运行器">{healthQuery.data.graph_runner_enabled ? '已启用' : '未启用'}</Item>
              <Item label="鉴权模式">
                {healthQuery.data.auth_mode === 'local-demo'
                  ? '本地单用户演示(无真实认证,请勿暴露到公网)' : healthQuery.data.auth_mode}
              </Item>
              <Item label="运行时长">{Math.round(healthQuery.data.uptime_seconds)} 秒</Item>
            </dl>
          </div>
        )}
      </Card>

      <DataSection artifactsQuery={artifactsQuery} />

      <PrintSection />

      <Card>
        <SectionTitle>部署说明(诚实边界)</SectionTitle>
        <div className="space-y-1.5 p-4 text-sm text-ink-secondary">
          <p>· 本应用为单照护者、单患者的本地/受控自托管形态,当前仓库没有认证与多用户隔离;请勿在未加鉴权的情况下暴露到公网。</p>
          <p>· 服务端必须以单写者进程运行(不要使用多个 uvicorn worker)。</p>
          <p>· 恢复数据库属于管理员维护能力,涉及停写和目标路径管理,不在普通页面提供覆盖当前数据库的入口。</p>
          <p>· 本应用不构成医疗建议;「按报告记录」「待核实」等状态保留了记录的不确定性。</p>
        </div>
      </Card>
    </div>
  );
}

function Item({ label, children }: {
  label: string; children: React.ReactNode;
}): React.ReactElement {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border pb-1.5">
      <dt className="text-ink-muted">{label}</dt>
      <dd className="text-right">{children}</dd>
    </div>
  );
}

function DataSection({ artifactsQuery }: {
  artifactsQuery: {
    isPending: boolean; isError: boolean; error: unknown;
    data: Awaited<ReturnType<typeof api.artifacts>> | undefined;
  };
}): React.ReactElement {
  const queryClient = useQueryClient();
  const [confirm, setConfirm] = useState<'export' | 'backup' | null>(null);
  const [busy, setBusy] = useState(false);
  const [reports, setReports] = useState<ArtifactReportDto[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [verifications, setVerifications] = useState<Record<string, string>>({});

  const runCreate = (kind: 'export' | 'backup') => {
    setBusy(true);
    setError(null);
    const action = kind === 'export' ? api.createExport() : api.createBackup();
    action
      .then((report) => {
        setReports((previous) => [report, ...previous]);
        void queryClient.invalidateQueries({ queryKey: qk.artifacts });
      })
      .catch((err) => setError(err instanceof Error ? err.message : '创建失败。'))
      .finally(() => setBusy(false));
  };

  const verify = (artifactId: string) => {
    setVerifications((previous) => ({ ...previous, [artifactId]: '校验中…' }));
    api.artifactVerify(artifactId)
      .then((report) => {
        const passed = report['sha256_matches'] !== false
          && report['integrity_check'] === 'ok';
        setVerifications((previous) => ({
          ...previous,
          [artifactId]: passed
            ? `校验通过(integrity ok${report['sha256_matches'] === true ? ',sha256 匹配' : ''},行数:${JSON.stringify(report['row_counts'] ?? {})})`
            : `校验结果:${JSON.stringify(report)}`,
        }));
      })
      .catch((err) => setVerifications((previous) => ({
        ...previous, [artifactId]: err instanceof Error ? err.message : '校验失败',
      })));
  };

  return (
    <>
      <Card>
        <SectionTitle>导出与备份</SectionTitle>
        <div className="space-y-3 p-4">
          <p className="text-xs text-ink-muted">
            「导出照护数据」是全库 JSON 交换文件(.json.gz,服务端先做在线快照保证一致性);
            「创建备份」是完整 SQLite 数据库副本(.db)。都不是只导出当前药单。
          </p>
          <div className="flex flex-wrap gap-2">
            <button type="button" onClick={() => setConfirm('export')} disabled={busy}
              className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-strong disabled:opacity-50">
              导出照护数据
            </button>
            <button type="button" onClick={() => setConfirm('backup')} disabled={busy}
              className="rounded-lg border border-primary px-4 py-2 text-sm font-medium text-primary-strong hover:bg-primary-soft disabled:opacity-50">
              创建备份
            </button>
          </div>
          {error && <p className="text-sm text-danger">{error}</p>}
          {reports.length > 0 && (
            <ul className="space-y-1.5 text-sm">
              {reports.map((report, index) => (
                <li key={index} className="rounded-lg bg-surface-alt px-3 py-2">
                  <span className="font-medium">{report.kind === 'export' ? '导出' : '备份'}完成</span>
                  <span className="ml-2 font-mono text-xs">{report.artifact_id}</span>
                  {report.created_at && (
                    <span className="ml-2 text-xs text-ink-muted">{String(report.created_at)}</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </Card>

      <Card>
        <SectionTitle>已有产物</SectionTitle>
        {artifactsQuery.isPending && <SkeletonList rows={2} />}
        {artifactsQuery.isError && <div className="p-4"><ErrorState error={artifactsQuery.error} /></div>}
        {artifactsQuery.data && artifactsQuery.data.length === 0 && (
          <p className="p-4 text-sm text-ink-muted">还没有导出/备份产物。</p>
        )}
        {artifactsQuery.data && artifactsQuery.data.length > 0 && (
          <ul className="divide-y divide-border px-4 pb-3">
            {artifactsQuery.data.map((artifact) => (
              <li key={artifact.artifact_id} className="flex flex-wrap items-center gap-2 py-2.5 text-sm">
                <Badge tone={artifact.kind === 'export' ? 'primary' : 'neutral'}>
                  {artifact.kind === 'export' ? '导出' : '备份'}
                </Badge>
                <span className="font-mono text-xs">{artifact.artifact_id}</span>
                <span className="text-xs text-ink-muted">{Math.round(artifact.size_bytes / 1024)} KB</span>
                <span className="ml-auto flex gap-1.5">
                  <a href={api.artifactDownloadUrl(artifact.artifact_id)}
                    download={`medication-coordinator-${artifact.artifact_id}`}
                    className="inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-xs hover:bg-surface-alt">
                    <Download size={12} aria-hidden /> 下载
                  </a>
                  <button type="button" onClick={() => verify(artifact.artifact_id)}
                    className="rounded-lg border border-border px-2 py-1 text-xs hover:bg-surface-alt">
                    校验
                  </button>
                </span>
                {verifications[artifact.artifact_id] && (
                  <span className="w-full break-all text-xs text-ink-secondary">
                    {verifications[artifact.artifact_id]}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <ConfirmDialog
        open={confirm !== null} onOpenChange={(open) => { if (!open) setConfirm(null); }}
        title={confirm === 'export' ? '创建全库导出?' : '创建数据库备份?'}
        description="将在服务端生成产物文件(含全部照护记录),完成后可从「已有产物」下载。产物包含患者敏感信息,请妥善保管。"
        confirmLabel={confirm === 'export' ? '创建导出' : '创建备份'}
        busy={busy}
        onConfirm={() => { const kind = confirm; setConfirm(null); if (kind) runCreate(kind); }}
      />
    </>
  );
}

function PrintSection(): React.ReactElement {
  const stateQuery = useQuery({
    queryKey: qk.memoryState(),
    queryFn: ({ signal }) => api.memoryState({}, signal),
  });
  const medications = stateQuery.data?.medications ?? [];
  return (
    <Card>
      <SectionTitle>打印当前药单</SectionTitle>
      <div className="p-4">
        <p className="mb-2 text-xs text-ink-muted">
          使用浏览器打印当前真实药单(含记录截至时间);不会生成医生签名或临床证明。
        </p>
        <div id="print-medication-list" className="print-full rounded-lg border border-border p-3 text-sm">
          <p className="font-serif text-base font-semibold">用药协管员 · 当前药单</p>
          <p className="mt-0.5 text-xs text-ink-muted">
            记录截至:{new Date().toLocaleString('zh-CN')}
            (数据来源:本地服务端数据库,按报告记录)
          </p>
          {medications.length === 0
            ? <p className="mt-2 text-ink-muted">当前没有在用药记录。</p>
            : (
              <ol className="mt-2 list-decimal space-y-1 pl-5">
                {medications.map((medication) => (
                  <li key={medication.ref}>
                    {medication.display_name}
                    {medication.dose ? ` · 剂量 ${medication.dose}` : ' · 剂量未记录'}
                    {medication.route ? ` · ${medication.route}` : ''}
                    {medication.schedule ? ` · ${medication.schedule}` : ''}
                    {` · 开始 ${medication.start_at.slice(0, 10)}`}
                  </li>
                ))}
              </ol>
            )}
          <p className="mt-2 text-xs text-ink-muted">此清单为家庭照护记录,不构成医疗证明;如有疑问请咨询医生/药师。</p>
        </div>
        <button type="button" onClick={() => window.print()}
          className="no-print mt-3 inline-flex items-center gap-1.5 rounded-lg border border-border px-3.5 py-2 text-sm hover:bg-surface-alt">
          <Printer size={14} aria-hidden /> 打印
        </button>
      </div>
    </Card>
  );
}
