"""公开门面：:class:`MetaCatalog`。

组合注册簿、Schema 校验、字段级比较、影响分析与全文检索。所有成功结果都以
普通 dict/list 返回（可直接 JSON 序列化）；注册、比较、读取与检索均不会改动
既有 Schema 的注册内容、版本关系与资产依赖关系。
"""

from __future__ import annotations

import copy
from typing import Any

from . import compare as compare_mod
from . import schema_fields as sf
from .errors import NotFoundError, SchemaComparisonInvalid
from .impact import analyze_field
from .registry import FieldRef, Registry
from .search import SearchIndex
from .validator import validate_schema

# 升级影响汇总中无任何命中资产的状态（严重顺序低于 metadata）。
UNAFFECTED = "unaffected"


class MetaCatalog:
    def __init__(self) -> None:
        self._registry = Registry()
        self._index = SearchIndex()
        self._reports: dict[str, dict[str, Any]] = {}
        self._indexed_reports: set[str] = set()

    # ============================================================ 注册：Schema
    def register_schema(self, name: str, version: str, document: Any) -> dict[str, Any]:
        """注册一个不可变的 Schema 版本。文档不合法时抛 SchemaComparisonInvalid。"""
        validate_schema(document)
        record = self._registry.register_schema(name, version, document)
        self._index_schema(record.name, record.version, record.document, record.title)
        return self.get_schema(name, version)

    def get_schema(self, name: str, version: str) -> dict[str, Any]:
        record = self._registry.get_schema(name, version)
        return {
            "name": record.name,
            "version": record.version,
            "title": record.title,
            "document": copy.deepcopy(record.document),
        }

    def list_versions(self, name: str) -> list[str]:
        return self._registry.list_versions(name)

    # ============================================================== 注册：资产
    def register_asset(
        self,
        asset_id: str,
        name: str,
        kind: str,
        refs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """注册目录资产。

        ``refs`` 为 ``[{"schema": 名, "version": 版本, "path": 字段指针}, ...]``，
        表示资产对字段的直接引用；路径可写文档指针（``/properties/a``）或
        逻辑字段路径（``/a``），注册时统一规整。
        """
        asset = self._registry.register_asset(asset_id, name, kind, refs or [])
        self._index_asset(asset.id, asset.name, asset.kind, list(asset.refs))
        return self._asset_view(asset)

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        return self._asset_view(self._registry.get_asset(asset_id))

    @staticmethod
    def _asset_view(asset) -> dict[str, Any]:
        return {
            "asset_id": asset.id,
            "name": asset.name,
            "kind": asset.kind,
            "refs": [
                {"schema": r.schema, "version": r.version, "path": r.path}
                for r in asset.refs
            ],
        }

    # ================================================================ 影响分析
    def analyze_impact(self, name: str, version: str, path: str) -> dict[str, Any]:
        """字段级影响分析：返回直接引用资产与经其他 Schema 传递引用的资产。"""
        return analyze_field(self._registry, name, version, path)

    # ======================================================== 字段级变更识别
    def compare_schemas(
        self,
        name: str,
        baseline_version: str,
        candidate: Any = None,
        *,
        candidate_version: str | None = None,
        renames: list[dict[str, str]] | list[list[str]] | None = None,
    ) -> dict[str, Any]:
        """比较基线版本与候选文档，生成并入库可检索的字段级变更报告。

        ``candidate`` 直接给出候选 JSON Schema 文档；或省略它、仅传
        ``candidate_version`` 以引用同 Schema 标识下已注册的候选版本。
        ``renames`` 为 ``[{"from": 旧路径, "to": 新路径}, ...]``。

        任何不合法情形统一抛 :class:`SchemaComparisonInvalid`。
        相同输入返回相同报告（幂等，不会重复入库）。
        """
        report = self._build_comparison(
            name, baseline_version, candidate, candidate_version, renames
        )

        # 幂等入库并进入全文检索范围。
        report_id = report["report_id"]
        if report_id not in self._reports:
            self._reports[report_id] = report
        if report_id not in self._indexed_reports:
            for i, change in enumerate(report["changes"]):
                self._index_change(report, change, i)
            self._indexed_reports.add(report_id)
        return copy.deepcopy(report)

    def _build_comparison(
        self,
        name: str,
        baseline_version: str,
        candidate: Any,
        candidate_version: str | None,
        renames: list[dict[str, str]] | list[list[str]] | None,
    ) -> dict[str, Any]:
        """按 ``compare_schemas`` 语义解析候选并构造变更报告。

        只读取注册内容，不落库、不入索引；候选非法、版本不存在或重命名
        不合法时抛 :class:`SchemaComparisonInvalid`，超过公开限制时抛
        :class:`ImpactAnalysisTooLarge`。
        """
        # 1) 解析候选文档。
        if candidate is None:
            if candidate_version is None:
                raise SchemaComparisonInvalid(
                    "必须提供候选文档 candidate 或已注册的 candidate_version",
                    details={"reason": "candidate_missing"},
                )
            try:
                candidate_record = self._registry.get_schema(name, candidate_version)
            except NotFoundError as exc:
                raise SchemaComparisonInvalid(
                    f"指定版本 {name}@{candidate_version} 不存在",
                    details={"reason": "version_not_found", **exc.details},
                ) from exc
            candidate_document = copy.deepcopy(candidate_record.document)
            label = candidate_version
        else:
            validate_schema(candidate)
            candidate_document = copy.deepcopy(candidate)
            label = candidate_version  # 允许调用方给候选文档附带版本标签

        # 2) 基线版本必须存在。
        if not self._registry.has_schema(name, baseline_version):
            raise SchemaComparisonInvalid(
                f"指定版本 {name}@{baseline_version} 不存在",
                details={"reason": "version_not_found", "schema": name, "version": baseline_version},
            )

        # 3) 生成报告（字段数/变更数限制与重命名校验在其中完成）。
        return compare_mod.build_report(
            self._registry,
            name,
            baseline_version,
            candidate_document,
            label,
            renames,
        )

    # ============================================================ 升级影响汇总
    def analyze_upgrade_impact(
        self,
        name: str,
        baseline_version: str,
        candidate: Any = None,
        *,
        candidate_version: str | None = None,
        renames: list[dict[str, str]] | list[list[str]] | None = None,
        asset_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """升级影响汇总：按资产汇总一次 Schema 升级的命中情况。

        候选文档、候选版本、重命名映射与字段路径语义与
        :meth:`compare_schemas` 完全一致，``report_id`` 也与同输入的
        ``compare_schemas`` 相同；区别在于本调用不生成、不改写报告与检索
        索引，也不改动任何注册内容。

        ``asset_ids`` 为 ``None`` 时覆盖全部资产；指定时去重且与顺序无关，
        只汇总所列资产，未知资产抛 :class:`NotFoundError`。每条命中在保留
        变更报告全部字段的基础上增加 ``impact_kind``：``direct``（直接
        命中）、``transitive``（传递命中）或 ``both``（两者都命中）。
        """
        # 1) 沿用 compare_schemas 的解析、校验、限制与报告构造（不落库）。
        report = self._build_comparison(
            name, baseline_version, candidate, candidate_version, renames
        )

        # 2) 入选资产：None 覆盖全部；指定时去重，未知资产抛 NotFoundError。
        if asset_ids is None:
            selected = self._registry.all_assets()
        else:
            selected = [self._registry.get_asset(aid) for aid in sorted(set(asset_ids))]

        # 3) 以变更报告中既有的直接/传递影响资产集合为口径逐变更汇总。
        buckets: dict[str, dict[str, Any]] = {
            a.id: {
                "asset_id": a.id,
                "name": a.name,
                "kind": a.kind,
                "status": UNAFFECTED,
                "changes": [],
            }
            for a in selected
        }
        selected_set = set(buckets)
        hit_paths: set[str] = set()

        for change in report["changes"]:
            direct_ids = {
                x["asset_id"] for x in change["direct_assets"]
            } & selected_set
            transitive_ids = {
                x["asset_id"] for x in change["transitive_assets"]
            } & selected_set
            hit_ids = direct_ids | transitive_ids
            if not hit_ids:
                continue
            hit_paths.add(change["path"])
            for aid in hit_ids:
                if aid in direct_ids and aid in transitive_ids:
                    impact_kind = "both"
                elif aid in direct_ids:
                    impact_kind = "direct"
                else:
                    impact_kind = "transitive"
                # 同一资产对同一条报告变更只产生一条记录；报告变更已稳定
                # 排序，按报告顺序追加即保持既有稳定排序。
                entry = copy.deepcopy(change)
                entry["impact_kind"] = impact_kind
                buckets[aid]["changes"].append(entry)

        # 4) 资产状态取命中变更兼容结论的最严重者。
        severity = {
            compare_mod.BREAKING: 3,
            compare_mod.COMPATIBLE: 2,
            compare_mod.METADATA_COMPAT: 1,
        }
        counts = {
            compare_mod.BREAKING: 0,
            compare_mod.COMPATIBLE: 0,
            compare_mod.METADATA_COMPAT: 0,
            UNAFFECTED: 0,
        }
        for bucket in buckets.values():
            status = UNAFFECTED
            best = 0
            for ch in bucket["changes"]:
                rank = severity[ch["compatibility"]]
                if rank > best:
                    best, status = rank, ch["compatibility"]
            bucket["status"] = status
            counts[status] += 1

        return {
            "report_id": report["report_id"],
            "schema": report["schema"],
            "baseline_version": report["baseline_version"],
            "candidate_version": report["candidate_version"],
            "summary": {
                "asset_total": len(buckets),
                "breaking_assets": counts[compare_mod.BREAKING],
                "compatible_assets": counts[compare_mod.COMPATIBLE],
                "metadata_assets": counts[compare_mod.METADATA_COMPAT],
                "unaffected_assets": counts[UNAFFECTED],
                "changed_paths": len(hit_paths),
            },
            "assets": [buckets[aid] for aid in sorted(buckets)],
        }

    def get_report(self, report_id: str) -> dict[str, Any]:
        if report_id not in self._reports:
            raise NotFoundError(
                f"变更报告 {report_id} 不存在", details={"report_id": report_id}
            )
        return copy.deepcopy(self._reports[report_id])

    def list_reports(
        self, name: str | None = None, version: str | None = None
    ) -> list[dict[str, Any]]:
        """列出报告摘要（可按 Schema 名称与版本过滤，版本匹配基线或候选）。"""
        out = []
        for rid in sorted(self._reports):
            r = self._reports[rid]
            if name is not None and r["schema"] != name:
                continue
            if version is not None and version not in (
                r["baseline_version"],
                r["candidate_version"],
            ):
                continue
            out.append(
                {
                    "report_id": r["report_id"],
                    "schema": r["schema"],
                    "baseline_version": r["baseline_version"],
                    "candidate_version": r["candidate_version"],
                    "summary": r["summary"],
                }
            )
        return out

    # ================================================================== 检索
    def search(self, keyword: str | None = None, **filters: Any) -> list[dict[str, Any]]:
        """组合检索 Schema / 资产 / 字段变更。

        可选过滤：``schema``、``version``、``field_path``、``change_kind``、
        ``compatibility``、``asset_name``、``doc_type``、``limit``。
        返回命中条目（含命中字段与命中资产摘要），排序稳定，与插入顺序无关。
        """
        return self._index.search(keyword, **filters)

    # ------------------------------------------------------------ 索引维护
    def _index_schema(self, name: str, version: str, document: Any, title: str | None) -> None:
        fields = sf.expand(document)
        root = fields.get("")
        description = ""
        if root is not None:
            d = root.schema.get("description")
            description = d if isinstance(d, str) else ""
        text_fields = {
            "name": name,
            "version": version,
            "title": title or "",
            "description": description,
            "field_paths": "\n".join(sorted(fields)),
        }
        payload = {
            "name": name,
            "version": version,
            "title": title,
            "summary": {
                "name": name,
                "version": version,
                "title": title,
                "field_count": len(fields),
            },
        }
        self._index.add_doc(
            "schema", ("schema", name, version), text_fields, payload
        )

    def _index_asset(self, asset_id: str, name: str, kind: str, refs: list[FieldRef]) -> None:
        schemas = sorted({r.schema for r in refs})
        versions = sorted({f"{r.schema}@{r.version}" for r in refs})
        text_fields = {
            "id": asset_id,
            "name": name,
            "kind": kind,
            "refs": "\n".join(f"{r.schema}@{r.version}{r.path}" for r in refs),
        }
        payload = {
            "id": asset_id,
            "name": name,
            "kind": kind,
            "schemas": schemas,
            "versions": versions,
            "summary": {
                "asset_id": asset_id,
                "name": name,
                "kind": kind,
                "ref_count": len(refs),
            },
        }
        self._index.add_doc("asset", ("asset", asset_id), text_fields, payload)

    def _index_change(self, report: dict[str, Any], change: dict[str, Any], seq: int) -> None:
        direct = change["direct_assets"]
        transitive = change["transitive_assets"]
        impacted: dict[str, str] = {}
        for a in direct:
            impacted[a["asset_id"]] = a["name"]
        for a in transitive:
            impacted.setdefault(a["asset_id"], a["name"])

        asset_text = "\n".join(sorted(impacted.values()))
        asset_id_text = "\n".join(sorted(impacted))
        summary_text = _summary_text(change["old_summary"]) + "\n" + _summary_text(
            change["new_summary"]
        )
        text_fields = {
            "schema": report["schema"],
            "version": " ".join(
                v for v in (report["baseline_version"], report["candidate_version"]) if v
            ),
            "field_path": "\n".join(
                p for p in (change["old_path"], change["new_path"], change["path"]) if p
            ),
            "change_kind": change["change_kind"],
            "compatibility": change["compatibility"],
            "impact_assets": asset_text,
            "impact_asset_ids": asset_id_text,
            "summary": summary_text,
        }
        key = (
            "change",
            report["schema"],
            report["baseline_version"],
            report["candidate_version"] or "",
            report["report_id"],
            change["path"],
            change["old_path"] or "",
            change["change_kind"],
            seq,
        )
        payload = {
            "report_id": report["report_id"],
            "schema": report["schema"],
            "baseline_version": report["baseline_version"],
            "candidate_version": report["candidate_version"],
            "path": change["path"],
            "old_path": change["old_path"],
            "new_path": change["new_path"],
            "change_kind": change["change_kind"],
            "compatibility": change["compatibility"],
            "old_summary": change["old_summary"],
            "new_summary": change["new_summary"],
            "matched_assets": [
                {"asset_id": aid, "name": impacted[aid]} for aid in sorted(impacted)
            ],
            "direct_assets": direct,
            "transitive_assets": transitive,
            "summary": {
                "report_id": report["report_id"],
                "schema": report["schema"],
                "path": change["path"],
                "change_kind": change["change_kind"],
                "compatibility": change["compatibility"],
                "assets": [
                    {"asset_id": aid, "name": impacted[aid]} for aid in sorted(impacted)
                ],
            },
        }
        self._index.add_doc("change", key, text_fields, payload)


def _summary_text(summary: dict[str, Any] | None) -> str:
    if not summary:
        return ""
    parts: list[str] = []
    for key, value in summary.items():
        parts.append(f"{key}={_flatten(value)}")
    return "\n".join(parts)


def _flatten(value: Any) -> str:
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)
