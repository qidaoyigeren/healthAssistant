# 数据模型、关键事务与崩溃恢复

## 1. 身份模型（五种身份，职责互斥）

| 身份 | 含义 | 生成与约束 |
| --- | --- | --- |
| `request_key` | 客户端一次提交意图 | UI 生成并持久保存；HTTP 超时/重连/轮询恢复复用 |
| `event_id` | 一次业务事实报告 | 服务端首次受理时生成（uuid4 hex）落库；不因换 HTTP 请求或重试改变 |
| `run_id` | 处理该事件的一次工作流执行 | 首次受理时生成；恢复沿用；显式重新评估可另建关联 run |
| `operation_id` | 一次逻辑副作用 | 写入前持久化意图；恢复沿用；由 `(event_id, operation_type, target_ref, input_version)` 确定性派生 |
| `attempt_id` | 某次执行尝试 | 每次领取/外呼新建；仅用于审计与成本，不充当去重键 |

请求唯一约束：`(scope_id, principal_id, operation_name, request_key)`。当前单患者实现用固定受信 scope，但 scope 不由客户端声明。请求哈希基于校验后的规范 JSON（目标患者、事件类型、动作、有意义载荷），不含 trace id/鉴权 token。同 key/同 payload → 原事件；同 key/异 payload → 422（保留现有契约）；**不**用内容相似度去重真实的新报告。

## 2. 新增/变更表

### `operation_receipts`（P0 新增）

```sql
CREATE TABLE IF NOT EXISTS operation_receipts (
  id INTEGER PRIMARY KEY,
  scope_id TEXT NOT NULL DEFAULT 'local-demo',
  operation_id TEXT NOT NULL,
  event_id TEXT,
  run_id TEXT,
  operation_type TEXT NOT NULL,      -- consolidate_event / record_medication_change / record_warnings / record_conclusion / open_review_case / apply_review_decision / deliver_notification
  target_ref TEXT,                   -- 如 medication:克拉霉素 / conflict:12
  input_hash TEXT NOT NULL,          -- 规范化输入指纹
  expected_revision TEXT,
  status TEXT NOT NULL,              -- succeeded / failed / effect_unknown
  result_ref TEXT,                   -- 指向 interaction_id / conclusion_id / version id …
  attempt_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (scope_id, operation_id)
);
```

写路径短事务②（禁止 LLM/网络在事务内）：

```sql
BEGIN IMMEDIATE;
  -- 1. 校验执行资格（worker 传入 lease 上下文时）
  SELECT ... FROM outbox_tasks WHERE id=? AND status='running'
    AND lease_token=? AND lease_expires_at > :now;   -- 不满足 → ROLLBACK, lease_rejected
  -- 2. 查回执
  SELECT * FROM operation_receipts WHERE scope_id=? AND operation_id=?;
  -- 3a. 已存在且 input_hash 不同 → 拒绝（回执冲突，人工审计）
  -- 3b. 已存在且 status='succeeded' → COMMIT，返回原 result_ref（幂等命中）
  -- 4. 执行领域写入（现有 MemoryStore 函数，同连接同事务）
  -- 5. 写审计（随领域事务原子成功/失败）
  -- 6. INSERT operation_receipts(..., status='succeeded', result_ref=...)
COMMIT;
```

去重边界：`consolidate_event` 与显式药物变更各有回执（或合并为同一领域命令提交）；警告按 `(检测操作, 药物对, 证据版本)` 区分——同一检测操作重放不重复 INSERT，真实新版本重查有新的 operation_id；review 建单/决策/恢复/通知（P2）各有稳定 operation id；合法的再次检查有新的逻辑操作身份，**不用** `(run_id, node_name)` 去重整个循环，也**不**在每次尝试生成随机 operation id。

### `outbox_tasks` 增量字段（P0）

```sql
ALTER TABLE outbox_tasks ADD COLUMN next_attempt_at TEXT;
ALTER TABLE outbox_tasks ADD COLUMN heartbeat_at TEXT;
ALTER TABLE outbox_tasks ADD COLUMN last_error_class TEXT;  -- retryable/permanent/safety/effect_unknown/internal
ALTER TABLE outbox_tasks ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3;
ALTER TABLE outbox_tasks ADD COLUMN deadline_at TEXT;
```

`claim` 追加 `AND (next_attempt_at IS NULL OR next_attempt_at <= :now)`；`complete/fail/heartbeat` 追加租约 fencing：

```sql
UPDATE outbox_tasks SET status='done', ... 
 WHERE id=? AND status='running' AND lease_token=? AND lease_expires_at > :now;
-- rowcount==0 → LeaseRejected：旧执行者的任何写入被静默拒绝并记审计
```

领域写路径同样校验执行资格——只给 `complete()` 加条件不够。

### `idempotency_keys` → 增量升级（P0）

增加 `event_id`、`run_id`、`scope_id`、`principal_id` 列（nullable，历史行保持可读，不改写旧身份）。唯一约束维持现有 `key`，未来多 scope 时再迁 `(scope_id, principal_id, operation_name, request_key)`。

### 历史兼容

既有 `interactions.event_key`（`session:turn` 形态）与 legacy adoption 逻辑（[memory.py:1554-1578](../../stage0/memory.py#L1554-L1578)）保留不动；API 事件改为显式 `client_event_id` 后只影响新事件。迁移全部 `IF NOT EXISTS`/`ADD COLUMN` 幂等，执行前提示备份（`python -m stage0.backup --backup`）。

## 3. 关键事务

### 事务①：事件受理（现状已有，仅补身份生成）

```sql
BEGIN IMMEDIATE;
  INSERT INTO idempotency_keys(key, request_hash, status='in_flight', event_id=:new, run_id=:new, ...);
  INSERT INTO outbox_tasks(task_type='process_event', payload={event, session_id, event_id, run_id, turn_id=run_id, idempotency_key, event_key=api:{key}}, dedup_key='api-event:'+key, next_attempt_at=NULL);
COMMIT;
```

`turn_id` 改为等于 `run_id`（uuid），彻底消除截断碰撞。payload 内保留 `event_key="api:{key}"`。

### 事务②：领域写（上文 operation receipt 短事务）

### 事务③：审核决策 + 恢复任务（P2）

```sql
BEGIN IMMEDIATE;
  -- 校验 principal/reviewer 角色、case scope、expected_revision（CAS）
  UPDATE review_cases SET status=..., assignee=..., revision=revision+1 WHERE id=? AND revision=?;
  INSERT INTO review_decisions(idempotency_key UNIQUE, case_id, revision, action, payload, actor_id, ...);
  INSERT INTO resume_tasks(operation_id UNIQUE, run_id, decision_ref, ...)  -- Worker 消费后转 Command(resume=...)
COMMIT;
```

### 事务④：结果发布（P0 收敛目标）

```sql
BEGIN IMMEDIATE;
  UPDATE outbox_tasks SET status='done', result_json=?, lease_token=NULL ... WHERE id=? AND lease_token=? AND status='running';
  UPDATE idempotency_keys SET status='committed', response_json=? WHERE key=?;
  UPDATE workflow_runs SET status='succeeded', result_ref=? WHERE run_id=?;   -- P1 引入该表；P0 先以事件+任务为权威
COMMIT;
```

崩溃窗口：事务④失败时任务仍 `running`、租约过期后被回收；重执行在事务②命中回执 → 无重复副作用；最终由事务④或 reconciler 收敛状态。

## 4. 对账（reconciler）覆盖的三类不一致

1. 结果已提交（receipts 有 succeeded）但任务未标 done → 补 done + committed。
2. 待审核已建单但 interrupt 标记未落盘（P2）→ 以 review_cases 为权威重建等待状态。
3. 已作审核决策但恢复任务未消费（P2）→ 决策幂等重放一次。

入口：`POST /v1/reconcile`（运维权限）+ worker 启动时自检。活动工作（running 且租约未过期）不参与清理。

## 5. 保留与清理

| 数据 | 保留 | 清理规则 |
| --- | --- | --- |
| 幂等键 | ≥30 天（可配） | 仅 `committed`/`failed` 终态且无活动引用；过期后删除不失去业务去重（event/receipt 仍在） |
| operation_receipts | 永久（审计证据） | 不清理 |
| outbox_tasks done | ≥30 天 | 归档后清理 |
| checkpoint（P1） | run 终态后按保留策略 | saver 自管 schema，不手工伪造 |
| review_cases / decisions | 永久 | 不清理 |
| turn_traces / audit_log | 按现有策略 | 活动工作不清理 |

## 6. 崩溃恢复时序（关键场景）

```text
场景 A：领域写成功 → checkpoint/complete 前崩溃
  t1 worker 领取（lease L1）→ 事务② committed（receipt R1= succeeded）
  t2 崩溃，无事务④
  t3 租约过期 → 回收 → worker 重领（lease L2）
  t4 重跑 execute_write → 查 R1 命中 → 返回原 result_ref（无第二个副作用）
  t5 事务④ 发布 → converged

场景 B：旧执行者迟到写入
  t1 worker A 领取（L1）→ 阻塞
  t2 租约过期 → worker B 领取（L2）
  t3 A 的 complete/fail/heartbeat/领域写 → 条件 UPDATE rowcount=0 → LeaseRejected，审计记录
  t4 B 正常发布

场景 C：等待人工后重启（P2）
  工单持久化、run 状态 waiting_review、checkpoint 已存；重启后 reviewer 仍见工单，
  恢复走原 thread/run；活动预算从持久化值继续，不归零。
```
