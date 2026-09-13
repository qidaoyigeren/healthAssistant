"""material-review@2 — 以用户交付物为目标的证据调查。

代码准备可信上下文并守住边界（权限、作用域、事实版本、引用可追溯、交付检查、
安全边界）；模型负责理解用户问题、选择调查事项、选择材料与查询、修订调查、
提出有证据关联的发现与报告草稿。

本包与 `investigation@1`（medication-evidence-review@1）并存：旧契约原样保留，
新任务走新契约。
"""
from .contract import CONTRACT_VERSION, STATE_VERSION, TaskSpec, build_task_spec

__all__ = ['CONTRACT_VERSION', 'STATE_VERSION', 'TaskSpec', 'build_task_spec']
