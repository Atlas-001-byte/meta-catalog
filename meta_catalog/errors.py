"""错误类型与公开错误码。"""


class CatalogError(Exception):
    """所有目录服务错误的基类。

    :ivar code: 稳定的公开错误码字符串。
    """

    code = "CatalogError"

    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(CatalogError):
    """指定的 Schema、版本、资产或报告不存在。"""

    code = "NotFound"


class AlreadyExistsError(CatalogError):
    """同名资源已注册（注册内容不可变，不支持覆盖）。"""

    code = "AlreadyExists"


class SchemaComparisonInvalid(CatalogError):
    """字段级比较请求不合法。

    覆盖以下情形：候选文档不是合法 JSON Schema、指定版本不存在、
    重命名起点或终点不存在、同一路径被重复映射、重命名映射跨版本不一致。
    """

    code = "SchemaComparisonInvalid"


class ImpactAnalysisInvalid(CatalogError):
    """批量影响分析请求不合法。

    覆盖以下情形：请求不是字典、``paths`` 不是列表或含非字符串、
    资产标识列表不是列表或含非字符串 / 空字符串、``mode`` 不是
    ``all`` 或 ``any``、路径不是合法逻辑 JSONPointer、去重后的字段
    集合为空。
    """

    code = "ImpactAnalysisInvalid"


class ImpactAnalysisTooLarge(CatalogError):
    """分析对象数量或影响链长度/数量超过公开限制。"""

    code = "ImpactAnalysisTooLarge"


class FieldTraceInvalid(CatalogError):
    """字段血缘追溯请求不合法。

    覆盖以下情形：``name`` / ``baseline_version`` / ``target_version`` 不是
    非空字符串，``path`` 不是字符串或不是合法的逻辑 JSON Pointer（非 ``""``
    根路径且不以 ``/`` 开头）。
    """

    code = "FieldTraceInvalid"


class FieldTraceAmbiguous(CatalogError):
    """字段血缘路线存在冲突，无法给出唯一步进。

    覆盖以下情形：相邻版本间存在多份互相冲突的比较报告对同一旧字段给出不同
    新路径，或显式重命名传播对同一字段给出多个新路径；``details`` 列出
    冲突的 ``report_id`` 与路径。
    """

    code = "FieldTraceAmbiguous"


class SearchQueryInvalid(CatalogError):
    """分页检索请求不合法。

    覆盖以下情形：``page_size`` 不是 1 到 200 的普通整数、``keyword`` 或
    过滤值不是字符串或 None、出现未公开的关键字参数（如 ``limit``）、
    ``cursor`` 缺失内容、格式非法、来源未知或与当前查询条件 / ``page_size``
    不匹配。
    """

    code = "SearchQueryInvalid"
