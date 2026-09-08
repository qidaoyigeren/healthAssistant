# Harness P3 实施收尾与最终验收

更新：2026-09-07。当前依据是 `../final-acceptance/acceptance-summary.json` 和 `../final-acceptance/p3-eval.json`。原 `experiment_report.*` 保留为历史工件，不再代表当前验收结论。

## 完成范围

P3 已提供默认关闭的 `batch_read` 和 `delegate_task`，均经过共享工具执行器。worker 是固定角色、限定工具与输入、受父 run 预算约束的确定性只读流水线，`worker_model=None`，不能作为医护身份或真实独立 LLM Agent 使用。委派状态、尝试、结果与父子关联可持久化；权限、租约、取消、证据范围和恢复行为由测试覆盖。

本次补齐了实验的最终验收，而未开启默认开关。没有新增真实模型调用、真实人工审核或生产部署。

## 发现并修复的验收缺口

历史 p3-1.0 长证据场景中，single_agent / batch 各读 3 页，delegate 读 9 页，工作量不等价。必要检查还允许“没有委派结果”通过空集合判断；多药覆盖以派发次数估算，可能被重复查询充数。命令行也总是返回 0，重复实验只保留中间一次，可能丢失其他轮次的失败。

当前数据集 `p3-1.1-equivalent-coverage` 做了以下修正：

- 三种方案完整读取同一组 3 份合成标签，各 9 页。验收器在实际 EvidenceStore.read 返回处采集页面，以 source_uri、offset、returned_chars 重组，逐字比较完整内容。
- 多药覆盖比较不同预期查询与真实派发查询的集合，重复派发不能增加覆盖率。
- 委派必须产出与请求数量一致的验证结果；本合成用例的已知声明必须全部 verified。
- 必要检查覆盖、安全和简单场景零委派都是通过条件。任一次重复失败均保留；流程验收失败时 CLI 返回非零。未达到性能采用门槛本身不是测试失败。
- 单主循环逐页读，普通批处理每批 3 页，worker 在同一执行层完成分页。相同工具注册与输入范围保持一致。

## 结果解释

等量回读使历史“委派相对批处理只有约 4% 上下文收益”的比较失效。当前合成实验中，委派达到设定的离线候选门槛；精确决策数、payload、预算与三次重复结果见 `../final-acceptance/p3-eval.json`。这表示值得继续验证，**不表示已获得生产启用结论**。

最终 `--repeat 3` 验收通过。长证据场景三方案均回读 9 页，single_agent / batch / delegate 的规划决策分别为 12 / 6 / 4；多药检索分别为 7 / 3 / 3。9 个场景与模式组合的必要检查完成率均为 1.0；简单场景没有委派。统一入口的 281 项后端测试也全部通过。

`adopt_delegation=true` / `adopt_batching=true` 仅表示预设合成工作负载下的候选判定。两个运行开关仍默认关闭。planner/IO 的人工延迟、估算 token 与固定脚本不构成真实模型质量、实际费用或生产延迟证据。未产生警告时引用有效率是“无待校验引用”，不能解读为临床正确率 100%。

## 复测与兼容性

在仓库根目录运行：

```powershell
.venv\Scripts\python.exe -m unittest stage0.test_harness_p3 stage0.test_harness_acceptance -v
.venv\Scripts\python.exe -m stage0.harness_p3_eval --repeat 3 --out docs/harness-upgrade/final-acceptance/p3-eval.json
```

推荐使用 `python -m stage0.run_harness_acceptance` 一并检查 P0–P2 回归。当前最终结果应查看统一验收工件，避免引用历史测试数量。新增负例覆盖缺失必要检查不可被认定为性能候选，以及同参数实际重试不可被错误折叠。

没有新增破坏性数据库迁移。关闭 P3 开关只影响新能力入口，不删除委派、证据、回执或预算记录。已开始 run 的语义配置由 manifest 恢复检查保护，不能通过修改开关或放大环境预算重新解释在途任务；不兼容时需显式迁移或恢复原配置。

## 待验证项与下一阶段

尚未验证真实 worker LLM 的上下文隔离收益、独立 held-out 数据集、真实医护判断、跨进程传播和多患者部署。当前合成集在修复过程中被使用，属于回归集，不能改名为独立验证集。

P4 不依赖 P3。只有出现明确的多 worker / 多患者 / 可用性目标后才进入部署改造。无需为了采用成熟运行时设计而同时引入多套 Agent 框架或调度服务。
