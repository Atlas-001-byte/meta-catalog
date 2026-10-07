"""公开限制常量。

这些限制是公开行为契约的一部分：触发上限时统一抛出
:class:`meta_catalog.errors.ImpactAnalysisTooLarge`。
"""

# 单次比较允许展开的字段节点数量上限（分析对象规模）。
MAX_FIELDS = 100_000

# 单次比较报告中允许的变更条目数量上限。
MAX_CHANGES = 10_000

# 单个字段做影响分析时允许遍历的跨 Schema 引用链最大深度。
MAX_IMPACT_DEPTH = 32

# 单个字段做影响分析时允许访问的跨 Schema 引用边数量上限。
MAX_IMPACT_VISITED = 5_000

# 单条变更允许关联的影响资产数量上限（直接 + 传递合计）。
MAX_IMPACT_ASSETS = 10_000

# 单次 register_batch 允许登记的资源总数上限。
MAX_BATCH_RESOURCES = 1_000
