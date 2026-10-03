"""Schema / 资产注册表（不可变存储）。

注册后内容不会被比较、影响分析或检索修改；所有读取返回深拷贝或
只读视图。版本之间不建立可变关系，跨 Schema 引用以文档中声明的
``catalog://`` 形式保存为依赖边。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field as dc_field
from typing import Any

from .errors import CatalogError, ErrorCode
from .json_schema import (
    FieldConstraints,
    SchemaShapeError,
    canonical_json,
    prepare,
)


def version_sort_key(version: str) -> tuple[Any, ...]:
    """版本排序：尽量按点分数字段比较，无法解析的尾部按字典序。"""
    parts: list[Any] = []
    for chunk in version.split("."):
        try:
            parts.append((0, int(chunk)))
        except ValueError:
            parts.append((1, chunk))
    return tuple(parts)


@dataclass(frozen=True)
class SchemaEdge:
    """一条从某 Schema 字段指向另一 Schema 的引用边。"""

    source_name: str
    source_version: str
    source_field_path: str
    target_name: str
    target_version: str | None  # None 表示声明时未锁定版本
    target_pointer: str


@dataclass
class SchemaRecord:
    name: str
    version: str
    doc: dict[str, Any]
    fields: dict[str, FieldConstraints]
    canonical: str
    edges: list[SchemaEdge] = dc_field(default_factory=list)


@dataclass(frozen=True)
class AssetReference:
    """资产对某 Schema 字段的直接引用。"""

    schema_name: str
    version: str | None
    field_path: str  # "" 表示整个 Schema（根）


@dataclass
class Asset:
    asset_id: str
    name: str
    asset_type: str
    references: tuple[AssetReference, ...]
    description: str = ""


class Registry:
    def __init__(self) -> None:
        self._schemas: dict[str, dict[str, SchemaRecord]] = {}
        self._assets: dict[str, Asset] = {}
        # target_name -> 反向边（在源版本注册时建立，之后不变）
        self._rev_edges: dict[str, list[SchemaEdge]] = {}

    # ------------------------------------------------------------------
    # Schema 注册
    # ------------------------------------------------------------------

    def register_schema(self, name: str, version: str, raw: Any) -> SchemaRecord:
        if not isinstance(name, str) or not name:
            raise CatalogError("schema name must be a non-empty string", code=ErrorCode.SCHEMA_INVALID)
        if not isinstance(version, str) or not version:
            raise CatalogError("schema version must be a non-empty string", code=ErrorCode.SCHEMA_INVALID)
        try:
            doc, fields, _canonical = prepare(raw)
        except SchemaShapeError as exc:
            raise CatalogError(str(exc), code=ErrorCode.SCHEMA_INVALID) from exc

        versions = self._schemas.setdefault(name, {})
        if version in versions:
            raise CatalogError(
                f"schema {name}@{version} already registered",
                code=ErrorCode.ALREADY_EXISTS,
            )

        edges: list[SchemaEdge] = []
        for path, fc in fields.items():
            for target_name, target_version, pointer in sorted(fc.external_refs):
                edges.append(
                    SchemaEdge(
                        source_name=name,
                        source_version=version,
                        source_field_path=path,
                        target_name=target_name,
                        target_version=target_version,
                        target_pointer=pointer,
                    )
                )
        edges.sort(key=lambda e: (e.source_field_path, e.target_name, e.target_version or "", e.target_pointer))

        record = SchemaRecord(
            name=name,
            version=version,
            doc=copy.deepcopy(doc),
            fields=fields,
            canonical=canonical_json(doc),
            edges=edges,
        )
        versions[version] = record
        for edge in edges:
            bucket = self._rev_edges.setdefault(edge.target_name, [])
            if edge not in bucket:
                bucket.append(edge)
        return record

    def has_schema(self, name: str, version: str) -> bool:
        return name in self._schemas and version in self._schemas[name]

    def get_schema(self, name: str, version: str) -> SchemaRecord:
        if name not in self._schemas:
            raise CatalogError(f"schema not found: {name}", code=ErrorCode.SCHEMA_NOT_FOUND)
        if version not in self._schemas[name]:
            raise CatalogError(
                f"version not found: {name}@{version}",
                code=ErrorCode.VERSION_NOT_FOUND,
            )
        return self._schemas[name][version]

    def latest_version(self, name: str) -> str | None:
        versions = self._schemas.get(name)
        if not versions:
            return None
        return sorted(versions, key=version_sort_key)[-1]

    def resolve_version(self, name: str, version: str | None) -> str | None:
        """把未锁定版本的引用解析为当前最新版本；目标不存在返回 None。"""
        if version is not None:
            return version if self.has_schema(name, version) else None
        return self.latest_version(name)

    def list_schema_names(self) -> list[str]:
        return sorted(self._schemas)

    def list_versions(self, name: str) -> list[str]:
        if name not in self._schemas:
            raise CatalogError(f"schema not found: {name}", code=ErrorCode.SCHEMA_NOT_FOUND)
        return sorted(self._schemas[name], key=version_sort_key)

    def reverse_edges(self, target_name: str) -> list[SchemaEdge]:
        """所有声明引用目标 Schema 的边（目标版本在遍历时解析）。"""
        return sorted(
            self._rev_edges.get(target_name, []),
            key=lambda e: (
                e.source_name,
                version_sort_key(e.source_version),
                e.source_field_path,
                e.target_version or "",
                e.target_pointer,
            ),
        )

    # ------------------------------------------------------------------
    # 资产注册
    # ------------------------------------------------------------------

    def register_asset(
        self,
        asset_id: str,
        name: str,
        asset_type: str,
        references: list[AssetReference | dict[str, Any]] | None = None,
        description: str = "",
    ) -> Asset:
        if not isinstance(asset_id, str) or not asset_id:
            raise CatalogError("asset_id must be a non-empty string", code=ErrorCode.SCHEMA_INVALID)
        if asset_id in self._assets:
            raise CatalogError(f"asset already exists: {asset_id}", code=ErrorCode.ALREADY_EXISTS)
        refs: list[AssetReference] = []
        for raw_ref in references or []:
            if isinstance(raw_ref, AssetReference):
                ref = raw_ref
            else:
                schema_name = raw_ref.get("schema")
                if not isinstance(schema_name, str) or not schema_name:
                    raise CatalogError("asset reference requires schema name", code=ErrorCode.SCHEMA_INVALID)
                version = raw_ref.get("version")
                if version is not None and not isinstance(version, str):
                    raise CatalogError("asset reference version must be string", code=ErrorCode.SCHEMA_INVALID)
                field_path = raw_ref.get("field", "")
                if not isinstance(field_path, str):
                    raise CatalogError("asset reference field must be string", code=ErrorCode.SCHEMA_INVALID)
                if field_path and not field_path.startswith("/"):
                    field_path = "/" + field_path
                ref = AssetReference(schema_name, version, field_path)
            refs.append(ref)
        refs.sort(key=lambda r: (r.schema_name, r.version or "", r.field_path))
        asset = Asset(
            asset_id=asset_id,
            name=name,
            asset_type=asset_type,
            references=tuple(refs),
            description=description,
        )
        self._assets[asset_id] = asset
        return asset

    def get_asset(self, asset_id: str) -> Asset:
        if asset_id not in self._assets:
            raise CatalogError(f"asset not found: {asset_id}", code=ErrorCode.SCHEMA_NOT_FOUND)
        return self._assets[asset_id]

    def list_assets(self) -> list[Asset]:
        return [self._assets[k] for k in sorted(self._assets)]

    def assets_referencing(self, name: str, version: str, paths: set[str]) -> list[Asset]:
        """稳定顺序返回在给定字段路径集合（含子树重叠）上直接引用
        ``name@version`` 的资产。未锁定版本的引用解析为当前最新版本
        且解析结果等于 ``version`` 时命中。
        """
        matched: set[str] = set()
        for asset in self._assets.values():
            for ref in asset.references:
                if ref.schema_name != name:
                    continue
                resolved = self.resolve_version(name, ref.version)
                if resolved != version:
                    continue
                if any(_paths_overlap(ref.field_path, p) for p in paths):
                    matched.add(asset.asset_id)
                    break
        return [self._assets[i] for i in sorted(matched)]


def _paths_overlap(a: str, b: str) -> bool:
    """两个字段路径是否构成同一子树（根 '' 与任何路径重叠）。"""
    if a == "" or b == "" or a == b:
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")
