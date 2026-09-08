# P4：证据质量与缓存语义

`stage0/evidence_quality.py` 和 `ddi_engine.py` 已接入版本化缓存。键含实体、语料内容哈希、索引指纹、抽取代码、提示词、模型、服务端点哈希和策略版本。matched/not_found/provider_error/parse_error 分别处理；默认 TTL 为 86400/900/30/60 秒，可用 `DDI_CACHE_TTL_<STATUS>` 配置。旧无版本缓存不直接命中，过期条目被替换时保留历史正文；读路径记录命中或失效原因。

保留双向检索及精确药品提及门禁，增加有界检索、去重、无进展终止和可替换重排器。`DDI_RERANKER=exact_coverage` 为可选本地重排，默认仍用原排序：当前开发比较未证明重排收益。检索状态进入 DDI 观察和 AnswerBundle，不把预算结束称为全面覆盖。

引用真实性与支持判定分开。当前是明确标注的保守文字规则：supported/contradicted/insufficient，检查原文、实体、否定/不确定表述及未知人群条件。结果不是完整语义推理或医学验证。证据抽屉可核对摘录，接口为 `POST /v1/evidence/assess-claim`。

KEGG 的 P 是 precaution，缺少进一步依据时严重程度保持 unknown，并沿用现有 unknown 升级保护；没有将其定义为新的临床等级。依据：[KEGG API 官方手册](https://www.genome.jp/kegg/rest/keggapi.html)。

显式来源替代接口 `POST /v1/evidence/source-replacements` 要求 ops 角色、同一来源 URI、已知版本、谱系与替代依据，保存 old/new evidence 关系并使实际依赖该旧版本的结论失效；只修改哈希不会自动替代。历史证据保留可读。

`quality-ablation.json` 是 10 条合成文字反例的基线/重排/支持检查/组合比较，记录实测本地耗时与零模型调用。它不评价真实模型，也不证明检索排序或临床质量提升；独立数据仍 unavailable。统一验收重新生成该报告。
