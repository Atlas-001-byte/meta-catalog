"""公开门面：注册、比较、影响分析、报告读取与检索。

所有成功结果只通过方法返回；报告保存在进程内只增存储中，不规定
落盘格式。注册、比较、读取和检索均不会改写既有 Schema 注册内容、
版本关系或资产依赖关系。
"""

from __future__ import annotations

from typing import Any

from .comparison import (
    ChangeKind,
    Compatibility,
    RenameLineage,
    compare_records,
    build_ephemeral_record,
)
from .errors import CatalogError, ErrorCode, SchemaComparisonInvalid
from .impact import ImpactAnalyzer
from .registry import AssetReference, Registry
from .search import AssetSearchIndex, ChangeSearchIndex

__all__ = ["Catalog", "ChangeKind", "Compatibility", "AssetReference"]


class Catalog:
    def __init__(self) -> None:
        self._registry = Registry()
        self._lineage = RenameLineage()
        self._asset_search = AssetSearchIndex(self._registry)
        self._change_search = ChangeSearchIndex()

    # ------------------------------------------------------------------
    # 注册（基线能力，注册后不可变）
    # ------------------------------------------------------------------

    def register_schema(self, name: str, version: str, document: Any) -> dict[str, Any]:
        """注册一个 Schema 版本。

        ``document`` 为 dict 或 JSON 字符串；非法时抛
        :class:`CatalogError`（``SchemaInvalid``）。
        """
        record = self._registry.register_schema(name, version, document)
        return {
            "schema": record.name,
            "version": record.version,
            "field_count": len(record.fields),
            "references": [
                {
                    "field_path": e.source_field_path,
                    "target_schema": e.target_name,
                    "target_version": e.target_version,
                    "target_pointer": e.target_pointer,
                }
                for e in record.edges
            ],
        }

    def register_asset(
        self,
        asset_id: str,
        name: str,
        asset_type: str,
        references: list[dict[str, Any]] | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        """注册目录资产。

        引用形式：``{"schema": 名称, "version": 版本(可选),
        "field": 字段路径(可选, 省略表示整个 Schema)}``。
        """
        asset = self._registry.register_asset(
            asset_id, name, asset_type, references, description
        )
        return {
            "asset_id": asset.asset_id,
            "name": asset.name,
            "type": asset.asset_type,
            "references": [
                {
                    "schema": r.schema_name,
                    "version": r.version,
                    "field_path": r.field_path or "/",
                }
                for r in asset.references
            ],
        }

    # ------------------------------------------------------------------
    # 影响分析（基线能力）
    # ------------------------------------------------------------------

    def impact_analysis(self, schema: str, version: str, field_paths: list[str] | None = None) -> dict[str, Any]:
        """返回直接引用与经其他 Schema 传递引用的资产（稳定顺序、路径去重）。"""
        record = self._registry.get_schema(schema, version)
        paths = sorted(set(field_paths) if field_paths else {""})
        analyzer = ImpactAnalyzer(self._registry)
        result = analyzer.analyze(record.name, record.version, paths)
        return {
            "schema": record.name,
            "version": record.version,
            "fields": [
                {"field_path": path or "/", "impacted_assets": [a.to_dict() for a in result[path]]}
                for path in paths
            ],
        }

    # ------------------------------------------------------------------
    # 字段级比较（本次新增）
    # ------------------------------------------------------------------

    def compare_schemas(
        self,
        name: str,
        baseline_version: str,
        candidate_version: str,
        candidate_document: Any = None,
        renames: list[Any] | None = None,
    ) -> dict[str, Any]:
        """比较两个 Schema 版本并生成字段级变更报告。

        - ``candidate_document`` 为 None 时，``candidate_version`` 必须是
          已注册版本；否则以内联文档作为候选（不落盘、不改变注册内容）。
        - 候选不是合法 JSON Schema、版本不存在、rename 起点/终点不存在、
          路径重复映射、rename 跨版本不一致，统一抛
          :class:`SchemaComparisonInvalid`。
        - 分析对象或影响链超过公开限制时抛
          :class:`ImpactAnalysisTooLarge`。

        相同输入（含相同 rename 映射顺序无关）产生字段顺序、影响顺序、
        兼容结论与报告 ID 均一致的报告。
        """
        try:
            baseline = self._registry.get_schema(name, baseline_version)
        except CatalogError as exc:
            raise SchemaComparisonInvalid(str(exc)) from exc

        if candidate_document is None:
            try:
                candidate = self._registry.get_schema(name, candidate_version)
            except CatalogError as exc:
                raise SchemaComparisonInvalid(str(exc)) from exc
        else:
            candidate = build_ephemeral_record(name, candidate_version, candidate_document)

        report = compare_records(
            self._registry, baseline, candidate, renames, self._lineage
        )
        # 只有成功报告才进入跨版本 rename 血统与检索索引
        self._lineage.record(
            baseline.version, candidate.version,
            [(p[0], p[1]) for p in report["renames"]],
        )
        self._change_search.add(report)
        return report

    def get_report(self, report_id: str) -> dict[str, Any]:
        report = self._change_search.get(report_id)
        if report is None:
            raise CatalogError(f"change report not found: {report_id}", code=ErrorCode.SCHEMA_NOT_FOUND)
        return report

    def list_reports(self) -> list[dict[str, Any]]:
        """报告元数据列表（不含完整变更明细）。"""
        return [
            {
                "report_id": r["report_id"],
                "schema": r["schema"],
                "baseline_version": r["baseline_version"],
                "candidate_version": r["candidate_version"],
                "change_count": len(r["changes"]),
            }
            for r in self._change_search.list_reports()
        ]

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def search_assets(self, query: str = "", *, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        """资产全文检索（基线口径保持不变：关键词 AND、稳定排序）。"""
        return self._asset_search.search(query, limit=limit, offset=offset)

    def search_changes(
        self,
        *,
        schema: str | None = None,
        version: str | None = None,
        field_path: str | None = None,
        kind: str | None = None,
        compatibility: str | None = None,
        impacted_asset: str | None = None,
        query: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """变更报告组合检索（条件之间 AND）。

        可按 Schema 名称、版本、字段路径、变更种类（:class:`ChangeKind`
        的值）、兼容结论（:class:`Compatibility` 的值）与影响资产名称
        组合；``query`` 提供与资产检索同口径的自由关键词。
        返回命中字段（``matched_fields``）与命中资产摘要
        （``matched_assets``）。
        """
        try:
            return self._change_search.search(
                schema=schema,
                version=version,
                field_path=field_path,
                kind=kind,
                compatibility=compatibility,
                impacted_asset=impacted_asset,
                query=query,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise SchemaComparisonInvalid(str(exc)) from exc
