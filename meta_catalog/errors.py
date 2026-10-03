"""公开错误码与异常类型。"""

from enum import Enum


class ErrorCode(str, Enum):
    """对外公开的错误码。"""

    SCHEMA_INVALID = "SchemaInvalid"
    SCHEMA_NOT_FOUND = "SchemaNotFound"
    VERSION_NOT_FOUND = "VersionNotFound"
    SCHEMA_COMPARISON_INVALID = "SchemaComparisonInvalid"
    IMPACT_ANALYSIS_TOO_LARGE = "ImpactAnalysisTooLarge"
    LIMIT_EXCEEDED = "LimitExceeded"
    ALREADY_EXISTS = "AlreadyExists"


class CatalogError(Exception):
    """所有公开错误的基类，携带稳定错误码。"""

    code: ErrorCode = ErrorCode.SCHEMA_INVALID

    def __init__(self, message: str, *, code: ErrorCode | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code

    @property
    def error_code(self) -> str:
        return self.code.value


class SchemaComparisonInvalid(CatalogError):
    """比较输入不合法：候选不是合法 JSON Schema、版本不存在、重命名映射非法等。"""

    code = ErrorCode.SCHEMA_COMPARISON_INVALID


class ImpactAnalysisTooLarge(CatalogError):
    """分析对象（字段数）或影响链（传递深度/资产数）超过公开限制。"""

    code = ErrorCode.IMPACT_ANALYSIS_TOO_LARGE
