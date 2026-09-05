# Stage 7（记忆深化）实施报告

- 实施日期：2026-09-05。基线：`e3fcc78` 工作区 + 《生产化与亮点升级设计.md》（同日交付，本文档含对其的 4 处实施修正）。
- 范围纪律：只实施 Stage 7（主线 A 全部）；Stage 8–11 未展开；未清空、未改动现有 `stage0/memory.db`（所有测试与冒烟均使用临时库或副本）；未填写任何未测量数字。
- 验收工件：本目录 `eval_report.json`（S01–S28 + 5 项消融）。

## 一、变更清单

| 文件 | 变更 |
| --- | --- |
| [memory.py](../../stage0/memory.py) | ① schema 4-p2：`conclusion_dependencies`（5 类依赖 + 双索引）、`fact_reports`（报告台账）、`agent_checkpoints`（A5 设计预留，无写入路径）、`dependency_tasks.lease_expires_at`；一次性确定性回填（`deps_backfilled` 标记 + `dependency_backfill` 审计）。② 依赖推导/写入/查询：`_derive_conclusion_deps_tx`、`_write_conclusion_dependencies_tx`（含 predecessor 继承与重查后继的集合哈希重记）、`_direct_dependents_tx`、`_dependents_of_tx`（visited 防环的不设限闭包）。③ 选择性失效：`_invalidate_medication_dependents_tx(changed_name)` 三路命中（medication 直达 / pair 成员 / 集合哈希含 NULL 保守），`selective_invalidation` 消融回退全量 revision 路径；`dependency_index` 消融回退全表扫描（事实变更/撤回/冲突重开三处切换）。④ 重查健壮化：`recover_expired_rechecks`（租约过期回收，attempts 计数，≥3 判 failed）、认领写租约、predecessor 后继去重（at-least-once → effectively-once）。⑤ AS-OF：`query_state` 冲突按 `conflict_actions` 折叠重建（无轨迹行降级 `resolved_at`），docstring 与实现不符处已修正；`retrieve_episodic(as_of)` 增加 `recorded_at<=` 知识截止过滤与独立的 `occurred_before` 有效时间参数。⑥ 巩固状态机：T1 写 `fact_reports`、T1 后 `_promotion_scan`（规则触发、冲突阻断并审计）、`_promote_to_verified_tx`（新版本行 verified、不 bump scope、不失效依赖）、`verify_semantic_fact`（照护者确认，冲突阻断）、`semantic_fact_conflict` 打开时该键标记 disputed。⑦ `medication_set_hash()` 公开方法。⑧ `_drug_dep_key`：优先 ddi_engine 成分归一，回退 compact（两侧同函数保证一致） |
| [agent.py](../../stage0/agent.py) | `_recheck_hook` 改为按结论类型分发：`warning`+`患者个体风险` 标记（或 `condition_warning` kind）→ `_recheck_condition`（焦点药来自 medication 引用，RAG 标签文本 + `AgentPlanner._condition_warnings` 确定性推导，无发现回退保守模板）；其余 → `_recheck_ddi`（原逻辑 + `_current_detect_result` 按当前药单集合哈希的实例级 LRU（64），同批 N 个任务只跑一次检测） |
| [eval_memory.py](../../stage0/eval_memory.py) | 新增 S23–S28（AS-OF 冲突 ×2、AS-OF 事件、巩固状态机、condition 重查、选择性失效）；消融机制加入 `selective_invalidation`（S28 为其承重场景）；`dependency_index` 不入消融循环（等价性由单测覆盖，注释说明） |
| [backup.py](../../stage0/backup.py)（新） | `--backup`（sqlite3 backup API 在线备份 + sha256/行数 sidecar）、`--export`（JSON.gz 交换格式，跳过 FTS 虚表）、`--restore <file> --to <path>`（拒绝覆盖既有目标；JSON 导入走 MemoryStore 建模式 + FK 关闭批量插入 + integrity/foreign_key 校验失败即删目标 + FTS 重建）、`--verify`（integrity/quick/fk + sidecar 对账） |
| [test_memory_p2.py](../../stage0/test_memory_p2.py)（新） | 15 项测试：依赖行推导（pair/set/条件无集合依赖）、回填确定性与幂等、选择性 vs 全量消融对比、`dependency_index` 等价性、传递失效、批处理去重（检测恰 1 次）、effectively-once 去重、租约回收与失败上限、AS-OF 折叠等价、重放不计晋升、冲突阻断晋升、备份/导出往返 ×3 |
| [test_memory_p0.py](../../stage0/test_memory_p0.py) | `schema_version` 断言 `4-p1` → `4-p2`（P2 迁移的合法版本递进；该测试的真实意图——重开不丢数据——不变） |
| .gitignore | 增加 `stage0/backups/`、`stage0/exports/` |

## 二、与设计文档的偏差（已回写设计文档）

1. **warning 依赖签名修正（最重要）**：设计初稿让全药单 DDI 对警告只挂 `medication_pair`——实施时发现这会使既有 S14 回归失败（新增辛伐他汀必须打 stale 氨氯地平×克拉霉素旧警告，这是 scope-revision 的语义承诺）。已实施口径：warning = pair + **集合哈希** + 引用解析依赖；选择性收益集中于患者个体条件结论（焦点药+事实，无集合依赖）。S28 场景同步改写。
2. **传递闭包不设深度上限**：visited 防环已保证终止；真实链深 ≤2；异常深链全失效是保守方向，比深度截断更简单安全。
3. **undo 折叠语义**：undo 行自身的 `undone_by` 指向被撤销动作（与 `resolve_conflict` 写入约定一致），折叠状态回退到该动作的 `previous_status`。实施中曾按相反方向实现并被等价性测试捕获——该测试保留为回归。
4. **A4 新增 `fact_reports` 台账表**：设计只写了"N 个不同 event_key"，实施补上确定性载体；重放不新增行（重放永不累计晋升）；存量库不回填（见限制 1）。

## 三、测试证据（全部命令可复现）

| 命令 | 结果 |
| --- | --- |
| `python -m unittest stage0.test_stage3 stage0.test_stage5 stage0.test_stage6 stage0.test_memory_p0 stage0.test_memory_p1 stage0.test_memory_p2` | **92/92 OK**（约 3.5s） |
| `python -m stage0.eval_memory --ablate` | **28/28 通过**；消融 policy / bitemporal / dependency / **selective_invalidation** 四机制均 load-bearing（工件：本目录 `eval_report.json`） |
| 迁移冒烟（现有 memory.db **副本**，未动原库） | schema 4-p2、integrity ok；6 条存量结论回填 54 条依赖行（semantic_fact 28 / medication 20 / medication_pair 3 / medication_set 3，集合依赖为 NULL 哈希=保守） |

## 四、消融指标对比（设计文档第七节矩阵的 Stage 7 行，全部代码断言判定）

| 指标 | 消融关（旧路径） | 默认（新路径） | 证据 |
| --- | --- | --- | --- |
| 失效覆盖率 | 1.0 | **1.0（维持）** | S14/S15/S07 + 全量回归 |
| 误失效率（S28 ground truth：无关条件结论） | **1.0**（加无关药后误伤） | **0** | `test_selective_vs_full_invalidation_ablation`、S28 `unrelated_condition_finding_kept` |
| 重查去重收益（detect 调用数 ÷ 任务数） | 1.0 | **1/3 ≈ 0.33**（3 任务一次检测；LRU 命中时为 0） | `test_batch_dedup_runs_detector_once` |
| AS-OF 正确率 | 0/3（S23 现状必败） | **3/3** | S23/S24/S25 |
| 巩固晋升正确率 | 无路径 | **S26 五分支全过**（含矛盾阻断、重放不计入） | S26 + PromotionTests |
| 租约恢复 | running 永久卡死 | 过期回收→open，3 次认领未完成→failed | `test_lease_expiry_recovery_and_failure_cap` |
| 依赖索引等价性 | — | 开/关产生**相同** stale 集 | `test_dependency_index_equivalence` |

（以上均为确定性场景断言，非性能测量；`dependency_index` 的性能收益属 Stage 11 埋点，未测量不填数。）

## 五、仍存在的限制

1. **`fact_reports` 存量不回填**：迁移前的旧事件无法确定性归属 event_key，晋升计数从迁移后新事件开始；旧事实可通过 `verify_semantic_fact` 照护者确认晋升。
2. **AS-OF 冲突折叠的时间精度为秒**：`conflict_actions.created_at` 与 `known_at` 同秒内的动作顺序按 id 决定（与执行序一致），亚秒级查询不区分。
3. **`agent_checkpoints` 只有表结构**（A5 设计预留）：无写入/恢复路径，Stage 8 评估后决定。
4. **重查检测缓存是 agent 实例级**（容量 64）：跨进程/重启不共享；进程级共享缓存属 Stage 11（D4 memoization）。
5. **condition 重查依赖 RAG 可用性**：RAG 异常时回退保守模板（不升级、不解除），不做二次尝试。
6. **备份未加密、无定时**：`--backup` 为手动命令；Litestream/定时与恢复演练文档属 Stage 11（D6）。
7. **晋升 N=2 为默认常量**（env 可调）：同照护者两次相同报告即晋升是当前规则；"独立程度"更强的定义（如跨会话/跨日）留待产品判断。
8. **README/REPORT 未在本轮更新**：按仓库惯例（实现与文档分轮提交），建议随 Stage 7 评审后一并更新。

## 六、回滚方式

- 代码：`git revert` 本轮提交即可；schema 侧 `conclusion_dependencies`/`fact_reports`/`agent_checkpoints` 为纯新增表、`lease_expires_at` 为纯新增列，旧代码开库无感知（向后兼容）。
- 行为：`MemoryStore(ablations={'selective_invalidation'})` 即回到全量失效；`{'dependency_index'}` 回到全表扫描——两开关已由消融验证可用。
