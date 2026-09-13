/** Only public progress wording belongs in product UI; metadata stays diagnostic. */
export const retrievalProgress: Record<string, string> = {
  invalid_filter: '正在核对检索条件，本次尚未搜索',
  invalid_query: '正在核对查询参数，本次尚未搜索',
  empty_filter_scope: '所选范围没有可供核查的材料',
  no_match: '已搜索，暂未找到匹配依据',
  retrieval_error: '检索执行失败，已有结果保留',
  found: '已找到依据，正在核对原文',
  catalog: '已读取可核查材料的目录',
};
