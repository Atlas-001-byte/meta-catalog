"""元数据目录与检索服务。

对外公开接口见 :class:`meta_catalog.catalog.MetaCatalog` 与
:mod:`meta_catalog.errors` 中的错误码。
"""

from .catalog import MetaCatalog
from .errors import (
    CatalogError,
    SchemaComparisonInvalid,
    ImpactAnalysisTooLarge,
    AlreadyExistsError,
    NotFoundError,
    SearchQueryInvalid,
)

__all__ = [
    "MetaCatalog",
    "CatalogError",
    "SchemaComparisonInvalid",
    "ImpactAnalysisTooLarge",
    "AlreadyExistsError",
    "NotFoundError",
    "SearchQueryInvalid",
]
