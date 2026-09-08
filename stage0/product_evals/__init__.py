"""Product upgrade development evals (docs/product-upgrade/P0/eval-protocol.md).

任务格式、失败分类与 held-out 隔离规则见协议文档。run_eval 对真实失败返回
非零退出码;必需数据/模块缺失时输出 unavailable,绝不输出 pass。
"""
