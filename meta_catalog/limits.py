"""公开限制。

注册、比较、影响分析均受这些上限约束；超限返回
``ImpactAnalysisTooLarge``（比较/影响场景）或 ``LimitExceeded``。
"""

LIMITS = {
    # 单个 Schema 文档扁平化后的字段路径上限（分析对象规模）。
    "max_fields_per_schema": 10_000,
    # 单次比较允许涉及的字段路径总数（新旧两份去重后）。
    "max_fields_per_comparison": 20_000,
    # 影响分析中 Schema 间传递引用的最大跳数（影响链长度）。
    "max_impact_depth": 50,
    # 单次影响分析返回的影响资产数量上限。
    "max_impacted_assets": 5_000,
    # 单个 Schema 文档的原始字节数上限。
    "max_schema_bytes": 4 * 1024 * 1024,
}
