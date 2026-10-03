"""元数据目录与检索服务。

公开接口见 :mod:`meta_catalog.catalog`。
"""

from meta_catalog.errors import (
    SchemaComparisonInvalid,
    ImpactAnalysisTooLarge,
    CatalogError,
    ErrorCode,
)
from meta_catalog.limits import LIMITS
from meta_catalog.catalog import Catalog, ChangeKind, Compatibility

__all__ = [
    "Catalog",
    "ChangeKind",
    "Compatibility",
    "SchemaComparisonInvalid",
    "ImpactAnalysisTooLarge",
    "CatalogError",
    "ErrorCode",
    "LIMITS",
]
